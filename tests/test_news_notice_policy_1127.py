"""Exercise the scheduled entry point, delivery history and both local ledgers."""
import asyncio
from datetime import date
from decimal import Decimal
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from research_center.ai_budget_service import MonthlyBudget, SCHEDULE_OWNS_AI_NOTICE
from research_center.deepseek_service import ai_task
from research_center.openai_credit_service import OpenAICreditLedger


DAY = date(2026, 11, 27)


@pytest.fixture
def scheduled(monkeypatch, tmp_path):
    import main
    from research_center.scheduled_outcome_notice import ScheduledOutcomeNotices

    store = ScheduledOutcomeNotices(tmp_path / 'outcomes')
    center = SimpleNamespace(deepseek=None, config=SimpleNamespace(database_path=tmp_path / 'news.db'))
    send = AsyncMock()
    monkeypatch.setattr(main, 'load_config', lambda: {'chat_id': 1})
    monkeypatch.setattr(main, 'get_tw_today', lambda: DAY)
    monkeypatch.setattr(main, 'get_workdata_path', lambda *parts: store.path.parent)
    monkeypatch.setattr(main, 'safe_send_bot_message', send)
    monkeypatch.setattr('research_center.orchestrator.ResearchCenter', lambda: center)
    return main, store, send


def outcome_rows(store):
    with store._db() as conn:
        return [json.loads(row[0]) for row in conn.execute('SELECT detail FROM outcomes ORDER BY rowid')]


@pytest.mark.parametrize('stop', ['insufficient_search_time', 'transport_unknown'])
def test_provider_events_stay_in_ledger_and_terminal_failure_notifies_once(scheduled, tmp_path, monkeypatch, stop):
    main, store, send = scheduled
    ledger = OpenAICreditLedger(tmp_path / 'openai')
    fallback = MonthlyBudget(tmp_path / 'deepseek')

    def refresh(*args, **kwargs):
        assert SCHEDULE_OWNS_AI_NOTICE.get()
        # An attempt can fail preflight or switch providers without the schedule failing.
        ledger.route('deepseek', 'openai_job_budget_exceeded')
        fallback.incident('insufficient_balance', background=True)
        if stop == 'transport_unknown':
            _, request_id = ledger.choose({'secondary': 'synthetic'}, Decimal('0.17'), reserve=True)
            ledger.finish(request_id, 'unknown', None, {'status': 'transport_unknown'})
            ledger.warn(stop, '費用未知', operation='web_search')
        return [], {'classification_status': 'partial_success', 'ai_classified_count': 0,
                    'selected_classification_complete': stop != 'transport_unknown',
                    'search_complete': False, 'minimax_diagnostics': {'stop_reason': stop},
                    'publication_date_gaps': 10, 'classification_diagnostics': {
                        'stop_reason': stop if stop == 'transport_unknown' else None}}
    monkeypatch.setattr(main, 'run_news_refresh', refresh)

    for _ in range(2):
        result = asyncio.run(main._scheduled_news_refresh(SimpleNamespace(bot=object())))
        assert result['status'] == 'partial_success'
    assert not SCHEDULE_OWNS_AI_NOTICE.get()
    assert len(outcome_rows(store)) == 2
    assert not ledger.claim_notice() and not fallback.claim_notice()
    with ledger.db() as conn:
        assert {row[0] for row in conn.execute('SELECT state FROM notices')} == {'local_only'}
        unknown = conn.execute('SELECT status,estimate,actual FROM requests').fetchall()
    if stop == 'transport_unknown':
        assert unknown == [('unknown', '0.17', None)] * 2
        send.assert_awaited_once()
        assert send.await_args.args[2].count('費用未知') == 1
    else:
        assert unknown == []
        send.assert_not_awaited()
    with fallback.db() as conn:
        assert conn.execute('SELECT state FROM notices').fetchall() == [('local_only',)]
    # Another job is not silenced by the news incident.
    with ai_task('topic_maintain', background=True):
        fallback.incident('insufficient_balance', background=True)
        ledger.warn('transport_unknown', '題材整理停止')
    assert ledger.claim_notice() and fallback.claim_notice()


def test_preflight_block_reports_once_without_starting_refresh(scheduled, monkeypatch):
    main, store, send = scheduled
    monkeypatch.setattr(main, '_scheduled_ai_gate', AsyncMock(return_value='openai_credits_unavailable'))
    refresh = Mock()
    monkeypatch.setattr(main, 'run_news_refresh', refresh)
    for _ in range(2):
        assert asyncio.run(main._scheduled_news_refresh(SimpleNamespace(bot=object())))['status'] == 'skipped_budget'
    refresh.assert_not_called()
    send.assert_awaited_once()
    assert '帳戶額度不足' in send.await_args.args[2]
    assert all(row['block_reason'] == 'openai_credits_unavailable' for row in outcome_rows(store))


def test_database_failure_not_silenced_after_successful_classification(scheduled, monkeypatch):
    main, store, send = scheduled
    monkeypatch.setattr(main, 'run_news_refresh', Mock(return_value=([], {
        'classification_status': 'ai_success', 'ai_classified_count': 10, 'selected_classification_complete': True})))
    monkeypatch.setattr(main, 'run_news_scheduled_latest', Mock(side_effect=sqlite3.OperationalError('database locked')))
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(main._scheduled_news_refresh(SimpleNamespace(bot=object())))
    send.assert_awaited_once()
    assert '讀取與整理已保存新聞失敗' in send.await_args.args[2]
    assert [(row['status'], row['classified']) for row in outcome_rows(store)] == [('failed', 10)]


def test_cancellation_is_one_interruption_and_not_success(scheduled, monkeypatch):
    main, store, send = scheduled
    monkeypatch.setattr(main, 'run_news_refresh', Mock(side_effect=asyncio.CancelledError()))
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main._scheduled_news_refresh(SimpleNamespace(bot=object())))
    send.assert_awaited_once()
    assert '中斷' in send.await_args.args[2]
    assert {row['status'] for row in outcome_rows(store)} == {'interrupted'}


def test_quarantine_only_is_not_a_classification_failure():
    import main
    detail = main._scheduled_news_detail([], {'selected_classification_complete': False,
        'unprocessed_selected_count': 2, 'classification_diagnostics': {'company_identity_quarantined': 2}})
    assert detail['selected_classification_complete'] is False
    assert not detail['failed']
