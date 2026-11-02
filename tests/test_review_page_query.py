import unittest
from app.review_batch_query import ReviewBatchCursor,rewrite_review_query

SQL="""SELECT claim.*, COALESCE((SELECT jsonb_agg(jsonb_build_object(
 'id',ri.id,'kind',ri.kind,'status',ri.status,'note',ri.note) ORDER BY ri.id)
 FROM review_items ri WHERE (ri.detail->>'claim_id'=claim.id::text
 OR ri.detail->>'chunk_id'=claim.chunk_id::text)), '[]'::jsonb) AS reviews
 FROM claims claim LEFT JOIN documents d ON d.id=claim.document_id
 WHERE claim.document_id=ANY(%s) ORDER BY claim.id LIMIT %s OFFSET %s"""

class BatchTests(unittest.TestCase):
    def test_preserves_parameters_and_filters(self):
        result=rewrite_review_query(SQL)
        self.assertIsNotNone(result)
        self.assertEqual(result.count('%s'),SQL.count('%s'))
        self.assertIn('WHERE claim.document_id=ANY(%s)',result)
        self.assertIn("ri.detail->>'chunk_id'=claim.chunk_id::text",result)
        self.assertIn('ORDER BY ri.id',result)

    def test_unsupported_queries_are_unchanged(self):
        self.assertIsNone(rewrite_review_query('SELECT COUNT(*) FROM claims'))
        self.assertIsNone(rewrite_review_query(SQL.replace('ORDER BY claim.id LIMIT','ORDER BY claim.id DESC LIMIT')))
        self.assertIsNone(rewrite_review_query(SQL.replace("'note',ri.note","'note',claim.subject_text")))
        self.assertIsNone(rewrite_review_query(SQL.replace('WHERE (ri.detail','WHERE ri.status=%s AND (ri.detail')))

    def test_restores_reviews_and_removes_internal_field(self):
        class Fake:
            def execute(self,q,p):self.query=q;self.params=p
            def fetchall(self):return [
                {'id':1,'reviews':[],'__litkg_reviews_payload':[{'id':10,'note':'same'},{'id':11,'note':'other'}]},
                {'id':2,'reviews':[],'__litkg_reviews_payload':[{'id':10,'note':'same'}]}]
        raw=Fake();cursor=ReviewBatchCursor(raw);params=([1],2,1)
        cursor.execute(SQL,params);rows=cursor.fetchall()
        self.assertEqual(raw.params,params)
        self.assertEqual([r['id'] for r in rows[0]['reviews']],[10,11])
        self.assertEqual([r['id'] for r in rows[1]['reviews']],[10])
        self.assertEqual(set(rows[0]),{'id','reviews'})
        rows[0]['reviews'][0]['note']='changed'
        self.assertEqual(rows[1]['reviews'][0]['note'],'same')

    def test_empty_page(self):
        class Fake:
            def execute(self,q,p):pass
            def fetchall(self):return []
        c=ReviewBatchCursor(Fake());c.execute(SQL,([],5,0));self.assertEqual(c.fetchall(),[])

    def test_first_page_and_keyset_keep_original_fast_query(self):
        class Fake:
            def execute(self,q,p):self.query=q
        raw=Fake();cursor=ReviewBatchCursor(raw);cursor.execute(SQL,([1],5,0))
        self.assertEqual(raw.query,SQL);self.assertFalse(cursor.optimized)

if __name__=='__main__':unittest.main()
