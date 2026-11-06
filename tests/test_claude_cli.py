import os
import signal
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
        for name, path in (("SESSIONS_FILE", "sessions.json"), ("SYSTEM_PROMPT_FILE", "system-prompt.md")):
            patcher = mock.patch.object(claude_cli, name, Path(tmp.name) / path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_thread_state_roundtrip(self):
        claude_cli.update("C1:1.0", session_id="S1")
        claude_cli.update("C1:1.0", seen_ts="5.0")
        self.assertEqual(claude_cli.get("C1:1.0"), {"session_id": "S1", "seen_ts": "5.0"})
        self.assertEqual(claude_cli.get("C1:2.0"), {})

    def test_first_run_starts_a_session_and_saves_it(self):
        def claude(prompt, session_id, key, new):
            self.assertTrue(new)
            self.assertEqual(claude_cli.get("k")["session_id"], session_id)  # saved before the run starts
            return {"result": "hola", "session_id": session_id}
        with mock.patch.object(claude_cli, "_claude", side_effect=claude):
            self.assertEqual(claude_cli.run("k", "prompt"), ("hola", False))
        self.assertEqual(len(claude_cli.get("k")["session_id"]), 36)

    def test_later_runs_resume_the_thread_session(self):
        claude_cli.update("k", session_id="S1")
        with mock.patch.object(claude_cli, "_claude", return_value={"result": "ok", "session_id": "S1"}) as claude:
            claude_cli.run("k", "prompt")
        claude.assert_called_once_with("prompt", "S1", "k", False)

    def claude_cmd(self, **settings):
        proc = mock.Mock(returncode=0)
        proc.communicate.return_value = ('{"result": "ok"}', "")
        with mock.patch.multiple(claude_cli.config, **settings), \
                mock.patch.object(claude_cli.subprocess, "Popen", return_value=proc) as popen:
            claude_cli._claude("prompt", None)
        self.stdin = proc.communicate.call_args.args[0]
        return popen.call_args.args[0]

    def test_prompts_stay_off_the_command_line(self):
        cmd = self.claude_cmd(PROMPT_SUFFIX="")
        self.assertEqual(self.stdin, "prompt")
        self.assertNotIn("prompt", cmd)
        self.assertFalse(any(arg.startswith("You are claudino") for arg in cmd))

    def test_new_sessions_get_their_id_up_front(self):
        proc = mock.Mock(returncode=0)
        proc.communicate.return_value = ('{"result": "ok"}', "")
        with mock.patch.object(claude_cli.subprocess, "Popen", return_value=proc) as popen:
            claude_cli._claude("prompt", "ID1", "k", True)
            claude_cli._claude("prompt", "ID1", "k", False)
        first, second = (c.args[0] for c in popen.call_args_list)
        self.assertEqual(first[first.index("--session-id") + 1], "ID1")
        self.assertEqual(second[second.index("--resume") + 1], "ID1")

    def test_permission_checks_are_skipped_by_default(self):
        cmd = self.claude_cmd(PERMISSION_MODE="bypassPermissions", PROMPT_SUFFIX="")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("--permission-mode", cmd)
        self.assertEqual(self.stdin, "prompt")

    def test_other_permission_modes_are_passed_through(self):
        cmd = self.claude_cmd(PERMISSION_MODE="auto")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_prompt_suffix_is_appended(self):
        self.claude_cmd(PROMPT_SUFFIX="caveman mode")
        self.assertEqual(self.stdin, "prompt\n\ncaveman mode")

    def test_local_instructions_are_appended(self):
        local = Path(self.tmp) / "instructions.md"
        local.write_text("Private note.")
        cmd = self.claude_cmd(LOCAL_INSTRUCTIONS_FILE=local)
        path = Path(cmd[cmd.index("--append-system-prompt-file") + 1])
        prompt = path.read_text()
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
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
            config.RUN_AS = ""
            config.LOCAL_INSTRUCTIONS_FILE = Path(self.tmp) / "none.md"
            script = Path(self.tmp) / "claude"
            # The tool runs in its own session, like Claude's Bash tool does, so a group kill misses it.
            marker = Path(self.tmp) / "tool.pid"
            script.write_text(f"#!/bin/sh\npython3 -c 'import os; os.setsid(); open(\"{marker}\", \"w\")"
                              f".write(str(os.getpid())); os.execvp(\"sleep\", [\"sleep\", \"30\"])' &\nwait\n")
            script.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{self.tmp}:/usr/bin:/bin"}):
                def stop_once_the_tool_runs():
                    for _ in range(200):
                        if marker.exists() and marker.read_text():
                            break
                        time.sleep(0.05)
                    claude_cli.stop("k")
                threading.Thread(target=stop_once_the_tool_runs).start()
                started = time.time()
                with self.assertRaises(claude_cli.Stopped):
                    claude_cli._claude("prompt", None, "k")
        self.assertLess(time.time() - started, 10)
        tool = int(marker.read_text())
        for _ in range(100):
            try:
                os.kill(tool, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(tool, signal.SIGKILL)
            self.fail("the tool outlived the stop")
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

    def test_run_as_goes_through_sudo_in_the_workdir(self):
        with mock.patch.multiple(claude_cli.config, RUN_AS="agent", EXTRA_PATH=["/home/agent/.local/bin"]):
            cmd, cwd = claude_cli.command(["claude", "-p", "x"], "/home/agent")
        self.assertEqual(cmd[:6], ["sudo", "-n", "-H", "-u", "agent", "--"])
        self.assertIn("PATH=/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin", cmd)
        self.assertEqual(cmd[-4:], ["/home/agent", "claude", "-p", "x"])
        self.assertEqual(cwd, "/")

    def test_run_as_command_changes_directory_and_runs(self):
        with mock.patch.multiple(claude_cli.config, RUN_AS="agent", EXTRA_PATH=[]):
            cmd, _ = claude_cli.command(["pwd"], self.tmp)
        shell = cmd[cmd.index("sh"):]  # what sudo would run, minus sudo itself
        out = claude_cli.subprocess.run(shell, capture_output=True, text=True).stdout.strip()
        self.assertEqual(os.path.realpath(out), os.path.realpath(self.tmp))

    def test_without_run_as_the_command_is_unchanged(self):
        with mock.patch.object(claude_cli.config, "RUN_AS", ""):
            self.assertEqual(claude_cli.command(["claude"], "/w"), (["claude"], "/w"))

    def test_kill_falls_back_to_sudo_for_the_session_user(self):
        proc = mock.Mock(pid=10)
        with mock.patch.object(claude_cli.config, "RUN_AS", "agent"), \
                mock.patch.object(claude_cli, "_tree", return_value=[10, 11]), \
                mock.patch.object(claude_cli.os, "kill", side_effect=[None, PermissionError()]), \
                mock.patch.object(claude_cli.subprocess, "run") as run:
            claude_cli._kill(proc)
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", "-u", "agent", "--", "kill", "-KILL", "11"])

    def test_sessions_never_get_slack_tokens(self):
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "y", "PATH": "/usr/bin"}):
            env = claude_cli._env()
        self.assertNotIn("SLACK_BOT_TOKEN", env)
        self.assertNotIn("SLACK_APP_TOKEN", env)
        self.assertTrue(env["PATH"].endswith("/usr/bin"))
        self.assertIn(".local/bin", env["PATH"])


if __name__ == "__main__":
    unittest.main()
