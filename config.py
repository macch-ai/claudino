"""claudino settings, read once from the environment.

systemd loads them from ~/.config/claude-relay/env (see .env.example). Every setting
except the Slack tokens is optional; leave the CLAUDINO_ALERT_* ones empty to turn
Sentry alert hand-over off.
"""

import os
import pwd
from pathlib import Path


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _path(name: str, default: str) -> Path:
    return Path(os.path.expanduser(_str(name, default)))


def _set(name: str) -> set:
    return {part.strip() for part in _str(name).split(",") if part.strip()}


# Who may talk to claudino: Slack user IDs, comma separated. Empty means nobody.
ALLOWED_USERS = _set("CLAUDINO_ALLOWED_USERS")

# Claude Code runs.
# Unix user that runs Claude (through `sudo -n -u`), so sessions cannot read the relay's Slack tokens.
# Empty runs Claude as the relay's own user. Set up with deploy/setup-relay-user.sh.
RUN_AS = _str("CLAUDINO_RUN_AS")


def _session_home() -> str:
    try:
        return pwd.getpwnam(RUN_AS).pw_dir if RUN_AS else os.path.expanduser("~")
    except KeyError:
        return os.path.expanduser("~")


def _session_path(path: str) -> str:
    """Expand ~ to the home of the user that runs Claude."""
    return _session_home() + path[1:] if path == "~" or path.startswith("~/") else path


WORKDIR = Path(_session_path(_str("CLAUDINO_WORKDIR", "~")))
PERMISSION_MODE = _str("CLAUDINO_PERMISSION_MODE", "bypassPermissions")
EXTRA_PATH = [_session_path(p) for p in _str("CLAUDINO_EXTRA_PATH", "~/.local/bin").split(":") if p]
PROMPT_SUFFIX = _str("CLAUDINO_PROMPT_SUFFIX")  # appended to every relayed request, e.g. "caveman mode"
# Thread status shown while a run works ("claudino <status>"). Haiku writes rotating lines for
# each request; LOADING_MESSAGES ("|" separated) show until they are ready. CLAUDINO_STATUS=off hides it.
STATUS = "" if _str("CLAUDINO_STATUS").lower() == "off" else _str("CLAUDINO_STATUS", "is working on it...")
LOADING_MESSAGES = [part.strip() for part in _str("CLAUDINO_LOADING_MESSAGES").split("|") if part.strip()][:10] or None
# Stream each run's tool calls and narration into the thread as Slack task cards; "off" hides them.
STREAM = _str("CLAUDINO_STREAM", "on").lower() != "off"
TIMEOUT_SECONDS = int(_str("CLAUDINO_TIMEOUT_SECONDS", str(50 * 60)))
MAX_RUNS = max(1, int(_str("CLAUDINO_MAX_RUNS", "3")))  # Claude runs at once; more threads wait their turn
FILES_KEEP_DAYS = int(_str("CLAUDINO_FILES_KEEP_DAYS", "14"))  # downloaded Slack files are deleted after this; 0 keeps them

# Local files.
CONFIG_DIR = _path("CLAUDINO_CONFIG_DIR", "~/.config/claude-relay")
STATE_DIR = _path("CLAUDINO_STATE_DIR", "~/.local/state/claude-relay")
LOCAL_INSTRUCTIONS_FILE = CONFIG_DIR / "instructions.md"  # private additions to the system prompt

# Optional Sentry alert hand-over to a Claude Code routine.
ALERT_CHANNEL = _str("CLAUDINO_ALERT_CHANNEL")  # channel where the Sentry app posts alerts
ALERT_BOT_USER = _str("CLAUDINO_ALERT_BOT_USER")  # the Sentry app's bot user ID
ALERT_ROUTINE_URL = _str("CLAUDINO_ALERT_ROUTINE_URL")  # the routine's /fire endpoint
ALERT_TOKEN_FILE = _path("CLAUDINO_ALERT_TOKEN_FILE", "~/.config/claude-alert-fire/token")
CLAUDE_APP_USER = _str("CLAUDINO_CLAUDE_APP_USER")  # Claude app user; its mentions in the alert channel count too


def alerts_enabled() -> bool:
    return bool(ALERT_CHANNEL and ALERT_BOT_USER and ALERT_ROUTINE_URL)


def with_suffix(prompt: str) -> str:
    return f"{prompt}\n\n{PROMPT_SUFFIX}" if PROMPT_SUFFIX else prompt
