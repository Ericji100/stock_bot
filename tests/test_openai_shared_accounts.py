"""Offline regressions for shared, API-confirmed OpenAI account routing."""

from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
from unittest.mock import Mock, call

import httpx
import pytest

from research_center.ai_provider_service import RoutedAIService
from research_center.deepseek_service import DeepSeekResult, ai_task
from research_center.openai_credit_service import (
    OpenAICreditLedger,
    OpenAICreditsUnavailable,
    OpenAIUnavailable,
)
from research_center.openai_service import OpenAIService
from tests.test_openai_routing import grant, response


NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
ACCOUNTS = {'primary': 'synthetic-first', 'secondary': 'synthetic-second'}


@pytest.fixture(autouse=True)
def forbid_real_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('This regression suite must use httpx.MockTransport only')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)


@pytest.fixture
def ledger(tmp_path):
    return OpenAICreditLedger(
        tmp_path / 'ledger', [grant('primary'), grant('secondary')],
        job_limit=0, now=lambda: NOW,
    )


@pytest.fixture
def fallback():
    client = Mock(model='deepseek-flash', organizer=False,
                  timeout_seconds=180, max_output_tokens=32768)
    client.generate_report.return_value = DeepSeekResult(
        'offline fallback', {},
        {'provider': 'deepseek', 'actual_model': 'deepseek-flash'},
    )
    return client


def routed(ledger, fallback, handler, accounts=None):
    return RoutedAIService(OpenAIService(
        ledger, ACCOUNTS if accounts is None else accounts,
        transport=httpx.MockTransport(handler),
    ), fallback)


def success(request):
    data = response('offline report')
    data['model'] = json.loads(request.content)['model']
    return httpx.Response(200, json=data)


def financial_rows(ledger):
    with ledger.db() as conn:
        return {
            'grants': conn.execute('SELECT * FROM grants ORDER BY id').fetchall(),
            'requests': conn.execute('SELECT * FROM requests ORDER BY id').fetchall(),
            'blocks': conn.execute(
                'SELECT * FROM account_blocks ORDER BY account_id').fetchall(),
        }


@pytest.mark.parametrize('status', [402, 429])
@pytest.mark.parametrize('code', ['credit_balance_exhausted', 'insufficient_quota'])
def test_explicit_credit_rejection_routes_second_then_first(ledger, fallback, status, code):
    calls = []

    def handler(request):
        calls.append(request)
        if request.headers['Authorization'] == 'Bearer synthetic-second':
            return httpx.Response(status, json={'error': {'code': code}})
        return success(request)

    prompt = 'complete original offline evidence'
    with ai_task('research'):
        result = routed(ledger, fallback, handler).generate_report(prompt)

    assert result.diagnostics['provider'] == 'openai'
    assert [r.headers['Authorization'] for r in calls] == [
        'Bearer synthetic-second', 'Bearer synthetic-first']
    assert [json.loads(r.content)['input'] for r in calls] == [prompt, prompt]
    assert all(r.url.path == '/v1/responses' for r in calls)
    with ledger.db() as conn:
        assert conn.execute(
            'SELECT grant_id,status,actual FROM requests ORDER BY rowid').fetchall() == [
                ('secondary', 'rejected', '0'),
                ('primary', 'success', result.diagnostics['cost_estimate_usd']),
            ]
        assert conn.execute('SELECT * FROM account_blocks').fetchall() == [
            ('secondary', 'secondary', 'credit_balance_exhausted')]
    fallback.generate_report.assert_not_called()


def test_both_exhausted_use_existing_deepseek_without_later_probes(ledger, fallback):
    calls = []

    def handler(request):
        calls.append(request.headers['Authorization'])
        return httpx.Response(429, json={'error': {'code': 'credit_balance_exhausted'}})

    router = routed(ledger, fallback, handler)
    for prompt in ['original evidence', 'different subsequent job evidence']:
        with ai_task('research'):
            result = router.generate_report(prompt)
        assert result.markdown == 'offline fallback'
        assert result.diagnostics['fallback_reason'] == 'openai_credits_unavailable'
    assert calls == ['Bearer synthetic-second', 'Bearer synthetic-first']
    assert fallback.generate_report.call_args_list == [
        call('original evidence'), call('different subsequent job evidence')]
    with ledger.db() as conn:
        assert conn.execute('SELECT count(*) FROM requests').fetchone()[0] == 2
        assert conn.execute('SELECT status,actual FROM requests').fetchall() == [
            ('rejected', '0'), ('rejected', '0')]


