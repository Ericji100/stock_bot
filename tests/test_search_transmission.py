"""Offline regression checks for search events, gaps and citation boundaries."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import date
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from research_center.openai_service import parse_search_response, evidence_url, OpenAIService
from research_center.deepseek_search_service import DeepSeekSearchService
from research_center.models import CommandRequest, SourceItem
from research_center.search_transmission import collect_search_context, source_events_packet, search_context_prompt
from research_center.source_rank import make_source_items


def response(records, *, urls=None, **context):
    return {'output': [
        {'type': 'web_search_call', 'action': {'sources': [{'url': url, 'title': 'Tool title'} for url in urls or ['https://example.com/a']]}},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'results': records, **context}, ensure_ascii=False)}]},
    ]}


def adapter(raw):
    primary = OpenAIService.__new__(OpenAIService)
    primary._request = Mock(return_value=(raw, {'actual_model': 'offline_replay'}))
    account = SimpleNamespace(singleflight=lambda key: nullcontext(), cached=lambda key: None, cache=Mock(), activity=Mock())
    client = SimpleNamespace(provider='openai_router', account=account, is_configured=lambda: True,
                             _post=lambda op, body, estimate: primary.search(body['query']))
    return DeepSeekSearchService(client), primary


def sample_records():
    return [{'title': '公司甲', 'url': 'https://example.com/a', 'summary': '公司甲營收增加，仍須查證', 'company': 'A'},
            {'title': '公司乙', 'url': 'https://example.com/a', 'summary': '公司乙毛利下降', 'company': 'B'},
            {'title': '缺口', 'url': None, 'summary': '未找到公司丙公告，不代表沒有公告'}]


def test_same_url_keeps_every_event_without_inflating_sources():
    records = sample_records()
    result = parse_search_response(response(records, limitations=['尚缺官方正文']))
    assert len(result['results']) == 1
    assert [item['record'] for item in result['results'][0]['summary_events']] == records[:2]
    assert all(item['summary'] in result['results'][0]['summary'] for item in records[:2])
    assert result['results'][0]['date'] is None
    assert result['results'][0]['content'] == ''
    assert result['transmission_audit']['matched_event_count'] == 2
    assert result['search_context_notes'][0]['record'] == records[2]
    assert result['search_context_notes'][1]['value'] == ['尚缺官方正文']


def test_uncited_generated_url_is_quarantined_not_discarded_or_trusted():
    invented = {'title': '未查證', 'url': 'https://invented.example/b', 'summary': '待查內容'}
    result = parse_search_response(response([invented]))
    assert [row['url'] for row in result['results']] == ['https://example.com/a']
    assert result['search_context_notes'] == [{'result_index': 0, 'record': invented, 'kind': 'uncited_url', 'verified': False}]


@pytest.mark.parametrize('url', [
    'https://money.udn.com/money/story/5612/9661773?from=edn_search_result&utm_source=openai',
    'https://money.udn.com/money/story/5612/9661773?from=edn_previous_story',
])
def test_known_article_tracking_alias_matches_and_retains_original(url):
    base = 'https://money.udn.com/money/story/5612/9661773'
    result = parse_search_response(response([{'url': base, 'summary': '完整事件'}], urls=[url]))
    assert result['results'][0]['url'] == base
    assert result['results'][0]['search_evidence_url'] == url
    assert result['results'][0]['summary'] == '完整事件'


@pytest.mark.parametrize('url', [
    'https://example.com/a?from=other', 'https://money.udn.com/search?from=other',
    'https://money.udn.com/money/story/5612/9661773?id=1&date=2026-11-04&code=2330',
])
def test_substantive_or_unknown_query_parameters_are_not_removed(url):
    assert evidence_url(url) == url


def test_broken_model_json_preserves_text_without_faking_sources():
    raw = response([])
    raw['output'][1]['content'][0]['text'] = '模型未依 JSON 輸出，但此處有缺口'
    result = parse_search_response(raw)
    assert result['search_context_notes'][0]['text'] == raw['output'][1]['content'][0]['text']
    assert len(result['results']) == 1


def test_cross_query_dedup_preserves_both_records_and_cache_version():
    service, primary = adapter(response(sample_records()))
    with patch('research_center.web_fetch_enrichment.recover_search_publication_dates'):
        result = service.discover(CommandRequest('research', '/research', report_date=date(2026, 11, 4)),
                                  [{'label': 'test', 'queries': ['one', 'two']}])
    assert len(result.sources) == 1
    assert len(result.sources[0].search_summary_events) == 2
    assert result.sources[0].published_date is None
    assert len(result.diagnostics['search_context_notes']) == 2
    assert primary._request.call_count == 2
    assert result.diagnostics['source_count'] == 1


def test_existing_lossy_processed_cache_is_not_reused():
    import hashlib
    service, primary = adapter(response(sample_records()))
    old = 'search:' + hashlib.sha256(json.dumps([{'query': 'one', 'count': 10, 'need_content': True}, '', 'openai-search-url-v2'], sort_keys=True).encode()).hexdigest()
    service.client.account.cached = lambda key: {'results': [{'summary': 'old_lossy'}]} if key == old else None
    assert service.search('one')['results'][0]['summary_events']
    assert primary._request.call_count == 1


def test_same_url_across_provider_priority_preserves_events_and_text():
    from research_center.orchestrator import _merge_sources, PROVIDER_PRIORITY
    low = min(PROVIDER_PRIORITY, key=PROVIDER_PRIORITY.get)
    high = max(PROVIDER_PRIORITY, key=PROVIDER_PRIORITY.get)
    event = {'result_index': 0, 'record': sample_records()[0]}
    source = SourceItem('S001', 'Title', 'https://example.com/a', 'Level 3', snippet='事件摘要', provider=low, search_summary_events=[event])
    body = replace(source, snippet='原文正文', provider=high, search_summary_events=[])
    for first, second in ((source, body), (body, source)):
        result = _merge_sources([first], [second])
        assert len(result) == 1
        assert result[0].search_summary_events == [event]
        assert '事件摘要' in result[0].snippet and '原文正文' in result[0].snippet


def test_higher_priority_source_keeps_its_successful_fetch_metadata():
    from research_center.orchestrator import _merge_sources, PROVIDER_PRIORITY
    low = min(PROVIDER_PRIORITY, key=PROVIDER_PRIORITY.get)
    high = max(PROVIDER_PRIORITY, key=PROVIDER_PRIORITY.get)
    source = SourceItem('S001', 'Old', 'https://example.com/a', 'Level 3', provider=low,
                        snippet='舊事件', fetch_status='failed', failure_reason='old_failure', fetch_provider='old')
    better = replace(source, provider=high, source_level='Level 1', snippet='官方正文', fetch_status='success',
                     failure_reason=None, fetch_provider='official', document_kind='parsed_record')
    merged = _merge_sources([source], [better])[0]
    assert merged.fetch_status == 'success' and merged.failure_reason is None
    assert merged.fetch_provider == 'official' and merged.document_kind == 'parsed_record'
    assert '舊事件' in merged.snippet and '官方正文' in merged.snippet


@pytest.mark.parametrize('command', ['research', 'value_scan', 'macro', 'theme', 'theme_flow', 'theme_radar', 'sector_strength'])
def test_all_report_prompts_include_events_and_non_source_gaps(command):
    from research_center.ai_workflow_service import build_high_model_input_package, _build_low_model_digest_payload
    from research_center.prompt_registry import build_prompt_from_request
    service, _ = adapter(response(sample_records(), limitations=['原文待補']))
    request = CommandRequest(command, '/' + command, target='測試', report_date=date(2026, 11, 4))
    with patch('research_center.web_fetch_enrichment.recover_search_publication_dates'):
        discovery = service.discover(request, [{'label': 'test', 'queries': ['one']}])
    data = {'deepseek_search_discovery': discovery.diagnostics}
    low = _build_low_model_digest_payload(request, data, discovery.sources)
    assert low['search_source_events'][0]['events'][1]['record'] == sample_records()[1]
    for packaged in (False, True):
        if packaged:
            data['high_model_input_package'] = build_high_model_input_package(request, data, discovery.sources, prompt_chars_estimate=600000)
        prompt = build_prompt_from_request(request, data, discovery.sources)
        assert all(record['summary'] in prompt for record in sample_records())
        assert '原文待補' in prompt
        assert len(discovery.sources) == 1
    # Date/source qualification can exclude a URL, but must not erase its notes.
    data['high_model_input_package'] = build_high_model_input_package(request, data, [], prompt_chars_estimate=600000)
    quarantined = build_prompt_from_request(request, data, [])
    assert all(record['summary'] in quarantined for record in sample_records())
    assert data['high_model_input_package']['selected_sources'] == []
    assert '未列入本次合格來源' in quarantined
    from research_center.segmented_analysis_service import _high_model_packet_plans, _build_segment_prompt
    plans = _high_model_packet_plans(data)
    assert plans
    segment_text = '\n'.join(_build_segment_prompt(request, data, plan, []) for plan in plans)
    assert all(record['summary'] in segment_text for record in sample_records())


def test_news_retry_carries_complete_events_and_gaps_past_text_limits():
    from research_center.news_service import _batch_classify_news, _sources_to_news_items
    service, _ = adapter(response(sample_records()))
    with patch('research_center.web_fetch_enrichment.recover_search_publication_dates'):
        discovery = service.discover(CommandRequest('news', '/news refresh'), [{'queries': ['one']}])
    items = _sources_to_news_items(discovery.sources)
    center = SimpleNamespace(config=SimpleNamespace(deepseek_only=False), low_model_minimax=None)
    prompts = []
    def call(center, model, prompt, timeout):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise RuntimeError('offline_retry')
        return SimpleNamespace(raw='{}')
    with patch('research_center.news_service._call_news_classifier', side_effect=call), \
         patch('research_center.news_service._classify_text_limit', return_value=1), \
         patch('research_center.news_service._classify_retry_text_limit', return_value=1):
        _batch_classify_news(items, center, lambda msg: None,
                             search_context_notes=collect_search_context({'deepseek_search_discovery': discovery.diagnostics}),
                             search_source_events=source_events_packet(discovery.sources))
    assert len(prompts) == 2
    assert all(all(record['summary'] in prompt for record in sample_records()) for prompt in prompts)


def test_radar_payload_keeps_events_outside_compact_limits():
    from stock_ai_bot.monitoring.radar_service import RadarCandidate, _build_ai_comment_payload, _merge_source_dicts
    from dataclasses import asdict
    event = {'result_index': 0, 'record': sample_records()[0]}
    rows = [asdict(SourceItem('S001', 'Title', 'https://example.com/a', 'Level 3', search_summary_events=[event]))]
    _merge_source_dicts(rows, [{**rows[0], 'search_summary_events': [{'result_index': 1, 'record': sample_records()[1]}]}])
    assert len(rows) == 1 and len(rows[0]['search_summary_events']) == 2
    candidate = RadarCandidate(code='2330', name='台積電', web_sources=rows)
    candidate.evidence_pack['search_context_notes'] = [{'kind': 'no_url', 'record': sample_records()[2]}]
    payload = _build_ai_comment_payload(candidate, date(2026, 11, 4), compact_profile='minimal')
    assert payload['search_source_events'][0]['events'] == rows[0]['search_summary_events']
    assert payload['search_context_notes'][0]['record'] == sample_records()[2]


def test_context_prompt_does_not_turn_notes_into_evidence():
    notes = [{'kind': 'no_url', 'record': sample_records()[2], 'verified': False}]
    collected = collect_search_context({'search_context_notes': notes, 'deepseek_search_discovery': {'search_context_notes': notes}})
    assert collected == notes
    assert '不是已驗證來源' in search_context_prompt(collected)
    assert make_source_items([{'url': '', 'snippet': '缺口'}]) == []


def test_partial_search_stop_preserves_notes_and_events():
    from research_center.deepseek_service import DeepSeekUnavailable
    service, _ = adapter(response(sample_records()))
    native = service.search('one')
    service.search = Mock(side_effect=[native, DeepSeekUnavailable('offline stop', status='quota')])
    with pytest.raises(DeepSeekUnavailable) as caught:
        service.discover(CommandRequest('research', '/research'), [{'queries': ['one', 'two']}])
    exc = caught.value
    assert exc.partial_discovery_sources[0].search_summary_events[1]['record'] == sample_records()[1]
    assert exc.partial_discovery_diagnostics['search_context_notes'][0]['record'] == sample_records()[2]
    assert exc.partial_discovery_diagnostics['complete'] is False


def test_known_future_events_remain_archived_not_reintroduced_as_pending_notes():
    original = {'result_index': 0, 'record': {'summary': '不可提前看到的未來實績'}}
    inventory = [{'url': 'https://example.com/future', 'events': [original], 'published_date': '2026-11-05'}]
    data = {'deepseek_search_discovery': {'search_event_inventory': inventory}}
    notes = collect_search_context(data, [], date(2026, 11, 4))
    assert notes[0]['kind'] == 'after_cutoff_source_excluded'
    assert original['record']['summary'] not in search_context_prompt(notes)
    assert data['deepseek_search_discovery']['search_event_inventory'][0]['events'] == [original]


def test_topic_actual_stage_callback_carries_notes_and_all_events():
    from contextlib import ExitStack
    from unittest.mock import MagicMock
    from research_center.topic_maintain_service import run_topic_maintain
    from research_center.topic_models import TopicChangePack, TopicChangeMode, TopicChangeStatus
    service, _ = adapter(response(sample_records()))
    request = CommandRequest('topic_maintain', '/topic_maintain', ai_model='minimax', report_date=date(2026, 11, 4))
    with patch('research_center.web_fetch_enrichment.recover_search_publication_dates'):
        discovery = service.discover(request, [{'queries': ['one']}])
    center = SimpleNamespace(config=SimpleNamespace(deepseek_only=False, enable_low_model_digest=False),
        minimax=SimpleNamespace(generate_json=Mock(return_value=SimpleNamespace(raw='{"candidates":[]}'))),
        low_model_minimax=None)
    def flow(request, *, sources, structured_data, **kwargs):
        structured_data['deepseek_search_discovery'] = discovery.diagnostics
        return discovery.sources, False
    center._gemini_discovery_runner = SimpleNamespace(run_discovery_flow=flow)
    def pipeline(**kwargs):
        kwargs['call_ai_json']('stage one', 'candidate_extract')
        kwargs['call_ai_json']('stage two', 'detail_expand_1')
        return TopicChangePack('offline', None, TopicChangeMode.UPDATE, TopicChangeStatus.PENDING,
                               'offline', '', '', '', 'low'), []
    with ExitStack() as stack:
        for target, options in [
            ('research_center.topic_maintain_service.is_formal_library_empty', {'return_value': False}),
            ('research_center.topic_maintain_service._load_prompt', {'return_value': 'template'}),
            ('research_center.data_services.collect_structured_data', {'return_value': ({}, [])}),
            ('research_center.web_fetch_enrichment._enrich_sources_with_web_fetch', {'return_value': None}),
            ('research_center.prompt_logging.write_prompt_log', {'return_value': 'offline.json'}),
            ('research_center.topic_maintain_service.raw_response_path', {'return_value': MagicMock()}),
            ('research_center.topic_maintain_service.save_change_pack', {'return_value': None}),
            ('research_center.topic_maintain_service.run_topic_pipeline', {'side_effect': pipeline}),
        ]:
            stack.enter_context(patch(target, **options))
        run_topic_maintain(request, center=center)
    assert center.minimax.generate_json.call_count == 2
    for call in center.minimax.generate_json.call_args_list:
        assert all(record['summary'] in call.args[0] for record in sample_records())
