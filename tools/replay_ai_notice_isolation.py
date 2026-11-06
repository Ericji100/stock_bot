"""Replay notice migration using a read-only ledger snapshot and owned copies only."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def tables(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: sorted(conn.execute('SELECT * FROM "' + name + '"').fetchall(), key=repr) for name in names}


def clone(source, root):
    root.mkdir(parents=True, exist_ok=False)
    target = root / 'openai-credits.sqlite3'
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as old, \
            closing(sqlite3.connect(target)) as new:
        old.backup(new)
    return target


def replay(snapshot, task_root, result_root):
    snapshot, task_root, result_root = (p.resolve() for p in (snapshot, task_root, result_root))
    marker = json.loads((task_root / '.codex-task.json').read_text(encoding='utf-8'))
    if (Path(marker['project']).resolve() != ROOT
            or task_root != ROOT / '.codex-tmp' / marker['session'] / marker['task']
            or not snapshot.is_relative_to(ROOT / 'verification')
            or not result_root.is_relative_to(ROOT / 'verification')):
        raise ValueError('Replay requires this project verification snapshot and owned test root')
    sys.path.insert(0, str(ROOT))
    from research_center.deepseek_service import ai_task
    from research_center.openai_credit_service import OpenAICreditLedger

    before_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    original = tables(snapshot)
    folder = task_root / ('notice-replay-' + uuid4().hex)
    folder.mkdir()
    exact = clone(snapshot, folder / 'exact')
    prod = OpenAICreditLedger(exact.parent)
    with prod.db() as conn:
        attribution = [dict(zip(('notice_id', 'purpose', 'command', 'created_at', 'evidence'), row))
                       for row in conn.execute('SELECT notice_id,purpose,command,created_at,evidence FROM notice_attribution')]
    migrated = tables(exact)
    assert all(migrated[name] == rows for name, rows in original.items()), 'Original ledger rows changed'
    assert set(migrated) == set(original) | {'notice_attribution'}
    first_metadata = migrated['notice_attribution']
    with prod.db():
        pass
    assert tables(exact)['notice_attribution'] == first_metadata, 'Migration not idempotent'

    scenario = clone(exact, folder / 'pending-history')
    old_ids = ('9e6db7b541424720873187c7874e99fa', 'bb5b25b31ef74ce79c55e31f3eddcbdc')
    with closing(sqlite3.connect(scenario)) as conn:
        for identifier in old_ids:
            row = conn.execute('SELECT purpose FROM notice_attribution WHERE notice_id=?', (identifier,)).fetchone()
            assert row == ('development',), 'Observed incident lacks development provenance'
            # Reconstruct only the old waiting-to-send state, on this synthetic branch.
            conn.execute("UPDATE notices SET state='pending' WHERE id=?", (identifier,))
        conn.commit()
    replay_ledger = OpenAICreditLedger(scenario.parent)
    historical_claims = []
    while notice := replay_ledger.claim_notice():
        assert notice[0] not in old_ids
        historical_claims.append(notice[0])
        replay_ledger.notice_result(notice[0], True)
    with ai_task('news', background=True):
        replay_ledger.warn('transport_unknown', '離線重播：新的正式錯誤', operation='web_search')
        replay_ledger.warn('transport_unknown', '離線重播：同工作重複錯誤', operation='web_search')
    notice = replay_ledger.claim_notice()
    assert notice is not None and notice[0] not in old_ids
    assert '排程新聞整理' in notice[1] and '搜尋階段' in notice[1]
    assert replay_ledger.claim_notice() is None
    assert replay_ledger.notice_result(notice[0], False)
    assert replay_ledger.claim_notice() is None
    assert replay_ledger.notice_result(notice[0], True) is False
    with ai_task('news', background=True):
        replay_ledger.warn('transport_unknown', '離線重播：下一份正式工作', operation='web_search')
    next_notice = replay_ledger.claim_notice()
    assert next_notice is not None and next_notice[0] != notice[0]
    replay_ledger.notice_result(next_notice[0], True)
    simulated = tables(scenario)
    assert all(simulated[name] == rows for name, rows in original.items() if name != 'notices')
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before_hash
    with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)) as conn:
        unknowns = [dict(zip(('id', 'purpose', 'estimate', 'actual', 'run_id'), row)) for row in conn.execute(
            "SELECT id,purpose,estimate,actual,run_id FROM requests WHERE status IN ('unknown','pending') ORDER BY id")]
    checks = [
        'migration_preserves_all_original_tables_and_rows', 'attribution_migration_idempotent',
        'both_observed_historical_notices_proven_development', 'old_pending_test_notices_not_dispatched',
        'new_production_warning_survives_same_old_reason', 'same_job_warning_deduplicated',
        'next_job_same_reason_not_suppressed', 'uncertain_delivery_not_retried_or_rewritten',
        'all_non_notice_tables_unchanged_after_simulation', 'verification_snapshot_unchanged',
    ]
    result = {'passed': True, 'checks': [{'name': check, 'passed': True} for check in checks],
              'snapshot_sha256': before_hash, 'original_table_counts': {k: len(v) for k, v in original.items()},
              'attribution': attribution, 'pending_and_unknown_requests_preserved': unknowns,
              'other_legitimate_historical_production_claims_in_copy': historical_claims,
              'original_schema_plus_one_attribution_table': set(migrated) == set(original) | {'notice_attribution'},
              'synthetic_notices_in_copy_only': True, 'new_request_rows': 0,
              'new_paid_calls': 0, 'production_ledger_modified': False, 'bot_restarted': False,
              'completed_at': datetime.now(timezone.utc).isoformat()}
    result_root.mkdir(parents=True, exist_ok=True)
    (result_root / 'replay.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'checks': len(checks), 'legacy_notices': len(attribution),
                      'pending_and_unknown_preserved': len(unknowns), 'new_paid_calls': 0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--task-root', required=True, type=Path)
    parser.add_argument('--result-root', required=True, type=Path)
    args = parser.parse_args()
    replay(args.snapshot, args.task_root, args.result_root)
