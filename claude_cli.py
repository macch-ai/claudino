"""Run Claude Code headless: one resumable session per Slack thread."""

import json
import os
import signal
import subprocess
import threading
from pathlib import Path

import config

SESSIONS_FILE = config.STATE_DIR / "sessions.json"
INSTRUCTIONS_FILE = Path(__file__).with_name("claudino_instructions.md")

_lock = threading.Lock()
_runs_lock = threading.Lock()
_running = {}  # key -> the thread's running claude process
_stopped = set()  # keys whose run a user stopped


class Stopped(Exception):
    """A user stopped the thread's run."""


def _load() -> dict:
    return json.loads(SESSIONS_FILE.read_text()) if SESSIONS_FILE.exists() else {}


def get(key: str) -> dict:
    with _lock:
        return _load().get(key, {})


def update(key: str, **values) -> None:
    with _lock:
        data = _load()
        data.setdefault(key, {}).update(values)
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSIONS_FILE.write_text(json.dumps(data, indent=1))


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLACK_")}
    env["PATH"] = ":".join(config.EXTRA_PATH + [env.get("PATH", "/usr/bin:/bin")])
    return env


def permission_args() -> list:
    """bypassPermissions (the default) skips every permission check; any other mode is passed through."""
    if config.PERMISSION_MODE == "bypassPermissions":
        return ["--dangerously-skip-permissions"]
    return ["--permission-mode", config.PERMISSION_MODE]


def instructions() -> str:
    """The shared system prompt plus this machine's private additions, when there are any."""
    text = INSTRUCTIONS_FILE.read_text()
    if config.LOCAL_INSTRUCTIONS_FILE.exists():
        text += "\n" + config.LOCAL_INSTRUCTIONS_FILE.read_text()
    return text


def _tree(pid: int) -> list:
    """pid and every process below it. Claude's Bash tool starts its own process groups, so a
    group kill misses them; the parent links still lead to them."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
    children = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(f.isdigit() for f in fields):
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    found, todo = [], [pid]
    while todo:
        current = todo.pop()
        found.append(current)
        todo += children.get(current, [])
    return found


def _kill(proc: subprocess.Popen) -> None:
    """Kill claude and every tool it started, found before any of them dies and gets reparented."""
    for pid in _tree(proc.pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stop(key: str) -> None:
    """Stop the thread's run: kill it now, or make it stop before it starts."""
    with _runs_lock:
        _stopped.add(key)
        proc = _running.get(key)
    if proc:
        _kill(proc)


def clear_stop(key: str) -> None:
    """A new run starts for the thread: an earlier stop no longer applies."""
    with _runs_lock:
        _stopped.discard(key)


def _claude(prompt: str, session_id: str | None, key: str = "") -> dict:
    cmd = ["claude", "-p", config.with_suffix(prompt), "--output-format", "json", *permission_args(),
           "--append-system-prompt", instructions()]
    if session_id:
        cmd += ["--resume", session_id]
    with _runs_lock:
        if key in _stopped:
            _stopped.discard(key)
            raise Stopped()
        proc = subprocess.Popen(cmd, cwd=config.WORKDIR, env=_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        _running[key] = proc
    try:
        stdout, stderr = proc.communicate(timeout=config.TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill(proc)
        proc.communicate()
        raise
    finally:
        with _runs_lock:
            _running.pop(key, None)
            stopped = key in _stopped
            _stopped.discard(key)
    if stopped:
        raise Stopped()
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exited {proc.returncode}: {(stderr or stdout)[-500:]}")


def run(key: str, prompt: str) -> tuple[str, bool]:
    """Send prompt to the thread's session, resuming it when there is one. Returns (answer, is_error).
    Raises Stopped when a user stops the run; the thread keeps its session."""
    session_id = get(key).get("session_id")
    try:
        data = _claude(prompt, session_id, key)
    except RuntimeError:
        if not session_id:
            raise
        update(key, session_id=None, seen_ts="0")
        raise
    if data.get("session_id"):
        update(key, session_id=data["session_id"])
    return data.get("result") or "", bool(data.get("is_error"))
