"""out/stats.md: per-variant summary computed from the graph files."""
import random
from collections import Counter
from pathlib import Path

from .common import load_json


def section(path):
    g = load_json(path)
    m, s = g['metadata'], g['metadata'].get('stats', {})
    lines = [f'## {Path(path).name} — {m.get("system")} / {m.get("variant")}', '',
             f'- documents: {len(g["documents"])}; nodes: {len(g["nodes"])}; claims: {len(g["claims"])}',
             f'- anchored: {s.get("anchored")}; unanchored: {s.get("unanchored")}; anchor rate: '
             + (f'{s["anchor_rate"]:.3f}' if isinstance(s.get('anchor_rate'), float) else '—'),
             f'- model: {m.get("model")}; prompt_sha256: {m.get("prompt_sha256")}; mapping_sha256: {m.get("mapping_sha256")}',
             f'- elapsed seconds: {m.get("elapsed_seconds")}', '']
    if m.get('consistency'):
        lines += ['### Differences from LitKG (explicit)', ''] + [f'- {x}' for x in m['consistency']] + ['']
    lines += ['### Unmapped predicates (top 20)', '']
    lines += [f'- `{k}`: {v}' for k, v in Counter(s.get('unmapped_predicates', {})).most_common(20)] or ['- none']
    lines += ['', '### Entity types', ''] + [f'- {k}: {v}' for k, v in s.get('entity_types', {}).items()]
    lines += ['', '### LLM usage and calls', '', f'- calls: {s.get("llm_calls", 0)}; cache hits: {s.get("llm_cache_hits", 0)}; '
              f'usage: {s.get("llm_usage", {})}']
    lines += ['', '### Failures and drops', ''] + [f'- {k}: {v}' for k, v in sorted(s.get('drops', {}).items())]
    other = {k: v for k, v in s.items() if isinstance(v, int) and k not in {'anchored', 'unanchored', 'documents', 'nodes', 'claims'}}
    lines += [f'- {k}: {v}' for k, v in sorted(other.items())]
    reasons = Counter(u.get('reason') for u in s.get('unanchored_samples', []))
    lines += ['', f'- unanchored reasons (first 200 samples): {dict(reasons)}', '', '### 20 random claims (seed 0)', '']
    nodes = {n['id']: n for n in g['nodes']}
    for c in random.Random(0).sample(g['claims'], min(20, len(g['claims']))):
        quote = (c['source_refs'][0]['quote'] if c['source_refs'] else '(unanchored)').replace('\n', ' ')
        lines.append(f'- `{c["id"]}` {nodes[c["subject"]["id"]]["label"]} —{c["predicate"]}→ '
                     f'{nodes[c["object"]["id"]]["label"]} | {quote[:240]}')
    return '\n'.join(lines) + '\n'


def write(out_dir):
    files = sorted(Path(out_dir).glob('graph.*.json'))
    text = '# Baseline statistics\n\nAll numbers are computed from the graph files in this directory.\n\n'
    text += '\n'.join(section(f) for f in files) if files else 'No graph files found.\n'
    target = Path(out_dir) / 'stats.md'
    target.write_text(text, encoding='utf-8')
    return target
