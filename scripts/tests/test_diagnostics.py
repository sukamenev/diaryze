"""Regression checks for concurrent logging and bounded failed-file scheduling."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts.diarizer import cli, diagnostics, pipeline, runtime


class DiagnosticsTests(unittest.TestCase):
    # Clear cancellation flags so later tests start in a clean runtime state.
    def tearDown(self):
        runtime.STOP_EVENT.clear()
        runtime.FORCE_STOP_EVENT.clear()

    # Check that concurrent log entries retain recording lists, full replies, and tracebacks.
    def test_parallel_log_preserves_file_lists_full_response_and_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with diagnostics.run_log(tmp) as path:
                # Record a distinct exception and model reply from one worker.
                def fail(i):
                    try:
                        raise ValueError('bad response ' + str(i))
                    except ValueError as exc:
                        diagnostics.record('model_error', items=[{'audio_rel': f'{i}.wav'}],
                                           error=exc, response='x' * 5000, model='test')
                with ThreadPoolExecutor(max_workers=5) as pool:
                    list(pool.map(fail, range(30)))
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 30)
            self.assertEqual({r['files'][0] for r in rows}, {f'{i}.wav' for i in range(30)})
            self.assertEqual(len({r['run_id'] for r in rows}), 1)
            for row in rows:
                self.assertEqual(len(row['response']), 5000)
                self.assertIn('ValueError', row['traceback'])

    # Build a temporary audio item and the options needed to exercise the worker pool.
    def worker_fixture(self, tmp):
        path = Path(tmp) / 'test.wav'
        path.write_bytes(b'test')
        item = {'audio_abs': str(path), 'audio_rel': 'test.wav', 'mtime': 1}
        args = SimpleNamespace(subdir='.', retry_skipped=True, retry_refused=False, limit=0,
                               dry_run=False,
                               use_faad=False, no_faad=True, untrunc_bin=None, no_untrunc=True,
                               model_chain=['test'], workers=1, max_file_attempts=3,
                               batch_short_max=1, batch_short_sec=300, max_single_sec=780)
        return item, args

    # Check that a successful retry clears the failed-file count while retaining the error history.
    def test_error_count_survives_recovery_but_failed_count_clears(self):
        stats = pipeline.Stats()
        item = {'audio_rel': 'test.wav', 'duration_ms': 10}
        stats.fail([item], 'invalid response', stage='test')
        self.assertEqual(stats.snapshot()['failed_files'], 1)
        with mock.patch.object(pipeline, 'completed'):
            stats.complete(item, {})
        self.assertEqual(stats.snapshot()['errors'], 1)
        self.assertEqual(stats.snapshot()['failed_files'], 0)

    # Check that unchanged failed or skipped recordings do not cause endless queue retries.
    def test_unchanged_failed_or_skipped_file_is_not_rescheduled_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            item, args = self.worker_fixture(tmp)
            # Take one queued item for each mocked worker job.
            def pack(queue, *unused):
                return [queue.popleft()] if queue else None
            # Record a failure for the mocked job.
            def error(job, args, stats):
                stats.fail(job, 'invalid response', stage='test')
            with mock.patch.object(cli, 'scan_pending', return_value=[item]), \
                    mock.patch.object(cli, 'pack_next_job', side_effect=pack), \
                    mock.patch.object(cli, 'run_job', side_effect=error) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = cli.run_workers(args)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(result['errors'], 3)
            self.assertEqual(result['failed_files'], 1)

    # Check that an unexpected exception in a worker propagates to its caller.
    def test_worker_exception_reaches_caller(self):
        with tempfile.TemporaryDirectory() as tmp:
            item, args = self.worker_fixture(tmp)
            with mock.patch.object(cli, 'scan_pending', return_value=[item]), \
                    mock.patch.object(cli, 'pack_next_job', side_effect=ValueError('probe exploded')), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaisesRegex(ValueError, 'probe exploded'):
                cli.run_workers(args)
            self.assertTrue(runtime.STOP_EVENT.is_set())

    # Check that cancellation interrupts the shared service pause.
    def test_service_pause_is_interruptible(self):
        stats = pipeline.Stats()
        with mock.patch.object(pipeline.time, 'monotonic', return_value=100), \
                contextlib.redirect_stdout(io.StringIO()):
            for _ in range(8):
                stats.fail([{'audio_rel': 'a.wav'}], 'INTERNAL (code 500)', stage='test')
            self.assertEqual(stats._backend_until, 400)
            runtime.STOP_EVENT.set()
            with self.assertRaises(InterruptedError):
                stats.wait_backend()


if __name__ == '__main__':
    unittest.main()
