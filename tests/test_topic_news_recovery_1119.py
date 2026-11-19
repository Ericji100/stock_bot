"""Strict evidence binding and restart-safe, no-search backlog recovery."""
import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from research_center.topic_models import TopicChangePack, TopicEvidence
from research_center.topic_input_package import merge_topic_sources, topic_stage_variables, append_topic_shared_records, expand_topic_stage_variables
from research_center.topic_quote_binding import bind_topic_quotes, quote_catalog, extend_short_source_quote, topic_repair_plan
from research_center.topic_citation_audit import audit_topic_citations, restore_topic_source_quotes
from research_center.news_repository import NewsRepository
from research_center.news_models import NewsItem
from research_center import news_service as news
from research_center.deepseek_service import ai_task, RUN_ID
from research_center.scheduled_outcome_notice import ScheduledOutcomeNotices
from tests.test_news_schedule_balance import offline_refresh, article, source


def data(body=None):
    return {'report_date':'2026-11-17','discovery_sources':[{'source_id':'S001', 'url':'https://example.test/a',
        'published_date':'2026-11-16', 'content':body or '公司表示產能正在擴建，實際客戶訂單及獲利仍須觀察，不能視為確定受惠。',
        'fetch_status':'success','fetch_provider':'requests_bs4'}]}


def pack(evidence):
    return TopicChangePack.from_dict({'change_id':'offline','created_at':'2026-11-17',
        'actions':[{'theme_id':'power','theme_name':'電力','evidence':[evidence]}]})


def test_catalog_roundtrip_covers_every_character_and_is_stable():
    d=data(('完整正文與反證。\n'*700)+'短尾')
    rows=merge_topic_sources(d); before=deepcopy(d)
    spans=quote_catalog(rows)
    assert ''.join(row['text'] for row in spans)==rows[0]['content']
    assert len({row['quote_id'] for row in spans})==len(spans)
    assert all(row['text']==rows[0]['content'][row['start']:row['end']] for row in spans)
    assert quote_catalog(rows)==spans and d==before


def test_body_codec_is_opt_in_for_other_command_payloads():
    from research_center.topic_input_package import lossless_evidence_json,expand_evidence_json
    value={'raw':{'$topic_body':[{'text':'保留原始字典，不把來源任意欄位當作引用'}]}}
    assert expand_evidence_json(json.loads(lossless_evidence_json(value)))==value


def test_quote_id_survives_model_and_normalizer_and_binds_exact_text():
    from research_center.topic_schema_normalizer import normalize_topic_detail_actions
    d=data();span=quote_catalog(merge_topic_sources(d))[0]
    payload={'actions':[{'theme_id':'power','evidence':[{'quote_id':span['quote_id']}]}]}
    p=pack({});p.actions=normalize_topic_detail_actions(payload, fallback_candidates=[])
    assert p.actions[0].evidence[0].quote_id==span['quote_id']
    bind_topic_quotes(p,d)
    assert p.actions[0].evidence[0].supporting_quote==span['text']
    assert audit_topic_citations(p,d)['passed']
    assert payload['actions'][0]['evidence'][0]=={'quote_id':span['quote_id']}


@pytest.mark.parametrize('override',[{'quote_id':'Qunknown'},{'url':'https://example.test/wrong'},
    {'publish_date':'2026-11-18'},{'source_id':'S999'}])
def test_wrong_span_or_conflicting_identity_never_passes(override):
    d=data();s=quote_catalog(merge_topic_sources(d))[0]
    p=pack({'quote_id':s['quote_id'],'url':s['url'],**override})
    bind_topic_quotes(p,d)
    assert not audit_topic_citations(p,d)['passed']


def test_same_url_different_body_version_cannot_borrow_span():
    d=data();d['discovery_sources'].append({**d['discovery_sources'][0],'source_id':'S002','published_date':'2026-11-15','content':'不同期別原文，絕對不能借用其他資料期別的公司營收或訂單。'})
    s=quote_catalog(merge_topic_sources(d))[0]
    p=pack({'quote_id':s['quote_id'],'source_id':'S002'})
    assert bind_topic_quotes(p,d)['issues']


