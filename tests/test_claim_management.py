import os,unittest,uuid
from pathlib import Path
from unittest.mock import patch
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi.testclient import TestClient
from app.graph_store import store_claim
from app.schemas import Claim
from test_review_regressions import candidate,entity
from pdf_fixtures import text_pdf


@unittest.skipUnless(os.environ.get('LITKG_TEST_DATABASE_URL'),'Requires disposable PostgreSQL database')
class ClaimManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_api import TEST_ENV
        with patch.dict(os.environ,TEST_ENV):
            from app import main,tasks
        cls.main,cls.tasks=main,tasks
        cls.client=TestClient(main.app)
        cls.dsn=os.environ['LITKG_TEST_DATABASE_URL']
        cls.schema='litkg_claim_ui_'+uuid.uuid4().hex
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

    def seed(self):
        marker=uuid.uuid4().hex
        with self.connect() as conn:
            kb=conn.execute('INSERT INTO knowledge_bases(slug,name) VALUES(%s,%s) RETURNING id',(marker,marker)).fetchone()['id']
            doc=conn.execute("INSERT INTO documents(sha256,filename,source_path,status,catalog_status) VALUES(%s,'kept.pdf','/test/kept.pdf','done','legacy') RETURNING id",(marker,)).fetchone()['id']
            conn.execute('INSERT INTO knowledge_base_documents(knowledge_base_id,document_id) VALUES(%s,%s)',(kb,doc))
            chunk=conn.execute("INSERT INTO chunks(document_id,chunk_index,text,status) VALUES(%s,0,'FMT increased CD8 cells. FMT increased CD4 cells.','done') RETURNING id",(doc,)).fetchone()['id']
            ids=[]
            for cell in ('CD8 cells','CD4 cells'):
                row=Claim.model_validate(candidate(f'FMT increased {cell}.',object_=entity(cell,'CellType',{'kind':'abundance','direction':'increased'})))
                cid,_=store_claim(conn.cursor(),document_id=doc,chunk_id=chunk,claim=row,extraction_model='fixture')
                ids.append(cid)
        return kb,doc,chunk,ids

    def request(self,kb,path,body):
        with patch.object(self.main,'get_conn',side_effect=self.connect),patch.object(self.main.refresh_derived,'apply_async'):
            return self.client.post(f'/knowledge-bases/{kb}/claims/{path}',json=body)

    def test_selected_deletion_preserves_unselected_claims_sources_and_history(self):
        kb,doc,chunk,ids=self.seed()
        pdf=self.enterContext(text_pdf(['A preserved source PDF.']))
        before=pdf.read_bytes()
        with self.connect() as conn:
            conn.execute("UPDATE documents SET source_path=%s,catalog_version='new-document-version' WHERE id=%s",(str(pdf),doc))
        other,_,_,other_ids=self.seed()
        body={'claim_ids':[ids[0]]}
        preview=self.request(kb,'delete-preview',body).json()
        self.assertEqual(preview['count'],1)
        deleted=self.request(kb,'delete',{**body,'selection_hash':preview['selection_hash']})
        self.assertEqual(deleted.status_code,200,deleted.text)
        self.assertEqual(deleted.json()['deleted_count'],1)
        with self.connect() as conn:
            self.assertEqual([r['id'] for r in conn.execute('SELECT id FROM claims WHERE document_id=%s',(doc,))],[ids[1]])
            self.assertTrue(conn.execute('SELECT id FROM documents WHERE id=%s',(doc,)).fetchone())
            self.assertTrue(conn.execute('SELECT id FROM chunks WHERE id=%s',(chunk,)).fetchone())
            self.assertTrue(conn.execute('SELECT id FROM claim_history WHERE original_claim_id=%s',(ids[0],)).fetchone())
            snapshot=conn.execute('SELECT snapshot FROM claim_history WHERE original_claim_id=%s ORDER BY id DESC LIMIT 1',(ids[0],)).fetchone()['snapshot']
            self.assertIsNone(snapshot['catalog_version'])
            self.assertEqual(snapshot['document_catalog_version'],'new-document-version')
            self.assertFalse(conn.execute('SELECT id FROM facts WHERE claim_id=%s',(ids[0],)).fetchone())
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM claims WHERE id=ANY(%s)',(other_ids,)).fetchone()['n'],2)
        self.assertEqual(pdf.read_bytes(),before)

    def test_all_matching_deletes_across_pages_and_filter_leaves_other_predicate(self):
        kb,doc,chunk,ids=self.seed()
        with self.connect() as conn:
            conn.execute("INSERT INTO claims(document_id,chunk_id,predicate,evidence_text,claim_json) SELECT %s,%s,'AFFECTS','repeated fixture',%s FROM generate_series(1,501)",(doc,chunk,Jsonb(candidate('FMT increased CD8 cells.'))))
            conn.execute("UPDATE claims SET predicate='TARGETS' WHERE id=%s",(ids[1],))
        body={'all_matching':True,'predicate':'AFFECTS'}
        preview=self.request(kb,'delete-preview',body).json()
        self.assertEqual(preview['count'],502)
        deleted=self.request(kb,'delete',{**body,'selection_hash':preview['selection_hash']})
        self.assertEqual(deleted.status_code,200,deleted.text)
        self.assertEqual(deleted.json()['deleted_count'],502)
        with self.connect() as conn:
            self.assertEqual([r['id'] for r in conn.execute('SELECT id FROM claims WHERE document_id=%s',(doc,))],[ids[1]])

    def test_scope_and_stale_preview_are_rejected_without_deleting(self):
        kb,doc,_,ids=self.seed()
        other,_,_,other_ids=self.seed()
        self.assertEqual(self.request(kb,'delete-preview',{'claim_ids':other_ids}).status_code,409)
        body={'all_matching':True}
        preview=self.request(kb,'delete-preview',body).json()
        with self.connect() as conn:
            conn.execute('UPDATE documents SET pipeline_generation=pipeline_generation+1 WHERE id=%s',(doc,))
        self.assertEqual(self.request(kb,'delete',{**body,'selection_hash':preview['selection_hash']}).status_code,409)
        preview=self.request(kb,'delete-preview',body).json()
        with self.connect() as conn:
            conn.execute('INSERT INTO knowledge_base_documents(knowledge_base_id,document_id) VALUES(%s,%s)',(other,doc))
        self.assertEqual(self.request(kb,'delete',{**body,'selection_hash':preview['selection_hash'],'allow_shared':True}).status_code,409)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM claims WHERE document_id=%s',(doc,)).fetchone()['n'],2)
        self.assertEqual(self.request(kb,'delete-preview',{}).status_code,422)

    def test_shared_impact_requires_explicit_confirmation(self):
        kb,doc,_,ids=self.seed()
        other,_,_,_=self.seed()
        with self.connect() as conn:
            conn.execute('INSERT INTO knowledge_base_documents(knowledge_base_id,document_id) VALUES(%s,%s)',(other,doc))
        body={'claim_ids':[ids[0]]}
        preview=self.request(kb,'delete-preview',body).json()
        self.assertEqual(preview['other_affected_knowledge_bases'],[other])
        request={**body,'selection_hash':preview['selection_hash']}
        self.assertEqual(self.request(kb,'delete',request).status_code,409)
        self.assertEqual(self.request(kb,'delete',{**request,'allow_shared':True}).status_code,200)

    def test_pending_maintenance_blocks_deletion_and_cancelled_task_never_runs(self):
        kb,doc,_,ids=self.seed()
        job=str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute("INSERT INTO maintenance_jobs(id,knowledge_base_id,document_id,mode) VALUES(%s,%s,%s,'reparse')",(job,kb,doc))
        body={'claim_ids':ids}
        preview=self.request(kb,'delete-preview',body).json()
        request={**body,'selection_hash':preview['selection_hash']}
        self.assertEqual(self.request(kb,'delete',request).status_code,409)
        with self.connect() as conn:
            conn.execute("UPDATE maintenance_jobs SET status='cancelled' WHERE id=%s",(job,))
        with patch.object(self.tasks,'get_conn',side_effect=self.connect),patch.object(self.tasks.parse_document,'run') as parser:
            self.assertEqual(self.tasks.maintain_knowledge_base.run(job)['status'],'cancelled')
            parser.assert_not_called()
        self.assertEqual(self.request(kb,'delete',request).status_code,200)
        with patch.object(self.main,'get_conn',side_effect=self.connect):
            jobs=self.client.get(f'/knowledge-bases/{kb}/maintenance').json()['jobs']
        self.assertEqual(jobs[0]['status'],'cancelled')

    def test_cancellation_during_a_document_stops_the_next_and_is_not_overwritten(self):
        kb,doc,_,_=self.seed()
        _,second,_,_=self.seed()
        with self.connect() as conn:
            conn.execute('INSERT INTO knowledge_base_documents(knowledge_base_id,document_id) VALUES(%s,%s)',(kb,second))
        for scope in (doc,None):
            job=str(uuid.uuid4())
            with self.connect() as conn:
                conn.execute("INSERT INTO maintenance_jobs(id,knowledge_base_id,document_id,mode) VALUES(%s,%s,%s,'derived')",(job,kb,scope))
            def stop(_):
                with self.connect() as conn:
                    conn.execute("UPDATE maintenance_jobs SET status='cancelled' WHERE id=%s",(job,))
                return {'status':'done'}
            with patch.object(self.tasks,'get_conn',side_effect=self.connect),patch.object(self.tasks.refresh_derived,'run',side_effect=stop) as refresh:
                self.assertEqual(self.tasks.maintain_knowledge_base.run(job)['status'],'cancelled')
                self.assertEqual(refresh.call_count,1)
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT status FROM maintenance_jobs WHERE id=%s',(job,)).fetchone()['status'],'cancelled')
