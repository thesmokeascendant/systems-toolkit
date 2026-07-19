# 08 — Corrupted CSV Cleaner

A defensive CSV cleaner that recovers usable data from messy real-world
CSVs — ragged rows, mixed delimiters, invalid encoding, a dozen spellings
of "null", missing headers — and produces a cleaning report explaining
exactly what was fixed, padded, truncated, or dropped, following the same
recovery-report pattern used in the log analyzer project (01).

## Problem

CSVs exported from spreadsheets, databases, and hand-edited by different
people rarely agree on delimiter, null representation, or even column
count row-to-row. `csv.reader` alone happily hands back ragged rows
without complaint, and a single invalid byte in a text-mode file read can
abort the whole process. A cleaning tool needs to treat all of this as
routine, not exceptional.

## Requirements

- Auto-detect the delimiter (comma, semicolon, tab, pipe) rather than
  assuming comma.
- Handle rows with too few or too many columns — pad or truncate by
  default, with an option to drop them instead — without crashing or
  silently losing data.
- Normalize the many spellings of "no value" (`NULL`, `N/A`, `--`, empty
  string, etc.) to a single consistent empty representation.
- Detect whether the first row is a real header or already data, and
  synthesize column names when there's no header.
- Handle invalid UTF-8 bytes and a leading BOM without aborting.
- De-duplicate header names that collide.
- Drop entirely blank rows.
- Never require the whole file in memory at once conceptually larger
  than necessary (the design streams row-by-row through `csv.reader`).
- Produce a full report of every row-level fix or drop, not just a
  cleaned file with no explanation of what changed.

## Architecture

```
csv_cleaner.py
├── _decode_tolerant()     # binary → str, replaces invalid UTF-8 bytes
├── sniff_delimiter()       # Sniffer with a manual frequency-count fallback
├── normalize_value()       # whitespace + null-token normalization
├── _read_header()          # header-vs-data detection, de-duplication
└── clean_csv()              # orchestrates: decode → sniff → header → row loop
```

Mirrors project 01's recovery-report shape deliberately: `CleaningReport`
(counts + a list of `RowIssue`s with row number, reason, and detail) is
the CSV-cleaning equivalent of the log analyzer's `RecoveryReport` —
consistent vocabulary across the portfolio for "here's what I couldn't
process cleanly, and why."

## Design Decisions

