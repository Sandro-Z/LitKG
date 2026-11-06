"""Dependency-free single-user HTTP service and static review frontend."""
import json
import os
import re
import threading
import http.client
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .engine import evaluate
from .llm import Client, NoRedirect
from .models import normalize, blocks, anchor, digest, public_claim, SCENARIOS, VERSION
from .sources import attach_litkg_sources, attach_uploaded_sources
from .store import Store

STATIC = Path(__file__).parent / 'static'
MAX_BODY = 64 * 1024 * 1024
MAX_LITKG_EXPORT = 128 * 1024 * 1024


class Service:
    def __init__(self, store, client_factory=Client):
        self.store, self.client_factory = store, client_factory
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.litkg_base = os.environ.get('QC_LITKG_URL', 'http://host.docker.internal:8080').rstrip('/')
        self.store.recover()

    def local_get(self, path, *, max_bytes=MAX_BODY):
        if not re.fullmatch(r'/knowledge-bases(?:\?limit=500|/[1-9][0-9]*/quality-export)', path):
            raise ValueError('无效 LitKG 导入路径')
        request = urllib.request.Request(self.litkg_base + path, headers={"Accept": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
                length = response.headers.get("Content-Length", "")
                if length.isdecimal() and int(length) > max_bytes:
                    raise ValueError("LITKG_EXPORT_TOO_LARGE")
                raw = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"LITKG_UPSTREAM_HTTP_{exc.code}") from None
        except urllib.error.URLError as exc:
            code = "LITKG_UPSTREAM_TIMEOUT" if isinstance(exc.reason, TimeoutError) else "LITKG_UPSTREAM_UNREACHABLE"
            raise ValueError(code) from None
        except TimeoutError:
            raise ValueError("LITKG_UPSTREAM_TIMEOUT") from None
        except (OSError, http.client.HTTPException):
            raise ValueError("LITKG_UPSTREAM_UNREACHABLE") from None
        if len(raw) > max_bytes:
            raise ValueError("LITKG_EXPORT_TOO_LARGE")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError("LITKG_UPSTREAM_INVALID_JSON") from None

    def submit(self, gid, mode):
        if mode not in {'automatic', 'llm'}:
            raise ValueError('mode 必须为 automatic 或 llm')
        if mode == 'llm' and not self.client_factory().enabled:
            raise ValueError('LLM_NOT_CONFIGURED')
        rid = self.store.start_run(gid, mode)
        def work():
            try:
                self.store.update_run(rid, 'running')
                graph = self.store.graph(gid)
                result = evaluate(graph, self.client_factory() if mode == 'llm' else None,
                                  lambda report: self.store.update_run(rid, 'running', report),
                                  max_chars=int(os.environ.get('QC_CONTEXT_CHARS', '48000')))
                self.store.update_run(rid, 'completed', result)
            except Exception:
                # Provider errors, imported data and local configuration never go into logs.
                self.store.update_run(rid, 'failed', error='REVIEW_FAILED')
        self.pool.submit(work)
        return self.store.run(rid)


def handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'LitKG-QC/1'
        def log_message(self, *args):
            pass

        def reply(self, value, code=200, content_type='application/json; charset=utf-8'):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                self.dispatch('GET')
            except KeyError:
                self.reply({'error': 'NOT_FOUND'}, 404)
            except ValueError as exc:
                self.reply({'error': str(exc)}, 400)
            except Exception:
                self.reply({'error': 'INTERNAL_ERROR'}, 500)

        def do_POST(self):
            try:
                # Same-origin browser mutations, and ordinary local API clients.
                origin = self.headers.get('Origin')
                if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get('Host'):
                    self.reply({'error': 'ORIGIN_REJECTED'}, 403)
                    return
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    self.reply({'error': 'JSON_REQUIRED'}, 415)
                    return
                self.dispatch('POST')
            except KeyError:
                self.reply({'error': 'NOT_FOUND'}, 404)
            except ValueError as exc:
                self.reply({'error': str(exc)}, 400)
            except Exception:
                self.reply({'error': 'INTERNAL_ERROR'}, 500)

        def body(self):
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_BODY:
                raise ValueError('请求大小须为1字节至64MiB')
            self.connection.settimeout(30)
            raw = self.rfile.read(size)
            try:
                value = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('非有限数值')))
            except (ValueError, UnicodeError):
                raise ValueError('INVALID_JSON') from None
            if not isinstance(value, dict):
                raise ValueError('请求须为JSON对象')
            return value

        def dispatch(self, method):
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path
            if method == 'GET' and path == '/health':
                self.reply({'status': 'ok', 'version': VERSION})
            elif method == 'GET' and path == '/api/config':
                client = service.client_factory()
                self.reply(dict(version=VERSION, llm_configured=client.enabled, model=client.model,
                                scenarios={k: {'name': v[0], 'levels': v[1]} for k, v in SCENARIOS.items()}))
            elif method == 'GET' and path == '/api/litkg/knowledge-bases':
                self.reply(service.local_get('/knowledge-bases?limit=500'))
            elif method == 'POST' and path == '/api/import/litkg':
                body = self.body()
                kid = body.get('knowledge_base_id')
                if not isinstance(kid, int) or isinstance(kid, bool) or kid < 1:
                    raise ValueError('knowledge_base_id须为正整数')
                raw = service.local_get(f'/knowledge-bases/{kid}/quality-export', max_bytes=MAX_LITKG_EXPORT)
                if body.get('scope'):
                    raw.setdefault('policy', {})['scope'] = body['scope']
                graph = normalize(raw, body.get('name', ''))
                source_dir = os.environ.get("QC_LITKG_SOURCE_DIR", str(Path(__file__).resolve().parents[1] / "data" / "uploads"))
                sources = attach_litkg_sources(graph, source_dir)
                self.reply({"id": service.store.add_graph(graph, raw), "original_sources": sources}, 201)
            elif method == 'POST' and path == '/api/graphs':
                body = self.body()
                payload = body.get('payload')
                if isinstance(payload, list):
                    payload = {'claims': payload}
                if not isinstance(payload, dict):
                    raise ValueError('payload须为JSON图谱对象或claim数组')
                for field in ('documents', 'ontology', 'scope'):
                    if field in body:
                        if field == 'documents':
                            payload.setdefault('documents', []).extend(body[field])
                        else:
                            payload.setdefault('policy', {})[field] = body[field]
                graph = normalize(payload, body.get('name', ''), body.get('mapping'))
                sources = attach_uploaded_sources(graph, body.get("source_files", []), body.get("source_complete"))
                self.reply({"id": service.store.add_graph(graph, payload), "original_sources": sources}, 201)
            elif method == 'GET' and path == '/api/graphs':
                self.reply({'graphs': service.store.list_graphs()})
            elif method == 'GET' and path.startswith('/api/runs/'):
                self.reply(service.store.run(path.split('/')[-1]))
            elif path.startswith('/api/graphs/'):
                pieces = path.strip('/').split('/')
                gid = pieces[2]
                graph = service.store.graph(gid)
                action = pieces[3] if len(pieces) > 3 else ''
                if method == 'GET' and not action:
                    self.reply(dict(id=gid, graph=graph, latest_run=service.store.latest_run(gid)))
                elif method == 'GET' and action == 'export':
                    self.reply(dict(graph=graph, run=service.store.latest_run(gid), human=service.store.sample_state(gid)))
                elif method == 'POST' and action == 'runs':
                    self.reply(service.submit(gid, self.body().get('mode', 'automatic')), 202)
                elif method == 'POST' and action == 'sample':
                    body = self.body()
                    self.reply(service.store.sample(gid, body.get('adaptive', True), body.get('new_round', False), body.get('name', '')))
                elif method == 'GET' and action == 'sample':
                    self.reply(service.store.sample_state(gid, urllib.parse.parse_qs(parts.query).get('round_id', [None])[0]))
                elif method == 'GET' and action == 'rounds':
                    self.reply(service.store.sample_state(gid))
                elif method == 'POST' and action == 'rounds':
                    body = self.body()
                    self.reply(service.store.sample(gid, body.get('adaptive', True), True, body.get('name', '')), 201)
                elif method == 'POST' and action == 'reviews':
                    body = self.body()
                    self.reply(service.store.review(gid, body.get('claim_id'), body.get('reviewer'), body.get('verdict'), body.get('note', ''), body.get('round_id')))
                elif method == 'GET' and action == 'claims' and len(pieces) == 5:
                    cid = urllib.parse.unquote(pieces[4])
                    claim = next((c for c in graph['claims'] if c['id'] == cid), None)
                    if claim is None:
                        raise KeyError()
                    corpus = blocks(graph)
                    evidence = []
                    latest = service.store.latest_run(gid)
                    extra = [r for a in (latest or {}).get('report', {}).get('assessments', []) if a['claim_id'] == cid for r in a.get('evidence', [])] if (latest or {}).get('report') else []
                    for ref in claim['source_refs'] + extra:
                        a = anchor(ref, corpus)
                        doc = str(ref.get('doc_id', ref.get('document_id')))
                        bid = str(ref.get('block_id', ref.get('chunk_id')))
                        block = corpus.get((doc, bid))
                        evidence.append(dict(reference=ref, anchor=a, block=block, verified=bool(a),
                                             warning=None if a else '引用尚未锚定；证据句本身不证明已读原文'))
                    # Fallback exposes saved source blocks even if references are absent.
                    if not evidence:
                        evidence = [dict(reference={}, anchor=None, block=b, verified=False, warning='无已验证锚点')
                                    for (doc, _), b in corpus.items() if claim.get('document_id') == doc]
                    self.reply(dict(claim=public_claim(claim, {n['id']: n for n in graph['nodes']}), evidence=evidence))
                else:
                    raise KeyError()
            elif method == 'GET' and path in {'/', '/app.js', '/style.css'}:
                filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                mime = {'/': 'text/html', '/app.js': 'text/javascript', '/style.css': 'text/css'}[path]
                self.reply((STATIC / filename).read_bytes(), content_type=mime + '; charset=utf-8')
            else:
                raise KeyError()
    return Handler


def main():
    os.umask(0o077)
    store = Store(os.environ.get('QC_DB_PATH', '/data/quality.sqlite3'))
    service = Service(store)
    server = ThreadingHTTPServer((os.environ.get('QC_BIND', '127.0.0.1'), int(os.environ.get('QC_PORT', '8090'))), handler(service))
    server.serve_forever()


if __name__ == '__main__':
    main()
