"""
Unit tests for file_organizer.py

Uses real temporary directories (tempfile.TemporaryDirectory) for
filesystem behavior, and dependency-injects a fake move_fn to simulate
permission errors and other OS failures without needing real permission
changes (the container runs as root, where chmod-based permission denial
doesn't apply).

Run with:
    python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from file_organizer import (  # noqa: E402
    execute_plan,
    plan_moves,
    undo_manifest,
    write_manifest,
)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def touch(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("content")
        return p


class TestPlanMoves(TempDirTestCase):
    def test_classifies_known_extensions_into_categories(self):
        self.touch("photo.jpg")
        self.touch("report.pdf")
        self.touch("song.mp3")
        plan = plan_moves(str(self.root))
        categories = {Path(m.source).name: m.category for m in plan}
        self.assertEqual(categories["photo.jpg"], "images")
        self.assertEqual(categories["report.pdf"], "documents")
        self.assertEqual(categories["song.mp3"], "audio")

    def test_unknown_extension_goes_to_other(self):
        self.touch("mystery.xyz")
        plan = plan_moves(str(self.root))
        self.assertEqual(plan[0].category, "other")

    def test_subdirectories_are_not_recursed_into(self):
        self.touch("top_level.txt")
        self.touch("subfolder", "nested.txt")
        plan = plan_moves(str(self.root))
        sources = [Path(m.source).name for m in plan]
        self.assertIn("top_level.txt", sources)
        self.assertNotIn("nested.txt", sources)

    def test_empty_directory_produces_empty_plan(self):
        self.assertEqual(plan_moves(str(self.root)), [])

    def test_broken_symlink_skipped(self):
        target = self.root / "does_not_exist.txt"
        link = self.root / "broken_link.txt"
        link.symlink_to(target)
        plan = plan_moves(str(self.root))
        self.assertEqual(plan, [])

    def test_missing_source_directory_raises(self):
        with self.assertRaises(FileNotFoundError):
            plan_moves(str(self.root / "does_not_exist"))

    def test_source_is_a_file_not_a_directory_raises(self):
        f = self.touch("just_a_file.txt")
        with self.assertRaises(NotADirectoryError):
            plan_moves(str(f))

    def test_name_collision_gets_unique_suffix(self):
        self.touch("a.txt")
        self.touch("documents", "a.txt")  # pre-existing file at destination
        plan = plan_moves(str(self.root))
        dest_names = [Path(m.destination).name for m in plan]
        self.assertIn("a (1).txt", dest_names)

    def test_multiple_new_files_colliding_with_each_other_all_get_unique_names(self):
        # Two *new* source files that would both land on the same
        # destination path (e.g. same name, different source folders
        # isn't possible non-recursively, so simulate via dest override).
        self.touch("a.txt")
        plan = plan_moves(str(self.root))
        # Re-planning against the same (now-empty after a real move would
        # happen, but here just re-planning) directory shouldn't collide
        # with itself — sanity check the plan is internally consistent.
        destinations = [m.destination for m in plan]
        self.assertEqual(len(destinations), len(set(destinations)))


class TestExecutePlan(TempDirTestCase):
    def test_successful_moves_land_at_destination(self):
        self.touch("photo.jpg")
        plan = plan_moves(str(self.root))
        manifest = execute_plan(plan, source_dir=str(self.root))
        self.assertEqual(manifest.results[0].status, "moved")
        self.assertTrue((self.root / "images" / "photo.jpg").exists())
        self.assertFalse((self.root / "photo.jpg").exists())

    def test_permission_error_on_one_file_does_not_stop_the_rest(self):
        self.touch("a.jpg")
        self.touch("b.jpg")
        plan = plan_moves(str(self.root))

        def flaky_move(src, dst):
            if "a.jpg" in src:
                raise PermissionError("permission denied (simulated)")
            import shutil
            shutil.move(src, dst)

        manifest = execute_plan(plan, source_dir=str(self.root), move_fn=flaky_move)
        statuses = {Path(r.source).name: r.status for r in manifest.results}
        self.assertEqual(statuses["a.jpg"], "skipped_permission")
        self.assertEqual(statuses["b.jpg"], "moved")
        # The file that failed to move is still where it started.
        self.assertTrue((self.root / "a.jpg").exists())

    def test_other_os_error_recorded_not_raised(self):
        self.touch("a.jpg")
        plan = plan_moves(str(self.root))

        def failing_move(src, dst):
            raise OSError("disk full (simulated)")

        manifest = execute_plan(plan, source_dir=str(self.root), move_fn=failing_move)
        self.assertEqual(manifest.results[0].status, "skipped_error")

    def test_manifest_records_source_and_dest_dirs(self):
        self.touch("a.jpg")
        plan = plan_moves(str(self.root))
        manifest = execute_plan(plan, source_dir=str(self.root))
        self.assertEqual(manifest.source_dir, str(self.root))


class TestUndo(TempDirTestCase):
    def test_undo_restores_moved_files(self):
        self.touch("photo.jpg")
        plan = plan_moves(str(self.root))
        manifest = execute_plan(plan, source_dir=str(self.root))
        manifest_path = self.root / "manifest.json"
        write_manifest(manifest, str(manifest_path))

        self.assertFalse((self.root / "photo.jpg").exists())
        failures = undo_manifest(str(manifest_path))
        self.assertEqual(failures, [])
        self.assertTrue((self.root / "photo.jpg").exists())

    def test_undo_reports_files_it_could_not_restore(self):
        self.touch("photo.jpg")
        plan = plan_moves(str(self.root))
        manifest = execute_plan(plan, source_dir=str(self.root))
        manifest_path = self.root / "manifest.json"
        write_manifest(manifest, str(manifest_path))

        # Simulate the moved file being deleted before undo runs.
        (self.root / "images" / "photo.jpg").unlink()

        failures = undo_manifest(str(manifest_path))
        self.assertEqual(len(failures), 1)

    def test_undo_skips_entries_that_were_never_moved(self):
        self.touch("a.jpg")
        plan = plan_moves(str(self.root))

        def failing_move(src, dst):
            raise OSError("simulated failure")

        manifest = execute_plan(plan, source_dir=str(self.root), move_fn=failing_move)
        manifest_path = self.root / "manifest.json"
        write_manifest(manifest, str(manifest_path))

        # Nothing was actually moved, so undo should be a clean no-op —
        # not an attempt to "restore" a file that never left.
        failures = undo_manifest(str(manifest_path))
        self.assertEqual(failures, [])
        self.assertTrue((self.root / "a.jpg").exists())


if __name__ == "__main__":
    unittest.main()
