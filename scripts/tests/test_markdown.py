"""Regression checks for script-owned metadata in Markdown transcripts."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import tempfile
import unittest

from scripts.diarizer.archive import md_has_matching_sha
from scripts.diarizer.identification import split_markdown
from scripts.diarizer.markdown import TranscriptFile, patch_scratch


BODY = b"\r\n[00:00:01.00] **Anna** Keep this.\r\n"
RAW = (
    b"---\r\nschema_version: '1.0' # retain this\r\nsha256: source123\r\n"
    b"scratch:\r\n  other_script: [true, 15]\r\n\r\n# retain this too\r\n"
    b"speakers: {}\r\n---\r\n" + BODY
)


class TranscriptFileTests(unittest.TestCase):
    # Verify that one script can edit and clear its entry without altering the rest of the MD.
    def test_scratch_preserves_other_metadata_body_and_file_attributes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "call.md"
            path.write_bytes(RAW)
            timestamp = 1700000000123456789
            os.utime(path, ns=(timestamp, timestamp))
            path.chmod(0o640)
            before = path.stat()
            md = TranscriptFile(path)
            value = {"suspected": True, "details": ["---", "red\ngreen", None, 2]}
            self.assertTrue(md.set_scratch("speaker_check", value))
            self.assertFalse(md.set_scratch("speaker_check", value))
            raw = path.read_bytes()
            self.assertEqual(md.get_scratch("speaker_check"), value)
            self.assertEqual(md.get_scratch("other_script"), [True, 15])
            self.assertTrue(raw.endswith(b"---\r\n" + BODY))
            self.assertIn(b"# retain this too\r\nspeakers: {}\r\n", raw)
            self.assertEqual(md.read_metadata()["sha256"], "source123")
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(before.st_mode))
            self.assertTrue(md.clear_scratch("speaker_check"))
            self.assertFalse(md.clear_scratch("speaker_check"))
            self.assertEqual(md.get_scratch("other_script"), [True, 15])
            self.assertEqual(md.get_scratch("speaker_check", "missing"), "missing")

    # Check that readers find the source digest even after a large scratch field.
    def test_large_scratch_and_indented_delimiter(self):
        raw = patch_scratch(b"---\nschema_version: '1.0'\n---\nBody\n", "tool", {"v": "abc" * 3000})
        raw = raw.replace(b"\n---\nBody", b"\nsha256: source123\n---\nBody")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "call.md"
            path.write_bytes(raw)
            self.assertTrue(md_has_matching_sha(path, "source123"))
            self.assertFalse(md_has_matching_sha(path, "other"))
        literal = b"---\nsummary: |\n  before\n  ---\n  after\nspeakers: {}\n---\nBody\n"
        changed = patch_scratch(literal, "tool", False)
        self.assertEqual(split_markdown(changed)[2]["summary"], "before\n---\nafter\n")
        self.assertTrue(changed.endswith(b"---\nBody\n"))

    # Support the optional field in an existing inline mapping without changing other fields.
    def test_inline_metadata_and_empty_scratch(self):
        raw = b"---\n{sha256: source123, speakers: {}, }\n---\nBody\n"
        changed = patch_scratch(raw, "tool", ["Ж", 2, False])
        self.assertEqual(split_markdown(changed)[2]["scratch"]["tool"], ["Ж", 2, False])
        self.assertIn(b"sha256: source123, speakers: {}", changed)
        cleaned = patch_scratch(changed, "tool")
        self.assertEqual(split_markdown(cleaned)[2]["scratch"], {})

    # Run independent writers against one file and verify that all keys survive.
    def test_concurrent_scripts_keep_each_others_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "call.md"
            path.write_bytes(RAW)
            md = TranscriptFile(path)
            with ThreadPoolExecutor(max_workers=6) as workers:
                changes = [workers.submit(md.set_scratch, f"script_{i}", i) for i in range(18)]
                self.assertTrue(all(change.result(timeout=5) for change in changes))
            self.assertEqual(md.read_metadata()["scratch"],
                             {"other_script": [True, 15], **{f"script_{i}": i for i in range(18)}})
