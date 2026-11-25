"""Offline integration checks; all writes use a temporary archive."""

import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import wave

import yaml

from scripts.diarizer import (agy, archive, audio, checkpoints, cli, config, console, pipeline,
                              processes, prompts, quota, runtime)


# Wrap a fake provider response in the CLI's expected JSON envelope.
def envelope(text, status="SUCCESS", conversation="test-conversation"):
    return (
        0 if status == "SUCCESS" else 1,
        json.dumps({"status": status, "response": text, "conversation_id": conversation,
                    "usage": {"input_tokens": 10, "output_tokens": 5}}),
        "", None,
    )


REPLY = 'SPEAKERS_JSON: {"Собеседник": {"person": null, "note": "Не представился."}}\n[00:00:01.00] **Собеседник** Привет.'


class TemporaryArchive(unittest.TestCase):
    # Create an isolated archive and reset runtime state for each test.
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="diarizer-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.manifest = self.root / "Data/manifest.jsonl"
        patch = mock.patch.object(config, "ROOT", str(self.root))
        patch.start()
        self.addCleanup(patch.stop)

    # Create a recording fixture and return its pipeline metadata.
    def item(self, name="test.m4a", duration=5000):
        source = self.root / "Phone/2026" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"synthetic input: " + name.encode())
        return {
            "audio_abs": str(source), "audio_rel": str(source.relative_to(self.root)),
            "clean_abs": str(source), "target_md": str(self.root / "Data/Text/Phone/2026" / (name + ".md")),
            "sha256": archive.sha256_file(source), "duration_ms": duration,
            "dt": None, "direction": None, "contact": "Test", "phones": [],
        }


class ArchiveTests(TemporaryArchive):
    # Check that archive scans exclude transcript output, service folders, and nonaudio files.
    def test_whole_archive_scan_excludes_text_services_and_nonaudio(self):
        wanted = {"Phone/2019/call.wav", "Phone/2025/call.m4a", "Recordings/mono.mp3",
                  "Smartphone/voice.opus", "root.amr", "Phone/.контакт.m4a", "Phone/raw"}
        excluded = {"Text/Phone/fake.wav", "Data/Text/Phone/fake.wav", "docs/example.wav", "scripts/fixture.wav", "Data/voice_samples/sample.flac",
                    ".agents/sample.m4a", "Phone/.Trash/deleted.m4a", "Phone/.nomedia"}
        for name in wanted | excluded:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        (self.root / "Phone/readme.json").write_text('{}')
        (self.root / "Smartphone/backup.crypt12").write_bytes(b"encrypted backup")
        renamed = self.root / "Recordings/sound.blob"
        renamed.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        wanted.add("Recordings/sound.blob")
        (self.root / "Phone/link.m4a").symlink_to(self.root / "root.amr")
        found = {x["audio_rel"] for x in archive.scan_pending()}
        self.assertEqual(found, wanted)
        scoped = archive.scan_pending(str(self.root / "Recordings"))
        self.assertEqual({x["audio_rel"] for x in scoped}, {"Recordings/mono.mp3", "Recordings/sound.blob"})

    # Check that an explicit source folder cannot escape the archive or name a service path.
    def test_source_folder_rejects_service_directories_and_escape(self):
        for name in ("Text", "docs", "scripts"):
            (self.root / name).mkdir()
            with self.subTest(folder=name), self.assertRaises(ValueError):
                archive.scan_pending(name)
        for path in ("..", str(self.root.parent), "missing"):
            with self.subTest(folder=path), self.assertRaises(ValueError):
                archive.resolve_source_dir(path)

    # Check recognition of recorder and WhatsApp dates and unknown call direction.
    def test_dates_from_miui_recorders_whatsapp_and_unknown_direction(self):
        cases = {
            "70000000106_190627_134712.m4a": "2019-06-27T13:47:12",
            "Запись вызовов Контакт_200105_131659.m4a": "2020-01-05T13:16:59",
            "recording-20170618-153931.mp3": "2017-06-18T15:39:31",
            "PTT-20191110-WA0000.opus": "2019-11-10",
            "20150911_заметка.wav": "2015-09-11",
            "Контакт_+70000000110_2024-06-04_13-18-54_unknown.m4a": "2024-06-04T13:18:54",
            "1761650305-0-+70000000108.m4a": "2025-10-28T14:18:25+03:00",
            "0d20190914194949p+70000000107.3gp": "2019-09-14T19:49:49",
        }
        for name, expected in cases.items():
            with self.subTest(filename=name):
                self.assertEqual(archive.parse_filename(name)[0], expected)
        self.assertIsNone(archive.parse_filename("PTT-20191332-WA0000.opus")[0])
        self.assertEqual(archive.parse_filename("70000000106_190627_134712.m4a")[3], ["+70000000106"])
        vrf = archive.parse_filename("0d20190914194949p+70000000107_incoming.3gp")
        self.assertEqual(vrf[1], "in")
        self.assertEqual(vrf[3], ["+70000000107"])

    # Check that current hashes and terminal statuses determine whether audio is pending.
    def test_queue_respects_hashes_and_terminal_manifest_states(self):
        items = {name: self.item(name + ".m4a") for name in ("fresh", "done", "changed", "skipped", "refused")}
        for name in ("done", "changed"):
            item = items[name]
            md = Path(item["target_md"])
            md.parent.mkdir(parents=True, exist_ok=True)
            digest = item["sha256"] if name == "done" else "0" * 64
            md.write_text(f'---\nsha256: "{digest}"\n---\n', encoding="utf-8")
        for name in ("done", "skipped", "refused"):
            archive.append_manifest({"audio": items[name]["audio_rel"], "sha256": items[name]["sha256"], "status": name})
        with self.manifest.open("a") as f:
            f.write("invalid interrupted line\n")
        pending = archive.scan_pending("Phone/2026")
        self.assertEqual({Path(x["audio_rel"]).stem for x in pending}, {"fresh", "changed"})
        retried = archive.scan_pending("Phone/2026", retry_skipped=True, retry_refused=True)
        self.assertEqual({Path(x["audio_rel"]).stem for x in retried}, {"fresh", "changed", "skipped", "refused"})


