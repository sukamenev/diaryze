"""Exercise portable locks, native subprocess pipes, and the Windows agy setup offline."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.diarizer import agy, runtime
from scripts.diarizer.platform import processes, windows
from scripts.diarizer.platform import IS_WINDOWS, locked_path


class PlatformTests(unittest.TestCase):
    # Check a cross-process lock attempt and reacquisition after release.
    def test_nonblocking_path_lock_contends_across_processes(self):
        code = """
import sys
from pathlib import Path
from scripts.diarizer.platform import locked_path
try:
    with locked_path(Path(sys.argv[1]), create=True, blocking=False):
        print('acquired')
except BlockingIOError:
    print('busy')
"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.lock"
            command = [sys.executable, "-c", code, str(path)]
            with locked_path(path, create=True):
                self.assertEqual(processes.run_capture(command, timeout=10).stdout, "busy\n")
            self.assertEqual(processes.run_capture(command, timeout=10).stdout, "acquired\n")

    # Check that the streaming reader preserves UTF-8 and returns a reply before EOF.
    def test_windows_capture_returns_complete_envelope_before_exit(self):
        code = """
import json, sys, time
prompt = sys.stdin.buffer.read().decode('utf-8')
sys.stderr.buffer.write('диагностика\\r\\n'.encode('utf-8'))
sys.stderr.buffer.flush()
reply = json.dumps({'status': 'SUCCESS', 'result': prompt}, ensure_ascii=False)
sys.stdout.buffer.write(reply.encode('utf-8'))
sys.stdout.buffer.flush()
time.sleep(30)
"""
        command = [sys.executable, "-c", code]
        with processes.managed_process(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding="utf-8") as proc:
            result = processes._capture_windows(proc, command, "Привет", 8,
                                                agy.agy_envelope_complete, 0.1)
        self.assertEqual(json.loads(result.stdout)["result"], "Привет")
        self.assertEqual(result.stderr, "диагностика\n")
        self.assertNotEqual(result.returncode, 0)

    # Check Windows cancellation handlers without requiring a console or live provider.
    def test_second_interrupt_sets_force_stop(self):
        import signal

        self.addCleanup(runtime.STOP_EVENT.clear)
        self.addCleanup(runtime.FORCE_STOP_EVENT.clear)
        self.addCleanup(runtime.CONSOLE_INTERRUPTED.clear)
        with patch.object(runtime.os, "write"), runtime.stop_signals():
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            self.assertTrue(runtime.STOP_EVENT.is_set())
            self.assertFalse(runtime.FORCE_STOP_EVENT.is_set())
            handler(signal.SIGINT, None)
            self.assertTrue(runtime.FORCE_STOP_EVENT.is_set())

    # Check that the existing audio reader is available in the standard Windows agy home.
    def test_agy_uses_standard_windows_home(self):
        with TemporaryDirectory() as directory, patch.object(agy, "IS_WINDOWS", True), \
                patch.object(agy.Path, "home", return_value=Path(directory)), \
                patch.object(agy, "run_capture", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
            _, _, _, home = agy.run_agy("prompt", "model", 10, True)
            reader = Path(directory) / ".gemini/config/agents/audio-reader/agent.md"
            self.assertIn("tools: [read_file]", reader.read_text(encoding="utf-8"))
        self.assertIsNone(home)
        self.assertIsNone(run.call_args.kwargs["env"])
        self.assertIn("audio-reader", run.call_args.args[0])

    # Check that a Windows Job Object reaches the child after the parent exits.
    @unittest.skipUnless(IS_WINDOWS, "requires the Windows process API")
    def test_job_stops_descendant_after_parent_exits(self):
        code = """
import subprocess, sys
sys.stdin.read()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
print(child.pid, flush=True)
"""
        result = processes.run_capture([sys.executable, "-c", code], input="go", timeout=8,
                                       finish_when=lambda output: output.strip().isdigit(), finish_grace=0.2)
        pid = int(result.stdout)
        kernel = windows._kernel()
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0)
            finally:
                kernel.CloseHandle(handle)
