"""claudino settings, read once from the environment.

systemd loads them from ~/.config/claude-relay/env (see .env.example). Every setting
except the Slack tokens is optional; leave the CLAUDINO_ALERT_* ones empty to turn
Sentry alert hand-over off.
"""

import os
from pathlib import Path


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _path(name: str, default: str) -> Path:
    return Path(os.path.expanduser(_str(name, default)))


def _set(name: str) -> set:
    return {part.strip() for part in _str(name).split(",") if part.strip()}


# Who may talk to claudino: Slack user IDs, comma separated. Empty means nobody.
ALLOWED_USERS = _set("CLAUDINO_ALLOWED_USERS")
# Other agents' Slack app IDs that may delegate to claudino by mentioning it explicitly (never by
# thread follow-ups). They must ignore agent messages themselves, so claudino's answer can't start a loop.
TRUSTED_AGENT_APPS = _set("CLAUDINO_TRUSTED_AGENT_APPS")

# Claude Code runs.
WORKDIR = _path("CLAUDINO_WORKDIR", "~")
PERMISSION_MODE = _str("CLAUDINO_PERMISSION_MODE", "bypassPermissions")
EXTRA_PATH = [os.path.expanduser(p) for p in _str("CLAUDINO_EXTRA_PATH", "~/.local/bin").split(":") if p]
PROMPT_SUFFIX = _str("CLAUDINO_PROMPT_SUFFIX")  # appended to every relayed request, e.g. "caveman mode"
TIMEOUT_SECONDS = int(_str("CLAUDINO_TIMEOUT_SECONDS", str(50 * 60)))

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
