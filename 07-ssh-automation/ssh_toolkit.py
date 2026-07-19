#!/usr/bin/env python3
"""
ssh_toolkit.py — defensive multi-host SSH command runner and file sync,
built on the `ssh`/`scp`/`rsync` CLIs via subprocess (no paramiko
dependency).

Design goals:
  - One unreachable host in a fleet must never stop the others from
    being processed.
  - Distinguish transport-level failure (connection refused, timeout,
    host key mismatch) from the remote command's own exit code — these
    mean very different things and callers need to be able to tell them
    apart.
  - Never hang waiting for an interactive password prompt — batch mode
    and connection timeouts are always on by default.
  - The actual subprocess call is injectable, so the retry/error-
    classification/multi-host logic can be fully tested without a real
    SSH server or network access.

Usage:
    from ssh_toolkit import SSHTarget, run_on_host, run_on_hosts

    target = SSHTarget(host="10.0.0.5", user="deploy")
    result = run_on_host(target, "uptime")

    targets = [SSHTarget(host=h, user="deploy") for h in ["10.0.0.5", "10.0.0.6"]]
    results = run_on_hosts(targets, "systemctl status nginx")

CLI:
    ./ssh_toolkit.py run --hosts hosts.txt --user deploy "uptime"
    ./ssh_toolkit.py copy --hosts hosts.txt --user deploy local.txt /remote/path/
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_COMMAND_TIMEOUT = 30
SSH_TRANSPORT_EXIT_CODE = 255

# Substrings in stderr that indicate the failure happened at the SSH
# transport layer, not inside the remote command. Used to classify a
# 255 exit code, since remote commands can (rarely, but legitimately)
# also exit 255 on their own.
TRANSPORT_ERROR_MARKERS = (
    "connection refused",
    "could not resolve hostname",
    "connection timed out",
    "no route to host",
    "permission denied (publickey",
    "host key verification failed",
    "operation timed out",
)


class SSHToolkitError(Exception):
    """Base class for all ssh_toolkit-raised errors."""


class SSHNotInstalledError(SSHToolkitError):
    """The `ssh` (or `scp`/`rsync`) executable could not be found."""


@dataclass
class SSHTarget:
    host: str
    user: str
    port: int = 22
    identity_file: Optional[str] = None
    strict_host_key_checking: bool = True

    def label(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"


@dataclass
class HostResult:
    host: str
    success: bool
    returncode: Optional[int]
    stdout: str = ""
    stderr: str = ""
    category: str = "ok"  # "ok", "transport_error", "command_error", "timeout"
    error: Optional[str] = None


CommandRunner = Callable[[list[str], int], subprocess.CompletedProcess]


def _default_runner(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _build_ssh_argv(
    target: SSHTarget, remote_command: str, connect_timeout: int
) -> list[str]:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",  # never hang on an interactive password prompt
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", f"StrictHostKeyChecking={'yes' if target.strict_host_key_checking else 'accept-new'}",
        "-p", str(target.port),
    ]
    if target.identity_file:
        argv += ["-i", target.identity_file]
    argv.append(f"{target.user}@{target.host}")
    argv.append(remote_command)
    return argv


def _classify_failure(returncode: Optional[int], stderr: str) -> tuple[str, bool]:
    """Return (category, is_retryable)."""
    if returncode == SSH_TRANSPORT_EXIT_CODE:
        lowered = stderr.lower()
        if any(marker in lowered for marker in TRANSPORT_ERROR_MARKERS):
            # Auth/host-key failures won't fix themselves on retry;
            # connection-refused/timeout/unreachable might.
            if "permission denied" in lowered or "host key verification failed" in lowered:
                return "transport_error", False
            return "transport_error", True
        return "transport_error", True
    return "command_error", False


def run_on_host(
    target: SSHTarget,
    command: str,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
    max_retries: int = 2,
    runner: CommandRunner = _default_runner,
    sleep_fn=time.sleep,
) -> HostResult:
    """
    Run a single command on a single host. Retries transport-level
    failures that look transient (connection refused, timeout) with
    backoff; never retries authentication or host-key failures, and
    never retries a remote command that ran and simply exited non-zero.
    """
    argv = _build_ssh_argv(target, command, connect_timeout)
    last_result: Optional[HostResult] = None

    for attempt in range(max_retries + 1):
        try:
            proc = runner(argv, command_timeout)
        except FileNotFoundError:
            raise SSHNotInstalledError("ssh executable not found on PATH")
        except subprocess.TimeoutExpired:
            last_result = HostResult(
                host=target.host, success=False, returncode=None,
                category="timeout", error=f"command timed out after {command_timeout}s",
            )
            if attempt < max_retries:
                sleep_fn(min(0.5 * (2 ** attempt), 4.0))
                continue
            return last_result

        if proc.returncode == 0:
            return HostResult(
                host=target.host, success=True, returncode=0,
                stdout=proc.stdout, stderr=proc.stderr, category="ok",
            )

        category, retryable = _classify_failure(proc.returncode, proc.stderr)
        last_result = HostResult(
            host=target.host, success=False, returncode=proc.returncode,
            stdout=proc.stdout, stderr=proc.stderr, category=category,
            error=proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else None,
        )
        if retryable and attempt < max_retries:
            sleep_fn(min(0.5 * (2 ** attempt), 4.0))
            continue
        return last_result

    return last_result  # pragma: no cover — loop always returns above


def run_on_hosts(
    targets: list[SSHTarget],
    command: str,
    parallel: bool = True,
    max_workers: int = 10,
    **kwargs,
) -> list[HostResult]:
    """
    Run the same command across many hosts. One host's failure never
    prevents the others from being attempted — every target gets a
    result, in the same order as `targets` was given, regardless of
    which finished first when running in parallel.
    """
    if not parallel:
        return [run_on_host(t, command, **kwargs) for t in targets]

    results: list[Optional[HostResult]] = [None] * len(targets)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_index = {
            pool.submit(run_on_host, t, command, **kwargs): i for i, t in enumerate(targets)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            results[index] = future.result()
    return results  # type: ignore[return-value]


def build_scp_argv(
    target: SSHTarget, local_path: str, remote_path: str, upload: bool, connect_timeout: int
) -> list[str]:
    argv = [
        "scp",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", f"StrictHostKeyChecking={'yes' if target.strict_host_key_checking else 'accept-new'}",
        "-P", str(target.port),
    ]
    if target.identity_file:
        argv += ["-i", target.identity_file]
    remote = f"{target.user}@{target.host}:{remote_path}"
    if upload:
        argv += [local_path, remote]
    else:
        argv += [remote, local_path]
    return argv


def copy_to_host(
    target: SSHTarget,
    local_path: str,
    remote_path: str,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    transfer_timeout: int = 120,
    runner: CommandRunner = _default_runner,
) -> HostResult:
    """Upload a local file to a remote host via scp."""
    argv = build_scp_argv(target, local_path, remote_path, upload=True, connect_timeout=connect_timeout)
    try:
        proc = runner(argv, transfer_timeout)
    except FileNotFoundError:
        raise SSHNotInstalledError("scp executable not found on PATH")
    except subprocess.TimeoutExpired:
        return HostResult(host=target.host, success=False, returncode=None, category="timeout",
                           error=f"transfer timed out after {transfer_timeout}s")

    if proc.returncode == 0:
        return HostResult(host=target.host, success=True, returncode=0, stdout=proc.stdout, stderr=proc.stderr)
    category, _ = _classify_failure(proc.returncode, proc.stderr)
    return HostResult(
        host=target.host, success=False, returncode=proc.returncode,
        stdout=proc.stdout, stderr=proc.stderr, category=category,
        error=proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else None,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Multi-host SSH command runner")
    sub = parser.add_subparsers(dest="action", required=True)

    run_p = sub.add_parser("run", help="Run a command on one or more hosts")
    run_p.add_argument("command")
    run_p.add_argument("--hosts", required=True, help="Path to a file, one host per line")
    run_p.add_argument("--user", required=True)
    run_p.add_argument("--port", type=int, default=22)
    run_p.add_argument("--identity-file")
    run_p.add_argument("--sequential", action="store_true", help="Run one host at a time")

    copy_p = sub.add_parser("copy", help="Copy a local file to one or more hosts")
    copy_p.add_argument("local_path")
    copy_p.add_argument("remote_path")
    copy_p.add_argument("--hosts", required=True)
    copy_p.add_argument("--user", required=True)
    copy_p.add_argument("--port", type=int, default=22)
    copy_p.add_argument("--identity-file")

    return parser


def _load_hosts(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    hosts = _load_hosts(args.hosts)
    targets = [
        SSHTarget(host=h, user=args.user, port=args.port, identity_file=args.identity_file)
        for h in hosts
    ]

    if args.action == "run":
        results = run_on_hosts(targets, args.command, parallel=not args.sequential)
    else:
        results = [copy_to_host(t, args.local_path, args.remote_path) for t in targets]

    failures = 0
    for r in results:
        status = "OK" if r.success else f"FAILED ({r.category})"
        print(f"[{r.host}] {status}")
        if r.success and r.stdout.strip():
            print(r.stdout.rstrip())
        if not r.success:
            failures += 1
            if r.error:
                print(f"  {r.error}", file=sys.stderr)

    print(f"\n{len(results) - failures}/{len(results)} host(s) succeeded")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
