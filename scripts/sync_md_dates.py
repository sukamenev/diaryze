#!/usr/bin/env python3
"""Synchronize Text/ MD filesystem mtime to recorded_at (or filename date) for chronological sorting."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import os

if __package__:
    from .diarizer.archive import recorded_mtime_ns
    from .diarizer.config import ROOT, TZ
    from .diarizer.identification import split_markdown
else:
    from diarizer.archive import recorded_mtime_ns
    from diarizer.config import ROOT, TZ
    from diarizer.identification import split_markdown


def sync_md_dates(root, *, folder=None, apply=False):
    root = Path(root).resolve()
    text_root = (root / "Text").resolve()
    target_dir = Path(folder).resolve() if folder else text_root
    if not target_dir.is_relative_to(text_root) or not target_dir.is_dir():
        raise ValueError("Choose a folder inside " + str(text_root))

    result = {
        "files": 0,
        "updated": 0,
        "already_ok": 0,
        "no_date": 0,
        "failed": [],
    }
    journal = None
    try:
        for path in sorted(target_dir.rglob("*.md")):
            if path.is_symlink() or not path.resolve().is_relative_to(text_root):
                continue
            try:
                raw = path.read_bytes()
                _, _, meta = split_markdown(raw)
                audio = path.relative_to(text_root).as_posix()[:-3]
                result["files"] += 1
                target_ns = recorded_mtime_ns(meta.get("recorded_at"), audio_rel=audio)
                if target_ns is None:
                    result["no_date"] += 1
                    continue
                st = path.stat(follow_symlinks=False)
                if abs(st.st_mtime_ns - target_ns) <= 1_000_000_000:
                    result["already_ok"] += 1
                    continue
                if not apply:
                    result["updated"] += 1
                    continue
                if journal is None:
                    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
                    log = root / "work/logs/migrations" / f"md-dates-{stamp}.jsonl"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    journal = log.open("x", encoding="utf-8")
                    result["journal"] = str(log.relative_to(root))
                old_mtime_ns = st.st_mtime_ns
                os.utime(path, ns=(st.st_atime_ns, target_ns), follow_symlinks=False)
                entry = {
                    "text": str(path.relative_to(root)),
                    "old_mtime": datetime.fromtimestamp(old_mtime_ns / 1e9, TZ).isoformat(timespec="seconds"),
                    "new_mtime": datetime.fromtimestamp(target_ns / 1e9, TZ).isoformat(timespec="seconds"),
                }
                journal.write(json.dumps(entry, ensure_ascii=False) + "\n")
                journal.flush()
                result["updated"] += 1
            except (OSError, ValueError) as exc:
                result["failed"].append({"text": str(path), "error": str(exc)})
    finally:
        if journal is not None:
            journal.close()
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folder", help="Optional subfolder under Text/")
    ap.add_argument("--apply", action="store_true", help="Update mtime on MD files")
    args = ap.parse_args()
    res = sync_md_dates(ROOT, folder=args.folder, apply=args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
