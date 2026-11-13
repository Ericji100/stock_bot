"""No-network regressions for source namespaces, strict quotes and news coverage."""
from copy import deepcopy
from datetime import date
import json
import asyncio
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest

from research_center.source_identity import bind_source_ids, reference_sources
from research_center.topic_input_package import merge_topic_sources, topic_stage_variables
from research_center.topic_citation_audit import audit_topic_citations, restore_topic_source_quotes
from research_center.topic_models import TopicChangePack
from research_center.entity_resolver import exact_company_identity, company_aliases_from_sources
from research_center.news_models import NewsItem
from research_center.news_service import _correct_news_category, _fair_scheduled_news, news_category_coverage
from research_center.deepseek_service import ai_task
from research_center.deepseek_search_service import _news_task_quality


def sources():
    return [dict(source_id='S001', url='https://example.test/a', published_date='2026-11-06', content='第一篇完整正文與風險'),
            dict(source_id='S001', url='https://example.test/b', published_date='2026-07-13', content='第二篇不同事件與正文')]


def pack(evidence=None):
    return TopicChangePack.from_dict({'change_id':'test', 'mode':'update', 'status':'pending',
        'created_at':'2026-11-13T22:30:00+0800', 'actions':[{'theme_id':'test', 'theme_name':'測試',
            'evidence':[evidence or {'source':'test','url':'https://example.test/a','content':'保留主張'}]}]})


def test_stable_ids_are_unique_order_independent_and_idempotent():
    original = sources()
    before = deepcopy(original)
    rows = bind_source_ids(original)
    assert len({row['source_id'] for row in rows}) == 2
    assert all(row['source_id_aliases'] == ['S001'] for row in rows)
    assert bind_source_ids(rows) == rows
    assert {r['url']:r['source_id'] for r in bind_source_ids(original[::-1])} == {r['url']:r['source_id'] for r in rows}
    assert original == before


def test_merge_preserves_same_url_versions_events_and_full_bodies():
    first = {**sources()[0], 'search_summary_events':[{'event':'first'}]}
    enriched = {**first, 'content':first['content']+'；補充正文', 'search_summary_events':[{'event':'second'}]}
    later = {**first, 'published_date':'2026-11-07', 'content':'同網址不同資料期別'}
    data = {'discovery_sources':[first, later], 'web_fetched_sources':[enriched]}
    before = deepcopy(data)
    rows = merge_topic_sources(data)
    assert len(rows) == 2 and len({r['source_id'] for r in rows}) == 2
    assert rows[0]['content'] == enriched['content']
    assert rows[0]['search_summary_events'] == [{'event':'first'},{'event':'second'}]
    assert data == before


def test_date_annotated_alias_requires_unique_exact_supplied_document():
    rows = bind_source_ids(sources())
    assert reference_sources('S001（2026-11-06）', rows)[0]['url'] == sources()[0]['url']
    assert len(reference_sources('S001', rows)) == 2
    assert not reference_sources('S001（2026-11-05）', rows)
    assert not reference_sources('S001（2026-11-06）', rows, published_date='2026-07-13')
    p = pack()
    p.actions[0].affected_companies = [{'evidence_source_ids':['S001（2026-11-06）']}]
    assert audit_topic_citations(p, {'discovery_sources':sources()})['passed']


def test_same_id_same_date_different_urls_stays_ambiguous():
    rows = sources()
    rows[1]['published_date'] = rows[0]['published_date']
    p = pack({'source':'unrelated','url':'https://third.test/a','content':'主張'})
    p.actions[0].affected_companies = [{'evidence_source_ids':['S001（2026-11-06）']}]
    result = audit_topic_citations(p, {'discovery_sources':[*rows,{'url':'https://third.test/a'}]})
    assert not result['passed'] and result['counts']['source_id_ambiguous'] == 1


@pytest.mark.parametrize('replacement',['', '營收已成長30%，客戶已確認下單。'])
def test_strict_quote_repair_never_fabricates_or_changes_numbers(replacement):
    body = '公司說明：營收已成長20%，客戶仍在驗證階段，訂單尚待確認。'
    quote = replacement or '營收已成長20%，客戶仍在驗證階段，訂單尚待確認。'
    p = pack({'source':'test','url':'https://example.test/a','source_id':'S001','content':'主張', 'supporting_quote':quote})
    data = {'discovery_sources':[{'source_id':'S001','url':'https://example.test/a','content':body}]}
    before = deepcopy(p.to_dict())
    assert restore_topic_source_quotes(p, data) == []
    assert p.to_dict() == before
    assert audit_topic_citations(p, data)['passed'] == (not replacement)


def test_explicit_source_id_must_match_url_even_when_quote_exists_elsewhere():
    rows = sources()
    rows[1]['source_id'] = 'S002'
    p = pack({'source':'test','url':rows[0]['url'],'source_id':'S002','content':'主張'})
    result = audit_topic_citations(p, {'discovery_sources':rows})
    assert not result['passed'] and result['counts']['source_id_url_or_date_mismatch'] == 1


