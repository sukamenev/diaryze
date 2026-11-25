#!/usr/bin/env python3
"""Consolidate schema 1.0 metadata without model calls or rewriting transcript bodies."""

import argparse
from collections import Counter
import json
from pathlib import Path

if __package__:
    from .migrate_identification import migrate
    from .diarizer.catalog_fields import patch_catalog_fields
    from .diarizer.config import ROOT
    from .diarizer.identification import split_markdown
else:
    from migrate_identification import migrate
    from diarizer.catalog_fields import patch_catalog_fields
    from diarizer.config import ROOT
    from diarizer.identification import split_markdown


# Run the catalog-field migration in preview or apply mode.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', help='Folder inside the transcript tree (default: all transcripts)')
    parser.add_argument('--summaries', help='JSON map keyed by audio SHA-256: source_summary and summary')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    summaries = json.loads(Path(args.summaries).read_text()) if args.summaries else {}
    counts = Counter()
    # Calculate the catalog-field update and count populated fields.
    def patch(raw, people, orgs):
        changed, status = patch_catalog_fields(raw, people, orgs, summaries=summaries)
        _, _, meta = split_markdown(changed)
        counts['summary_present' if meta.get('summary') else 'summary_null'] += 1
        counts['spam_null' if meta.get('spam_score') is None else 'spam_scored'] += 1
        return changed, status
    result = migrate(ROOT, folder=args.folder, apply=args.apply,
                     patcher=patch, journal_prefix='catalog-1.0')
    result['fields'] = dict(counts)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return bool(result['failed'])


if __name__ == '__main__':
    raise SystemExit(main())
