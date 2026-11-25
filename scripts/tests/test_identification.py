"""Evidence-based identity status and non-destructive, idempotent backfill."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from scripts.diarizer.identification import identification_status, patch_markdown
from scripts.diarizer.transcript import parse_speakers_and_lines, validate_and_render_md
from scripts.migrate_identification import migrate
from scripts.relink_catalog_speakers import relink_markdown_speakers
from scripts.sync_md_dates import sync_md_dates


PEOPLE = {"known_person": {"name": "Известный человек"}}
ORGS = {"known_org": {"name": "Организация"}}
CONFIRMED = {"person": "known_person", "source": "speech", "note": "Представился."}


class IdentificationTests(unittest.TestCase):
    # Evaluate speaker identification with the shared person and organization fixtures.
    def assess(self, speakers, **kwargs):
        return identification_status(speakers, PEOPLE, ORGS, **kwargs)

    # Check that status reflects which declared speakers have catalog links.
    def test_partial_and_complete_statuses_reflect_linked_speakers(self):
        speakers = {"Сергей": CONFIRMED, "Иван": {"person": None}}
        self.assertEqual(self.assess(speakers), "partial")
        speakers["Иван"]["note"] = "Имя слышно, фамилия неизвестна, карточки нет."
        self.assertEqual(self.assess(speakers), "partial")
        speakers["Иван"] = {"person": "known_person", "source": "user", "note": "Подтверждено."}
        self.assertEqual(self.assess(speakers), "complete")

    # Check that filename evidence can identify a known person and invalid links are excluded.
    def test_filename_confirms_known_person_while_missing_source_or_unknown_do_not(self):
        guessed = {"person": "known_person", "source": "filename"}
        self.assertEqual(self.assess({"Сергей": guessed}), "complete")
        guessed["note"] = "По контакту или владельцу папки."
        self.assertEqual(self.assess({"Сергей": guessed}), "complete")
        self.assertEqual(self.assess({"Сергей": {"person": "known_person"}}), "unresolved")
        self.assertEqual(self.assess({"Сергей": {"person": "missing", "source": "speech"}}), "unresolved")
        self.assertEqual(self.assess({"Сергей": {"person": "missing", "source": "filename"}}), "unresolved")
        self.assertEqual(self.assess({"Гость": {"person": None, "source": "speech", "note": "Неизвестный."}}), "unresolved")

    # Check that unresolved speaker keys affect status while inline question marks are ignored.
    def test_unmapped_and_ambiguous_voices_block_complete_status_except_question_mark(self):
        self.assertEqual(self.assess({"Сергей": CONFIRMED}, used_speakers={"Сергей", "Иван"}), "partial")
        self.assertEqual(self.assess({"Сергей": CONFIRMED}, used_speakers={"Сергей", "?"}), "complete")
        self.assertEqual(self.assess({"unknown": CONFIRMED}), "unresolved")
        self.assertEqual(self.assess({}, no_speech=True), "not_applicable")
        self.assertEqual(self.assess({}, no_speech=True, used_speakers={"Сергей"}), "unresolved")

    # Check that an organization affiliation alone cannot identify a human.
    def test_organization_does_not_identify_an_unnamed_human(self):
        info = {"org": "known_org", "source": "speech", "role": "operator", "temp": True,
                "note": "Оператор назвал организацию, но не представился."}
        self.assertEqual(self.assess({"Оператор": info}), "unresolved")
        info["role"] = "robot"
        self.assertEqual(self.assess({"Оператор": info}), "complete")

    # Check that new Markdown includes the computed status and expected schema fields.
    def test_new_transcript_has_computed_status_and_schema_2_fields(self):
        item = {"audio_rel": "Recordings/test.wav", "sha256": "a" * 64, "duration_ms": 5000}
        for speakers, expected in (({"Голос 1": {"person": None}}, "unresolved"),
                                   ({"Оператор": {"person": "unknown", "temp": True, "note": "Не представился."}}, "unresolved"),
                                   ({"Сергей": CONFIRMED}, "complete")):
            md = validate_and_render_md(item, speakers, [(0, next(iter(speakers)), "Привет"), (1000, "?", "[шум]")], False, "model", PEOPLE, ORGS)
            meta = yaml.safe_load(md.split("---", 2)[1])
            self.assertEqual(meta["identification"], expected)
            self.assertEqual(meta["schema_version"], "1.0")
            self.assertNotIn("?", meta["speakers"])
            self.assertNotIn("## Речь", md)
            for sp_info in meta["speakers"].values():
                self.assertNotEqual(sp_info.get("person"), "unknown")
            self.assertIn("[00:00:01.00] **?** [шум]", md)
            self.assertIn(f"[00:00:00.00] **{next(iter(speakers))}** Привет", md)
        with self.assertRaises(ValueError):
            validate_and_render_md(item, {"?": {"person": None, "note": "x"}}, [(0, "?", "x")], False, "m", PEOPLE, ORGS)
        with self.assertRaises(ValueError):
            validate_and_render_md(item, {"Оператор": {"person": None, "temp": "yes", "note": "x"}}, [(0, "Оператор", "x")], False, "m", PEOPLE, ORGS)
        md = validate_and_render_md(item, None, [], True, "model", PEOPLE, ORGS)
        meta = yaml.safe_load(md.split("---", 2)[1])
        self.assertEqual(meta["identification"], "not_applicable")
        self.assertEqual(meta["speakers"], {})


class MigrationTests(unittest.TestCase):
    # Build legacy Markdown with comments and transcript text for migration tests.
    def sample(self, newline=b"\n"):
        raw = '''---
schema_version: '1.0' # keep this comment
sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
created_at: "2026-11-15T18:00:00+03:00"
duration_ms: 5000
speakers:
  S1: {person: known_person, basis: audio}
  S2: {label: "Неизвестный"}
# preserve manual formatting and comments
---

## Речь

[00:00:00.00] **S1:** Текст с ручной правкой.
[00:00:01.00] **S2:** Ответ.

## Заметки

Моя заметка.\x20\x20
'''.encode()
        return raw.replace(b"\n", newline)

    # Check that migration changes only the schema and identification fields.
    def test_patch_preserves_every_byte_except_the_two_root_fields(self):
        for newline in (b"\n", b"\r\n"):
            raw = self.sample(newline)
            changed, status = patch_markdown(raw, PEOPLE, ORGS)
            expected = raw.replace(b"schema_version: '1.0' # keep this comment" + newline,
                b"schema_version: '1.0' # keep this comment" + newline + b'identification: partial' + newline)
            self.assertEqual(changed, expected)
            self.assertEqual(status, "partial")
            self.assertEqual(patch_markdown(changed, PEOPLE, ORGS), (changed, status))

    # Check that migration rejects future schema versions.
    def test_unknown_future_schema_is_not_downgraded(self):
        with self.assertRaises(ValueError):
            patch_markdown(self.sample().replace(b"'1.0'", b"'3.0'"), PEOPLE, ORGS)

    # Check that the migration updates the index and a second application makes no changes.
    def test_apply_updates_index_and_second_run_is_a_noop(self):
        with tempfile.TemporaryDirectory(prefix="identity-migration-test-") as tmp:
            root = Path(tmp)
            path = root / "00_Diaryze_Data/Text/Recordings/test.wav.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(self.sample())
            data = root / "00_Diaryze_Data"
            (data / "people.yaml").write_text(yaml.safe_dump({"people": PEOPLE}))
            record = {"audio": "Recordings/test.wav", "text": "00_Diaryze_Data/Text/Recordings/test.wav.md",
                      "status": "done", "schema_version": "1.0", "sha256": "a" * 64,
                      "created_at": "original date", "model": "original model"}
            manifest = data / "manifest.jsonl"
            manifest.write_text(json.dumps(record) + "\n")
            dry = migrate(root)
            self.assertEqual(dry["updated"], 1)
            self.assertEqual(path.read_bytes(), self.sample())
            with contextlib.redirect_stdout(io.StringIO()):
                result = migrate(root, apply=True)
            self.assertFalse(result["failed"])
            self.assertEqual((result["updated"], result["manifest_updated"]), (1, 1))
            records = [json.loads(line) for line in manifest.read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[-1]["identification"], "partial")
            self.assertEqual(records[-1]["created_at"], "original date")
            before = path.read_bytes(), manifest.read_bytes()
            result = migrate(root, apply=True)
            self.assertEqual((result["updated"], result["unchanged"], result["manifest_updated"]), (0, 1, 0))
            self.assertEqual(before, (path.read_bytes(), manifest.read_bytes()))

    # Check that catalog relinking uses contact evidence and preserves speech-based links.
    def test_relink_catalog_speakers_links_matching_contact_without_overwriting_speech(self):
        people = {
            "ivanov_sergey": {"name": "Иванов Сергей"},
            "kuznetsov_andrey": {"name": "Андрей Кузнецов", "phones": ["+70000000103"]},
        }
        raw = '''---
schema_version: '1.0'
identification: not_checked
audio: Phone/2026/kuz.m4a
sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
duration_s: 10.00
contact: Андрей Кузнецов
phones:
- '+70000000103'
speakers:
  Сергей:
    person: ivanov_sergey
    source: speech
  Андрей Кузнецов:
    person: null
    label: Андрей Кузнецов
---

## Речь

[00:00:01.00] **Сергей** Привет, Андрей.
[00:00:02.00] **Андрей Кузнецов** Привет.
'''.encode("utf-8")
        fixed, status = relink_markdown_speakers(raw, people, {})
        meta = yaml.safe_load(fixed.decode("utf-8").split("---", 2)[1])
        self.assertEqual(meta["speakers"]["Андрей Кузнецов"]["person"], "kuznetsov_andrey")
        self.assertEqual(meta["speakers"]["Андрей Кузнецов"]["source"], "filename")
        self.assertEqual(status, "complete")
        self.assertEqual(relink_markdown_speakers(fixed, people, {})[0], fixed)

    # Check that a known nickname can match an unlinked speaker.
    def test_relink_catalog_speakers_matches_nicknames(self):
        people = {
            "ivanov_sergey": {"name": "Иванов Сергей"},
            "kuznetsov_andrey": {"name": "Андрей Кузнецов", "phones": ["+70000000103"], "nicknames": ["Кузнец"]},
        }
        raw = '''---
schema_version: '1.0'
identification: partial
audio: Phone/2026/kuz2.m4a
sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
duration_s: 10.00
contact: Кузнец
phones:
- '+70000000103'
speakers:
  Сергей:
    person: ivanov_sergey
    source: speech
  Кузнец:
    person: null
    note: Обращаются по прозвищу.
---

[00:00:01.00] **Сергей** Привет, Кузнец.
[00:00:02.00] **Кузнец** Привет.
'''.encode("utf-8")
        fixed, status = relink_markdown_speakers(raw, people, {})
        meta = yaml.safe_load(fixed.decode("utf-8").split("---", 2)[1])
        self.assertEqual(meta["speakers"]["Кузнец"]["person"], "kuznetsov_andrey")
        self.assertEqual(meta["speakers"]["Кузнец"]["source"], "filename")
        self.assertEqual(status, "complete")

    # Check that date sync uses recorded metadata or the filename and is idempotent.
    def test_sync_md_dates_sets_mtime_from_recorded_at_and_filename_idempotently(self):
        with tempfile.TemporaryDirectory(prefix="md-dates-test-") as tmp:
            root = Path(tmp)
            p1 = root / "00_Diaryze_Data/Text/Phone/2024/call1.m4a.md"
            p2 = root / "00_Diaryze_Data/Text/Phone/2026/1770573484-1-+70000000102.m4a.md"
            p1.parent.mkdir(parents=True)
            p2.parent.mkdir(parents=True)
            p1.write_text("---\nschema_version: '1.0'\nrecorded_at: '2024-06-04T13:18:54'\n---\n\n## Речь\n", encoding="utf-8")
            p2.write_text("---\nschema_version: '1.0'\n---\n\n## Речь\n", encoding="utf-8")
            res = sync_md_dates(root, apply=True)
            self.assertEqual((res["files"], res["updated"], res["no_date"]), (2, 2, 0))
            self.assertEqual(round(p2.stat().st_mtime), 1770573484)
            res2 = sync_md_dates(root, apply=True)
            self.assertEqual((res2["updated"], res2["already_ok"]), (0, 2))


if __name__ == "__main__":
    unittest.main()
