"""Evaluate guards against the supplied audit bundle without database/model calls."""
import argparse
import json
from pathlib import Path

from app.claim_audit import summarize

D1_IDS = {19549,19550,19471,19526,19540,19545,15350,15351,16212,16219,14711,14712,14713,
          17863,20470,20471,23344,23383,23384,23368,23369}


def evaluate(directory):
    rows = {name: json.loads((directory / (name + '.json')).read_text())
            for name in ('fixtures_defects', 'contradicted_ledger', 'control_supported_sample')}
    rows['D1'] = [r for r in rows['fixtures_defects'] if int(r['claim_id'].split(':')[-1]) in D1_IDS]
    result = {}
    for name, values in rows.items():
        result[name] = summarize(values)
        result[name]['rate'] = result[name]['hit_count'] / len(values) if values else 0
    result['acceptance'] = {
        'D1_complete': len(rows['D1']) == len(D1_IDS),
        'D1_capture_at_least_90pct': bool(rows['D1']) and result['D1']['rate'] >= .9,
        'supported_false_positive_at_most_2pct': bool(rows['control_supported_sample']) and result['control_supported_sample']['rate'] <= .02,
        'ledger_capture_at_least_90pct': bool(rows['contradicted_ledger']) and result['contradicted_ledger']['rate'] >= .9}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit-dir', type=Path, default=Path('audit'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = evaluate(args.audit_dir)
    if args.output:
        with args.output.open('x') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({name: {k:v for k,v in data.items() if k != 'hits'} for name,data in result.items()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
