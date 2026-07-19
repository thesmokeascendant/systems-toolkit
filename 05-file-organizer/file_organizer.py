#!/usr/bin/env python3
"""
file_organizer.py — organizes a directory's files into category
subfolders by extension, defensively.

Design goals:
  - A single failed file (permission denied, disk full, whatever) must
    not abort the whole run — it's logged and skipped, and every other
    file still gets organized.
  - Every move is logged to a manifest, so a run can be undone.
  - Name collisions never silently overwrite an existing file.
  - Symlinks are handled explicitly: broken links are skipped, and
    directory symlinks are never followed (which would risk infinite
    loops on a cyclic symlink structure).
  - Dry-run mode shows the plan without touching the filesystem.

Usage:
    from file_organizer import plan_moves, execute_plan

    plan = plan_moves("/path/to/messy_folder")
    manifest = execute_plan(plan)

CLI:
    ./file_organizer.py ~/Downloads --dry-run
    ./file_organizer.py ~/Downloads --manifest run.json
    ./file_organizer.py --undo run.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

DEFAULT_CATEGORY_MAP = {
    "images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"},
    "documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".odt", ".rtf"},
    "spreadsheets": {".xls", ".xlsx", ".csv", ".ods"},
    "audio": {".mp3", ".wav", ".flac", ".ogg", ".m4a"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"},
    "archives": {".zip", ".tar", ".gz", ".rar", ".7z"},
    "code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".sh", ".rb", ".go"},
}


def _build_extension_lookup(category_map: dict[str, set[str]]) -> dict[str, str]:
    lookup = {}
    for category, extensions in category_map.items():
        for ext in extensions:
            lookup[ext] = category
    return lookup


@dataclass
class PlannedMove:
    source: str
    destination: str
    category: str


@dataclass
class MoveResult:
    source: str
    destination: str
    category: str
    status: str  # "moved", "skipped_permission", "skipped_error"
    error: Optional[str] = None


@dataclass
class OrganizeManifest:
    source_dir: str
    dest_dir: str
    timestamp: float
    results: list[MoveResult]

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


def _classify(path: Path, extension_lookup: dict[str, str]) -> str:
    return extension_lookup.get(path.suffix.lower(), "other")


def _unique_destination(dest: Path) -> Path:
    """
    If dest already exists, append ' (1)', ' (2)', etc. until a free name
    is found. Never overwrites an existing file.
    """
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    counter = 1
    while True:
        candidate = dest.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _iter_files_safely(source_dir: Path):
    """
    Yield regular files directly inside source_dir (non-recursive by
    design — organizing subfolders' contents implicitly is surprising
    behavior for a "tidy this folder" tool). Broken symlinks and
    permission-denied entries are skipped, not raised.
    """
    try:
        entries = list(source_dir.iterdir())
    except PermissionError:
        return
    for entry in entries:
        try:
            if entry.is_symlink() and not entry.exists():
                continue  # broken symlink — nothing to move
            if entry.is_dir():
                continue  # non-recursive: subfolders are left alone
            if entry.is_file():
                yield entry
        except PermissionError:
            continue


def plan_moves(
    source_dir: str,
    dest_dir: Optional[str] = None,
    category_map: Optional[dict[str, set[str]]] = None,
) -> list[PlannedMove]:
    """
    Build the move plan without touching the filesystem. dest_dir
    defaults to source_dir itself (category folders created alongside
    the files being organized).
    """
    source_path = Path(source_dir)
    if not source_path.exists():
        raise FileNotFoundError(f"source directory not found: {source_path}")
    if not source_path.is_dir():
        raise NotADirectoryError(f"not a directory: {source_path}")

    dest_path = Path(dest_dir) if dest_dir else source_path
    lookup = _build_extension_lookup(category_map or DEFAULT_CATEGORY_MAP)

    plan: list[PlannedMove] = []
    planned_destinations: set[Path] = set()
    for file_path in _iter_files_safely(source_path):
        category = _classify(file_path, lookup)
        category_dir = dest_path / category
        candidate = category_dir / file_path.name
        # Account for collisions against files already planned this run,
        # not just what's currently on disk.
        while candidate in planned_destinations or (
            candidate.exists() and candidate != file_path
        ):
            candidate = _unique_destination(candidate)
            if candidate not in planned_destinations:
                break
        planned_destinations.add(candidate)
        plan.append(PlannedMove(str(file_path), str(candidate), category))

    return plan


def execute_plan(
    plan: list[PlannedMove],
    source_dir: str,
    dest_dir: Optional[str] = None,
    move_fn: Callable[[str, str], None] = shutil.move,
) -> OrganizeManifest:
    """
    Execute a move plan. A failure on any individual file (permission
    denied, or any other OSError) is recorded in the manifest and does
    NOT stop the rest of the plan from executing.

    move_fn is injectable so tests can simulate permission errors,
    full disks, etc. without needing real filesystem permission changes.
    """
    results: list[MoveResult] = []
    try:
        for move in plan:
            dest = Path(move.destination)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                move_fn(move.source, move.destination)
                results.append(
                    MoveResult(move.source, move.destination, move.category, status="moved")
                )
            except PermissionError as e:
                results.append(
                    MoveResult(
                        move.source, move.destination, move.category,
                        status="skipped_permission", error=str(e),
                    )
                )
            except OSError as e:
                results.append(
                    MoveResult(
                        move.source, move.destination, move.category,
                        status="skipped_error", error=str(e),
                    )
                )
    except KeyboardInterrupt:
        # Whatever completed before the interrupt is still a valid,
        # undoable manifest — write it out rather than losing the record
        # of what already moved.
        pass

    return OrganizeManifest(
        source_dir=source_dir,
        dest_dir=dest_dir or source_dir,
        timestamp=time.time(),
        results=results,
    )


def write_manifest(manifest: OrganizeManifest, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest.as_dict(), f, indent=2)


def undo_manifest(path: str, move_fn: Callable[[str, str], None] = shutil.move) -> list[str]:
    """
    Reverse every successful move recorded in a manifest, in reverse
    order. Returns a list of files that could NOT be restored (e.g. the
    destination no longer exists because it was moved/deleted since).
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    failures = []
    for result in reversed(data["results"]):
        if result["status"] != "moved":
            continue
        src, dst = result["destination"], result["source"]
        try:
            if not Path(src).exists():
                failures.append(src)
                continue
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            move_fn(src, dst)
        except OSError:
            failures.append(src)
    return failures


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Organize files into category subfolders")
    parser.add_argument("source", nargs="?", help="Directory to organize")
    parser.add_argument("--dest", help="Destination directory (default: same as source)")
    parser.add_argument("--dry-run", action="store_true", help="Show the plan without moving files")
    parser.add_argument("--manifest", help="Write the move manifest to this path")
    parser.add_argument("--undo", metavar="MANIFEST_PATH", help="Undo a previous run from its manifest")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.undo:
        failures = undo_manifest(args.undo)
        if failures:
            print(f"undo complete with {len(failures)} file(s) that could not be restored:", file=sys.stderr)
            for f in failures:
                print(f"  {f}", file=sys.stderr)
            return 1
        print("undo complete — all files restored")
        return 0

    if not args.source:
        print("error: source directory is required (or use --undo)", file=sys.stderr)
        return 1

    try:
        plan = plan_moves(args.source, dest_dir=args.dest)
    except (FileNotFoundError, NotADirectoryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not plan:
        print("nothing to organize — no files found")
        return 0

    if args.dry_run:
        for move in plan:
            print(f"{move.source}  ->  {move.destination}")
        print(f"\n{len(plan)} file(s) would be moved (dry run, nothing changed)")
        return 0

    manifest = execute_plan(plan, source_dir=args.source, dest_dir=args.dest)
    moved = sum(1 for r in manifest.results if r.status == "moved")
    skipped = len(manifest.results) - moved
    print(f"moved {moved} file(s), skipped {skipped}")
    if skipped:
        for r in manifest.results:
            if r.status != "moved":
                print(f"  skipped: {r.source} ({r.status}: {r.error})", file=sys.stderr)

    if args.manifest:
        write_manifest(manifest, args.manifest)
        print(f"manifest written to {args.manifest} (use --undo to reverse this run)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
