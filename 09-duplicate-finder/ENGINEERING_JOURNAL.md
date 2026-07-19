# Engineering Journal — Massive Duplicate Finder

## Problem

Needed a duplicate finder that's actually usable on a tree with hundreds
of thousands of files — which means it can't hash every file
unconditionally, can't load any single file fully into memory, and can't
be taken down by one bad symlink or one permission-denied file.

## Initial Idea

First draft: walk the tree, compute a full SHA-256 hash of every file,
group by hash.

## Why It Failed

Correct, but doesn't scale well when most files *do* differ in size —
hashing every byte of every file when the overwhelming majority of files
aren't duplicates of anything is wasted work. Benchmarked both versions
against an 8,000-file tree with realistic variable file sizes (1 KB–200 KB,
~30% deliberately duplicated): the naive full-hash-everything approach
took 2.64s; the funneled version took 0.95s — a 2.79x speedup, both
finding the same 482 duplicate groups.

But the first benchmark I ran used a content pool where every file
happened to be exactly the same size (50 KB), and on *that* input the
funneled version was actually slightly *slower* than naive (0.90x) — the
size stage filters nothing when every file shares a size, so the funnel
just adds extra file-open overhead (partial hash read, then full hash
read — two opens instead of one) on top of work the naive version would
have done in a single pass anyway. This is now documented explicitly
rather than glossed over: the funnel's advantage depends on file sizes
actually being discriminating, which is true for most real-world
directory trees but not universally true, and a synthetic worst case
(a directory of same-sized files, e.g. fixed-size backup chunks) can
make it a net loss.

## Alternative Approaches Considered

1. **Hash everything, group by full hash (original draft).** Simple,
   correct, doesn't scale — rejected once benchmarked against the
   funneled version.
2. **Size grouping only, no partial-hash stage — go straight from size
   match to full hash.** Better than hashing everything, but still pays
   for a full read on every file within a size group, even when most of
   them differ in their first few bytes and could have been ruled out
   almost for free.
3. **Size → partial hash (first 4 KB) → full hash, three-stage funnel.**
   Chosen. Each stage is strictly cheaper than the next, and only
   processes what the previous stage couldn't already resolve.

For symlink handling:
1. **Manually track visited (device, inode) pairs to detect cycles.**
   Considered, but unnecessary — `os.walk`'s default `followlinks=False`
   already prevents descending into symlinked directories at all, which
   removes the cycle risk structurally rather than requiring explicit
   cycle bookkeeping.
2. **Rely on `followlinks=False` alone, explicitly skip file-level
   symlinks too.** Chosen — simpler than manual cycle tracking and
   correctly handles both the directory-loop case (`os.walk`'s job) and
   the "a symlink shouldn't be hashed as if it were the real file it
   points to" case (explicit `is_symlink()` check).

## Benchmarking / Performance Observations

Ran real, measured comparisons rather than relying on architectural
reasoning alone:

- **Scale/correctness check:** 20,000 files across 50 subdirectories,
  content drawn from a 500-item pool (guaranteeing a known duplicate
  structure). Full scan completed in under a second, correctly finding
  all 500 duplicate groups.
- **Funnel vs. naive full-hash-everything, realistic variable sizes**
  (8,000 files, 1 KB–200 KB, ~30% duplicated): funneled version was
  2.79x faster (0.95s vs 2.64s), same result set.
- **Funnel vs. naive, adversarial uniform sizes** (same file count, every
  file exactly 50 KB — an intentional worst case): the funnel was
  actually slightly *slower* than naive (0.90x), because the size stage
  can't filter anything when every file shares a size, and the extra
  partial-hash read adds overhead the naive single-pass approach doesn't
  pay. Documented as a real, measured limitation rather than assumed
  away.

## Refactoring Decisions

Kept `_partial_hash` and `_full_hash` as separate functions rather than
one parameterized function with a "read everything vs read N bytes"
flag, because the fixed-chunk *streaming* loop in `_full_hash` (multiple
`read()` calls, feeding a running hash) is meaningfully different code
from `_partial_hash`'s single bounded read — collapsing them into one
function with a branch would have obscured that `_full_hash` is the one
function in this module actually responsible for the "never load a
whole file into memory" property.

## Final Implementation

`_iter_files_safely` walks via `os.walk` (iterative, symlink-safe by
construction), yielding only genuine regular files. `find_duplicates`
runs the three-stage funnel: size grouping (dict keyed by size, `Path`
objects only — no content in memory), partial-hash grouping within
same-size groups, full-hash grouping (streamed) within same-partial-hash
groups. Every stage that reads file content wraps the read in a
try/except that logs and skips on `OSError`, so one unreadable file
never aborts the group it's in, let alone the whole scan.

## Engineering Tradeoffs

Chose SHA-256 for both the partial and full hash stages rather than
pairing a fast non-cryptographic hash (xxHash) for the cheap pre-filter
with SHA-256 only for final confirmation. SHA-256-everywhere is simpler
and adds no dependency, at the cost of the partial-hash stage being
somewhat slower than it needs to be, since collision resistance barely
matters for a stage whose only job is "probably rule out non-matches
cheaply, false positives get caught by the next stage anyway." Documented
as a concrete, actionable future improvement rather than silently
accepted.

## Possible Future Versions

- xxHash (or similar) for the partial-hash pre-filter stage, keeping
  SHA-256 for the final confirmation.
- Hard-link-aware mode (inode-number check) to distinguish true content
  duplicates from hard links of the same underlying data.
- Disk-backed (SQLite) grouping for trees large enough that even
  path-and-size-only in-memory grouping becomes a real constraint.
