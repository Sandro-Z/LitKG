"""Real PostgreSQL checks for reset isolation, role projection and legacy revalidation."""
import hashlib
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.claim_reset import reset_claims
from app.claim_semantics import result_sql, result_view
from app.graph_store import project_existing_claim, store_claim, upsert_entity
from app.graph_query import fetch_graph
from app.normalization import entity_identity
from app.schemas import Claim, MaintenanceRequest
from test_review_regressions import candidate, entity, prepared
from pdf_fixtures import text_pdf


@unittest.skipUnless(os.environ.get('LITKG_TEST_DATABASE_URL'),'Requires disposable PostgreSQL database')
class AuditIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn=os.environ['LITKG_TEST_DATABASE_URL']
        cls.schema='litkg_audit_'+uuid.uuid4().hex
        with psycopg.connect(cls.dsn) as conn:
            conn.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
            conn.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public')
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        with cls.connect() as conn:
            conn.execute((Path(__file__).resolve().parents[1]/'db/init.sql').read_text())

    @classmethod
    def connect(cls):
        return psycopg.connect(cls.dsn,row_factory=dict_row,options=f'-csearch_path={cls.schema},public')

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.dsn) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def seed(self, raw=None, path='/test/source.pdf'):
        raw=raw or candidate('FMT increased CD8 cells.')
        marker=uuid.uuid4().hex
        with self.connect() as conn:
            kb=conn.execute('INSERT INTO knowledge_bases(slug,name) VALUES (%s,%s) RETURNING id',(marker,marker)).fetchone()['id']
            doc=conn.execute("INSERT INTO documents(sha256,filename,source_path,status,catalog_status,catalog_version) VALUES (%s,'source.pdf',%s,'done','done','v') RETURNING id",(marker,path)).fetchone()['id']
            conn.execute('INSERT INTO knowledge_base_documents(knowledge_base_id,document_id) VALUES (%s,%s)',(kb,doc))
            chunk=conn.execute("INSERT INTO chunks(document_id,chunk_index,text,status) VALUES (%s,0,%s,'done') RETURNING id",(doc,raw['evidence']['sentence'])).fetchone()['id']
            claim=prepared(raw).claims[0] if raw['predicate']!='MEASURED_BY' else Claim.model_validate(raw)
            cid,_=store_claim(conn.cursor(),document_id=doc,chunk_id=chunk,claim=claim,extraction_model='fixture')
        return kb,doc,chunk,cid

    def test_reset_preserves_pdf_and_chunks_and_requires_explicit_shared_scope(self):
        with text_pdf(['FMT increased CD8 cells.']) as pdf:
            before=hashlib.sha256(pdf.read_bytes()).digest()
            kb,doc,chunk,cid=self.seed(path=str(pdf))
            other,other_doc,_,other_cid=self.seed()
            with self.connect() as conn:
                conn.execute('INSERT INTO knowledge_base_documents(knowledge_base_id,document_id) VALUES (%s,%s)',(other,doc))
                conn.execute("UPDATE chunks SET status='failed',error='input review pending' WHERE id=%s",(chunk,))
                conn.execute("UPDATE documents SET status='partial',error='input review pending' WHERE id=%s",(doc,))
                cur=conn.cursor()
                preview=reset_claims(cur,kb,idle_check=lambda:True)
                self.assertEqual(preview['claim_count'],1)
                self.assertEqual(preview['other_affected_knowledge_bases'],[other])
                self.assertFalse(preview['applied'])
                self.assertEqual(conn.execute('SELECT count(*) AS n FROM claims WHERE id=%s',(cid,)).fetchone()['n'],1)
            with self.assertRaisesRegex(ValueError,'shared'):
                with self.connect() as conn:
                    reset_claims(conn.cursor(),kb,apply=True,idle_check=lambda:True)
            with self.assertRaisesRegex(ValueError,'Broker'):
                with self.connect() as conn:
                    reset_claims(conn.cursor(),kb,apply=True,include_shared=True,idle_check=lambda:False)
            with self.connect() as conn:
                result=reset_claims(conn.cursor(),kb,apply=True,include_shared=True,idle_check=lambda:True)
                self.assertEqual(result['deleted_claims'],1)
            with self.connect() as conn:
                self.assertFalse(conn.execute('SELECT id FROM claims WHERE id=%s',(cid,)).fetchone())
                self.assertTrue(conn.execute('SELECT id FROM claims WHERE id=%s',(other_cid,)).fetchone())
                self.assertTrue(conn.execute('SELECT id,text FROM chunks WHERE id=%s',(chunk,)).fetchone())
                self.assertEqual(conn.execute('SELECT status,error FROM chunks WHERE id=%s',(chunk,)).fetchone(),{'status':'failed','error':'input review pending'})
                self.assertEqual(conn.execute('SELECT status,error FROM documents WHERE id=%s',(doc,)).fetchone(),{'status':'partial','error':'input review pending'})
                self.assertTrue(conn.execute('SELECT id FROM documents WHERE id=%s',(doc,)).fetchone())
                self.assertEqual(conn.execute('SELECT pipeline_generation,extraction_epoch FROM documents WHERE id=%s',(doc,)).fetchone(),{'pipeline_generation':1,'extraction_epoch':1})
                self.assertTrue(conn.execute('SELECT id FROM claim_history WHERE original_claim_id=%s',(cid,)).fetchone())
                self.assertFalse(conn.execute('SELECT id FROM facts WHERE claim_id=%s',(cid,)).fetchone())
            self.assertEqual(hashlib.sha256(pdf.read_bytes()).digest(),before)

    def test_projection_retains_product_and_quarantines_invalid_legacy_record(self):
        raw=candidate('Bacterium converts choline into TMA.',entity('Bacterium','MicrobialTaxon'),entity('choline','Compound'),'TRANSFORMS',
                      qualifiers={'evidence_type':'experimental','product':entity('TMA','Compound')})
        _,doc,_,cid=self.seed(raw)
        with self.connect() as conn:
            rows=conn.execute("SELECT p.role,e.canonical_name FROM fact_participants p JOIN entities e ON e.id=p.entity_id JOIN facts f ON f.id=p.fact_id WHERE f.claim_id=%s AND role='product'",(cid,)).fetchall()
            self.assertEqual(rows,[{'role':'product','canonical_name':'TMA'}])
        bad=candidate('Flow cytometry was used.',entity('Flow cytometry','Assay'),entity('Flow cytometry','Assay'),'MEASURED_BY')
        kb,doc,_,cid=self.seed(bad)
        with self.connect() as conn:
            cur=conn.cursor()
            project_existing_claim(cur,cid,bad,document_id=doc)
            saved=conn.execute('SELECT claim_json FROM claims WHERE id=%s',(cid,)).fetchone()['claim_json']
            self.assertEqual(saved['semantic_status'],'pending')
            self.assertEqual(saved['subject'],Claim.model_validate(bad).subject.model_dump())
            self.assertEqual(fetch_graph(cur,knowledge_base_id=kb,include_total=True)['total'],0)
            self.assertEqual(fetch_graph(cur,knowledge_base_id=kb,include_total=True,include_pending=True)['total'],1)

    def test_local_nodes_are_not_reused_by_global_alias_lookup(self):
        with self.connect() as conn:
            cur=conn.cursor()
            first=upsert_entity(cur,entity_identity('wild-type','MicrobialStrain',document_id=236))
            second=upsert_entity(cur,entity_identity('wild-type','MicrobialStrain',document_id=239))
            self.assertNotEqual(first,second)
            local=upsert_entity(cur,entity_identity('Local organism','MicrobialStrain',document_id=236,pending_merge=True))
            global_id=upsert_entity(cur,entity_identity('Local organism','MicrobialStrain',document_id=239))
            self.assertNotEqual(local,global_id)

    def test_python_and_sql_result_status_agree(self):
        cases=[{'predicate':'AFFECTS','negated':True},
               {'predicate':'PRODUCES','result_status':'no_effect_reported','qualifiers':{'direction':'unchanged'}},
               {'predicate':'AFFECTS','object':{'process':{'direction':'unchanged'}}},
               {'predicate':'DIFFERS_BETWEEN','subject':{'process':{'direction':'decreased'}}},
               {'predicate':'AFFECTS','qualifiers':{'processes':[{'role':'host','direction':'unchanged'}]}},
               {'predicate':'AFFECTS','qualifiers':{'processes':[{'role':'object','direction':'increased'}]}},
               {'predicate':'AFFECTS','result_status':'no_effect_reported','qualifiers':{'result_interpretation':'not_assessed'}},
               {'predicate':'AFFECTS','result_status':'unknown','qualifiers':{'result_status':'not_significant'}},
               {'predicate':'AFFECTS','object':{'process':{'direction':'unchanged'}},'qualifiers':{'object_direction':'increased'}}]
        with self.connect() as conn:
            for value in cases:
                actual=conn.execute(f'WITH c AS (SELECT %s::jsonb AS claim_json,%s::text AS predicate,%s::boolean AS negated) SELECT {result_sql("c")} AS status FROM c',
                                    (Jsonb(value),value['predicate'],value.get('negated',False))).fetchone()['status']
                self.assertEqual(actual,result_view(value),value)

    def test_reparse_runs_pdf_parser_but_reextract_reuses_saved_text(self):
        from test_api import TEST_ENV
        with patch.dict(os.environ,TEST_ENV):
            from app import tasks
        for mode in ('reparse','reextract'):
            kb,doc,_,_=self.seed()
            job=str(uuid.uuid4())
            with self.connect() as conn:
                conn.execute('INSERT INTO maintenance_jobs(id,knowledge_base_id,document_id,mode) VALUES (%s,%s,%s,%s)',(job,kb,doc,MaintenanceRequest(mode=mode).mode))
            with patch.object(tasks,'get_conn',side_effect=self.connect), \
                 patch.object(tasks.parse_document,'run',return_value={'status':'cataloging'}) as parser, \
                 patch.object(tasks.build_document_catalog,'run',return_value={'status':'done'}) as catalog, \
                 patch.object(tasks.extract_chunk,'run',return_value={'status':'done'}), \
                 patch.object(tasks.refresh_derived,'run',return_value={'status':'done'}), \
                 patch.object(tasks,'refresh_document_status',return_value='done'):
                self.assertEqual(tasks.maintain_knowledge_base.run(job)['status'],'done')
                self.assertEqual(parser.call_count,int(mode=='reparse'))
                catalog.assert_called_once_with(doc,force=True,publish=False)
