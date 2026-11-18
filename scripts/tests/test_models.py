"""Shared 503 cooldowns, recovery, cancellation and integration with all request paths."""

from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts.diarizer import models, pipeline, runtime
from scripts.diarizer.diagnostics import run_log
from scripts.tests.test_diarizer import REPLY, TemporaryArchive, envelope


ERROR_503 = (3, json.dumps({'status': 'ERROR', 'error': 'UNAVAILABLE (code 503): No capacity'}), '', None)


class ModelCooldownTests(unittest.TestCase):
    def setUp(self):
        runtime.STOP_EVENT.clear()
        self.addCleanup(runtime.STOP_EVENT.clear)
        self.now = 100.0
        clock = mock.patch.object(models.time, 'monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.registry = models.ModelCooldowns()

    def advance(self, seconds):
        self.now += seconds
        return False

    def test_503_pauses_only_that_model_for_300_seconds_then_primary_returns(self):
        self.registry.block('primary')
        self.assertEqual(self.registry.remaining('primary'), 300)
        self.assertEqual(self.registry.select(['primary', 'fallback']), 1)
        self.assertEqual(self.registry.remaining('fallback'), 0)
        self.now += 299
        self.assertEqual(self.registry.select(['primary', 'fallback']), 1)
        self.now += 1
        self.assertEqual(self.registry.select(['primary', 'fallback']), 0)

    def test_repeated_503_extends_pause_from_latest_failure(self):
        self.registry.block('primary')
        self.now += 60
        self.registry.block('primary')
        self.assertEqual(self.registry.remaining('primary'), 300)

    def test_all_models_wait_for_earliest_expiry_without_real_sleep(self):
        with mock.patch.dict(models.MODEL_ERROR_POLICIES['503'], {'cooldown_s': 5}):
            self.registry.block('first')
            self.now += 2
            self.registry.block('second')
        with mock.patch.object(runtime.STOP_EVENT, 'wait', side_effect=self.advance) as wait:
            selected = self.registry.select(['first', 'second'], preferred=1)
        self.assertEqual(selected, 0)
        self.assertEqual(self.now, 105)
        self.assertEqual(wait.call_count, 3)
        self.assertEqual(self.registry.remaining('second'), 2)

    def test_single_model_recovers_and_content_rejection_stays_local(self):
        with mock.patch.dict(models.MODEL_ERROR_POLICIES['503'], {'cooldown_s': 2}):
            self.registry.block('only')
        with mock.patch.object(runtime.STOP_EVENT, 'wait', side_effect=self.advance):
            self.assertEqual(self.registry.select(['only']), 0)
        self.assertEqual(self.registry.select(['a', 'b'], excluded={0}), 1)
        self.assertEqual(self.registry.select(['a', 'b']), 0)

    def test_wait_is_cancelled_without_marking_the_audio_as_failed(self):
        self.registry.block('only')
        with mock.patch.object(runtime.STOP_EVENT, 'wait', return_value=True):
            with self.assertRaises(InterruptedError):
                self.registry.select(['only'])

    def test_workers_share_one_registry(self):
        blocked = threading.Event()

        def first():
            self.registry.block('primary')
            blocked.set()

        def second():
            self.assertTrue(blocked.wait(timeout=2))
            return self.registry.select(['primary', 'fallback'])

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first)
            b = pool.submit(second)
            a.result(timeout=2)
            self.assertEqual(b.result(timeout=2), 1)

    def test_diagnostics_include_reason_files_and_recovery_time(self):
        items = [{'audio_rel': 'Phone/test.m4a'}]
        with tempfile.TemporaryDirectory(prefix='cooldown-log-test-') as tmp:
            with run_log(tmp) as path:
                self.registry.block('primary', items=items)
                self.registry.select(['primary', 'fallback'], items=items)
            events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([e['event'] for e in events], ['model_cooldown', 'model_cooldown_skipped'])
        self.assertEqual(events[0]['reason'], '503')
        self.assertEqual(events[0]['seconds'], 300)
        self.assertIn('resume_at', events[0])
        self.assertEqual(events[0]['files'], ['Phone/test.m4a'])
        self.assertEqual(events[1]['model'], 'fallback')


