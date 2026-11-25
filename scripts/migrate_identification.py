#!/usr/bin/env python3
"""Annotate existing MDs without re-transcribing or rewriting their contents."""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

import yaml

if __package__:
    from .diarizer.archive import append_manifest, load_manifest_state, load_refs, sync_file_mtime
    from .diarizer.config import ROOT, data_path, transcripts_dir
    from .diarizer.identification import patch_markdown, split_markdown
else:
    from diarizer.archive import append_manifest, load_manifest_state, load_refs, sync_file_mtime
    from diarizer.config import ROOT, data_path, transcripts_dir
    from diarizer.identification import patch_markdown, split_markdown


# Apply a Markdown transformation across eligible archive transcripts.
def migrate(root, *, folder=None, apply=False, patcher=None, journal_prefix="identification",
            patcher_with_path=False):
    root = Path(root).resolve()
    text_root = transcripts_dir(root).resolve()
    folder = Path(folder).resolve() if folder else text_root
    if not folder.is_relative_to(text_root.resolve()) or not folder.is_dir():
        raise ValueError("Choose a folder inside " + str(text_root))
    refs = load_refs(root=root)
    state = load_manifest_state(root=root)
    counts = Counter()
    result = {"files": 0, "updated": 0, "unchanged": 0, "failed": [], "manifest_updated": 0}
    journal = None

    # Append updated metadata for changed transcripts whose audio hash still matches.
    def update_manifest(audio, meta, status):
        previous = state.get(audio)
        if not previous or previous.get("status") != "done" or previous.get("sha256") != meta.get("sha256"):
            return
        if previous.get("schema_version") == meta['schema_version'] and previous.get("identification") == status:
            return
        entry = dict(previous, schema_version=meta['schema_version'], identification=status,
                     metadata_updated_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        append_manifest(entry, root=root)
        state[audio] = entry
        result["manifest_updated"] += 1

    try:
        for path in sorted(folder.rglob("*.md")):
            if path.is_symlink() or not path.resolve().is_relative_to(text_root.resolve()):
                continue
            try:
                before = path.stat()
                raw = path.read_bytes()
                _, _, meta = split_markdown(raw)
                audio = path.relative_to(text_root).as_posix()[:-3]
                if meta.get("audio") and meta["audio"] != audio:
                    raise ValueError("audio path disagrees with MD path")
                context = {"audio_rel": audio} if patcher_with_path else {}
                changed, status = (patcher or patch_markdown)(raw, *refs, **context)
                _, _, changed_meta = split_markdown(changed)
                result["files"] += 1
                counts[status] += 1
                if changed == raw:
                    result["unchanged"] += 1
                    if apply:
                        update_manifest(audio, changed_meta, status)
                    continue
                if not apply:
                    result["updated"] += 1
                    continue
                if journal is None:
                    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
                    log = data_path("logs", "migrations", journal_prefix + "-" + stamp + ".jsonl", root=root)
                    log.parent.mkdir(parents=True, exist_ok=True)
                    journal = log.open("x", encoding="utf-8")
                    result["journal"] = str(log.relative_to(root))
                temp = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".identification-",
                                                     suffix=".tmp", delete=False) as f:
                        temp = Path(f.name)
                        f.write(changed)
                        f.flush()
                        os.fsync(f.fileno())
                        os.fchmod(f.fileno(), stat.S_IMODE(before.st_mode))
                    if path.read_bytes() != raw or path.stat().st_mtime_ns != before.st_mtime_ns:
                        raise ValueError("MD changed during migration; left untouched")
                    os.replace(temp, path)
                    sync_file_mtime(path, changed_meta.get("recorded_at"), audio_rel=audio, fallback_ns=before.st_mtime_ns)
                finally:
                    if temp is not None:
                        temp.unlink(missing_ok=True)
                if path.read_bytes() != changed:
                    raise ValueError("MD changed immediately after migration")
                entry = {"text": str(path.relative_to(root)), "identification": status,
                         "old_schema_version": meta["schema_version"],
                         "sha256_before": hashlib.sha256(raw).hexdigest(),
                         "sha256_after": hashlib.sha256(changed).hexdigest()}
                journal.write(json.dumps(entry, ensure_ascii=False) + "\n")
                journal.flush()
                update_manifest(audio, changed_meta, status)
                result["updated"] += 1
                if result["updated"] % 200 == 0:
                    print("Обновлено", result["updated"], "MD", flush=True)
            except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
                result["failed"].append({"text": str(path), "error": str(exc)})
    finally:
        if journal is not None:
            journal.close()
    result["statuses"] = dict(counts)
    return result


# Run the identification migration in preview or apply mode.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", help="Folder inside the transcript tree (default: all transcripts)")
    parser.add_argument("--apply", action="store_true", help="Write changes; otherwise only report")
    args = parser.parse_args()
    result = migrate(ROOT, folder=args.folder, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return bool(result["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
