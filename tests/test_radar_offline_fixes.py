from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from research_center.deepseek_service import DeepSeekUnavailable
from research_center.evidence_governance import document_issuer_codes, source_company_conflicts, source_matches_company
from research_center.models import CommandRequest, SourceItem
from research_center.mops_sources import financial_prompt_context
from research_center.search_routing import candidate_search_plan, gap_query
from research_center.search_transmission import collect_search_context, retain_search_provenance
from stock_ai_bot.monitoring import radar_service as radar


AS_OF = date(2026, 11, 4)
PDF = 'https://mopsov.twse.com.tw/nas/STR/260720260424M001.pdf'


def financial_rows():
    return [{'Quarter': f'{2023+i//4}Q{i%4+1}', 'EPS': i+1, 'Revenue': 100+i,
             'revenue': 100+i, 'unique_metric': {'nested': {'value': i}}} for i in range(12)]


def test_rebuild_preserves_notes_attempts_and_same_url_events():
    note = {'kind': 'no_url', 'verified': False, 'record': {'summary': 'unresolved cancellation evidence'}}
    attempt = {'analysis_date': AS_OF.isoformat(), 'critical_gap_checks': [{'attempted': True, 'remaining': ['official_document']}]}
    source = {'url': 'https://example.test/event', 'search_summary_events': [{'summary': 'first'}, {'summary': 'second'}]}
    item = radar.RadarCandidate(code='1234', name='test', web_sources=[source],
                               evidence_pack={'search_context_notes': [note], 'search_attempts': [attempt]})
    original = deepcopy(item.evidence_pack)
    for _ in range(2):
        item.evidence_pack = radar._build_radar_evidence_pack(item, AS_OF, {'financial_data': financial_rows()})
        radar._refresh_radar_three_layer_context(item, AS_OF)
    assert item.evidence_pack['search_context_notes'] == original['search_context_notes']
    assert item.evidence_pack['search_attempts'] == original['search_attempts']
    payload = radar._build_ai_comment_payload(item, AS_OF)
    assert payload['search_context_notes'] == [note]
    assert len(payload['search_source_events'][0]['events']) == 2


def test_common_provenance_merge_deduplicates_only_identical_records():
    old = {'search_context_notes': [{'text': 'a'}, {'text': 'b'}]}
    new = {'search_context_notes': [{'text': 'b'}, {'text': 'c'}]}
    assert retain_search_provenance(old, new)['search_context_notes'] == [{'text': 'a'}, {'text': 'b'}, {'text': 'c'}]
    assert old['search_context_notes'] == [{'text': 'a'}, {'text': 'b'}]


@pytest.mark.parametrize('profile', ['normal', 'tight', 'minimal'])
def test_financial_core_survives_every_profile_without_unique_field_loss(profile):
    rows = financial_rows()
    for row in rows:
        row.update({f'unique_{i}': i for i in range(65)})
    before = deepcopy(rows)
    item = radar.RadarCandidate(code='1234', evidence_pack={'research_structured_data': {'financial_data': rows}})
    payload = radar._build_ai_comment_payload(item, AS_OF, compact_profile=profile)
    summary = payload['ai_compact_pack']['payload']['research_summary']
    assert len(summary['financial_data']) == len(rows)
    assert summary['financial_data'][0]['Quarter'] == '2025Q4'
    assert summary['financial_data'][0]['unique_64'] == 64
    assert summary['financial_data'][0]['unique_metric']['nested']['value'] == 11
    assert summary['financial_input_audit']['field_aliases']['revenue'] == 'Revenue'
    assert rows == before


def test_explicit_release_wins_over_deadline_future_values_remain_quarantined():
    rows = [{'Quarter': '2026Q3', 'EPS': 5, 'published_at': '2026-10-28'},
            {'Quarter': '2026Q3', 'EPS': 99999, 'published_at': '2026-11-06'},
            {'Quarter': '2026Q2', 'EPS': 4}, {'Quarter': '2026Q4', 'EPS': 88888, 'published_at': '2026-11-01'}]
    original = deepcopy(rows)
    context = financial_prompt_context(rows, AS_OF)
    assert [r['EPS'] for r in context['rows']] == [5, 4]
    assert len(context['excluded_after_date']) == 2
    assert rows == original


def test_alias_conflicts_unknown_period_and_staleness_are_not_hidden():
    rows = [{'Quarter': '2024Q1', 'EPS': 1, 'eps': 2}, {'period': 'unknown', 'value': 3}]
    context = financial_prompt_context(rows, AS_OF)
    assert context['rows'][0]['EPS'] == 1 and context['rows'][0]['eps'] == 2
    assert context['alias_conflicts']
    assert context['unverified_rows'] == [rows[1]]
    assert context['stale_or_missing']


