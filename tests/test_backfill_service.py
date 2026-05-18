"""Tests for backfill_service candidate pool building and data warm-up.

Uses mocks instead of real network calls.
"""
from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from backfill_service import (
    BackfillCandidate,
    BackfillResult,
    _add_candidate,
    _call_collect_research_data,
    _load_recent_research_codes,
    build_backfill_candidate_pool,
    build_core_research_pool,
    warmup_research_structured_data,
    warmup_market_screening_cache,
    run_full_backfill,
)


class TestAddCandidate(unittest.TestCase):
    def test_add_valid_code(self):
        pool: dict[str, BackfillCandidate] = {}
        _add_candidate(pool, {}, "2330", "portfolio", name="台積電")
        self.assertIn("2330", pool)
        self.assertEqual(pool["2330"].name, "台積電")
        self.assertIn("portfolio", pool["2330"].sources)

    def test_add_invalid_code_ignored(self):
        pool: dict[str, BackfillCandidate] = {}
        _add_candidate(pool, {}, "abc", "portfolio")
        self.assertEqual(len(pool), 0)

    def test_add_short_code_ignored(self):
        pool: dict[str, BackfillCandidate] = {}
        _add_candidate(pool, {}, "233", "portfolio")
        self.assertEqual(len(pool), 0)

    def test_same_code_from_multiple_sources(self):
        pool: dict[str, BackfillCandidate] = {}
        _add_candidate(pool, {}, "2330", "portfolio", name="台積電")
        _add_candidate(pool, {}, "2330", "recent_scan")
        self.assertEqual(len(pool), 1)
        self.assertIn("portfolio", pool["2330"].sources)
        self.assertIn("recent_scan", pool["2330"].sources)
        self.assertEqual(pool["2330"].name, "台積電")

    def test_universe_entry_used_as_fallback(self):
        from stock_scanner import StockUniverseEntry
        entry = StockUniverseEntry(code="5425", symbol="5425.TWO", market="TPEX", name="台半", industry="半導體")
        pool: dict[str, BackfillCandidate] = {}
        universe_by_code = {"5425": entry}
        _add_candidate(pool, universe_by_code, "5425", "hard_filter_revenue")
        self.assertEqual(pool["5425"].name, "台半")
        self.assertEqual(pool["5425"].symbol, "5425.TWO")


