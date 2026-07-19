#!/usr/bin/env python3
"""
pdf_parser.py — defensive PDF text, table, and metadata extraction.

Real-world PDFs are frequently not what they claim to be: truncated
downloads, password-protected files, scanned images with no text layer
at all, or plain files with a .pdf extension slapped on. This module
treats all of that as expected input, not exceptional failure.

Usage:
    from pdf_parser import extract_text, extract_tables, get_metadata

    pages = extract_text("report.pdf")
    tables = extract_tables("report.pdf")

CLI:
    ./pdf_parser.py report.pdf --text
    ./pdf_parser.py report.pdf --tables
    ./pdf_parser.py report.pdf --metadata
    ./pdf_parser.py encrypted.pdf --text --password secret123
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError


class PDFParserError(Exception):
    """Base class for all pdf_parser-raised errors."""


class NotAPDFError(PDFParserError):
    """The file doesn't have a valid PDF header — it isn't a PDF at all."""


class CorruptedPDFError(PDFParserError):
    """The file starts like a PDF but couldn't be fully parsed."""


class EncryptedPDFError(PDFParserError):
    """The PDF requires a password that wasn't supplied, or it was wrong."""


class EmptyFileError(PDFParserError):
    """The file exists but has zero bytes."""


PDF_MAGIC = b"%PDF-"


@dataclass
class PageText:
    page_number: int  # 1-indexed
    text: str
    likely_scanned: bool = False  # no extractable text layer


@dataclass
class ExtractionResult:
    source: str
    page_count: int
    pages: list[PageText] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)

    @property
    def scanned_page_numbers(self) -> list[int]:
        return [p.page_number for p in self.pages if p.likely_scanned]


def _is_password_error(exc: Exception) -> bool:
    """
    pdfplumber wraps the underlying pdfminer exception in its own
    PdfminerException, and that wrapper's str() is empty — the useful
    information is the wrapped exception's type, in exc.args[0]. Checking
    the type name (rather than importing pdfminer's internal exception
    class directly) keeps this resilient to pdfminer being vendored or
    its exception module path changing between versions.
    """
    if exc.args and type(exc.args[0]).__name__ == "PDFPasswordIncorrect":
        return True
    return "password" in str(exc).lower()


def _validate_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"not a regular file: {path}")
    if path.stat().st_size == 0:
        raise EmptyFileError(f"file is empty: {path}")
    with path.open("rb") as f:
        header = f.read(len(PDF_MAGIC))
    if header != PDF_MAGIC:
        raise NotAPDFError(f"not a PDF file (missing %PDF- header): {path}")


def extract_text(
    path: str, password: Optional[str] = None, pages: Optional[list[int]] = None
) -> ExtractionResult:
    """
    Extract text page by page. A page with no extractable text (typical
    of a scanned image with no OCR layer) is not an error — it's flagged
    via `likely_scanned` so the caller can decide whether to route it to
    an OCR pipeline instead of treating it as a parsing failure.

    `pages`, if given, is a list of 1-indexed page numbers to extract;
    out-of-range page numbers are silently skipped rather than raising,
    since a caller requesting pages 1-100 of a 5-page document is asking
    a reasonable question with a partial answer, not an error.
    """
    file_path = Path(path)
    _validate_file(file_path)

    try:
        with pdfplumber.open(file_path, password=password) as pdf:
            results: list[PageText] = []
            for i, page in enumerate(pdf.pages, start=1):
                if pages is not None and i not in pages:
                    continue
                text = page.extract_text() or ""
                results.append(
                    PageText(page_number=i, text=text.strip(), likely_scanned=not text.strip())
                )
            return ExtractionResult(source=str(file_path), page_count=len(pdf.pages), pages=results)
    except PdfminerException as e:
        if _is_password_error(e):
            raise EncryptedPDFError(f"password required or incorrect for {file_path}") from e
        raise CorruptedPDFError(f"could not parse {file_path}: {e}") from e
    except Exception as e:
        # Any other pdfminer/pdfplumber internal exception type for
        # malformed PDF structure — surface uniformly rather than letting
        # an internal parser exception type leak into caller code.
        if _is_password_error(e):
            raise EncryptedPDFError(f"could not open {file_path}: {e}") from e
        raise CorruptedPDFError(f"could not parse {file_path}: {e}") from e


def extract_tables(path: str, password: Optional[str] = None) -> list[list[list[Optional[str]]]]:
    """
    Extract all tables from all pages. Returns a list of tables, each a
    list of rows, each row a list of cell values (None for empty cells).
    A document with no tables returns an empty list, not an error.
    """
    file_path = Path(path)
    _validate_file(file_path)

    try:
        with pdfplumber.open(file_path, password=password) as pdf:
            all_tables = []
            for page in pdf.pages:
                for table in page.extract_tables():
                    all_tables.append(table)
            return all_tables
    except PdfminerException as e:
        if _is_password_error(e):
            raise EncryptedPDFError(f"password required or incorrect for {file_path}") from e
        raise CorruptedPDFError(f"could not parse {file_path}: {e}") from e
    except Exception as e:
        if _is_password_error(e):
            raise EncryptedPDFError(f"could not open {file_path}: {e}") from e
        raise CorruptedPDFError(f"could not parse {file_path}: {e}") from e


def get_metadata(path: str, password: Optional[str] = None) -> dict:
    """
    Extract document metadata. Missing individual fields (title, author,
    etc.) are returned as None rather than raising — most real-world PDFs
    have incomplete metadata.
    """
    file_path = Path(path)
    _validate_file(file_path)

    try:
        reader = PdfReader(file_path)
        if reader.is_encrypted:
            if password is None:
                raise EncryptedPDFError(f"{file_path} is encrypted; password required")
            if reader.decrypt(password) == 0:
                raise EncryptedPDFError(f"incorrect password for {file_path}")
        meta = reader.metadata or {}
        return {
            "title": getattr(meta, "title", None),
            "author": getattr(meta, "author", None),
            "subject": getattr(meta, "subject", None),
            "creator": getattr(meta, "creator", None),
            "page_count": len(reader.pages),
        }
    except (FileNotDecryptedError,):
        raise EncryptedPDFError(f"{file_path} is encrypted; password required or incorrect")
    except PdfReadError as e:
        raise CorruptedPDFError(f"could not read {file_path}: {e}") from e


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Defensive PDF text/table/metadata extractor")
    parser.add_argument("pdf_path")
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--tables", action="store_true")
    parser.add_argument("--metadata", action="store_true")
    parser.add_argument("--password", help="Password for encrypted PDFs")
    parser.add_argument("--pages", help="Comma-separated 1-indexed page numbers, e.g. 1,3,5")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    pages = [int(p) for p in args.pages.split(",")] if args.pages else None

    try:
        if args.tables:
            tables = extract_tables(args.pdf_path, password=args.password)
            print(json.dumps(tables, indent=2))
        elif args.metadata:
            meta = get_metadata(args.pdf_path, password=args.password)
            print(json.dumps(meta, indent=2))
        else:
            result = extract_text(args.pdf_path, password=args.password, pages=pages)
            for page in result.pages:
                if page.likely_scanned:
                    print(f"--- page {page.page_number} (no extractable text — likely scanned) ---")
                else:
                    print(f"--- page {page.page_number} ---")
                    print(page.text)
            if result.scanned_page_numbers:
                print(
                    f"\nnote: {len(result.scanned_page_numbers)} page(s) had no text layer "
                    f"(pages {result.scanned_page_numbers}) — consider OCR",
                    file=sys.stderr,
                )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except EmptyFileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except NotAPDFError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except EncryptedPDFError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except CorruptedPDFError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
