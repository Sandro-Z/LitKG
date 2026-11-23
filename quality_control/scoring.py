"""Transparent scores, per-reviewer consensus and explicitly weighted human input."""
import math
from copy import deepcopy
from .models import SCENARIOS, DIMENSIONS, digest

SCORING_VERSION = 'qc-scoring/2.1.0'
HUMAN_VALUES = {'supported': 100, 'partial': 50, 'incorrect': 0}
GOLD_WEIGHT = .5
# dimension -> (pooled gold metric, coverage that scales its weight)
GOLD_FIELDS = (('correctness', 'precision', 'claim_coverage'), ('completeness', 'recall', 'document_coverage'))


def scenario_scores(dimensions, complete=False, blocker=False):
    result = {}
    for key, (name, primary) in SCENARIOS.items():
        weights = dict(zip(DIMENSIONS, [*primary, primary[3] * .5, primary[2] * .5]))
        known = {k: v for k, v in dimensions.items() if k in weights and v is not None}
        total = sum(weights.values())
        present = sum(weights[k] for k in known)
        score = round(sum(known[k] * weights[k] for k in known) / present, 2) if present else None
        verified = score if len(known) == len(weights) and complete else None
        unmet = [k for k, level in zip(DIMENSIONS, primary) if dimensions.get(k) is not None
                 and dimensions[k] < {1: 50, 2: 65, 3: 80}[level]]
        missing = [k for k in weights if dimensions.get(k) is None]
        suitability = '不适用' if blocker or unmet else ('待验证' if verified is None else '适用')
        result[key] = dict(name=name, weights=weights, score=score, overall=verified, provisional=score,
                           status='complete' if verified is not None else ('partial' if score is not None else 'unavailable'),
                           measured_weight=round(present / total, 4), suitability=suitability,
                           unmet=unmet, missing=missing, thresholds={k: {1: 50, 2: 65, 3: 80}[v]
                           for k, v in zip(DIMENSIONS, primary)})
    return result


def _weight(graph, key, default):
    requested = (graph.get('policy') or {}).get(key, default)
    if not isinstance(requested, (int, float)) or isinstance(requested, bool) or not 0 <= requested <= 1:
        return default
    return requested


def apply_gold(report, graph):
    """Fold the pooled gold-standard precision / recall into correctness / completeness.

    Idempotent and read-time safe: starts from machine_dimensions (the evidence review's own scores), which
    are never changed.  Effective weight = policy.gold_weight (default 0.5) x coverage: the share of graph
    claims the gold standard could judge (precision) or of graph documents it annotates (recall).  Without an
    applicable gold set every dimension stays exactly the machine value.
    """
    machine = deepcopy(report.get('machine_dimensions') or report.get('dimensions') or {})
    report['machine_dimensions'] = machine
    dims = deepcopy(machine)
    gold = report.get('gold') if isinstance(report.get('gold'), dict) else {}
    pooled = gold.get('pooled') if gold.get('status') == 'applied' else None
    requested = _weight(graph, 'gold_weight', GOLD_WEIGHT)
    fusion = {}
    if pooled:
        for field, metric, coverage_key in GOLD_FIELDS:
            value, coverage = pooled.get(metric), pooled.get(coverage_key) or 0
            gold_score = round(100 * value, 2) if value is not None else None
            machine_value = machine.get(field)
            weight = requested * coverage if gold_score is not None else 0
            if gold_score is None or weight == 0:
                method, final, weight = 'machine_only', machine_value, 0
            elif machine_value is None:
                method, final = 'gold_only', gold_score
            else:
                method, final = 'weighted_machine_and_gold', round((1 - weight) * machine_value + weight * gold_score, 2)
            dims[field] = final
            fusion[field] = dict(metric=metric, gold_score=gold_score, machine_score=machine_value, coverage=coverage,
                                 requested_weight=requested, effective_weight=round(weight, 6), method=method, score=final)
    report['dimensions'] = dims
    report['gold_fusion'] = fusion or None
    return report


def gold_reason(fusion):
    """One-line explanation of a gold-fused dimension (None when gold did not change it)."""
    if not fusion or fusion['method'] == 'machine_only':
        return None
    name = '金标准 precision' if fusion['metric'] == 'precision' else '金标准 recall'
    if fusion['method'] == 'gold_only':
        return f'机器评审未给出该维度，采用{name} {fusion["gold_score"]}（覆盖 {100 * fusion["coverage"]:.1f}%）；不能视为全图已验证'
    w = fusion['effective_weight']
    return (f'机器 {fusion["machine_score"]} × {1 - w:.4f} + {name} {fusion["gold_score"]} × {w:.4f}'
            f'（金标准权重 {fusion["requested_weight"]} × 覆盖 {100 * fusion["coverage"]:.1f}%）')


def wilson(supported, n):
    if not n:
        return None
    p, z = supported / n, 1.96
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0, center - half), 4), round(min(1, center + half), 4)]


def latest_votes(history):
    """One active opinion per person and claim across rounds; revisions stay archived."""
    latest = {}
    for row in sorted(history, key=lambda r: r['id']):
        latest[(row['claim_id'], row['reviewer'])] = row
    return list(latest.values())


