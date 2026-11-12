"""Offline boundary tests: a deferred search is not an unknown paid request."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from research_center.ai_execution_control import (
    CONTROL, ExecutionControl, WorkStopped, execution_scope,
    is_graceful_search_stop, require_search_dispatch_time,
)
from research_center.deepseek_service import DeepSeekService, ai_task
from research_center.deepseek_search_service import DeepSeekSearchService
from research_center.models import CommandRequest
from research_center.news_repository import NewsRepository
from research_center import news_service as news
from research_center.openai_credit_service import OpenAIUnavailable
from research_center.search_routing import search_route
from tests.test_ai_schedule_stability import ledger_client, request_rows
from tests.test_news_schedule_balance import article, source, offline_refresh


def completed(request):
    body = json.loads(request.content)
    searches = ([{'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search'}}]
                if body.get('tools') else [])
    return httpx.Response(200, json={'model': body['model'], 'status': 'completed',
        'output': searches + [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}],
        'usage': {'input_tokens': 100, 'output_tokens': 10}})


@pytest.mark.parametrize('remaining,allowed', [(37.049111, False), (94.999, False), (95., True), (120., True)])
def test_dispatch_threshold_is_checked_without_sleep(remaining, allowed):
    clock = [0.]
    control = ExecutionControl(remaining, clock=lambda: clock[0])
    token = CONTROL.set(control)
    try:
        with ai_task('news', background=True):
            if allowed:
                require_search_dispatch_time(600)
            else:
                with pytest.raises(WorkStopped) as error:
                    require_search_dispatch_time(600)
                assert error.value.status == 'insufficient_search_time'
                assert error.value.diagnostics['http_sent'] is False
                assert error.value.diagnostics['required_seconds'] == 95
    finally:
        CONTROL.reset(token)


@pytest.mark.parametrize('command,background', [('news', False), ('research', True), ('radar', True)])
def test_other_workflows_and_manual_search_are_unchanged(command, background):
    with ai_task(command, background=background), execution_scope(1):
        require_search_dispatch_time(600)


def test_short_configured_timeout_and_cancel_remain_respected():
    with ai_task('news', background=True), execution_scope(40):
        require_search_dispatch_time(30)
    with ai_task('news', background=True), execution_scope(500) as control:
        control.stop()
        with pytest.raises(WorkStopped) as error:
            require_search_dispatch_time(600)
        assert error.value.status == 'work_deadline'
        assert not is_graceful_search_stop(error.value.status)


@pytest.mark.parametrize('stage', ['initial_search', 'critical_gap_search'])
def test_native_search_refuses_before_reserving_but_parent_can_classify(tmp_path, stage):
    calls = []
    ledger, client = ledger_client(tmp_path, httpx.MockTransport(lambda r: calls.append(r) or completed(r)))
    with ai_task('news', background=True), execution_scope(2400):
        with execution_scope(70), search_route('news', stage), pytest.raises(WorkStopped) as error:
            client.search('news sources')
        assert error.value.status == 'insufficient_search_time'
        assert request_rows(ledger) == []
        client.generate_json('classify retained sources')
    assert len(calls) == 1 and request_rows(ledger)[0][0] == 'success'


def test_native_cache_still_usable_in_final_seconds(tmp_path):
    calls = []
    ledger, client = ledger_client(tmp_path, httpx.MockTransport(lambda r: calls.append(r) or completed(r)))
    for remaining in (120, 5):
        with ai_task('news', background=True), execution_scope(remaining):
            result = client.search('same saved query')
    assert result['diagnostics']['cache_hit'] is True
    assert len(calls) == len(request_rows(ledger)) == 1


def test_guard_rechecks_after_reservation_and_marks_not_sent_zero(tmp_path, monkeypatch):
    ledger, client = ledger_client(tmp_path, httpx.MockTransport(lambda _: pytest.fail('Must not send')))
    clock = [0.]
    control = ExecutionControl(120, clock=lambda: clock[0])
    choose = ledger.choose
    def delayed_choose(*args, **kwargs):
        result = choose(*args, **kwargs)
        clock[0] = 40.
        return result
    monkeypatch.setattr(ledger, 'choose', delayed_choose)
    token = CONTROL.set(control)
    try:
        with ai_task('news', background=True), pytest.raises(WorkStopped):
            client.search('late dispatch')
    finally:
        CONTROL.reset(token)
    assert request_rows(ledger)[0][:2] == ('not_sent', '0')


def test_actual_transport_unknown_is_never_softened_or_released(tmp_path):
    def timeout(_):
        raise httpx.ReadTimeout('synthetic unknown, not a real request')
    ledger, client = ledger_client(tmp_path, httpx.MockTransport(timeout))
    with ai_task('news', background=True), execution_scope(200), pytest.raises(OpenAIUnavailable) as error:
        client.search('sent search')
    assert error.value.status == 'transport_unknown'
    before = request_rows(ledger)
    assert before[0][0] == 'unknown' and before[0][1] is None and Decimal(before[0][2]) > 0
    with ai_task('news', background=True), execution_scope(37), pytest.raises(WorkStopped):
        client.search('not sent search')
    assert request_rows(ledger) == before


def test_deepseek_fallback_search_also_checks_before_reserving():
    account = SimpleNamespace(reserve=Mock(side_effect=AssertionError('Must not reserve')))
    client = SimpleNamespace(account=account, timeout_seconds=180)
    with ai_task('news', background=True), execution_scope(37), pytest.raises(WorkStopped):
        DeepSeekService._post(client, 'v1/tools/search', {'query': 'offline'}, Decimal('.00075'))
    account.reserve.assert_not_called()


def test_discovery_retains_sources_events_and_deferred_query():
    original = source(article(1))
    def deferred(*args, **kwargs):
        require_search_dispatch_time(600)
    service = DeepSeekSearchService(SimpleNamespace())
    service.search = deferred
    request = CommandRequest(command='news', raw_text='/news refresh')
    with ai_task('news', background=True), execution_scope(37), pytest.raises(WorkStopped) as error:
        service.discover(request, [{'label': 'remaining', 'queries': ['one', 'two']}], seed_sources=[original])
    assert error.value.partial_discovery_sources[0].content == original.content
    diag = error.value.partial_discovery_diagnostics
    assert not diag['complete'] and diag['deferred_search']['query'] == 'one'
    assert diag['runs'][0]['unexecuted_query_count'] == 2


@pytest.mark.parametrize('reason,can_classify', [('insufficient_search_time', True), ('transport_unknown', False)])
def test_refresh_continues_only_for_not_sent_time_stop(tmp_path, monkeypatch, offline_refresh, reason, can_classify):
    center, calls = offline_refresh
    repo = NewsRepository(tmp_path / 'news.db')
    original = source(article(1))
    monkeypatch.setattr(news, '_discover_news_sources', lambda *args: ([deepcopy(original)],
        {'complete': False, 'stop_reason': reason, 'deferred_search': {'query': 'unexecuted query'}}))
    with ai_task('news', background=True):
        items, meta = news._run_news_refresh(center, repo)
    assert bool(calls) == can_classify
    assert meta['selected_classification_complete'] == can_classify
    assert meta['classification_status'] == 'partial_success'
    assert meta['minimax_diagnostics']['stop_reason'] == reason
    assert repo.get_by_url(original.url).full_text == original.content


@pytest.mark.parametrize('reason,failed', [('insufficient_search_time', False), ('transport_unknown', True)])
def test_scheduled_notice_does_not_confuse_deferred_search_with_unknown(reason, failed):
    import main
    meta = {'classification_status': 'partial_success', 'ai_classified_count': 1,
        'minimax_diagnostics': {'stop_reason': reason}}
    center = SimpleNamespace(config=SimpleNamespace(database_path='unused'), deepseek=None)
    async def run():
        with patch('research_center.orchestrator.ResearchCenter', return_value=center), \
             patch.object(main, 'NewsRepository'), \
             patch.object(main, 'run_news_refresh', return_value=([], meta)), \
             patch.object(main, 'load_config', return_value={'chat_id': 1}), \
             patch.object(main, '_scheduled_ai_gate', AsyncMock(return_value='')), \
             patch.object(main, '_flush_ai_notices', AsyncMock(return_value=[])), \
             patch.object(main, '_scheduled_outcome_notice', AsyncMock()) as notice:
            result = await main._scheduled_news_refresh(SimpleNamespace())
        assert notice.call_args.args[3]['failed'] == failed
        assert result['status'] == 'partial_success'
    with ai_task('news', background=True):
        asyncio.run(run())
