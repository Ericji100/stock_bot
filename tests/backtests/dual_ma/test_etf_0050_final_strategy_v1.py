from __future__ import annotations

import csv
from decimal import Decimal

from backtests.dual_ma import etf_0050_final_strategy_v1 as final


def test_main_comparison_is_exactly_the_five_decision_lines() -> None:
    assert final.MAIN_IDS == (
        "FINAL_SIGNAL_CLOSE_20BPS",
        "FINAL_NEXT_SESSION_OPEN_20BPS",
        "BUY_HOLD_CASH",
        "BUY_HOLD_REINVEST",
        "TAIEX_TOTAL_RETURN_EQUIVALENT_500K",
    )


def test_annual_rows_preserve_continuous_account_between_years() -> None:
    rows = [
        {"date": "2006-01-02", "equity": "500000"},
        {"date": "2006-12-29", "equity": "550000"},
        {"date": "2007-01-02", "equity": "540000"},
        {"date": "2007-12-31", "equity": "594000"},
    ]
    annual = final._annual_rows("X", rows)
    assert len(annual) == 2
    assert annual[0]["starting_equity"] == Decimal("500000")
    assert annual[1]["starting_equity"] == Decimal("550000")
    assert annual[1]["return"] == Decimal("0.08")


def test_benchmark_is_explicitly_non_tradable_and_drawdown_is_causal() -> None:
    summary, daily = final._benchmark_payload([
        {"date": "2006-01-02", "index_value": "100", "equivalent_500k": "500000"},
        {"date": "2006-01-03", "index_value": "80", "equivalent_500k": "400000"},
        {"date": "2006-01-04", "index_value": "120", "equivalent_500k": "600000"},
    ])
    assert summary["category"] == "NON_TRADABLE_MARKET_REFERENCE"
    assert "不可直接交易" in summary["tradeability"]
    assert summary["max_drawdown"] == Decimal("-0.2")
    assert daily[-1]["drawdown"] == Decimal("0")


def test_link_validator_accepts_same_document_anchor(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# 報告\n\n[跳轉](#結果)\n\n## 結果\n", encoding="utf-8")
    assert final._broken_markdown_links(tmp_path) == []
    (tmp_path / "README.md").write_text("# 報告\n\n[跳轉](#不存在)\n", encoding="utf-8")
    assert final._broken_markdown_links(tmp_path) == ["README.md->#不存在 (missing anchor)"]


def test_parity_projection_uses_the_sealed_source_rows(tmp_path) -> None:
    source = tmp_path / "source.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["strategy_id", "execution_timing", "value"])
        writer.writeheader()
        writer.writerow({"strategy_id": "FIXED", "execution_timing": "OPEN", "value": "1.00"})
        writer.writerow({"strategy_id": "OTHER", "execution_timing": "OPEN", "value": "9.00"})
    result = final._parity_projection(
        source_path=source,
        actual_rows=[{"strategy_id": "FIXED", "execution_timing": "OPEN", "value": "1"}],
        source_selector={"strategy_id": "FIXED", "execution_timing": "OPEN"},
    )
    assert result["expected_rows"] == 1
    assert result["actual_rows"] == 1
    assert result["exact"] is True


def test_reinvest_audit_proves_payment_day_close_and_cash_conservation() -> None:
    order = final.CATHAY_WEB_028_TW_V1.with_research_slippage(20).buy(Decimal("10"), 1)
    cash_after = Decimal("100") + Decimal("50") - order.cash_amount
    daily = [
        {"comparison_id": final.BUY_HOLD_REINVEST_ID, "date": "2006-01-02", "raw_close": "9", "cash": "100"},
        {"comparison_id": final.BUY_HOLD_REINVEST_ID, "date": "2006-01-03", "raw_close": "10", "cash": cash_after},
        {"comparison_id": final.BUY_HOLD_REINVEST_ID, "date": "2006-01-04", "raw_close": "11", "cash": cash_after},
    ]
    transactions = [
        {"strategy_id": final.BUY_HOLD_REINVEST_ID, "date": "2006-01-02"},
        {
            "strategy_id": final.BUY_HOLD_REINVEST_ID, "date": "2006-01-03",
            "reference_price": order.reference_price, "fill_price": order.fill_price,
            "shares": order.shares, "gross": order.gross, "commission": order.commission,
            "tax": order.tax, "cash_amount": order.cash_amount,
        },
    ]
    actions = [{
        "strategy_id": final.BUY_HOLD_REINVEST_ID, "stage": "CASH_PAID",
        "ex_date": "2005-12-01", "payment_date": "2006-01-03", "cash": "50",
    }]
    audit, errors = final._buy_hold_reinvest_audit(
        daily=daily, transactions=transactions, actions=actions,
        expected_daily_rows=3, expected_reinvest_rows=1,
    )
    assert errors == []
    assert len(audit) == 1
    assert audit[0]["timing_assumption"] == "PAYMENT_DAY_CLOSE"
    assert audit[0]["all_checks_pass"] is True


def test_report_contract_rejects_old_or_misleading_claims() -> None:
    valid = (
        "# 0050 最終策略定版報告｜候選 v1.2\n"
        "付款日收盤再投入；實務是否能在同日使用該筆股息，須視券商實際入帳時點。"
        "SIGNAL_CLOSE 是理想化研究對照；不證明未來正期望。"
    )
    assert final._report_contract_violations(valid) == []
    invalid = valid.replace("理想化研究對照", "理想化上限")
    assert "forbidden phrase: 理想化上限" in final._report_contract_violations(invalid)


def test_v12_identity_is_consistent() -> None:
    assert final.VERSION.endswith("v1.2")
    assert final.RUN_ROOT_DEFAULT.name.endswith("v1-2")
    assert final.REPORT_ROOT_DEFAULT.name.endswith("v1-2")
