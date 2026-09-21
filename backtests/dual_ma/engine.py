from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import product
from typing import Callable, Iterable

from .contracts import (
    COST_MODEL,
    EXECUTION_MODEL,
    SPEC_SHA256,
    STRATEGY_VERSION,
    ContractValidationError,
    RunManifest,
    semantic_result_fingerprint,
    validate_daily_inputs,
)
from .costs import CATHAY_WEB_028_TW_V1, CostModel, OrderCost, size_entry
from .models import (
    AddVariant,
    BacktestResult,
    Bar,
    ExitReason,
    ExitVariant,
    InstrumentType,
    Leg,
    Pullback,
    SignalStatus,
    StrategyKind,
)
from .pivots import DowDefenseTracker


@dataclass
class _StrategyState:
    kind: StrategyKind
    pullback: Pullback | None = None
    pullback_sequence: int = 0
    open_legs: list[Leg] = field(default_factory=list)
    leg_sequence: int = 0
    episode_sequence: int = 0
    active_episode_id: str | None = None
    episode_leg_sequence: int = 0
    active_dow_defense: Decimal | None = None
    active_dow_version: int | None = None
    dow_lifecycle_sequence: int = 0
    aggregate_2r_armed: bool = False
    aggregate_2r_date: date | None = None
    below_21_count: int = 0


def _sma(closes: list[Decimal], window: int) -> Decimal | None:
    if len(closes) < window:
        return None
    return sum(closes[-window:], Decimal("0")) / window


def _order_dict(order: OrderCost) -> dict[str, object]:
    return {
        "side": order.side,
        "reference_price": order.reference_price,
        "fill_price": order.fill_price,
        "shares": order.shares,
        "gross": order.gross,
        "commission": order.commission,
        "tax": order.tax,
        "cash_amount": order.cash_amount,
    }


MATRIX_GROUPS = {
    (AddVariant.R1_AGGREGATE, ExitVariant.DOW_TRAIL): "M1",
    (AddVariant.R1_AGGREGATE, ExitVariant.R2_DOW_TRAIL): "M2",
    (AddVariant.R1_AGGREGATE, ExitVariant.R2_21MA_2CLOSE): "M3",
    (AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL): "M4",
    (AddVariant.SIGNAL_ONLY, ExitVariant.R2_DOW_TRAIL): "M5",
    (AddVariant.SIGNAL_ONLY, ExitVariant.R2_21MA_2CLOSE): "M6",
}


