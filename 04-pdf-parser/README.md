# 04 — PDF Text Extraction Tool

A defensive PDF text, table, and metadata extractor built on `pypdf` and
`pdfplumber`. Treats password-protected files, scanned images with no
text layer, truncated downloads, and files that aren't actually PDFs as
expected input, not exceptional failure.

## Problem

PDFs collected from the real world (user uploads, scraped downloads,
email attachments) are frequently not clean, well-formed documents:
some are encrypted, some are scanned images with no extractable text at
all, some are truncated by an interrupted download, and some aren't PDFs
despite the file extension. A parser that assumes clean input will crash
on all of these instead of reporting them clearly.

## Requirements

- Extract text page by page from well-formed PDFs.
- Detect and clearly report: missing files, empty files, non-PDF files,
  corrupted/truncated PDFs, and encrypted PDFs (with or without a
  password supplied).
- Flag pages with no extractable text (typical of scanned images) instead
  of silently returning empty strings indistinguishable from a truly
  blank page.
- Extract tables without crashing on documents that have none.
- Extract metadata (title, author, page count) even when fields are
  missing.
- Never let an internal `pdfminer`/`pypdf` exception type leak out as an
  unhandled exception — wrap everything in a small, purpose-built
  exception hierarchy.

## Architecture

```
pdf_parser.py
├── _validate_file()        # existence, non-empty, real %PDF- header
├── _is_password_error()    # reliably detects wrapped password failures
├── extract_text()          # page-by-page text, flags likely-scanned pages
├── extract_tables()        # all tables across all pages
├── get_metadata()          # title/author/subject/creator/page_count
└── Exception hierarchy     # PDFParserError → {NotAPDF, Corrupted,
                             #                  Encrypted, EmptyFile}
```

Validation happens before any PDF-library call: a missing file, an empty
file, or a file without a `%PDF-` header is rejected immediately with a
clear, specific error rather than being handed to `pdfplumber` and
producing a confusing internal traceback.

## Design Decisions

- **A `%PDF-` magic-byte check before anything else.** A file with a
  `.pdf` extension that isn't actually a PDF (plain text, an HTML error
  page saved with the wrong extension, etc.) should fail immediately and
  specifically, not three layers deep inside `pdfminer`'s parser.
- **Password-incorrect detection required inspecting the wrapped
  exception's `args`, not its string form.** `pdfplumber` wraps the
  underlying `pdfminer.PDFPasswordIncorrect` in its own
  `PdfminerException`, whose `str()` is empty — see Lessons Learned for
  how this was actually discovered.
- **A page with no extractable text is flagged, not silently empty.** A
  scanned page and a genuinely blank page both produce `""` from
  `extract_text()`. Without a `likely_scanned` flag, a caller can't tell
  "this page needs OCR" from "this page really is blank" — that
  distinction matters enough to be a first-class field on the result.
- **Out-of-range page numbers are silently skipped, not an error.** A
  caller asking for pages `[1, 2, 3, 100]` of a 3-page document almost
  certainly wants pages 1-3, not an exception halting the whole request.

## Algorithms Used

- Direct byte-header inspection for format validation (no external magic
  library needed — the PDF spec's own 5-byte signature is sufficient).
- Page-by-page streaming extraction via `pdfplumber`'s page iterator
  (doesn't require loading the whole document's text into memory before
  the first page is available to the caller).

## Tradeoffs

- Uses `pdfplumber` (built on `pdfminer.six`) for text/table extraction
  rather than the faster but layout-blind `pypdf` text extraction, and
  uses `pypdf` specifically for metadata/encryption handling where its
  API is simpler. Two libraries instead of one adds a bit of surface
  area, but each is used for what it's actually better at.
- No OCR integration. Scanned pages are *flagged*, not automatically
  routed through `pytesseract` — OCR is a meaningfully different
  operation (accuracy tradeoffs, much slower, needs image conversion)
  that a text-extraction tool shouldn't silently trigger without the
  caller opting in.

## Edge Cases Handled

| Case | Behavior |
|---|---|
| File doesn't exist | `FileNotFoundError`, clear message |
| File exists but is 0 bytes | `EmptyFileError` |
| File lacks `%PDF-` header | `NotAPDFError` |
| Truncated/corrupted PDF | `CorruptedPDFError`, not a raw parser traceback |
| Encrypted, no password given | `EncryptedPDFError` |
| Encrypted, wrong password given | `EncryptedPDFError` |
| Encrypted, correct password given | Extraction proceeds normally |
| Page with no text layer (scanned) | Flagged `likely_scanned=True`, not an error |
| Requested page numbers out of range | Silently skipped, valid pages still returned |
| Document with no tables | Empty list, not an error |
| Missing metadata fields | `None` for that field, not a crash |

## Examples

```
$ ./pdf_parser.py report.pdf --text
--- page 1 ---
Engineering Report
This is page one...
--- page 2 ---
This is page two.

$ ./pdf_parser.py scanned.pdf --text
--- page 1 (no extractable text — likely scanned) ---
note: 1 page(s) had no text layer (pages [1]) — consider OCR

$ ./pdf_parser.py locked.pdf --text
error: password required or incorrect for locked.pdf

$ ./pdf_parser.py locked.pdf --text --password secret123
--- page 1 ---
...
```

## Limitations

- No OCR — scanned/image-only pages are flagged but not transcribed.
- No support for extracting embedded images (out of scope for a text
  extractor; the broader `pdf` skill covers `pdfimages`/`pdf2image` for
  that).
- Table extraction relies on `pdfplumber`'s line-detection heuristics,
  which can miss tables with no visible borders (borderless tables laid
  out purely with whitespace).

## Lessons Learned

The encrypted-PDF error path initially produced a useless message —
`error: could not parse fixtures/encrypted.pdf:` with nothing after the
colon. The cause: `pdfplumber` wraps `pdfminer`'s `PDFPasswordIncorrect`
inside its own `PdfminerException`, and that wrapper's `str()` is empty;
the useful information (the *type* of the wrapped exception) is only in
`exc.args[0]`. Catching the wrapper by name and checking
`type(exc.args[0]).__name__ == "PDFPasswordIncorrect"` — rather than
trying to catch `pdfminer`'s exception class directly, or pattern-matching
on an error string that turned out to be empty — was what actually fixed
it. Confirmed the fix against real password-required and wrong-password
fixtures, not just the happy path, since this exact failure mode is
invisible unless you test it directly.

## Future Improvements

- Optional OCR fallback for flagged scanned pages, gated behind an
  explicit `--ocr` flag rather than automatic.
- Borderless-table detection tuning (pdfplumber's `table_settings`).
- Streaming table extraction for very large documents instead of
  collecting all tables into memory at once.

## References

- `pypdf` documentation
- `pdfplumber` documentation
- PDF 1.7 specification (ISO 32000-1), §7.5.2 — file header
