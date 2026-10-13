import csv
import hashlib
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from backtests.dual_ma import etf_0050_regime_entry_exit_comparison_v1 as subject
from backtests.dual_ma.costs import CATHAY_WEB_028_TW_V1, size_entry
from backtests.dual_ma.models import Bar, InstrumentType
from backtests.dual_ma.pivots import DowDefenseTracker


def _csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def formal_output(tmp_path_factory):
    root = tmp_path_factory.mktemp("0050-regime-qa-fix")
    run_root, report_root = root / "run", root / "report"
    subject._write_run(run_root=run_root, report_root=report_root)
    return run_root, report_root


def _row(*, ma21="10", ma144="10", close="10", low="9", k="20", d="20"):
    return {
        "date": date(2024, 1, 2), "sma21": Decimal(ma21), "sma144": Decimal(ma144),
        "technical_close": Decimal(close), "technical_low": Decimal(low),
        "entry_k": Decimal(k), "entry_d": Decimal(d),
    }


def _entry_row(day, histogram, *, low="9"):
    return {
        "date": day,
        "histogram": Decimal(histogram),
        "technical_close": Decimal("10"),
        "technical_low": Decimal(low),
        "technical_high": Decimal("11"),
        "sma21": Decimal("10"),
        "entry_k": Decimal("20"),
        "entry_d": Decimal("20"),
        "price_rebase_factor": Decimal("1"),
    }


def test_p2_zero_to_positive_without_negative_segment_fails_closed():
    rows = [
        _entry_row(date(2024, 1, 2), "0"),
        _entry_row(date(2024, 1, 3), "1"),
    ]
    candidates, _ = subject.entry_candidates(rows)
    assert candidates["P2"] == {}


def test_p2_true_negative_segment_emits_with_segment_low():
    rows = [
        _entry_row(date(2024, 1, 2), "-1", low="9"),
        _entry_row(date(2024, 1, 3), "-0.5", low="8"),
        _entry_row(date(2024, 1, 4), "1", low="9.5"),
    ]
    candidates, _ = subject.entry_candidates(rows)
    signal = candidates["P2"][date(2024, 1, 4)]
    assert signal["structure_start"] == date(2024, 1, 2)
    assert signal["structure_end"] == date(2024, 1, 3)
    assert signal["pivot_low"] == Decimal("8")
    assert signal["pivot_low_date"] == date(2024, 1, 3)


def test_regime_equality_is_high_and_strict_low_is_low():
    assert subject._regime(_row(ma21="10", ma144="10")) == "HIGH"
    assert subject._regime(_row(ma21="9.999", ma144="10")) == "LOW"


def test_ma21_breakout_is_strict_on_signal_day():
    previous = _row(close="10", ma21="10")
    assert not subject._breakout(previous, _row(close="10", ma21="10"))
    assert subject._breakout(previous, _row(close="10.01", ma21="10"))


def test_relation_equality_is_not_death_or_golden_side():
    assert subject._relation(_row(k="20", d="20")) == "EQ"
    assert subject._relation(_row(k="19.99", d="20")) == "LT"
    assert subject._relation(_row(k="20.01", d="20")) == "GT"


def test_fee_adjusted_position_sizing_respects_cash_and_twenty_percent_risk():
    model = CATHAY_WEB_028_TW_V1.with_research_slippage(20)
    sized = size_entry("100", "80", InstrumentType.ETF, model,
                       max_total_cost=Decimal("500000"),
                       max_initial_r=Decimal("100000"), max_shares=10_000_000)
    assert sized is not None
    assert sized.buy.cash_amount <= Decimal("500000")
    assert Decimal("0") < sized.initial_r <= Decimal("100000")


def test_sealed_hash_constants_are_lowercase_sha256():
    assert len(subject.SEALED_RUN_MANIFEST_SHA256) == 64
    assert len(subject.SEALED_REPORT_MANIFEST_SHA256) == 64
    assert len(subject.SEALED_READBACK_SHA256) == 64
    assert subject.SEALED_RUN_MANIFEST_SHA256 == subject.SEALED_RUN_MANIFEST_SHA256.lower()


def test_stop_is_strict_and_has_priority_over_e1_macd():
    position = {"exit_rule": "E1", "regime": "HIGH", "effective_stop": Decimal("90"),
                "macd_armed": True, "two_r_armed": False}
    assert subject.resolve_exit_reason(close=Decimal("90"), position=position,
                                       macd_death=False, kd_death=False) is None
    assert subject.resolve_exit_reason(close=Decimal("89.99"), position=position,
                                       macd_death=True, kd_death=False) == "FIXED_STOP"


