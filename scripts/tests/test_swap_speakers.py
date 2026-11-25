import contextlib
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest

from scripts.swap_speakers import main, swap_speakers


FRONT = """---
summary: Анна и Иван обсуждают покупку.
speakers:
  Анна:
    person: anna
    source: filename
  Иван:
    person: ivan
    source: filename
  Гость:
    person: null
---

"""
BODY = """[00:00:00.00] **Анна** Иван, привет.
  [00:00:01.00] Иван : Здравствуй, Анна.
[00:00:02.00] **Анна:** А это кто?
[00:00:03.00] **Гость** Это я.
[00:00:04.00] **?** Да.
Заметка об Анне и Иване."""
SWAPPED = """[00:00:00.00] **Иван** Иван, привет.
  [00:00:01.00] Анна : Здравствуй, Анна.
[00:00:02.00] **Иван:** А это кто?
[00:00:03.00] **Гость** Это я.
[00:00:04.00] **?** Да.
Заметка об Анне и Иване."""


class SwapSpeakersTests(unittest.TestCase):
    # Check that swapping twice restores the original text and changes only speaker labels.
    def test_swap_is_reversible_and_changes_only_labels(self):
        raw = (FRONT + BODY).replace("\n", "\r\n").encode()
        changed, counts = swap_speakers(raw, "Анна", "Иван")
        self.assertEqual(changed, (FRONT + SWAPPED).replace("\n", "\r\n").encode())
        self.assertEqual(counts, {"Анна": 2, "Иван": 1})
        self.assertEqual(swap_speakers(changed, "Анна", "Иван")[0], raw)

    # Check that applying the CLI's previewed swap preserves mtime and file mode.
    def test_cli_previews_and_apply_preserves_mtime_and_mode(self):
        raw = (FRONT + BODY).encode()
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as output:
            path = Path(folder) / "recording.m4a.md"
            path.write_bytes(raw)
            stamp = 1700000000123456789
            os.utime(path, ns=(stamp, stamp))
            path.chmod(0o640)
            args = [str(path), "Анна", "Иван"]
            main(args)
            self.assertEqual(path.read_bytes(), raw)
            self.assertIn("Анна → Иван: 2 turns", output.getvalue())
            main([*args, "--apply"])
            self.assertEqual(path.read_bytes(), (FRONT + SWAPPED).encode())
            self.assertEqual(path.stat().st_mtime_ns, stamp)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    # Check that a swap requires both labels to exist and have attributed turns.
    def test_rejects_missing_label_or_label_without_turns(self):
        raw = (FRONT + BODY.replace("**Гость**", "**?**")).encode()
        with self.assertRaisesRegex(ValueError, "missing from the YAML"):
            swap_speakers(raw, "Анна", "Олег")
        with self.assertRaisesRegex(ValueError, "no timestamped turns"):
            swap_speakers(raw, "Анна", "Гость")
