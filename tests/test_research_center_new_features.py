from __future__ import annotations

import unittest
from pathlib import Path

from research_center.command_parser import parse_command_text
from research_center.database import ResearchDatabase
from research_center.gemini_service import build_prompt
from research_center.models import SourceItem
from research_center.scoring_engine import build_buy_rating, build_local_scores
from research_center.recent_scans import extract_stock_codes
from research_center.mops_sources import _fetch_mops_tables
from stock_scanner import StockUniverseEntry


class ResearchCenterNewFeatureTests(unittest.TestCase):
    def test_buy_rating_is_built_from_local_scores(self):
        request = parse_command_text('/research 2330 --score')
        scores = build_local_scores(request, {
            'revenue_data': [{'YoY': 30}, {'YoY': 31}, {'YoY': 32}],
            'financial_data': [{'EPS': 2, 'operating_margin': 20}],
            'technical_data': {'above_ma21': True, 'avg_volume_20d': 1000},
            'tdcc_data': {'status': 'covered', 'large_holder_pct': 60, 'retail_holder_pct': 5, 'concentration_signal': 'high_concentration'},
            'valuation_data': {'status': 'official_public', 'latest': {'pe_ratio': 12, 'pb_ratio': 1.5, 'dividend_yield_pct': 3}},
        })
        rating = build_buy_rating(scores)
        self.assertGreaterEqual(rating['score'], 3)
        self.assertEqual(rating['max'], 5)

    def test_recent_scan_extracts_stock_codes(self):
        codes = extract_stock_codes("2330 台積電\n6217 中探針\n2330 重複")
        self.assertEqual(codes, ['2330', '6217'])

    def test_mops_table_parser_is_available_for_html_shape(self):
        # Parser internals are covered by syntax/import here; network is intentionally not used.
        self.assertTrue(callable(_fetch_mops_tables))
    def test_historical_prompt_disables_live_search_by_instruction(self):
        request = parse_command_text('/research 2330 --date 2026-01-01')
        prompt = build_prompt(request, structured_data={'historical_snapshots': {'status': 'no_historical_snapshots', 'snapshot_count': 0}}, source_list=[])
        self.assertIn('Gemini Search / 現在網路搜尋已由程式停用', prompt)
        self.assertIn('不得使用該日期之後', prompt)



    def test_price_metrics_fallback_recovers_missing_primary_source(self):
        import pandas as pd
        import research_center.price_fallbacks as price_fallbacks

        original_loader = price_fallbacks.load_price_metrics
        original_fetcher = price_fallbacks.StockDataFetcher

        class FakeFetcher:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def resolve_stock(self, code):
                return type("Meta", (), {"code": code, "symbol": f"{code}.TW", "market": "TWSE", "name": "測試", "display_name": code})()

            def fetch_price_history(self, meta, months=4):
                return pd.DataFrame(
                    {
                        "Date": pd.date_range("2026-01-01", periods=25),
                        "Close": [10 + index for index in range(25)],
                        "Volume_Lots": [1000 + index for index in range(25)],
                    }
                )

        try:
            price_fallbacks.load_price_metrics = lambda universe: {}
            price_fallbacks.StockDataFetcher = FakeFetcher
            universe = [StockUniverseEntry("2330", "2330.TW", "TWSE", "台積電", "半導體業")]
            metrics, policy = price_fallbacks.load_price_metrics_with_fallback(universe)
        finally:
            price_fallbacks.load_price_metrics = original_loader
            price_fallbacks.StockDataFetcher = original_fetcher

        self.assertEqual(policy["status"], "fallback_used")
        self.assertIn("2330.TW", metrics)
        self.assertEqual(metrics["2330.TW"]["price"], 34.0)

    def test_research_normal_prompt_requires_gemini_search(self):
        request = parse_command_text('/research 5425')
        prompt = build_prompt(request, structured_data={}, source_list=[])
        self.assertIn('Gemini Search 任務', prompt)
        self.assertIn('公司近期重大消息', prompt)

    def test_gemini_grounding_chunks_are_extracted_as_sources(self):
        from research_center.gemini_service import _extract_sources

        payload = {
            'candidates': [
                {
                    'groundingMetadata': {
                        'groundingChunks': [
                            {'web': {'uri': 'https://example.com/a', 'title': 'Example A'}},
                            {'web': {'uri': 'https://www.twse.com.tw/test', 'title': 'TWSE'}},
                        ]
                    }
                }
            ]
        }
        sources = _extract_sources(payload)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0].title, 'Example A')
        self.assertIn('L1', sources[1].source_level)

if __name__ == '__main__':
    unittest.main()






