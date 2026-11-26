"""Collect, review and select independent voice references for phone owners."""

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from . import config
from .agy import agy_forget, extract_envelope_error_and_usage, run_agy
from .archive import is_audio_candidate, is_excluded_source_dir, parse_filename, sha256_file
from .audio import prepare_audio_work, probe_duration_ms
from .diagnostics import record
from .owners import find_folder_owner
from .platform import locked_path
from .platform.processes import run_capture
from .prompt_files import load_prompt
from .runtime import STOP_EVENT
from .ui import tr


SAMPLE_NAME = re.compile(r"(\d{4}-\d{2}-\d{2}|undated)_\d{2,}\.flac")
PERSON_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
CHANNELS = ("phone", "recorder")
READ_STATUSES = {"read", "unreadable", "not_attempted"}
CLIP_STATUSES = {"single_speaker", "multiple_speakers", "no_speech", "uncertain", "unreadable"}


# Convert a date or timestamp to the recording day in the archive's time zone.
def _day(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (moment.astimezone(config.TZ) if moment.tzinfo else moment).date()


# Prefer an explicit recording date and fall back to embedded recording metadata.
def _recording_day(item):
    for value in (item.get("recorded_at"), item.get("dt")):
        if day := _day(value):
            return day
    if not (path := item.get("audio_abs")):
        return None
    try:
        result = run_capture(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "format_tags:stream_tags", "-of", "json", path],
            capture_output=True, text=True, timeout=config.VOICE_SAMPLE_DATE_PROBE_TIMEOUT_SEC,
        )
    except InterruptedError:
        raise
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        metadata = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(metadata, dict):
        return None
    for entry in [metadata.get("format") or {}, *(metadata.get("streams") or [])]:
        tags = {key.lower(): value for key, value in (entry.get("tags") or {}).items()}
        for key in ("date_recorded", "date", "icrd", "creation_time"):
            if day := _day(tags.get(key)):
                return day
    return None


# Locate the archive's approved voice sample library.
def _sample_root():
    return config.data_path("voice_samples", root=Path(config.ROOT).resolve())


# Find the approved sample directory for a person in the archive data.
def _sample_dir(person):
    if not isinstance(person, str) or not PERSON_ID.fullmatch(person):
        raise ValueError(tr("Invalid voice sample person ID"))
    return _sample_root() / person


# Locate one recording channel and reject symbolic links along the library path.
def _channel_dir(person, channel):
    if channel not in CHANNELS:
        raise ValueError(tr("Invalid voice sample channel"))
    folder = _sample_dir(person) / channel
    if folder.resolve() != folder:
        raise ValueError(tr("Voice sample channel cannot use a symbolic link: {path}", path=folder))
    return folder


# Resolve one of the adjacent review directories without traversing symbolic links.
def _review_dir(folder, status):
    path = folder / status
    if path.resolve() != path:
        raise ValueError(tr("Voice sample review cannot use a symbolic link: {path}", path=path))
    return path


# Encode the exact reference sent to the model as portable transcript metadata.
def voice_sample_value(reference):
    return Path(reference["file"]).relative_to(_sample_root()).as_posix() if reference else "none"


# Identify the cataloged owner whose sample is relevant to this phone call.
def phone_owner(item, people):
    owner, owner_file = find_folder_owner(item["audio_rel"])
    if not owner or owner.get("type") != "phone":
        return None, None
    person = owner.get("owner")
    if isinstance(person, str) and PERSON_ID.fullmatch(person) and person in people:
        return person, owner_file
    return None, None


# List independent clips placed in the approved phone and recorder directories.
def _approved_samples(person):
    for channel in CHANNELS:
        try:
            folder = _channel_dir(person, channel)
            confirmed = _review_dir(folder, "confirmed")
            candidate = _review_dir(folder, "candidate")
        except ValueError:
            continue
        # Direct clips were approved under the previous archive layout.
        for directory in (confirmed, folder):
            for path in directory.glob("*.flac"):
                match = SAMPLE_NAME.fullmatch(path.name)
                if not match or path.is_symlink() or not path.is_file():
                    continue
                if directory == confirmed and (candidate / path.with_suffix(".json").name).exists():
                    continue
                stamp = match[1]
                day = _day(stamp)
                if day or stamp == "undated":
                    yield path, day, channel


