"""Non-interactive subprocesses with bounded cleanup of their process trees."""

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .config import PROCESS_RESULT_GRACE_SEC, PROCESS_TERMINATION_GRACE_SEC
from .runtime import FORCE_STOP_EVENT


# Read process ancestry and group IDs for descendant cleanup.
def _process_table():
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid="],
                                    capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return
        if result.returncode == 0:
            for row in result.stdout.splitlines():
                try:
                    pid, parent, group = map(int, row.split())
                except ValueError:
                    continue
                yield pid, parent, group
        return
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            yield int(path.parent.name), int(fields[1]), int(fields[2])
        except (OSError, ValueError, IndexError):
            continue


# Find process groups containing descendants, including detached sessions.
def _child_groups(parent_pid):
    """Include detached descendants, such as sudo's PTY command session."""
    children = {}
    for pid, parent, group in _process_table():
        children.setdefault(parent, []).append((pid, group))
    groups = {parent_pid}
    pending = [parent_pid]
    while pending:
        for pid, group in children.get(pending.pop(), ()):
            groups.add(group)
            pending.append(pid)
    # Never signal the terminal's foreground group, even for malformed /proc data.
    return {group for group in groups if group > 0 and group != os.getpgrp()}


# Stop the subprocess and its descendant groups.
def terminate_tree(proc, grace=PROCESS_TERMINATION_GRACE_SEC):
    groups = _child_groups(proc.pid)

    # Send a termination signal to each group in the subprocess tree.
    def send(sig):
        for group in groups:
            try:
                os.killpg(group, sig)
            except (ProcessLookupError, PermissionError):
                pass

    send(signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # The wrapper may have exited while descendants still hold pipes or a PTY.
    send(signal.SIGKILL)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


# Track a subprocess and clean up its tree when the context exits.
@contextmanager
def managed_process(args, **kwargs):
    if FORCE_STOP_EVENT.is_set():
        raise InterruptedError("force stopped")
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    proc = subprocess.Popen(args, start_new_session=True, **kwargs)
    try:
        yield proc
    finally:
        terminate_tree(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


# Capture subprocess output with cancellation, timeout, and completion checks.
def run_capture(args, *, input=None, timeout=None, finish_when=None,
                finish_grace=PROCESS_RESULT_GRACE_SEC, capture_output=True, text=True, **kwargs):
    """Like subprocess.run, but no terminal input, and force-stop aware."""
    if not capture_output or not text:
        raise ValueError("run_capture requires captured text output")
    deadline = None if timeout is None else time.monotonic() + timeout
    done_seen = None
    kwargs.setdefault("encoding", "utf-8")
    with managed_process(args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs) as proc:
        first = True
        while True:
            if FORCE_STOP_EVENT.is_set():
                raise InterruptedError("force stopped")
            try:
                stdout, stderr = proc.communicate(input=input if first else None, timeout=0.2)
                return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired as exc:
                first = False
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    raise subprocess.TimeoutExpired(args, timeout, output=exc.output, stderr=exc.stderr)
                partial = exc.output or b""
                if isinstance(partial, bytes):
                    partial = partial.decode(kwargs["encoding"], errors="replace")
                if finish_when and finish_when(partial):
                    done_seen = done_seen if done_seen is not None else now
                    if now - done_seen >= finish_grace:
                        terminate_tree(proc)
                        try:
                            stdout, stderr = proc.communicate(timeout=1)
                        except subprocess.TimeoutExpired as final:
                            stdout, stderr = final.output or b"", final.stderr or b""
                        if isinstance(stdout, bytes):
                            stdout = stdout.decode(kwargs["encoding"], errors="replace")
                        if isinstance(stderr, bytes):
                            stderr = stderr.decode(kwargs["encoding"], errors="replace")
                        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
