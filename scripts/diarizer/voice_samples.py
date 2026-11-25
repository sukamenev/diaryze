"""Collect, review and select independent voice references for phone owners."""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from . import config
from .agy import agy_forget, extract_envelope_error_and_usage, run_agy
from .archive import is_audio_candidate, is_excluded_dir, parse_filename, sha256_file
from .audio import prepare_audio_work, probe_duration_ms
from .owners import find_folder_owner
from .processes import run_capture
from .prompt_files import load_prompt
from .runtime import STOP_EVENT


SAMPLE_NAME = re.compile(r"(\d{4}-\d{2}-\d{2}|undated)_\d{2,}\.flac")
PERSON_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
CHANNELS = ("phone", "recorder")
MIN_SAMPLE_MS = 10_000
MAX_SAMPLE_MS = 20_000
MAX_SOURCE_SEC = 180
MAX_SOURCE_CALLS = 4
MAX_CANDIDATES = 3


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
            capture_output=True, text=True, timeout=15,
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


# Find the approved sample directory for a person in the archive data.
def _sample_dir(person):
    if not isinstance(person, str) or not PERSON_ID.fullmatch(person):
        raise ValueError("Invalid voice sample person ID")
    return Path(config.ROOT).resolve() / "Data" / "voice_samples" / person


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
        folder = _sample_dir(person) / channel
        if folder.resolve() != folder:
            continue
        for path in folder.glob("*.flac"):
            match = SAMPLE_NAME.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
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
        folders[:] = [name for name in folders
                      if not is_excluded_dir(name) and not (base / name).is_symlink()]
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
                "audio_abs": str(path), "audio_rel": str(rel_dir / name), "dt": dt,
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
    while waiting and len(selected) < MAX_SOURCE_CALLS and not STOP_EVENT.is_set():
        index = next((i for i, call in enumerate(waiting) if call["direction"] not in directions), 0)
        call = waiting.pop(index)
        if call["contact_key"] in contacts:
            continue
        try:
            duration, _ = probe_duration_ms(call["audio_abs"])
            if duration is None or not MIN_SAMPLE_MS <= duration <= MAX_SOURCE_SEC * 1000:
                continue
            folder = temp / "inputs" / str(len(selected) + 1)
            folder.mkdir(parents=True, exist_ok=True)
            item, error = prepare_audio_work(
                call, str(folder), MAX_SOURCE_SEC, args.chunk_sec, args.overlap_sec,
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
        raise ValueError("Expected a voice sample object or null")
    number = reply.get("call")
    start, end = reply.get("start_ms"), reply.get("end_ms")
    evidence = reply.get("evidence")
    others = reply.get("supporting_calls")
    if (any(type(n) is not int for n in (number, start, end))
            or not 1 <= number <= len(calls)
            or not 0 <= start < end <= calls[number - 1]["duration_ms"]
            or not MIN_SAMPLE_MS <= end - start <= MAX_SAMPLE_MS
            or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500
            or not isinstance(others, list) or not others
            or any(type(n) is not int or not 1 <= n <= len(calls) or n == number for n in others)):
        raise ValueError("Invalid voice sample bounds or cross-call evidence")
    call = calls[number - 1]
    return call, {
        "schema_version": "1.0",
        "recorded_on": _day(call["dt"]).isoformat(),
        "source": {"audio": call["audio_rel"], "sha256": call["sha256"],
                   "start_ms": start, "end_ms": end},
        "supporting_calls": [calls[n - 1]["audio_rel"] for n in sorted(set(others))],
        "evidence": evidence.strip(),
    }


# Validate the model's ordered candidate list and ensure selected clips do not overlap.
def _parse_candidates(text, calls):
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    reply = json.loads(fenced[1] if fenced else text)
    if reply is None:
        return []
    if not isinstance(reply, list) or len(reply) > MAX_CANDIDATES:
        raise ValueError("Expected up to three voice sample candidates")
    candidates = []
    for entry in reply:
        call, metadata = _parse_interval(entry, calls)
        current = metadata["source"]
        if any(prior["audio"] == current["audio"] and
               max(prior["start_ms"], current["start_ms"]) < min(prior["end_ms"], current["end_ms"])
               for prior in (previous["source"] for _, previous in candidates)):
            raise ValueError("Voice sample candidates overlap")
        candidates.append((call, metadata))
    return candidates


# Ask for several clips using the same comparative listening session.
def _request_candidates(person, calls, args):
    prompt = load_prompt(
        "voice-sample.md", person=person,
        calls=json.dumps([
            {"call": n, "file": item["clean_abs"], "direction": item["direction"],
             "duration_ms": item["duration_ms"]}
            for n, item in enumerate(calls, 1)
        ], ensure_ascii=False),
    )
    rc, stdout, stderr, home = run_agy(prompt, args.model, args.timeout, not args.no_custom_agent)
    error, text, _, conversation = extract_envelope_error_and_usage(rc, stdout, stderr)
    try:
        if error:
            raise ValueError(error[:500])
        return _parse_candidates(text, calls)
    finally:
        agy_forget(home, conversation)


# Extract a clean standalone FLAC and confirm that decoding produced the selected span.
def _extract(call, source, clip):
    span = source["end_ms"] - source["start_ms"]
    if sha256_file(call["audio_abs"]) != source["sha256"]:
        raise ValueError(f"Voice sample source changed during preparation: {call['audio_rel']}")
    result = run_capture(
        ["ffmpeg", "-y", "-v", "error", "-xerror", "-ss", f"{source['start_ms'] / 1000:.3f}",
         "-i", call["clean_abs"], "-t", f"{span / 1000:.3f}", "-map", "0:a:0",
         "-map_metadata", "-1", "-c:a", "flac", str(clip)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise ValueError(f"Cannot extract voice sample: {result.stderr.strip()[:300]}")
    actual, error = probe_duration_ms(str(clip))
    if error or abs(actual - span) > 100:
        raise ValueError(f"Unexpected voice sample duration: {error or actual}")


# Locate the review folder for one owner and the folder that supplies its calls.
def _review_folder(person, source_dir):
    _sample_dir(person)  # Validate the shared path component.
    root = Path(config.ROOT).resolve()
    scope = Path(source_dir).resolve().relative_to(root)
    folder = root / "work" / "voice_samples" / person / scope
    if folder.resolve() != folder:
        raise ValueError("Voice sample review folder must be inside the archive")
    return folder


# List the available clips awaiting a decision in the current review folder.
def pending_voice_samples(person, source_dir):
    pending = _review_folder(person, source_dir) / "candidates"
    if pending.is_symlink():
        raise ValueError("Voice sample review cannot use a symbolic link")
    return sorted(path for path in pending.glob("[0-9][0-9].flac")
                  if path.is_file() and not path.is_symlink())


# Serialize sample preparation and review across CLI processes.
@contextmanager
def _review_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


# Stage a review set independently of the source recordings.
def prepare_voice_samples(person, source_dir, args, *, target_item=None):
    folder = _review_folder(person, source_dir)
    with _review_lock(_review_folder(person, config.ROOT)):
        if existing := pending_voice_samples(person, source_dir):
            return existing
        folder.mkdir(parents=True, exist_ok=True)
        pending = folder / "candidates"
        with (tempfile.TemporaryDirectory(prefix="diaryze-voice-inputs-") as inputs,
              tempfile.TemporaryDirectory(dir=folder, prefix=".prepare-") as temporary):
            target_day = _recording_day(target_item) if target_item is not None else None
            calls = _prepare_calls(person, source_dir, target_day, Path(inputs), args)
            if len(calls) < 2 or STOP_EVENT.is_set():
                return []
            candidates = _request_candidates(person, calls, args)
            if not candidates or STOP_EVENT.is_set():
                return []
            staged = Path(temporary) / "candidates"
            staged.mkdir()
            for number, (call, metadata) in enumerate(candidates, 1):
                clip = staged / f"{number:02}.flac"
                _extract(call, metadata["source"], clip)
                metadata.update(person=person, channel="phone", model=args.model, sha256=sha256_file(clip))
                clip.with_suffix(".json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                )
            staged.rename(pending)
        return pending_voice_samples(person, source_dir)


# Approve the chosen clip or discard a complete pending review set.
def review_voice_sample(path, *, approve):
    base = Path(config.ROOT).resolve() / "work" / "voice_samples"
    path = Path(config.ROOT).resolve() / path
    pending = path.parent if approve else path
    if (pending.name != "candidates" or pending.resolve() != pending
            or not pending.is_relative_to(base) or len(pending.relative_to(base).parts) < 2):
        raise ValueError("Expected a path to a pending voice sample set")
    person = pending.relative_to(base).parts[0]
    _sample_dir(person)
    with _review_lock(_review_folder(person, config.ROOT)):
        if not pending.is_dir():
            raise ValueError(f"No pending voice samples at {pending}")
        if not approve:
            shutil.rmtree(pending)
            return None
        clip = path
        provenance = clip.with_suffix(".json")
        if (not re.fullmatch(r"0[1-3]\.flac", clip.name)
                or not clip.is_file() or clip.is_symlink() or provenance.is_symlink()):
            raise ValueError("Expected an independent sample and its metadata")
        metadata = json.loads(provenance.read_text(encoding="utf-8"))
        stamp = metadata.get("recorded_on")
        if (not isinstance(metadata, dict) or metadata.get("person") != person or metadata.get("channel") != "phone"
                or not _day(stamp) or _day(stamp).isoformat() != stamp
                or metadata.get("sha256") != sha256_file(clip)):
            raise ValueError("The pending sample or its metadata changed")
        destination = _sample_dir(person) / "phone"
        if destination.resolve() != destination:
            raise ValueError("Voice sample destination must be inside the archive data")
        destination.mkdir(parents=True, exist_ok=True)
        sequence = 1
        while (path := destination / f"{stamp}_{sequence:02}.flac").exists() or path.is_symlink():
            if not path.is_symlink() and sha256_file(path) == metadata["sha256"]:
                break
            sequence += 1
        if not path.exists():
            with tempfile.TemporaryDirectory(dir=destination, prefix=".approve-") as temporary:
                staged_clip = Path(temporary) / clip.name
                staged_json = Path(temporary) / provenance.name
                shutil.copyfile(clip, staged_clip)
                shutil.copyfile(provenance, staged_json)
                staged_json.replace(path.with_suffix(".json"))
                staged_clip.replace(path)
        shutil.rmtree(pending)
        return path
