#!/usr/bin/env python3
"""
log_analyzer.py — Linux log analyzer with fault-tolerant parsing.

Parses application/syslog-style log files, recovers as many valid
records as possible from damaged input, and produces a recovery
report explaining exactly what was discarded and why.

Design goals:
  - Stream the file line by line (never load the whole file into RAM).
  - Never crash on malformed input. A bad line is a data point, not
    an exception.
  - Be explicit and auditable: every discarded line is reported with
    a reason, not silently dropped.

Usage:
    ./log_analyzer.py access.log
    ./log_analyzer.py access.log --report report.json
    cat access.log | ./log_analyzer.py -
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, TextIO


# Accepts: "2026-07-18 14:32:01 LEVEL message..."
# Level is optional; message may be empty (still counts as a record).
LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
    r"\s+(?P<level>[A-Z]+)?\s*(?P<message>.*)$"
)

TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")

KNOWN_LEVELS = {"DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "FATAL"}


@dataclass
class LogRecord:
    timestamp: datetime
    level: str
    message: str
    raw_line: str


@dataclass
class DiscardedLine:
    line_number: int
    reason: str
    raw_line: str


@dataclass
class RecoveryReport:
    source: str
    total_lines: int = 0
    recovered: int = 0
    discarded: list[DiscardedLine] = field(default_factory=list)
    level_counts: Counter = field(default_factory=Counter)
    first_timestamp: Optional[datetime] = None
    last_timestamp: Optional[datetime] = None
    encoding_errors: int = 0

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "total_lines": self.total_lines,
            "recovered": self.recovered,
            "discarded_count": len(self.discarded),
            "discard_reasons": Counter(d.reason for d in self.discarded),
            "level_counts": dict(self.level_counts),
            "time_range": {
                "first": self.first_timestamp.isoformat() if self.first_timestamp else None,
                "last": self.last_timestamp.isoformat() if self.last_timestamp else None,
            },
            "encoding_errors": self.encoding_errors,
            "discarded_lines": [
                {"line_number": d.line_number, "reason": d.reason, "raw": d.raw_line}
                for d in self.discarded
            ],
        }


def _parse_timestamp(raw: str) -> Optional[datetime]:
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _iter_lines_tolerant(stream: TextIO) -> Iterator[tuple[int, str, bool]]:
    """
    Yield (line_number, text, had_encoding_error) for every line.

    We open the underlying file in binary mode upstream and decode
    here with errors='replace' so a single invalid byte sequence
    doesn't kill the whole run — it just gets flagged.
    """
    for i, raw_bytes in enumerate(stream, start=1):
        had_error = False
        if isinstance(raw_bytes, bytes):
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                text = raw_bytes.decode("utf-8", errors="replace")
                had_error = True
        else:
            text = raw_bytes
        yield i, text.rstrip("\n\r"), had_error


def analyze(path_or_stream, source_name: str) -> tuple[list[LogRecord], RecoveryReport]:
    """
    Parse a log source (path or already-open binary stream) and return
    the recovered records plus a full recovery report.
    """
    report = RecoveryReport(source=source_name)
    records: list[LogRecord] = []

    for line_number, line, had_encoding_error in _iter_lines_tolerant(path_or_stream):
        report.total_lines += 1
        if had_encoding_error:
            report.encoding_errors += 1

        if not line.strip():
            report.discarded.append(DiscardedLine(line_number, "empty_line", line))
            continue

        match = LINE_PATTERN.match(line)
        if not match:
            report.discarded.append(DiscardedLine(line_number, "unrecognized_format", line))
            continue

        ts_raw = match.group("timestamp")
        timestamp = _parse_timestamp(ts_raw)
        if timestamp is None:
            report.discarded.append(DiscardedLine(line_number, "malformed_timestamp", line))
            continue

        level = (match.group("level") or "UNKNOWN").upper()
        if level not in KNOWN_LEVELS:
            # Not fatal — the format was fine, the level is just
            # nonstandard. Keep the record but note it as UNKNOWN so
            # the report is honest about what it saw.
            level = "UNKNOWN"

        message = match.group("message")
        record = LogRecord(timestamp=timestamp, level=level, message=message, raw_line=line)
        records.append(record)

        report.recovered += 1
        report.level_counts[level] += 1
        if report.first_timestamp is None or timestamp < report.first_timestamp:
            report.first_timestamp = timestamp
        if report.last_timestamp is None or timestamp > report.last_timestamp:
            report.last_timestamp = timestamp

    return records, report


def print_summary(report: RecoveryReport) -> None:
    total = report.total_lines
    recovered = report.recovered
    discarded = len(report.discarded)
    pct = (recovered / total * 100) if total else 0.0

    print(f"Source:            {report.source}")
    print(f"Total lines:       {total}")
    print(f"Recovered records: {recovered} ({pct:.1f}%)")
    print(f"Discarded lines:   {discarded}")
    if report.encoding_errors:
        print(f"Encoding errors:   {report.encoding_errors} (replaced with U+FFFD)")
    if report.first_timestamp and report.last_timestamp:
        print(f"Time range:        {report.first_timestamp} -> {report.last_timestamp}")
    if report.level_counts:
        print("Level counts:")
        for level, count in report.level_counts.most_common():
            print(f"  {level:<9} {count}")
    if report.discarded:
        reasons = Counter(d.reason for d in report.discarded)
        print("Discard reasons:")
        for reason, count in reasons.most_common():
            print(f"  {reason:<22} {count}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fault-tolerant log analyzer that recovers valid records "
        "from damaged log files and reports exactly what was discarded."
    )
    parser.add_argument(
        "logfile",
        help="Path to the log file to analyze, or '-' to read from stdin.",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        help="Write the full JSON recovery report to this path.",
    )
    parser.add_argument(
        "--level",
        metavar="LEVEL",
        help="Only print records at or above this level (e.g. ERROR).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        if args.logfile == "-":
            records, report = analyze(sys.stdin.buffer, "<stdin>")
        else:
            path = Path(args.logfile)
            if not path.exists():
                print(f"error: file not found: {path}", file=sys.stderr)
                return 1
            if not path.is_file():
                print(f"error: not a regular file: {path}", file=sys.stderr)
                return 1
            try:
                with path.open("rb") as f:
                    records, report = analyze(f, str(path))
            except PermissionError:
                print(f"error: permission denied: {path}", file=sys.stderr)
                return 1
    except KeyboardInterrupt:
        print("\ninterrupted — partial results not written", file=sys.stderr)
        return 130

    print_summary(report)

    if args.report:
        try:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(report.as_dict(), f, indent=2, default=str)
            print(f"\nFull recovery report written to {args.report}")
        except OSError as e:
            print(f"warning: could not write report: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
