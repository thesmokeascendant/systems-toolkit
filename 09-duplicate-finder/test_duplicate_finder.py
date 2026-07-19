"""
Unit tests for duplicate_finder.py

Uses real temporary directories for filesystem structure (duplicates,
symlinks, a directory symlink loop). Permission-error handling is tested
via unittest.mock.patch on Path.open/Path.stat rather than real chmod,
since this container runs as root, where chmod-based denial doesn't
apply — the same problem solved with dependency injection in the file
organizer project (05), solved here with targeted mocking instead since
duplicate_finder's three-stage funnel doesn't have a single clean
injection point the way a single move_fn call does.

Run with:
    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duplicate_finder import (  # noqa: E402
    _full_hash,
    _partial_hash,
    find_duplicates,
)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, content: bytes) -> Path:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p


class TestFindDuplicatesBasics(TempDirTestCase):
    def test_identical_files_grouped_as_duplicates(self):
        self.write("a.txt", b"hello world")
        self.write("b.txt", b"hello world")
        report = find_duplicates(str(self.root))
        self.assertEqual(len(report.duplicate_groups), 1)
        self.assertEqual(len(report.duplicate_groups[0]), 2)

    def test_different_content_not_grouped(self):
        self.write("a.txt", b"hello world")
        self.write("b.txt", b"something else entirely")
        report = find_duplicates(str(self.root))
        self.assertEqual(report.duplicate_groups, [])

    def test_same_size_different_content_not_falsely_grouped(self):
        # Same length, different bytes — must not collide by size alone.
        self.write("a.txt", b"AAAAAAAAAA")
        self.write("b.txt", b"BBBBBBBBBB")
        report = find_duplicates(str(self.root))
        self.assertEqual(report.duplicate_groups, [])

    def test_three_way_duplicate_grouped_together(self):
        self.write("a.txt", b"same content")
        self.write("b.txt", b"same content")
        self.write("c.txt", b"same content")
        report = find_duplicates(str(self.root))
        self.assertEqual(len(report.duplicate_groups), 1)
        self.assertEqual(len(report.duplicate_groups[0]), 3)

    def test_duplicates_in_different_subdirectories_found(self):
        self.write("top.txt", b"nested dupe")
        self.write("a/b/c/nested.txt", b"nested dupe")
        report = find_duplicates(str(self.root))
        self.assertEqual(len(report.duplicate_groups), 1)

    def test_empty_directory_produces_no_duplicates(self):
        report = find_duplicates(str(self.root))
        self.assertEqual(report.duplicate_groups, [])
        self.assertEqual(report.files_scanned, 0)

    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_duplicates(str(self.root / "nope"))

    def test_root_is_a_file_raises(self):
        f = self.write("just_a_file.txt", b"x")
        with self.assertRaises(NotADirectoryError):
            find_duplicates(str(f))


class TestMinSize(TempDirTestCase):
    def test_empty_files_excluded_by_default_min_size(self):
        self.write("empty1.txt", b"")
        self.write("empty2.txt", b"")
        report = find_duplicates(str(self.root))  # default min_size=1
        self.assertEqual(report.duplicate_groups, [])

    def test_empty_files_included_with_min_size_zero(self):
        self.write("empty1.txt", b"")
        self.write("empty2.txt", b"")
        report = find_duplicates(str(self.root), min_size=0)
        self.assertEqual(len(report.duplicate_groups), 1)


class TestSymlinkHandling(TempDirTestCase):
    def test_symlink_to_file_not_treated_as_a_duplicate(self):
        real = self.write("real.txt", b"content")
        link = self.root / "link.txt"
        link.symlink_to(real)
        report = find_duplicates(str(self.root))
        self.assertEqual(report.duplicate_groups, [])
        self.assertEqual(report.files_skipped_symlink, 1)
        self.assertEqual(report.files_scanned, 1)  # only the real file

    def test_broken_symlink_does_not_crash(self):
        target = self.root / "does_not_exist.txt"
        link = self.root / "broken.txt"
        link.symlink_to(target)
        report = find_duplicates(str(self.root))  # must not raise
        self.assertEqual(report.files_skipped_symlink, 1)

    def test_directory_symlink_loop_does_not_hang_or_recurse(self):
        self.write("a.txt", b"content")
        subdir = self.root / "subdir"
        subdir.mkdir()
        loop_link = subdir / "loop_back"
        loop_link.symlink_to(self.root)  # points back to an ancestor
        # This must complete at all (a naive recursive walker would
        # hang or blow the stack here).
        report = find_duplicates(str(self.root))
        self.assertEqual(report.files_scanned, 1)


class TestHashingFunctions(TempDirTestCase):
    def test_partial_hash_matches_for_identical_prefixes(self):
        a = self.write("a.txt", b"X" * 10000)
        b = self.write("b.txt", b"X" * 10000)
        self.assertEqual(_partial_hash(a), _partial_hash(b))

    def test_partial_hash_differs_for_different_content(self):
        a = self.write("a.txt", b"X" * 10000)
        b = self.write("b.txt", b"Y" * 10000)
        self.assertNotEqual(_partial_hash(a), _partial_hash(b))

    def test_full_hash_streams_large_file_without_error(self):
        # A few MB, larger than one chunk, to exercise the streaming loop
        # (multiple read() calls) rather than a single-read shortcut.
        big = self.write("big.bin", b"\0" * (3 * 1024 * 1024 + 123))
        digest = _full_hash(big, chunk_size=1024 * 1024)
        self.assertIsNotNone(digest)
        self.assertEqual(len(digest), 64)  # sha256 hex digest length

    def test_partial_hash_returns_none_on_unreadable_file(self):
        path = self.write("unreadable.txt", b"content")
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertIsNone(_partial_hash(path))

    def test_full_hash_returns_none_on_unreadable_file(self):
        path = self.write("unreadable.txt", b"content")
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertIsNone(_full_hash(path))


class TestPermissionErrorIsolation(TempDirTestCase):
    def test_one_unreadable_file_does_not_stop_the_rest_of_the_scan(self):
        self.write("good_a.txt", b"duplicate content")
        self.write("good_b.txt", b"duplicate content")
        bad = self.write("bad.txt", b"duplicate content")

        real_open = Path.open

        def flaky_open(self_path, *args, **kwargs):
            if self_path == bad:
                raise PermissionError("simulated permission denied")
            return real_open(self_path, *args, **kwargs)

        with patch.object(Path, "open", flaky_open):
            report = find_duplicates(str(self.root))

        # The two readable duplicates are still found despite bad.txt
        # failing to hash.
        self.assertEqual(len(report.duplicate_groups), 1)
        self.assertEqual(len(report.duplicate_groups[0]), 2)
        self.assertGreaterEqual(report.files_skipped_error, 1)


class TestReportStats(TempDirTestCase):
    def test_wasted_bytes_computed_correctly(self):
        self.write("a.txt", b"1234567890")  # 10 bytes
        self.write("b.txt", b"1234567890")
        self.write("c.txt", b"1234567890")
        report = find_duplicates(str(self.root))
        # 3 copies of a 10-byte file → 2 copies are "wasted"
        self.assertEqual(report.wasted_bytes, 20)

    def test_bytes_scanned_reflects_min_size_filter(self):
        self.write("tiny.txt", b"")  # excluded by default min_size=1
        self.write("real.txt", b"hello")
        report = find_duplicates(str(self.root))
        self.assertEqual(report.bytes_scanned, 5)

    def test_progress_callback_invoked(self):
        for i in range(10):
            self.write(f"file_{i}.txt", f"content {i}".encode())
        calls = []
        find_duplicates(str(self.root), progress_fn=calls.append, progress_interval=3)
        self.assertGreater(len(calls), 0)


if __name__ == "__main__":
    unittest.main()
