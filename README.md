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

- SSH: `facundo@hermes` over Tailscale SSH. The tailnet policy refuses other users.
- Slack tokens: `~/.config/claude-relay/env` (`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`), written by `scripts/set-tokens.sh`.
- Alert task API token: `~/.config/claude-alert-fire/token`.
- State: `~/.local/state/claude-relay/` (last alert seen, thread sessions, downloaded files).
- The Claude Code CLI must be logged in (`claude auth status`).
- Slack app bot scopes: channels:history, channels:read, chat:write, im:write, reactions:read, reactions:write, emoji:read, files:read.

## CI/CD (Depot CI)

`.depot/workflows/ci-cd.yml` runs the tests on every pull request and push. Every merge to `main` then deploys to hermes:

1. The deploy job joins the tailnet as `tag:ci` (Tailscale OAuth client in the Depot CI secrets `TS_OAUTH_CLIENT_ID` / `TS_OAUTH_SECRET`).
2. It calls `POST http://100.99.99.71:8765/deploy?sha=<commit>` on **claudino-deployer** (`deploy/deployer.py`), a small service that listens only on hermes' Tailscale address. Its only action: fast-forward `~/claude-relay` to `origin/main` (refusing any other commit) and run `scripts/deploy.sh`.
3. `scripts/deploy.sh` installs requirements, runs the tests, refreshes the systemd unit if it changed, restarts `claude-relay` and checks it reconnected to Slack. If any step fails it resets to the previous commit and restarts that.
4. The job checks `GET /health` reports the merged commit.

Manual deploy on hermes, if CI is unavailable: `cd ~/claude-relay && git pull --ff-only && scripts/deploy.sh "$(git rev-parse HEAD@{1})"`.

Changes to `deploy/deployer.py` itself take effect after `systemctl --user restart claudino-deployer` on hermes.

## First install on hermes

`uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt`, copy `deploy/*.service` to `~/.config/systemd/user/`, run `scripts/set-tokens.sh`, then `systemctl --user enable --now claude-relay claudino-deployer`.

## Tests

```sh
scripts/test.sh
```

It creates `.venv` with `uv` when missing, installs the requirements and runs `python -m unittest discover -s tests -t . -v`. Slack and Claude are faked, so tests run anywhere.

## Testing in Slack

- claudino's Slack user ID: `U0CARJPDVEU` (mention it as `<@U0CARJPDVEU>`).
- Test channel: `#hostgator-facundo-vps` (`C0CB808BPE3`). claudino is a member.
- After a deploy, mention claudino there and read its reply in the thread. Logs: `ssh hermes journalctl --user -u claude-relay -n 50`.
