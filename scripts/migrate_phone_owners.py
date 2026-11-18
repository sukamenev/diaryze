#!/usr/bin/env python3
"""Include silent device owners in existing robot-only phone transcripts."""

import argparse
from functools import partial
import json
import re

import yaml

if __package__:
    from .diarizer.config import ROOT, TS_LINE_RE
    from .diarizer.identification import patch_markdown, split_markdown, status_from_markdown
    from .diarizer.owners import with_silent_phone_owner
    from .migrate_identification import migrate
else:
    from diarizer.config import ROOT, TS_LINE_RE
    from diarizer.identification import patch_markdown, split_markdown, status_from_markdown
    from diarizer.owners import with_silent_phone_owner
    from migrate_identification import migrate


def patch_phone_owners(raw, people, orgs, *, root=ROOT, audio_rel=None):
    """Insert just the owner mapping and update status, preserving existing MD text."""
    lines, end, meta = split_markdown(raw)
    body = b"".join(lines[end + 1:]).decode("utf-8")
    used = {(m.group("sp_b") or m.group("sp_p")).strip()
            for line in body.splitlines() if (m := TS_LINE_RE.match(line.strip()))}
    speakers = meta.get("speakers")
    updated = with_silent_phone_owner(
        speakers, meta.get("audio") or audio_rel or "", people,
        used_speakers=used, direction=meta.get("direction"), root=root,
    )
    if updated is speakers:
        return raw, status_from_markdown(raw, people, orgs)
    start = next((i for i in range(1, end)
                  if re.fullmatch(rb"speakers:[ \t]*(?:#.*)?", lines[i].rstrip(b"\r\n"))), None)
    if start is None:
        raise ValueError("Cannot safely extend speakers mapping")
    stop = next((i for i in range(start + 1, end)
                 if re.match(rb"[^\s#][^:]*:", lines[i])), end)
    newline = b"\r\n" if lines[start].endswith(b"\r\n") else b"\n"
    added = {key: info for key, info in updated.items() if key not in speakers}
    block = yaml.safe_dump(added, allow_unicode=True, sort_keys=False, width=1000)
    inserted = b"".join(b"  " + line.encode("utf-8") + newline for line in block.splitlines())
    changed, status = patch_markdown(b"".join(lines[:stop]) + inserted + b"".join(lines[stop:]), people, orgs)
    _, _, checked = split_markdown(changed)
    expected = dict(meta, speakers=updated, identification=status)
    if checked != expected:
        raise ValueError("Owner insertion unexpectedly changed other YAML fields")
    return changed, status


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folder", help="Optional folder inside Text/")
    ap.add_argument("--apply", action="store_true", help="Write changes and update manifest")
    args = ap.parse_args()
    result = migrate(ROOT, folder=args.folder, apply=args.apply,
                     patcher=partial(patch_phone_owners, root=ROOT), journal_prefix="phone-owners",
                     patcher_with_path=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return bool(result["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
