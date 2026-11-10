import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fire_claude


class FireTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        token = Path(tmp.name) / "token"
        token.write_text("tok\n")
        patcher = mock.patch.object(fire_claude, "TOKEN_FILE", token)
        patcher.start()
        self.addCleanup(patcher.stop)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"claude_code_session_url": "https://claude.ai/code/x"}'
        self.urlopen = mock.patch.object(fire_claude.urllib.request, "urlopen", return_value=response).start()
        self.addCleanup(mock.patch.stopall)

    def sent(self):
        request = self.urlopen.call_args.args[0]
        return request, json.loads(request.data)["text"]

    def test_alert_fire_names_the_alert_and_appends_extra(self):
        url = fire_claude.fire("1.2", extra="\nfollowup_ts=3.4")
        request, text = self.sent()
        self.assertEqual(url, "https://claude.ai/code/x")
        self.assertIn("alert_ts=1.2", text)
        self.assertTrue(text.endswith("followup_ts=3.4"))
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        self.assertEqual(request.get_full_url(), fire_claude.FIRE_URL)

    def test_fire_text_sends_the_text_as_is(self):
        fire_claude.fire_text("mention_ts=9.9")
        _, text = self.sent()
        self.assertEqual(text, "mention_ts=9.9")


if __name__ == "__main__":
    unittest.main()

    def test_deploy_alert_fire_says_it_is_a_failed_build(self):
        fire_claude.fire("1.2", kind="deploy")
        _, text = self.sent()
        self.assertIn("deploy_alert_ts=1.2", text)
        self.assertIn("Workers Build", text)
        self.assertNotIn("alert_ts=1.2\n", text.replace("deploy_alert_ts=1.2", ""))
