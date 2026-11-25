#!/usr/bin/env python3
"""Swap two transcript speaker labels while preserving the rest of the Markdown."""

import argparse
from pathlib import Path
import stat

import yaml

if __package__:
    from .diarizer.config import TS_LINE_RE
    from .diarizer.identification import split_markdown
    from .diarizer.markdown import replace_markdown
else:
    from diarizer.config import TS_LINE_RE
    from diarizer.identification import split_markdown
    from diarizer.markdown import replace_markdown


# Exchange two speaker keys throughout the transcript, keeping metadata coherent.
def swap_speakers(raw, first, second):
    if first == second or {first, second} & {"", "?", "unknown"}:
        raise ValueError("Choose two distinct speaker labels")
    lines, end, meta = split_markdown(raw)
    speakers = meta.get("speakers")
    replacements = {first: second, second: first}
    for speaker in replacements:
        if not isinstance(speakers, dict) or speaker not in speakers:
            raise ValueError(f"Speaker {speaker!r} is missing from the YAML speakers mapping")

    counts = dict.fromkeys(replacements, 0)
    for index in range(end + 1, len(lines)):
        line = lines[index].decode("utf-8")
        trimmed = line.lstrip(" \t")
        match = TS_LINE_RE.match(trimmed.rstrip("\r\n"))
        if not match:
            continue
        start, stop = match.span("sp_b" if match["sp_b"] is not None else "sp_p")
        label = trimmed[start:stop]
        speaker = label.strip()
        if speaker not in replacements:
            continue
        indent = len(line) - len(trimmed)
        start += indent + len(label) - len(label.lstrip())
        stop = start + len(speaker)
        lines[index] = (line[:start] + replacements[speaker] + line[stop:]).encode("utf-8")
        counts[speaker] += 1

    for speaker, count in counts.items():
        if count == 0:
            raise ValueError(f"Speaker {speaker!r} has no timestamped turns")
    return b"".join(lines), counts


# Preview or apply a speaker swap for a selected Markdown file.
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown", type=Path, help="path to one transcript")
    parser.add_argument("first", help="first label from the speakers mapping")
    parser.add_argument("second", help="second label from the speakers mapping")
    parser.add_argument("--apply", action="store_true", help="write the swap to the file")
    args = parser.parse_args(argv)
    path = args.markdown
    try:
        before = path.lstat()
        if path.suffix.lower() != ".md" or not stat.S_ISREG(before.st_mode):
            raise ValueError("Choose a regular .md file")
        raw = path.read_bytes()
        changed, counts = swap_speakers(raw, args.first, args.second)
        if args.apply:
            replace_markdown(path, before, raw, changed)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.exit(1, f"error: {exc}\n")

    print(f"{args.first} → {args.second}: {counts[args.first]} turns")
    print(f"{args.second} → {args.first}: {counts[args.second]} turns")
    print(f"Saved {path}" if args.apply else "Preview only. Use --apply to save.")


if __name__ == "__main__":
    main()