# Check whether the owner already has a usable approved sample.
def has_approved_sample(person):
    return any(_approved_samples(person))


# Select the owner's closest dated clip, preferring a phone sample on ties.
def owner_reference(item, people):
    person, _ = phone_owner(item, people)
    if not person or not (samples := list(_approved_samples(person))):
        return None
    target = _recording_day(item)

    # Rank by day distance or recent date, then channel and filename for stable ties.
    def rank(sample):
        path, day, channel = sample
        distance = (abs((day - target).days) if target and day else
                    -day.toordinal() if day else float("inf"))
        return distance, channel != "phone", path.name

    path, _, _ = min(samples, key=rank)
    return {"person": person, "file": str(path)}


# Find dated calls with known direction and distinct-contact evidence for this owner.
def _source_calls(person, source_dir, target_day):
    root = Path(config.ROOT).resolve()
    _, scope_owner_file = find_folder_owner(str(Path(source_dir).relative_to(root) / "_"))
    candidates = []
    for directory, folders, files in os.walk(source_dir):
        base = Path(directory)
        folders[:] = [name for name in folders if not is_excluded_source_dir(base / name)]
        rel_dir = base.relative_to(root)
        owner, owner_file = find_folder_owner(str(rel_dir / "_"))
        if not owner or owner.get("type") != "phone" or owner.get("owner") != person:
            continue
        if scope_owner_file and scope_owner_file != owner_file:
            continue
        for name in files:
            dt, direction, contact, phones = parse_filename(name)
            if not dt or direction not in {"in", "out"} or not (phones or contact):
                continue
            path = base / name
            if path.is_symlink() or not path.is_file() or not is_audio_candidate(path):
                continue
            candidates.append({
                "audio_abs": str(path), "audio_rel": (rel_dir / name).as_posix(), "dt": dt,
                "direction": direction, "contact_key": phones[0] if phones else contact.casefold(),
                "owner_file": owner_file,
            })

    # Favor calls close in date or, for manual requests without a date, recent calls.
    def rank(candidate):
        day = _day(candidate["dt"])
        distance = abs((day - target_day).days) if target_day else -day.toordinal()
        return distance, candidate["audio_rel"]

    return sorted(candidates, key=rank)


# Prepare short, decodable calls from different contacts and prefer both directions.
def _prepare_calls(person, source_dir, target_day, temp, args):
    waiting = _source_calls(person, source_dir, target_day)
    if waiting:
        owner_file = waiting[0]["owner_file"]
        waiting = [call for call in waiting if call["owner_file"] == owner_file]
    selected, contacts, directions = [], set(), set()
    while waiting and len(selected) < config.VOICE_SAMPLE_MAX_SOURCE_CALLS and not STOP_EVENT.is_set():
        index = next((i for i, call in enumerate(waiting) if call["direction"] not in directions), 0)
        call = waiting.pop(index)
        if call["contact_key"] in contacts:
            continue
        try:
            duration, _ = probe_duration_ms(call["audio_abs"])
            if duration is None or not config.VOICE_SAMPLE_MIN_SOURCE_MS <= duration <= config.VOICE_SAMPLE_MAX_SOURCE_SEC * 1000:
                continue
            folder = temp / "inputs" / str(len(selected) + 1)
            folder.mkdir(parents=True, exist_ok=True)
            item, error = prepare_audio_work(
                call, str(folder), config.VOICE_SAMPLE_MAX_SOURCE_SEC, args.chunk_sec, args.overlap_sec,
                untrunc_bin=args.untrunc_bin, use_faad=args.use_faad,
            )
        except InterruptedError:
            raise
        except OSError:
            continue
        if not error and not item["parts"]:
            selected.append(item)
            contacts.add(call["contact_key"])
            directions.add(call["direction"])
    return selected


