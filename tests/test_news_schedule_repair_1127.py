"""Offline contracts for lossless batch routing and truthful news status."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_center import news_service as news
from research_center.deepseek_service import ai_task, DeepSeekUnavailable
from research_center.news_repository import NewsRepository
from research_center.scheduled_outcome_notice import _news_issues, ScheduledOutcomeNotices, format_outcome_notice
from research_center.search_transmission import partition_search_context
from tests.test_news_schedule_balance import article, source, offline_refresh


def test_partition_preserves_each_event_and_orphan_without_mutating():
    rows = [{'kind': 'uncited_url', 'record': {'url': f'https://a/{i}', 'summary': f'事件{i}'}}
            for i in range(7)]
    rows += [{'kind': 'missing_data', 'value': ['尚缺官方正文']}]
    before = deepcopy(rows)
    packets = partition_search_context(rows, [['https://a/1'], ['https://a/2'], ['https://b/3']])
    assert rows == before and sum(map(len, packets)) == len(rows)
    assert rows[1] in packets[0] and rows[2] in packets[1]
    assert sorted(json.dumps(row, sort_keys=True) for group in packets for row in group) == sorted(
        json.dumps(row, sort_keys=True) for row in rows)


def test_multicompany_record_reaches_each_relevant_batch_with_full_evidence():
    row = {'events': [{'record': {'url': 'https://a/1', 'summary': '反證A'}},
                      {'record': {'url': 'https://a/2', 'summary': '矛盾B'}}]}
    assert partition_search_context([row], [['https://a/1'], ['https://a/2']]) == [[row], [row]]


def test_same_url_multiple_events_are_not_collapsed():
    rows = [{'url': 'https://a/1', 'text': text} for text in ['利多', '反證', '資料缺口']]
    assert partition_search_context(rows, [['https://a/1']]) == [rows]
    assert partition_search_context(rows, []) == []


@pytest.mark.parametrize('shrink', [False, True])
def test_classifier_complete_context_survives_budget_rebatch_and_retry(monkeypatch, shrink):
    items = [article(i) for i in range(7)]
    notes = [{'record': {'url': f'https://other/{i}', 'summary': f'完整底稿{i}'}}
             for i in range(19)]
    original = deepcopy(notes)
    prompts, guard_calls = [], []
    center = SimpleNamespace(config=SimpleNamespace(deepseek_only=True, deepseek_model='offline'),
        deepseek_organizer=SimpleNamespace(model='offline',
            account=SimpleNamespace(cached=lambda _: None, cache=lambda *args: None)))
    def guard(*args):
        guard_calls.append(args[1])
        if shrink and len(guard_calls) == 1:
            raise DeepSeekUnavailable('budget', status='job_budget_exceeded')
    def call(*args):
        prompts.append(args[2])
        if len(prompts) == 1:
            raise ValueError('offline malformed response')
        return SimpleNamespace(raw={str(i): {'category': 'company_news', 'summary': '保存判讀'} for i in range(3)})
    monkeypatch.setattr(news, '_scheduled_news_batch_guard', guard)
    monkeypatch.setattr(news, '_call_news_classifier', call)
    diagnostics = {}
    with ai_task('news', background=True):
        result = news._batch_classify_news(items, center, lambda _: None, ai_model='deepseek',
                                          search_context_notes=notes, diagnostics=diagnostics)
    assert len(result) == len(items) and notes == original
    assert all(f'完整底稿{i}' in ''.join(prompts) for i in range(19))
    assert diagnostics['search_context_delivery']['pending_records'] == 0
    assert diagnostics['search_context_archive'] == notes
    assert max(map(len, prompts)) < sum(map(len, prompts))


def test_incomplete_request_keeps_undelivered_context_pending(monkeypatch):
    notes = [{'text': f'底稿{i}'} for i in range(8)]
    center = SimpleNamespace(config=SimpleNamespace(deepseek_only=False), low_model_minimax=None)
    monkeypatch.setattr(news, '_call_news_classifier', lambda *args:
                        (_ for _ in ()).throw(DeepSeekUnavailable('unknown', status='transport_unknown')))
    diagnostics = {}
    result = news._batch_classify_news([article(i) for i in range(6)], center, lambda _: None,
                                      ai_model='gemini', search_context_notes=notes, diagnostics=diagnostics)
    assert len(result) == 6 and diagnostics['stop_reason'] == 'transport_unknown'
    assert diagnostics['search_context_delivery']['pending_records'] == len(notes)
    assert diagnostics['search_context_archive'] == notes


@pytest.mark.parametrize('tags,published,body,expected', [
    ([], '', '2026-11-26召開董事會，2026-11-27出貨', False),
    (['publication_unknown'], '', '發布時間尚待確認', False),
    (['publication_inferred'], '2026-11-26', '', False),
    (['publication_explicit'], '2026-11-28', '', False),
    (['publication_explicit'], '2026-11-26', '', True),
    (['publication_unknown'], '', '<meta property="article:published_time" content="2026-11-26T09:00:00+08:00">', True),
])
def test_publication_date_never_uses_arbitrary_event_or_storage_time(tags, published, body, expected):
    item = article(1)
    item.published_at, item.tags, item.full_text = published, tags, body
    item.summary = '台股事件摘要'
    with ai_task('news', background=True, report_date='2026-11-27'):
        news._backfill_refresh_item_dates([item])
        assert news._has_explicit_news_datetime(item) is expected
        audit = news.news_publication_audit([item])
    assert audit['gap_count'] == int(not expected)
    if expected:
        assert item.published_at == ('2026-11-26T09:00:00+08:00' if 'article:published_time' in body else '2026-11-26')


def test_refresh_date_audit_matches_saved_rows_and_reports_cohort_progress(tmp_path, monkeypatch, offline_refresh):
    center, calls = offline_refresh
    repo = NewsRepository(tmp_path / 'news.db')
    pending = [article(i, pending=True) for i in range(2)]
    repo.save_many(pending)
    no_date = replace(source(article(50)), published_date=None, date_status='unknown',
                      content='營收預計於2026-11-27公布，並非本文發布日期。')
    monkeypatch.setattr(news, '_discover_news_sources', lambda *args: ([no_date],
                        {'complete': False, 'stop_reason': 'insufficient_search_time'}))
    with ai_task('news', background=True):
        selected, meta = news._run_news_refresh(center, repo)
    assert meta['selected_classification_complete'] and not meta['search_complete']
    assert meta['publication_date_gaps'] == 1
    assert meta['pending_progress']['resolved_before'] == 2
    stored = repo._query('SELECT * FROM news_articles WHERE url=?', (no_date.url,))[0]
    assert not stored.published_at and not news._has_explicit_news_datetime(stored)
    assert news.news_publication_audit(selected)['gap_count'] == meta['publication_date_gaps']


def progress(before=2, carried=0, new=4):
    return {'before': before, 'remaining_before': carried, 'resolved_before': before-carried,
            'new_pending': new, 'remaining': carried+new}


@pytest.mark.parametrize('carried,new,backlog', [(0,4,False), (1,4,False), (2,0,True), (2,4,True)])
def test_backlog_alert_uses_actual_old_cohort_not_total_increase(carried, new, backlog):
    detail = {'pending': carried+new, 'pending_progress': progress(carried=carried, new=new)}
    assert ('backlog' in _news_issues(detail, {'pending': 2})) is backlog


def test_invalid_progress_cannot_silence_legacy_backlog():
    assert 'backlog' in _news_issues({'pending': 4, 'pending_progress': progress(new=-1)}, {'pending': 2})


def test_same_unresolved_article_still_alerts_when_other_old_articles_finish():
    previous = {'pending': 2, 'pending_progress': progress(carried=2, new=0) | {'carried_urls': ['stuck', 'done']}}
    current = {'pending': 2, 'pending_progress': progress(carried=1, new=1) | {'carried_urls': ['stuck']}}
    assert 'backlog' in _news_issues(current, previous)


def test_graceful_search_gap_remains_partial_and_is_local_only(tmp_path):
    async def run():
        store, send = ScheduledOutcomeNotices(tmp_path), AsyncMock()
        prior = {'status': 'ai_success', 'pending': 2, 'classified': 30}
        await store.observe('news', 'old', '2026-11-26', prior, send)
        detail = {'status': 'partial_success', 'selected_classification_complete': True,
            'pending': 4, 'pending_progress': progress(), 'classified': 30, 'failed': False,
            'search_complete': False, 'search_stop_reason': 'insufficient_search_time',
            'publication_date_gaps': 6, 'missing_categories': []}
        assert await store.observe('news', 'new', '2026-11-27', detail, send) == 'silent'
        assert await store.observe('news', 'repeat', '2026-11-27', detail, send) == 'silent'
        assert store.recorded_run_ids('news') == {'old', 'new', 'repeat'}
        text = format_outcome_notice('news', '2026-11-27', detail)
        assert '剩餘搜尋時間不足' in text and '來源日期缺口' in text
        assert '已處理 2 則' in text and '新增待辦 4 則' in text
        assert '連續兩天未減少' not in text and '分類未完整' not in text
        send.assert_not_awaited()
    asyncio.run(run())
