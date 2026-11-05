"""Real filesystem/database operations in isolated fixtures, without paid HTTP."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from research_center import topic_repository as repository
from research_center.topic_apply_service import confirm_change_pack, reject_change_pack
from research_center.topic_models import TopicChangePack, TopicChangeMode, TopicChangeStatus, TopicChangeAction, TopicActionType


class MarketDataDateAuditTests(unittest.TestCase):
    def test_legacy_theme_flow_snapshot_dates_are_audited_without_network(self):
        from datetime import date
        from research_center import theme_radar_service as service
        root = Path(os.environ['CODEX_TASK_TEST_ROOT']) / uuid4().hex
        directory = root / '.cache/market_movers'
        directory.mkdir(parents=True)
        rows = [{'code': 'A', 'price_date': '2026-10-30'}, {'code': 'B', 'price_date': '2026-10-30'},
                {'code': 'C', 'price_date': '2026-11-02'}]
        payload = {'market_data_date': '2026-11-02', 'active_movers': rows, 'top_gainers': rows}
        (directory / 'market_movers_2026-11-03.json').write_text(json.dumps(payload), encoding='utf-8')
        with patch.object(service, 'ROOT_DIR', root), patch.object(service, 'build_market_movers') as build:
            data = service._load_cached_market_movers_snapshot(date(2026, 11, 3))
        build.assert_not_called()
        self.assertEqual(data['market_data_date'], '2026-10-30')
        audit = data['data_quality']['price_date_audit']
        self.assertEqual(audit['date_counts']['2026-10-30'], 2)
        self.assertEqual(audit['scope'], 'snapshot_visible_stock_rows_only')

    def test_input_counts_follow_stock_references_not_json_wrapper_keys(self):
        from research_center.ai_workflow_service import _build_core_input_audit, _semantic_count, COMPLETE_SEGMENT_CONTEXT_SCHEMA_VERSION
        stocks = [{'code': str(code)} for code in range(1000, 1100)]
        refs = {'representation': 'stock_code_refs', 'stock_codes': [row['code'] for row in stocks]}
        self.assertEqual(_semantic_count(refs), 100)
        chunks = {'資料型態': '完整分段清單', '總筆數': 999,
                  '段落': [{'資料': stocks[:40]}, {'資料': stocks[40:]}]}
        self.assertEqual(_semantic_count(chunks), 100)
        self.assertEqual(_semantic_count({'schema_version': COMPLETE_SEGMENT_CONTEXT_SCHEMA_VERSION, 'payload': refs}), 100)
        audit = _build_core_input_audit('theme_radar', {'strong_stocks': stocks}, {'strong_stocks': refs})
        row = next(row for row in audit['sections'] if row['section'] == 'strong_stocks')
        self.assertEqual(row['raw_count'], row['sent_count'])
        self.assertIn('股票主檔', row['note'])

    def test_a_few_new_bars_do_not_relabel_the_whole_market(self):
        from datetime import date
        from research_center.market_movers_service import _market_data_date, _price_date_audit
        rows = [{'price_date': '2026-10-30'}] * 1984 + [{'price_date': '2026-11-02'}] * 4
        audit = _price_date_audit(rows, date(2026, 11, 3))
        self.assertEqual(_market_data_date(rows), '2026-10-30')
        self.assertEqual(audit['latest_date_stock_count'], 4)
        self.assertEqual(audit['date_counts']['2026-10-30'], 1984)
        self.assertTrue(audit['mixed_dates'])
        self.assertTrue(audit['warnings'])

    def test_unknown_dates_are_not_replaced_with_the_report_day(self):
        from datetime import date
        from research_center.market_movers_service import _assemble_market_movers
        data = _assemble_market_movers(date(2026, 11, 3), [{'code': 'TEST', 'name': '合成資料'}])
        self.assertEqual(data['market_data_date'], 'unknown')
        self.assertEqual(data['data_quality']['price_date_audit']['undated_stock_count'], 1)

    def test_future_dates_are_explicitly_warned_without_deleting_rows(self):
        from datetime import date
        from research_center.market_movers_service import _price_date_audit
        rows = [{'price_date': '2026-11-04'}, {'price_date': 'invalid'}]
        audit = _price_date_audit(rows, date(2026, 11, 3))
        self.assertEqual(audit['undated_stock_count'], 1)
        self.assertTrue(any('指定日期' in warning for warning in audit['warnings']))
        self.assertEqual(len(rows), 2)


class TopicFilesystemWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(os.environ.get('CODEX_TASK_TEST_ROOT','.cache/test_tmp/deepseek_data'))/uuid4().hex
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        paths={'_CHANGE_PACK_DIR':'packs','_AUDIT_DIR':'audit','_PROMPT_LOG_DIR':'prompts',
               '_RAW_RESP_DIR':'raw','_BACKUP_DIR':'backup','_TOPIC_PROFILES_PATH':'profiles.json',
               '_COMPANY_TOPIC_MAP_PATH':'company_map.json','_SUPPLY_CHAIN_PATH':'nodes.json',
               '_COMPANY_KNOWLEDGE_PATH':'knowledge.json'}
        for key,name in paths.items():
            self.stack.enter_context(patch.object(repository,key,self.root/name))

    def pack(self, change_id, reviewed=True):
        pack=TopicChangePack(change_id=change_id,parent_change_id=None,mode=TopicChangeMode.UPDATE,
            status=TopicChangeStatus.PENDING,model='deepseek-flash',created_at='2026-11-02T10:00:00+0800',
            updated_at='2026-11-02T10:00:00+0800',summary='隔離測試資料',confidence='medium',
            actions=[TopicChangeAction(action_type=TopicActionType.CREATE_THEME,theme_id='test_power',theme_name='隔離電源題材',
                reason='合成測試證據，非真實市場資料',risk_notes=['需求不確定'],missing_data=['待查實際訂單'])])
        pack.extra['ai_review_complete']=reviewed
        repository.save_change_pack(pack)
        return pack

    def test_review_confirm_requery_and_reject_do_not_touch_production(self):
        self.pack('change_confirm')
        self.assertEqual(repository.load_change_pack('change_confirm').status,TopicChangeStatus.PENDING)
        result=confirm_change_pack('change_confirm','isolated-test')
        self.assertTrue(result.success,result.errors)
        self.assertEqual(repository.load_change_pack('change_confirm').status,TopicChangeStatus.CONFIRMED)
        self.assertEqual(repository.load_topic_profiles()[0].theme_name,'隔離電源題材')
        before=(self.root/'profiles.json').read_bytes()
        self.pack('change_reject')
        self.assertTrue(reject_change_pack('change_reject','isolated-test','資料不足')['success'])
        self.assertEqual(repository.load_change_pack('change_reject').status,TopicChangeStatus.REJECTED)
        self.assertEqual((self.root/'profiles.json').read_bytes(),before)
        self.assertFalse(confirm_change_pack('change_reject').success)

    def test_unreviewed_pack_cannot_write_topic_library(self):
        self.pack('change_incomplete',reviewed=False)
        self.assertFalse(confirm_change_pack('change_incomplete').success)
        self.assertFalse((self.root/'profiles.json').exists())


class NewsEvidencePersistenceTests(unittest.TestCase):
    def test_date_corrections_survive_duplicate_pending_items_without_losing_ai_fields(self):
        from research_center.news_models import NewsItem
        from research_center.news_repository import NewsRepository
        root=Path(os.environ['CODEX_TASK_TEST_ROOT'])/uuid4().hex
        repo=NewsRepository(root/'date.sqlite3')
        original=NewsItem(title='同篇新聞',url='https://example.test/item',published_at='2026-11-01',
                          summary='已完成分析',counter_evidence=['需求轉弱'])
        repo.save(original)
        incoming=NewsItem(title=original.title,url=original.url,published_at='2026-11-02T10:00:00',tags=['local_rule_fallback'])
        repo.save_many([incoming])
        restored=repo.get_by_url(original.url)
        self.assertEqual(restored.published_at,incoming.published_at)
        self.assertEqual(restored.summary,original.summary)
        self.assertEqual(restored.counter_evidence,original.counter_evidence)
        incoming.published_at='2026-11-02'
        repo.save_many([incoming])
        self.assertEqual(repo.get_by_url(original.url).published_at,'2026-11-02T10:00:00')

    def test_unknown_publication_date_stays_unknown_after_refresh(self):
        from research_center.news_models import NewsItem
        from research_center.news_service import _backfill_refresh_item_dates
        item=NewsItem(title='日期不明的台股新聞',url='https://example.test/undated',news_origin='refresh')
        _backfill_refresh_item_dates([item])
        self.assertEqual(item.published_at,'')
        self.assertTrue(item.created_at)

    def test_ai_counter_evidence_and_gaps_survive_save_update_and_query(self):
        from research_center.news_models import NewsItem
        from research_center.news_repository import NewsRepository
        from research_center.news_service import _apply_classification_meta
        from research_center.news_formatters import format_news_refresh_result
        root=Path(os.environ.get('CODEX_TASK_TEST_ROOT','.cache/test_tmp/deepseek_data'))/uuid4().hex
        repo=NewsRepository(root/'news.sqlite3')
        item=NewsItem(title='隔離新聞',url='https://example.com/evidence',source='測試媒體')
        repo.save(item)
        items=_apply_classification_meta([item],{'0':{'category':'ai_semiconductor',
            'summary':'新產品量產','news_signal_reason':'可能帶動供應鏈',
            'counter_evidence':['客戶可能延後驗收'],'missing_data':['缺少營收占比'],
            'tags':['market_story']}})
        items[0].full_text='公司原文'
        repo.save_many(items)
        restored=repo.query_all_recent()[0]
        self.assertEqual(restored.counter_evidence,['客戶可能延後驗收'])
        self.assertEqual(restored.missing_data,['缺少營收占比'])
        self.assertEqual(NewsItem.from_dict(restored.to_dict()).missing_data,restored.missing_data)
        from datetime import date
        from research_center.date_aware_context import _news_item_to_context
        context=_news_item_to_context(restored,date(2026,11,2),[])
        self.assertEqual(context['missing_data'],restored.missing_data)
        self.assertEqual(context['counter_evidence'],restored.counter_evidence)
        text=format_news_refresh_result(0,1,1,[restored])
        self.assertIn('資料不足：缺少營收占比',text)
        self.assertIn('客戶可能延後驗收',text)
        self.assertIn('分析線索：可能帶動供應鏈',text)
        self.assertNotIn('market_story',text)


class NewsSearchQualityGateTests(unittest.TestCase):
    def test_shared_publication_dates_convert_utc_to_taiwan_without_moving_date_only_values(self):
        from datetime import datetime, timezone
        from research_center.source_date_normalizer import normalize_published_date
        self.assertEqual(normalize_published_date('2026-11-01T16:00:00+00:00'),'2026-11-02')
        self.assertEqual(normalize_published_date('2026-11-01T16:00:00Z'),'2026-11-02')
        self.assertEqual(normalize_published_date(datetime(2026,11,1,16,tzinfo=timezone.utc)),'2026-11-02')
        self.assertEqual(normalize_published_date('2026-11-01'),'2026-11-01')
        self.assertEqual(normalize_published_date('2026-11-01T16:00:00'),'2026-11-01')

    def run_search(self, *, day='2026-11-02', command='news', require_official=False, short=False):
        from datetime import date
        from types import SimpleNamespace
        from research_center.deepseek_search_service import DeepSeekSearchService
        service=DeepSeekSearchService(SimpleNamespace())
        rows=[{'title':'台股供應鏈風險與新訂單','url':f'https://{host}/{path}', 'date':day,
               'content':'短文' if short else '台股供應鏈訂單增加，但需注意庫存與需求轉弱風險。'*10}
              for host,path in [('www.ctee.com.tw','news/20261102700039-430502'),
                                ('news.cnyes.com','news/id/12345'),('money.udn.com','money/story/5607/12345'),
                                ('technews.tw','2026/11/02/evidence')]]
        tasks=[{'label':'供應鏈與產業','queries':[f'完整查詢{i}' for i in range(6)],
                'news_quality_policy':{'terms':['供應鏈'],'require_official':require_official}}]
        with patch.object(service,'search',return_value={'results':rows}) as search:
            result=service.discover(SimpleNamespace(command=command,report_date=date(2026,11,2)),tasks)
        return search.call_count,result

    def test_fresh_relevant_multi_domain_articles_stop_redundant_queries_but_keep_sources(self):
        count,result=self.run_search()
        self.assertEqual(count,2)
        self.assertEqual(len(result.sources),4)
        self.assertEqual(result.diagnostics['runs'][0]['early_stop'],'quality_satisfied')
        self.assertEqual(result.diagnostics['runs'][0]['unexecuted_query_count'],4)

    def test_old_short_or_non_official_results_do_not_satisfy_quality_gate(self):
        for kwargs in [{'day':'2026-10-26'},{'short':True},{'require_official':True}]:
            with self.subTest(kwargs=kwargs):
                count,result=self.run_search(**kwargs)
                self.assertEqual(count,3)
                run = result.diagnostics['runs'][0]
                self.assertFalse(run['quality']['passed'])
                self.assertEqual(run['early_stop'], 'no_new_sources_quality_insufficient')
                self.assertEqual(run['unexecuted_query_count'], 3)
                self.assertEqual(len(result.sources), 4)

    def test_insufficient_quality_continues_when_queries_add_sources(self):
        from datetime import date
        from types import SimpleNamespace
        from research_center.deepseek_search_service import DeepSeekSearchService
        service = DeepSeekSearchService(SimpleNamespace())
        tasks = [{'label': '官方缺口', 'queries': [f'query{i}' for i in range(6)],
                  'news_quality_policy': {'terms': ['台股'], 'require_official': True}}]
        def search(query, **kwargs):
            return {'results': [{'title': '台股新聞', 'url': f'https://example.test/{query}',
                                 'date': '2026-11-02', 'summary': '台股需求與庫存風險。' * 20}]}
        with patch.object(service, 'search', side_effect=search) as called:
            result = service.discover(SimpleNamespace(command='news', report_date=date(2026,11,2)), tasks)
        self.assertEqual(called.call_count, 6)
        self.assertFalse(result.diagnostics['runs'][0]['quality']['passed'])
        self.assertNotIn('early_stop', result.diagnostics['runs'][0])
        self.assertEqual(len(result.sources), 6)

    def test_other_commands_are_not_limited_by_news_gate(self):
        count,result=self.run_search(command='macro')
        self.assertEqual(count,6)

    def test_news_categories_and_negative_evidence_task_are_not_removed(self):
        from research_center.news_service import build_news_discovery_queries
        tasks=build_news_discovery_queries()
        self.assertEqual(len(tasks),13)
        self.assertTrue(next(t for t in tasks if t['label']=='反證與利空')['news_quality_policy']['terms'])
        self.assertTrue(next(t for t in tasks if t['label']=='個股公告 / MOPS')['news_quality_policy']['require_official'])

    def test_real_prompt_registry_preserves_news_quality_policy_for_official_search(self):
        from research_center.command_parser import parse_command_text
        from research_center.prompt_registry import build_grounding_discovery_prompts
        tasks=build_grounding_discovery_prompts(parse_command_text('/news refresh'),{},[])
        self.assertEqual(len(tasks),13)
        self.assertTrue(all(task['news_quality_policy']['terms'] for task in tasks))
        self.assertTrue(next(t for t in tasks if t['label']=='個股公告 / MOPS')['news_quality_policy']['require_official'])

    def test_unknown_provider_dates_use_article_url_dates_not_an_arbitrary_body_date(self):
        from datetime import date
        from research_center.deepseek_search_service import _news_task_quality
        rows=[{'title':'台股供應鏈','url':f'https://{host}/{path}', 'snippet':'台股供應鏈有新訂單但需求風險仍在。'*12}
              for host,path in [('www.ctee.com.tw','news/20261102700039-430502'),
                                ('www.ctee.com.tw','news/20261102700040-430502'),
                                ('technews.tw','2026/11/02/power'),('technews.tw','2026/11/02/chip')]]
        quality=_news_task_quality(rows,{'terms':['供應鏈']},date(2026,11,2))
        self.assertTrue(quality['passed'])
        self.assertEqual(quality['url_dated_articles'],4)
        for row in rows:
            row['url']=row['url'].replace('20261102','undated').replace('2026/11/02','undated')
            row['snippet']='報導回顧2026-11-02事件，並非刊登日期。'*12
        self.assertFalse(_news_task_quality(rows,{'terms':['供應鏈']},date(2026,11,2))['passed'])


class LosslessSegmentMergeTests(unittest.TestCase):
    def test_official_radar_uses_full_packet_above_legacy_limit(self):
        from datetime import date
        from stock_ai_bot.monitoring import radar_service as radar
        candidates=[radar.RadarCandidate(code='9999',name='測試公司')]
        original='完整核心與反證'*20000
        with patch.object(radar,'_build_ai_comment_prompt',return_value=original) as render:
            jobs=radar._build_ai_comment_prompt_jobs(candidates,date(2026,11,2),max_chars=600000)
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['prompt'],original)
        self.assertEqual(jobs[0]['profile'],'normal')
        self.assertEqual(render.call_count,1)

    def test_adjacent_small_plans_keep_every_original_payload(self):
        from research_center.command_parser import parse_command_text
        from research_center.segmented_analysis_service import _merge_small_segment_plans, _build_segment_prompt
        request=parse_command_text('/research 2330 --deep')
        plans=[{'label':'core','title':'核心','payload':{'facts':'x'*125000}},
               {'label':'risk','title':'反證','payload':{'counter_evidence':'訂單存在取消風險','source_ids':['S001']}},
               {'label':'sources','title':'來源','payload':{'sources':'y'*120000}}]
        merged=_merge_small_segment_plans(request,{},plans,target_chars=140000)
        self.assertEqual(len(merged),2)
        self.assertEqual(merged[0]['payload']['sections'][0]['payload'],plans[0]['payload'])
        self.assertEqual(merged[0]['payload']['sections'][1]['payload'],plans[1]['payload'])
        self.assertEqual(merged[1],plans[2])
        self.assertTrue(all(len(_build_segment_prompt(request,{},p,[]))<160000 for p in merged))


class ClosedSessionPriceCacheTests(unittest.TestCase):
    def test_only_certified_same_day_evening_bars_can_bypass_short_ttl(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from stock_ai_bot.scanning.stock_scanner import _closed_session_price_symbols
        metric={'price_date':'2026-11-02','change_pct':1,'volume_ratio':2}
        payload={'metrics':{'9999.TW':metric,'8888.TW':{**metric,'price_date':'2026-10-30'}},
                 'closed_session':{'date':'2026-11-02','symbols':['9999.TW','8888.TW']}}
        evening=datetime(2026,11,2,21,30,tzinfo=ZoneInfo('Asia/Taipei'))
        self.assertEqual(_closed_session_price_symbols(payload,evening),{'9999.TW'})
        self.assertEqual(_closed_session_price_symbols(payload,evening.replace(hour=15)),set())
        self.assertEqual(_closed_session_price_symbols(payload,evening.replace(day=3)),set())
        self.assertEqual(_closed_session_price_symbols(payload,evening.replace(day=3,hour=8)),{'9999.TW'})
        self.assertEqual(_closed_session_price_symbols(payload,evening.replace(day=3,hour=9)),set())
        self.assertEqual(_closed_session_price_symbols({'metrics':payload['metrics']},evening),set())

    def test_expired_evening_cache_fetches_only_gaps_and_force_still_fetches_all(self):
        from datetime import datetime
        from types import SimpleNamespace
        from stock_ai_bot.scanning import stock_scanner as scanner
        class FixedNow(datetime):
            @classmethod
            def now(cls,tz=None):
                return cls(2026,11,2,21,30,tzinfo=tz)
        metric={'price_date':'2026-11-02','change_pct':1,'volume_ratio':2}
        payload={'metrics':{'9999.TW':metric},
                 'closed_session':{'date':'2026-11-02','symbols':['9999.TW']}}
        universe=[SimpleNamespace(symbol=symbol) for symbol in ['9999.TW','8888.TW']]
        for force,expected in [(False,['8888.TW']),(True,['9999.TW','8888.TW'])]:
            with self.subTest(force=force), patch.object(scanner,'datetime',FixedNow), \
                    patch.object(scanner,'PRICE_CACHE_PATH') as path, \
                    patch.object(scanner,'_read_json',return_value=payload), \
                    patch.object(scanner,'_is_fresh',return_value=False), \
                    patch.object(scanner,'_download_chunk_price_metrics',return_value={s:metric for s in expected}) as fetch, \
                    patch.object(scanner,'_write_json') as write,patch.object(scanner.time,'sleep'):
                path.exists.return_value=True
                result=scanner.load_price_metrics(universe,force_refresh=force)
                self.assertEqual(set(result),{'9999.TW','8888.TW'})
                self.assertEqual(fetch.call_args.args[0],expected)
                self.assertEqual(set(write.call_args.args[1]['closed_session']['symbols']),set(result))

    def test_single_stock_refresh_preserves_other_stocks_without_renewing_their_age(self):
        from datetime import datetime
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo
        from stock_ai_bot.scanning import stock_scanner as scanner
        class FixedNow(datetime):
            @classmethod
            def now(cls,tz=None):
                return cls(2026,11,2,21,30,tzinfo=tz)
        metric={'price_date':'2026-11-02','change_pct':1,'volume_ratio':2}
        old_time='2026-11-02T19:00:00+08:00'
        payload={'metrics':{s:metric for s in ['9999.TW','8888.TW']},
                 'metric_fetched_at':{s:old_time for s in ['9999.TW','8888.TW']}}
        with patch.object(scanner,'datetime',FixedNow),patch.object(scanner,'PRICE_CACHE_PATH') as path, \
                patch.object(scanner,'_read_json',return_value=payload),patch.object(scanner,'_is_fresh',return_value=True), \
                patch.object(scanner,'_download_chunk_price_metrics',return_value={'9999.TW':metric}) as fetch, \
                patch.object(scanner,'_write_json') as write,patch.object(scanner.time,'sleep'):
            path.exists.return_value=True
            scanner.load_price_metrics([SimpleNamespace(symbol='9999.TW')])
            fetch.assert_called_once_with(['9999.TW'])
            stored=write.call_args.args[1]
            self.assertEqual(set(stored['metrics']),{'9999.TW','8888.TW'})
            self.assertEqual(stored['metric_fetched_at']['8888.TW'],old_time)
            self.assertEqual(scanner._fresh_price_cache_symbols(stored,FixedNow.now(ZoneInfo('Asia/Taipei')),1800,True),{'9999.TW'})


class ThemeCandidateRelevanceTests(unittest.TestCase):
    def test_theme_flow_keeps_reference_companies_separate_from_observed_inflow(self):
        from research_center import data_services as service
        from research_center.command_parser import parse_command_text
        reference={'matched_companies':[{'code':'9999','name':'關聯公司'}],
                   'matched_universe':[{'code':'9999'}], 'topic_context':{'matched_topics':['電源']},
                   'supply_chain_profile':{'keywords':['電源']}}
        hooks=['attach_date_aware_context','attach_news_context','attach_news_events',
               'attach_shared_entity_context','attach_shared_event_context','attach_company_knowledge_autofill',
               'attach_data_inventory','attach_feature_pack','attach_data_gap_summary','refill_data_gaps',
               'attach_unified_evidence_pack','attach_convergence_fields']
        with ExitStack() as stack:
            for name in hooks: stack.enter_context(patch.object(service,name))
            stack.enter_context(patch.object(service,'_official_sources',return_value=[]))
            stack.enter_context(patch.object(service,'build_theme_flow_data',return_value={
                'theme':{'theme_id':'power'},'related_stocks':[],'related_stock_count':0,
                'layer_market_validation':{'status':'missing'}}))
            stack.enter_context(patch.object(service,'collect_theme_data',return_value=reference))
            data,_=service.collect_structured_data(parse_command_text('/theme_flow 電源'))
        self.assertEqual(data['related_stock_count'],0)
        self.assertEqual(data['related_stocks'],[])
        self.assertEqual(data['matched_companies'],reference['matched_companies'])
        self.assertIn('不等於當日強勢股',data['notes'][0])

    def test_supply_chain_match_precedes_generic_industry_before_top_limit(self):
        from research_center import data_services
        from research_center.models import CommandRequest
        from stock_ai_bot.scanning.stock_scanner import StockUniverseEntry
        universe=[StockUniverseEntry('1111','1111.TW','TWSE','一般零件','電子零組件'),
                  StockUniverseEntry('9999','9999.TW','TWSE','直接供應商','電子零組件')]
        with patch.object(data_services,'load_stock_universe',return_value=universe), \
             patch.object(data_services,'_theme_profile',return_value={'keywords':['電源'],'industries':['電子零組件']}), \
             patch.object(data_services,'build_theme_topic_context',return_value={'related_supply_chain_nodes':[{'company_code':'9999'}]}), \
             patch.object(data_services,'load_company_knowledge',return_value={'companies':{}}):
            result=data_services.collect_theme_data(CommandRequest(command='theme',raw_text='/theme 電源',target='電源',top=1))
        self.assertEqual([row['code'] for row in result['matched_companies']],['9999'])
        self.assertIn('仍須本次證據驗證',result['matched_companies'][0]['theme_match_reason'])

    def test_no_match_is_not_replaced_with_arbitrary_stock(self):
        from research_center import data_services
        from research_center.models import CommandRequest
        from stock_ai_bot.scanning.stock_scanner import StockUniverseEntry
        with patch.object(data_services,'load_stock_universe',return_value=[StockUniverseEntry('1111','1111.TW','TWSE','水泥公司','水泥')]), \
             patch.object(data_services,'_theme_profile',return_value={'keywords':['電源'],'industries':[]}), \
             patch.object(data_services,'build_theme_topic_context',return_value={}), \
             patch.object(data_services,'load_company_knowledge',return_value={'companies':{}}):
            result=data_services.collect_theme_data(CommandRequest(command='theme',raw_text='/theme 電源',target='電源'))
        self.assertEqual(result['matched_companies'],[])
        self.assertIn('不以股票宇宙前幾檔',result['notes'][-1])


class TopicInputCacheTests(unittest.TestCase):
    def key(self,data,sources=None,model='deepseek-flash',prompts=None):
        from research_center.topic_maintain_service import _topic_workflow_cache_key
        return _topic_workflow_cache_key(TopicChangeMode.UPDATE,'2026-11-02',model,data,sources or [],prompts or ['規則'])

    def test_run_timestamp_changes_do_not_force_new_paid_review(self):
        self.assertEqual(self.key({'generated_at':'10:00','facts':['訂單未定']}),self.key({'generated_at':'11:00','facts':['訂單未定']}))

    def test_evidence_publication_model_prompt_and_library_changes_invalidate_cache(self):
        base=self.key({'facts':['訂單未定']},[{'url':'https://example.test','published_date':'2026-11-01'}])
        self.assertNotEqual(base,self.key({'facts':['訂單已取消']},[{'url':'https://example.test','published_date':'2026-11-01'}]))
        self.assertNotEqual(base,self.key({'facts':['訂單未定']},[{'url':'https://example.test','published_date':'2026-11-02'}]))
        self.assertNotEqual(self.key({}),self.key({},model='other-model'))
        self.assertNotEqual(self.key({}),self.key({},prompts=['新規則']))