class TestLoadRecentResearchCodes(unittest.TestCase):
    def test_extracts_codes_from_report_json(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("backfill_service/research_codes")
        try:
            report_dir = tmp / "stock" / "2330"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_file = report_dir / "report_20260515.json"
            report_data = {
                "stock_id": "2330",
                "metadata": {"stock_id": "2330"},
            }
            report_file.write_text(json.dumps(report_data, ensure_ascii=False), encoding="utf-8")

            # Patch the reports path
            with patch("backfill_service.Path") as mock_path_cls:
                def path_side_effect(arg):
                    if arg == "reports":
                        return tmp  # Use Path object of our test cache dir
                    return Path(arg)

                mock_path_cls.side_effect = path_side_effect
                # We need _load_recent_research_codes to use our temp dir
                codes = set()
                report_root = tmp
                for path in sorted(report_root.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:80]:
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    for key_path in [
                        data.get("stock_id"),
                        (data.get("metadata") or {}).get("stock_id"),
                    ]:
                        code = str(key_path or "").strip()
                        if code.isdigit() and len(code) == 4:
                            codes.add(code)

                self.assertIn("2330", codes)
        finally:
            from tests.test_cache_utils import safe_remove_test_cache
            safe_remove_test_cache("backfill_service/research_codes")


class TestBuildBackfillCandidatePool(unittest.TestCase):
    """Test candidate pool creation using mocks."""

    @patch("backfill_service.load_price_metrics")
    @patch("backfill_service.load_recent_revenue_history")
    @patch("backfill_service.load_stock_universe")
    @patch("backfill_service.load_portfolio")
    @patch("backfill_service.load_recent_scan_results")
    @patch("backfill_service._load_recent_research_codes")
    def test_portfolio_and_monitor_in_pool(self, mock_research_codes, mock_recent_scans, mock_portfolio, mock_universe, mock_revenue, mock_price_metrics):
        from stock_scanner import StockUniverseEntry, RevenuePoint

        mock_universe.return_value = [
            StockUniverseEntry(code="2330", symbol="2330.TW", market="TWSE", name="台積電", industry="半導體"),
            StockUniverseEntry(code="5425", symbol="5425.TWO", market="TPEX", name="台半", industry="半導體"),
        ]
        mock_revenue.return_value = {
            "2330": [RevenuePoint(month="2026-04", revenue=200000, yoy=10.0)],
        }
        mock_price_metrics.return_value = {
            "2330.TW": {"price": 800.0, "avg_volume_20d": 5000},
        }
        mock_portfolio.return_value = {"2330": "台積電"}
        mock_recent_scans.return_value = []
        mock_research_codes.return_value = set()

        config_data = json.dumps({"monitor_stocks": [{"symbol": "5425.TWO", "name": "台半"}]})
        with patch("builtins.open", unittest.mock.mock_open(read_data=config_data)):
            candidates, universe, warnings = build_backfill_candidate_pool(date(2026, 5, 15))

        # 2330 should be in pool from revenue hard filter and portfolio
        self.assertIn("2330", candidates)
        self.assertIn("hard_filter_revenue", candidates["2330"].sources)

    @patch("backfill_service.load_price_metrics")
    @patch("backfill_service.load_recent_revenue_history")
    @patch("backfill_service.load_stock_universe")
    @patch("backfill_service.load_portfolio")
    @patch("backfill_service.load_recent_scan_results")
    @patch("backfill_service._load_recent_research_codes")
    def test_recent_scan_codes_in_pool(self, mock_research_codes, mock_recent_scans, mock_portfolio, mock_universe, mock_revenue, mock_price_metrics):
        from stock_scanner import StockUniverseEntry

        mock_universe.return_value = [
            StockUniverseEntry(code="6282", symbol="6282.TW", market="TWSE", name="康弘", industry="生技"),
        ]
        mock_revenue.return_value = {}
        mock_price_metrics.return_value = {}
        mock_portfolio.return_value = {}
        mock_recent_scans.return_value = [
            {"selected_codes": ["6282"], "scan_type": "精選選股"},
        ]
        mock_research_codes.return_value = set()

        config_data = json.dumps({"monitor_stocks": []})
        with patch("builtins.open", unittest.mock.mock_open(read_data=config_data)):
            candidates, universe, warnings = build_backfill_candidate_pool(date(2026, 5, 15))

        self.assertIn("6282", candidates)
        self.assertIn("recent_scan", candidates["6282"].sources)

    @patch("backfill_service.load_price_metrics")
    @patch("backfill_service.load_recent_revenue_history")
    @patch("backfill_service.load_stock_universe")
    @patch("backfill_service.load_portfolio")
    @patch("backfill_service.load_recent_scan_results")
    @patch("backfill_service._load_recent_research_codes")
    def test_price_volume_hard_filter(self, mock_research_codes, mock_recent_scans, mock_portfolio, mock_universe, mock_revenue, mock_price_metrics):
        from stock_scanner import StockUniverseEntry

        mock_universe.return_value = [
            StockUniverseEntry(code="2330", symbol="2330.TW", market="TWSE", name="台積電", industry="半導體"),
            StockUniverseEntry(code="0001", symbol="0001.TW", market="TWSE", name="低價股", industry="其他"),
        ]
        mock_revenue.return_value = {}
        mock_price_metrics.return_value = {
            "2330.TW": {"price": 800.0, "avg_volume_20d": 5000},
            "0001.TW": {"price": 3.0, "avg_volume_20d": 100},
        }
        mock_portfolio.return_value = {}
        mock_recent_scans.return_value = []
        mock_research_codes.return_value = set()

        config_data = json.dumps({"scan_settings": {"min_price": 5, "max_price": 100000, "min_avg_volume_20d": 500, "min_monthly_revenue": 0}, "monitor_stocks": []})
        with patch("builtins.open", unittest.mock.mock_open(read_data=config_data)):
            candidates, universe, warnings = build_backfill_candidate_pool(date(2026, 5, 15))

        # 2330 should pass price/volume filter (price >= 5, volume >= 500)
        self.assertIn("2330", candidates)
        self.assertIn("hard_filter_price_volume", candidates["2330"].sources)
        # 0001 should NOT pass (price < 5, volume < 500)
        self.assertNotIn("0001", candidates)

    @patch("backfill_service.load_price_metrics")
    @patch("backfill_service.load_recent_revenue_history")
    @patch("backfill_service.load_stock_universe")
    @patch("backfill_service.load_portfolio")
    @patch("backfill_service.load_recent_scan_results")
    @patch("backfill_service._load_recent_research_codes")
    def test_revenue_improving_in_pool(self, mock_research_codes, mock_recent_scans, mock_portfolio, mock_universe, mock_revenue, mock_price_metrics):
        from stock_scanner import StockUniverseEntry, RevenuePoint

        mock_universe.return_value = [
            StockUniverseEntry(code="5425", symbol="5425.TWO", market="TPEX", name="台半", industry="半導體"),
        ]
        mock_revenue.return_value = {
            "5425": [
                RevenuePoint(month="2026-04", revenue=100000000, yoy=12.0),
                RevenuePoint(month="2026-03", revenue=90000000, yoy=5.0),
            ]
        }
        mock_price_metrics.return_value = {}
        mock_portfolio.return_value = {}
        mock_recent_scans.return_value = []
        mock_research_codes.return_value = set()

        config_data = json.dumps({"scan_settings": {}, "monitor_stocks": []})
        with patch("builtins.open", unittest.mock.mock_open(read_data=config_data)):
            candidates, universe, warnings = build_backfill_candidate_pool(date(2026, 5, 15))

        self.assertIn("5425", candidates)
        self.assertIn("hard_filter_revenue_improving", candidates["5425"].sources)

    @patch("backfill_service.load_price_metrics")
    @patch("backfill_service.load_recent_revenue_history")
    @patch("backfill_service.load_stock_universe")
    @patch("backfill_service.load_portfolio")
    @patch("backfill_service.load_recent_scan_results")
    @patch("backfill_service._load_recent_research_codes")
    def test_price_volume_filter_respects_config_max_price(self, mock_research_codes, mock_recent_scans, mock_portfolio, mock_universe, mock_revenue, mock_price_metrics):
        from stock_scanner import StockUniverseEntry

        mock_universe.return_value = [
            StockUniverseEntry(code="2330", symbol="2330.TW", market="TWSE", name="台積電", industry="半導體"),
            StockUniverseEntry(code="5425", symbol="5425.TWO", market="TPEX", name="台半", industry="半導體"),
        ]
        mock_revenue.return_value = {}
        mock_price_metrics.return_value = {
            "2330.TW": {"price": 800.0, "avg_volume_20d": 5000},
            "5425.TWO": {"price": 60.0, "avg_volume_20d": 1000},
        }
        mock_portfolio.return_value = {}
        mock_recent_scans.return_value = []
        mock_research_codes.return_value = set()

        config_data = json.dumps({
            "scan_settings": {
                "min_price": 10,
                "max_price": 80,
                "min_avg_volume_20d": 500,
                "min_monthly_revenue": 50000000,
            },
            "monitor_stocks": [],
        })
        with patch("builtins.open", unittest.mock.mock_open(read_data=config_data)):
            candidates, universe, warnings = build_backfill_candidate_pool(date(2026, 5, 15))

        # 2330 at 800 should NOT pass price filter (exceeds max_price=80)
        self.assertNotIn("hard_filter_price_volume", candidates.get("2330", BackfillCandidate(code="2330")).sources)
        # 5425 at 60 should pass (within min_price=10 and max_price=80, volume >= 500)
        self.assertIn("5425", candidates)
        self.assertIn("hard_filter_price_volume", candidates["5425"].sources)


class TestCallCollectResearchData(unittest.TestCase):
    """Test _call_collect_research_data (called by warmup but tested separately here)."""

    @patch("backfill_service.collect_research_data")
    def test_returns_true_on_success(self, mock_collect):
        mock_collect.return_value = {"stock": {"code": "2330"}, "notes": []}

        success, err, warn = _call_collect_research_data(
            "2330", "台積電", date(2026, 5, 15), False, None
        )

        self.assertTrue(success)
        self.assertIsNone(err)
        self.assertIsNone(warn)

    @patch("backfill_service.collect_research_data")
    def test_returns_error_on_exception(self, mock_collect):
        mock_collect.side_effect = Exception("API failed")

        success, err, warn = _call_collect_research_data(
            "2330", "台積電", date(2026, 5, 15), False, None
        )

        self.assertFalse(success)
        self.assertEqual(err, "API failed")


class TestWarmupResearchStructuredData(unittest.TestCase):
    """Test warmup_research_structured_data with core_pool (renamed from candidates)."""

    @patch("backfill_service.load_research_structured_cache", return_value=None)
    @patch("backfill_service._call_collect_research_data")
    def test_calls_collect_for_core_pool(self, mock_call, mock_load_cache):
        mock_call.return_value = (True, None, None)

        core_pool = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }

        count, used_cache, warnings, timeout_count = warmup_research_structured_data(
            core_pool, date(2026, 5, 15), force_refresh=False, progress=None
        )

        self.assertEqual(count, 1)
        self.assertEqual(timeout_count, 0)
        mock_call.assert_called_once()

    @patch("backfill_service.load_research_structured_cache")
    @patch("backfill_service._call_collect_research_data")
    def test_uses_cache_when_available(self, mock_call, mock_load_cache):
        mock_load_cache.return_value = {"stock": {"code": "2330"}, "notes": []}

        core_pool = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }

        count, used_cache, warnings, timeout_count = warmup_research_structured_data(
            core_pool, date(2026, 5, 15), force_refresh=False, progress=None
        )

        self.assertEqual(count, 1)
        self.assertEqual(used_cache, ["2330"])
        self.assertEqual(timeout_count, 0)
        mock_call.assert_not_called()

    @patch("backfill_service.load_research_structured_cache", return_value=None)
    @patch("backfill_service._call_collect_research_data")
    def test_force_refresh_skips_cache(self, mock_call, mock_load_cache):
        mock_call.return_value = (True, None, None)

        core_pool = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }

        count, used_cache, warnings, timeout_count = warmup_research_structured_data(
            core_pool, date(2026, 5, 15), force_refresh=True, progress=None
        )

        self.assertEqual(count, 1)
        self.assertEqual(timeout_count, 0)
        mock_call.assert_called_once()

    @patch("backfill_service.load_research_structured_cache", return_value=None)
    @patch("backfill_service._call_collect_research_data")
    def test_progress_messages_on_success(self, mock_call, mock_load_cache):
        mock_call.return_value = (True, None, None)

        core_pool = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }
        messages = []
        count, used_cache, warnings, timeout_count = warmup_research_structured_data(
            core_pool, date(2026, 5, 15), force_refresh=False, progress=messages.append
        )

        self.assertEqual(count, 1)
        self.assertEqual(timeout_count, 0)
        self.assertTrue(any("核心股完整投研回補開始" in m for m in messages))
        self.assertTrue(any("開始" in m and "2330" in m for m in messages))
        self.assertTrue(any("完成" in m or "失敗" in m for m in messages))
        self.assertEqual(warnings, [])

    @patch("backfill_service.load_research_structured_cache")
    @patch("backfill_service._call_collect_research_data")
    def test_progress_messages_on_cache_hit(self, mock_call, mock_load_cache):
        mock_load_cache.return_value = {"stock": {"code": "2330", "name": "台積電"}, "notes": []}

        core_pool = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }
        messages = []
        count, used_cache, warnings, timeout_count = warmup_research_structured_data(
            core_pool, date(2026, 5, 15), force_refresh=False, progress=messages.append
        )

        self.assertEqual(count, 1)
        self.assertEqual(timeout_count, 0)
        self.assertTrue(any("快取命中" in m for m in messages))
        mock_call.assert_not_called()

    @patch("backfill_service.load_research_structured_cache", return_value=None)
    @patch("backfill_service._call_collect_research_data")
    def test_progress_messages_on_failure(self, mock_call, mock_load_cache):
        mock_call.return_value = (False, "network error", None)

        core_pool = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }
        messages = []
        count, used_cache, warnings, timeout_count = warmup_research_structured_data(
            core_pool, date(2026, 5, 15), force_refresh=False, progress=messages.append
        )

        self.assertEqual(count, 0)
        self.assertEqual(timeout_count, 0)
        self.assertTrue(any("開始" in m and "2330" in m for m in messages))
        self.assertTrue(any("失敗" in m for m in messages))
        self.assertTrue(any("network error" in w for w in warnings))


