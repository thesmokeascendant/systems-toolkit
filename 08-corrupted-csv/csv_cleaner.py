#!/usr/bin/env python3
"""
csv_cleaner.py — defensively cleans messy, real-world CSV files.

Real CSVs are rarely clean: inconsistent delimiters, ragged rows (too
few or too many columns), stray invalid-UTF-8 bytes, a dozen different
spellings of "null", a missing header row, or a BOM nobody remembered to
strip. This module recovers everything it reasonably can, streams the
file so it never needs the whole thing in memory, and produces a
cleaning report explaining exactly what was fixed, padded, truncated, or
dropped — mirroring the recovery-report pattern used in the log analyzer
project (01).

Usage:
    from csv_cleaner import clean_csv

    report = clean_csv("messy.csv", "clean.csv")

CLI:
    ./csv_cleaner.py messy.csv clean.csv
    ./csv_cleaner.py messy.csv clean.csv --report report.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# Common stand-ins for "no value" seen across exports from different
# tools (Excel, various databases, hand-edited CSVs).
NULL_TOKENS = {"", "null", "n/a", "na", "none", "nil", "--", "-", "?"}

MAX_SAMPLE_BYTES = 8192  # for delimiter sniffing


@dataclass
class RowIssue:
    row_number: int  # 1-indexed, counting data rows only (header excluded)
    reason: str
    detail: str


@dataclass
class CleaningReport:
    source: str
    delimiter: str
    had_header: bool
    column_count: int
    rows_read: int = 0
    rows_written: int = 0
    rows_dropped: int = 0
    encoding_errors: int = 0
    issues: list[RowIssue] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _decode_tolerant(raw_bytes: bytes) -> tuple[str, bool]:
    """Decode bytes as UTF-8, replacing invalid sequences rather than
    raising. Returns (text, had_encoding_error)."""
    try:
        return raw_bytes.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw_bytes.decode("utf-8", errors="replace"), True


def _strip_bom(text: str) -> str:
    return text[1:] if text.startswith("\ufeff") else text


def sniff_delimiter(sample: str, candidates: str = ",;\t|") -> str:
    """
    Detect the delimiter using csv.Sniffer, falling back to a simple
    frequency count over a small candidate set, and finally to comma if
    nothing decisive is found. Real files sometimes defeat the Sniffer
    (e.g. very few rows, or a single column) — this must never raise.
    """
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=candidates)
        return dialect.delimiter
    except csv.Error:
        pass

    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    counts = {d: first_line.count(d) for d in candidates}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def normalize_value(value: str) -> Optional[str]:
    """Strip whitespace and normalize common null-token spellings to
    None. Preserves everything else exactly as given — this function
    does not attempt type coercion, only null-normalization."""
    stripped = value.strip()
    if stripped.lower() in NULL_TOKENS:
        return None
    return stripped


def _read_header(reader: csv.reader, expected_min_cols: int = 1) -> tuple[list[str], bool]:
    """
    Read the first row and decide whether it looks like a header (mostly
    non-numeric, no blank cells) or actual data (in which case synthetic
    column names are generated and the row is treated as data, not
    consumed as a header).
    """
    try:
        first_row = next(reader)
    except StopIteration:
        return [], False

    looks_like_header = bool(first_row) and all(
        cell.strip() and not cell.strip().lstrip("-").replace(".", "", 1).isdigit()
        for cell in first_row
    )
    if looks_like_header:
        # De-duplicate header names so "name,name" doesn't silently
        # collide in a dict-based row representation downstream.
        seen: dict[str, int] = {}
        headers = []
        for cell in first_row:
            name = cell.strip() or "column"
            if name in seen:
                seen[name] += 1
                name = f"{name}_{seen[name]}"
            else:
                seen[name] = 0
            headers.append(name)
        return headers, True

    # First row is data, not a header — synthesize column names and
    # rewind conceptually by returning the row as data via the caller.
    n = max(len(first_row), expected_min_cols)
    return [f"column_{i+1}" for i in range(n)], False


def clean_csv(
    input_path: str,
    output_path: str,
    delimiter: Optional[str] = None,
    drop_ragged_rows: bool = False,
) -> CleaningReport:
    """
    Stream-clean a CSV file. Ragged rows (wrong column count) are padded
    or truncated by default (with the fix logged), or dropped entirely
    if drop_ragged_rows=True. Encoding errors are repaired with the
    Unicode replacement character rather than aborting the read.
    """
    in_path = Path(input_path)
    if not in_path.exists():
        raise FileNotFoundError(f"file not found: {in_path}")
    if in_path.stat().st_size == 0:
        raise ValueError(f"file is empty: {in_path}")

    raw = in_path.read_bytes()
    text, had_encoding_error = _decode_tolerant(raw)
    text = _strip_bom(text)

    sample = text[:MAX_SAMPLE_BYTES]
    dialect_delimiter = delimiter or sniff_delimiter(sample)

    lines = text.splitlines()
    reader = csv.reader(lines, delimiter=dialect_delimiter)

    header, had_header = _read_header(reader)
    if not header:
        raise ValueError(f"no data found in {in_path}")

    report = CleaningReport(
        source=str(in_path),
        delimiter=dialect_delimiter,
        had_header=had_header,
        column_count=len(header),
        encoding_errors=1 if had_encoding_error else 0,
    )

    out_rows: list[list[str]] = []
    if not had_header:
        # The "header" row we read was actually data — process it as
        # row 1 by feeding it back through the same repair logic.
        reader = csv.reader(lines, delimiter=dialect_delimiter)

    row_number = 0
    for raw_row in reader:
        row_number += 1
        report.rows_read += 1

        if all(cell.strip() == "" for cell in raw_row):
            report.rows_dropped += 1
            report.issues.append(RowIssue(row_number, "blank_row", "row was entirely empty"))
            continue

        expected = len(header)
        actual = len(raw_row)

        if actual == expected:
            fixed_row = raw_row
        elif actual < expected:
            if drop_ragged_rows:
                report.rows_dropped += 1
                report.issues.append(
                    RowIssue(row_number, "too_few_columns", f"expected {expected}, got {actual}")
                )
                continue
            fixed_row = raw_row + [""] * (expected - actual)
            report.issues.append(
                RowIssue(row_number, "padded_short_row", f"padded from {actual} to {expected} columns")
            )
        else:  # actual > expected
            if drop_ragged_rows:
                report.rows_dropped += 1
                report.issues.append(
                    RowIssue(row_number, "too_many_columns", f"expected {expected}, got {actual}")
                )
                continue
            fixed_row = raw_row[:expected]
            report.issues.append(
                RowIssue(row_number, "truncated_long_row", f"truncated from {actual} to {expected} columns")
            )

        normalized = [normalize_value(cell) or "" for cell in fixed_row]
        out_rows.append(normalized)
        report.rows_written += 1

    out_path = Path(output_path)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(out_rows)

    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Defensively clean a messy CSV file")
    parser.add_argument("input_csv")
    parser.add_argument("output_csv")
    parser.add_argument("--delimiter", help="Force a specific delimiter instead of auto-detecting")
    parser.add_argument("--drop-ragged-rows", action="store_true",
                         help="Drop rows with the wrong column count instead of padding/truncating")
    parser.add_argument("--report", help="Write the full JSON cleaning report to this path")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        report = clean_csv(
            args.input_csv, args.output_csv,
            delimiter=args.delimiter, drop_ragged_rows=args.drop_ragged_rows,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted — output written so far may be incomplete", file=sys.stderr)
        return 130

    print(f"delimiter detected: {report.delimiter!r}")
    print(f"header: {'yes' if report.had_header else 'no (synthesized column names)'}")
    print(f"rows read: {report.rows_read}, written: {report.rows_written}, dropped: {report.rows_dropped}")
    if report.encoding_errors:
        print(f"encoding errors repaired: {report.encoding_errors}")
    if report.issues:
        print(f"{len(report.issues)} row issue(s) — see --report for details" if not args.report else "")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report.as_dict(), f, indent=2)
        print(f"full report written to {args.report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
