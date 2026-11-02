import os
import unittest
from psycopg.types.json import Jsonb

from app.graph_store import upsert_entity
from app.normalization import entity_identity,qualifiers_fingerprint
from app.facts import store_fact,fact_qualifiers
from app.schemas import Claim,Participant
import test_entity_resolution_integration as resolution


@unittest.skipUnless(os.environ.get('LITKG_TEST_DATABASE_URL'),'Requires disposable PostgreSQL database')
class EntityHygieneIntegrationTests(unittest.TestCase):
    setUp=resolution.EntityResolutionIntegrationTests.setUp
    connect=resolution.EntityResolutionIntegrationTests.connect
    drop_schema=resolution.EntityResolutionIntegrationTests.drop_schema
    document=resolution.EntityResolutionIntegrationTests.document
    legacy_claim=resolution.EntityResolutionIntegrationTests.legacy_claim

    def seed(self,ids=(None,None)):
        result=[]
        for index,name in enumerate(['sodium-butyrate','sodium_butyrate']):
            text=f'{name} inhibits COX-1. Extra measurement {index}.'
            doc=self.document(text)
            claim=Claim.model_validate({'subject':{'text':name,'type':'Compound','normalized_id':ids[index],
                                         'process':{'kind':'activity','direction':'increased'}},
                                        'object':{'text':'COX-1','type':'Protein'},'predicate':'INHIBITS',
                                        'evidence':{'sentence':text.split('. ')[0]+'.'},
                                        'additional_evidence':[{'sentence':f'Extra measurement {index}.'}], 'confidence':.9})
            claim.participants=[Participant(role='co_intervention',entity=claim.subject.model_copy(deep=True))]
            cid=self.legacy_claim(doc,claim)
            with self.connect() as conn:
                cur=conn.cursor()
                row=conn.execute('SELECT * FROM claims WHERE id=%s',(cid,)).fetchone()
                relation=conn.execute('SELECT relation_id FROM relation_evidence WHERE claim_id=%s',(cid,)).fetchone()['relation_id']
                participants=[{'role':role,'entity_id':row[role+'_entity_id'],'mention':getattr(claim,role).text,'entity':getattr(claim,role)} for role in ('subject','object')]
                participants.append({'role':'co_intervention','entity_id':row['subject_entity_id'],'mention':claim.subject.text,'entity':claim.participants[0].entity})
                q=fact_qualifiers(claim,participants)
                q['participants']=[['co_intervention',row['subject_entity_id']]]
                conn.execute('UPDATE relations SET qualifiers=%s,qualifiers_hash=%s WHERE id=%s',(Jsonb(q),qualifiers_fingerprint(q),relation))
                store_fact(cur,cid,relation,claim,{'predicate':claim.predicate,'participants':participants,'qualifiers':q},doc[0])
            result.append((cid,claim.model_dump(mode='json'),row['subject_entity_id']))
        return result

    def test_format_merge_reuses_transaction_preserves_claims_and_all_evidence(self):
        before=self.seed()
        with self.connect() as conn:
            survivor=upsert_entity(conn.cursor(),entity_identity('sodium butyrate','Compound'),reconcile_existing=True)
        self.assertEqual(survivor,min(r[2] for r in before))
        with self.connect() as conn:
            rows=conn.execute('SELECT id,claim_json,subject_entity_id FROM claims ORDER BY id').fetchall()
            self.assertEqual([(r['id'],r['claim_json']) for r in rows],[(cid,raw) for cid,raw,_ in before])
            self.assertEqual({r['subject_entity_id'] for r in rows},{survivor})
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM fact_evidence').fetchone()['n'],4)
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM relation_evidence').fetchone()['n'],2)
            self.assertEqual({r['entity_id'] for r in conn.execute("SELECT entity_id FROM fact_participants WHERE role='subject'")},{survivor})
            for row in conn.execute('SELECT qualifiers FROM facts'):
                self.assertEqual([p['entity_id'] for p in row['qualifiers']['processes'] if p['role']=='co_intervention'],[survivor])
            self.assertEqual(upsert_entity(conn.cursor(),entity_identity('sodium_butyrate','Compound'),reconcile_existing=True),survivor)

    def test_conflicting_ids_prevent_format_merge(self):
        before=self.seed(('CHEBI:100','CHEBI:200'))
        with self.connect() as conn:
            upsert_entity(conn.cursor(),entity_identity('sodium butyrate','Compound','CHEBI:100'),reconcile_existing=True)
            self.assertEqual({r['subject_entity_id'] for r in conn.execute('SELECT subject_entity_id FROM claims')},{row[2] for row in before})

    def test_merge_rolls_back_atomically(self):
        before=self.seed()
        with self.assertRaisesRegex(RuntimeError,'rollback probe'):
            with self.connect() as conn:
                upsert_entity(conn.cursor(),entity_identity('sodium butyrate','Compound'),reconcile_existing=True)
                raise RuntimeError('rollback probe')
        with self.connect() as conn:
            rows=conn.execute('SELECT id,claim_json,subject_entity_id FROM claims ORDER BY id').fetchall()
            self.assertEqual([(r['id'],r['claim_json'],r['subject_entity_id']) for r in rows],before)
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM fact_evidence').fetchone()['n'],4)