@pytest.mark.parametrize('status,code,expected', [
    (401, 'credit_balance_exhausted', 'authentication_failed'),
    (403, 'insufficient_quota', 'authentication_failed'),
    (400, 'credit_balance_exhausted', 'http_error'),
    (429, 'rate_limit_exceeded', 'rate_limited'),
    (429, '', 'rate_limited'),
    (500, 'credit_balance_exhausted', 'transport_unknown'),
    (503, 'insufficient_quota', 'transport_unknown'),
    (408, '', 'transport_unknown'),
    (409, '', 'transport_unknown'),
    (None, '', 'transport_unknown'),
])
def test_other_failures_never_rotate_or_fallback(ledger, fallback, status, code, expected):
    calls = []

    def handler(request):
        calls.append(request.headers['Authorization'])
        if status is None:
            raise httpx.ReadTimeout('offline timeout', request=request)
        return httpx.Response(status, json={'error': {
            'code': code, 'message': 'credit_balance_exhausted insufficient_quota'}})

    with ai_task('research'), pytest.raises(OpenAIUnavailable) as caught:
        routed(ledger, fallback, handler).generate_report('offline evidence')
    assert caught.value.status == expected
    assert not isinstance(caught.value, OpenAICreditsUnavailable)
    assert calls == ['Bearer synthetic-second']
    fallback.generate_report.assert_not_called()
    with ledger.db() as conn:
        assert conn.execute('SELECT * FROM account_blocks').fetchall() == []
        row = conn.execute('SELECT status,estimate,actual FROM requests').fetchone()
    assert Decimal(row[1]) > 0
    assert (row[0], row[2]) == (
        ('unknown', None) if expected == 'transport_unknown' else ('rejected', '0'))


def test_unknown_usage_stops_same_job_and_retains_reservation(ledger, fallback):
    calls = []

    def handler(request):
        calls.append(request)
        data = response('output without billable usage')
        data.pop('usage')
        return httpx.Response(200, json=data)

    router = routed(ledger, fallback, handler)
    with ai_task('research'):
        with pytest.raises(OpenAIUnavailable) as caught:
            router.generate_report('first evidence')
        assert caught.value.status == 'transport_unknown'
        before = financial_rows(ledger)
        with pytest.raises(OpenAIUnavailable) as caught:
            router.generate_report('different evidence must not be resent')
        assert caught.value.status == 'transport_unknown'
        with pytest.raises(OpenAIUnavailable) as caught:
            ledger.choose({'primary'}, Decimal('.01'), reserve=True)
        assert caught.value.status == 'transport_unknown'
    assert financial_rows(ledger) == before
    assert len(calls) == 1
    report = ledger.summary()
    assert report['unknown_or_pending'] == 1
    with ledger.db() as conn:
        estimate, actual = conn.execute('SELECT estimate,actual FROM requests').fetchone()
    assert actual is None
    assert Decimal(report['used_or_reserved']) == Decimal(estimate) > 0
    fallback.generate_report.assert_not_called()


def test_expired_allowance_and_ratios_do_not_gate_shared_usage(tmp_path):
    historical = grant('secondary', amount='0.01', end='2026-11-02T00:00:00Z')
    root = tmp_path / 'shared'
    for development, background in [(False, False), (False, True), (True, False)]:
        ledger = OpenAICreditLedger(root, [historical], job_limit=0,
                                    development=development, now=lambda: NOW)
        with ai_task('news' if background else 'research', background=background):
            chosen, ident = ledger.choose(ACCOUNTS, Decimal('100'), reserve=True)
            assert chosen['account_id'] == 'secondary'
            assert chosen['grant_id'] == historical['grant_id']
            ledger.finish(ident, 'success', Decimal('100'), {})
    summary = ledger.summary()
    assert summary['official_balance_known'] is False
    assert summary['balance_is_local_allowance'] is False
    assert summary['allocation_policy'] == 'shared_api_confirmed'
    assert Decimal(summary['used_or_reserved']) == 300
    assert Decimal(summary['production_used']) == 200
    assert Decimal(summary['development_used']) == 100
    assert summary['unknown_or_pending'] == 0
    historical_summary = summary['grants'][0]
    assert historical_summary['historical_evidence_only'] is True
    assert {'background_limit', 'manual_limit', 'reserve'}.isdisjoint(historical_summary)
    with ledger.db() as conn:
        assert conn.execute('SELECT background,purpose FROM requests ORDER BY rowid').fetchall() == [
            (0, 'production'), (1, 'production'), (0, 'development')]
        assert json.loads(conn.execute('SELECT record FROM grants').fetchone()[0]) == historical


