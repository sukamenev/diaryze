"""Project paths, tunable processing defaults and transcript conventions."""

from contextlib import contextmanager
from datetime import timedelta
from datetime import timezone
import os
import re
from pathlib import Path


ROOT = os.getcwd()
DATA_DIR = "00_Diaryze_Data"
LEGACY_DATA_DIR = "Data"


# Use the canonical data directory, or the existing directory of a legacy archive.
def data_path(*parts, root=None):
    root = Path(ROOT if root is None else root)
    current = root / DATA_DIR
    legacy = root / LEGACY_DATA_DIR
    directory = legacy if not current.exists() and legacy.is_dir() else current
    return directory.joinpath(*parts)


# Use the new transcript tree, or the existing tree in an older archive.
def transcripts_dir(root=None):
    root = Path(ROOT if root is None else root)
    current = data_path("Text", root=root)
    legacy = root / "Text"
    return legacy if not current.exists() and legacy.is_dir() else current


# Set the shared archive paths for a CLI run.
@contextmanager
def use_archive(root):
    """Select the archive for the lifetime of one CLI run."""
    global ROOT
    previous = ROOT
    ROOT = str(Path(root).expanduser().resolve())
    try:
        yield ROOT
    finally:
        ROOT = previous

# User-adjustable defaults. CLI options override the corresponding values.
DEFAULT_MODEL = "gemini-4-hydrogen-medium"
DEFAULT_WORKERS = 5
BATCH_SHORT_MAX = 5
BATCH_SHORT_SEC = 300
MAX_SINGLE_SEC = 780
CHUNK_SEC = 600
OVERLAP_SEC = 15
REQUEST_TIMEOUT_SEC = 600
MAX_FILE_ATTEMPTS = 3
PART_REPAIR_ATTEMPTS = 3

# A 503 pauses this model for every worker in the current run.
MODEL_503_COOLDOWN_S = 300
MODEL_ERROR_POLICIES = {
    # scope: model = shared temporary pause, run = disabled, task = this task only.
    "503": {"scope": "model", "cooldown_s": MODEL_503_COOLDOWN_S, "threshold": 1},
    "server": {"scope": "model", "cooldown_s": 60, "threshold": 1},
    "connection": {"scope": "model", "cooldown_s": 60, "threshold": 1},
    "timeout": {"scope": "model", "cooldown_s": 1800, "threshold": 2},
    "quota": {"scope": "model", "cooldown_s": 300, "threshold": 1},
    "fatal": {"scope": "run"},
    "content": {"scope": "task"},
    "format": {"scope": "task"},
    "input": {"scope": "task"},
    "unknown": {"scope": "task"},
}
QUOTA_RESET_BUFFER_S = 20
QUOTA_MIN_WAIT_S = 30
MODEL_WAIT_POLL_S = 1
BACKEND_RETRY_BASE_S = 15
BACKEND_RETRY_MAX_S = 300

PROMPTS_DIR = Path(__file__).with_name("prompts")

TRANSCRIPT_SCHEMA_VERSION = "1.0"
NEEDS_RECOVERY = "needs_recovery"
NO_RECOVERABLE_AUDIO = {"empty_file", "no_audio_stream"}


TZ = timezone(timedelta(hours=3))


AGY_REAL = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli")


AGY_SHARED = ("antigravity-oauth-token", "settings.json", "installation_id")


CUSTOM_AGENT_NAME = "audio-reader"


EXCLUDED_DIRS = {DATA_DIR.lower(), LEGACY_DATA_DIR.lower(), "text", "docs", "scripts",
                 "work", ".work", "prompts", "promts", ".git", ".agents", ".codex",
                 ".aws", ".bin", "__pycache__", "@eadir", ".venv"}

AUDIO_EXTENSIONS = {
    ".3g2", ".3gp", ".aac", ".ac3", ".adts", ".aif", ".aifc", ".aiff",
    ".amr", ".ape", ".au", ".awb", ".avi", ".caf", ".dct", ".dss", ".flac",
    ".gsm", ".m2ts", ".m4a", ".m4b", ".mka", ".mkv", ".mov", ".mp2",
    ".mp3", ".mp4", ".mpeg", ".mpg", ".mts", ".oga", ".ogg", ".opus",
    ".ra", ".spx", ".tta", ".vob", ".w64", ".wav", ".weba", ".webm",
    ".wma", ".wmv", ".wv",
}


EXCLUDED_FILES = {".nomedia", ".ds_store", "thumbs.db", ".owner"}


VALID_SOURCES = {"speech", "audio", "filename", "user"}


TS_LINE_RE = re.compile(
    r"^\[(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{2,3})\]\s+"
    r"(?:\*\*(?P<sp_b>[^*\n\r:]+?)(?::\*\*|\*\*:?)|(?P<sp_p>[^*\n\r:]+?):)\s*(?P<text>.*)$"
)


QUOTA_ERR_PAT = re.compile(
    r"(quota|rate\s*limit|exhausted|too\s*many\s*requests|resource_exhausted|429|limit\s*reached)",
    re.I,
)


SAFETY_ERR_PAT = re.compile(
    r"(content safety filters|Prohibited Use Policy|prompt could not be submitted|"
    r"output could not be generated|blocked by.*safety|safety_filter)",
    re.I,
)


REFUSAL_TEXT_PAT = re.compile(
    r"(?:cannot|can't|unable to)\s+(?:transcribe|process|fulfill|assist)|"
    r"violates?\s+(?:our|the)?\s*policy|не\s+могу\s+(?:транскрибировать|расшифровать|обработать)",
    re.I,
)


UNAVAILABLE_503_PAT = re.compile(
    r'(?:\bcode\s*503\b|"error_code"\s*:\s*503\b|\bhttp\s*503\b|'
    r'\b503\s+service\s+unavailable\b|\bstatus[:= ]+503\b|\bunavailable\s*\([^)]*503\b)',
    re.I,
)


DEFAULT_FALLBACK_MODELS = "gemini-3.8-flash-medium"
