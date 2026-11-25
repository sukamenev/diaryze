"""Restore the caller's terminal input and common reporting modes on exit."""

from contextlib import contextmanager
import os
import sys
import termios


# Save and restore terminal settings around archive processing.
@contextmanager
def preserve_terminal():
    saved = []
    seen = set()
    output_fd = None
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            fd = stream.fileno()
            if not os.isatty(fd):
                continue
            device = os.fstat(fd).st_rdev
            if device not in seen:
                saved.append((fd, termios.tcgetattr(fd)))
                seen.add(device)
            if stream is sys.stdout or stream is sys.stderr:
                output_fd = fd
        except (OSError, ValueError, AttributeError, termios.error):
            continue
    try:
        yield
    finally:
        for fd, attrs in saved:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
            except termios.error:
                pass
        if output_fd is not None:
            try:
                # No screen/history clearing; restore cursor/keypad/paste modes.
                os.write(output_fd, b"\x1b[?2004l\x1b[?1l\x1b>\x1b[<u\x1b[?25h"
                         b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l")
            except OSError:
                pass
