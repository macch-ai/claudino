#!/usr/bin/env python3
"""Call one Slack Web API method as claudino's bot and print the response.

Slack's thread UI (statuses, loading lines, the stop button, streams) behaves differently from its
docs, and production deploys only from main. Use this on the server to try a call before merging:

    ssh <server> 'cd ~/claude-relay && .venv/bin/python scripts/slack-probe.py \\
        agents.sessions.setStatus channel_id=C123 thread_ts=1700000000.000100 status=processing'

Arguments after the method are name=value pairs, sent as strings; a value starting with [ or { is
sent as JSON (loading_messages='["one","two"]'). The bot token comes from SLACK_BOT_TOKEN or, when
unset, ~/.config/claude-relay/env. Run the matching reset call when you are done (for example status=active).
"""

import json
import os
import sys
from pathlib import Path

from slack_sdk import WebClient

ENV_FILE = Path("~/.config/claude-relay/env").expanduser()


def bot_token() -> str:
    if os.environ.get("SLACK_BOT_TOKEN"):
        return os.environ["SLACK_BOT_TOKEN"]
    for line in ENV_FILE.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "SLACK_BOT_TOKEN":
            return value.strip().strip('"')
    sys.exit(f"SLACK_BOT_TOKEN is not set and not in {ENV_FILE}")


def params(pairs: list) -> dict:
    out = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep:
            sys.exit(f"expected name=value, got {pair!r}")
        out[name] = json.loads(value) if value[:1] in ("[", "{") else value  # keeps ts like 1.000100 exact
    return out


def main(argv: list) -> None:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        sys.exit(__doc__)
    response = WebClient(token=bot_token()).api_call(argv[1], json=params(argv[2:]))
    print(json.dumps(response.data, indent=1))


if __name__ == "__main__":
    main(sys.argv)
