import unittest
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pdf_fixtures import text_pdf

from app.parser import (
    fallback_pdf_text,
    call_grobid,
    normalize_doi,
    parse_pdf_to_chunks,
    parse_tei_sections,
    split_into_chunks,
)
from app.provenance import evidence_location, locate_evidence_pages


TEI = '''<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>Aspirin study</title></titleStmt>
      <sourceDesc><biblStruct><idno type="DOI">https://doi.org/10.1234/study</idno></biblStruct></sourceDesc>
    </fileDesc>
    <profileDesc><abstract><p coords="1,0,0,100,20">Study abstract.</p></abstract></profileDesc>
  </teiHeader>
  <text>
    <body><div><head>Results</head>
      <p coords="2,0,0,100,20">Aspirin inhibited COX-1.</p>
      <p coords="3,0,0,100,20;4,0,0,100,20">EGFR binds gefitinib.</p>
    </div></body>
    <back><listBibl><biblStruct><idno type="DOI">10.5678/reference</idno></biblStruct></listBibl></back>
  </text>
</TEI>'''


class ParserTests(unittest.TestCase):
    def test_grobid_requests_sentence_segmentation_and_both_coordinate_types(self):
        response = MagicMock(text=TEI)
        with text_pdf(["Study abstract."]) as pdf, patch(
            "app.parser.requests.post", return_value=response,
        ) as post:
            call_grobid(str(pdf))
        fields = post.call_args.kwargs["data"]
        self.assertEqual(fields["segmentSentences"], "1")
        self.assertEqual(fields["teiCoordinates"], ["p", "s"])

    def test_sentence_coordinates_override_cross_page_paragraph_coordinates(self):
        xml = '''<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div>
          <p coords="2,0,0,100,20;3,0,0,100,20"><s coords="2,0,0,100,20">Aspirin inhibited COX-1.</s><s coords="3,0,0,100,20">EGFR binds gefitinib.</s></p>
        </div></body></text></TEI>'''
        _, sections = parse_tei_sections(xml)
        chunk = split_into_chunks(sections)[0]
        self.assertEqual(chunk["text"], "Aspirin inhibited COX-1. EGFR binds gefitinib.")
        self.assertEqual(locate_evidence_pages("Aspirin inhibited COX-1.", chunk), (2, 2))
        self.assertEqual(locate_evidence_pages("EGFR binds gefitinib.", chunk), (3, 3))

    def test_sentence_only_coordinates_and_inline_markup_preserve_text(self):
        xml = '''<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div>
          <p><s coords="5,0,0,100,20">IL-<hi>6</hi> activates STAT3<ref>[1]</ref>.</s></p>
        </div></body></text></TEI>'''
        _, sections = parse_tei_sections(xml)
        chunk = split_into_chunks(sections)[0]
        self.assertEqual(chunk["text"], "IL-6 activates STAT3[1].")
        self.assertEqual(locate_evidence_pages(chunk["text"], chunk), (5, 5))

    def test_real_pdf_supplements_missing_coordinates_without_losing_grobid_doi(self):
        xml = re.sub(r' coords="[^"]*"', '', TEI)
        with text_pdf(["Study abstract.", "Aspirin inhibited COX-1.", "EGFR binds gefitinib."],
                      {"/Title": "PDF fallback title"}) as pdf, patch(
            "app.parser.call_grobid", return_value=xml,
        ):
            metadata, chunks = parse_pdf_to_chunks(str(pdf))
        self.assertEqual(metadata, {"title": "Aspirin study", "doi": "10.1234/study"})
        self.assertEqual(chunks[0]["page_start"], 1)
        results = chunks[1]
        self.assertEqual((results["page_start"], results["page_end"]), (2, 3))
        self.assertEqual(locate_evidence_pages("Aspirin inhibited COX-1.", results), (2, 2))
        self.assertEqual(locate_evidence_pages("EGFR binds gefitinib.", results), (3, 3))
        self.assertEqual(results["section_title"], "Results")

    def test_pdf_text_failure_preserves_grobid_text_doi_and_known_pages(self):
        xml = TEI.replace(' coords="1,0,0,100,20"', '')
        with patch("app.parser.call_grobid", return_value=xml), patch(
            "app.parser.fallback_pdf_text", side_effect=RuntimeError("no text layer"),
        ), self.assertLogs("app.parser", level="WARNING"):
            metadata, chunks = parse_pdf_to_chunks("unused.pdf")
        self.assertEqual(metadata["doi"], "10.1234/study")
        self.assertIsNone(chunks[0]["page_start"])
        self.assertEqual(locate_evidence_pages("Aspirin inhibited COX-1.", chunks[1]), (2, 2))

    def test_header_metadata_and_short_sections_keep_page_coordinates(self):
        metadata, sections = parse_tei_sections(TEI)
        self.assertEqual(metadata, {"title": "Aspirin study", "doi": "10.1234/study"})
        chunks = split_into_chunks(sections)
        self.assertEqual(chunks[0]["page_start"], 1)
        results = chunks[1]
        self.assertEqual((results["page_start"], results["page_end"]), (2, 4))
        self.assertEqual(locate_evidence_pages("Aspirin inhibited COX-1.", results), (2, 2))
        self.assertEqual(locate_evidence_pages("EGFR binds gefitinib.", results), (3, 4))

    def test_reference_doi_is_not_used_as_document_doi(self):
        xml = TEI.replace('<idno type="DOI">https://doi.org/10.1234/study</idno>', '')
        metadata, _ = parse_tei_sections(xml)
        self.assertIsNone(metadata["doi"])

    def test_doi_normalizes_prefixes_and_rejects_invalid_values(self):
        for value in ["doi: 10.1234/study", "https://dx.doi.org/10.1234/study", "10.1234/study."]:
            self.assertEqual(normalize_doi(value), "10.1234/study")
        for value in [None, "", "invalid", "10.12/no", "10.1234/has spaces"]:
            self.assertIsNone(normalize_doi(value))

    def test_overlap_and_trim_preserve_offsets(self):
        text = "  abcdefghijklmnopqrst  "
        sections = [{
            "section_title": "Results",
            "text": text,
            "page_spans": [
                {"start": 2, "end": 12, "page_start": 2, "page_end": 2},
                {"start": 12, "end": 22, "page_start": 3, "page_end": 3},
            ],
        }]
        chunks = split_into_chunks(sections, max_chars=12, overlap_chars=4)
        self.assertEqual(chunks[0]["text"], "abcdefghij")
        self.assertEqual(locate_evidence_pages("mnopqr", chunks[1]), (3, 3))
        for chunk in chunks:
            for span in chunk["page_spans"]:
                self.assertGreaterEqual(span["start"], 0)
                self.assertLessEqual(span["end"], len(chunk["text"]))

    def test_cross_paragraph_sentence_uses_page_range(self):
        chunk = {
            "text": "Aspirin inhibits\nCOX-1.",
            "page_start": 2,
            "page_end": 3,
            "page_spans": [
                {"start": 0, "end": 16, "page_start": 2, "page_end": 2},
                {"start": 17, "end": 23, "page_start": 3, "page_end": 3},
            ],
        }
        self.assertEqual(locate_evidence_pages("Aspirin inhibits COX-1.", chunk), (2, 3))

    def test_missing_coordinates_do_not_invent_pages(self):
        xml = TEI.replace(' coords="2,0,0,100,20"', '')
        _, sections = parse_tei_sections(xml)
        chunk = split_into_chunks(sections)[1]
        self.assertIsNone(chunk["page_start"])
        self.assertEqual(locate_evidence_pages("Aspirin inhibited COX-1.", chunk), (None, None))
        self.assertEqual(locate_evidence_pages("EGFR binds gefitinib.", chunk), (3, 4))

    def test_pdf_fallback_keeps_metadata_and_actual_page_numbers(self):
        reader = SimpleNamespace(
            metadata={"/Title": "Fallback study"},
            pages=[
                SimpleNamespace(extract_text=lambda: ""),
                SimpleNamespace(extract_text=lambda: "Aspirin inhibits COX-1."),
            ],
        )
        with patch("app.parser.PdfReader", return_value=reader), patch(
            "app.parser.call_grobid", side_effect=RuntimeError("offline")
        ):
            metadata, chunks = parse_pdf_to_chunks("unused.pdf")
        self.assertEqual(metadata["title"], "Fallback study")
        self.assertIsNone(metadata["doi"])
        self.assertEqual(chunks[0]["page_start"], 2)

    def test_pdf_fallback_accepts_explicit_first_page_doi(self):
        reader = SimpleNamespace(
            metadata={},
            pages=[SimpleNamespace(extract_text=lambda: "doi: 10.1234/paper. Results")],
        )
        with patch("app.parser.PdfReader", return_value=reader):
            metadata, _ = fallback_pdf_text("unused.pdf")
        self.assertEqual(metadata["doi"], "10.1234/paper")

    def test_empty_grobid_body_uses_pdf_fallback(self):
        with patch("app.parser.call_grobid", return_value='<TEI xmlns="http://www.tei-c.org/ns/1.0"/>'), patch(
            "app.parser.fallback_pdf_text",
            return_value=({"title": None, "doi": None}, [{"section_title": "Page 1", "text": "Fallback"}]),
        ) as fallback:
            _, chunks = parse_pdf_to_chunks("unused.pdf")
        fallback.assert_called_once()
        self.assertEqual(chunks[0]["text"], "Fallback")

    def test_legacy_evidence_page_and_chunk_range_are_distinguished(self):
        row = {
            "chunk_page_start": 2,
            "chunk_page_end": 5,
            "section_title": "Results",
            "claim_json": {"evidence": {"page": 3}},
        }
        location = evidence_location(row)
        self.assertEqual((location["page"], location["page_end"]), (3, 3))
        row["claim_json"] = {}
        location = evidence_location(row)
        self.assertEqual((location["page"], location["page_end"]), (2, 5))
        self.assertIsNone(location["doi"])


if __name__ == "__main__":
    unittest.main()