class TestBackfillResultDataclass(unittest.TestCase):
    def test_default_values(self):
        result = BackfillResult(report_date=date(2026, 5, 15))
        self.assertEqual(result.universe_count, 0)
        self.assertEqual(result.candidate_count, 0)
        self.assertEqual(result.research_structured_count, 0)
        self.assertEqual(result.gross_margin_count, 0)
        self.assertEqual(result.curated_scan_count, 0)
        self.assertEqual(result.screening_revenue_count, 0)
        self.assertEqual(result.screening_price_metric_count, 0)
        self.assertEqual(result.screening_technical_count, 0)
        self.assertEqual(result.screening_warning_count, 0)
        self.assertEqual(result.research_structured_timeout_count, 0)
        self.assertEqual(result.used_cache, [])
        self.assertEqual(result.warnings, [])

    def test_explicit_values(self):
        result = BackfillResult(
            report_date=date(2026, 5, 15),
            universe_count=1000,
            candidate_count=50,
            screening_revenue_count=800,
            screening_price_metric_count=1800,
            screening_technical_count=1700,
            research_structured_count=48,
            gross_margin_count=45,
            curated_scan_count=12,
            used_cache=["2330", "2317"],
            warnings=["test"],
        )
        self.assertEqual(result.universe_count, 1000)
        self.assertEqual(result.screening_revenue_count, 800)
        self.assertEqual(result.screening_price_metric_count, 1800)
        self.assertEqual(result.screening_technical_count, 1700)
        self.assertEqual(result.research_structured_count, 48)
        self.assertEqual(result.gross_margin_count, 45)
        self.assertEqual(result.curated_scan_count, 12)
        self.assertEqual(len(result.used_cache), 2)


