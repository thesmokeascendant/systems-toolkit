#!/usr/bin/env python3
"""
git_health.py — inspects a Git repository for common health issues.

Runs entirely through the `git` CLI via subprocess (no GitPython
dependency), treating every git invocation as something that can fail:
git not installed, not a repository, an empty repository with no commits
yet, a corrupted `.git` directory, or a command that just hangs.

Usage:
    from git_health import check_repository

    report = check_repository("/path/to/repo")
    for finding in report.findings:
        print(finding.level, finding.message)

CLI:
    ./git_health.py /path/to/repo
    ./git_health.py /path/to/repo --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STALE_BRANCH_DAYS = 90
LARGE_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
GIT_TIMEOUT_SECONDS = 15


class GitHealthError(Exception):
    """Base class for all git_health-raised errors."""


class GitNotInstalledError(GitHealthError):
    """The `git` executable could not be found on PATH."""


class NotAGitRepositoryError(GitHealthError):
    """The target path is not inside a Git working tree."""


class GitCommandTimeoutError(GitHealthError):
    """A git command did not complete within the timeout."""


@dataclass
class Finding:
    level: str  # "info", "warning", "critical"
    code: str
    message: str


@dataclass
class HealthReport:
    repo_path: str
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, code: str, message: str) -> None:
        self.findings.append(Finding(level, code, message))

    @property
    def has_critical(self) -> bool:
        return any(f.level == "critical" for f in self.findings)

    def as_dict(self) -> dict:
        return {"repo_path": self.repo_path, "findings": [asdict(f) for f in self.findings]}


def run_git(args: list[str], cwd: str, timeout: int = GIT_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """
    Run a git command defensively. Returns (returncode, stdout, stderr).
    Never raises for a non-zero exit code — that's meaningful data for
    the caller (e.g. `git rev-parse` failing IS how you detect "not a
    repo"), not an exceptional condition.
    """
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise GitNotInstalledError("git executable not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise GitCommandTimeoutError(f"git {' '.join(args)} timed out after {timeout}s") from e
    return result.returncode, result.stdout, result.stderr


def _require_repository(repo_path: str) -> None:
    code, out, _ = run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo_path)
    if code != 0 or out.strip() != "true":
        raise NotAGitRepositoryError(f"not a git repository: {repo_path}")


def _has_commits(repo_path: str) -> bool:
    code, _, _ = run_git(["rev-parse", "--verify", "HEAD"], cwd=repo_path)
    return code == 0


def _check_working_tree(repo_path: str, report: HealthReport) -> None:
    code, out, _ = run_git(["status", "--porcelain=v1"], cwd=repo_path)
    if code != 0:
        report.add("warning", "status_failed", "could not read working tree status")
        return

    staged = modified = untracked = conflicted = 0
    for line in out.splitlines():
        if not line:
            continue
        index_status, worktree_status = line[0], line[1]
        if index_status == "U" or worktree_status == "U" or line.startswith("UU"):
            conflicted += 1
        elif index_status not in (" ", "?"):
            staged += 1
        if worktree_status == "M":
            modified += 1
        if line.startswith("??"):
            untracked += 1

    if conflicted:
        report.add("critical", "merge_conflicts", f"{conflicted} file(s) have unresolved merge conflicts")
    if staged:
        report.add("info", "staged_changes", f"{staged} file(s) staged but not committed")
    if modified:
        report.add("warning", "uncommitted_changes", f"{modified} tracked file(s) modified but not staged")
    if untracked:
        level = "warning" if untracked > 20 else "info"
        report.add(level, "untracked_files", f"{untracked} untracked file(s) in the working tree")


def _check_detached_head(repo_path: str, report: HealthReport) -> None:
    code, out, _ = run_git(["symbolic-ref", "-q", "HEAD"], cwd=repo_path)
    if code != 0:
        report.add("warning", "detached_head", "repository is in detached HEAD state")


def _check_upstream(repo_path: str, report: HealthReport) -> None:
    code, out, _ = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=repo_path)
    if code != 0:
        report.add("info", "no_upstream", "current branch has no upstream tracking branch configured")
        return

    code, out, _ = run_git(["rev-list", "--count", "@{u}..HEAD"], cwd=repo_path)
    if code == 0 and out.strip().isdigit() and int(out.strip()) > 0:
        n = int(out.strip())
        report.add("info", "unpushed_commits", f"{n} commit(s) ahead of upstream, not pushed")


def _check_stale_branches(repo_path: str, report: HealthReport, stale_days: int = STALE_BRANCH_DAYS) -> None:
    code, out, _ = run_git(
        ["for-each-ref", "--format=%(refname:short) %(committerdate:iso-strict)", "refs/heads/"],
        cwd=repo_path,
    )
    if code != 0 or not out.strip():
        return

    now = datetime.now(timezone.utc)
    stale = []
    for line in out.strip().splitlines():
        try:
            branch, iso_date = line.rsplit(" ", 1)
            commit_date = datetime.fromisoformat(iso_date)
        except ValueError:
            continue  # malformed line — skip rather than crash the whole check
        age_days = (now - commit_date).days
        if age_days > stale_days:
            stale.append((branch, age_days))

    for branch, age_days in stale:
        report.add("info", "stale_branch", f"branch '{branch}' has no commits in {age_days} days")


def _check_large_files(repo_path: str, report: HealthReport, threshold: int = LARGE_FILE_BYTES) -> None:
    code, out, _ = run_git(["ls-files"], cwd=repo_path)
    if code != 0:
        return

    root = Path(repo_path)
    large_files = []
    for rel_path in out.splitlines():
        if not rel_path:
            continue
        full_path = root / rel_path
        try:
            size = full_path.stat().st_size
        except OSError:
            continue  # file listed by git but unreadable/missing on disk — skip
        if size > threshold:
            large_files.append((rel_path, size))

    for rel_path, size in large_files:
        mb = size / (1024 * 1024)
        report.add("warning", "large_tracked_file", f"'{rel_path}' is {mb:.1f} MB and tracked in git")


def _check_conflict_markers(repo_path: str, report: HealthReport) -> None:
    """
    Look for leftover merge-conflict markers in tracked files — this
    catches the case where someone resolved a conflict by eye but missed
    a marker, which `git status` alone won't show once the file is staged.
    """
    code, out, _ = run_git(["ls-files"], cwd=repo_path)
    if code != 0:
        return

    root = Path(repo_path)
    markers = ("<<<<<<<", "=======", ">>>>>>>")
    flagged = []
    for rel_path in out.splitlines():
        full_path = root / rel_path
        try:
            content = full_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(content.startswith(m) or f"\n{m}" in content for m in markers):
            flagged.append(rel_path)

    for rel_path in flagged:
        report.add("critical", "leftover_conflict_markers", f"'{rel_path}' contains conflict marker text")


def check_repository(repo_path: str, stale_days: int = STALE_BRANCH_DAYS) -> HealthReport:
    """Run the full health check suite against a repository path."""
    report = HealthReport(repo_path=repo_path)

    path = Path(repo_path)
    if not path.exists():
        raise FileNotFoundError(f"path does not exist: {repo_path}")
    if not path.is_dir():
        raise NotADirectoryError(f"not a directory: {repo_path}")

    _require_repository(repo_path)

    if not _has_commits(repo_path):
        report.add("info", "empty_repository", "repository has no commits yet")
        return report  # nothing else is meaningful to check on an empty repo

    _check_working_tree(repo_path, report)
    _check_detached_head(repo_path, report)
    _check_upstream(repo_path, report)
    _check_stale_branches(repo_path, report, stale_days=stale_days)
    _check_large_files(repo_path, report)
    _check_conflict_markers(repo_path, report)

    if not report.findings:
        report.add("info", "clean", "no issues found")

    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check a Git repository for common health issues")
    parser.add_argument("repo_path", nargs="?", default=".")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--stale-days", type=int, default=STALE_BRANCH_DAYS)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        report = check_repository(args.repo_path, stale_days=args.stale_days)
    except (FileNotFoundError, NotADirectoryError, NotAGitRepositoryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except GitNotInstalledError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except GitCommandTimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(f"Health report for {report.repo_path}")
        for f in report.findings:
            print(f"  [{f.level.upper():8}] {f.code}: {f.message}")

    return 1 if report.has_critical else 0


if __name__ == "__main__":
    sys.exit(main())
