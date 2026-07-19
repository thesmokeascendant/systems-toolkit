# Engineering Journal — PDF Text Extraction Tool

## Problem

Needed a PDF extractor where "the file is encrypted," "the file is
corrupted," and "the file is a scanned image" are three clearly
distinguishable, non-crashing outcomes — not three different unhandled
tracebacks from three different internal library layers.

## Initial Idea

First draft caught a single broad `except Exception` around the
`pdfplumber.open()` call and tried to classify the failure by checking
`"password" in str(exception).lower()`.

## Why It Failed

Ran the CLI by hand against the encrypted fixture (both with no password
and with a wrong password) before writing any tests — this is the smoke
test that caught it early. Both cases produced:
`error: could not parse fixtures/encrypted.pdf:` — completely empty
after the colon. `pdfplumber` wraps the real `pdfminer` exception
(`PDFPasswordIncorrect`) in its own `PdfminerException`, and that
wrapper's `str()` returns an empty string. The actual exception type is
only reachable via `exc.args[0]`. String-matching against `str(exception)`
was checking a string that was always empty for exactly the case it was
supposed to catch.

## Alternative Approaches Considered

1. **Import `pdfminer.pdfdocument.PDFPasswordIncorrect` directly and catch
   it.** Tried this first as the "obvious" fix — but `pdfplumber` had
   already wrapped it by the time it reached calling code, so `except
   PDFPasswordIncorrect` never matched; only the wrapper `PdfminerException`
   was visible at the catch site.
2. **Catch `PdfminerException` and inspect `repr(exc)` for the substring
   "PDFPasswordIncorrect".** Works, but string-matching a repr is fragile
   — any change to how the wrapped exception reprs itself breaks
   detection silently.
3. **Catch `PdfminerException`, inspect `type(exc.args[0]).__name__`.**
   Chosen. Doesn't require importing `pdfminer`'s internal exception
   class (which would couple this code to `pdfminer`'s module layout, a
   dependency of a dependency), and checks the actual wrapped exception's
   type rather than a formatted string of it.

## Benchmarking / Performance Observations

Not benchmarked for very large PDFs. `pdfplumber`'s page iterator does
give per-page extraction without needing the whole document's text
resident at once, which is the main performance property this project
cares about — true streaming benchmarks would belong with the
duplicate-finder project's scale-focused work instead.

## Refactoring Decisions

Pulled the password-detection logic into its own `_is_password_error()`
helper, tested implicitly through both `extract_text` and `extract_tables`
exercising the same encrypted fixture — rather than duplicating the
`args[0]` inspection inline in both functions' `except` blocks, which is
exactly the kind of duplication that would silently drift if one copy
got fixed and the other didn't.

## Final Implementation

Byte-level `%PDF-` header check happens before any library call, so a
non-PDF file never reaches `pdfplumber` at all. `pdfplumber` handles
text/table extraction; `pypdf` handles metadata and its own encryption
check (`reader.is_encrypted` / `reader.decrypt()`), since its API for
that specific job is simpler than `pdfplumber`'s. Password-error detection
uses wrapped-exception-type inspection, confirmed against real fixtures
generated for exactly this case.

## Engineering Tradeoffs

Using two PDF libraries (`pypdf` + `pdfplumber`) instead of standardizing
on one adds a small amount of conceptual surface area, but each library's
strong suit (pypdf: encryption/metadata; pdfplumber: text/table layout
extraction) meant a single-library implementation would have fought
against one library's weaker area for one of the two features.

## Possible Future Versions

- OCR fallback (`pytesseract` + `pdf2image`) for pages flagged
  `likely_scanned`, opt-in via a CLI flag.
- Configurable `pdfplumber` table-detection settings for borderless
  tables.
