"""Timecodes, response parsing, speaker validation and Markdown rendering."""

from datetime import datetime
from decimal import Decimal
import json
import re
import yaml

from . import config
from .config import TRANSCRIPT_SCHEMA_VERSION, TS_LINE_RE, TZ, VALID_SOURCES
from .identification import identification_status
from .owners import with_silent_phone_owner
from .catalog_fields import duration_seconds, valid_summary, valid_spam_score


class MetadataDumper(yaml.SafeDumper):
    pass


MetadataDumper.add_representer(Decimal, lambda dumper, value:
    dumper.represent_scalar('tag:yaml.org,2002:float', format(value, '.2f')))


# Format a millisecond offset as an archive timestamp.
def format_ts(ms: int) -> str:
    cs_total = int(round(ms / 10))
    cs = cs_total % 100
    sec_total = cs_total // 100
    s = sec_total % 60
    m = (sec_total // 60) % 60
    h = sec_total // 3600
    return f"{h:02d}:{m:02d}:{s:02d}.{cs:02d}"


# Parse an utterance timestamp into milliseconds.
def parse_ts_ms(h: str | None, m: str, s: str, frac: str) -> int:
    cs = int(frac) if len(frac) == 2 else int(round(int(frac) / 10))
    return ((int(h or 0) * 3600) + (int(m) * 60) + int(s)) * 1000 + cs * 10


# Separate a batch response into blocks keyed by recording number.
def parse_item_blocks(text: str, count: int) -> dict[int, str]:
    if count == 1 and "=== ITEM 1 ===" not in text:
        return {1: text.strip()}
    blocks = {}
    matches = list(re.finditer(r"^===\s*ITEM\s+(\d+)\s*===\s*$", text, re.M))
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks[num] = text[start:end].strip()
    return blocks


# Extract speaker metadata and timed utterances from one response.
def parse_speakers_and_lines(raw_block: str, offset_ms: int = 0):
    speakers = None
    lines = []
    no_speech = False
    for raw in raw_block.splitlines():
        s = raw.strip()
        if not s or s.startswith("```"):
            continue
        if s.startswith("SPEAKERS_JSON:"):
            payload = s.split("SPEAKERS_JSON:", 1)[1].strip()
            speakers = json.loads(payload)
            continue
        if s == "NO_SPEECH":
            no_speech = True
            continue
        if s.startswith("["):
            m = TS_LINE_RE.match(s)
            if not m:
                raise ValueError(f"invalid line: {s[:120]}")
            local_ms = parse_ts_ms(*m.groups()[:4])
            sp_name = (m.group("sp_b") or m.group("sp_p")).strip()
            lines.append((local_ms + offset_ms, sp_name, m.group("text").strip()))
    return speakers, lines, no_speech


# Check that a speaker key can be rendered in transcript lines.
def _valid_speaker_key(sp_key: str) -> bool:
    return (
        bool(sp_key)
        and sp_key not in {"?", "unknown"}
        and not any(c in sp_key for c in "*:\r\n")
        and not any(ord(c) < 32 for c in sp_key)
    )


# Find a usable, unique speaker label for an invalid key.
def _salvage_speaker_key(raw_key: str, existing: dict) -> str:
    cleaned = "".join(c if ord(c) >= 32 and c not in "*:\r\n" else " " for c in raw_key).strip()
    if _valid_speaker_key(cleaned) and cleaned not in existing:
        return cleaned
    idx = 1
    while f"Голос {idx}" in existing:
        idx += 1
    return f"Голос {idx}"


# Validate speaker links and timecodes and render the final Markdown.
def validate_and_render_md(
    item: dict,
    speakers: dict | None,
    lines: list[tuple[int, str, str]],
    no_speech: bool,
    model: str,
    people: dict,
    orgs: dict,
    salvage: bool = False,
    extra_notes: list[str] | None = None,
    register_catalog=None,
) -> str:
    notes = list(extra_notes or [])
    if no_speech and not lines:
        speakers = {}
    elif not isinstance(speakers, dict) or not speakers:
        if not salvage:
            raise ValueError("missing or empty speakers")
        speakers = {}
        notes.append("Внимание: модель не вернула блок speakers, подставлены по умолчанию.")

    clean_speakers = {}
    remap = {}
    for sp, info in speakers.items():
        raw_key = str(sp).strip()
        sp_key = raw_key
        if not _valid_speaker_key(sp_key):
            if not salvage:
                raise ValueError(f"invalid speaker id: {sp}")
            sp_key = _salvage_speaker_key(raw_key, clean_speakers)
            remap[raw_key] = sp_key
        if not isinstance(info, dict):
            if not salvage:
                raise ValueError(f"speaker {sp} not a dict")
            info = {"label": str(info)}
        info = dict(info)
        if "identity_note" in info:
            legacy_note = info.pop("identity_note")
            info.setdefault("note", legacy_note)
        if "basis" in info:
            legacy_basis = info.pop("basis")
            info.setdefault("source", legacy_basis)
        if info.get("source") == "audio":
            info["source"] = "speech"
        if info.get("person") == "unknown":
            info["person"] = None
        clean_speakers[sp_key] = info

    if register_catalog is not None and not salvage:
        register_catalog(clean_speakers, item, people, orgs)

    for sp_key, info in list(clean_speakers.items()):
        info.pop("nicknames", None)
        info.pop("nickname", None)
        if "note" in info and (not isinstance(info["note"], str) or not info["note"].strip()):
            if not salvage:
                raise ValueError(f"invalid note for {sp_key}")
            info.pop("note", None)
        if info.get("person") is not None and info["person"] not in people:
            if not salvage:
                raise ValueError(f"unknown person: {info['person']}")
            bad_p = info.pop("person")
            info["person"] = None
            info.pop("source", None)
            info.setdefault("label", bad_p)
            notes.append(f"Убран неизвестный person={bad_p} (отсутствует в people.yaml).")
        if info.get("org") is not None and info["org"] not in orgs:
            if not salvage:
                raise ValueError(f"unknown org: {info['org']}")
            bad_o = info.pop("org")
            info.pop("source", None)
            info.setdefault("label", bad_o)
            notes.append(f"Убран неизвестный org={bad_o} (отсутствует в orgs.yaml).")
        if info.get("source") is not None and info["source"] not in VALID_SOURCES:
            if not salvage:
                raise ValueError(f"invalid source: {info['source']}")
            info.pop("source", None)
        if "temp" in info:
            if not isinstance(info["temp"], bool):
                if not salvage:
                    raise ValueError(f"invalid temp for {sp_key}: {info['temp']}")
                info.pop("temp", None)
            elif not info["temp"]:
                info.pop("temp", None)
        is_robot_org = info.get("role") == "robot" and isinstance(info.get("org"), str)
        if "person" not in info and not is_robot_org:
            info["person"] = None
        ordered = {}
        for k in ("person", "org", "role", "source", "temp", "label", "note"):
            if k in info:
                ordered[k] = info[k]
        for k, v in info.items():
            if k not in ordered:
                ordered[k] = v
        clean_speakers[sp_key] = ordered
    speakers = clean_speakers

    dur = item["duration_ms"]
    if not lines and not no_speech:
        raise ValueError("empty transcript without NO_SPEECH")

    prev_ms = 0
    clamped_over = 0
    reordered = 0
    body_lines = []
    for ms, sp, text in lines:
        sp = remap.get(sp.strip(), sp.strip())
        if sp in {"?", "unknown"}:
            sp = "?"
        elif sp not in speakers:
            if not salvage:
                raise ValueError(f"undeclared speaker: {sp}")
            speakers[sp] = {"person": None, "label": sp}
        if ms > dur + 1000:
            if not salvage:
                raise ValueError(f"timestamp {ms} > duration_ms+1000 ({dur+1000})")
            clamped_over += 1
        clamped = dur if ms > dur else max(0, ms)
        if clamped < prev_ms:
            if not salvage:
                raise ValueError(f"non-monotonic timestamp {clamped} < {prev_ms}")
            clamped = prev_ms
            reordered += 1
        prev_ms = clamped
        body_lines.append(f"[{format_ts(clamped)}] **{sp}** {text}" if text else f"[{format_ts(clamped)}] **{sp}**")

    if clamped_over:
        notes.append(f"Внимание: {clamped_over} таймкод(ов) превышали длительность и были подрезаны до {format_ts(dur)}.")
    if reordered:
        notes.append(f"Внимание: {reordered} таймкод(ов) нарушали монотонность и были выровнены.")

    speakers = with_silent_phone_owner(
        speakers, item["audio_rel"], people,
        used_speakers={sp for _, sp, _ in lines}, direction=item.get("direction"), root=config.ROOT,
    )
    meta = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "summary": valid_summary(item.get('summary')),
        "spam_score": valid_spam_score(item.get('spam_score')),
        "identification": identification_status(speakers, people, orgs,
            used_speakers={sp for _, sp, _ in lines}, no_speech=no_speech),
        "audio": item["audio_rel"],
        "sha256": item["sha256"],
        "duration_s": duration_seconds(dur),
    }
    if item.get("recorded_at") or item.get("dt"):
        meta["recorded_at"] = item.get("recorded_at") or item.get("dt")
    if item.get("direction"):
        meta["direction"] = item["direction"]
    if item.get("contact"):
        meta["contact"] = item["contact"]
    if item.get("phones"):
        meta["phones"] = item["phones"]
    if item.get("language"):
        meta["language"] = item["language"]
    meta["model"] = model
    meta["voice_sample"] = item.get("voice_sample")
    meta["created_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    if notes:
        meta["notes"] = " ".join(notes)
    meta["speakers"] = speakers

    yaml_text = yaml.dump(meta, Dumper=MetadataDumper, allow_unicode=True, sort_keys=False, width=1000).strip()
    body = ("\n".join(body_lines) if body_lines else "[без речи]") + "\n"
    return f"---\n{yaml_text}\n---\n\n{body}"


# Format a recording duration for compact display.
def format_duration_short(ms: int) -> str:
    sec = ms / 1000.0
    if sec < 60:
        return f"{sec:4.0f}s"
    return f"{sec / 60.0:4.1f}m"


# Summarize speaker labels and catalog links for the console.
def format_speakers_brief(speakers: dict) -> str:
    parts = []
    for sp, info in (speakers or {}).items():
        if not isinstance(info, dict):
            parts.append(f"{sp}:?")
            continue
        person = info.get("person")
        if person == "unknown":
            person = None
        who = person or info.get("org") or info.get("label")
        if who and who != sp:
            parts.append(f"{sp}: {who}")
        else:
            parts.append(str(sp))
    return ", ".join(parts) or "-"