def consensus(history):
    grouped = {}
    for row in latest_votes(history):
        grouped.setdefault(row['claim_id'], []).append(row)
    result = []
    for cid, opinions in grouped.items():
        decided = [v for v in opinions if v['verdict'] in HUMAN_VALUES]
        scores = [HUMAN_VALUES[v['verdict']] for v in decided]
        result.append(dict(claim_id=cid, score=round(sum(scores) / len(scores), 2) if scores else None,
                           reviewers=len(opinions), decided_reviewers=len(decided),
                           uncertain_reviewers=len(opinions) - len(decided),
                           disagreement=len(set(v['verdict'] for v in decided)) > 1,
                           strict_supported=bool(decided) and all(v['verdict'] == 'supported' for v in decided),
                           opinions=[dict(v) for v in opinions]))
    return result


def human_metrics(reviews, total):
    claims = consensus(reviews)
    decided = [c for c in claims if c['score'] is not None]
    n, supported = len(decided), sum(c['strict_supported'] for c in decided)
    return dict(reviewed=len(claims), decided=n, supported=supported,
                score=round(sum(c['score'] for c in decided) / n, 2) if n else None,
                strict_support_rate=supported / n if n else None,
                uncertain=sum(c['score'] is None for c in claims),
                uncertain_votes=sum(v['verdict'] == 'uncertain' for v in latest_votes(reviews)),
                reviewer_count=len({r['reviewer'] for r in reviews}), active_votes=len(latest_votes(reviews)),
                history_count=len(reviews), disagreements=sum(c['disagreement'] for c in claims),
                population_coverage=n / total if total else None,
                wilson_95=wilson(supported, n), population=total,
                note='每人每条claim取最新意见，跨人等权、再按claim等权；历史改判不重复加票。Wilson区间仅描述已判样本的严格一致支持率，不能证明全图准确率。')


def combine_human(report, state, graph):
    """Read-time score projection: raw machine runs and human audit history stay intact.

    Order: machine evidence scores -> gold-standard fusion (apply_gold) -> human sample fusion on correctness.
    """
    result = deepcopy(report)
    apply_gold(result, graph)
    machine = result['machine_dimensions']
    metrics = state['metrics']
    sampled = len({cid for r in state.get('rounds', []) for cid in r['claim_ids']})
    decided = metrics['decided']
    requested = _weight(graph, 'human_weight', .30)
    completion = min(1, decided / sampled) if sampled else 0
    weight = requested * completion if metrics['score'] is not None else 0
    machine_value = result['dimensions'].get('correctness')   # after the gold-standard fusion
    human_value = metrics['score']
    if human_value is None or requested == 0:
        method, final = 'machine_only', machine_value
    elif machine_value is None:
        method, final = 'human_sample_only', human_value
    else:
        method = 'weighted_machine_and_human'
        final = round((1 - weight) * machine_value + weight * human_value, 2)
    result['dimensions']['correctness'] = final
    result['scoring_version'] = SCORING_VERSION
    result['human'] = dict(metrics=metrics, consensus=state['consensus'], rounds=len(state.get('rounds', [])),
                           requested_weight=requested, effective_weight=round(weight, 6),
                           sample_completion=round(completion, 4), sampled_unique_claims=sampled, method=method)
    result['score_revision'] = digest([(v['id'], v['verdict']) for v in state['reviews']])
    complete = result.get('complete', False)
    # A sample cannot certify previously unreviewed graph claims.
    if method == 'human_sample_only' or metrics['uncertain_votes'] or metrics['disagreements']:
        complete = False
    blocker = result.get('gate') == 'FAIL'
    result['machine_scenarios'] = scenario_scores(machine, result.get('complete', False), blocker)
    result['scenarios'] = scenario_scores(result['dimensions'], complete, blocker)
    result['human_adjusted'] = method != 'machine_only'
    result['machine_complete'] = result.get('complete', False)
    result['complete'] = complete
    result['score_complete'] = complete
    if human_value is not None:
        details = result.setdefault('dimension_details', {}).setdefault('correctness', {})
        gold_fused = (result.get('gold_fusion') or {}).get('correctness', {}).get('method', 'machine_only') != 'machine_only'
        base = '机器+金标准正确性' if gold_fused else '机器正确性'
        details.update(score=final, machine_score=machine.get('correctness'), human_score=human_value,
                       effective_human_weight=round(weight, 6), method=method,
                       status='complete' if complete else 'partial')
        if gold_fused:
            details['pre_human_score'] = machine_value
        if method == 'weighted_machine_and_human':
            details['reason'] = f'{base} {machine_value} × {1-weight:.4f} + 人工多人汇总 {human_value} × {weight:.4f}；历史改判不重复加票'
        elif method == 'human_sample_only':
            details['reason'] = '仅人工样本的多人等权评分；缺少机器证据评分，不能视为全图已验证'
        if any(c['score'] is not None and c['score'] < 100 for c in state['consensus']) and not blocker:
            result['gate'] = 'NEEDS_REVISION'
        elif metrics['uncertain_votes'] or metrics['disagreements']:
            result['gate'] = 'INCOMPLETE' if result.get('gate') == 'PASS' else result.get('gate')
    return result
