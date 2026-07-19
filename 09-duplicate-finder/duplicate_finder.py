#!/usr/bin/env python3
"""
duplicate_finder.py — finds duplicate files across a directory tree
containing potentially hundreds of thousands of files, without loading
any file fully into memory and without either the symlink-loop trap or
the "hash every single file" performance trap.

Algorithm (three funnel stages, each one only processes what the
previous stage couldn't already rule out):

    1. Group by file size.        Files of different sizes can never be
       duplicates — this is nearly free (just a stat() call) and
       eliminates most files immediately on a typical filesystem.
    2. Within a size group, group by a partial hash (first 4 KB).
       Cheap relative to a full hash, and rules out most same-size
       files that merely happen to share a size (e.g. many empty
       config files) before paying for a full read.
    3. Within a partial-hash group, group by full hash (streamed in
       fixed-size chunks — memory use is O(chunk_size), not O(file_size),
       so a 50 GB file costs the same memory as a 5 KB one).

Usage:
    from duplicate_finder import find_duplicates

    report = find_duplicates("/data", progress_fn=print)
    for group in report.duplicate_groups:
        print(group)

CLI:
    ./duplicate_finder.py /data
    ./duplicate_finder.py /data --json --min-size 1024
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

PARTIAL_HASH_BYTES = 4096
FULL_HASH_CHUNK_BYTES = 1024 * 1024  # 1 MB — bounds memory regardless of file size
DEFAULT_PROGRESS_INTERVAL = 1000  # report every N files scanned


@dataclass
class ScanError:
    path: str
    reason: str


@dataclass
class DuplicateReport:
    root: str
    files_scanned: int = 0
    bytes_scanned: int = 0
    files_skipped_error: int = 0
    files_skipped_symlink: int = 0
    duplicate_groups: list[list[str]] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)

    @property
    def wasted_bytes(self) -> int:
        """
        Bytes that could be reclaimed by keeping only one copy of each
        duplicate group. Requires re-statting one file per group, which
        is cheap compared to the hashing work already done.
        """
        total = 0
        for group in self.duplicate_groups:
            if len(group) < 2:
                continue
            try:
                size = Path(group[0]).stat().st_size
            except OSError:
                continue
            total += size * (len(group) - 1)
        return total

    def as_dict(self) -> dict:
        d = asdict(self)
        d["wasted_bytes"] = self.wasted_bytes
        return d


ProgressCallback = Callable[[str], None]


def _iter_files_safely(root: str, report: DuplicateReport):
    """
    Iteratively walk a directory tree (os.walk, not manual recursion —
    no Python recursion-depth risk on deep trees), yielding regular
    files only. Symlinks (to files or directories) are never followed:
    `os.walk`'s default `followlinks=False` already keeps it out of
    symlinked subdirectories, which is what actually prevents symlink
    cycles from causing an infinite walk; individual file symlinks are
    additionally skipped explicitly here so a symlink is never hashed
    as if it were the real file. Permission errors on any directory are
    logged and that subtree is skipped, not fatal to the whole scan.
    """
    def on_error(os_error: OSError) -> None:
        report.errors.append(ScanError(os_error.filename or "<unknown>", "permission_denied"))

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error, followlinks=False):
        for name in filenames:
            full_path = Path(dirpath) / name
            try:
                if full_path.is_symlink():
                    report.files_skipped_symlink += 1
                    continue
                if not full_path.is_file():
                    continue
            except OSError as e:
                report.errors.append(ScanError(str(full_path), f"stat_failed: {e}"))
                report.files_skipped_error += 1
                continue
            yield full_path


def _partial_hash(path: Path, num_bytes: int = PARTIAL_HASH_BYTES) -> Optional[str]:
    try:
        with path.open("rb") as f:
            chunk = f.read(num_bytes)
        return hashlib.sha256(chunk).hexdigest()
    except OSError:
        return None


def _full_hash(path: Path, chunk_size: int = FULL_HASH_CHUNK_BYTES) -> Optional[str]:
    """
    Stream the file in fixed-size chunks so peak memory use for hashing
    is bounded by chunk_size, not file size — this is what makes hashing
    a 50 GB file no more memory-expensive than hashing a 5 KB one.
    """
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def find_duplicates(
    root: str,
    min_size: int = 1,
    progress_fn: Optional[ProgressCallback] = None,
    progress_interval: int = DEFAULT_PROGRESS_INTERVAL,
) -> DuplicateReport:
    """
    Find duplicate files under `root`. Files smaller than `min_size`
    bytes are skipped entirely (a common tuning knob — tiny files
    rarely matter for reclaiming disk space and there are usually a lot
    of them, e.g. empty __init__.py files, which would otherwise form a
    huge, uninteresting duplicate group).
    """
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"path does not exist: {root}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    report = DuplicateReport(root=str(root_path))

    # Stage 1: group by size. Only file paths and sizes are held in
    # memory here — never file contents — so this scales to very large
    # trees at a memory cost proportional to file *count*, not file
    # *size*.
    size_groups: dict[int, list[Path]] = {}
    try:
        for i, file_path in enumerate(_iter_files_safely(root, report), start=1):
            report.files_scanned += 1
            if progress_fn and i % progress_interval == 0:
                progress_fn(f"scanned {i} files...")
            try:
                size = file_path.stat().st_size
            except OSError as e:
                report.errors.append(ScanError(str(file_path), f"stat_failed: {e}"))
                report.files_skipped_error += 1
                continue
            if size < min_size:
                continue
            report.bytes_scanned += size
            size_groups.setdefault(size, []).append(file_path)
    except KeyboardInterrupt:
        if progress_fn:
            progress_fn("interrupted — reporting duplicates found among files scanned so far")

    # Stage 2: within each size group with more than one file, bucket by
    # a cheap partial hash before paying for a full read.
    partial_groups: dict[tuple[int, str], list[Path]] = {}
    for size, paths in size_groups.items():
        if len(paths) < 2:
            continue
        for path in paths:
            digest = _partial_hash(path)
            if digest is None:
                report.errors.append(ScanError(str(path), "read_failed_partial_hash"))
                report.files_skipped_error += 1
                continue
            partial_groups.setdefault((size, digest), []).append(path)

    # Stage 3: within each partial-hash group with more than one file,
    # confirm with a full streamed hash.
    full_groups: dict[str, list[Path]] = {}
    for (_size, _partial), paths in partial_groups.items():
        if len(paths) < 2:
            continue
        for path in paths:
            digest = _full_hash(path)
            if digest is None:
                report.errors.append(ScanError(str(path), "read_failed_full_hash"))
                report.files_skipped_error += 1
                continue
            full_groups.setdefault(digest, []).append(path)

    for digest, paths in full_groups.items():
        if len(paths) > 1:
            report.duplicate_groups.append(sorted(str(p) for p in paths))

    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find duplicate files in a directory tree")
    parser.add_argument("root")
    parser.add_argument("--min-size", type=int, default=1, help="Ignore files smaller than this many bytes")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    progress_fn = None if args.quiet else lambda msg: print(msg, file=sys.stderr)

    try:
        report = find_duplicates(args.root, min_size=args.min_size, progress_fn=progress_fn)
    except (FileNotFoundError, NotADirectoryError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
        return 0

    print(f"scanned {report.files_scanned} file(s), {report.bytes_scanned:,} bytes")
    if report.files_skipped_symlink:
        print(f"skipped {report.files_skipped_symlink} symlink(s)")
    if report.errors:
        print(f"{len(report.errors)} error(s) encountered (see --json for details)")

    if not report.duplicate_groups:
        print("no duplicates found")
        return 0

    print(f"\n{len(report.duplicate_groups)} duplicate group(s), "
          f"{report.wasted_bytes:,} bytes reclaimable:")
    for group in report.duplicate_groups:
        print(f"  {len(group)} copies:")
        for path in group:
            print(f"    {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
