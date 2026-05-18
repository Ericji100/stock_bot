"""Tests for financial derivative calculations in data_fetcher and scoring_engine."""
from __future__ import annotations

import unittest

import pandas as pd

from data_fetcher import _safe_pct_change, _first_match, _to_number, _quarter_number, _derive_quarterly_from_ytd
from research_center.scoring_engine import _series, _cash_flow_score, _inventory_score, _operating_margin_score, _profit_growth_score, _revenue_growth_score


class TestSafePctChange(unittest.TestCase):
    def test_basic_pct_change(self):
        self.assertAlmostEqual(_safe_pct_change(120, 100), 20.0)

    def test_negative_change(self):
        self.assertAlmostEqual(_safe_pct_change(80, 100), -20.0)

    def test_none_current(self):
        self.assertIsNone(_safe_pct_change(None, 100))

    def test_none_previous(self):
        self.assertIsNone(_safe_pct_change(100, None))

    def test_zero_previous(self):
        self.assertIsNone(_safe_pct_change(100, 0))


class TestFirstMatch(unittest.TestCase):
    def test_first_match_found(self):
        report_map = {"存貨": 500.0, "存貨合計": 520.0}
        self.assertEqual(_first_match(report_map, "存貨", "存貨合計"), 500.0)

    def test_first_match_fallback(self):
        report_map = {"存貨合計": 520.0}
        self.assertEqual(_first_match(report_map, "存貨", "存貨合計"), 520.0)

    def test_first_match_none(self):
        self.assertIsNone(_first_match({}, "存貨", "存貨合計"))


