#!/usr/bin/env python3
"""React and reply in the Sentry alert channel as claudino's bot, for the alert routine.

With CLAUDINO_RUN_AS, Claude sessions run as a user that cannot read the bot token. That user may
run this script as the relay's user through one sudo rule (deploy/setup-relay-user.sh), and the
script acts only in CLAUDINO_ALERT_CHANNEL:

    alert-slack.py add <channel> <ts> <emoji>    add a reaction (already_reacted counts as done)
    alert-slack.py get <channel> <ts>            print "emoji user1,user2" per reaction
    alert-slack.py post <channel> <thread_ts>    reply in a thread with the text on stdin; prints "ok <ts>"
    alert-slack.py whoami                        check the token

The text comes on stdin because sudo logs every command line.
"""

import os
import re
import sys
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

ENV_FILE = Path("~/.config/claude-relay/env").expanduser()
USAGE = "usage: alert-slack.py add|get|post <channel> <ts> [emoji] | whoami"


def settings() -> dict:
    """The relay's env file, overridden by the environment."""
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep and not name.lstrip().startswith("#"):
                values[name.strip()] = value.strip().strip('"').strip("'")
    values.update({k: v for k, v in os.environ.items() if k in ("SLACK_BOT_TOKEN", "CLAUDINO_ALERT_CHANNEL")})
    return values


def check(channel: str, ts: str, alert_channel: str) -> None:
    if not alert_channel or channel != alert_channel:
        sys.exit(f"alert-slack: only the alert channel ({alert_channel or 'unset'}) is allowed")
    if not re.fullmatch(r"\d+\.\d+", ts):
        sys.exit(f"alert-slack: bad timestamp {ts!r}")


def main(argv: list, stdin=sys.stdin) -> int:
    cmd, args = (argv[1], argv[2:]) if len(argv) > 1 else ("", [])
    env = settings()
    client = WebClient(token=env.get("SLACK_BOT_TOKEN", ""))
    try:
        if cmd == "whoami" and not args:
            r = client.auth_test()
            print(True, r.get("user"), r.get("user_id"))
            return 0
        if cmd not in ("add", "get", "post") or len(args) != (3 if cmd == "add" else 2):
            sys.exit(USAGE)
        channel, ts = args[0], args[1]
        check(channel, ts, env.get("CLAUDINO_ALERT_CHANNEL", ""))
        if cmd == "add":
            if not re.fullmatch(r"[a-z0-9_+\-']{1,100}", args[2]):
                sys.exit(f"alert-slack: bad emoji {args[2]!r}")
            try:
                client.reactions_add(channel=channel, timestamp=ts, name=args[2])
            except SlackApiError as e:
                if e.response.get("error") != "already_reacted":
                    raise
            print("ok")
        elif cmd == "get":
            message = client.reactions_get(channel=channel, timestamp=ts).get("message", {})
            for reaction in message.get("reactions", []):
                print(reaction["name"], ",".join(reaction.get("users", [])))
        else:
            text = stdin.read()
            if not text.strip():
                sys.exit("alert-slack: empty message")
            r = client.chat_postMessage(channel=channel, thread_ts=ts, text=text, unfurl_links=False)
            print("ok", r["ts"])
        return 0
    except SlackApiError as e:
        print(f"alert-slack: {e.response.get('error')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