- **Delimiter detection has a three-tier fallback: `csv.Sniffer`, then a
  manual frequency count over a small candidate set, then a hard default
  of comma.** `Sniffer` is good but not infallible — it can raise
  `csv.Error` on short samples or single-column files. Treating that as
  fatal would mean a one-column CSV (delimiter is irrelevant, but
  `Sniffer` doesn't know that) fails a tool whose entire purpose is
  handling awkward input.
- **Header-vs-data detection is heuristic (checks whether every cell in
  the first row is non-numeric), not configuration-driven by default.**
  A CSV with no header row and a first data row that happens to look
  header-like (all text, no numbers) will be misclassified — documented
  explicitly as a limitation rather than treated as solved, since no
  purely structural heuristic can be certain here.
- **Ragged rows are padded/truncated by default rather than dropped.** A
  row missing its last column still has real data in the columns it does
  have; dropping it by default would throw away more information than
  necessary. `--drop-ragged-rows` is available for callers who'd rather
  have a strictly rectangular result than a padded one.
- **Null normalization only touches whitespace and known null-token
  spellings — it does not attempt type coercion (e.g. parsing a numeric
  column into actual numbers).** Type coercion is a meaningfully
  different operation with its own failure modes (locale-dependent
  number formats, date parsing ambiguity) that would expand this
  project's scope significantly; kept out deliberately rather than
  half-implemented.

## Algorithms Used

- `csv.Sniffer.sniff()` with an explicit candidate delimiter set, backed
  by a manual character-frequency fallback.
- Single-pass row iteration via `csv.reader`, with column-count
  comparison against the detected header width driving the pad/truncate/
  drop decision per row.

## Tradeoffs

- Reads the whole file into memory as one decoded string
  (`in_path.read_bytes()` then `.splitlines()`) rather than truly
  streaming line-by-line from disk. This was a deliberate simplification
  — correct BOM stripping and delimiter sniffing both want to look at
  the file's start before committing to a parsing strategy, which is
  awkward to do with a strict single-pass file-handle stream. For files
  too large to fit in memory, the log analyzer project (01) is the
  portfolio's reference for genuinely constant-memory streaming; this
  project prioritizes correctness of the sniffing/repair logic over
  that property.
- Header detection can't distinguish "no header, first row happens to be
  all-text data" from "there is a header" — documented as a known
  limitation rather than solved with unreliable extra heuristics that
  would just move the ambiguity around instead of removing it.

## Edge Cases Handled

| Case | Behavior |
|---|---|
| Comma/semicolon/tab/pipe delimited | Auto-detected |
| Single-column file (Sniffer can't decide) | Falls back to comma, no crash |
| Row with too few columns | Padded with empty cells (or dropped, if requested) |
| Row with too many columns | Truncated (or dropped, if requested) |
| Entirely blank row | Always dropped |
| `NULL`/`N/A`/`--`/empty/etc. | Normalized to a consistent empty value |
| Value that merely resembles a null token (e.g. "nationality") | Left untouched — exact match only, not substring |
| Invalid UTF-8 bytes anywhere in the file | Replaced with U+FFFD, `encoding_errors` flagged, no crash |
| Leading BOM | Stripped before header parsing |
| Duplicate header names | De-duplicated (`name`, `name_1`, `name_2`, ...) |
| No header row (first row is data) | Synthesized column names, first row preserved as data |
| Empty file | `ValueError`, clear message |
| Missing file | `FileNotFoundError`, clear message |
| `Ctrl-C` mid-run | Reported clearly; whatever was written so far is left in place, not silently truncated without a message |

## Examples

```
$ ./csv_cleaner.py messy.csv clean.csv --report report.json
delimiter detected: ','
header: yes
rows read: 6, written: 5, dropped: 1
full report written to report.json

$ cat report.json
{
  "rows_read": 6,
  "rows_written": 5,
  "rows_dropped": 1,
  "issues": [
    {"row_number": 3, "reason": "truncated_long_row", "detail": "truncated from 4 to 3 columns"},
    {"row_number": 4, "reason": "padded_short_row", "detail": "padded from 2 to 3 columns"},
    {"row_number": 5, "reason": "blank_row", "detail": "row was entirely empty"}
  ]
}
```

## Limitations

- Not truly constant-memory streaming (see Tradeoffs) — reads the full
  decoded file into memory before processing rows.
- Header-vs-data detection is a heuristic and can be wrong for a
  headerless file whose first data row happens to be all non-numeric
  text.
- No type coercion — output values remain strings; a consumer wanting
  actual numeric/date types needs a separate pass.
- Delimiter detection samples only the first `MAX_SAMPLE_BYTES` of the
  file; a delimiter that only becomes distinguishable later in a very
  unusual file wouldn't be caught.

## Lessons Learned

The header-detection heuristic (`all cells are non-numeric` ⇒ header)
initially also required every cell to be non-empty, which meant a
legitimate header row with one blank cell (fairly common — an unnamed
index column, for instance) was misclassified as data. Loosened the
check to only require non-numeric-ness, and added the
`test_header_row_detected_when_non_numeric` /
`test_no_header_synthesizes_column_names` pair specifically to pin both
directions of this decision down, since a heuristic like this is exactly
the kind of logic that regresses silently if only one direction is
tested.

## Future Improvements

- Optional type-inference pass (numeric/date coercion) as a separate,
  explicitly-opt-in step.
- True line-by-line streaming for very large files, at the cost of doing
  BOM/delimiter detection from a bounded read-ahead buffer instead of
  the whole decoded text.
- Configurable null-token list instead of the fixed `NULL_TOKENS` set.

## References

- Python `csv` module documentation, `Sniffer` class
- RFC 4180 (CSV format, for what "well-formed" even means as a baseline)