class PipelineTests(TemporaryArchive):
    # Build the default options for pipeline tests with an optional legacy quota flag.
    def args(self, fallback=False):
        return SimpleNamespace(model="first", model_chain=["first", "second"], timeout=10,
                               no_custom_agent=False, stop_on_quota=False, fallback_on_quota=fallback)

    # Check that successful batches publish transcripts, manifest entries, and usage.
    def test_batch_publishes_markdown_manifest_and_usage(self):
        items = [self.item("first.m4a"), self.item("other/second.m4a")]
        items[0]["dt"] = "2025-03-14"
        people = {"ivanov_ivan": {"name": "Иван Иванов"}}
        scope = self.root / "Phone/2026/other"
        (scope / ".owner").write_text("type: recorder\n", encoding="utf-8")
        (self.root / "Phone/.owner").write_text("type: phone\nowner: ivanov_ivan\n", encoding="utf-8")
        reference = self.root / "Data/voice_samples/ivanov_ivan/phone/confirmed/2025-03-14_01.flac"
        reference.parent.mkdir(parents=True)
        reference.write_bytes(b"approved fixture")
        reply = "\n".join(f"=== ITEM {i} ===\n{REPLY}" for i in (1, 2))
        stats = pipeline.Stats()
        with mock.patch.object(pipeline, "run_agy", return_value=envelope(reply)) as run, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()) as output:
            pipeline.process_short_batch(items, self.args(), people, {}, stats)
        prompt = run.call_args.args[0]
        self.assertEqual(prompt.count(str(reference)), 1)
        self.assertLess(prompt.index(str(reference)), prompt.index("ITEM 2:"))
        self.assertEqual((stats.done, stats.audio_ms, stats.tok_in, stats.tok_out), (2, 10000, 10, 5))
        self.assertEqual(len(output.getvalue().splitlines()), 6)
        self.assertIn("Готово 1", output.getvalue())
        self.assertIn("Готово 2", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())
        for item, expected_sample in zip(items, ("ivanov_ivan/phone/confirmed/2025-03-14_01.flac", "none"), strict=True):
            md = Path(item["target_md"]).read_text()
            self.assertIn("[00:00:01.00] **Собеседник** Привет.", md)
            meta = yaml.safe_load(md.split("---", 2)[1])
            self.assertEqual(meta["schema_version"], "1.0")
            self.assertEqual(meta["voice_sample"], expected_sample)
            self.assertEqual(meta["sha256"], item["sha256"])
            self.assertEqual(set(meta["speakers"]), {"Собеседник"})
            self.assertEqual(meta["identification"], "unresolved")
            self.assertFalse(Path(item["target_md"] + ".part").exists())
        self.assertEqual(len(archive.load_manifest_state()), 2)

    # Bypass voice selection and record that the batch ran without a sample.
    def test_batch_can_force_recognition_without_voice_samples(self):
        item = self.item()
        args = self.args()
        args.no_voice_samples = True
        with mock.patch.object(pipeline, "owner_reference") as select, \
                mock.patch.object(pipeline, "run_agy", return_value=envelope(REPLY)), \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_short_batch([item], args, {}, {}, pipeline.Stats())
        select.assert_not_called()
        md = Path(item["target_md"]).read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(md.split("---", 2)[1])["voice_sample"], "none")

    # Check that published Markdown inherits its recording date as mtime.
    def test_commit_md_sets_mtime_from_recorded_at(self):
        item = self.item("call_2024-06-04_13-18-54_incoming.m4a")
        item["dt"] = "2024-06-04T13:18:54"
        with mock.patch.object(pipeline, "run_agy", return_value=envelope(REPLY)), \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_short_batch([item], self.args(), {}, {}, pipeline.Stats())
        expected_ts = datetime.fromisoformat("2024-06-04T13:18:54+03:00").timestamp()
        self.assertAlmostEqual(Path(item["target_md"]).stat().st_mtime, expected_ts, delta=1.0)

    # Check that quota fallback works with and without the legacy compatibility flag.
    def test_quota_fallback_is_automatic_with_or_without_compatibility_flag(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                item = self.item(f"quota-{fallback}.m4a")
                stats = pipeline.Stats()
                responses = [envelope("Quota reached. Resets in 2h11m10s.", "ERROR"), envelope(REPLY)]
                with mock.patch.object(pipeline, "run_agy", side_effect=responses) as run, \
                        mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
                    pipeline.process_short_batch([item], self.args(fallback), {}, {}, stats)
                self.assertEqual([call.args[1] for call in run.call_args_list],
                                 ["first", "second"])
                self.assertGreater(stats.models.remaining('first'), 7800)
                self.assertEqual(stats.done, 1)

    # Check that model refusals try eligible alternatives without publishing Markdown.
    def test_refusal_tries_chain_without_publishing_a_transcript(self):
        item = self.item()
        refused = envelope("Your previous response was blocked by content safety filters", "ERROR")
        with mock.patch.object(pipeline, "run_agy", return_value=refused) as run, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(pipeline.ModelRefused) as raised:
                pipeline.process_short_batch([item], self.args(), {}, {}, pipeline.Stats())
        self.assertEqual(raised.exception.models_tried, ["first", "second"])
        self.assertEqual(run.call_count, 2)
        self.assertFalse(Path(item["target_md"]).exists())

    # Check fallback after service overload for both short batches and long recordings.
    def test_503_switches_across_model_chain_for_batches_and_long_items(self):
        err503 = (
            3,
            json.dumps({"status": "ERROR", "error": "Our servers are experiencing high traffic right now. (UNAVAILABLE (code 503): No capacity available)"}),
            'AGY_ERROR: {"status":"UNAVAILABLE","error_code":503}',
            None,
        )
        chain_args = SimpleNamespace(
            model="first", model_chain=["first", "second", "third"],
            timeout=10, no_custom_agent=False, stop_on_quota=False, fallback_on_quota=False,
        )
        # Multi-item short batch switches directly to second -> third without splitting
        items = [self.item("b1.m4a"), self.item("b2.m4a")]
        reply2 = "\n".join(f"=== ITEM {i} ===\n{REPLY}" for i in (1, 2))
        stats = pipeline.Stats()
        with mock.patch.object(pipeline, "run_agy", side_effect=[err503, err503, envelope(reply2)]) as run, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_short_batch(items, chain_args, {}, {}, stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ["first", "second", "third"])
        self.assertEqual(stats.done, 2)
        meta = yaml.safe_load(Path(items[0]["target_md"]).read_text(encoding="utf-8").split("---", 2)[1])
        self.assertEqual(meta["model"], "third")

        # Long recording part switches model on 503 as well
        long_item = self.item("long-503.m4a", duration=12000)
        long_item["parts"] = [{"part": 1, "path": "part1.wav", "duration_ms": 12000, "offset_ms": 0}]
        long_stats = pipeline.Stats()
        with mock.patch.object(pipeline, "run_agy", side_effect=[err503, envelope(REPLY)]) as run_l, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_long_item(long_item, chain_args, {}, {}, long_stats)
        self.assertEqual([c.args[1] for c in run_l.call_args_list], ["first", "second"])
        self.assertEqual(long_stats.done, 1)

        # Exhausting all models with 503 waits for availability, interruptibly.
        exhaust_item = self.item("exhaust-503.m4a")
        exhaust_stats = pipeline.Stats()
        with mock.patch.object(pipeline, "run_agy", return_value=err503), \
                mock.patch.object(runtime.STOP_EVENT, "wait", return_value=True) as waiting, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(InterruptedError):
                pipeline.process_short_batch([exhaust_item], chain_args, {}, {}, exhaust_stats)
        waiting.assert_called_once()
        self.assertEqual(exhaust_stats.errors, 0)
        self.assertFalse(Path(exhaust_item["target_md"]).exists())

    # Check that salvaged results use one standard warning block after fallback is exhausted.
    def test_salvaged_result_has_one_three_line_warning_block_after_chain_exhaustion(self):
        item = self.item()
        stats = pipeline.Stats()
        too_late = REPLY.replace("00:00:01.00", "00:00:08.00")
        with mock.patch.object(pipeline, "run_agy", return_value=envelope(too_late)), \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()) as output:
            pipeline.process_short_batch([item], self.args(), {}, {}, stats)
        self.assertEqual(stats.done, 1)
        lines = output.getvalue().splitlines()
        header = next(i for i, line in enumerate(lines) if item['target_md'] in line)
        self.assertEqual(len(lines[header:]), 3)
        self.assertTrue(all(line.startswith('[WARN]') for line in lines[:header]))
        self.assertIn("WARN", lines[header])
        self.assertIn("Готово 1", output.getvalue())

    # Check session continuity and absolute timestamps across parts of a long recording.
    def test_long_recording_keeps_session_and_absolute_timestamps(self):
        item = self.item(duration=20000)
        item["parts"] = [
            {"part": 1, "path": "part1.wav", "duration_ms": 12000, "offset_ms": 0},
            {"part": 2, "path": "part2.wav", "duration_ms": 10000, "offset_ms": 10000},
        ]
        first = REPLY + "\n[00:00:11.00] **Собеседник:** Перекрытие."
        second = "[00:00:00.50] **Собеседник** Дубль.\n[00:00:02.00] **Собеседник** Продолжение."
        stats = pipeline.Stats()
        with mock.patch.object(pipeline, "run_agy", side_effect=[envelope(first), envelope(second)]) as run, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_long_item(item, self.args(), {}, {}, stats)
        self.assertEqual(run.call_args_list[1].kwargs["conversation_id"], "test-conversation")
        md = Path(item["target_md"]).read_text()
        self.assertIn("[00:00:12.00] **Собеседник** Продолжение.", md)
        self.assertNotIn("Дубль.", md)
        self.assertEqual(yaml.safe_load(md.split("---", 2)[1])["voice_sample"], "none")
        self.assertEqual((stats.done, stats.audio_ms), (1, 20000))

    # Check that later parts preserve earlier speaker links.
    def test_later_part_updates_do_not_erase_earlier_identity_links(self):
        item = self.item(duration=20000)
        item["parts"] = [
            {"part": 1, "path": "part1.wav", "duration_ms": 12000, "offset_ms": 0},
            {"part": 2, "path": "part2.wav", "duration_ms": 10000, "offset_ms": 10000},
        ]
        first = 'SPEAKERS_JSON: {"Известный": {"person": "known", "source": "speech", "note": "Представился."}, "Иван": {"person": null, "note": "Не представился."}}\n[00:00:01.00] Известный: Привет.\n[00:00:11.00] Иван: Здравствуйте.'
        second = 'SPEAKERS_JSON: {"Иван": {"person": null, "label": "Иван", "note": "Назвал имя, фамилия и карточка неизвестны."}}\n[00:00:02.00] Иван: Продолжение.'
        with mock.patch.object(pipeline, "run_agy", side_effect=[envelope(first), envelope(second)]), \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_long_item(item, self.args(), {"known": {"name": "Известный"}}, {}, pipeline.Stats())
        meta = yaml.safe_load(Path(item["target_md"]).read_text().split("---", 2)[1])
        self.assertEqual(meta["speakers"]["Известный"]["person"], "known")
        self.assertEqual(meta["identification"], "partial")

    # Check that a filename contact is registered once and reused on later encounters.
    def test_auto_registers_new_named_contact_from_filename_without_duplicating(self):
        item = self.item("Иван Петров_+70000000104_2026-08-01_12-00-00_incoming.m4a")
        item["contact"] = "Иван Петров"
        item["phones"] = ["+70000000104"]
        people = {"ivanov_sergey": {"name": "Сергей Иванов"}}
        reply = (
            'SPEAKERS_JSON: {"Иван": {"person": "petrov_ivan", "source": "filename", "label": "Иван Петров", "note": "По контакту файла."}}\n'
            "[00:00:01.00] **Иван** Привет."
        )
        stats = pipeline.Stats()
        with mock.patch.object(pipeline, "run_agy", return_value=envelope(reply)), \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_short_batch([item], self.args(), people, {}, stats)
        self.assertIn("petrov_ivan", people)
        self.assertEqual(people["petrov_ivan"]["name"], "Иван Петров")
        self.assertEqual(people["petrov_ivan"]["phones"], ["+70000000104"])
        disk_people = yaml.safe_load((self.root / "Data/people.yaml").read_text(encoding="utf-8"))["people"]
        self.assertIn("petrov_ivan", disk_people)
        md_meta = yaml.safe_load(Path(item["target_md"]).read_text(encoding="utf-8").split("---", 2)[1])
        self.assertEqual(md_meta["speakers"]["Иван"]["person"], "petrov_ivan")
        self.assertEqual(md_meta["identification"], "complete")

    # Check that a new robot organization is registered even for temporary speakers.
    def test_auto_registers_new_robot_org_even_with_temp_true(self):
        item = self.item("88007008000_88007008000_2025-12-29_15-06-37_outgoing.m4a")
        item["contact"] = "88007008000"
        item["phones"] = ["+78000000112"]
        orgs = {}
        reply = (
            'SPEAKERS_JSON: {"Робот": {"org": "beeline", "role": "robot", "temp": true, '
            '"source": "speech", "label": "Билайн", "note": "Электронный консультант Билайн."}}\n'
            "[00:00:00.50] **Робот** Это Билайн."
        )
        stats = pipeline.Stats()
        with mock.patch.object(pipeline, "run_agy", return_value=envelope(reply)), \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_short_batch([item], self.args(), {}, orgs, stats)
        self.assertIn("beeline", orgs)
        self.assertEqual(orgs["beeline"]["name"], "Билайн")
        self.assertEqual(orgs["beeline"]["phones"], ["+78000000112"])
        disk_orgs = yaml.safe_load((self.root / "Data/orgs.yaml").read_text(encoding="utf-8"))["orgs"]
        self.assertIn("beeline", disk_orgs)
        md_meta = yaml.safe_load(Path(item["target_md"]).read_text(encoding="utf-8").split("---", 2)[1])
        self.assertEqual(md_meta["speakers"]["Робот"]["org"], "beeline")
        self.assertEqual(md_meta["identification"], "complete")

    # Check that a distinctive nickname is kept in the catalog and generic forms are discarded.
    def test_auto_appends_distinctive_nickname_without_leaking_into_md_or_adding_banal_forms(self):
        item = self.item("Владимир Смирнов_+70000000108_2026-08-02_12-00-00_incoming.m4a")
        item["contact"] = "Владимир Смирнов"
        item["phones"] = ["+70000000108"]
        people = {
            "smirnov_vladimir": {"name": "Владимир Смирнов", "phones": ["+70000000108"]},
        }
        (self.root / "docs").mkdir(parents=True, exist_ok=True)
        (self.root / "docs/people.yaml").write_text(
            yaml.safe_dump({"schema_version": "1.0", "people": people}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        reply = (
            'SPEAKERS_JSON: {"Владимир": {"person": "smirnov_vladimir", "source": "speech", '
            '"nicknames": ["Кузнец", "Вова", "брат", "кузнец", "Владимир"]}, '
            '"Курьер": {"person": null, "temp": true, "nicknames": ["Быстрый"], "note": "Курьер доставки."}}\n'
            "[00:00:01.00] **Владимир** Привет.\n"
            "[00:00:02.00] **Курьер** Доставка."
        )
        stats = pipeline.Stats()
        with mock.patch.object(pipeline, "run_agy", return_value=envelope(reply)), \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_short_batch([item], self.args(), people, {}, stats)
        self.assertEqual(people["smirnov_vladimir"]["nicknames"], ["Кузнец"])
        disk_doc = yaml.safe_load((self.root / "docs/people.yaml").read_text(encoding="utf-8"))
        self.assertEqual(disk_doc["schema_version"], "1.1")
        self.assertEqual(disk_doc["people"]["smirnov_vladimir"]["nicknames"], ["Кузнец"])
        md_meta = yaml.safe_load(Path(item["target_md"]).read_text(encoding="utf-8").split("---", 2)[1])
        self.assertNotIn("nicknames", md_meta["speakers"]["Владимир"])
        self.assertNotIn("nicknames", md_meta["speakers"]["Курьер"])


class CheckpointTests(TemporaryArchive):
    # Build job options for long-recording checkpoint tests.
    def args(self):
        return SimpleNamespace(model="first", model_chain=["first", "second"], timeout=10,
                               no_custom_agent=False, stop_on_quota=False, fallback_on_quota=False)

    # Construct a two-part recording with fixed segment boundaries.
    def long_item(self):
        item = self.item("long-checkpoint.m4a", duration=20000)
        item["parts"] = [
            {"part": 1, "path": "part1.wav", "duration_ms": 10000, "offset_ms": 0},
            {"part": 2, "path": "part2.wav", "duration_ms": 10000, "offset_ms": 10000},
        ]
        return item

    # Store the first accepted segment in a checkpoint for resume tests.
    def save_first_part(self, checkpoint, voice_sample=None):
        checkpoint.save(1,
                        speakers={"Собеседник": {"person": None, "note": "Не представился."}},
                        lines=[[1000, "Собеседник", "Привет."]],
                        model="first", summary="Обсудили бумагу.", voice_sample=voice_sample)

    # Check that a saved first part is reused and the checkpoint removed after publication.
    def test_resumes_from_saved_part_and_discards_checkpoint(self):
        item = self.long_item()
        checkpoint = checkpoints.LongCheckpoint(item)
        self.save_first_part(checkpoint)
        saved = json.loads(checkpoint.path.read_text(encoding="utf-8"))
        self.assertNotIn("no_speech", saved["parts"]["1"])
        del saved["parts"]["1"]["voice_sample"]
        checkpoint.path.write_text(json.dumps(saved), encoding="utf-8")

        second = ('SPEAKERS_JSON: {"Собеседник": {"person": null, "note": "Не представился."}}\n'
                  '[00:00:02.00] **Собеседник** Продолжение.')
        stats = pipeline.Stats()
        with mock.patch.object(pipeline, "run_agy", return_value=envelope(second)) as run, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_long_item(item, self.args(), {}, {}, stats)
        self.assertEqual(run.call_count, 1)
        self.assertIn("Known speakers from earlier parts", run.call_args.args[0])
        self.assertIn("Обсудили бумагу.", run.call_args.args[0])
        md = Path(item["target_md"]).read_text(encoding="utf-8")
        self.assertIn("[00:00:01.00] **Собеседник** Привет.", md)
        self.assertIn("[00:00:12.00] **Собеседник** Продолжение.", md)
        self.assertIsNone(yaml.safe_load(md.split("---", 2)[1])["voice_sample"])
        self.assertEqual(stats.done, 1)
        self.assertFalse(checkpoint.path.exists())

    # Keep a shared voice reference when resuming a recording processed with that sample.
    def test_resumes_with_the_same_voice_sample(self):
        item = self.long_item()
        label = "ivanov_ivan/phone/confirmed/2025-03-14_01.flac"
        reference = self.root / "Data/voice_samples" / label
        reference.parent.mkdir(parents=True)
        reference.write_bytes(b"approved fixture")
        self.save_first_part(checkpoints.LongCheckpoint(item), voice_sample=label)
        with mock.patch.object(pipeline, "owner_reference", return_value={"person": "ivanov_ivan", "file": str(reference)}), \
                mock.patch.object(pipeline, "run_agy", return_value=envelope(REPLY)) as run, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_long_item(item, self.args(), {}, {}, pipeline.Stats())
        self.assertIn(str(reference), run.call_args.args[0])
        md = Path(item["target_md"]).read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(md.split("---", 2)[1])["voice_sample"], label)

    # Redo parts that used a sample when the recording is resumed without samples.
    def test_without_samples_reprocesses_a_checkpoint_with_a_reference(self):
        item = self.long_item()
        label = "ivanov_ivan/phone/confirmed/2025-03-14_01.flac"
        self.save_first_part(checkpoints.LongCheckpoint(item), voice_sample=label)
        args = self.args()
        args.no_voice_samples = True
        with mock.patch.object(pipeline, "owner_reference") as select, \
                mock.patch.object(pipeline, "run_agy", return_value=envelope(REPLY)) as run, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_long_item(item, args, {}, {}, pipeline.Stats())
        select.assert_not_called()
        self.assertEqual(run.call_count, 2)
        md = Path(item["target_md"]).read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(md.split("---", 2)[1])["voice_sample"], "none")

    # Check that changed segment boundaries invalidate incompatible checkpoints.
    def test_layout_change_ignores_saved_parts(self):
        item = self.long_item()
        checkpoint = checkpoints.LongCheckpoint(item)
        self.save_first_part(checkpoint)
        item["parts"][0]["duration_ms"] = 11000

        with mock.patch.object(pipeline, "run_agy", side_effect=[envelope(REPLY), envelope(REPLY)]) as run, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_long_item(item, self.args(), {}, {}, pipeline.Stats())
        self.assertEqual(run.call_count, 2)
        self.assertFalse(checkpoint.path.exists())

    # Check that a corrupt saved part is recomputed while valid earlier parts are reused.
    def test_invalid_saved_part_is_recomputed_without_touching_earlier_part(self):
        item = self.long_item()
        checkpoint = checkpoints.LongCheckpoint(item)
        self.save_first_part(checkpoint)
        checkpoint.save(2,
                        speakers={"Собеседник": {"person": None, "note": "Не представился."}},
                        lines=[[1000, "Собеседник"]], model="first", summary=None)

        second = '[00:00:02.00] **Собеседник** Продолжение.'
        with mock.patch.object(pipeline, "run_agy", return_value=envelope(second)) as run, \
                mock.patch.object(pipeline, "agy_forget"), contextlib.redirect_stdout(io.StringIO()):
            pipeline.process_long_item(item, self.args(), {}, {}, pipeline.Stats())
        self.assertEqual(run.call_count, 1)
        md = Path(item["target_md"]).read_text(encoding="utf-8")
        self.assertIn("[00:00:01.00] **Собеседник** Привет.", md)
        self.assertIn("[00:00:12.00] **Собеседник** Продолжение.", md)


