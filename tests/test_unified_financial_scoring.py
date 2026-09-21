from __future__ import annotations

from types import SimpleNamespace

from stock_ai_bot.scanning.stock_scanner import RevenuePoint, classify_revenue_group
from stock_ai_bot.scoring.unified_financial_scoring import (
    effective_revenue_rows,
    score_unified_financial,
    score_unified_revenue,
)


def test_effective_revenue_rows_omits_january_and_combines_jan_feb_yoy():
    rows = [
        {"month": "2025-01-01", "revenue": 100, "yoy": 1},
        {"month": "2025-02-01", "revenue": 100, "yoy": 1},
        {"month": "2026-01-01", "revenue": 120, "yoy": 20},
        {"month": "2026-02-01", "revenue": 120, "yoy": 20},
    ]

    effective = effective_revenue_rows(rows)

    assert not any(str(row.get("month")).startswith("2026-01") for row in effective)
    latest = effective[-1]
    assert latest["jan_feb_combined"] is True
    assert latest["revenue"] == 240
    assert round(latest["yoy"], 6) == 20


def test_stock_scanner_group_uses_adjusted_jan_feb_observation():
    points = [
        RevenuePoint("2026-04-01", 130, 2),
        RevenuePoint("2026-03-01", 125, 2),
        RevenuePoint("2026-02-01", 120, -50),
        RevenuePoint("2026-01-01", 120, -50),
        RevenuePoint("2025-12-01", 110, 2),
        RevenuePoint("2025-11-01", 108, 2),
        RevenuePoint("2025-02-01", 100, 1),
        RevenuePoint("2025-01-01", 100, 1),
    ]

    assert classify_revenue_group(points) == "group_1"


def test_revenue_score_never_awards_missing_data_or_untrusted_event_time():
    item = SimpleNamespace(revenue_history=[])
    missing = score_unified_revenue(item, {"revenue": {"history": []}})
    assert missing["score"] == 0

    rows = []
    year, month = 2024, 1
    for index in range(24):
        rows.append(
            {
                "month": f"{year:04d}-{month:02d}-01",
                "revenue": 100 + index * 5,
                "yoy": 5 + index,
            }
        )
        month += 1
        if month == 13:
            year += 1
            month = 1
    snapshot = {
        "revenue": {
            "history": rows,
            "peer_context": {"valid_peer_count": 5, "positive_ratio": 0.8},
            "preannouncement_price": {
                "status": "unavailable",
                "published_at_source": "statutory_deadline_fallback",
            },
        }
    }

    detail = score_unified_revenue(SimpleNamespace(revenue_history=rows), snapshot)

    assert 0 < detail["score"] <= 20
    assert any("公布時間" in risk for risk in detail["risks"])
    assert not any("公告前 20 日" in reason for reason in detail["reasons"])
    assert any(rule["rule_id"] == "REV_PEER_BREADTH" for rule in detail["details"]["rule_results"])


def test_financial_score_uses_new_statement_fields_and_official_valuation_context():
    rows = []
    for index in range(8):
        year = 2024 + index // 4
        quarter = index % 4 + 1
        rows.append(
            {
                "Quarter": f"{year}Q{quarter}",
                "Revenue": 100 + index * 10,
                "Gross_Profit": 25 + index * 4,
                "Gross_Margin": 20 + index,
                "Operating_Margin": 5 + index,
                "EPS": 0.5 + index * 0.2,
                "Net_Income": 10 + index * 2,
                "Non_Operating_Income": 1,
                "Pre_Tax_Income": 20 + index * 2,
                "Operating_Cash_Flow": 15 + index,
                "Free_Cash_Flow": 8 + index,
                "Contract_Liabilities": 10 + index * 2,
                "Paid_In_Capital": 100,
                "Inventory": 70 - index * 2,
                "Inventory_Turnover": 1 + index * 0.1,
                "Inventory_To_TTM_Revenue": 0.2 - index * 0.01,
                "Current_Ratio": 2.2,
                "Quick_Ratio": 2.0,
                "Debt_Ratio": 0.5 - index * 0.01,
            }
        )
    valuation = {
        "latest": {"pe_ratio": 10, "pb_ratio": 1.5},
        "history": [{"pe_ratio": 8 + index * 0.3} for index in range(40)],
        "peers": [{"pe_ratio": value} for value in (9, 10, 11, 12, 13, 14)],
    }

    detail = score_unified_financial(
        SimpleNamespace(industry="電子零組件業"),
        {"financial": {"financial_data": rows, "valuation_data": valuation}},
    )

    assert 10 <= detail["score"] <= 15
    assert detail["details"]["contract_liabilities"]["capital_ratio"] > 0.05
    assert detail["details"]["valuation"]["history_count"] >= 36
    assert any(rule["rule_id"].startswith("FIN_") for rule in detail["details"]["rule_results"])


def test_financial_industry_is_explicitly_not_applicable():
    detail = score_unified_financial(
        SimpleNamespace(industry="金融保險"),
        {"financial": {"financial_data": [{"Quarter": "2026Q1", "EPS": 1}]}},
    )

    assert detail["score"] == 0
    assert detail["details"]["applicability"] == "rule_scope_not_applicable"


def test_contract_liability_does_not_score_single_quarter_or_sparse_history():
    rows = [
        {
            "Quarter": "2026Q1",
            "Contract_Liabilities": 50,
            "Paid_In_Capital": 100,
        }
    ]

    detail = score_unified_financial(
        SimpleNamespace(industry="電子零組件業"),
        {"financial": {"financial_data": rows}},
    )
    contract = detail["details"]["contract_liabilities"]

    assert contract["score"] == 0
    assert contract["applicable"] is False
    assert contract["reported_quarters"] == 1
    assert any("合約負債申報或非零歷史不足" in risk for risk in detail["risks"])


def test_contract_liability_scores_only_after_applicability_gate():
    rows = [
        {
            "Quarter": f"2025Q{index + 1}",
            "Contract_Liabilities": 10 + index * 2,
            "Contract_Liabilities_Status": "reported",
            "Paid_In_Capital": 100,
        }
        for index in range(4)
    ]

    detail = score_unified_financial(
        SimpleNamespace(industry="電子零組件業"),
        {"financial": {"financial_data": rows}},
    )
    contract = detail["details"]["contract_liabilities"]

    assert contract["applicable"] is True
    assert contract["reported_quarters"] == 4
    assert contract["nonzero_quarters"] == 4
    assert contract["score"] == 1
    assert any("合約負債占股本至少 5%" in reason for reason in detail["reasons"])
    assert any("合約負債連續三季上升" in reason for reason in detail["reasons"])
