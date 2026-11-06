"""Offline synthetic OpenAI-compatible provider for browser regression tests only."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from quality_control.tests.test_quality import FakeLLM
from quality_control.prompts import FIDELITY, EXTRACT, RUBRIC
from quality_control.rubrics import RUBRICS


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        raw = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        prompt = raw['messages'][0]['content']
        data = json.loads(raw['messages'][1]['content'])
        response = FakeLLM().ask(prompt, data)
        if prompt == EXTRACT:
            # Exercise endpoint noun proxies when optional arrays are omitted.
            response = {'candidates': response['candidates']}
        elif prompt == FIDELITY:
            response['assessments'][0].pop('specialization')
            response['assessments'][0].pop('granularity')
        elif prompt == RUBRIC:
            for field in ('specialization', 'granularity'):
                response[field] = {k: {'score': 90 if field == 'specialization' else 95,
                                       'rationale': 'Synthetic source-backed fixture'} for k in RUBRICS[field]}
        content = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(response)}}],
                              'usage': {'total_tokens': 1}}).encode()
        self.send_response(200);self.send_header('Content-Type', 'application/json');self.end_headers();self.wfile.write(content)


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8010), Handler).serve_forever()
