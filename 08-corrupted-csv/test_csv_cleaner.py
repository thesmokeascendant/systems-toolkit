"""
Unit tests for csv_cleaner.py

Run with:
    python3 -m unittest discover -s tests -v
"""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csv_cleaner import (  # noqa: E402
    clean_csv,
    normalize_value,
    sniff_delimiter,
)


class TempFileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, content: bytes) -> str:
        path = self.root / name
        path.write_bytes(content)
        return str(path)

    def read_output_rows(self, path: str) -> list[list[str]]:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.reader(f))


class TestNormalizeValue(unittest.TestCase):
    def test_strips_whitespace(self):
        self.assertEqual(normalize_value("  Ada  "), "Ada")

    def test_common_null_tokens_become_none(self):
        for token in ("", "NULL", "null", "N/A", "n/a", "None", "--", "?"):
            with self.subTest(token=token):
                self.assertIsNone(normalize_value(token))

    def test_real_value_resembling_null_prefix_kept(self):
        # Must not over-match — "nationality" should not be treated as
        # a null token just because it contains "na".
        self.assertEqual(normalize_value("nationality"), "nationality")


class TestSniffDelimiter(unittest.TestCase):
    def test_detects_comma(self):
        self.assertEqual(sniff_delimiter("a,b,c\n1,2,3\n"), ",")

    def test_detects_semicolon(self):
        self.assertEqual(sniff_delimiter("a;b;c\n1;2;3\n"), ";")

    def test_detects_tab(self):
        self.assertEqual(sniff_delimiter("a\tb\tc\n1\t2\t3\n"), "\t")

    def test_single_column_falls_back_to_comma_without_crashing(self):
        result = sniff_delimiter("just_one_column\nvalue\n")
        self.assertEqual(result, ",")

    def test_empty_sample_does_not_crash(self):
        result = sniff_delimiter("")
        self.assertEqual(result, ",")


class TestCleanCsvBasics(TempFileTestCase):
    def test_clean_well_formed_csv_passes_through(self):
        path = self.write("clean.csv", b"name,age\nAda,36\nGrace,41\n")
        out = str(self.root / "out.csv")
        report = clean_csv(path, out)
        self.assertEqual(report.rows_read, 2)
        self.assertEqual(report.rows_written, 2)
        self.assertEqual(report.rows_dropped, 0)
        rows = self.read_output_rows(out)
        self.assertEqual(rows[0], ["name", "age"])
        self.assertEqual(rows[1], ["Ada", "36"])

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            clean_csv(str(self.root / "nope.csv"), str(self.root / "out.csv"))

    def test_empty_file_raises(self):
        path = self.write("empty.csv", b"")
        with self.assertRaises(ValueError):
            clean_csv(path, str(self.root / "out.csv"))


class TestRaggedRows(TempFileTestCase):
    def test_short_row_padded_by_default(self):
        path = self.write("ragged.csv", b"a,b,c\n1,2,3\n4,5\n")
        out = str(self.root / "out.csv")
        report = clean_csv(path, out)
        rows = self.read_output_rows(out)
        self.assertEqual(rows[2], ["4", "5", ""])
        self.assertEqual(report.rows_written, 2)
        self.assertTrue(any(i.reason == "padded_short_row" for i in report.issues))

    def test_long_row_truncated_by_default(self):
        path = self.write("ragged.csv", b"a,b\n1,2\n3,4,5,6\n")
        out = str(self.root / "out.csv")
        report = clean_csv(path, out)
        rows = self.read_output_rows(out)
        self.assertEqual(rows[2], ["3", "4"])
        self.assertTrue(any(i.reason == "truncated_long_row" for i in report.issues))

    def test_ragged_rows_dropped_when_requested(self):
        path = self.write("ragged.csv", b"a,b,c\n1,2,3\n4,5\n")
        out = str(self.root / "out.csv")
        report = clean_csv(path, out, drop_ragged_rows=True)
        self.assertEqual(report.rows_written, 1)
        self.assertEqual(report.rows_dropped, 1)
        self.assertTrue(any(i.reason == "too_few_columns" for i in report.issues))

    def test_blank_row_dropped_always(self):
        path = self.write("blank.csv", b"a,b\n1,2\n,\n3,4\n")
        out = str(self.root / "out.csv")
        report = clean_csv(path, out)
        self.assertEqual(report.rows_dropped, 1)
        self.assertTrue(any(i.reason == "blank_row" for i in report.issues))


class TestEncodingHandling(TempFileTestCase):
    def test_invalid_utf8_bytes_do_not_crash(self):
        content = b"name,note\nAda,\xff\xfe broken\nGrace,fine\n"
        path = self.write("bad_encoding.csv", content)
        out = str(self.root / "out.csv")
        report = clean_csv(path, out)
        self.assertEqual(report.encoding_errors, 1)
        self.assertEqual(report.rows_written, 2)

    def test_bom_stripped_from_first_header_cell(self):
        content = "\ufeffname,age\nAda,36\n".encode("utf-8")
        path = self.write("bom.csv", content)
        out = str(self.root / "out.csv")
        clean_csv(path, out)
        rows = self.read_output_rows(out)
        self.assertEqual(rows[0][0], "name")  # not "\ufeffname"


class TestHeaderDetection(TempFileTestCase):
    def test_header_row_detected_when_non_numeric(self):
        path = self.write("with_header.csv", b"name,age\nAda,36\n")
        out = str(self.root / "out.csv")
        report = clean_csv(path, out)
        self.assertTrue(report.had_header)
        rows = self.read_output_rows(out)
        self.assertEqual(rows[0], ["name", "age"])

    def test_no_header_synthesizes_column_names(self):
        path = self.write("no_header.csv", b"Ada,36\nGrace,41\n")
        out = str(self.root / "out.csv")
        report = clean_csv(path, out)
        self.assertFalse(report.had_header)
        rows = self.read_output_rows(out)
        self.assertEqual(rows[0], ["column_1", "column_2"])
        # Both data rows preserved, including the one that would have
        # been mistaken for a header.
        self.assertEqual(report.rows_written, 2)

    def test_duplicate_header_names_deduplicated(self):
        path = self.write("dup_header.csv", b"name,name,age\nAda,Smith,36\n")
        out = str(self.root / "out.csv")
        clean_csv(path, out)
        rows = self.read_output_rows(out)
        self.assertEqual(rows[0], ["name", "name_1", "age"])


class TestDelimiterDetectionIntegration(TempFileTestCase):
    def test_semicolon_delimited_file_cleaned_correctly(self):
        path = self.write("semi.csv", b"name;age\nAda;36\n")
        out = str(self.root / "out.csv")
        report = clean_csv(path, out)
        self.assertEqual(report.delimiter, ";")
        rows = self.read_output_rows(out)
        self.assertEqual(rows[1], ["Ada", "36"])

    def test_forced_delimiter_overrides_detection(self):
        path = self.write("pipe.csv", b"name|age\nAda|36\n")
        out = str(self.root / "out.csv")
        report = clean_csv(path, out, delimiter="|")
        self.assertEqual(report.delimiter, "|")


class TestNullNormalizationIntegration(TempFileTestCase):
    def test_null_tokens_normalized_to_empty_in_output(self):
        path = self.write("nulls.csv", b"name,age\nAda,NULL\nGrace,N/A\n")
        out = str(self.root / "out.csv")
        clean_csv(path, out)
        rows = self.read_output_rows(out)
        self.assertEqual(rows[1], ["Ada", ""])
        self.assertEqual(rows[2], ["Grace", ""])


if __name__ == "__main__":
    unittest.main()
