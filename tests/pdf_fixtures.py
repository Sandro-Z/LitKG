"""Small text PDFs with known physical page positions, without external assets."""

from contextlib import contextmanager
from pathlib import Path
import tempfile

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


@contextmanager
def text_pdf(pages, metadata=None):
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 700 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = stream
    if metadata:
        writer.add_metadata(metadata)
    with tempfile.TemporaryDirectory(prefix="litkg-page-test-") as directory:
        path = Path(directory) / "paper.pdf"
        writer.write(path)
        yield path

