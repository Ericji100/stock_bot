from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

from research_center.command_parser import parse_command_text
from research_center.ai_workflow_service import build_ai_workflow_coverage
from research_center.models import SourceItem
from research_center.report_builder import build_report_json, fallback_markdown, render_html, write_report_artifacts
from research_center.report_validator import validate_report
from research_center.scoring_engine import build_local_scores


class ReportSchemaAndScoringTests(unittest.TestCase):
    def test_research_local_scores_fill_schema_scores(self):
        request = parse_command_text('/research 2330 --score')
        structured_data = {
            'revenue_data': [{'YoY': 30}, {'YoY': 28}, {'YoY': 26}, {'YoY': 25}, {'YoY': 24}, {'YoY': 27}],
            'financial_data': [{'EPS': 1.2, 'operating_margin': 16}, {'EPS': 1.4, 'operating_margin': 17}, {'EPS': 1.5, 'operating_margin': 18}, {'EPS': 1.6, 'operating_margin': 19}],
            'technical_data': {'above_ma21': True, 'avg_volume_20d': 2000},
            'institutional_data': [{'NetBuy': 100}, {'NetBuy': 50}],
            'margin_data': [{'MarginBalance': 1000}, {'MarginBalance': 980}],
        }
        structured_data['local_scoring'] = {'policy': 'test', 'scores': build_local_scores(request, structured_data)}
        report_json = build_report_json(request, '# 2330 個股研究報告\n\n## 資料來源列表\n- [S001] TWSE', 'summary', [SourceItem('S001', 'TWSE', 'https://www.twse.com.tw/', 'Level 1')], True, None, structured_data)
        self.assertGreaterEqual(len(report_json['scores']), 6)
        self.assertIn('score_reason', report_json['scores'][0])

    def test_report_validator_records_missing_sections_and_source_refs(self):
        request = parse_command_text('/value_scan')
        markdown = '# 價值重估掃描報告\n\n## 價值重估總結\n- 測試 [S001]\n\n## 資料來源列表\n- [S001] TWSE'
        sources = [SourceItem('S001', 'TWSE', 'https://www.twse.com.tw/', 'Level 1')]
        report_json = build_report_json(request, markdown, 'summary', sources, False, None, {'local_scoring': {'scores': [{'score_name': '測試', 'score_value': 50, 'score_max': 100, 'score_reason': 'ok', 'deduction_reason': 'none'}]}})
        qa = validate_report(markdown, request, sources, report_json)
        self.assertIn('S001', ''.join(qa['source_refs']))
        self.assertIn('候選', qa['missing_sections'])

    def test_write_report_artifacts_respects_output_formats(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("report_schema/test_write_report_artifacts")
        try:
            request = parse_command_text('/research 2330 --no-html --no-json')
            artifacts, report_json = write_report_artifacts(
                tmp,
                request,
                '# 2330 個股研究報告\n\n## 資料來源列表\n- [S001] TWSE',
                'summary',
                [SourceItem('S001', 'TWSE', 'https://www.twse.com.tw/', 'Level 1')],
                False,
                None,
                {'local_scoring': {'policy': 'test', 'scores': []}},
            )
            self.assertTrue(artifacts.markdown_path.exists())
            self.assertFalse(artifacts.html_path.exists())
            self.assertFalse(artifacts.json_path.exists())
            self.assertTrue(artifacts.sources_path.exists())
            self.assertIn('qa_validation', report_json['metadata'])
        finally:
            safe_remove_test_cache("report_schema/test_write_report_artifacts")

    def test_write_report_artifacts_serializes_date_objects(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("report_schema/test_write_report_date_objects")
        try:
            request = parse_command_text('/research 2330')
            artifacts, report_json = write_report_artifacts(
                tmp,
                request,
                '# 2330 個股研究報告\n\n## 資料來源列表\n- [S001] TWSE',
                'summary',
                [SourceItem('S001', 'TWSE', 'https://www.twse.com.tw/', 'Level 1', published_date=date(2026, 5, 24))],
                False,
                None,
                {'local_scoring': {'policy': 'test', 'scores': []}, 'debug_date': date(2026, 5, 24)},
            )
            self.assertTrue(artifacts.json_path.exists())
            self.assertTrue(artifacts.sources_path.exists())
            self.assertIn('"published_date": "2026-05-24"', artifacts.sources_path.read_text(encoding="utf-8"))
            self.assertIn('"qa_validation"', artifacts.json_path.read_text(encoding="utf-8"))
        finally:
            safe_remove_test_cache("report_schema/test_write_report_date_objects")

    def test_write_report_artifacts_preserves_ai_workflow_coverage(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("report_schema/test_write_report_ai_workflow_coverage")
        try:
            request = parse_command_text('/research 2330')
            coverage = build_ai_workflow_coverage(
                command="research",
                local_data_package={"summary": "ok"},
                low_model_digest={"status": "success", "model": "MiniMax-M3", "facts": [{"finding": "test"}]},
                high_model_input_package={"input": "ok"},
                dedupe_strategy="evidence_summary_with_source_index",
                source_index=[{"source_id": "S001"}],
                input_audit={"mode": "test"},
                html_sections={"main": True},
                diagnostics={"prompt_chars": 1000},
            )
            artifacts, report_json = write_report_artifacts(
                tmp,
                request,
                '# 2330 research\n\n## 資料來源\n- [S001] TWSE',
                'summary',
                [SourceItem('S001', 'TWSE', 'https://www.twse.com.tw/', 'Level 1')],
                True,
                None,
                {
                    'analysis_model': 'Gemini',
                    'ai_workflow_coverage': coverage,
                    'local_scoring': {'policy': 'test', 'scores': []},
                },
            )

            self.assertTrue(artifacts.json_path.exists())
            self.assertEqual(report_json["metadata"]["ai_workflow_coverage"]["status"], "aligned")
            saved = json.loads(artifacts.json_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(saved["metadata"]["ai_workflow_coverage"]["schema_version"], "ai_workflow_coverage_v1")
            self.assertEqual(saved["metadata"]["ai_workflow_coverage"]["missing_capabilities"], [])
        finally:
            safe_remove_test_cache("report_schema/test_write_report_ai_workflow_coverage")



    def test_render_html_defaults_to_main_tab_and_separates_auxiliary_content(self):
        sources = [SourceItem('S001', 'TWSE', 'https://www.twse.com.tw/long/path', 'Level 1')]
        markdown = '# 測試報告\n\n## 摘要\n主內容 [S001]\n\n## 完整資料來源清單\n- [S001] TWSE\n\n## 規格檢查提醒\n- 測試提醒'
        report_json = build_report_json(parse_command_text('/research 2330'), markdown, 'summary', sources, True, None, {'analysis_model': 'Gemini'})
        report_json['metadata']['qa_validation'] = {'passed': False, 'warnings': ['測試提醒']}
        rendered = render_html(report_json, markdown)
        self.assertIn('id="tab-main" checked', rendered)
        self.assertIn('for="tab-sources"', rendered)
        self.assertIn('for="tab-qa"', rendered)
        self.assertIn('overflow-x: hidden', rendered)
        self.assertIn('overflow-wrap: anywhere', rendered)
        self.assertIn('source-card', rendered)

    def test_render_html_converts_markdown_tables_to_responsive_tables(self):
        sources = [SourceItem('S001', 'TWSE', 'https://www.twse.com.tw/', 'Level 1')]
        markdown = (
            '# 測試報告\n\n'
            '## 重點表格\n'
            '| 股票 | 分數 | 判斷 |\n'
            '|---|---:|---|\n'
            '| 2330 | 95 | 穩健 |\n'
            '| 2317 | 70 | 觀察 |\n'
            '\n## 完整資料來源清單\n- [S001] TWSE'
        )
        report_json = build_report_json(
            parse_command_text('/research 2330'),
            markdown,
            'summary',
            sources,
            True,
            None,
            {'analysis_model': 'Gemini', 'local_scoring': {'scores': []}},
        )
        rendered = render_html(report_json, markdown)

        self.assertIn('<table class="responsive-table">', rendered)
        self.assertIn('<thead>', rendered)
        self.assertIn('data-label="股票"', rendered)
        self.assertIn('td::before', rendered)
        self.assertIn('for="tab-quality"', rendered)
        self.assertIn('panel-quality', rendered)
        self.assertNotIn('| 股票 | 分數 | 判斷 |', rendered)

    def test_render_html_includes_required_data_gap_tab(self):
        sources = [
            SourceItem(
                'S001',
                '台灣證券交易所 VIX proxy',
                'https://www.twse.com.tw/',
                'Level 1',
                snippet='VIX 與市場風險資料',
            )
        ]
        structured_data = {
            'analysis_model': 'Gemini',
            'local_scoring': {'scores': []},
            'required_data_gap_summary': {
                'status': 'missing_required_data',
                'requirement_count': 2,
                'covered_count': 1,
                'missing_count': 1,
                'initial_missing_count': 2,
                'gap_fill_task_count': 1,
                'backfill_recommended': True,
                'covered': [
                    {
                        'field': 'global_risk_vix',
                        'label': '國際風險與 VIX 指數',
                        'tier': 'hard',
                        'matched_source_count': 1,
                        'matched_source_ids': ['S001'],
                    }
                ],
                'missing': [
                    {
                        'field': 'taiwan_derivatives',
                        'label': '台指選擇權與 Put/Call',
                        'tier': 'hard',
                        'backfill_queries': ['台指選擇權 Put Call Ratio TAIFEX'],
                    }
                ],
            },
            'required_data_gap_backfill_tasks': {
                'tasks': [
                    {
                        'label': 'required_gap:taiwan_derivatives',
                        'queries': ['台指選擇權 Put Call Ratio TAIFEX'],
                    }
                ]
            },
            'required_gap_minimax_discovery': {
                'status': 'ok',
                'source_count': 3,
                'diagnostics': {'error_reasons': []},
            },
        }
        markdown = '# 台股總經市場報告\n\n## 完整資料來源清單\n- [S001] TWSE'
        report_json = build_report_json(
            parse_command_text('/macro 台股'),
            markdown,
            'summary',
            sources,
            True,
            None,
            structured_data,
        )
        rendered = render_html(report_json, markdown)

        self.assertIn('id="tab-required-gap"', rendered)
        self.assertIn('for="tab-required-gap"', rendered)
        self.assertIn('panel-required-gap', rendered)
        self.assertIn('必備資料檢查', rendered)
        self.assertIn('國際風險與 VIX 指數', rendered)
        self.assertIn('台指選擇權與 Put/Call', rendered)
        self.assertIn('MiniMax MCP Search', rendered)

    def test_render_html_splits_bold_lead_sections_into_readable_blocks(self):
        sources = [SourceItem('S001', 'TWSE', 'https://www.twse.com.tw/', 'Level 1')]
        markdown = (
            '# 2330 research\n\n'
            '**營收表現：** 公司近期營收維持成長，主要來自高階製程需求。\n'
            '**法人籌碼：** 外資連續買超，投信持續加碼。\n'
            '**風險提醒：** 匯率與客戶庫存仍需追蹤。\n\n'
            '## 完整資料來源清單\n- [S001] TWSE'
        )
        report_json = build_report_json(
            parse_command_text('/research 2330'),
            markdown,
            'summary',
            sources,
            True,
            None,
            {'analysis_model': 'Gemini', 'local_scoring': {'scores': []}},
        )
        rendered = render_html(report_json, markdown)

        self.assertIn('class="report-subsection"', rendered)
        self.assertIn('class="report-subsection-title">營收表現</h4>', rendered)
        self.assertIn('class="report-subsection-title">法人籌碼</h4>', rendered)
        self.assertLess(rendered.index('營收表現'), rendered.index('法人籌碼'))

    def test_render_html_splits_long_paragraphs_for_readability(self):
        sources = [SourceItem('S001', 'TWSE', 'https://www.twse.com.tw/', 'Level 1')]
        long_text = (
            '第一句說明公司營收與產品組合變化。第二句說明法人籌碼與成交量變化。'
            '第三句說明產業趨勢與供應鏈線索。第四句說明毛利率與獲利風險。'
            '第五句說明後續追蹤指標與反證條件。第六句說明投資判斷仍需等待資料驗證。'
            '第七句補充客戶需求與庫存循環仍需交叉確認。第八句補充題材想像空間不能取代已驗證財務資料。'
            '第九句補充若後續營收與法人籌碼同步改善，才可提高重估信心。第十句補充若來源不足，報告必須保守標示資料缺口。'
        )
        markdown = f'# 2330 research\n\n{long_text}\n\n## 完整資料來源清單\n- [S001] TWSE'
        report_json = build_report_json(
            parse_command_text('/research 2330'),
            markdown,
            'summary',
            sources,
            True,
            None,
            {'analysis_model': 'Gemini', 'local_scoring': {'scores': []}},
        )
        rendered = render_html(report_json, markdown)

        self.assertGreaterEqual(rendered.count('class="readable-paragraph"'), 2)
        self.assertIn('line-height: 1.72', rendered)

    def test_report_sources_render_provider_in_html_and_concise_markdown(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("report_schema/test_report_sources_render")
        try:
            request = parse_command_text('/research 2330')
            sources = [
                SourceItem(
                    'S001',
                    'Tavily source',
                    'https://example.com/a',
                    'Level 3',
                    snippet='content',
                    provider='tavily_extract',
                    provider_detail='extract_depth=basic',
                )
            ]
            markdown = '# 2330 研究\n\n## 資料來源列表\n- [S001] Tavily source'
            artifacts, report_json = write_report_artifacts(
                tmp,
                request,
                markdown,
                'summary',
                sources,
                True,
                None,
                {'analysis_model': 'test', 'local_scoring': {'scores': []}},
            )
            written_md = artifacts.markdown_path.read_text(encoding='utf-8')
            html = artifacts.html_path.read_text(encoding='utf-8')
            self.assertIn('Tavily 網頁擷取', written_md)
            self.assertNotIn('extract_depth=basic', written_md)
            self.assertNotIn('content', written_md)
            self.assertIn('Tavily 網頁擷取', html)
            self.assertIn('extract depth：basic', html)
            self.assertNotIn('tavily_extract', html)
            self.assertNotIn('extract_depth=basic', html)
        finally:
            safe_remove_test_cache("report_schema/test_report_sources_render")

    def test_markdown_source_appendix_is_limited_but_sources_json_is_complete(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("report_schema/test_source_appendix_limit")
        try:
            request = parse_command_text('/theme AI電源')
            sources = [
                SourceItem(
                    f"S{i:03d}",
                    f"Source title {i}",
                    f"https://example.com/{i}",
                    "Level 3",
                    snippet="very long snippet should stay out of markdown",
                    provider="minimax_mcp_search",
                )
                for i in range(1, 46)
            ]
            artifacts, _report_json = write_report_artifacts(
                tmp,
                request,
                "# Theme\n\n## 資料來源列表\n- [S001] Source",
                "summary",
                sources,
                True,
                None,
                {'analysis_model': 'test', 'local_scoring': {'scores': []}},
            )
            written_md = artifacts.markdown_path.read_text(encoding='utf-8')
            written_sources = artifacts.sources_path.read_text(encoding='utf-8')
            self.assertIn("Markdown 僅列前 40 筆精簡來源", written_md)
            self.assertIn("S040", written_md)
            self.assertNotIn("S041", written_md)
            self.assertNotIn("very long snippet should stay out of markdown", written_md)
            self.assertIn('"source_id": "S045"', written_sources)
            self.assertIn("very long snippet should stay out of markdown", written_sources)
        finally:
            safe_remove_test_cache("report_schema/test_source_appendix_limit")

    def test_write_report_artifacts_sanitizes_model_markdown(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("report_schema/test_report_markdown_sanitize")
        try:
            request = parse_command_text('/theme_flow AI伺服器 --no-html --no-json')
            sources = [SourceItem('S001', 'Example', 'https://example.com/a', 'Level 2')]
            markdown = '```markdown\n# AI伺服器\n\n## 摘要\n引用 [S?] 與 [S001]\n```'
            artifacts, _report_json = write_report_artifacts(
                tmp,
                request,
                markdown,
                markdown,
                sources,
                True,
                None,
                {'analysis_model': 'test', 'local_scoring': {'scores': []}},
            )
            written_md = artifacts.markdown_path.read_text(encoding='utf-8')
            self.assertFalse(written_md.lstrip().startswith('```markdown'))
            self.assertNotIn('[S?]', written_md)
            self.assertIn('來源未對應', written_md)
            self.assertIn('# AI伺服器', written_md)
        finally:
            safe_remove_test_cache("report_schema/test_report_markdown_sanitize")

    def test_write_report_artifacts_removes_model_preface_and_adds_source_bridge(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("report_schema/test_report_preface_source_bridge")
        try:
            request = parse_command_text("/theme_radar --no-html --no-json")
            sources = [SourceItem("S001", "台股類股輪動", "https://example.com/tw", "Level 2")]
            markdown = "好的，我將根據您提供的結構化資料產出完整報告。\n\n# 市場題材雷達\n\n## 摘要\n台股 AI 供應鏈轉強。"

            artifacts, report_json = write_report_artifacts(
                tmp,
                request,
                markdown,
                markdown,
                sources,
                True,
                None,
                {"analysis_model": "test", "local_scoring": {"scores": []}},
            )

            written_md = artifacts.markdown_path.read_text(encoding="utf-8")
            self.assertNotIn("好的，我將", written_md)
            self.assertTrue(written_md.lstrip().startswith("# 市場題材雷達"))
            self.assertIn("來源引用補充", written_md)
            self.assertIn("[S001] 台股類股輪動", written_md)
            self.assertTrue(report_json["metadata"]["qa_validation"]["passed"])
        finally:
            safe_remove_test_cache("report_schema/test_report_preface_source_bridge")

    def test_report_validator_ignores_forbidden_words_in_source_appendix_titles(self):
        markdown = "# 類股強弱\n\n## 摘要\n台股類股轉強 [S001]\n\n## 完整資料來源清單\n| 來源 | 標題 |\n|---|---|\n| S001 | 五月必漲股？ |"
        request = parse_command_text("/sector_strength")
        sources = [SourceItem("S001", "五月必漲股？", "https://youtube.com/watch?v=x", "Level 3")]
        report_json = build_report_json(request, markdown, "summary", sources, True, None, {"local_scoring": {"scores": []}})

        from research_center.report_validator import validate_report

        qa = validate_report(markdown, request, sources, report_json)

        self.assertNotIn("必漲", qa["forbidden_hits"])

    def test_macro_validator_accepts_vix_as_volatility_section(self):
        request = parse_command_text("/macro")
        markdown = (
            "# 台股宏觀\n\n"
            "## 市場總覽\n內容 [S001]\n\n"
            "## 指數分析\n內容 [S001]\n\n"
            "## VIX 與市場風險\n內容 [S001]\n\n"
            "## 資金與籌碼\n內容 [S001]\n\n"
            "## 風險\n內容 [S001]\n\n"
            "## 資料來源\n- [S001] TWSE"
        )
        sources = [SourceItem("S001", "TWSE", "https://www.twse.com.tw/", "Level 1")]
        report_json = build_report_json(
            request,
            markdown,
            "summary",
            sources,
            True,
            None,
            {"analysis_model": "gemini-test", "local_scoring": {"scores": []}},
        )

        qa = validate_report(markdown, request, sources, report_json)

        self.assertNotIn("波動", qa["missing_sections"])

    def test_shared_data_layer_is_preserved_in_report_metadata_for_research_commands(self):
        structured_data = {
            "news_context": {
                "status": "partial",
                "usable_count": 1,
                "items": [{"title": "TSMC news", "url": "https://example.com/news"}],
            },
            "saved_news_context": {
                "usable_count": 1,
                "items": [{"title": "Saved news", "url": "https://example.com/saved"}],
            },
            "news_persistence_status": {"saved": 1, "skipped": 0},
            "feature_pack": {"scope": "test", "candidates": [{"code": "2330", "name": "台積電"}]},
            "data_coverage": {"status": "partial", "missing_fields": ["gross_margin"]},
            "local_scoring": {"policy": "test", "scores": []},
        }
        commands = [
            "/research 2330 --source-only",
            "/value_scan 2330 --deep",
            "/macro global",
            "/theme AI",
            "/theme_radar --days 7",
            "/theme_flow AI伺服器 --days 7",
            "/sector_strength --source radar",
        ]

        for raw_command in commands:
            with self.subTest(raw_command=raw_command):
                request = parse_command_text(raw_command)
                report_json = build_report_json(
                    request,
                    "# Test\n\n## 資料來源\n- [S001] Example",
                    "summary",
                    [SourceItem("S001", "Example", "https://example.com", "Level 3")],
                    True,
                    None,
                    structured_data,
                )
                metadata = report_json["metadata"]
                self.assertIn("shared_data_layer", metadata)
                self.assertIn("news_context", metadata)
                self.assertIn("saved_news_context", metadata)
                self.assertIn("news_persistence_status", metadata)
                self.assertIn("feature_pack", metadata)
                self.assertIn("data_coverage", metadata)
                self.assertEqual(metadata["news_context"]["usable_count"], 1)
                self.assertEqual(metadata["feature_pack"]["candidates"][0]["code"], "2330")
                self.assertEqual(metadata["data_coverage"]["missing_fields"], ["gross_margin"])

    def test_fallback_markdown_includes_shared_data_layer_summary(self):
        request = parse_command_text("/research 2330 --source-only")
        structured_data = {
            "news_context": {"usable_count": 1, "items": [{"title": "news"}]},
            "feature_pack": {"target": "2330"},
            "data_coverage": {"status": "partial"},
            "local_scoring": {"scores": []},
        }

        markdown = fallback_markdown(request, structured_data, [], reason="test")

        self.assertIn("共享資料層摘要", markdown)
        self.assertIn("news_context", markdown)
        self.assertIn("feature_pack", markdown)
        self.assertIn("data_coverage", markdown)

    def test_value_scan_fallback_markdown_includes_market_imagination_sections(self):
        request = parse_command_text("/value_scan 精選選股 --deep")
        structured_data = {
            "candidate_pool": "精選選股",
            "ai_candidates": [
                {
                    "code": "2330",
                    "name": "台積電",
                    "old_market_label": "晶圓代工",
                    "new_market_label": "AI 先進製程與封裝",
                    "rerating_score": 82,
                    "verification_score": 58,
                    "rerating_evidence": ["AI/HPC 需求支撐"],
                    "counter_evidence": ["營收占比仍需驗證"],
                    "missing_data": ["CoWoS 產能與客戶占比"],
                }
            ],
            "local_scoring": {"scores": []},
        }

        markdown = fallback_markdown(request, structured_data, [], reason="test")

        self.assertIn("市場推演摘要", markdown)
        self.assertIn("市場正在交易什麼故事", markdown)
        self.assertIn("早期蛛絲馬跡", markdown)
        self.assertIn("下一波可能發酵的催化劑", markdown)
        self.assertIn("如果要大漲，還缺什麼訊號", markdown)
        self.assertIn("反向驗證與失敗條件", markdown)
        self.assertIn("想像力結論", markdown)

    def test_sector_strength_metadata_keeps_market_movers_summary(self):
        request = parse_command_text("/sector_strength --date 2026-05-24")
        structured_data = {
            "command_role": "sector_strength",
            "report_date": "2026-05-24",
            "market_data_date": "2026-05-22",
            "report_generated_at": "2026-05-24T21:30:00",
            "source": "market",
            "sector_rankings": [
                {
                    "sector": "半導體業",
                    "sector_score": 100,
                    "strong_stock_count": 2,
                    "sector_strong_samples": [{"code": "2344", "name": "華邦電"}],
                    "representative_stocks": [],
                }
            ],
            "market_movers": {
                "market_data_date": "2026-05-22",
                "report_generated_at": "2026-05-24T21:29:00",
                "source_mode": "market",
                "top_gainers": [{"code": "2344", "name": "華邦電", "change_pct": 9.9}],
                "data_quality": {"missing_fields": []},
            },
            "local_scoring": {"scores": []},
        }

        report_json = build_report_json(
            request,
            "# Test\n\n## 資料來源列表\n- [S001] Example",
            "summary",
            [SourceItem("S001", "Example", "https://example.com", "Level 3")],
            True,
            None,
            structured_data,
        )

        metadata = report_json["metadata"]
        shared = metadata["shared_data_layer"]
        self.assertEqual(metadata["market_data_date"], "2026-05-22")
        self.assertEqual(metadata["report_generated_at"], "2026-05-24T21:30:00")
        self.assertEqual(shared["sector_strength"]["market_data_date"], "2026-05-22")
        self.assertEqual(shared["sector_strength"]["market_movers"]["top_gainers"][0]["code"], "2344")

    def test_value_scan_report_json_preserves_evidence_pack_and_completeness_matrix(self):
        request = parse_command_text("/value_scan 精選選股 --deep")
        structured_data = {
            "ai_candidates": [{"code": "2330", "name": "台積電"}],
            "ai_candidate_evidence_pack": [
                {
                    "code": "2330",
                    "name": "台積電",
                    "financial_detail": {"status": "covered"},
                    "gross_margin_cache": {"gross_margin": 50},
                    "chip_backup_summary": {"status": "covered"},
                    "valuation_data": {},
                    "tdcc_data": {},
                    "mops_documents": [{"title": "MOPS"}],
                    "source_events": [{"title": "event"}],
                    "company_knowledge": {"status": "covered"},
                    "missing_data_status": None,
                }
            ],
            "local_scoring": {"scores": []},
        }

        report_json = build_report_json(
            request,
            "# Test\n\n## 資料來源\n- [S001] Example",
            "summary",
            [SourceItem("S001", "Example", "https://mops.twse.com.tw/mops/web/t05st02", "Level 1")],
            True,
            None,
            structured_data,
        )

        snapshot = report_json["structured_data"]
        self.assertIn("ai_candidate_evidence_pack", snapshot)
        self.assertIn("data_completeness_matrix", snapshot)
        self.assertEqual(snapshot["data_completeness_matrix"][0]["stock"], "2330 台積電")
        self.assertIn("source_quality", report_json["metadata"])
        self.assertGreaterEqual(report_json["metadata"]["source_quality"]["items"][0]["source_quality_score"], 60)
        self.assertIn("report_quality", report_json["metadata"])
        self.assertEqual(report_json["metadata"]["report_schema_version"], "report_quality_v1")
        self.assertIn("evidence_pack", snapshot)
        self.assertIn("source_coverage_summary", snapshot)

    def test_value_scan_markdown_appends_data_completeness_matrix(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("report_schema/test_value_scan_completeness_matrix")
        try:
            request = parse_command_text("/value_scan 精選選股 --deep")
            structured_data = {
                "ai_candidates": [{"code": "2330", "name": "台積電"}],
                "ai_candidate_evidence_pack": [
                    {
                        "code": "2330",
                        "name": "台積電",
                        "financial_detail": {"status": "covered"},
                        "gross_margin_cache": {},
                        "chip_backup_summary": {"status": "no data"},
                        "valuation_data": {},
                        "tdcc_data": {},
                        "mops_documents": [],
                        "source_events": [],
                        "company_knowledge": {"status": "missing"},
                        "missing_data_status": ["gross_margin_cache", "chip_backup_data", "mops_documents", "source_events", "company_knowledge"],
                    }
                ],
                "local_scoring": {"scores": []},
            }
            artifacts, _ = write_report_artifacts(
                tmp,
                request,
                "# Test\n\n## 資料來源\n- [S001] Example",
                "summary",
                [SourceItem("S001", "Example", "https://example.com", "Level 3")],
                True,
                None,
                structured_data,
            )
            markdown = artifacts.markdown_path.read_text(encoding="utf-8")
            self.assertIn("資料完整度矩陣", markdown)
            self.assertIn("公司知識庫", markdown)
            self.assertIn("2330 台積電", markdown)
        finally:
            safe_remove_test_cache("report_schema/test_value_scan_completeness_matrix")

    def test_all_research_report_json_contains_unified_quality_layer(self):
        request = parse_command_text("/research 2330 --deep")
        structured_data = {
            "stock": {"code": "2330", "name": "TSMC"},
            "price_data": {"price": 900},
            "revenue_data": [{"YoY": 10}],
            "financial_data": [{"EPS": 10}],
            "local_rerating_snapshot": {"rerating_score": 70},
            "news_context": {"usable_count": 1},
            "feature_pack": {"scope": "single_stock"},
            "data_coverage": {"status": "partial"},
            "local_scoring": {"scores": []},
        }

        report_json = build_report_json(
            request,
            "# Test\n\n## Sources\n- [S001] Example",
            "summary",
            [SourceItem("S001", "Example", "https://www.twse.com.tw", "Level 1")],
            True,
            None,
            structured_data,
        )

        self.assertEqual(report_json["metadata"]["report_schema_version"], "report_quality_v1")
        self.assertIn("report_quality", report_json["metadata"])
        self.assertIn("evidence_pack", report_json["structured_data"])
        self.assertEqual(report_json["structured_data"]["evidence_pack"]["stock"]["code"], "2330")
        self.assertIn("data_completeness_matrix", report_json["structured_data"])
        self.assertIn("source_coverage_summary", report_json["structured_data"])

    def test_report_json_includes_shared_data_coordination_layers(self):
        request = parse_command_text("/research 2330 --deep")
        structured_data = {
            "stock": {"code": "2330", "name": "TSMC"},
            "data_gap_summary": {"schema_version": "data_gap_v1", "missing_fields": ["financial_data"]},
            "unified_evidence_pack": {"schema_version": "evidence_pack_v1", "items": []},
            "news_events": [{"event_type": "news", "title": "news"}],
            "news_event_summary": {"schema_version": "news_event_v1", "event_count": 1},
            "search_query_log": {"schema_version": "search_tasks_v1", "task_count": 1},
        }
        report_json = build_report_json(
            request,
            "# Test\n\n## Sources\n- [S001] Example",
            "summary",
            [SourceItem("S001", "Example", "https://www.twse.com.tw", "Level 1")],
            True,
            None,
            structured_data,
        )

        shared = report_json["metadata"]["shared_data_layer"]
        self.assertIn("data_gap_summary", shared)
        self.assertIn("unified_evidence_pack", shared)
        self.assertIn("news_events", shared)
        self.assertEqual(report_json["structured_data"]["data_gap_summary"]["schema_version"], "data_gap_v1")
        self.assertEqual(report_json["structured_data"]["unified_evidence_pack"]["schema_version"], "evidence_pack_v1")

    def test_report_json_preserves_tavily_discovery_diagnostics(self):
        request = parse_command_text("/value_scan 精選選股 --deep --top 1")
        structured_data = {
            "ai_candidates": [{"code": "2330", "name": "台積電"}],
            "ai_candidate_evidence_pack": [{"code": "2330", "name": "台積電"}],
            "search_query_log": {
                "schema_version": "search_tasks_v1",
                "task_count": 1,
                "providers": [{"provider": "tavily_search", "source_count": 1}],
            },
            "tavily_search_discovery": {
                "enabled": True,
                "provider": "tavily",
                "runs": [{"label": "官方公告與月營收", "status": "ok", "source_count": 1}],
            },
        }
        report_json = build_report_json(
            request,
            "# Test\n\n## Sources\n- [S001] Example",
            "summary",
            [SourceItem("S001", "Example", "https://example.com", "Level 2", provider="tavily_search")],
            False,
            "fallback",
            structured_data,
        )

        self.assertEqual(report_json["metadata"]["tavily_search_discovery"]["provider"], "tavily")
        self.assertEqual(report_json["metadata"]["tavily_search_discovery"]["runs"][0]["status"], "ok")
        self.assertEqual(report_json["metadata"]["shared_data_layer"]["search_query_log"]["providers"][0]["provider"], "tavily_search")

    def test_non_value_scan_markdown_appends_report_quality_summary(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("report_schema/test_research_quality_summary")
        try:
            request = parse_command_text("/research 2330 --deep")
            artifacts, _ = write_report_artifacts(
                tmp,
                request,
                "# Test\n\n## Sources\n- [S001] Example",
                "summary",
                [SourceItem("S001", "Example", "https://www.twse.com.tw", "Level 1")],
                True,
                None,
                {
                    "stock": {"code": "2330", "name": "TSMC"},
                    "price_data": {"price": 900},
                    "news_context": {"usable_count": 1},
                    "feature_pack": {"scope": "single_stock"},
                    "data_coverage": {"status": "partial"},
                    "local_scoring": {"scores": []},
                },
            )
            markdown = artifacts.markdown_path.read_text(encoding="utf-8")
            self.assertIn("報告資料完整度與來源品質", markdown)
            self.assertIn("資料覆蓋分數", markdown)
            self.assertIn("缺資料解讀規則", markdown)
            self.assertNotIn("Missing Data Policy", markdown)
        finally:
            safe_remove_test_cache("report_schema/test_research_quality_summary")


if __name__ == '__main__':
    unittest.main()
