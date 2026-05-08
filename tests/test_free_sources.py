from __future__ import annotations

import unittest

import pandas as pd

from research_center.command_parser import parse_command_text
from research_center.free_sources import parse_tdcc_frame, parse_twse_valuation_json
from research_center.scoring_engine import build_local_scores
from research_center.value_validation import build_value_cross_validation


class FreeSourceTests(unittest.TestCase):
    def test_parse_twse_valuation_json_filters_stock(self):
        payload = {
            'fields': ['證券代號', '證券名稱', '殖利率(%)', '本益比', '股價淨值比', '財報年/季'],
            'data': [
                ['2330', '台積電', '1.5', '22.3', '5.1', '114/1'],
                ['2317', '鴻海', '3.0', '12.0', '1.2', '114/1'],
            ],
        }
        rows = parse_twse_valuation_json(payload, '2330')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], '2330')
        self.assertEqual(rows[0]['pe_ratio'], 22.3)

    def test_parse_tdcc_frame_builds_concentration_signal(self):
        frame = pd.DataFrame(
            [
                {'資料日期': '20260501', '證券代號': '2330', '持股分級': '1', '人數': '1000', '股數': '100', '占集保庫存數比例%': '2'},
                {'資料日期': '20260501', '證券代號': '2330', '持股分級': '5', '人數': '500', '股數': '200', '占集保庫存數比例%': '4'},
                {'資料日期': '20260501', '證券代號': '2330', '持股分級': '15', '人數': '20', '股數': '300', '占集保庫存數比例%': '30'},
                {'資料日期': '20260501', '證券代號': '2330', '持股分級': '16', '人數': '5', '股數': '400', '占集保庫存數比例%': '28'},
            ]
        )
        result = parse_tdcc_frame(frame, '2330', 'sample.csv')
        self.assertEqual(result['status'], 'covered')
        self.assertEqual(result['concentration_signal'], 'high_concentration')
        self.assertEqual(result['large_holder_pct'], 58.0)

    def test_research_scores_include_free_source_scores(self):
        request = parse_command_text('/research 2330 --score')
        scores = build_local_scores(
            request,
            {
                'revenue_data': [{'YoY': 30}, {'YoY': 28}, {'YoY': 26}],
                'financial_data': [{'EPS': 1.5, 'operating_margin': 18}],
                'tdcc_data': {'status': 'covered', 'large_holder_pct': 58, 'retail_holder_pct': 6, 'concentration_signal': 'high_concentration'},
                'valuation_data': {'status': 'official_public', 'latest': {'pe_ratio': 12, 'pb_ratio': 1.5, 'dividend_yield_pct': 4}},
                'gross_margin_cache': {'status': 'covered', 'series': [{'gross_margin': 32}, {'gross_margin': 30}]},
            },
        )
        names = {score['score_name'] for score in scores}
        self.assertIn('TDCC 籌碼集中度', names)
        self.assertIn('估值安全邊際', names)
        self.assertIn('毛利率快取驗證', names)

    def test_value_validation_counts_free_sources(self):
        result = build_value_cross_validation(
            {
                'tdcc_data': {'status': 'covered', 'source': 'tdcc.csv'},
                'valuation_data': {'status': 'official_public', 'source': 'twse'},
                'gross_margin_cache': {'status': 'covered', 'source': 'gross_margin.json'},
            },
            [],
        )
        self.assertEqual(result['checks']['tdcc_distribution']['status'], 'verified')
        self.assertEqual(result['checks']['official_valuation']['status'], 'verified')
        self.assertGreaterEqual(result['verification_score'], 30)


if __name__ == '__main__':
    unittest.main()