# Verify one proposed interval and its corroborating call indexes.
def _parse_interval(reply, calls):
    if not isinstance(reply, dict):
        raise ValueError(tr("Expected a voice sample object or null"))
    number = reply.get("call")
    start, end = reply.get("start_ms"), reply.get("end_ms")
    evidence = reply.get("evidence")
    others = reply.get("supporting_calls")
    if (any(type(n) is not int for n in (number, start, end))
            or not 1 <= number <= len(calls)
            or not 0 <= start < end <= calls[number - 1]["duration_ms"]
            or not config.VOICE_SAMPLE_MIN_MS <= end - start <= config.VOICE_SAMPLE_MAX_MS
            or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500
            or not isinstance(others, list)
            or any(type(n) is not int or not 1 <= n <= len(calls) or n == number for n in others)
            or len(set(others)) < config.VOICE_SAMPLE_MIN_SOURCE_CALLS - 1):
        raise ValueError(tr("Invalid voice sample bounds or cross-call evidence"))
    call = calls[number - 1]
    return call, {
        "schema_version": "1.0",
        "recorded_on": _day(call["dt"]).isoformat(),
        "source": {"audio": call["audio_rel"], "sha256": call["sha256"],
                   "start_ms": start, "end_ms": end},
        "supporting_calls": [calls[n - 1]["audio_rel"] for n in sorted(set(others))],
        "evidence": evidence.strip(),
    }


# Parse a model's JSON response, with or without a surrounding code fence.
def _json_reply(text):
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return json.loads(fenced[1] if fenced else text)


# Validate the response and require a diagnostic for every call, even with no candidates.
def _parse_sample_reply(text, calls):
    if not text.strip():
        raise ValueError(tr("The model returned an empty voice sample response"))
    reply = _json_reply(text)
    if not isinstance(reply, dict):
        raise ValueError(tr("Expected a voice sample report with a summary, calls, and candidates"))
    summary, reports, entries = reply.get("summary"), reply.get("calls"), reply.get("candidates")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
        raise ValueError(tr("Expected a brief explanation of the voice sample outcome"))
    if not isinstance(reports, list) or len(reports) != len(calls):
        raise ValueError(tr("Expected one diagnostic report for every source call"))

    by_number = {}
    for report in reports:
        if not isinstance(report, dict):
            raise ValueError(tr("Invalid source call diagnostic"))
        number, status, detail = report.get("call"), report.get("read_status"), report.get("detail")
        if (type(number) is not int or not 1 <= number <= len(calls) or number in by_number
                or not isinstance(status, str) or status not in READ_STATUSES
                or not isinstance(detail, str) or not detail.strip() or len(detail) > 500):
            raise ValueError(tr("Invalid source call diagnostic"))
        by_number[number] = {"call": number, "read_status": status, "detail": detail.strip()}
    reports = [by_number[number] for number in sorted(by_number)]
    return summary.strip(), reports, _parse_candidates(entries, calls, by_number)


# Compare two intervals in the same original recording.
def _source_intervals_overlap(left, right):
    return (left["audio"] == right["audio"]
            and max(left["start_ms"], right["start_ms"]) < min(left["end_ms"], right["end_ms"]))


# Validate candidate intervals, corroboration, and overlap using the per-call report.
def _parse_candidates(entries, calls, reports):
    if not isinstance(entries, list) or len(entries) > config.VOICE_SAMPLE_MAX_CANDIDATES:
        raise ValueError(tr("Expected up to {count} voice sample candidates", count=config.VOICE_SAMPLE_MAX_CANDIDATES))
    candidates = []
    for entry in entries:
        call, metadata = _parse_interval(entry, calls)
        cited = [entry["call"], *entry["supporting_calls"]]
        if any(reports[number]["read_status"] != "read" for number in cited):
            raise ValueError(tr("A candidate cited a call without readable audio"))
        current = metadata["source"]
        if any(_source_intervals_overlap(previous["source"], current) for _, previous in candidates):
            raise ValueError(tr("Voice sample candidates overlap"))
        candidates.append((call, metadata))
    return candidates


