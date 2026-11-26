"""Read this week's production evidence; replay only into a new owned test root."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def readonly(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True))


def snapshot_ledger(path):
    with readonly(path) as conn:
        conn.execute('BEGIN')
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                 if r[0] != 'cache' and not r[0].startswith('sqlite_')]
        return {name: {'schema': conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0],
                       'rows': conn.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid').fetchall()}
                for name in names}


async def replay(source, work, out):
    from research_center.scheduled_outcome_notice import ScheduledOutcomeNotices
    from research_center.scheduled_run_store import ScheduledRunStore
    from research_center.resource_guard_service import ResourceGuardService
    from research_center.scheduled_task_service import ScheduledTaskService, ScheduledTaskSpec
    ledger_path = source / '.workdata/ai_budget/openai/openai-credits.sqlite3'
    original = snapshot_ledger(ledger_path)
    replay_root = work / ('replay-' + out.stem)
    replay_root.mkdir(exist_ok=False)
    copy = replay_root / 'openai-credits.sqlite3'
    with sqlite3.connect(copy) as conn:
        for table in original.values():
            conn.execute(table['schema'])
            if table['rows']:
                name = next(key for key, value in original.items() if value is table)
                marks = ','.join('?' for _ in table['rows'][0])
                conn.executemany('INSERT INTO "' + name.replace('"', '""') + '" VALUES (' + marks + ')', table['rows'])
    before = snapshot_ledger(copy)

    events, proofs = [], []
    for day in ('2026-11-23', '2026-11-24', '2026-11-25', '2026-11-26'):
        path = source / 'logs/scheduled_tasks' / (day + '.jsonl')
        raw = path.read_bytes()
        events.extend(json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip())
        proofs.append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()})

    req_id = 'bc5f16edab1847fdb315858ba09c63cf'
    with closing(sqlite3.connect(copy)) as conn:
        columns = [row[1] for row in conn.execute('PRAGMA table_info(requests)')]
    request = next(dict(zip(columns, row)) for row in before['requests']['rows'] if row[columns.index('id')] == req_id)
    assert request['status'] in {'pending', 'unknown'} and request['actual'] is None
    artifact = source / '.workdata/ai_budget/openai/requests' / (req_id + '-request.json')
    raw_request = artifact.read_bytes()
    prepared = json.loads(raw_request)
    assert prepared['request_id'] == req_id and prepared['run_id'] == request['run_id']
    assert prepared['command'] == 'radar' and prepared['body']['truncation'] == 'disabled'
    proofs.append({'path': str(artifact), 'sha256': hashlib.sha256(raw_request).hexdigest()})
    radar_events = [e for e in events if e.get('task_id') == 'scheduled:radar:2130' and e['timestamp'].startswith('2026-11-25')]
    starts = [e for e in radar_events if e['status'] == 'started']
    assert len(starts) == 1 and not any(e['status'] in {'completed', 'failed', 'timeout', 'partial_success'} for e in radar_events)
    store = ScheduledRunStore(replay_root / 'radar')
    # Historical logs have no PID: simulate a proven-dead owner, never invent it in production.
    store.begin(request['run_id'], 'radar', '2026-11-25', owner_pid=999999, owner_creation='offline-dead-owner')
    store.checkpoint(request['run_id'], 'sources', {'request_id': req_id, 'body': prepared['body'], 'is_final_report': False})
    recovered = store.recover(is_owner_alive=lambda *args: False)
    delivered = []
    async def send(text):
        delivered.append(text)
    await store.notify_recovered(send)
    await store.notify_recovered(send)
    assert len(recovered) == 1 and len(delivered) == 1
    with store.db() as conn:
        saved = json.loads(conn.execute('SELECT payload FROM checkpoints').fetchone()[0])
    assert saved['body'] == prepared['body']

    outcomes = source / '.workdata/prod/runtime/schedule_outcomes/outcomes.sqlite3'
    news_db = source / '.workdata/prod/database/stock_research.db'
    with readonly(outcomes) as conn:
        history = conn.execute("SELECT day,run_id,detail FROM outcomes WHERE command='news' AND day BETWEEN '2026-11-23' AND '2026-11-26' ORDER BY day").fetchall()
    with readonly(news_db) as conn:
        runs = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT run_id,detail FROM news_refresh_runs WHERE day BETWEEN '2026-11-23' AND '2026-11-26'")}
    notices, sent, news_results = ScheduledOutcomeNotices(replay_root / 'news'), [], []
    async def news_send(text):
        sent.append(text)
    for day, run_id, raw in history:
        detail, run = json.loads(raw), runs[run_id]
        pending = run['pending_classification_summary']
        detail.update(search_complete=run.get('search_complete'),
                      historical_pending=pending.get('historical_or_date_unknown'))
        state = await notices.observe('news', run_id, day, detail, news_send)
        duplicate = await notices.observe('news', run_id, day, detail, news_send)
        assert state == 'sent' and duplicate == 'duplicate'
        assert detail['pending'] == pending['eligible_recent']
        assert str(detail['historical_pending']) not in sent[-1].split('近期待分類')[1].split('。')[0]
        news_results.append({'day': day, 'classified': detail['classified'], 'recent_pending': detail['pending'],
                             'historical_or_unknown_date': detail['historical_pending'], 'notice_state': state,
                             'duplicate': duplicate, 'text': sent[-1]})

    waits = [e for e in events if e.get('resource_wait_seconds', 0) > 300]
    assert waits
    guard = ResourceGuardService({'background_backfill': 1})
    calls = []
    async def worker():
        calls.append('must-not-start')
    service = ScheduledTaskService(resource_guard=guard, audit_sink=lambda *a: None)
    async with guard.acquire('background_backfill'):
        outcome = await service._run_with_timeout(ScheduledTaskSpec('offline-wait', '回補等待重播',
            resource_group='background_backfill', resource_wait_seconds=.01, timeout_seconds=7200), worker)
        assert outcome['status'] == 'deferred' and not calls
        assert guard.snapshot()['pools']['background_backfill']['active'] == 1

    assert snapshot_ledger(copy) == before
    from research_center.openai_credit_service import OpenAICreditLedger, OpenAIUnavailable
    from research_center.deepseek_service import RUN_ID, ai_task
    token = RUN_ID.set(request['run_id'])
    try:
        with ai_task('radar', background=True):
            try:
                OpenAICreditLedger(replay_root).choose(['secondary', 'primary'], reserve=True)
            except OpenAIUnavailable as exc:
                assert exc.status == 'transport_unknown'
            else:
                raise AssertionError('中斷恢復不得解除同工作未知費用護欄')
    finally:
        RUN_ID.reset(token)
    assert snapshot_ledger(copy) == before
    # A live Bot can add legitimate rows; verify immutable historical unknown separately.
    with readonly(ledger_path) as conn:
        now = conn.execute('SELECT * FROM requests WHERE id=?', (req_id,)).fetchone()
    assert dict(zip(columns, now)) == request
    result = {'paid_requests': 0, 'production_writes': 0, 'source_proofs': proofs,
              'radar': {'original_started_at': starts[0]['timestamp'], 'request_id': req_id,
                        'original_status': request['status'], 'unknown_reservation_usd': request['estimate'],
                        'original_input_preserved': True, 'checkpoint_body_sha256': digest(saved['body']),
                        'recovery_notice_count': len(delivered), 'automatic_model_retry': False,
                        'same_run_unknown_resend_still_blocked': True,
                        'historical_owner_not_invented': True, 'production_row_unchanged': True},
              'news': news_results,
              'backfill': {'observed_wait_count_over_300s': len(waits),
                           'max_observed_wait_seconds': max(e['resource_wait_seconds'] for e in waits),
                           'replayed_status': outcome['status'], 'new_work_started': len(calls)},
              'financial_copy': {name: {'rows': len(table['rows']), 'sha256': digest(table['rows'])}
                                 for name, table in before.items()},
              'financial_copy_all_original_rows_unchanged': True}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'radar': result['radar'], 'news_cases': len(news_results),
                      'backfill': result['backfill'], 'all_financial_rows_unchanged': True}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--task-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    work, out = args.task_root.resolve(), args.output.resolve()
    marker = json.loads((work / '.codex-task.json').read_text(encoding='utf-8-sig'))
    assert Path(marker['project']).resolve() == ROOT
    assert work == ROOT / '.codex-tmp' / marker['session'] / marker['task']
    assert out.is_relative_to(ROOT / 'verification') and not out.exists()
    assert args.source_root.resolve() != ROOT and work.is_relative_to(ROOT)
    out.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(replay(args.source_root.resolve(), work, out))


if __name__ == '__main__':
    main()
