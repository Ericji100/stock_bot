from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio
import json
import sqlite3

import httpx
import pytest

from research_center.ai_execution_control import (
    CONTROL, ExecutionControl, WorkStopped, bounded_timeout, check_work,
    execution_scope, checkpoint_sources, source_checkpoint,
)
from research_center.deepseek_service import ai_task
from research_center.openai_credit_service import OpenAICreditLedger
from research_center.openai_service import OpenAIService
from research_center.deepseek_search_service import DeepSeekSearchService
from research_center.models import CommandRequest, SourceItem
from research_center.news_repository import NewsRepository
from research_center.scheduled_task_service import ScheduledTaskService, ScheduledTaskSpec
from stock_ai_bot.telegram.telegram_health_service import TelegramHealthService
from telegram.error import TimedOut


def test_child_phase_expiry_does_not_cancel_classification_parent():
    now = [1.]
    outer = ExecutionControl(100, clock=lambda: now[0])
    child = ExecutionControl(20, parent=outer, reason='search_phase_deadline', clock=lambda: now[0])
    now[0] = 22.
    with pytest.raises(WorkStopped, match='停止') as error:
        child.check()
    assert error.value.status == 'search_phase_deadline'
    outer.check()
    outer.stop()
    with pytest.raises(WorkStopped):
        child.check()


def test_deadline_bounds_transport_wait_and_scope_resets():
    with execution_scope(5) as control:
        assert 0 < bounded_timeout(600) <= 5
        control.stop()
        with pytest.raises(WorkStopped):
            check_work()
    assert CONTROL.get() is None and bounded_timeout(600) == 600


def test_scheduled_timeout_stops_worker_before_next_paid_call():
    async def run():
        release, finished = Event(), Event()
        results = []
        def worker():
            release.wait(2)
            try:
                check_work()
                results.append('unexpected-new-paid-call')
            except WorkStopped:
                results.append('stopped')
            finally:
                finished.set()
        async def runner():
            await asyncio.to_thread(worker)
        service = ScheduledTaskService(sink=lambda _: None)
        with pytest.raises(asyncio.TimeoutError):
            await service._run_with_timeout(ScheduledTaskSpec('offline', 'offline', timeout_seconds=.02), runner)
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert results == ['stopped']
    asyncio.run(run())


def ledger_client(tmp_path, transport):
    ledger = OpenAICreditLedger(tmp_path, grants=[dict(grant_id='synthetic', account_id='secondary',
        organization_id='synthetic', starts_at='2026-11-01T00:00:00+00:00',
        expires_at='2026-12-01T00:00:00+00:00', verified_at='2026-11-01T00:00:00+00:00',
        allowance_usd='100', evidence='offline synthetic fixture')],
        now=lambda: datetime(2026, 11, 7, tzinfo=timezone.utc))
    with ledger.db():
        pass
    return ledger, OpenAIService(ledger, {'secondary':'synthetic-key'}, transport=transport)


def request_rows(ledger):
    with closing(sqlite3.connect(ledger.root/'openai-credits.sqlite3')) as c:
        return c.execute('SELECT status,actual,estimate FROM requests').fetchall()


