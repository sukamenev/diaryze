#!/usr/bin/env python3
"""Read-only duplicate audit: size, SHA-256, then an actual byte comparison."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path

if __package__:
    from .diarizer.archive import is_audio_candidate, is_excluded_dir
    from .diarizer.config import EXCLUDED_FILES, ROOT
else:
    from diarizer.archive import is_audio_candidate, is_excluded_dir
    from diarizer.config import EXCLUDED_FILES, ROOT


# Capture file metadata to detect changes during the duplicate scan.
def signature(path):
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


# Compare candidate files byte for byte before declaring a duplicate.
def same_bytes(left, right):
    with left.open('rb') as a, right.open('rb') as b:
        while True:
            first, second = a.read(1024 * 1024), b.read(1024 * 1024)
            if first != second:
                return False
            if not first:
                return True


# Find identical audio files using size, hash, and byte comparisons.
def audit(root):
    root = Path(root).resolve()
    sizes = defaultdict(list)
    inventory = {}
    failures = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not is_excluded_dir(d)
                         and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if name.lower() in EXCLUDED_FILES or path.is_symlink() or not path.is_file():
                continue
            if not is_audio_candidate(str(path)):
                continue
            try:
                sig = signature(path)
                inventory[path] = sig
                sizes[sig[2]].append(path)
            except OSError as exc:
                failures.append({'path': str(path.relative_to(root)), 'error': str(exc)})
    groups = []
    hashed = 0
    for size, paths in sorted(sizes.items()):
        if len(paths) < 2:
            continue
        digests = defaultdict(list)
        for path in paths:
            try:
                with path.open('rb') as f:
                    digest = hashlib.file_digest(f, 'sha256').hexdigest()
                if signature(path) != inventory[path]:
                    raise ValueError('Source changed while hashing')
                digests[digest].append(path)
                hashed += 1
                if hashed % 2000 == 0:
                    print('Хешировано', hashed, 'файлов', flush=True)
            except (OSError, ValueError) as exc:
                failures.append({'path': str(path.relative_to(root)), 'error': str(exc)})
        for digest, matches in digests.items():
            if len(matches) < 2:
                continue
            try:
                if any(not same_bytes(matches[0], path) for path in matches[1:]):
                    raise ValueError('SHA-256 matches but byte comparison differs')
                if any(signature(path) != inventory[path] for path in matches):
                    raise ValueError('Source changed while comparing')
                groups.append({'schema_version': '1.0', 'sha256': digest, 'bytes': size,
                               'files': sorted(str(p.relative_to(root)) for p in matches)})
            except (OSError, ValueError) as exc:
                failures.append({'paths': [str(p.relative_to(root)) for p in matches], 'error': str(exc)})
    groups.sort(key=lambda g: (g['bytes'], g['sha256']), reverse=True)
    # Catch changes occurring after a successful comparison, before publication.
    stable = []
    for group in groups:
        if all(signature(root / p) == inventory[root / p] for p in group['files']):
            stable.append(group)
        else:
            failures.append({'paths': group['files'], 'error': 'Source changed before report'})
    return stable, {'schema_version': '1.0', 'candidates': len(inventory), 'hashed': hashed, 'failures': failures}


# Run a read-only duplicate audit and write its reports.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='New report directory (default: work/reports/duplicates/<timestamp>)')
    args = parser.parse_args()
    groups, summary = audit(ROOT)
    stamp = datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')
    out = Path(args.output) if args.output else Path(ROOT) / "work" / "reports" / 'duplicates' / stamp
    out.mkdir(parents=True, exist_ok=False)
    (out / 'groups.jsonl').write_text(''.join(json.dumps(g, ensure_ascii=False) + '\n' for g in groups))
    summary.update(groups=len(groups), extra_copies=sum(len(g['files']) - 1 for g in groups),
                   extra_bytes=sum(g['bytes'] * (len(g['files']) - 1) for g in groups),
                   empty_files=sum(len(g['files']) for g in groups if not g['bytes']))
    combinations = Counter()
    for group in groups:
        if not group['bytes']:
            continue
        folders = tuple(sorted(set(str(Path(p).parent) for p in group['files'])))
        # WhatsApp weekly subfolders belong to one archive branch for priority selection.
        def branch(folder):
            parts = Path(folder).parts
            if parts[0] == 'Smartphone':
                return '/'.join(parts[:3])
            if parts[0].startswith('['):
                return parts[0]
            return folder
        branches = tuple(sorted(set(branch(f) for f in folders)))
        combinations[branches] += 1
    lines = ['# Побайтовые копии аудио', '',
             'Проверка всех аудиокандидатов архива независимо от имени: размер → SHA-256 → сравнение каждого байта. '
             'Аудио и транскрипции не изменялись. Приоритет папок не назначен.', '',
             f"Кандидатов: {summary['candidates']}; групп копий: {len(groups)}; "
             f"лишних копий при сохранении одной на группу: {summary['extra_copies']}; "
             f"объём: {summary['extra_bytes'] / 1024**2:.1f} МиБ.", '',
             'Папки ниже — ветви архива; точные пути каждого файла есть в groups.jsonl. '
             'Число групп — не число файлов для удаления. Внутри одной папки тоже возможны копии с разными именами.', '',
             '| Папки с одинаковым содержимым | Групп |', '|---|---:|']
    for branches, count in combinations.most_common():
        lines.append('| ' + ' ↔ '.join('`' + b.replace('|', '\\|') + '`' for b in branches) + f' | {count} |')
    lines += ['', f"Пустые файлы: {summary['empty_files']} (отдельная группа, если их несколько); "
              'они не участвуют в таблице приоритетов содержимого.', '',
              '[Полный список групп и точных путей](groups.jsonl), [сводка и ошибки чтения](summary.json).', '']
    (out / 'README.md').write_text('\n'.join(lines))
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(out / 'README.md', flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return bool(summary['failures'])


if __name__ == '__main__':
    raise SystemExit(main())
