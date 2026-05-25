from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from backfill_gap_service import (
    analyze_chip_gaps,
    analyze_revenue_gaps,
    analyze_tdcc_gaps,
    analyze_technical_gaps,
    build_backfill_gap_report,
    candidates_to_rows,
    normalize_code,
    write_gap_report,
)
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class _Candidate:
    def __init__(self, code: str, symbol: str | None = None, market: str = "TWSE"):
        self.code = code
        self.symbol = symbol or f"{code}.TW"
        self.market = market
        self.name = code


class _RevenuePoint:
    def __init__(self, revenue: float | None = 100.0, yoy: float | None = 1.0):
        self.revenue = revenue
        self.yoy = yoy


class _ChipContext:
    def __init__(self, daily_data=None, weekly_data=None):
        self.daily_data = daily_data if daily_data is not None else pd.DataFrame()
        self.weekly_data = weekly_data if weekly_data is not None else pd.DataFrame()
        self.scan_settings = {"target_trading_days": 2}


class BackfillGapServiceTests(unittest.TestCase):
    def tearDown(self):
        safe_remove_test_cache("backfill_gap_service")

    def test_normalize_code_handles_suffix(self):
        self.assertEqual(normalize_code("5425.TWO"), "5425")
        self.assertEqual(normalize_code("2330.TW"), "2330")

    def test_chip_gap_reports_missing_codes_and_market_coverage(self):
        rows = candidates_to_rows({
            "2330": _Candidate("2330", market="TWSE"),
            "5425": _Candidate("5425", symbol="5425.TWO", market="TPEX"),
        })
        daily = pd.DataFrame(
            [
                {"date": "2026-05-01", "code": "2330", "market": "TWSE", "foreign_net_lots": 1, "trust_net_lots": 1, "foreign_ratio_pct": 10, "source": "cache"},
                {"date": "2026-05-02", "code": "2330", "market": "TWSE", "foreign_net_lots": 1, "trust_net_lots": 1, "foreign_ratio_pct": 10, "source": "FinMind"},
            ]
        )

        section = analyze_chip_gaps(rows, daily, target_days=2)

        self.assertEqual(section.candidate_count, 2)
        self.assertEqual(section.ready_count, 1)
        self.assertEqual(section.missing_codes, ["5425"])
        self.assertEqual(section.market_coverage["TWSE"]["coverage_pct"], 1.0)
        self.assertEqual(section.market_coverage["TPEX"]["coverage_pct"], 0.0)
        self.assertIn("FinMind", section.details["source_counts"])

    def test_revenue_gap_detects_short_history_and_missing_yoy(self):
        rows = candidates_to_rows({"2330": _Candidate("2330"), "5425": _Candidate("5425")})
        revenue = {"2330": [_RevenuePoint(yoy=None), _RevenuePoint(), _RevenuePoint()]}

        section = analyze_revenue_gaps(rows, revenue, min_months=4)

        self.assertIn("2330", section.reason_by_code)
        self.assertIn("revenue_yoy_missing", section.reason_by_code["2330"])
        self.assertIn("5425", section.missing_codes)

    def test_technical_gap_uses_cache_files(self):
        tmp = ensure_test_cache_dir("backfill_gap_service/technical")
        tech_dir = tmp / "technical_daily"
        tech_dir.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(
            {
                "date": pd.date_range("2026-01-01", periods=130),
                "open": [1] * 130,
                "high": [1] * 130,
                "low": [1] * 130,
                "close": [1] * 130,
                "volume": [100] * 130,
            }
        )
        frame.to_csv(tech_dir / "2330_TW.csv", index=False)
        rows = candidates_to_rows({"2330": _Candidate("2330"), "5425": _Candidate("5425", symbol="5425.TWO")})

        with patch("backfill_gap_service.TECH_CACHE_DIR", tech_dir):
            section = analyze_technical_gaps(rows, date(2026, 5, 20), min_rows=120)

        self.assertEqual(section.ready_count, 1)
        self.assertEqual(section.missing_codes, ["5425"])

    def test_tdcc_gap_counts_weeks(self):
        rows = candidates_to_rows({"2330": _Candidate("2330"), "5425": _Candidate("5425")})
        weekly = pd.DataFrame(
            [
                {"snapshot_date": "2026-05-01", "code": "2330", "big_holder_pct": 1},
                {"snapshot_date": "2026-05-08", "code": "2330", "big_holder_pct": 1},
            ]
        )

        section = analyze_tdcc_gaps(rows, weekly, min_weeks=2)

        self.assertEqual(section.ready_count, 1)
        self.assertIn("5425", section.missing_codes)

    def test_build_and_write_gap_report(self):
        tmp = ensure_test_cache_dir("backfill_gap_service/write")
        candidates = {"2330": _Candidate("2330"), "5425": _Candidate("5425")}
        daily = pd.DataFrame(
            [
                {"date": "2026-05-01", "code": "2330", "foreign_net_lots": 1, "trust_net_lots": 1, "foreign_ratio_pct": 1},
                {"date": "2026-05-02", "code": "2330", "foreign_net_lots": 1, "trust_net_lots": 1, "foreign_ratio_pct": 1},
            ]
        )
        report = build_backfill_gap_report(
            report_date=date(2026, 5, 20),
            candidates=candidates,
            core_pool={"2330": candidates["2330"]},
            revenue_history={"2330": [_RevenuePoint(), _RevenuePoint(), _RevenuePoint(), _RevenuePoint()]},
            chip_context=_ChipContext(daily_data=daily),
        )
        path = write_gap_report(date(2026, 5, 20), report, tmp)

        self.assertTrue(path.exists())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("health", payload)
        self.assertIn("chip", payload["still_missing"])


if __name__ == "__main__":
    unittest.main()
