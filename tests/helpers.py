"""Import relay.py without Slack: a fake Bolt app stands in for the real one."""

import os
import sys
import types
from unittest import mock

RELAY_USER_ID = "URELAY"


def load_relay():
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
    fake_app = mock.MagicMock()
    fake_app.client.auth_test.return_value = {"user_id": RELAY_USER_ID}
    fake_app.event = lambda name: (lambda handler: handler)
    bolt = types.ModuleType("slack_bolt")
    bolt.App = mock.MagicMock(return_value=fake_app)
    adapter = types.ModuleType("slack_bolt.adapter")
    socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket_mode.SocketModeHandler = mock.MagicMock()
    stubs = {"slack_bolt": bolt, "slack_bolt.adapter": adapter, "slack_bolt.adapter.socket_mode": socket_mode}
    with mock.patch.dict(sys.modules, stubs):
        sys.modules.pop("relay", None)
        import relay
    return relay
