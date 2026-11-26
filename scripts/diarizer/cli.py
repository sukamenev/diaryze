"""Command-line options, signals and the worker pool."""

from collections import deque
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import yaml

from . import config
from .archive import catalog_paths, init_catalogs, load_refs, resolve_source_dir, scan_pending
from .audio import _get_libfaad, find_untrunc
from .config import (
    BATCH_SHORT_MAX, BATCH_SHORT_SEC, CHUNK_SEC, DEFAULT_FALLBACK_MODELS,
    DEFAULT_MODEL, DEFAULT_WORKERS, MAX_FILE_ATTEMPTS, MAX_SINGLE_SEC,
    OVERLAP_SEC, REQUEST_TIMEOUT_SEC, use_archive,
)
from .pipeline import Stats, pack_next_job, run_job
from .runtime import STOP_EVENT, stop_signals
from .console import _display, emit, progress_output, render_finished
from .selftest import run_selftest
from .terminal import preserve_terminal
from .diagnostics import record, run_log
from .errors import ModelsUnavailable, classify_error, ui_error
from .ui import LANGUAGES, argparse_ui, tr, use_ui
from .voice_samples import (
    has_approved_sample, import_confirmed_voice_samples, pending_voice_samples, phone_owner, prepare_voice_samples,
    review_voice_sample,
)

VOICE_SAMPLE_ERRORS = (OSError, ValueError, subprocess.TimeoutExpired, yaml.YAMLError)


# Select the interface language before parsing the full command line.
def main():
    selector = argparse.ArgumentParser(add_help=False)
    selector.add_argument("--ui", nargs="?", default=config.DEFAULT_UI)
    selected, _ = selector.parse_known_args()
    language = selected.ui if selected.ui in LANGUAGES else config.DEFAULT_UI
    with use_ui(language):
        with argparse_ui():
            parser, args = _parse_args()
        return _main(parser, args)


# Parse the command-line options in the selected interface language.
def _parse_args():
    ap = argparse.ArgumentParser(description=tr("Parallel agy audio diarizer"))
    ap.add_argument("--ui", choices=LANGUAGES, default=config.DEFAULT_UI,
                    help=tr("Interface language (default: {default})", default=config.DEFAULT_UI))
    ap.add_argument("--root", default=".", help=tr("Archive root (default: current directory)"))
    ap.add_argument("--folder", "--subdir", dest="subdir", default=".", metavar="PATH",
                    help=tr("Folder to scan within the archive (default: the whole archive)"))
    ap.add_argument("--init", action="store_true", help=tr("Create empty local catalogs and exit"))
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=tr("Number of parallel workers"))
    ap.add_argument("--model", default=DEFAULT_MODEL, help=tr("Initial model"))
    ap.add_argument("--models", default="", help=tr("Comma-separated ordered model list (overrides --model + --fallback-models)"))
    ap.add_argument("--fallback-models", default=DEFAULT_FALLBACK_MODELS,
                        help=tr("Comma-separated fallback models for provider and task errors"))
    ap.add_argument("--fallback-on-quota", action="store_true",
                    help=tr("Compatibility option: quota fallback is now automatic"))
    ap.add_argument("--limit", type=int, default=0, help=tr("Max pending files to process (0 = all)"))
    ap.add_argument("--batch-short-max", type=int, default=BATCH_SHORT_MAX, help=tr("Max short files per agy call"))
    ap.add_argument("--batch-short-sec", type=int, default=BATCH_SHORT_SEC, help=tr("Target max short audio sec per agy call"))
    ap.add_argument("--max-single-sec", type=int, default=MAX_SINGLE_SEC, help=tr("Slice recordings longer than this"))
    ap.add_argument("--chunk-sec", type=int, default=CHUNK_SEC, help=tr("Length of each segment in seconds"))
    ap.add_argument("--overlap-sec", type=int, default=OVERLAP_SEC, help=tr("Overlap between segments in seconds"))
    ap.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT_SEC, help=tr("Request timeout in seconds"))
    ap.add_argument("--no-custom-agent", action="store_true", help=tr("Disable minimal custom agent"))
    ap.add_argument("--no-untrunc", action="store_true", help=tr("Disable automatic untrunc fallback for damaged containers"))
    ap.add_argument("--no-faad", action="store_true", help=tr("Disable libfaad2 raw AAC frame recovery"))
    ap.add_argument("--retry-skipped", action="store_true", help=tr("Re-try files previously marked status=skipped in manifest.jsonl"))
    ap.add_argument("--retry-recovery", action="store_true",
                    help=tr("Retry files marked needs_recovery and legacy damaged files marked skipped"))
    ap.add_argument("--retry-refused", action="store_true", help=tr("Re-try files previously marked status=refused in manifest.jsonl"))
    ap.add_argument("--stop-on-quota", action="store_true", help=tr("Exit on quota exhaustion instead of sleeping"))
    ap.add_argument("--max-file-attempts", type=int, default=MAX_FILE_ATTEMPTS,
                    help=tr("Max queue assignments per unchanged file in one run (default: %(default)s)"))
    ap.add_argument("--dry-run", action="store_true", help=tr("List pending queue without running agy"))
    ap.add_argument("--selftest", action="store_true", help=tr("Run internal parser/validator/recovery self-test"))
    voice = ap.add_mutually_exclusive_group()
    voice.add_argument("--prepare-voice-sample", nargs="?", const=True, metavar="PERSON",
                       help=tr("Find up to {count} samples for the folder owner, or specify a person ID",
                               count=config.VOICE_SAMPLE_MAX_CANDIDATES))
    voice.add_argument("--approve-voice-sample", metavar="FILE", help=tr("Approve one candidate and clear its review set"))
    voice.add_argument("--reject-voice-sample", metavar="PATH", help=tr("Discard a pending set by candidate FLAC or folder"))
    voice.add_argument("--no-voice-samples", action="store_true",
                       help=tr("Transcribe without preparing or using any voice samples"))
    ap.add_argument("--skip-voice-sample-preflight", action="store_true",
                    help=tr("Transcribe immediately using any already approved voice samples"))
    return ap, ap.parse_args()