def test_latest_started_historical_grant_is_attribution_not_availability(tmp_path):
    old = grant('secondary', end='2026-11-05T00:00:00Z')
    latest = {**grant('latest', end='2026-11-10T00:00:00Z'),
              'account_id': 'secondary', 'organization_id': 'secondary',
              'starts_at': '2026-11-05T00:00:00Z', 'verified_at': '2026-11-05T00:00:00Z'}
    future = {**grant('future', end='2027-01-01T00:00:00Z'),
              'account_id': 'secondary', 'organization_id': 'secondary',
              'starts_at': '2026-12-01T00:00:00Z', 'verified_at': '2026-12-01T00:00:00Z'}
    ledger = OpenAICreditLedger(tmp_path, [future, old, latest], job_limit=0, now=lambda: NOW)
    assert ledger.choose(ACCOUNTS, Decimal('1000'))[0] == latest
    reopened = OpenAICreditLedger(tmp_path, [], job_limit=0, now=lambda: NOW)
    assert reopened.choose(ACCOUNTS)[0] == latest


def test_no_grant_uses_distinct_accounting_references_without_fake_allowance(tmp_path):
    ledger = OpenAICreditLedger(tmp_path, [], job_limit=0, now=lambda: NOW)
    with ai_task('research'):
        secondary, ident = ledger.choose(ACCOUNTS, Decimal('2'), reserve=True)
        ledger.finish(ident, 'success', Decimal('1.25'), {})
    with ai_task('research'):
        primary, other = ledger.choose({'primary'}, Decimal('3'), reserve=True)
        ledger.finish(other, 'unknown', None, {'offline': True})
    reopened = OpenAICreditLedger(tmp_path, [], job_limit=0, now=lambda: NOW)
    assert reopened.choose(ACCOUNTS)[0] == secondary
    assert secondary['grant_id'] != primary['grant_id']
    with reopened.db() as conn:
        assert conn.execute('SELECT * FROM grants').fetchall() == []
        refs = conn.execute('SELECT account_id,id,record FROM account_references').fetchall()
        request_refs = {row[0] for row in conn.execute('SELECT grant_id FROM requests')}
    assert {account for account, _, _ in refs} == set(ACCOUNTS)
    assert request_refs == {ident for _, ident, _ in refs}
    for account, ident, record in refs:
        reference = json.loads(record)
        assert reference['account_id'] == account
        assert reference['grant_id'] == ident
        assert reference['record_kind'] == 'account_reference_only'
        assert reference['balance_verified'] is False
        assert {'allowance_usd', 'starts_at', 'expires_at'}.isdisjoint(reference)
    summary = reopened.summary()
    assert summary['grants'] == []
    assert summary['official_balance_known'] is False
    assert Decimal(summary['used_or_reserved']) == Decimal('4.25')
    assert summary['unknown_or_pending'] == 1


def test_job_cap_accumulates_across_accounts_without_reset(ledger):
    ledger.job_limit = Decimal('1')
    with ai_task('research'):
        chosen, ident = ledger.choose(ACCOUNTS, Decimal('.6'), reserve=True)
        ledger.finish(ident, 'success', Decimal('.6'), {})
        ledger.block(chosen, 'credit_balance_exhausted')
        with pytest.raises(OpenAICreditsUnavailable) as caught:
            ledger.choose(ACCOUNTS, Decimal('.41'), reserve=True)
        assert caught.value.status == 'openai_job_budget_exceeded'
        chosen, ident = ledger.choose(ACCOUNTS, Decimal('.4'), reserve=True)
        assert chosen['account_id'] == 'primary'
        ledger.finish(ident, 'success', Decimal('.4'), {})
        with pytest.raises(OpenAICreditsUnavailable) as caught:
            ledger.choose(ACCOUNTS, Decimal('.01'), reserve=True)
        assert caught.value.status == 'openai_job_budget_exceeded'
    assert ledger.job_limit == Decimal('1')
    assert Decimal(ledger.summary()['used_or_reserved']) == 1


