#!/usr/bin/env python3
"""Read-only search and filtering across Markdown transcripts."""

import argparse
import json
from pathlib import Path
import re
import sys

import yaml

if __package__:
    from .diarizer.config import ROOT, TS_LINE_RE, transcripts_dir
    from .diarizer.markdown import TranscriptFile
    from .diarizer.owners import find_folder_owner
else:
    from diarizer.config import ROOT, TS_LINE_RE, transcripts_dir
    from diarizer.markdown import TranscriptFile
    from diarizer.owners import find_folder_owner


BOT_NAME_HINTS = (
    "автоответчик", "автоинформатор", "голосовой секретарь", "голосовой ассистент",
    "голосовой помощник", "робот", "линия ожидания", "секретарь (автоответчик)",
)
WORD_RE = re.compile(r"[а-яёa-z0-9]+(?:[-'][а-яёa-z0-9]+)*", re.I)


# Count meaningful words and utterances and detect no-speech transcripts.
def meaningful_speech_stats(body: str) -> tuple[int, int, bool]:
    """Return (meaningful_word_count, utterance_line_count, is_no_speech) from MD body."""
    words = 0
    lines = 0
    for raw_line in body.splitlines():
        m = TS_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        lines += 1
        text = m.group("text")
        text = re.sub(r"\[возможно:\s*([^\]]+)\]", r"\1", text, flags=re.I)
        text = re.sub(r"\[[^\]]+\]", " ", text)
        words += len(WORD_RE.findall(text))
    no_speech = lines == 0 and bool(re.search(r"(?m)^\[без речи\]\s*$", body))
    return words, lines, no_speech


# Decide whether the relevant speakers are all robots or transient contacts.
def _is_bot_or_temp_only(speakers: dict, owner=None) -> bool:
    if not isinstance(speakers, dict) or not speakers:
        return False
    has_non_owner_bot = False
    for sp, info in speakers.items():
        if not isinstance(info, dict):
            return False
        pid = info.get("person")
        if pid is not None and (info.get("role") == "owner" or pid == owner):
            continue
        if pid is not None:
            return False
        low = sp.lower()
        is_bot = bool(info.get("temp") or info.get("role") == "robot" or any(h in low for h in BOT_NAME_HINTS))
        if not is_bot:
            return False
        has_non_owner_bot = True
    return has_non_owner_bot


# Resolve a requested search folder within the transcript tree.
def _resolve_search_dir(text_root: Path, folder: str | None) -> Path:
    if not folder or folder == ".":
        return text_root
    cleaned = folder.strip().lstrip("/")
    for prefix in ("Data/Text", "Text"):
        if cleaned == prefix:
            return text_root
        if cleaned.startswith(prefix + "/"):
            cleaned = cleaned[len(prefix) + 1:]
            break
    target = (text_root / cleaned).resolve()
    if not target.is_relative_to(text_root) or not target.is_dir():
        raise ValueError(f"Folder not found in the transcript tree: {folder}")
    return target


# Strip formatting from a phone number for matching.
def _phone_digits(raw: str) -> str:
    return re.sub(r"\D+", "", str(raw or ""))


