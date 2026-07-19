"""
Unit tests for git_health.py

Every test operates on a real git repository created in a temporary
directory via the actual `git` CLI (git init, commit, branch, etc.) —
no mocking of git itself, since the whole point of this tool is to
interpret real git output correctly.

Run with:
    python3 -m unittest discover -s tests -v
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from git_health import (  # noqa: E402
    NotAGitRepositoryError,
    check_repository,
)


def git(args: list[str], cwd: str) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True)


class GitRepoTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        git(["init", "-q"], cwd=self.repo)
        git(["config", "user.email", "test@example.com"], cwd=self.repo)
        git(["config", "user.name", "Test User"], cwd=self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, content: str = "content\n") -> Path:
        p = Path(self.repo) / name
        p.write_text(content)
        return p

    def commit(self, message: str = "commit") -> None:
        git(["add", "-A"], cwd=self.repo)
        git(["commit", "-q", "-m", message], cwd=self.repo)

    def findings_by_code(self, report):
        return {f.code: f for f in report.findings}


class TestNonRepository(unittest.TestCase):
    def test_non_repo_directory_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(NotAGitRepositoryError):
                check_repository(tmp)

    def test_missing_path_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            check_repository("/definitely/does/not/exist/anywhere")


class TestEmptyRepository(GitRepoTestCase):
    def test_empty_repo_flagged_and_no_other_checks_run(self):
        report = check_repository(self.repo)
        codes = self.findings_by_code(report)
        self.assertIn("empty_repository", codes)
        # An empty repo has no HEAD, so nothing else should have run —
        # exactly one finding.
        self.assertEqual(len(report.findings), 1)


class TestCleanRepository(GitRepoTestCase):
    def test_clean_repo_reports_clean(self):
        self.write("file.txt")
        self.commit()
        report = check_repository(self.repo)
        codes = self.findings_by_code(report)
        # no_upstream is always present in a local-only repo — that's
        # expected and not itself "dirty" — but no working-tree issues.
        self.assertNotIn("uncommitted_changes", codes)
        self.assertNotIn("untracked_files", codes)
        self.assertNotIn("merge_conflicts", codes)


class TestWorkingTreeState(GitRepoTestCase):
    def test_modified_tracked_file_detected(self):
        self.write("file.txt")
        self.commit()
        self.write("file.txt", "changed\n")
        report = check_repository(self.repo)
        self.assertIn("uncommitted_changes", self.findings_by_code(report))

    def test_untracked_file_detected(self):
        self.write("file.txt")
        self.commit()
        self.write("new_file.txt")
        report = check_repository(self.repo)
        self.assertIn("untracked_files", self.findings_by_code(report))

    def test_staged_file_detected(self):
        self.write("file.txt")
        self.commit()
        self.write("staged.txt")
        git(["add", "staged.txt"], cwd=self.repo)
        report = check_repository(self.repo)
        self.assertIn("staged_changes", self.findings_by_code(report))

    def test_many_untracked_files_escalates_to_warning(self):
        self.write("file.txt")
        self.commit()
        for i in range(25):
            self.write(f"untracked_{i}.txt")
        report = check_repository(self.repo)
        finding = self.findings_by_code(report)["untracked_files"]
        self.assertEqual(finding.level, "warning")


class TestDetachedHead(GitRepoTestCase):
    def test_detached_head_detected(self):
        self.write("file.txt")
        self.commit()
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True
        )
        commit_sha = result.stdout.strip()
        git(["checkout", "-q", commit_sha], cwd=self.repo)
        report = check_repository(self.repo)
        self.assertIn("detached_head", self.findings_by_code(report))

    def test_normal_branch_not_flagged_as_detached(self):
        self.write("file.txt")
        self.commit()
        report = check_repository(self.repo)
        self.assertNotIn("detached_head", self.findings_by_code(report))


class TestUpstream(GitRepoTestCase):
    def test_no_upstream_flagged_for_local_only_repo(self):
        self.write("file.txt")
        self.commit()
        report = check_repository(self.repo)
        self.assertIn("no_upstream", self.findings_by_code(report))


class TestStaleBranches(GitRepoTestCase):
    def test_recent_branch_not_flagged_stale(self):
        self.write("file.txt")
        self.commit()
        git(["branch", "recent-branch"], cwd=self.repo)
        report = check_repository(self.repo, stale_days=90)
        self.assertNotIn("stale_branch", self.findings_by_code(report))

    def test_old_branch_flagged_stale_with_low_threshold(self):
        self.write("file.txt")
        self.commit()
        git(["branch", "old-branch"], cwd=self.repo)
        # Use a 0-day threshold so "just committed" already counts as
        # stale — avoids needing to fabricate a commit in the past.
        report = check_repository(self.repo, stale_days=-1)
        self.assertIn("stale_branch", self.findings_by_code(report))


class TestLargeFiles(GitRepoTestCase):
    def test_large_tracked_file_flagged(self):
        big = Path(self.repo) / "big.bin"
        big.write_bytes(b"\0" * (11 * 1024 * 1024))
        self.commit()
        report = check_repository(self.repo)
        self.assertIn("large_tracked_file", self.findings_by_code(report))

    def test_small_tracked_file_not_flagged(self):
        self.write("small.txt", "tiny content\n")
        self.commit()
        report = check_repository(self.repo)
        self.assertNotIn("large_tracked_file", self.findings_by_code(report))


class TestConflictMarkers(GitRepoTestCase):
    def test_leftover_conflict_markers_flagged_critical(self):
        self.write(
            "conflicted.txt",
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n",
        )
        self.commit()
        report = check_repository(self.repo)
        finding = self.findings_by_code(report)["leftover_conflict_markers"]
        self.assertEqual(finding.level, "critical")
        self.assertTrue(report.has_critical)

    def test_normal_file_not_falsely_flagged(self):
        self.write("normal.txt", "just some equals === signs mid-line, not a conflict\n")
        self.commit()
        report = check_repository(self.repo)
        self.assertNotIn("leftover_conflict_markers", self.findings_by_code(report))


if __name__ == "__main__":
    unittest.main()
