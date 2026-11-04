"""Plan/apply evidence-bound budget attribution; never delete or rewrite charges."""
from __future__ import annotations

import argparse
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_center.ai_budget_service import MonthlyBudget, shared_budget_root


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identifiers(value):
    requests, runs = set(), set()
    if isinstance(value, dict):
        if isinstance(value.get('run_id'), str):
            runs.add(value['run_id'])
        requests.update(x for x in value.get('request_ids', []) if isinstance(x, str))
        runs.update(x for x in value.get('run_ids', []) if isinstance(x, str))
        for child in value.values():
            a, b = identifiers(child)
            requests.update(a)
            runs.update(b)
    elif isinstance(value, list):
        for child in value:
            a, b = identifiers(child)
            requests.update(a)
            runs.update(b)
    return requests, runs


def development_usage_requests(project, path):
    checkout = path.parents[2]
    budget_root = shared_budget_root(project)
    if path.name != 'usage.sqlite3' or path.parent.name != 'deepseek' or path.parent.parent.name != '.cache':
        raise ValueError('只接受開發工作樹原生用量帳本。')
    if not (checkout / '.git').is_file() or shared_budget_root(checkout) != budget_root:
        raise ValueError('用量帳本不是同一專案的隔離工作樹，不能當測試證據。')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        rows = conn.execute('SELECT id,day,background,command,status,estimated_usd,actual_usd FROM usage').fetchall()
    production = budget_root.parent.parent / '.cache/deepseek/usage.sqlite3'
    production_ids = set()
    if production.exists():
        with closing(sqlite3.connect(production.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            production_ids = {row[0] for row in conn.execute('SELECT id FROM usage')}
    return {row[0]: row for row in rows if row[0] not in production_ids}


def make_plan(project, evidence_paths):
    ledger = shared_budget_root(project) / 'budget.sqlite3'
    with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        rows = conn.execute('SELECT id,day,background,command,run_id,status,estimate,actual FROM spending').fetchall()
    proofs = []
    matches = {}
    skipped = []
    for path in evidence_paths:
        path = path.resolve(strict=True)
        usage_rows = None
        if path.suffix == '.sqlite3':
            usage_rows = development_usage_requests(project, path)
            requests, runs = set(usage_rows), set()
        else:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            requests, runs = identifiers(value)
        proof = {'path': str(path), 'sha256': digest(path)}
        proofs.append(proof)
        for row in rows:
            if row[0] not in requests and row[4] not in runs:
                continue
            if usage_rows is not None:
                source = usage_rows[row[0]]
                if tuple(source) != (row[0], row[1], row[2], row[3], row[5], row[6], row[7]):
                    raise ValueError('開發用量與共用帳本內容不一致，停止歸類。')
            if row[5] in ('pending', 'unknown'):
                skipped.append({'request_id': row[0], 'status': row[5]})
                continue
            matches.setdefault(row[0], {'row': row, 'proofs': []})['proofs'].append(proof)
    entries = []
    amount = Decimal(0)
    for request_id, match in sorted(matches.items()):
        row = match['row']
        amount += Decimal(row[7] if row[7] is not None else row[6]) if row[5] not in ('failed', 'rejected') else 0
        entries.append({'request_id': request_id, 'fingerprint': MonthlyBudget.spending_fingerprint(row),
                        'evidence': json.dumps(match['proofs'], sort_keys=True, ensure_ascii=False),
                        'reason': '使用者授權將已核對測試產物的請求獨立歸類；保留原始實際費用。'})
    return {'schema': 1, 'ledger': str(ledger.resolve()), 'evidence': proofs, 'entries': entries,
            'excluded_recorded_usd': str(amount), 'unknown_or_pending_retained': skipped}


def apply_plan(project, path, expected_sha):
    if not expected_sha or digest(path) != expected_sha:
        raise ValueError('歸類計畫雜湊不符。')
    plan = json.loads(path.read_text(encoding='utf-8'))
    budget_root = shared_budget_root(project)
    if plan['schema'] != 1 or Path(plan['ledger']).resolve() != (budget_root / 'budget.sqlite3').resolve():
        raise ValueError('正式帳本路徑不符。')
    for proof in plan['evidence']:
        if digest(Path(proof['path'])) != proof['sha256']:
            raise ValueError('原始測試證據已變更，停止套用。')
        if Path(proof['path']).suffix == '.sqlite3':
            eligible = development_usage_requests(project, Path(proof['path']))
            for item in plan['entries']:
                if proof in json.loads(item['evidence']) and item['request_id'] not in eligible:
                    raise ValueError('請求已出現在正式用量帳本，停止測試歸類。')
    return MonthlyBudget(budget_root).attribute_development(plan['entries'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--evidence', action='append', default=[], type=Path)
    parser.add_argument('--apply-sha256')
    args = parser.parse_args()
    if args.apply_sha256:
        print(json.dumps({'attributed': apply_plan(args.project, args.plan, args.apply_sha256)}))
    else:
        if not args.evidence:
            parser.error('必須明確指定測試證據，不自動推測全部紀錄都是測試。')
        plan = make_plan(args.project, args.evidence)
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        with args.plan.open('x', encoding='utf-8') as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        print(json.dumps({'matched_requests': len(plan['entries']), 'excluded_recorded_usd': plan['excluded_recorded_usd'],
                          'unknown_retained': len(plan['unknown_or_pending_retained']), 'sha256': digest(args.plan)}))


if __name__ == '__main__':
    main()
