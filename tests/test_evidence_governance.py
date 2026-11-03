from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
from unittest.mock import MagicMock
from types import SimpleNamespace

import bs4
import pytest

from research_center.evidence_governance import build_evidence_governance, official_evidence_status, review_claim_evidence
from research_center.models import CommandRequest, SourceItem
from research_center.source_date_normalizer import source_temporal_record, publication_metadata_date
from research_center.source_snapshots import point_in_time_data
from research_center.analysis_followup_service import AnalysisFollowupStore, explicit_prediction_records


def source(**kwargs):
    return replace(SourceItem('S018', '公司營收', 'https://mops.twse.com.tw/announcement', 'Level 1',
                              published_date='2026-05-15', snippet='甲公司營收較去年衰退，尚須觀察訂單恢復。'), **kwargs)


def test_dates_are_separate():
    row = source_temporal_record(source(event_date='2026-04-01', data_date='2026-03-31',
        updated_date='2026-05-16', fetched_at='2026-11-03T12:00:00+08:00'), date(2026, 5, 15))
    assert row['published_date'] == '2026-05-15'
    assert row['event_date'] == '2026-04-01'
    assert row['updated_date'] == '2026-05-16'
    assert row['available_by_report_date'] is True
    assert publication_metadata_date({'dateModified': '2026-06-01'}) is None


def test_unknown_inferred_and_future_are_not_verified():
    audit = build_evidence_governance('research', date(2026, 5, 15),
        [source(date_status='inferred'), source(published_date=None), source(published_date='2026-05-16')])
    assert audit['sources'][0]['available_by_report_date'] is None
    assert audit['sources'][1]['publication_status'] == 'unknown'
    assert audit['sources'][2]['available_by_report_date'] is False
    assert audit['warnings'] and audit['semantic_verified'] is False


@pytest.mark.parametrize('command', ['research', 'value_scan', 'macro', 'theme', 'theme_flow', 'theme_radar', 'sector_strength', 'radar', 'news', 'topic_maintain'])
def test_all_ten_commands_share_policy(command):
    from research_center.reader_quality_rules import reader_quality_rules
    audit = build_evidence_governance(command, date(2026, 5, 15), [source()])
    assert audit['command'] == command
    rules = reader_quality_rules(command)
    assert '官方入口網址僅是查詢線索' in rules
    assert '保留具現實基礎的想像力' in rules


def test_official_reference_not_fact_and_hostname_safe():
    assert official_evidence_status(source(document_kind='reference_link')) == 'reference_only'
    assert official_evidence_status(source(document_kind='parsed_record')) == 'retrieved_requires_claim_check'
    assert official_evidence_status(source(url='https://twse.com.tw.evil.example/file')) == 'non_official'
    from research_center.company_knowledge_update_service import source_quality_score
    assert not source_quality_score(source(document_kind='reference_link'))['usable_for_company_knowledge']


def test_company_mapping_conflict_and_candidate_preserved():
    data = {'stock_index': {'2330': {'name': '台積電'}},
            'claims': [{'code': '2330', 'name': '另一家公司'}, {'code': '2241', 'verification_status': 'candidate'}]}
    audit = build_evidence_governance('theme', None, [], data)
    assert len(audit['company_mapping_issues']) == 2
    assert data['claims'][1]['verification_status'] == 'candidate'


def test_claim_audit_flags_reference_and_opposite_without_claiming_truth():
    audit = review_claim_evidence('營收明顯成長 [S018]\n預期訂單恢復 [S018]', [source(document_kind='reference_link')])
    assert audit['flagged_count'] == 2
    assert audit['claims'][1]['kind'] == 'forecast'
    assert audit['semantic_verified'] is False
    assert any('相反方向' in issue for issue in audit['claims'][0]['issues'])