class AudioTests(TemporaryArchive):
    # Check that truncated AAC requires recovery and can be prepared through FAAD.
    @unittest.skipUnless(shutil.which("ffprobe") and shutil.which("ffmpeg"), "ffmpeg/ffprobe required")
    def test_truncated_aac_needs_recovery_without_tools_and_recovers_with_faad(self):
        reference = self.item("reference.m4a")
        generated = processes.run_capture([
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=44100:duration=2",
            "-c:a", "aac", reference["audio_abs"],
        ], timeout=15)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        data = Path(reference["audio_abs"]).read_bytes()
        size = 0
        while size < len(data) and data[size + 4:size + 8] != b"moov":
            box_size = int.from_bytes(data[size:size + 4], "big")
            self.assertGreaterEqual(box_size, 8)
            size += box_size
        self.assertEqual(data[size + 4:size + 8], b"moov")
        item = self.item("interrupted.m4a")
        path = Path(item["audio_abs"])
        path.write_bytes(data[:size])
        digest = archive.sha256_file(str(path))
        args = SimpleNamespace(max_single_sec=20, chunk_sec=10, overlap_sec=0,
                               use_faad=False, untrunc_bin=None)
        stats = pipeline.Stats()
        with mock.patch.object(pipeline, "run_agy") as run_agy, contextlib.redirect_stdout(io.StringIO()):
            pipeline.run_job([item], args, stats)
        run_agy.assert_not_called()
        failed = archive.load_manifest_state()[item["audio_rel"]]
        self.assertEqual(failed["status"], "needs_recovery")
        self.assertEqual(failed["sha256"], digest)
        self.assertIn("moov atom not found", failed["reason"])
        self.assertEqual(stats.snapshot()["needs_recovery"], 1)
        self.assertFalse(Path(item["target_md"]).exists())
        self.assertNotIn(item["audio_rel"], {f["audio_rel"] for f in archive.scan_pending()})
        self.assertIn(item["audio_rel"], {f["audio_rel"] for f in archive.scan_pending(retry_recovery=True)})

        if audio._get_libfaad():
            work = self.root / "recover"
            work.mkdir()
            ready, error = audio.prepare_audio_work(item, str(work), 20, 10, 0, untrunc_bin=None)
            self.assertIsNone(error)
            self.assertAlmostEqual(ready["duration_ms"], 2000, delta=200)
            self.assertEqual(ready["sha256"], digest)
            self.assertIsNone(audio.decode_audio_error(ready["clean_abs"]))
        self.assertEqual(archive.sha256_file(str(path)), digest)

        path.write_bytes(data)
        self.assertIn(item["audio_rel"], {f["audio_rel"] for f in archive.scan_pending()})

    # Check that recovery retry includes previously damaged inputs and excludes empty files.
    def test_retry_recovery_includes_legacy_damage_but_not_empty_inputs(self):
        broken = self.item("broken.m4a")
        empty = self.item("empty.m4a")
        for item, reason in ((broken, "moov atom not found"), (empty, "empty_file")):
            archive.append_manifest({"audio": item["audio_rel"], "status": "skipped", "reason": reason})
        self.assertEqual(archive.scan_pending(), [])
        retried = archive.scan_pending(retry_recovery=True)
        self.assertEqual([f["audio_rel"] for f in retried], [broken["audio_rel"]])

    # Check that video contributes only its audio stream and that video without audio is rejected.
    @unittest.skipUnless(shutil.which("ffprobe") and shutil.which("ffmpeg"), "ffmpeg/ffprobe required")
    def test_video_uses_only_audio_and_silent_video_is_rejected(self):
        for sound in (False, True):
            with self.subTest(audio=sound):
                item = self.item(f"video-{sound}.mp4")
                cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=s=16x16:r=10"]
                if sound:
                    cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000"]
                cmd += ["-t", "0.5", "-c:v", "mpeg4"]
                if sound:
                    cmd += ["-c:a", "aac"]
                cmd += [item["audio_abs"]]
                generated = processes.run_capture(cmd, timeout=10)
                self.assertEqual(generated.returncode, 0, generated.stderr)
                digest = archive.sha256_file(item["audio_abs"])
                work = self.root / f"work-{sound}"
                work.mkdir()
                with mock.patch.object(audio, "faad_recover_aac") as recovery:
                    ready, error = audio.prepare_audio_work(item, str(work), 20, 10, 0)
                recovery.assert_not_called()
                if not sound:
                    self.assertIsNone(ready)
                    self.assertEqual(error, "no_audio_stream")
                else:
                    self.assertIsNone(error)
                    probe = processes.run_capture(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type", "-of", "json", ready["clean_abs"]])
                    types = [s["codec_type"] for s in json.loads(probe.stdout)["streams"]]
                    self.assertEqual(types, ["audio"])
                self.assertEqual(archive.sha256_file(item["audio_abs"]), digest)

    # Check that a container unsupported by the model can be prepared as WAV.
    @unittest.skipUnless(shutil.which("ffprobe") and shutil.which("ffmpeg"), "ffmpeg/ffprobe required")
    def test_unusual_container_can_fall_back_to_temporary_wav(self):
        item = self.item("sound.blob")
        with wave.open(item["audio_abs"], "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(8000)
            f.writeframes(b"\0\0" * 8000)
        digest = archive.sha256_file(item["audio_abs"])
        work = self.root / "work"
        work.mkdir()
        ready, error = audio.prepare_audio_work(item, str(work), 20, 10, 0, use_faad=False)
        self.assertIsNone(error)
        self.assertTrue(ready["clean_abs"].endswith(".wav"))
        self.assertEqual(ready["sha256"], digest)
        self.assertEqual(archive.sha256_file(item["audio_abs"]), digest)

    # Check that remuxing and slicing leave the original recording byte-identical.
    @unittest.skipUnless(shutil.which("ffprobe") and shutil.which("ffmpeg"), "ffmpeg/ffprobe required")
    def test_prepare_remuxes_and_slices_without_changing_source(self):
        item = self.item("sample.wav")
        with wave.open(item["audio_abs"], "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(8000)
            f.writeframes(b"\0\0" * 28000)
        digest = archive.sha256_file(item["audio_abs"])
        work = self.root / "work"
        work.mkdir()
        ready, error = audio.prepare_audio_work(item, str(work), 2, 2, 0, use_faad=False)
        self.assertIsNone(error)
        self.assertEqual(ready["duration_ms"], 3500)
        self.assertEqual([p["offset_ms"] for p in ready["parts"]], [0, 2000])
        self.assertEqual(ready["sha256"], digest)
        self.assertEqual(archive.sha256_file(item["audio_abs"]), digest)
        self.assertTrue(all(Path(p["path"]).is_file() for p in ready["parts"]))


class RuntimeTests(unittest.TestCase):
    # Check that the default archive scope and both folder option names behave consistently.
    def test_cli_default_and_folder_aliases(self):
        cwd = Path.cwd()
        cases = (
            ([], cwd, "."),
            (["--folder", "Recordings"], cwd, "Recordings"),
            (["--subdir", "Phone/2020"], cwd, "Phone/2020"),
            (["--root", "archive", "--folder", "Phone/2020"], cwd / "archive", "Phone/2020"),
        )
        for flags, root, folder in cases:
            argv = ["diarize.py", "--dry-run", "--no-faad", "--no-untrunc", *flags]
            with self.subTest(flags=flags), mock.patch("sys.argv", argv), \
                    mock.patch.object(cli, "resolve_source_dir"), \
                    mock.patch.object(cli, "run_workers") as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                cli.main()
            self.assertEqual(Path(run.call_args.args[0].root), root)
            self.assertEqual(run.call_args.args[0].subdir, folder)

    # Check the default model fallback chain.
    def test_cli_default_model_chain(self):
        argv = ["diarize.py", "--dry-run", "--no-faad", "--no-untrunc"]
        with mock.patch("sys.argv", argv), \
                mock.patch.object(cli, "resolve_source_dir"), \
                mock.patch.object(cli, "run_workers") as rw, \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main()
        self.assertEqual(
            rw.call_args.args[0].model_chain,
            ["gemini-4-hydrogen-medium", "gemini-3.8-flash-medium"],
        )

    # Check that recorder prompts avoid assumptions about ownership or the number of speakers.
    def test_recorder_hint_does_not_assume_phone_owner_or_two_people(self):
        hint = prompts.owner_hint("Recordings/mono.wav")
        self.assertNotIn("ivanov_sergey", hint)
        self.assertIn("one speaker key", hint)
        self.assertIn("No device owner", prompts.owner_hint("Phone/2026/call.m4a"))

    # Check that a large catalog is filtered using the recording's contact and phone number.
    def test_prompt_refs_filter_large_catalog_by_item_phone_and_contact(self):
        people = {f"person_{i}": {"name": f"Контакт {i}", "phones": [f"+7000000{i:04d}"]} for i in range(30)}
        people["ivanov_sergey"] = {"name": "Сергей Иванов", "tags": ["owner"]}
        people["target_p"] = {"name": "Иван Тестов", "phones": ["+70000000104"]}
        people["nick_match"] = {"name": "Пётр Соколов", "nicknames": ["Сокол"]}
        people["person_6"]["nicknames"] = ["Кузнец", "Мастер"]
        orgs = {"target_o": {"name": "Такси Пример", "phones": ["+70000000105"]}}
        rendered = json.loads(prompts.format_refs_for_prompt(
            people, orgs, [{"contact": "Иван Сокол", "phones": ["80000000104"]}],
        ))
        self.assertIn("ivanov_sergey", rendered["people.yaml"])
        self.assertIn("target_p", rendered["people.yaml"])
        self.assertIn("nick_match", rendered["people.yaml"])
        self.assertNotIn("person_5", rendered["people.yaml"])
        self.assertEqual(rendered["other_people_keys"]["person_5"], "Контакт 5")
        self.assertEqual(rendered["other_people_keys"]["person_6"], "Контакт 6 [Кузнец, Мастер]")
        self.assertEqual(rendered["other_orgs_keys"]["target_o"], "Такси Пример")

    # Check nearest-owner lookup and the ordering of cached prompt references.
    def test_owner_dotfile_resolution_and_prompt_cache_order(self):
        with tempfile.TemporaryDirectory(prefix="owner-test-") as tmp:
            root = Path(tmp)
            (root / "Phone/Lena").mkdir(parents=True)
            (root / "Recordings").mkdir(parents=True)
            (root / "Phone/.owner").write_text("type: phone\nowner: ivanov_sergey\n", encoding="utf-8")
            (root / "Phone/Lena/.owner").write_text("type: phone\nowner: petrova_elena\n", encoding="utf-8")
            (root / "Recordings/.owner").write_text("type: recorder\nowner: null\n", encoding="utf-8")
            self.assertIn("ivanov_sergey", prompts.owner_hint("Phone/2020/call.m4a", root=str(root)))
            self.assertIn("petrova_elena", prompts.owner_hint("Phone/Lena/call.m4a", root=str(root)))
            self.assertIn("one speaker key", prompts.owner_hint("Recordings/memo.wav", root=str(root)))
        batch = prompts.build_batch_prompt(
            [{"clean_abs": "/tmp/a.m4a", "audio_rel": "Phone/2020/a.m4a", "duration_ms": 1000, "contact": "C", "phones": []}],
            {"ivanov_sergey": {"name": "Сергей"}}, {},
        )
        self.assertLess(batch.index("Transcribe speech faithfully"), batch.index("people.yaml"))
        self.assertLess(batch.index("people.yaml"), batch.index("ITEM 1:"))

    # Check that processing modules share the same cancellation and console state.
    def test_modules_share_cancellation_and_console_objects(self):
        self.assertIs(runtime.STOP_EVENT, pipeline.STOP_EVENT)
        self.assertIs(runtime.STOP_EVENT, quota.STOP_EVENT)
        self.assertIs(runtime.STOP_EVENT, cli.STOP_EVENT)
        self.assertIs(runtime.FORCE_STOP_EVENT, processes.FORCE_STOP_EVENT)
        self.assertIs(runtime.FORCE_STOP_EVENT, pipeline.FORCE_STOP_EVENT)
        self.assertIs(runtime.PRINT_LOCK, console.PRINT_LOCK)

    # Check that the quota wait respects reset information and responds to cancellation.
    def test_quota_wait_uses_reset_and_can_be_interrupted(self):
        now = datetime(2026, 11, 15, 17, 9, 59, tzinfo=timezone.utc)
        self.assertEqual(quota.quota_wait_seconds("Resets in 2h11m10s", now), 7890)
        clock = SimpleNamespace(value=100.0)

        # Advance the fake clock and detect an unexpectedly long polling interval.
        def sleep(seconds):
            self.assertLessEqual(seconds, 15)
            clock.value += seconds

        fake_time = SimpleNamespace(monotonic=lambda: clock.value)
        for stop_after, expected in ((None, 7990), (115, 115)):
            clock.value = 100

            # Expose cancellation once the fake clock reaches the selected time.
            def is_set():
                return stop_after is not None and clock.value >= stop_after

            # Advance simulated time and report whether cancellation occurred.
            def event_wait(seconds):
                sleep(seconds)
                return is_set()

            event = SimpleNamespace(is_set=is_set, wait=event_wait)
            with mock.patch.object(quota, "time", fake_time), \
                    mock.patch.object(quota, "STOP_EVENT", event), \
                    mock.patch.object(quota, "_LAST_QUOTA_WAIT_DONE", [0.0]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                if stop_after is None:
                    quota.wait_for_quota_reset("Resets in 2h11m10s")
                else:
                    with self.assertRaises(InterruptedError):
                        quota.wait_for_quota_reset("Resets in 2h11m10s")
                self.assertEqual(clock.value, expected)
                self.assertIn("Жду 7890с (до ", output.getvalue())

    # Check subprocess envelope handling using a fake agent reply.
    def test_agy_subprocess_envelope_without_starting_agy(self):
        payload = envelope(REPLY)[1]
        result = SimpleNamespace(stdout=payload, stderr="warning", returncode=0)
        with mock.patch.object(agy, "ensure_agy_home", return_value=None), \
                mock.patch.object(agy, "run_capture", return_value=result) as run:
            rc, out, err, home = agy.run_agy("prompt", "model-test", 10, False)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "model-test")
        self.assertEqual(run.call_args.kwargs["input"], "prompt")
        error, text, usage, conv = agy.extract_envelope_error_and_usage(rc, out, err)
        self.assertIsNone(error)
        self.assertEqual((text, conv), (REPLY, "test-conversation"))
        self.assertEqual(usage["input_tokens"], 10)

    # Check that an agent profile is installed even when the shared token file is absent.
    def test_agy_home_without_token_file_installs_reader_profile(self):
        # macOS keeps the login in Keychain, so the token file may be absent.
        with tempfile.TemporaryDirectory() as real, tempfile.TemporaryDirectory() as tmp:
            Path(real, "settings.json").write_text("{}")
            with mock.patch.object(agy, "AGY_REAL", real), \
                    mock.patch.object(agy.tempfile, "gettempdir", return_value=tmp):
                home = agy.ensure_agy_home(True)
            self.assertIsNotNone(home)
            agent = Path(home, ".gemini", "config", "agents", "audio-reader", "agent.md")
            self.assertTrue(agent.is_file())
            cli = Path(home, ".gemini", "antigravity-cli")
            self.assertEqual((cli / "settings.json").resolve(),
                             Path(real, "settings.json").resolve())
            self.assertFalse((cli / "antigravity-oauth-token").exists())

    # Check that the agent command uses the installed reader profile without a token file.
    def test_run_agy_uses_reader_profile_without_token_file(self):
        with tempfile.TemporaryDirectory() as real, tempfile.TemporaryDirectory() as tmp:
            Path(real, "settings.json").write_text("{}")
            payload = envelope(REPLY)[1]
            result = SimpleNamespace(stdout=payload, stderr="", returncode=0)
            with mock.patch.object(agy, "AGY_REAL", real), \
                    mock.patch.object(agy.tempfile, "gettempdir", return_value=tmp), \
                    mock.patch.object(agy, "run_capture", return_value=result) as run:
                _, _, _, home = agy.run_agy("prompt", "model-test", 10, True)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--agent") + 1], "audio-reader")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(run.call_args.kwargs["env"]["HOME"], home)


if __name__ == "__main__":
    unittest.main()
