"""Silent phone owners remain identifiable participants in robot-only calls."""

from functools import partial
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from scripts.diarizer import config, transcript
from scripts.diarizer.identification import split_markdown
from scripts.diarizer.owners import with_silent_phone_owner
from scripts.migrate_identification import migrate
from scripts.migrate_phone_owners import patch_phone_owners


class PhoneOwnerTests(unittest.TestCase):
    # Build a temporary archive with nested phone owners and sample robot speech.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="phone-owner-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "Phone/Lena").mkdir(parents=True)
        (self.root / "Phone/.owner").write_text("type: phone\nowner: ivanov_sergey\nlabel: Сергей\n")
        (self.root / "Phone/Lena/.owner").write_text("type: phone\nowner: petrova_elena\nlabel: Елена\n")
        self.people = {
            "ivanov_sergey": {"name": "Сергей Иванов"},
            "petrova_elena": {"name": "Елена Петрова"},
        }
        self.robot = {"Робот": {"person": None, "role": "robot", "temp": True,
                                "note": "Автоответчик, сервис не назван"}}
        self.item = {"audio_rel": "Phone/2025/call.m4a", "sha256": "a" * 64,
                     "duration_ms": 1568810, "direction": "out", "contact": "Елена Петрова",
                     "summary": "Сработал секретарь, затем тишина."}
        self.lines = [(660, "Робот", "На связи секретарь. Расскажите, как я могу помочь?")]

    # Apply silent-owner inference to the fixture with overridable call details.
    def enrich(self, speakers=None, **kwargs):
        args = dict(audio_rel=self.item["audio_rel"], direction="out", used_speakers={"Робот"}, root=self.root)
        args.update(kwargs)
        return with_silent_phone_owner(self.robot if speakers is None else speakers, people=self.people, **args)

    # Render the fixture through production transcript validation.
    def render(self, item=None, **kwargs):
        args = dict(speakers=self.robot, lines=self.lines, no_speech=False,
                    model="test", people=self.people, orgs={})
        args.update(kwargs)
        with mock.patch.object(config, "ROOT", str(self.root)):
            return transcript.validate_and_render_md(item or self.item, **args)

    # Check that an outgoing call with only robot speech retains the known silent owner.
    def test_robot_only_outgoing_call_has_known_silent_owner_and_partial_status(self):
        md = self.render()
        meta = yaml.safe_load(md.split("---", 2)[1])
        self.assertEqual(meta["identification"], "partial")
        self.assertEqual(meta["speakers"]["Сергей"], {
            "person": "ivanov_sergey", "role": "owner", "source": "filename",
            "note": "Владелец телефона; без слышимых реплик",
        })
        self.assertEqual(meta["speakers"]["Робот"], self.robot["Робот"])
        self.assertEqual(md.split("---", 2)[2].strip(),
                         "[00:00:00.66] **Робот** На связи секретарь. Расскажите, как я могу помочь?")
        self.assertEqual(set(self.robot), {"Робот"})
        self.assertNotIn("petrova_elena", str(meta["speakers"]))

    # Check that incoming calls use the nearest folder's owner metadata.
    def test_nearest_owner_and_incoming_direction_are_respected(self):
        speakers = self.enrich(audio_rel="Phone/Lena/call.m4a", direction="in")
        self.assertEqual(speakers["Елена"]["person"], "petrova_elena")
        self.assertNotIn("Сергей", speakers)

    # Check that a known robot organization and known silent owner both count as identified.
    def test_identified_robot_and_silent_owner_are_both_identified(self):
        speakers = {"Робот": {"role": "robot", "org": "service", "source": "speech"}}
        md = self.render(speakers=speakers, orgs={"service": {"name": "Сервис"}})
        self.assertEqual(yaml.safe_load(md.split("---", 2)[1])["identification"], "complete")

    # Check that audible human speech prevents inference and an existing owner is not duplicated.
    def test_human_voice_prevents_owner_inference_and_existing_owner_is_not_duplicated(self):
        for human in ({"person": None, "note": "Телефоном пользуется другой человек"},
                      {"person": "ivanov_sergey", "source": "speech"}):
            speakers = dict(self.robot, Собеседник=human)
            self.assertIs(self.enrich(speakers, used_speakers={"Робот", "Собеседник"}), speakers)

    # Check that an empty recording has no inferred speaker.
    def test_no_speech_does_not_create_a_speaker(self):
        md = self.render(speakers=None, lines=[], no_speech=True)
        meta = yaml.safe_load(md.split("---", 2)[1])
        self.assertEqual(meta["speakers"], {})
        self.assertEqual(meta["identification"], "not_applicable")

    # Check that recorder and unknown-owner metadata prevent silent phone owner inference.
    def test_recorder_override_missing_owner_and_unknown_voice_do_not_infer_owner(self):
        (self.root / "Phone/Recorder").mkdir()
        (self.root / "Phone/Recorder/.owner").write_text("type: recorder\nowner: ivanov_sergey\nlabel: Сергей\n")
        (self.root / "Phone/Unknown").mkdir()
        (self.root / "Phone/Unknown/.owner").write_text("type: phone\nowner: null\n")
        for args in (
            {"audio_rel": "Recordings/memo.wav"},
            {"audio_rel": "Phone/Recorder/memo.wav"},
            {"audio_rel": "Phone/Unknown/call.m4a"},
            {"audio_rel": "../outside.wav"},
            {"used_speakers": {"Робот", "?"}},
            {"direction": None},
        ):
            with self.subTest(args=args):
                self.assertIs(self.enrich(**args), self.robot)
        del self.people["ivanov_sergey"]
        self.assertIs(self.enrich(), self.robot)

    # Check that an owner name collision preserves the audible robot speaker.
    def test_name_collision_does_not_overwrite_robot(self):
        speakers = {"Сергей": self.robot["Робот"]}
        result = self.enrich(speakers, used_speakers={"Сергей"})
        self.assertEqual(result["Сергей"], speakers["Сергей"])
        self.assertEqual(result["Сергей 2"]["person"], "ivanov_sergey")

    # Check that a legacy transcript can infer its audio path from its archive location.
    def test_legacy_md_without_audio_field_uses_its_archive_path(self):
        raw = self.legacy().replace(b"audio: Phone/2025/call.m4a\n", b"")
        changed, status = patch_phone_owners(raw, self.people, {}, root=self.root,
                                            audio_rel=self.item["audio_rel"])
        self.assertEqual(status, "partial")
        meta = yaml.safe_load(changed.split(b"---", 2)[1])
        self.assertNotIn("audio", meta)
        self.assertEqual(meta["speakers"]["Сергей"]["person"], "ivanov_sergey")

    # Build legacy Markdown with a manually edited comment and no silent owner entry.
    def legacy(self, newline=b"\n"):
        raw = self.render(people={}).encode()
        raw = raw.replace(b"speakers:\n", b"# manual comment\nspeakers:\n")
        raw = raw.replace(b"---\n\n[", b"tags: [manual]\n---\n\n[")
        return raw.replace(b"\n", newline)

    # Check that silent-owner migration preserves existing content and is idempotent.
    def test_migration_preserves_body_comments_and_other_metadata_and_is_idempotent(self):
        for newline in (b"\n", b"\r\n"):
            raw = self.legacy(newline)
            changed, status = patch_phone_owners(raw, self.people, {}, root=self.root)
            self.assertEqual(status, "partial")
            old_lines, old_end, old_meta = split_markdown(raw)
            new_lines, new_end, new_meta = split_markdown(changed)
            self.assertEqual(old_lines[old_end + 1:], new_lines[new_end + 1:])
            self.assertIn(b"# manual comment" + newline, changed)
            new_meta.pop("speakers")
            old_meta.pop("speakers")
            old_meta["identification"] = "partial"
            self.assertEqual(old_meta, new_meta)
            self.assertEqual(patch_phone_owners(changed, self.people, {}, root=self.root), (changed, "partial"))

    # Check that the archive migration updates the manifest and preserves the recording mtime.
    def test_archive_migration_updates_manifest_and_preserves_recording_mtime(self):
        raw = self.legacy().replace(b"audio: Phone/2025/call.m4a\n", b"")
        path = self.root / "Text" / (self.item["audio_rel"] + ".md")
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        before_mtime = path.stat().st_mtime_ns
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "people.yaml").write_text(yaml.safe_dump({"people": self.people}))
        manifest = docs / "manifest.jsonl"
        record = {"status": "done", "schema_version": "1.0", "audio": self.item["audio_rel"],
                  "sha256": self.item["sha256"], "identification": "unresolved"}
        manifest.write_text(json.dumps(record) + "\n")
        patcher = partial(patch_phone_owners, root=self.root)
        dry = migrate(self.root, patcher=patcher, patcher_with_path=True)
        self.assertEqual(dry["updated"], 1)
        self.assertEqual(path.read_bytes(), raw)
        result = migrate(self.root, apply=True, patcher=patcher, journal_prefix="phone-owners",
                         patcher_with_path=True)
        self.assertEqual(result["failed"], [])
        self.assertEqual((result["updated"], result["manifest_updated"]), (1, 1))
        self.assertEqual(json.loads(manifest.read_text().splitlines()[-1])["identification"], "partial")
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)
        result = migrate(self.root, apply=True, patcher=patcher, patcher_with_path=True)
        self.assertEqual((result["updated"], result["manifest_updated"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
