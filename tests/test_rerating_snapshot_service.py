"""tests/test_rerating_snapshot_service.py - 價值重估底稿服務最小測試（純 unittest）。"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import pandas as pd


def _build_mock_entry(code="2330", name="測試股票", symbol="2330", industry="半導體"):
    e = MagicMock()
    e.code = code
    e.name = name
    e.symbol = symbol
    e.industry = industry
    return e


def _mock_revenue_point(revenue=1000000, yoy=20.0):
    p = MagicMock()
    p.revenue = revenue
    p.yoy = yoy
    return p


class ReratingSnapshotServiceTests(unittest.TestCase):
    """直接呼叫 build_rerating_snapshot_for_stock() 的最小測試。"""

    def setUp(self):
        self.patchers = []
        self.mocks = {}

        # Ordered list of (target_string, mock_name) so patch targets are always strings
        targets = [
            ("research_center.rerating_snapshot_service.load_stock_universe", "load_stock_universe"),
            ("research_center.rerating_snapshot_service.load_recent_revenue_history", "load_recent_revenue_history"),
            ("research_center.rerating_snapshot_service.load_price_metrics_with_fallback", "load_price_metrics_with_fallback"),
            ("research_center.rerating_snapshot_service.build_free_research_sources", "build_free_research_sources"),
            ("research_center.rerating_snapshot_service.build_chip_backup_snapshot", "build_chip_backup_snapshot"),
            ("research_center.rerating_snapshot_service.build_mops_reference_events", "build_mops_reference_events"),
            ("research_center.rerating_snapshot_service.build_chip_backup_events", "build_chip_backup_events"),
            ("research_center.rerating_snapshot_service.enrich_company_rows", "enrich_company_rows"),
            ("research_center.rerating_snapshot_service.build_value_cross_validation", "build_value_cross_validation"),
            # StockDataFetcher is a delayed import inside the function
            ("data_fetcher.StockDataFetcher", "StockDataFetcher"),
        ]

        for target_str, mock_name in targets:
            p = patch(target_str)
            mock = p.start()
            self.patchers.append(p)
            self.mocks[mock_name] = mock

        entry = _build_mock_entry()
        self.mocks["load_stock_universe"].return_value = [entry]
        self.mocks["load_recent_revenue_history"].return_value = {"2330": [_mock_revenue_point()]}
        self.mocks["load_price_metrics_with_fallback"].return_value = (
            {"2330": {"price": 800.0, "avg_volume_20d": 5000.0}},
            "live",
        )
        self.mocks["build_free_research_sources"].return_value = {
            "valuation": {"tdcc_score": 50.0, "valuation_score": 40.0},
            "tdcc": {"status": "mock"},
            "gross_margin_cache": {},
            "mops_documents": {},
        }
        self.mocks["build_chip_backup_snapshot"].return_value = {}
        self.mocks["build_mops_reference_events"].return_value = []
        self.mocks["build_chip_backup_events"].return_value = []
        # Fixed verification_score so composite formula is predictable
        self.mocks["build_value_cross_validation"].return_value = {"verification_score": 40.0}

        m_fetcher_instance = MagicMock()
        m_meta = MagicMock()
        m_meta.code = "2330"
        m_meta.symbol = "2330"
        m_fetcher_instance.resolve_stock.return_value = m_meta
        m_fetcher_instance.fetch_quarterly_financials.return_value = pd.DataFrame()
        self.mocks["StockDataFetcher"].return_value.__enter__.return_value = m_fetcher_instance

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def test_import_succeeds(self):
        """驗證模組可匯入，無 NameError。"""
        from research_center.rerating_snapshot_service import build_rerating_snapshot_for_stock
        self.assertTrue(callable(build_rerating_snapshot_for_stock))

    def test_returns_required_fields(self):
        """驗證回傳欄位包含所有必要鍵值。"""
        from research_center.rerating_snapshot_service import build_rerating_snapshot_for_stock

        result = build_rerating_snapshot_for_stock("2330")

        required_keys = [
            "stock_id", "stock_name", "rerating_score",
            "verification_score", "tdcc_score", "valuation_score",
            "local_rerating_composite_score",
            "old_market_label", "new_market_label",
            "rerating_evidence", "counter_evidence",
            "data_gaps", "source_coverage",
        ]
        for key in required_keys:
            self.assertIn(key, result, msg=f"缺少欄位: {key}")

    def test_stock_id_and_name(self):
        """驗證 stock_id 與 stock_name 正確。"""
        from research_center.rerating_snapshot_service import build_rerating_snapshot_for_stock

        result = build_rerating_snapshot_for_stock("2330")
        self.assertEqual(result["stock_id"], "2330")
        self.assertEqual(result["stock_name"], "測試股票")

    def test_composite_formula_weights(self):
        """驗證複合分數 = rerating*0.6 + verification*0.25 + tdcc*0.1 + valuation*0.05。"""
        from research_center.rerating_snapshot_service import build_rerating_snapshot_for_stock

        result = build_rerating_snapshot_for_stock("2330")

        rerating = result["rerating_score"]
        verification = result["verification_score"]
        tdcc = result["tdcc_score"]
        valuation = result["valuation_score"]
        composite = result["local_rerating_composite_score"]

        expected = round(
            max(
                0,
                min(
                    100,
                    rerating * 0.6 + verification * 0.25 + tdcc * 0.1 + valuation * 0.05,
                ),
            ),
            2,
        )
        self.assertAlmostEqual(
            composite, expected, places=2,
            msg=(
                f"複合分數公式錯誤：composite={composite}, expected={expected} "
                f"(rerating={rerating}, verification={verification}, tdcc={tdcc}, valuation={valuation})"
            ),
        )

    def test_composite_not_equal_to_rerating_score_alone(self):
        """驗證複合分數不是單純等於 rerating_score（差異應來自其他權重）。"""
        from research_center.rerating_snapshot_service import build_rerating_snapshot_for_stock

        result = build_rerating_snapshot_for_stock("2330")
        self.assertNotEqual(
            result["local_rerating_composite_score"],
            result["rerating_score"],
            msg="local_rerating_composite_score 不应等于 rerating_score，公式應含其他權重",
        )


if __name__ == "__main__":
    unittest.main()