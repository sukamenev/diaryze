"""Quota reset parsing and coordinated, interruptible worker waits."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import re
import threading
import time

from .config import MODEL_ERROR_POLICIES, QUOTA_MIN_WAIT_S, QUOTA_RESET_BUFFER_S, TZ
from .console import emit
from .runtime import STOP_EVENT


QUOTA_WAIT_LOCK = threading.Lock()


_LAST_QUOTA_WAIT_DONE = [0.0]


def quota_wait_seconds(err_msg: str, now: datetime | None = None) -> int:
    """Wait until an absolute reset or a `Resets in 2h11m10s` duration."""
    now = now if now is not None else datetime.now(timezone.utc)
    rst_m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))", err_msg)
    if rst_m:
        try:
            reset_dt = datetime.fromisoformat(rst_m.group(1).replace("Z", "+00:00"))
            delta = (reset_dt - now).total_seconds()
            if delta >= 0:
                return max(QUOTA_MIN_WAIT_S, int(delta) + QUOTA_RESET_BUFFER_S)
        except ValueError:
            pass
    duration_m = re.search(r"\bresets?\s+in\s+((?:\d+\s*[dhms]\s*)+)(?!\w)", err_msg, re.I)
    if duration_m:
        units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        delta = sum(
            int(value) * units[unit.lower()]
            for value, unit in re.findall(r"(\d+)\s*([dhms])", duration_m.group(1), re.I)
        )
        return max(QUOTA_MIN_WAIT_S, delta + QUOTA_RESET_BUFFER_S)
    retry = re.search(r"retry[ _-]?(?:after|in|delay)[\"' ]*[:=]?\s*[\"']?(\d+)(?:\s*(?:s|seconds?))?\b", err_msg, re.I)
    if retry:
        return max(QUOTA_MIN_WAIT_S, int(retry[1]) + QUOTA_RESET_BUFFER_S)
    return MODEL_ERROR_POLICIES['quota']['cooldown_s']


def wait_for_quota_reset(err_msg: str):
    """Coordinate across worker threads to sleep when rate/quota limit is hit."""
    hit_at = time.monotonic()
    with QUOTA_WAIT_LOCK:
        if STOP_EVENT.is_set():
            raise InterruptedError("stopped while waiting for quota")
        # Skip duplicate sleep if another worker already waited after this error started
        if _LAST_QUOTA_WAIT_DONE[0] > hit_at:
            return
        sleep_s = quota_wait_seconds(err_msg)
        resume_at = datetime.now(TZ) + timedelta(seconds=sleep_s)
        emit(
            f"\n[{datetime.now(TZ).strftime('%H:%M:%S')}] Лимит квоты ({err_msg[:140]}). "
            f"Жду {sleep_s}с (до {resume_at:%Y-%m-%d %H:%M:%S %z})...",
            flush=True,
        )
        end_t = time.monotonic() + sleep_s
        while time.monotonic() < end_t:
            if STOP_EVENT.wait(min(15, max(0, end_t - time.monotonic()))):
                raise InterruptedError("stopped while waiting for quota")
        _LAST_QUOTA_WAIT_DONE[0] = time.monotonic()