# Run one independent audio request and clean up its completed session.
def _request_voice_reply(prompt, args):
    rc, stdout, stderr, home = run_agy(prompt, args.model, args.timeout, not args.no_custom_agent)
    # Structured diagnostics can mention safety without indicating a provider failure.
    error, text, usage, conversation = extract_envelope_error_and_usage(rc, stdout, stderr, detect_safety=False)
    try:
        if error:
            raise ValueError(error[:500])
        return text, usage
    finally:
        agy_forget(home, conversation)


# Choose the language for model diagnostics shown in the archive's logs.
def _diagnostic_language(args):
    return "Russian" if getattr(args, "ui", config.DEFAULT_UI) == "ru" else "English"


# Ask for several clips using the same comparative listening session.
def _request_candidates(person, calls, args):
    minimum_calls = config.VOICE_SAMPLE_MIN_SOURCE_CALLS
    inputs = [{"call": n, "file": item["clean_abs"], "direction": item["direction"],
               "duration_ms": item["duration_ms"]} for n, item in enumerate(calls, 1)]
    prompt = load_prompt(
        "voice-sample.md", person=person,
        calls=json.dumps(inputs, ensure_ascii=False),
        diagnostic_language=_diagnostic_language(args),
        min_seconds=f"{config.VOICE_SAMPLE_MIN_MS / 1000:g}", max_seconds=f"{config.VOICE_SAMPLE_MAX_MS / 1000:g}",
        preferred_seconds=f"{config.VOICE_SAMPLE_PREFERRED_MS / 1000:g}", min_calls=minimum_calls,
        max_candidates=config.VOICE_SAMPLE_MAX_CANDIDATES,
    )
    record("voice_sample_request", items=calls, person=person, model=args.model, calls=inputs)
    text = ""
    try:
        text, usage = _request_voice_reply(prompt, args)
        try:
            summary, reports, candidates = _parse_sample_reply(text, calls)
        except (TypeError, ValueError) as exc:
            raise ValueError(tr("Invalid voice sample response: {error}", error=exc)) from exc
        record("voice_sample_result", items=calls, person=person, model=args.model, summary=summary,
               call_diagnostics=reports, candidate_count=len(candidates), response=text, usage=usage)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        record("voice_sample_request_error", items=calls, person=person, model=args.model, response=text, error=exc)
        raise
    if not candidates and not STOP_EVENT.is_set():
        readable = sum(report["read_status"] == "read" for report in reports)
        if readable < minimum_calls:
            raise ValueError(tr("The model could read {count} of {total} calls. Comparing voices needs at least {required}.",
                                count=readable, total=len(calls), required=minimum_calls))
        raise ValueError(tr("No suitable voice clip found in the reviewed calls."))
    return candidates


# Require one uniquely numbered report per clip.
def _parse_clip_reports(text, count):
    reply = _json_reply(text)
    entries = reply.get("clips") if isinstance(reply, dict) else None
    if not isinstance(entries, list) or len(entries) != count:
        raise ValueError(tr("Expected one report per voice clip"))
    reports = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(tr("Invalid voice clip report"))
        number, detail = entry.get("clip"), entry.get("detail")
        if (type(number) is not int or not 1 <= number <= count or number in reports
                or not isinstance(detail, str) or not detail.strip() or len(detail) > 500):
            raise ValueError(tr("Invalid voice clip report"))
        reports[number] = dict(entry, detail=detail.strip())
    return [reports[number] for number in range(1, count + 1)]


# Validate the statuses before accepting the model's speaker counts.
def _parse_clip_check(text, count):
    reports = _parse_clip_reports(text, count)
    for report in reports:
        status = report.get("status")
        if not isinstance(status, str) or status not in CLIP_STATUSES:
            raise ValueError(tr("Invalid speaker check result"))
    return reports