def test_e1_requires_arm_and_e2_requires_2r_arm_plus_new_cross():
    e1 = {"exit_rule": "E1", "regime": "HIGH", "effective_stop": Decimal("90"),
          "macd_armed": False, "two_r_armed": False}
    assert subject.resolve_exit_reason(close=Decimal("100"), position=e1,
                                       macd_death=True, kd_death=False) is None
    e1["macd_armed"] = True
    assert subject.resolve_exit_reason(close=Decimal("100"), position=e1,
                                       macd_death=True, kd_death=False) == "E1_MACD_DEATH"
    e2 = {"exit_rule": "E2", "regime": "LOW", "effective_stop": Decimal("90"),
          "macd_armed": True, "two_r_armed": False}
    assert subject.resolve_exit_reason(close=Decimal("100"), position=e2,
                                       macd_death=False, kd_death=True) is None
    e2["two_r_armed"] = True
    assert subject.resolve_exit_reason(close=Decimal("100"), position=e2,
                                       macd_death=False, kd_death=True) == "E2_POST_2R_KD_DEATH"


def test_e3_defense_only_rises_and_legacy_low_suppression_is_explicit():
    assert subject.raised_defense(Decimal("90"), Decimal("80")) == Decimal("90")
    assert subject.raised_defense(Decimal("90"), Decimal("95")) == Decimal("95")
    low = {"exit_rule": "E1", "regime": "LOW", "effective_stop": Decimal("90"),
           "macd_armed": False, "two_r_armed": False}
    assert subject.resolve_exit_reason(close=Decimal("80"), position=low,
                                       macd_death=False, kd_death=False) == "FIXED_STOP"
    assert subject.resolve_exit_reason(close=Decimal("80"), position=low,
                                       macd_death=False, kd_death=False,
                                       legacy_low_stop_inactive=True) is None


def test_e3_n2_pivot_requires_confirmation_then_later_close_breakout():
    values = [(10, 8, 9), (11, 9, 10), (15, 10, 14), (12, 9, 10),
              (11, 8, 9), (10, 5, 6), (11, 7, 9), (12, 8, 10), (16, 9, 16)]
    bars = []
    tracker = DowDefenseTracker()
    emitted = []
    for index, (high, low, close) in enumerate(values):
        day = date(2024, 1, 1) + timedelta(days=index)
        bars.append(Bar.from_values(day, close, high, low, close))
        _, _, new = tracker.update(bars)
        if index < 8:
            assert not new
        emitted.extend(new)
    assert len(emitted) == 1
    assert emitted[0].pivot_low.confirmed_date == date(2024, 1, 8)
    assert emitted[0].activated_date == date(2024, 1, 9)
    assert emitted[0].defense == Decimal("5")


def test_end_to_end_matrix_costs_links_and_independent_trade_metrics(formal_output):
    run_root, report_root = formal_output
    result = subject.validate(run_root, report_root)
    assert result["status"] == "PASS"
    assert result["matrix_lines"] == 36
    assert result["cost_lines"] == 108
    assert result["account_reconciliation_lines"] == 108
    assert result["matrix_metrics_independent"] is True
    assert result["report_links"]["status"] == "PASS"
    assert result["report_links"]["checked_count"] >= 100
    assert hashlib.sha256((run_root / "matrix-summary.csv").read_bytes()).digest() != hashlib.sha256(
        (run_root / "matrix-trade-metrics.csv").read_bytes()
    ).digest()


