from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_center.command_parser import parse_command_text
from research_center.database import ResearchDatabase
from research_center.gemini_service import build_prompt
from research_center.models import CommandRequest, ReportArtifacts, SourceItem
from research_center.prompt_registry import prompt_metadata


class PromptContractTests(unittest.TestCase):
    def test_research_score_prompt_loads_original_scoring_drafts(self):
        request = parse_command_text('/research 2330 --score')
        prompt = build_prompt(
            request,
            structured_data={'stock': {'code': '2330', 'name': 'TSMC'}, 'strategy_summary': []},
            source_list=[SourceItem(source_id='S001', title='MOPS', url='https://mops.twse.com.tw/', source_level='Level 1')],
        )
        self.assertIn('股票量化評分標準原稿', prompt)
        self.assertIn('股票標籤重估模型原稿', prompt)
        self.assertIn('/research', prompt)
        self.assertIn('資料來源', prompt)
        self.assertIn('不得捏造', prompt)
        metadata = prompt_metadata(request)
        self.assertEqual(metadata['template'], 'research_score.md')
        self.assertIn('股票量化評分標準.md', metadata['scoring_files'])

    def test_value_scan_prompt_uses_top_10_default_and_rerating_rules(self):
        request = parse_command_text('/value_scan')
        prompt = build_prompt(
            request,
            structured_data={'candidate_pool': '精選選股', 'candidates': []},
            source_list=[],
        )
        self.assertIn('前 10 名', prompt)
        self.assertIn('股票標籤重估模型原稿', prompt)
        self.assertIn('財務硬指標', prompt)
        self.assertIn('不得只因新聞熱門就給高分', prompt)

    def test_macro_and_report_defaults_follow_latest_spec(self):
        macro = parse_command_text('/macro')
        self.assertEqual(macro.market_scope, '全球')
        self.assertEqual(macro.region_scope, 'global')

        recent = parse_command_text('/report')
        self.assertEqual(recent.target, '__recent__')

        latest = parse_command_text('/report latest')
        self.assertEqual(latest.target, 'latest')

        latest_stock = parse_command_text('/report 6217 latest')
        self.assertEqual(latest_stock.target, '6217')

        latest_theme = parse_command_text('/report theme AI伺服器 latest')
        self.assertEqual(latest_theme.target, 'theme AI伺服器')


    def test_output_format_flags_are_parsed(self):
        request = parse_command_text('/research 2330 --no-html --no-json')
        self.assertEqual(request.output_formats, ('md',))
        with self.assertRaises(Exception):
            parse_command_text('/research 2330 --no-html --no-json --no-md')

    def test_database_latest_report_can_filter_by_date(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = ResearchDatabase(Path(tmp) / 'stock_research.db')
            sources: list[SourceItem] = []
            old_request = CommandRequest(command='research', raw_text='/research 2330 --date 2026-01-01', target='2330', report_date=__import__('datetime').date(2026, 1, 1))
            new_request = CommandRequest(command='research', raw_text='/research 2330 --date 2026-01-02', target='2330', report_date=__import__('datetime').date(2026, 1, 2))
            old_artifacts = ReportArtifacts('old', 'research', Path('old.md'), Path('old.html'), Path('old.json'), Path('old.sources.json'))
            new_artifacts = ReportArtifacts('new', 'research', Path('new.md'), Path('new.html'), Path('new.json'), Path('new.sources.json'))
            db.save_report(old_request, old_artifacts, 'old summary', sources, False, None)
            db.save_report(new_request, new_artifacts, 'new summary', sources, False, None)
            row = db.latest_report(target='2330', report_type='research', report_date='2026-01-01')
            self.assertIsNotNone(row)
            self.assertEqual(row['report_id'], 'old')


if __name__ == '__main__':
    unittest.main()

