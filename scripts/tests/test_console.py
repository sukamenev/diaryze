"""Readable record blocks, automatic styling and concurrent output."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import contextlib
import io
import os
import pty
import re
import select
import termios
import threading
import time
import unittest
from unittest import mock

from scripts.diarizer.console import emit, render_completed, render_finished
from scripts.diarizer import console, pipeline, runtime


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


def screen_lines(text, width=300):
    """Interpret the small set of terminal controls used by the console renderer."""
    rows, row, col = [""], 0, 0
    for token in re.findall(r"\x1b\[[0-9;]*[A-Za-z]|[^\x1b]", text):
        if token.startswith("\x1b"):
            if token == "\x1b[2K":
                rows[row] = ""
            elif token == "\x1b[1A":
                row = max(0, row - 1)
            continue
        if token == "\r":
            col = 0
            continue
        if token == "\n":
            row += 1
            col = 0
        else:
            if col == width:
                row += 1
                col = 0
            while len(rows) <= row:
                rows.append("")
            old = rows[row].ljust(col)
            rows[row] = old[:col] + token + old[col + 1:]
            col += 1
        while len(rows) <= row:
            rows.append("")
    return [line.rstrip() for line in rows if line.strip()]


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.item = {"target_md": "/archive/Text/Phone/2026/Пример звонка.m4a.md",
                     "duration_ms": 186000, "dt": "2026-09-20T15:52:00"}
        self.meta = {"speakers": {"S1": {"label": "Лена"}, "S2": {"person": "ivanov_sergey"}}}
        self.stats = {"done": 1, "remaining": 1235, "audio_ms": 186000, "elapsed_s": 20.5,
                      "skipped": 0, "refused": 0, "errors": 0,
                      "tok_in": 7000, "tok_cache": 0, "tok_out": 5000}
        self.now = datetime(2026, 11, 15, 22, 43, 45, tzinfo=timezone.utc)

    def render(self, **kwargs):
        return render_completed(self.item, self.meta, self.stats, now=self.now, **kwargs)

    def test_plain_block_has_one_timestamp_full_path_and_speed_multiplier(self):
        text = self.render(color=False)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertEqual(text.count("[22:43:45]"), 1)
        self.assertIn(self.item["target_md"], text.splitlines()[0])
        self.assertIn("3.1 мин · S1: Лена · S2: ivanov_sergey", text)
        self.assertIn("Готово 1 · в очереди 1235 · всего аудио 3.1 мин · x9.1", text)
        self.assertIn("токены: вход 7k, кэш 0k, выход 5k", text)
        for absent in ("=", "m/h", "\x1b", "пропуски", "отказы", "ошибки"):
            self.assertNotIn(absent, text)

    def test_nonzero_counters_and_notes_stand_out_without_extra_lines(self):
        self.stats.update(skipped=1, refused=2, errors=3)
        self.meta["notes"] = "Требуется проверка.\nВторая строка."
        text = self.render(color=True)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertIn("\x1b[1;33mWARN", text)
        self.assertIn("\x1b[1;31mсбои 3", text)
        self.assertIn("\x1b[3;33mТребуется проверка.", text)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
        self.assertEqual(plain, self.render(color=False))

    def test_color_requires_tty_and_respects_no_color_and_dumb(self):
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True):
            with contextlib.redirect_stdout(TtyBuffer()):
                self.assertIn("\x1b", self.render())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertNotIn("\x1b", self.render())
            with mock.patch.dict(os.environ, {"NO_COLOR": ""}), contextlib.redirect_stdout(TtyBuffer()):
                self.assertNotIn("\x1b", self.render())
            with mock.patch.dict(os.environ, {"TERM": "dumb"}), contextlib.redirect_stdout(TtyBuffer()):
                self.assertNotIn("\x1b", self.render())

    def test_untrusted_names_cannot_inject_terminal_commands(self):
        self.meta["speakers"]["S1"]["label"] = "Имя\x1b[2J\r\n"
        text = self.render(color=False)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertNotIn("\x1b", text)
        self.assertIn("Имя\\x1b[2J\\x0d\\x0a", text)

    def test_identification_status_is_visible_without_adding_a_line(self):
        self.meta["identification"] = "partial"
        text = self.render(color=False)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertIn("Участники: частично", text.splitlines()[1])

    def test_final_summary_uses_hours_minutes_and_seconds_without_equals(self):
        self.stats.update(done=158, audio_ms=25428000, elapsed_s=1514)
        text = render_finished(self.stats, stopped=True, color=False)
        self.assertTrue(text.startswith("Остановлено · Готово 158"))
        self.assertIn("всего аудио 7 ч 03 мин 48 с", text)
        self.assertIn("x16.8", text)
        self.assertTrue(text.endswith("время работы 25 мин 14 с"))
        for absent in ("=", "423.8", "1514", "в очереди", "пропуски", "\x1b"):
            self.assertNotIn(absent, text)
        self.stats["elapsed_s"] = 3605
        self.assertIn("время работы 1 ч 00 мин 05 с", render_finished(self.stats, color=False))

    def test_worker_blocks_do_not_interleave(self):
        class SlowBuffer(io.StringIO):
            def write(self, text):
                for char in text:
                    super().write(char)
                    time.sleep(0.00001)
                return len(text)

        stream = SlowBuffer()
        barrier = threading.Barrier(2)
        blocks = ["first\n  first details\n  first totals", "second\n  second details\n  second totals"]

        def report(block):
            barrier.wait(timeout=3)
            emit(block, file=stream)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(report, blocks))
        self.assertIn(stream.getvalue(), ["\n".join(blocks) + "\n", "\n".join(reversed(blocks)) + "\n"])


class LiveConsoleTests(unittest.TestCase):
    def setUp(self):
        runtime.STOP_EVENT.clear()
        runtime.CONSOLE_INTERRUPTED.clear()
        self.addCleanup(runtime.STOP_EVENT.clear)
        self.addCleanup(runtime.CONSOLE_INTERRUPTED.clear)
        self.item = {'target_md': '/archive/example.m4a.md', 'audio_rel': 'example.m4a',
                     'duration_ms': 60000, 'dt': '2026-11-17T01:00:00'}
        self.meta = {'speakers': {'Сергей': {'person': 'ivanov_sergey'}}, 'identification': 'complete'}
        self.stats = {'done': 1, 'remaining': 4, 'audio_ms': 60000, 'elapsed_s': 10,
                      'skipped': 0, 'refused': 0, 'errors': 0,
                      'tok_in': 10000, 'tok_cache': 5000, 'tok_out': 3000}

    def test_only_latest_progress_is_visible_and_record_blocks_remain(self):
        output = TtyBuffer()
        with mock.patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
                mock.patch.object(console, '_columns', return_value=300), \
                contextlib.redirect_stdout(output), console.progress_output():
            console.completed(self.item, self.meta, self.stats)
            self.stats.update(done=2, remaining=3)
            console.completed(self.item, self.meta, self.stats)
            lines = screen_lines(output.getvalue())
            self.assertEqual(len(lines), 5)
            self.assertEqual(sum('example.m4a.md' in line for line in lines), 2)
            self.assertNotIn('Готово 1', '\n'.join(lines))
            self.assertIn('Готово 2', lines[-1])

    def test_warning_is_printed_above_status_and_final_summary_replaces_status(self):
        output = TtyBuffer()
        with mock.patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
                mock.patch.object(console, '_columns', return_value=300), \
                contextlib.redirect_stdout(output), console.progress_output():
            console.completed(self.item, self.meta, self.stats)
            console.emit('[WARN] Model paused')
            before = screen_lines(output.getvalue())
            self.assertIn('[WARN] Model paused', before[-2])
            self.assertIn('Готово 1', before[-1])
            console.emit(console.render_finished(self.stats), final=True)
            after = screen_lines(output.getvalue())
            self.assertTrue(after[-1].startswith('Завершено'))
            self.assertEqual(sum('Готово' in line for line in after), 1)
            self.assertEqual(sum('[WARN]' in line for line in after), 1)

    def test_piped_and_dumb_outputs_remain_three_plain_lines_per_record(self):
        for output, term in ((io.StringIO(), 'xterm'), (TtyBuffer(), 'dumb')):
            with self.subTest(term=term), mock.patch.dict(os.environ, {'TERM': term}, clear=True), \
                    contextlib.redirect_stdout(output), console.progress_output():
                console.completed(self.item, self.meta, self.stats)
                console.completed(self.item, self.meta, self.stats)
                self.assertEqual(len(output.getvalue().splitlines()), 6)
                self.assertNotIn('\x1b', output.getvalue())

    def test_narrow_terminal_clips_live_status_without_wrapping_or_broken_color(self):
        output = TtyBuffer()
        with mock.patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
                mock.patch.object(console, '_columns', return_value=40), \
                contextlib.redirect_stdout(output), console.progress_output():
            console.completed(self.item, self.meta, self.stats)
            line = screen_lines(output.getvalue(), width=40)[-1]
            self.assertLessEqual(len(line), 39)
            self.assertTrue(line.endswith('…'))
            self.assertTrue(output.getvalue().endswith('\x1b[0m'))

    def test_no_color_disables_styling_but_keeps_live_status(self):
        output = TtyBuffer()
        with mock.patch.dict(os.environ, {'TERM': 'xterm', 'NO_COLOR': ''}, clear=True), \
                mock.patch.object(console, '_columns', return_value=300), \
                contextlib.redirect_stdout(output), console.progress_output():
            console.completed(self.item, self.meta, self.stats)
            console.completed(self.item, self.meta, self.stats)
            self.assertIn('\r\x1b[2K', output.getvalue())
            self.assertNotRegex(output.getvalue(), r'\x1b\[[0-9;]*m')

    def test_exception_cleanup_erases_status_and_signal_messages_are_preserved(self):
        output = TtyBuffer()
        with mock.patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
                mock.patch.object(console, '_columns', return_value=300), \
                contextlib.redirect_stdout(output):
            with self.assertRaises(RuntimeError), console.progress_output():
                console.completed(self.item, self.meta, self.stats)
                raise RuntimeError('test failure')
            self.assertNotIn('Готово', '\n'.join(screen_lines(output.getvalue())))
            with contextlib.redirect_stderr(output), console.progress_output():
                console.completed(self.item, self.meta, self.stats)
                runtime.STOP_EVENT.set()
                runtime.CONSOLE_INTERRUPTED.set()
                output.write('\n[STOP] draining\n')  # Signal-handler output bypasses PRINT_LOCK.
                console.emit('After signal')
            lines = screen_lines(output.getvalue())
            self.assertIn('[STOP] draining', lines)
            self.assertEqual(lines[-1], 'After signal')

    def test_concurrent_completions_keep_both_records_and_one_shared_status(self):
        output = TtyBuffer()
        stats = pipeline.Stats()
        barrier = threading.Barrier(2)

        def complete(number):
            barrier.wait(timeout=2)
            item = dict(self.item, target_md=f'/archive/{number}.md', audio_rel=f'{number}.m4a')
            stats.complete(item, self.meta)

        with mock.patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
                mock.patch.object(console, '_columns', return_value=300), \
                contextlib.redirect_stdout(output), console.progress_output():
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(complete, (1, 2)))
            lines = screen_lines(output.getvalue())
            self.assertEqual(sum('/archive/' in line for line in lines), 2)
            self.assertEqual(sum('Готово' in line for line in lines), 1)
            self.assertIn('Готово 2', lines[-1])

    def test_real_pty_stderr_does_not_overwrite_records_or_leave_duplicate_status(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        termios.tcsetwinsize(slave, (24, 300))
        os.set_blocking(master, False)
        output = bytearray()

        def drain():
            while select.select([master], [], [], 0.05)[0]:
                try:
                    chunk = os.read(master, 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                output.extend(chunk)

        with os.fdopen(os.dup(slave), 'w') as stdout, os.fdopen(os.dup(slave), 'w') as stderr:
            with mock.patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), console.progress_output():
                console.completed(self.item, self.meta, self.stats)
                drain()
                console.emit('[ERR] Visible error', file=stderr)
                drain()
                self.stats.update(done=2)
                console.completed(self.item, self.meta, self.stats)
                drain()
                console.emit(console.render_finished(self.stats), final=True)
                drain()
        lines = screen_lines(output.decode())
        self.assertEqual(sum('example.m4a.md' in line for line in lines), 2)
        self.assertEqual(sum('[ERR] Visible error' in line for line in lines), 1)
        self.assertEqual(sum('Готово' in line for line in lines), 1)
        self.assertTrue(lines[-1].startswith('Завершено'))


if __name__ == "__main__":
    unittest.main()
