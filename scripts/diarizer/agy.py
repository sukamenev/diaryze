"""Isolated agy sessions, subprocess lifecycle and response envelopes."""

import glob
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading

from . import config
from .config import (
    AGY_REAL,
    AGY_SHARED,
    CUSTOM_AGENT_NAME,
    SAFETY_ERR_PAT,
)
from .errors import normalize_error
from .platform import IS_WINDOWS
from .platform.processes import run_capture
from .prompt_files import load_prompt


AGY_HOME_LOCK = threading.Lock()


# Keep the existing audio reader instructions available in either agy home.
def _install_audio_reader(home):
    path = Path(home) / ".gemini" / "config" / "agents" / CUSTOM_AGENT_NAME / "agent.md"
    content = load_prompt("audio-reader.md") + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


# Prepare a separate agent home with shared credentials and the optional reader profile.
def ensure_agy_home(use_custom_agent: bool) -> str | None:
    """Create isolated temp HOME for agy to keep /resume clean and load minimal agent."""
    if IS_WINDOWS:
        if use_custom_agent:
            with AGY_HOME_LOCK:
                _install_audio_reader(Path.home())
        return None
    # Do not require antigravity-oauth-token: macOS signs in through Keychain,
    # so the file can be absent while agy is authenticated. Link whatever shared
    # files exist and install the reader profile regardless.
    home = os.path.join(tempfile.gettempdir(), f"diaryze-agy-{os.getuid()}")
    with AGY_HOME_LOCK:
        os.makedirs(home, mode=0o700, exist_ok=True)
        st = os.lstat(home)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return None
        cli = os.path.join(home, ".gemini", "antigravity-cli")
        os.makedirs(cli, exist_ok=True)
        for name in AGY_SHARED:
            src, dst = os.path.join(AGY_REAL, name), os.path.join(cli, name)
            if not os.path.exists(src):
                continue
            if os.path.islink(dst) and os.readlink(dst) == src:
                continue
            if os.path.lexists(dst):
                os.unlink(dst)
            os.symlink(src, dst)
        if use_custom_agent:
            _install_audio_reader(home)
        return home


# Remove local artifacts belonging to a completed agent conversation.
def agy_forget(home: str | None, conv_id: str | None):
    if not home or not conv_id or not re.fullmatch(r"[\w-]+", conv_id):
        return
    cli = os.path.join(home, ".gemini", "antigravity-cli")
    for sub in ("conversations", "brain", "annotations", "presence"):
        for p in glob.glob(os.path.join(cli, sub, conv_id + "*")):
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# Check whether stdout contains a complete agent response envelope.
def agy_envelope_complete(text: str) -> bool:
    try:
        env = json.loads(text)
    except ValueError:
        return False
    return isinstance(env, dict) and "status" in env


# Send a prompt to the agent with a timeout and optional conversation continuity.
def run_agy(prompt: str, model: str, timeout: int, use_custom_agent: bool, conversation_id: str | None = None):
    """Run `agy -p` with JSON envelope handling and hang protection."""
    home = ensure_agy_home(use_custom_agent)
    env = dict(os.environ, HOME=home) if home else None
    cmd = [
        "agy",
        "--sandbox",
        "--model", model,
        "--output-format", "json",
        "--disable-slash-commands",
        "--print-timeout", f"{timeout}s",
    ]
    if use_custom_agent and (home or IS_WINDOWS):
        cmd += ["--agent", CUSTOM_AGENT_NAME, "--dangerously-skip-permissions"]
    if conversation_id:
        cmd += ["--conversation", conversation_id]

    try:
        result = run_capture(
            cmd, input=prompt, timeout=timeout + config.AGY_TIMEOUT_GRACE_SEC,
            finish_when=agy_envelope_complete, env=env, cwd=config.ROOT,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"agy timeout ({timeout}s)") from exc
    return result.returncode, result.stdout, result.stderr, home



# Separate transcript text, usage, and session ID from provider errors.
def extract_envelope_error_and_usage(rc: int, stdout: str, stderr: str, *, detect_safety=True):
    env = None
    try:
        env = json.loads(stdout)
    except Exception:
        pass
    env = env if isinstance(env, dict) else {}
    usage = env.get("usage") or {}
    usage = usage if isinstance(usage, dict) else {}
    conv_id = env.get("conversation_id")
    # A complete SUCCESS envelope is the final answer even if agy lingers after
    # printing it: the hang guard then kills the process and rc is -9. Partial
    # and timed-out answers carry status ERROR, so the exit code alone must not
    # discard a ready transcript.
    if isinstance(env, dict) and env.get("status") == "SUCCESS":
        text = str(env.get("result") or env.get("response") or "")
        if detect_safety and SAFETY_ERR_PAT.search(text[:500]):
            return f"SAFETY: {text.strip()[:240]}", "", usage, conv_id
        return None, text, usage, conv_id
    msg = ""
    if isinstance(env, dict):
        msg = str(env.get("error") or env.get("message") or env.get("response") or "")
    msg = (msg + "\n" + stderr + "\n" + stdout[:500]).strip()
    return normalize_error(msg, rc), "", usage, conv_id
