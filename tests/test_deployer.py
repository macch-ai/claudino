import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("deployer", Path(__file__).resolve().parents[1] / "deploy/deployer.py")
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)


def done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class DeployerTest(unittest.TestCase):
    def git(self, target="abc123", merge_rc=0):
        answers = {"fetch": done(), "merge": done(returncode=merge_rc, stderr="diverged")}

        def fake(*args):
            if args[0] == "rev-parse":
                return done(target if args[1] == "origin/main" else "old999")
            return answers[args[0]]
        return mock.patch.object(deployer, "git", side_effect=fake)

    def test_deploys_the_requested_commit(self):
        with self.git(), mock.patch.object(deployer.subprocess, "run", return_value=done("running")) as run:
            status, body = deployer.deploy("abc")
        self.assertEqual((status, body), (200, "running"))
        self.assertEqual(run.call_args.args[0][1], "old999")

    def test_refuses_a_commit_that_is_not_main(self):
        with self.git(), mock.patch.object(deployer.subprocess, "run") as run:
            status, _ = deployer.deploy("fff")
        self.assertEqual(status, 409)
        run.assert_not_called()

    def test_reports_a_failed_fast_forward(self):
        with self.git(merge_rc=1), mock.patch.object(deployer.subprocess, "run") as run:
            status, body = deployer.deploy("abc")
        self.assertEqual(status, 500)
        self.assertIn("fast-forward failed", body)
        run.assert_not_called()

    def test_failed_deploy_script_is_an_error(self):
        with self.git(), mock.patch.object(deployer.subprocess, "run", return_value=done("FAILED: tests", 1)):
            status, body = deployer.deploy("abc")
        self.assertEqual(status, 500)
        self.assertIn("FAILED: tests", body)

    def test_one_deploy_at_a_time(self):
        deployer.lock.acquire()
        try:
            self.assertEqual(deployer.deploy("abc")[0], 409)
        finally:
            deployer.lock.release()


if __name__ == "__main__":
    unittest.main()
