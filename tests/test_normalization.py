import unittest

from app.normalization import (
    canonicalize_qualifiers,
    claim_fingerprint,
    entity_identity,
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
