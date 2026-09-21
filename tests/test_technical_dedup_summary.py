from __future__ import annotations

import unittest
from datetime import date

from technical_scanner import TechnicalScanResult, format_technical_report_messages
from stock_ai_bot.telegram.telegram_stock_formatting import STOCK_MARK_START, strip_stock_markers


class TechnicalDedupSummaryTests(unittest.TestCase):
    def test_summary_deduplicates_stock_and_merges_all_trigger_reasons(self):
        result = TechnicalScanResult(
            report_date=date(2026, 9, 21),
            total_symbols=3,
            hard_filter_passed=3,
            matched_symbols=3,
            bullish={
                "MACD 黃金交叉": {
                    "半導體業": ["2330 台積電 (1050.0)", "2330 台積電 (1050.0)"],
                },
                "突破 21MA": {"航運業": ["2603 長榮 (210.0)"]},
            },
            bearish={
                "KD 死亡交叉": {
                    "半導體業": ["2330 台積電 (1050.0)"],
                    "水泥工業": ["1101 台泥 (25.0)"],
                }
            },
            sources={"unit"},
            strategy_signals={
                "A": [
                    {
                        "stock_id": "2330",
                        "stock_name": "台積電",
                        "close": 1050.0,
                        "industry": "半導體業",
                        "sub_signal_type": "A1_direct_ma21_breakout",
                        "notes": "wave_return=12.5%",
                        "features": {"retracement_ratio": 0.4},
                    }
                ],
                "B": [],
                "C": [],
                "D": [],
            },
            dual_ma_signals=[
                {
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "close": 1050.0,
                    "industry": "半導體業",
                    "primary_group": "ma21_long",
                    "matched_target_mas": [21],
                    "triggers": {"MA21": ["突破"]},
                }
            ],
            kd_ma_signals=[
                {
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "close": 1050.0,
                    "industry": "半導體業",
                    "primary_group": "K2",
                    "matched_groups": ["K2"],
                    "triggers": {"MA21": ["回踩收復"]},
                    "k": 60.0,
                    "d": 50.0,
                }
            ],
        )

        messages = format_technical_report_messages(result)
        first_summary_index = next(
            index for index, message in enumerate(messages) if message.startswith("📂 技術選股去重彙整")
        )
        summary_messages = messages[first_summary_index:]
        clean_summary = strip_stock_markers("\n".join(summary_messages))

        self.assertTrue(all(message.startswith("📂 技術選股去重彙整") for message in summary_messages))
        self.assertIn("共 3 檔", summary_messages[0])
        self.assertIn("【多方／策略觸發】", clean_summary)
        self.assertIn("【多空訊號並存】", clean_summary)
        self.assertIn("【純風險訊號】", clean_summary)
        self.assertIn("[半導體業]", clean_summary)
        self.assertEqual(clean_summary.count("2330 台積電 (1050.0)"), 1)
        self.assertEqual(clean_summary.count("MACD 黃金交叉"), 1)
        self.assertIn("A1｜直接突破型（前波漲幅 12.5%、回檔比例 40%）", clean_summary)
        self.assertIn("雙均線｜MA21 > MA105 或 MA144（今日突破 MA21）", clean_summary)
        self.assertIn("K2｜KD 多方排列突破或收復 MA21（K 60.00 > D 50.00、今日回踩收復 MA21）", clean_summary)
        self.assertIn("風險：KD 死亡交叉", clean_summary)
        self.assertNotIn("A1_direct_ma21_breakout", clean_summary)
        self.assertIn(STOCK_MARK_START, "\n".join(summary_messages))

    def test_summary_paginates_without_dropping_or_duplicating_stocks(self):
        stocks = [f"{3000 + index} 測試股{index:02d} ({30 + index:.1f})" for index in range(30)]
        result = TechnicalScanResult(
            report_date=date(2026, 9, 21),
            total_symbols=30,
            hard_filter_passed=30,
            matched_symbols=30,
            bullish={"突破 5MA": {"測試產業": stocks}},
            bearish={},
            sources={"unit"},
            strategy_signals={"A": [], "B": [], "C": [], "D": []},
        )

        messages = format_technical_report_messages(result, max_chars=500)
        summary_messages = [message for message in messages if message.startswith("📂 技術選股去重彙整")]
        clean_summary = strip_stock_markers("\n".join(summary_messages))

        self.assertGreater(len(summary_messages), 1)
        self.assertIn("第 1/", summary_messages[0].splitlines()[0])
        for stock in stocks:
            self.assertEqual(clean_summary.count(stock), 1)


if __name__ == "__main__":
    unittest.main()