# Filter transcripts by metadata, participants, and spoken content.
def search_transcripts(
    root,
    *,
    folder: str | None = None,
    min_sec: float | None = None,
    max_sec: float | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
    min_lines: int | None = None,
    max_lines: int | None = None,
    no_speech: bool = False,
    min_spam: float | None = None,
    max_spam: float | None = None,
    unscored_spam: bool = False,
    only_bot: bool = False,
    person: str | None = None,
    org: str | None = None,
    identification: list[str] | None = None,
    direction: str | None = None,
    phone: str | None = None,
    contact: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    text_query: str | None = None,
    sort_by: str = "date",
    asc: bool = False,
    limit: int = 0,
) -> list[dict]:
    root_path = Path(root).resolve()
    text_root = transcripts_dir(root_path).resolve()
    search_dir = _resolve_search_dir(text_root, folder)
    id_filter = set(identification or [])
    phone_needle = _phone_digits(phone) if phone else ""
    contact_needle = (contact or "").strip().lower()
    text_re = re.compile(text_query, re.I) if text_query else None

    matches = []
    for path in search_dir.rglob("*.md"):
        if path.is_symlink() or not path.resolve().is_relative_to(text_root):
            continue
        try:
            meta, body = TranscriptFile(path).read()
        except (OSError, ValueError, yaml.YAMLError):
            continue

        dur = meta.get("duration_s")
        if min_sec is not None and (dur is None or float(dur) < min_sec):
            continue
        if max_sec is not None and (dur is None or float(dur) > max_sec):
            continue

        spam = meta.get("spam_score")
        if unscored_spam and spam is not None:
            continue
        if min_spam is not None and (spam is None or float(spam) < min_spam):
            continue
        if max_spam is not None and (spam is None or float(spam) > max_spam):
            continue

        if id_filter and meta.get("identification") not in id_filter:
            continue
        if direction:
            dir_norm = {"incoming": "in", "outgoing": "out"}.get(direction, direction)
            meta_dir = {"incoming": "in", "outgoing": "out"}.get(meta.get("direction"), meta.get("direction"))
            if meta_dir != dir_norm:
                continue

        rec_at = str(meta.get("recorded_at") or "")
        if date_from and (not rec_at or rec_at < date_from):
            continue
        if date_to and (not rec_at or rec_at[:len(date_to)] > date_to):
            continue

        if contact_needle and contact_needle not in str(meta.get("contact") or "").lower():
            continue
        if phone_needle:
            ph_list = [_phone_digits(p) for p in (meta.get("phones") or [])]
            if not any(phone_needle in p for p in ph_list):
                continue

        speakers = meta.get("speakers") if isinstance(meta.get("speakers"), dict) else {}
        if person and not any(isinstance(v, dict) and v.get("person") == person for v in speakers.values()):
            continue
        if org and not any(isinstance(v, dict) and v.get("org") == org for v in speakers.values()):
            continue
        if only_bot:
            audio_rel = path.relative_to(text_root).as_posix()[:-3]
            try:
                folder_meta, _ = find_folder_owner(audio_rel, root=root_path)
            except (OSError, yaml.YAMLError):
                folder_meta = None
            owner = folder_meta.get("owner") if folder_meta and folder_meta.get("type") == "phone" else None
            if not _is_bot_or_temp_only(speakers, owner):
                continue

        words, utt_lines, body_no_speech = meaningful_speech_stats(body)
        if no_speech and not body_no_speech:
            continue
        if min_words is not None and words < min_words:
            continue
        if max_words is not None and words > max_words:
            continue
        if min_lines is not None and utt_lines < min_lines:
            continue
        if max_lines is not None and utt_lines > max_lines:
            continue

        if text_re:
            haystack = f"{meta.get('summary') or ''}\n{body}"
            if not text_re.search(haystack):
                continue

        rel_md = path.relative_to(root_path).as_posix()
        matches.append({
            "md": rel_md,
            "audio": meta.get("audio") or path.relative_to(text_root).as_posix()[:-3],
            "recorded_at": meta.get("recorded_at"),
            "duration_s": float(dur) if dur is not None else None,
            "words": words,
            "lines": utt_lines,
            "no_speech": body_no_speech,
            "spam_score": spam,
            "identification": meta.get("identification"),
            "direction": meta.get("direction"),
            "contact": meta.get("contact"),
            "phones": meta.get("phones") or [],
            "speakers": list(speakers.keys()),
            "summary": meta.get("summary"),
        })

    key_fns = {
        "date": lambda r: (r["recorded_at"] or "", r["md"]),
        "duration": lambda r: (r["duration_s"] if r["duration_s"] is not None else -1.0, r["md"]),
        "words": lambda r: (r["words"], r["md"]),
        "lines": lambda r: (r["lines"], r["md"]),
        "spam": lambda r: (r["spam_score"] if r["spam_score"] is not None else -1.0, r["md"]),
    }
    matches.sort(key=key_fns[sort_by], reverse=not asc)
    if limit and limit > 0:
        matches = matches[:limit]
    return matches