# Report a validation error using the same headings as the argument parser.
def _argument_error(parser, message):
    with argparse_ui():
        parser.error(message)


# Validate the selected options and execute the corresponding archive action.
def _main(parser, args):
    if args.max_file_attempts < 1:
        _argument_error(parser, tr("--max-file-attempts must be positive"))
    raw_chain = args.models if args.models.strip() else ",".join(
        x for x in (args.model, args.fallback_models) if x.strip()
    )
    args.model_chain = []
    for m in raw_chain.split(","):
        ms = m.strip()
        if ms and ms not in args.model_chain:
            args.model_chain.append(ms)
    if not args.model_chain:
        args.model_chain = [args.model]
    args.model = args.model_chain[0]
    args.untrunc_bin = None if args.no_untrunc else find_untrunc()
    args.use_faad = False if args.no_faad else (_get_libfaad() is not None)

    if args.selftest:
        run_selftest(args)
        return

    with use_archive(args.root) as archive_root:
        args.root = archive_root
        try:
            source_dir = resolve_source_dir(args.subdir)
        except ValueError as exc:
            _argument_error(parser, str(exc))
        if args.prepare_voice_sample or args.approve_voice_sample or args.reject_voice_sample:
            if args.dry_run or args.init:
                _argument_error(parser, tr("Voice sample actions cannot be combined with --dry-run or --init"))
            return _run_voice_sample_action(args, source_dir)
        if args.init:
            init_catalogs()
            emit(tr("Catalogs: {paths}", paths=", ".join(catalog_paths())))
            return
        if not args.dry_run:
            missing = [tool for tool in ("agy", "ffmpeg", "ffprobe") if not shutil.which(tool)]
            if missing:
                _argument_error(parser, tr("Missing executable(s) on PATH: {tools}", tools=", ".join(missing)))
            try:
                load_refs()
            except (OSError, ValueError, yaml.YAMLError) as exc:
                _argument_error(parser, str(exc))
            init_catalogs()
            config.transcripts_dir().mkdir(parents=True, exist_ok=True)
        return _run(args)


