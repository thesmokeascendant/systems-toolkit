# Engineering Journal — SSH Automation Toolkit

## Problem

Needed a multi-host command runner where "the host is unreachable" and
"the command I ran on it failed" are distinguishable outcomes, and where
a bad password or unreachable host on one machine in a list of fifty
never blocks the other forty-nine.

## Initial Idea

First draft treated any non-zero `ssh` exit code as a single
`"failed"` category and retried all of them uniformly with backoff.

## Why It Failed

OpenSSH's client uses exit code 255 for its own connection-level
failures — but that one code covers a wide range of very different
problems: connection refused (worth retrying), a wrong SSH key (retrying
is pointless and wastes time), and a rejected host key (retrying is
actively wrong — that's a security check functioning correctly, not a
transient hiccup). Treating all of them as "retry with backoff" meant a
misconfigured credential would burn through the full retry budget every
single run, adding real latency to a fleet-wide command for a failure
mode retrying can never fix.

## Alternative Approaches Considered

1. **Never retry anything on exit code 255 — require the caller to retry
   the whole toolkit invocation manually.** Rejected — this throws away
   real value for the genuinely transient cases (a host that's mid-reboot,
   a momentary network blip), which are common enough in fleet automation
   to be worth handling automatically.
2. **Retry every 255 uniformly (the original approach).** Rejected once
   `test_auth_failure_never_retried` was written and I actually thought
   through what a wrong SSH key does under this policy — three wasted
   attempts, every run, for a failure that will never resolve itself.
3. **Classify 255 by inspecting stderr for known OpenSSH error phrasing,
   split into retryable vs non-retryable.** Chosen. Not perfectly robust
   (a remote command could theoretically print similar text and get
   misclassified — documented explicitly in the README's Limitations
   section rather than glossed over), but correct for the actual OpenSSH
   client's own error messages, which is what this toolkit is built
   against.

## Benchmarking / Performance Observations

Not applicable — no real SSH server was available to benchmark against
in this environment (no `sshd`, no `paramiko`, no network egress). The
parallel fan-out (`ThreadPoolExecutor`) is architecturally the right
choice for I/O-bound multi-host work regardless, since the bottleneck in
real usage is network round-trip time per host, not CPU.

## Refactoring Decisions

Pulled failure classification into its own `_classify_failure()`
function, separate from `run_on_host()`'s retry loop, specifically so it
could be tested (indirectly, via `run_on_host`'s behavior) without
needing to also exercise the retry timing logic for every classification
case. Before this split, "does the retry loop stop at the right time"
and "did we correctly identify this as an auth failure" were tangled
into one code path, which made it hard to write a focused test for
either concern alone.

## Final Implementation

`run_on_host()` owns the retry loop; `_classify_failure()` is a pure
function of `(returncode, stderr)` that returns both a category label
and whether it's worth retrying. `run_on_hosts()` fans out via
`ThreadPoolExecutor`, writing results into a pre-sized list indexed by
the target's original position so result order matches input order
regardless of completion order under parallel execution.

## Engineering Tradeoffs

Chose CLI-wrapping (`ssh`/`scp` via subprocess) over a Python SSH library
specifically because `paramiko` wasn't installable in this environment
(no network egress for `pip install`) and because it better demonstrates
direct Unix tool usage, which is this portfolio's stated theme. The real
cost: error information is limited to whatever OpenSSH prints to stderr,
which is why the whole transport-vs-command classification problem
exists in the first place — a library like `paramiko` would raise
distinct, structured exception types instead of requiring stderr text
matching.

## Possible Future Versions

- `rsync`-based sync as an alternative to `scp` for large/repeated
  directory transfers.
- Structured (JSON) remote output parsing for hosts running a companion
  agent script, instead of treating stdout as opaque text.
- Host-key fingerprint pinning per target, instead of the current binary
  strict/accept-new policy.
