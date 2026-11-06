import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

from slack_sdk.errors import SlackApiError

spec = importlib.util.spec_from_file_location(
    "alert_slack", Path(__file__).resolve().parent.parent / "scripts" / "alert-slack.py")
alert_slack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alert_slack)

ALERTS = "CALERTS"


class AlertSlackTest(unittest.TestCase):
    def setUp(self):
        env = {"SLACK_BOT_TOKEN": "xoxb-test", "CLAUDINO_ALERT_CHANNEL": ALERTS}
        for patcher in (mock.patch.object(alert_slack, "settings", return_value=env),
                        mock.patch.object(alert_slack, "WebClient")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = alert_slack.WebClient.return_value

    def run_cli(self, *args, stdin=""):
        with contextlib.redirect_stdout(io.StringIO()):
            return alert_slack.main(["alert-slack.py", *args], stdin=io.StringIO(stdin))

    def test_reply_goes_to_the_alert_thread_with_text_from_stdin(self):
        self.client.chat_postMessage.return_value = {"ts": "9.1"}
        self.assertEqual(self.run_cli("post", ALERTS, "5.0", stdin="Fixed in #1"), 0)
        self.client.chat_postMessage.assert_called_once_with(
            channel=ALERTS, thread_ts="5.0", text="Fixed in #1", unfurl_links=False)

    def test_other_channels_are_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli("post", "COTHER", "5.0", stdin="hi")
        with self.assertRaises(SystemExit):
            self.run_cli("add", "D123", "5.0", "eyes")
        self.client.chat_postMessage.assert_not_called()
        self.client.reactions_add.assert_not_called()

    def test_bad_timestamps_and_emoji_are_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli("add", ALERTS, "5.0;x", "eyes")
        with self.assertRaises(SystemExit):
            self.run_cli("add", ALERTS, "5.0", "eyes --x")

    def test_empty_message_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli("post", ALERTS, "5.0", stdin="  \n")

    def test_already_reacted_counts_as_done(self):
        self.client.reactions_add.side_effect = SlackApiError("dup", {"error": "already_reacted"})
        self.assertEqual(self.run_cli("add", ALERTS, "5.0", "eyes"), 0)

    def test_slack_errors_fail_with_the_error_name(self):
        self.client.reactions_add.side_effect = SlackApiError("bad", {"error": "invalid_auth"})
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(self.run_cli("add", ALERTS, "5.0", "eyes"), 1)
        self.assertIn("invalid_auth", err.getvalue())


if __name__ == "__main__":
    unittest.main()
