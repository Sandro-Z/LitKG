import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from app.ingestion import UploadRejected, safe_filename, store_pdf


class IngestionTests(unittest.TestCase):
    def test_safe_filename_removes_client_paths(self):
        self.assertEqual(
            safe_filename(r"C:\fakepath\paper.pdf"),
            "paper.pdf",
        )
        self.assertEqual(safe_filename("../../paper.pdf"), "paper.pdf")

    def test_store_pdf_is_content_addressed_and_deduplicated(self):
        content = b"%PDF-1.7\nminimal test data"
        with tempfile.TemporaryDirectory() as directory:
            upload_dir = Path(directory)
            first = store_pdf(
                BytesIO(content),
                "first.pdf",
                upload_dir,
                max_upload_bytes=1024,
            )
            second = store_pdf(
                BytesIO(content),
                "second.pdf",
                upload_dir,
                max_upload_bytes=1024,
            )

            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.source_path, second.source_path)
            self.assertEqual(len(list(upload_dir.glob("*.pdf"))), 1)

    def test_store_pdf_rejects_invalid_inputs(self):
        cases = [
            ("notes.txt", b"%PDF-1.7"),
            ("fake.pdf", b"not a PDF"),
            ("empty.pdf", b""),
        ]
        for filename, content in cases:
            with self.subTest(filename=filename):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(UploadRejected):
                        store_pdf(
                            BytesIO(content),
                            filename,
                            Path(directory),
                            max_upload_bytes=1024,
                        )

    def test_store_pdf_enforces_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UploadRejected):
                store_pdf(
                    BytesIO(b"%PDF-" + b"x" * 32),
                    "large.pdf",
                    Path(directory),
                    max_upload_bytes=10,
                )


if __name__ == "__main__":
    unittest.main()