# Run a standalone preparation or review action against the archive's voice samples.
def _run_voice_sample_action(args, source_dir):
    log_path = None
    try:
        with stop_signals(), preserve_terminal():
            if person := args.prepare_voice_sample:
                people, _ = load_refs()
                if person is True:
                    marker = Path(source_dir).relative_to(config.ROOT) / ".owner"
                    person, _ = phone_owner({"audio_rel": marker.as_posix()}, people)
                    if person is None:
                        raise ValueError(tr("Choose a folder with a known phone owner or provide the owner ID"))
                if person not in people:
                    raise ValueError(tr("Person is missing from the catalog: {person}", person=person))
                with run_log() as log_path:
                    record("voice_sample_preparation_started", person=person, folder=args.subdir, model=args.model)
                    try:
                        samples = prepare_voice_samples(person, source_dir, args)
                    except VOICE_SAMPLE_ERRORS as exc:
                        record("voice_sample_error", person=person, folder=args.subdir, error=exc)
                        raise
                if STOP_EVENT.is_set():
                    return 130
                if not samples:
                    raise ValueError(tr("No suitable voice sample found for {person}", person=person))
                _show_voice_candidates(samples)
            elif path := args.approve_voice_sample:
                sample = review_voice_sample(path, approve=True)
                emit(tr("Voice sample approved: {sample}", sample=sample))
            else:
                review_voice_sample(args.reject_voice_sample, approve=False)
                emit(tr("Voice sample review rejected: {path}", path=args.reject_voice_sample))
    except InterruptedError:
        return 130
    except VOICE_SAMPLE_ERRORS as exc:
        _show_voice_error(exc, log_path)
        return 1
    return 0


# Show the review instructions with one linkable path for the whole set.
def _show_voice_candidates(samples):
    emit(tr("Listen to the voice clips in {folder}/ ({count}).", folder=_display(samples[0].parent),
            count=len(samples)))
    emit(tr("Move the best FLAC to the adjacent confirmed/ folder, leave the JSON files in place, and rerun transcription."))


# Show one short error and point to its diagnostic log when available.
def _show_voice_error(error, log_path, *, scope=""):
    message = _display(" ".join(ui_error(error).split()))
    if len(message) > 240:
        message = message[:239] + "…"
    prefix = f"{_display(scope)}: " if scope else ""
    emit(f"[ERR] {prefix}{message}", file=sys.stderr)
    if log_path is not None:
        emit(tr("Details in {path}", path=_display(log_path)), file=sys.stderr)


# Stage samples for each phone owner within the requested folder.
def _voice_sample_preflight(candidates, args, *, log_path=None):
    people, _ = load_refs()
    requested = Path(resolve_source_dir(args.subdir))
    checked = set()
    owners_by_folder = {}
    waiting = False
    for item in candidates:
        if STOP_EVENT.is_set():
            return True
        parent = Path(item["audio_rel"]).parent
        if parent not in owners_by_folder:
            owners_by_folder[parent] = phone_owner(item, people)
        person, owner_file = owners_by_folder[parent]
        if not person or owner_file in checked:
            continue
        checked.add(owner_file)
        owner_root = Path(config.ROOT) / Path(owner_file).parent
        source = requested if requested.is_relative_to(owner_root) else owner_root
        try:
            if has_approved_sample(person):
                continue
            samples = pending_voice_samples(person, source)
            if not samples:
                samples = prepare_voice_samples(person, source, args, target_item=item)
            if STOP_EVENT.is_set():
                return True
            if not samples:
                raise ValueError(tr("No suitable voice sample found for {person}", person=person))
        except InterruptedError:
            return True
        except VOICE_SAMPLE_ERRORS as exc:
            waiting = True
            record('voice_sample_error', items=[item], person=person, error=exc)
            _show_voice_error(exc, log_path, scope=owner_file)
            if getattr(args, "stop_on_quota", False) and classify_error(exc) == "quota":
                break
            continue
        waiting = True
        record('voice_samples_pending', items=[item], person=person, paths=[str(path) for path in samples])
        _show_voice_candidates(samples)
    return waiting


# Run the worker pool with signal handling, terminal cleanup, and diagnostics.
def _run(args):
    with stop_signals(), preserve_terminal(), progress_output():
        if args.dry_run:
            return run_workers(args)
        with run_log() as path:
            record('run_started', options=vars(args))
            try:
                result = run_workers(args, log_path=path)
                return 1 if result and result['failed_files'] else 0
            except ModelsUnavailable as exc:
                record('fatal_error', error=exc)
                emit(f"[ERR] {ui_error(exc)}")
                return 1
            except BaseException as exc:
                record('fatal_error', error=exc)
                raise