@pytest.mark.parametrize('url,expected', [(PDF, {'2607'}),
    ('https://mopsov.twse.com.tw/nas/STR/12345620260424M001.pdf', {'123456'}),
    ('https://mops.twse.com.tw/STR/2607', {'2607'}),
    ('https://mops.twse.com.tw/STR/2607/a.pdf', {'2607'}),
    ('https://example.test/proxy?url=https%3A%2F%2Fmopsov.twse.com.tw%2Fnas%2FSTR%2F260720260424M001.pdf', {'2607'}),
    ('https://example.test/STR/260720260424M001.pdf', set()),
    ('https://mopsov.twse.com.tw/nas/STR/260720261399M001.pdf', set())])
def test_document_issuer_never_consumes_the_date_as_stock_code(url, expected):
    assert document_issuer_codes({'url': url}) == expected


def test_body_issuer_overrides_shared_group_name_as_mapping_evidence():
    row = {'url': PDF, 'title': '長榮 2603', 'snippet': '長榮國際儲運 股票代碼: 2607',
           'document_kind': 'document_body', 'fetch_status': 'success',
           'published_date': '2026-04-24', 'date_status': 'explicit'}
    assert not source_matches_company(row, '2603', '長榮')
    assert any(issue['kind'] == 'issuer_mismatch' for issue in source_company_conflicts(row, {'stock': {'code': '2603'}}))
    candidate = radar.RadarCandidate(code='2603', name='長榮', web_sources=[row])
    assert not radar._candidate_has_official_document(candidate, AS_OF)
    candidate = radar.RadarCandidate(code='2607', name='榮運', web_sources=[{**row, 'title': '榮運 2607'}])
    assert radar._candidate_has_official_document(candidate, AS_OF)


def test_wrong_issuer_is_retained_as_explicit_background_not_primary_evidence():
    item = radar.RadarCandidate(code='2603', name='長榮')
    source = SourceItem('S001', '長榮 2603', PDF, 'Level 1', published_date='2026-04-24',
                        date_status='explicit', document_kind='document_body', snippet='股票代碼: 2607', fetch_status='success')
    radar._retain_radar_discovery(item, {}, [source], AS_OF)
    assert item.web_sources == []
    assert item.evidence_pack['search_context_notes'][0]['source']['url'] == PDF
    assert item.evidence_pack['search_context_notes'][0]['verified'] is False


def test_unqualified_source_inventory_keeps_wrong_company_warning():
    notes = collect_search_context({'stock': {'code': '2603'}, 'deepseek_search_discovery': {
        'search_event_inventory': [{'url': PDF, 'events': [{'summary': 'wrongly attributed event'}]}]}}, [], AS_OF)
    assert any(n.get('kind') == 'company_issuer_conflict' for n in notes)
    assert any(n.get('events') for n in notes)


def test_orphan_model_url_keeps_its_record_and_gains_issuer_warning():
    note = {'kind': 'unreferenced_model_url', 'verified': False,
            'record': {'title': 'misattributed company', 'url': PDF, 'summary': 'original event'}}
    notes = collect_search_context({'stock': {'code': '2603'},
        'deepseek_search_discovery': {'search_context_notes': [note]}}, [], AS_OF)
    assert notes[0]['record'] == note['record'] and notes[0]['kind'] == note['kind']
    assert notes[0]['company_mapping_issues'][0]['code'] == '2607'
    assert 'company_mapping_issues' not in note


def test_search_plan_keeps_five_perspectives_and_only_refills_missing_quantitative_data():
    data = {'financial_data': [{'Quarter': '2026Q2', 'EPS': 1}], 'institutional_data': [{'Date': '2026-11-04', 'foreign': 100}]}
    before = deepcopy(data)
    plan = candidate_search_plan('1234', 'test', 'industry', AS_OF, data)
    assert len(plan['queries']) == 2
    assert len(plan['coverage_topics']) == 5
    assert plan['local_coverage']['institutional_data_recent']
    assert data == before
    empty = candidate_search_plan('1234', 'test', 'industry', AS_OF, {})
    assert len(empty['queries']) == 3
    assert '財報' in empty['queries'][-1] and '法人' in empty['queries'][-1]


def test_period_and_date_without_numeric_values_do_not_satisfy_local_quality():
    plan = candidate_search_plan('1234', 'test', 'industry', AS_OF,
        {'financial_data': [{'Quarter': '2026Q2'}], 'institutional_data': [{'Date': '2026-11-04'}]})
    assert len(plan['queries']) == 3
    assert plan['local_coverage']['financial_stale_or_missing']
    assert not plan['local_coverage']['institutional_data_recent']


