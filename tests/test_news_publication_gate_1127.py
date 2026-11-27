"""Publication provenance must agree across quality, storage and AI context."""
from dataclasses import asdict, replace
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from research_center.deepseek_search_service import _news_task_quality
from research_center.models import CommandRequest
from research_center.news_models import NewsItem
from research_center.news_repository import NewsRepository
from research_center.source_date_normalizer import news_in_publication_window, news_publication_record
from research_center.source_rank import make_source_items

DAY = date(2026, 11, 27)
END = datetime.fromisoformat('2026-11-27T18:00:00+08:00')
URL = 'https://money.udn.com/news/20261127/article'


@pytest.mark.parametrize('extra', [
    {}, {'date_status': 'explicit'}, {'date': '2026-11-27'},
    {'event_date': '2026-11-27'}, {'updated_date': '2026-11-27'},
    {'fetched_at': END.isoformat()}, {'created_at': END.isoformat()},
    {'published_date': '2026-11-27', 'date_status': 'inferred'},
    {'published_date': '2026-11-27', 'tags': ['publication_unknown']},
    {'published_date': '2026-11-27', 'found_by': ['source_date:inferred']},
    {'published_at': '1 hours ago'},
    {'published_at': '1 hours ago', 'date_status': 'explicit'},
    {'full_text': '{"datePublished":"1 hours ago"}'},
    {'published_date': '2026-11-27', 'document_kind': 'reference_link'},
])
def test_no_storage_event_url_or_weak_date_becomes_recent(extra):
    raw = {'url': URL, 'title': '營收公布日期 2026-11-27', **extra}
    assert not news_in_publication_window(raw, 48, END)
    assert not news_in_publication_window(make_source_items([raw])[0], 48, END)


@pytest.mark.parametrize('extra', [
    {'published_at': END.isoformat()}, {'published_date': '2026-11-27'},
    {'datePublished': '2026-11-27'},
    {'content': '<meta property="article:published_time" content="2026-11-27T09:00:00+08:00">'},
    {'date_status': 'inferred', 'content': '{"datePublished":"2026-11-27"}'},
])
def test_real_publication_evidence_is_usable(extra):
    assert news_in_publication_window({'url': URL, **extra}, 48, END)


def test_timezone_future_and_conflict_are_not_hidden():
    assert news_publication_record({'published_at': '2026-11-26T20:00:00Z'}, END)['published_date'] == '2026-11-27'
    assert not news_in_publication_window({'published_at': '2026-11-27T20:00:00+08:00'}, 48, END)
    conflict = {'published_date': '2026-11-27', 'content': '{"datePublished":"2026-11-20"}'}
    assert news_publication_record(conflict, END)['publication_status'] == 'conflict'
    assert not news_in_publication_window(conflict, 48, END)
    metadata = {'content':'<meta property="article:published_time" content="2026-11-26T20:00:00Z">'}
    assert news_publication_record(metadata, END)['published_date'] == '2026-11-27'


@pytest.mark.parametrize('reverse', [False, True])
def test_same_url_date_correction_keeps_events_and_provenance(reverse):
    rows = [dict(url=URL, date='2026-11-26', snippet='舊事件', search_summary_events=[{'event':'舊事件'}]),
            dict(url=URL, published_date='2026-11-27', content='完整正文', search_summary_events=[{'event':'新事件'}])]
    item = make_source_items(rows[::-1] if reverse else rows)[0]
    assert item.date_status == 'explicit' and item.published_date == '2026-11-27'
    assert item.snippet == '舊事件' and item.content == '完整正文'
    assert {row['event'] for row in item.search_summary_events} == {'舊事件', '新事件'}
    assert news_in_publication_window(item, 48, END)


@pytest.mark.parametrize('reverse', [False, True])
def test_same_url_conflicting_dates_stay_pending(reverse):
    rows = [dict(url=URL, published_date='2026-11-27', content='第一版'),
            dict(url=URL, published_date='2026-11-20', content='第二版')]
    item = make_source_items(rows[::-1] if reverse else rows)[0]
    assert item.date_status == 'conflict'
    assert '第一版' in item.content and '第二版' in item.content
    assert not news_in_publication_window(item, 48, END)
    assert make_source_items([asdict(item)])[0].date_status == 'conflict'


@pytest.mark.parametrize('kind', ['url', 'inferred', 'unknown', 'confirmed'])
def test_quality_uses_the_same_provenance_gate(kind):
    rows = []
    for n, host in enumerate(['cna.com.tw', 'money.udn.com', 'ctee.com.tw', 'cnyes.com']):
        extra = {} if kind == 'url' else {'published_date':'2026-11-27', 'date_status':kind if kind != 'confirmed' else 'explicit'}
        rows.append({'url':f'https://{host}/news/20261127/{n}', 'title':'半導體營收', 'snippet':'半導體營收與市場風險，仍待觀察。'*30, **extra})
    result = _news_task_quality(rows, {'terms':['半導體']}, DAY)
    assert result['passed'] == (kind == 'confirmed')
    assert result['recent_substantive_articles'] == (4 if kind == 'confirmed' else 0)
    assert result['url_dated_articles'] == 0


