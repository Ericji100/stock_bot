from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from backtests.dual_ma import etf_0050_standard_report_v3 as standard_report
from backtests.dual_ma.etf_0050_ma21_fixed_stop_v1 import (
    ACCEPTED_RUN_ROOT,
    CURRENT_STRATEGY_ID,
    STRATEGY_ID,
    baseline_parity,
    build_indicator_rows,
    build_ma21_macd_run,
    run_study,
)


def _row(
    offset: int,
    close: str,
    sma21: str,
    histogram: str,
    *,
    low: str | None = None,
    factor: str = "1",
    dow_events: tuple = (),
    raw_close: str | None = None,
) -> dict:
    day = date(2025, 1, 1) + timedelta(days=offset)
    return {
        "date": day,
        "technical_close": Decimal(close),
        "technical_low": Decimal(low or close),
        "raw_close": Decimal(raw_close or close),
        "price_rebase_factor": Decimal(factor),
        "sma21": Decimal(sma21),
        "histogram": Decimal(histogram),
        "dow_events": dow_events,
    }


def _run(rows: list[dict], *, slippage_bps: int = 0):
    raw = {
        row["date"]: SimpleNamespace(close=row["raw_close"])
        for row in rows
    }
    return build_ma21_macd_run(
        indicator_rows=rows,
        raw_by_date=raw,
        start=rows[0]["date"],
        end=rows[-1]["date"],
        slippage_bps=slippage_bps,
    )


def test_new_breakout_only_once_and_new_breakout_after_exit_can_enter() -> None:
    rows = [
        _row(0, "9", "10", "0.2", low="8"),
        _row(1, "11", "10", "0.3", low="9"),
        _row(2, "12", "10", "0.2"),
        _row(3, "12", "10", "-0.1"),
        _row(4, "9", "10", "-0.2", low="8.5"),
        _row(5, "11", "10", "0.1", low="9"),
    ]
    run, opportunities, _audit = _run(rows)
    assert [row["role"] for row in run.transactions] == ["MOTHER", "EXIT", "MOTHER"]
    assert len(opportunities) == 2
    assert all(row["execution_status"] == "成交" for row in opportunities)


def test_green_entry_must_arm_at_zero_before_later_death_cross() -> None:
    rows = [
        _row(0, "9", "10", "-0.3", low="8"),
        _row(1, "11", "10", "-0.2", low="9"),
        _row(2, "12", "10", "-0.1"),
        _row(3, "13", "10", "0"),
        _row(4, "12", "10", "-0.01"),
    ]
    run, _opportunities, audit = _run(rows)
    assert [row["role"] for row in run.transactions] == ["MOTHER", "EXIT"]
    assert run.transactions[-1]["date"] == rows[-1]["date"]
    assert [row["event"] for row in audit] == [
        "ENTRY", "MACD_ARMED", "VALID_RED_TO_GREEN_EXIT"
    ]
    assert audit[0]["armed_on_entry"] is False


def test_pre_entry_death_cross_is_not_retroactively_used() -> None:
    rows = [
        _row(0, "9", "10", "0.1", low="8"),
        _row(1, "9", "10", "-0.1", low="8.5"),
        _row(2, "11", "10", "-0.05", low="9"),
    ]
    run, _opportunities, audit = _run(rows)
    assert [row["role"] for row in run.transactions] == ["MOTHER"]
    assert audit[-1]["event"] == "OPEN_AT_CUTOFF"


def test_histogram_zero_is_armed_and_next_negative_day_exits() -> None:
    rows = [
        _row(0, "9", "10", "-0.1", low="8"),
        _row(1, "11", "10", "0", low="9"),
        _row(2, "12", "10", "-0.0001"),
    ]
    run, _opportunities, audit = _run(rows)
    assert [row["role"] for row in run.transactions] == ["MOTHER", "EXIT"]
    assert audit[0]["armed_on_entry"] is True
    assert audit[1]["event"] == "VALID_RED_TO_GREEN_EXIT"


def test_stop_has_priority_same_day_reentry_is_blocked_and_episode_resets() -> None:
    rows = [
        _row(0, "9", "10", "0.1", low="8.8"),
        _row(1, "11", "10", "0.2", low="9.5"),
        _row(2, "9", "10", "0.1", low="9.1"),
        _row(3, "8.7", "8.5", "-0.1", low="8.6"),
        _row(4, "8.0", "8.5", "-0.2", low="7.8"),
        _row(5, "9.0", "8.5", "0.1", low="8.2"),
    ]
    run, opportunities, audit = _run(rows)
    assert [row["role"] for row in run.transactions] == ["MOTHER", "EXIT", "MOTHER"]
    assert run.transactions[1]["exit_reason"] == "INITIAL_PROTECTIVE_STOP"
    assert opportunities[1]["execution_status"] == "未成交"
    assert opportunities[1]["not_filled_reason"] == "同日已賣出，不得重新進場"
    assert opportunities[2]["leg_id"] == "MA21-MACD-L2"
    assert any(row["event"] == "INITIAL_STOP_PRIORITY_OVER_MACD" for row in audit)


