import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import claude_cli


class ClaudeCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        patcher = mock.patch.object(claude_cli, "SESSIONS_FILE", Path(tmp.name) / "sessions.json")
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
        claude.assert_called_once_with("prompt", None, "k")
        self.assertEqual(claude_cli.get("k")["session_id"], "S2")

    def test_later_runs_resume_the_thread_session(self):
        claude_cli.update("k", session_id="S1")
        with mock.patch.object(claude_cli, "_claude", return_value={"result": "ok", "session_id": "S1"}) as claude:
            claude_cli.run("k", "prompt")
        claude.assert_called_once_with("prompt", "S1", "k")

    def claude_cmd(self, **settings):
        proc = mock.Mock(returncode=0)
        proc.communicate.return_value = ('{"result": "ok"}', "")
        with mock.patch.multiple(claude_cli.config, **settings), \
                mock.patch.object(claude_cli.subprocess, "Popen", return_value=proc) as popen:
            claude_cli._claude("prompt", None)
        return popen.call_args.args[0]

    def test_permission_checks_are_skipped_by_default(self):
        cmd = self.claude_cmd(PERMISSION_MODE="bypassPermissions", PROMPT_SUFFIX="")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("--permission-mode", cmd)
        self.assertEqual(cmd[cmd.index("-p") + 1], "prompt")

    def test_other_permission_modes_are_passed_through(self):
        cmd = self.claude_cmd(PERMISSION_MODE="auto")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_prompt_suffix_is_appended(self):
        cmd = self.claude_cmd(PROMPT_SUFFIX="caveman mode")
        self.assertEqual(cmd[cmd.index("-p") + 1], "prompt\n\ncaveman mode")

    def test_local_instructions_are_appended(self):
        local = Path(self.tmp) / "instructions.md"
        local.write_text("Private note.")
        cmd = self.claude_cmd(LOCAL_INSTRUCTIONS_FILE=local)
        prompt = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertTrue(prompt.startswith("You are claudino"))
        self.assertTrue(prompt.endswith("Private note."))

    def test_failed_resume_resets_the_thread(self):
        claude_cli.update("k", session_id="S1", seen_ts="9.0")
        with mock.patch.object(claude_cli, "_claude", side_effect=RuntimeError("gone")):
            with self.assertRaises(RuntimeError):
                claude_cli.run("k", "prompt")
        self.assertEqual(claude_cli.get("k"), {"session_id": None, "seen_ts": "0"})

    def test_stop_kills_the_running_claude_and_its_tools(self):
        with mock.patch.object(claude_cli, "permission_args", return_value=[]), \
                mock.patch.object(claude_cli, "config") as config:
            config.with_suffix = lambda p: p
            config.WORKDIR, config.TIMEOUT_SECONDS, config.EXTRA_PATH = self.tmp, 30, []
            config.LOCAL_INSTRUCTIONS_FILE = Path(self.tmp) / "none.md"
            script = Path(self.tmp) / "claude"
            script.write_text("#!/bin/sh\nsleep 30 &\nwait\n")
            script.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{self.tmp}:/usr/bin:/bin"}):
                threading.Timer(0.3, claude_cli.stop, args=("k",)).start()
                started = time.time()
                with self.assertRaises(claude_cli.Stopped):
                    claude_cli._claude("prompt", None, "k")
        self.assertLess(time.time() - started, 10)
        self.assertNotIn("k", claude_cli._running)
        self.assertNotIn("k", claude_cli._stopped)

    def test_stop_before_the_run_starts_stops_it(self):
        claude_cli.stop("k")
        with mock.patch.object(claude_cli.subprocess, "Popen") as popen:
            with self.assertRaises(claude_cli.Stopped):
                claude_cli._claude("prompt", None, "k")
        popen.assert_not_called()

    def test_new_run_clears_an_old_stop(self):
        claude_cli.stop("k")
        claude_cli.clear_stop("k")
        self.assertNotIn("k", claude_cli._stopped)

    def test_stopped_run_keeps_the_thread_session(self):
        claude_cli.update("k", session_id="S1", seen_ts="9.0")
        with mock.patch.object(claude_cli, "_claude", side_effect=claude_cli.Stopped()):
            with self.assertRaises(claude_cli.Stopped):
                claude_cli.run("k", "prompt")
        self.assertEqual(claude_cli.get("k"), {"session_id": "S1", "seen_ts": "9.0"})

    def test_sessions_never_get_slack_tokens(self):
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "y", "PATH": "/usr/bin"}):
            env = claude_cli._env()
        self.assertNotIn("SLACK_BOT_TOKEN", env)
        self.assertNotIn("SLACK_APP_TOKEN", env)
        self.assertTrue(env["PATH"].endswith("/usr/bin"))
        self.assertIn(".local/bin", env["PATH"])


if __name__ == "__main__":
    unittest.main()
