"""Sign in with Slack (OpenID Connect) for pairing the claudino desktop app from the browser.

The Slack app needs "Sign in with Slack" (scopes openid, profile) and the redirect URL
{PUBLIC_URL}/login/callback. Its client ID and secret live in hermes' relay env file.
"""

import json
import os
import urllib.parse
import urllib.request

PUBLIC_URL = os.environ.get("CLAUDINO_PUBLIC_URL", "https://claudino.macch.ai").rstrip("/")
CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")
REDIRECT_URI = f"{PUBLIC_URL}/login/callback"


class LoginError(Exception):
    """Slack did not confirm who the person is."""


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def authorize_url(state: str, nonce: str, team_id: str = "") -> str:
    params = {"response_type": "code", "scope": "openid profile", "client_id": CLIENT_ID,
              "redirect_uri": REDIRECT_URI, "state": state, "nonce": nonce}
    if team_id:
        params["team"] = team_id  # skip the workspace picker
    return "https://slack.com/openid/connect/authorize?" + urllib.parse.urlencode(params)


def _post(url: str, data: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    if not body.get("ok"):
        raise LoginError(body.get("error") or "slack_error")
    return body


def profile(code: str) -> dict:
    """Exchange the callback code: the Slack user and team who signed in, with their name and photo (blocking)."""
    tokens = _post("https://slack.com/api/openid.connect.token",
                   {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code, "redirect_uri": REDIRECT_URI})
    info = _post("https://slack.com/api/openid.connect.userInfo", {}, token=tokens["access_token"])
    return {
        "user": info.get("https://slack.com/user_id", ""),
        "team": info.get("https://slack.com/team_id", ""),
        "name": info.get("name") or info.get("given_name") or "",
        "avatar": info.get("https://slack.com/user_image_72") or info.get("picture") or "",
    }
