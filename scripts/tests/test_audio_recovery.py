"""Strict AAC recovery and provider media rejection, using synthetic audio only."""

import contextlib
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts.diarizer import archive, audio, checkpoints, pipeline, runtime
from scripts.tests.test_diarizer import REPLY, TemporaryArchive, envelope
from scripts.tests.test_error_policies import failure


MEDIA_ERROR = ('INVALID_ARGUMENT (code 400): Unable to process input: '
               'the file or media data is empty, corrupted, or malformed.')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe required')
class StrictAudioTests(TemporaryArchive):
    def generate_aac(self):
        reference = self.item('reference.m4a')
        p = audio.run_capture([
            'ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
            'sine=frequency=440:sample_rate=44100:duration=0.5', '-ac', '2',
            '-c:a', 'aac', '-b:a', '128k', reference['audio_abs'],
        ])
        self.assertEqual(p.returncode, 0, p.stderr)
        p = audio.run_capture([
            'ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_packets',
            '-show_entries', 'packet=pos,size', '-of', 'json', reference['audio_abs'],
        ])
        packet = json.loads(p.stdout)['packets'][0]
        data = Path(reference['audio_abs']).read_bytes()
        pos, size = int(packet['pos']), int(packet['size'])
        first_frame = data[pos:pos + size]
        return reference, data, pos, size, first_frame

    def interrupted(self, data):
        offset = 0
        while data[offset + 4:offset + 8] != b'moov':
            size = int.from_bytes(data[offset:offset + 4], 'big')
            self.assertGreaterEqual(size, 8)
            offset += size
            self.assertLess(offset, len(data))
        item = self.item('interrupted.tmp')
        Path(item['audio_abs']).write_bytes(data[:offset])
        return item

    def test_first_frame_with_decoder_delay_is_preserved_and_decodes(self):
        if not audio._get_libfaad():
            self.skipTest('FAAD2 required')
        _, data, _, _, first_frame = self.generate_aac()
        item = self.interrupted(data)
        digest = archive.sha256_file(item['audio_abs'])
        work = self.root / 'recover'
        work.mkdir()
        ready, error = audio.prepare_audio_work(item, str(work), 20, 10, 0)
        self.assertIsNone(error)
        adts = (work / 'faad_recovered.aac').read_bytes()
        first_length = ((adts[3] & 3) << 11) | (adts[4] << 3) | (adts[5] >> 5)
        self.assertEqual(adts[7:first_length], first_frame)
        self.assertIsNone(audio.decode_audio_error(ready['clean_abs']))
        self.assertAlmostEqual(ready['duration_ms'], 500, delta=100)
        self.assertEqual(archive.sha256_file(item['audio_abs']), digest)

    def test_rejected_faad_output_continues_to_untrunc(self):
        if not audio._get_libfaad():
            self.skipTest('FAAD2 required')
        reference, data, *_ = self.generate_aac()
        item = self.interrupted(data)
        work = self.root / 'recover'
        work.mkdir()
        decode = audio.decode_audio_error

        def validate(path):
            if Path(path).name == 'faad_recovered.m4a':
                return 'audio decode failed: damaged AAC'
            return decode(path)

        with mock.patch.object(audio, 'decode_audio_error', side_effect=validate), \
                mock.patch.object(audio, 'untrunc_recover', return_value=(reference['audio_abs'], 500)) as recover:
            ready, error = audio.prepare_audio_work(item, str(work), 20, 10, 0, untrunc_bin='untrunc')
        recover.assert_called_once()
        self.assertIsNone(error)
        self.assertIsNone(decode(ready['clean_abs']))

    def test_readable_container_with_bad_aac_is_not_sent_to_model(self):
        _, data, pos, size, _ = self.generate_aac()
        item = self.item('corrupted.m4a')
        bad_pos = pos + size  # Keep the first frame and codec probing intact.
        Path(item['audio_abs']).write_bytes(data[:bad_pos] + b'\xff' * 16 + data[bad_pos + 16:])
        self.assertIsNone(audio.probe_duration_ms(item['audio_abs'])[1])
        self.assertIn('audio decode failed', audio.decode_audio_error(item['audio_abs']))
        args = SimpleNamespace(max_single_sec=20, chunk_sec=10, overlap_sec=0,
                               use_faad=False, untrunc_bin=None)
        stats = pipeline.Stats()
        with mock.patch.object(pipeline, 'run_agy') as run, contextlib.redirect_stdout(io.StringIO()):
            pipeline.run_job([item], args, stats)
        run.assert_not_called()
        state = archive.load_manifest_state()[item['audio_rel']]
        self.assertEqual(state['status'], 'needs_recovery')
        self.assertIn('audio decode failed', state['reason'])
        self.assertEqual(state['sha256'], archive.sha256_file(item['audio_abs']))
        self.assertEqual(stats.needs_recovery, 1)

    def test_untrunc_output_requires_decodable_audio(self):
        _, data, pos, size, _ = self.generate_aac()
        item = self.interrupted(data)
        broken = data[:pos] + b'\xff' * size + data[pos + size:]
        work = self.root / 'recover'
        work.mkdir()
        capture = audio.run_capture

        def run(cmd, **kwargs):
            if cmd[0] == 'test-untrunc':
                Path(cmd[cmd.index('-dst') + 1]).write_bytes(broken)
                return None
            return capture(cmd, **kwargs)

        with mock.patch.object(audio, 'run_capture', side_effect=run):
            self.assertEqual(audio.untrunc_recover(item['audio_abs'], str(work), 'test-untrunc'), (None, None))

    def test_cancelled_decode_does_not_mark_source_as_damaged(self):
        reference, *_ = self.generate_aac()
        args = SimpleNamespace(max_single_sec=20, chunk_sec=10, overlap_sec=0,
                               use_faad=False, untrunc_bin=None)
        with mock.patch.object(audio, 'decode_audio_error', side_effect=InterruptedError), \
                mock.patch.object(pipeline, 'run_agy') as run:
            pipeline.run_job([reference], args, pipeline.Stats())
        run.assert_not_called()
        self.assertFalse(self.manifest.exists())