# Render search results as a compact text table.
def format_table(rows: list[dict]) -> str:
    if not rows:
        return "Ничего не найдено."
    out = [f"Найдено: {len(rows)}"]
    for r in rows:
        dt = (r["recorded_at"] or "-")[:19].replace("T", " ")
        dur = f"{r['duration_s']:6.1f}s" if r["duration_s"] is not None else "     -"
        dir_ch = {"in": "←", "out": "→", "incoming": "←", "outgoing": "→"}.get(r["direction"], "·")
        who = r["contact"] or (r["phones"][0] if r["phones"] else "") or ", ".join(r["speakers"][:2]) or "-"
        spam = f"spam={r['spam_score']:.2f}" if r["spam_score"] is not None else ""
        extra = f" | {spam}" if spam else ""
        out.append(f"{dt:19s} {dir_ch} {dur} | {r['words']:4d}w {r['lines']:3d}l | {who[:26]:26s}{extra} | {r['md']}")
    return "\n".join(out)


# Read search options and print the matching records.
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=ROOT, help="Audio archive root (default: current directory)")
    ap.add_argument("--folder", help="Subfolder inside the transcript tree (e.g. Phone/2025)")
    ap.add_argument("--min-sec", type=float)
    ap.add_argument("--max-sec", type=float)
    ap.add_argument("--min-words", type=int)
    ap.add_argument("--max-words", type=int)
    ap.add_argument("--min-lines", type=int)
    ap.add_argument("--max-lines", type=int)
    ap.add_argument("--no-speech", action="store_true", help="Only [без речи] transcripts")
    ap.add_argument("--min-spam", type=float)
    ap.add_argument("--max-spam", type=float)
    ap.add_argument("--unscored-spam", action="store_true", help="Only spam_score: null")
    ap.add_argument("--only-bot", action="store_true", help="Only calls where non-owner speakers are bots/temp")
    ap.add_argument("--person", help="Catalog person ID")
    ap.add_argument("--org", help="Catalog org ID")
    ap.add_argument("--identification", action="append", help="Filter identification status (repeatable)")
    ap.add_argument("--direction", choices=["in", "out", "incoming", "outgoing"])
    ap.add_argument("--phone", help="Digits substring in phones")
    ap.add_argument("--contact", help="Substring in contact")
    ap.add_argument("--from", dest="date_from", help="Min recorded_at (e.g. 2025-01-01)")
    ap.add_argument("--to", dest="date_to", help="Max recorded_at (e.g. 2025-12-31)")
    ap.add_argument("--text", help="Regex substring in transcript speech or summary")
    ap.add_argument("--sort", choices=["date", "duration", "words", "lines", "spam"], default="date")
    ap.add_argument("--asc", action="store_true", help="Sort ascending instead of descending")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--format", choices=["table", "paths", "audio-paths", "jsonl"], default="table")
    args = ap.parse_args()

    rows = search_transcripts(
        args.root,
        folder=args.folder,
        min_sec=args.min_sec,
        max_sec=args.max_sec,
        min_words=args.min_words,
        max_words=args.max_words,
        min_lines=args.min_lines,
        max_lines=args.max_lines,
        no_speech=args.no_speech,
        min_spam=args.min_spam,
        max_spam=args.max_spam,
        unscored_spam=args.unscored_spam,
        only_bot=args.only_bot,
        person=args.person,
        org=args.org,
        identification=args.identification,
        direction=args.direction,
        phone=args.phone,
        contact=args.contact,
        date_from=args.date_from,
        date_to=args.date_to,
        text_query=args.text,
        sort_by=args.sort,
        asc=args.asc,
        limit=args.limit,
    )
    if args.format == "table":
        print(format_table(rows))
    elif args.format == "paths":
        for r in rows:
            print(r["md"])
    elif args.format == "audio-paths":
        for r in rows:
            print(r["audio"])
    elif args.format == "jsonl":
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