def test_company_knowledge_uses_same_binding_and_unknown_span_is_blocked():
    d=data();span=quote_catalog(merge_topic_sources(d))[0]
    p=pack({'url':'https://example.test/a'})
    p.company_knowledge_updates={'companies':{'2330':{'evidence':[{'quote_id':span['quote_id']}]}}}
    bind_topic_quotes(p,d)
    assert p.company_knowledge_updates['companies']['2330']['evidence'][0]['supporting_quote']==span['text']
    p.company_knowledge_updates['companies']['2330']['evidence'][0]['quote_id']='Qunknown'
    bind_topic_quotes(p,d)
    audit=audit_topic_citations(p,d)
    assert not audit['passed'] and topic_repair_plan(p,audit)['other_issues']


def test_prompt_has_catalog_and_original_sources_no_truncation():
    d=data();v=topic_stage_variables(d,{})
    spans=json.loads(v['topic_quote_catalog_json'])
    assert spans and 'text' not in spans[0]
    assert expand_topic_stage_variables(v)['discovery_sources_json'][0]['content']==d['discovery_sources'][0]['content']
    assert spans[0]['quote_id'] in append_topic_shared_records('',v)
    assert d['discovery_sources'][0]['content'] in v['discovery_sources_json']


def test_exact_fifteen_character_quote_extends_to_original_sentence():
    q='例如東元與半導體廠推動綠電轉供'
    d=data('業者同時布局綠電；'+q+'，台達則以大型儲能與微電網案場提供支援。')
    p=pack({'url':'https://example.test/a','supporting_quote':q})
    before=audit_topic_citations(p,d)
    assert before['issues'][0]['reason']=='supporting_quote_too_short'
    restore_topic_source_quotes(p,d)
    assert q in p.actions[0].evidence[0].supporting_quote
    assert p.actions[0].evidence[0].supporting_quote in d['discovery_sources'][0]['content']
    assert audit_topic_citations(p,d)['passed']


def test_duplicate_short_paraphrase_and_one_character_never_repaired():
    assert extend_short_source_quote('是','是的公司說明原文還有其他資料與反證。') is None
    q='例如東元與半導體廠推動綠電轉供'
    assert extend_short_source_quote(q,q+'。'+q+'。') is None
    assert extend_short_source_quote(q,'東元獲得大量訂單與營收。') is None
    p=pack({'url':'https://example.test/a','supporting_quote':'公司保證未來訂單暴增且沒有風險。'})
    restore_topic_source_quotes(p,data())
    assert not audit_topic_citations(p,data())['passed']


def test_ambiguous_alias_remains_blocked_and_repair_scope_only_bad_action():
    d=data();d['discovery_sources'].append({**d['discovery_sources'][0],'url':'https://example.test/b','content':'另一公司與事件的完整原文，不能猜配來源。'})
    p=pack({'url':'https://example.test/a','content':'保留分析'})
    p.actions[0].affected_companies=[{'evidence_source_ids':['S001']}]
    p.actions[0].evidence.append(TopicEvidence.from_dict({'url':'https://example.test/b','content':'另一背景'}))
    bind_topic_quotes(p,d)
    audit=audit_topic_citations(p,d)
    assert any(x['reason']=='source_id_ambiguous' for x in audit['issues'])
    plan=topic_repair_plan(p,audit)
    assert plan['blocked_theme_ids']==['power'] and plan['automatic_paid_repair'] is False


def test_run_journal_recovers_dead_owner_once_and_preserves_inputs(tmp_path):
    repo=NewsRepository(tmp_path/'news.db');it=article(1,pending=True)
    repo.save_many([it]);repo.record_refresh('run','2026-11-18','search')
    assert not repo.recover_interrupted_refreshes(lambda pid: True)
    recovered=repo.recover_interrupted_refreshes(lambda pid: False)
    assert len(recovered)==1 and repo.refresh_requires_resume()
    assert not repo.recover_interrupted_refreshes(lambda pid: False)
    assert repo.get_by_url(it.url).full_text==it.full_text
    with sqlite3.connect(repo.db_path) as c:
        assert c.execute('SELECT COUNT(*) FROM news_refresh_events').fetchone()[0]==2


def test_recovery_notification_is_one_attempt_and_no_ai_call(tmp_path):
    store=ScheduledOutcomeNotices(tmp_path/'outcome');send=AsyncMock()
    async def run():
        detail={'status':'interrupted','failed':True,'reason':'搜尋程序中斷，未結算請求不重送'}
        assert await store.observe('news','run','2026-11-18',detail,send)=='sent'
        assert await store.observe('news','run','2026-11-18',detail,send)=='duplicate'
    asyncio.run(run());assert send.await_count==1
    assert store.recorded_run_ids('news')=={'run'}


