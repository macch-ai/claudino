"""claudino: Slack relay that hands work to Claude Code on hermes.

- New top-level Sentry alerts in #production-errors start the alert task.
- A message that mentions @claudino in any public channel it belongs to starts a
  mention run. In a thread where claudino already took part, teammates' replies
  don't need the mention (unless they mention someone else instead).
- Mention runs execute on the first requester's Mac through their claudino desktop
  app (desktop_hub.py), so they use that teammate's own Claude Team seat.
- While a run for a thread is working, new requests there are queued and handled
  together by one next run once claudino replies, so work is never duplicated.
- Each request gets an :eyes: reaction as soon as it is seen.

On start it catches up on Sentry alerts posted since the last one it saw.
"""

import json
import logging
import subprocess
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

import claude_cli
import desktop_hub
import fire_claude

CHANNEL_ID = "C0C7YJPATEK"  # #production-errors
SENTRY_USER_ID = "U0C8J4GER8W"  # Sentry app's bot user
CLAUDE_USER_ID = "U0C9799P96K"  # Claude (Claude Tag) app; mentions in #production-errors count too
RELAY_APP_ID = "A0CARJB2Q2C"  # claudino
ALLOWED_USERS = {"U0C5CGDBP8E", "U0C68RT2SEL"}  # Facundo, Salvador
MAX_CATCH_UP_SECONDS = 24 * 60 * 60  # never replay more than 1 day after an outage
BUSY_TIMEOUT_SECONDS = 60 * 60  # a run that never replied stops blocking its thread
STATE_FILE = Path.home() / ".local/state/claude-relay/last_alert_ts"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude-relay")

app = App(token=os.environ["SLACK_BOT_TOKEN"], ignoring_self_events_enabled=False)
_auth = app.client.auth_test()
RELAY_USER_ID = _auth["user_id"]
# Mention runs go to the requester's own Mac; while it is off they wait in a queue on disk.
hub = desktop_hub.Hub(on_post=lambda *args: post_automation(*args), on_connect=lambda user: resume_queued(user),
                      allowed_users=ALLOWED_USERS, team_id=_auth.get("team_id", ""))
QUEUE_FILE = Path.home() / ".local/state/claude-relay/queued.json"  # {user: {"C..:ts": {channel, thread_ts, requests}}}

lock = threading.Lock()
busy = {}  # thread_ts -> {"since": float, "channel": str, "pending": [(ts, user)]}
active_threads = set()  # (channel, thread_ts) where claudino took part


def is_sentry_alert(message: dict) -> bool:
    """A top-level message posted by the Sentry app."""
    is_reply = message.get("thread_ts") not in (None, message.get("ts"))
    return message.get("user") == SENTRY_USER_ID and not is_reply


def already_handed_over(ts: str) -> bool:
    """Claude already took the alert or someone already mentioned it in the thread."""
    replies = app.client.conversations_replies(channel=CHANNEL_ID, ts=ts, limit=50)["messages"]
    parent, thread = replies[0], replies[1:]
    if any(r.get("name") == "eyes" and RELAY_USER_ID in r.get("users", []) for r in parent.get("reactions") or []):
        return True
    return any(m.get("user") in (RELAY_USER_ID, CLAUDE_USER_ID) for m in thread)


def hand_over(ts: str) -> None:
    try:
        if already_handed_over(ts):
            log.info("skip %s: already handed over", ts)
            remember(ts)
            return
        active_threads.add((CHANNEL_ID, ts))
        url = fire_claude.fire(ts)
        log.info("fired Claude task for %s: %s", ts, url)
        remember(ts)
    except SlackApiError as error:
        log.error("hand over %s failed: %s", ts, error.response.get("error"))
    except Exception as error:  # fire endpoint errors
        log.error("fire for %s failed: %s", ts, error)


def is_from_claudino(message: dict) -> bool:
    return message.get("user") == RELAY_USER_ID or message.get("app_id") == RELAY_APP_ID


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


def addressed_to_claudino(message: dict) -> bool:
    text = message.get("text") or ""
    channel = message.get("channel")
    if f"<@{RELAY_USER_ID}>" in text:
        return True
    if channel == CHANNEL_ID and f"<@{CLAUDE_USER_ID}>" in text:
        return True
    thread_ts = message.get("thread_ts")
    if not thread_ts or thread_ts == message.get("ts"):
        return False
    others = set(re.findall(r"<@([UW][A-Z0-9]+)", text)) - {RELAY_USER_ID, CLAUDE_USER_ID}
    if others:
        return False  # talking to someone else ("Sent using @Claude" footers don't count)
    return claudino_in_thread(channel, thread_ts)


