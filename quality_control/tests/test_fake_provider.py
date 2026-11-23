"""The browser tests' offline provider must keep working with the real LLM client."""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from quality_control.engine import evaluate
from quality_control.llm import Client
from quality_control.models import normalize
from quality_control.prompts import EXTRACT
from quality_control.tests.fake_provider import Handler, parser
from quality_control.tests.test_quality import fixture


class FakeProviderTests(unittest.TestCase):
    def setUp(self):
        self.paths = []
        paths = self.paths

        class Recording(Handler):
            def do_POST(self):
                paths.append(self.path)
                super().do_POST()

        server = ThreadingHTTPServer(('127.0.0.1', 0), Recording)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_port}/v1'
        environment = patch.dict(os.environ, {'QC_LLM_BASE_URL': self.base, 'QC_LLM_MODEL': 'fake',
                                              'no_proxy': '127.0.0.1', 'NO_PROXY': '127.0.0.1'})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('QC_LLM_API_KEY_FILE', 'QC_LLM_API_KEY', 'QC_LLM_MAX_CONCURRENCY'):
            os.environ.pop(name, None)

    def post(self, payload):
        request = urllib.request.Request(self.base + '/chat/completions', json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json'})
        return urllib.request.urlopen(request, timeout=10)

    def test_review_through_real_client_scores_fixture(self):
        client = Client()
        report = evaluate(normalize(fixture()), client)
        self.assertIsNone(report.get('failure_code'))
        self.assertEqual([], report['errors'])
        self.assertEqual('PASS', report['gate'])
        self.assertEqual(100, report['dimensions']['correctness'])
        self.assertEqual(100, report['dimensions']['completeness'])
        self.assertEqual(90, report['dimensions']['specialization'])
        self.assertEqual(95, report['dimensions']['granularity'])
        evidence = report['assessments'][0]['evidence']
        self.assertEqual([('d1', 'b1', 0, 18, 'Alpha treats Beta.')],
                         [(e['doc_id'], e['block_id'], e['start'], e['end'], e['quote']) for e in evidence])
        self.assertEqual(4, client.calls)
        self.assertEqual(['/v1/chat/completions'] * 4, self.paths)

    def test_streams_or_returns_json_citing_request_evidence_ids(self):
        data = {'scope': None, 'blocks': [
            {'doc_id': 'd0', 'id': 'b0', 'text': '[E000001] Gamma binds Delta.'},
            {'doc_id': 'd1', 'id': 'b1', 'text': '[E000002] Alpha treats Beta.'}]}
        messages = [{'role': 'system', 'content': EXTRACT + '\nEvidence protocol. Cite evidence_id values.'},
                    {'role': 'user', 'content': json.dumps(data)}]
        with self.post({'model': 'fake', 'messages': messages, 'stream': True,
                        'stream_options': {'include_usage': True}}) as response:
            self.assertIn('text/event-stream', response.headers['Content-Type'])
            events = [line[len('data: '):] for line in response.read().decode().splitlines()
                      if line.startswith('data: ')]
        self.assertEqual('[DONE]', events[-1])
        chunks = [json.loads(event) for event in events[:-1]]
        choices = [chunk['choices'][0] for chunk in chunks if chunk['choices']]
        self.assertEqual('stop', choices[-1]['finish_reason'])
        self.assertIn('usage', chunks[-1])
        streamed = json.loads(''.join(choice['delta'].get('content') or '' for choice in choices))
        with self.post({'model': 'fake', 'messages': messages}) as response:
            self.assertIn('application/json', response.headers['Content-Type'])
            plain = json.loads(json.load(response)['choices'][0]['message']['content'])
        self.assertEqual(streamed, plain)
        self.assertEqual([{'evidence_id': 'E000002', 'role': 'supports'}], plain['candidates'][0]['evidence'])

    def test_unknown_prompt_gets_http_400_and_default_bind_is_loopback(self):
        messages = [{'role': 'system', 'content': 'Another prompt'}, {'role': 'user', 'content': '{}'}]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post({'model': 'fake', 'messages': messages, 'stream': True})
        with caught.exception as error:
            self.assertEqual(400, error.code)
            self.assertEqual('messages', json.load(error)['error']['param'])
        defaults = parser().parse_args([])
        self.assertEqual(('127.0.0.1', 8010), (defaults.host, defaults.port))


if __name__ == '__main__':
    unittest.main()
