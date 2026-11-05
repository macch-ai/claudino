"""claudino: Slack relay that hands work to Claude Code on your own server.

- New top-level Sentry alerts in the alert channel fire a Claude Code routine (optional).
- A message that mentions @claudino in any public channel it belongs to starts a
  mention run. In a thread where claudino already took part, teammates' replies
  don't need the mention (unless they mention someone else instead).
- While a run for a thread is working, new requests there are queued and handled
  together by one next run once claudino replies, so work is never duplicated.
- Each request gets an emoji reaction as soon as it is seen.

On start it catches up on Sentry alerts posted since the last one it saw.
Settings live in config.py.
"""

import json
import logging
import subprocess
import os
import re
import threading
import time
import urllib.request

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

import claude_cli
import config
import fire_claude

MAX_CATCH_UP_SECONDS = 24 * 60 * 60  # never replay more than 1 day after an outage
BUSY_TIMEOUT_SECONDS = 60 * 60  # a run that never replied stops blocking its thread
LAST_ALERT_FILE = config.STATE_DIR / "last_alert_ts"
FILES_DIR = config.STATE_DIR / "files"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude-relay")

app = App(token=os.environ["SLACK_BOT_TOKEN"], ignoring_self_events_enabled=False)
_me = app.client.auth_test()
RELAY_USER_ID, RELAY_BOT_ID = _me["user_id"], _me.get("bot_id")

lock = threading.Lock()
busy = {}  # thread_ts -> {"since": float, "channel": str, "pending": [(ts, user)]}
active_threads = set()  # (channel, thread_ts) where claudino took part


def is_sentry_alert(message: dict) -> bool:
    """A top-level message posted by the Sentry app in the alert channel."""
    is_reply = message.get("thread_ts") not in (None, message.get("ts"))
    return (config.alerts_enabled() and message.get("user") == config.ALERT_BOT_USER
            and message.get("channel", config.ALERT_CHANNEL) == config.ALERT_CHANNEL and not is_reply)


def already_handed_over(ts: str) -> bool:
    """Claude already took the alert or someone already mentioned it in the thread."""
    replies = app.client.conversations_replies(channel=config.ALERT_CHANNEL, ts=ts, limit=50)["messages"]
    parent, thread = replies[0], replies[1:]
    if any(r.get("name") == "eyes" and RELAY_USER_ID in r.get("users", []) for r in parent.get("reactions") or []):
        return True
    return any(m.get("user") and m.get("user") in (RELAY_USER_ID, config.CLAUDE_APP_USER) for m in thread)


def hand_over(ts: str) -> None:
    try:
        if already_handed_over(ts):
            log.info("skip %s: already handed over", ts)
            remember(ts)
            return
        active_threads.add((config.ALERT_CHANNEL, ts))
        url = fire_claude.fire(ts)
        log.info("fired Claude task for %s: %s", ts, url)
        remember(ts)
    except SlackApiError as error:
        log.error("hand over %s failed: %s", ts, error.response.get("error"))
    except Exception as error:  # fire endpoint errors
        log.error("fire for %s failed: %s", ts, error)


def is_from_claudino(message: dict) -> bool:
    return message.get("user") == RELAY_USER_ID or (RELAY_BOT_ID is not None and message.get("bot_id") == RELAY_BOT_ID)


def claudino_in_thread(channel: str, thread_ts: str) -> bool:
    if (channel, thread_ts) in active_threads:
        return True
    try:
        messages = app.client.conversations_replies(channel=channel, ts=thread_ts, limit=200)["messages"]
    except SlackApiError as error:
        log.warning("read thread %s failed: %s", thread_ts, error.response.get("error"))
        return False
    seen = any(is_from_claudino(m) or f"<@{RELAY_USER_ID}>" in (m.get("text") or "") for m in messages)
    if seen:
        active_threads.add((channel, thread_ts))
    return seen