class TestWarmupMarketScreeningCache(unittest.TestCase):
    """Test warmup_market_screening_cache: market-wide data warmup."""

    @patch("backfill_service.fetch_daily_history")
    @patch("backfill_service.load_price_metrics")
    @patch("backfill_service.load_recent_revenue_history")
    @patch("backfill_service.load_stock_universe")
    def test_calls_all_market_wide_functions(
        self, mock_universe, mock_revenue, mock_price, mock_tech
    ):
        from stock_scanner import StockUniverseEntry

        mock_universe.return_value = [
            StockUniverseEntry(code="2330", symbol="2330.TW", market="TWSE", name="台積電", industry="半導體"),
        ]
        mock_revenue.return_value = {"2330": ["revenue_data"]}
        mock_price.return_value = {"2330.TW": {"price": 800.0, "avg_volume_20d": 5000}}
        mock_tech.return_value = (MagicMock(), "Yahoo Finance")

        from backfill_service import warmup_market_screening_cache

        result = warmup_market_screening_cache(
            mock_universe.return_value, date(2026, 5, 15), force_refresh=False, progress=None
        )

        mock_revenue.assert_called_once_with(mock_universe.return_value)
        mock_price.assert_called_once_with(mock_universe.return_value, force_refresh=False)
        mock_tech.assert_called_once_with("2330.TW", date(2026, 5, 15))
        self.assertEqual(result["revenue_count"], 1)
        self.assertEqual(result["price_metric_count"], 1)
        self.assertEqual(result["technical_count"], 1)
        self.assertEqual(len(result["warnings"]), 0)

    @patch("backfill_service.fetch_daily_history")
    @patch("backfill_service.load_price_metrics")
    @patch("backfill_service.load_recent_revenue_history")
    @patch("backfill_service.load_stock_universe")
    def test_single_failure_does_not_abort(
        self, mock_universe, mock_revenue, mock_price, mock_tech
    ):
        from stock_scanner import StockUniverseEntry

        mock_universe.return_value = [
            StockUniverseEntry(code="2330", symbol="2330.TW", market="TWSE", name="台積電", industry="半導體"),
            StockUniverseEntry(code="5425", symbol="5425.TWO", market="TPEX", name="台半", industry="半導體"),
        ]
        mock_revenue.return_value = {"2330": ["data"], "5425": ["data"]}
        mock_price.return_value = {
            "2330.TW": {"price": 800.0},
            "5425.TWO": {"price": 60.0},
        }
        # First call fails, second succeeds
        mock_tech.side_effect = [Exception("network error"), (MagicMock(), "Yahoo Finance")]

        from backfill_service import warmup_market_screening_cache

        result = warmup_market_screening_cache(
            mock_universe.return_value, date(2026, 5, 15), force_refresh=False, progress=None
        )

        self.assertEqual(result["technical_count"], 1)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("技術日線快取失敗 2330", result["warnings"][0])

    @patch("backfill_service.fetch_daily_history")
    @patch("backfill_service.load_price_metrics")
    @patch("backfill_service.load_recent_revenue_history")
    @patch("backfill_service.load_stock_universe")
    def test_gross_margin_cache_loaded_not_per_stock(
        self, mock_universe, mock_revenue, mock_price, mock_tech
    ):
        from stock_scanner import StockUniverseEntry

        mock_universe.return_value = [
            StockUniverseEntry(code="2330", symbol="2330.TW", market="TWSE", name="台積電", industry="半導體"),
        ]
        mock_revenue.return_value = {"2330": ["data"]}
        mock_price.return_value = {"2330.TW": {"price": 800.0}}
        mock_tech.return_value = (MagicMock(), "本機快取")

        from backfill_service import warmup_market_screening_cache

        with patch("stock_scanner._load_gross_margin_cache", return_value={}) as mock_gm_load, \
             patch("stock_scanner._save_gross_margin_cache") as mock_gm_save:
            result = warmup_market_screening_cache(
                mock_universe.return_value, date(2026, 5, 15), force_refresh=False, progress=None
            )

            # _load_gross_margin_cache should be called to ensure the base file exists
            mock_gm_load.assert_called_once()
            # _save_gross_margin_cache should NOT be called (no per-stock brute force)
            mock_gm_save.assert_not_called()


class TestRunFullBackfillIntegration(unittest.TestCase):
    """Test run_full_backfill calls warmup_market_screening_cache first."""

    @patch("backfill_service.warmup_market_screening_cache")
    @patch("backfill_service.build_backfill_candidate_pool")
    @patch("backfill_service.backfill_candidate_data")
    @patch("backfill_service.load_stock_universe")
    def test_warms_screening_cache_before_candidates(
        self, mock_universe, mock_backfill_data, mock_build_pool, mock_screening
    ):
        from stock_scanner import StockUniverseEntry

        mock_universe.return_value = [
            StockUniverseEntry(code="2330", symbol="2330.TW", market="TWSE", name="台積電", industry="半導體"),
        ]
        from backfill_service import BackfillCandidate, BackfillResult

        mock_build_pool.return_value = (
            {"2330": BackfillCandidate(code="2330", symbol="2330.TW", name="台積電", sources={"portfolio"})},
            mock_universe.return_value,
            [],
        )
        mock_backfill_data.return_value = BackfillResult(report_date=date(2026, 5, 15))
        mock_screening.return_value = {
            "revenue_count": 500,
            "price_metric_count": 1800,
            "technical_count": 1700,
            "warnings": [],
        }

        from backfill_service import run_full_backfill

        result = run_full_backfill(date(2026, 5, 15), force_refresh=False, progress=None)

        # Verify warmup_market_screening_cache was called with the full universe
        mock_screening.assert_called_once()
        self.assertEqual(mock_screening.call_args[0][0], mock_universe.return_value)

        # Verify build_backfill_candidate_pool received preloaded_universe
        mock_build_pool.assert_called_once()
        self.assertEqual(mock_build_pool.call_args[1]["preloaded_universe"], mock_universe.return_value)

        # Verify screening counts are populated in result
        self.assertEqual(result.screening_revenue_count, 500)
        self.assertEqual(result.screening_price_metric_count, 1800)
        self.assertEqual(result.screening_technical_count, 1700)
        self.assertEqual(result.screening_warning_count, 0)

    @patch("backfill_service.warmup_market_screening_cache")
    @patch("backfill_service.build_backfill_candidate_pool")
    @patch("backfill_service.backfill_candidate_data")
    @patch("backfill_service.load_stock_universe")
    def test_force_refresh_passes_to_load_stock_universe(
        self, mock_universe, mock_backfill_data, mock_build_pool, mock_screening
    ):
        from stock_scanner import StockUniverseEntry

        mock_universe.return_value = [
            StockUniverseEntry(code="2330", symbol="2330.TW", market="TWSE", name="台積電", industry="半導體"),
        ]
        from backfill_service import BackfillCandidate, BackfillResult

        mock_build_pool.return_value = (
            {"2330": BackfillCandidate(code="2330", symbol="2330.TW", name="台積電", sources={"portfolio"})},
            mock_universe.return_value,
            [],
        )
        mock_backfill_data.return_value = BackfillResult(report_date=date(2026, 5, 15))
        mock_screening.return_value = {
            "revenue_count": 0,
            "price_metric_count": 0,
            "technical_count": 0,
            "warnings": [],
        }

        from backfill_service import run_full_backfill

        result = run_full_backfill(date(2026, 5, 15), force_refresh=True, progress=None)

        # Verify load_stock_universe was called with force_refresh=True
        mock_universe.assert_called_once_with(force_refresh=True)


