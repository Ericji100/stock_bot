"""Read-only stage costs; no API, quota adjustment, cache mutation or scheduler."""
import argparse
from collections import defaultdict
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3


def cost_report(path, *, purpose=None, command=None, round_id=None, grant_id=None):
    path = Path(path).resolve(strict=True)
    groups = {}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        rows = conn.execute('SELECT grant_id,background,command,run_id,operation,status,estimate,actual,detail,purpose,round_id FROM requests')
        for grant, background, cmd, run_id, operation, status, estimate, actual, raw, scope, rnd in rows:
            if (purpose and scope != purpose or command and cmd != command or round_id and rnd != round_id
                    or grant_id and grant != grant_id):
                continue
            detail = json.loads(raw or '{}')
            key = (scope, cmd, 'background' if background else 'manual',
                   detail.get('workflow_stage') or 'legacy_unclassified',
                   detail.get('actual_model') or detail.get('requested_model') or 'unknown', grant)
            group = groups.setdefault(key, dict(requests=0, runs=set(), actual=Decimal(0),
                unknown=Decimal(0), pending=Decimal(0), search_calls=0, tools=Decimal(0),
                reasons=set(), statuses=defaultdict(int)))
            group['requests'] += 1
            group['runs'].add(run_id)
            group['statuses'][status] += 1
            if actual is not None:
                group['actual'] += Decimal(str(actual))
            if status == 'unknown':
                group['unknown'] += Decimal(str(estimate))
            if status == 'pending':
                group['pending'] += Decimal(str(estimate))
            calls = detail.get('search_calls')
            if type(calls) is int and calls >= 0:
                group['search_calls'] += calls
                group['tools'] += Decimal(calls) * Decimal('.01')
            if detail.get('search_reason'):
                group['reasons'].add(detail['search_reason'])
    output = []
    for key, group in sorted(groups.items()):
        output.append(dict(zip(('purpose', 'command', 'pool', 'stage', 'model', 'grant_id'), key),
            requests=group['requests'], runs=len(group['runs']), settled_usd=str(group['actual']),
            tool_usd=str(group['tools']), model_and_content_usd=str(group['actual']-group['tools']),
            unknown_reserved_usd=str(group['unknown']), pending_reserved_usd=str(group['pending']),
            paid_search_calls=group['search_calls'], followup_reasons=sorted(group['reasons']),
            statuses=dict(group['statuses'])))
    return {'groups': output, 'settled_usd': str(sum((g['actual'] for g in groups.values()), Decimal(0))),
            'unknown_reserved_usd': str(sum((g['unknown'] for g in groups.values()), Decimal(0))),
            'pending_reserved_usd': str(sum((g['pending'] for g in groups.values()), Decimal(0))),
            'limitations': '實付與未知／待結算預留分列；舊紀錄不推測階段，未含未記錄於此 OpenAI 帳本的 DeepSeek 費用。快取命中不新增付費請求，另見 workflow activity。無混合流程 Live 結果前不推算節省率或品質。'}


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='唯讀檢視 AI 初搜、補查與分析費用')
    parser.add_argument('ledger')
    parser.add_argument('--purpose', choices=['production', 'development'])
    parser.add_argument('--command')
    parser.add_argument('--round-id')
    parser.add_argument('--grant-id', help='限定已核對的贈額週期，不假設每月自動發放')
    args = parser.parse_args()
    print(json.dumps(cost_report(args.ledger, purpose=args.purpose, command=args.command,
                                round_id=args.round_id, grant_id=args.grant_id), ensure_ascii=False, indent=2))