def from_trusted_agent(message: dict) -> bool:
    """A trusted agent's own message (not an edit or join) that mentions claudino."""
    return (message.get("app_id") in config.TRUSTED_AGENT_APPS
            and message.get("subtype") in (None, "bot_message")
            and bool(message.get("user"))
            and f"<@{RELAY_USER_ID}>" in (message.get("text") or ""))


def addressed_to_claudino(message: dict) -> bool:
    text = message.get("text") or ""
    channel = message.get("channel")
    if f"<@{RELAY_USER_ID}>" in text:
        return True
    if config.CLAUDE_APP_USER and channel == config.ALERT_CHANNEL and f"<@{config.CLAUDE_APP_USER}>" in text:
        return True
    thread_ts = message.get("thread_ts")
    if not thread_ts or thread_ts == message.get("ts"):
        return False
    others = set(re.findall(r"<@([UW][A-Z0-9]+)", text)) - {RELAY_USER_ID, config.CLAUDE_APP_USER}
    if others:
        return False  # talking to someone else ("Sent using @Claude" footers don't count)
    return claudino_in_thread(channel, thread_ts)


EMOJI_PROMPT = (
    "You are claudino, an engineering assistant in this Slack workspace. A teammate just sent you the message below. "
    "Pick the one Slack emoji reaction a thoughtful teammate would add to show they saw it and how it lands "
    "(for example: eyes, saluting_face, rocket, fire, rotating_light, thinking_face, mag, ok_hand, muscle, "
    "white_check_mark, pray, joy, sweat_smile, hammer_and_wrench, chart_with_upwards_trend, coffee). "
    "Any emoji from Slack's standard set is fine. Reply with only its short name, without colons.\n\nMessage:\n"
)


_custom_emoji = {"names": [], "at": 0.0}


def custom_emoji() -> list:
    """The workspace's custom emoji names, refreshed at most once an hour."""
    if time.time() - _custom_emoji["at"] > 3600:
        try:
            emoji = app.client.emoji_list()["emoji"]
            _custom_emoji["names"] = sorted(emoji)
        except SlackApiError as error:
            log.warning("emoji.list failed: %s", error.response.get("error"))
        _custom_emoji["at"] = time.time()
    return _custom_emoji["names"]


def emoji_prompt(text: str) -> str:
    custom = custom_emoji()
    extra = ("This workspace also has custom emoji you can use (prefer one when it fits well): "
             + ", ".join(custom) + ". ") if custom else ""
    return EMOJI_PROMPT.replace("Reply with only", extra + "Reply with only") + text


def pick_emoji(text: str) -> str:
    text = re.sub(r"\s*\*?Sent using\*?\s*<@[A-Z0-9]+(\|[^>]*)?>\s*$", "", text or "").strip()[:2000]
    try:
        proc = subprocess.run(
            ["claude", "-p", emoji_prompt(text), "--model", "haiku", "--tools", "", "--output-format", "json"],
            cwd="/tmp", env=claude_cli._env(), capture_output=True, text=True, timeout=20)
        name = json.loads(proc.stdout).get("result", "").strip().strip(":").lower()
        if re.fullmatch(r"[a-z0-9_+\-']{1,60}", name):
            return name
        log.warning("emoji pick returned %r", name[:80])
    except Exception as error:
        log.warning("emoji pick failed: %s", error)
    return "eyes"


def react(channel: str, ts: str, name: str) -> bool:
    try:
        app.client.reactions_add(channel=channel, timestamp=ts, name=name)
        return True
    except SlackApiError as error:
        code = error.response.get("error")
        if code == "already_reacted":
            return True
        log.warning("reaction %s on %s failed: %s", name, ts, code)
        return False


def ack(channel: str, ts: str, text: str = "") -> None:
    """React on the request with an emoji Haiku picks for it (falls back to :eyes:)."""
    def go() -> None:
        name = pick_emoji(text)
        if not react(channel, ts, name) and name != "eyes":
            name = "eyes"
            react(channel, ts, name)
        log.info("reacted :%s: on %s", name, ts)
    threading.Thread(target=go, daemon=True).start()