class DualMABacktestEngine:
    """Deterministic close-proxy simulator for one symbol and one matrix cell."""

    def __init__(
        self,
        add_variant: AddVariant,
        exit_variant: ExitVariant,
        *,
        slippage_bps: int = 0,
        cost_model: CostModel = CATHAY_WEB_028_TW_V1,
    ) -> None:
        self.add_variant = AddVariant(add_variant)
        self.exit_variant = ExitVariant(exit_variant)
        self.cost_model = cost_model.with_slippage(slippage_bps)

    def run(
        self,
        symbol: str,
        bars: Iterable[Bar],
        *,
        run_manifest: RunManifest,
        instrument_type: InstrumentType = InstrumentType.STOCK,
        backtest_start: date | None = None,
        backtest_end: date | None = None,
        required_warmup_bars: int = 143,
    ) -> BacktestResult:
        ordered = list(bars)
        matrix_group = MATRIX_GROUPS[(self.add_variant, self.exit_variant)]
        if run_manifest.matrix_id != matrix_group:
            raise ContractValidationError(
                f"manifest matrix_id {run_manifest.matrix_id} does not match {matrix_group}"
            )
        if run_manifest.slippage_bps != self.cost_model.slippage_bps:
            raise ContractValidationError(
                "manifest slippage_bps does not match engine slippage"
            )
        expected_cost_model = CATHAY_WEB_028_TW_V1.with_slippage(
            self.cost_model.slippage_bps
        )
        if self.cost_model != expected_cost_model:
            raise ContractValidationError(
                f"engine cost model terms must exactly match {COST_MODEL}"
            )
        audit = validate_daily_inputs(
            symbol,
            instrument_type,
            ordered,
            run_manifest,
            backtest_start=backtest_start,
            backtest_end=backtest_end,
            required_warmup_bars=required_warmup_bars,
        )
        symbol = audit.symbol
        instrument_type = audit.instrument_type
        result = BacktestResult(
            symbol=symbol,
            add_variant=self.add_variant,
            exit_variant=self.exit_variant,
            slippage_bps=self.cost_model.slippage_bps,
            matrix_group=matrix_group,
            instrument_type=instrument_type,
            strategy_version=STRATEGY_VERSION,
            spec_sha256=SPEC_SHA256,
            run_metadata=run_manifest.to_dict(),
            input_audit=audit.to_dict(),
            manifest_fingerprint=run_manifest.fingerprint,
            input_fingerprint=audit.input_fingerprint,
            assumptions={
                "spec_version": STRATEGY_VERSION,
                "spec_sha256": SPEC_SHA256,
                "execution_model": EXECUTION_MODEL,
                "cost_model": self.cost_model.model_id,
                "commission_rounding": "per order, nearest whole TWD, ROUND_HALF_UP",
                "minimum_fee": self.cost_model.minimum_fee_assumption,
                "fill_price_tick_rounding": (
                    "none; frozen spec does not define an exchange tick-rounding rule"
                ),
                "slippage_bps_per_side": self.cost_model.slippage_bps,
                "backtest_interval": "start and end dates are inclusive",
                "bias": "signal and fill both use the signal-day close",
                "equal_pivot_zone": "maximal contiguous equal-price extreme",
                "dow_scope": (
                    "medium and long keep independent active defense lifecycles; "
                    "a close break, regime end, or all-leg episode end invalidates "
                    "that strategy's defense"
                ),
            },
        )
        states = {
            StrategyKind.MEDIUM: _StrategyState(StrategyKind.MEDIUM),
            StrategyKind.LONG: _StrategyState(StrategyKind.LONG),
        }
        closes: list[Decimal] = []
        prefix: list[Bar] = []
        dow = DowDefenseTracker()

        for index, bar in enumerate(ordered):
            if bar.date > audit.backtest_end:
                break
            prefix.append(bar)
            closes.append(bar.close)
            sma5, sma21, sma144 = (_sma(closes, size) for size in (5, 21, 144))
            _, pivots, dow_events = dow.update(prefix)
            if bar.date < audit.backtest_start:
                continue
            for state in states.values():
                self._process_strategy_day(
                    result=result,
                    state=state,
                    bar=bar,
                    index=index,
                    sma5=sma5,
                    sma21=sma21,
                    sma144=sma144,
                    pivots=pivots,
                    dow_events=dow_events,
                    instrument_type=instrument_type,
                )
        result.semantic_result_fingerprint = semantic_result_fingerprint(result)
        return result

    def _process_strategy_day(
        self,
        *,
        result: BacktestResult,
        state: _StrategyState,
        bar: Bar,
        index: int,
        sma5: Decimal | None,
        sma21: Decimal | None,
        sma144: Decimal | None,
        pivots: list,
        dow_events: list,
        instrument_type: InstrumentType,
    ) -> None:
        fast, slow = (
            (sma5, sma21)
            if state.kind == StrategyKind.MEDIUM
            else (sma21, sma144)
        )
        regime = fast is not None and slow is not None and fast > slow
        events: list[dict[str, object]] = []
        had_open_legs_before_actions = bool(state.open_legs)
        before_actions = self._position_snapshot(
            state, bar.close, instrument_type
        )
        aggregate_pnl = self._aggregate_open_pnl(
            state.open_legs, bar.close, instrument_type
        )
        aggregate_r = sum((leg.initial_r for leg in state.open_legs), Decimal("0"))

        # The price-structure tracker emits qualified events. Each strategy owns
        # a separate active defense lifecycle, which can reset after a break or
        # regime termination and then accept a later, lower qualified defense.
        if self.exit_variant in (ExitVariant.DOW_TRAIL, ExitVariant.R2_DOW_TRAIL):
            if regime:
                for event in dow_events:
                    previous = state.active_dow_defense
                    if previous is None or event.defense > previous:
                        if previous is None:
                            state.dow_lifecycle_sequence += 1
                        state.active_dow_defense = event.defense
                        state.active_dow_version = event.version
                        events.append(
                            {
                                "type": (
                                    "DOW_DEFENSE_ESTABLISHED"
                                    if previous is None
                                    else "DOW_DEFENSE_RAISED"
                                ),
                                "from": previous,
                                "to": event.defense,
                                "lifecycle": state.dow_lifecycle_sequence,
                                "pivot_low_confirmed_date": (
                                    event.pivot_low.confirmed_date
                                ),
                                "breakout_date": event.breakout_date,
                                "defense_version": event.version,
                            }
                        )
                    else:
                        events.append(
                            {
                                "type": "DOW_DEFENSE_NOT_RAISED",
                                "candidate": event.defense,
                                "active": previous,
                                "defense_version": event.version,
                            }
                        )

            if state.active_dow_defense is not None:
                for leg in state.open_legs:
                    previous = leg.dow_stop
                    if previous is None or state.active_dow_defense > previous:
                        leg.dow_stop = state.active_dow_defense
                        events.append(
                            {
                                "type": "DOW_DEFENSE_APPLIED_TO_LEG",
                                "leg_id": leg.leg_id,
                                "from": previous,
                                "to": state.active_dow_defense,
                                "lifecycle": state.dow_lifecycle_sequence,
                                "defense_version": state.active_dow_version,
                            }
                        )

        if self.exit_variant in (ExitVariant.R2_DOW_TRAIL, ExitVariant.R2_21MA_2CLOSE):
            for leg in state.open_legs:
                pnl = self.cost_model.immediate_sale_pnl(
                    bar.close, leg.shares, leg.buy_total, instrument_type
                )
                if leg.two_r_date is None and pnl >= Decimal("2") * leg.initial_r:
                    leg.two_r_date = bar.date
                    leg.breakeven_stop = self.cost_model.breakeven_reference_price(
                        leg.shares, leg.buy_total, instrument_type
                    )
                    events.append(
                        {
                            "type": "LEG_2R_ARMED",
                            "leg_id": leg.leg_id,
                            "initial_r": leg.initial_r,
                            "unrealized_net_pnl": pnl,
                            "breakeven_stop": leg.breakeven_stop,
                        }
                    )

        if (
            self.exit_variant == ExitVariant.R2_21MA_2CLOSE
            and state.open_legs
            and not state.aggregate_2r_armed
            and aggregate_pnl >= Decimal("2") * aggregate_r
        ):
            state.aggregate_2r_armed = True
            state.aggregate_2r_date = bar.date
            events.append(
                {
                    "type": "AGGREGATE_2R_ARMED",
                    "aggregate_r": aggregate_r,
                    "unrealized_net_pnl": aggregate_pnl,
                }
            )

        for leg in state.open_legs:
            candidates = [leg.initial_stop]
            if self.exit_variant in (ExitVariant.R2_DOW_TRAIL, ExitVariant.R2_21MA_2CLOSE):
                if leg.breakeven_stop is not None:
                    candidates.append(leg.breakeven_stop)
            if self.exit_variant in (ExitVariant.DOW_TRAIL, ExitVariant.R2_DOW_TRAIL):
                if leg.dow_stop is not None:
                    candidates.append(leg.dow_stop)
            new_stop = max(candidates)
            if new_stop < leg.effective_stop:
                raise AssertionError("effective stop cannot decrease")
            leg.effective_stop = new_stop

        # Exit priority is structural stop, then profit exit, then regime exit.
        stopped = [leg for leg in state.open_legs if bar.close < leg.effective_stop]
        for leg in stopped:
            self._close_leg(
                result, state, leg, bar, ExitReason.STRUCTURAL_STOP, instrument_type
            )
            events.append(
                {
                    "type": "EXIT",
                    "leg_id": leg.leg_id,
                    "reason": ExitReason.STRUCTURAL_STOP.value,
                    "effective_stop": leg.effective_stop,
                }
            )

        if (
            state.active_dow_defense is not None
            and bar.close < state.active_dow_defense
        ):
            events.append(
                {
                    "type": "DOW_DEFENSE_INVALIDATED",
                    "reason": "CLOSE_BREAK",
                    "defense": state.active_dow_defense,
                    "lifecycle": state.dow_lifecycle_sequence,
                    "defense_version": state.active_dow_version,
                }
            )
            state.active_dow_defense = None
            state.active_dow_version = None

        if (
            self.exit_variant == ExitVariant.R2_21MA_2CLOSE
            and state.open_legs
            and state.aggregate_2r_armed
            and sma21 is not None
        ):
            if bar.close < sma21:
                state.below_21_count += 1
                events.append(
                    {
                        "type": (
                            "EXIT_WARNING"
                            if state.below_21_count == 1
                            else "PROFIT_EXIT_TRIGGER"
                        ),
                        "count": state.below_21_count,
                    }
                )
            else:
                if state.below_21_count:
                    events.append({"type": "MA21_BELOW_RESET"})
                state.below_21_count = 0
            if state.below_21_count >= 2:
                for leg in list(state.open_legs):
                    self._close_leg(
                        result, state, leg, bar, ExitReason.PROFIT_EXIT, instrument_type
                    )
                    events.append(
                        {
                            "type": "EXIT",
                            "leg_id": leg.leg_id,
                            "reason": ExitReason.PROFIT_EXIT.value,
                        }
                    )

        if not regime and state.open_legs:
            for leg in list(state.open_legs):
                self._close_leg(
                    result, state, leg, bar, ExitReason.REGIME_EXIT, instrument_type
                )
                events.append(
                    {
                        "type": "EXIT",
                        "leg_id": leg.leg_id,
                        "reason": ExitReason.REGIME_EXIT.value,
                    }
                )

        if not regime and state.active_dow_defense is not None:
            events.append(
                {
                    "type": "DOW_DEFENSE_INVALIDATED",
                    "reason": "REGIME_END",
                    "defense": state.active_dow_defense,
                    "lifecycle": state.dow_lifecycle_sequence,
                    "defense_version": state.active_dow_version,
                }
            )
            state.active_dow_defense = None
            state.active_dow_version = None

        if (
            had_open_legs_before_actions
            and not state.open_legs
            and state.active_dow_defense is not None
        ):
            events.append(
                {
                    "type": "DOW_DEFENSE_INVALIDATED",
                    "reason": "EPISODE_END",
                    "defense": state.active_dow_defense,
                    "lifecycle": state.dow_lifecycle_sequence,
                    "defense_version": state.active_dow_version,
                }
            )
            state.active_dow_defense = None
            state.active_dow_version = None

        if not state.open_legs:
            state.aggregate_2r_armed = False
            state.aggregate_2r_date = None
            state.below_21_count = 0
            state.active_episode_id = None
            state.episode_leg_sequence = 0

        reclaim = False
        signal_stop: Decimal | None = None
        pullback_event_id: str | None = None
        if not regime:
            if state.pullback is not None:
                events.append({"type": "PULLBACK_INVALIDATED_BY_REGIME"})
            state.pullback = None
        elif state.pullback is None:
            if fast is not None and bar.close < fast:
                state.pullback_sequence += 1
                state.pullback = Pullback(
                    index,
                    bar.date,
                    bar.low,
                    f"{state.kind.value}-PB{state.pullback_sequence}",
                )
                events.append(
                    {
                        "type": "PULLBACK_STARTED",
                        "pullback_event_id": state.pullback.event_id,
                        "start_date": bar.date,
                        "low": bar.low,
                    }
                )
        else:
            state.pullback.low = min(state.pullback.low, bar.low)
            if fast is not None and bar.close > fast and index > state.pullback.start_index:
                reclaim = True
                signal_stop = state.pullback.low
                pullback_event_id = state.pullback.event_id
                events.append(
                    {
                        "type": "RECLAIM_SIGNAL",
                        "pullback_event_id": pullback_event_id,
                        "pullback_start_date": state.pullback.start_date,
                        "pullback_low": signal_stop,
                    }
                )
                state.pullback = None

        if reclaim and signal_stop is not None:
            self._handle_entry_signal(
                result,
                state,
                bar,
                signal_stop,
                pullback_event_id,
                events,
                instrument_type,
            )

        result.decisions.append(
            {
                "date": bar.date,
                "symbol": result.symbol,
                "strategy": state.kind.value,
                "instrument_type": instrument_type.value,
                "matrix_group": result.matrix_group,
                "close": bar.close,
                "sma5": sma5,
                "sma21": sma21,
                "sma144": sma144,
                "regime": regime,
                "pullback_active": state.pullback is not None,
                "pullback_event_id": state.pullback.event_id if state.pullback else None,
                "pullback_low": state.pullback.low if state.pullback else None,
                "before_actions": before_actions,
                "after_actions": self._position_snapshot(
                    state, bar.close, instrument_type
                ),
                "confirmed_pivots": [
                    {
                        "kind": pivot.kind,
                        "value": pivot.value,
                        "zone_start_date": pivot.zone_start_date,
                        "zone_end_date": pivot.zone_end_date,
                        "confirmed_date": pivot.confirmed_date,
                    }
                    for pivot in pivots
                ],
                "events": events,
                "data_events": [],
            }
        )

    def _handle_entry_signal(
        self,
        result: BacktestResult,
        state: _StrategyState,
        bar: Bar,
        signal_stop: Decimal,
        pullback_event_id: str | None,
        events: list[dict[str, object]],
        instrument_type: InstrumentType,
    ) -> None:
        is_add = bool(state.open_legs)
        if is_add and state.episode_leg_sequence >= 4:
            events.append(
                {
                    "type": "ENTRY_DECISION",
                    "role": "ADD",
                    "status": SignalStatus.MAX_LEGS_REJECTED.value,
                    "pullback_event_id": pullback_event_id,
                }
            )
            return
        if is_add and self.add_variant == AddVariant.R1_AGGREGATE:
            pnl = self._aggregate_open_pnl(state.open_legs, bar.close, instrument_type)
            risk = sum((leg.initial_r for leg in state.open_legs), Decimal("0"))
            if pnl < risk:
                events.append(
                    {
                        "type": "ENTRY_DECISION",
                        "role": "ADD",
                        "status": SignalStatus.ADD_GATE_REJECTED.value,
                        "pullback_event_id": pullback_event_id,
                        "aggregate_unrealized_net_pnl": pnl,
                        "required_initial_r": risk,
                    }
                )
                return

        sized = size_entry(
            bar.close,
            signal_stop,
            instrument_type,
            self.cost_model,
        )
        role = "ADD" if is_add else "MOTHER"
        if sized is None:
            events.append(
                {
                    "type": "ENTRY_DECISION",
                    "role": role,
                    "status": SignalStatus.NON_EXECUTABLE.value,
                    "pullback_event_id": pullback_event_id,
                    "initial_stop": signal_stop,
                }
            )
            return

        if not is_add:
            state.episode_sequence += 1
            state.active_episode_id = f"{state.kind.value}-E{state.episode_sequence}"
            state.episode_leg_sequence = 0
        if state.active_episode_id is None:
            raise AssertionError("an add must belong to an active episode")
        state.episode_leg_sequence += 1
        state.leg_sequence += 1
        inherited_dow = (
            state.active_dow_defense
            if self.exit_variant in (ExitVariant.DOW_TRAIL, ExitVariant.R2_DOW_TRAIL)
            else None
        )
        if inherited_dow is not None and inherited_dow > sized.buy.fill_price:
            events.append(
                {
                    "type": "DOW_DEFENSE_INVALIDATED",
                    "reason": "ABOVE_NEW_ENTRY_FILL",
                    "defense": inherited_dow,
                    "entry_fill_price": sized.buy.fill_price,
                    "lifecycle": state.dow_lifecycle_sequence,
                    "defense_version": state.active_dow_version,
                }
            )
            state.active_dow_defense = None
            state.active_dow_version = None
            inherited_dow = None
        effective_stop = max(
            value for value in (signal_stop, inherited_dow) if value is not None
        )
        if effective_stop > sized.buy.fill_price:
            raise AssertionError("effective stop cannot exceed a new leg's entry fill")
        leg = Leg(
            leg_id=f"{state.kind.value}-L{state.leg_sequence}",
            symbol=result.symbol,
            strategy=state.kind,
            entry_date=bar.date,
            entry_reference_close=bar.close,
            entry_fill_price=sized.buy.fill_price,
            shares=sized.shares,
            buy_gross=sized.buy.gross,
            buy_commission=sized.buy.commission,
            buy_total=sized.buy.cash_amount,
            initial_stop=signal_stop,
            initial_r=sized.initial_r,
            effective_stop=effective_stop,
            episode_id=state.active_episode_id,
            leg_number=state.episode_leg_sequence,
            dow_stop=inherited_dow,
        )
        state.open_legs.append(leg)
        result.legs.append(leg)
        result.transactions.append(
            {
                "date": bar.date,
                "symbol": result.symbol,
                "strategy": state.kind.value,
                "instrument_type": instrument_type.value,
                "matrix_group": result.matrix_group,
                "leg_id": leg.leg_id,
                "episode_id": leg.episode_id,
                "leg_number": leg.leg_number,
                "pullback_event_id": pullback_event_id,
                "signal_date": bar.date,
                "execution_model": EXECUTION_MODEL,
                "slippage_bps_per_side": self.cost_model.slippage_bps,
                "role": role,
                "status": SignalStatus.FILLED.value,
                "initial_stop": signal_stop,
                "dow_stop": leg.dow_stop,
                "effective_stop": leg.effective_stop,
                "initial_r": leg.initial_r,
                **_order_dict(sized.buy),
            }
        )
        events.append(
            {
                "type": "ENTRY_DECISION",
                "role": role,
                "status": SignalStatus.FILLED.value,
                "leg_id": leg.leg_id,
                "episode_id": leg.episode_id,
                "leg_number": leg.leg_number,
                "pullback_event_id": pullback_event_id,
                "shares": leg.shares,
                "initial_stop": leg.initial_stop,
                "dow_stop": leg.dow_stop,
                "effective_stop": leg.effective_stop,
                "initial_r": leg.initial_r,
            }
        )

    def _position_snapshot(
        self,
        state: _StrategyState,
        reference_close: Decimal,
        instrument_type: InstrumentType,
    ) -> dict[str, object]:
        return {
            "valuation_reference_close": reference_close,
            "aggregate_unrealized_net_pnl": self._aggregate_open_pnl(
                state.open_legs, reference_close, instrument_type
            ),
            "aggregate_initial_r": sum(
                (leg.initial_r for leg in state.open_legs), Decimal("0")
            ),
            "open_leg_count": len(state.open_legs),
            "aggregate_2r_armed": state.aggregate_2r_armed,
            "below_21_count": state.below_21_count,
            "active_episode_id": state.active_episode_id,
            "episode_legs_created": state.episode_leg_sequence,
            "active_dow_defense": state.active_dow_defense,
            "active_dow_version": state.active_dow_version,
            "dow_lifecycle": state.dow_lifecycle_sequence,
            "open_legs": [
                {
                    "leg_id": leg.leg_id,
                    "episode_id": leg.episode_id,
                    "leg_number": leg.leg_number,
                    "shares": leg.shares,
                    "initial_r": leg.initial_r,
                    "initial_stop": leg.initial_stop,
                    "breakeven_stop": leg.breakeven_stop,
                    "dow_stop": leg.dow_stop,
                    "effective_stop": leg.effective_stop,
                    "two_r_date": leg.two_r_date,
                }
                for leg in state.open_legs
            ],
        }

    def _aggregate_open_pnl(
        self,
        legs: list[Leg],
        reference_close: Decimal,
        instrument_type: InstrumentType,
    ) -> Decimal:
        return sum(
            (
                self.cost_model.immediate_sale_pnl(
                    reference_close, leg.shares, leg.buy_total, instrument_type
                )
                for leg in legs
            ),
            Decimal("0"),
        )

    def _close_leg(
        self,
        result: BacktestResult,
        state: _StrategyState,
        leg: Leg,
        bar: Bar,
        reason: ExitReason,
        instrument_type: InstrumentType,
    ) -> None:
        sale = self.cost_model.sell(bar.close, leg.shares, instrument_type)
        leg.exit_date = bar.date
        leg.exit_reason = reason
        leg.exit_reference_close = bar.close
        leg.exit_fill_price = sale.fill_price
        leg.sell_gross = sale.gross
        leg.sell_commission = sale.commission
        leg.sell_tax = sale.tax
        leg.sell_net = sale.cash_amount
        leg.net_pnl = sale.cash_amount - leg.buy_total
        gross_pnl = sale.gross - leg.buy_gross
        state.open_legs.remove(leg)
        result.transactions.append(
            {
                "date": bar.date,
                "symbol": result.symbol,
                "strategy": state.kind.value,
                "instrument_type": instrument_type.value,
                "matrix_group": result.matrix_group,
                "leg_id": leg.leg_id,
                "episode_id": leg.episode_id,
                "leg_number": leg.leg_number,
                "role": "EXIT",
                "status": "FILLED",
                "exit_reason": reason.value,
                "effective_stop": leg.effective_stop,
                "net_pnl": leg.net_pnl,
                "gross_pnl": gross_pnl,
                "total_fees_and_tax": (
                    leg.buy_commission + sale.commission + sale.tax
                ),
                "execution_model": EXECUTION_MODEL,
                "slippage_bps_per_side": self.cost_model.slippage_bps,
                **_order_dict(sale),
            }
        )


