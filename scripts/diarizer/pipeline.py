"""Batch and long-recording workflows, retries and progress statistics."""

from collections import deque
from datetime import datetime
import os
import sys
import tempfile
import threading
import time

from .agy import agy_forget, extract_envelope_error_and_usage, run_agy
from .archive import append_manifest, load_refs, publish_md, register_catalog_entries, sha256_file
from .checkpoints import LongCheckpoint
from .audio import prepare_audio_work, probe_duration_ms
from .config import (
    BACKEND_RETRY_BASE_S, BACKEND_RETRY_MAX_S, NEEDS_RECOVERY, NO_RECOVERABLE_AUDIO, PART_REPAIR_ATTEMPTS,
    REFUSAL_TEXT_PAT, SAFETY_ERR_PAT, TZ,
)
from .models import ModelAttempts, ModelCooldowns
from .errors import MediaInputError, ModelRefused, ModelsUnavailable, ModelTaskFailed, classify_error, normalize_error
from .prompts import build_batch_prompt, build_part_prompt
from .runtime import FORCE_STOP_EVENT, STOP_EVENT
from .console import completed, emit
from .diagnostics import record
from .catalog_fields import response_summary
from .prompt_files import load_prompt
from .transcript import (
    format_ts,
    parse_item_blocks,
    parse_speakers_and_lines,
    validate_and_render_md,
)


def capture_summary(item, raw):
    try:
        found, summary = response_summary(raw)
        if found:
            item['summary'] = summary
    except (ValueError, TypeError) as exc:
        # Invalid optional metadata must not trigger re-transcription of the audio.
        record('summary_error', items=[item], error=exc, response=raw)


def record_refused(item: dict, reason: str, models_tried: list[str], stats: "Stats"):
    record('refused', items=[item], error=reason, models=models_tried)
    append_manifest({
        "audio": item["audio_rel"],
        "sha256": item["sha256"],
        "status": "refused",
        "reason": str(reason)[:500],
        "models": list(models_tried),
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
    })
    with stats.lock:
        stats.refused += 1
    models_lbl = ",".join(models_tried) if models_tried else "?"
    emit(f"[REFUSED] {item['audio_rel']} ({models_lbl}): {str(reason)[:180]}", flush=True)


def record_audio_problem(item, error, stats, *, stage, sha256=None):
    status = "skipped" if error in NO_RECOVERABLE_AUDIO else NEEDS_RECOVERY
    record(status, items=[item], stage=stage, error=error)
    append_manifest({
        "audio": item["audio_rel"],
        "sha256": sha256 or (
            sha256_file(item["audio_abs"]) if os.path.exists(item["audio_abs"]) else ""),
        "status": status,
        "reason": str(error),
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
    })
    with stats.lock:
        if status == NEEDS_RECOVERY:
            stats.needs_recovery += 1
        else:
            stats.skipped += 1
    tag = "RECOVERY" if status == NEEDS_RECOVERY else "SKIP"
    emit(f"[{tag}] {item['audio_rel']}: {error}", flush=True)


