"""Real filesystem/database operations in isolated fixtures, without paid HTTP."""
from contextlib import ExitStack
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from research_center import topic_repository as repository
from research_center.topic_apply_service import confirm_change_pack, reject_change_pack
from research_center.topic_models import TopicChangePack, TopicChangeMode, TopicChangeStatus, TopicChangeAction, TopicActionType


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