class MediaRejectionTests(TemporaryArchive):
    def setUp(self):
        super().setUp()
        self.args = SimpleNamespace(model='first', model_chain=['first', 'second', 'third'],
                                    timeout=10, no_custom_agent=False, stop_on_quota=False,
                                    max_single_sec=780, chunk_sec=600, overlap_sec=15)
        self.stats = pipeline.Stats()
        runtime.STOP_EVENT.clear()
        self.addCleanup(runtime.STOP_EVENT.clear)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def run_job(self, items, responses):
        with mock.patch.object(pipeline, 'prepare_audio_work', side_effect=[(it, None) for it in items]), \
                mock.patch.object(pipeline, 'run_agy', side_effect=responses) as run:
            pipeline.run_job(items, self.args, self.stats)
        return run

    def test_single_media_error_marks_recovery_without_model_fallback(self):
        bad = dict(self.item('bad.m4a'), parts=None)
        rejected = (*failure(MEDIA_ERROR)[:3], '/tmp/isolated-agy-test')
        with mock.patch.object(pipeline, 'agy_forget') as forget:
            run = self.run_job([bad], [rejected])
        self.assertEqual(run.call_count, 1)
        forget.assert_any_call('/tmp/isolated-agy-test', 'test-conversation')
        self.assertEqual(self.stats.needs_recovery, 1)
        self.assertEqual(self.stats.errors, 0)
        self.assertTrue(all(self.stats.models.remaining(m) == 0 for m in self.args.model_chain))
        state = archive.load_manifest_state()[bad['audio_rel']]
        self.assertEqual(state['status'], 'needs_recovery')
        self.assertEqual(state['sha256'], bad['sha256'])
        self.assertFalse(Path(bad['target_md']).exists())
        self.assertNotIn(bad['audio_rel'], {it['audio_rel'] for it in archive.scan_pending()})
        self.assertIn(bad['audio_rel'], {it['audio_rel'] for it in archive.scan_pending(retry_recovery=True)})
        good = dict(self.item('next.m4a'), parts=None)
        run = self.run_job([good], [envelope(REPLY)])
        self.assertEqual(run.call_args.args[1], 'first')
        self.assertEqual(self.stats.done, 1)

    def test_batch_is_split_and_healthy_file_is_published(self):
        good, bad = [dict(self.item(name), parts=None) for name in ('good.m4a', 'bad.m4a')]
        run = self.run_job([good, bad], [failure(MEDIA_ERROR), envelope(REPLY), failure(MEDIA_ERROR)])
        self.assertEqual([call.args[1] for call in run.call_args_list], ['first'] * 3)
        self.assertEqual(self.stats.done, 1)
        self.assertEqual(self.stats.needs_recovery, 1)
        self.assertTrue(Path(good['target_md']).exists())
        self.assertFalse(Path(bad['target_md']).exists())
        state = archive.load_manifest_state()
        self.assertEqual(state[good['audio_rel']]['status'], 'done')
        self.assertEqual(state[bad['audio_rel']]['status'], 'needs_recovery')

    def test_long_media_error_keeps_checkpoint_without_publishing_partial_md(self):
        item = self.item('long.m4a', duration=10000)
        item['parts'] = [
            {'part': 1, 'path': 'one.wav', 'duration_ms': 5000, 'offset_ms': 0},
            {'part': 2, 'path': 'two.wav', 'duration_ms': 5000, 'offset_ms': 5000},
        ]
        run = self.run_job([item], [envelope(REPLY), failure(MEDIA_ERROR)])
        self.assertEqual([call.args[1] for call in run.call_args_list], ['first', 'first'])
        self.assertEqual(self.stats.done, 0)
        self.assertEqual(self.stats.needs_recovery, 1)
        self.assertFalse(Path(item['target_md']).exists())
        saved = checkpoints.LongCheckpoint(item)
        self.assertIsNotNone(saved.get(1))
        self.assertIsNone(saved.get(2))
