# Engineering Journal — Corrupted CSV Cleaner

## Problem

Needed a CSV cleaner that treats "the delimiter isn't a comma," "this row
has the wrong number of columns," and "there's no header at all" as
routine input variation, not exceptional failure — while producing an
honest, itemized record of every fix it made.

## Initial Idea

First draft assumed comma delimiter unconditionally and required every
row to have exactly the header's column count, raising on the first
mismatch.

## Why It Failed

Ran it against a hand-built semicolon-delimited fixture before writing
any tests, expecting it to at least produce garbled output — instead it
produced a single "column" per row containing the entire semicolon-joined
line, because comma-splitting a semicolon-delimited file finds no commas
to split on. That's the kind of silent-wrong-output failure that's worse
than a crash: a crash tells you something's wrong immediately, silently
wrong output doesn't. This is what motivated building real delimiter
detection instead of assuming comma.

Separately, raising on the first ragged row meant a single stray line
(a trailing blank line, a row with one extra trailing comma from a
spreadsheet export) would abort processing of an otherwise-fine file —
exactly the "one bad line kills everything" failure mode this whole
portfolio has been deliberately avoiding since project 01.

## Alternative Approaches Considered

For delimiter detection:
1. **Always require the caller to specify the delimiter.** Rejected as
   the default — defeats the purpose of a tool meant to handle messy,
   unknown-provenance files without hand-holding. Kept as an *optional*
   override (`--delimiter`) for when the caller does know.
2. **`csv.Sniffer` alone.** Tried this first as the "proper" stdlib
   solution — but `Sniffer.sniff()` raises `csv.Error` on inputs it can't
   confidently classify (a single-column file has no delimiter to find,
   for instance), which would make the tool crash on a legitimately valid
   edge case.
3. **`Sniffer`, falling back to a manual frequency count over a small
   candidate set, falling back to comma.** Chosen — three-tier fallback
   so a `Sniffer` failure degrades gracefully instead of propagating.

For ragged rows:
1. **Raise/abort on any row with the wrong column count.** Rejected for
   the reason above — one bad row shouldn't cost every other row in the
   file.
2. **Pad short rows and truncate long rows by default, with an opt-in
   `--drop-ragged-rows` flag for callers who want strict rectangularity
   instead.** Chosen — recovers the most data by default (a mostly-good
   row with one missing trailing field is still mostly-good data) while
   giving control to callers who'd rather have a smaller, perfectly
   rectangular result.

## Benchmarking / Performance Observations

Not benchmarked at scale; see the README's Tradeoffs section for the
explicit decision to read the whole file into memory rather than truly
stream it, made in favor of correct BOM/delimiter-sniffing behavior. For
files where memory is a real constraint, project 01's line-by-line
streaming pattern is the portfolio's reference design instead.

## Refactoring Decisions

Pulled null-token normalization into its own `normalize_value()`
function, tested independently of `clean_csv()` via direct unit tests
(`TestNormalizeValue`), specifically so the "does 'nationality' get
wrongly treated as a null token because it contains 'na'" question could
be answered with a fast, isolated test instead of constructing a whole
CSV fixture. That test exists because it's exactly the kind of
substring-matching bug that's easy to introduce accidentally (checking
`"na" in value.lower()` instead of exact-matching against the null-token
set) and easy to miss without a test aimed directly at it.

## Final Implementation

Single read of the file's bytes, tolerant decode (replacing invalid
UTF-8), BOM strip, delimiter sniff against a bounded sample, then a
single pass over rows via `csv.reader` comparing each row's length
against the header's column count to decide pad/truncate/drop. Every
row-level decision is logged to `CleaningReport.issues`, mirroring
project 01's `RecoveryReport` shape.

## Engineering Tradeoffs

Chose full-file-in-memory processing over strict line-by-line streaming
specifically because delimiter sniffing and BOM handling both benefit
from seeing the file's start before any row-processing commitments are
made — a truly single-pass streaming design would need a more complex
read-ahead buffer to get the same correctness, for a project whose scope
is "clean messy CSVs correctly," not "handle CSVs too large to fit in
memory" (that concern already has a dedicated reference implementation
in project 01, and will get another in project 09's duplicate finder).

## Possible Future Versions

- Optional numeric/date type-coercion pass, kept separate from null
  normalization since it's a meaningfully different (and error-prone)
  operation.
- Bounded read-ahead streaming variant for very large files.
- Configurable null-token list instead of the current fixed set.
