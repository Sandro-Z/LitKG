"""Offline synthetic OpenAI-compatible provider for browser regression tests only.

It speaks the part of POST /v1/chat/completions that quality_control.llm.Client uses:
the review stage is recognised by the system prompt's prefix (the client appends an
evidence protocol to it), the fixture answers from FakeLLM cite the request's
[E000001]-style evidence ids, and the reply is an SSE stream when the request asks
for one (plain JSON otherwise). A request it cannot answer gets an HTTP 400 error
object instead of a dropped connection.

    python3 -m quality_control.tests.fake_provider            # 127.0.0.1:8010
    python3 -m quality_control.tests.fake_provider --host 0.0.0.0   # isolated test network only
"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from quality_control.prompts import EXTRACT, FIDELITY, MATCH, RUBRIC
from quality_control.rubrics import RUBRICS
from quality_control.tests.test_quality import FakeLLM

STAGES = (EXTRACT, FIDELITY, MATCH, RUBRIC)
EVIDENCE_MARK = re.compile(r'\[(E\d{6})\] ')
USAGE = {'total_tokens': 1}


def stage_prompt(system):
    """The fixed stage prompt the system message starts with, or None."""
    if not isinstance(system, str):
        return None
    return next((prompt for prompt in STAGES if system.startswith(prompt)), None)


def evidence_index(data):
    """Map each evidence id the client wrote into the blocks to ((doc_id, block_id), passage)."""
    index = {}
    for block in (data.get('blocks') or []) if isinstance(data, dict) else []:
        if not isinstance(block, dict) or not isinstance(block.get('text'), str):
            continue
        key = (str(block.get('doc_id')), str(block.get('block_id', block.get('id'))))
        pieces = EVIDENCE_MARK.split(block['text'])
        for identifier, passage in zip(pieces[1::2], pieces[2::2]):
            index[identifier] = (key, passage)
    return index


def cite(value, index):
    """Replace FakeLLM's literal quotes with the id of the passage that contains them.

    A quote found in no passage stays literal, so the engine's own anchoring decides.
    """
    if isinstance(value, list):
        return [cite(item, index) for item in value]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get('quote'), str) and 'doc_id' in value:
        key = (str(value['doc_id']), str(value.get('block_id')))
        found = [i for i, (k, passage) in index.items() if value['quote'] in passage]
        found.sort(key=lambda i: index[i][0] != key)
        if not found:
            return value
        cited = {'evidence_id': found[0]}
        if 'role' in value:
            cited['role'] = value['role']
        return cited
    return {key: cite(item, index) for key, item in value.items()}


def answer(prompt, data):
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
    return cite(response, evidence_index(data))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream(self, content, include_usage):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        size = len(content) // 3 + 1
        events = [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]}]
        events += [{'choices': [{'index': 0, 'delta': {'content': content[i:i + size]}, 'finish_reason': None}]}
                   for i in range(0, len(content), size)]
        events.append({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
        if include_usage:
            events.append({'choices': [], 'usage': USAGE})
        for event in events:
            self.wfile.write(b'data: ' + json.dumps(event, ensure_ascii=False).encode() + b'\n\n')
        self.wfile.write(b'data: [DONE]\n\n')

    def do_POST(self):
        if self.path.split('?')[0].rstrip('/').rsplit('/', 2)[-2:] != ['chat', 'completions']:
            self.reply(404, {'error': {'message': 'Unknown endpoint', 'code': 'not_found'}})
            return
        try:
            request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            messages = request['messages']
            prompt = stage_prompt(messages[0]['content'])
            if prompt is None:
                raise ValueError('unknown review stage')
            content = json.dumps(answer(prompt, json.loads(messages[1]['content'])), ensure_ascii=False)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            self.reply(400, {'error': {'message': 'The fake provider cannot answer this request',
                                       'param': 'messages'}})
            return
        if request.get('stream'):
            self.stream(content, (request.get('stream_options') or {}).get('include_usage'))
        else:
            self.reply(200, {'choices': [{'index': 0, 'finish_reason': 'stop',
                                          'message': {'role': 'assistant', 'content': content}}],
                             'usage': USAGE})


def parser():
    result = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    result.add_argument('--host', default='127.0.0.1',
                        help='interface to bind (default 127.0.0.1; 0.0.0.0 only on an isolated test network)')
    result.add_argument('--port', type=int, default=8010, help='port to listen on (default 8010)')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
