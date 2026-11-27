import asyncio
from dataclasses import asdict
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from research_center.deepseek_search_service import DeepSeekSearchService, _news_task_quality
from research_center.deepseek_service import TASK, ai_task
from research_center.models import CommandRequest
from research_center.news_search_window import NEWS_EDITION_WINDOW, NEWS_PRIOR_EDITION_MISSED, NewsSearchWindow
from research_center.search_routing import SEARCH_ROUTE
from research_center.source_rank import make_source_items
from research_center.web_fetch_service import _html_publication_times


WINDOW = NewsSearchWindow(date(2026, 11, 27))


@pytest.mark.parametrize('stamp,status,lane', [
    ('2026-11-26T17:59:59+08:00', 'explicit', 'supplement'),
    ('2026-11-26T10:00:00Z', 'explicit', 'primary'),
    ('2026-11-27T17:59:59+08:00', 'explicit', 'primary'),
    ('2026-11-27T18:00:00+08:00', 'explicit', 'outside'),
    ('2026-11-23T18:00:00+08:00', 'explicit', 'supplement'),
    ('2026-11-23T17:59:59+08:00', 'explicit', 'outside'),
    ('2026-11-23', 'explicit', 'date_pending'),
    ('2026-11-24', 'explicit', 'supplement'),
    ('2026-11-26', 'explicit', 'date_pending'),
    ('2026-11-27', 'explicit', 'date_pending'),
    ('2026-11-28', 'explicit', 'outside'),
    ('2026-11-27T12:00:00+08:00', 'inferred', 'date_pending'),
    ('2026-11-27T12:00:00+08:00', 'conflict', 'date_pending'),
])
def test_publication_lanes(stamp, status, lane):
    assert WINDOW.lane({'published_date': stamp, 'date_status': status}) == lane


def test_publisher_time_survives_duplicate_and_preserves_conflicts():
    from bs4 import BeautifulSoup
    url = 'https://cna.com.tw/news/afe/sample.aspx'
    soup = BeautifulSoup('<script type="application/ld+json">{"datePublished":"2026-11-26T10:00:00Z",'
                         '"dateModified":"2026-11-27T10:00:00Z"}</script>', 'html.parser')
    assert _html_publication_times(soup, '2026-11-26') == ('2026-11-26T18:00:00+08:00',)
    first = {'url': url, 'published_date': '2026-11-26T10:00:00Z'}
    date_only = {'url': url, 'published_date': '2026-11-26'}
    source, = make_source_items([first, date_only])
    assert source.published_date == '2026-11-26'
    assert WINDOW.lane(source) == 'primary'
    conflicting, = make_source_items([asdict(source), {**date_only, 'published_date': '2026-11-26T17:00:00+08:00'}])
    assert WINDOW.lane(conflicting) == 'date_pending'


def test_supplement_only_cannot_pass_primary_quality():
    policy = {'terms': ['半導體']}
    urls = [f'https://{site}/news/{i}' for i, site in enumerate(['cna.com.tw', 'money.udn.com'] * 2)]
    rows = [{'url': url, 'title': f'半導體需求與供應商展望 {i}', 'content': '半導體訂單需求增加，需核對供應商公告與可驗證來源。'*12,
             'published_date': '2026-11-27T11:30:00+08:00', 'found_by': ['news_primary_search']}
            for i, url in enumerate(urls)]
    assert _news_task_quality(rows, policy, WINDOW.scheduled_day, window=WINDOW)['passed']
    for policy_marker, published_date in [('news_supplement_search', '2026-11-27T11:30:00+08:00'),
                                          ('news_primary_search', '2026-11-25T12:00:00+08:00')]:
        moved = [{**row, 'published_date': published_date, 'found_by': [policy_marker]} for row in rows]
        outcome = _news_task_quality(moved, policy, WINDOW.scheduled_day, window=WINDOW)
        assert outcome['recent_substantive_articles'] == 0 and not outcome['passed']

    from research_center.news_service import _news_temporal_record, _sources_to_news_items
    source, = make_source_items([{**rows[0], 'found_by': ['news_supplement_search']}])
    item, = _sources_to_news_items([source])
    token = NEWS_EDITION_WINDOW.set(WINDOW)
    try:
        assert _news_temporal_record(item)['scheduled_publication_lane'] == 'outside'
    finally:
        NEWS_EDITION_WINDOW.reset(token)


