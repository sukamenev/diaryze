"""Conservative speaker-identification status and lossless Markdown migration."""

import re

import yaml

from .config import TRANSCRIPT_SCHEMA_VERSION, TS_LINE_RE


STATUSES = {"unresolved", "partial", "complete", "not_applicable"}
LEGACY_STATUSES = STATUSES | {"not_checked"}
STATUS_LABELS = {
    "unresolved": "не установлены",
    "partial": "частично",
    "complete": "установлены",
    "not_applicable": "без речи",
}


# Check whether a speaker has an accepted catalog link and source.
def confirmed_identity(info, people, orgs):
    if not isinstance(info, dict):
        return False
    source = info.get("source") if "source" in info else info.get("basis")
    if source not in {"speech", "audio", "user", "filename"}:
        return False
    person = info.get("person")
    if isinstance(person, str) and person != "unknown" and person in people:
        return True
    # An organization identifies a robot/IVR, but not an unnamed human operator.
    return info.get("role") == "robot" and isinstance(info.get("org"), str) and info["org"] in orgs


# Compute how many declared or used speaker keys are identified.
def identification_status(speakers, people, orgs, *, used_speakers=(), no_speech=False):
    speakers = speakers if isinstance(speakers, dict) else {}
    used_speakers = set(used_speakers)
    if no_speech and not used_speakers:
        return "not_applicable"
    # Inline '?' / legacy 'unknown' marks an unclear utterance, not an extra participant.
    ids = set(speakers) | (used_speakers - {"?", "unknown"})
    if not ids:
        return "unresolved"
    linked = 0
    for speaker in ids:
        if speaker in {"?", "unknown"}:
            continue
        info = speakers.get(speaker) or {}
        if not isinstance(info, dict):
            continue
        linked += confirmed_identity(info, people, orgs)
    if linked == len(ids):
        return "complete"
    if linked:
        return "partial"
    return "unresolved"


# Separate YAML front matter from the transcript while retaining the original lines.
def split_markdown(raw):
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != b"---":
        raise ValueError("Markdown has no YAML front matter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == b"---"), None)
    if end is None:
        raise ValueError("Unclosed YAML front matter")
    meta = yaml.safe_load(b"".join(lines[1:end]).decode("utf-8"))
    if not isinstance(meta, dict):
        raise ValueError("YAML front matter must be a mapping")
    return lines, end, meta


# Recalculate identification using the stored speakers and utterance keys.
def status_from_markdown(raw, people, orgs):
    lines, end, meta = split_markdown(raw)
    body = b"".join(lines[end + 1:]).decode("utf-8")
    used = {
        (m.group("sp_b") or m.group("sp_p")).strip()
        for line in body.splitlines()
        if (m := TS_LINE_RE.match(line.strip()))
    }
    no_speech = not used and bool(re.search(r"(?m)^\[без речи\]\s*$", body))
    return identification_status(meta.get("speakers"), people, orgs,
                                 used_speakers=used, no_speech=no_speech)


# Update the root schema and status fields while preserving other content.
def patch_markdown(raw, people, orgs):
    """Change only root schema/status lines; preserve every other byte."""
    lines, end, meta = split_markdown(raw)
    if str(meta.get("schema_version")) not in {"1.0", "1.1", "1.2", "2.0"}:
        raise ValueError("Unsupported transcript schema: " + str(meta.get("schema_version")))
    if "identification" in meta and (not isinstance(meta["identification"], str) or meta["identification"] not in LEGACY_STATUSES):
        raise ValueError("Unsupported identification value")
    status = status_from_markdown(raw, people, orgs)
    if meta.get("identification") == status:
        return raw, status
    version_index = None
    status_index = None
    for i in range(1, end):
        line = lines[i]
        ending = b"\r\n" if line.endswith(b"\r\n") else b"\n"
        text = line.removesuffix(ending)
        for key, value in ((b"schema_version", TRANSCRIPT_SCHEMA_VERSION), (b"identification", status)):
            if not text.startswith(key + b":"):
                continue
            match = re.fullmatch(rb"(" + key + rb":[ \t]*)(?:\"[^\"]*\"|'[^']*'|[^# \t]+)([ \t]*(?:#.*)?)", text)
            if not match:
                raise ValueError("Cannot safely update " + key.decode())
            if key == b"schema_version":
                version_index = i
            else:
                status_index = i
                lines[i] = match[1] + value.encode() + match[2] + ending
    if version_index is None:
        raise ValueError("Missing schema_version line")
    if status_index is None:
        ending = b"\r\n" if lines[version_index].endswith(b"\r\n") else b"\n"
        lines.insert(version_index + 1, b"identification: " + status.encode() + ending)
    return b"".join(lines), status
