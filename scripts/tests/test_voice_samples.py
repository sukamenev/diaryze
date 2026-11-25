"""Exercise the review workflow with synthetic audio and a mocked provider."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diarizer import archive, cli, config, prompts, runtime, voice_samples


PERSON = "ivanov_aleksey"
PEOPLE = {PERSON: {"name": "Алексей Иванов"}}


# Build a mock voice sample result, including a report for each input.
def sample_reply(candidates, *statuses, summary="Reviewed audio"):
    return {
        "summary": summary,
        "calls": [{"call": number, "read_status": status, "detail": f"Diagnostic {number}."}
                  for number, status in enumerate(statuses, 1)],
        "candidates": candidates,
    }


class VoiceSampleTests(unittest.TestCase):
    # Set up a standalone archive with a known owner and fake model arguments.
    def setUp(self):
        runtime.STOP_EVENT.clear()
        runtime.FORCE_STOP_EVENT.clear()
        temporary = tempfile.TemporaryDirectory(prefix="voice-sample-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patch = mock.patch.object(config, "ROOT", str(self.root))
        patch.start()
        self.addCleanup(patch.stop)
        self.phone = self.root / "Phone"
        self.phone.mkdir()
        (self.phone / ".owner").write_text(f"type: phone\nowner: {PERSON}\n", encoding="utf-8")
        self.args = SimpleNamespace(
            subdir=".", dry_run=False, retry_skipped=False, retry_refused=False, limit=0,
            use_faad=False, untrunc_bin=None, no_faad=True, no_untrunc=True,
            model="test", model_chain=["test"], chunk_sec=90, overlap_sec=10,
            timeout=10, no_custom_agent=False,
        )

    # Place an approved fixture where references are discovered automatically.
    def sample(self, filename, channel="phone"):
        path = self.root / "00_Diaryze_Data" / "voice_samples" / PERSON / channel / "confirmed" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"approved fixture")
        return path

    # Verify date distance on both sides and phone preference only on ties.
    def test_selects_nearest_date_and_new_sample_is_seen(self):
        self.sample("undated_01.flac")
        self.sample("2025-05-01_01.flac")
        later = self.sample("2025-05-12_01.flac", channel="recorder")
        item = {"audio_rel": "Phone/call.m4a", "dt": "2025-05-10T10:00:00"}
        self.assertEqual(voice_samples.owner_reference(item, PEOPLE)["file"], str(later))
        earlier = self.sample("2025-05-08_01.flac")
        self.assertEqual(voice_samples.owner_reference(item, PEOPLE)["file"], str(earlier))
        newest = self.sample("2025-05-11_01.flac", channel="recorder")
        self.assertEqual(voice_samples.owner_reference(item, PEOPLE)["file"], str(newest))

    # Verify metadata date fallback and nested recorder overrides.
    def test_embedded_date_and_owner_override(self):
        self.sample("2024-01-01_01.flac")
        expected = self.sample("2020-02-20_01.flac")
        item = {"audio_rel": "Phone/call.m4a", "audio_abs": "/unused/call.m4a"}
        ffprobe = SimpleNamespace(returncode=0, stdout='{"format":{"tags":{"creation_time":"2020-02-19T21:15:00Z"}}}')
        with mock.patch.object(voice_samples, "run_capture", return_value=ffprobe) as probe:
            self.assertEqual(voice_samples.owner_reference(item, PEOPLE)["file"], str(expected))
            probe.assert_called_once()
        override = self.phone / "Recorder"
        override.mkdir()
        (override / ".owner").write_text("type: recorder\n", encoding="utf-8")
        self.assertIsNone(voice_samples.owner_reference(dict(item, audio_rel="Phone/Recorder/a.wav"), PEOPLE))

    # Check that references travel with both short files and long recording parts.
    def test_prompts_include_only_the_relevant_approved_reference(self):
        reference = self.sample("2025-05-12_01.flac")
        item = {"audio_rel": "Phone/call.m4a", "dt": "2025-05-10",
                "direction": "in", "clean_abs": "/tmp/main.wav", "duration_ms": 20_000}
        other = dict(item, audio_rel="Recorder/other.wav")
        batch = prompts.build_batch_prompt([item, other], PEOPLE, {})
        self.assertEqual(batch.count(str(reference)), 1)
        self.assertLess(batch.index(str(reference)), batch.index("ITEM 2:"))
        part = {"part": 1, "path": "/tmp/part.wav", "duration_ms": 12_000}
        first = prompts.build_part_prompt(item, part, PEOPLE, {}, True)
        later = prompts.build_part_prompt(item, dict(part, part=2), PEOPLE, {}, False, fresh_session=True)
        for prompt in (first, later):
            self.assertIn(str(reference), prompt)
            self.assertIn("Exclude the reference's speech", prompt)

    # Verify that overlaps and out-of-bounds model replies are rejected before ffmpeg.
    def test_rejects_invalid_candidate_intervals(self):
        calls = [{"audio_rel": path, "dt": "2025-05-10", "sha256": "x", "duration_ms": 40_000}
                 for path in ("Phone/a.wav", "Phone/b.wav")]
        result = {"call": 1, "start_ms": 0, "end_ms": 12_000, "evidence": "matched",
                  "supporting_calls": [2]}
        for invalid in (dict(result, start_ms=-1), dict(result, end_ms=41_000),
                        dict(result, supporting_calls=[1]), dict(result, call=True)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                voice_samples._parse_sample_reply(json.dumps(sample_reply([invalid], "read", "read")), calls)
        with self.assertRaisesRegex(ValueError, "overlap"):
            reply = sample_reply([result, dict(result, start_ms=100)], "read", "read")
            voice_samples._parse_sample_reply(json.dumps(reply), calls)
        with self.assertRaisesRegex(ValueError, "without readable audio"):
            voice_samples._parse_sample_reply(json.dumps(sample_reply([result], "read", "unreadable")), calls)

    # Reject old empty arrays, absent responses, and incomplete per-call diagnostics.
    def test_requires_a_diagnostic_report_when_no_clip_qualifies(self):
        calls = [{"audio_rel": "Phone/1.wav"}, {"audio_rel": "Phone/2.wav"}]
        missing_call = json.dumps(sample_reply([], "read"))
        for reply, reason in (("", "empty voice sample response"), ("[]", "Expected a voice sample report"),
                              (missing_call, "every source call")):
            with self.subTest(reply=reply), self.assertRaisesRegex(ValueError, reason):
                voice_samples._parse_sample_reply(reply, calls)

    # Report and record the model's per-call reasons even when the candidate list is empty.
    def test_standalone_preparation_logs_no_candidate_reply(self):
        calls = [{"audio_rel": f"Phone/{number}.wav", "clean_abs": f"/tmp/{number}.wav",
                  "direction": "in", "duration_ms": 30_000} for number in (1, 2)]
        reply = sample_reply([], "read", "unreadable", summary="The second call was blocked by a safety filter.")
        reply["calls"][1]["detail"] = "read_file reported access denied."
        response = json.dumps(reply)
        result = (0, json.dumps({"status": "SUCCESS", "result": response}), "", None)
        flags = ["diarize.py", "--root", str(self.root), "--folder", "Phone", "--prepare-voice-sample"]
        with (mock.patch("sys.argv", flags),
              mock.patch.object(cli, "load_refs", return_value=(PEOPLE, {})),
              mock.patch.object(voice_samples, "_prepare_calls", return_value=calls),
              mock.patch.object(voice_samples, "run_agy", return_value=result) as provider,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(cli.main(), 1)
        provider.assert_called_once()
        self.assertIn("/tmp/2.wav", provider.call_args.args[0])
        self.assertIn(reply["summary"], output.getvalue())
        self.assertIn("Call 2 (audio unavailable) Phone/2.wav: read_file reported access denied.", output.getvalue())
        log, = config.data_path("logs").glob("diarize-*.jsonl")
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        request = next(row for row in rows if row["event"] == "voice_sample_request")
        self.assertEqual(request["files"], ["Phone/1.wav", "Phone/2.wav"])
        self.assertEqual(request["calls"][1]["file"], "/tmp/2.wav")
        recorded = next(row for row in rows if row["event"] == "voice_sample_result")
        self.assertEqual(recorded["candidate_count"], 0)
        self.assertEqual(recorded["call_diagnostics"], reply["calls"])
        self.assertEqual(recorded["response"], response)

    # Check that nested .owner files get independent review sets even for the same person.
    def test_preflight_groups_by_nearest_owner_file(self):
        second = self.phone / "OtherPhone"
        second.mkdir()
        shutil.copyfile(self.phone / ".owner", second / ".owner")
        items = [{"audio_rel": path} for path in
                 ("Phone/2025/a.wav", "Phone/2024/b.wav", "Phone/OtherPhone/2025/a.wav")]
        with (mock.patch.object(cli, "load_refs", return_value=(PEOPLE, {})),
              mock.patch.object(cli, "prepare_voice_samples", return_value=[]) as prepare,
              mock.patch.object(cli, "emit")):
            self.assertTrue(cli._voice_sample_preflight(items, self.args))
        self.assertEqual([call.args[1] for call in prepare.call_args_list], [self.phone, second])

    # Resolve a standalone preparation request to the inherited folder owner.
    def test_prepare_without_person_uses_nearest_owner(self):
        scope = self.phone / "2021"
        scope.mkdir()
        argv = ["diarize.py", "--root", str(self.root), "--folder", "Phone/2021", "--prepare-voice-sample"]
        with (mock.patch("sys.argv", argv),
              mock.patch.object(cli, "load_refs", return_value=(PEOPLE, {})),
              mock.patch.object(cli, "prepare_voice_samples", return_value=[scope / "fixture.flac"]) as prepare,
              mock.patch.object(cli, "emit"),
              mock.patch.object(cli, "find_untrunc", return_value=None),
              mock.patch.object(cli, "_get_libfaad", return_value=None)):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(prepare.call_args.args[:2], (PERSON, str(scope)))

    # Exercise real cutting, file-manager selections, and storage independent of the source.
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    def test_stages_three_then_uses_only_the_moved_clips(self):
        archive.init_catalogs()
        people_file, _ = archive.catalog_paths()
        Path(people_file).write_text(f'people:\n  {PERSON}:\n    name: Алексей\n', encoding="utf-8")
        scope = self.phone / "2021"
        scope.mkdir()
        self.args.subdir = "Phone/2021"
        sources = []
        for index, direction in enumerate(("incoming", "outgoing", "incoming"), 1):
            path = scope / f"Contact{index}_+7000000000{index}_2021-05-1{index}_10-00-00_{direction}.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            f"sine=frequency={400 + 25 * index}:duration=18", str(path)], check=True)
            sources.append(path)
        outside = self.phone / "Other_+70000000004_2021-05-13_10-00-00_outgoing.wav"
        shutil.copyfile(sources[0], outside)
        answer = [
            {"call": n, "start_ms": 1000, "end_ms": 12_000, "evidence": "fixture",
             "supporting_calls": [2 if n == 1 else 1]} for n in range(1, 4)
        ]
        reply = sample_reply(answer, "read", "read", "read")
        result = (0, json.dumps({"status": "SUCCESS", "result": json.dumps(reply)}), "", None)

        with (mock.patch.object(voice_samples, "run_agy", return_value=result) as provider,
              mock.patch.object(voice_samples, "emit"),
              mock.patch.object(cli, "run_job") as transcribe,
              mock.patch.object(cli, "emit"),
              mock.patch.object(cli, "record")):
            self.assertIsNone(cli.run_workers(self.args))
            self.assertIsNone(cli.run_workers(self.args))
            provider.assert_called_once()
            transcribe.assert_not_called()
        candidates = voice_samples.pending_voice_samples(PERSON, scope)
        self.assertEqual(len(candidates), 3)
        item = archive.scan_pending("Phone/2021")[0]
        self.assertIsNone(voice_samples.owner_reference(item, PEOPLE))
        pending = self.root / "00_Diaryze_Data/voice_samples" / PERSON / "phone/candidate"
        confirmed = pending.with_name("confirmed")
        self.assertTrue(all(path.parent == pending for path in candidates))
        provenance = {path.name: json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                      for path in candidates}
        self.assertTrue(all(meta["source"]["audio"].startswith("Phone/2021/") for meta in provenance.values()))
        metadata = provenance[candidates[1].name]
        original = self.root / metadata["source"]["audio"]
        self.assertEqual(metadata["source"]["sha256"], archive.sha256_file(original))
        original.unlink()
        for candidate in candidates[1:]:
            candidate.rename(confirmed / candidate.name)
        self.assertIsNone(voice_samples.owner_reference(item, PEOPLE))
        with (mock.patch.object(voice_samples, "run_agy") as provider,
              mock.patch.object(cli, "run_job") as transcribe,
              mock.patch.object(cli, "pack_next_job", side_effect=[[item], None]),
              mock.patch.object(cli, "scan_pending", return_value=[item]),
              mock.patch.object(cli, "emit"), mock.patch.object(cli, "record")):
            self.args.workers = 1
            self.args.limit = 1
            self.args.batch_short_max = 1
            self.args.batch_short_sec = 60
            self.args.max_single_sec = 120
            cli.run_workers(self.args)
            provider.assert_not_called()
            transcribe.assert_called_once()
            self.assertIsNotNone(voice_samples.owner_reference(item, PEOPLE))
        stored = list(confirmed.glob("*.flac"))
        self.assertEqual({archive.sha256_file(path) for path in stored},
                         {provenance[candidate.name]["sha256"] for candidate in candidates[1:]})
        self.assertTrue(all(path.with_suffix(".json").is_file() for path in stored))
        self.assertEqual(list(pending.iterdir()), [])
        self.assertTrue(all(path.exists() for path in sources if path != original))

    # Confirm one candidate without removing candidates from a different source folder.
    def test_approves_candidate_without_affecting_other_reviews(self):
        folder = self.root / "00_Diaryze_Data/voice_samples" / PERSON / "phone/candidate"
        folder.mkdir(parents=True)
        clip = folder / "2025-05-10_01.flac"
        other = folder / "2025-05-10_02.flac"
        for path, scope in ((clip, "Phone/2025"), (other, "Phone")):
            path.write_bytes(b"candidate fixture")
            path.with_suffix(".json").write_text(json.dumps({
                "recorded_on": "2025-05-10", "channel": "phone", "person": PERSON,
                "sha256": archive.sha256_file(path), "scope": scope,
            }), encoding="utf-8")
        approved = voice_samples.review_voice_sample(clip, approve=True)
        self.assertEqual(approved, folder.with_name("confirmed") / clip.name)
        self.assertEqual(approved.read_bytes(), b"candidate fixture")
        self.assertFalse(clip.exists())
        self.assertEqual(voice_samples.pending_voice_samples(PERSON, self.phone), [other])
        with mock.patch.object(cli, "load_refs", return_value=(PEOPLE, {})):
            self.assertFalse(cli._voice_sample_preflight([{"audio_rel": "Phone/call.wav"}], self.args))