def test_open_episode_is_left_for_cutoff_valuation_not_forced_exit() -> None:
    rows = [
        _row(0, "9", "10", "-0.2", low="8"),
        _row(1, "11", "10", "-0.1", low="9"),
        _row(2, "12", "10", "-0.05"),
    ]
    run, _opportunities, audit = _run(rows)
    assert [row["role"] for row in run.transactions] == ["MOTHER"]
    assert audit[-1]["event"] == "OPEN_AT_CUTOFF"


def test_dow_events_are_ignored_and_fixed_initial_stop_never_raises() -> None:
    inherited = SimpleNamespace(defense=Decimal("9.5"), version=1)
    raised = SimpleNamespace(defense=Decimal("11.5"), version=2)
    rows = [
        _row(0, "9", "10", "0.1", low="8"),
        _row(1, "11", "10", "0.2", low="9", dow_events=(inherited,)),
        _row(2, "12", "10", "0.1", dow_events=(raised,)),
        _row(3, "7", "10", "-0.1"),
    ]
    run, _opportunities, audit = _run(rows)
    assert [row["role"] for row in run.transactions] == ["MOTHER", "EXIT"]
    assert run.transactions[-1]["exit_reason"] == "INITIAL_PROTECTIVE_STOP"
    entry = next(row for row in audit if row["event"] == "ENTRY")
    assert entry["inherited_dow_stop"] is None
    assert entry["effective_stop"] == Decimal("8")
    assert not any(row["event"].startswith("DOW_") for row in audit)
    assert any(row["event"] == "INITIAL_STOP_PRIORITY_OVER_MACD" for row in audit)


def test_never_inherits_defense_in_any_slippage_scenario() -> None:
    defense = SimpleNamespace(defense=Decimal("10.01"), version=1)
    rows = [
        _row(0, "9", "9.5", "0.1", low="9"),
        _row(
            1, "10.02", "9.5", "0.2", low="9", dow_events=(defense,),
            raw_close="10",
        ),
    ]
    _run0, _opportunities0, audit0 = _run(rows, slippage_bps=0)
    _run20, _opportunities20, audit20 = _run(rows, slippage_bps=20)
    entry0 = next(row for row in audit0 if row["event"] == "ENTRY")
    entry20 = next(row for row in audit20 if row["event"] == "ENTRY")
    assert entry0["inherited_dow_stop"] is None
    assert entry0["effective_stop"] == Decimal("9")
    assert entry20["inherited_dow_stop"] is None
    assert entry20["effective_stop"] == Decimal("9")


def test_split_rebase_keeps_sma_macd_and_stop_continuous() -> None:
    raw = []
    start = date(2025, 5, 1)
    for index in range(22):
        price = Decimal("100") if index < 21 else Decimal("25")
        raw.append(
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "open": price, "high": price, "low": price, "close": price,
            }
        )
    split_day = start + timedelta(days=21)
    event = SimpleNamespace(
        event_date=split_day,
        cash_price_factor=None,
        share_price_factor=Decimal("0.25"),
        rights_price_factor=None,
    )
    indicators = build_indicator_rows(raw, (event,))
    assert indicators[-1]["sma21"] == Decimal("25")
    assert indicators[-1]["histogram"] == 0
    assert "dow_events" not in indicators[-1]
    assert indicators[-2]["technical_close"] == Decimal("100")

    rows = [
        _row(0, "90", "100", "0.1", low="80"),
        _row(1, "110", "100", "0.2", low="90"),
        _row(2, "27.5", "25", "0.1", low="22.5", factor="0.25"),
    ]
    run, _opportunities, audit = _run(rows)
    assert [row["role"] for row in run.transactions] == ["MOTHER"]
    assert audit[-1]["event"] == "OPEN_AT_CUTOFF"


def test_split_rebases_fixed_initial_stop_before_stop_check() -> None:
    defense = SimpleNamespace(defense=Decimal("9.5"), version=1)
    rows = [
        _row(0, "9", "10", "0.1", low="8"),
        _row(1, "11", "10", "0.2", low="9", dow_events=(defense,)),
        _row(2, "2.6", "2.5", "0.1", low="2.4", factor="0.25"),
        _row(3, "1.9", "2.5", "-0.1", low="1.8"),
    ]
    run, _opportunities, audit = _run(rows)
    assert run.transactions[-1]["exit_reason"] == "INITIAL_PROTECTIVE_STOP"
    assert any(row["event"] == "INITIAL_STOP_PRIORITY_OVER_MACD" for row in audit)


