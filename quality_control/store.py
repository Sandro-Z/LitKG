"""SQLite snapshots, durable run ledger and append-only human decisions."""
from functools import lru_cache
import json
import math
import os
import random
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .scoring import human_metrics, latest_votes, consensus, combine_human


def now():
    return datetime.now(timezone.utc).isoformat()


class ActiveGraphRunError(RuntimeError):
    pass


def _require_graph(db, graph_id):
    if db.execute('SELECT 1 FROM graphs WHERE id=?', (graph_id,)).fetchone() is None:
        raise KeyError('GRAPH_NOT_FOUND')


def _report_overview(report):
    if not isinstance(report, dict):
        return None
    result = {}
    for key, value in report.items():
        if value is None or type(value) in (bool, int, float) or isinstance(value, str) and len(value) <= 256:
            result[key] = value
        elif key in ('calibration', 'dimension_details', 'dimension_status', 'dimension_summary', 'dimensions', 'failure_summary', 'human', 'machine_scenarios', 'manual_summary', 'metrics', 'progress', 'protocol', 'scenarios', 'score_complete') and isinstance(value, dict):
            if len(json.dumps(value, ensure_ascii=False)) <= 24000:
                result[key] = value
    return result


def _report_page(report, offset):
    if not isinstance(report, dict):
        return report
    size = 50
    assessments = report.get('assessments') or []
    errors = report.get('errors') or []
    invalid = {str(e['claim_id']) for e in errors
               if e.get('code') == 'INVALID_ASSESSMENT' and e.get('claim_id') is not None}
    failed_claims = {str(a['claim_id']) for a in assessments
                     if a.get('verdict') == 'INSUFFICIENT_EVIDENCE'
                     and a.get('rationale') == '无效标签、无效证据或不满足判定前置条件'
                     and str(a.get('claim_id')) in invalid}
    distinct = {str(a['claim_id']) for a in assessments if a.get('claim_id') is not None}
    failed_blocks = {(str(e['doc_id']), str(e['block_id'])) for e in errors
                     if e.get('code') == 'INVALID_OR_UNREVIEWED_BLOCK'
                     and e.get('doc_id') is not None and e.get('block_id') is not None}
    offset = min(offset, max(0, (len(assessments) - 1) // size * size))
    page = assessments[offset:offset + size]
    page_ids = {str(a.get('claim_id')) for a in page}
    result = {}
    for key, value in report.items():
        if key == 'assessments':
            result[key] = page
        elif key == 'errors':
            result[key] = errors[:80]
        elif isinstance(value, list):
            if value and isinstance(value[0], dict) and 'claim_id' in value[0]:
                result[key] = [v for v in value if str(v.get('claim_id')) in page_ids][:100]
            else:
                result[key] = value[:80]
        elif key == 'ontology' and isinstance(value, dict):
            result[key] = {k: v[:80] if isinstance(v, list) else v for k, v in value.items()}
        else:
            result[key] = value
    result['preview'] = dict(offset=offset, size=size, assessments=len(assessments),
                             reviewed=len(distinct - failed_claims), errors=len(errors),
                             failed_blocks=len(failed_blocks),
                             failed_claim_ids=sorted(failed_claims & page_ids))
    return result


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS graphs(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL, payload TEXT NOT NULL, original TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, graph_id TEXT NOT NULL REFERENCES graphs(id),
                    status TEXT NOT NULL, mode TEXT NOT NULL, report TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(graph_id) WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS samples(graph_id TEXT PRIMARY KEY REFERENCES graphs(id), seed TEXT NOT NULL,
                    claim_ids TEXT NOT NULL, uncertainty REAL NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, graph_id TEXT NOT NULL REFERENCES graphs(id),
                    claim_id TEXT NOT NULL, reviewer TEXT NOT NULL, verdict TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS review_rounds(id TEXT PRIMARY KEY, graph_id TEXT NOT NULL REFERENCES graphs(id),
                    name TEXT NOT NULL, seed TEXT NOT NULL, claim_ids TEXT NOT NULL, uncertainty REAL NOT NULL, created_at TEXT NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            db.execute("""CREATE TABLE IF NOT EXISTS graph_overview (
                id TEXT PRIMARY KEY REFERENCES graphs(id) ON DELETE CASCADE,
                name TEXT NOT NULL, snapshot_hash TEXT NOT NULL, created_at TEXT NOT NULL, claim_count INTEGER NOT NULL, document_count INTEGER NOT NULL
            )""")
            db.execute('CREATE INDEX IF NOT EXISTS graph_overview_created ON graph_overview(created_at DESC)')
            for row in db.execute("""SELECT id, name, snapshot_hash, created_at, payload FROM graphs
                                     WHERE NOT EXISTS (SELECT 1 FROM graph_overview WHERE id=graphs.id)"""):
                graph = json.loads(row['payload'])
                db.execute('INSERT INTO graph_overview VALUES (?, ?, ?, ?, ?, ?)',
                           (row['id'], row['name'], row['snapshot_hash'], row['created_at'], len(graph['claims']), len(graph['documents'])))
            run_columns = {row['name'] for row in db.execute('PRAGMA table_info(runs)')}
            if 'report_overview' not in run_columns:
                db.execute('ALTER TABLE runs ADD COLUMN report_overview TEXT')
                for row in db.execute('SELECT id, report FROM runs WHERE report IS NOT NULL'):
                    overview = _report_overview(json.loads(row['report']))
                    db.execute('UPDATE runs SET report_overview=? WHERE id=?',
                               (json.dumps(overview, ensure_ascii=False), row['id']))
            db.execute('CREATE INDEX IF NOT EXISTS runs_graph_created ON runs(graph_id, created_at DESC)')
            db.execute('CREATE INDEX IF NOT EXISTS reviews_graph ON reviews(graph_id)')
            db.execute('CREATE INDEX IF NOT EXISTS review_rounds_graph ON review_rounds(graph_id, created_at)')
            if 'cancel_requested' not in {row['name'] for row in db.execute('PRAGMA table_info(runs)')}:
                db.execute('ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0')
            if 'round_id' not in {r['name'] for r in db.execute('PRAGMA table_info(reviews)')}:
                db.execute('ALTER TABLE reviews ADD COLUMN round_id TEXT')
            for sample in db.execute('SELECT * FROM samples').fetchall():
                existing = db.execute('SELECT id FROM review_rounds WHERE graph_id=? ORDER BY created_at LIMIT 1', (sample['graph_id'],)).fetchone()
                rid = existing['id'] if existing else 'legacy-' + sample['graph_id']
                if not existing:
                    db.execute('INSERT INTO review_rounds VALUES (?,?,?,?,?,?,?)',
                               (rid, sample['graph_id'], '第1轮（历史样本）', sample['seed'], sample['claim_ids'],
                                sample['uncertainty'], sample['created_at']))
                db.execute('UPDATE reviews SET round_id=? WHERE graph_id=? AND round_id IS NULL', (rid, sample['graph_id']))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_graph(self, graph, original):
        gid = uuid.uuid4().hex
        with self.connect() as db:
            created_at = now()
            db.execute('INSERT INTO graphs VALUES (?,?,?,?,?,?)',
                       (gid, graph['name'], graph['snapshot_hash'], json.dumps(graph, ensure_ascii=False),
                        json.dumps(original, ensure_ascii=False), created_at))
            db.execute('INSERT INTO graph_overview VALUES (?, ?, ?, ?, ?, ?)',
                       (gid, graph['name'], graph['snapshot_hash'], created_at, len(graph['claims']), len(graph['documents'])))
        return gid

    def claim_context(self, graph_id, claim_id):
        with self.connect() as db:
            row = db.execute("""SELECT c.value AS claim, json_extract(g.payload, '$.nodes') AS nodes
                                FROM graphs AS g, json_each(g.payload, '$.claims') AS c
                                WHERE g.id=? AND CAST(json_extract(c.value, '$.id') AS TEXT)=?""",
                             (graph_id, claim_id)).fetchone()
            if row is None:
                raise KeyError('CLAIM_NOT_FOUND')
            last = db.execute('SELECT mode, report FROM runs WHERE graph_id=? ORDER BY created_at DESC LIMIT 1',
                              (graph_id,)).fetchone()
            claim = json.loads(row['claim'])
            nodes = json.loads(row['nodes']) if row['nodes'] else []
            if last and last['mode'] != 'ontology':
                report = json.loads(last['report']) if last['report'] else None
            else:
                latest = self.latest_run(graph_id)
                report = (latest or {}).get('report')
            extra = [ref for item in (report or {}).get('assessments', [])
                     if str(item.get('claim_id')) == claim_id for ref in item.get('evidence', [])]
            ids = {str(ref.get('doc_id', ref.get('document_id'))) for ref in claim['source_refs'] + extra if isinstance(ref, dict)}
            if claim.get('document_id') is not None:
                ids.add(str(claim['document_id']))
            docs = db.execute("""SELECT d.value FROM graphs AS g, json_each(g.payload, '$.documents') AS d
                                 WHERE g.id=? AND CAST(json_extract(d.value, '$.id') AS TEXT)
                                     IN (SELECT value FROM json_each(?))""",
                              (graph_id, json.dumps(sorted(ids)))).fetchall() if ids else []
        return claim, {'nodes': nodes, 'documents': [json.loads(d[0]) for d in docs]}, extra

    def graph(self, gid):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM graphs WHERE id=?', (gid,)).fetchone()
        if not row:
            raise KeyError('GRAPH_NOT_FOUND')
        return json.loads(row['payload'])

    def delete_graph(self, graph_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _require_graph(db, graph_id)
            active = db.execute(
                "SELECT 1 FROM runs WHERE graph_id=? AND status IN ('queued','running')",
                (graph_id,),
            ).fetchone()
            if active:
                raise ActiveGraphRunError()
            db.execute('DELETE FROM reviews WHERE graph_id=?', (graph_id,))
            db.execute('DELETE FROM review_rounds WHERE graph_id=?', (graph_id,))
            db.execute('DELETE FROM samples WHERE graph_id=?', (graph_id,))
            db.execute('DELETE FROM runs WHERE graph_id=?', (graph_id,))
            db.execute('DELETE FROM graphs WHERE id=?', (graph_id,))
        self._graph_report_page.cache_clear()

    def list_graphs(self):
        with self.connect() as db:
            rows = db.execute('SELECT id,name,snapshot_hash,created_at FROM graphs ORDER BY created_at DESC').fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                graph = self.graph(item['id'])
                item.update(claim_count=len(graph['claims']), document_count=len(graph['documents']),
                            latest_run=self.latest_run(item['id']))
            except KeyError:
                with self.connect() as db:
                    if db.execute('SELECT 1 FROM graphs WHERE id=?', (item['id'],)).fetchone() is not None:
                        raise
                continue
            result.append(item)
        return result

    def list_graph_overviews(self):
        with self.connect() as db:
            rows = db.execute("""SELECT g.id, g.name, g.snapshot_hash, g.created_at, g.claim_count, g.document_count, r.id AS run_id, r.graph_id AS run_graph_id, r.status AS run_status, r.mode AS run_mode, r.error AS run_error, r.created_at AS run_created_at, r.updated_at AS run_updated_at, r.cancel_requested AS run_cancel_requested, r.report_overview,
                     COALESCE((SELECT MAX(id) FROM reviews WHERE graph_id=g.id), 0) AS review_revision,
                     (SELECT COUNT(*) FROM review_rounds WHERE graph_id=g.id) AS round_count
                     FROM graph_overview AS g LEFT JOIN runs AS r ON r.id=(
                         SELECT id FROM runs WHERE graph_id=g.id ORDER BY created_at DESC LIMIT 1
                     ) ORDER BY g.created_at DESC""").fetchall()
        result = []
        for row in rows:
            graph = {key: row[key] for key in ('id', 'name', 'snapshot_hash', 'created_at', 'claim_count', 'document_count')}
            graph['review_revision'] = row['review_revision']
            graph['round_count'] = row['round_count']
            latest = {key: row['run_' + key] for key in ('id', 'graph_id', 'status', 'mode', 'error', 'created_at', 'updated_at', 'cancel_requested')} if row['run_id'] else None
            if row['review_revision'] or (latest and latest['mode'] == 'ontology'):
                latest = self._graph_report_page(graph['id'], row['run_id'], row['run_updated_at'],
                                                 row['review_revision'], row['round_count'], row['run_cancel_requested'], 0)
                if latest:
                    latest = {**latest, 'report': _report_overview(latest.get('report'))}
            elif latest:
                latest['report'] = json.loads(row['report_overview']) if row['report_overview'] else None
            graph['latest_run'] = latest
            result.append(graph)
        return result

    def latest_run_page(self, graph_id, offset):
        with self.connect() as db:
            _require_graph(db, graph_id)
            row = db.execute('SELECT id, updated_at, cancel_requested FROM runs WHERE graph_id=? ORDER BY created_at DESC LIMIT 1',
                             (graph_id,)).fetchone()
            reviews = db.execute('SELECT COALESCE(MAX(id), 0) FROM reviews WHERE graph_id=?', (graph_id,)).fetchone()[0]
            rounds = db.execute('SELECT COUNT(*) FROM review_rounds WHERE graph_id=?', (graph_id,)).fetchone()[0]
        return self._graph_report_page(graph_id, row['id'] if row else None, row['updated_at'] if row else None,
                                       reviews, rounds, row['cancel_requested'] if row else 0, offset)

    @lru_cache(maxsize=16)
    def _graph_report_page(self, graph_id, run_id, updated_at, review_revision, round_count, cancel_requested, offset):
        if not run_id and not review_revision:
            return None
        if not review_revision and not round_count and run_id:
            with self.connect() as db:
                row = db.execute('SELECT * FROM runs WHERE id=? AND graph_id=?', (run_id, graph_id)).fetchone()
            if row is None:
                raise KeyError('RUN_NOT_FOUND')
            result = dict(row)
            if result['mode'] != 'ontology':
                result.pop('report_overview', None)
                result['report'] = _report_page(json.loads(result['report']), offset) if result['report'] else None
                return result
        result = self.latest_run(graph_id)
        return {**result, 'report': _report_page(result.get('report'), offset)} if result else None

    def start_run(self, gid, mode):
        rid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _require_graph(db, gid)
            try:
                db.execute('INSERT INTO runs (id, graph_id, status, mode, report, error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)', (rid, gid, 'queued', mode, None, None, now(), now()))
            except sqlite3.IntegrityError:
                raise ValueError('已有待执行或运行中的评审') from None
        return rid

    def request_run_cancel(self, run_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError('RUN_NOT_FOUND')
            if row['status'] not in ('queued', 'running'):
                return False
            db.execute('UPDATE runs SET cancel_requested=1 WHERE id=?', (run_id,))
            return True

    def run_cancel_requested(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT cancel_requested FROM runs WHERE id=?', (run_id,)).fetchone()
            return row is not None and bool(row['cancel_requested'])

    def update_run(self, rid, status, report=None, error=None):
        with self.connect() as db:
            if status not in ('queued', 'running'):
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT cancel_requested FROM runs WHERE id=?', (rid,)).fetchone()
                if row is not None and row['cancel_requested']:
                    status, error = 'cancelled', 'RUN_CANCELLED'
                    if report is not None:
                        report.update(failure_code=error, complete=False)
            db.execute('UPDATE runs SET status=?,report=COALESCE(?,report), report_overview=COALESCE(?, report_overview),error=?,updated_at=? WHERE id=?',
                        (status, json.dumps(report, ensure_ascii=False) if report is not None else None, json.dumps(_report_overview(report), ensure_ascii=False) if report is not None else None, error, now(), rid))

    def run(self, rid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
        if not row:
            raise KeyError('RUN_NOT_FOUND')
        result = dict(row)
        result.pop('report_overview', None)
        result['report'] = json.loads(result['report']) if result['report'] else None
        report = result['report']
        if report and 'dimensions' in report:
            graph = self.graph(result['graph_id'])
            # Local ontology reruns must not erase a completed evidence review.
            if result['mode'] == 'automatic':
                with self.connect() as db:
                    prior = db.execute("SELECT id,report FROM runs WHERE graph_id=? AND mode='llm' AND status='completed' AND created_at<=? ORDER BY created_at DESC LIMIT 1",
                                       (result['graph_id'], result['created_at'])).fetchone()
                if prior and prior['report']:
                    evidence_report = json.loads(prior['report'])
                    if 'dimensions' in evidence_report:
                        evidence_report['ontology'] = report['ontology']
                        evidence_report['evidence_run_id'] = prior['id']
                        evidence_report['ontology_run_id'] = rid
                        report = evidence_report
            from .engine import finalize
            if all(k in report for k in ('assessments', 'candidates', 'matches', 'ontology', 'stages')):
                complete = bool(graph['documents']) and all(d.get('parsing_status') == 'complete' and d.get('blocks') for d in graph['documents'])
                finalize(graph, report, complete, bool(graph['policy'].get('scope')))
            result['report'] = combine_human(report, self.sample_state(result['graph_id']), graph)
        return result

    def latest_run(self, gid):
        with self.connect() as db:
            row = db.execute('SELECT id FROM runs WHERE graph_id=? ORDER BY created_at DESC LIMIT 1', (gid,)).fetchone()
        if row:
            return self.run(row['id'])
        state = self.sample_state(gid)
        if not state['reviews']:
            return None
        # Human-only work still has a score projection; no paid run is created.
        from .engine import evaluate
        graph = self.graph(gid)
        report = combine_human(evaluate(graph), state, graph)
        return dict(id=None, graph_id=gid, mode='human', status='completed', report=report,
                    error=None, created_at=state['history'][0]['created_at'], updated_at=state['history'][-1]['created_at'])

    def recover(self):
        # Never silently restart paid requests after a container restart.
        with self.connect() as db:
            db.execute("UPDATE runs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'interrupted' END,error=CASE WHEN cancel_requested=1 THEN 'RUN_CANCELLED' ELSE 'SERVICE_RESTARTED' END,updated_at=? WHERE status IN ('queued','running')", (now(),))

    def sample(self, gid, adaptive=True, new_round=False, name=''):
        state = self.sample_state(gid)
        if state['sample'] and not new_round:
            return state
        with self.connect() as db:
            _require_graph(db, gid)
            claim_ids = [row[0] for row in db.execute("""SELECT json_extract(c.value, '$.id')
                         FROM graphs AS g, json_each(g.payload, '$.claims') AS c WHERE g.id=?""",
                         (gid,))]
        latest = self.latest_run(gid)
        uncertainty = (latest.get('report') or {}).get('uncertainty', 1) if latest else 1
        n = min(len(claim_ids), min(15, 10 + math.ceil(5 * uncertainty)) if adaptive else 15)
        seed = str(secrets.randbits(64))
        selected = random.Random(int(seed)).sample(claim_ids, n)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _require_graph(db, gid)
            existing = db.execute('SELECT id FROM review_rounds WHERE graph_id=? ORDER BY created_at DESC LIMIT 1', (gid,)).fetchone()
            if existing and not new_round:
                return self.sample_state(gid, existing['id'])
            # Keep the original sample table readable for existing installations.
            db.execute('INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?)',
                       (gid, seed, json.dumps(selected), uncertainty, now()))
            rid = uuid.uuid4().hex
            db.execute('INSERT INTO review_rounds VALUES (?,?,?,?,?,?,?)',
                       (rid, gid, (str(name).strip() or f'第{len(state["rounds"]) + 1}轮')[:200],
                        seed, json.dumps(selected), uncertainty, now()))
        return self.sample_state(gid, rid)

    def sample_state(self, gid, round_id=None):
        with self.connect() as db:
            graph_count = db.execute('SELECT claim_count FROM graph_overview WHERE id=?', (gid,)).fetchone()
            if graph_count is None:
                _require_graph(db, gid)
            rounds = [dict(r) for r in db.execute('SELECT * FROM review_rounds WHERE graph_id=? ORDER BY created_at,id', (gid,)).fetchall()]
            rows = db.execute('SELECT * FROM reviews WHERE graph_id=? ORDER BY id', (gid,)).fetchall()
        for r in rounds:
            r['claim_ids'] = json.loads(r['claim_ids'])
        selected = next((r for r in rounds if r['id'] == round_id), None) if round_id else (rounds[-1] if rounds else None)
        if round_id and selected is None:
            raise KeyError('REVIEW_ROUND_NOT_FOUND')
        history = [dict(r) for r in rows]
        for r in rounds:
            r['metrics'] = human_metrics([v for v in history if v['round_id'] == r['id']], len(r['claim_ids']))
        active = latest_votes(history)
        return dict(sample=selected, rounds=rounds, reviews=active, consensus=consensus(history), history=history,
                    round_history=[r for r in history if selected and r['round_id'] == selected['id']],
                    metrics=human_metrics(history, graph_count['claim_count']))

    def review(self, gid, cid, reviewer, verdict, note, round_id=None):
        if verdict not in {'supported', 'partial', 'incorrect', 'uncertain'}:
            raise ValueError('无效人工判定')
        state = self.sample_state(gid, round_id)
        if not state['sample'] or cid not in state['sample']['claim_ids']:
            raise ValueError('只能评审已固定随机样本中的 claim')
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120:
            raise ValueError('请填写评审员，最多120字符')
        if not isinstance(note, str) or len(note) > 6000:
            raise ValueError('评审备注最多6000字符')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _require_graph(db, gid)
            db.execute('INSERT INTO reviews(graph_id,claim_id,reviewer,verdict,note,created_at,round_id) VALUES (?,?,?,?,?,?,?)',
                       (gid, cid, reviewer.strip(), verdict, note, now(), state['sample']['id']))
        return self.sample_state(gid, state['sample']['id'])