def thread_is_alert(thread_ts: str) -> bool:
    parent = app.client.conversations_replies(channel=CHANNEL_ID, ts=thread_ts, limit=1)["messages"][0]
    return is_sentry_alert(parent)


EMOJI_PROMPT = (
    "You are claudino, an engineering assistant in Macch's Slack. A teammate just sent you the message below. "
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


FILES_DIR = Path.home() / ".local/state/claude-relay/files"


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
    """Run the requests on the first requester's Mac, through their claudino desktop app."""
    key = f"{channel}:{thread_ts}"
    owner = requests[0][1]
    requesters = " ".join(sorted({f"<@{user}>" for _, user in requests}))
    try:
        for attempt in range(2):
            prompt, last_seen = thread_prompt(channel, thread_ts, requests, key)
            with lock:
                if thread_ts in busy:
                    busy[thread_ts]["seen_until"] = float(last_seen)
            try:
                started = time.time()
                answer, is_error = hub.run(owner, key, prompt, claude_cli.INSTRUCTIONS_FILE.read_text(),
                                           claude_cli.TIMEOUT_SECONDS)
                claude_cli.update(key, seen_ts=last_seen)
                log.info("session %s answered on %s's Mac in %.0fs (error=%s)",
                         key, owner, time.time() - started, is_error)
                break
            except desktop_hub.RunFailed as error:
                if attempt:
                    raise
                claude_cli.update(key, seen_ts="0")  # the app drops a failed session: resend the whole thread
                log.warning("session %s failed, retrying with a new one: %s", key, error)
        post(channel, thread_ts, answer or f"{requesters} la sesión terminó sin respuesta.")
    except desktop_hub.Offline:
        log.info("session %s queued: no desktop app connected for %s", key, owner)
        if enqueue(owner, channel, thread_ts, requests):
            post(channel, thread_ts, f"<@{owner}> tu Mac está apagada o sin la app de MACCH: "
                                     "lo hago apenas se conecte.")
        else:
            reply_done(thread_ts)  # already announced: just free the thread
    except Exception as error:
        log.error("session %s failed: %s", key, error)
        post(channel, thread_ts, f"{requesters} falló la sesión de Claude en la Mac de <@{owner}>: "
                                 f"`{str(error)[:300]}`")


_queue_lock = threading.Lock()


def _load_queue() -> dict:
    return json.loads(QUEUE_FILE.read_text()) if QUEUE_FILE.exists() else {}


def _save_queue(queue: dict) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(queue, indent=1))


def enqueue(user: str, channel: str, thread_ts: str, requests: list) -> bool:
    """Keep a thread's requests until user's Mac connects. True when the thread was not queued yet."""
    key = f"{channel}:{thread_ts}"
    with _queue_lock:
        queue = _load_queue()
        threads = queue.setdefault(user, {})
        new = key not in threads
        entry = threads.setdefault(key, {"channel": channel, "thread_ts": thread_ts, "requests": []})
        known = {ts for ts, _ in entry["requests"]}
        entry["requests"] += [[ts, u] for ts, u in requests if ts not in known]
        _save_queue(queue)
    return new


def resume_queued(user: str) -> None:
    """user's Mac just connected: run the threads that waited for it."""
    with _queue_lock:
        queue = _load_queue()
        threads = queue.pop(user, {})
        _save_queue(queue)
    for entry in threads.values():
        channel, thread_ts = entry["channel"], entry["thread_ts"]
        requests = [tuple(r) for r in entry["requests"]]
        log.info("resuming queued thread %s:%s for %s", channel, thread_ts, user)
        with lock:
            state = busy.get(thread_ts)
            if state and time.time() - state["since"] < BUSY_TIMEOUT_SECONDS:
                state["pending"].extend(requests)
                continue
            busy[thread_ts] = {"since": time.time(), "channel": channel, "pending": []}
        start(channel, thread_ts, requests)


AUTOMATION_COMMAND = re.compile(r"^ejecut[aá]\s+(.+)$", re.IGNORECASE | re.DOTALL)
PAIR_COMMAND = re.compile(r"^vincular\s+`?([A-Za-z0-9]{3}-?[A-Za-z0-9]{3})`?\.?$", re.IGNORECASE)


