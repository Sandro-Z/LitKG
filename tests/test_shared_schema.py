"""Shared extraction contracts stay independent of the subject-specific schema."""
import json
import os
import unittest
from unittest.mock import patch

from app.entity_types import TYPE_DEFINITIONS, TYPE_PARENTS
from app.predicate_registry import PREDICATES
from app.processes import PROCESS_DEFINITIONS
from app.evaluation import evaluate_semantic_cases
from test_review_regressions import candidate, entity, prepared


class SharedSchemaTests(unittest.TestCase):
    def test_every_registered_endpoint_and_parent_has_a_defined_type(self):
        for name, spec in PREDICATES.items():
            for role in ('subject_types', 'object_types'):
                self.assertLessEqual(set(spec[role]) - {'*'}, set(TYPE_DEFINITIONS), (name, role))
        self.assertLessEqual(set(TYPE_PARENTS) | set(TYPE_PARENTS.values()), set(TYPE_DEFINITIONS))

    def test_prompt_example_is_valid_under_the_actual_branch_schema(self):
        from test_api import TEST_ENV
        with patch.dict(os.environ, TEST_ENV):
            from app import llm
        row = json.loads(llm.COMPARISON_EXAMPLE)
        result = prepared(row)
        self.assertEqual(len(result.claims), 1, result._claim_rejections)
        prompt = str(llm.build_extraction_messages('Results', row['evidence']['sentence']))
        for token in ('FMT', 'MicrobialTaxon', 'OTU', 'CROSS_FEEDS'):
            self.assertNotIn(token, prompt)

    def test_negative_and_positive_arms_keep_separate_polarities(self):
        quote = 'Treatment A failed to influence natural tumor growth, but increased Treatment B efficacy after blockade.'
        negative = candidate(quote, entity('Treatment A', 'Intervention'),
            entity('natural tumor growth', 'Phenotype', {'kind':'growth', 'direction':'unspecified'}))
        positive = candidate(quote, entity('Treatment A', 'Intervention'),
            entity('Treatment B efficacy', 'Phenotype', {'kind':'activity', 'direction':'increased'}))
        self.assertIn('growth', PROCESS_DEFINITIONS)
        self.assertEqual(prepared(negative).claims[0].result_status, 'no_effect_reported')
        self.assertEqual(prepared(positive).claims[0].result_status, 'observed_change')

    def test_fixed_semantics_evaluator_keeps_unassessed_cases_out_of_success_count(self):
        report = evaluate_semantic_cases([
            {'name':'explicit negative', 'candidate':candidate('Treatment A did not affect CD8 cells.'),
             'source_text':'Treatment A did not affect CD8 cells.', 'expected':'accepted',
             'expected_fields':{'result_status':'no_effect_reported'}},
            {'name':'missing source', 'observable_only':True, 'reason':'No original context', 'expected':'unassessed'},
        ])
        self.assertEqual((report['passed_count'], report['unassessed_count'], report['failed_count']), (1,1,0))
        self.assertNotIn('precision', report)
