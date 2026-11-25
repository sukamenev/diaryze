"""Classify provider errors separately from task content and response formatting."""

import errno
import re
import subprocess

from .config import QUOTA_ERR_PAT, SAFETY_ERR_PAT, UNAVAILABLE_503_PAT
from .ui import tr


class ModelRefused(RuntimeError):
    # Keep the refusal message and the models that actually tried the task.
    def __init__(self, reason, models_tried=None):
        super().__init__(reason)
        self.reason = reason
        self.models_tried = list(models_tried or [])


class ModelTaskFailed(RuntimeError):
    # Preserve the diagnostic used in transcript notes independently of the UI.
    def __init__(self, details):
        super().__init__(f"Цепочка моделей не обработала задание: {details}")
        self.details = details


class MediaInputError(RuntimeError):
    """The provider rejected the media bytes; changing models cannot repair them."""


class ModelsUnavailable(RuntimeError):
    # Record whether model exhaustion affects the entire run.
    def __init__(self, message, *, all_disabled=False):
        super().__init__(message)
        self.all_disabled = all_disabled


PREFIXES = {
    "SAFETY": "content", "QUOTA": "quota", "ERR_503": "503",
    "ERR_SERVER": "server", "ERR_CONNECTION": "connection", "ERR_TIMEOUT": "timeout",
    "FATAL": "fatal", "FORMAT": "format", "INPUT": "input", "MEDIA": "media",
}
LABELS = {
    "503": "overload / 503", "server": "server failure", "connection": "connection failure",
    "timeout": "timeout", "quota": "quota", "fatal": "model or access error",
    "content": "content refusal", "format": "invalid response format",
    "input": "unsupported input or request size", "media": "damaged media",
    "unknown": "unknown error",
}
_FATAL = re.compile(
    r"invalid[ _-]?api[ _-]?key|incorrect[ _-]?api[ _-]?key|unauthorized|authentication failed|"
    r"unknown model|invalid model|model[^\n]{0,100}(?:not found|does not exist|not available for|no access)|"
    r"(?:no|not have|denied) access to (?:the |this )?model", re.I)
_NETWORK = re.compile(
    r"connection (?:reset|refused|aborted|closed)|network (?:error|unreachable)|"
    r"econnreset|econnrefused|ehostunreach|enetunreach|eai_again|"
    r"stream was interrupted|remote end closed|temporary failure in name resolution", re.I)
_TIMEOUT = re.compile(r"\btimeout\b|timed out|deadline[_ ]exceeded", re.I)
_OVERLOAD = re.compile(r"\boverloaded\b|\bat capacity\b|no capacity available|servers.*high traffic", re.I)
_CONTENT = re.compile(r"content policy|safety policy|policy violation|blocked for safety", re.I)
_MEDIA = re.compile(
    r"(?:file|media|audio)(?: or media)?(?: data)? is empty, corrupted, or malformed", re.I)
_INPUT_SIZE = re.compile(
    r"context (?:window|length|limit)|maximum (?:context|length|tokens)|token limit|"
    r"(?:input|prompt|request|audio|file)[^\n]{0,40}too (?:long|large)|"
    r"request entity too large|unsupported audio", re.I)
_HTTP_CODE = re.compile(
    r"(?:\b(?:http(?:/[\d.]+)?|code|status(?:_code)?|error_code)\s*[\"':=() ]*|\bHTTPError:\s*)"
    r"(401|403|404|429|500|502|503|504)\b", re.I)


# Localize the task error heading while preserving raw provider diagnostics.
def ui_error(error):
    if isinstance(error, ModelTaskFailed):
        return tr("The model chain could not process the job: {details}", details=error.details)
    return str(error)


# Map provider failures to the categories used by retry policies.
def classify_error(error):
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "connection"
    if isinstance(error, OSError) and error.errno in {
        errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED,
        errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN,
    }:
        return "connection"
    text = str(error)
    for prefix, kind in PREFIXES.items():
        if re.match(re.escape(prefix) + r"\b", text):
            return kind
    if SAFETY_ERR_PAT.search(text) or _CONTENT.search(text):
        return "content"
    codes = set(_HTTP_CODE.findall(text))
    if codes & {"401", "403", "404"} or _FATAL.search(text):
        return "fatal"
    if _MEDIA.search(text):
        return "media"
    if _INPUT_SIZE.search(text):
        return "input"
    if "429" in codes:
        return "quota"
    if UNAVAILABLE_503_PAT.search(text) or _OVERLOAD.search(text):
        return "503"
    if QUOTA_ERR_PAT.search(text):
        return "quota"
    if codes & {"500", "502", "504"} or re.search(r"\bINTERNAL\s*\(|\bBAD_GATEWAY\b", text):
        return "server"
    if _TIMEOUT.search(text):
        return "timeout"
    if _NETWORK.search(text):
        return "connection"
    return "unknown"


# Turn provider output and process status into a consistent error message.
def normalize_error(error, rc=None):
    kind = classify_error(error)
    prefix = next((p for p, k in PREFIXES.items() if k == kind), "ERR")
    code = f"(rc={rc})" if rc is not None and prefix.startswith('ERR') else ""
    # Keep reset times and the full provider reason for policy and diagnostics.
    return f"{prefix}{code}: {error}"
