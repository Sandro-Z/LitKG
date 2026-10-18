import unittest

from app.doi import extract_document_doi, normalize_doi


class DoiTests(unittest.TestCase):
    def test_normalizes_resolver_urls_and_citation_punctuation(self):
        cases = {
            "DOI: https://dx.doi.org/10.1234/paper.": "10.1234/paper",
            "https://doi.org/10.1234/paper?utm_source=pdf#section": "10.1234/paper",
            "https://doi.org/10.1234/paper%28v2%29": "10.1234/paper(v2)",
            "10.1234/paper).": "10.1234/paper",
            "10.1234/paper(v2)).": "10.1234/paper(v2)",
            "10.1234/paper]": "10.1234/paper",
            "10.1234/pa\u200bper\u00ad": "10.1234/paper",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_doi(value), expected)

    def test_rejects_invalid_identifiers(self):
        for value in [None, "", "not a DOI", "10.12/paper", "10.1234/", "10.1234/has spaces"]:
            with self.subTest(value=value):
                self.assertIsNone(normalize_doi(value))

    def test_finds_doi_at_end_of_long_document(self):
        pages = ["Article body without an identifier."] * 25
        for last_page in [
            "Appendix A\ndoi: 10.1234/paper.",
            "Appendix A\nhttps://doi.org/10.1234/paper",
            "Appendix A\n10.1234/paper",
        ]:
            with self.subTest(last_page=last_page):
                self.assertEqual(extract_document_doi([*pages, last_page]), "10.1234/paper")

    def test_repairs_doi_wraps_without_removing_real_hyphens(self):
        for text in [
            "https://doi.org/\n10.1234/paper-2026",
            "DOI: 10.1234/\npaper-2026",
            "DOI: 10.1234/paper-\n2026",
            "DOI: 10.1234/part/\npaper-2026",
        ]:
            with self.subTest(text=text):
                expected = "10.1234/part/paper-2026" if "/part/" in text else "10.1234/paper-2026"
                self.assertEqual(extract_document_doi([text]), expected)

    def test_reference_exclusion_persists_across_pages(self):
        for heading in ["References", "8. REFERENCES", "References and Notes", "Bibliography", "参考文献"]:
            with self.subTest(heading=heading):
                pages = [
                    "Body without a DOI.",
                    f"{heading}\nA cited article. doi: 10.1234/reference-one",
                    "A second cited article. https://doi.org/10.1234/reference-two",
                ]
                self.assertIsNone(extract_document_doi(pages))

    def test_appendix_after_references_is_still_searched(self):
        for heading in ["Appendix A. Supplementary data", "Supporting information", "附录 A"]:
            with self.subTest(heading=heading):
                pages = [
                    "Body text.",
                    "References\nSmith et al. (2024). doi: 10.5678/reference",
                    f"{heading}\nSupplementary data to this article: https://doi.org/10.1234/paper",
                ]
                self.assertEqual(extract_document_doi(pages), "10.1234/paper")

    def test_reference_and_appendix_on_same_page_are_separated(self):
        text = ("References\nOther work doi: 10.5678/reference\n"
                "Appendix A\nDOI: 10.1234/paper")
        self.assertEqual(extract_document_doi([text]), "10.1234/paper")

    def test_citation_entries_and_data_identifiers_are_not_article_dois(self):
        for text in [
            "Smith et al. (2024), doi: 10.5678/reference",
            "[1] Smith (2024). doi: 10.5678/reference",
            "1. Smith (2024). https://doi.org/10.5678/reference",
            "Smith, A. (2024). Study title.\nhttps://doi.org/10.5678/reference",
            "Data availability\nThe dataset is available at https://doi.org/10.5678/data",
            "Code availability: https://doi.org/10.5678/code",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(extract_document_doi([text]))

    def test_skips_data_doi_and_finds_article_doi(self):
        pages = [
            "Data availability: dataset https://doi.org/10.5678/data",
            "Appendix A\ndoi: 10.1234/paper",
        ]
        self.assertEqual(extract_document_doi(pages), "10.1234/paper")

    def test_competing_dois_require_self_identification(self):
        self.assertIsNone(extract_document_doi(["doi: 10.1234/one", "doi: 10.1234/two"]))
        self.assertEqual(extract_document_doi([
            "doi: 10.1234/related", "Cite this article: Smith et al. (2024). doi: 10.1234/paper",
        ]), "10.1234/paper")
        self.assertIsNone(extract_document_doi([
            "Article DOI: 10.1234/one", "Article DOI: 10.1234/two",
        ]))

    def test_repeated_dois_are_case_insensitively_deduplicated(self):
        self.assertEqual(extract_document_doi([
            "https://doi.org/10.1234/Paper", "doi: 10.1234/paper",
        ]), "10.1234/Paper")

    def test_running_author_header_does_not_hide_article_doi(self):
        self.assertEqual(extract_document_doi([
            "Smith et al.\nAppendix A\nhttps://doi.org/10.1234/paper",
        ]), "10.1234/paper")

    def test_legacy_sici_suffix_keeps_internal_punctuation(self):
        doi = "10.1234/(SICI)1097-1234(199601)1:1<2::AID-TEST>3.0.CO;2-A"
        self.assertEqual(normalize_doi(doi), doi)
        self.assertEqual(extract_document_doi([f"Appendix\nDOI: {doi}."]), doi)


if __name__ == "__main__":
    unittest.main()