class Stats:
    def __init__(self, pending_count=None):
        self.lock = threading.Lock()
        self.started_at = time.monotonic()
        self.pending_count = pending_count
        self.done = 0
        self.skipped = 0
        self.needs_recovery = 0
        self.refused = 0
        self.errors = 0
        self.tok_in = 0
        self.tok_cache = 0
        self.tok_out = 0
        self.audio_ms = 0
        self.failed_files = set()
        self._completed_files = set()
        self._backend_until = 0.0
        self._backend_failures = 0
        self.models = ModelCooldowns()

    def fail(self, items, error, *, stage, model=None, **details):
        record('file_error', items=items, error=error, stage=stage, model=model, **details)
        with self.lock:
            pending = {it['audio_rel'] for it in items} - self._completed_files
            self.errors += len(pending)
            self.failed_files.update(pending)
            if (not isinstance(error, (ModelTaskFailed, ModelsUnavailable, ModelRefused))
                    and classify_error(error) in {'503', 'server', 'connection', 'timeout'}):
                self._backend_failures += 1
                delay = min(BACKEND_RETRY_MAX_S,
                            BACKEND_RETRY_BASE_S * 2 ** min(self._backend_failures - 1, 30))
                self._backend_until = max(self._backend_until, time.monotonic() + delay)
            else:
                delay = 0
        if delay:
            emit(f'[WARN] Сбой сервиса: пауза перед новыми запросами {delay} с', flush=True)

    def wait_backend(self):
        while True:
            with self.lock:
                delay = self._backend_until - time.monotonic()
            if delay <= 0:
                return
            if STOP_EVENT.wait(min(delay, 1)):
                raise InterruptedError('stopped during service cooldown')

    def _snapshot(self):
        return {
            "done": self.done, "audio_ms": self.audio_ms,
            "skipped": self.skipped, "needs_recovery": self.needs_recovery,
            "refused": self.refused, "errors": self.errors,
            "tok_in": self.tok_in, "tok_cache": self.tok_cache, "tok_out": self.tok_out,
            "elapsed_s": time.monotonic() - self.started_at,
            "remaining": self.pending_count() if self.pending_count is not None else None,
            "failed_files": len(self.failed_files),
        }

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def complete(self, item, meta):
        record('file_done', items=[item], model=meta.get('model'), text=item.get('target_md'))
        with self.lock:
            self.done += 1
            self.audio_ms += item["duration_ms"]
            self.failed_files.discard(item['audio_rel'])
            self._completed_files.add(item['audio_rel'])
            self._backend_failures = 0
            completed(item, meta, self._snapshot())

    def add_usage(self, u: dict):
        if not u:
            return
        with self.lock:
            self.tok_in += u.get("input_tokens") or 0
            self.tok_cache += u.get("cache_read_tokens") or 0
            self.tok_out += (u.get("output_tokens") or 0) + (u.get("thinking_tokens") or 0)


def _retry_items_individually(items: list[dict], reason: str, args, people: dict, orgs: dict, stats: Stats):
    for it in items:
        try:
            process_short_batch([it], args, people, orgs, stats)
        except ModelRefused as mr:
            record_refused(it, mr.reason, mr.models_tried, stats)
        except MediaInputError as exc:
            record_audio_problem(it, exc, stats, stage='media_input', sha256=it['sha256'])
        except InterruptedError:
            raise
        except ModelsUnavailable:
            raise
        except Exception as e2:
            stats.fail([it], e2, stage='individual_retry', model=args.model)
            emit(f"[ERR] {it['audio_rel']}: {reason} -> retry failed: {e2}", file=sys.stderr, flush=True)


def _request(prompt, model, args, stats, items, *, conversation_id=None, **details):
    """Capture provider failures and transient exceptions through the same path."""
    try:
        rc, stdout, stderr, home = run_agy(
            prompt, model, args.timeout, not args.no_custom_agent, conversation_id=conversation_id,
        )
    except InterruptedError:
        raise
    except OSError as exc:
        if classify_error(exc) not in {"timeout", "connection"}:
            raise
        record('model_error', items=items, model=model, error=exc, **details)
        return normalize_error(exc), "", None, None
    err, text, usage, cid = extract_envelope_error_and_usage(rc, stdout, stderr)
    stats.add_usage(usage)
    if err:
        record('model_error', items=items, model=model, error=err,
               stdout=stdout, stderr=stderr, **details)
        if classify_error(err) == 'media':
            agy_forget(home, cid)
            raise MediaInputError(err)
    else:
        stats.models.success(model)
    return err, text, cid, home


