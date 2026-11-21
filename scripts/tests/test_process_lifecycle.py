"""Real local subprocess/PTY tests; never launches agy or touches audio."""

import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from scripts.diarizer.agy import agy_envelope_complete, extract_envelope_error_and_usage
from scripts.diarizer.processes import run_capture


ROOT = Path(__file__).resolve().parents[2]


def running(pid):
    if sys.platform == "darwin":
        status = subprocess.run(["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True)
        return status.returncode == 0 and bool(status.stdout.strip()) and not status.stdout.strip().startswith("Z")
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False


def kill_marker(path):
    if path.exists():
        pid = int(path.read_text())
        if running(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class ProcessTests(unittest.TestCase):
    def test_no_terminal_input_or_controlling_tty(self):
        code = '''
import json, os, sys
try:
    fd = os.open('/dev/tty', os.O_RDWR)
except OSError:
    controlling_tty = False
else:
    controlling_tty = True
    os.close(fd)
print(json.dumps([sys.stdin.read(), os.isatty(0), os.isatty(1), os.isatty(2), controlling_tty]))
'''
        result = run_capture([sys.executable, "-c", code], timeout=5)
        self.assertEqual(json.loads(result.stdout), ["", False, False, False, False])
        result = run_capture([sys.executable, "-c", code], input="prompt", timeout=5)
        self.assertEqual(json.loads(result.stdout)[0], "prompt")

    def test_complete_envelope_survives_hang_guard_kill(self):
        code = '''
import json, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({"status": "SUCCESS", "response": "READY", "conversation_id": "hang-test"}), flush=True)
time.sleep(30)
'''
        result = run_capture([sys.executable, "-c", code], timeout=12,
                             finish_when=agy_envelope_complete, finish_grace=0.3)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertEqual(json.loads(result.stdout)["status"], "SUCCESS")
        error, text, _, conv = extract_envelope_error_and_usage(
            result.returncode, result.stdout, result.stderr)
        self.assertIsNone(error)
        self.assertEqual((text, conv), ("READY", "hang-test"))

    def test_timeout_kills_detached_descendant(self):
        with tempfile.TemporaryDirectory(prefix="diarizer-process-test-") as tmp:
            marker = Path(tmp) / "child.pid"
            self.addCleanup(kill_marker, marker)
            child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
            code = f'''
import os, pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
p = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)
pathlib.Path({str(marker)!r}).write_text(str(p.pid))
print('READY', flush=True)
time.sleep(60)
'''
            with self.assertRaises(subprocess.TimeoutExpired):
                run_capture([sys.executable, "-c", code], timeout=0.5)
            self.assertTrue(marker.exists())
            self.assertFalse(running(int(marker.read_text())))

    def test_signal_handler_does_not_acquire_console_lock(self):
        code = '''
import signal
from scripts.diarizer.runtime import PRINT_LOCK, STOP_EVENT, FORCE_STOP_EVENT, stop_signals
with stop_signals():
    with PRINT_LOCK:
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
    assert STOP_EVENT.is_set() and FORCE_STOP_EVENT.is_set()
print('DONE')
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DONE", result.stdout)

    def test_double_ctrl_c_cleans_children_and_restores_terminal(self):
        with tempfile.TemporaryDirectory(prefix="diarizer-pty-test-") as tmp:
            marker = Path(tmp) / "child.pid"
            self.addCleanup(kill_marker, marker)
            child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
            command = f'''
import pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
p = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)
pathlib.Path({str(marker)!r}).write_text(str(p.pid))
time.sleep(60)
'''
            driver = f'''
import fcntl, pathlib, sys, termios, threading, time
from scripts.diarizer.processes import run_capture
from scripts.diarizer.runtime import stop_signals
from scripts.diarizer.terminal import preserve_terminal
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
original_attrs = termios.tcgetattr(0)
def work():
    try:
        run_capture([sys.executable, '-c', {command!r}])
    except InterruptedError:
        pass
with stop_signals(), preserve_terminal():
    attrs = termios.tcgetattr(0)
    attrs[3] &= ~(termios.ECHO | termios.ICANON)
    termios.tcsetattr(0, termios.TCSANOW, attrs)
    worker = threading.Thread(target=work)
    worker.start()
    while not pathlib.Path({str(marker)!r}).exists():
        time.sleep(0.01)
    print('READY', flush=True)
    worker.join()
    print('DONE', flush=True)
assert termios.tcgetattr(0) == original_attrs
print('RESTORED', flush=True)
'''
            master, slave = pty.openpty()
            proc = subprocess.Popen([sys.executable, "-c", driver], cwd=ROOT, stdin=slave,
                                    stdout=slave, stderr=slave, start_new_session=True)
            output = bytearray()

            def read_until(token, timeout=5):
                deadline = time.monotonic() + timeout
                while token not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        output.extend(os.read(master, 65536))
                self.assertIn(token, output, output.decode(errors="replace"))

            try:
                read_until(b"READY")
                os.write(master, b"\x03")
                read_until(b"[STOP]")
                os.write(master, b"\x03")
                read_until(b"[FORCE STOP]")
                # Keep draining the PTY while the driver restores the terminal.
                read_until(b"DONE", timeout=10)
                read_until(b"\x1b[?2004l")
                # Darwin can revoke the slave PTY when its session leader exits.
                read_until(b"RESTORED")
                proc.wait(timeout=5)
                self.assertEqual(proc.returncode, 0, output.decode(errors="replace"))
                self.assertFalse(running(int(marker.read_text())))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                kill_marker(marker)
                os.close(master)
                os.close(slave)


if __name__ == "__main__":
    unittest.main()
