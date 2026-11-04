# claudino

Slack relay that hands Macch's engineering work to Claude Code on **hermes**.

- **Sentry alerts**: each new top-level Sentry alert in #production-errors fires the "claudino: alertas y menciones (hermes)" scheduled task (`fire_claude.py`), which investigates and replies in the alert thread.
- **Mentions**: `@claudino` in any public channel it belongs to runs a Claude Code session on the **requester's own Mac**, through the claudino desktop app (auto permission mode), so it uses that teammate's Claude Team seat instead of an account on hermes. Each Slack thread keeps one session on that Mac that is resumed on every new message, and the relay posts the answer. If the requester's Mac is not connected, claudino says so in the thread and runs nothing.
- **Desktop app** (`desktop/`): menu bar app that connects to `wss://claudino.macch.ai`, a Cloudflare Tunnel to the hub on hermes (`desktop_hub.py`); no VPN needed. Link it with "Iniciar sesión con Slack" (browser, Sign in with Slack) or `@claudino vincular <code>`; unlink it from the app. While a teammate's Mac is off, their mention runs wait in a queue on hermes and run when it reconnects. It also runs **automations**: saved prompts on a schedule, or on demand with `@claudino ejecutá <name>`, optionally posting results to a Slack channel.
- **Threads**: once claudino is part of a thread, teammates' replies reach it without a mention (unless they mention someone else). Requests sent while a run is working are queued and handled together afterwards.
- **Reactions**: every request gets an emoji chosen by Haiku (standard or the workspace's custom emoji), with :eyes: as fallback.
- **Files**: images and files in the thread, and forwarded messages, are passed to the session.
- Only Facundo and Salvador can talk to it (`ALLOWED_USERS` in `relay.py`).

## Files

| Path | What it is |
| --- | --- |
| `relay.py` | Slack Socket Mode app: routing, queue, reactions, posting answers, `vincular` / `ejecutá` commands |
| `desktop_hub.py` | Hub on hermes' localhost:8766, published as `claudino.macch.ai` by Cloudflare Tunnel: pairs desktop apps (browser login pages too) and sends them runs |
| `slack_login.py` | Sign in with Slack (OpenID Connect) for the browser pairing |
| `deploy/cloudflared.*` | systemd unit and config for the tunnel |
| `desktop/` | Electron desktop app (menu bar): pairing, runs Claude Code locally, tasks, automations |
| `claude_cli.py` | Thread state (what each session already saw) and the `claude -p` helper used for emoji picks |
| `claudino_instructions.md` | System prompt appended to every session |
| `fire_claude.py` | Fires the alert scheduled task through its API trigger |
| `deploy/claude-relay.service` | systemd user unit (copy of the installed one) |
| `scripts/set-tokens.sh` | Stores the Slack tokens on hermes |

## Configuration on hermes (never committed)

- Slack tokens: `~/.config/claude-relay/env` (`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`), written by `scripts/set-tokens.sh`.
- Alert task API token: `~/.config/claude-alert-fire/token`.
- Desktop app tokens: `~/.config/claude-relay/desktop-tokens.json` (`{"U...": "token"}`, mode 600), written by pairing. Delete a line to unlink that teammate's Mac. Queued runs for Macs that are off: `~/.local/state/claude-relay/queued.json`.
- The hub listens on `DESKTOP_HUB_HOST:DESKTOP_HUB_PORT` (default `127.0.0.1:8766`); only cloudflared reaches it.
- Sign in with Slack: `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET` in `~/.config/claude-relay/env` (optional `CLAUDINO_PUBLIC_URL`, default `https://claudino.macch.ai`). Without them the app only offers `@claudino vincular <code>`.

### Cloudflare Tunnel (once)

1. On hermes: install `cloudflared`, then `cloudflared tunnel login` and `cloudflared tunnel create claudino`.
2. `cloudflared tunnel route dns claudino claudino.macch.ai`.
3. Copy `deploy/cloudflared.yml` to `~/.cloudflared/claudino.yml` and fill in the tunnel ID and home path.
4. Copy `deploy/cloudflared.service` to `~/.config/systemd/user/` and `systemctl --user enable --now cloudflared`.
5. Check: `curl https://claudino.macch.ai/health` answers `ok`.

### Sign in with Slack (once)

In the claudino Slack app (api.slack.com/apps): OAuth & Permissions -> add the redirect URL `https://claudino.macch.ai/login/callback` and the user scopes `openid`, `profile`. Put the app's client ID and secret in `~/.config/claude-relay/env` and restart `claude-relay`.
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

## Desktop app

Needs Node 22+ and a logged-in Claude Code CLI (`claude auth status` should say `claude.ai`). No VPN: it reaches hermes at `wss://claudino.macch.ai`.

```sh
cd desktop && npm install && npm start
```

Then open Conexión and send the command it shows in Slack. Local state lives in `~/.config/claudino/` (`desktop.json` with the hub token, `sessions.json`, `automations.json`). Scheduled automations only run while the app is open and the Mac is awake; a missed daily run is skipped, not replayed.

Dev aids: `CLAUDINO_HUB_URL=ws://127.0.0.1:8766` points the app at a local hub; `CLAUDINO_SCREENSHOT=/path.png` (plus `CLAUDINO_SCREENSHOT_PAGE`, `CLAUDINO_SCREENSHOT_DELAY_MS`) saves a capture of the window.

## Tests

```sh
python -m unittest discover -s tests -t . -v
```

Slack and Claude are faked, so tests run anywhere with the requirements installed.
