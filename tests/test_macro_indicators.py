from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import date
from unittest.mock import patch

from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

from research_center import macro_indicators


@dataclass
class FakeStock:
    code: str
    symbol: str
    name: str
    industry: str


class MacroIndustryFlowCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache_subdir = "macro_indicators"
        self.cache_dir = ensure_test_cache_dir(self.cache_subdir)
        self.cache_patch = patch.object(macro_indicators, "MACRO_PROXY_CACHE_DIR", self.cache_dir)
        self.cache_patch.start()

    def tearDown(self) -> None:
        self.cache_patch.stop()
        safe_remove_test_cache(self.cache_subdir)

    def test_industry_flow_writes_and_reuses_same_day_cache(self) -> None:
        stocks = [
            FakeStock("2330", "2330.TW", "台積電", "半導體"),
            FakeStock("2308", "2308.TW", "台達電", "電子零組件"),
        ]
        metrics = {
            "2330.TW": {"price": 1000, "avg_volume_20d": 2000},
            "2308.TW": {"price": 400, "avg_volume_20d": 1000},
        }

        with (
            patch.object(macro_indicators, "load_stock_universe", return_value=stocks) as load_universe,
            patch.object(macro_indicators, "_load_price_metrics_with_timeout", return_value=(metrics, {"status": "ok"}, False)) as load_price,
        ):
            first = macro_indicators._industry_flow_context({}, report_date=date(2026, 6, 10))

        self.assertFalse(first["cache_hit"])
        self.assertEqual(first["loaded_symbols"], 2)
        self.assertEqual(first["priced_symbols"], 2)
        self.assertTrue((self.cache_dir / "industry_flow_2026-06-10.json").exists())

        with (
            patch.object(macro_indicators, "load_stock_universe", side_effect=AssertionError("should use cache")) as cached_universe,
            patch.object(macro_indicators, "_load_price_metrics_with_timeout", side_effect=AssertionError("should use cache")) as cached_price,
        ):
            second = macro_indicators._industry_flow_context({}, report_date=date(2026, 6, 10))

        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["loaded_symbols"], 2)
        load_universe.assert_called_once()
        load_price.assert_called_once()
        cached_universe.assert_not_called()
        cached_price.assert_not_called()

    def test_timeout_without_cache_returns_simplified_proxy(self) -> None:
        stocks = [
            FakeStock("2330", "2330.TW", "台積電", "半導體"),
            FakeStock("2317", "2317.TW", "鴻海", "其他電子"),
        ]
        with (
            patch.object(macro_indicators, "load_stock_universe", return_value=stocks),
            patch.object(
                macro_indicators,
                "_load_price_metrics_with_timeout",
                return_value=({}, {"status": "timeout"}, True),
            ),
        ):
            result = macro_indicators._industry_flow_context({}, report_date=date(2026, 6, 10))

        self.assertTrue(result["degraded"])
        self.assertEqual(result["degraded_reason"], "price_metrics_timeout_simplified_proxy")
        self.assertEqual(result["loaded_symbols"], 2)
        self.assertEqual(result["priced_symbols"], 0)
        self.assertEqual(result["status"], "simplified_sector_proxy")

    def test_price_universe_uses_industry_sample_not_full_universe(self) -> None:
        stocks = [
            FakeStock(f"1{i:03d}", f"1{i:03d}.TW", f"半導體{i}", "半導體")
            for i in range(20)
        ] + [
            FakeStock(f"2{i:03d}", f"2{i:03d}.TW", f"電子{i}", "電子零組件")
            for i in range(20)
        ]

        sampled = macro_indicators._macro_proxy_price_universe(stocks)
        industries = {}
        for entry in sampled:
            industries.setdefault(entry.industry, 0)
            industries[entry.industry] += 1

        self.assertEqual(len(sampled), 24)
        self.assertEqual(industries["半導體"], macro_indicators.MACRO_PROXY_MAX_PRICE_SYMBOLS_PER_INDUSTRY)
        self.assertEqual(industries["電子零組件"], macro_indicators.MACRO_PROXY_MAX_PRICE_SYMBOLS_PER_INDUSTRY)


if __name__ == "__main__":
    unittest.main()
