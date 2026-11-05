"""Fire the Claude Code routine that handles one Sentry alert.

Usage: python fire_claude.py <alert_ts>
"""

import json
import sys
import urllib.request

import config


def fire(ts: str, extra: str = "") -> str:
    text = (f"alert_ts={ts}\nchannel={config.ALERT_CHANNEL}\n"
            "A new Sentry alert was posted at the top level of the alerts channel. "
            "Read it and its thread with the Slack connector.")
    return fire_text(text + extra)


def fire_text(text: str) -> str:
    """POST text to the routine's /fire endpoint; returns the new session's URL."""
    req = urllib.request.Request(
        config.ALERT_ROUTINE_URL,
        data=json.dumps({"text": config.with_suffix(text)}).encode(),
        headers={
            "Authorization": "Bearer " + config.ALERT_TOKEN_FILE.read_text().strip(),
            "anthropic-beta": "experimental-cc-routine-2026-04-01",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get("claude_code_session_url", "")


if __name__ == "__main__":
    print(fire(sys.argv[1]))
