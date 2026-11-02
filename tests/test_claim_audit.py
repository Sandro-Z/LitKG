import copy
import unittest
from unittest.mock import patch

from app.claim_audit import make_audit_plan, main


class ClaimAuditPlanTests(unittest.TestCase):
    def scope(self):
        raw = {'subject': {'text':'X'}, 'object': {'text':'Y'}, 'predicate':'CORRELATED_WITH',
               'evidence': {'sentence':'No relationship between X and Y has been identified.'}}
        return ({'id':7, 'status':'done'}, [{'id':9,'status':'done'}], [{'id':11,'chunk_id':9,'claim_json':raw}])

    def test_plan_preserves_assertion_and_only_proposes_review(self):
        scope = self.scope()
        before = copy.deepcopy(scope)
        plan = make_audit_plan(*scope)
        self.assertTrue(plan['dry_run'])
        self.assertEqual(plan['mode'],'audit')
        self.assertEqual(plan['changes'],[])
        self.assertEqual(plan['before']['claims'][0]['claim_json'],scope[2][0]['claim_json'])
        self.assertEqual(plan['reviews'][0]['claim_id'],11)
        self.assertEqual(plan['reviews'][0]['findings'][0]['rule_id'],'V1-NO-RELATION')
        self.assertEqual(scope,before)

    def test_clean_claim_has_no_review_and_active_scope_is_rejected(self):
        doc,chunks,claims = self.scope()
        claims[0]['claim_json']['negated'] = True
        self.assertFalse(make_audit_plan(doc,chunks,claims)['reviews'])
        chunks[0]['status']='extracting'
        with self.assertRaisesRegex(ValueError,'active'):
            make_audit_plan(doc,chunks,claims)

    def test_explicit_dry_run_cannot_apply(self):
        with patch('sys.argv',['claim_audit','--apply-plan','plan.json','--dry-run']):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code,2)