# Check the extracted clips in a fresh session before publishing any candidates.
def _check_voice_clips(person, clips, args, *, repaired=False):
    inputs = [{"clip": number, "file": str(path)} for number, path in enumerate(clips, 1)]
    items = [{"audio_rel": metadata["source"]["audio"]} for metadata in clips.values()]
    prompt = load_prompt("voice-sample-check.md", clips=json.dumps(inputs, ensure_ascii=False),
                         diagnostic_language=_diagnostic_language(args))
    record("voice_sample_check_request", items=items, person=person, model=args.model, clips=inputs, repaired=repaired)
    text = ""
    try:
        text, usage = _request_voice_reply(prompt, args)
        try:
            reports = _parse_clip_check(text, len(clips))
        except ValueError as exc:
            raise ValueError(tr("Invalid voice clip verification response: {error}", error=exc)) from exc
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        record("voice_sample_check_error", items=items, person=person, model=args.model,
               repaired=repaired, response=text, error=exc)
        raise
    record("voice_sample_check_result", items=items, person=person, model=args.model, clips=reports,
           repaired=repaired, response=text, usage=usage)
    return reports


# Validate local repair offsets against the exact window sent to the model.
def _parse_clip_repairs(text, windows):
    reports = _parse_clip_reports(text, len(windows))
    for report, window in zip(reports, windows, strict=True):
        interval = report.get("interval", ...)
        if interval is None:
            continue
        if not isinstance(interval, dict):
            raise ValueError(tr("Invalid adjusted voice clip interval"))
        start, end = interval.get("start_ms"), interval.get("end_ms")
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= window["duration_ms"]
                or not config.VOICE_SAMPLE_MIN_MS <= end - start <= config.VOICE_SAMPLE_MAX_MS
                or (start, end) == (window["previous_start_ms"], window["previous_end_ms"])):
            raise ValueError(tr("Invalid adjusted voice clip interval"))
    return reports


# Find adjusted intervals using short windows around the rejected cuts.
def _repair_voice_clips(person, clips, calls, reference, args):
    calls_by_path = {call["audio_rel"]: call for call in calls}
    items = [{"audio_rel": metadata["source"]["audio"]} for metadata in clips.values()]
    text = ""
    try:
        with tempfile.TemporaryDirectory(prefix="diaryze-voice-repair-") as temporary:
            windows = []
            for number, metadata in enumerate(clips.values(), 1):
                source = metadata["source"]
                call = calls_by_path[source["audio"]]
                start = max(0, source["start_ms"] - config.VOICE_SAMPLE_REPAIR_CONTEXT_MS)
                end = min(call["duration_ms"], source["end_ms"] + config.VOICE_SAMPLE_REPAIR_CONTEXT_MS)
                window_file = Path(temporary) / f"{number}.flac"
                _extract(call, dict(source, start_ms=start, end_ms=end), window_file)
                windows.append({"clip": number, "file": str(window_file), "duration_ms": end - start,
                                "previous_start_ms": source["start_ms"] - start,
                                "previous_end_ms": source["end_ms"] - start,
                                "direction": call["direction"], "owner_evidence": metadata["evidence"]})
            prompt = load_prompt(
                "voice-sample-repair.md", person=person, windows=json.dumps(windows, ensure_ascii=False),
                reference=json.dumps(str(reference) if reference else None, ensure_ascii=False),
                diagnostic_language=_diagnostic_language(args),
                min_seconds=f"{config.VOICE_SAMPLE_MIN_MS / 1000:g}", max_seconds=f"{config.VOICE_SAMPLE_MAX_MS / 1000:g}",
            )
            if STOP_EVENT.is_set():
                return {}
            record("voice_sample_repair_request", items=items, person=person, model=args.model, windows=windows,
                   reference=str(reference) if reference else None)
            text, usage = _request_voice_reply(prompt, args)
            reports = _parse_clip_repairs(text, windows)
            record("voice_sample_repair_result", items=items, person=person, model=args.model,
                   clips=reports, response=text, usage=usage)
            if STOP_EVENT.is_set():
                return {}
            repaired = {}
            for (clip, metadata), report, window in zip(clips.items(), reports, windows, strict=True):
                interval = report["interval"]
                if interval is None:
                    continue
                source = metadata["source"]
                window_start = source["start_ms"] - window["previous_start_ms"]
                adjusted = dict(source, start_ms=window_start + interval["start_ms"],
                                end_ms=window_start + interval["end_ms"])
                _extract(calls_by_path[source["audio"]], adjusted, clip)
                repaired[clip] = dict(metadata, source=adjusted, sha256=sha256_file(clip))
            return repaired
    except InterruptedError:
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        record("voice_sample_repair_error", items=items, person=person, model=args.model, response=text, error=exc)
        return {}