def test_named_2007_dividend_right_survives_exit_until_payment(formal_output):
    run_root, _ = formal_output
    named = json.loads((run_root / "named-dividend-case-2007.json").read_text(encoding="utf-8-sig"))
    assert named["cell_id"] == "HIGH_P1_E1_SIGNAL_CLOSE_20BPS"
    assert named["episode_id"] == "HIGH-P1-E1-0007"
    assert named["exit_date"] == "2007-11-08"
    assert Decimal(named["entitlement_total"]) == Decimal("23792.5")
    assert Decimal(named["trade_dividend_cash"]) == Decimal("23792.5")
    assert Decimal(named["receivable_at_exit"]) == Decimal("23792.5")
    trades = _csv(run_root / "trade-ledger.csv")
    trade = next(row for row in trades if row["cell_id"] == named["cell_id"]
                 and row["episode_id"] == named["episode_id"])
    assert Decimal(trade["dividend_paid_by_exit"]) == 0
    assert Decimal(trade["dividend_cash"]) == Decimal("23792.5")
    events = _csv(run_root / "event-ledger.csv")
    entitlement = next(row for row in events if row["cell_id"] == named["cell_id"]
                       and row["episode_id"] == named["episode_id"]
                       and row["event"] == "DIVIDEND_ENTITLEMENT")
    payment = next(row for row in events if row["cell_id"] == named["cell_id"]
                   and row["episode_id"] == named["episode_id"]
                   and row["event"] == "DIVIDEND_PAID")
    assert entitlement["date"] == "2007-10-24"
    assert payment["date"] == "2007-12-03"
    daily = _csv(run_root / "daily-equity.csv")
    exit_day = next(row for row in daily if row["cell_id"] == named["cell_id"]
                    and row["date"] == "2007-11-08")
    assert Decimal(exit_day["economic_equity"]) - Decimal(exit_day["equity"]) == Decimal("23792.5")


def test_all_36_cells_reconstruct_dividend_entitlements_and_all_108_accounts_conserve(formal_output):
    run_root, _ = formal_output
    dividend = _csv(run_root / "dividend-ledger-reconciliation.csv")
    assert len({row["cell_id"] for row in dividend}) == 36
    assert all(row["status"] == "PASS" and Decimal(row["difference"]) == 0 for row in dividend)
    assert sum(1 for row in dividend if Decimal(row["receivable_at_exit"]) > 0) >= 140
    accounts = _csv(run_root / "account-reconciliation-108.csv")
    assert len(accounts) == 108
    assert all(row["status"] == "PASS" for row in accounts)
    assert all(Decimal(row["cash_difference"]) == 0 for row in accounts)
    assert all(Decimal(row["minimum_cash"]) >= 0 for row in accounts)


def test_real_p3_cycles_use_named_second_pullback_pivot_and_allow_non_lt_signal_day(formal_output):
    run_root, _ = formal_output
    rows = [row for row in _csv(run_root / "kd-cycle-audit.csv")
            if row["signal_generated"] == "True"]
    assert rows
    assert any(row["entry_k_relation"] != "LT" for row in rows)
    for row in rows:
        assert Decimal(row["k2_so_far"]) > Decimal(row["k1"])
        assert row["second_k_lt_d_start"] <= row["pivot_low_date"] <= row["pivot_end"]
        assert Decimal(row["pivot_low"]) > 0


def test_real_split_changes_shares_and_stop_coordinate_but_cash_actions_do_not(formal_output):
    run_root, _ = formal_output
    events = _csv(run_root / "event-ledger.csv")
    splits = [row for row in events if row["event"] == "SPLIT_REBASE"]
    assert splits
    for row in splits:
        assert row["date"] == "2025-06-18"
        assert int(row["shares_after"]) == int(row["shares_before"]) * 4
        assert Decimal(row["initial_stop"]) == Decimal(row["previous_initial_stop"]) * Decimal("0.25")
        assert Decimal(row["effective_stop"]) == Decimal(row["previous_effective_stop"]) * Decimal("0.25")
    cash_actions = [row for row in events if row["event"] == "CASH_ACTION_TECHNICAL_REBASE"]
    assert cash_actions
    assert all(row["shares_before"] == row["shares_after"] for row in cash_actions)


def test_e2_and_e3_real_event_order_is_causal(formal_output):
    run_root, _ = formal_output
    events = _csv(run_root / "event-ledger.csv")
    trades = _csv(run_root / "trade-ledger.csv")
    grouped = {}
    for row in events:
        grouped.setdefault((row["cell_id"], row.get("episode_id", "")), []).append(row)
    for trade in trades:
        if trade["exit_reason"] == "E2_POST_2R_KD_DEATH":
            armed = [row for row in grouped[(trade["cell_id"], trade["episode_id"])]
                     if row["event"] == "E2_TWO_R_ARMED"]
            assert armed and armed[0]["date"] <= trade["exit_signal_date"]
    dow = [row for row in events if row["event"] == "E3_DOW_DEFENSE_RAISED"]
    assert dow
    assert all(Decimal(row["stop"]) > Decimal(row["previous_stop"]) for row in dow)
    assert all(row["pivot_confirmed_date"] <= row["date"]
               and row["breakout_date"] <= row["date"] for row in dow)