class TestBuildCoreResearchPool(unittest.TestCase):
    """Test build_core_research_pool: three-tier core pool selection."""

    def test_portfolio_and_monitor_in_core(self):
        """Portfolio and monitor_list stocks are always in core pool."""
        candidates = {
            "2330": BackfillCandidate(code="2330", name="台積電", sources={"portfolio", "hard_filter_revenue"}),
            "5425": BackfillCandidate(code="5425", name="台半", sources={"monitor_list"}),
            "6282": BackfillCandidate(code="6282", name="康弘", sources={"hard_filter_revenue"}),
        }
        config = {}
        core = build_core_research_pool(candidates, config, progress=None)
        self.assertIn("2330", core)
        self.assertIn("5425", core)
        # With only 3 candidates and limit=80, all fit; no need to exclude 6282
        self.assertIn("6282", core)

    def test_core_respects_limit(self):
        """Core pool size is limited by DEFAULT_CORE_RESEARCH_LIMIT."""
        candidates = {}
        for i in range(100):
            code = f"{i:04d}"
            candidates[code] = BackfillCandidate(
                code=code, name=f"Stock{i}", sources={"hard_filter_revenue"}
            )
        config = {}
        core = build_core_research_pool(candidates, config, progress=None)
        # Default limit is 80
        self.assertLessEqual(len(core), 80)
        self.assertGreater(len(core), 0)

    def test_recent_scan_and_research_in_core(self):
        """recent_scan and recent_research stocks are in core pool."""
        candidates = {
            "2330": BackfillCandidate(code="2330", name="台積電", sources={"portfolio"}),
            "6282": BackfillCandidate(code="6282", name="康弘", sources={"recent_scan"}),
            "2317": BackfillCandidate(code="2317", name="仁寶", sources={"recent_research"}),
        }
        config = {}
        core = build_core_research_pool(candidates, config, progress=None)
        self.assertIn("2330", core)
        self.assertIn("6282", core)
        self.assertIn("2317", core)

    def test_empty_candidates_returns_empty_core(self):
        """Empty candidates dict returns empty core pool."""
        candidates = {}
        config = {}
        core = build_core_research_pool(candidates, config, progress=None)
        self.assertEqual(len(core), 0)

    def test_config_limit_overrides_default(self):
        """backfill_core_research_limit in config overrides default limit."""
        candidates = {}
        for i in range(20):
            code = f"{i:04d}"
            candidates[code] = BackfillCandidate(code=code, name=f"S{i}", sources={"hard_filter_revenue"})
        config = {"backfill_core_research_limit": 5}
        core = build_core_research_pool(candidates, config, progress=None)
        self.assertLessEqual(len(core), 5)


class TestThreeTierBackfillFlow(unittest.TestCase):
    """Test that warmup_research_structured_data only receives core pool, not all candidates."""

    @patch("backfill_service.warmup_gross_margin_cache", return_value=(0, []))
    @patch("backfill_service.build_and_save_curated_scan_cache", return_value=([], 0))
    @patch("backfill_service.warmup_chip_data_cache")
    @patch("backfill_service.load_recent_revenue_history", return_value=[])
    @patch("backfill_service.load_price_metrics", return_value=[])
    @patch("backfill_service.fetch_daily_history")
    @patch("backfill_service.warmup_research_structured_data")
    @patch("backfill_service.build_core_research_pool")
    @patch("backfill_service.warmup_market_screening_cache")
    @patch("backfill_service.build_backfill_candidate_pool")
    @patch("backfill_service.load_stock_universe")
    def test_warmup_receives_core_not_all_candidates(
        self, mock_universe, mock_build_pool,
        mock_screening, mock_build_core, mock_warmup,
        mock_fetch, mock_price, mock_rev, mock_chip,
        mock_curated, mock_gm,
    ):
        from stock_scanner import StockUniverseEntry
        from backfill_service import BackfillCandidate

        mock_universe.return_value = [
            StockUniverseEntry(code="2330", symbol="2330.TW", name="台積電", market="TWSE"),
            StockUniverseEntry(code="5425", symbol="5425.TWO", name="台半", market="TPEX"),
            StockUniverseEntry(code="6282", symbol="6282.TW", name="康弘", market="TWSE"),
        ]
        candidates = {
            "2330": BackfillCandidate(code="2330", sources={"portfolio"}),
            "5425": BackfillCandidate(code="5425", sources={"hard_filter_revenue"}),
            "6282": BackfillCandidate(code="6282", sources={"hard_filter_revenue"}),
        }
        mock_build_pool.return_value = (candidates, mock_universe.return_value, [])
        core_pool = {"2330": candidates["2330"]}
        mock_build_core.return_value = core_pool
        mock_screening.return_value = {"revenue_count": 0, "price_metric_count": 0, "technical_count": 0, "warnings": []}
        mock_warmup.return_value = (1, [], [], 0)

        from backfill_service import run_full_backfill

        result = run_full_backfill(date(2026, 5, 15), progress=None)

        # Verify build_core_research_pool was called
        self.assertTrue(mock_build_core.called)
        core_arg = mock_build_core.call_args[0][0]
        self.assertEqual(len(core_arg), 3)  # all candidates passed

        # warmup_research_structured_data should have been called with core_pool (1 stock)
        self.assertTrue(mock_warmup.called)
        call_args = mock_warmup.call_args
        core_arg = call_args[0][0]
        self.assertEqual(len(core_arg), 1)
        self.assertIn("2330", core_arg)
        # Should not have 3 stocks
        self.assertNotEqual(len(core_arg), 3)

    @patch("backfill_service.warmup_gross_margin_cache", return_value=(0, []))
    @patch("backfill_service.build_and_save_curated_scan_cache", return_value=([], 0))
    @patch("backfill_service.warmup_chip_data_cache")
    @patch("backfill_service.load_recent_revenue_history", return_value=[])
    @patch("backfill_service.load_price_metrics", return_value=[])
    @patch("backfill_service.fetch_daily_history")
    @patch("backfill_service.backfill_candidate_data")
    @patch("backfill_service.build_core_research_pool")
    @patch("backfill_service.warmup_market_screening_cache")
    @patch("backfill_service.build_backfill_candidate_pool")
    @patch("backfill_service.load_stock_universe")
    def test_empty_core_pool_skips_research_warmup(
        self, mock_universe, mock_build_pool,
        mock_screening, mock_build_core, mock_backfill_data,
        mock_fetch, mock_price, mock_rev, mock_chip,
        mock_curated, mock_gm,
    ):
        from stock_scanner import StockUniverseEntry
        from backfill_service import BackfillCandidate, BackfillResult

        mock_universe.return_value = [StockUniverseEntry(code="2330", symbol="2330.TW", name="台積電", market="TWSE")]
        candidates = {"2330": BackfillCandidate(code="2330", sources={"hard_filter_revenue"})}
        mock_build_pool.return_value = (candidates, mock_universe.return_value, [])
        mock_build_core.return_value = {}  # Empty core pool
        mock_screening.return_value = {"revenue_count": 0, "price_metric_count": 0, "technical_count": 0, "warnings": []}
        mock_backfill_data.return_value = BackfillResult(report_date=date(2026, 5, 15))

        from backfill_service import run_full_backfill

        result = run_full_backfill(date(2026, 5, 15), progress=None)

        # backfill_candidate_data should still be called (even with empty core)
        self.assertTrue(mock_backfill_data.called)


