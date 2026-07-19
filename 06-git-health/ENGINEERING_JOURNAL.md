# Engineering Journal — Git Repository Health Checker

## Problem

Needed a health check that correctly handles the git states that are
normal but easy to forget when writing the first draft: a brand new repo
with zero commits, a detached HEAD checkout, a branch with no upstream.
Each of these makes at least one "obvious" git command behave differently
than it does on a normal, fully-set-up repository.

## Initial Idea

First draft ran `git log -1 --format=%cI <branch>` in a Python loop over
every local branch to compute staleness, and called `git rev-parse HEAD`
unconditionally at the start of every check function.

## Why It Failed

Two separate issues, found by deliberately testing against an empty repo
and a multi-branch repo before writing the formal test suite:

1. **Empty repo crash.** `git rev-parse HEAD` on a repository with no
   commits exits non-zero with "unknown revision or path not in the
   working tree" on stderr. Every check function that assumed `HEAD`
   existed (which was most of them) either produced a confusing empty
   result or a misleading finding. The fix was checking
   `_has_commits()` once, up front, and short-circuiting the rest of the
   suite for an empty repo rather than trying to make every individual
   check independently empty-repo-safe.
2. **Per-branch `git log` loop.** Worked, but spawned one subprocess per
   branch. For a repository with dozens of branches, that's dozens of
   process spawns for information `git for-each-ref` can return in one
   call. Not a correctness bug, but a real inefficiency that also meant
   per-branch error handling was duplicated N times instead of once.

## Alternative Approaches Considered

For the empty-repo problem:
1. **Wrap every check function's git calls in try/except and return a
   neutral result on failure.** Rejected — this would silently produce
   partial, misleading reports (e.g. "0 untracked files" on a repo where
   the real answer is "this check couldn't run at all") rather than
   being honest that there's nothing meaningful to check yet.
2. **Check `_has_commits()` once in `check_repository()` and skip the
   rest of the suite entirely if false.** Chosen — produces exactly one
   clear, honest finding (`empty_repository`) instead of a report full of
   checks that silently no-op.

For the stale-branch performance issue:
1. **Keep the per-branch loop, add a process pool for parallelism.**
   Rejected — adds real complexity (worker management, error aggregation
   across processes) to fix a problem that has a simpler fix.
2. **Single `git for-each-ref --format=...` call returning name + date
   for every branch at once.** Chosen. One subprocess call total,
   regardless of branch count, and the per-line parsing failure mode
   (malformed output) is handled once instead of N times.

## Benchmarking / Performance Observations

Not formally benchmarked, but the `for-each-ref` change is a clear
algorithmic improvement (O(1) subprocess spawns vs O(branches)) rather
than a marginal one — spawning a subprocess is expensive relative to the
actual git work being done for a single branch's log entry.

## Refactoring Decisions

Kept every check as a `_check_*(repo_path, report)` function that
appends to a shared `HealthReport` rather than returning its own result
object that `check_repository()` would need to merge. This was a
deliberate choice after the first draft had each check return a list of
findings that then needed concatenating — functionally identical, but
appending directly to the shared report removed a whole category of
"did I remember to include this check's results in the final list"
mistakes.

## Final Implementation

`run_git()` is the single point of contact with `subprocess`, and the
only place that raises for git-absent or timeout. Every other function
treats a non-zero git exit code as data to branch on, not an exception.
`check_repository()` orchestrates: validate the path, confirm it's a
repo, confirm it has commits, then run the full check suite in sequence.

## Engineering Tradeoffs

Chose to shell out to the real `git` CLI over using GitPython. This adds
a small amount of output-parsing responsibility (choosing `--porcelain=v1`
specifically, for instance) that a library would otherwise handle, but
it keeps the project's dependency footprint at zero beyond `git` itself
being installed — appropriate for a tool whose whole purpose is
demonstrating direct, correct use of git plumbing commands.

## Possible Future Versions

- Git-LFS-aware large-file detection.
- `git count-objects` based repository bloat checks.
- A `--since <commit-range>` mode that limits file-content scanning
  (large files, conflict markers) to files touched in recent history,
  for faster repeated runs on large repositories.
