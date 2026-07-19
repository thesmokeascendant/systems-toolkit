"""
Unit tests for log_analyzer.py

Run with:
    python3 -m unittest discover -s tests -v
"""

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from log_analyzer import analyze, _parse_timestamp  # noqa: E402


def _stream(text: str):
    """Simulate the binary file stream analyze() expects."""
    return io.BytesIO(text.encode("utf-8"))


class TestTimestampParsing(unittest.TestCase):
    def test_standard_format(self):
        self.assertIsNotNone(_parse_timestamp("2026-07-18 09:00:00"))

    def test_iso_format(self):
        self.assertIsNotNone(_parse_timestamp("2026-07-18T09:00:00"))

    def test_invalid_timestamp_returns_none(self):
        self.assertIsNone(_parse_timestamp("2026-25-99 99:99:99"))

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_timestamp("not a timestamp"))


class TestAnalyze(unittest.TestCase):
    def test_all_valid_lines_recovered(self):
        log = (
            "2026-07-18 09:00:00 INFO started\n"
            "2026-07-18 09:00:01 ERROR something broke\n"
        )
        records, report = analyze(_stream(log), "test")
        self.assertEqual(report.recovered, 2)
        self.assertEqual(len(report.discarded), 0)
        self.assertEqual(records[0].level, "INFO")
        self.assertEqual(records[1].level, "ERROR")

    def test_empty_lines_discarded_not_crash(self):
        log = "2026-07-18 09:00:00 INFO ok\n\n\n2026-07-18 09:00:01 INFO ok again\n"
        records, report = analyze(_stream(log), "test")
        self.assertEqual(report.recovered, 2)
        self.assertEqual(
            sum(1 for d in report.discarded if d.reason == "empty_line"), 2
        )

    def test_malformed_timestamp_discarded_with_reason(self):
        log = "2026-99-99 99:99:99 ERROR bad time\n"
        records, report = analyze(_stream(log), "test")
        self.assertEqual(report.recovered, 0)
        self.assertEqual(report.discarded[0].reason, "malformed_timestamp")

    def test_unrecognized_format_discarded(self):
        log = "not a log line at all\n"
        records, report = analyze(_stream(log), "test")
        self.assertEqual(report.recovered, 0)
        self.assertEqual(report.discarded[0].reason, "unrecognized_format")

    def test_unknown_level_still_recovered(self):
        log = "2026-07-18 09:00:00 NOTICE something happened\n"
        records, report = analyze(_stream(log), "test")
        self.assertEqual(report.recovered, 1)
        self.assertEqual(records[0].level, "UNKNOWN")

    def test_invalid_utf8_does_not_crash_and_is_flagged(self):
        raw = b"2026-07-18 09:00:00 INFO ok\n\xff\xfe not valid utf8\n"
        records, report = analyze(io.BytesIO(raw), "test")
        self.assertEqual(report.encoding_errors, 1)
        # The pipeline kept running after the bad bytes.
        self.assertGreaterEqual(report.total_lines, 2)

    def test_empty_file_no_crash(self):
        records, report = analyze(_stream(""), "test")
        self.assertEqual(report.total_lines, 0)
        self.assertEqual(report.recovered, 0)

    def test_time_range_tracked_correctly(self):
        log = (
            "2026-07-18 09:05:00 INFO middle\n"
            "2026-07-18 09:00:00 INFO earliest\n"
            "2026-07-18 09:10:00 INFO latest\n"
        )
        records, report = analyze(_stream(log), "test")
        self.assertEqual(report.first_timestamp.hour, 9)
        self.assertEqual(report.first_timestamp.minute, 0)
        self.assertEqual(report.last_timestamp.minute, 10)


if __name__ == "__main__":
    unittest.main()
