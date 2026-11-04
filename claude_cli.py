"""Run Claude Code headless on hermes (auto permission mode): one resumable session per Slack thread."""

import json
import os
import subprocess
import threading
from pathlib import Path

WORKDIR = Path.home() / "code"
STATE_FILE = Path.home() / ".local/state/claude-relay/sessions.json"
INSTRUCTIONS_FILE = Path(__file__).with_name("claudino_instructions.md")
TIMEOUT_SECONDS = 50 * 60
EXTRA_PATH = [str(Path.home() / ".local/bin"), str(Path.home() / ".nvm/versions/node/v24.13.0/bin")]

_lock = threading.Lock()


def _load() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def get(key: str) -> dict:
    with _lock:
        return _load().get(key, {})


def update(key: str, **values) -> None:
    with _lock:
        data = _load()
        data.setdefault(key, {}).update(values)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, indent=1))


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLACK_")}
    env["PATH"] = ":".join(EXTRA_PATH + [env.get("PATH", "/usr/bin:/bin")])
    return env


def _claude(prompt: str, session_id: str | None) -> dict:
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--permission-mode", "auto",
           "--append-system-prompt", INSTRUCTIONS_FILE.read_text()]
    if session_id:
        cmd += ["--resume", session_id]
    proc = subprocess.run(cmd, cwd=WORKDIR, env=_env(), capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}")


def run(key: str, prompt: str) -> tuple[str, bool]:
    """Send prompt to the thread's session, resuming it when there is one. Returns (answer, is_error)."""
    session_id = get(key).get("session_id")
    try:
        data = _claude(prompt, session_id)
    except RuntimeError:
        if not session_id:
            raise
        update(key, session_id=None, seen_ts="0")
        raise
    if data.get("session_id"):
        update(key, session_id=data["session_id"])
    return data.get("result") or "", bool(data.get("is_error"))
