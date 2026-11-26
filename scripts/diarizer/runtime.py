"""Shared cancellation events and console lock for all workers."""

from contextlib import contextmanager
import os
import signal
import threading

from .platform import STOP_SIGNALS
from .ui import tr


STOP_EVENT = threading.Event()


FORCE_STOP_EVENT = threading.Event()


PRINT_LOCK = threading.Lock()

# A signal prints directly, bypassing the console lock and moving its cursor.
CONSOLE_INTERRUPTED = threading.Event()


# Install signal handlers for orderly draining and immediate cancellation.
@contextmanager
def stop_signals():
    """First Ctrl+C drains active work; second, TERM or HUP cancels it."""
    STOP_EVENT.clear()
    FORCE_STOP_EVENT.clear()
    CONSOLE_INTERRUPTED.clear()
    force = tr("[FORCE STOP] Cancelling active processes and restoring the terminal...")
    drain = tr("[STOP] Finishing active jobs (press Ctrl+C again to cancel immediately)...")
    force, drain = (f"\n{message}\n".encode("utf-8") for message in (force, drain))

    # Escalate a repeated interrupt or termination signal to a forced stop.
    def handle_stop(signum, frame):
        if signum != signal.SIGINT or STOP_EVENT.is_set():
            if FORCE_STOP_EVENT.is_set():
                return
            STOP_EVENT.set()
            FORCE_STOP_EVENT.set()
            message = force
        else:
            STOP_EVENT.set()
            message = drain
        # Taking PRINT_LOCK or using buffered print from a signal handler can deadlock.
        CONSOLE_INTERRUPTED.set()
        try:
            os.write(2, message)
        except OSError:
            pass

    previous = {}
    try:
        for sig in STOP_SIGNALS:
            previous[sig] = signal.signal(sig, handle_stop)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
