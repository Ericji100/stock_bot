from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from research_center.command_parser import parse_command_text
from research_center.config import ResearchCenterConfig
from research_center.database import ResearchDatabase
from research_center.date_guard import filter_sources_for_report_date
from research_center.data_services import _theme_profile, _value_rerating_score
from research_center.knowledge_base import enrich_company_rows, theme_knowledge_summary
from research_center.macro_indicators import _fear_greed_zone
from research_center.official_connectors import parse_taifex_vix_html, parse_twse_institutional_json
from research_center.value_validation import build_value_cross_validation
from research_center.mops_sources import financial_detail_snapshot
from research_center.models import CommandRequest, SourceItem
from research_center.report_builder import fallback_markdown, write_report_artifacts
from research_center.source_rank import rank_source


class CommandParserTests(unittest.TestCase):
    def test_research_deep_date(self):
        request = parse_command_text('/research 6217 --date 2026-01-07 --deep', user_id='u1')
        self.assertEqual(request.command, 'research')
        self.assertEqual(request.target, '6217')
        self.assertEqual(request.mode, 'deep')
        self.assertEqual(request.report_date, date(2026, 1, 7))

    def test_macro_scope(self):
        request = parse_command_text('/macro 台股 AI')
        self.assertEqual(request.market_scope, '台股')
        self.assertEqual(request.theme_scope, 'AI')
        self.assertEqual(request.region_scope, '台灣')

    def test_conflict_source_only_score(self):
        with self.assertRaises(ValueError):
            parse_command_text('/research 2330 --source-only --score')

    def test_top_not_allowed_for_research(self):
        with self.assertRaises(ValueError):
            parse_command_text('/research 2330 --top 10')


class SourceRankTests(unittest.TestCase):
    def test_official_source_is_level_1(self):
        self.assertEqual(rank_source('https://mops.twse.com.tw/server-java/t05st10'), 'Level 1')

    def test_forum_source_is_level_4(self):
        self.assertEqual(rank_source('https://www.ptt.cc/bbs/Stock/index.html'), 'Level 4')


class ReportAndDatabaseTests(unittest.TestCase):
    def test_write_report_and_db(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db = ResearchDatabase(root / 'stock_research.db')
            request = CommandRequest(command='research', raw_text='/research 2330 --source-only', target='2330', target_type='stock', source_only=True, mode='source_only')
            sources = [SourceItem(source_id='S001', title='TWSE', url='https://www.twse.com.tw/', source_level='Level 1')]
            markdown = fallback_markdown(request, {'stock': {'code': '2330'}}, sources)
            artifacts, report_json = write_report_artifacts(root / 'reports', request, markdown, 'summary', sources, False, None)
            db.save_report(request, artifacts, 'summary', sources, False, None)
            self.assertTrue(artifacts.markdown_path.exists())
            self.assertTrue(artifacts.html_path.exists())
            self.assertTrue(artifacts.json_path.exists())
            row = db.latest_report(target='2330')
            self.assertIsNotNone(row)
            self.assertEqual(row['report_id'], artifacts.report_id)
            self.assertEqual(report_json['report_type'], 'research')

    def test_database_saves_events(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = ResearchDatabase(Path(tmp) / 'stock_research.db')
            db.save_events([
                {
                    'event_type': 'mops',
                    'target': '2330',
                    'title': 'material event',
                    'source_url': 'https://mops.twse.com.tw/',
                    'source_level': 'Level 1',
                    'published_date': '2026-01-01',
                    'payload': {'ok': True},
                }
            ])
            rows = db.query_events_before('2330', '2026-01-07')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['event_type'], 'mops')


class DateGuardTests(unittest.TestCase):
    def test_date_guard_drops_undated_and_future_sources(self):
        sources = [
            SourceItem(source_id='S001', title='old', url='https://example.com/old', source_level='Level 3', published_date='2026-01-01'),
            SourceItem(source_id='S002', title='future', url='https://example.com/future', source_level='Level 3', published_date='2026-01-08'),
            SourceItem(source_id='S003', title='unknown', url='https://example.com/unknown', source_level='Level 3'),
        ]
        kept, dropped = filter_sources_for_report_date(sources, date(2026, 1, 7))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].title, 'old')
        self.assertEqual(len(dropped), 2)


class ThemeAndValueScanTests(unittest.TestCase):
    def test_theme_profile_known_theme(self):
        profile = _theme_profile('AI伺服器')
        self.assertIn('supply_chain', profile)
        self.assertIn('AI', profile['keywords'])

    def test_value_rerating_score_has_labels_and_components(self):
        score = _value_rerating_score('半導體業', 80, 2500, 35)
        self.assertGreater(score['score'], 50)
        self.assertIn('old_market_label', score)
        self.assertIn('new_market_label', score)
        self.assertIn('revenue_turnaround', score['components'])

class KnowledgeAndValidationTests(unittest.TestCase):
    def test_company_knowledge_enrichment_marks_covered_and_missing(self):
        rows = [{"code": "2330", "name": "TSMC"}, {"code": "9999", "name": "Missing"}]
        enriched = enrich_company_rows(rows, {"companies": {"2330": {"product_lines": ["晶圓代工"], "customers": ["AI 客戶"]}}})
        self.assertEqual(enriched[0]["company_knowledge"]["status"], "covered")
        self.assertEqual(enriched[1]["company_knowledge"]["status"], "missing")
        summary = theme_knowledge_summary(enriched)
        self.assertEqual(summary["covered_companies"], 1)

    def test_value_cross_validation_scores_missing_evidence_conservatively(self):
        row = {"latest_monthly_revenue": 100, "revenue_yoy": 12, "company_knowledge": {"customers": [], "product_lines": []}}
        validation = build_value_cross_validation(row)
        self.assertLess(validation["verification_score"], 50)
        self.assertTrue(validation["risk_flags"])

    def test_fear_greed_zone(self):
        self.assertEqual(_fear_greed_zone(80), "greed")
        self.assertEqual(_fear_greed_zone(20), "fear")


class OfficialConnectorTests(unittest.TestCase):
    def test_parse_taifex_vix_html(self):
        html = """
        <table><tr><th>交易日期</th><th>臺指選擇權波動率指數</th></tr>
        <tr><td>2026/01/05</td><td>18.25</td></tr>
        <tr><td>2026/01/08</td><td>20.00</td></tr></table>
        """
        rows = parse_taifex_vix_html(html, date(2026, 1, 7))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 18.25)

    def test_parse_twse_institutional_json(self):
        payload = {
            "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
            "data": [["外資及陸資", "1,000", "500", "500"]],
        }
        rows = parse_twse_institutional_json(payload)
        self.assertEqual(rows[0]["net_amount"], 500)

    def test_financial_detail_snapshot(self):
        snapshot = financial_detail_snapshot([{"Quarter": "2025Q4", "EPS": 3.1, "gross_margin": 52.0}])
        self.assertEqual(snapshot["status"], "covered")
        self.assertGreater(snapshot["score_points"], 0)

if __name__ == '__main__':
    unittest.main()
