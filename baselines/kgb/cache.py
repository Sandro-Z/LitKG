"""Content-addressed JSON cache; a hit never repeats a paid or rate-limited call."""
import json
import os
from pathlib import Path


class Cache:
    def __init__(self, root, namespace):
        self.root = Path(root) / namespace

    def path(self, key):
        return self.root / key[:2] / f'{key}.json'

    def get(self, key):
        p = self.path(key)
        if p.is_file():
            return json.loads(p.read_text(encoding='utf-8'))
        return None

    def put(self, key, value):
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f'.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        tmp.replace(p)
        return value
