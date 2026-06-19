"""tests/test_value_scan_evidence_pack.py - ai_candidates 排序時機與 evidence pack 欄位完整性測試（純 unittest）。"""
from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pandas as pd


def _mock_entry(code="2330", name="測試股票", symbol="2330", industry="半導體"):
    e = MagicMock()
    e.code = code
    e.name = name
    e.symbol = symbol
    e.industry = industry
    return e


def _mock_revenue_point(revenue=1000000.0, yoy=20.0):
    p = MagicMock()
    p.revenue = revenue
    p.yoy = yoy
    return p


class ValueScanEvidencePackTests(unittest.TestCase):
    def test_early_signal_priority_pool_sort_policy_helper(self):
        from research_center.data_services import (
            _value_scan_early_signal_priority,
            _value_scan_should_preserve_early_candidates,
        )

        policy = {"source": "精選選股交叉命中快取"}
        row = {
            "revenue_yoy": 35,
            "avg_volume_20d": 1000,
            "score_components": {"theme_label_shift": 1},
            "old_market_label": "old",
            "new_market_label": "new",
            "price": 50,
            "rerating_evidence": ["營收轉強", "新市場標籤"],
        }

        self.assertTrue(_value_scan_should_preserve_early_candidates(policy))
        self.assertGreater(_value_scan_early_signal_priority(row, policy), 0)

    def test_early_signal_priority_preserves_radar_pool(self):
        from research_center.data_services import _value_scan_should_preserve_early_candidates

        self.assertTrue(_value_scan_should_preserve_early_candidates({"source": "選股雷達"}))

    """測試 ai_candidates 排序時機與 evidence pack 欄位完整性。

    使用「全市場初篩」避免精選選股流程需要真實 I/O。
    """

    def setUp(self):
        self.patchers = []
        self.mocks = {}

        targets = [
            ("research_center.data_services.load_stock_universe", "load_stock_universe"),
            ("research_center.data_services.load_recent_revenue_history", "load_recent_revenue_history"),
            ("research_center.data_services.load_price_metrics_with_fallback", "load_price_metrics_with_fallback"),
            ("research_center.data_services.build_free_research_sources", "build_free_research_sources"),
            ("research_center.data_services.build_chip_backup_snapshot", "build_chip_backup_snapshot"),
            ("research_center.data_services.build_mops_reference_events", "build_mops_reference_events"),
            ("research_center.data_services.build_chip_backup_events", "build_chip_backup_events"),
            ("research_center.data_services.enrich_company_rows", "enrich_company_rows"),
            ("research_center.knowledge_base.load_company_knowledge", "load_company_knowledge"),
            ("research_center.data_services.build_value_cross_validation", "build_value_cross_validation"),
            ("data_fetcher.StockDataFetcher", "StockDataFetcher"),
        ]
        for target, name in targets:
            p = patch(target)
            self.patchers.append(p)
            self.mocks[name] = p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    # ------------------------------------------------------------------
    # ai_candidates 排序時機 + source_events
    # ------------------------------------------------------------------

    def test_ai_candidates_after_cross_validation(self):
        """驗證 ai_candidates 在 cross_validation 後才建立。"""
        entry_a = _mock_entry(code="A", name="股票A", symbol="A", industry="半導體")
        entry_b = _mock_entry(code="B", name="股票B", symbol="B", industry="半導體")

        def mock_load_universe(_):
            return [entry_a, entry_b]

        def mock_revenue_history(_):
            return {
                "A": [_mock_revenue_point(revenue=1000000.0, yoy=20.0)],
                "B": [_mock_revenue_point(revenue=1000000.0, yoy=20.0)],
            }

        def mock_price_metrics(*args, **kwargs):
            return (
                {
                    "A": {"price": 100.0, "avg_volume_20d": 1000.0},
                    "B": {"price": 100.0, "avg_volume_20d": 1000.0},
                },
                {"status": "mock"},
            )

        def mock_enrich(rows):
            return rows

        def mock_cross_validation(row, events):
            if row["code"] == "A":
                return {"verification_score": 30.0, "tdcc_score": 70.0, "valuation_score": 70.0, "source_coverage": []}
            else:
                return {"verification_score": 90.0, "tdcc_score": 70.0, "valuation_score": 70.0, "source_coverage": []}

        fetcher_mock = MagicMock()
        fetcher_mock.resolve_stock.return_value = MagicMock()
        fetcher_mock.fetch_quarterly_financials.return_value = pd.DataFrame()

        self.mocks["load_stock_universe"].side_effect = mock_load_universe
        self.mocks["load_recent_revenue_history"].side_effect = mock_revenue_history
        self.mocks["load_price_metrics_with_fallback"].side_effect = mock_price_metrics
        self.mocks["enrich_company_rows"].side_effect = mock_enrich
        self.mocks["build_value_cross_validation"].side_effect = mock_cross_validation
        self.mocks["build_free_research_sources"].return_value = {}
        self.mocks["build_chip_backup_snapshot"].return_value = {}
        self.mocks["build_mops_reference_events"].return_value = []
        self.mocks["build_chip_backup_events"].return_value = []
        self.mocks["load_company_knowledge"].return_value = {"companies": {}}
        self.mocks["StockDataFetcher"].return_value.__enter__.return_value = fetcher_mock

        from research_center.data_services import collect_value_scan_data
        from research_center.command_parser import parse_command_text

        request = parse_command_text("/value_scan 全市場初篩 --deep --date 2026-05-15")
        result = collect_value_scan_data(request)

        ai_candidates = result["ai_candidates"]
        self.assertEqual(len(ai_candidates), 2)
        for c in ai_candidates:
            self.assertIn("verification_score", c)

    def test_deep_mode_default_30_candidates(self):
        """deep 模式未指定 --top 時，最多 30 檔。"""
        entries = [_mock_entry(code=str(i), name=f"股票{i}", symbol=str(i), industry="半導體") for i in range(5)]

        def mock_load_universe(_):
            return entries

        def mock_revenue_history(_):
            return {str(i): [_mock_revenue_point()] for i in range(5)}

        def mock_price_metrics(*args, **kwargs):
            return (
                {str(i): {"price": 100.0, "avg_volume_20d": 1000.0} for i in range(5)},
                {"status": "mock"},
            )

        def mock_enrich(rows):
            return rows

        def mock_cross_validation(row, events):
            return {"verification_score": 70.0, "tdcc_score": 70.0, "valuation_score": 70.0, "source_coverage": []}

        fetcher_mock = MagicMock()
        fetcher_mock.resolve_stock.return_value = MagicMock()
        fetcher_mock.fetch_quarterly_financials.return_value = pd.DataFrame()

        self.mocks["load_stock_universe"].side_effect = mock_load_universe
        self.mocks["load_recent_revenue_history"].side_effect = mock_revenue_history
        self.mocks["load_price_metrics_with_fallback"].side_effect = mock_price_metrics
        self.mocks["enrich_company_rows"].side_effect = mock_enrich
        self.mocks["build_value_cross_validation"].side_effect = mock_cross_validation
        self.mocks["build_free_research_sources"].return_value = {}
        self.mocks["build_chip_backup_snapshot"].return_value = {}
        self.mocks["build_mops_reference_events"].return_value = []
        self.mocks["build_chip_backup_events"].return_value = []
        self.mocks["load_company_knowledge"].return_value = {"companies": {}}
        self.mocks["StockDataFetcher"].return_value.__enter__.return_value = fetcher_mock

        from research_center.data_services import collect_value_scan_data
        from research_center.command_parser import parse_command_text

        request = parse_command_text("/value_scan 全市場初篩 --deep --date 2026-05-15")
        result = collect_value_scan_data(request)

        self.assertEqual(len(result["ai_candidates"]), 5)
        self.assertEqual(result["ai_candidate_limit"], 30)

    def test_pool_15_deep_sends_15(self):
        """候選池 15 檔時，deep 應送 15 檔。"""
        entries = [_mock_entry(code=str(i), name=f"股票{i}", symbol=str(i), industry="半導體") for i in range(15)]

        def mock_load_universe(_):
            return entries

        def mock_revenue_history(_):
            return {str(i): [_mock_revenue_point()] for i in range(15)}

        def mock_price_metrics(*args, **kwargs):
            return (
                {str(i): {"price": 100.0, "avg_volume_20d": 1000.0} for i in range(15)},
                {"status": "mock"},
            )

        def mock_enrich(rows):
            return rows

        def mock_cross_validation(row, events):
            return {"verification_score": 70.0, "tdcc_score": 70.0, "valuation_score": 70.0, "source_coverage": []}

        fetcher_mock = MagicMock()
        fetcher_mock.resolve_stock.return_value = MagicMock()
        fetcher_mock.fetch_quarterly_financials.return_value = pd.DataFrame()

        self.mocks["load_stock_universe"].side_effect = mock_load_universe
        self.mocks["load_recent_revenue_history"].side_effect = mock_revenue_history
        self.mocks["load_price_metrics_with_fallback"].side_effect = mock_price_metrics
        self.mocks["enrich_company_rows"].side_effect = mock_enrich
        self.mocks["build_value_cross_validation"].side_effect = mock_cross_validation
        self.mocks["build_free_research_sources"].return_value = {}
        self.mocks["build_chip_backup_snapshot"].return_value = {}
        self.mocks["build_mops_reference_events"].return_value = []
        self.mocks["build_chip_backup_events"].return_value = []
        self.mocks["load_company_knowledge"].return_value = {"companies": {}}
        self.mocks["StockDataFetcher"].return_value.__enter__.return_value = fetcher_mock

        from research_center.data_services import collect_value_scan_data
        from research_center.command_parser import parse_command_text

        request = parse_command_text("/value_scan 全市場初篩 --deep --date 2026-05-15")
        result = collect_value_scan_data(request)

        self.assertEqual(len(result["ai_candidates"]), 15)
        self.assertEqual(result["ai_candidate_limit"], 30)

    def test_source_events_from_ai_candidates(self):
        """驗證 source_events 從最終 ai_candidates 彙整。"""
        entries = [_mock_entry(code=str(i), name=f"股票{i}", symbol=str(i), industry="半導體") for i in range(3)]

        def mock_load_universe(_):
            return entries

        def mock_revenue_history(_):
            return {str(i): [_mock_revenue_point()] for i in range(3)}

        def mock_price_metrics(*args, **kwargs):
            return (
                {str(i): {"price": 100.0, "avg_volume_20d": 1000.0} for i in range(3)},
                {"status": "mock"},
            )

        def mock_enrich(rows):
            return rows

        def mock_cross_validation(row, events):
            return {"verification_score": 70.0, "tdcc_score": 70.0, "valuation_score": 70.0, "source_coverage": []}

        def mock_free_sources(code, symbol, date):
            return {}

        def mock_chip_snapshot(code, date):
            return {}

        def mock_mops_events(code, date):
            return [{"source": "mops", "code": code}]

        def mock_chip_events(code, date):
            return [{"source": "chip", "code": code}]

        fetcher_mock = MagicMock()
        fetcher_mock.resolve_stock.return_value = MagicMock()
        fetcher_mock.fetch_quarterly_financials.return_value = pd.DataFrame()

        self.mocks["load_stock_universe"].side_effect = mock_load_universe
        self.mocks["load_recent_revenue_history"].side_effect = mock_revenue_history
        self.mocks["load_price_metrics_with_fallback"].side_effect = mock_price_metrics
        self.mocks["enrich_company_rows"].side_effect = mock_enrich
        self.mocks["build_value_cross_validation"].side_effect = mock_cross_validation
        self.mocks["build_free_research_sources"].side_effect = mock_free_sources
        self.mocks["build_chip_backup_snapshot"].side_effect = mock_chip_snapshot
        self.mocks["build_mops_reference_events"].side_effect = mock_mops_events
        self.mocks["build_chip_backup_events"].side_effect = mock_chip_events
        self.mocks["load_company_knowledge"].return_value = {"companies": {}}
        self.mocks["StockDataFetcher"].return_value.__enter__.return_value = fetcher_mock

        from research_center.data_services import collect_value_scan_data
        from research_center.command_parser import parse_command_text

        request = parse_command_text("/value_scan 全市場初篩 --deep --date 2026-05-15")
        result = collect_value_scan_data(request)

        source_events = result["source_events"]
        ai_codes = {c["code"] for c in result["ai_candidates"]}

        for event in source_events:
            code = event.get("code", "")
            self.assertIn(code, ai_codes, f"source_events 中的 {code} 應該來自 ai_candidates")

    # ------------------------------------------------------------------
    # evidence pack 欄位完整性測試
    # ------------------------------------------------------------------

    def test_evidence_pack_has_all_required_fields(self):
        """驗證 evidence pack 每檔包含所有必要欄位。"""
        from research_center.data_services import _build_ai_candidate_evidence_pack

        row = {
            "code": "2330",
            "name": "台積電",
            "symbol": "2330",
            "industry": "半導體",
            "price": 100.0,
            "avg_volume_20d": 5000.0,
            "latest_monthly_revenue": 2000000.0,
            "revenue_yoy": 15.0,
            "old_market_label": "低價",
            "new_market_label": "中高價",
            "rerating_score": 85.0,
            "verification_score": 80.0,
            "rerating_evidence": ["證據1", "證據2"],
            "counter_evidence": ["反證1"],
            "score_components": {"quality": 90, "value": 80},
            "cross_validation": {
                "verification_score": 80.0,
                "tdcc_score": 75.0,
                "valuation_score": 70.0,
                "source_coverage": ["source1"],
            },
            "financial_detail": {"Q1": 100, "Q2": 110},
            "gross_margin_cache": {"gross_margin": 50.0},
            "chip_backup_data": {"top3_holders": ["A", "B", "C"], "holding_ratio": 60.0, "total_shares": 1000000},
            "valuation_data": {"pe": 20.0},
            "tdcc_data": {"tdcc": 30.0},
            "mops_documents": [{"doc": "1"}],
            "source_events": [{"event": "event1"}],
            "company_knowledge": {"note": "test"},
        }

        pack = _build_ai_candidate_evidence_pack([row])

        self.assertEqual(len(pack), 1)
        item = pack[0]

        required_fields = [
            "code", "name", "symbol", "industry", "price", "avg_volume_20d",
            "latest_monthly_revenue", "revenue_yoy",
            "old_market_label", "new_market_label",
            "rerating_score", "verification_score", "local_rerating_composite_score",
            "tdcc_score", "valuation_score",
            "rerating_evidence", "counter_evidence", "score_components", "cross_validation",
            "financial_detail", "gross_margin_cache", "chip_backup_summary",
            "valuation_data", "tdcc_data", "mops_documents",
            "source_events", "company_knowledge", "source_coverage",
            "missing_data_status",
        ]
        for field in required_fields:
            self.assertIn(field, item, f"欄位 {field} 應該存在於 evidence pack")

    def test_evidence_pack_missing_data_status_fields(self):
        """驗證缺失資料時 missing_data_status 包含正確欄位。"""
        from research_center.data_services import _build_ai_candidate_evidence_pack

        row = {
            "code": "2330",
            "name": "台積電",
            "rerating_score": 85.0,
        }

        pack = _build_ai_candidate_evidence_pack([row])
        missing = pack[0]["missing_data_status"]

        self.assertIsInstance(missing, list)
        self.assertIn("financial_detail", missing)
        self.assertIn("gross_margin_cache", missing)
        self.assertIn("chip_backup_data", missing)
        self.assertIn("revenue", missing)
        self.assertIn("mops_documents", missing)
        self.assertIn("source_events", missing)
        self.assertIn("company_knowledge", missing)

    def test_evidence_pack_chip_summary_structure(self):
        """驗證晶片資料過大時使用摘要模式。"""
        from research_center.data_services import _build_ai_candidate_evidence_pack

        large_chip = {"data": "x" * 3000, "top3_holders": ["A", "B", "C"], "holding_ratio": 60.0, "total_shares": 1000000}
        row = {
            "code": "2330",
            "name": "台積電",
            "rerating_score": 85.0,
            "chip_backup_data": large_chip,
        }

        pack = _build_ai_candidate_evidence_pack([row])
        chip_summary = pack[0]["chip_backup_summary"]

        self.assertIn("_note", chip_summary)
        self.assertIn("holder_count", chip_summary)

    def test_evidence_pack_no_data_has_status(self):
        """驗證無任何資料時，仍有 status 而不是整個欄位消失。"""
        from research_center.data_services import _build_ai_candidate_evidence_pack

        row = {
            "code": "2330",
            "name": "台積電",
            "rerating_score": 85.0,
        }

        pack = _build_ai_candidate_evidence_pack([row])
        item = pack[0]

        self.assertEqual(item["financial_detail"], {"status": "unavailable"})
        self.assertEqual(item["gross_margin_cache"], {})
        self.assertEqual(item["chip_backup_summary"], {"status": "no data"})
        self.assertEqual(item["valuation_data"], {})
        self.assertEqual(item["tdcc_data"], {})
        self.assertEqual(item["mops_documents"], {})

    def test_local_rerating_composite_score_formula(self):
        """驗證 local_rerating_composite_score 使用 60/25/10/5 公式。"""
        from research_center.data_services import _build_ai_candidate_evidence_pack

        row = {
            "code": "2330",
            "name": "台積電",
            "rerating_score": 80.0,
            "verification_score": 60.0,
            "cross_validation": {
                "tdcc_score": 50.0,
                "valuation_score": 40.0,
            },
        }

        pack = _build_ai_candidate_evidence_pack([row])
        composite = pack[0]["local_rerating_composite_score"]

        # 80*0.6 + 60*0.25 + 50*0.1 + 40*0.05 = 48 + 15 + 5 + 2 = 70
        self.assertEqual(composite, 70.0)

    def test_local_rerating_composite_score_bounded(self):
        """驗證 composite 分數限制在 0～100 之間。"""
        from research_center.data_services import _build_ai_candidate_evidence_pack

        row = {
            "code": "2330",
            "name": "台積電",
            "rerating_score": 100.0,
            "verification_score": 100.0,
            "cross_validation": {
                "tdcc_score": 100.0,
                "valuation_score": 100.0,
            },
        }

        pack = _build_ai_candidate_evidence_pack([row])
        composite = pack[0]["local_rerating_composite_score"]

        self.assertEqual(composite, 100.0)

        row_zero = {
            "code": "2330",
            "name": "台積電",
            "rerating_score": 0.0,
            "verification_score": 0.0,
            "cross_validation": {
                "tdcc_score": 0.0,
                "valuation_score": 0.0,
            },
        }

        pack_zero = _build_ai_candidate_evidence_pack([row_zero])
        composite_zero = pack_zero[0]["local_rerating_composite_score"]

        self.assertEqual(composite_zero, 0.0)

    def test_value_scan_universe_uses_latest_radar_cache(self):
        from research_center.command_parser import parse_command_text
        from research_center.data_services import _value_scan_universe

        universe = [
            _mock_entry("2330", "台積電", "2330.TW", "半導體"),
            _mock_entry("6282", "康舒", "6282.TW", "電源"),
        ]
        radar_result = SimpleNamespace(
            report_date=date(2026, 5, 22),
            request=SimpleNamespace(source="technical"),
            candidates=[SimpleNamespace(code="6282"), SimpleNamespace(code="2330")],
        )
        request = parse_command_text("/value_scan 選股雷達 --date 2026-05-22")

        with patch("research_center.data_services.load_stock_universe", return_value=universe), \
             patch("radar_service.load_radar_result", return_value=radar_result):
            selected, policy = _value_scan_universe(request)

        self.assertEqual([item.code for item in selected], ["6282", "2330"])
        self.assertEqual(policy["source"], "選股雷達")
        self.assertEqual(policy["radar_source"], "technical")

    def test_value_scan_universe_uses_latest_ready_curated_cache_without_rebuild(self):
        from research_center.data_services import _value_scan_universe
        from research_center.models import CommandRequest

        universe = [
            _mock_entry("2330", "TSMC", "2330.TW", "semiconductor"),
            _mock_entry("5425", "Taiwan Semi", "5425.TWO", "semiconductor"),
        ]
        request = CommandRequest(
            command="value_scan",
            raw_text="/value_scan curated --deep",
            candidate_pool="curated",
            target_type="candidate_pool",
        )
        latest_cached = {
            "scan_id": "curated-20260604",
            "report_date": "2026-06-04",
            "codes": ["5425", "2330"],
        }
        self.mocks["load_stock_universe"].return_value = universe

        with patch("research_center.data_services.find_cached_curated_scan", return_value=None), \
             patch("research_center.data_services.find_latest_cached_curated_scan", return_value=latest_cached), \
             patch("research_center.data_services.build_curated_scan_result") as build_curated:
            selected, policy = _value_scan_universe(request)

        build_curated.assert_not_called()
        self.assertEqual([item.code for item in selected], ["5425", "2330"])
        self.assertEqual(policy["status"], "latest_cached")
        self.assertEqual(policy["report_date"], "2026-06-04")
        self.assertEqual(policy["scan_id"], "curated-20260604")

    def test_value_scan_universe_uses_monitor_pool(self):
        from research_center.command_parser import parse_command_text
        from research_center.data_services import _value_scan_universe

        universe = [_mock_entry("6282", "康舒", "6282.TW", "電源")]
        request = parse_command_text("/value_scan 監控清單")

        with patch("research_center.data_services.load_stock_universe", return_value=universe), \
             patch("research_center.data_services._load_monitor_codes", return_value=["6282"]):
            selected, policy = _value_scan_universe(request)

        self.assertEqual([item.code for item in selected], ["6282"])
        self.assertEqual(policy["source"], "監控清單")

    def test_value_scan_universe_resolves_single_stock_name_pool(self):
        from research_center.command_parser import parse_command_text
        from research_center.data_services import _value_scan_universe

        universe = [_mock_entry("6282", "康舒", "6282.TW", "電源")]
        request = parse_command_text("/value_scan 康舒")
        resolved = SimpleNamespace(code="6282", name="康舒", symbol="6282.TW")

        with patch("research_center.data_services.load_stock_universe", return_value=universe), \
             patch("research_center.data_services.resolve_stock_reference", return_value=resolved):
            selected, policy = _value_scan_universe(request)

        self.assertEqual([item.code for item in selected], ["6282"])
        self.assertEqual(policy["source"], "單一股票")


if __name__ == "__main__":
    unittest.main()
