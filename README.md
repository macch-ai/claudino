# claudino

Slack relay that hands Macch's engineering work to Claude Code on **hermes**.

- **Sentry alerts**: each new top-level Sentry alert in #production-errors fires the "claudino: alertas y menciones (hermes)" scheduled task (`fire_claude.py`), which investigates and replies in the alert thread.
- **Mentions**: `@claudino` in any public channel it belongs to opens a Claude Code session on hermes (`claude_cli.py`, auto permission mode). Each Slack thread keeps one session that is resumed on every new message, and the relay posts the answer.
- **Threads**: once claudino is part of a thread, teammates' replies reach it without a mention (unless they mention someone else). Requests sent while a run is working are queued and handled together afterwards.
- **Reactions**: every request gets an emoji chosen by Haiku (standard or the workspace's custom emoji), with :eyes: as fallback.
- **Files**: images and files in the thread, and forwarded messages, are passed to the session.
- Only Facundo and Salvador can talk to it (`ALLOWED_USERS` in `relay.py`).

## Files

| Path | What it is |
| --- | --- |
| `relay.py` | Slack Socket Mode app: routing, queue, reactions, posting answers |
| `claude_cli.py` | Runs `claude -p` per thread and remembers each thread's session |
| `claudino_instructions.md` | System prompt appended to every session |
| `fire_claude.py` | Fires the alert scheduled task through its API trigger |
| `deploy/claude-relay.service` | systemd user unit (copy of the installed one) |
| `scripts/set-tokens.sh` | Stores the Slack tokens on hermes |

## Configuration on hermes (never committed)

- Slack tokens: `~/.config/claude-relay/env` (`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`), written by `scripts/set-tokens.sh`.
- Alert task API token: `~/.config/claude-alert-fire/token`.
- State: `~/.local/state/claude-relay/` (last alert seen, thread sessions, downloaded files).
- The Claude Code CLI must be logged in (`claude auth status`).
- Slack app bot scopes: channels:history, channels:read, chat:write, im:write, reactions:read, reactions:write, emoji:read, files:read.

## Deploy

The checkout lives at `~/claude-relay` on hermes:

```sh
cd ~/claude-relay && git pull && .venv/bin/pip install -r requirements.txt
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user restart claude-relay && journalctl --user -u claude-relay -f
```

First install: `uv venv .venv && .venv/bin/pip install -r requirements.txt`, copy `deploy/claude-relay.service` to `~/.config/systemd/user/`, run `scripts/set-tokens.sh`, then `systemctl --user enable --now claude-relay`.

## Tests

```sh
python -m unittest discover -s tests -t . -v
```

Slack and Claude are faked, so tests run anywhere with the requirements installed.