def test_historical_guard_archives_future_not_forecast(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_TASK_TEST_ROOT', str(tmp_path))
    original = {'price_data': [{'data_date': '2026-06-01', 'close': 99}],
                'forecast': {'kind': 'forecast', 'period_end': '2027-01-01', 'hypothesis': '條件式轉機'},
                'risk': [{'published_date': '2026-05-01', 'date_status': 'explicit', 'url': 'https://example.com/a', 'snippet': '風險'}]}
    guarded = point_in_time_data(original, date(2026, 5, 15))
    assert guarded['price_data'][0]['point_in_time_status'] == 'excluded'
    assert guarded['forecast']['hypothesis'] == '條件式轉機'
    assert guarded['risk'] == original['risk']
    assert original['price_data'][0]['close'] == 99
    archive = json.loads(Path(guarded['point_in_time_audit']['archive_path']).read_text(encoding='utf-8'))
    assert archive[0]['record']['close'] == 99


def test_filter_preserves_ids_and_all_metadata():
    from research_center.date_guard import filter_sources_for_report_date
    original = source(date_status='explicit', period_end='2026-03-31', document_kind='parsed_record')
    result, _ = filter_sources_for_report_date([source(source_id='S001', published_date='2027-01-01'), original], date(2026, 5, 15))
    assert result == [original] and result[0].source_id == 'S018'


def test_cache_rejects_wrong_identity_future_and_invalid_timestamp(tmp_path):
    from research_center import structured_cache as cache
    target = date(2026, 5, 15)
    with patch.object(cache, 'CACHE_DIR', tmp_path):
        path = cache.save_research_structured_cache('2330', target, {'stock': {'code': '2330'}})
        good = json.loads(path.read_text(encoding='utf-8'))
        assert cache.load_research_structured_cache('2330', target)
        for bad in ({**good, 'stock_code': '2241'}, {**good, 'generated_at': 'not-a-date'},
                    {**good, 'data': {'price_data': [{'data_date': '2026-06-01'}]}}):
            path.write_text(json.dumps(bad), encoding='utf-8')
            assert cache.load_research_structured_cache('2330', target) is None
            if bad.get('generated_at') != 'not-a-date':
                assert cache.load_latest_research_structured_cache('2330', before_or_on=target) is None


def test_html_modification_date_not_publication():
    from research_center.web_fetch_service import _extract_html_published_date, _html_updated_date
    html = '<meta itemprop="dateModified" content="2026-05-16"><script type="application/ld+json">{"dateModified":"2026-05-16"}</script>'
    soup = bs4.BeautifulSoup(html, 'html.parser')
    assert _extract_html_published_date(soup, html) is None
    assert _html_updated_date(soup) == '2026-05-16'


def test_pending_preserved_dedup_and_completed_not_requeued(tmp_path):
    store = AnalysisFollowupStore(tmp_path / 'followup.db')
    rows = [{'theme_id': f't{i}', 'missing_data': ['待補來源']} for i in range(10)]
    store.defer('topic_maintain', rows)
    assert len(store.pending('topic_maintain', limit=20)) == 10
    store.defer('topic_maintain', rows)
    assert len(store.pending('topic_maintain', limit=20)) == 10
    store.resolve('topic_maintain', rows[:2], success=True)
    store.resolve('topic_maintain', rows[2:3], success=False)
    store.defer('topic_maintain', rows)
    assert len(store.pending('topic_maintain', limit=20)) == 7
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM backlog').fetchone()[0] == 10
        conn.execute("UPDATE backlog SET next_attempt='2000-01-01' WHERE identity='t2'")
    assert store.pending('topic_maintain', limit=1)[0]['theme_id'] != 't2', '失敗重試不得永遠占住尚未處理候選的位置'


def test_explicit_predictions_immutable_and_observation_requires_evidence(tmp_path):
    store = AnalysisFollowupStore(tmp_path / 'followup.db')
    text = '## 預測追蹤\n| 標的 | 假設 | 觀察截止日 | 驗證條件 | 失效條件 | 來源 |\n|---|---|---|---|---|---|\n| 2241 | 營收轉機 | 2026-12-31 | 訂單轉強 | 訂單下滑 | [S018] |'
    rows = explicit_prediction_records(text)
    result = store.record_predictions('r1', '2026-05-15', rows)
    identity = result['recorded_ids'][0]
    store.record_predictions('r1', '2026-05-15', rows)
    with pytest.raises(ValueError):
        store.observe(identity, {'status': 'supported', 'data_date': '2026-12-31'})
    store.observe(identity, {'status': 'not_assessable', 'data_date': '2026-12-31', 'reason': '沒有可靠觀測'})
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM predictions').fetchone()[0] == 1
        assert json.loads(conn.execute('SELECT payload FROM predictions').fetchone()[0]) == rows[0]
    assert explicit_prediction_records('股價可能成長，沒有明確追蹤期間。') == []


def test_official_cache_coalesces_same_key_and_not_different_keys(tmp_path):
    from stock_ai_bot.data_sources.official_market_cache import OfficialDailyCache
    cache = OfficialDailyCache(tmp_path)
    started, release, other = Event(), Event(), Event()
    calls = []
    day = date(2026, 5, 15)
    payload = {'stat': 'OK', 'date': '20260515', 'data': [[1]]}
    def loader():
        calls.append(1)
        started.set()
        assert release.wait(3)
        return payload
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(cache.get, 'https://example.com/a', {}, day, loader)
        assert started.wait(2)
        second = pool.submit(cache.get, 'https://example.com/a', {}, day, loader)
        def independent():
            other.set()
            return payload
        third = pool.submit(cache.get, 'https://example.com/b', {}, day, independent)
        try:
            assert other.wait(2), '不同來源不應被第一個慢請求鎖住'
        finally:
            release.set()
        assert first.result() == second.result() == third.result() == payload
    assert len(calls) == 1


def test_high_package_and_final_keep_governance():
    from research_center.ai_workflow_service import build_high_model_input_package
    from research_center.segmented_analysis_service import _final_local_summary
    request = CommandRequest('research', '/research 2241', target='2241', report_date=date(2026, 5, 15))
    data = {'stock': {'code': '2241'}, 'local_scoring': {'total': 40}}
    package = build_high_model_input_package(request, data, [source()], prompt_chars_estimate=300000)
    data['high_model_input_package'] = package
    assert package['evidence_governance']['report_date'] == '2026-05-15'
    assert _final_local_summary(data)['evidence_governance'] == package['evidence_governance']


def test_native_news_keeps_date_provenance_and_rejects_future():
    from research_center.news_models import NewsItem
    from research_center.news_service import _classification_payload, _apply_classification_meta, _filter_by_published_window
    item = NewsItem(id='n1', published_at=datetime.now().date().isoformat(), tags=['publication_inferred'])
    assert _classification_payload([item], 100)[0]['date_audit']['publication_status'] == 'inferred'
    result = _apply_classification_meta([item], {'n1': {'tags': ['catalyst']}})
    assert 'publication_inferred' in result[0].tags
    assert not _filter_by_published_window(result, 24, keep_unknown_date=False)
    future = NewsItem(id='n2', published_at=(datetime.now() + timedelta(days=3)).date().isoformat())
    assert not _filter_by_published_window([future], 168)


def test_topic_native_preparation_and_pending_store_without_http(tmp_path, monkeypatch):
    from research_center import topic_maintain_service as service
    monkeypatch.setenv('CODEX_TASK_TEST_ROOT', str(tmp_path))
    request = CommandRequest('topic_maintain', '/topic_maintain --deep', mode='deep', report_date=date(2026, 5, 15))
    client = MagicMock()
    client.account.cached.return_value = None
    client.max_output_tokens = 32768
    client.with_output_limit.return_value = client
    candidate = {'theme_id': 'proof_test', 'theme_name': '隔離測試題材', 'reason': '未驗證的測試資料'}
    client.generate_json.side_effect = [SimpleNamespace(raw=json.dumps({'candidates': [candidate]}, ensure_ascii=False), diagnostics={}),
                                        SimpleNamespace(raw=json.dumps({'actions': [candidate]}, ensure_ascii=False), diagnostics={})]
    center = SimpleNamespace(config=SimpleNamespace(deepseek_only=True, deepseek_model='deepseek-flash', enable_low_model_digest=False),
                             deepseek=client, deepseek_organizer=client)
    log = tmp_path / 'prompts' / 'p.json'
    log.parent.mkdir()
    from contextlib import ExitStack
    with ExitStack() as stack:
        for name, value in [('is_formal_library_empty', False), ('load_company_knowledge_data', {}),
                            ('load_topic_profiles', []), ('load_change_pack', None), ('save_change_pack', None),
                            ('raw_response_path', tmp_path / 'raw.json')]:
            stack.enter_context(patch.object(service, name, return_value=value))
        stack.enter_context(patch('research_center.data_services.collect_structured_data', return_value=({}, [])))
        stack.enter_context(patch('research_center.prompt_logging.write_prompt_log', return_value=log))
        pack = service.run_topic_maintain(request, center=center, skip_webfetch_evidence=True)
    assert pack.extra['report_metadata']['model'] == 'deepseek-flash'
    assert client.generate_json.call_count == 2
    assert '日期、公司與證據核對' in client.generate_json.call_args.args[0]
    with sqlite3.connect(tmp_path / 'analysis_followup.sqlite3') as conn:
        assert conn.execute('SELECT COUNT(*) FROM backlog').fetchone()[0] == 1
    assert pack.extra['evidence_review_complete'] is False


@pytest.mark.parametrize('command', ['research', 'value_scan', 'macro', 'theme', 'theme_flow', 'theme_radar', 'sector_strength'])
def test_all_report_packages_carry_governance(command):
    from research_center.ai_workflow_service import build_high_model_input_package
    request = CommandRequest(command, '/' + command, report_date=date(2026, 5, 15))
    package = build_high_model_input_package(request, {}, [source()], prompt_chars_estimate=300000)
    assert package['evidence_governance']['sources'][0]['source_id'] == 'S018'
    assert package['command_specific_data']['evidence_governance_ref'] == 'high_model_input_package.evidence_governance'
    assert package['evidence_governance']['semantic_verified'] is False


def test_native_radar_short_prompt_keeps_scoring_and_evidence_rules():
    from stock_ai_bot.monitoring import radar_service as radar
    item = radar.RadarCandidate(code='2241', name='艾姆勒', total_score=45)
    prompt = radar._build_ai_comment_prompt([item], date(2026, 5, 15))
    assert '官方入口網址僅是查詢線索' in prompt and '不改本地分數' in prompt
    assert item.total_score == 45


def test_report_html_and_json_show_governance_without_fake_ai_success(tmp_path):
    from research_center.report_builder import write_report_artifacts
    request = CommandRequest('theme', '/theme 測試題材', target='測試題材')
    data = {'ai_status': 'fallback'}
    with patch('research_center.report_builder._register_report_artifacts'):
        artifacts, qa = write_report_artifacts(request=request, markdown='# 測試題材\n資料不足，僅保留來源 [S018]。',
            summary='資料不足', sources=[source(document_kind='reference_link')], ai_used=False,
            fallback_reason='offline_fixture', structured_data=data, report_root=tmp_path)
    payload = json.loads(artifacts.json_path.read_text(encoding='utf-8'))
    assert payload['metadata']['ai_used'] is False
    assert payload['structured_data']['evidence_governance']['semantic_verified'] is False
    assert '只有查詢入口' in artifacts.html_path.read_text(encoding='utf-8')


def test_later_updated_body_not_valid_historical_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_TASK_TEST_ROOT', str(tmp_path))
    data = {'sources': [{'url': 'https://example.com/a', 'published_date': '2026-01-01',
                         'updated_date': '2026-06-01', 'snippet': '六月新增的資訊'}]}
    guarded = point_in_time_data(data, date(2026, 5, 15))
    assert guarded['sources'][0]['point_in_time_status'] == 'excluded'


def test_auto_company_candidate_status_survives_normalization():
    from research_center.company_knowledge_update_service import _merge_company_entry
    from research_center.knowledge_base import _normalize_company_knowledge
    row = _merge_company_entry({}, {'verification_status': 'candidate', 'confidence': 'candidate', 'product_lines': ['測試產品']})
    normalized = _normalize_company_knowledge(row)
    assert normalized['verification_status'] == 'candidate'
    assert normalized['usage_policy'] == 'hypothesis_only'


def test_topic_source_id_references_keep_body_and_date_versions():
    from research_center.topic_input_package import merge_topic_sources, referenced_topic_sources, topic_stage_variables
    data = {'report_date': '2026-05-15', 'discovery_sources': [
        {'source_id': 'S001', 'url': 'https://example.com/a', 'published_date': '2026-05-01', 'snippet': '五月產品與反證'},
        {'source_id': 'S002', 'url': 'https://example.com/a', 'published_date': '2026-04-01', 'snippet': '四月尚無訂單'}],
        'web_fetched_sources': [{'source_id': 'S003', 'url': 'https://example.com/a', 'published_date': '2026-05-01', 'content': '五月產品與反證，完整正文。'}]}
    sources = merge_topic_sources(data)
    assert len(sources) == 2
    assert 'S003' in sources[0]['source_id_aliases']
    assert len(referenced_topic_sources({'source_refs': ['S002']}, sources)) == 1
    assert referenced_topic_sources({'source_refs': ['S003']}, sources)[0]['content'].endswith('完整正文。')
    variables = topic_stage_variables(data, {}, [{'theme_id': 'unrelated', 'theme_name': '其他名稱', 'source_refs': ['S002']}])
    assert '四月尚無訂單' in variables['web_fetched_sources_json']
    assert '五月產品與反證' not in variables['web_fetched_sources_json']


def test_missing_date_cannot_be_explicit_by_flag():
    assert source_temporal_record(source(published_date=None, date_status='explicit'), date(2026, 5, 15))['publication_status'] == 'unknown'
    from research_center.source_date_normalizer import normalize_published_date
    assert normalize_published_date({'news_origin': 'refresh', 'created_at': '2026-05-15'}) is None


def test_topic_empty_duplicate_url_cannot_hide_supplied_source_provenance():
    from research_center.topic_pipeline_service import _hydrate_topic_evidence
    from research_center.topic_models import TopicEvidence, TopicSourceLevel
    from types import SimpleNamespace
    evidence=TopicEvidence('媒體',TopicSourceLevel.L3_COMMUNITY,'待核事實',url='https://money.udn.com/a',source_id='S043')
    pack=SimpleNamespace(actions=[SimpleNamespace(evidence=[evidence])])
    data={'discovery_sources':[{'source_id':'S043','url':evidence.url,'source_level':'L2_media','published_date':'2026-11-03','date_status':'explicit'}],
          'web_fetched_sources':[{'url':evidence.url,'content':'正文','source_level':None}]}
    _hydrate_topic_evidence(pack,data)
    assert evidence.source_level == TopicSourceLevel.L2_MEDIA
    assert evidence.publish_date == '2026-11-03'