def test_large_topic_packet_uses_official_count_without_trimming(tmp_path):
    prompt = '完整正文與反證' * 70000
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith('/input_tokens'):
            return httpx.Response(200, json={'object':'response.input_tokens','input_tokens':300000})
        return httpx.Response(200, json={'model':body['model'],'status':'completed',
            'output':[{'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}],
            'usage':{'input_tokens':300000,'output_tokens':100}})
    ledger, client = ledger_client(tmp_path, httpx.MockTransport(respond))
    assert not client.prompt_capacity(prompt)['fits']
    with ai_task('topic_maintain', background=True):
        client.generate_json(prompt)
    assert [path for path,_ in calls] == ['/v1/responses/input_tokens','/v1/responses']
    assert all(body['input'] == prompt for _,body in calls)
    assert calls[0][1]['instructions'] == calls[1][1]['instructions']
    assert calls[1][1]['truncation'] == 'disabled'
    assert len(request_rows(ledger)) == 1
    with ledger.db() as conn:
        detail=json.loads(conn.execute('SELECT detail FROM requests').fetchone()[0])
    assert detail['input_capacity_check']['counted_input_tokens'] == 300000


@pytest.mark.parametrize('response_kind',['oversize','invalid','transport'])
def test_unverified_or_oversized_count_never_reserves_analysis(tmp_path, response_kind):
    from research_center.openai_credit_service import OpenAIUnavailable
    from research_center.openai_service import OpenAIError
    calls=[]
    def respond(request):
        calls.append(request.url.path)
        assert request.url.path.endswith('/input_tokens')
        if response_kind=='transport':
            raise httpx.ReadTimeout('synthetic count-only timeout')
        return httpx.Response(200,json={'object':'response.input_tokens',
            'input_tokens':1050000 if response_kind=='oversize' else 'invalid'})
    ledger,client=ledger_client(tmp_path,httpx.MockTransport(respond))
    with ai_task('topic_maintain',background=True), pytest.raises((OpenAIError,OpenAIUnavailable)):
        client.generate_json('完整正文' * 100000)
    assert calls==['/v1/responses/input_tokens']
    assert request_rows(ledger)==[]


def test_short_capacity_and_stopped_count_do_not_add_network(tmp_path):
    ledger,client=ledger_client(tmp_path,httpx.MockTransport(lambda _:pytest.fail('No HTTP allowed')))
    assert client.verified_prompt_capacity('完整短資料')['fits']
    with execution_scope() as control:
        control.stop()
        with pytest.raises(WorkStopped):
            client.count_input_tokens('資料')
    assert request_rows(ledger)==[]


def test_topic_provider_capacity_gets_candidate_and_detail_stages():
    from research_center.topic_pipeline_service import _full_prompt_fits,_candidate_capacity_prompts
    calls=[]
    def capacity(prompt,**kwargs):
        calls.append((prompt,kwargs))
        return {'fits':True}
    assert _full_prompt_fits('完整中文' * 100000,capacity)
    parts=_candidate_capacity_prompts({'discovery_sources':[{'url':'https://example.test','content':'原文'}]},
        {},'{discovery_sources_json}',lambda template,v:template.replace('{discovery_sources_json}',v['discovery_sources_json']),capacity)
    assert len(parts)==1 and '原文' in parts[0]
    assert [kwargs['stage'] for _,kwargs in calls]==['detail_expand','candidate_extract']


def test_stopped_job_cannot_reserve_or_send(tmp_path):
    transport = httpx.MockTransport(lambda _: pytest.fail('external call attempted'))
    ledger, client = ledger_client(tmp_path, transport)
    with execution_scope() as control, ai_task('news', background=True):
        control.stop()
        with pytest.raises(WorkStopped):
            client.search('offline')
    assert request_rows(ledger) == []


def test_late_optional_followup_is_not_reserved_parent_can_classify(tmp_path):
    from research_center.search_routing import search_route
    calls = []
    def completed(request):
        calls.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={'model': body['model'], 'status': 'completed',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}],
            'usage': {'input_tokens': 1000, 'output_tokens': 100}})
    ledger, client = ledger_client(tmp_path, httpx.MockTransport(completed))
    with ai_task('news', background=True), execution_scope(2700):
        with execution_scope(11), search_route('news', 'critical_gap_search'):
            with pytest.raises(WorkStopped) as error:
                client.search('optional followup')
            assert error.value.status == 'insufficient_followup_time'
            assert request_rows(ledger) == []
        client.generate_json('classify retained original sources')
    assert len(calls) == 1 and request_rows(ledger)[0][0] == 'success'


def test_late_optional_followup_reuses_existing_native_cache(tmp_path):
    from research_center.search_routing import search_route
    calls = []
    def completed(request):
        calls.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={'model': body['model'], 'status': 'completed',
            'output': [{'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search'}},
                {'type': 'message', 'content': [{'type': 'output_text', 'text': '{"results":[]}'}]}],
            'usage': {'input_tokens': 1000, 'output_tokens': 100}})
    ledger, client = ledger_client(tmp_path, httpx.MockTransport(completed))
    for seconds in (120, 11):
        with ai_task('news', background=True), execution_scope(seconds), search_route('news', 'critical_gap_search'):
            result = client.search('identical optional followup')
    assert result['diagnostics']['cache_hit'] is True
    assert len(calls) == len(request_rows(ledger)) == 1


