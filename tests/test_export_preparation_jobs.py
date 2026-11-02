import csv
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from frontend.export_jobs import ExportJobs,request_timeout

class JobsTests(unittest.TestCase):
    def test_export_timeouts_and_invalid_configuration(self):
        with patch.dict(os.environ,{'LITKG_CONNECT_TIMEOUT':'10','LITKG_EXPORT_READ_TIMEOUT':'1200','LITKG_API_READ_TIMEOUT':'180'}):
            self.assertEqual(request_timeout('/knowledge-bases/26/claims/export'),(10,1200))
            self.assertEqual(request_timeout('/knowledge-bases/26/claims'),(10,180))
        with patch.dict(os.environ,{'LITKG_CONNECT_TIMEOUT':'synthetic_private_value','LITKG_EXPORT_READ_TIMEOUT':'inf'}):
            self.assertEqual(request_timeout('/claims/export'),(10,1200))

    def test_nonblocking_and_persists_ready_artifacts(self):
        gate=threading.Event()
        payload={'claims':[{'id':1,'participants':[{'role':'治疗组','name':'A, B'}],'note':'line1\nline2'}], 'total':1,'exported_count':1,'document_count':1}
        def fetch(path,params):gate.wait(2);return payload
        with tempfile.TemporaryDirectory() as tmp:
            jobs=ExportJobs(tmp)
            job=jobs.submit(fetch,'/synthetic',{},'claims','claims-26')
            self.assertIn(jobs.status(job)['status'],{'queued','running'})
            gate.set();jobs.jobs[job]['future'].result(3)
            state=jobs.status(job);self.assertEqual(state['status'],'ready')
            first,second=jobs.files(job)
            self.assertEqual(json.loads(first.read_text(encoding='utf-8')),payload)
            rows=list(csv.DictReader(io.StringIO(second.read_text(encoding='utf-8-sig'))))
            self.assertEqual(json.loads(rows[0]['participants']),payload['claims'][0]['participants'])
            self.assertEqual(rows[0]['note'],'line1\nline2')
            self.assertEqual(jobs.status(job)['rows'],1)
            jobs.pool.shutdown()

    def test_bad_count_never_exposes_download_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs=ExportJobs(tmp)
            job=jobs.submit(lambda *a,**k:{'facts':[],'total':2,'exported_count':0},'/synthetic',{},'facts','facts-26')
            jobs.jobs[job]['future'].result(3)
            self.assertEqual(jobs.status(job)['status'],'failed')
            with self.assertRaises(ValueError):jobs.files(job)
            jobs.pool.shutdown()

    def test_failures_do_not_display_original_exception(self):
        def fetch(*a,**k):raise RuntimeError('synthetic_private_value')
        with tempfile.TemporaryDirectory() as tmp:
            jobs=ExportJobs(tmp);job=jobs.submit(fetch,'/synthetic',{},'claims','claims')
            jobs.jobs[job]['future'].result(3)
            self.assertNotIn('synthetic_private_value',str(jobs.status(job)))
            self.assertEqual(jobs.status(job)['error_code'],'EXPORT_PREPARE_FAILED')
            jobs.pool.shutdown()

if __name__=='__main__':unittest.main()
