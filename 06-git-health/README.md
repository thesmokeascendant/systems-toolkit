# 06 — Git Repository Health Checker

A defensive Git repository inspector, built directly on the `git` CLI via
`subprocess`. Detects dirty working trees, detached HEAD, stale branches,
unpushed commits, oversized tracked files, and leftover merge-conflict
markers — all without needing a GitPython-style dependency.

## Problem

"Is this repository in good shape?" is a question people ask by running
five or six different `git` commands and mentally combining the results.
Automating that combination is straightforward in the happy case, but a
naive script breaks the moment it's pointed at something that isn't a
repository, a repository with zero commits, or a detached-HEAD checkout —
all of which are completely normal states for a real repository to be in.

## Requirements

- Detect whether a path is a git repository at all, and fail clearly (not
  with a raw `git` stderr dump) if it isn't.
- Handle a freshly-initialized repository with no commits without
  crashing on commands that assume `HEAD` exists.
- Report working-tree state: staged changes, unstaged modifications,
  untracked files, unresolved merge conflicts.
- Detect detached HEAD state.
- Detect branches with no commits in N days (stale branches).
- Detect commits ahead of the configured upstream (unpushed work), and
  flag when no upstream is configured at all.
- Flag large tracked files (a common source of bloated repositories).
- Flag leftover merge-conflict marker text in tracked files — a conflict
  resolved carelessly by hand can leave `<<<<<<<`/`=======`/`>>>>>>>`
  lines committed without `git status` ever showing it once the file is
  staged.
- Never let git being absent, a command timing out, or an unreadable file
  crash the whole check run.

## Architecture

```
git_health.py
├── run_git()                  # defensive subprocess wrapper
├── _require_repository()      # git rev-parse --is-inside-work-tree
├── _has_commits()              # empty-repo guard
├── _check_working_tree()      # git status --porcelain parsing
├── _check_detached_head()
├── _check_upstream()          # ahead-count + missing-upstream detection
├── _check_stale_branches()    # for-each-ref + committerdate
├── _check_large_files()       # git ls-files + stat()
├── _check_conflict_markers()  # git ls-files + content scan
└── check_repository()         # orchestrates all checks into a HealthReport
```

Every check function takes the already-open `HealthReport` and appends
findings to it rather than returning its own result type — this keeps
`check_repository()`'s orchestration simple (call each check in order)
and means adding a new check is a one-line addition, not a change to a
result-merging step.

## Design Decisions

- **`subprocess` + the real `git` CLI, not GitPython.** This project is
  explicitly about demonstrating Unix tool fluency — shelling out to
  `git` and parsing its plumbing-command output (`for-each-ref`,
  `ls-files`, `status --porcelain=v1`) is the more direct demonstration
  of that than wrapping a Python library that does it for you.
- **`git status --porcelain=v1` explicitly, not the default porcelain
  format.** The default porcelain format's exact field layout isn't
  guaranteed stable across git versions the way `v1` is documented to be
  — parsing untrusted-in-the-sense-of-changing CLI output needs a pinned
  format.
- **An empty repository short-circuits all other checks.** Almost every
  other git command in this tool assumes `HEAD` resolves to something;
  running them against a repo with zero commits either errors or returns
  meaningless output. Checking `_has_commits()` first and returning early
  avoids a cascade of confusing findings for what is really just one
  fact: "this repo has no commits yet."
- **Non-zero exit codes from `git` are data, not exceptions.**
  `git rev-parse --is-inside-work-tree` failing IS how you detect "not a
  repository" — raising an exception on every non-zero git exit would
  make the most basic detection check impossible to write cleanly.
  `run_git()` only raises for git being *absent* or *timing out*, both of
  which are genuinely exceptional.

## Algorithms Used

- Line-by-line parsing of `git status --porcelain=v1` output using the
  documented two-character status code format.
- `git for-each-ref` with a custom `--format` to get branch name + ISO
  commit date in one call, instead of one `git log` call per branch.

## Tradeoffs

- Large-file and conflict-marker checks scan every file `git ls-files`
  returns, reading file content for the conflict-marker check. For a
  very large repository (tens of thousands of tracked files) this is the
  slowest part of the tool — acceptable for a health-check run by a
  developer on demand, not necessarily for a pre-commit hook on every
  commit. A future version could restrict the content scan to recently
  changed files only.
- Stale-branch detection only looks at local branches
  (`refs/heads/`), not remote-tracking branches — a repository's remote
  branch list can be huge and isn't something the local checkout is
  responsible for cleaning up.

## Edge Cases Handled

| Case | Behavior |
|---|---|
| Path is not a git repository | `NotAGitRepositoryError`, clear message |
| Path doesn't exist | `FileNotFoundError` |
| Repository has zero commits | Single `empty_repository` finding, other checks skipped |
| Detached HEAD | Flagged as a warning |
| Modified/staged/untracked files | Each reported separately with counts |
| Unresolved merge conflicts in working tree | Flagged critical |
| Leftover conflict-marker text already committed | Flagged critical (separately from working-tree conflicts) |
| No upstream configured | Flagged info (expected for local-only repos, not itself "bad") |
| Commits ahead of upstream | Reported with count |
| Branch with no recent commits | Flagged info, configurable threshold |
| Large tracked file | Flagged warning with size |
| `git` not installed | `GitNotInstalledError`, clear message, no traceback |
| `git` command hangs | `GitCommandTimeoutError` after a bounded timeout |
| File git tracks but is missing/unreadable on disk | Skipped in size/conflict checks, not a crash |
| Malformed `for-each-ref` output line | Skipped, doesn't abort branch-staleness check |

## Examples

```
$ ./git_health.py ~/projects/myapp
Health report for /home/user/projects/myapp
  [WARNING ] uncommitted_changes: 2 tracked file(s) modified but not staged
  [INFO    ] untracked_files: 3 untracked file(s) in the working tree
  [INFO    ] no_upstream: current branch has no upstream tracking branch configured

$ ./git_health.py ~/projects/myapp --json
{
  "repo_path": "/home/user/projects/myapp",
  "findings": [...]
}
```

Exit code is `1` if any finding is `critical` (unresolved conflicts or
leftover conflict-marker text), `0` otherwise — usable directly in CI.

## Limitations

- Doesn't check remote-tracking branch staleness, only local branches.
- Large-file threshold is a flat byte count, not Git-LFS-aware — a
  properly LFS-tracked large file will still be flagged since this tool
  checks the working-tree file size, not whether it's a pointer file.
- No check for repository-wide `.git` object store bloat (e.g. via
  `git count-objects`) — this focuses on working-tree and branch
  hygiene, not storage-level repository health.

## Lessons Learned

The first version of the stale-branch check used `git log -1
--format=%cI <branch>` in a loop over every branch — one subprocess call
per branch. Switched to a single `git for-each-ref --format=...` call
that returns every branch's name and commit date at once, both for
performance (one process spawn instead of N) and because it removes an
entire category of per-branch error handling (a failed `git log` call on
one branch no longer needs special-casing inside a loop).

## Future Improvements

- Git-LFS-aware large file detection (skip files that are actually LFS
  pointers).
- `git count-objects -v` based repository bloat/gc-needed detection.
- Optional check restricted to files changed in the last N commits, for
  faster pre-commit-hook usage on large repositories.

## References

- `git status` documentation, `--porcelain=v1` format specification
- `git for-each-ref` documentation
- `git rev-parse` documentation
