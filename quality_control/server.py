"""Dependency-free single-user HTTP service and static review frontend."""
import json
import logging
import sqlite3
import time
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
from .store import ActiveGraphRunError, Store

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
                if self.store.run_cancel_requested(rid):
                    self.store.update_run(rid, 'cancelled', error='RUN_CANCELLED')
                    return
                self.store.update_run(rid, 'running')
                graph = self.store.graph(gid)
                client = self.client_factory() if mode == 'llm' else None
                last_checkpoint = time.monotonic() - 5

                def stop_reason():
                    if self.store.run_cancel_requested(rid):
                        return 'RUN_CANCELLED'
                    return None

                if client is not None:
                    client.cancel_check = stop_reason

                def checkpoint(report):
                    nonlocal last_checkpoint
                    now = time.monotonic()
                    if now - last_checkpoint >= 5:
                        self.store.update_run(rid, 'running', report)
                        last_checkpoint = now

                result = evaluate(graph, client, checkpoint, max_chars=int(os.environ.get('QC_CONTEXT_CHARS', '48000')), stop_reason=stop_reason)
                failure = result.get('failure_code')
                self.store.update_run(rid, 'failed' if failure else 'completed', result, error=failure)
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
                if urllib.parse.parse_qs(parts.query).get('view') == ['overview']:
                    self.reply({'graphs': service.store.list_graph_overviews()})
                    return
                self.reply({'graphs': service.store.list_graphs()})
            elif method == 'POST' and re.fullmatch(r'/api/runs/[^/]+/cancel', path):
                self.body()
                run_id = path.split('/')[3]
                if service.store.request_run_cancel(run_id):
                    self.reply({'cancel_requested': True}, 202)
                else:
                    self.reply({'error': 'RUN_NOT_ACTIVE'}, 409)
            elif method == 'GET' and path.startswith('/api/runs/'):
                self.reply(service.store.run(path.split('/')[-1]))
            elif path.startswith('/api/graphs/'):
                pieces = path.strip('/').split('/')
                gid = pieces[2]
                action = pieces[3] if len(pieces) > 3 else ''
                if method == 'POST' and action == 'delete' and len(pieces) == 4:
                    if self.body().get('confirm_graph_id') != gid:
                        raise ValueError('GRAPH_DELETE_CONFIRMATION_REQUIRED')
                    try:
                        service.store.delete_graph(gid)
                    except ActiveGraphRunError:
                        self.reply({'error': 'GRAPH_REVIEW_ACTIVE'}, 409)
                    except sqlite3.Error as exc:
                        code = getattr(exc, 'sqlite_errorcode', 0) & 255
                        logging.getLogger(__name__).warning('graph delete failed (sqlite code %d)', code)
                        if code == sqlite3.SQLITE_FULL:
                            self.reply({'error': 'GRAPH_DELETE_DISK_FULL'}, 507)
                        elif code in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH):
                            self.reply({'error': 'GRAPH_DELETE_DATABASE_READ_ONLY'}, 500)
                        elif code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                            self.reply({'error': 'GRAPH_DELETE_BUSY'}, 503)
                        elif code == sqlite3.SQLITE_CONSTRAINT:
                            self.reply({'error': 'GRAPH_DELETE_CONFLICT'}, 409)
                        else:
                            self.reply({'error': 'GRAPH_DELETE_STORAGE_ERROR'}, 500)
                    else:
                        self.reply({'deleted': gid})
                    return
                if method == 'GET' and action == 'report':
                    offset = urllib.parse.parse_qs(parts.query).get('offset', ['0'])[0]
                    if not re.fullmatch(r'[0-9]{1,8}', offset):
                        raise ValueError('INVALID_REPORT_OFFSET')
                    self.reply({'run': service.store.latest_run_page(gid, int(offset))})
                    return
                if method == 'GET' and not action:
                    graph = service.store.graph(gid)
                    self.reply(dict(id=gid, graph=graph, latest_run=service.store.latest_run(gid)))
                elif method == 'GET' and action == 'export':
                    graph = service.store.graph(gid)
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
                    claim, graph, extra = service.store.claim_context(gid, cid)
                    corpus = blocks(graph)
                    evidence = []
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


def configure_sqlite_temp(db_path):
    """Keep SQLite spill files off the container's small temporary filesystem."""
    configured = os.environ.get('SQLITE_TMPDIR')
    if configured:
        temp_dir = Path(configured)
    else:
        temp_dir = Path(db_path).resolve().parent / '.sqlite-tmp'
        temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if temp_dir.is_symlink() or not temp_dir.is_dir() or not os.access(temp_dir, os.W_OK | os.X_OK):
        raise RuntimeError('SQLITE_TEMP_DIRECTORY_UNWRITABLE')
    os.environ['SQLITE_TMPDIR'] = str(temp_dir)


def main():
    os.umask(0o077)
    db_path = os.environ.get('QC_DB_PATH', '/data/quality.sqlite3')
    configure_sqlite_temp(db_path)
    store = Store(db_path)
    service = Service(store)
    server = ThreadingHTTPServer((os.environ.get('QC_BIND', '127.0.0.1'), int(os.environ.get('QC_PORT', '8090'))), handler(service))
    server.serve_forever()


if __name__ == '__main__':
    main()
