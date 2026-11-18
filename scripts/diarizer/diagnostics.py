"""Persistent per-run diagnostics, serialized across worker threads."""

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import threading
import traceback

from . import config
from .config import TZ


_LOCK = threading.Lock()
_LOG = None
_RUN_ID = None


@contextmanager
def run_log(directory=None):
    global _LOG, _RUN_ID
    directory = Path(directory) if directory else Path(config.ROOT) / "work" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(TZ).strftime('%Y%m%dT%H%M%S%f%z') + '-' + str(os.getpid())
    path = directory / ('diarize-' + run_id + '.jsonl')
    with path.open('x', encoding='utf-8') as stream:
        with _LOCK:
            previous = _LOG, _RUN_ID
            _LOG, _RUN_ID = stream, run_id
        try:
            yield path
        finally:
            with _LOCK:
                _LOG, _RUN_ID = previous


def record(event, *, items=(), error=None, **details):
    with _LOCK:
        if _LOG is None:
            return
        entry = {'schema_version': '1.0', 'time': datetime.now(TZ).isoformat(timespec='milliseconds'),
                 'run_id': _RUN_ID, 'event': event,
                 'files': [it['audio_rel'] for it in items], **details}
        if error is not None:
            entry['error'] = str(error)
            if isinstance(error, BaseException):
                entry['exception'] = type(error).__name__
                entry['traceback'] = ''.join(traceback.format_exception(error))
        _LOG.write(json.dumps(entry, ensure_ascii=False) + '\n')
        _LOG.flush()