def test_pending_checkpoint_accepts_enrichment_without_losing_date_or_ai(tmp_path):
    from research_center.news_models import NewsItem
    repo = NewsRepository(tmp_path / 'news.db')
    old = NewsItem(url='https://example.test/news/a', title='原始題名', summary='摘要', full_text='摘要',
                   published_at='2026-11-06', tags=['publication_unknown', 'local_rule_fallback'],
                   created_at='2026-11-06T12:00:00')
    repo.save_many([old])
    enriched = NewsItem(url=old.url, title='完整題名', summary='完整摘要', full_text='完整原始正文' * 100,
                        published_at='2026-11-07T12:00:00', tags=['publication_explicit', 'local_rule_fallback'])
    repo.save_many([enriched])
    repo.save_many([old])
    restored = repo.get_by_url(old.url)
    assert restored.full_text == enriched.full_text
    assert restored.published_at == enriched.published_at and restored.created_at == old.created_at
    assert 'publication_explicit' in restored.tags and 'publication_unknown' not in restored.tags
    assert restored.classification_status == 'pending_ai'
    enriched.tags = ['publication_explicit', 'ai_classified']
    enriched.summary = '已完成 AI 分析'
    repo.save_many([enriched])
    repo.save_many([old])
    assert repo.get_by_url(old.url).summary == '已完成 AI 分析'
    assert repo.get_by_url(old.url).classification_status == 'ai_classified'


def test_source_conversion_preserves_original_body_and_explicit_backfill():
    from research_center.news_service import _sources_to_news_items, _backfill_refresh_item_dates
    source = SourceItem(source_id='S1', title='本文', url='https://example.test/a', source_level='Level 2',
                        snippet='搜尋摘要', content='完整原文' * 200)
    item = _sources_to_news_items([source])[0]
    assert item.summary == source.snippet and item.full_text == source.content
    with patch('research_center.source_date_normalizer.publication_metadata_date', return_value='2026-11-07'):
        _backfill_refresh_item_dates([item])
    assert item.tags.count('publication_explicit') == 1
    assert 'publication_unknown' not in item.tags and 'publication_inferred' not in item.tags


def test_news_partial_preview_counts_all_pending_not_just_deferred():
    from research_center.news_formatters import format_news_refresh_result
    text = format_news_refresh_result(143, 153, 6, [], {'classification_status': 'partial_success',
        'search_complete': False, 'deferred_classification_count': 135, 'local_fallback_count': 18,
        'ai_classified_count': 0})
    assert '新聞整理部分完成' in text and '待 AI 複核：153 則' in text
    assert '本次不是完整 AI 新聞分析' in text


def test_stop_between_reservation_and_send_refunds_only_unsent(tmp_path, monkeypatch):
    ledger, client = ledger_client(tmp_path, httpx.MockTransport(lambda _: pytest.fail('sent')))
    choose = ledger.choose
    with execution_scope() as control, ai_task('news', background=True):
        def reserve(*args, **kwargs):
            result = choose(*args, **kwargs)
            if kwargs.get('reserve'):
                control.stop()
            return result
        monkeypatch.setattr(ledger, 'choose', reserve)
        with pytest.raises(WorkStopped):
            client.search('offline')
    assert request_rows(ledger)[0][:2] == ('not_sent','0')


def test_inflight_unknown_preserved_no_second_request(tmp_path):
    calls = []
    with execution_scope() as control:
        def timeout(request):
            calls.append(request)
            control.stop()
            raise httpx.ReadTimeout('synthetic', request=request)
        ledger, client = ledger_client(tmp_path, httpx.MockTransport(timeout))
        with ai_task('news', background=True):
            from research_center.openai_credit_service import OpenAIUnavailable
            with pytest.raises(OpenAIUnavailable):
                client.search('first')
            with pytest.raises(WorkStopped):
                client.search('next')
    rows = request_rows(ledger)
    assert len(calls) == len(rows) == 1
    assert rows[0][0:2] == ('unknown',None) and Decimal(rows[0][2]) > 0