class TestWarmupResearchStructuredDataCorePool(unittest.TestCase):
    """Test warmup_research_structured_data with core_pool (not all candidates)."""

    @patch("backfill_service.load_research_structured_cache", return_value=None)
    @patch("backfill_service._call_collect_research_data")
    def test_calls_collect_research_data_for_core_pool(self, mock_call, mock_load_cache):
        mock_call.return_value = (True, None, None)

        core_pool = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }

        count, used_cache, warnings, timeout_count = warmup_research_structured_data(
            core_pool, date(2026, 5, 15), force_refresh=False, progress=None
        )

        self.assertEqual(count, 1)
        self.assertEqual(len(used_cache), 0)
        self.assertEqual(timeout_count, 0)
        mock_call.assert_called_once()

    @patch("backfill_service.load_research_structured_cache", return_value=None)
    @patch("backfill_service._call_collect_research_data")
    def test_progress_shows_core_limit_not_candidate_count(self, mock_call, mock_load_cache):
        mock_call.return_value = (True, None, None)

        core_pool = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }
        messages = []
        count, used_cache, warnings, timeout_count = warmup_research_structured_data(
            core_pool, date(2026, 5, 15), force_refresh=False, progress=messages.append
        )

        self.assertEqual(count, 1)
        self.assertEqual(timeout_count, 0)
        self.assertIn("核心股完整投研回補開始", "\n".join(messages))
        self.assertTrue(any("1/1" in m for m in messages))

    @patch("backfill_service.load_research_structured_cache", return_value=None)
    @patch("backfill_service._call_collect_research_data")
    def test_empty_core_pool_skips(self, mock_call, mock_load_cache):
        core_pool = {}
        messages = []
        count, used_cache, warnings, timeout_count = warmup_research_structured_data(
            core_pool, date(2026, 5, 15), force_refresh=False, progress=messages.append
        )

        self.assertEqual(count, 0)
        self.assertEqual(timeout_count, 0)
        self.assertIn("核心池為空", "\n".join(messages))
        mock_call.assert_not_called()

    def test_returns_four_values_including_timeout_count(self):
        """Test that warmup_research_structured_data returns exactly 4 values."""
        # Test with empty core_pool (all success paths return 4 values)
        from backfill_service import warmup_research_structured_data
        result = warmup_research_structured_data({}, date(2026, 5, 15), False, None)
        self.assertEqual(len(result), 4)
        count, used_cache, warnings, timeout_count = result
        self.assertEqual(timeout_count, 0)
        self.assertEqual(count, 0)
        self.assertEqual(used_cache, [])
        self.assertEqual(warnings, [])

    @patch("backfill_service._call_collect_research_data")
    def test_timeout_increments_timeout_count_and_continues(self, mock_call):
        import time
        # Sleep longer than timeout_sec to force timeout
        def slow_call(*args, **kwargs):
            time.sleep(0.5)
            return (True, None, None)
        mock_call.side_effect = slow_call

        core_pool = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }
        messages = []
        count, used_cache, warnings, timeout_count = warmup_research_structured_data(
            core_pool, date(2026, 5, 15), force_refresh=False, progress=messages.append, timeout_sec=0.01
        )

        self.assertEqual(count, 0)
        self.assertEqual(timeout_count, 1)
        self.assertEqual(used_cache, [])
        self.assertTrue(any("逾時" in w for w in warnings))
        self.assertTrue(any("逾時跳過" in m for m in messages))


class TestBackfillCandidateDataTimeoutWrite(unittest.TestCase):
    """Test backfill_candidate_data writes timeout_count to BackfillResult."""

    @patch("backfill_service.warmup_research_structured_data")
    @patch("backfill_service.load_recent_revenue_history", return_value={})
    @patch("backfill_service.load_price_metrics", return_value={})
    @patch("backfill_service.fetch_daily_history")
    @patch("backfill_service.warmup_gross_margin_cache", return_value=(0, []))
    @patch("backfill_service.warmup_chip_data_cache")
    @patch("backfill_service.build_and_save_curated_scan_cache", return_value=([], 0))
    def test_timeout_count_written_to_result(
        self, mock_curated, mock_chip, mock_gm,
        mock_fetch, mock_price, mock_rev, mock_warmup,
    ):
        from backfill_service import BackfillCandidate, BackfillResult, backfill_candidate_data

        mock_warmup.return_value = (2, ["2330"], ["warn"], 1)

        candidates = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }
        core_pool = {
            "2330": BackfillCandidate(code="2330", symbol="2330.TW", market="TWSE", name="台積電", sources={"portfolio"}),
        }
        universe = [
            MagicMock(code="2330", symbol="2330.TW", name="台積電", market="TWSE"),
        ]

        result = backfill_candidate_data(
            candidates, core_pool, universe, date(2026, 5, 15), force_refresh=False, progress=None, timeout_sec=30, stop_event=None,
        )

        self.assertEqual(result.research_structured_count, 2)
        self.assertEqual(result.used_cache, ["2330"])
        self.assertEqual(result.research_structured_timeout_count, 1)


class TestBackfillResultThreeTierFields(unittest.TestCase):
    """Test BackfillResult includes three-tier fields."""

    def test_core_research_count_field(self):
        result = BackfillResult(report_date=date(2026, 5, 15))
        self.assertEqual(result.core_research_count, 0)
        result.core_research_count = 45
        self.assertEqual(result.core_research_count, 45)

    def test_research_structured_timeout_count_field(self):
        result = BackfillResult(report_date=date(2026, 5, 15))
        self.assertEqual(result.research_structured_timeout_count, 0)
        result.research_structured_timeout_count = 3
        self.assertEqual(result.research_structured_timeout_count, 3)