# Extract a clean standalone FLAC and confirm that decoding produced the selected span.
def _extract(call, source, clip):
    span = source["end_ms"] - source["start_ms"]
    if sha256_file(call["audio_abs"]) != source["sha256"]:
        raise ValueError(tr("Voice sample source changed during preparation: {path}", path=call["audio_rel"]))
    result = run_capture(
        ["ffmpeg", "-y", "-v", "error", "-xerror", "-ss", f"{source['start_ms'] / 1000:.3f}",
         "-i", call["clean_abs"], "-t", f"{span / 1000:.3f}", "-map", "0:a:0",
         "-map_metadata", "-1", "-c:a", "flac", str(clip)],
        capture_output=True, text=True, timeout=config.VOICE_SAMPLE_EXTRACT_TIMEOUT_SEC,
    )
    if result.returncode:
        raise ValueError(tr("Cannot extract voice sample: {error}", error=result.stderr.strip()[:300]))
    actual, error = probe_duration_ms(str(clip))
    if error or abs(actual - span) > config.VOICE_SAMPLE_DURATION_TOLERANCE_MS:
        raise ValueError(tr("Unexpected voice sample duration: {duration}", duration=error or actual))


# Identify a review by the source folder relative to the archive.
def _review_scope(source_dir):
    root = Path(config.ROOT).resolve()
    return Path(source_dir).resolve().relative_to(root).as_posix()


# Read each scope's provenance from the sidecars in the candidate folder.
def _review_sets(candidate):
    reviews = {}
    for path in sorted(candidate.glob("*.json")):
        name = path.with_suffix(".flac").name
        if not SAMPLE_NAME.fullmatch(name):
            continue
        if path.is_symlink():
            raise ValueError(tr("Expected local sample metadata at {path}", path=path))
        metadata = json.loads(path.read_text(encoding="utf-8"))
        scope = metadata.get("scope") if isinstance(metadata, dict) else None
        if not isinstance(scope, str) or not scope:
            raise ValueError(tr("Invalid voice sample provenance at {path}", path=path))
        reviews.setdefault(scope, {})[name] = metadata
    return reviews


# List the unmoved clips for the source folder's phone review.
def pending_voice_samples(person, source_dir):
    candidate = _review_dir(_channel_dir(person, "phone"), "candidate")
    review = _review_sets(candidate).get(_review_scope(source_dir), {})
    return [path for name in review if (path := candidate / name).is_file() and not path.is_symlink()]


# Serialize changes to one person's samples of the same recording type.
@contextmanager
def _review_lock(person, channel):
    directory = _sample_root().parent / ".work" / "voice_samples" / person
    lock_path = directory / f"{channel}.lock"
    if lock_path.resolve() != lock_path:
        raise ValueError(tr("Voice sample locks cannot use a symbolic link"))
    directory.mkdir(parents=True, exist_ok=True)
    with locked_path(lock_path, create=True, blocking=False):
        yield


# Reserve a dated filename that is free in both review folders and the staging folder.
def _next_sample_name(stamp, *folders):
    sequence = 1
    while True:
        name = f"{stamp}_{sequence:02}.flac"
        paths = [folder / name for folder in folders]
        if not any(os.path.lexists(path) or os.path.lexists(path.with_suffix(".json")) for path in paths):
            return name
        sequence += 1