def _render_reply(item, raw, model, people, orgs, *, salvage=False, reason=""):
    capture_summary(item, raw)
    speakers, lines, no_speech = parse_speakers_and_lines(raw)
    return validate_and_render_md(
        item, speakers, lines, no_speech, model, people, orgs, salvage=salvage,
        extra_notes=[f"Сохранено после сбоя строгой валидации ({reason})."] if salvage else None,
        register_catalog=None if salvage else register_catalog_entries,
    )


def _response_error(raw, error):
    # Spoken refusals inside a transcript are not provider refusals.
    is_refusal = raw and 'SPEAKERS_JSON:' not in raw and (
        SAFETY_ERR_PAT.search(raw[:500]) or REFUSAL_TEXT_PAT.search(raw[:500]))
    return f"{'SAFETY' if is_refusal else 'FORMAT'}: {error}"


def _restore_part(item, part, saved, model, people, orgs):
    """Validate a saved part once and return its speaker map and local lines."""
    if not isinstance(saved, dict):
        raise ValueError('saved part is not a mapping')
    speakers = saved.get('speakers')
    if speakers is not None and not isinstance(speakers, dict):
        raise ValueError('invalid speakers in saved part')
    raw_lines = saved.get('lines')
    if not isinstance(raw_lines, list):
        raise ValueError('invalid lines in saved part')
    lines = []
    for raw in raw_lines:
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise ValueError('invalid line in saved part')
        local_ms, speaker, text = raw
        if (isinstance(local_ms, bool) or not isinstance(local_ms, int)
                or not isinstance(speaker, str) or not isinstance(text, str)):
            raise ValueError('invalid line fields in saved part')
        lines.append((min(part['duration_ms'], max(0, local_ms)), speaker, text))
    validate_and_render_md(
        dict(item, duration_ms=part['duration_ms'], summary=saved.get('summary')),
        speakers, lines, not lines, model, people, orgs,
        salvage=bool(saved.get('warning')), register_catalog=None,
    )
    return speakers, lines


def _request_part(item, part, args, people, orgs, stats, attempts, chain,
                  model_idx, conv_id, home, speakers):
    """Recognize one part, returning its data and the updated session state."""
    prompt_item = dict(item)  # Keep the previous-part summary across model switches.
    item['summary'] = None
    repair_prompt = None
    attempt = 0
    candidate = None
    warning = None
    try:
        while True:
            stats.wait_backend()
            selected = attempts.select(model_idx)
            if selected != model_idx:
                agy_forget(home, conv_id)
                conv_id = None
                repair_prompt = None
                attempt = 0
            model_idx = selected
            cur_model = chain[model_idx]
            attempts.started(model_idx)
            prompt = repair_prompt or build_part_prompt(
                prompt_item, part, people, orgs, is_first=(part['part'] == 1),
                fresh_session=not conv_id, speaker_context=speakers,
            )
            err, text, cid, new_home = _request(
                prompt, cur_model, args, stats, [item], conversation_id=conv_id, part=part['part'],
            )
            home = new_home if new_home is not None else home
            conv_id = cid or conv_id
            if err:
                agy_forget(home, conv_id)
                conv_id = None
                model_idx = attempts.failed(
                    model_idx, err, scope=f"{item['audio_rel']} part {part['part']}")
                repair_prompt = None
                attempt = 0
                continue
            capture_summary(item, text)
            try:
                sp, plines, no_speech = parse_speakers_and_lines(text)
                if plines:
                    candidate = (sp, plines, cur_model, item.get('summary'))
                validate_and_render_md(
                    dict(item, duration_ms=part['duration_ms']), {**(speakers or {}), **(sp or {})},
                    plines, no_speech, cur_model, people, orgs,
                    register_catalog=register_catalog_entries,
                )
                return sp, plines, cur_model, warning, model_idx, conv_id, home
            except (ValueError, TypeError) as exc:
                record('validation_error', items=[item], model=cur_model, part=part['part'],
                       error=exc, response=text)
                attempt += 1
                failure = _response_error(text, exc)
                if (classify_error(failure) != 'content' and attempt < PART_REPAIR_ATTEMPTS
                        and not stats.models.remaining(cur_model)):
                    repair_prompt = load_prompt('repair-part.md', part_number=part['part'],
                                                error=exc, part_end=format_ts(part['duration_ms']))
                    continue
                agy_forget(home, conv_id)
                conv_id = None
                model_idx = attempts.failed(
                    model_idx, failure, scope=f"{item['audio_rel']} part {part['part']}")
                repair_prompt = None
                attempt = 0
    except ModelTaskFailed as exc:
        if candidate is None:
            raise
        sp, plines, used_model, item['summary'] = candidate
        warning = f"Часть {part['part']}: {exc} (сохранено с подрезанием)."
        return sp, plines, used_model, warning, model_idx, conv_id, home


