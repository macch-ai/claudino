"""Fire the Claude Code scheduled task that handles one Sentry alert."""
import json, sys, urllib.request
from pathlib import Path

from claude_cli import CAVEMAN

FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/trig_01YFZ9tPxQGx7r31fmmKLAgi/fire"
TOKEN_FILE = Path.home() / ".config/claude-alert-fire/token"
CHANNEL_ID = "C0C7YJPATEK"


def fire(ts: str, extra: str = "") -> str:
    text = (f"alert_ts={ts}\nchannel={CHANNEL_ID}\n"
            "A new Sentry alert was posted at the top level of #production-errors. "
            "Read it and its thread with the Slack connector.")
    text += extra
    return fire_text(text)


def fire_text(text: str) -> str:
    req = urllib.request.Request(
        FIRE_URL,
        data=json.dumps({"text": text + CAVEMAN}).encode(),
        headers={
            "Authorization": "Bearer " + TOKEN_FILE.read_text().strip(),
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