# Cut and screen the clips before making the surviving set available for review.
def prepare_voice_samples(person, source_dir, args, *, target_item=None):
    channel = "phone"
    folder = _channel_dir(person, channel)
    candidate = _review_dir(folder, "candidate")
    confirmed = _review_dir(folder, "confirmed")
    scope = _review_scope(source_dir)
    with _review_lock(person, channel):
        if existing := pending_voice_samples(person, source_dir):
            return existing
        confirm_pending = _review_sets(candidate).get(scope, {})
        if any(os.path.lexists(confirmed / name) for name in confirm_pending):
            raise ValueError(tr("Selected clips are in {folder}. Start transcription to validate them.",
                                folder=confirmed))
        candidate.mkdir(parents=True, exist_ok=True)
        confirmed.mkdir(exist_ok=True)
        with (tempfile.TemporaryDirectory(prefix="diaryze-voice-inputs-") as inputs,
              tempfile.TemporaryDirectory(dir=candidate, prefix=".prepare-") as temporary):
            target_day = _recording_day(target_item) if target_item is not None else None
            calls = _prepare_calls(person, source_dir, target_day, Path(inputs), args)
            if STOP_EVENT.is_set():
                return []
            minimum_calls = config.VOICE_SAMPLE_MIN_SOURCE_CALLS
            if len(calls) < minimum_calls:
                record("voice_sample_insufficient_calls", items=calls, person=person, folder=scope,
                       required=minimum_calls)
                raise ValueError(tr("Prepared {count} calls. Voice comparison needs {required} readable calls with different contacts.",
                                    count=len(calls), required=minimum_calls))
            candidates = _request_candidates(person, calls, args)
            if STOP_EVENT.is_set():
                return []
            staged = Path(temporary)
            clips = {}
            for call, metadata in candidates:
                name = _next_sample_name(metadata["recorded_on"], candidate, confirmed, staged, folder)
                clip = staged / name
                _extract(call, metadata["source"], clip)
                metadata.update(person=person, channel=channel, scope=scope, model=args.model,
                                sha256=sha256_file(clip))
                clips[clip] = metadata
            checks = dict(zip(clips, _check_voice_clips(person, clips, args), strict=True))
            if STOP_EVENT.is_set():
                return []
            rejected = {clip: clips[clip] for clip, report in checks.items() if report["status"] == "multiple_speakers"}
            repaired = {}
            if rejected:
                reference = next((clip for clip, report in checks.items() if report["status"] == "single_speaker"), None)
                repaired = _repair_voice_clips(person, rejected, calls, reference, args)
                if repaired and not STOP_EVENT.is_set():
                    try:
                        reports = _check_voice_clips(person, repaired, args, repaired=True)
                    except InterruptedError:
                        raise
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        # The verification error is logged. Keep the original passing clips.
                        pass
                    else:
                        clips.update(repaired)
                        checks.update(zip(repaired, reports, strict=True))
            if STOP_EVENT.is_set():
                return []
            passed = []
            for clip in sorted(checks, key=lambda path: path in repaired):
                report = checks[clip]
                if report["status"] != "single_speaker":
                    continue
                metadata = clips[clip]
                if any(_source_intervals_overlap(metadata["source"], clips[other]["source"]) for other in passed):
                    record("voice_sample_repair_overlap", items=[{"audio_rel": metadata["source"]["audio"]}],
                           person=person, clip=clip.name)
                    continue
                if sha256_file(clip) != metadata["sha256"]:
                    raise ValueError(tr("The selected sample or its metadata changed: {path}", path=clip))
                metadata["speaker_check"] = {"model": args.model, "status": report["status"], "detail": report["detail"]}
                clip.with_suffix(".json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                passed.append(clip)
            if not passed:
                raise ValueError(tr("No clip passed the single-speaker check."))
            for clip in passed:
                metadata = clip.with_suffix(".json")
                metadata.replace(candidate / metadata.name)
                clip.replace(candidate / clip.name)
        return pending_voice_samples(person, source_dir)


# Check that a selected named clip still matches its staged provenance.
def _validate_confirmation(person, channel, clip, metadata):
    match = SAMPLE_NAME.fullmatch(clip.name)
    if not match or not clip.is_file() or clip.is_symlink():
        raise ValueError(tr("Expected a candidate FLAC at {path}", path=clip))
    stamp = metadata.get("recorded_on")
    if (match[1] != stamp or not _day(stamp) or metadata.get("person") != person
            or metadata.get("channel") != channel or metadata.get("sha256") != sha256_file(clip)):
        raise ValueError(tr("The selected sample or its metadata changed: {path}", path=clip))


# Publish verified provenance and clear the finished review without touching other sets.
def _finish_review(person, channel, candidate, confirmed, review, selected):
    for name in selected:
        _validate_confirmation(person, channel, confirmed / name, review[name])
    with tempfile.TemporaryDirectory(dir=confirmed, prefix=".approve-") as temporary:
        for name in selected:
            source = (candidate / name).with_suffix(".json")
            destination = confirmed / source.name
            raw = source.read_bytes()
            if os.path.lexists(destination):
                if destination.is_symlink() or destination.read_bytes() != raw:
                    raise ValueError(tr("Conflicting sample provenance at {path}", path=destination))
                continue
            staged = Path(temporary) / source.name
            staged.write_bytes(raw)
            staged.replace(destination)
    for name in review.keys() - selected:
        _discard_proposal(candidate / name, review[name])
    for name in selected:
        _discard_proposal(candidate / name, review[name])
    return [confirmed / name for name in sorted(selected)]


# Clear only recognizable candidate audio and its matching provenance.
def _discard_proposal(path, metadata):
    if path.is_file() and not path.is_symlink() and sha256_file(path) == metadata.get("sha256"):
        path.unlink()
    path.with_suffix(".json").unlink()


# Import user-moved clips from each channel before opening the queue.
def import_confirmed_voice_samples():
    base = _sample_root()
    if base.resolve() != base:
        raise ValueError(tr("Voice sample library cannot use a symbolic link"))
    for person in sorted(base.iterdir()) if base.is_dir() else ():
        if not PERSON_ID.fullmatch(person.name) or person.is_symlink() or not person.is_dir():
            continue
        for channel in CHANNELS:
            folder = _channel_dir(person.name, channel)
            candidate = _review_dir(folder, "candidate")
            confirmed = _review_dir(folder, "confirmed")
            if not candidate.is_dir() or not confirmed.is_dir():
                continue
            with _review_lock(person.name, channel):
                for review in _review_sets(candidate).values():
                    selected = {name for name in review if os.path.lexists(confirmed / name)}
                    if selected:
                        yield from _finish_review(person.name, channel, candidate, confirmed, review, selected)


# Approve a clip from the terminal or discard a pending review by file or folder.
def review_voice_sample(path, *, approve):
    path = Path(os.path.abspath(Path(config.ROOT) / path))
    filename = path.name if path.suffix == ".flac" else None
    directory = path.parent if filename else path
    relative = directory.relative_to(_sample_root())
    if len(relative.parts) != 3 or relative.name not in {"candidate", "confirmed"} or path.resolve() != path:
        raise ValueError(tr("Expected a path to a voice sample candidate"))
    person, channel, status = relative.parts
    if (approve and not filename) or (not approve and status != "candidate"):
        raise ValueError(tr("Expected a FLAC for approval or a candidate folder or FLAC for rejection"))
    folder = _channel_dir(person, channel)
    candidate = _review_dir(folder, "candidate")
    confirmed = _review_dir(folder, "confirmed")
    with _review_lock(person, channel):
        reviews = _review_sets(candidate)
        matching = [review for review in reviews.values() if filename is None or filename in review]
        if len(matching) != 1:
            raise ValueError(tr("Choose a FLAC to identify one pending voice review"))
        review, = matching
        selected = {name for name in review if os.path.lexists(confirmed / name)}
        if not approve:
            if selected:
                raise ValueError(tr("Move the selected clips back into candidate before rejecting this set"))
            for name, metadata in review.items():
                _discard_proposal(candidate / name, metadata)
            return None
        if status == "candidate":
            if filename in selected:
                raise ValueError(tr("Sample already exists at {path}", path=confirmed / filename))
            _validate_confirmation(person, channel, path, review[filename])
            confirmed.mkdir(exist_ok=True)
            path.rename(confirmed / filename)
            selected.add(filename)
        if filename not in selected:
            raise ValueError(tr("Expected the selected FLAC at {path}", path=path))
        _finish_review(person, channel, candidate, confirmed, review, selected)
        return confirmed / filename