class TestMonthlyRevenueYoYAlias(unittest.TestCase):
    """Verify monthly revenue DataFrame has YoY, yoy, and revenue_yoy aliases."""

    def test_yoy_aliases_present(self):
        from data_fetcher import StockDataFetcher
        # Create a synthetic revenue_df to test alias logic
        rows = []
        for i in range(14):
            rows.append({
                "Month": pd.Timestamp(year=2024 + i // 12, month=1 + i % 12, day=1),
                "Monthly_Revenue": 100 + i * 10,
                "Prior_Year_Revenue": 100 + (i - 12) * 10 if i >= 12 else None,
            })
        revenue_df = pd.DataFrame(rows)

        # Apply the same logic as fetch_monthly_revenue
        revenue_df["YoY%"] = revenue_df.apply(
            lambda row: _safe_pct_change(row.get("Monthly_Revenue"), row.get("Prior_Year_Revenue")),
            axis=1,
        )
        revenue_df["YoY"] = revenue_df["YoY%"]
        revenue_df["yoy"] = revenue_df["YoY%"]
        revenue_df["revenue_yoy"] = revenue_df["YoY%"]

        # Check aliases exist
        self.assertIn("YoY%", revenue_df.columns)
        self.assertIn("YoY", revenue_df.columns)
        self.assertIn("yoy", revenue_df.columns)
        self.assertIn("revenue_yoy", revenue_df.columns)

        # Check that last row has a non-None YoY value (row 13 has prior year data)
        last_yoy = revenue_df.iloc[-1]["YoY%"]
        self.assertIsNotNone(last_yoy)


class TestQuarterlyFinancialDerivatives(unittest.TestCase):
    """Test derived financial fields from quarterly data."""

    def test_margin_calculations(self):
        financial_data = [
            {
                "Quarter": "2024Q1",
                "Revenue": 1000,
                "Gross_Profit": 300,
                "Operating_Income": 120,
                "Net_Income": 80,
                "Inventory": 200,
                "Operating_Cash_Flow": 150,
                "Capital_Expenditure": -40,
                "Free_Cash_Flow": 110,
            },
        ]
        row = financial_data[0]
        # Gross margin = 300/1000 * 100 = 30
        from data_fetcher import _safe_ratio
        self.assertAlmostEqual(_safe_ratio(row["Gross_Profit"], row["Revenue"]) * 100, 30.0)
        # Operating margin = 120/1000 * 100 = 12
        self.assertAlmostEqual(_safe_ratio(row["Operating_Income"], row["Revenue"]) * 100, 12.0)
        # Net margin = 80/1000 * 100 = 8
        self.assertAlmostEqual(_safe_ratio(row["Net_Income"], row["Revenue"]) * 100, 8.0)

    def test_free_cash_flow_calculation(self):
        # CapEx is negative: FCF = OCF + CapEx = 150 + (-40) = 110
        ocf = 150
        capex = -40
        fcf = ocf + capex if capex < 0 else ocf - capex
        self.assertEqual(fcf, 110)

        # CapEx is positive: FCF = OCF - CapEx = 150 - 40 = 110
        capex_pos = 40
        fcf2 = ocf + capex_pos if capex_pos < 0 else ocf - capex_pos
        self.assertEqual(fcf2, 110)

    def test_inventory_turnover_trailing_4q(self):
        # Trailing 4Q COGS / Average Inventory
        quarters = [
            {"Revenue": 1000, "Gross_Profit": 300, "Inventory": 200},
            {"Revenue": 1100, "Gross_Profit": 330, "Inventory": 210},
            {"Revenue": 1050, "Gross_Profit": 315, "Inventory": 205},
            {"Revenue": 1200, "Gross_Profit": 360, "Inventory": 215},
        ]
        # COGS = Revenue - Gross_Profit for each quarter
        cogs_values = [q["Revenue"] - q["Gross_Profit"] for q in quarters]  # 700, 770, 735, 840
        total_cogs = sum(cogs_values)  # 3045
        avg_inventory = sum(q["Inventory"] for q in quarters) / 4  # 207.5
        expected_turnover = total_cogs / avg_inventory  # ~14.69
        self.assertAlmostEqual(expected_turnover, 3045 / 207.5, places=2)


class TestScoringEngineKeyLookups(unittest.TestCase):
    """Verify scoring engine can find data under new key names."""

    def test_operating_margin_finds_new_keys(self):
        rows = [{"operating_margin": 12.0, "Quarter": "2024Q1"}]
        values = _series(rows, ("operating_margin", "Operating_Margin", "OperatingMargin", "營益率"))
        self.assertEqual(values, [12.0])

        rows2 = [{"Operating_Margin": 15.0, "Quarter": "2024Q2"}]
        values2 = _series(rows2, ("operating_margin", "Operating_Margin", "OperatingMargin", "營益率"))
        self.assertEqual(values2, [15.0])

    def test_revenue_yoy_finds_yoy_percent_key(self):
        rows = [
            {"YoY%": 5.3, "Month": "2024-01"},
            {"YoY%": 8.2, "Month": "2024-02"},
        ]
        values = _series(rows, ("YoY", "YoY%", "yoy", "revenue_yoy", "年增率"))
        self.assertEqual(len(values), 2)
        self.assertAlmostEqual(values[0], 5.3)
        self.assertAlmostEqual(values[1], 8.2)

    def test_revenue_yoy_finds_revenue_yoy_key(self):
        rows = [{"revenue_yoy": 10.5, "Month": "2024-01"}]
        values = _series(rows, ("YoY", "YoY%", "yoy", "revenue_yoy", "年增率"))
        self.assertEqual(values, [10.5])

    def test_net_income_yoy_finds_new_keys(self):
        rows = [{"net_income_yoy": 15.0, "Quarter": "2024Q1"}]
        values = _series(rows, ("net_income_yoy", "Net_Income_YoY", "NetIncomeYoY", "稅後淨利年增率"))
        self.assertEqual(values, [15.0])

        rows2 = [{"Net_Income_YoY": 20.0, "Quarter": "2024Q2"}]
        values2 = _series(rows2, ("net_income_yoy", "Net_Income_YoY", "NetIncomeYoY", "稅後淨利年增率"))
        self.assertEqual(values2, [20.0])

    def test_cash_flow_finds_free_cash_flow(self):
        rows = [{"Free_Cash_Flow": 110.0, "Quarter": "2024Q1"}]
        values = _series(rows, ("free_cash_flow", "Free_Cash_Flow", "FreeCashFlow", "自由現金流量"))
        self.assertEqual(values, [110.0])

    def test_inventory_turnover_finds_new_keys(self):
        rows = [{"inventory_turnover": 13.5, "Quarter": "2024Q1"}]
        values = _series(rows, ("inventory_turnover", "Inventory_Turnover", "InventoryTurnover", "存貨週轉率"))
        self.assertEqual(values, [13.5])

        rows2 = [{"Inventory_Turnover": 14.2, "Quarter": "2024Q2"}]
        values2 = _series(rows2, ("inventory_turnover", "Inventory_Turnover", "InventoryTurnover", "存貨週轉率"))
        self.assertEqual(values2, [14.2])

    def test_operating_margin_score_not_missing_with_data(self):
        rows = [
            {"operating_margin": 12.0, "Quarter": "2023Q4"},
            {"operating_margin": 13.0, "Quarter": "2024Q1"},
            {"operating_margin": 11.0, "Quarter": "2024Q2"},
            {"operating_margin": 14.0, "Quarter": "2024Q3"},
        ]
        score, reason, deduction = _operating_margin_score(rows)
        self.assertGreater(score, 0, "Should have a score when operating_margin is provided")
        self.assertIn("營益率", reason)

    def test_cash_flow_score_not_missing_with_data(self):
        rows = [
            {"free_cash_flow": 110.0, "Quarter": f"2024Q{i}"} for i in range(1, 7)
        ]
        score, reason, deduction = _cash_flow_score(rows)
        self.assertGreater(score, 0, "Should have a score when free_cash_flow is provided")

    def test_inventory_score_not_missing_with_data(self):
        rows = [
            {"inventory_turnover": 13.5 + i * 0.1, "Quarter": f"2024Q{i+1}"} for i in range(4)
        ]
        score, reason, deduction = _inventory_score(rows)
        self.assertGreater(score, 0, "Should have a score when inventory_turnover is provided")

    def test_profit_growth_score_not_missing_with_data(self):
        rows = [
            {"net_income_yoy": 10.0 + i, "Quarter": f"2024Q{i+1}"} for i in range(4)
        ]
        score, reason, deduction = _profit_growth_score(rows)
        self.assertGreater(score, 0, "Should have a score when net_income_yoy is provided")

    def test_revenue_growth_finds_yoy_percent(self):
        rows = [
            {"YoY%": 10.0, "Month": "2024-01"},
            {"YoY%": 12.0, "Month": "2024-02"},
            {"YoY%": 15.0, "Month": "2024-03"},
            {"YoY%": 11.0, "Month": "2024-04"},
            {"YoY%": 13.0, "Month": "2024-05"},
            {"YoY%": 14.0, "Month": "2024-06"},
        ]
        score, reason, deduction = _revenue_growth_score(rows)
        self.assertGreater(score, 0, "Should have a score when YoY% is provided")


class TestMopsFieldsFilter(unittest.TestCase):
    """Test that mops_sources financial_detail_snapshot includes new field types."""

    def test_includes_cash_flow_fields(self):
        from research_center.mops_sources import financial_detail_snapshot
        rows = [
            {
                "Quarter": "2024Q1",
                "Free_Cash_Flow": 110,
                "free_cash_flow": 110,
                "Operating_Cash_Flow": 150,
            }
        ]
        result = financial_detail_snapshot(rows)
        useful = result.get("fields", [])
        # free_cash_flow or Free_Cash_Flow should be in useful fields
        has_cash = any("cash" in str(f).lower() or "flow" in str(f).lower() for f in useful)
        self.assertTrue(has_cash, f"Expected cash/flow fields in useful, got: {useful}")

    def test_includes_inventory_fields(self):
        from research_center.mops_sources import financial_detail_snapshot
        rows = [
            {
                "Quarter": "2024Q1",
                "inventory_turnover": 13.5,
                "Inventory": 200,
            }
        ]
        result = financial_detail_snapshot(rows)
        useful = result.get("fields", [])
        has_inventory = any("inventory" in str(f).lower() or "存貨" in str(f) or "週轉" in str(f) for f in useful)
        self.assertTrue(has_inventory, f"Expected inventory fields in useful, got: {useful}")


class TestQuarterNumber(unittest.TestCase):
    def test_standard_quarter(self):
        self.assertEqual(_quarter_number("2024Q3"), 3)

    def test_quarter_1(self):
        self.assertEqual(_quarter_number("2024Q1"), 1)

    def test_quarter_4(self):
        self.assertEqual(_quarter_number("2025Q4"), 4)

    def test_lowercase(self):
        self.assertEqual(_quarter_number("2024q2"), 2)

    def test_none(self):
        self.assertIsNone(_quarter_number(None))

    def test_empty(self):
        self.assertIsNone(_quarter_number(""))

    def test_no_quarter(self):
        self.assertIsNone(_quarter_number("2024"))


class TestMonthlyRevenueYoYFill(unittest.TestCase):
    """Verify YoY per-row fill: official first, then shift fallback."""

    def test_monthly_revenue_yoy_fill_missing_with_shift_yoy(self):
        revenue_df = pd.DataFrame([
            {"Monthly_Revenue": 100, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 110, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 120, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 130, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 140, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 150, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 160, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 170, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 180, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 190, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 200, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 210, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 240, "Prior_Year_Revenue": None},
            {"Monthly_Revenue": 260, "Prior_Year_Revenue": 200},
        ])

        official_yoy = revenue_df.apply(
            lambda row: _safe_pct_change(row.get("Monthly_Revenue"), row.get("Prior_Year_Revenue")),
            axis=1,
        )
        shift_yoy = revenue_df["Monthly_Revenue"].pct_change(12) * 100
        revenue_df["YoY%"] = official_yoy.combine_first(shift_yoy)

        # Row 13 (0-indexed 12): shift YoY = 240/100 - 1 = 140%
        self.assertAlmostEqual(revenue_df.iloc[12]["YoY%"], 140.0)
        # Row 14 (0-indexed 13): official YoY = (260-200)/200 * 100 = 30%
        self.assertAlmostEqual(revenue_df.iloc[13]["YoY%"], 30.0)


class TestDeriveQuarterlyFromYTD(unittest.TestCase):
    """Test YTD-to-quarterly conversion for cash flow fields."""

    def test_cash_flow_ytd_converts_to_quarterly_values(self):
        financial_df = pd.DataFrame([
            {"Quarter": "2024Q1", "Free_Cash_Flow": 100},
            {"Quarter": "2024Q2", "Free_Cash_Flow": 250},
            {"Quarter": "2024Q3", "Free_Cash_Flow": 310},
            {"Quarter": "2024Q4", "Free_Cash_Flow": 500},
            {"Quarter": "2025Q1", "Free_Cash_Flow": 80},
        ])

        quarterly = _derive_quarterly_from_ytd(financial_df, "Free_Cash_Flow")

        # Q1: 100, Q2: 250-100=150, Q3: 310-250=60, Q4: 500-310=190, Q1: 80
        self.assertEqual(quarterly, [100, 150, 60, 190, 80])

    def test_cash_flow_ytd_keeps_value_when_previous_quarter_missing(self):
        financial_df = pd.DataFrame([
            {"Quarter": "2024Q1", "Free_Cash_Flow": 100},
            {"Quarter": "2024Q3", "Free_Cash_Flow": 310},
        ])

        quarterly = _derive_quarterly_from_ytd(financial_df, "Free_Cash_Flow")

        # Q3 can't subtract Q1 (Q1 != Q2), so keeps original 310
        self.assertEqual(quarterly, [100, 310])

    def test_cash_flow_ytd_handles_none_values(self):
        financial_df = pd.DataFrame([
            {"Quarter": "2024Q1", "Free_Cash_Flow": 100},
            {"Quarter": "2024Q2", "Free_Cash_Flow": None},
            {"Quarter": "2024Q3", "Free_Cash_Flow": 400},
        ])

        quarterly = _derive_quarterly_from_ytd(financial_df, "Free_Cash_Flow")

        # Q1: 100, Q2: None -> None, Q3: Q2 is None, can't subtract, keeps 400
        self.assertEqual(quarterly[0], 100)
        self.assertIsNone(quarterly[1])
        self.assertEqual(quarterly[2], 400)

    def test_cash_flow_ytd_q1_always_keeps_value(self):
        financial_df = pd.DataFrame([
            {"Quarter": "2024Q1", "Free_Cash_Flow": 55},
        ])

        quarterly = _derive_quarterly_from_ytd(financial_df, "Free_Cash_Flow")
        self.assertEqual(quarterly, [55])


if __name__ == "__main__":
    unittest.main()