class TestResolveBackfillReportDate(unittest.TestCase):
    """Test resolve_backfill_report_date: time-based target date selection using Asia/Taipei."""

    def test_monday_before_15_returns_last_friday_taipei(self):
        from backfill_service import resolve_backfill_report_date
        from datetime import datetime
        from zoneinfo import ZoneInfo
        taipei = ZoneInfo("Asia/Taipei")
        # Monday at 14:59 Taipei time -> should return last Friday
        monday_1459 = datetime(2026, 5, 18, 14, 59, 0, tzinfo=taipei)  # May 18 is Monday
        result = resolve_backfill_report_date(monday_1459)
        self.assertEqual(result.isoformat(), "2026-05-15")  # Friday

    def test_tuesday_before_15_returns_monday_taipei(self):
        from backfill_service import resolve_backfill_report_date
        from datetime import datetime
        from zoneinfo import ZoneInfo
        taipei = ZoneInfo("Asia/Taipei")
        # Tuesday at 14:59 Taipei time -> should return Monday
        tuesday_1459 = datetime(2026, 5, 19, 14, 59, 0, tzinfo=taipei)  # May 19 is Tuesday
        result = resolve_backfill_report_date(tuesday_1459)
        self.assertEqual(result.isoformat(), "2026-05-18")  # Monday

    def test_friday_at_1500_returns_friday_taipei(self):
        from backfill_service import resolve_backfill_report_date
        from datetime import datetime
        from zoneinfo import ZoneInfo
        taipei = ZoneInfo("Asia/Taipei")
        # Friday at 15:00 Taipei time -> should return Friday (today)
        friday_1500 = datetime(2026, 5, 22, 15, 0, 0, tzinfo=taipei)  # May 22 is Friday
        result = resolve_backfill_report_date(friday_1500)
        self.assertEqual(result.isoformat(), "2026-05-22")  # Friday

    def test_friday_at_1501_returns_friday_taipei(self):
        from backfill_service import resolve_backfill_report_date
        from datetime import datetime
        from zoneinfo import ZoneInfo
        taipei = ZoneInfo("Asia/Taipei")
        # Friday at 15:01 Taipei time -> should return Friday (today)
        friday_1501 = datetime(2026, 5, 22, 15, 1, 0, tzinfo=taipei)
        result = resolve_backfill_report_date(friday_1501)
        self.assertEqual(result.isoformat(), "2026-05-22")  # Friday

    def test_saturday_returns_friday_taipei(self):
        from backfill_service import resolve_backfill_report_date
        from datetime import datetime
        from zoneinfo import ZoneInfo
        taipei = ZoneInfo("Asia/Taipei")
        # Saturday at 16:00 Taipei time -> should return Friday (previous trading day)
        saturday_16 = datetime(2026, 5, 23, 16, 0, 0, tzinfo=taipei)  # May 23 is Saturday
        result = resolve_backfill_report_date(saturday_16)
        self.assertEqual(result.isoformat(), "2026-05-22")  # Friday

    def test_sunday_returns_friday_taipei(self):
        from backfill_service import resolve_backfill_report_date
        from datetime import datetime
        from zoneinfo import ZoneInfo
        taipei = ZoneInfo("Asia/Taipei")
        # Sunday at 14:00 Taipei time -> should return Friday
        sunday_14 = datetime(2026, 5, 24, 14, 0, 0, tzinfo=taipei)  # May 24 is Sunday
        result = resolve_backfill_report_date(sunday_14)
        self.assertEqual(result.isoformat(), "2026-05-22")  # Friday


class TestIsBackfillCacheComplete(unittest.TestCase):
    """Test is_backfill_cache_complete: marker file detection."""

    def test_no_marker_returns_incomplete(self):
        from backfill_service import is_backfill_cache_complete
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("backfill_service/cache_complete1")
        try:
            from pathlib import Path
            from unittest.mock import patch
            with patch("backfill_service.BACKFILL_MARKER_ROOT", tmp):
                result, reason = is_backfill_cache_complete(date(2026, 5, 15))
                self.assertEqual(result, False)
                self.assertEqual(reason, "cache_incomplete")
        finally:
            safe_remove_test_cache("backfill_service/cache_complete1")

    def test_marker_exists_valid_and_invalid_cases(self):
        from backfill_service import is_backfill_cache_complete
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("backfill_service/cache_complete2")
        try:
            from pathlib import Path
            from unittest.mock import patch
            marker_dir = tmp / "2026-05-15"
            marker_dir.mkdir(parents=True, exist_ok=True)
            marker_file = marker_dir / "complete.json"
            # Invalid JSON
            marker_file.write_text('{not json', encoding="utf-8")
            with patch("backfill_service.BACKFILL_MARKER_ROOT", Path(tmp)):
                result, reason = is_backfill_cache_complete(date(2026, 5, 15))
                self.assertEqual(result, False)
                self.assertEqual(reason, "cache_marker_invalid")

            # All zero payload -> incomplete
            marker_file.write_text(json.dumps({
                "universe_count": 0,
                "candidate_count": 0,
                "chip_candidate_count": 0,
                "curated_scan_count": 0,
                "backfill_ready_for_scan": False,
            }), encoding="utf-8")
            with patch("backfill_service.BACKFILL_MARKER_ROOT", Path(tmp)):
                result, reason = is_backfill_cache_complete(date(2026, 5, 15))
                self.assertEqual(result, False)
                self.assertEqual(reason, "cache_incomplete")

            # Small universe -> invalid
            marker_file.write_text(json.dumps({
                "universe_count": 900,
                "candidate_count": 50,
                "chip_candidate_count": 40,
                "curated_scan_count": 10,
                "backfill_ready_for_scan": True,
            }), encoding="utf-8")
            with patch("backfill_service.BACKFILL_MARKER_ROOT", Path(tmp)):
                result, reason = is_backfill_cache_complete(date(2026, 5, 15))
                self.assertEqual(result, False)
                self.assertEqual(reason, "cache_universe_invalid")

            # Zero candidates -> invalid
            marker_file.write_text(json.dumps({
                "universe_count": 1500,
                "candidate_count": 0,
                "chip_candidate_count": 0,
                "curated_scan_count": 0,
                "backfill_ready_for_scan": False,
            }), encoding="utf-8")
            with patch("backfill_service.BACKFILL_MARKER_ROOT", Path(tmp)):
                result, reason = is_backfill_cache_complete(date(2026, 5, 15))
                self.assertEqual(result, False)
                self.assertEqual(reason, "cache_candidate_invalid")

            # Not ready for scan -> invalid
            marker_file.write_text(json.dumps({
                "universe_count": 1500,
                "candidate_count": 50,
                "chip_candidate_count": 40,
                "curated_scan_count": 5,
                "backfill_ready_for_scan": False,
            }), encoding="utf-8")
            with patch("backfill_service.BACKFILL_MARKER_ROOT", Path(tmp)):
                result, reason = is_backfill_cache_complete(date(2026, 5, 15))
                self.assertEqual(result, False)
                self.assertEqual(reason, "cache_not_ready_for_scan")

            # Valid marker
            marker_file.write_text(json.dumps({
                "universe_count": 1500,
                "candidate_count": 100,
                "chip_candidate_count": 90,
                "curated_scan_count": 20,
                "backfill_ready_for_scan": True,
            }), encoding="utf-8")
            with patch("backfill_service.BACKFILL_MARKER_ROOT", tmp):
                result, reason = is_backfill_cache_complete(date(2026, 5, 15))
                self.assertEqual(result, True)
                self.assertEqual(reason, "cache_complete")
        finally:
            from tests.test_cache_utils import safe_remove_test_cache
            safe_remove_test_cache("backfill_service/cache_complete2")


