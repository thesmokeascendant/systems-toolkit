# Engineering Journal — Log Analyzer

## Problem

Needed a log parser that treats malformed input as the normal case, not
the exception. Most tutorial-grade log parsers assume clean input and fall
over the moment they meet a truncated line or a bad byte.

## Initial Idea

First draft opened the file in text mode (`open(path, "r")`) and parsed
each line directly with the regex. Worked fine on clean input.

## Why It Failed

Text-mode reading with default encoding raises `UnicodeDecodeError` on the
first invalid byte and aborts the whole read — there's no way to catch it
per-line because the decode happens inside the file iterator itself, not
in code I control. A single corrupted byte anywhere in a multi-thousand
line file would kill the entire analysis. That's the opposite of what a
*recovery* tool should do.

## Alternative Approaches Considered

1. **Wrap the whole read in try/except and abort on first bad line.**
   Rejected — defeats the purpose. One bad line shouldn't cost you every
   subsequent good line.
2. **Read the whole file as bytes, split on `\n`, decode each chunk with
   `errors="replace"`.** Works, but loads the entire file into memory
   first — violates the streaming requirement for large files.
3. **Open in binary mode, iterate lines as bytes, decode per-line with
   `errors="replace"`.** Chosen. Keeps the streaming property (the file
   iterator still yields one line at a time) while giving per-line control
   over decode failures.

## Benchmarking / Performance Observations

Not yet benchmarked against a multi-GB file — this project's sample logs
are small by design (the point is correctness on edge cases, not scale).
Noting this as a gap: the duplicate-finder project (09) is where the
portfolio's scale/performance work is meant to live, so I'm deliberately
not duplicating that effort here.

## Refactoring Decisions

Originally the "is this line garbage" logic and the "what does this line
mean" logic were both inline in one big loop inside `analyze()`. Pulled
timestamp parsing out into `_parse_timestamp()` and line iteration out into
`_iter_lines_tolerant()` specifically so each piece could be unit-tested in
isolation without needing a full file on disk. That split is why the test
file can test `_parse_timestamp()` directly instead of only testing through
the CLI.

## Final Implementation

Single streaming pass. Binary read → per-line tolerant decode → regex
structural match → timestamp parse → classify as recovered or discarded
with a specific reason. Every discard reason is a fixed string so the
report can aggregate counts instead of being a wall of free-text errors.

## Engineering Tradeoffs

Chose correctness and auditability (explicit discard reasons, full
recovery report) over raw parsing speed. For a log analyzer whose whole
purpose is trustworthy recovery from damaged input, being able to answer
"why was this line dropped" matters more than shaving milliseconds off a
regex match.

## Possible Future Versions

- Format plugins so the same recovery-report architecture works for
  nginx/Apache logs, not just the one timestamp format implemented here.
- A `--fix` mode that writes out a cleaned log file alongside the report.
