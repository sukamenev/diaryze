"""Compact, serialized console reports with optional ANSI styling."""

from contextlib import contextmanager
from datetime import datetime
import os
import re
import shutil
import sys
import unicodedata

from .config import TZ
from .identification import STATUS_LABELS
from .runtime import CONSOLE_INTERRUPTED, PRINT_LOCK, STOP_EVENT
from .platform.terminal import supports_ansi
from .ui import tr


_LIVE = None
_SGR = re.compile(r"\x1b\[[0-9;]*m")


# Determine whether the stream supports terminal presentation.
def _interactive(stream):
    return (supports_ansi(stream)
            and os.environ.get("TERM") != "dumb" and not STOP_EVENT.is_set())


# Read the current terminal width for progress rendering.
def _columns(stream):
    try:
        return max(1, os.get_terminal_size(stream.fileno()).columns)
    except (AttributeError, OSError, ValueError):
        return max(1, shutil.get_terminal_size().columns)


# Compute how much terminal space a character occupies.
def _cell_width(char):
    return 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)


# Clip progress text to a single physical terminal row.
def _fit_progress(text, width):
    """Keep the live status on one physical row, leaving the last column free."""
    limit = max(0, width - 1)
    cells = sum(_cell_width(c) for c in _SGR.sub("", text))
    if cells <= limit:
        return text, cells
    if not limit:
        return "", 0
    parts, used = [], 0
    for token in re.findall(r"\x1b\[[0-9;]*m|.", text):
        if _SGR.fullmatch(token):
            parts.append(token)
        else:
            size = _cell_width(token)
            if used + size > limit - 1:
                break
            parts.append(token)
            used += size
    return "".join(parts) + "…" + ("\x1b[0m" if "\x1b" in text else ""), used + 1


# Check whether two output streams share a terminal.
def _same_terminal(first, second):
    if first is second:
        return True
    try:
        return (first.isatty() and second.isatty()
                and os.fstat(first.fileno()).st_rdev == os.fstat(second.fileno()).st_rdev)
    except (AttributeError, OSError, ValueError):
        return False


# Clear the active progress row before writing other output.
def _erase_progress():
    """Called under PRINT_LOCK; leave the cursor at the start of the cleared row."""
    global _LIVE
    live, _LIVE = _LIVE, None
    if live is None:
        return
    stream, _, cells = live
    if getattr(stream, "closed", False):
        return
    if CONSOLE_INTERRUPTED.is_set() and _same_terminal(stream, sys.stderr):
        return
    # A terminal resize can reflow the previously drawn line into several rows.
    width = _columns(stream)
    rows = max(1, (cells + width - 1) // width)
    stream.write("\r\x1b[2K" + "\x1b[1A\r\x1b[2K" * (rows - 1))
    stream.flush()


# Display the pending progress line when the terminal can show it.
def _draw_progress(stream, text):
    global _LIVE
    fitted, cells = _fit_progress(text, _columns(stream))
    stream.write(fitted)
    stream.flush()
    _LIVE = stream, text, cells


# Set up and clean up the shared transient progress display.
@contextmanager
def progress_output():
    """Own the transient status for one CLI run, including exceptional exits."""
    with PRINT_LOCK:
        _erase_progress()
    try:
        yield
    finally:
        with PRINT_LOCK:
            _erase_progress()


# Make untrusted text safe to print to a terminal.
def _display(value):
    # File names and model text must not inject terminal commands or extra lines.
    return "".join(f"\\x{ord(c):02x}" if ord(c) < 32 or ord(c) == 127 else c for c in str(value))


class Palette:
    # Choose styling behavior for the output stream.
    def __init__(self, stream=None, enabled=None):
        stream = stream if stream is not None else sys.stdout
        self.enabled = enabled if enabled is not None else (
            supports_ansi(stream)
            and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ
        )

    # Apply a terminal style when color is enabled.
    def paint(self, value, code):
        text = _display(value)
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled else text

    # Render text in the subdued status style.
    def dim(self, value):
        return self.paint(value, "2")


# Write a message atomically relative to progress and other workers.
def emit(message, *, file=None, flush=True, final=False):
    """Write an entire message/block under the shared worker output lock."""
    stream = file if file is not None else sys.stdout
    message = str(message)
    palette = Palette(stream)
    message = re.sub(r"^\[(WARN|ERR|REFUSED|SKIP|RECOVERY)\]", lambda m: palette.paint(
        m.group(), "1;31" if m.group(1) == "ERR" else "33"), message, count=1)
    with PRINT_LOCK:
        live = _LIVE
        if live is not None and (CONSOLE_INTERRUPTED.is_set() or _same_terminal(stream, live[0])):
            _erase_progress()
        else:
            live = None
        stream.write(message + "\n")
        if flush or live is not None:
            stream.flush()
        if live is not None and not final and _interactive(live[0]):
            _draw_progress(live[0], live[1])


# Format a recording's duration for the completion display.
def _duration(ms):
    return f"{ms / 1000:.0f} {tr('s')}" if ms < 60000 else f"{ms / 60000:.1f} {tr('min')}"


# Format elapsed time for progress and run totals.
def format_time(seconds):
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} {tr('h')} {minutes:02d} {tr('min')} {seconds:02d} {tr('s')}"
    if minutes:
        return f"{minutes} {tr('min')} {seconds:02d} {tr('s')}"
    return f"{seconds} {tr('s')}"