# Scan the pending queue, coordinate workers, and report final totals.
def run_workers(args, *, log_path=None):
    without_samples = getattr(args, 'no_voice_samples', False)
    confirmed = []
    if not args.dry_run and not without_samples:
        for sample in import_confirmed_voice_samples():
            record('voice_sample_confirmed', path=str(sample))
            confirmed.append(sample)
    cands = scan_pending(args.subdir, retry_skipped=args.retry_skipped, retry_refused=args.retry_refused,
                         retry_recovery=getattr(args, "retry_recovery", False))
    if args.limit > 0:
        cands = cands[: args.limit]
    if (cands and not args.dry_run and not without_samples and not getattr(args, 'skip_voice_sample_preflight', False)
            and _voice_sample_preflight(cands, args, log_path=log_path)):
        return
    if log_path is not None:
        emit(tr("Diagnostic log: {path}", path=log_path))
    for sample in confirmed:
        emit(tr("Voice sample approved: {sample}", sample=sample))
    faad_state = "ok" if args.use_faad else tr("disabled" if args.no_faad else "missing")
    untrunc_state = args.untrunc_bin or tr("disabled" if args.no_untrunc else "missing")
    models_str = " → ".join(args.model_chain)
    emit(tr("Queued files in {folder}: {count} · faad {faad} · untrunc {untrunc}",
            folder=args.subdir, count=len(cands), faad=faad_state, untrunc=untrunc_state), flush=True)
    if not args.use_faad and not args.no_faad:
        emit(tr("[WARN] libfaad2 is unavailable; raw AAC recovery is disabled"), flush=True)
    if not args.untrunc_bin and not args.no_untrunc:
        emit(tr("[WARN] untrunc is unavailable; automatic container repair is disabled"), flush=True)
    if args.dry_run:
        if len(cands) > 20:
            emit(tr("Showing the first 20 of {count}:", count=len(cands)))
        for i, c in enumerate(cands[:20], 1):
            emit(f"  {i:2d}. {c['dt'] or '?'} | {c['audio_rel']}")
        return
    if not cands:
        record('run_finished', summary={'done': 0, 'failed_files': 0}, failed_files=[])
        return
    pending_dq = deque(cands)
    active_rels = set()
    attempts = Counter()
    max_attempts = getattr(args, 'max_file_attempts', MAX_FILE_ATTEMPTS)
    stats = Stats(pending_count=lambda: len(pending_dq))
    emit(tr("Start · workers {workers} · models {models}", workers=args.workers, models=models_str), flush=True)

    q_lock = threading.Lock()

    # Track retry attempts against a particular version of a source file.
    def file_key(item):
        return item['audio_rel'], item.get('mtime'), os.path.getsize(item['audio_abs'])

    # Claim each job under the queue lock and release its files when finished.
    def work():
        while not STOP_EVENT.is_set():
            with q_lock:
                if not pending_dq and args.limit == 0:
                    fresh = [
                        c for c in scan_pending(
                            args.subdir, retry_skipped=args.retry_skipped, retry_refused=args.retry_refused,
                        )
                        if c["audio_rel"] not in active_rels and attempts[file_key(c)] < max_attempts
                    ]
                    pending_dq.extend(fresh)
                job = pack_next_job(pending_dq, args.batch_short_max, args.batch_short_sec, args.max_single_sec)
                if job:
                    for it in job:
                        active_rels.add(it["audio_rel"])
                        attempts[file_key(it)] += 1
                    record('job_started', items=job,
                           attempts={it['audio_rel']: attempts[file_key(it)] for it in job})
            if not job:
                return
            try:
                run_job(job, args, stats)
            finally:
                with q_lock:
                    for it in job:
                        active_rels.discard(it["audio_rel"])

    # Handle cancellation and stop the pool if a worker fails unexpectedly.
    def worker_loop():
        try:
            work()
        except InterruptedError:
            return
        except Exception as exc:
            STOP_EVENT.set()
            record('worker_error', error=exc, stage='worker_loop')
            raise

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = [pool.submit(worker_loop) for _ in range(args.workers)]
            wait(futs)
            for future in futs:
                future.result()
    finally:
        summary = stats.snapshot()
        record('run_finished', summary=summary, failed_files=sorted(stats.failed_files))
        emit(render_finished(summary, stopped=STOP_EVENT.is_set()), final=True)
    return summary