def without_mentions(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+(\|[^>]*)?>", "", text or "").strip()


def automation_command(text: str) -> str | None:
    """The automation name in "@claudino ejecutá <name>", or None for any other message."""
    match = AUTOMATION_COMMAND.match(without_mentions(text))
    return match.group(1).strip(" `\"'*_.") or None if match else None


def pair_command(text: str) -> str | None:
    """The code in "@claudino vincular 4F7-K2Q", or None for any other message."""
    match = PAIR_COMMAND.match(without_mentions(text))
    return match.group(1) if match else None


def pair_desktop(channel: str, thread_ts: str, user: str, code: str, profile: dict) -> None:
    """Link the desktop app showing this code to the teammate who sent it (Slack vouches for who they are)."""
    details = {
        "name": profile.get("real_name") or profile.get("display_name") or "",
        "handle": profile.get("display_name") or profile.get("name") or "",
        "avatar": profile.get("image_72") or "",
    }
    try:
        ok = hub.complete_pair(code, user, details)
    except Exception as error:
        log.error("pairing for %s failed: %s", user, error)
        ok = False
    post(channel, thread_ts, f"<@{user}> listo: tu Mac quedó vinculada a claudino." if ok else
         f"<@{user}> ese código no existe o venció: pedí uno nuevo en la app (Conexión).")


def run_automation(channel: str, thread_ts: str, user: str, name: str) -> None:
    """Run one of the requester's automations on their Mac and answer in the thread."""
    try:
        answer, is_error = hub.run_automation(user, name, claude_cli.TIMEOUT_SECONDS)
        log.info("automation %r ran on %s's Mac (error=%s)", name, user, is_error)
        post(channel, thread_ts, f"<@{user}> *{name}*:\n{answer or 'terminó sin respuesta.'}")
    except desktop_hub.Offline:
        post(channel, thread_ts, f"<@{user}> tu Mac no está conectada a claudino: abrí la app de escritorio.")
    except Exception as error:
        log.error("automation %r for %s failed: %s", name, user, error)
        post(channel, thread_ts, f"<@{user}> falló la automatización *{name}*: `{str(error)[:300]}`")


def post_automation(user: str, channel: str, name: str, text: str) -> None:
    """A scheduled automation on a teammate's Mac finished: publish its result where they configured."""
    try:
        app.client.chat_postMessage(channel=channel, text=f"*{name}* · automatización de <@{user}>\n{text}"[:39000],
                                    unfurl_links=False)
    except SlackApiError as error:
        log.error("automation %r post in %s failed: %s", name, channel, error.response.get("error"))


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
    if channel == CHANNEL_ID and is_sentry_alert(event):
        hand_over(event["ts"])
        return
    if is_from_claudino(event):
        if event.get("thread_ts"):
            reply_done(event["thread_ts"])
        return
    if event.get("subtype") or event.get("bot_id") or not event.get("user"):
        return
    if not addressed_to_claudino(event):
        return
    if event["user"] not in ALLOWED_USERS:
        log.info("skip %s from %s: not allowed", event.get("ts"), event.get("user"))
        return
    code = pair_command(event.get("text") or "")
    if code:
        ack(channel, event["ts"], event.get("text") or "")
        thread_ts = event.get("thread_ts") or event["ts"]
        threading.Thread(target=pair_desktop, daemon=True,
                         args=(channel, thread_ts, event["user"], code, event.get("user_profile") or {})).start()
        return
    name = automation_command(event.get("text") or "")
    if name:
        ack(channel, event["ts"], event.get("text") or "")
        thread_ts = event.get("thread_ts") or event["ts"]
        threading.Thread(target=run_automation, args=(channel, thread_ts, event["user"], name), daemon=True).start()
        return
    dispatch(event)


@app.event("reaction_added")
def on_reaction(event: dict) -> None:
    pass


def remember(ts: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists() or float(ts) > float(STATE_FILE.read_text()):
        STATE_FILE.write_text(ts)


def catch_up() -> None:
    if not STATE_FILE.exists():
        remember(str(time.time()))
        return
    oldest = str(max(float(STATE_FILE.read_text()), time.time() - MAX_CATCH_UP_SECONDS))
    history = app.client.conversations_history(channel=CHANNEL_ID, oldest=oldest, limit=200)["messages"]
    for message in reversed(history):
        if is_sentry_alert(message) and message["ts"] != oldest:
            hand_over(message["ts"])


if __name__ == "__main__":
    hub.start()
    catch_up()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