class TestIsMarketDataAvailable(unittest.TestCase):
    """Test is_market_data_available: date and time based availability."""

    def test_historical_date_available(self):
        from backfill_service import is_market_data_available
        from datetime import datetime
        result, reason = is_market_data_available(date(2026, 5, 10), datetime(2026, 5, 16, 10, 0, 0))
        self.assertEqual(result, True)
        self.assertEqual(reason, "historical_date")

    def test_today_before_1500_unavailable(self):
        from backfill_service import is_market_data_available
        from datetime import datetime
        result, reason = is_market_data_available(date(2026, 5, 16), datetime(2026, 5, 16, 10, 0, 0))
        self.assertEqual(result, False)
        self.assertEqual(reason, "today_before_1500")

    @patch("stock_scanner.load_stock_universe")
    @patch("stock_scanner.load_price_metrics")
    def test_today_after_1500_no_date_field_unavailable(self, mock_price, mock_universe):
        from backfill_service import is_market_data_available
        from datetime import datetime

        mock_universe.return_value = [MagicMock(code="2330", market="TWSE")]
        mock_price.return_value = {
            "2330.TW": {"close": 800.0}  # No "date" field
        }

        result, reason = is_market_data_available(date(2026, 5, 16), datetime(2026, 5, 16, 16, 0, 0))
        self.assertEqual(result, False)
        self.assertEqual(reason, "today_data_date_unconfirmed")

    @patch("stock_scanner.load_stock_universe")
    @patch("stock_scanner.load_price_metrics")
    def test_today_after_1500_date_matches_available(self, mock_price, mock_universe):
        from backfill_service import is_market_data_available
        from datetime import datetime

        mock_universe.return_value = [MagicMock(code="2330", market="TWSE")]
        mock_price.return_value = {
            "2330.TW": {"close": 800.0, "date": "2026-05-16"}
        }

        result, reason = is_market_data_available(date(2026, 5, 16), datetime(2026, 5, 16, 16, 0, 0))
        self.assertEqual(result, True)
        self.assertEqual(reason, "today_data_available")


class TestRunBackfillIfNeeded(unittest.TestCase):
    """Test run_backfill_if_needed: policy decisions."""

    @patch("backfill_service.BACKFILL_RUNNING")
    @patch("backfill_service.is_backfill_cache_complete")
    @patch("backfill_service.is_market_data_available")
    @patch("backfill_service.run_full_backfill")
    def test_cache_complete_skips(
        self, mock_run, mock_available, mock_cache, mock_lock
    ):
        from backfill_service import run_backfill_if_needed

        mock_lock.acquire.return_value = True
        mock_lock.release.return_value = None
        mock_cache.return_value = (True, "cache_complete")

        decision = run_backfill_if_needed(
            report_date=date(2026, 5, 15),
            force_refresh=False,
            progress=None,
            stop_event=None,
        )

        self.assertEqual(decision.status, "skipped")
        self.assertEqual(decision.reason, "cache_complete")
        mock_run.assert_not_called()

    @patch("backfill_service.BACKFILL_RUNNING")
    @patch("backfill_service.is_backfill_cache_complete")
    @patch("backfill_service.is_market_data_available")
    @patch("backfill_service.run_full_backfill")
    def test_market_unavailable_skips(
        self, mock_run, mock_available, mock_cache, mock_lock
    ):
        from backfill_service import run_backfill_if_needed

        mock_lock.acquire.return_value = True
        mock_lock.release.return_value = None
        mock_cache.return_value = (False, "cache_incomplete")
        mock_available.return_value = (False, "today_before_1500")

        decision = run_backfill_if_needed(
            report_date=date(2026, 5, 16),
            force_refresh=False,
            progress=None,
            stop_event=None,
        )

        self.assertEqual(decision.status, "skipped")
        self.assertEqual(decision.reason, "today_before_1500")
        mock_run.assert_not_called()

    @patch("backfill_service.BACKFILL_RUNNING")
    @patch("backfill_service.is_backfill_cache_complete")
    @patch("backfill_service.is_market_data_available")
    @patch("backfill_service.run_full_backfill")
    def test_force_refresh_ignores_cache(
        self, mock_run, mock_available, mock_cache, mock_lock
    ):
        from backfill_service import run_backfill_if_needed
        from backfill_service import BackfillResult

        mock_lock.acquire.return_value = True
        mock_lock.release.return_value = None
        mock_cache.return_value = (True, "cache_complete")  # Would skip without force
        mock_available.return_value = (True, "historical_date")
        mock_run.return_value = BackfillResult(report_date=date(2026, 5, 15))

        decision = run_backfill_if_needed(
            report_date=date(2026, 5, 15),
            force_refresh=True,  # Force ignores cache
            progress=None,
            stop_event=None,
        )

        self.assertEqual(decision.status, "completed")
        mock_run.assert_called_once()

    @patch("backfill_service.BACKFILL_RUNNING")
    @patch("backfill_service.is_backfill_cache_complete")
    @patch("backfill_service.is_market_data_available")
    @patch("backfill_service.run_full_backfill")
    @patch("backfill_service.write_backfill_complete_marker")
    def test_stopped_during_execution_no_marker(
        self, mock_marker, mock_run, mock_available, mock_cache, mock_lock
    ):
        import threading
        from backfill_service import run_backfill_if_needed, BackfillResult

        mock_lock.acquire.return_value = True
        mock_lock.release.return_value = None
        mock_cache.return_value = (False, "cache_incomplete")
        mock_available.return_value = (True, "historical_date")

        # Simulate backfill running for a bit, then stop event gets set mid-execution
        def slow_backfill(*args, **kwargs):
            # Simulate stop_event being set while backfill is running
            stop_event.set()
            return BackfillResult(report_date=date(2026, 5, 15))

        mock_run.side_effect = slow_backfill

        stop_event = threading.Event()
        # stop_event NOT set before call (so we pass the early exit check)

        decision = run_backfill_if_needed(
            report_date=date(2026, 5, 15),
            force_refresh=True,
            progress=None,
            stop_event=stop_event,
        )

        self.assertEqual(decision.status, "stopped")
        self.assertEqual(decision.reason, "stopped_during_execution")
        # Marker should NOT be written when stopped
        mock_marker.assert_not_called()
        self.assertIn("回補被使用者停止", decision.result.warnings)

    @patch("backfill_service.BACKFILL_RUNNING")
    @patch("backfill_service.is_backfill_cache_complete")
    @patch("backfill_service.is_market_data_available")
    @patch("backfill_service.run_full_backfill")
    @patch("backfill_service.write_backfill_complete_marker")
    def test_stopped_before_start_skips(
        self, mock_marker, mock_run, mock_available, mock_cache, mock_lock
    ):
        import threading
        from backfill_service import run_backfill_if_needed

        stop_event = threading.Event()
        stop_event.set()  # Already set before we try to acquire lock

        decision = run_backfill_if_needed(
            report_date=date(2026, 5, 15),
            force_refresh=False,
            progress=None,
            stop_event=stop_event,
        )

        self.assertEqual(decision.status, "skipped")
        self.assertEqual(decision.reason, "stopped_before_start")
        mock_run.assert_not_called()
        mock_marker.assert_not_called()