def dispatch(message: dict) -> None:
    channel, ts, user = message["channel"], message["ts"], message["user"]
    thread_ts = message.get("thread_ts") or ts
    ack(channel, ts, message.get("text") or "")
    with lock:
        state = busy.get(thread_ts)
        if state and time.time() - state["since"] < BUSY_TIMEOUT_SECONDS:
            state["pending"].append((ts, user))
            log.info("queued %s: a run for thread %s is still working", ts, thread_ts)
            return
        busy[thread_ts] = {"since": time.time(), "channel": channel, "pending": []}
    start(channel, thread_ts, [(ts, user)])


SEEN_WINDOW_SECONDS = 30  # fallback when a run never recorded what it saw


def start(channel: str, thread_ts: str, requests: list) -> None:
    active_threads.add((channel, thread_ts))
    threading.Thread(target=work, args=(channel, thread_ts, requests), daemon=True).start()


def file_note(f: dict, ts: str) -> str:
    """Download a Slack file for the session and say where it is."""
    try:
        if f.get("file_access") == "check_file_info" or not f.get("url_private"):
            f = app.client.files_info(file=f["id"])["file"]
    except SlackApiError as error:
        return f"[file {f.get('id')}: could not read it ({error.response.get('error')})]"
    name = f.get("name") or f.get("id") or "file"
    url = f.get("url_private_download") or f.get("url_private")
    if not url:
        return f"[file {name}: no download link]"
    dest = FILES_DIR / f"{ts}_{f.get('id')}_{re.sub(r'[^A-Za-z0-9._-]', '_', name)}"
    if not dest.exists():
        try:
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["SLACK_BOT_TOKEN"]})
            with urllib.request.urlopen(req, timeout=60) as resp:
                if "text/html" in resp.headers.get("Content-Type", ""):
                    return f"[file {name}: Slack refused the download (claudino lacks files:read)]"
                data = resp.read()
        except Exception as error:
            return f"[file {name}: download failed ({error})]"
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return f"[attached file {name} ({f.get('mimetype', '?')}) saved at {dest}; open it with the Read tool]"


def render(m: dict) -> str:
    """A message's text plus its files and forwarded/shared messages."""
    parts = [m.get("text") or ""]
    for f in m.get("files") or []:
        parts.append(file_note(f, m["ts"]))
    for a in m.get("attachments") or []:
        who = a.get("author_name") or a.get("author_subname") or a.get("channel_name") or ""
        body = a.get("text") or a.get("fallback") or ""
        if body or who:
            parts.append(f"[forwarded/shared message from {who}]: {body}")
        for f in a.get("files") or []:
            parts.append(file_note(f, m["ts"]))
    return "\n".join(p for p in parts if p)


def thread_prompt(channel: str, thread_ts: str, requests: list, key: str) -> tuple[str, str]:
    """The thread messages the session has not seen yet, plus who is asking for what."""
    seen_ts = claude_cli.get(key).get("seen_ts") or "0"
    messages = app.client.conversations_replies(channel=channel, ts=thread_ts, limit=200)["messages"]
    fresh = seen_ts == "0"
    new = [m for m in messages if float(m["ts"]) > float(seen_ts) and (fresh or not is_from_claudino(m))]
    lines = []
    for m in new:
        author = "claudino (you)" if is_from_claudino(m) else f"<@{m.get('user') or m.get('bot_id')}>"
        lines.append(f"[{m['ts']}] {author}: {render(m)}")
    requesters = " ".join(sorted({f"<@{user}>" for _, user in requests}))
    header = (f"Slack channel {channel}, thread {thread_ts}. Requesters: {requesters}. "
              f"Requests: the messages with ts {', '.join(ts for ts, _ in requests)}.\n"
              + ("Thread so far:\n" if fresh else "New messages in the thread since your last answer:\n"))
    last_seen = max([m["ts"] for m in messages] + [seen_ts], key=float)
    return header + "\n".join(lines), last_seen