def test_real_planner_caps_four_day_supplement_and_keeps_primary_gap(monkeypatch):
    service = DeepSeekSearchService(SimpleNamespace(provider='openai_router'))
    request = CommandRequest(command='news', raw_text='/news refresh', target='台股', target_type='news')
    tasks = [{'label': label, 'objective': '核對官方政策', 'queries': [f'{label} 需求'],
              'news_quality_policy': {'terms': ['半導體'], 'require_official': True}} for label in ('政策甲','政策乙')]
    dispatched = []
    def search(query, report_date):
        route = SEARCH_ROUTE.get()
        dispatched.append((query, report_date, route['stage']))
        date_only = route['stage'] == 'initial_search'
        return {'results': [{'url': f'https://cna.com.tw/news/afe/{len(dispatched)}', 'title': '半導體需求',
                             'content': '半導體近期發展。'*30,
                             'published_date': '2026-11-27' if date_only else '2026-11-24T12:00:00+08:00'}]}
    monkeypatch.setattr(service, 'search', search)
    monkeypatch.setattr('research_center.web_fetch_enrichment.recover_search_publication_dates', Mock())
    token = NEWS_EDITION_WINDOW.set(WINDOW)
    try:
        with ai_task('news', background=True, report_date='2026-11-28'):
            result = service.discover(request, tasks)
    finally:
        NEWS_EDITION_WINDOW.reset(token)
    assert len(dispatched) == 3
    assert [stage for _, _, stage in dispatched] == ['initial_search', 'initial_search', 'critical_gap_search']
    assert all(day == '2026-11-27' and len(query) <= 128 and '台灣' in query for query, day, _ in dispatched)
    assert all('2026-11-26 18:00 起至 2026-11-27 18:00 前' in row[0] for row in dispatched[:2])
    assert '2026-11-23 18:00 起至 2026-11-26 18:00 前' in dispatched[2][0]
    assert len(result.sources) == 3
    assert WINDOW.audit(result.sources)['counts'] == {'primary': 0, 'supplement': 1, 'date_pending': 2, 'outside': 0}
    assert not result.diagnostics['complete']


def test_edition_query_does_not_leave_today_or_delayed_clock_in_provider_query():
    for supplement in (False, True):
        query = WINDOW.query('台股 今日 最新 近24小時 2026-11-28 2026/11/28 聯發科 2454 官方風險', supplement=supplement)
        assert '聯發科 2454 官方風險' in query and '2026-11-28' not in query and '2026/11/28' not in query
        assert '今日' not in query and '近24小時' not in query


def test_saved_supplement_avoids_buying_another_missed_edition_search(monkeypatch):
    service = DeepSeekSearchService(SimpleNamespace(provider='openai_router'))
    request = CommandRequest(command='news', raw_text='/news refresh', target='台股', target_type='news')
    task = {'label': '晶片', 'queries': ['晶片'], 'news_quality_policy': {'terms': ['半導體']}}
    source = {'url': 'https://cna.com.tw/news/afe/1', 'published_date':'2026-11-24', 'title':'半導體事件',
              'snippet': '半導體供應鏈原文背景。'*30}
    search = Mock(return_value={'results': []})
    monkeypatch.setattr(service, 'search', search)
    token = NEWS_EDITION_WINDOW.set(WINDOW)
    missed = NEWS_PRIOR_EDITION_MISSED.set(True)
    try:
        with ai_task('news', background=True):
            result = service.discover(request, [task], seed_sources=[source])
    finally:
        NEWS_PRIOR_EDITION_MISSED.reset(missed)
        NEWS_EDITION_WINDOW.reset(token)
    search.assert_called_once()
    assert len(result.sources) == 1 and not result.diagnostics['complete']


def test_enqueued_news_uses_original_day_and_resets_context(monkeypatch):
    import main
    enqueue = AsyncMock()
    monkeypatch.setattr(main, 'enqueue_scheduled_task', enqueue)
    monkeypatch.setattr(main, 'get_tw_today', lambda: WINDOW.scheduled_day)
    ctx = SimpleNamespace(job=None)
    asyncio.run(main.scheduled_news_refresh(ctx))
    _, _, run = enqueue.await_args.args
    monkeypatch.setattr(main, 'get_tw_today', lambda: date(2026, 11, 28))
    async def actual(context):
        assert NEWS_EDITION_WINDOW.get() == WINDOW
        assert TASK.get() == ('news', True, '2026-11-27')
    monkeypatch.setattr(main, '_run_scheduled_news_refresh', actual)
    asyncio.run(run())
    assert NEWS_EDITION_WINDOW.get() is None


def test_date_recovery_prioritizes_current_citations_without_deleting_seeds(monkeypatch):
    from research_center.deepseek_search_service import _recover_news_quality_dates
    older = [{'url': f'https://news.example/a/{i}', 'title':'舊保存來源'} for i in range(8)]
    current = {'url':'https://news.example/a/9', 'title':'本輪候選',
               'found_by':[WINDOW.query('查詢'), 'news_primary_search'], 'search_summary_events':[{'record': {'title':'本輪候選'}}]}
    rows = [*older, current]
    recovered = Mock()
    monkeypatch.setattr('research_center.web_fetch_enrichment.recover_search_publication_dates', recovered)
    _recover_news_quality_dates(SimpleNamespace(command='news'), rows, set(), {}, None, window=WINDOW)
    checked = recovered.call_args.args[1]
    assert checked[0].url == current['url'] and len(checked) == 4
    assert {row['url'] for row in rows} == {row['url'] for row in [*older, current]}


def test_new_article_queue_prioritizes_confirmed_main_window():
    from research_center.news_models import NewsItem
    from research_center.news_service import _fair_scheduled_news
    rows = [NewsItem(title='大盤走勢', url='https://news.example/historical', published_at='2026-11-24', importance_score=100),
            NewsItem(title='大盤走勢', url='https://news.example/pending', published_at='2026-11-27'),
            NewsItem(title='大盤走勢', url='https://news.example/new', published_at='2026-11-27T09:00:00+08:00')]
    token = NEWS_EDITION_WINDOW.set(WINDOW)
    try:
        ordered = _fair_scheduled_news(rows, set(), {})
    finally:
        NEWS_EDITION_WINDOW.reset(token)
    assert ordered[0] is rows[2] and {item.url for item in ordered} == {item.url for item in rows}
