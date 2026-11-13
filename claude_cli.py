"""Run Claude Code headless: one resumable session per Slack thread."""

import json
import logging
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
# claudino's own mark on commits and PRs, replacing Claude Code's "Generated with Claude Code" one, so the
# team can tell claudino's work apart from their own Claude Code sessions.
ATTRIBUTION = {"commit": "Co-authored-by: claudino <noreply@anthropic.com>",
               "pr": "🤖 Generated with claudino, Claude Code in Slack"}

log = logging.getLogger("claude-relay")
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


def settings_args() -> list:
    """Claude Code settings every run gets on top of the user's own."""
    return ["--settings", json.dumps({"attribution": ATTRIBUTION})]


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


def _claude(prompt: str, session_id: str | None, key: str = "", new: bool = False, on_event=None) -> dict:
    """Run claude and return its result event. Every other event of its stream-json output goes to
    on_event as it arrives (assistant turns, tool results), so the relay can show the work live."""
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", *permission_args(), *model_args(),
           *settings_args(), "--append-system-prompt-file", instructions_file()]
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
    timed_out = threading.Event()

    def time_out() -> None:
        timed_out.set()
        _kill(proc)

    timer = threading.Timer(config.TIMEOUT_SECONDS, time_out)
    timer.daemon = True
    stderr = []
    reader = threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True)
    result, last = None, ""
    try:
        timer.start()
        reader.start()
        try:
            proc.stdin.write(config.with_suffix(prompt))
            proc.stdin.close()
        except BrokenPipeError:
            pass
        for line in proc.stdout:
            last = line or last
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                result = event
            elif on_event:
                try:
                    on_event(event)
                except Exception as error:  # showing progress must never break the run
                    log.warning("progress for %s failed: %s", key, error)
        proc.wait()
        reader.join(timeout=5)
    finally:
        timer.cancel()
        with _runs_lock:
            _running.pop(key, None)
            stopped = key in _stopped
            _stopped.discard(key)
    if stopped:
        raise Stopped()
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, config.TIMEOUT_SECONDS)
    if result is None:
        raise RuntimeError(f"claude exited {proc.returncode}: {(''.join(stderr) or last)[-500:]}")
    return result


def run(key: str, prompt: str, on_event=None) -> tuple[str, bool, str]:
    """Send prompt to the thread's session, resuming it when there is one. Returns (answer, is_error, model).
    on_event gets the run's stream-json events as they arrive.
    Raises Stopped when a user stops the run; the thread keeps its session.
    A new session's ID is saved before it starts, so a run a restart cuts off can be resumed."""
    session_id = get(key).get("session_id")
    new = not session_id
    if new:
        session_id = str(uuid.uuid4())
        update(key, session_id=session_id)
    try:
        data = _claude(prompt, session_id, key, new, on_event)
    except RuntimeError:
        update(key, session_id=None, seen_ts="0")
        raise
    if data.get("session_id"):
        update(key, session_id=data["session_id"])
    return data.get("result") or "", bool(data.get("is_error")), main_model(data)