def post(channel: str, thread_ts: str, text: str) -> None:
    try:
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text[:39000], unfurl_links=False)
    except SlackApiError as error:
        log.error("post in %s failed: %s", thread_ts, error.response.get("error"))
        reply_done(thread_ts)


def work(channel: str, thread_ts: str, requests: list) -> None:
    key = f"{channel}:{thread_ts}"
    requesters = " ".join(sorted({f"<@{user}>" for _, user in requests}))
    try:
        for attempt in range(2):
            prompt, last_seen = thread_prompt(channel, thread_ts, requests, key)
            with lock:
                if thread_ts in busy:
                    busy[thread_ts]["seen_until"] = float(last_seen)
            try:
                started = time.time()
                answer, is_error = claude_cli.run(key, prompt)
                claude_cli.update(key, seen_ts=last_seen)
                log.info("session %s answered in %.0fs (error=%s)", key, time.time() - started, is_error)
                break
            except RuntimeError as error:
                if attempt:
                    raise
                log.warning("session %s failed, retrying with a new one: %s", key, error)
        post(channel, thread_ts, answer or f"{requesters} the Claude session ended without an answer.")
    except Exception as error:
        log.error("session %s failed: %s", key, error)
        post(channel, thread_ts, f"{requesters} the Claude session failed: `{str(error)[:300]}`")


def reply_done(thread_ts: str) -> None:
    """claudino replied in the thread: the run is over; start queued requests."""
    with lock:
        state = busy.pop(thread_ts, None)
        if not state:
            return
        seen_until = state.get("seen_until", state.get("fired_at", 0) + SEEN_WINDOW_SECONDS)
        pending = [(ts, user) for ts, user in state["pending"] if float(ts) > seen_until]
        if len(pending) < len(state["pending"]):
            log.info("dropped %d queued requests the run already saw", len(state["pending"]) - len(pending))
        if not pending:
            return
        state["pending"] = pending
        busy[thread_ts] = {"since": time.time(), "channel": state["channel"], "pending": []}
    start(state["channel"], thread_ts, state["pending"])


@app.event("message")
def on_message(event: dict) -> None:
    channel = event.get("channel")
    if is_sentry_alert(event):
        hand_over(event["ts"])
        return
    if is_from_claudino(event):
        if event.get("thread_ts"):
            reply_done(event["thread_ts"])
        return
    if from_trusted_agent(event):
        log.info("request %s from agent %s", event.get("ts"), event.get("app_id"))
        dispatch(event)
        return
    if event.get("subtype") or event.get("bot_id") or not event.get("user"):
        return
    if not addressed_to_claudino(event):
        return
    if event["user"] not in config.ALLOWED_USERS:
        log.info("skip %s from %s: not allowed", event.get("ts"), event.get("user"))
        return
    dispatch(event)


@app.event("reaction_added")
def on_reaction(event: dict) -> None:
    """Acknowledge the event so Bolt does not log it as unhandled."""


def remember(ts: str) -> None:
    LAST_ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not LAST_ALERT_FILE.exists() or float(ts) > float(LAST_ALERT_FILE.read_text()):
        LAST_ALERT_FILE.write_text(ts)


def catch_up() -> None:
    if not config.alerts_enabled():
        return
    if not LAST_ALERT_FILE.exists():
        remember(str(time.time()))
        return
    oldest = str(max(float(LAST_ALERT_FILE.read_text()), time.time() - MAX_CATCH_UP_SECONDS))
    history = app.client.conversations_history(channel=config.ALERT_CHANNEL, oldest=oldest, limit=200)["messages"]
    for message in reversed(history):
        if is_sentry_alert(message) and message["ts"] != oldest:
            hand_over(message["ts"])


if __name__ == "__main__":
    if not config.ALLOWED_USERS:
        log.warning("CLAUDINO_ALLOWED_USERS is empty: claudino will ignore every message")
    log.info("alerts %s, permission mode %s", "on" if config.alerts_enabled() else "off", config.PERMISSION_MODE)
    catch_up()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
