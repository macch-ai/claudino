import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_cli


class ClaudeCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(claude_cli, "STATE_FILE", Path(tmp.name) / "sessions.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_thread_state_roundtrip(self):
        claude_cli.update("C1:1.0", session_id="S1")
        claude_cli.update("C1:1.0", seen_ts="5.0")
        self.assertEqual(claude_cli.get("C1:1.0"), {"session_id": "S1", "seen_ts": "5.0"})
        self.assertEqual(claude_cli.get("C1:2.0"), {})

    def test_first_run_starts_a_session_and_saves_it(self):
        with mock.patch.object(claude_cli, "_claude", return_value={"result": "hola", "session_id": "S2"}) as claude:
            self.assertEqual(claude_cli.run("k", "prompt"), ("hola", False))
        claude.assert_called_once_with("prompt", None)
        self.assertEqual(claude_cli.get("k")["session_id"], "S2")

    def test_later_runs_resume_the_thread_session(self):
        claude_cli.update("k", session_id="S1")
        with mock.patch.object(claude_cli, "_claude", return_value={"result": "ok", "session_id": "S1"}) as claude:
            claude_cli.run("k", "prompt")
        claude.assert_called_once_with("prompt", "S1")

    def test_prompt_asks_for_caveman_mode(self):
        proc = mock.Mock(stdout='{"result": "ok"}', returncode=0)
        with mock.patch.object(claude_cli.subprocess, "run", return_value=proc) as run:
            claude_cli._claude("prompt", None)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-p") + 1], "prompt\n\ncaveman mode")

    def test_failed_resume_resets_the_thread(self):
        claude_cli.update("k", session_id="S1", seen_ts="9.0")
        with mock.patch.object(claude_cli, "_claude", side_effect=RuntimeError("gone")):
            with self.assertRaises(RuntimeError):
                claude_cli.run("k", "prompt")
        self.assertEqual(claude_cli.get("k"), {"session_id": None, "seen_ts": "0"})

    def test_sessions_never_get_slack_tokens(self):
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "y", "PATH": "/usr/bin"}):
            env = claude_cli._env()
        self.assertNotIn("SLACK_BOT_TOKEN", env)
        self.assertNotIn("SLACK_APP_TOKEN", env)
        self.assertTrue(env["PATH"].endswith("/usr/bin"))
        self.assertIn(".local/bin", env["PATH"])


if __name__ == "__main__":
    unittest.main()
