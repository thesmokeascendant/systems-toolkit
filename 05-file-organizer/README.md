# 05 — File Organizer

A defensive file organizer that sorts a directory's files into category
subfolders by extension — with a dry-run mode, a reversible manifest for
every run, collision-safe renaming, and per-file failure isolation so one
bad file never aborts the whole batch.

## Problem

The naive version of this tool (`for f in files: shutil.move(f, category_dir)`)
has three real risks: it overwrites files that happen to share a name with
something already in the destination, it has no way to undo a run once
it's done, and a single permission error or OS-level failure on one file
kills the loop and leaves the run half-finished with no record of what
happened.

## Requirements

- Sort files into category subfolders by extension (images, documents,
  spreadsheets, audio, video, archives, code, other).
- Never recurse into subdirectories — "organize this folder" means this
  folder's files, not everything beneath it.
- Never overwrite an existing file — collisions get a `(1)`, `(2)`, ...
  suffix.
- Support `--dry-run` to preview the plan with zero filesystem changes.
- Record every run in a manifest that can fully undo it with `--undo`.
- A single file's failure (permission denied, OS error) must not stop the
  rest of the batch — record it and continue.
- Handle broken symlinks and permission-denied directory entries without
  crashing.
- Be testable without depending on real filesystem permission changes
  (the reference environment runs as root, where `chmod 000` doesn't
  actually deny access).

## Architecture

```
file_organizer.py
├── DEFAULT_CATEGORY_MAP     # extension → category
├── plan_moves()             # pure planning: source scan → move plan, no I/O
├── execute_plan()           # applies a plan, isolates per-file failures
├── write_manifest() / undo_manifest()  # reversibility
└── _iter_files_safely()     # defensive directory scan (symlinks, perms)
```

Planning and execution are separate on purpose: `plan_moves()` never
touches the filesystem beyond reading directory entries, which is what
makes `--dry-run` trivial (it's just "run the planner, don't call the
executor") and what makes the planning logic unit-testable without
worrying about partial filesystem state from a previous test.

## Design Decisions

- **Non-recursive by default.** A tool that silently reaches into
  subfolders is more likely to surprise a user than help them — "organize
  this messy folder" reasonably means the files sitting directly in it.
- **Collision resolution checks both disk state and the current plan.**
  If two files being organized in the same run would land on the same
  destination path, the second one still needs a unique name — checking
  only `dest.exists()` on disk would miss a collision between two moves
  planned in the same batch, since neither has happened yet.
- **`move_fn` is injectable**, exactly like the `sleep_fn` pattern used in
  the API client and web scraper projects. It's what makes permission-
  error handling testable in a container that runs as root, where real
  `chmod`-based permission denial doesn't apply — a fake `move_fn` can
  raise `PermissionError` on command regardless of actual OS permissions.
- **Broken symlinks are skipped silently, not reported as errors.** A
  dangling symlink in a folder being tidied is common (a deleted target,
  a moved file) and isn't something the user needs to be alerted about
  for an organizing task — there's nothing to move.
- **The manifest records every result, not just successes.** An undo that
  only knew about successful moves would still work correctly (it only
  reverses `status == "moved"` entries), but keeping skipped entries in
  the manifest too means the manifest is a complete, honest record of
  what happened during the run, not just a to-do list of what to reverse.

## Algorithms Used

- Non-recursive directory scan (`Path.iterdir()`), not `os.walk` — walking
  would require explicit symlink-loop guarding, which is unnecessary
  complexity for a scan that was never going to recurse into
  subdirectories in the first place.
- Linear collision-avoidance scan (`(1)`, `(2)`, ... until a free name is
  found), checked against both disk state and in-flight plan state.

## Tradeoffs

- Non-recursive is a deliberate scope limit, not an oversight — a
  recursive mode would need its own explicit opt-in flag and its own
  symlink-loop handling, which is real added complexity for a
  meaningfully different (and riskier) operation than "tidy this one
  folder."
- The category map is a fixed Python dict, not an external config file.
  For this project's scope (demonstrating the pattern), a hardcoded-but-
  overridable map (`plan_moves(..., category_map=...)`) is simpler than
  building config-file loading and validation for marginal benefit.

## Edge Cases Handled

| Case | Behavior |
|---|---|
| Empty directory | Empty plan, `"nothing to organize"`, no error |
| Unknown file extension | Sorted into `other/` |
| Subdirectory present | Left untouched (non-recursive) |
| Broken symlink | Skipped, not moved, no crash |
| Permission denied listing a directory | Returns no entries from that directory, no crash |
| Permission denied moving a specific file | Recorded as `skipped_permission`, rest of batch continues |
| Any other OS error during move (e.g. disk full) | Recorded as `skipped_error`, rest of batch continues |
| Name collision with existing file | Destination gets a `(1)`, `(2)`, ... suffix — never overwrites |
| Name collision between two files in the same run | Also resolved via unique suffix, checked against the in-progress plan |
| Source directory doesn't exist | `FileNotFoundError`, clear message |
| Source path is a file, not a directory | `NotADirectoryError`, clear message |
| `Ctrl-C` mid-run | Whatever completed is still recorded in the manifest, not lost |
| Undo after the moved file was deleted/moved again | Reported as an unrestorable failure, not a silent no-op or crash |
| Undo on entries that were never actually moved | Skipped (nothing to reverse), not treated as a failure |

## Examples

```
$ ./file_organizer.py ~/Downloads --dry-run
~/Downloads/photo.jpg  ->  ~/Downloads/images/photo.jpg
~/Downloads/report.pdf  ->  ~/Downloads/documents/report.pdf

2 file(s) would be moved (dry run, nothing changed)

$ ./file_organizer.py ~/Downloads --manifest run.json
moved 2 file(s), skipped 0
manifest written to run.json (use --undo to reverse this run)

$ ./file_organizer.py --undo run.json
undo complete — all files restored
```

## Limitations

- Non-recursive only — no option (yet) to organize subfolder contents
  too.
- Category map is extension-based only; a file with a misleading
  extension is misclassified (e.g. a renamed `.txt` that's actually a
  `.zip`).
- Undo relies on the manifest file still existing and being unmodified;
  there's no checksum verification that a "restored" file is byte-
  identical to what was originally moved (in practice it always will be,
  since moves — not copies — are used, but this isn't independently
  verified).

## Lessons Learned

The first version of `_unique_destination()` only checked `dest.exists()`
on disk, which is correct for a single file but wrong for a batch: two
different source files with the same name (possible once destination
override support was added) would both compute the *same* "next free"
name, since neither move had actually happened on disk yet when both were
planned. Fixed by tracking a `planned_destinations` set alongside the
existing on-disk check — `test_multiple_new_files_colliding_with_each_other_all_get_unique_names`
exists specifically to keep this from regressing.

## Future Improvements

- Optional `--recursive` mode with explicit symlink-loop protection.
- Content-based classification fallback (magic-byte sniffing) for files
  with missing or misleading extensions.
- Configurable category map via an external JSON/YAML file.

## References

- Python `pathlib` documentation
- Python `shutil.move` documentation
