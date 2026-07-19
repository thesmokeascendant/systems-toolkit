#!/usr/bin/env python3
"""
generate_fixtures.py — regenerates fixtures/*.pdf from scratch.

Run this if the fixtures directory is missing or you want to regenerate
it deterministically:

    python3 generate_fixtures.py
"""

from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Table,
    TableStyle,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def main() -> None:
    FIXTURES_DIR.mkdir(exist_ok=True)
    styles = getSampleStyleSheet()

    # 1. Normal two-page text PDF.
    doc = SimpleDocTemplate(str(FIXTURES_DIR / "normal_text.pdf"), pagesize=letter)
    doc.build(
        [
            Paragraph("Engineering Report", styles["Title"]),
            Paragraph(
                "This is page one of a normal, well-formed PDF document "
                "used to test text extraction.",
                styles["Normal"],
            ),
            PageBreak(),
            Paragraph("This is page two.", styles["Normal"]),
        ]
    )

    # 2. PDF containing a table.
    doc2 = SimpleDocTemplate(str(FIXTURES_DIR / "with_table.pdf"), pagesize=letter)
    data = [["Name", "Role", "Years"], ["Ada", "Engineer", "10"], ["Grace", "Engineer", "12"]]
    table = Table(data)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    doc2.build([Paragraph("Team Table", styles["Title"]), table])

    # 3. Image/shape-only PDF with no extractable text ("scanned" stand-in).
    c = canvas.Canvas(str(FIXTURES_DIR / "no_text.pdf"), pagesize=letter)
    c.rect(100, 500, 200, 100, fill=1)
    c.showPage()
    c.save()

    # 4. Encrypted version of the normal text PDF.
    reader = PdfReader(str(FIXTURES_DIR / "normal_text.pdf"))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password="secret123")
    with open(FIXTURES_DIR / "encrypted.pdf", "wb") as f:
        writer.write(f)

    # 5. Corrupted PDF — a valid file truncated halfway through.
    original = (FIXTURES_DIR / "normal_text.pdf").read_bytes()
    (FIXTURES_DIR / "corrupted.pdf").write_bytes(original[: len(original) // 2])

    # 6. Not a PDF at all, despite the extension.
    (FIXTURES_DIR / "not_a_pdf.pdf").write_bytes(
        b"This is just a plain text file pretending to be a PDF.\n"
    )

    # 7. Zero-byte file.
    (FIXTURES_DIR / "empty.pdf").touch()

    print(f"Fixtures written to {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