class ModelPipelineTests(TemporaryArchive):
    def setUp(self):
        super().setUp()
        runtime.STOP_EVENT.clear()
        self.addCleanup(runtime.STOP_EVENT.clear)
        self.args = SimpleNamespace(model='first', model_chain=['first', 'second', 'third'],
                                    timeout=10, no_custom_agent=False, stop_on_quota=False,
                                    fallback_on_quota=False)
        self.stats = pipeline.Stats()
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        forget = mock.patch.object(pipeline, 'agy_forget')
        forget.start()
        self.addCleanup(forget.stop)

    def test_next_job_skips_model_that_returned_503_for_previous_job(self):
        with mock.patch.object(pipeline, 'run_agy', side_effect=[ERROR_503, envelope(REPLY), envelope(REPLY)]) as run:
            pipeline.process_short_batch([self.item('one.m4a')], self.args, {}, {}, self.stats)
            pipeline.process_short_batch([self.item('two.m4a')], self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'second', 'second'])
        self.assertEqual(self.stats.done, 2)
        self.assertEqual(self.stats.errors, 0)

    def test_long_recording_switches_session_when_another_worker_pauses_its_model(self):
        item = self.item('long.m4a', duration=20000)
        item['parts'] = [
            {'part': 1, 'path': 'part1.wav', 'duration_ms': 10000, 'offset_ms': 0},
            {'part': 2, 'path': 'part2.wav', 'duration_ms': 10000, 'offset_ms': 10000},
        ]

        def reply(*args, **kwargs):
            if args[1] == 'first':
                self.stats.models.block('first')  # Another in-flight request failed.
            return envelope(REPLY)

        with mock.patch.object(pipeline, 'run_agy', side_effect=reply) as run:
            pipeline.process_long_item(item, self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'second'])
        self.assertIsNone(run.call_args_list[1].kwargs['conversation_id'])
        self.assertEqual(self.stats.done, 1)

    def test_repair_503_also_pauses_model_for_subsequent_jobs(self):
        late = REPLY.replace('00:00:01.00', '00:00:08.00')
        with mock.patch.object(pipeline, 'run_agy', side_effect=[envelope(late), ERROR_503, envelope(REPLY), envelope(REPLY)]) as run:
            pipeline.process_short_batch([self.item('repair.m4a')], self.args, {}, {}, self.stats)
            pipeline.process_short_batch([self.item('next.m4a')], self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'first', 'second', 'second'])
        self.assertEqual(self.stats.done, 2)

    def test_repair_does_not_call_a_model_paused_by_another_worker(self):
        late = REPLY.replace('00:00:01.00', '00:00:08.00')

        def reply(*args, **kwargs):
            if args[1] == 'first':
                self.stats.models.block('first')
                return envelope(late)
            return envelope(REPLY)

        with mock.patch.object(pipeline, 'run_agy', side_effect=reply) as run:
            pipeline.process_short_batch([self.item('repair.m4a')], self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'second'])
        self.assertEqual(self.stats.done, 1)

    def test_all_paused_models_recover_without_retrying_content_rejection(self):
        clock = [100.0]

        def advance(seconds):
            clock[0] += seconds
            return False

        with mock.patch.object(models.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.dict(models.MODEL_ERROR_POLICIES['503'], {'cooldown_s': 3}), \
                mock.patch.object(runtime.STOP_EVENT, 'wait', side_effect=advance), \
                mock.patch.object(pipeline, 'run_agy', side_effect=[
                    ERROR_503, envelope('blocked by content safety filters', 'ERROR'),
                    ERROR_503, envelope(REPLY),
                ]) as run:
            pipeline.process_short_batch([self.item('recover.m4a')], self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'second', 'third', 'first'])
        self.assertEqual(self.stats.done, 1)
        self.assertEqual(self.stats.errors, 0)

    def test_refusal_reports_models_actually_tried_when_primary_was_initially_paused(self):
        clock = [100.0]

        def advance(seconds):
            clock[0] += seconds
            return False

        with mock.patch.object(models.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.dict(models.MODEL_ERROR_POLICIES['503'], {'cooldown_s': 2}), \
                mock.patch.object(runtime.STOP_EVENT, 'wait', side_effect=advance), \
                mock.patch.object(pipeline, 'run_agy', return_value=envelope('blocked by content safety filters', 'ERROR')) as run:
            self.stats.models.block('first')
            with self.assertRaises(pipeline.ModelRefused) as raised:
                pipeline.process_short_batch([self.item('refused.m4a')], self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['second', 'third', 'first'])
        self.assertEqual(raised.exception.models_tried, ['second', 'third', 'first'])


if __name__ == '__main__':
    unittest.main()
