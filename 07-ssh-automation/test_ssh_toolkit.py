"""
Unit tests for ssh_toolkit.py

No real SSH server or network access is available in this environment,
so every test injects a fake `runner` (matching the CommandRunner
signature) that returns scripted subprocess.CompletedProcess results.
This is the same dependency-injection pattern used for sleep_fn/move_fn
in earlier projects, and it's arguably the *right* approach here
regardless of environment: real multi-host SSH integration tests would
need actual infrastructure this test suite shouldn't depend on.

Run with:
    python3 -m unittest discover -s tests -v
"""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ssh_toolkit import (  # noqa: E402
    SSHTarget,
    build_scp_argv,
    copy_to_host,
    run_on_host,
    run_on_hosts,
)


def scripted_runner(script: list[dict]):
    """
    Returns (runner, call_log). Each call to the runner consumes the next
    step in `script` (the last step repeats if more calls happen than
    steps provided). call_log records every argv passed in, in order.
    """
    call_log = []

    def runner(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
        call_log.append(argv)
        step = script[min(len(call_log) - 1, len(script) - 1)]
        if step.get("raise") == "timeout":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        if step.get("raise") == "not_found":
            raise FileNotFoundError("ssh not found")
        return subprocess.CompletedProcess(
            argv, step.get("rc", 0), stdout=step.get("out", ""), stderr=step.get("err", "")
        )

    return runner, call_log


class TestRunOnHost(unittest.TestCase):
    def test_successful_command_returns_stdout(self):
        runner, _ = scripted_runner([{"rc": 0, "out": "hello\n"}])
        result = run_on_host(SSHTarget("h1", "u"), "echo hello", runner=runner, sleep_fn=lambda s: None)
        self.assertTrue(result.success)
        self.assertEqual(result.stdout, "hello\n")

    def test_remote_command_nonzero_exit_is_command_error_not_retried(self):
        runner, log = scripted_runner([{"rc": 1, "err": "app crashed"}])
        result = run_on_host(SSHTarget("h1", "u"), "false", runner=runner, sleep_fn=lambda s: None)
        self.assertFalse(result.success)
        self.assertEqual(result.category, "command_error")
        self.assertEqual(len(log), 1)  # never retried

    def test_connection_refused_is_retried_and_can_succeed(self):
        runner, log = scripted_runner([
            {"rc": 255, "err": "ssh: connect to host h1 port 22: Connection refused"},
            {"rc": 0, "out": "ok\n"},
        ])
        result = run_on_host(SSHTarget("h1", "u"), "cmd", runner=runner, sleep_fn=lambda s: None)
        self.assertTrue(result.success)
        self.assertEqual(len(log), 2)

    def test_connection_refused_exhausted_retries_reports_transport_error(self):
        runner, log = scripted_runner([
            {"rc": 255, "err": "ssh: connect to host h1 port 22: Connection refused"},
        ])
        result = run_on_host(
            SSHTarget("h1", "u"), "cmd", runner=runner, max_retries=2, sleep_fn=lambda s: None
        )
        self.assertFalse(result.success)
        self.assertEqual(result.category, "transport_error")
        self.assertEqual(len(log), 3)  # initial + 2 retries

    def test_auth_failure_never_retried(self):
        runner, log = scripted_runner([{"rc": 255, "err": "Permission denied (publickey)."}])
        result = run_on_host(
            SSHTarget("h1", "u"), "cmd", runner=runner, max_retries=3, sleep_fn=lambda s: None
        )
        self.assertFalse(result.success)
        self.assertEqual(len(log), 1)  # not retried despite max_retries=3

    def test_host_key_verification_failure_never_retried(self):
        runner, log = scripted_runner([{"rc": 255, "err": "Host key verification failed."}])
        result = run_on_host(
            SSHTarget("h1", "u"), "cmd", runner=runner, max_retries=3, sleep_fn=lambda s: None
        )
        self.assertFalse(result.success)
        self.assertEqual(len(log), 1)

    def test_timeout_reported_without_crashing(self):
        runner, log = scripted_runner([{"raise": "timeout"}])
        result = run_on_host(
            SSHTarget("h1", "u"), "cmd", runner=runner, max_retries=0, sleep_fn=lambda s: None
        )
        self.assertFalse(result.success)
        self.assertEqual(result.category, "timeout")

    def test_timeout_is_retried(self):
        runner, log = scripted_runner([
            {"raise": "timeout"},
            {"rc": 0, "out": "recovered\n"},
        ])
        result = run_on_host(
            SSHTarget("h1", "u"), "cmd", runner=runner, max_retries=1, sleep_fn=lambda s: None
        )
        self.assertTrue(result.success)
        self.assertEqual(len(log), 2)

    def test_ssh_argv_includes_batch_mode_and_connect_timeout(self):
        runner, log = scripted_runner([{"rc": 0}])
        run_on_host(SSHTarget("h1", "u"), "cmd", runner=runner, sleep_fn=lambda s: None)
        argv = log[0]
        self.assertIn("BatchMode=yes", argv)
        self.assertTrue(any("ConnectTimeout=" in a for a in argv))

    def test_identity_file_included_when_set(self):
        runner, log = scripted_runner([{"rc": 0}])
        target = SSHTarget("h1", "u", identity_file="/home/user/.ssh/id_ed25519")
        run_on_host(target, "cmd", runner=runner, sleep_fn=lambda s: None)
        self.assertIn("-i", log[0])
        self.assertIn("/home/user/.ssh/id_ed25519", log[0])


class TestRunOnHosts(unittest.TestCase):
    def _multi_host_runner(self):
        def runner(argv, timeout):
            userhost = argv[-2]
            if "bad-host" in userhost:
                return subprocess.CompletedProcess(
                    argv, 255, stdout="", stderr="ssh: Could not resolve hostname bad-host"
                )
            return subprocess.CompletedProcess(argv, 0, stdout=f"ok from {userhost}\n", stderr="")

        return runner

    def test_results_returned_in_target_order_regardless_of_parallel_completion(self):
        targets = [SSHTarget(h, "u") for h in ["good1", "bad-host", "good2"]]
        results = run_on_hosts(
            targets, "uptime", parallel=True, runner=self._multi_host_runner(),
            max_retries=0, sleep_fn=lambda s: None,
        )
        self.assertEqual([r.host for r in results], ["good1", "bad-host", "good2"])

    def test_one_bad_host_does_not_prevent_others_from_succeeding(self):
        targets = [SSHTarget(h, "u") for h in ["good1", "bad-host", "good2"]]
        results = run_on_hosts(
            targets, "uptime", parallel=True, runner=self._multi_host_runner(),
            max_retries=0, sleep_fn=lambda s: None,
        )
        successes = [r.success for r in results]
        self.assertEqual(successes, [True, False, True])

    def test_sequential_mode_produces_same_results_as_parallel(self):
        targets = [SSHTarget(h, "u") for h in ["good1", "bad-host", "good2"]]
        results = run_on_hosts(
            targets, "uptime", parallel=False, runner=self._multi_host_runner(),
            max_retries=0, sleep_fn=lambda s: None,
        )
        self.assertEqual([r.success for r in results], [True, False, True])

    def test_empty_target_list_returns_empty_results(self):
        results = run_on_hosts([], "uptime", runner=self._multi_host_runner())
        self.assertEqual(results, [])


class TestCopyToHost(unittest.TestCase):
    def test_successful_upload(self):
        runner, log = scripted_runner([{"rc": 0}])
        result = copy_to_host(SSHTarget("h1", "u"), "local.txt", "/remote/path.txt", runner=runner)
        self.assertTrue(result.success)
        self.assertEqual(log[0][0], "scp")

    def test_scp_argv_order_for_upload(self):
        argv = build_scp_argv(SSHTarget("h1", "u"), "local.txt", "/remote/x.txt", upload=True, connect_timeout=10)
        self.assertEqual(argv[-2], "local.txt")
        self.assertEqual(argv[-1], "u@h1:/remote/x.txt")

    def test_scp_argv_order_for_download(self):
        argv = build_scp_argv(SSHTarget("h1", "u"), "local.txt", "/remote/x.txt", upload=False, connect_timeout=10)
        self.assertEqual(argv[-2], "u@h1:/remote/x.txt")
        self.assertEqual(argv[-1], "local.txt")

    def test_failed_upload_reports_transport_error(self):
        runner, _ = scripted_runner([{"rc": 255, "err": "ssh: Connection refused"}])
        result = copy_to_host(SSHTarget("h1", "u"), "local.txt", "/remote/x.txt", runner=runner)
        self.assertFalse(result.success)
        self.assertEqual(result.category, "transport_error")

    def test_upload_timeout_reported_not_crashed(self):
        runner, _ = scripted_runner([{"raise": "timeout"}])
        result = copy_to_host(SSHTarget("h1", "u"), "local.txt", "/remote/x.txt", runner=runner)
        self.assertFalse(result.success)
        self.assertEqual(result.category, "timeout")


if __name__ == "__main__":
    unittest.main()
