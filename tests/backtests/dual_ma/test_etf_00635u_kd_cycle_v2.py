from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from backtests.dual_ma import etf_00635u_kd_cycle_formal_v2 as formal
from backtests.dual_ma import etf_00635u_kd_cycle_v2 as kd_cycle
from backtests.dual_ma import etf_00635u_research_v1 as research
from backtests.dual_ma.etf_00635u_strategy_v1 import (
    ExecutionTiming,
    IndicatorRow,
    RawETFRow,
)


START = date(2024, 1, 2)


def test_passive_comparator_metrics_retain_open_position_and_entry_cash():
    line = research.ComparisonLine(
        "PASSIVE",
        "TRADABLE_PASSIVE_COMPARATOR",
        ((START, Decimal("500000")), (START + timedelta(days=1), Decimal("510000"))),
        {
            "transactions": (
                {"cash_amount": Decimal("499994.89660")},
            ),
        },
    )

    metrics = formal._metrics_for_lines((line,), {})["PASSIVE"]

    assert metrics["closed_positions"] == 0
    assert metrics["open_positions"] == 1
    assert metrics["maximum_single_entry_cash"] == Decimal("499994.89660")


def _row(
    offset,
    entry_k,
    entry_d,
    *,
    close="10",
    low="9",
    high="11",
    ma21="10",
    ma144="20",
    raw_open=None,
    exit_k="60",
    exit_d="50",
):
    close = Decimal(close)
    base = IndicatorRow(
        day=START + timedelta(days=offset),
        raw_open=Decimal(raw_open) if raw_open is not None else close,
        raw_close=close,
        technical_close=close,
        technical_low=Decimal(low),
        sma21=None if ma21 is None else Decimal(ma21),
        sma144=None if ma144 is None else Decimal(ma144),
        macd_histogram=Decimal("0"),
        k=Decimal(exit_k),
        d=Decimal(exit_d),
    )
    return kd_cycle.KDEntryRow(
        base=base,
        technical_high=Decimal(high),
        entry_k=None if entry_k is None else Decimal(entry_k),
        entry_d=None if entry_d is None else Decimal(entry_d),
    )


def _valid_cycle(*, breakout_on_cross=False, k2="12", open_after=None):
    rows = [
        _row(0, "5", "20", close="9", low="8", ma21="10"),
        _row(1, "6", "20", close="9", low="7", ma21="10"),
        _row(2, "30", "20", close="10", low="9", high="12", ma21="10"),
        _row(3, "35", "20", close="9", low="8", high="13", ma21="10"),
        _row(4, k2, "20", close="9", low="8.5", ma21="10"),
    ]
    if breakout_on_cross:
        rows.extend([
            _row(5, k2, "20", close="9", low="8.4", ma21="10"),
            _row(6, "30", "20", close="11", low="9", ma21="10"),
        ])
    else:
        rows.append(_row(5, "13", "20", close="11", low="9", ma21="10"))
    if open_after is not None:
        rows.append(_row(len(rows), "31", "20", close="11", low="9", ma21="10", raw_open=open_after))
    return rows


def _death_day_signal_then_second_breakout(*, cancellation_open=None):
    """The historical duplicate path: signal on second-low start, then breakout again."""
    rows = [
        _row(0, "5", "20", close="9", low="8", ma21="10"),
        _row(1, "6", "20", close="9", low="7", ma21="10"),
        _row(2, "30", "20", close="10", low="9", high="12", ma21="10"),
        _row(3, "35", "20", close="9", low="8", high="13", ma21="10"),
        _row(4, "12", "20", close="11", low="9", ma21="10"),
        _row(5, "13", "20", close="9", low="8.5", ma21="10"),
        _row(6, "14", "20", close="11", low="9", ma21="10"),
        _row(
            7, "30", "20", close="11", low="9", ma21="10",
            raw_open=cancellation_open,
        ),
    ]
    return rows


def test_contract_pin_is_exact():
    assert kd_cycle.CONTRACT_SHA256 == (
        "2cffeef16decb2b2747ccdff63d2c16143a5d25c03441b8d8858e2d71dc062fc"
    )
    assert kd_cycle.sha256_file(kd_cycle.CONTRACT) == kd_cycle.CONTRACT_SHA256


def test_corrected_parameter_grid_is_3_by_3_by_3_per_strategy():
    assert formal.GRID_MA_VALUES == (18, 21, 24)
    assert formal.GRID_K_VALUES == (8, 9, 10)
    assert formal.GRID_D_VALUES == (45, 55, 65)
    assert len(formal.GRID_PARAMETERS) == 27
    assert (21, 9, 55) in formal.GRID_PARAMETERS


