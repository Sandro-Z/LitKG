import unittest

from app.normalization import (
    canonicalize_qualifiers,
    claim_fingerprint,
    entity_identity,
    extract_abbreviations,
    normalize_external_id,
    normalize_predicate,
    qualifiers_fingerprint,
)


class NormalizationTests(unittest.TestCase):
    def test_name_based_entities_merge_case_and_whitespace(self):
        first = entity_identity("  Aspirin ", "Drug")
        second = entity_identity("ASPIRIN", "drug")

        self.assertEqual(first.canonical_key, second.canonical_key)
        self.assertEqual(first.entity_type, "Drug")

    def test_external_id_merges_different_aliases(self):
        first = entity_identity(
            "acetylsalicylic acid",
            "Drug",
            "CHEBI:15365",
        )
        second = entity_identity("Aspirin", "drug", "chebi:15365")

        self.assertEqual(first.canonical_key, second.canonical_key)
        self.assertNotEqual(first.normalized_alias, second.normalized_alias)

    def test_same_name_with_different_types_does_not_merge(self):
        gene = entity_identity("MET", "Gene")
        protein = entity_identity("MET", "Protein")

        self.assertNotEqual(gene.canonical_key, protein.canonical_key)

    def test_protein_name_variants_and_greek_letters(self):
        variants = ["TNF-α", "TNF alpha", "TNF_ALPHA", "TNFα"]
        self.assertEqual(len({entity_identity(value, "Protein").canonical_key for value in variants}), 1)
        self.assertEqual(entity_identity("IL 6", "Protein").canonical_key, entity_identity("IL-6", "Protein").canonical_key)

    def test_variant_species_and_chemical_stereochemistry_remain_distinct(self):
        pairs = [
            ("AKT1", "AKT2", "Protein"), ("ERK1/2", "ERK12", "Protein"),
            ("EGFR", "EGFRvIII", "Protein"), ("human EGFR", "murine EGFR", "Protein"),
            ("cis-platin", "trans-platin", "Drug"), ("IL-6", "IL-16", "Protein"),
        ]
        for first, second, entity_type in pairs:
            with self.subTest(first=first, second=second):
                self.assertNotEqual(entity_identity(first, entity_type).canonical_key, entity_identity(second, entity_type).canonical_key)

    def test_drug_and_compound_share_chemical_identity(self):
        self.assertEqual(entity_identity("Aspirin", "Compound").canonical_key, entity_identity("Aspirin", "Drug").canonical_key)

    def test_external_identifier_prefixes_urls_and_isoforms(self):
        for value in ["CHEBI_15365", "chebi: 015365", "https://purl.obolibrary.org/obo/CHEBI_15365"]:
            self.assertEqual(normalize_external_id(value), "CHEBI:15365")
        self.assertEqual(normalize_external_id("https://www.uniprot.org/uniprotkb/P00533"), "UniProt:P00533")
        self.assertEqual(normalize_external_id("PubChem CID:2244"), "PubChem:2244")
        self.assertEqual(normalize_external_id("NCBI Gene:1956"), "NCBIGene:1956")
        self.assertNotEqual(normalize_external_id("UniProt:P00533"), normalize_external_id("UniProt:P00533-2"))

    def test_document_definitions_resolve_both_forward_and_reverse_abbreviations(self):
        definitions = extract_abbreviations("Interleukin-6 (IL-6) activates EGFR (epidermal growth factor receptor).")
        self.assertEqual(definitions["il6"], "Interleukin-6")
        self.assertEqual(definitions["egfr"], "epidermal growth factor receptor")
        short = entity_identity("IL6", "Protein", abbreviations=definitions)
        long = entity_identity("Interleukin 6", "Protein", abbreviations=definitions)
        self.assertEqual(short.canonical_key, long.canonical_key)
        self.assertIn("IL6", short.aliases)

    def test_ambiguous_abbreviations_are_scoped_and_not_guessed(self):
        definitions = extract_abbreviations("Androgen receptor (AR). Aldose reductase (AR).")
        self.assertIsNone(definitions["ar"])
        first = entity_identity("AR", "Protein", abbreviations=definitions, document_id=1)
        second = entity_identity("AR", "Protein", abbreviations=definitions, document_id=2)
        self.assertTrue(first.context_ambiguous)
        self.assertNotEqual(first.canonical_key, second.canonical_key)
        self.assertEqual(first.canonical_name, "AR")

    def test_abbreviation_does_not_drop_species_or_isoform_number(self):
        definitions = extract_abbreviations("human epidermal growth factor receptor (EGFR). AKT1 (AKT).")
        self.assertEqual(definitions["egfr"], "human epidermal growth factor receptor")
        self.assertNotIn("akt", definitions)

    def test_local_compound_numbers_remain_document_specific(self):
        self.assertNotEqual(entity_identity("compound 1", "Compound", document_id=1).canonical_key, entity_identity("compound 1", "Drug", document_id=2).canonical_key)
        self.assertEqual(entity_identity("Aspirin", "Compound", document_id=1).canonical_key, entity_identity("Aspirin", "Drug", document_id=2).canonical_key)

    def test_greek_abbreviations_align_with_spelled_out_names(self):
        definitions = extract_abbreviations("Tumor necrosis factor alpha (TNF-α).")
        self.assertEqual(definitions["tnfalpha"], "Tumor necrosis factor alpha")
        self.assertEqual(entity_identity("TNF-α", "Protein", abbreviations=definitions).canonical_key, entity_identity("tumor necrosis factor α", "Protein").canonical_key)

    def test_unknown_names_are_not_global_entity_identities(self):
        first = entity_identity("", "Protein", document_id=1)
        second = entity_identity("", "Protein", document_id=2)
        self.assertTrue(first.local_label)
        self.assertNotEqual(first.canonical_key, second.canonical_key)

    def test_predicate_is_canonicalized(self):
        self.assertEqual(normalize_predicate(" binds to "), "BINDS_TO")
        self.assertEqual(normalize_predicate("up-regulates"), "UP_REGULATES")

    def test_qualifier_hash_is_independent_of_mapping_order(self):
        first = {"unit": " nM ", "value": "10", "unused": None}
        second = {"value": "10", "unit": "NM"}

        self.assertNotEqual(
            canonicalize_qualifiers(first),
            canonicalize_qualifiers(second),
        )
        self.assertEqual(
            qualifiers_fingerprint(first),
            qualifiers_fingerprint(second),
        )

    def test_claim_fingerprint_tracks_provenance_and_polarity(self):
        subject = entity_identity("Aspirin", "Drug")
        object_ = entity_identity("COX-1", "Protein")
        base = {
            "subject": subject,
            "predicate": "INHIBITS",
            "object_": object_,
            "qualifiers": {"assay": "in vitro"},
            "evidence_text": "Aspirin inhibits COX-1.",
            "negated": False,
            "speculative": False,
        }

        self.assertEqual(claim_fingerprint(**base), claim_fingerprint(**base))
        self.assertNotEqual(
            claim_fingerprint(**base),
            claim_fingerprint(**{**base, "negated": True}),
        )
        self.assertNotEqual(
            claim_fingerprint(**base),
            claim_fingerprint(
                **{
                    **base,
                    "evidence_text": (
                        "A second experiment confirmed this."
                    ),
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