def test_development_case_and_round_caps_survive_jobs_and_accounts(tmp_path):
    def case(name):
        return OpenAICreditLedger(tmp_path, [], job_limit=1, now=lambda: NOW,
                                 development=True, round_id='offline-round',
                                 round_limit=2, case_id=name)

    research = case('research')
    with ai_task('research'):
        chosen, ident = research.choose(ACCOUNTS, Decimal('.6'), reserve=True)
        research.finish(ident, 'success', Decimal('.6'), {})
        research.block(chosen, 'credit_balance_exhausted')
    with ai_task('research'):
        with pytest.raises(OpenAICreditsUnavailable) as caught:
            research.choose(ACCOUNTS, Decimal('.41'), reserve=True)
        assert caught.value.status == 'openai_job_budget_exceeded'
        chosen, ident = research.choose(ACCOUNTS, Decimal('.4'), reserve=True)
        assert chosen['account_id'] == 'primary'
        research.finish(ident, 'success', Decimal('.4'), {})
    news = case('news')
    with ai_task('news'):
        _, ident = news.choose(ACCOUNTS, Decimal('.9'), reserve=True)
        news.finish(ident, 'success', Decimal('.9'), {})
    before = financial_rows(news)
    with ai_task('radar'), pytest.raises(OpenAIUnavailable) as caught:
        case('radar').choose(ACCOUNTS, Decimal('.11'), reserve=True)
    assert caught.value.status == 'openai_round_budget_exceeded'
    assert financial_rows(news) == before
    assert Decimal(news.summary()['used_or_reserved']) == Decimal('1.9')


