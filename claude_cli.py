"""Run Claude Code headless: one resumable session per Slack thread."""

import json
import os
import signal
import subprocess
import threading
import uuid
from pathlib import Path

import config

SESSIONS_FILE = config.STATE_DIR / "sessions.json"
INSTRUCTIONS_FILE = Path(__file__).with_name("claudino_instructions.md")
# The system prompt goes to Claude in a file and the prompt on stdin: under CLAUDINO_RUN_AS, sudo logs
# every command line to the journal and auth.log, and Slack threads do not belong there.
SYSTEM_PROMPT_FILE = config.STATE_DIR / "system-prompt.md"

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


def command(args: list, cwd) -> tuple[list, str]:
    """The command line and working directory that run args in cwd as the user that runs Claude.
    With CLAUDINO_RUN_AS it goes through sudo, which starts from a clean environment."""
    if not config.RUN_AS:
        return args, str(cwd)
    path = ":".join(config.EXTRA_PATH + ["/usr/local/bin", "/usr/bin", "/bin"])
    return (["sudo", "-n", "-H", "-u", config.RUN_AS, "--", "env", f"PATH={path}",
             "sh", "-c", 'cd "$0" && exec "$@"', str(cwd), *args], "/")


def permission_args() -> list:
    """bypassPermissions (the default) skips every permission check; any other mode is passed through."""
    if config.PERMISSION_MODE == "bypassPermissions":
        return ["--dangerously-skip-permissions"]
    return ["--permission-mode", config.PERMISSION_MODE]


def model_args() -> list:
    """--model and --effort when CLAUDINO_MODEL / CLAUDINO_EFFORT set them."""
    args = ["--model", config.MODEL] if config.MODEL else []
    return args + (["--effort", config.EFFORT] if config.EFFORT else [])


def main_model(data: dict) -> str:
    """The model that did most of the run: Claude's result lists every model it used (Haiku helpers too)."""
    usage = data.get("modelUsage") or {}
    return max(usage, key=lambda name: usage[name].get("costUSD") or 0, default="")


def instructions() -> str:
    """The shared system prompt plus this machine's private additions, when there are any."""
    text = INSTRUCTIONS_FILE.read_text()
    if config.LOCAL_INSTRUCTIONS_FILE.exists():
        text += "\n" + config.LOCAL_INSTRUCTIONS_FILE.read_text()
    return text


def instructions_file() -> str:
    """instructions() in a file the user that runs Claude can read (the state dir is group-readable)."""
    text = instructions()
    if not SYSTEM_PROMPT_FILE.exists() or SYSTEM_PROMPT_FILE.read_text() != text:
        SYSTEM_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SYSTEM_PROMPT_FILE.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.chmod(0o640)
        tmp.replace(SYSTEM_PROMPT_FILE)
    return str(SYSTEM_PROMPT_FILE)


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
    """Kill claude and every tool it started, found before any of them dies and gets reparented.
    Processes of CLAUDINO_RUN_AS are killed through sudo (sudo itself exits with its child)."""
    theirs = []
    for pid in _tree(proc.pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            theirs.append(str(pid))
    if theirs and config.RUN_AS:
        subprocess.run(["sudo", "-n", "-u", config.RUN_AS, "--", "kill", "-KILL", *theirs],
                       capture_output=True, timeout=30)


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


def _claude(prompt: str, session_id: str | None, key: str = "", new: bool = False) -> dict:
    cmd = ["claude", "-p", "--output-format", "json", *permission_args(), *model_args(),
           "--append-system-prompt-file", instructions_file()]
    if session_id:
        cmd += ["--session-id" if new else "--resume", session_id]
    with _runs_lock:
        if key in _stopped:
            _stopped.discard(key)
            raise Stopped()
        cmd, cwd = command(cmd, config.WORKDIR)
        proc = subprocess.Popen(cmd, cwd=cwd, env=_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        _running[key] = proc
    try:
        stdout, stderr = proc.communicate(config.with_suffix(prompt), timeout=config.TIMEOUT_SECONDS)
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


def run(key: str, prompt: str) -> tuple[str, bool, str]:
    """Send prompt to the thread's session, resuming it when there is one. Returns (answer, is_error, model).
    Raises Stopped when a user stops the run; the thread keeps its session.
    A new session's ID is saved before it starts, so a run a restart cuts off can be resumed."""
    session_id = get(key).get("session_id")
    new = not session_id
    if new:
        session_id = str(uuid.uuid4())
        update(key, session_id=session_id)
    try:
        data = _claude(prompt, session_id, key, new)
    except RuntimeError:
        update(key, session_id=None, seen_ts="0")
        raise
    if data.get("session_id"):
        update(key, session_id=data["session_id"])
    return data.get("result") or "", bool(data.get("is_error")), main_model(data)