def test_entry_kd_is_raw_ohlc_derived_and_prefix_stable():
    raw = [
        RawETFRow(
            day=START + timedelta(days=index),
            open=Decimal("10") + Decimal(index % 5) / 10,
            high=Decimal("11") + Decimal(index % 5) / 10,
            low=Decimal("9") + Decimal(index % 5) / 10,
            close=Decimal("10") + Decimal(index % 5) / 10,
        )
        for index in range(40)
    ]
    prefix = kd_cycle.build_kd_entry_rows(raw[:25])
    full = kd_cycle.build_kd_entry_rows(raw)
    assert [row.entry_k for row in prefix] == [row.entry_k for row in full[:25]]
    assert [row.entry_d for row in prefix] == [row.entry_d for row in full[:25]]
    assert prefix[7].entry_k is None and prefix[7].entry_d is None
    assert prefix[8].entry_k is not None and prefix[8].entry_d is not None
    assert kd_cycle.ENTRY_RSV_PERIOD == 9
    assert kd_cycle.ENTRY_K_PERIOD == 9
    assert kd_cycle.ENTRY_D_PERIOD == 55
    explicit = kd_cycle.build_kd_entry_rows(
        raw, entry_k_period=9, entry_d_period=55
    )
    assert full == explicit


def test_named_date_regression_requires_k_above_d_and_keeps_rule_evidence():
    rows = []
    for line_id in (
        "KD_CYCLE_D_SIGNAL_CLOSE",
        "KD_CYCLE_D_NEXT_SESSION_OPEN",
    ):
        rows.extend([
            {
                "line_id": line_id, "date": date(2022, 10, 5),
                "entry_k": Decimal("45.14"), "entry_d": Decimal("37.50"),
                "cycle_id": "CYCLE1", "cycle_phase": "SEEK_FIRST_LOW",
                "cycle_status": "SEEKING_FIRST_LOW", "k1": Decimal("12.19"),
                "k1_date": date(2022, 7, 21), "k2_so_far": Decimal("14.95"),
                "k2_low_date": date(2022, 9, 28),
                "k2_strictly_above_k1": True, "ma21_breakout": False,
                "in_signal_window": False, "signal_generated": False,
                "execution_status": "", "execution_detail": "",
            },
            {
                "line_id": line_id, "date": date(2022, 11, 7),
                "entry_k": Decimal("48.67"), "entry_d": Decimal("39.08"),
                "cycle_id": "CYCLE2", "cycle_phase": "MIDDLE_HIGH",
                "cycle_status": "MIDDLE_HIGH_UPDATED", "k1": Decimal("26.40"),
                "k1_date": date(2022, 10, 21), "k2_so_far": None,
                "k2_low_date": None, "k2_strictly_above_k1": False,
                "ma21_breakout": True, "in_signal_window": False,
                "signal_generated": False, "execution_status": "",
                "execution_detail": "",
            },
        ])

    result = formal._named_date_regression(rows)

    assert len(result) == 4
    assert all(row["k_relation"] == "K_GT_D" for row in result)
    november = [row for row in result if row["date"] == date(2022, 11, 7)]
    assert all(row["cycle_phase"] == "MIDDLE_HIGH" for row in november)
    assert all(row["ma21_breakout"] is True for row in november)
    assert all(row["in_signal_window"] is False for row in november)
    assert all(row["signal_generated"] is False for row in november)
    assert all(row["execution_status"] == "NO_ORDER" for row in november)


def test_kd_equalities_are_boundaries_and_incomplete_edges_do_not_signal():
    rows = [
        _row(0, None, None),
        _row(1, "10", "10"),
        _row(2, "9", "10"),
        _row(3, "10", "10"),
        _row(4, "11", "10"),
    ]
    audit, signals = kd_cycle.scan_kd_cycles(rows, kd_cycle.KDCyclePolicy.D)
    assert not signals
    assert all(row["entry_k_relation"] != "EQUAL_SEGMENT" for row in audit)
    assert any(row["entry_k_relation"] == "EQUAL_BOUNDARY" for row in audit)


