# claudino

Slack relay that hands Macch's engineering work to Claude Code on **hermes**.

> **Moving off hermes.** The replacement lives in `worker/` (Cloudflare Workers, Free plan) and `desktop/` (menu bar app). Mentions run on the **requester's own Mac** with their Claude Team seat; hermes and Tailscale are not needed. Until the cutover below, hermes keeps answering exactly as described further down.

## claudino on Cloudflare + the desktop app

- **Worker** (`worker/`): receives Slack events over HTTP (`/slack/events`, signed), and routes each request to the first requester's Mac. One Durable Object per teammate (`UserDO`: the Mac's hibernating WebSocket, its token, queued threads), per thread (`ThreadDO`: one run at a time, later requests queued, retry once with a fresh session) and one for pairing (`PairingDO`). Sentry alerts fire the same Claude Code routine as hermes. If a teammate's Mac is off, their threads wait and run when it reconnects (one notice per thread); runs never go to another teammate's Mac. Slack files reach the Mac through `/files/:id`, so the bot token stays in Cloudflare. The system prompt is `machine_ai_instructions.md` (hermes keeps `claudino_instructions.md`).
- **Desktop app** (`desktop/`): links with "Iniciar sesión con Slack" (browser, Sign in with Slack, after a page naming the Mac being linked) or `@Machine AI vincular <code>`. Runs `claude -p` locally (auto permission mode; `ANTHROPIC_API_KEY` dropped so it never bills an API key), picks the reaction emoji with Haiku, and runs **automations** (scheduled or `@Machine AI ejecutá <name>`, optionally posting to a channel). State in `~/.config/claudino/`.

Run the app: `cd desktop && npm install && npm start` (Node 22+, `claude auth status` must say `claude.ai`). Dev aids: `CLAUDINO_HUB_URL` points it at another Worker (e.g. `ws://127.0.0.1:8787` for `wrangler dev`); `CLAUDINO_SCREENSHOT=/path.png` (+ `CLAUDINO_SCREENSHOT_PAGE`, `CLAUDINO_SCREENSHOT_DELAY_MS`) saves a capture of the window.

Worker: `cd worker && npm install && npm test` (unit tests), `npx wrangler dev` (local; secrets in `worker/.dev.vars`), `npx wrangler deploy`.

### Setup and cutover (once)

1. `npx wrangler login` with the Cloudflare account that hosts it, then `cd worker && npx wrangler deploy` (prints the `workers.dev` URL; keep `DEFAULT_URL` in `desktop/src/hub.ts` in sync).
2. Secrets, typed by a person: `npx wrangler secret put` for `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `ROUTINE_TOKEN`.
3. Slack app (api.slack.com/apps → claudino): OAuth & Permissions → redirect URL `<worker URL>/login/callback` and user scopes `openid`, `profile`.
4. Each teammate opens the app and links it.
5. Cutover, in one go: Event Subscriptions on with Request URL `<worker URL>/slack/events` (bot event `message.channels`), then Socket Mode off. From then on hermes gets no events; stop `claude-relay` there when convenient.

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
python -m unittest discover -s tests -t . -v
```

Slack and Claude are faked, so tests run anywhere with the requirements installed.