def run_matrix(
    symbol: str,
    bars: Iterable[Bar],
    *,
    manifest_factory: Callable[[str, int], RunManifest],
    instrument_type: InstrumentType = InstrumentType.STOCK,
    slippage_scenarios: tuple[int, ...] = (0, 10, 20),
    backtest_start: date | None = None,
    backtest_end: date | None = None,
    required_warmup_bars: int = 143,
) -> dict[str, BacktestResult]:
    """Run the required 2 x 3 strategy matrix under each mandated slippage case."""
    materialized = list(bars)
    results: dict[str, BacktestResult] = {}
    seen_run_ids: set[str] = set()
    for add_variant, exit_variant, slippage_bps in product(
        AddVariant, ExitVariant, slippage_scenarios
    ):
        key = f"{add_variant.value}|{exit_variant.value}|{slippage_bps}bps"
        matrix_id = MATRIX_GROUPS[(add_variant, exit_variant)]
        manifest = manifest_factory(matrix_id, slippage_bps)
        if manifest.run_id in seen_run_ids:
            raise ContractValidationError(
                f"run_id must be unique within a matrix batch: {manifest.run_id}"
            )
        seen_run_ids.add(manifest.run_id)
        results[key] = DualMABacktestEngine(
            add_variant, exit_variant, slippage_bps=slippage_bps
        ).run(
            symbol,
            materialized,
            run_manifest=manifest,
            instrument_type=instrument_type,
            backtest_start=backtest_start,
            backtest_end=backtest_end,
            required_warmup_bars=required_warmup_bars,
        )
    return results