def test_l1_and_k1_can_be_different_days_and_ties_use_earliest_day():
    rows = _valid_cycle()
    rows[1] = _row(1, "6", "20", close="9", low="7", ma21="10")
    audit, signals = kd_cycle.scan_kd_cycles(rows, kd_cycle.KDCyclePolicy.D)
    assert len(signals) == 1
    signal = signals[0]
    assert signal["k1"] == Decimal("5")
    assert signal["k1_date"] == rows[0].day
    assert signal["l1"] == Decimal("7")
    assert signal["l1_date"] == rows[1].day

    tied = list(rows)
    tied[1] = _row(1, "5", "20", close="9", low="8", ma21="10")
    tied.insert(2, _row(1.5, "6", "20", close="9", low="8", ma21="10"))
    # Use an explicit distinct date while retaining equal minima.
    tied[2] = replace(tied[2], base=replace(tied[2].base, day=START + timedelta(days=2)))
    tied = [replace(row, base=replace(row.base, day=START + timedelta(days=i))) for i, row in enumerate(tied)]
    _, tied_signals = kd_cycle.scan_kd_cycles(tied, kd_cycle.KDCyclePolicy.D)
    assert tied_signals[0]["k1_date"] == tied[0].day
    assert tied_signals[0]["l1_date"] == tied[0].day


def test_k2_must_be_strictly_greater_than_k1():
    _audit, equal_signals = kd_cycle.scan_kd_cycles(
        _valid_cycle(k2="5"), kd_cycle.KDCyclePolicy.D
    )
    _audit, higher_signals = kd_cycle.scan_kd_cycles(
        _valid_cycle(k2="5.0001"), kd_cycle.KDCyclePolicy.D
    )
    assert equal_signals == []
    assert len(higher_signals) == 1


def test_breakout_is_legal_inside_second_low_or_on_golden_cross_day():
    low_audit, low_signals = kd_cycle.scan_kd_cycles(
        _valid_cycle(), kd_cycle.KDCyclePolicy.D
    )
    cross_audit, cross_signals = kd_cycle.scan_kd_cycles(
        _valid_cycle(breakout_on_cross=True), kd_cycle.KDCyclePolicy.D
    )
    assert len(low_signals) == len(cross_signals) == 1
    assert low_signals[0]["breakout_kd_state"] == "SECOND_K_LT_D"
    assert low_signals[0]["same_day_golden_cross"] is False
    assert cross_signals[0]["breakout_kd_state"] == "SECOND_END_GOLDEN_CROSS"
    assert cross_signals[0]["same_day_golden_cross"] is True
    assert any(row["ma21_breakout"] is False for row in low_audit)
    assert any(row["ma21_breakout"] is True for row in cross_audit)


def test_expired_cycle_rolls_forward_and_consumed_cycle_never_repeats():
    rows = _valid_cycle(breakout_on_cross=True)
    rows[-1] = _row(6, "30", "20", close="9", low="8", ma21="10")
    rows.extend([
        _row(7, "12", "20", close="9", low="7.5", ma21="10"),
        _row(8, "30", "20", close="10", low="9", high="14", ma21="10"),
        _row(9, "35", "20", close="9", low="8", high="15", ma21="10"),
        _row(10, "13", "20", close="9", low="8", ma21="10"),
        _row(11, "14", "20", close="11", low="9", ma21="10"),
        _row(12, "15", "20", close="12", low="10", ma21="10"),
    ])
    audit, signals = kd_cycle.scan_kd_cycles(rows, kd_cycle.KDCyclePolicy.D)
    assert any(row["cycle_status"] == "EXPIRED_ROLLED_FORWARD" for row in audit)
    assert len(signals) == 1
    assert len({row["cycle_id"] for row in signals}) == 1


def test_filled_signal_on_death_cross_consumes_cycle_immediately():
    result = kd_cycle.run_kd_cycle_strategy(
        _death_day_signal_then_second_breakout(),
        policy=kd_cycle.KDCyclePolicy.D,
        timing=ExecutionTiming.SIGNAL_CLOSE,
    )
    signals = [row for row in result.cycle_audit if row["signal_generated"]]
    cycle_entries = [
        row for row in result.state.trades
        if row["role"] == "ENTRY"
        and row["entry_type"] == "KD_9_9_55_HIGHER_LOW_MA21_BREAKOUT"
    ]
    assert len(signals) == len(cycle_entries) == 1
    assert any(
        row["cycle_status"] == "PIVOT_HISTORY_REQUIRED"
        for row in result.cycle_audit
    )
    assert signals[0]["pivot_end"] < signals[0]["date"]
    assert signals[0]["initial_stop"] == Decimal("8.5")
    assert signals[0]["execution_status"] == "FILLED"
    assert result.cycle_audit[-1]["cycle_status"] == "CONSUMED_PATTERN_NOT_REUSED"
    assert kd_cycle.assert_cycle_consumption(
        result.cycle_audit, result.state.trades
    )["maximum_signal_generated_per_line_cycle"] == 1


