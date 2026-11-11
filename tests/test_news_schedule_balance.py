"""Offline regressions; run only through the parent's owned-temp pytest driver."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from research_center import news_service as news
from research_center.ai_execution_control import CONTROL, ExecutionControl, WorkStopped, execution_scope
from research_center.deepseek_service import DeepSeekUnavailable, ai_task
from research_center.deepseek_search_service import DeepSeekSearchService
from research_center.models import CommandRequest, SourceItem
from research_center.news_models import NewsItem
from research_center.news_repository import NewsRepository


def article(index, *, days=0, pending=False):
    return NewsItem(id=str(index), title=f'\u53f0\u80a1 \u516c\u53f8\u8a02\u55ae {index}',
        url=f'https://www.cna.com.tw/news/afe/item-{index}.aspx', source='CNA',
        published_at=(date.today() - timedelta(days=days)).isoformat(),
        created_at=(datetime.now() - timedelta(hours=2)).isoformat(),
        summary='\u53f0\u80a1\u71df\u6536\u8a02\u55ae\u98a8\u96aa' * 20, full_text='original body ' * 50,
        tags=['publication_explicit', *(['local_rule_fallback'] if pending else [])])


def source(item):
    return SourceItem(item.id, item.title, item.url, 'Level 2', published_date=item.published_at,
                      snippet=item.summary, content=item.full_text, date_status='explicit')


@pytest.fixture
def offline_refresh(monkeypatch):
    account = SimpleNamespace(cached=lambda _: None, cache=lambda *args: None)
    center = SimpleNamespace(config=SimpleNamespace(deepseek_only=True, deepseek_model='offline'),
        deepseek_organizer=SimpleNamespace(account=account, model='offline'))
    monkeypatch.setattr('stock_ai_bot.portfolio.portfolio_manager.load_portfolio', lambda: {})
    monkeypatch.setattr(news, '_enrich_sources_with_web_fetch', lambda *args, **kwargs: None)
    monkeypatch.setattr(news, '_scheduled_news_batch_guard', lambda *args: None)
    calls = []
    def classify(*args):
        calls.append(args[2])
        return SimpleNamespace(raw={str(i): {'category': 'company_news', 'summary': 'AI result'} for i in range(30)})
    monkeypatch.setattr(news, '_call_news_classifier', classify)
    return center, calls


def test_unchanged_classified_input_never_calls_classifier_again(tmp_path, monkeypatch, offline_refresh):
    center, calls = offline_refresh
    repo = NewsRepository(tmp_path / 'news.db')
    originals = [source(article(i)) for i in range(6)]
    monkeypatch.setattr(news, '_discover_news_sources', lambda *args: (deepcopy(originals), {'complete': True}))
    with ai_task('news', background=True):
        _, first = news._run_news_refresh(center, repo)
        count = len(calls)
        _, second = news._run_news_refresh(center, repo)
    assert first['ai_classified_count'] == 6 and count == 2
    assert len(calls) == count
    assert second['reused_classification_count'] == 6
    assert second['classification_reuse']['items'][0]['classified_at']
    assert 'not reanalysed' in second['classification_reuse']['note']


def test_thirty_cap_drains_backlog_and_new_events_fairly(tmp_path, monkeypatch, offline_refresh):
    center, calls = offline_refresh
    repo = NewsRepository(tmp_path / 'news.db')
    pending = [article(i, pending=True) for i in range(40)]
    pending_urls = {item.url for item in pending}
    repo.save_many(pending)
    originals = [source(article(100 + i)) for i in range(12)]
    monkeypatch.setattr(news, '_discover_news_sources', lambda *args: (deepcopy(originals), {'complete': True}))
    with ai_task('news', background=True):
        selected, first = news._run_news_refresh(center, repo)
        assert len(selected) == 30 and first['scheduled_balance']
        assert sum(item.url in pending_urls for item in selected) > 6
        assert any('item-100.' in item.url for item in selected)
        assert first['backlog_status'] == 'ordinary_deferred'
        assert first['search_complete'] and first['selected_classification_complete']
        news._run_news_refresh(center, repo)
    assert repo.pending_classification_summary()['eligible_recent'] == 0
    assert len(calls) > 6


def test_manual_limit_unchanged_and_scheduled_never_unlimited(monkeypatch):
    monkeypatch.delenv('NEWS_AI_CLASSIFY_LIMIT', raising=False)
    with ai_task('news'):
        assert news._classify_limit() == 18
    with ai_task('news', background=True):
        assert news._classify_limit() == 30
        monkeypatch.setenv('NEWS_AI_CLASSIFY_LIMIT', '0')
        assert news._classify_limit() == 30


def test_recent_older_pending_and_important_new_each_get_a_turn():
    recent = [article(i, pending=True) for i in range(20)]
    older = [article(i + 30, days=3, pending=True) for i in range(20)]
    fresh = [article(i + 100) for i in range(20)]
    fresh[-1].importance_score = 9999
    result = news._fair_scheduled_news(recent + older + fresh, {item.url for item in recent + older}, {})[:6]
    assert fresh[-1] in result
    assert any(item in recent for item in result) and any(item in older for item in result)


def test_snapshots_merge_events_body_and_keep_original_versions(tmp_path):
    repo = NewsRepository(tmp_path / 'news.db')
    first = replace(source(article(1)), search_summary_events=[{'event': 'first'}], content='long original ' * 100)
    later = replace(first, published_date='', date_status='unknown', content='short',
                    search_summary_events=[{'event': 'second'}])
    repo.save_discovery_checkpoint([first, later])
    merged = repo.recent_discovery_sources(date.today())
    assert len(merged) == 1 and merged[0].content == first.content
    assert {row['event'] for row in merged[0].search_summary_events} == {'first', 'second'}
    assert any('saved_publication:unknown' in marker for marker in merged[0].found_by)
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM news_discovery_sources').fetchone()[0] == 2


def test_fingerprint_requires_proven_original_and_prompt_model_identity(tmp_path):
    repo = NewsRepository(tmp_path / 'news.db')
    original = article(1)
    legacy = deepcopy(original)
    legacy.tags.append('ai_classified')
    repo.save_many([legacy])
    needed, keys = repo.prepare_classification_inputs([deepcopy(original)], classifier_context='prompt-A/model-A')
    assert needed  # No grandfathering from an AI-modified legacy row.
    classified = news._apply_classification_meta(needed, {'0': {'category': 'company_news'}})
    repo.save_classification_checkpoint(classified, keys)
    assert not repo.prepare_classification_inputs([deepcopy(original)], classifier_context='prompt-A/model-A')[0]
    assert repo.prepare_classification_inputs([deepcopy(original)], classifier_context='prompt-B/model-A')[0]
    assert repo.prepare_classification_inputs([deepcopy(original)], classifier_context='prompt-A/model-B')[0]
    changed = deepcopy(original)
    changed.full_text += ' actual new event'
    assert repo.prepare_classification_inputs([changed], classifier_context='prompt-A/model-A')[0]


def test_pending_window_uses_asof_not_rediscovery_and_unknown_stays_unknown(tmp_path):
    repo = NewsRepository(tmp_path / 'news.db')
    known, unknown, old = article(1, pending=True), article(2, pending=True), article(3, days=60, pending=True)
    unknown.published_at = ''
    unknown.tags = ['publication_unknown', 'local_rule_fallback']
    repo.save_many([known, unknown, old])
    rows = repo.query_pending_classification(limit=0, as_of=date.today())
    assert [row.url for row in rows] == [known.url]
    assert repo.get_by_url(unknown.url).published_at == ''
    assert not repo.query_pending_classification(limit=0, as_of=date.today() - timedelta(days=30))
    assert repo.pending_classification_summary()['historical_or_date_unknown'] == 2


@pytest.mark.parametrize('stop', ['expired', 'stopped', 'transport_unknown'])
def test_parent_stop_or_unknown_prevents_next_classifier_call(monkeypatch, stop):
    calls = []
    parent = ExecutionControl(None)
    token = CONTROL.set(parent)
    def classify(*args):
        calls.append(1)
        if stop == 'transport_unknown':
            raise DeepSeekUnavailable('unknown', 'transport_unknown')
        if stop == 'expired':
            parent.deadline = parent.clock() - 1
        else:
            parent.stop()
        return SimpleNamespace(raw={'0': {'category': 'company_news'}})
    monkeypatch.setattr(news, '_call_news_classifier', classify)
    monkeypatch.setattr(news, '_classify_batch_size', lambda: 1)
    diagnostics = {}
    try:
        with execution_scope(1500):
            result = news._classify_news_batches([article(1), article(2)], SimpleNamespace(), lambda _: None,
                ai_model='gemini', prompt_text='{news_batch_json}', label='offline', diagnostics=diagnostics)
    finally:
        CONTROL.reset(token)
    assert len(calls) == 1 and len(result) == 2
    assert 'local_rule_fallback' in result[-1].tags and diagnostics['stop_reason']


def test_search_incomplete_is_partial_even_when_selected_classification_completes(tmp_path, monkeypatch, offline_refresh):
    center, _ = offline_refresh
    repo = NewsRepository(tmp_path / 'news.db')
    monkeypatch.setattr(news, '_discover_news_sources', lambda *args: ([source(article(1))], {'complete': False}))
    with ai_task('news', background=True):
        _, meta = news._run_news_refresh(center, repo)
    assert meta['selected_classification_complete'] and not meta['search_complete']
    assert meta['classification_status'] == 'partial_success'
    assert meta['remaining_search_gaps_status'] == 'declared_incomplete'


def test_saved_source_quality_not_count_controls_new_queries(monkeypatch):
    service = DeepSeekSearchService(SimpleNamespace(provider='offline'))
    service.max_queries_per_task = 1
    request = CommandRequest(command='news', raw_text='/news refresh', report_date=date.today())
    task = {'label': 'news', 'queries': ['one query'], 'news_quality_policy': {'terms': ['\u53f0\u80a1']}}
    good = [source(article(i)) for i in range(4)]
    good[2] = replace(good[2], url='https://money.udn.com/money/story/1/2')
    good[3] = replace(good[3], url='https://money.udn.com/money/story/1/3')
    search = Mock(return_value={'results': []})
    monkeypatch.setattr(service, 'search', search)
    with ai_task('news', background=True):
        result = service.discover(request, [task], seed_sources=good)
        assert result.diagnostics['runs'][0]['early_stop'] == 'saved_source_quality_satisfied'
        search.assert_not_called()
        poor = [replace(source(article(i + 50)), published_date='2001-01-01') for i in range(80)]
        result = service.discover(request, [task], seed_sources=poor)
    assert search.call_count == 1 and not result.diagnostics['complete']
    assert len(result.sources) == 80


def test_original_parent_deadline_keeps_five_minute_reserve(monkeypatch):
    observed = []
    def refresh(*args):
        current = CONTROL.get()
        observed.append(current.remaining())
        assert current.parent.parent is parent
        return [], {}
    monkeypatch.setattr(news, '_run_news_refresh', refresh)
    with ai_task('news', background=True), execution_scope(1200) as parent:
        news.run_news_refresh(SimpleNamespace(), SimpleNamespace())
        assert 0 < observed[0] <= 900
        assert parent.remaining() > 1190


def test_scheduled_batches_shrink_against_existing_ledger_without_reserving(monkeypatch):
    from research_center.openai_credit_service import OpenAICreditsUnavailable
    checks, sent = [], []
    def choose(accounts, estimate):
        checks.append(estimate)
        if estimate > 1:
            raise OpenAICreditsUnavailable('small existing allowance', 'openai_job_budget_exceeded')
    client = SimpleNamespace(model='offline', account=SimpleNamespace(cached=lambda _: None, cache=lambda *args: None),
        estimate_request=lambda prompt, exact=False: Decimal(len(json.loads(prompt))),
        ledger=SimpleNamespace(choose=choose), accounts={'offline': 'not-a-key'})
    center = SimpleNamespace(config=SimpleNamespace(deepseek_only=True, deepseek_model='offline'), deepseek_organizer=client)
    def classify(center, model, prompt, timeout):
        sent.append(json.loads(prompt))
        return SimpleNamespace(raw={'0': {'category': 'company_news'}})
    monkeypatch.setattr(news, '_call_news_classifier', classify)
    diagnostics = {}
    with ai_task('news', background=True):
        result = news._classify_news_batches([article(i) for i in range(4)], center, lambda _: None,
            ai_model='deepseek', prompt_text='{news_batch_json}', label='offline', diagnostics=diagnostics)
    assert len(result) == 4 and len(sent) == 4
    assert all(len(packet) == 1 for packet in sent)
    assert diagnostics['budget_adapted_batches'] > 0 and max(checks) == 3


def test_retry_unknown_never_starts_a_later_batch(monkeypatch):
    call = Mock(side_effect=[ValueError('invalid reply'), DeepSeekUnavailable('unknown', 'transport_unknown')])
    monkeypatch.setattr(news, '_call_news_classifier', call)
    monkeypatch.setattr(news, '_classify_batch_size', lambda: 1)
    result = news._classify_news_batches([article(1), article(2)], SimpleNamespace(), lambda _: None,
        ai_model='gemini', prompt_text='{news_batch_json}', label='offline')
    assert call.call_count == 2 and all('local_rule_fallback' in item.tags for item in result)
