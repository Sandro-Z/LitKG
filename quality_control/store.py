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
from .scoring import human_metrics


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
            ''')
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
        return result

    def latest_run(self, gid):
        with self.connect() as db:
            row = db.execute('SELECT id FROM runs WHERE graph_id=? ORDER BY created_at DESC LIMIT 1', (gid,)).fetchone()
        return self.run(row['id']) if row else None

    def recover(self):
        # Never silently restart paid requests after a container restart.
        with self.connect() as db:
            db.execute("UPDATE runs SET status='interrupted',error='SERVICE_RESTARTED',updated_at=? WHERE status IN ('queued','running')", (now(),))

    def sample(self, gid, adaptive=True):
        graph = self.graph(gid)
        latest = self.latest_run(gid)
        uncertainty = (latest.get('report') or {}).get('uncertainty', 1) if latest else 1
        n = min(len(graph['claims']), min(15, 10 + math.ceil(5 * uncertainty)) if adaptive else 15)
        seed = str(secrets.randbits(64))
        selected = random.Random(int(seed)).sample([c['id'] for c in graph['claims']], n)
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?)',
                       (gid, seed, json.dumps(selected), uncertainty, now()))
        return self.sample_state(gid)

    def sample_state(self, gid):
        graph = self.graph(gid)
        with self.connect() as db:
            sample = db.execute('SELECT * FROM samples WHERE graph_id=?', (gid,)).fetchone()
            rows = db.execute('SELECT * FROM reviews WHERE graph_id=? ORDER BY id', (gid,)).fetchall()
        current = {}
        for row in rows:
            current[row['claim_id']] = dict(row)
        result = dict(sample) if sample else None
        if result:
            result['claim_ids'] = json.loads(result['claim_ids'])
        return dict(sample=result, reviews=list(current.values()), history=[dict(r) for r in rows],
                    metrics=human_metrics(list(current.values()), len(graph['claims'])))

    def review(self, gid, cid, reviewer, verdict, note):
        if verdict not in {'supported', 'partial', 'incorrect', 'uncertain'}:
            raise ValueError('无效人工判定')
        state = self.sample_state(gid)
        if not state['sample'] or cid not in state['sample']['claim_ids']:
            raise ValueError('只能评审已固定随机样本中的 claim')
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120:
            raise ValueError('请填写评审员，最多120字符')
        if not isinstance(note, str) or len(note) > 6000:
            raise ValueError('评审备注最多6000字符')
        with self.connect() as db:
            db.execute('INSERT INTO reviews(graph_id,claim_id,reviewer,verdict,note,created_at) VALUES (?,?,?,?,?,?)',
                       (gid, cid, reviewer.strip(), verdict, note, now()))
        return self.sample_state(gid)