def test_position_already_open_signal_consumes_cycle_immediately():
    rows = [
        _row(0, "30", "20", close="9", low="8", ma21="10", ma144="1"),
        _row(1, "31", "20", close="11", low="9", ma21="10", ma144="1"),
        _row(2, "5", "20", close="9", low="9", ma21="10", ma144="20"),
        _row(3, "6", "20", close="9", low="9", ma21="10", ma144="20"),
        _row(4, "30", "20", close="10", low="9", high="12", ma21="10", ma144="20"),
        _row(5, "35", "20", close="9", low="9", high="13", ma21="10", ma144="20"),
        _row(6, "12", "20", close="11", low="9", ma21="10", ma144="20"),
        _row(7, "13", "20", close="9", low="9", ma21="10", ma144="20"),
        _row(8, "14", "20", close="11", low="9", ma21="10", ma144="20"),
        _row(9, "15", "20", close="11", low="9", ma21="10", ma144="20"),
    ]
    result = kd_cycle.run_kd_cycle_strategy(
        rows, policy=kd_cycle.KDCyclePolicy.C,
        timing=ExecutionTiming.SIGNAL_CLOSE,
    )
    signals = [row for row in result.cycle_audit if row["signal_generated"]]
    assert len(signals) == 1
    assert signals[0]["execution_status"] == "EXCLUDED"
    assert signals[0]["execution_detail"] == "POSITION_ALREADY_OPEN"
    assert result.cycle_audit[-1]["cycle_status"] == "CONSUMED_PATTERN_NOT_REUSED"


def test_next_open_cancellation_consumes_cycle_immediately():
    result = kd_cycle.run_kd_cycle_strategy(
        _death_day_signal_then_second_breakout(cancellation_open="8.5"),
        policy=kd_cycle.KDCyclePolicy.D,
        timing=ExecutionTiming.NEXT_SESSION_OPEN,
    )
    signals = [row for row in result.cycle_audit if row["signal_generated"]]
    assert len(signals) == 1
    assert signals[0]["execution_status"] == "CANCELLED_OPEN_AT_OR_BELOW_STOP"
    assert not result.state.trades
    assert result.cycle_audit[-1]["cycle_status"] == "CONSUMED_PATTERN_NOT_REUSED"


def test_pivot_stop_requires_strict_close_below_and_equal_does_not_exit():
    rows = _valid_cycle() + [
        _row(6, "30", "20", close="8.5", low="8.4", ma21="10"),
        _row(7, "31", "20", close="8.49", low="8.3", ma21="10"),
    ]
    result = kd_cycle.run_kd_cycle_strategy(
        rows, policy=kd_cycle.KDCyclePolicy.D,
        timing=ExecutionTiming.SIGNAL_CLOSE,
    )
    entry = next(row for row in result.state.trades if row["role"] == "ENTRY")
    exit_trade = next(row for row in result.state.trades if row["role"] == "EXIT")
    assert entry["initial_stop"] == Decimal("8.5")
    assert exit_trade["signal_date"] == rows[7].day
    assert exit_trade["exit_reason"] == "INITIAL_PROTECTIVE_STOP"


def test_strategy_c_regime_boundaries_and_d_ignores_ma144():
    low = _valid_cycle()
    _, c_low = kd_cycle.scan_kd_cycles(low, kd_cycle.KDCyclePolicy.C)
    assert len(c_low) == 1 and c_low[0]["strategy_branch"] == "C_LOW_KD_CYCLE"

    equal = [replace(row, base=replace(row.base, sma144=row.base.sma21)) for row in low]
    _, c_equal = kd_cycle.scan_kd_cycles(equal, kd_cycle.KDCyclePolicy.C)
    assert c_equal == []

    high = [replace(row, base=replace(row.base, sma144=Decimal("1"))) for row in low]
    _, c_high = kd_cycle.scan_kd_cycles(high, kd_cycle.KDCyclePolicy.C)
    assert c_high == []

    _, d_equal = kd_cycle.scan_kd_cycles(equal, kd_cycle.KDCyclePolicy.D)
    _, d_high = kd_cycle.scan_kd_cycles(high, kd_cycle.KDCyclePolicy.D)
    assert len(d_equal) == len(d_high) == 1