def test_accepted_baseline_fixture_rows_pass_exact_parity_checker() -> None:
    files = {
        "metrics": "scenario-summary.csv",
        "details": "trade-details.csv",
        "transactions": "transactions.csv",
        "curves": "daily-equity.csv",
        "actions": "corporate-actions.csv",
    }
    output = {}
    for key, name in files.items():
        with (ACCEPTED_RUN_ROOT / name).open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            output[key] = [
                row for row in csv.DictReader(handle)
                if row["strategy_id"] in {
                    "A_LONG", CURRENT_STRATEGY_ID,
                    "BUY_HOLD_CASH", "BUY_HOLD_REINVEST"
                }
            ]
    assert baseline_parity(output)["status"] == "PASS"


def test_strategy_identifier_is_isolated_from_accepted_methods() -> None:
    assert STRATEGY_ID == "MA21_FIXED_INITIAL_STOP_MACD_21_55_55"


def test_end_to_end_run_and_report_are_self_verified(tmp_path) -> None:
    run_root = tmp_path / "run"
    report_root = tmp_path / "report"
    standard_state = (
        standard_report.YEARS,
        standard_report.METHOD_ORDER,
        standard_report.TIMING_PREFIXES,
        standard_report.COLORS,
        dict(standard_report.EXIT_REASON_ZH),
    )

    result = run_study(output_root=run_root, report_root=report_root)

    assert (
        standard_report.YEARS,
        standard_report.METHOD_ORDER,
        standard_report.TIMING_PREFIXES,
        standard_report.COLORS,
        standard_report.EXIT_REASON_ZH,
    ) == standard_state
    assert result["status"] == "STOP_WAIT_00_QA"
    assert result["baseline_parity"] == "PASS"
    assert result["new_strategy_trade_rows"] == 47
    assert result["new_strategy_open_rows"] == 1
    for root in (run_root, report_root):
        readback = json.loads((root / "readback.json").read_text(encoding="utf-8"))
        assert readback == {"mismatches": [], "status": "PASS"}
    report_parity = json.loads(
        (report_root / "report-parity.json").read_text(encoding="utf-8")
    )
    assert report_parity["status"] == "PASS"
    with (run_root / "scenario-summary.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        assert [row["strategy_id"] for row in csv.DictReader(handle)] == [
            "A_LONG", CURRENT_STRATEGY_ID, STRATEGY_ID,
            "BUY_HOLD_CASH", "BUY_HOLD_REINVEST"
        ]
    with (run_root / "macd-event-audit.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        audit = list(csv.DictReader(handle))
    assert not any(row["event"].startswith("DOW_") for row in audit)
    assert any(row["event"] == "INITIAL_STOP_EXIT" for row in audit)
    with (run_root / "trade-details.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        fixed_details = [
            row for row in csv.DictReader(handle) if row["strategy_id"] == STRATEGY_ID
        ]
    assert fixed_details
    assert len(fixed_details) == 47
    assert all(row["initial_stop"] for row in fixed_details)
    with (run_root / "transactions.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        entry_stops = {
            row["leg_id"]: row["initial_stop_raw"] for row in csv.DictReader(handle)
            if row["strategy_id"] == STRATEGY_ID and row["role"] == "MOTHER"
        }
    assert {row["leg_id"]: row["initial_stop"] for row in fixed_details} == entry_stops
    trade_markdown = (report_root / "trade-details.md").read_text(encoding="utf-8")
    assert "賣出成交／截止估值價" in trade_markdown
    assert "截止日估算（尚未賣出）" in trade_markdown
    qa = json.loads(
        (run_root / "details" / "qa-recompute.json").read_text(encoding="utf-8")
    )
    assert qa["protected_trees_unchanged"] is True
    with (run_root / "details" / "all_scenarios_metrics.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        all_metrics = list(csv.DictReader(handle))
    assert len(all_metrics) == 15
    assert {int(row["slippage_bps"]) for row in all_metrics} == {0, 10, 20}
    fixed = [row for row in all_metrics if row["strategy_id"] == STRATEGY_ID]
    assert len(fixed) == 3
    assert all(Decimal(row["minimum_cash"]) >= 0 for row in all_metrics)
    assert all(Decimal(row["actual_fees_and_tax"]) > 0 for row in fixed)
    assert all(Decimal(row["dividends_received"]) > 0 for row in fixed)