def test_p3_machine_readable_trade_summary_covers_12_accounts_and_reconciles(formal_output):
    run_root, report_root = formal_output
    closed = _csv(run_root / "p3-trades-closed.csv")
    opened = _csv(run_root / "p3-positions-open.csv")
    payload = json.loads((run_root / "p3-trade-summary.json").read_text(encoding="utf-8-sig"))
    expected = {(regime, exit_rule, timing)
                for regime in ("HIGH", "LOW")
                for exit_rule in ("E1", "E2", "E3")
                for timing in ("SIGNAL_CLOSE", "NEXT_SESSION_OPEN")}
    assert {(row["regime"], row["exit_rule"], row["timing"]) for row in closed} == expected
    assert payload["audit"]["status"] == "PASS"
    assert payload["audit"]["expected_account_coverage"] == 12
    assert payload["audit"]["observed_account_coverage"] == 12
    assert len(closed) == payload["audit"]["closed_trade_count"]
    for row in closed:
        assert row["entry_rule"] == "P3" and row["status"] == "CLOSED"
        expected_pnl = (Decimal(row["exit_cash"])
                        + Decimal(row["dividend_economic_rights"])
                        - Decimal(row["buy_total"]))
        assert Decimal(row["net_pnl"]) == expected_pnl
        assert Decimal(row["net_return_pct"]) == (
            expected_pnl / Decimal(row["buy_total"]) * Decimal("100"))
        assert Decimal(row["pnl_reconstruction_difference"]) == 0
        assert Decimal(row["return_recalculation_difference"]) == 0
        assert row["audit_status"] == "PASS"
    assert all(row["status"] == "OPEN" and not row["exit_date"]
               and not row["net_pnl"] and not row["net_return_pct"] for row in opened)
    assert (report_root / "p3-trades-closed.csv").is_file()
    assert (report_root / "p3-positions-open.csv").is_file()
    assert (report_root / "p3-trade-summary.json").is_file()


def test_pareto_is_practical_and_grouped_with_sparse_candidates_not_auto_selected(formal_output):
    run_root, _ = formal_output
    pareto = _csv(run_root / "pareto-candidates.csv")
    assert {row["regime"] for row in pareto} == {"HIGH", "LOW"}
    assert all(row["timing"] == "NEXT_SESSION_OPEN" and row["slippage_bps"] == "20" for row in pareto)
    restricted = [row for row in pareto if row["eligible_for_automatic_selection"] == "False"]
    assert restricted
    assert all(int(row["closed_trades"]) < 20
               or Decimal(row["top5_profit_concentration"]) > Decimal("0.80")
               for row in restricted)


def test_attribution_design_rejects_noop_rule_replacement_labels(formal_output):
    run_root, _ = formal_output
    rows = json.loads((run_root / "replacement-attribution-design.json").read_text(encoding="utf-8-sig"))
    by = {row["case"]: row for row in rows}
    base = by["ALL_STOPS_CONTROL"]
    high = by["HIGH_RULE_ONLY"]
    low = by["LOW_RULE_ONLY"]
    both = by["BOTH_RULES"]
    assert (high["high_entry"], high["high_exit"]) == (base["high_entry"], base["high_exit"])
    assert (low["low_entry"], low["low_exit"]) == (base["low_entry"], base["low_exit"])
    assert high["identifiable_rule_change"] is False
    assert low["identifiable_rule_change"] is False
    assert both["identifiable_rule_change"] is False
    assert "不可估" in high["factor_changed"]
    assert "不可估" in low["factor_changed"]
    assert "不可估" in both["factor_changed"]
    assert "只換" not in json.dumps(rows, ensure_ascii=False)
    assert (both["high_entry"], both["high_exit"]) == (high["high_entry"], high["high_exit"])
    assert (both["low_entry"], both["low_exit"]) == (low["low_entry"], low["low_exit"])
    assert by["NEW_ENGINE_DAILY_STOP_POLICY_CONTROL"]["stop_policy"] == "DAILY_HIGH_ONLY"
    assert by["LOCKED_ENTRY_STOP_POLICY_CONTROL"]["stop_policy"] == "ENTRY_HIGH_ONLY"


