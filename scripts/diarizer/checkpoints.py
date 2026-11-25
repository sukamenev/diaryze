"""Accepted parts of long recordings, kept until the final transcript is written.

A checkpoint belongs to the source audio hash and to the exact part layout
(offset and duration of every part). If either differs, it is ignored.

``CHECKPOINT_VERSION`` is a single compatibility switch: bump it whenever the
prompt, validation or saved-part format changes and old parts must not be
reused. Prompt hashes are deliberately not used by default.
"""

import json
import os
from pathlib import Path
import tempfile

from . import config
from .config import TRANSCRIPT_SCHEMA_VERSION
from .diagnostics import record


CHECKPOINT_VERSION = 1


# Locate the archive directory for resumable transcription checkpoints.
def checkpoint_dir() -> Path:
    return Path(config.ROOT) / "work" / "checkpoints"


# Record the part boundaries that determine checkpoint compatibility.
def _layout(item):
    return [[p["part"], p["offset_ms"], p["duration_ms"]] for p in item["parts"]]


class LongCheckpoint:
    # Restore the contiguous prefix of accepted parts for a compatible recording.
    def __init__(self, item):
        self.item = item
        self.path = checkpoint_dir() / f"{item['sha256']}.json"
        self.parts = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            record("checkpoint_ignored", items=[item], error=exc, path=str(self.path))
            return
        if (not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION
                or data.get("sha256") != item["sha256"]
                or data.get("schema_version") != TRANSCRIPT_SCHEMA_VERSION
                or data.get("layout") != _layout(item)
                or not isinstance(data.get("parts"), dict)):
            record("checkpoint_ignored", items=[item], path=str(self.path), reason="layout or hash differs")
            return
        # Only a contiguous prefix is usable: later parts depend on earlier speaker keys.
        for number in range(1, len(item["parts"]) + 1):
            saved = data["parts"].get(str(number))
            if not isinstance(saved, dict):
                break
            self.parts[number] = saved

    # Retrieve a previously accepted part by number.
    def get(self, number):
        return self.parts.get(number)

    # Invalidate the specified part and all later parts that depend on it.
    def drop_from(self, number):
        for key in [k for k in self.parts if k >= number]:
            del self.parts[key]

    # Write the accepted part and updated checkpoint atomically.
    def save(self, number, *, speakers, lines, model, summary, warning=None, voice_sample=None):
        """Store one accepted part atomically, invalidating any later parts."""
        self.drop_from(number)
        self.parts[number] = {
            "speakers": speakers or {},
            "lines": [list(line) for line in lines],
            "model": model,
            "summary": summary,
            "warning": warning,
            "voice_sample": voice_sample,
        }
        data = {
            "version": CHECKPOINT_VERSION,
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "audio": self.item["audio_rel"],
            "sha256": self.item["sha256"],
            "layout": _layout(self.item),
            "parts": {str(k): v for k, v in sorted(self.parts.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".checkpoint-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        record("checkpoint_saved", items=[self.item], part=number, model=model, path=str(self.path))

    # Remove the checkpoint once the recording has been published.
    def discard(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            record("checkpoint_discard_error", items=[self.item], error=exc, path=str(self.path))
