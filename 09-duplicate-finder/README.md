# 09 — Massive Duplicate Finder

A duplicate-file finder built for scale: it never hashes a file it
doesn't have to, never loads a file fully into memory regardless of
size, never follows a symlink into a loop, and never lets a permission
error on one file abort a scan of the other 99,999.

## Problem

The naive version of this tool — hash every file, group by hash — works
fine on a thousand files. At a hundred thousand, it's needlessly slow
(hashing every byte of every file when most files aren't duplicates of
anything) and it's fragile (one unreadable file or one symlink loop
takes down the whole scan).

## Requirements

- Correctly handle a directory tree with 100,000+ files.
- Never load a whole file into memory to hash it — memory use for
  hashing must be bounded by chunk size, not file size.
- Never get stuck in a symlink loop.
- Recover from permission errors on individual files/directories without
  aborting the scan.
- Report progress during a long-running scan.
- Continue processing after any individual file's failure.
- Avoid the "hash absolutely everything" performance trap.

## Architecture

```
duplicate_finder.py
├── _iter_files_safely()   # os.walk-based (iterative, no recursion limit
│                           # risk), skips symlinks, tolerates permission errors
├── _partial_hash()        # first 4 KB only — cheap pre-filter
├── _full_hash()           # streamed in fixed-size chunks — O(1) memory
└── find_duplicates()      # three-stage funnel: size → partial hash → full hash
```

The three-stage funnel is the core design: each stage only processes
files the previous stage couldn't already rule out as non-duplicates.
On a real filesystem, most files have a size no other file shares — those
are eliminated after nothing more than a `stat()` call, never touching
file content at all.

## Design Decisions

- **Three-stage funnel (size → partial hash → full hash), not "hash
  everything and group by hash."** Files of different sizes can never be
  duplicates, so comparing sizes first eliminates the vast majority of
  files for the cost of a `stat()` call. A cheap partial hash (first
  4 KB) then further narrows same-size files before paying for a full
  read. Measured against an 8,000-file tree with realistic variable
  sizes (~30% duplicated): 2.79x faster than hashing everything (0.95s
  vs 2.64s), same result. This advantage depends on file sizes actually
  being discriminating — a directory where every file happens to be the
  same size (e.g. fixed-size backup chunks) defeats the size filter
  entirely, and in that measured worst case the funnel was slightly
  *slower* than naive (0.90x) due to the extra partial-hash read. Real
  directory trees are almost never uniform-size, but this tradeoff is
  documented rather than assumed away.
- **`os.walk`, not manual recursion, for tree traversal.** A hand-rolled
  recursive walker risks Python's recursion limit on a sufficiently deep
  tree; `os.walk` is iterative internally regardless of tree depth.
- **`followlinks=False` (the default) is relied on explicitly, and
  individual file symlinks are separately skipped.** `os.walk` never
  descends into a symlinked directory by default — that's what actually
  prevents a directory symlink loop from causing an infinite or crashing
  walk. Skipping file symlinks too means a symlink is never mistaken for
  (and hashed as if it were) the file it points to.
- **Hashing streams in fixed-size chunks (`_full_hash`), never
  `f.read()` with no size argument.** This is what makes peak memory use
  for hashing a function of chunk size (1 MB, configurable), not file
  size — hashing a 50 GB file costs the same memory as hashing a 5 KB
  one.
- **A `min_size` filter exists and defaults to excluding zero-byte
  files.** Without it, every empty file in a tree (often numerous —
  empty `__init__.py`s, placeholder files) would form one giant,
  practically useless "duplicate group." Explicit opt-in
  (`--min-size 0`) is available for callers who do want that.

## Algorithms Used

- Size-based bucketing (dict keyed by size).
- Partial-then-full hash bucketing (SHA-256), each stage only run on
  groups the previous stage couldn't already resolve to size 1.
- Streamed, fixed-chunk-size hashing for the full-hash stage.

## Tradeoffs

- Holds one `Path` object per scanned file in memory during the
  size-grouping stage (not file *contents* — just paths and sizes).
  This scales with file *count*, which is the right tradeoff for this
  tool's stated scale target (hundreds of thousands of files is
  megabytes of path strings, not a memory problem) — but a tool aimed
  at tens of millions of files would need a different, disk-backed
  grouping structure.
