"""Transparent engineering scores, coverage-aware suitability and sampling stats."""
import math
from .models import SCENARIOS, DIMENSIONS


def scenario_scores(dimensions, complete=False, blocker=False):
    result = {}
    for key, (name, primary) in SCENARIOS.items():
        weights = dict(zip(DIMENSIONS, [*primary, primary[3] * .5, primary[2] * .5]))
        known = {k: v for k, v in dimensions.items() if k in weights and v is not None}
        total = sum(weights.values())
        present = sum(weights[k] for k in known)
        provisional = round(sum(known[k] * weights[k] for k in known) / present, 2) if present else None
        score = provisional if len(known) == len(weights) and complete else None
        unmet = [k for k, level in zip(DIMENSIONS, primary) if dimensions.get(k) is not None
                 and dimensions[k] < {1: 50, 2: 65, 3: 80}[level]]
        missing = [k for k in weights if dimensions.get(k) is None]
        suitability = '不适用' if blocker or unmet else ('待验证' if score is None else '适用')
        result[key] = dict(name=name, weights=weights, overall=score, provisional=provisional,
                           measured_weight=round(present / total, 4), suitability=suitability,
                           unmet=unmet, missing=missing, thresholds={k: {1: 50, 2: 65, 3: 80}[v]
                           for k, v in zip(DIMENSIONS, primary)})
    return result


def human_metrics(reviews, total):
    decided = [r for r in reviews if r['verdict'] != 'uncertain']
    n, supported = len(decided), sum(r['verdict'] == 'supported' for r in decided)
    interval = None
    if n:
        p, z = supported / n, 1.96
        center = (p + z * z / (2 * n)) / (1 + z * z / n)
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        interval = [round(max(0, center - half), 4), round(min(1, center + half), 4)]
    return dict(reviewed=len(reviews), decided=n, supported=supported,
                strict_support_rate=supported / n if n else None,
                uncertain=sum(r['verdict'] == 'uncertain' for r in reviews),
                wilson_95=interval, population=total,
                note='区间仅描述已判定的均匀随机样本；未定和未审单列，最多15条不能证明全图正确')