def test_legacy_financial_rows_and_unknown_charges_remain_unchanged(tmp_path):
    historical = grant('secondary', amount='0.01', end='2026-11-02T00:00:00Z')
    path = tmp_path / 'openai-credits.sqlite3'
    # Seed the previous schema without the new accounting-reference table.
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE grants (id TEXT PRIMARY KEY, record TEXT NOT NULL);
            CREATE TABLE requests (id TEXT PRIMARY KEY, grant_id TEXT NOT NULL,
                background INTEGER NOT NULL, command TEXT, run_id TEXT, operation TEXT,
                status TEXT NOT NULL, estimate TEXT NOT NULL, actual TEXT, detail TEXT,
                purpose TEXT NOT NULL DEFAULT 'production', round_id TEXT NOT NULL DEFAULT '',
                case_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE account_blocks (account_id TEXT PRIMARY KEY, grant_id TEXT, reason TEXT);
        ''')
        conn.execute('INSERT INTO grants VALUES (?,?)', (
            'secondary', json.dumps(historical, sort_keys=True, ensure_ascii=False)))
        conn.executemany('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', [
            ('old-paid', 'secondary', 0, 'research', 'old-job', 'model',
             'success', '20.000', '19.125', '{"original": "paid"}', 'production', '', ''),
            ('old-unknown', 'secondary', 1, 'news', 'unknown-job', 'model',
             'unknown', '7.750', None, '{"original": "unknown"}', 'production', '', ''),
            ('old-pending', 'secondary', 0, 'research', 'dev-job', 'model',
             'pending', '3.500', None, '{"original": "pending"}', 'development', '', ''),
        ])
        conn.execute('INSERT INTO account_blocks VALUES (?,?,?)',
                     ('secondary', 'secondary', 'credit_balance_exhausted'))
        before = {
            'grants': conn.execute('SELECT * FROM grants ORDER BY id').fetchall(),
            'requests': conn.execute('SELECT * FROM requests ORDER BY id').fetchall(),
            'blocks': conn.execute('SELECT * FROM account_blocks ORDER BY account_id').fetchall(),
        }
    ledger = OpenAICreditLedger(tmp_path, [historical], job_limit=0, now=lambda: NOW)
    with ai_task('research'):
        assert ledger.choose(ACCOUNTS)[0]['account_id'] == 'primary'
    summary = ledger.summary()
    assert Decimal(summary['used_or_reserved']) == Decimal('30.375')
    assert Decimal(summary['production_used']) == Decimal('26.875')
    assert Decimal(summary['development_used']) == Decimal('3.500')
    assert summary['unknown_or_pending'] == 2
    assert financial_rows(ledger) == before
    changed = OpenAICreditLedger(tmp_path, [{**historical, 'allowance_usd': '1000'}], now=lambda: NOW)
    with pytest.raises(ValueError):
        changed.choose(ACCOUNTS)
    assert financial_rows(ledger) == before


def test_exhaustion_survives_new_ledger_key_and_historical_grant(ledger, fallback):
    ledger.choose(ACCOUNTS)
    ledger.block(ledger.grants[1], 'credit_balance_exhausted')
    original_blocks = financial_rows(ledger)['blocks']
    newer = {**grant('next-secondary', end='2027-01-01T00:00:00Z'),
             'account_id': 'secondary', 'organization_id': 'secondary',
             'starts_at': '2026-12-01T00:00:00Z', 'verified_at': '2026-12-01T00:00:00Z'}
    reopened = OpenAICreditLedger(ledger.root, [*ledger.grants, newer], job_limit=0,
                                 now=lambda: datetime(2026, 12, 2, tzinfo=timezone.utc))
    calls = []

    def handler(request):
        calls.append(request.headers['Authorization'])
        return success(request)

    with ai_task('research'):
        routed(reopened, fallback, handler, {
            **ACCOUNTS, 'secondary': 'synthetic-replaced-key'}).generate_report('later job')
    assert calls == ['Bearer synthetic-first']
    assert financial_rows(reopened)['blocks'] == original_blocks
    fallback.generate_report.assert_not_called()


@pytest.mark.parametrize('account', ['primary', 'secondary'])
def test_operator_pause_on_either_account_stops_whole_selection(ledger, fallback, account):
    ledger.choose(ACCOUNTS)
    ledger.block(next(g for g in ledger.grants if g['account_id'] == account), 'user_pause')
    before = financial_rows(ledger)
    handler = Mock(side_effect=AssertionError('No HTTP dispatch during an operator pause'))
    router = routed(ledger, fallback, handler)
    with ai_task('research'):
        for action in [router.ensure_ready, lambda: router.generate_report('paused evidence')]:
            with pytest.raises(OpenAIUnavailable) as caught:
                action()
            assert caught.value.status == 'openai_account_blocked'
            assert not isinstance(caught.value, OpenAICreditsUnavailable)
    handler.assert_not_called()
    fallback.generate_report.assert_not_called()
    fallback.account.preflight.assert_not_called()
    assert financial_rows(ledger) == before


def test_exhaustion_block_never_overwrites_existing_operator_pause(ledger):
    chosen, _ = ledger.choose(ACCOUNTS)
    ledger.block(chosen, 'user_pause')
    before = financial_rows(ledger)['blocks']
    ledger.block(chosen, 'credit_balance_exhausted')
    assert financial_rows(ledger)['blocks'] == before
    with ai_task('research'), pytest.raises(OpenAIUnavailable) as caught:
        ledger.choose(ACCOUNTS)
    assert caught.value.status == 'openai_account_blocked'


def test_credit_rejection_settles_zero_but_preserves_pause_arriving_in_flight(ledger, fallback):
    calls = []

    def handler(request):
        calls.append(request.headers['Authorization'])
        secondary = next(g for g in ledger.grants if g['account_id'] == 'secondary')
        ledger.block(secondary, 'user_pause')
        return httpx.Response(429, json={'error': {'code': 'credit_balance_exhausted'}})

    with ai_task('research'), pytest.raises(OpenAIUnavailable) as caught:
        routed(ledger, fallback, handler).generate_report('in-flight evidence')
    assert caught.value.status == 'openai_account_blocked'
    assert calls == ['Bearer synthetic-second']
    with ledger.db() as conn:
        assert conn.execute('SELECT status,actual FROM requests').fetchall() == [('rejected', '0')]
        assert conn.execute('SELECT * FROM account_blocks').fetchall() == [
            ('secondary', 'secondary', 'user_pause')]
    fallback.generate_report.assert_not_called()


def test_unknown_development_round_blocks_new_case_without_releasing_charge(tmp_path):
    kwargs = dict(development=True, round_id='offline-round', round_limit=5,
                  job_limit=2, now=lambda: NOW)
    ledger = OpenAICreditLedger(tmp_path, [], case_id='research', **kwargs)
    with ai_task('research'):
        _, ident = ledger.choose(ACCOUNTS, Decimal('1.5'), reserve=True)
        ledger.finish(ident, 'unknown', None, {'offline': 'unreconciled'})
    before = financial_rows(ledger)
    other = OpenAICreditLedger(tmp_path, [], case_id='news', **kwargs)
    with ai_task('news'), pytest.raises(OpenAIUnavailable) as caught:
        other.choose({'primary'}, Decimal('.01'), reserve=True)
    assert caught.value.status == 'transport_unknown'
    assert financial_rows(other) == before
    summary = other.summary()
    assert Decimal(summary['used_or_reserved']) == Decimal('1.5')
    assert Decimal(summary['development_used']) == Decimal('1.5')
    assert summary['unknown_or_pending'] == 1
