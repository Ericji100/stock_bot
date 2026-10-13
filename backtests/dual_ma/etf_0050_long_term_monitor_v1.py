"""0050-specific continuous monitoring lifecycle for the five timing rules.

The stock-selection engine treats one selection episode as finite: a regime
exit invalidates that episode and later signals require a new selection event.
0050 has no discrete selection episodes because it is monitored every session.
For this ETF-only adapter, each new false-to-true MA regime therefore starts a
fresh monitoring cycle and may accept a new complete D -> E -> V -> T signal.

No entry gate, exit rule, price coordinate, cost term, split treatment or
same-day re-entry rule is changed here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .contracts import RunManifest
from .etf_0050_base_v1 import (
    SYMBOL,
    ETF0050Error,
    SplitEvent,
    StrategyRunSlice,
    StrategySpec,
)
from .h1h2_long_fastma_extension_2022 import LongExtensionEntryEngine
from .h2_flat_reentry_2022h2 import ReentryAfterExitEngine
from .h2_ma144_risk_width_entry_2022h2 import RiskWidthEntryEngine
from .models import BacktestResult, Bar, ExitReason, InstrumentType, StrategyKind


LIFECYCLE_VERSION = "dual-ma-0050-continuous-regime-cycles-v1"

LONG_TERM_STRATEGY_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec("A_MEDIUM", "基本版－中線", StrategyKind.MEDIUM, "BASIC"),
    StrategySpec("A_LONG", "基本版－長線", StrategyKind.LONG, "BASIC"),
    StrategySpec("B_MEDIUM", "7%風險寬度－中線", StrategyKind.MEDIUM, "RISK_WIDTH"),
    StrategySpec("B_LONG", "7%風險寬度－長線", StrategyKind.LONG, "RISK_WIDTH"),
    StrategySpec(
        "C_LONG",
        "144日線向上＋風險寬度7%＋高於21日線5%－長線",
        StrategyKind.LONG,
        "EXTENSION",
    ),
)


class ETFContinuousRegimeCycleMixin:
    """Clear only a prior regime-exit lock at a new ETF MA regime cycle."""

    def run(self, symbol: str, bars: Any, **kwargs: Any) -> Any:
        self._etf_previous_regime: dict[StrategyKind, bool] = {}
        return super().run(symbol, bars, **kwargs)

    def _prepare_etf_regime_cycle(
        self, strategy: StrategyKind, current_regime: bool
    ) -> dict[str, Any] | None:
        previous_regime = self._etf_previous_regime.get(strategy, False)
        cycle_started = bool(current_regime and not previous_regime)
        cleared: tuple[date, ExitReason, str] | None = None
        if cycle_started:
            prior = self._last_closed.get(strategy)
            if prior is not None and prior[1] == ExitReason.REGIME_EXIT:
                cleared = self._last_closed.pop(strategy)
        self._etf_previous_regime[strategy] = bool(current_regime)
        if not cycle_started:
            return None
        return {
            "type": "ETF_REGIME_CYCLE_STARTED",
            "lifecycle_version": LIFECYCLE_VERSION,
            "previous_regime": previous_regime,
            "regime_exit_lock_cleared": cleared is not None,
            "prior_exit_date": None if cleared is None else cleared[0],
            "prior_exit_reason": None if cleared is None else cleared[1].value,
            "prior_leg_id": None if cleared is None else cleared[2],
        }

    def _process_strategy_day(self, **kwargs: Any) -> None:
        state = kwargs["state"]
        if state.kind == StrategyKind.MEDIUM:
            fast, slow = kwargs["sma5"], kwargs["sma21"]
        else:
            fast, slow = kwargs["sma21"], kwargs["sma144"]
        regime = fast is not None and slow is not None and fast > slow
        lifecycle_event = self._prepare_etf_regime_cycle(state.kind, regime)
        super()._process_strategy_day(**kwargs)
        if lifecycle_event is not None:
            decision = kwargs["result"].decisions[-1]
            if decision["strategy"] != state.kind.value:
                raise AssertionError("ETF lifecycle event attached to wrong strategy")
            decision["events"].insert(0, lifecycle_event)


class ETFContinuousBasicEngine(ETFContinuousRegimeCycleMixin, ReentryAfterExitEngine):
    pass


class ETFContinuousRiskWidthEngine(
    ETFContinuousRegimeCycleMixin, RiskWidthEntryEngine
):
    pass


class ETFContinuousExtensionEngine(
    ETFContinuousRegimeCycleMixin, LongExtensionEntryEngine
):
    pass


def build_long_term_engine(
    spec: StrategySpec,
    *,
    slippage_bps: int,
    gate_map: Mapping[date, Any],
    official_sessions: Sequence[date],
    extension_sma_map: Mapping[date, Decimal | None] | None = None,
) -> Any:
    common = {"slippage_bps": slippage_bps, "kd_2r_death_cross_exit": False}
    if spec.engine_kind == "BASIC":
        return ETFContinuousBasicEngine(**common)
    gated = {**common, "gate_map": gate_map, "official_sessions": official_sessions}
    if spec.engine_kind == "RISK_WIDTH":
        return ETFContinuousRiskWidthEngine(**gated)
    if spec.engine_kind == "EXTENSION":
        return ETFContinuousExtensionEngine(
            **gated, extension_sma_map=extension_sma_map
        )
    raise ETF0050Error(f"unknown engine kind: {spec.engine_kind}")


def run_five_long_term_interfaces(
    *,
    signal_bars: Sequence[Bar],
    raw_execution_bars: Sequence[Bar],
    split_events: Sequence[SplitEvent],
    gate_map: Mapping[date, Any],
    official_sessions: Sequence[date],
    manifest_factory: Callable[[str, int], RunManifest],
    slippage_bps: int,
    backtest_start: date,
    backtest_end: date,
    required_warmup_bars: int = 143,
    split_signal_coordinate_already_adjusted: bool = False,
    signal_price_factors_by_date: Mapping[
        date, Decimal | int | float | str
    ] | None = None,
    extension_sma_map: Mapping[date, Decimal | None] | None = None,
) -> tuple[StrategyRunSlice, ...]:
    """Run the five 0050 methods as continuous, repeatable MA regime cycles."""

    if [bar.date for bar in signal_bars] != [bar.date for bar in raw_execution_bars]:
        raise ETF0050Error("signal and raw execution bars are not date aligned")
    split_map = {event.effective_date: event.ratio for event in split_events}
    output: list[StrategyRunSlice] = []
    for spec in LONG_TERM_STRATEGY_SPECS:
        engine = build_long_term_engine(
            spec,
            slippage_bps=slippage_bps,
            gate_map=gate_map,
            official_sessions=official_sessions,
            extension_sma_map=extension_sma_map,
        )
        result: BacktestResult = engine.run(
            SYMBOL,
            signal_bars,
            run_manifest=manifest_factory("M1", slippage_bps),
            instrument_type=InstrumentType.ETF,
            backtest_start=backtest_start,
            backtest_end=backtest_end,
            required_warmup_bars=required_warmup_bars,
            execution_bars=raw_execution_bars,
            split_ratios_by_date=split_map,
            split_signal_coordinate_already_adjusted=(
                split_signal_coordinate_already_adjusted
            ),
            signal_price_factors_by_date=signal_price_factors_by_date,
        )
        wanted = spec.strategy_kind.value
        decisions = tuple(row for row in result.decisions if row["strategy"] == wanted)
        transactions = tuple(
            row for row in result.transactions if row["strategy"] == wanted
        )
        legs = tuple(leg for leg in result.legs if leg.strategy == spec.strategy_kind)
        output.append(StrategyRunSlice(spec, result, decisions, transactions, legs))
    return tuple(output)