def test_legacy_unclosed_november18_sources_adopted_only_with_exact_run(tmp_path):
    repo=NewsRepository(tmp_path/'news.db')
    with ai_task('news',background=True):
        rid=RUN_ID.get();repo.save_discovery_checkpoint([source(article(1))])
    with sqlite3.connect(repo.db_path) as c:
        c.execute("UPDATE news_discovery_sources SET saved_at='2026-11-18T18:04:29+08:00'")
    rows=[{'task_id':'scheduled:news:1800','timestamp':'2026-11-18T18:00:00+08:00','status':'started'}]
    now=datetime.fromisoformat('2026-11-19T08:00:00+08:00')
    assert not repo.import_unclosed_refreshes(rows,{rid},as_of=now)
    assert not repo.import_unclosed_refreshes(rows,set(),as_of=now-timedelta(days=1))
    assert not repo.import_unclosed_refreshes(rows,set(),as_of=now) # No verified background attribution.
    restored=repo.import_unclosed_refreshes(rows,set(),as_of=now,scheduled_run_ids={rid})
    assert [x['run_id'] for x in restored]==[rid] and repo.refresh_requires_resume()
    assert not repo.import_unclosed_refreshes(rows,set(),as_of=now)


def test_backlog_plan_reserves_classification_without_expanding_limit():
    with ai_task('news',background=True):
        assert news.news_workload_plan(55)['skip_search']
        assert news.news_workload_plan(21)['search_seconds']==300
        assert news.news_workload_plan(0)['search_seconds']==900
        assert news.news_workload_plan(0,resume_saved=True)['skip_search']
        assert news.news_workload_plan(55)['classification_limit']==30


def test_resume_classifies_saved_data_without_any_search_or_fetch(tmp_path,monkeypatch,offline_refresh):
    center,calls=offline_refresh;repo=NewsRepository(tmp_path/'news.db')
    repo.save_many([article(i,pending=True) for i in range(4)])
    def forbidden(*a,**kw):raise AssertionError('No new search or fetch')
    monkeypatch.setattr(news,'_discover_news_sources',forbidden)
    monkeypatch.setattr(news,'_enrich_sources_with_web_fetch',forbidden)
    with ai_task('news',background=True):
        _,meta=news._run_news_refresh(center,repo,resume_saved=True)
    assert meta['ai_classified_count']==4 and calls
    assert meta['selected_classification_complete'] and not meta['search_complete']
    assert meta['classification_status']=='partial_success'


def test_run_wrapper_persists_failure_and_reuses_dead_owner_checkpoint(tmp_path,monkeypatch):
    repo=NewsRepository(tmp_path/'news.db')
    monkeypatch.setattr(news,'_run_news_refresh',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('offline crash')))
    with ai_task('news',background=True),pytest.raises(RuntimeError,match='offline crash'):
        news.run_news_refresh(SimpleNamespace(),repo)
    assert repo.refresh_requires_resume()


def test_post_init_closes_unfinished_news_and_notifies_without_ai(tmp_path,monkeypatch):
    import main
    repo=NewsRepository(tmp_path/'news.db')
    repo.record_refresh('dead-owner','2026-11-18','search')
    from tools import bot_watchdog
    monkeypatch.setattr(bot_watchdog,'default_process_exists',lambda pid: False)
    monkeypatch.setattr(main,'register_bot_commands',AsyncMock())
    monkeypatch.setattr(main,'initialize_etf_monitor',AsyncMock())
    monkeypatch.setattr(main,'write_bot_heartbeat',lambda **kw:None)
    monkeypatch.setattr(main,'load_config',lambda:{'chat_id':'offline'})
    monkeypatch.setattr('research_center.config.load_research_config',lambda:SimpleNamespace(database_path=repo.db_path))
    monkeypatch.setattr(main,'get_workdata_path',lambda *parts:tmp_path.joinpath(*parts))
    sender=AsyncMock();monkeypatch.setattr(main,'safe_send_bot_message',sender)
    app=SimpleNamespace(bot=object(),job_queue=None)
    asyncio.run(main.post_init(app))
    assert sender.await_count==1 and repo.refresh_requires_resume()
    asyncio.run(main.post_init(app))
    assert sender.await_count==1
