from __future__ import annotations

import unittest
from pathlib import Path

from research_center.command_parser import parse_command_text
from research_center.models import SourceItem
from research_center.report_builder import build_report_json, render_html, write_report_artifacts
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

    def test_report_sources_render_provider_in_markdown_and_html(self):
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
            self.assertIn('tavily_extract', written_md)
            self.assertIn('extract_depth=basic', written_md)
            self.assertIn('tavily_extract', html)
            self.assertIn('extract_depth=basic', html)
        finally:
            safe_remove_test_cache("report_schema/test_report_sources_render")


if __name__ == '__main__':
    unittest.main()