@pytest.mark.parametrize('label', ['radar_ai_sources', 'news_quality_gap', 'topic_maintain_initial'])
def test_followup_question_does_not_expose_internal_task_labels(label):
    request = CommandRequest(command='research', raw_text='/radar', target='1234 test')
    query = gap_query(request, {'label': label, 'objective': '官方原文與反證'}, ['official_document'], AS_OF)
    assert label not in query and '_' not in query
    assert '2026-11-04' in query and '官方' in query


def test_missing_target_does_not_fall_back_to_an_internal_label():
    request = CommandRequest(command='news', raw_text='/news refresh')
    query = gap_query(request, {'label': 'news_refresh_internal', 'objective': '近期官方消息'},
                      ['publication_date', 'official_document'], AS_OF)
    assert 'news_refresh_internal' not in query and '_' not in query


def test_company_followup_keeps_all_gaps_and_official_domains_within_query_limit():
    from research_center.search_routing import GAP_LABELS
    request = CommandRequest(command='research', raw_text='/radar', target='1234 測試公司')
    query = gap_query(request, {'label': 'radar_ai_sources', 'objective': '核對本次所有官方原文與資料缺口'},
                      list(GAP_LABELS), AS_OF)
    assert len(query) <= 128 and 'site:twse.com.tw' in query
    assert all(label in query for label in GAP_LABELS.values())


def test_checked_critical_gap_does_not_trigger_a_second_paid_supplement():
    item = radar.RadarCandidate(code='1234', evidence_pack={'search_attempts': [{
        'analysis_date': AS_OF.isoformat(), 'critical_gap_checks': [{'attempted': True, 'remaining': ['official_document']}]}]})
    with patch.object(radar, '_attach_web_sources') as attach:
        radar._ensure_radar_source_sufficiency([item], ['1234'], AS_OF, None)
    attach.assert_not_called()
    assert not radar._candidate_has_official_document(item, AS_OF)
    assert item.evidence_pack['search_context_notes'][0]['verified'] is False


def test_completed_coverage_checks_do_not_trigger_search_only_for_source_count():
    source = {'title': '1234 official', 'url': 'https://mops.twse.com.tw/article', 'snippet': '1234 risk',
              'document_kind': 'document_body', 'published_date': '2026-11-03', 'date_status': 'explicit'}
    item = radar.RadarCandidate(code='1234', web_sources=[source], evidence_pack={'search_attempts': [{
        'analysis_date': AS_OF.isoformat(), 'critical_gap_checks': [{'attempted': False, 'remaining': [],
                                                                  'status': 'coverage_checked_not_fact_verified'}]}]})
    with patch.object(radar, '_attach_web_sources') as attach:
        radar._ensure_radar_source_sufficiency([item], ['1234'], AS_OF, None)
    attach.assert_not_called()
    assert item.web_sources == [source]
    assert item.evidence_pack['search_context_notes'][0]['verified'] is False


def test_unknown_search_stops_remaining_candidates_and_final_analysis_without_losing_partial_data():
    item = radar.RadarCandidate(code='1234')
    later = radar.RadarCandidate(code='5678')
    exc = DeepSeekUnavailable('unknown fee', 'transport_unknown')
    exc.partial_discovery_sources = [SourceItem('S001', '1234', 'https://example.test/a', 'Level 3',
         published_date='2026-11-03', date_status='explicit', snippet='kept content')]
    exc.partial_discovery_diagnostics = {'search_context_notes': [{'kind': 'no_url', 'verified': False, 'text': 'keep this gap'}]}
    runner = SimpleNamespace(_run_minimax_mcp=Mock(side_effect=exc))
    with patch.object(radar, 'ResearchCenter', return_value=SimpleNamespace(_gemini_discovery_runner=runner)):
        radar._attach_research_center_sources([item, later], ['1234', '5678'], AS_OF, None)
    assert runner._run_minimax_mcp.call_count == 1
    assert item.web_sources[0]['snippet'] == 'kept content'
    assert any(n.get('text') == 'keep this gap' for n in item.evidence_pack['search_context_notes'])
    with patch.object(radar, '_call_ai_comment_model') as analyze, patch.object(radar, '_attach_web_sources') as search:
        radar._ensure_radar_source_sufficiency([item, later], ['1234', '5678'], AS_OF, None)
        result = radar._attach_ai_comments([item, later], ['1234', '5678'], 'deepseek', AS_OF, None)
    analyze.assert_not_called()
    search.assert_not_called()
    assert result['status'] == 'stopped' and result['comment_count'] == 0
    assert item.ai_comment['status'] == 'failed'