def process_long_item(item: dict, args, people: dict, orgs: dict, stats: Stats) -> bool:
    chain = getattr(args, 'model_chain', None) or [args.model]
    attempts = ModelAttempts(chain, stats.models, [item], stop_on_quota=args.stop_on_quota)
    model_idx = 0
    used_model = chain[0]
    conv_id = home = None
    speakers = None
    all_lines = []
    last_ms = -1
    part_warnings = []
    checkpoint = LongCheckpoint(item)
    restored = {}
    for part in item['parts']:
        saved = checkpoint.get(part['part'])
        if saved is None:
            break
        try:
            restored[part['part']] = _restore_part(
                item, part, saved, saved.get('model') or used_model, people, orgs)
        except (ValueError, TypeError, KeyError) as exc:
            record('checkpoint_part_invalid', items=[item], part=part['part'], error=exc,
                   path=str(checkpoint.path))
            checkpoint.drop_from(part['part'])
            break
    if restored:
        emit(f"Продолжение {item['audio_rel']}: сохранены части 1–{len(restored)} "
             f"из {len(item['parts'])}", flush=True)
        record('checkpoint_resumed', items=[item], parts=len(restored))
    try:
        for part in item['parts']:
            number = part['part']
            if number in restored:
                sp, plines = restored[number]
                saved = checkpoint.get(number)
                used_model = saved.get('model') or used_model
                item['summary'] = saved.get('summary')
                warning = saved.get('warning')
            else:
                sp, plines, used_model, warning, model_idx, conv_id, home = _request_part(
                    item, part, args, people, orgs, stats, attempts, chain,
                    model_idx, conv_id, home, speakers)
                checkpoint.save(number, speakers=sp, lines=plines, model=used_model,
                                summary=item.get('summary'), warning=warning)
            if warning:
                part_warnings.append(warning)
            if sp:
                speakers = {**(speakers or {}), **sp}
            for local_ms, speaker_id, txt in plines:
                clamped_local = min(part['duration_ms'], max(0, local_ms))
                abs_ms = clamped_local + part['offset_ms']
                if abs_ms <= last_ms:
                    if part_warnings:
                        abs_ms = last_ms
                    else:
                        continue
                last_ms = abs_ms
                all_lines.append((abs_ms, speaker_id, txt))
        md = validate_and_render_md(
            item, speakers, all_lines, not all_lines, used_model, people, orgs,
            salvage=bool(part_warnings), extra_notes=part_warnings,
            register_catalog=register_catalog_entries,
        )
        stats.complete(item, publish_md(item, md, used_model))
        checkpoint.discard()
        return True
    finally:
        agy_forget(home, conv_id)