def test_report_is_full_14_section_single_entry_with_trade_details(formal_output):
    run_root, report_root = formal_output
    readme = (report_root / "README.md").read_text(encoding="utf-8")
    assert len(readme.encode("utf-8")) > 20_000
    assert all(f"## {index:02d}" in readme for index in range(1, 15))
    assert "<a id=" not in readme
    assert "樣本內正報酬" in readme
    assert "樣本內為正期望" not in readme
    assert "候選與控制重合檢查" in readme
    assert "規則替換效果不可估" in readme
    assert "2026（截至09月）" in readme
    assert "未滿整個視窗" in readme
    assert "均線突破進場" in readme
    assert "MACD 翻紅進場" in readme
    assert "KD 二段轉強進場" in readme
    assert "MACD 轉弱出場" in readme
    assert "2R 後 KD 停利" in readme
    assert "道氏移動防守" in readme
    assert not re.search(r"\b(?:HIGH|LOW|P[123]|E[123])\b", readme)
    table = "|策略／比較基準|" + readme.split("|策略／比較基準|", 1)[1].split("\n\n", 1)[0]
    assert "|成交方式|期末資產|總報酬|年化報酬率|最大回落|" in table
    assert "|獲利年度|虧損年度|平盤年度|進場／出場／未平倉|最大投入|" in table
    assert "CAGR" not in table and "MDD" not in table
    assert "INITIAL_OPEN_THEN_HOLD" not in table and "INDEX_CLOSE" not in table
    details = (report_root / "trade-details.md").read_text(encoding="utf-8")
    assert "<a id=" not in details
    assert "HIGH-P1-E1-0007" not in details
    assert "## 多頭區｜均線突破＋MACD 轉弱出場｜次日開盤" in details
    assert "## 弱勢區｜KD 二段轉強＋2R 後 KD 停利｜訊號收盤" in details
    assert "固定初始防守" in details
    assert not re.search(r"\b(?:HIGH|LOW|P[123]|E[123])\b", details)
    assert "完整繁體中文逐筆交易明細" in details
    assert "## 組合候選｜次日開盤" in details
    assert "## 組合候選｜訊號收盤" in details
    attribution = _csv(run_root / "replacement-attribution.csv")
    attribution_trades = _csv(run_root / "replacement-attribution-trades.csv")
    for timing in ("NEXT_SESSION_OPEN", "SIGNAL_CLOSE"):
        summary = next(row for row in attribution
                       if row["case"] == "BOTH_RULES" and row["timing"] == timing)
        trades = [row for row in attribution_trades
                  if row["case"] == "BOTH_RULES" and row["timing"] == timing]
        assert len(trades) == int(summary["closed_trades"])
        for index, row in enumerate(trades, 1):
            assert f"|第 {index:04d} 筆|{row['entry_date']}|{row['exit_date']}|" in details
    periods = _csv(run_root / "replacement-attribution-periods.csv")
    both_periods = [row for row in periods if row["case"] == "BOTH_RULES"
                    and row["timing"] == "NEXT_SESSION_OPEN"
                    and row["period"].startswith("ROLLING_")]
    assert len([row for row in both_periods if row["complete_window"] == "False"]) == 3
    assert (report_root / "trade-ledger.csv").is_file()
    assert (report_root / "replacement-attribution-trades.csv").is_file()
    assert (report_root / "daily-equity.csv").is_file()
    assert (report_root / "event-ledger.csv").is_file()


def test_main_equity_chart_is_five_line_same_capital_reader_comparison(formal_output):
    run_root, report_root = formal_output
    svg = (report_root / "main-comparison-equity.svg").read_text(encoding="utf-8")
    assert svg.count("<polyline") == 5
    for label in ("組合候選", "封存 v1.7", "0050 持有－股利留現金",
                  "0050 持有－股利再投入", "加權報酬指數等值"):
        assert label in svg
    assert "元</text>" in svg
    assert "2006-01" in svg and "2026-09" in svg
    assert "組合候選與基準" in svg and "主策略" not in svg
    assert not (run_root / "matrix-equity.svg").exists()
    regime_svg = (report_root / "regime-candidates-equity.svg").read_text(encoding="utf-8")
    assert "2006-01" in regime_svg and "2026-09" in regime_svg
    assert "元</text>" in regime_svg


def test_old_v5_tree_is_byte_exact_after_qa_fix_run(formal_output):
    del formal_output
    result = subject.validate_old_v5()
    assert result["status"] == "PASS_EXACT"
    assert result["run"]["actual"] == subject.OLD_V5_RUN_TREE_SHA256
    assert result["report"]["actual"] == subject.OLD_V5_REPORT_TREE_SHA256
