#!/usr/bin/env python3
"""CLI entry point; implementation lives in the diarizer package."""

if __package__:
    from .diarizer.cli import main
else:
    from diarizer.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
