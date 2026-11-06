from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import httpx
import pytest

from research_center.ai_budget_service import MonthlyBudget
from research_center.ai_provider_service import BudgetNotices
from research_center.deepseek_service import ai_task
from research_center.openai_credit_service import OpenAICreditLedger, OpenAIUnavailable
from research_center.openai_service import OpenAIService


@pytest.fixture
def ledgers(tmp_path):
    now = [datetime(2026, 11, 6, 10, tzinfo=timezone.utc)]
    root = tmp_path / 'openai'
    production = OpenAICreditLedger(root, now=lambda: now[0])
    development = OpenAICreditLedger(root, development=True, now=lambda: now[0])
    with production.db():
        pass
    return production, development, now


def read(ledger, sql, args=()):
    with closing(sqlite3.connect((ledger.root / 'openai-credits.sqlite3').as_uri() + '?mode=ro', uri=True)) as conn:
        return conn.execute(sql, args).fetchall()


def legacy(ledger, identifier='old', *, purposes=('development',), run_id='old-run',
           command='radar', reason='transport_unknown', state='pending'):
    with ledger.db() as conn:
        for index, purpose in enumerate(purposes):
            conn.execute('''INSERT INTO requests
                (id,grant_id,background,command,run_id,operation,status,estimate,actual,detail,purpose)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (f'{identifier}-request-{index}', 'synthetic-grant', 1, command, run_id,
                 'web_search', 'unknown', '0.288884', None, '{}', purpose))
        conn.execute('INSERT INTO notices VALUES (?,?,?,?,?,?)',
                     (identifier, 1, run_id, reason, 'Original historical message', state))


@pytest.mark.parametrize('purposes,run_id,expected', [
    (('development',), 'run-dev', 'development'),
    (('production',), 'run-prod', 'production'),
    (('development', 'production'), 'mixed-run', 'unclassified'),
    ((), 'orphan-run', 'unclassified'),
    (('production',), '', 'unclassified'),
    (('unexpected',), 'bad-purpose', 'unclassified'),
])
def test_legacy_attribution_uses_evidence_without_guessing(ledgers, purposes, run_id, expected):
    prod, _, _ = ledgers
    legacy(prod, purposes=purposes, run_id=run_id)
    with prod.db():
        pass
    purpose, created, evidence = read(prod, 'SELECT purpose,created_at,evidence FROM notice_attribution')[0]
    assert purpose == expected
    assert created is None
    assert json.loads(evidence)['source'] == 'legacy_run_requests'
    assert read(prod, 'SELECT message,state FROM notices') == [('Original historical message', 'pending')]
    notice = prod.claim_notice()
    if expected == 'production':
        assert notice[0] == 'old' and '歷史正式通知' in notice[1] and '非本次工作' in notice[1]
    else:
        assert notice is None


def test_migration_is_idempotent_and_preserves_all_original_tables_and_unknown_costs(ledgers):
    prod, _, clock = ledgers
    legacy(prod)
    original = {name: read(prod, f'SELECT * FROM "{name}"') for (name,) in read(prod, "SELECT name FROM sqlite_master WHERE type='table' AND name!='notice_attribution'")}
    with prod.db():
        pass
    attributed = read(prod, 'SELECT * FROM notice_attribution')
    clock[0] += timedelta(days=1)
    with prod.db():
        pass
    assert attributed == read(prod, 'SELECT * FROM notice_attribution')
    for name, rows in original.items():
        assert rows == read(prod, f'SELECT * FROM "{name}"')


def test_migration_failure_rolls_back_attribution(ledgers, monkeypatch):
    prod, _, _ = ledgers
    legacy(prod)
    original = prod._attribute_legacy_notices
    def broken(conn, **kwargs):
        original(conn, **kwargs)
        raise RuntimeError('injected migration failure')
    with monkeypatch.context() as m:
        m.setattr(prod, '_attribute_legacy_notices', broken)
        with pytest.raises(RuntimeError):
            with prod.db():
                pass
    assert read(prod, 'SELECT * FROM notice_attribution') == []
    with prod.db():
        pass
    assert len(read(prod, 'SELECT * FROM notice_attribution')) == 1


def test_schema_without_request_purpose_does_not_manufacture_legacy_production(tmp_path):
    path = tmp_path / 'openai-credits.sqlite3'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('''CREATE TABLE requests (id TEXT PRIMARY KEY,grant_id TEXT,background INTEGER,
            command TEXT,run_id TEXT,operation TEXT,status TEXT,estimate TEXT,actual TEXT,detail TEXT)''')
        conn.execute("INSERT INTO requests VALUES ('r','g',1,'news','run','web_search','unknown','1',NULL,'{}')")
        conn.execute('CREATE TABLE notices (id TEXT PRIMARY KEY,background INTEGER,run_id TEXT,reason TEXT,message TEXT,state TEXT)')
        conn.execute("INSERT INTO notices VALUES ('n',1,'run','transport_unknown','legacy','pending')")
        conn.commit()
    ledger = OpenAICreditLedger(tmp_path)
    assert ledger.claim_notice() is None
    assert read(ledger, 'SELECT purpose FROM notice_attribution') == [('unclassified',)]


def test_development_notice_cannot_be_claimed_or_acknowledged_by_production(ledgers):
    prod, dev, _ = ledgers
    with ai_task('news', background=True):
        dev.warn('transport_unknown', 'offline development event', operation='web_search')
    assert dev.claim_notice() is None and prod.claim_notice() is None
    identifier = read(prod, 'SELECT id FROM notices')[0][0]
    assert prod.notice_result(identifier, True) is False
    assert dev.notice_result(identifier, True) is False
    assert read(prod, 'SELECT state FROM notices') == [('pending',)]


def test_old_dev_reason_does_not_mask_new_production_and_same_work_dedupes(ledgers):
    prod, _, _ = ledgers
    legacy(prod, state='sent')
    with ai_task('news', background=True):
        prod.warn('transport_unknown', 'new production error', operation='web_search')
        prod.warn('transport_unknown', 'repeat production error', operation='web_search')
    assert len(read(prod, 'SELECT * FROM notices')) == 2
    notice = prod.claim_notice()
    assert notice and notice[0] != 'old'
    assert '排程新聞整理' in notice[1] and '搜尋階段' in notice[1] and '2026-11-06 18:00:00' in notice[1]
    assert '不代表額度用完' in notice[1] and 'new production error' in notice[1]
    assert prod.notice_result(notice[0], True)
    assert read(prod, 'SELECT delivery_result_at FROM notice_attribution WHERE notice_id=?', (notice[0],))[0][0]
    assert prod.claim_notice() is None


def test_new_work_with_same_reason_is_not_suppressed_forever(ledgers):
    prod, _, _ = ledgers
    for _ in range(2):
        with ai_task('news', background=True):
            prod.warn('transport_unknown', 'new work')
        notice = prod.claim_notice()
        assert notice
        prod.notice_result(notice[0], True)
    assert len(read(prod, "SELECT * FROM notice_attribution WHERE purpose='production'")) == 2


def test_command_and_background_are_independent_dedup_keys(ledgers):
    prod, _, _ = ledgers
    with ai_task('news', background=True):
        prod.warn('http_error', 'one')
        with ai_task('radar', background=True):
            prod.warn('http_error', 'two')
        with ai_task('news', background=False):
            prod.warn('http_error', 'three')
    assert len(read(prod, 'SELECT * FROM notices')) == 3
    assert prod.claim_notice() is not None
    assert prod.claim_notice() is not None
    assert prod.claim_notice() is None


def test_unscoped_notifications_throttle_for_five_minutes_not_forever(ledgers):
    prod, _, clock = ledgers
    prod.warn('http_error', 'one')
    prod.warn('http_error', 'two')
    assert len(read(prod, 'SELECT * FROM notices')) == 1
    clock[0] += timedelta(minutes=6)
    prod.warn('http_error', 'three')
    assert len(read(prod, 'SELECT * FROM notices')) == 2


def test_uncertain_delivery_is_not_reclaimed_or_rewritten_as_success(ledgers):
    prod, _, _ = ledgers
    with ai_task('news', background=True):
        prod.warn('transport_unknown', 'unknown')
    notice = prod.claim_notice()
    assert prod.notice_result(notice[0], False)
    assert prod.claim_notice() is None
    assert prod.notice_result(notice[0], True) is False
    assert read(prod, 'SELECT state FROM notices') == [('delivery_unknown',)]


def test_concurrent_claimers_only_dispatch_once(ledgers):
    prod, _, _ = ledgers
    with ai_task('news', background=True):
        prod.warn('transport_unknown', 'unknown')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: OpenAICreditLedger(prod.root).claim_notice(), range(4)))
    assert sum(value is not None for value in results) == 1


def test_common_boundary_blocks_development_even_for_deepseek(ledgers, tmp_path):
    _, dev, _ = ledgers
    fallback = MonthlyBudget(tmp_path / 'deepseek')
    fallback.incident('authentication_failed', background=True)
    boundary = BudgetNotices(dev, fallback)
    assert boundary.claim_notice() is None
    assert boundary.notice_result('deepseek:any', True) is False
    assert fallback.claim_notice() is not None


def test_production_common_boundary_preserves_deepseek_contract(ledgers, tmp_path):
    prod, _, _ = ledgers
    legacy(prod)
    fallback = MonthlyBudget(tmp_path / 'deepseek')
    fallback.incident('authentication_failed', background=True)
    boundary = BudgetNotices(prod, fallback)
    notice = boundary.claim_notice()
    assert notice[0].startswith('deepseek:')
    boundary.notice_result(notice[0], False)
    assert boundary.claim_notice() is None
    with pytest.raises(ValueError):
        boundary.notice_result('unknown:any', True)


def test_route_notice_uses_same_purpose_and_work_isolation(ledgers):
    prod, dev, _ = ledgers
    for ledger in (dev, prod):
        with ai_task('radar', background=True):
            ledger.route('deepseek', 'openai_job_budget_exceeded')
            ledger.route('deepseek', 'openai_job_budget_exceeded')
    assert len(read(prod, 'SELECT * FROM notices')) == 2
    assert len(read(prod, 'SELECT * FROM route_events')) == 4
    notice = prod.claim_notice()
    assert '額度預檢階段' in notice[1] and '不是自動加值' in notice[1]
    assert prod.claim_notice() is None


def test_real_openai_failure_records_search_stage_and_preserves_unknown_reservation(ledgers):
    prod, _, _ = ledgers
    prod.grants = [dict(grant_id='fixture', account_id='a', organization_id='fixture',
                        starts_at='2026-11-01T00:00:00+00:00', expires_at='2026-12-01T00:00:00+00:00',
                        verified_at='2026-11-01T00:00:00+00:00', allowance_usd='100', evidence='synthetic only')]
    def broken(request):
        raise httpx.ReadTimeout('offline simulated timeout', request=request)
    client = OpenAIService(prod, {'a': 'synthetic-key'}, transport=httpx.MockTransport(broken))
    with ai_task('news', background=True), pytest.raises(OpenAIUnavailable):
        client.search('offline query')
    notice = prod.claim_notice()
    assert '排程新聞整理' in notice[1] and '搜尋階段' in notice[1]
    rows = read(prod, 'SELECT status,estimate,actual,purpose FROM requests')
    assert rows[0][0] == 'unknown' and rows[0][2] is None and rows[0][3] == 'production'
    assert float(rows[0][1]) > 0