- SHA-256 is used throughout for both partial and full hashing. A faster
  non-cryptographic hash (xxHash, CRC32) would be quicker for the
  partial-hash pre-filter stage, where collision resistance matters far
  less than raw speed — kept SHA-256 everywhere for simplicity and zero
  extra dependencies, noted here as a legitimate performance
  optimization left for a future version.
- The funnel adds a small amount of fixed overhead per file (an extra
  file open for the partial-hash read) that only pays for itself when
  the size stage actually filters something out. Measured directly: on
  a directory where every file happens to be exactly the same size, the
  funnel is ~10% slower than simply hashing everything once (see the
  engineering journal for the numbers). This is a genuine, measured
  edge case, not a hypothetical one — flat-size directories do occur
  (fixed-size backup chunks, padded records) even though they're
  uncommon.

## Edge Cases Handled

| Case | Behavior |
|---|---|
| 100,000+ files in a tree | Handled via the three-stage funnel; validated with a 20,000-file scale test completing in under a second |
| Symlink to a file | Skipped, never hashed as if it were the real file |
| Broken symlink | Skipped, no crash |
| Directory symlink pointing back to an ancestor (loop) | Never entered — `os.walk` doesn't follow directory symlinks by default |
| Permission denied on a directory | That subtree is skipped via `os.walk`'s `onerror` callback; the rest of the walk continues |
| Permission denied reading a specific file | That file is skipped (`read_failed_partial_hash` / `read_failed_full_hash`), the rest of the scan continues |
| Very large individual file | Hashed in bounded-memory chunks regardless of size |
| Zero-byte files | Excluded by default (`min_size=1`), includable via `--min-size 0` |
| Files that share a size but not content | Ruled out at the partial- or full-hash stage, never falsely grouped |
| `Ctrl-C` mid-scan | Duplicates among files already scanned before the interrupt are still reported, not discarded |
| Empty directory | Empty report, no error |

## Examples

```
$ ./duplicate_finder.py /data
scanned 84213 file(s), 128,993,442,101 bytes
skipped 340 symlink(s)

12 duplicate group(s), 4,209,882,112 bytes reclaimable:
  3 copies:
    /data/backups/2024/report.pdf
    /data/backups/2025/report.pdf
    /data/staging/report.pdf
  ...

$ ./duplicate_finder.py /data --json --min-size 1048576   # only files >= 1MB
```

## Limitations

- Path/size data for every scanned file is held in memory during
  grouping — appropriate for hundreds of thousands of files, not
  necessarily tens of millions (see Tradeoffs).
- No hard link detection — two hard links to the same inode are reported
  as duplicates (which is technically true — they are two directory
  entries with identical content — but a caller wanting to distinguish
  "true copies" from "hard links of the same data" needs an additional
  inode-number check this tool doesn't perform).
- SHA-256 throughout, not a faster non-cryptographic hash for the
  partial-hash pre-filter stage.

## Lessons Learned

The first version of the directory-symlink-loop test failed to catch
anything, because the test fixture created the loop as a symlink placed
*inside* a directory being walked, pointing back to that same directory
directly (`subdir/self_loop -> subdir`) rather than to an ancestor.
`os.walk` with `followlinks=False` never entered it either way, so the
test technically passed — but it wasn't actually exercising the
"pointing back up the tree" case that's the classically dangerous one
for naively recursive walkers. Rewrote the fixture so the symlink points
from a subdirectory back to the tree's own root
(`test_directory_symlink_loop_does_not_hang_or_recurse`), which is the
shape that would actually cause infinite recursion in a walker that
didn't guard against it — a passing test against the wrong-shaped
fixture would have given false confidence.

## Future Improvements

- Optional non-cryptographic hash (xxHash) for the partial-hash stage,
  keeping SHA-256 only for the final confirmation stage.
- Optional hard-link-aware mode that reports true content duplicates
  separately from same-inode hard links.
- Disk-backed (SQLite) grouping structure for trees too large to hold
  path/size data comfortably in memory.

## References

- Python `os.walk` documentation, `followlinks` parameter
- Python `hashlib` documentation