def test_repository_and_all_consumers_keep_unverified_only_in_archive(tmp_path):
    repo = NewsRepository(tmp_path / 'news.db')
    rows = [NewsItem(url=URL+str(i), title='欣陸營收', published_at=value, category='company',
                    related_symbols=['3703'], related_topics=['營收'], tags=['local_rule_fallback', *tags])
            for i, (value, tags) in enumerate([
                (datetime.now().isoformat(), []), (None, []),
                (datetime.now().isoformat(), ['publication_inferred']),
                (datetime.now().isoformat(), ['publication_unknown']),
                (datetime.now().isoformat(), ['publication_conflict'])])]
    repo.save_many(rows)
    assert repo.article_count() == len(repo.query_saved_recent()) == 5
    for items in [repo.query_recent(), repo.query_all_recent(), repo.query_by_symbol('3703'),
                  repo.query_by_topic('營收'), repo.query_by_category('company'), repo.query_pending_classification(limit=0)]:
        assert [item.url for item in items] == [rows[0].url]
    assert repo.count_recent() == 1
    assert repo.pending_classification_summary() == {'pending_total':5, 'eligible_recent':1, 'historical_or_date_unknown':4}
    assert repo.get_by_url(rows[2].url).published_at == rows[2].published_at


def test_repository_duplicate_corrections_and_conflicts_share_source_rule(tmp_path):
    repo = NewsRepository(tmp_path / 'news.db')
    waiting = NewsItem(url=URL, title='待查', published_at='2026-11-26', tags=['publication_inferred', 'local_rule_fallback'])
    explicit = replace(waiting, published_at='2026-11-27', tags=['publication_explicit', 'ai_classified'])
    repo.save_many([waiting, explicit])
    assert 'publication_explicit' in repo.get_by_url(URL).tags
    repo.save_many([replace(explicit, published_at='2026-11-20')])
    assert 'publication_conflict' in repo.get_by_url(URL).tags
    assert not news_in_publication_window(repo.get_by_url(URL), 48, END)


def test_context_and_radar_never_use_created_at_as_publication():
    from research_center.date_aware_context import build_saved_news_context
    from stock_ai_bot.monitoring import radar_service as radar
    valid = NewsItem(url=URL, title='欣陸營收', published_at='2026-11-27', related_symbols=['3703'])
    waiting = replace(valid, url=URL+'pending', tags=['publication_inferred'])
    unknown = replace(valid, url=URL+'unknown', published_at=None, created_at='2026-11-27')
    repo = SimpleNamespace(query_saved_recent=lambda **kw: [valid, waiting, unknown])
    ctx = build_saved_news_context(CommandRequest('research', '/research 3703', target='3703', report_date=DAY),
                                   {'stock':{'code':'3703','name':'欣陸'}}, repository=repo)
    assert ctx['usable_count'] == 1 and ctx['publication_pending_count'] == 2
    candidate = radar.RadarCandidate(code='3703', name='欣陸')
    with patch.object(radar, 'NewsRepository', return_value=repo):
        radar._attach_local_news([candidate], DAY)
    assert [row['url'] for row in candidate.news_items] == [URL]
    pending = candidate.evidence_pack['publication_pending_news']
    assert {row['source']['url'] for row in pending} == {waiting.url, unknown.url}
    assert radar._build_radar_evidence_pack(candidate, DAY, {})['publication_pending_news'] == pending


def test_backfill_does_not_erase_relative_date_provenance():
    from research_center.news_service import _backfill_refresh_item_dates
    item = NewsItem(url=URL, title='待查日期', published_at='1 hours ago')
    _backfill_refresh_item_dates([item])
    assert 'publication_inferred' in item.tags
    assert not news_in_publication_window(item, 48)


def test_legacy_source_normalization_and_news_persistence_keep_pending_status():
    from research_center.models import SourceItem
    from research_center.search_source_normalizer import normalize_source_items
    from research_center.news_context_service import _source_to_news_item
    from research_center.source_date_normalizer import source_publication_status
    raw = SourceItem('S1', '台股新聞', URL, 'Level 2', published_date='2026-11-27',
                     found_by=['source_date:unknown'], date_status='explicit')
    request = CommandRequest('research', '/research', report_date=DAY)
    item = normalize_source_items([raw], request)[0]
    assert item.date_status == source_publication_status(raw) == 'unknown'
    stored = _source_to_news_item(request, {}, item)
    assert not news_in_publication_window(stored, 48, END)