def test_late_success_is_settled_not_refunded_and_next_call_is_blocked(tmp_path):
    calls=[]
    with execution_scope() as control:
        def completed(request):
            calls.append(request)
            body=json.loads(request.content)
            control.stop()
            return httpx.Response(200,json={'model':body['model'],'status':'completed',
                'output':[{'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}],
                'usage':{'input_tokens':1000,'output_tokens':100}})
        ledger,client=ledger_client(tmp_path,httpx.MockTransport(completed))
        with ai_task('topic_maintain',background=True):
            client.generate_json('complete core')
            with pytest.raises(WorkStopped):
                client.generate_json('must not send')
    assert len(calls)==1
    row=request_rows(ledger)[0]
    assert row[0]=='success' and Decimal(row[1])>0


def test_same_organizer_packet_reuses_native_success_cache(tmp_path):
    calls=[]
    def completed(request):
        calls.append(request)
        body=json.loads(request.content)
        return httpx.Response(200,json={'model':body['model'],'status':'completed',
            'output':[{'type':'message','content':[{'type':'output_text','text':'{"facts":[]}'}]}],
            'usage':{'input_tokens':1000,'output_tokens':100}})
    ledger,base=ledger_client(tmp_path,httpx.MockTransport(completed))
    client=OpenAIService(ledger,base.accounts,organizer=True,transport=base.transport).with_timeout(180)
    for _ in range(2):
        with ai_task('topic_maintain',background=True):
            result=client.generate_json('same full segment and source IDs')
    assert result.diagnostics['cache_hit'] is True
    assert len(calls)==1 and len(request_rows(ledger))==1


def test_radar_followup_off_preserves_sources_and_gaps(monkeypatch):
    service = DeepSeekSearchService(SimpleNamespace(provider='openai_router'))
    monkeypatch.setattr('research_center.web_fetch_enrichment.recover_search_publication_dates', lambda *a: None)
    records = {'results':[{'title':'1234 公司風險', 'url':'https://example.test/a', 'summary':'完整原始內容'}]}
    request = CommandRequest(command='research', raw_text='/radar', target='1234 公司', report_date=date(2026,11,6))
    with patch.object(service, 'search', return_value=records) as search:
        result = service.discover(request, [{'label':'候選風險', 'queries':['initial']}])
    assert search.call_count == 1
    assert result.sources[0].snippet == '完整原始內容'
    assert result.diagnostics['critical_followup_limit'] == 0
    assert result.diagnostics['critical_gap_checks'][0]['remaining']
    assert result.diagnostics['critical_gap_checks'][0]['deferred_reason'] == 'radar_followup_disabled'
    assert not result.diagnostics['complete']
    with patch.object(service, 'search') as search:
        follow = service.discover(request, [{'label':'radar_evidence_gap','queries':['extra']}])
    search.assert_not_called()
    assert follow.diagnostics['critical_gap_checks'][0]['remaining']


def test_news_source_versions_preserved_and_fallback_cannot_erase_ai(tmp_path):
    from research_center.news_models import NewsItem
    repo = NewsRepository(tmp_path/'news.db')
    with ai_task('news', background=True):
        repo.save_discovery_checkpoint([SourceItem('S1','原文','https://example.test/a','Level 2',snippet='事件一')])
        repo.save_discovery_checkpoint([SourceItem('S1','原文','https://example.test/a','Level 2',snippet='事件二')])
    item = NewsItem(title='台股新聞',url='https://example.test/a',tags=['ai_classified'],summary='AI結論')
    repo.save_many([item])
    item.summary, item.tags = '未分類底稿', ['local_rule_fallback']
    repo.save_many([item])
    with closing(sqlite3.connect(repo.db_path)) as c:
        assert c.execute('SELECT count(*) FROM news_discovery_sources').fetchone()[0] == 2
        assert c.execute('SELECT summary FROM news_articles').fetchone()[0] == 'AI結論'


def test_checkpoint_io_failure_stops_before_another_search():
    with execution_scope(), source_checkpoint(lambda rows: (_ for _ in ()).throw(OSError('synthetic'))):
        with pytest.raises(WorkStopped) as error:
            checkpoint_sources([])
        assert error.value.status == 'source_checkpoint_failed'
        with pytest.raises(WorkStopped):
            check_work()


def test_news_phase_cutoff_retains_source_and_allows_classification(tmp_path, monkeypatch):
    from research_center.news_service import _discover_news_sources
    from research_center.ai_execution_control import checkpoint_sources
    repo = NewsRepository(tmp_path/'news.db')
    source = SourceItem('S1','台股新聞','https://example.test/a','Level 2',snippet='全部內容',published_date='2026-11-06')
    def discover(*args, **kwargs):
        checkpoint_sources([source])
        raise WorkStopped('search_phase_deadline')
    with patch('research_center.orchestrator._GeminiDiscoveryRunner') as runner, patch(
            'research_center.news_service._collect_holding_news_sources') as holdings:
        runner.return_value.run_discovery_flow.side_effect=discover
        with ai_task('news', background=True), execution_scope():
            sources, diagnostics = _discover_news_sources(SimpleNamespace(),repo,{},'deepseek',None)
            check_work()
    assert sources[0].snippet == '全部內容'
    assert diagnostics['complete'] is False
    holdings.assert_not_called()
    assert repo.query_pending_classification()[0].url == source.url


def test_news_batch_checkpoint_survives_later_unknown(tmp_path, monkeypatch):
    from research_center import news_service as n
    from research_center.news_models import NewsItem
    from research_center.deepseek_service import DeepSeekUnavailable
    repo=NewsRepository(tmp_path/'news.db')
    items=[NewsItem(title='台股新聞',url=f'https://example.test/{i}',published_at='2026-11-06') for i in range(2)]
    repo.save_many(n._fallback_classification(items))
    monkeypatch.setattr(n,'_classify_batch_size',lambda:1)
    center=SimpleNamespace(config=SimpleNamespace(deepseek_only=False))
    first=SimpleNamespace(raw='{"0":{"category":"company_news","summary":"已完成分析"}}')
    with patch.object(n,'_call_news_classifier',side_effect=[first,DeepSeekUnavailable('unknown','transport_unknown')]):
        result=n._classify_news_batches(items,center,lambda _:None,ai_model='gemini',
            prompt_text='{news_batch_json}',label='offline',checkpoint=repo.save_many)
    assert len(result)==2
    with closing(sqlite3.connect(repo.db_path)) as c:
        assert c.execute('SELECT summary FROM news_articles WHERE url=?',(items[0].url,)).fetchone()[0]=='已完成分析'
    assert repo.query_pending_classification()[0].url == items[1].url


@pytest.fixture
def quiet_health(tmp_path):
    clock=[datetime(2026,11,6,22,1,36,tzinfo=timezone(timedelta(hours=8)))]
    service=TelegramHealthService(tmp_path,clock=lambda:clock[0],emit=lambda _:None)
    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    return service,clock,bot


def test_two_eleven_second_incidents_are_diagnostic_only(quiet_health):
    service,clock,bot=quiet_health
    for _ in range(2):
        service.failed('getUpdates',TimedOut())
        asyncio.run(service.flush_notices(bot,1))
        clock[0]+=timedelta(seconds=11)
        service.succeeded('getUpdates')
        asyncio.run(service.flush_notices(bot,1))
        clock[0]+=timedelta(minutes=5)
    bot.send_message.assert_not_awaited()
    assert len(list((service.root/'diagnostics').glob('*.jsonl')))==1
    assert len((next((service.root/'diagnostics').glob('*.jsonl'))).read_text().splitlines())==4


def test_three_short_incidents_merge_once_and_survive_restart(quiet_health):
    service,clock,bot=quiet_health
    for _ in range(3):
        service.failed('getUpdates',TimedOut())
        clock[0]+=timedelta(seconds=11)
        service.succeeded('getUpdates')
        asyncio.run(service.flush_notices(bot,1))
        clock[0]+=timedelta(seconds=30)
    assert bot.send_message.await_count==1
    assert '近10分鐘發生3次' in bot.send_message.call_args.kwargs['text']
    resumed=TelegramHealthService(service.root,clock=lambda:clock[0],emit=lambda _:None)
    resumed.succeeded('getUpdates')
    asyncio.run(resumed.flush_notices(bot,1))
    assert bot.send_message.await_count==1


def test_sustained_outage_pairs_exactly_one_recovery(quiet_health):
    service,clock,bot=quiet_health
    service.failed('getUpdates',TimedOut())
    clock[0]+=timedelta(seconds=59)
    asyncio.run(service.flush_notices(bot,1))
    bot.send_message.assert_not_awaited()
    clock[0]+=timedelta(seconds=1)
    asyncio.run(service.flush_notices(bot,1))
    assert bot.send_message.await_count==1
    service.succeeded('getUpdates')
    asyncio.run(service.flush_notices(bot,1))
    asyncio.run(service.flush_notices(bot,1))
    assert bot.send_message.await_count==2
    assert '已恢復' in bot.send_message.call_args.kwargs['text']


def test_topic_single_organizer_uses_180_second_wait_and_900_total(tmp_path, monkeypatch):
    from research_center import ai_workflow_service as workflow
    from research_center.deepseek_service import DeepSeekResult
    seen=[]
    class Client:
        model='gpt-6-luna'
        provider='openai_router'
        def is_configured(self): return True
        def with_timeout(self, seconds):
            seen.append(seconds)
            return self
        def generate_json(self,prompt):
            assert 0 < CONTROL.get().remaining() <= 900
            return DeepSeekResult('{}',{}, {'actual_model':self.model})
    monkeypatch.setattr(workflow,'LOW_MODEL_ARTIFACT_DIR',tmp_path/'digests')
    monkeypatch.setattr(workflow,'write_prompt_log',lambda *a,**k:tmp_path/'prompt.json')
    request=CommandRequest(command='topic_maintain',raw_text='/topic_maintain',target='test')
    workflow.run_low_model_digest_for_payload(request,{'text':'完整來源'},minimax=Client(),enabled=True)
    assert seen==[180]
    assert CONTROL.get() is None


def test_topic_partial_organizer_preserves_completed_runs_and_stops(tmp_path, monkeypatch):
    from research_center import ai_workflow_service as workflow
    from research_center.deepseek_service import DeepSeekResult, DeepSeekUnavailable
    seen=[]
    class Client:
        model='gpt-6-luna'
        provider='openai_router'
        def is_configured(self): return True
        def with_timeout(self, seconds):
            assert seconds==180
            return self
        def generate_json(self,prompt):
            seen.append(prompt)
            if len(seen)==2:
                raise DeepSeekUnavailable('synthetic timeout','transport_unknown')
            return DeepSeekResult('{"facts":[{"text":"已成功的事實","source_ids":["S001"]}]}',{},
                                  {'actual_model':self.model})
    monkeypatch.setattr(workflow,'LOW_MODEL_ARTIFACT_DIR',tmp_path/'digests')
    monkeypatch.setattr(workflow,'write_prompt_log',lambda *a,**k:tmp_path/'prompt.json')
    monkeypatch.setattr(workflow,'LOW_MODEL_PROMPT_SOFT_LIMIT_CHARS',1)
    monkeypatch.setattr(workflow,'_build_low_model_payload_segments',lambda p:[
        {'label':str(i),'payload':{'text':f'S001 完整原文{i}'}} for i in range(3)])
    source=SourceItem('S001','來源','https://example.test/a','Level 2',snippet='完整原文')
    request=CommandRequest(command='topic_maintain',raw_text='/topic_maintain',target='test')
    with pytest.raises(DeepSeekUnavailable) as error:
        workflow.run_low_model_digest_for_payload(request,{'text':'complete'},sources=[source],
            minimax=Client(),enabled=True)
    partial=error.value.partial_low_model_digest
    assert len(seen)==2
    assert partial['status']=='partial_success'
    assert partial['facts'] and partial['segment_runs'][0]['status']=='success'
    assert partial['segment_runs'][1]['status']=='failed'
    assert partial['artifact_paths'] and list((tmp_path/'digests').rglob('*.json'))


def test_sustained_third_incident_is_one_alert_not_two(quiet_health):
    service,clock,bot=quiet_health
    for _ in range(2):
        service.failed('getUpdates',TimedOut())
        clock[0]+=timedelta(seconds=11)
        service.succeeded('getUpdates')
    service.failed('getUpdates',TimedOut())
    clock[0]+=timedelta(seconds=60)
    asyncio.run(service.flush_notices(bot,1))
    asyncio.run(service.flush_notices(bot,1))
    assert bot.send_message.await_count==1
    assert '近10分鐘發生3次' in bot.send_message.call_args.kwargs['text']
    service.succeeded('getUpdates')
    asyncio.run(service.flush_notices(bot,1))
    assert bot.send_message.await_count==2
    assert '已恢復' in bot.send_message.call_args.kwargs['text']


def test_legacy_pending_short_recovery_without_outage_sent_is_suppressed(quiet_health):
    service,clock,bot=quiet_health
    service.failed('getUpdates',TimedOut())
    service.succeeded('getUpdates')
    # Preserve old diagnostic entries but enforce the new rule at the final delivery boundary.
    service._notice('recovered','polling',service.state['channels']['polling'])
    service.persist()
    resumed=TelegramHealthService(service.root,clock=lambda:clock[0],emit=lambda _:None)
    resumed.succeeded('getUpdates')
    asyncio.run(resumed.flush_notices(bot,1))
    bot.send_message.assert_not_awaited()
    assert resumed.state['notices'][-1]['delivery']=='suppressed_threshold'
