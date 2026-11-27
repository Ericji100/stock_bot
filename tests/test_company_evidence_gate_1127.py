"""Offline admission checks: retain raw facts, isolate identity errors before prompts."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from unittest.mock import patch

import pytest

from research_center.evidence_governance import (
    company_identity_context, company_source_admission, company_safe_prompt_data,
    official_evidence_status, official_source_record, source_company_conflicts,
)
from research_center.models import CommandRequest, SourceItem
from research_center.news_models import NewsItem
from stock_ai_bot.monitoring import radar_service as radar

DAY = date(2026, 11, 26)
NAMES = {'3703': '欣陸', '3704': '合勤控'}


def context():
    return company_identity_context({'stock': {'code': '3703', 'name': '欣陸'},
                                     'authoritative_company_names': NAMES})


def wrong(**extra):
    return {'source_id': 'S900', 'title': '合勤控新品展望', 'url': 'https://example.test/article',
            'summary': '合勤控（3703）新增無人機訂單，尚待確認。', **extra}


@pytest.mark.parametrize('field', ['content', 'snippet', 'summary', 'full_text', 'text'])
@pytest.mark.parametrize('text', ['合勤控（3703）', '3703 合勤控', '3703合勤控', '3703 合勤控股份有限公司宣告新增訂單。'])
def test_all_text_channels_and_code_orders_reject_same_conflict(field, text):
    row = {'title': '公司消息', field: text, 'url': 'https://example.test/a'}
    gate = company_source_admission(row, context())
    assert gate['status'] == 'quarantined'
    assert not gate['supports_target_company']
    assert gate['issues'][0]['expected_name'] == '欣陸'


def test_correct_multi_company_article_stays_complete():
    row = {'title': '兩家公司合作', 'content': '欣陸（3703）與合勤控（3704）討論合作，尚未確認訂單。',
           'url': 'https://example.test/a'}
    assert company_source_admission(row, context())['status'] == 'eligible'
    assert company_safe_prompt_data(row, context()) == row
    peer={'title':'合勤控（3704）營收成長','content':'營收待核。','url':'https://example.test/peer'}
    safe=company_safe_prompt_data(peer,context())
    assert safe['content']==peer['content'] and '不能支持目標公司' in safe['證據用途']


def test_conflict_in_summary_cannot_hide_behind_correct_body():
    row = wrong(content='欣陸（3703）建材營造事業。')
    assert source_company_conflicts(row, context())


def test_official_alias_is_preserved_without_promoting_business_relations():
    master = {'公司代號':'3703', '公司簡稱':'欣陸', '公司名稱':'欣陸投資控股股份有限公司'}
    data = {**context(), 'base_sources':[{'url':'https://openapi.twse.com.tw/v1/master',
        'content':json.dumps([master]), 'found_by':['source_text:provided_content']}]}
    data = company_identity_context(data)
    row = {'title':'欣陸投資控股股份有限公司（3703）', 'url':'https://example.test/a'}
    assert not source_company_conflicts(row, data)
    assert not company_source_admission(row, data)['semantic_verified']


def test_prompt_only_isolation_keeps_financial_core_and_raw_unchanged():
    data = {'stock': {'code':'3703','name':'欣陸'}, 'financial_data':[{'Revenue':123456}],
            'news_context': {'local_news':[wrong()]}, 'nested_events':[{'record': {'summary':wrong()['summary']}}]}
    before = deepcopy(data)
    safe = company_safe_prompt_data(data, context())
    assert data == before and safe['financial_data'] == before['financial_data']
    assert '無人機訂單' not in json.dumps(safe, ensure_ascii=False)
    assert '原始紀錄SHA256' in safe['news_context']['local_news'][0]
    assert safe['nested_events'][0]['record']['公司身分隔離']
    assert company_safe_prompt_data(safe, context()) == safe


def test_radar_local_news_uses_shared_gate_and_keeps_full_quarantine():
    bad = NewsItem(title=wrong()['title'], url=wrong()['url'], summary=wrong()['summary'], published_at='2026-11-03')
    good = NewsItem(title='欣陸營收', url='https://example.test/good', summary='欣陸（3703）營收待觀察。', published_at='2026-11-03')
    sector = NewsItem(title='建材營造產業', url='https://example.test/sector', summary='族群輪動。', published_at='2026-11-03')
    candidate = radar.RadarCandidate(code='3703', name='欣陸', industry='建材營造')
    with patch.object(radar, 'NewsRepository') as repo:
        repo.return_value.query_saved_recent.return_value = [bad, good, sector]
        radar._attach_local_news([candidate], DAY)
    assert [n['url'] for n in candidate.news_items] == [good.url]
    archived = candidate.evidence_pack['company_evidence_quarantine']
    assert {r['source']['url'] for r in archived} == {bad.url, sector.url}
    assert archived[0]['source']['summary'] == bad.summary
    candidate.evidence_pack = radar._build_radar_evidence_pack(candidate, DAY, {})
    assert candidate.evidence_pack['company_evidence_quarantine'] == archived


def test_full_text_and_prior_conflict_flags_cannot_bypass_news_gate():
    bad = NewsItem(title='欣陸消息', summary='營收待核。', full_text=wrong()['summary'],
                   url='https://example.test/body', published_at='2026-11-03')
    flagged = NewsItem(title='欣陸營收', summary='欣陸營收增加。', tags=['company_identity_conflict'],
                      url='https://example.test/flagged', published_at='2026-11-03')
    item=radar.RadarCandidate(code='3703', name='欣陸')
    with patch.object(radar,'NewsRepository') as repo:
        repo.return_value.query_saved_recent.return_value=[bad,flagged]
        radar._attach_local_news([item],DAY)
    assert not item.news_items and len(item.evidence_pack['company_evidence_quarantine'])==2
    assert item.evidence_pack['company_evidence_quarantine'][0]['source']['full_text']==bad.full_text


def test_chinese_predicates_and_longer_company_names_are_not_confused():
    assert company_source_admission({'title':'欣陸營收成長'},context())['status']=='eligible'
    data=company_identity_context({'stock':{'code':'2603','name':'長榮'}})
    assert company_source_admission({'title':'長榮航營收成長'},data)['status']=='context_only'


def test_official_full_alias_wins_over_other_company_short_prefix():
    master={'公司代號':'2027','公司簡稱':'大成鋼','公司名稱':'大成不銹鋼工業股份有限公司'}
    data=company_identity_context({'stock':{'code':'2027','name':'大成鋼'},
        'authoritative_company_names':{'2027':'大成鋼','1210':'大成'},
        'base_sources':[{'url':'https://openapi.twse.com.tw/v1/master',
          'content':json.dumps([master]),'found_by':['source_text:provided_content']}]})
    for text in ['2027 大成不銹鋼工業股份有限公司','大成不銹鋼工業股份有限公司（2027）']:
        assert not source_company_conflicts({'content':text},data)


def test_url_free_derived_source_segments_are_isolated_by_identity():
    data={**context(),'source_identity_issues':{'S900':[{'kind':'name_mismatch'}]}}
    raw={'fact':{'source_id':'S900','段落':[{'文字':'不應進入的個股利多'}]},
         'core':[{'Revenue':12345}], 'snippet':{'段落':[{'文字':wrong()['summary']}]}}
    safe=company_safe_prompt_data(raw,data)
    assert '不應進入的個股利多' not in json.dumps(safe,ensure_ascii=False)
    assert '無人機訂單' not in json.dumps(safe,ensure_ascii=False)
    assert safe['core']==raw['core']
    assert '無人機訂單' not in json.dumps(company_safe_prompt_data({'events':[wrong()['summary']]},context()),ensure_ascii=False)
    assert '錯誤個股訂單' not in json.dumps(company_safe_prompt_data({'events':['3703合勤控錯誤個股訂單']},context()),ensure_ascii=False)


def test_topic_auxiliary_evidence_cannot_bypass_source_gate():
    from research_center.topic_input_package import topic_stage_variables, expand_topic_stage_variables
    raw={'report_date':'2026-11-26','authoritative_company_names':NAMES,
         'discovery_sources':[wrong()], 'webfetch_evidence':{'items':[wrong()]},
         'external_topic_source_caches':{'recent':[wrong()]}}
    before=deepcopy(raw)
    unpacked=expand_topic_stage_variables(topic_stage_variables(raw,{}))
    for key in ['discovery_sources_json','webfetch_evidence_json','external_topic_source_caches_json']:
        assert '無人機訂單' not in json.dumps(unpacked[key],ensure_ascii=False)
    assert raw==before


def test_generic_non_company_candidate_label_is_safe():
    assert company_safe_prompt_data({'candidate':'題材候選','score':123})['score']==123
    assert not source_company_conflicts({'content':'2027 台積電資本支出仍待觀察。'},company_identity_context())


def test_unverified_legal_name_with_shared_prefix_stays_pending_not_verified():
    data=company_identity_context({'stock':{'code':'2027','name':'大成鋼'},'authoritative_company_names':{'2027':'大成鋼','1210':'大成'}})
    gate=company_source_admission({'content':'2027 大成不銹鋼工業股份有限公司','url':'https://example.test/a'},data)
    assert gate['status']=='identity_pending' and not gate['supports_target_company']


def test_legacy_cached_radar_news_cannot_bypass_prompt_gate():
    item = radar.RadarCandidate(code='3703', name='欣陸', news_items=[wrong()],
        evidence_pack={'research_structured_data': {'stock':{'code':'3703','name':'欣陸'}, 'financial_data':[]}})
    before = deepcopy(item)
    prompt = radar._build_ai_comment_prompt([item], DAY)
    assert '新增無人機訂單' not in prompt
    assert item == before
    assert radar._build_ai_comment_payload(item, DAY)['local_news'] == []


def test_news_ingress_and_fallback_do_not_restore_bad_company_evidence():
    from research_center.news_service import _classification_payload, _apply_classification_meta, _fallback_classification
    item = NewsItem(id='bad', title=wrong()['title'], url=wrong()['url'], summary=wrong()['summary'], full_text=wrong()['summary'])
    payload = _classification_payload([item], 500)
    assert '無人機訂單' not in json.dumps(payload, ensure_ascii=False)
    rows = _apply_classification_meta([item], {'bad': {'related_symbols':['3703'], 'summary':'訂單已確認'}})
    assert rows[0].summary == wrong()['summary']
    assert 'company_identity_conflict' in rows[0].tags and not rows[0].related_symbols
    assert 'ai_classified' not in rows[0].tags
    assert 'ai_excluded' in _fallback_classification([item])[0].tags


def test_known_identity_conflicts_do_not_pay_for_classification_or_remain_in_backlog():
    from types import SimpleNamespace
    from research_center import news_service as news
    item=NewsItem(id='bad',title=wrong()['title'],url=wrong()['url'],summary=wrong()['summary'],tags=['local_rule_fallback'])
    center=SimpleNamespace(config=SimpleNamespace(deepseek_only=False,deepseek_model='fake'))
    with patch.object(news,'_call_news_classifier') as call:
        result=news._classify_news_batches([item],center,lambda x:None,ai_model='deepseek',prompt_text='{news_batch_json}',label='test')
    assert result==[item] and 'company_identity_conflict' in item.tags
    assert 'local_rule_fallback' not in item.tags and 'ai_classified' not in item.tags
    call.assert_not_called()


def test_media_body_never_gets_official_news_tag():
    from research_center.news_service import _sources_to_news_items
    media = SourceItem('S1','媒體','https://example.test/a','Level 2', snippet='原文',
                       document_kind='document_body', fetch_status='success')
    assert 'official_body_retrieved' not in _sources_to_news_items([media])[0].tags
    official = replace(media, url='https://mops.twse.com.tw/announcement')
    assert 'official_body_retrieved' in _sources_to_news_items([official])[0].tags


def test_official_reference_has_no_fabricated_publication_or_provider_claim():
    rows = radar._radar_official_basis_sources(DAY)
    assert all(r['published_date'] is None and r['date_status']=='unknown' for r in rows)
    assert all(official_evidence_status(r)=='reference_only' for r in rows)
    assert all(r['provider']=='official_reference' for r in rows)


def test_official_doc_in_research_cache_is_checked_but_parsed_data_not_called_body():
    body = {'source_id':'S1', 'url':'https://mops.twse.com.tw/STR/3703/notice.pdf',
        'title':'欣陸 3703 公告', 'content':'股票代號：3703 公司說明營收及風險。',
        'document_kind':'document_body','fetch_status':'success',
        'published_date':'2026-11-25','date_status':'explicit','provider':'official_fetch'}
    item = radar.RadarCandidate(code='3703',name='欣陸',evidence_pack={'research_sources':[body]})
    assert radar._candidate_has_official_document(item, DAY)
    item.evidence_pack['research_sources']=[{**body,'document_kind':'parsed_record'}]
    records=radar._candidate_official_records(item, DAY)
    assert records[0]['parsed_official_data_retrieved'] and not radar._candidate_has_official_document(item, DAY)
    assert records[0]['body_sha256'] and records[0]['claims_verified'] is False


@pytest.mark.parametrize('change', [
    {'fetch_status':'failed'}, {'date_status':'inferred'}, {'published_date':'2026-11-27'},
    {'data_date':'2026-11-27'}, {'updated_date':'2026-11-27'},
    {'url':'https://mops.twse.com.tw/STR/3704/notice.pdf'}])
def test_official_failed_future_unknown_or_other_issuer_stays_blocked(change):
    row={'url':'https://mops.twse.com.tw/STR/3703/notice.pdf', 'title':'欣陸3703',
         'content':'股票代號：3703', 'document_kind':'document_body', 'fetch_status':'success',
         'date_status':'explicit', 'published_date':'2026-11-25', **change}
    item=radar.RadarCandidate(code='3703',name='欣陸',web_sources=[row])
    assert not radar._candidate_has_official_document(item,DAY)


@pytest.mark.parametrize('command', ['research','value_scan','macro','theme','theme_flow','theme_radar','sector_strength','radar','news','topic_maintain'])
def test_all_high_input_packages_share_quarantine_and_keep_original(command):
    from research_center.ai_workflow_service import build_high_model_input_package
    item=SourceItem('S900',wrong()['title'],wrong()['url'],'Level 2',snippet=wrong()['summary'])
    data={'stock':{'code':'3703','name':'欣陸'},'financial_data':[{'Revenue':12345}]}
    package=build_high_model_input_package(CommandRequest(command, '/'+command,report_date=DAY),data,[item],prompt_chars_estimate=1000)
    assert '新增無人機訂單' not in json.dumps(package,ensure_ascii=False)
    assert item.snippet==wrong()['summary'] and data['financial_data']==[{'Revenue':12345}]