def process_short_batch(items: list[dict], args, people: dict, orgs: dict, stats: Stats):
    """Repair invalid answers, then exhaust eligible models before salvage."""
    chain = getattr(args, 'model_chain', None) or [args.model]
    attempts = ModelAttempts(chain, stats.models, items, stop_on_quota=args.stop_on_quota)
    model_idx = 0
    prompt = build_batch_prompt(items, people, orgs)
    scope = items[0]['audio_rel'] if len(items) == 1 else f"batch({len(items)})"
    home = conv_id = None
    candidate = None
    try:
        while True:
            stats.wait_backend()
            model_idx = attempts.select(model_idx)
            model = chain[model_idx]
            attempts.started(model_idx)
            err, text, conv_id, new_home = _request(prompt, model, args, stats, items)
            home = new_home if new_home is not None else home
            if err:
                if classify_error(err) == 'content' and len(items) > 1:
                    stats.models.failed(model, err, items=items, scope=scope)
                    agy_forget(home, conv_id)
                    conv_id = None
                    emit(f"[WARN] {scope}: отказ по содержимому на {model}; разбиваю по одному файлу")
                    _retry_items_individually(items, 'content refusal', args, people, orgs, stats)
                    return
                agy_forget(home, conv_id)
                conv_id = None
                model_idx = attempts.failed(model_idx, err, scope=scope)
                continue
            blocks = parse_item_blocks(text, len(items))
            failed = []
            for number, item in enumerate(items, 1):
                raw = blocks.get(number, '')
                try:
                    md = _render_reply(item, raw, model, people, orgs)
                except (ValueError, TypeError) as exc:
                    record('validation_error', items=[item], model=model, error=exc, response=raw)
                    failed.append((item, str(exc), raw))
                else:
                    stats.complete(item, publish_md(item, md, model))
            if not failed:
                return
            if len(items) > 1:
                for item, reason, _ in failed:
                    _retry_items_individually([item], reason, args, people, orgs, stats)
                return
            item, reason, raw = failed[0]
            try:
                _, candidate_lines, candidate_ns = parse_speakers_and_lines(raw)
                if candidate_lines or candidate_ns:
                    candidate = (raw, model, item.get('summary'))
            except (ValueError, TypeError):
                pass
            failure = _response_error(raw, reason)
            if conv_id and raw and classify_error(failure) != 'content' and not stats.models.remaining(model):
                stats.wait_backend()
                rprompt = load_prompt('repair-single.md', error=reason, duration=format_ts(item['duration_ms']))
                failure, repaired, cid, new_home = _request(
                    rprompt, model, args, stats, [item], conversation_id=conv_id, stage='repair',
                )
                home = new_home if new_home is not None else home
                conv_id = cid or conv_id
                if not failure:
                    try:
                        md = _render_reply(item, repaired, model, people, orgs)
                    except (ValueError, TypeError) as exc:
                        record('validation_error', items=[item], model=model, stage='repair',
                               error=exc, response=repaired)
                        reason = str(exc)
                        failure = _response_error(repaired, reason)
                        try:
                            _, candidate_lines, candidate_ns = parse_speakers_and_lines(repaired)
                            if candidate_lines or candidate_ns:
                                candidate = (repaired, model, item.get('summary'))
                        except (ValueError, TypeError):
                            pass
                    else:
                        stats.complete(item, publish_md(item, md, model))
                        return
            agy_forget(home, conv_id)
            conv_id = None
            model_idx = attempts.failed(model_idx, failure, scope=scope)
    except ModelTaskFailed as exc:
        if candidate is None:
            raise
        candidate_raw, candidate_model, item['summary'] = candidate
        md = _render_reply(item, candidate_raw, candidate_model, people, orgs,
                           salvage=True, reason=str(exc))
        stats.complete(item, publish_md(item, md, candidate_model))
        return
    except MediaInputError:
        if len(items) == 1:
            raise
        emit(f"[WARN] {scope}: повреждённые медиаданные; проверяю файлы по одному")
        _retry_items_individually(items, 'media input rejected', args, people, orgs, stats)
    finally:
        agy_forget(home, conv_id)


