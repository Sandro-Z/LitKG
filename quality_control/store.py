"""SQLite snapshots, durable run ledger and append-only human decisions."""
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
            db.execute('INSERT INTO graphs VALUES (?,?,?,?,?,?)',
                       (gid, graph['name'], graph['snapshot_hash'], json.dumps(graph, ensure_ascii=False),
                        json.dumps(original, ensure_ascii=False), now()))
        return gid

    def graph(self, gid):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM graphs WHERE id=?', (gid,)).fetchone()
        if not row:
            raise KeyError('GRAPH_NOT_FOUND')
        return json.loads(row['payload'])

    def list_graphs(self):
        with self.connect() as db:
            rows = db.execute('SELECT id,name,snapshot_hash,created_at FROM graphs ORDER BY created_at DESC').fetchall()
        result = []
        for row in rows:
            item = dict(row)
            graph = self.graph(item['id'])
            item.update(claim_count=len(graph['claims']), document_count=len(graph['documents']),
                        latest_run=self.latest_run(item['id']))
            result.append(item)
        return result

    def start_run(self, gid, mode):
        self.graph(gid)
        rid = uuid.uuid4().hex
        with self.connect() as db:
            try:
                db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)', (rid, gid, 'queued', mode, None, None, now(), now()))
            except sqlite3.IntegrityError:
                raise ValueError('已有待执行或运行中的评审') from None
        return rid

    def update_run(self, rid, status, report=None, error=None):
        with self.connect() as db:
            db.execute('UPDATE runs SET status=?,report=COALESCE(?,report),error=?,updated_at=? WHERE id=?',
                       (status, json.dumps(report, ensure_ascii=False) if report is not None else None, error, now(), rid))

    def run(self, rid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
        if not row:
            raise KeyError('RUN_NOT_FOUND')
        result = dict(row)
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
            db.execute("UPDATE runs SET status='interrupted',error='SERVICE_RESTARTED',updated_at=? WHERE status IN ('queued','running')", (now(),))

    def sample(self, gid, adaptive=True, new_round=False, name=''):
        graph = self.graph(gid)
        state = self.sample_state(gid)
        if state['sample'] and not new_round:
            return state
        latest = self.latest_run(gid)
        uncertainty = (latest.get('report') or {}).get('uncertainty', 1) if latest else 1
        n = min(len(graph['claims']), min(15, 10 + math.ceil(5 * uncertainty)) if adaptive else 15)
        seed = str(secrets.randbits(64))
        selected = random.Random(int(seed)).sample([c['id'] for c in graph['claims']], n)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
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
        graph = self.graph(gid)
        with self.connect() as db:
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
                    metrics=human_metrics(history, len(graph['claims'])))

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
            db.execute('INSERT INTO reviews(graph_id,claim_id,reviewer,verdict,note,created_at,round_id) VALUES (?,?,?,?,?,?,?)',
                       (gid, cid, reviewer.strip(), verdict, note, now(), state['sample']['id']))
        return self.sample_state(gid, state['sample']['id'])
