import unittest

from app.page_mapping import PdfPageIndex
from app.provenance import locate_evidence_pages, page_bounds


class PageMappingTests(unittest.TestCase):
    def test_missing_pages_align_each_sentence_and_retain_unknown_text(self):
        index = PdfPageIndex([(2, "Aspirin inhibited COX-1."), (4, "EGFR binds gefitinib.")])
        text = "Aspirin inhibited COX-1. Unmatched original evidence. EGFR binds gefitinib."
        spans = index.page_spans(text)
        self.assertEqual(page_bounds(spans), (None, None))
        chunk = {"text": text, "page_spans": spans}
        self.assertEqual(locate_evidence_pages("Aspirin inhibited COX-1.", chunk), (2, 2))
        self.assertEqual(locate_evidence_pages("EGFR binds gefitinib.", chunk), (4, 4))
        self.assertEqual(locate_evidence_pages("Unmatched original evidence.", chunk), (None, None))

    def test_ambiguous_occurrences_keep_a_range_and_allow_known_context(self):
        index = PdfPageIndex([(1, "Aspirin inhibits COX-1."), (4, "Aspirin inhibits COX-1.")])
        self.assertEqual(index.locate("Aspirin inhibits COX-1."), (1, 4))
        self.assertEqual(index.locate("Aspirin inhibits COX-1.", within=(3, 5)), (4, 4))
        self.assertEqual(index.locate("Aspirin inhibits COX-1.", within=(2, 3)), (None, None))

    def test_sentence_crossing_a_physical_page_break_retains_both_pages(self):
        index = PdfPageIndex([(2, "Aspirin inhibits"), (3, "COX-1 in cells.")])
        self.assertEqual(index.locate("Aspirin inhibits COX-1 in cells."), (2, 3))

    def test_pdf_typography_differences_match_without_losing_original_offsets(self):
        sentence = 'Gefitinib effectively inhibits "EGFR".'
        text = 'Prelude. Ge\ufb01tinib ef-\nfectively inhibits \u201cEG\u00adFR\u201d. Unmapped tail.'
        start, end = text.index("Ge"), text.index(". Unmapped") + 1
        chunk = {"text": text, "page_spans": [
            {"start": 0, "end": start, "page_start": None, "page_end": None},
            {"start": start, "end": end, "page_start": 7, "page_end": 7},
            {"start": end, "end": len(text), "page_start": None, "page_end": None},
        ]}
        self.assertEqual(locate_evidence_pages(sentence, chunk), (7, 7))
        self.assertEqual(PdfPageIndex([(7, text)]).locate(sentence), (7, 7))

    def test_partial_mapping_does_not_hide_unmapped_content(self):
        index = PdfPageIndex([(2, "Known evidence.")])
        text = "Known evidence. Unmapped evidence."
        spans = index.page_spans(text, [{"start": 0, "end": 15, "page_start": 2, "page_end": 2}])
        self.assertEqual(page_bounds(spans), (None, None))

    def test_invalid_page_ranges_stay_unknown(self):
        for first, last in [(4, 2), (True, 2), (0, 1), (None, 3)]:
            self.assertEqual(page_bounds([{"page_start": first, "page_end": last}]), (None, None))


if __name__ == "__main__":
    unittest.main()
