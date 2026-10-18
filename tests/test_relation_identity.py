"""Regression for batch 23's explicit-vs-compact comparison encoding."""
import copy
import unittest
from unittest.mock import MagicMock, patch

from app.facts import canonical_comparison_qualifiers
from app.graph_store import _claim_projection
from app.schemas import Claim


class RelationIdentityTests(unittest.TestCase):
    def test_comparison_encodings_share_identity_without_editing_raw_claim(self):
        compact = Claim.model_validate({
            'subject': {'text': 'Enterococcus faecium', 'type': 'MicrobialTaxon'},
            'predicate': 'DIFFERS_BETWEEN',
            'object': {'text': 'responders', 'type': 'StudyGroup'},
            'qualifiers': {'metric': 'abundance', 'direction': 'increased',
                           'comparison_group': 'responders', 'reference_group': 'nonresponders'},
            'evidence': {'sentence': 'Enterococcus faecium was more abundant in responders than nonresponders.'},
            'confidence': .9,
        })
        original = compact.model_dump()
        explicit = compact.model_copy(deep=True)
        from app.schemas import Process
        explicit.subject.process = Process(kind='abundance', direction='increased')
        ids = {'Enterococcus faecium': 1, 'responders': 2, 'nonresponders': 3}
        with patch('app.graph_store.lock_graph_projection'), patch('app.graph_store.upsert_entity',
                side_effect=lambda cur, identity, **kwargs: ids[identity.canonical_name]):
            a = _claim_projection(MagicMock(), compact, document_id=132)
            b = _claim_projection(MagicMock(), explicit, document_id=132)
        self.assertEqual(a['qualifiers'], b['qualifiers'])
        self.assertEqual(a['qualifiers_hash'], b['qualifiers_hash'])
        self.assertEqual(a['fingerprint'], b['fingerprint'])
        self.assertEqual(compact.model_dump(), original)

    def test_context_differences_and_unrecognized_metrics_are_preserved(self):
        base = {'metric': 'abundance', 'direction': 'increased', 'reference_group': 'NR'}
        expected = canonical_comparison_qualifiers('DIFFERS_BETWEEN', base)
        self.assertEqual(base, {'metric': 'abundance', 'direction': 'increased', 'reference_group': 'NR'})
        self.assertEqual(canonical_comparison_qualifiers('DIFFERS_BETWEEN', expected), expected)
        for key, value in [('direction', 'decreased'), ('metric', 'expression'),
                           ('reference_group', 'controls'), ('evidence_type', 'experimental')]:
            other = canonical_comparison_qualifiers('DIFFERS_BETWEEN', {**base, key: value})
            self.assertNotEqual(other, expected)
        for metric in ['frequency', 'percentage', 'relative abundance', 'Shannon index', None]:
            other = canonical_comparison_qualifiers('DIFFERS_BETWEEN', {**base, 'metric': metric})
            self.assertNotIn('processes', other)
        self.assertNotIn('processes', canonical_comparison_qualifiers('ASSOCIATED_WITH', base))
        existing = {**base, 'processes': [{'role': 'subject', 'kind': 'activity', 'direction': 'increased'}]}
        self.assertEqual(canonical_comparison_qualifiers('DIFFERS_BETWEEN', copy.deepcopy(existing)), existing)
