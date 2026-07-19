# 07 — SSH Automation Toolkit

A defensive multi-host SSH command runner and file-copy tool built on the
`ssh`/`scp` CLIs via subprocess. Runs a command (or a file copy) across a
fleet of hosts, isolates each host's failure from the others, and
distinguishes transport-level failures (unreachable host, refused
connection, bad host key) from the remote command's own exit code.

## Problem

Running a command across a list of servers by hand (`for h in hosts; do
ssh $h cmd; done`) has real failure modes a naive script ignores: a
single unreachable host shouldn't stop the loop, a password prompt on a
misconfigured host will hang the whole script forever, and "the SSH
connection failed" and "the remote command failed" are different
problems that need different responses — but both surface as a non-zero
exit code from `ssh` unless you look closer.

## Requirements

- Run a command across many hosts, with one host's failure never
  blocking the others.
- Never hang on an interactive password prompt (`BatchMode=yes` always
  set) or an unreachable host (`ConnectTimeout` always set).
- Distinguish SSH transport failures (connection refused, DNS failure,
  auth failure, host key mismatch) from the remote command's own exit
  code.
- Retry failures that are plausibly transient (connection refused,
  timeout); never retry failures that won't fix themselves (bad
  credentials, host key mismatch, the remote command's own logic
  failing).
- Support both parallel and sequential execution across a host list.
- Support uploading a file to many hosts via `scp`.
- Be fully testable without a real SSH server, since none is available
  in this environment (no `paramiko`, no `sshd`, no network egress).

## Architecture

```
ssh_toolkit.py
├── SSHTarget                # host/user/port/identity_file/host-key policy
├── _build_ssh_argv()        # constructs the ssh CLI invocation
├── _classify_failure()      # transport vs command error, retryable or not
├── run_on_host()            # single host: retry loop + classification
├── run_on_hosts()           # fan-out across a host list, parallel or not
├── copy_to_host()           # scp wrapper, same error classification
└── CommandRunner (injectable) # the actual subprocess call
```

Every function that touches the network goes through the `runner`
parameter (`CommandRunner`), which defaults to a thin `subprocess.run`
wrapper but can be swapped for a test double. This is the same pattern
used for `sleep_fn` in the API client project and `move_fn` in the file
organizer — here it does double duty, since it's also what makes the
whole test suite possible without real SSH infrastructure.

## Design Decisions

- **Exit code 255 is ambiguous, so it's classified by stderr content, not
  trusted blindly.** OpenSSH's client uses exit code 255 to signal its
  own connection-level failures, but a *remote command* can also
  legitimately exit 255 on its own. Checking `stderr` for known
  SSH-client error phrasing (`"connection refused"`,
  `"could not resolve hostname"`, etc.) is how the toolkit tells these
  apart — imperfect (a remote script that prints similar text could
  fool it) but far more accurate than assuming every 255 is transport-
  level.
- **Auth and host-key failures are never retried, even though they share
  exit code 255 with retryable connection failures.** Retrying a bad
  password or a rejected host key wastes time and, in the host-key case,
  actively works against the security property `StrictHostKeyChecking`
  exists to enforce — it should fail loud and fast, not quietly retry
  past it.
- **`run_on_hosts()` preserves target order in its results even when
  running in parallel.** A caller iterating over `zip(targets, results)`
  needs that pairing to be correct regardless of which host happened to
  respond first — results are written into a pre-sized list by index,
  not appended in completion order.
- **`BatchMode=yes` and `ConnectTimeout` are always set, not optional.**
  A tool meant to run unattended across many hosts must never be able to
  hang waiting for a human to type a password — this is a safety
  property, not a configurable preference.

## Algorithms Used

- Exponential backoff for retryable transport failures, same
  `min(base * 2^attempt, cap)` pattern as the API client and web
  scraper projects.
- `ThreadPoolExecutor` for parallel fan-out (I/O-bound work — waiting on
  network round trips — is exactly what threads, not processes, are
  suited for here), with results written back into an index-addressed
  list rather than collected in completion order.

## Tradeeoffs

- Depends on the `ssh`/`scp` CLI being installed rather than a Python
  SSH library (`paramiko`/`asyncssh`). This matches the "demonstrate
  direct Unix tool usage" theme of the portfolio and avoids a dependency
  that wasn't available in this environment — but a library-based
  approach would give richer, more reliable error information than
  parsing CLI stderr text.
- Transport-error classification via stderr substring matching is a
  heuristic, not a guarantee — documented explicitly in Limitations
  rather than presented as more reliable than it is.

## Edge Cases Handled

| Case | Behavior |
|---|---|
| One host in a fleet is unreachable | That host's result is `transport_error`; every other host still runs |
| Connection refused / DNS failure | Classified `transport_error`, retried with backoff |
| Wrong SSH key / rejected credentials | Classified `transport_error`, never retried |
| Host key verification failure | Classified `transport_error`, never retried |
| Remote command exits non-zero on its own | Classified `command_error`, never retried |
| Command or connection hangs | Bounded by `ConnectTimeout`/command timeout, reported as `timeout` |
| `ssh`/`scp` not installed | `SSHNotInstalledError`, clear message |
| Empty host list | Empty result list, not an error |
| Interactive password prompt would otherwise hang | Prevented entirely via `BatchMode=yes` |

## Examples

```
$ ./ssh_toolkit.py run --hosts hosts.txt --user deploy "uptime"
[web1.internal] OK
 14:32:01 up 5 days, load average: 0.15, 0.10, 0.08
[web2.internal] FAILED (transport_error)
  ssh: connect to host web2.internal port 22: Connection refused
[web3.internal] OK
 14:32:01 up 12 days, load average: 0.05, 0.04, 0.02

2/3 host(s) succeeded

$ ./ssh_toolkit.py copy --hosts hosts.txt --user deploy ./deploy.sh /tmp/deploy.sh
```

## Limitations

- Transport-vs-command error classification relies on matching known
  OpenSSH error phrasing in stderr — a remote command whose own error
  output happens to contain one of those phrases could be misclassified.
- No `rsync` support yet — `copy_to_host` is `scp`-only, which doesn't
  give incremental/delta transfer for repeated syncs of large
  directories.
- Host key policy is binary (`strict` or `accept-new`) — no support for
  pinning a specific expected host key fingerprint per target.
- Not tested against a real SSH server in this environment (none was
  available) — the retry/classification/fan-out *logic* is fully tested
  via the injected `CommandRunner`, but the literal `ssh`/`scp` argv
  construction has only been verified by inspection, not by an actual
  successful connection.

## Lessons Learned

The auth-failure and connection-refused cases share the same exit code
(255), which meant the first draft's retry logic — "retry any 255" —
would have retried a wrong SSH key three times before giving up, wasting
time and, worse, being indistinguishable in behavior from a script that
doesn't handle credentials failures at all. Writing the test for
`test_auth_failure_never_retried` *before* trusting the classification
logic (rather than assuming "exit 255 means transport problem, transport
problems are retryable" was good enough) is what surfaced the need to
split "transport error" into retryable and non-retryable sub-cases based
on stderr content.

## Future Improvements

- `rsync`-based directory sync as an alternative to `scp` for large or
  repeated transfers.
- Per-target host-key fingerprint pinning.
- Structured remote command output parsing (e.g. JSON-emitting remote
  scripts) rather than treating stdout as opaque text.

## References

- OpenSSH `ssh` and `scp` manual pages
- OpenSSH client exit status conventions
