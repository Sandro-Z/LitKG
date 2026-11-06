"""Evidence-backed dimension rubrics, independently usable from verdict labels."""
import re
from .models import finite

RUBRICS = {
    'specialization': {
        'identity_specificity': ('身份具体程度', .35),
        'terminology': ('领域术语准确程度', .25),
        'typing': ('实体与关系类型专门程度', .25),
        'scope_alignment': ('研究领域和任务范围适配', .15),
    },
    'granularity': {
        'atomicity': ('原子事实表达', .25),
        'qualifiers': ('人群、条件、时间和方法保留', .25),
        'polarity_modality': ('方向、否定和推测强度保留', .25),
        'quantitative_detail': ('原文存在的数值、单位与比较细节保留', .25),
    },
}


def number(value):
    if isinstance(value, dict):
        value = value.get('score')
    if isinstance(value, str) and re.fullmatch(r'\s*\d+(?:\.\d+)?\s*%?\s*', value):
        value = float(value.strip().rstrip('%').strip())
    return float(value) if finite(value) else None


def measure(value, field, evidence):
    """Accept v1 scalar scores; v2 criteria preserve denominator and reasons."""
    if not evidence:
        return None
    if isinstance(value, dict) and isinstance(value.get('criteria'), dict):
        value = value['criteria']
    if isinstance(value, dict) and any(k in value for k in RUBRICS[field]):
        criteria = []
        for key, (label, weight) in RUBRICS[field].items():
            item = value.get(key)
            score = number(item)
            criteria.append(dict(key=key, label=label, weight=weight, score=score,
                                 rationale=str(item.get('rationale', ''))[:3000] if isinstance(item, dict) else ''))
        present = sum(c['weight'] for c in criteria if c['score'] is not None)
        if not present:
            return None
        return dict(score=round(sum(c['weight'] * c['score'] for c in criteria if c['score'] is not None) / present, 2),
                    coverage=round(present, 4), method='weighted_rubric', criteria=criteria, evidence=evidence)
    scalar = number(value)
    if scalar is not None:
        return dict(score=scalar, coverage=1, method='direct_evidence_score', criteria=[], evidence=evidence)
    return None
