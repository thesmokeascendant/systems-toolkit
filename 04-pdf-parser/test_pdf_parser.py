"""
Unit tests for pdf_parser.py

Runs against generated fixture PDFs (fixtures/*.pdf) covering normal text,
tables, no-text ("scanned"), encrypted, corrupted, non-PDF, and empty
files. Fixtures are pre-generated (see fixtures/README.md for how) so
tests don't depend on reportlab at test-run time.

Run with:
    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdf_parser import (  # noqa: E402
    CorruptedPDFError,
    EmptyFileError,
    EncryptedPDFError,
    NotAPDFError,
    extract_tables,
    extract_text,
    get_metadata,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fx(name: str) -> str:
    return str(FIXTURES / name)


class TestExtractText(unittest.TestCase):
    def test_normal_pdf_extracts_all_pages(self):
        result = extract_text(fx("normal_text.pdf"))
        self.assertEqual(result.page_count, 2)
        self.assertEqual(len(result.pages), 2)
        self.assertIn("Engineering Report", result.pages[0].text)
        self.assertIn("page two", result.pages[1].text)

    def test_full_text_property_joins_pages(self):
        result = extract_text(fx("normal_text.pdf"))
        self.assertIn("Engineering Report", result.full_text)
        self.assertIn("page two", result.full_text)

    def test_page_filter_returns_only_requested_pages(self):
        result = extract_text(fx("normal_text.pdf"), pages=[2])
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].page_number, 2)

    def test_out_of_range_page_filter_returns_empty_not_error(self):
        result = extract_text(fx("normal_text.pdf"), pages=[99])
        self.assertEqual(len(result.pages), 0)
        self.assertEqual(result.page_count, 2)  # metadata still correct

    def test_no_text_pdf_flagged_as_likely_scanned(self):
        result = extract_text(fx("no_text.pdf"))
        self.assertEqual(len(result.scanned_page_numbers), 1)
        self.assertTrue(result.pages[0].likely_scanned)
        self.assertEqual(result.pages[0].text, "")

    def test_encrypted_without_password_raises_encrypted_error(self):
        with self.assertRaises(EncryptedPDFError):
            extract_text(fx("encrypted.pdf"))

    def test_encrypted_with_wrong_password_raises_encrypted_error(self):
        with self.assertRaises(EncryptedPDFError):
            extract_text(fx("encrypted.pdf"), password="wrong-password")

    def test_encrypted_with_correct_password_succeeds(self):
        result = extract_text(fx("encrypted.pdf"), password="secret123")
        self.assertIn("Engineering Report", result.full_text)

    def test_corrupted_pdf_raises_corrupted_error_not_crash(self):
        with self.assertRaises(CorruptedPDFError):
            extract_text(fx("corrupted.pdf"))

    def test_non_pdf_file_raises_not_a_pdf_error(self):
        with self.assertRaises(NotAPDFError):
            extract_text(fx("not_a_pdf.pdf"))

    def test_empty_file_raises_empty_file_error(self):
        with self.assertRaises(EmptyFileError):
            extract_text(fx("empty.pdf"))

    def test_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            extract_text(fx("does_not_exist.pdf"))


class TestExtractTables(unittest.TestCase):
    def test_table_extracted_as_rows(self):
        tables = extract_tables(fx("with_table.pdf"))
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0][0], ["Name", "Role", "Years"])
        self.assertEqual(tables[0][1], ["Ada", "Engineer", "10"])

    def test_pdf_with_no_tables_returns_empty_list(self):
        tables = extract_tables(fx("normal_text.pdf"))
        self.assertEqual(tables, [])

    def test_corrupted_pdf_raises_not_crash(self):
        with self.assertRaises(CorruptedPDFError):
            extract_tables(fx("corrupted.pdf"))


class TestGetMetadata(unittest.TestCase):
    def test_metadata_includes_page_count(self):
        meta = get_metadata(fx("normal_text.pdf"))
        self.assertEqual(meta["page_count"], 2)

    def test_encrypted_metadata_without_password_raises(self):
        with self.assertRaises(EncryptedPDFError):
            get_metadata(fx("encrypted.pdf"))

    def test_encrypted_metadata_with_correct_password_succeeds(self):
        meta = get_metadata(fx("encrypted.pdf"), password="secret123")
        self.assertEqual(meta["page_count"], 2)

    def test_corrupted_pdf_metadata_raises_corrupted_error(self):
        with self.assertRaises((CorruptedPDFError, NotAPDFError)):
            get_metadata(fx("corrupted.pdf"))


if __name__ == "__main__":
    unittest.main()