@pytest.mark.parametrize('provided', ['datePublished', 'date', 'published_date'])
def test_discovery_adapter_preserves_publication_proof_instead_of_renaming_generic_date(provided):
    from research_center.deepseek_search_service import DeepSeekSearchService
    service = DeepSeekSearchService(SimpleNamespace())
    row = {'url':URL, 'title':'台股新聞', 'summary':'台股風險與需求', provided:'2026-11-27'}
    with patch.object(service, 'search', return_value={'results':[row]}):
        result = service.discover(CommandRequest('news','/news refresh',report_date=DAY), [{'queries':['saved query']}])
    item = result.sources[0]
    assert item.date_status == ('inferred' if provided == 'date' else 'explicit')
    assert news_in_publication_window(item, 48, END) == (provided != 'date')


def test_openai_tool_annotation_not_model_generated_date_proves_publication():
    from research_center.openai_service import parse_search_response
    import json
    data = {'output':[
        {'type':'web_search_call', 'action':{'sources':[{'url':URL, 'published_date':'2026-11-27'}]}},
        {'type':'message','content':[{'type':'output_text','text':json.dumps({'results':[{'url':URL,'date':'2026-11-20','summary':'事件摘要'}]})}]},
    ]}
    item = parse_search_response(data)['results'][0]
    assert item['date_status']=='explicit' and item['published_date']=='2026-11-27'
    del data['output'][0]['action']['sources'][0]['published_date']
    assert not news_in_publication_window(parse_search_response(data)['results'][0], 48, END)


def test_body_metadata_correction_survives_source_reconstruction():
    row = {'url':URL, 'published_date':'2026-11-25', 'found_by':['source_date:inferred'],
           'date_status':'inferred', 'content':'{"datePublished":"2026-11-27"}'}
    first = make_source_items([row])[0]
    assert first.date_status == 'explicit' and first.published_date == '2026-11-27'
    assert 'source_date:inferred' not in first.found_by
    assert news_in_publication_window(make_source_items([asdict(first)])[0], 48, END)


def test_seed_metadata_recovery_updates_quality_and_returned_sources_together():
    from research_center.deepseek_search_service import DeepSeekSearchService
    from research_center.deepseek_service import ai_task
    service = DeepSeekSearchService(SimpleNamespace(provider='openai_router'))
    seeds = [dict(url=f'https://{host}/article', title='台股供應鏈', snippet='台股供應鏈需求風險。'*30,
                  date_status='unknown') for host in ['cna.com.tw','ctee.com.tw','money.udn.com','cnyes.com']]
    def recover(request, sources, diagnostics, progress):
        sources[:] = [replace(item, published_date='2026-11-27', date_status='explicit',
                              found_by=['source_date:explicit']) for item in sources]
    tasks = [{'label':'供應鏈與產業','queries':['saved-1','saved-2','saved-3'],
              'news_quality_policy':{'terms':['供應鏈']}}]
    with ai_task('news',background=True,report_date=DAY.isoformat()), \
         patch.object(service,'search',return_value={'results':[]}), \
         patch('research_center.web_fetch_enrichment.recover_search_publication_dates',side_effect=recover), \
         patch('research_center.search_routing.important_search_gaps',return_value=[]):
        result = service.discover(CommandRequest('news','/news refresh',report_date=DAY), tasks, seed_sources=seeds)
    assert result.diagnostics['runs'][0]['quality']['passed']
    assert len(result.sources)==4 and all(news_in_publication_window(item,48,END) for item in result.sources)


@pytest.mark.parametrize('old_status, old_day, expected', [
    ('inferred', '2026-11-26', 'explicit'), ('unknown', None, 'explicit'),
    ('explicit', '2026-11-20', 'conflict'), ('conflict', '2026-11-27', 'conflict')])
def test_body_recovery_updates_provenance_without_clearing_conflicts(old_status, old_day, expected):
    from research_center.web_fetch_enrichment import _enrich_sources_with_web_fetch
    source = make_source_items([{'url':URL, 'title':'台股新聞', 'snippet':'待核對正文',
                                 'published_date':old_day, 'date_status':old_status}])[0]
    fetched = SimpleNamespace(url=URL, title='台股新聞', content='取得的完整正文', published_date='2026-11-27',
        date_status='explicit', fetch_provider='offline', content_status='success', fetch_quality='good', failure_reason=None)
    sources = [source]
    with patch('research_center.web_fetch_enrichment.WebFetchService') as service:
        service.return_value.fetch_many.return_value = SimpleNamespace(results=[fetched], diagnostics={})
        _enrich_sources_with_web_fetch(CommandRequest('news', '/news refresh', report_date=DAY), sources, {})
    assert sources[0].date_status == expected
    assert news_in_publication_window(sources[0], 48, END) == (expected == 'explicit')