def test_quote_cannot_borrow_another_version_at_the_same_url():
    older = '公司說明：舊期訂單仍在驗證，尚未開始正式大量生產。'
    newer = '公司說明：新期已開始大量生產，訂單金額及營收貢獻均已確認。'
    docs = bind_source_ids([
        {'source_id':'S001','url':'https://example.test/a','published_date':'2026-11-01','content':older},
        {'source_id':'S001','url':'https://example.test/a','published_date':'2026-11-20','content':newer}])
    p = pack({'source':'test','url':docs[0]['url'],'source_id':docs[0]['source_id'],
              'publish_date':'2026-11-01','content':'主張','supporting_quote':older})
    data = {'discovery_sources':docs}
    assert audit_topic_citations(p,data)['passed']
    p.actions[0].evidence[0].supporting_quote = newer
    result = audit_topic_citations(p,data)
    assert not result['passed'] and result['counts']['supporting_quote_not_in_body'] == 1
    assert restore_topic_source_quotes(p,data) == []


def test_official_alias_never_comes_from_ai_or_media_and_never_promotes_relations():
    names = {'1111':'甲公司','2222':'乙公司'}
    row = {'公司代號':'1111','公司簡稱':'甲公司','公司名稱':'甲公司股份有限公司'}
    source = {'url':'https://openapi.twse.com.tw/v1/company','content':json.dumps([row]),'found_by':['source_text:provided_content']}
    data = {'authoritative_company_names':names, 'discovery_sources':[source]}
    aliases = company_aliases_from_sources(data)
    assert exact_company_identity(row['公司名稱'], '1111', names, aliases)['resolution'] == 'official_alias'
    assert exact_company_identity(row['公司名稱'], '2222', names, aliases)['status'] == 'conflict'
    data['discovery_sources'][0]['url'] = 'https://twse.com.tw.evil.test/company'
    assert not company_aliases_from_sources(data)
    assert exact_company_identity('中華精測','6510',{'6510':'精測'})['status'] == 'alias_pending'
    assert exact_company_identity('聯電','2330',{'2330':'台積電','2303':'聯電'})['status'] == 'conflict'


@pytest.mark.parametrize('title,expected', [
    ('外資10.1萬口空單創新高 台指期夜盤41K保衛戰','market_focus'),
    ('AI訂單熱、消費電子降溫 六家台廠法說拆解漲價與毛利落差','supply_chain'),
    ('某公司法說揭曉 季度EPS創高','company_news'),
    ('PCB概念股集體齊漲 資金輪動','sector_rotation'),
])
def test_main_event_category_not_generic_risk_or_ai_keyword(title, expected):
    assert _correct_news_category(NewsItem(title=title, category='company_news', tags=['ai_classified'])) == expected


def test_supply_chain_selection_uses_existing_article_not_index_and_keeps_all_rows():
    ordinary = [NewsItem(title=f'大盤法人買超新聞{i}',url=f'https://a.test/{i}', summary='資料'*80) for i in range(35)]
    index = NewsItem(title='供應鏈新聞首頁',url='https://www.moneydj.com',summary='資料'*80)
    supply = NewsItem(title='多家台廠訂單回升 產能與供料成本變化',url='https://a.test/article/supply',summary='資料'*80)
    with ai_task('news', background=True):
        result = _fair_scheduled_news([*ordinary,index,supply],set(),{})
    assert result[0] is supply and len(result) == 37
    assert {id(x) for x in result} == {id(x) for x in [*ordinary,index,supply]}
    supply.tags = ['ai_classified']
    index.tags = ['ai_classified','ai_excluded']
    coverage = news_category_coverage([supply,index])
    assert coverage['usable_category_counts']['supply_chain'] == 1
    assert 'supply_chain' in news_category_coverage([index])['missing_categories']


def test_supply_quality_cannot_be_satisfied_by_generic_ai_market_news():
    rows = [{'url':f'https://www.cna.com.tw/news/afe/20261113{n:04}.aspx', 'title':'台股盤後 指數與AI股票上漲',
             'published_date':'2026-11-13','snippet':'台股上漲與AI題材'*50} for n in range(4)]
    result = _news_task_quality(rows, {'category':'supply_chain','terms':['台股','AI']}, date(2026,11,13))
    assert result['recent_substantive_articles'] == 0 and not result['passed']


def test_scheduled_category_gap_is_partial_not_provider_failure():
    import main
    meta = {'classification_status':'ai_success','ai_classified_count':30,
            'classification_category_coverage':{'missing_categories':['supply_chain']}}
    center=SimpleNamespace(config=SimpleNamespace(database_path='unused'),deepseek=None)
    async def run():
        with patch('research_center.orchestrator.ResearchCenter',return_value=center), \
             patch.object(main,'load_config',return_value={'chat_id':1}), \
             patch.object(main,'NewsRepository'),patch.object(main,'run_news_refresh',return_value=([],meta)), \
             patch.object(main,'_scheduled_ai_gate',AsyncMock(return_value='')), \
             patch.object(main,'_flush_ai_notices',AsyncMock(return_value=[])), \
             patch.object(main,'_scheduled_outcome_notice',AsyncMock()) as notice:
            result=await main._scheduled_news_refresh(SimpleNamespace())
        detail=notice.call_args.args[3]
        assert detail['status']=='partial_success' and detail['failed'] is False
        assert '供應鏈與產業' in detail['reason'] and result['status']=='partial_success'
    with ai_task('news',background=True):
        asyncio.run(run())
