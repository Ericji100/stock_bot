from datetime import date

from backtests.dual_ma.etf_0050_long_term_monitor_v1 import (
    ETFContinuousBasicEngine,
    LIFECYCLE_VERSION,
    LONG_TERM_STRATEGY_SPECS,
)
from backtests.dual_ma.etf_0050_long_term_replay_v1 import entry_opportunity_audit
from backtests.dual_ma.etf_0050_base_v1 import StrategyRunSlice
from backtests.dual_ma.models import BacktestResult
from backtests.dual_ma.models import ExitReason, StrategyKind


def test_five_specs_make_all_stacked_long_gates_explicit() -> None:
    assert [spec.strategy_id for spec in LONG_TERM_STRATEGY_SPECS] == [
        "A_MEDIUM",
        "A_LONG",
        "B_MEDIUM",
        "B_LONG",
        "C_LONG",
    ]
    assert LONG_TERM_STRATEGY_SPECS[-1].label == (
        "144日線向上＋風險寬度7%＋高於21日線5%－長線"
    )


def test_new_bullish_cycle_clears_only_regime_exit_lock() -> None:
    engine = ETFContinuousBasicEngine()
    engine._etf_previous_regime = {StrategyKind.MEDIUM: False}
    engine._last_closed[StrategyKind.MEDIUM] = (
        date(2022, 8, 26),
        ExitReason.REGIME_EXIT,
        "old-leg",
    )

    event = engine._prepare_etf_regime_cycle(StrategyKind.MEDIUM, True)

    assert event is not None
    assert event["lifecycle_version"] == LIFECYCLE_VERSION
    assert event["regime_exit_lock_cleared"] is True
    assert StrategyKind.MEDIUM not in engine._last_closed


def test_structural_stop_is_not_cleared_by_new_cycle() -> None:
    engine = ETFContinuousBasicEngine()
    engine._etf_previous_regime = {StrategyKind.LONG: False}
    prior = (date(2024, 4, 19), ExitReason.STRUCTURAL_STOP, "stopped-leg")
    engine._last_closed[StrategyKind.LONG] = prior

    event = engine._prepare_etf_regime_cycle(StrategyKind.LONG, True)

    assert event is not None
    assert event["regime_exit_lock_cleared"] is False
    assert engine._last_closed[StrategyKind.LONG] == prior


def test_same_bullish_cycle_does_not_clear_again() -> None:
    engine = ETFContinuousBasicEngine()
    engine._etf_previous_regime = {StrategyKind.MEDIUM: True}
    engine._last_closed[StrategyKind.MEDIUM] = (
        date(2026, 3, 5),
        ExitReason.REGIME_EXIT,
        "leg",
    )

    assert engine._prepare_etf_regime_cycle(StrategyKind.MEDIUM, True) is None
    assert StrategyKind.MEDIUM in engine._last_closed


def test_opportunity_audit_distinguishes_filled_and_gate_rejections() -> None:
    spec = LONG_TERM_STRATEGY_SPECS[-1]
    result = object.__new__(BacktestResult)
    run = StrategyRunSlice(
        spec,
        result,
        (
            {
                "date": date(2024, 8, 14),
                "events": [
                    {"type": "ETF_REGIME_CYCLE_STARTED"},
                    {"type": "ENTRY_DECISION", "status": "FILLED"},
                ],
            },
            {
                "date": date(2024, 9, 2),
                "events": [
                    {
                        "type": "ENTRY_DECISION",
                        "status": "ENTRY_GATE_EXTENSION_BELOW_5PCT",
                    }
                ],
            },
        ),
        (),
        (),
    )

    rows = entry_opportunity_audit(run, slippage_bps=20)
    total = next(row for row in rows if row["period"] == "2022-2026")

    assert total["candidate_decisions"] == 2
    assert total["filled"] == 1
    assert total["extension_below_5pct"] == 1
    assert total["regime_cycles"] == 1