def test_strategy_c_uses_original_high_breakout_and_does_not_force_regime_exit():
    high_rows = [
        _row(0, "5", "20", close="9", low="8", ma21="10", ma144="1"),
        _row(1, "6", "20", close="11", low="9", ma21="10", ma144="1"),
        _row(2, "7", "20", close="12", low="10", ma21="10", ma144="20"),
    ]
    high_result = kd_cycle.run_kd_cycle_strategy(
        high_rows, policy=kd_cycle.KDCyclePolicy.C,
        timing=ExecutionTiming.SIGNAL_CLOSE,
    )
    entries = [row for row in high_result.state.trades if row["role"] == "ENTRY"]
    assert len(entries) == 1
    assert entries[0]["entry_type"] == "HIGH_REGIME_MA21_BREAKOUT"
    assert not [row for row in high_result.state.trades if row["role"] == "EXIT"]
    assert high_result.state.position is not None

    low_result = kd_cycle.run_kd_cycle_strategy(
        _valid_cycle() + [
            _row(6, "30", "20", close="12", low="10", ma21="10", ma144="1")
        ],
        policy=kd_cycle.KDCyclePolicy.C,
        timing=ExecutionTiming.SIGNAL_CLOSE,
    )
    assert low_result.state.position is not None
    assert not [row for row in low_result.state.trades if row["role"] == "EXIT"]


def test_strategy_d_ma144_rewrite_has_exact_order_event_and_account_parity():
    rows = _valid_cycle()
    rewritten = [
        replace(row, base=replace(row.base, sma144=Decimal(index + 1)))
        for index, row in enumerate(rows)
    ]
    baseline = kd_cycle.run_kd_cycle_strategy(
        rows, policy=kd_cycle.KDCyclePolicy.D,
        timing=ExecutionTiming.SIGNAL_CLOSE,
    )
    replay = kd_cycle.run_kd_cycle_strategy(
        rewritten, policy=kd_cycle.KDCyclePolicy.D,
        timing=ExecutionTiming.SIGNAL_CLOSE,
    )
    ignored = {"entry_sma144_diagnostic"}
    clean = lambda records: [
        {key: value for key, value in row.items() if key not in ignored}
        for row in records
    ]
    assert clean(baseline.state.trades) == clean(replay.state.trades)
    assert baseline.state.events == replay.state.events
    assert baseline.state.daily == replay.state.daily


def test_next_open_at_or_below_signal_low_is_cancelled():
    for opening in ("8.5", "8.49"):
        result = kd_cycle.run_kd_cycle_strategy(
            _valid_cycle(open_after=opening),
            policy=kd_cycle.KDCyclePolicy.D,
            timing=ExecutionTiming.NEXT_SESSION_OPEN,
        )
        assert not result.state.trades
        assert any(
            row["event"] == "ENTRY_REJECTED_GAP_AT_OR_BELOW_STOP"
            for row in result.state.events
        )
        assert any(
            row["execution_status"] == "CANCELLED_OPEN_AT_OR_BELOW_STOP"
            for row in result.cycle_audit
        )


def test_fixed_stop_precedes_kd_and_daily_accounting_is_conserved():
    rows = _valid_cycle()
    rows.extend([
        _row(6, "30", "20", close="30", low="10", ma21="10", exit_k="70", exit_d="50"),
        _row(7, "31", "20", close="8", low="7", ma21="10", exit_k="40", exit_d="50"),
    ])
    result = kd_cycle.run_kd_cycle_strategy(
        rows, policy=kd_cycle.KDCyclePolicy.D,
        timing=ExecutionTiming.SIGNAL_CLOSE,
    )
    entry = next(row for row in result.state.trades if row["role"] == "ENTRY")
    assert entry["entry_type"] == "KD_9_9_55_HIGHER_LOW_MA21_BREAKOUT"
    exits = [row for row in result.state.trades if row["role"] == "EXIT"]
    assert exits[-1]["exit_reason"] == "INITIAL_PROTECTIVE_STOP"
    assert any(row["event"] == "STOP_PRIORITY_OVER_KD" for row in result.state.events)
    assert any(row["event"] == "KD_2R_PERMANENTLY_ARMED" for row in result.state.events)
    assert all(
        row["equity"] == row["cash"] + row["liquidation_value"]
        and row["cash"] >= 0
        for row in result.state.daily
    )
    filled = next(row for row in result.cycle_audit if row.get("position_id"))
    assert filled["fee_adjusted_two_r_threshold"] == filled["initial_r"] * 2
    assert filled["two_r_armed_date"] is not None
    assert filled["exit_reason"] == "INITIAL_PROTECTIVE_STOP"
    assert filled["stop_priority_evidence"] == "STOP_PRIORITY_OVER_KD"