# Build the compact status line for work in progress.
def render_progress(stats, palette, *, clock=False):
    # Render a single progress field with optional emphasis.
    def field(label, value):
        return palette.dim(tr(label)) + " " + _display(value)

    parts = [field("Done", stats["done"])]
    if stats.get("remaining") is not None:
        parts.append(field("queued", stats["remaining"]))
    duration = format_time(stats["audio_ms"] / 1000) if clock else _duration(stats["audio_ms"])
    parts.append(field("audio total", duration))
    speed = stats["audio_ms"] / (1000 * max(1e-6, stats["elapsed_s"]))
    parts.append(palette.paint(f"x{speed:.1f}", "1"))
    for key, label, color in (("skipped", "skipped", "33"),
                              ("needs_recovery", "needs recovery", "33"),
                              ("refused", "refused", "33"), ("errors", "errors", "1;31")):
        if stats.get(key):
            parts.append(palette.paint(f"{tr(label)} {stats[key]}", color))
    if stats.get('failed_files'):
        parts.append(palette.paint(tr("still pending after errors {count}", count=stats["failed_files"]), "1;31"))
    tokens = ", ".join(field(label, f"{stats[key] // 1000}k") for label, key in (
        ("input", "tok_in"), ("cache", "tok_cache"), ("output", "tok_out")))
    parts.append(palette.dim(tr("tokens:")) + " " + tokens)
    return " · ".join(parts)


# Build the completion block for a saved transcript.
def render_completed(item, meta, stats, *, color=None, now=None):
    palette = Palette(enabled=color)
    now = now if now is not None else datetime.now(TZ)
    warning = bool(meta.get("notes"))
    tag = palette.paint("WARN" if warning else "OK", "1;33" if warning else "1;32")
    path = palette.paint(os.path.abspath(item["target_md"]), "36")
    header = f"{palette.dim(now.strftime('[%H:%M:%S]'))} {tag}  {path}"
    recorded = str(item.get("recorded_at") or item.get("dt") or tr("date unknown")).replace("T", " ")[:16]
    details = [palette.dim(recorded), _duration(item["duration_ms"])]
    for speaker, info in (meta.get("speakers") or {}).items():
        if not isinstance(info, dict):
            info = {"label": str(info)}
        who = info.get("person") or info.get("org") or info.get("label") or speaker
        details.append(palette.dim(f"{speaker}:") + " " + palette.paint(who, "1"))
    status = meta.get("identification")
    if status in STATUS_LABELS:
        color_code = "32" if status == "complete" else ("2" if status == "not_applicable" else "33")
        details.append(palette.paint(tr("Speakers: {status}", status=tr(STATUS_LABELS[status])), color_code))
    if warning:
        details.append(palette.paint(meta["notes"], "3;33"))
    return header + "\n  " + " · ".join(details) + "\n  " + render_progress(stats, palette)


# Emit the completion block and refresh live progress.
def completed(item, meta, stats):
    stream = sys.stdout
    if not _interactive(stream):
        emit(render_completed(item, meta, stats))
        return
    permanent, progress = render_completed(item, meta, stats).rsplit("\n", 1)
    with PRINT_LOCK:
        _erase_progress()
        stream.write(permanent + "\n")
        _draw_progress(stream, progress)


# Build the final run summary from the worker statistics.
def render_finished(stats, *, stopped=False, color=None):
    palette = Palette(enabled=color)
    status = palette.paint(tr("Stopped" if stopped else "Finished"), "1;33" if stopped else "1;32")
    stats = dict(stats, remaining=None)
    progress = render_progress(stats, palette, clock=True)
    return f"{status} · {progress} · {palette.dim(tr('elapsed'))} {format_time(stats['elapsed_s'])}"