def run_job(job_cands: list[dict], args, stats: Stats):
    people, orgs = load_refs()
    with tempfile.TemporaryDirectory(prefix="diarize_job_") as tmp_dir:
        ready = []
        for idx, cand in enumerate(job_cands):
            sub = os.path.join(tmp_dir, f"f{idx}")
            os.makedirs(sub, exist_ok=True)
            try:
                item, err = prepare_audio_work(
                    cand, sub, args.max_single_sec, args.chunk_sec, args.overlap_sec,
                    untrunc_bin=getattr(args, "untrunc_bin", None),
                    use_faad=getattr(args, "use_faad", True),
                )
            except InterruptedError:
                return
            except Exception as exc:
                if FORCE_STOP_EVENT.is_set():
                    return
                stats.fail([cand], exc, stage='audio_preparation')
                emit(f"[ERR] {cand['audio_rel']}: {exc}", file=sys.stderr, flush=True)
                continue
            if err:
                # A cancelled ffmpeg must not permanently mark its input as broken.
                if FORCE_STOP_EVENT.is_set():
                    return
                record_audio_problem(cand, err, stats, stage='audio_preparation')
                continue
            ready.append(item)

        if not ready:
            return
        longs = [x for x in ready if x["parts"]]
        shorts = [x for x in ready if not x["parts"]]
        for litem in longs:
            if FORCE_STOP_EVENT.is_set():
                return
            try:
                process_long_item(litem, args, people, orgs, stats)
            except ModelRefused as mr:
                record_refused(litem, mr.reason, mr.models_tried, stats)
            except MediaInputError as exc:
                record_audio_problem(litem, exc, stats, stage='media_input', sha256=litem['sha256'])
            except InterruptedError:
                return
            except ModelsUnavailable as exc:
                stats.fail([litem], exc, stage='model_availability')
                raise
            except Exception as e:
                if FORCE_STOP_EVENT.is_set():
                    return
                if "QUOTA" in str(e) and args.stop_on_quota:
                    STOP_EVENT.set()
                    return
                stats.fail([litem], e, stage='long_recording', model=args.model)
                emit(f"[ERR] {litem['audio_rel']}: {e}", file=sys.stderr, flush=True)
        if shorts and not FORCE_STOP_EVENT.is_set():
            try:
                process_short_batch(shorts, args, people, orgs, stats)
            except ModelRefused as mr:
                for sh in shorts:
                    record_refused(sh, mr.reason, mr.models_tried, stats)
            except MediaInputError as exc:
                for sh in shorts:
                    record_audio_problem(sh, exc, stats, stage='media_input', sha256=sh['sha256'])
            except InterruptedError:
                return
            except ModelsUnavailable as exc:
                stats.fail(shorts, exc, stage='model_availability')
                raise
            except Exception as e:
                if FORCE_STOP_EVENT.is_set():
                    return
                if "QUOTA" in str(e) and args.stop_on_quota:
                    STOP_EVENT.set()
                    return
                stats.fail(shorts, e, stage='short_batch', model=args.model)
                emit(f"[ERR] batch({len(shorts)}): {e}", file=sys.stderr, flush=True)


def pack_next_job(pending_dq: deque, max_files: int, max_sec: int, max_single_sec: int) -> list[dict] | None:
    """Pull either one long/unsized file or a mini-batch of short files from deque."""
    if not pending_dq:
        return None
    first = pending_dq.popleft()
    batch = [first]
    dur1, _ = probe_duration_ms(first["audio_abs"]) if os.path.getsize(first["audio_abs"]) > 0 else (None, "empty")
    if dur1 is None or dur1 > max_single_sec * 1000:
        return batch
    acc_ms = dur1
    while pending_dq and len(batch) < max_files and acc_ms < max_sec * 1000:
        nxt = pending_dq.popleft()
        d, _ = probe_duration_ms(nxt["audio_abs"]) if os.path.getsize(nxt["audio_abs"]) > 0 else (None, "empty")
        if d is None or d > max_single_sec * 1000 or (acc_ms + d > max_sec * 1000 and len(batch) >= 1):
            pending_dq.appendleft(nxt)
            break
        batch.append(nxt)
        acc_ms += d
    return batch
