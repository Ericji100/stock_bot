from datetime import date
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from research_center.ai_input_audit import _quality_checks
from research_center.models import CommandRequest
from research_center.report_confidence_service import build_report_confidence
from stock_ai_bot.data_sources.official_daily_quotes import parse_official_closes, merge_official_closes


class ReportDataQualityTests(unittest.TestCase):
    def test_official_close_uses_actual_day_lots_and_no_future_bar(self):
        rows = [{'Date':'1151102', 'Code':'2330', 'ClosingPrice':'100', 'Change':'2', 'TradeVolume':'1,000', 'TradeValue':'100,000'}]
        metric = parse_official_closes(rows, 'TWSE', date(2026, 11, 2))['2330.TW']
        self.assertEqual(metric['price_date'], '2026-11-02')
        self.assertEqual(metric['volume'], 1)
        self.assertEqual(metric['previous_close'], 98)
        self.assertFalse(parse_official_closes(rows, 'TWSE', date(2026, 11, 1)))

    def test_new_close_cannot_relabel_old_moving_average_or_volume_ratio(self):
        old = {'2330.TW': {'price_date':'2026-10-30', 'volume_ratio':8, 'above_ma20':True}}
        new = {'2330.TW': {'price_date':'2026-11-02', 'price':100}}
        merged = merge_official_closes(old, new)['2330.TW']
        self.assertNotIn('volume_ratio', merged)
        self.assertTrue(merged['history_refresh_required'])
        self.assertEqual(merged['historical_metric_snapshot'], old['2330.TW'])
        self.assertNotIn('history_refresh_required', old['2330.TW'])

    def test_many_sources_do_not_hide_low_quantitative_breadth_or_invalid_tdcc(self):
        request = CommandRequest(command='theme_flow', raw_text='/theme_flow AI', report_date=date(2026, 11, 3))
        checks = _quality_checks(request, {'data_quality': {'quantitative_stock_count':1}, 'tdcc_data':{'status':'invalid'}}, [])
        audit = {'source_coverage': {'official_sources':100, 'media_sources':100, 'risk_or_counter_sources':100,
                                    'total_sources':300, 'dated_sources':300},
                 'structured_coverage':{'coverage_ratio':1}, 'quality_checks': checks}
        result = build_report_confidence(request, ai_input_audit=audit)
        self.assertLessEqual(result['confidence_score'], 49)
        self.assertIn('不是預測成功率', result['policy'])

    def test_duplicate_bodies_not_counted_as_independent_full_text(self):
        text = '這是可追溯的來源完整正文。' * 50
        sources = [{'source': {'url':f'https://{host}/article', 'snippet':text, 'fetch_status':'success'}}
                   for host in ['a.test', 'b.test']]
        checks = _quality_checks(CommandRequest(command='theme', raw_text='/theme AI'), {}, sources)
        self.assertEqual(checks['body_evidence_count'], 1)
        self.assertEqual(checks['independent_domain_count'], 2)

    def test_quarterly_ytd_and_unknown_currency_are_not_invented_as_ttm(self):
        from research_center.mops_sources import financial_period_audit
        audit = financial_period_audit([{'Quarter':'2026Q3', 'EPS':29.43}], date(2026, 11, 3))
        self.assertEqual(audit['periods'][0]['eps_period_basis'], 'unverified')
        self.assertIsNone(audit['periods'][0]['currency'])
        self.assertTrue(audit['warnings'])
        self.assertNotIn('ttm_eps', audit)

    def test_flow_includes_related_weak_and_missing_stocks_not_first_universe_sample(self):
        from research_center import theme_radar_service as service
        from tests.test_theme_radar_feature import _prepared_library, _stock
        stocks = [_stock('0001','無關','水泥'), _stock('2330','台積電','半導體'), _stock('6415','矽力','電源')]
        metrics = {'2330.TW': {'price':100, 'price_date':'2026-11-02', 'change_pct':-3}}
        with patch.object(service, 'load_stock_universe', return_value=stocks), \
             patch.object(service, '_load_topic_library', side_effect=_prepared_library), \
             patch.object(service, '_load_cached_market_movers_snapshot', return_value={}), \
             patch.object(service, 'load_company_knowledge', return_value={'companies':{}}), \
             patch.object(service, 'load_price_metrics', return_value=metrics) as load, \
             patch.object(service, '_build_news_theme_stats', return_value=[]):
            result = service.build_theme_flow_data('AI伺服器', date(2026, 5, 22))
        self.assertEqual({entry.code for entry in load.call_args.args[0]}, {'2330','6415'})
        self.assertEqual({row['code'] for row in result['related_stocks']}, {'2330','6415'})
        self.assertEqual(result['related_stocks'][0]['change_pct'], -3)
        self.assertEqual(result['data_quality']['quantitative_stock_count'], 1)
        self.assertIn('6415', result['data_quality']['quantitative_missing_codes'])

    def test_verified_topic_company_needs_its_own_matching_body_quote(self):
        from tests.test_topic_citation_audit import TopicCitationAuditTests
        from research_center.topic_citation_audit import audit_topic_citations
        pack = TopicCitationAuditTests().pack()
        quote = '台積電2330公司表示這是公開可核對的供應鏈產品原文證據。'
        relation = {'company_code':'2330','company_name':'台積電','verification_status':'verified',
                    'evidence':[{'url':'https://example.test/article','supporting_quote':quote}]}
        pack.actions[0].company_relations = [relation]
        data = {'discovery_sources':[{'url':'https://example.test/article','content':quote}]}
        self.assertTrue(audit_topic_citations(pack,data)['passed'])
        relation['evidence'][0]['supporting_quote'] = '台積電2330這是一段模型捏造而原文完全沒有出現過的文字。'
        audit = audit_topic_citations(pack,data)
        self.assertFalse(audit['passed'])
        self.assertIn('verified_company_without_body_quote', audit['counts'])

    def test_old_invalid_tdcc_cache_is_not_adopted_as_a_scoring_input(self):
        from stock_ai_bot.data_sources.tdcc_distribution import validate_tdcc_snapshot
        original = {'status':'covered', 'large_holder_pct':184.38, 'retail_holder_pct':3}
        validated = validate_tdcc_snapshot(original)
        self.assertEqual(validated['status'], 'invalid')
        self.assertIsNone(validated['large_holder_pct'])
        self.assertEqual(validated['raw_invalid_snapshot'], original)
        self.assertEqual(original['large_holder_pct'], 184.38)

    def test_previous_close_does_not_trigger_staleness_cap_before_next_report_day(self):
        request = CommandRequest(command='theme', raw_text='/theme AI', report_date=date(2026,11,3))
        sources = [{'source':{'url':f'https://{host}/article','snippet':host*200,'fetch_status':'success'}}
                   for host in ['a.test','b.test']]
        checks = _quality_checks(request, {'data_quality':{'price_date_audit':{'dominant_price_date':'2026-11-02'}}}, sources)
        self.assertEqual(checks['confidence_cap'],100)

    def test_expired_market_snapshot_attempts_refresh_before_stale_fallback(self):
        from research_center import market_movers_service as service
        cached = {'data_quality':{'price_date_audit':{'date_counts':{'2026-10-30':100}}}}
        with patch.object(service,'_read_snapshot',return_value=cached), \
             patch.object(service,'_snapshot_is_fresh',return_value=False), \
             patch.object(service,'load_stock_universe',return_value=[]), \
             patch.object(service,'_safe_price_metrics',return_value={}) as load:
            result = service.build_market_movers(date(2026,11,3))
        load.assert_called_once()
        self.assertTrue(result['stale_snapshot_used'])

    def test_full_official_html_body_reaches_sources_without_a_2000_character_cut(self):
        from research_center.web_fetch_enrichment import _enrich_sources_with_web_fetch
        from research_center.models import SourceItem
        from research_center.web_fetch_service import WebFetchResult
        text = '官方季度財報完整段落與附註。' * 400
        url = 'https://company.test/financial/results.html'
        fetched = WebFetchResult(url=url, title='官方財報', content=text)
        result = SimpleNamespace(results=[fetched], diagnostics={})
        sources = [SourceItem('S1','官方財報',url,'Level 1')]
        data = {}
        with patch('research_center.web_fetch_enrichment.WebFetchService') as factory:
            factory.return_value.fetch_many.return_value = result
            _enrich_sources_with_web_fetch(CommandRequest(command='research', raw_text='/research 2330'), sources, data)
        self.assertEqual(sources[0].snippet,text)
        self.assertEqual(data['web_fetched_sources'][0]['content'],text)
        self.assertEqual(data['financial_period_audit']['official_body_urls'],[url])

    def test_product_matching_cannot_match_ai_inside_a_json_field_name(self):
        from research_center.knowledge_base import find_theme_company_candidates
        entry = SimpleNamespace(code='TEST',name='無關公司',symbol='TEST.TW',industry='水泥')
        result = find_theme_company_candidates([entry], 'AI', {'keywords':['AI']}, [], {'companies':{}})
        self.assertFalse(result)

    def test_deferred_news_status_reaches_ai_context_without_faking_publication_date(self):
        from research_center.news_models import NewsItem
        from research_center.date_aware_context import _news_item_to_context
        from research_center.news_formatters import _impact_label
        item = NewsItem(title='台股公告', created_at='2026-11-03T10:00:00', tags=['local_rule_fallback'])
        context = _news_item_to_context(item,date(2026,11,3),['台股'])
        self.assertEqual(context['classification_status'],'pending_ai')
        self.assertIsNone(context['days_before_analysis_date'])
        self.assertIn('待 AI 複核',_impact_label(item))
        self.assertEqual(item.to_dict()['classification_status'],'pending_ai')

    def test_provider_content_provenance_is_required_before_using_a_search_text_quote(self):
        from tests.test_topic_citation_audit import TopicCitationAuditTests
        from research_center.topic_citation_audit import audit_topic_citations
        quote = '這是搜尋服務確實回傳的原文段落，不是模型自己寫的摘要。'
        pack = TopicCitationAuditTests().pack(supporting_quote=quote)
        source = {'url':'https://example.test/article', 'snippet':quote}
        data = {'discovery_sources':[source]}
        self.assertFalse(audit_topic_citations(pack,data)['passed'])
        source['found_by'] = ['source_text:provided_content']
        self.assertTrue(audit_topic_citations(pack,data)['passed'])

    def test_unsupported_company_claim_is_preserved_as_candidate_not_auto_promoted(self):
        from tests.test_topic_citation_audit import TopicCitationAuditTests
        from research_center.topic_citation_audit import quarantine_unproven_company_claims
        pack = TopicCitationAuditTests().pack()
        claim = {'company_code':'1513','company_name':'公司','role':'假說角色',
                 'verification_status':'verified','evidence':[{'url':'https://example.test/article'}]}
        pack.actions[0].supply_chain_nodes=[claim]
        self.assertEqual(quarantine_unproven_company_claims(pack,{}),1)
        self.assertEqual(claim['verification_status'],'candidate')
        self.assertEqual(claim['validation_original_status'],'verified')
        self.assertEqual(claim['role'],'假說角色')
        self.assertTrue(claim['evidence'])

    def test_topic_source_conversion_preserves_body_provenance(self):
        from research_center.topic_maintain_service import _source_item_to_dict
        from research_center.models import SourceItem
        source = SourceItem('S1','來源','https://example.test/article','Level 1',
                            found_by=['source_text:provided_content'])
        self.assertEqual(_source_item_to_dict(source)['found_by'],source.found_by)

    def test_nonexistent_topic_update_is_blocked_before_backup_or_writes(self):
        from tests.test_topic_citation_audit import TopicCitationAuditTests
        from research_center import topic_apply_service as service
        from research_center.topic_models import TopicActionType
        pack = TopicCitationAuditTests().pack()
        pack.actions[0].action_type = TopicActionType.UPDATE_THEME
        with patch.object(service, 'load_change_pack', return_value=pack), \
             patch.object(service, 'load_topic_profiles', return_value=[]), \
             patch.object(service, 'backup_topic_files') as backup:
            result = service.confirm_change_pack(pack.change_id)
        self.assertFalse(result.success)
        self.assertIn('更新目標不存在', result.errors[0])
        backup.assert_not_called()

    def test_topic_target_audit_handles_create_then_update_without_remapping(self):
        import copy
        from tests.test_topic_citation_audit import TopicCitationAuditTests
        from research_center.topic_apply_service import validate_action_targets
        from research_center.topic_models import TopicActionType
        pack = TopicCitationAuditTests().pack()
        first = pack.actions[0]
        first.action_type = TopicActionType.CREATE_THEME
        second = copy.deepcopy(first)
        second.action_type = TopicActionType.UPDATE_THEME
        pack.actions.append(second)
        self.assertTrue(validate_action_targets(pack, [])['passed'])
        second.theme_id = 'nonexistent'
        self.assertFalse(validate_action_targets(pack, [])['passed'])

    def test_flow_row_keeps_historical_indicator_provenance(self):
        from research_center.theme_radar_service import _stock_row
        old = {'price_date':'2026-10-30', 'volume_ratio':8}
        metric = merge_official_closes({'T.TW':old}, {'T.TW':{'price_date':'2026-11-02','price':100}})
        row = _stock_row(SimpleNamespace(code='T',symbol='T.TW'),metric)
        self.assertTrue(row['history_refresh_required'])
        self.assertEqual(row['history_metrics_as_of'],'2026-10-30')
        self.assertEqual(row['historical_metric_snapshot'],old)
        self.assertIsNone(row['volume_ratio'])

    def test_explicit_topic_update_target_is_resolved_without_guessing_or_dropping_nodes(self):
        from tests.test_topic_citation_audit import TopicCitationAuditTests
        from research_center.topic_apply_service import resolve_action_targets, validate_action_targets
        from research_center.topic_models import TopicActionType
        pack = TopicCitationAuditTests().pack()
        action = pack.actions[0]
        action.action_type = TopicActionType.UPDATE_THEME
        original = action.theme_id
        action.target_theme_id = 'existing_power'
        action.supply_chain_nodes = [{'theme_id':original,'company_code':'2330','role':'原始角色'}]
        action.company_relations = [{'theme_id':original,'company_code':'2330'},
                                    {'theme_id':'explicit_other','company_code':'2308'}]
        profiles = [{'theme_id':'existing_power','theme_name':'既有電源'}]
        self.assertEqual(resolve_action_targets(pack,profiles),1)
        self.assertEqual(action.theme_id,'existing_power')
        self.assertEqual(action.extra['original_candidate_theme_id'],original)
        self.assertEqual(action.supply_chain_nodes[0]['theme_id'],'existing_power')
        self.assertEqual(action.supply_chain_nodes[0]['role'],'原始角色')
        self.assertEqual(action.company_relations[0]['theme_id'],'existing_power')
        self.assertEqual(action.company_relations[1]['theme_id'],'explicit_other')
        self.assertTrue(validate_action_targets(pack,profiles)['passed'])
        action.company_relations[0]['theme_id']=original
        self.assertEqual(resolve_action_targets(pack,profiles),0)
        self.assertEqual(action.company_relations[0]['theme_id'],'existing_power')

    def test_topic_missing_counter_evidence_is_disclosed_not_fabricated(self):
        from tests.test_topic_citation_audit import TopicCitationAuditTests
        from research_center.topic_quality import normalize_change_pack_quality
        pack=TopicCitationAuditTests().pack()
        normalize_change_pack_quality(pack)
        self.assertEqual(pack.actions[0].counter_evidence,[])
        self.assertTrue(any('未列出可核對的獨立反證' in item for item in pack.actions[0].missing_data))
        self.assertFalse(any('hypothesis_only' in text for text in pack.warnings))

    def test_price_volume_proxy_cannot_become_high_confidence_just_from_source_count(self):
        request = CommandRequest(command='theme_flow',raw_text='/theme_flow AI')
        checks = _quality_checks(request, {'data_quality':{
            'quantitative_stock_count':11, 'flow_evidence_basis':'price_volume_proxy'}}, [
                {'source':{'url':f'https://{domain}/article','content':domain*500,'fetch_status':'success'}}
                for domain in ('one.test','two.test')])
        self.assertEqual(checks['confidence_cap'],69)
        self.assertTrue(any('量價代理' in text for text in checks['warnings']))
