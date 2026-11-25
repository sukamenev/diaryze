#!/usr/bin/env python3
"""Compare recording dates and optionally correct filesystem mtime, without editing audio."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess

if __package__:
    from .diarizer.archive import is_audio_candidate, is_excluded_source_dir, parse_filename, resolve_source_dir
    from .diarizer.config import EXCLUDED_FILES, ROOT, TZ
else:
    from diarizer.archive import is_audio_candidate, is_excluded_source_dir, parse_filename, resolve_source_dir
    from diarizer.config import EXCLUDED_FILES, ROOT, TZ


# Capture the file identity and metadata used to detect concurrent changes.
def fingerprint(path):
    st = path.stat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


# Hash the source so mtime corrections can be verified byte for byte.
def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


# Format a nanosecond timestamp in the archive's time zone.
def iso(ns):
    return datetime.fromtimestamp(ns / 10**9, TZ).isoformat()


# Compare mtime, filename date, and embedded dates to assess a possible correction.
def audit_one(path, threshold):
    rel = path.relative_to(ROOT).as_posix()
    result = {'schema_version': '1.0', 'audio': rel}
    try:
        before = fingerprint(path)
        result.update(fingerprint=before, mtime=iso(before[3]))
        filename, *_ = parse_filename(path.name)
        result['filename_time'] = filename
        if not filename or 'T' not in filename:
            return dict(result, status='no_precise_filename_date')
        dt = datetime.fromisoformat(filename)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=TZ)
        delta = before[3] / 10**9 - dt.timestamp()
        result['mtime_minus_filename_seconds'] = round(delta, 3)
        needs_change = abs(delta) > threshold
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                'format_tags:stream_tags', '-of', 'json', str(path)],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        candidates = []
        if probe.returncode == 0:
            data = json.loads(probe.stdout)
            sources = [('format', data.get('format', {}).get('tags', {}))]
            sources += [(f'stream[{i}]', s.get('tags', {})) for i, s in enumerate(data.get('streams', []))]
            for source, tags in sources:
                for key, value in tags.items():
                    if key.lower() not in {'creation_time', 'date', 'date_recorded', 'icrd'}:
                        continue
                    try:
                        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                        known_zone = parsed.tzinfo is not None and 'T' in str(value)
                        candidates.append({'source': source + '.' + key, 'raw': value,
                                           'timestamp': parsed.timestamp() if known_zone else None})
                    except (ValueError, TypeError, OverflowError):
                        pass
        else:
            result['probe_error'] = probe.stderr.strip()
        result['embedded_dates'] = candidates
        reliable = [c for c in candidates if c['timestamp'] is not None]
        conflicting = [c for c in reliable if abs(c['timestamp'] - dt.timestamp()) > threshold]
        if conflicting:
            result['status'] = 'date_conflict'
        elif not needs_change:
            result['status'] = 'mtime_ok'
        elif reliable:
            # Container creation often describes the end of the call; preserve that date.
            chosen = reliable[0]
            result.update(status='ready', proposed_mtime_ns=round(chosen['timestamp'] * 10**9),
                          proposed_mtime=iso(round(chosen['timestamp'] * 10**9)), basis=chosen['source'],
                          sha256=digest(path))
        else:
            result['status'] = 'filename_only_needs_review'
        if fingerprint(path) != before:
            return dict(result, status='source_changed')
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return dict(result, status='error', error=str(exc))


# Recheck corroborated files and journal their mtime changes.
def apply_rows(rows, journal_path):
    counts = Counter()
    with journal_path.open('x', encoding='utf-8') as journal:
        # Flush each journal entry to disk before proceeding.
        def write(row):
            journal.write(json.dumps(row, ensure_ascii=False) + '\n')
            journal.flush()
            os.fsync(journal.fileno())
        for row in rows:
            if row['status'] != 'ready':
                continue
            path = Path(ROOT) / row['audio']
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(Path(ROOT)):
                    raise ValueError('Unsafe path')
                if fingerprint(path) != row['fingerprint'] or digest(path) != row['sha256']:
                    raise ValueError('Source changed since audit')
                st = path.stat()
                if fingerprint(path) != row['fingerprint']:
                    raise ValueError('Source changed while checking')
                entry = {'schema_version': '1.0', 'audio': row['audio'], 'sha256': row['sha256'],
                         'old_mtime_ns': st.st_mtime_ns, 'new_mtime_ns': row['proposed_mtime_ns'],
                         'basis': row['basis'], 'atime_ns': st.st_atime_ns}
                write(dict(entry, phase='before'))
                os.utime(path, ns=(st.st_atime_ns, row['proposed_mtime_ns']), follow_symlinks=False)
                if path.stat().st_mtime_ns != row['proposed_mtime_ns'] or digest(path) != row['sha256']:
                    raise ValueError('Verification after utime failed')
                # Reading to verify can change atime; restore it too.
                os.utime(path, ns=(st.st_atime_ns, row['proposed_mtime_ns']), follow_symlinks=False)
                write(dict(entry, phase='done'))
                counts['updated'] += 1
            except (OSError, ValueError) as exc:
                write({'schema_version': '1.0', 'audio': row['audio'], 'phase': 'failed', 'error': str(exc)})
                counts['failed'] += 1
    return dict(counts)


# Parse audit options and run the scan or apply a saved plan.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default='.', help='Archive folder to audit')
    parser.add_argument('--days', type=float, default=2, help='Allowed date difference (default: 2 days)')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--output', help='New JSONL audit report; default: print summary only')
    parser.add_argument('--plan', help='Apply an existing audit report instead of probing again')
    parser.add_argument('--apply', action='store_true', help='Correct mtime only for corroborated dates')
    parser.add_argument('--journal', help='New change-journal path, required with --apply')
    args = parser.parse_args()
    if args.days <= 0 or args.workers < 1:
        parser.error('--days and --workers must be positive')
    if args.apply and not args.journal:
        parser.error('--apply requires --journal')
    if args.plan:
        rows = [json.loads(s) for s in Path(args.plan).read_text().splitlines() if s.strip()]
        if any(r.get('schema_version') != '1.0' for r in rows):
            parser.error('Unsupported plan schema')
    else:
        paths = []
        for base, dirs, files in os.walk(resolve_source_dir(args.folder)):
            folder = Path(base)
            dirs[:] = sorted(d for d in dirs if not is_excluded_source_dir(folder / d))
            paths += [folder / name for name in sorted(files) if name.lower() not in EXCLUDED_FILES
                      and not (folder / name).is_symlink() and is_audio_candidate(str(folder / name))]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(lambda p: audit_one(p, args.days * 86400), paths))
    if args.output:
        with Path(args.output).open('x', encoding='utf-8') as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    result = {'files': len(rows), 'statuses': dict(Counter(r['status'] for r in rows))}
    if args.apply:
        result['applied'] = apply_rows(rows, Path(args.journal))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return bool(result['statuses'].get('error') or result.get('applied', {}).get('failed'))


if __name__ == '__main__':
    raise SystemExit(main())
