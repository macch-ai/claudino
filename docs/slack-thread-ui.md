# Slack thread UI: what we measured

Slack's agent UI in threads (working status, loading lines, stop button) often differs from its docs.
Each fact below was checked live in a test channel. Check a new call the same way before merging,
since production deploys only from `main`:

```bash
ssh <server> 'cd ~/claude-relay && .venv/bin/python scripts/slack-probe.py <method> name=value ...'
```

With the relay in its own user (`deploy/setup-relay-user.sh`), only that user can read the token, so a person runs it:
`cd /home/claudino/claude-relay && sudo -H -u claudino .venv/bin/python scripts/slack-probe.py ...`.

Open the thread in Slack web (Claude in Chrome) to see the result. Hover off the status to read
its resting text. Reset the thread when done (`agents.sessions.setStatus ... status=active`).

## Status calls

- `assistant.threads.setStatus` with `loading_messages` shows the rotating lines. Slack says
  "Stop isn't available for this agent", even with the `agent_session_stopped` subscription.
- `agents.sessions.setStatus status=processing` shows the stop button with a fixed label
  ("Stop claudino"). It hides `loading_messages` from any legacy call, before or after it.
  claudino keeps the rotating lines, so it uses only the legacy call.
- A thread's status no longer clears when claudino replies. Clear it explicitly: legacy
  `status=""`, or `agents.sessions.setStatus status=active` for a session the new API created
  (a legacy `""` leaves that one on "Stopping claudino...").

## Stopping a run

- `agent_session_stopped` arrives over Socket Mode once the event is subscribed in the app
  settings. No reinstall was needed: `chat:write` was already granted.
- Claude Code's Bash tool runs each command in its own process group. Kill the whole process
  tree, not the group, and make test fakes `setsid` the same way.

## Answer cards

- Answers can carry `data_table` and `data_visualization` blocks (2026 Block Kit). Slack allows at
  most 2 charts per message and 20,000 characters across a message's table cells; `cards.py` checks
  both. A post Slack refuses with blocks is sent again as text only.
- Reply buttons arrive as `block_actions` over Socket Mode once **Interactivity** is on. The click
  replaces the button row with "<@user> picked *label*" (`chat.update`), so a reply is sent once.
- Probe a card before merging a change to `cards.py`:
  `scripts/slack-probe.py chat.postMessage channel=C123 thread_ts=1700000000.000100 text=probe blocks='[...]'`.

## Live progress streams

- `chat.startStream` needs `recipient_user_id` and `recipient_team_id` in channels; claudino passes
  the first requester and its own team. Chunks are `task_update` cards (`timeline` mode) and
  `markdown_text` narration; one stream never mixes `markdown_text` arguments with `chunks`.
- A stream lasts at most 10 minutes and 12,000 text characters / 50 tasks. `progress.py` stops the
  stream at 9 minutes or near a cap and opens a new one, carrying tasks still in progress over.
- The stream's own message events must not end the run: `on_message` skips claudino messages while
  the thread's run streams and has not posted its answer, and every stream message `ts`.
- Probed live 2026-11-07 (#claudino-test): start, append (complete and error tasks) and stop all work
  in a channel thread. The stream message carries `user`, `streaming_state` and `blocks`; its `text`
  holds the narration plus the task titles. Probe again with
  `scripts/slack-probe.py chat.startStream channel=C123 thread_ts=... recipient_user_id=U... recipient_team_id=T... chunks='[{"type":"task_update","id":"t1","title":"Run tests","status":"in_progress"}]'`.
