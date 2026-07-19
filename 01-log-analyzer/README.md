# 01 — Linux Log Analyzer

A fault-tolerant log analyzer that parses syslog-style log files, recovers
every usable record even from damaged input, and produces a recovery report
that explains exactly what was discarded and why.

## Problem

Real logs are messy: truncated writes, mixed timestamp formats, stray binary
bytes from a crashed process, blank lines from log rotation. A naive parser
either crashes on the first bad line or silently drops data with no record
of what was lost. Neither is acceptable for a tool meant to run against
production logs.

## Requirements

- Parse standard `YYYY-MM-DD HH:MM:SS LEVEL message` log lines.
- Never crash on malformed input — a bad line is data, not an exception.
- Stream the file; never load the whole file into memory.
- Recover every line that can reasonably be recovered.
- For every discarded line, record the line number, the raw content, and
  the specific reason it was discarded.
- Handle invalid UTF-8 byte sequences without aborting the run.
- Work on regular files, `stdin`, and report file-not-found / permission
  errors cleanly instead of raising a traceback.

## Architecture

```
log_analyzer.py
├── _iter_lines_tolerant()   # binary-safe line reader, flags encoding errors
├── _parse_timestamp()       # tries known timestamp formats in order
├── analyze()                # core parse loop → (records, RecoveryReport)
├── print_summary()          # human-readable stdout summary
└── main()                   # CLI: argument parsing, I/O, exit codes
```

The parser is a single streaming pass: read one line, classify it, move on.
There is no buffering of the whole file and no two-pass logic, so memory
use is O(1) with respect to file size (aside from the accumulated records
list, which a future version could make optional for very large files —
see Future Improvements).

## Design Decisions

- **Binary-mode file reads.** The file is opened `rb` and decoded manually
  per line with `errors="replace"`. This means one corrupted byte sequence
  degrades gracefully (replacement character, flagged in the report)
  instead of raising `UnicodeDecodeError` and killing the whole run.
- **Regex-based line matching, not a state machine.** For a single-line
  log format, a compiled regex is simpler to read and audit than a hand
  rolled parser, and it's fast enough — Python's `re` module is C-backed.
- **Unknown levels are kept, not discarded.** A line with a valid timestamp
  but a non-standard level (e.g. `NOTICE`) is still a real log record. It's
  tagged `UNKNOWN` rather than thrown away — losing structurally valid data
  because of a naming mismatch would be the wrong failure mode for a
  *recovery* tool.
- **Discard reasons are an explicit enum-like set of strings** (
  `empty_line`, `unrecognized_format`, `malformed_timestamp`), not free-text
  exception messages, so the report can be aggregated and counted.

## Algorithms Used

- Single-pass streaming scan, O(n) in the number of lines.
- Regex-based structural match for the line format.
- Ordered fallback timestamp parsing (tries each known format in turn).

## Tradeoffs

- Keeping all recovered `LogRecord` objects in a list is simple and fine
  for typical log sizes, but for a multi-gigabyte file a generator-based
  streaming API (yield records instead of collecting them) would be more
  memory-efficient. Deferred — see Future Improvements.
- The line format is intentionally narrow (one timestamp format family).
  Broadening it to arbitrary log formats (e.g. Apache combined log format)
  would need a pluggable parser strategy rather than a single regex.

## Edge Cases Handled

| Case | Behavior |
|---|---|
| Empty file | Reports 0 total, 0 recovered, no crash |
| Blank lines | Discarded with reason `empty_line` |
| Line with no timestamp | Discarded with reason `unrecognized_format` |
| Impossible timestamp (`25:99:99`) | Discarded with reason `malformed_timestamp` |
| Invalid UTF-8 bytes | Line decoded with replacement char, `encoding_errors` incremented, run continues |
| Nonstandard log level | Record kept, tagged `UNKNOWN` |
| File not found | Clean error message, exit code 1, no traceback |
| Permission denied | Clean error message, exit code 1, no traceback |
| `Ctrl-C` mid-run | Caught, exits with code 130, no partial-write corruption |
| Input via `stdin` (`-`) | Supported for piping |

## Examples

Input (`sample_logs/mixed.log`) — 11 lines, includes a line with no
timestamp, an impossible timestamp, a blank line, and a nonstandard level.

```
$ ./log_analyzer.py sample_logs/mixed.log --report /tmp/report.json
Source:            sample_logs/mixed.log
Total lines:       11
Recovered records: 8 (72.7%)
Discarded lines:   3
Time range:        2026-07-18 09:00:01 -> 2026-07-18 09:06:02
Level counts:
  INFO      3
  DEBUG     1
  WARN      1
  ERROR     1
  CRITICAL  1
  UNKNOWN   1
Discard reasons:
  unrecognized_format    1
  malformed_timestamp    1
  empty_line             1

Full recovery report written to /tmp/report.json
```

## Limitations

- Only recognizes the two timestamp formats implemented (`YYYY-MM-DD
  HH:MM:SS` and its `T`-separated ISO variant). Other formats (e.g. syslog's
  `Mon DD HH:MM:SS`) are not yet supported and will be discarded as
  `unrecognized_format`.
- Multi-line log records (e.g. a Python traceback following an ERROR line)
  are treated as separate, unrelated lines rather than being associated
  with the record that triggered them.
- No timezone handling — timestamps are parsed as naive `datetime` objects.

## Lessons Learned

Building the encoding handling first, before the format parsing, made the
rest of the pipeline much simpler to reason about — once every line is
guaranteed to be a Python `str` (even if some are `str` with replacement
characters), the parsing logic never has to think about bytes again. Trying
to interleave decode-error handling with format parsing in an earlier draft
produced tangled `try/except` nesting; separating the concerns into two
clear stages (`_iter_lines_tolerant` → `analyze`) fixed that.

## Future Improvements

- Pluggable format parsers (syslog, Apache/nginx combined log format, JSON
  lines) selected via `--format` flag.
- Generator-based `analyze()` variant for constant-memory processing of
  very large files.
- Timezone-aware timestamp parsing.
- Multi-line record association (e.g. stack traces).

## References

- Python `re` module documentation
- Python `datetime.strptime` format codes
- RFC 3164 (BSD syslog format) — for future format support
