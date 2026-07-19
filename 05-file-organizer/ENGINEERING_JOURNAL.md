# Engineering Journal — File Organizer

## Problem

Needed a "tidy this folder" tool where a permission error on one file, a
naming collision, or Ctrl-C mid-run never leaves the folder in a worse or
more confusing state than before the tool ran — and where every run can
be undone.

## Initial Idea

First draft planned and executed moves in the same loop:
`for f in files: shutil.move(f, dest_dir / category(f) / f.name)`,
with collision handling that only checked `dest.exists()` right before
each move.

## Why It Failed

Two separate problems surfaced once I actually tried to test permission
handling and collision handling:

1. **Permission testing.** This container runs as root. `chmod 000` on a
   test file doesn't actually deny root access to it — so a test built
   around "make a file unreadable, verify the tool handles it" silently
   passed for the wrong reason (root could still move the file fine, no
   `PermissionError` was ever raised, the test just never exercised the
   error path it claimed to).
2. **In-batch collisions.** Once I added a `dest_dir` override (organizing
   into a different destination than the source), it became possible for
   two different source files to compute the same "first available" name
   in `_unique_destination()`, because that function only ever checked
   disk state — and disk state doesn't change until a move actually
   happens. Both files would plan to land at the same path, and the
   second move would silently overwrite the first.

## Alternative Approaches Considered

For permission testing:
1. **Run tests in a restricted user context / container.** Rejected as
   too heavy for this project's test suite, and it would make the tests
   themselves environment-dependent (pass differently depending on how
   CI is configured).
2. **Mock `shutil.move` at the module level with `unittest.mock.patch`.**
   Works, but patches the specific import path (`file_organizer.shutil.move`),
   which is more brittle to refactors than an explicit parameter.
3. **Make `move_fn` an injectable parameter of `execute_plan()`, default
   `shutil.move`.** Chosen — same dependency-injection pattern already
   used for `sleep_fn` in the API client and scraper projects. A test can
   pass a fake that raises `PermissionError` on command, independent of
   what the actual OS permissions are.

For in-batch collisions:
1. **Only check disk state, accept the batch-collision risk as rare.**
   Rejected once I saw it actually happen in a quick manual test with two
   same-named files planned in one run — "rare" doesn't cover a folder
   containing `invoice.pdf` from two different senders.
2. **Track a `planned_destinations` set alongside the on-disk check.**
   Chosen. `_unique_destination` still handles the disk-state case; the
   caller in `plan_moves()` additionally checks the set of destinations
   already claimed earlier in the same planning pass.

## Benchmarking / Performance Observations

Not benchmarked at scale — this project assumes a folder with a normal
number of files (tens to low thousands). A folder with hundreds of
thousands of files organized non-recursively would still work correctly
here but wasn't a design target; that scale concern is what project 09
(duplicate finder) is specifically built to handle.

## Refactoring Decisions

Split `plan_moves()` (pure, no I/O beyond reading directory entries) from
`execute_plan()` (does the actual moving) specifically so `--dry-run`
could be "call the planner and print it" rather than a parallel code path
that has to be kept in sync with the real execution logic. Before this
split, an earlier draft had `--dry-run` as an `if` branch inside the same
function that did the moving, which meant every change to move logic
risked the dry-run output silently drifting out of sync with what would
actually happen.

## Final Implementation

`plan_moves()` builds the full list of `(source, destination, category)`
without touching the filesystem (beyond listing directory entries).
`execute_plan()` applies it, catching `PermissionError` and other
`OSError` subtypes per-file so one failure doesn't abort the batch, and
recording every outcome (moved or skipped, with a reason) into a
manifest. `undo_manifest()` walks that manifest in reverse and only
reverses entries whose status was actually `"moved"`.

## Engineering Tradeoffs

Chose dependency injection (`move_fn`) over environment-based permission
testing (restricted users, containers) because it makes the test suite
itself portable and fast — it will correctly exercise the permission-
error path in any environment, root or not, rather than depending on how
the CI or sandbox happens to be configured.

## Possible Future Versions

- `--recursive` mode with explicit cycle detection for symlinked
  directories (the non-recursive design sidesteps this entirely today).
- Content-sniffing classification fallback for files with missing or
  wrong extensions.
