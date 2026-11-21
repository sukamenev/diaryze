"""Provider classification, policy scopes and fallback through real pipeline paths."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
import errno
import io
import json
import math
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

from scripts.diarizer import agy, cli, diagnostics, models, pipeline, quota, runtime
from scripts.diarizer.errors import ModelRefused, ModelsUnavailable, ModelTaskFailed, classify_error
from scripts.tests.test_diarizer import REPLY, TemporaryArchive, envelope


def failure(message):
    return envelope(message, 'ERROR')


class ErrorClassificationTests(unittest.TestCase):
    def test_provider_codes_messages_and_exceptions_have_distinct_scopes(self):
        cases = [
            ('UNAVAILABLE (code 503): No capacity', '503'),
            ('Our servers are overloaded', '503'),
            ('HTTP 500 Internal Server Error', 'server'),
            ('HTTP/1.1 502 Bad Gateway', 'server'),
            ('{"error_code": 504, "message": "timeout"}', 'server'),
            ('INTERNAL (code 500)', 'server'),
            ('ECONNRESET: socket closed', 'connection'),
            ('stream was interrupted', 'connection'),
            (ConnectionResetError('reset'), 'connection'),
            (OSError(errno.ENETUNREACH, 'network unreachable'), 'connection'),
            (TimeoutError('agy timeout'), 'timeout'),
            ('DEADLINE_EXCEEDED', 'timeout'),
            ('HTTP 429 Too many requests', 'quota'),
            ('Quota reached. Resets in 2h11m10s', 'quota'),
            ('invalid_api_key', 'fatal'),
            ('HTTP 401 Unauthorized', 'fatal'),
            ('status=403: account has no access to this model', 'fatal'),
            ('Unknown model: example', 'fatal'),
            ('Model example does not exist', 'fatal'),
            ('HTTP 403: prompt could not be submitted: Prohibited Use Policy', 'content'),
            ('HTTP 403: content policy violation', 'content'),
            ('SAFETY: content safety filters', 'content'),
            ('FORMAT: invalid JSON', 'format'),
            ('RESOURCE_EXHAUSTED: input token limit reached, maximum context length exceeded', 'input'),
            ('HTTP 413: request entity too large', 'input'),
            ('INVALID_ARGUMENT (code 400): Unable to process input: the file or media data is empty, corrupted, or malformed.', 'media'),
            ('MEDIA: rejected audio', 'media'),
            ('INVALID_ARGUMENT (code 400): invalid generation config', 'unknown'),
            ('Something unexplained happened', 'unknown'),
            (PermissionError('permission denied reading /tmp/audio'), 'unknown'),
        ]
        for error, expected in cases:
            with self.subTest(error=str(error)):
                self.assertEqual(classify_error(error), expected)

    def test_successful_transcript_is_not_classified_as_provider_failure(self):
        text = REPLY + ' Обсудили HTTP 503 и неверный ключ API.'
        err, parsed, *_ = agy.extract_envelope_error_and_usage(*envelope(text)[:3])
        self.assertIsNone(err)
        self.assertEqual(parsed, text)

    def test_envelope_classification_keeps_reset_at_end_of_long_error(self):
        payload = 'Quota reached. ' + 'detail ' * 300 + ' Resets in 2h11m10s'
        err, *_ = agy.extract_envelope_error_and_usage(*failure(payload)[:3])
        self.assertEqual(classify_error(err), 'quota')
        self.assertEqual(quota.quota_wait_seconds(err), 7890)

    def test_quota_retry_after_and_default_are_configured(self):
        self.assertEqual(quota.quota_wait_seconds('Retry-After: 60'), 80)
        self.assertEqual(quota.quota_wait_seconds('{"retry_after": 35}'), 55)
        self.assertEqual(quota.quota_wait_seconds('retryDelay: "45s"'), 65)
        with mock.patch.dict(models.MODEL_ERROR_POLICIES['quota'], {'cooldown_s': 77}):
            self.assertEqual(quota.quota_wait_seconds('Quota reached'), 77)


class GeneralModelPolicyTests(unittest.TestCase):
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

    def test_timeout_pause_threshold_and_success_reset(self):
        self.registry.failed('a', TimeoutError('timeout'))
        self.assertEqual(self.registry.remaining('a'), 0)
        self.registry.success('a')
        self.registry.failed('a', TimeoutError('timeout'))
        self.assertEqual(self.registry.remaining('a'), 0)
        self.registry.failed('a', TimeoutError('timeout'))
        self.assertEqual(self.registry.remaining('a'), 1800)
        self.registry.success('a')  # Another request may already be in flight.
        self.assertEqual(self.registry.remaining('a'), 1800)

    def test_timeout_counter_restarts_after_pause_without_a_success(self):
        for access in ('remaining', 'select', 'failed'):
            with self.subTest(access=access):
                registry = models.ModelCooldowns()
                registry.failed('a', TimeoutError('timeout'))
                registry.failed('a', TimeoutError('timeout'))
                self.assertEqual(registry.remaining('a'), 1800)
                self.now += 1799
                self.assertEqual(registry.select(['a', 'b']), 1)
                self.now += 1
                if access == 'remaining':
                    self.assertEqual(registry.remaining('a'), 0)
                elif access == 'select':
                    self.assertEqual(registry.select(['a', 'b']), 0)
                # The next failure starts at 1, even without a preceding availability check.
                registry.failed('a', TimeoutError('timeout'))
                self.assertEqual(registry.remaining('a'), 0)
                registry.failed('a', TimeoutError('timeout'))
                self.assertEqual(registry.remaining('a'), 1800)

    def test_two_timeouts_from_different_workers_pause_the_same_model(self):
        barrier = threading.Barrier(2)

        def fail():
            barrier.wait(timeout=2)
            self.registry.failed('a', TimeoutError('timeout'))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(fail) for _ in range(2)]
            for future in futures:
                future.result(timeout=2)
        self.assertEqual(self.registry.remaining('a'), 1800)
        self.assertEqual(self.registry.select(['a', 'b']), 1)

    def test_policy_threshold_and_interval_can_be_changed_in_config(self):
        with mock.patch.dict(models.MODEL_ERROR_POLICIES['timeout'], {'threshold': 3, 'cooldown_s': 9}):
            for _ in range(2):
                self.registry.failed('a', TimeoutError('timeout'))
                self.assertEqual(self.registry.remaining('a'), 0)
            self.registry.failed('a', TimeoutError('timeout'))
            self.assertEqual(self.registry.remaining('a'), 9)

    def test_quota_pause_uses_provider_reset_and_is_shared(self):
        self.registry.failed('a', 'QUOTA: Resets in 2h11m10s')
        self.assertEqual(self.registry.remaining('a'), 7890)
        self.assertEqual(self.registry.select(['a', 'b']), 1)
        self.now += 7890
        self.assertEqual(self.registry.select(['a', 'b']), 0)

    def test_all_fatally_disabled_models_fail_immediately_without_wait(self):
        for name in ('a', 'b'):
            self.registry.failed(name, 'Unknown model')
            self.assertTrue(math.isinf(self.registry.remaining(name)))
        with mock.patch.object(runtime.STOP_EVENT, 'wait', side_effect=AssertionError('must not wait')):
            with self.assertRaises(ModelsUnavailable) as raised:
                self.registry.select(['a', 'b'])
        self.assertTrue(raised.exception.all_disabled)

    def test_disabled_plus_content_rejection_is_local_to_task(self):
        self.registry.failed('a', 'Invalid API key')
        attempt = models.ModelAttempts(['a', 'b'], self.registry, [])
        attempt.started(1)
        with self.assertRaises(ModelRefused):
            attempt.failed(1, 'SAFETY: content safety filters')
        self.assertEqual(self.registry.select(['a', 'b']), 1)

    def test_policy_diagnostics_distinguish_threshold_fallback_from_pause(self):
        with tempfile.TemporaryDirectory(prefix='policy-log-test-') as tmp:
            with diagnostics.run_log(tmp) as path:
                self.registry.failed('a', TimeoutError('timeout'), items=[{'audio_rel': 'call.m4a'}])
                self.registry.failed('a', TimeoutError('timeout'), items=[{'audio_rel': 'call.m4a'}])
            events = [json.loads(line) for line in path.read_text().splitlines()]
        decisions = [e for e in events if e['event'] == 'model_error_policy']
        self.assertEqual([e['action'] for e in decisions], ['fallback_task', 'cooldown'])
        self.assertEqual([e['failure_count'] for e in decisions], [1, 2])
        self.assertEqual(events[-1]['seconds'], 1800)
        self.assertEqual(events[-1]['files'], ['call.m4a'])


class GeneralPipelinePolicyTests(TemporaryArchive):
    def setUp(self):
        super().setUp()
        runtime.STOP_EVENT.clear()
        self.addCleanup(runtime.STOP_EVENT.clear)
        self.args = SimpleNamespace(model='first', model_chain=['first', 'second', 'third'],
                                    timeout=10, no_custom_agent=False, stop_on_quota=False,
                                    fallback_on_quota=False, max_single_sec=780, chunk_sec=600,
                                    overlap_sec=15, use_faad=False, untrunc_bin=None)
        self.stats = pipeline.Stats()
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        forget = mock.patch.object(pipeline, 'agy_forget')
        forget.start()
        self.addCleanup(forget.stop)

    def test_server_connection_and_access_errors_use_next_model_and_skip_future_calls(self):
        for error in (failure('HTTP 500 Internal'), failure('HTTP 502 Bad Gateway'),
                      failure('HTTP 504 Gateway timeout'), ConnectionResetError('reset'),
                      failure('Unknown model'), failure('HTTP 401 Unauthorized')):
            with self.subTest(error=error):
                stats = pipeline.Stats()
                with mock.patch.object(pipeline, 'run_agy', side_effect=[error, envelope(REPLY), envelope(REPLY)]) as run:
                    pipeline.process_short_batch([self.item('one.m4a')], self.args, {}, {}, stats)
                    pipeline.process_short_batch([self.item('two.m4a')], self.args, {}, {}, stats)
                self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'second', 'second'])
                self.assertGreater(stats.models.remaining('first'), 0)

    def test_two_consecutive_timeout_exceptions_pause_model_across_jobs(self):
        with mock.patch.object(pipeline, 'run_agy', side_effect=[
            TimeoutError('timeout'), envelope(REPLY), TimeoutError('timeout'), envelope(REPLY), envelope(REPLY),
        ]) as run:
            for name in ('one.m4a', 'two.m4a', 'three.m4a'):
                pipeline.process_short_batch([self.item(name)], self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'second', 'first', 'second', 'second'])
        self.assertGreater(self.stats.models.remaining('first'), 0)
        self.assertEqual(self.stats.done, 3)

    def test_unknown_provider_error_is_task_local_and_local_os_errors_propagate(self):
        with mock.patch.object(pipeline, 'run_agy', side_effect=[failure('Unexplained response'), envelope(REPLY), envelope(REPLY)]) as run:
            pipeline.process_short_batch([self.item('one.m4a')], self.args, {}, {}, self.stats)
            pipeline.process_short_batch([self.item('two.m4a')], self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'second', 'first'])
        with mock.patch.object(pipeline, 'run_agy', side_effect=PermissionError('local file unavailable')) as run:
            with self.assertRaises(PermissionError):
                pipeline.process_short_batch([self.item('local.m4a')], self.args, {}, {}, self.stats)
        self.assertEqual(run.call_count, 1)

    def test_format_error_is_repaired_then_falls_back_without_global_pause(self):
        with mock.patch.object(pipeline, 'run_agy', side_effect=[
            envelope('invalid'), envelope('still invalid'), envelope(REPLY), envelope(REPLY),
        ]) as run:
            pipeline.process_short_batch([self.item('bad.m4a')], self.args, {}, {}, self.stats)
            pipeline.process_short_batch([self.item('next.m4a')], self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'first', 'second', 'first'])
        self.assertEqual(self.stats.models.remaining('first'), 0)

    def test_format_exhaustion_without_usable_speech_does_not_publish_md(self):
        item = self.item('bad.m4a')
        with mock.patch.object(pipeline, 'run_agy', return_value=envelope('invalid')) as run:
            with self.assertRaises(ModelTaskFailed):
                pipeline.process_short_batch([item], self.args, {}, {}, self.stats)
        self.assertEqual(run.call_count, 6)
        self.assertFalse(Path(item['target_md']).exists())
        self.assertFalse(self.manifest.exists())

    def test_plain_content_refusal_switches_without_repair_and_does_not_pause_model(self):
        with mock.patch.object(pipeline, 'run_agy', side_effect=[
            envelope('I cannot transcribe this audio.'), envelope(REPLY), envelope(REPLY),
        ]) as run:
            pipeline.process_short_batch([self.item('one.m4a')], self.args, {}, {}, self.stats)
            pipeline.process_short_batch([self.item('two.m4a')], self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'second', 'first'])

    def test_format_failure_text_cannot_pause_other_workers(self):
        item = self.item('format.m4a')
        with mock.patch.object(pipeline, 'run_agy', return_value=envelope('[bad timestamp] timeout')):
            with self.assertRaises(ModelTaskFailed) as raised:
                pipeline.process_short_batch([item], self.args, {}, {}, self.stats)
        self.stats.fail([item], raised.exception, stage='response_validation')
        self.assertEqual(self.stats._backend_until, 0)
        self.assertTrue(all(self.stats.models.remaining(m) == 0 for m in self.args.model_chain))

    def test_long_format_error_uses_repairs_then_another_model(self):
        item = self.item('long.m4a')
        item['parts'] = [{'part': 1, 'path': 'one.wav', 'duration_ms': 5000, 'offset_ms': 0}]
        with mock.patch.object(pipeline, 'run_agy', side_effect=[
            envelope('invalid'), envelope('invalid'), envelope('invalid'), envelope(REPLY),
        ]) as run:
            pipeline.process_long_item(item, self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'first', 'first', 'second'])
        self.assertEqual(self.stats.models.remaining('first'), 0)
        self.assertEqual(self.stats.done, 1)

    def test_switching_later_long_part_restores_speakers_and_previous_summary(self):
        item = self.item('long.m4a', duration=20000)
        item['parts'] = [
            {'part': 1, 'path': 'one.wav', 'duration_ms': 10000, 'offset_ms': 0},
            {'part': 2, 'path': 'two.wav', 'duration_ms': 10000, 'offset_ms': 10000},
        ]
        first = REPLY + '\nSUMMARY_JSON: "Обсудили плотность бумаги."'
        with mock.patch.object(pipeline, 'run_agy', side_effect=[
            envelope(first), failure('HTTP 502 Bad Gateway'), envelope(REPLY),
        ]) as run:
            pipeline.process_long_item(item, self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'first', 'second'])
        self.assertIsNone(run.call_args_list[-1].kwargs['conversation_id'])
        prompt = run.call_args_list[-1].args[0]
        self.assertIn('Known speakers from earlier parts', prompt)
        self.assertIn('Собеседник', prompt)
        self.assertIn('Обсудили плотность бумаги.', prompt)
        self.assertIn('[00:00:11.00]', Path(item['target_md']).read_text())

    def test_partial_batch_retries_only_failed_file(self):
        items = [self.item('good.m4a'), self.item('bad.m4a')]
        batch = '=== ITEM 1 ===\n' + REPLY + '\n=== ITEM 2 ===\ninvalid'
        with mock.patch.object(pipeline, 'run_agy', side_effect=[
            envelope(batch), envelope('invalid'), envelope('invalid'), envelope(REPLY),
        ]) as run:
            pipeline.process_short_batch(items, self.args, {}, {}, self.stats)
        self.assertEqual([c.args[1] for c in run.call_args_list], ['first', 'first', 'first', 'second'])
        self.assertEqual(self.stats.done, 2)
        records = [json.loads(line) for line in self.manifest.read_text().splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual([r['model'] for r in records], ['first', 'second'])

    def test_usable_response_can_be_salvaged_after_other_models_refuse_content(self):
        late = REPLY.replace('00:00:01.00', '00:00:08.00')
        item = self.item('late.m4a')
        with mock.patch.object(pipeline, 'run_agy', side_effect=[
            envelope(late), envelope(late), failure('content safety filters'), failure('content safety filters'),
        ]):
            pipeline.process_short_batch([item], self.args, {}, {}, self.stats)
        meta = yaml.safe_load(Path(item['target_md']).read_text().split('---', 2)[1])
        self.assertEqual(meta['model'], 'first')
        self.assertIn('Сохранено после сбоя', meta['notes'])

    def test_stop_on_quota_preserves_explicit_stop_behavior(self):
        self.args.stop_on_quota = True
        with mock.patch.object(pipeline, 'run_agy', return_value=failure('Quota reached. Resets in 1h')) as run:
            with self.assertRaisesRegex(RuntimeError, 'QUOTA'):
                pipeline.process_short_batch([self.item('quota.m4a')], self.args, {}, {}, self.stats)
        self.assertEqual(run.call_count, 1)

    def test_all_disabled_models_propagate_through_job_and_stop_worker_pool(self):
        item = self.item('fatal.m4a')
        item['parts'] = None
        self.args.__dict__.update(subdir='.', limit=20, workers=1, retry_skipped=False, retry_refused=False,
                                  dry_run=False, no_faad=True, no_untrunc=True,
                                  batch_short_max=1, batch_short_sec=300)
        with mock.patch.object(cli, 'scan_pending', return_value=[item]), \
                mock.patch.object(cli, 'pack_next_job', side_effect=lambda q, *args: [q.popleft()] if q else None), \
                mock.patch.object(pipeline, 'load_refs', return_value=({}, {})), \
                mock.patch.object(pipeline, 'prepare_audio_work', return_value=(item, None)), \
                mock.patch.object(pipeline, 'run_agy', return_value=failure('Unknown model')) as run:
            with self.assertRaises(ModelsUnavailable) as raised:
                cli.run_workers(self.args)
        self.assertTrue(raised.exception.all_disabled)
        self.assertTrue(runtime.STOP_EVENT.is_set())
        self.assertEqual(run.call_count, 3)
        self.assertFalse(Path(item['target_md']).exists())

    def test_cli_reports_no_available_models_with_exit_one(self):
        with tempfile.TemporaryDirectory(prefix='fatal-cli-test-') as tmp, \
                mock.patch('sys.argv', ['diarize.py', '--folder', tmp]), \
                mock.patch.object(cli, 'resolve_source_dir'), \
                mock.patch.object(cli.shutil, 'which', return_value='/usr/bin/tool'), \
                mock.patch.object(cli, '_get_libfaad', return_value=None), \
                mock.patch.object(cli, 'find_untrunc', return_value=None), \
                mock.patch.object(cli, 'run_log', side_effect=lambda: diagnostics.run_log(tmp)), \
                mock.patch.object(cli, 'run_workers', side_effect=ModelsUnavailable('all disabled', all_disabled=True)):
            self.assertEqual(cli.main(), 1)


if __name__ == '__main__':
    unittest.main()
