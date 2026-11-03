import inspect
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from research_center.models import CommandRequest, SourceItem
from research_center.reader_quality_rules import reader_quality_rules
from research_center.report_quality_service import review_reader_content
from research_center.report_numeric_validation import validate_valuation_tables


class ReaderQualityTests(unittest.TestCase):
    def test_successful_news_reclassification_updates_existing_database_row(self):
        from research_center.news_models import NewsItem
        from research_center.news_repository import NewsRepository
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        import sqlite3
        directory = ensure_test_cache_dir('reader_quality/news_reclassification')
        try:
            repo = NewsRepository(directory/'news.db')
            original = NewsItem(id='old',title='金控淨值排名',url='https://example.com/news',category='ai_semiconductor',
                                full_text='相同原文',summary='舊分類',tags=['ai_classified'])
            repo.save_many([original])
            revised = NewsItem(id='new',title=original.title,url=original.url,category='company_news',
                               full_text=original.full_text,summary='金控季度淨值，非AI消息',tags=['ai_classified'])
            self.assertEqual(repo.save_many([revised]),(0,1))
            fallback = NewsItem(id='fallback',title=original.title,url=original.url,category='other',
                                summary='本地保底',tags=['local_rule_fallback'])
            repo.save_many([fallback])
            with sqlite3.connect(repo.db_path) as conn:
                row = conn.execute('SELECT category,summary FROM news_articles WHERE url=?',(original.url,)).fetchone()
            self.assertEqual(row,('company_news','金控季度淨值，非AI消息'))
        finally:
            safe_remove_test_cache(directory)

    def test_news_id_binding_and_financial_ratio_category(self):
        from research_center.news_models import NewsItem
        from research_center.news_service import _classification_payload, _apply_classification_meta, _has_macro_policy_headline
        from research_center.news_categories import normalize_news_category
        item = NewsItem(id='stable-id', title='AI封測 Q3毛利率衝45%', url='https://example.com/123')
        self.assertEqual(_classification_payload([item], 80)[0]['id'], 'stable-id')
        self.assertFalse(_has_macro_policy_headline(item))
        updated = _apply_classification_meta([item], {'stable-id':{'category':'AI / 半導體','summary':'主事件'}})
        self.assertEqual(updated[0].category, 'ai_semiconductor')
        self.assertEqual(updated[0].summary, '主事件')
        self.assertEqual(normalize_news_category('個股利空'), 'company_news')
        self.assertEqual(normalize_news_category('台股重大新聞'), 'market_focus')
        single = NewsItem(id='profit',title='義隆電旺季不旺 稅前淨利衰退',url='https://example.com/profit',
                          full_text='本業衰退。推薦：題材輪動與美元匯率。')
        self.assertEqual(_apply_classification_meta([single],{'profit':{'category':'個股利空'}})[0].category,'company_news')

    def test_topic_candidate_capacity_split_keeps_every_source(self):
        from research_center.topic_pipeline_service import _candidate_capacity_prompts
        from research_center.deepseek_service import DeepSeekService
        sources = [{'source_id':f'S{i}', 'url':f'https://example.com/{i}', 'snippet':str(i)+'證據'*65000} for i in range(4)]
        data = {'discovery_sources':sources}
        prompts = _candidate_capacity_prompts(data, {}, '{discovery_sources_json}',
                    lambda template, variables:variables['discovery_sources_json'])
        self.assertGreater(len(prompts), 1)
        for source in sources:
            self.assertTrue(any(source['snippet'] in prompt and source['url'] in prompt for prompt in prompts))
        self.assertTrue(all(DeepSeekService(None).prompt_capacity(p,output_tokens=32768)['fits'] for p in prompts))

    def test_topic_split_candidates_merge_evidence_without_duplicating_theme(self):
        from unittest.mock import patch
        from research_center.topic_pipeline_service import _extract_candidates
        replies = [{'candidates':[{'theme_id':'power','theme_name':'電力','source_refs':[{'url':'https://example.com/1'}]}]},
                   {'candidates':[{'theme_id':'power','theme_name':'電力','source_refs':[{'url':'https://example.com/2'}]}]}]
        with patch('research_center.topic_pipeline_service._candidate_capacity_prompts',return_value=['part one','part two']):
            rows = _extract_candidates(prompt_variables={},load_prompt=lambda name:'template',render_prompt=lambda *args:'prompt',
                      call_ai_json=lambda *args:replies.pop(0),stage_logs=[],emit=lambda *args:None,full_evidence=True,structured_data={})
        self.assertEqual(len(rows),1)
        self.assertEqual(len(rows[0]['source_refs']),2)

    def test_final_market_synthesis_keeps_complete_existing_relation_packet(self):
        import json
        from research_center.segmented_analysis_service import _build_final_prompt, _final_local_summary
        for command, schema in [('theme_radar','theme_radar_relation_payload_v1'),
                                ('sector_strength','sector_strength_relation_payload_v1')]:
            with self.subTest(command=command):
                payload = {'schema_version': schema, 'stock_index': [{'code':str(i), 'evidence':'完整證據'} for i in range(104)],
                           'theme_rankings': [{'theme_name':str(i), 'candidate_stock_codes':['103']} for i in range(20)],
                           'sector_rankings': [{'sector':str(i)} for i in range(20)],
                           'subsector_rankings': [{'subsector':str(i)} for i in range(23)]}
                data = {'high_model_input_package': {'command':command, 'command_specific_data':{'payload':payload}}}
                summary = _final_local_summary(data, complete_core=True)
                self.assertEqual(summary['complete_core_packet'], payload)
                prompt = _build_final_prompt(CommandRequest(command, '/'+command), data, [], [], complete_segment_notes=True)
                sent = json.loads(prompt.split('Local core summary:\n')[1].split('\nSegment analysis notes:')[0])
                self.assertEqual(sent['complete_core_packet'], payload)
                self.assertNotIn('omitted_count', prompt)

    def test_relevant_source_selection_never_reassigns_citation_ids(self):
        from research_center.source_rank import select_theme_sources_for_prompt
        from research_center.search_source_normalizer import validate_source_identity_map
        sources = [SourceItem('S001', '其他新聞', 'https://example.com/other', 'Level 3'),
                   SourceItem('S015', 'AI 電源公司官方證據', 'https://example.com/power', 'Level 1')]
        selected, _ = select_theme_sources_for_prompt(sources, theme='AI電源', keywords=['電源'], max_sources=1)
        self.assertEqual(selected[0].source_id, 'S015')
        self.assertTrue(validate_source_identity_map(sources, selected)['passed'])
        conflicting = [SourceItem('S001', selected[0].title, selected[0].url, selected[0].source_level)]
        self.assertFalse(validate_source_identity_map(sources, conflicting)['passed'])

    def test_known_but_wrong_source_identity_cannot_pass_artifact_qa(self):
        from research_center.report_builder import write_report_artifacts
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        directory = ensure_test_cache_dir('reader_quality/source_identity')
        request = CommandRequest('research', '/research 2330', target='2330')
        source = SourceItem('S001', '媒體新聞', 'https://example.com/news', 'Level 3')
        data = {'high_model_input_package': {'complete_source_index': {'sources': [
                {'source_id':'S001', 'url':'https://example.com/official'}]}}}
        try:
            _, result = write_report_artifacts(directory, request, '# 報告\n公司已公告 [S001]',
                                              '摘要', [source], True, None, data)
            self.assertFalse(result['metadata']['source_identity_validation']['passed'])
            self.assertFalse(result['metadata']['qa_validation']['passed'])
            self.assertEqual(result['metadata']['ai_status'], 'partial_success')
            self.assertTrue(any('網址不一致' in error for error in result['metadata']['reader_content_review']['errors']))
        finally:
            safe_remove_test_cache(directory)

    def test_optional_missing_rankings_do_not_become_fake_report_gaps(self):
        from research_center.segmented_analysis_service import _final_local_summary
        for command in ('theme', 'theme_flow', 'macro'):
            with self.subTest(command=command):
                data = {'high_model_input_package': {'command': command}}
                result = _final_local_summary(data)
                self.assertEqual(result['command_role'], command)
                self.assertNotIn('sector_rankings', result)
                data['sector_rankings'] = [{'name': '實際提供的產業'}]
                self.assertEqual(_final_local_summary(data)['sector_rankings']['total_count'], 1)

    def test_usage_audit_does_not_mix_another_concurrent_case(self):
        import json
        from contextlib import contextmanager
        from tools.deepseek_live_validation import input_and_usage_audit
        class Connection:
            def execute(self, query):
                if 'FROM usage' in query:
                    rows = [('mine', 'chat/completions', 'success', '.01', json.dumps({'run_id': 'case1'})),
                            ('other', 'chat/completions', 'pending', None, json.dumps({'run_id': 'case2'}))]
                else:
                    rows = [('activity1', 'search', 1, json.dumps({'run_id': 'case1'})),
                            ('activity2', 'search', 0, json.dumps({'run_id': 'case2'}))]
                return SimpleNamespace(fetchall=lambda: rows)
        @contextmanager
        def database():
            yield Connection()
        result = input_and_usage_audit(SimpleNamespace(_db=database), set(), set(), {}, run_id='case1')
        self.assertEqual(result['paid_attempts'], 1)
        self.assertEqual(result['unknown_fee_count'], 0)
        self.assertEqual(result['estimated_usd'], .01)
        self.assertEqual(result['cache_hit_operations'], 1)

    def test_one_month_allowance_expires_without_restarting_account(self):
        from research_center.ai_budget_service import MonthlyBudget, BudgetBlocked
        budget = MonthlyBudget('__no_files_written__', manual_allowances={'2026-11': 2}, pacing=False)
        self.assertEqual(budget._limits(datetime(2026, 11, 3)), (Decimal(12), Decimal(4)))
        self.assertEqual(budget._limits(datetime(2026, 12, 1)), (Decimal(10), Decimal(2)))
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [(0, '', 'success', '2.5', '2.5')]
        budget._check(conn, Decimal('.1'), False, '', datetime(2026, 11, 3))
        with self.assertRaises(BudgetBlocked):
            budget._check(conn, Decimal('.1'), False, '', datetime(2026, 12, 1))
        self.assertEqual(budget.background, Decimal(7))
        self.assertEqual(budget.reserve, Decimal(1))

    def test_test_guard_does_not_double_a_configured_allowance(self):
        from tools.deepseek_live_validation import apply_test_allowance
        budget = SimpleNamespace(total=Decimal(10), manual=Decimal(2), _check=Mock(),
                                 manual_allowances={'2026-11': Decimal(2)})
        apply_test_allowance(budget, 2, {'month':'2026-11', 'used_or_reserved':'3'})
        self.assertEqual(budget.total, Decimal(10))
        self.assertEqual(budget.manual, Decimal(2))

    def test_invalid_one_month_allowances_are_rejected(self):
        from research_center.ai_budget_service import MonthlyBudget
        for value in ({'2026-13': 2}, {'2026-11': -1}, {'2026-11': 'NaN'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MonthlyBudget('__no_files_written__', manual_allowances=value)

    def test_required_financial_text_survives_source_order_and_final_synthesis(self):
        from research_center.ai_workflow_service import _source_excerpts
        from research_center.segmented_analysis_service import _build_segment_prompt, _build_final_prompt
        fillers = [SourceItem(f'S{i:03d}', '背景', f'https://example.com/{i}', 'Level 3', snippet='背景摘要')
                   for i in range(1, 31)]
        body = '已提供的官方新聞稿摘錄。' + 'x'*2400 + '毛利率66.4%，營業利益率58.7%。'
        core = SourceItem('S050', '最新季度財報', 'https://example.com/earnings', 'Level 3',
                          snippet=body, provider_detail='最新官方季度財報')
        rows = _source_excerpts([*fillers, core], limit=2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]['snippet'], body)
        self.assertEqual(rows[-1]['source_level'], 'Level 3')
        self.assertIn('not_full_statements', rows[-1]['content_scope'])
        for name in ('research', 'value_scan', 'theme', 'macro', 'theme_flow', 'theme_radar', 'sector_strength'):
            with self.subTest(command=name):
                request = CommandRequest(name, '/'+name)
                package = {'command': name, 'required_original_excerpts': rows}
                prompt = _build_final_prompt(request, {'high_model_input_package': package}, [], [core])
                self.assertIn('毛利率66.4%', prompt)
        prompt = _build_segment_prompt(CommandRequest('research', '/research'), {},
                    {'title': '原文', 'payload': {'required_original_excerpts': rows}}, [])
        self.assertIn('毛利率66.4%', prompt)

    def test_plain_pe_and_implied_price_are_checked(self):
        text = '| 情境 | EPS | PE | 精確計算 | 隱含價格 |\n|---|---|---|---|---|\n| 中性 | 100 | 20 | 100×20 | 2000 |'
        self.assertEqual(validate_valuation_tables(text)['checked_rows'], 1)
        self.assertFalse(validate_valuation_tables(text.replace('| 2000 |', '| 2200 |'))['passed'])

    def test_value_scan_keeps_local_matrix_without_fabricated_ai_analysis(self):
        from research_center.report_builder import _append_value_scan_candidate_analysis
        request = CommandRequest('value_scan', '/value_scan 2330', target='2330')
        body = '## 逐檔候選分析\n2330 的 AI 推論與風險。'
        data = {'ai_candidates': [{'code': '2330', 'name': '台積電'}],
                'ai_candidate_evidence_pack': [{'code': '2330', 'financial_detail': {'status': 'missing'}}]}
        output = _append_value_scan_candidate_analysis(request, body, data, include_analysis=False)
        self.assertTrue(output.startswith(body))
        self.assertIn('資料完整度矩陣', output)
        self.assertNotIn('市場正在交易什麼故事', output)
        self.assertNotIn('完整候選股逐檔重估分析', output)

    def test_extra_allowance_reuses_baseline_and_never_resets_ledger(self):
        from tools.deepseek_live_validation import apply_test_allowance
        from research_center.ai_budget_service import BudgetBlocked
        baseline = {'month': '2026-11', 'used_or_reserved': '3'}
        budget = SimpleNamespace(total=Decimal(10), manual=Decimal(2), _check=Mock())
        receipt = apply_test_allowance(budget, 2, baseline)
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [('ok', '4.99', '4.99')]
        budget._check(conn, Decimal('.01'), False, 'test', datetime(2026, 11, 3))
        with self.assertRaises(BudgetBlocked):
            budget._check(conn, Decimal('.02'), False, 'test', datetime(2026, 11, 3))
        with self.assertRaises(BudgetBlocked):
            budget._check(conn, Decimal(0), False, 'test', datetime(2026, 12, 1))
        self.assertEqual(receipt['baseline'], baseline)
        self.assertEqual(budget.total, Decimal(12))
        self.assertEqual(budget.manual, Decimal(4))
        self.assertEqual(conn.execute.call_args.args[0].split()[0], 'SELECT')

    def test_test_allowance_does_not_charge_production_background_against_manual_grant(self):
        from tools.deepseek_live_validation import apply_test_allowance
        from research_center.ai_budget_service import BudgetBlocked
        original=Mock()
        budget=SimpleNamespace(total=Decimal(10),manual=Decimal(2),_check=original)
        baseline={'month':'2026-11','used_or_reserved':'3','manual_used':'2'}
        apply_test_allowance(budget,2,baseline)
        conn=Mock()
        conn.execute.return_value.fetchall.return_value=[('success','3.88','3.88')]
        budget._check(conn,Decimal('.10'),False,'test',datetime(2026,11,3))
        self.assertIn('background=0',conn.execute.call_args.args[0])
        original.assert_called_once()
        with self.assertRaises(BudgetBlocked):
            budget._check(conn,Decimal('.13'),False,'test',datetime(2026,11,3))
        self.assertEqual(baseline['manual_used'],'2')

    def test_all_report_commands_load_same_policy(self):
        from research_center.prompt_registry import _rules_for_request
        for name in ('research', 'value_scan', 'macro', 'theme', 'theme_flow', 'theme_radar', 'sector_strength'):
            with self.subTest(command=name):
                self.assertIn(reader_quality_rules(), _rules_for_request(CommandRequest(name, '/'+name)))

    def test_special_workflows_and_segments_are_wired(self):
        from research_center import segmented_analysis_service as segment, topic_maintain_service as topic, news_service as news
        from stock_ai_bot.monitoring.radar_service import _build_ai_comment_prompt
        self.assertIn(reader_quality_rules(), topic._load_prompt('topic_detail_expand'))
        for func in (segment._build_segment_prompt, segment._build_final_prompt,
                     news._batch_classify_news, _build_ai_comment_prompt):
            self.assertIn('reader_quality_rules(', inspect.getsource(func))
        self.assertIn('不改本地分數', reader_quality_rules('radar'))
        self.assertLess(len(reader_quality_rules('radar')), 300)

    def test_unknown_citations_do_not_pass(self):
        source = SourceItem('S001', '公告', 'https://example.com', 'L1')
        check = review_reader_content('營收增加 [S999]', [source])
        self.assertFalse(check['mechanical_checks_passed'])
        self.assertEqual(check['unknown_source_ids'], ['S999'])
        self.assertEqual(check['semantic_review_status'], 'pending')

    def test_existing_citation_is_not_semantic_certification(self):
        source = SourceItem('S001', '公告', 'https://example.com', 'L1')
        check = review_reader_content('任意結論 [S001]', [source])
        self.assertTrue(check['mechanical_checks_passed'])
        self.assertEqual(check['semantic_review_status'], 'pending')

    def test_uncited_body_is_flagged_before_generated_appendix(self):
        check = review_reader_content('營收增加', [SourceItem('S001', '公告', 'https://example.com', 'L1')])
        self.assertTrue(check['warnings'])

    def test_horizontal_matrix_is_checked(self):
        text = '| 項目 | 保守 | 中性 |\n|---|---|---|\n| EPS | 10 | 12 |\n| 本益比 | 15–20 | 20 |\n| 合理股價 | 150–200 | 240 |'
        result = validate_valuation_tables(text)
        self.assertEqual(result['checked_rows'], 2)
        self.assertTrue(result['passed'])
        self.assertFalse(validate_valuation_tables(text.replace('240', '260'))['passed'])

    def test_pe_multiplier_column_matrix_and_plain_valuation_label(self):
        text = '| EPS 情境 | EPS | ×12.81 倍 | ×27.01 倍 |\n|---|---|---|---|\n| 年化 | 100 | 1281 | 2701 |'
        self.assertEqual(validate_valuation_tables(text)['checked_rows'], 2)
        self.assertFalse(validate_valuation_tables(text.replace('2701', '3000'))['passed'])
        text = '| 情境 | EPS | 本益比 | 估值 |\n|---|---|---|---|\n| 中性 | 100 | 20 | 2000 |'
        self.assertEqual(validate_valuation_tables(text)['checked_rows'], 1)

    def test_negative_eps_not_silently_treated_as_positive(self):
        text = '| 情境 | EPS | 本益比 | 合理股價 |\n|---|---|---|---|\n| 虧損 | -10 | 20 | 200 |'
        self.assertFalse(validate_valuation_tables(text)['passed'])

    def test_positive_ascii_range_still_works(self):
        text = '| 情境 | EPS | 本益比 | 合理股價 |\n|---|---|---|---|\n| 基準 | 10-12 | 20 | 200-240 |'
        self.assertTrue(validate_valuation_tables(text)['passed'])

    def test_numeric_repair_cannot_drop_risk_references(self):
        from research_center.report_numeric_validation import validate_valuation_repair
        original = '## 反證\n風險依據 [S002] https://example.com/risk\n'
        self.assertFalse(validate_valuation_repair(original, '## 反證\n風險依據待查\n')['passed'])
        self.assertTrue(validate_valuation_repair(original, original)['passed'])

    def test_numeric_repair_accepts_explicit_corrected_score_scale(self):
        from research_center.report_numeric_validation import validate_valuation_repair
        original = '評分公式：總分＝傳導（滿分 30）＋證據（滿分 25）＋量化（滿分 20）＋時程（滿分 15）－反證扣分（滿分 10）。滿分 100。'
        repaired = original.replace('反證扣分（滿分 10）。滿分 100',
                                    '反證扣分（扣分項，0–10 分）。正向構面合計上限為 90 分')
        result = validate_valuation_repair(original, repaired)
        self.assertTrue(result['passed'], result['errors'])
        self.assertEqual(result['score_formulas']['checked_formulas'], 1)
        self.assertFalse(validate_valuation_repair(original, '評分已修正。')['passed'])
        self.assertFalse(validate_valuation_repair(original, repaired.replace('上限為 90', '上限為 100'))['passed'])
