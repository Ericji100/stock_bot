from datetime import date, timedelta
from decimal import Decimal

from backtests.dual_ma.costs import CostModel
from backtests.dual_ma.engine import DualMABacktestEngine, _StrategyState
from backtests.dual_ma.models import (
    AddVariant,
    BacktestResult,
    Bar,
    ExitReason,
    ExitVariant,
    InstrumentType,
    Leg,
    DowDefenseEvent,
    Pivot,
    Pullback,
    StrategyKind,
)


START = date(2026, 1, 1)


def make_bar(index: int, close: str, low: str | None = None) -> Bar:
    value = Decimal(close)
    low_value = Decimal(low) if low is not None else value - Decimal("1")
    return Bar.from_values(
        START + timedelta(days=index),
        value,
        value + Decimal("1"),
        low_value,
        value,
    )


def make_leg(
    engine: DualMABacktestEngine,
    leg_id: str,
    entry: str,
    stop: str,
    shares: int = 10,
) -> Leg:
    entry_value = Decimal(entry)
    stop_value = Decimal(stop)
    buy = engine.cost_model.buy(entry_value, shares)
    stop_sale = engine.cost_model.sell(stop_value, shares, InstrumentType.STOCK)
    return Leg(
        leg_id=leg_id,
        symbol="FAKE",
        strategy=StrategyKind.MEDIUM,
        entry_date=START,
        entry_reference_close=entry_value,
        entry_fill_price=buy.fill_price,
        shares=shares,
        buy_gross=buy.gross,
        buy_commission=buy.commission,
        buy_total=buy.cash_amount,
        initial_stop=stop_value,
        initial_r=buy.cash_amount - stop_sale.cash_amount,
        effective_stop=stop_value,
        episode_id="MEDIUM_5_21-E1",
        leg_number=1,
    )


def empty_result(engine: DualMABacktestEngine) -> BacktestResult:
    return BacktestResult(
        symbol="FAKE",
        add_variant=engine.add_variant,
        exit_variant=engine.exit_variant,
        slippage_bps=0,
    )


def dow_event(defense: str, version: int, index: int) -> DowDefenseEvent:
    pivot_high = Pivot(
        "HIGH",
        Decimal("10"),
        0,
        0,
        2,
        START,
        START,
        START + timedelta(days=2),
    )
    pivot_low = Pivot(
        "LOW",
        Decimal(defense),
        3,
        3,
        5,
        START + timedelta(days=3),
        START + timedelta(days=3),
        START + timedelta(days=5),
    )
    return DowDefenseEvent(
        defense=Decimal(defense),
        pivot_low=pivot_low,
        prior_pivot_high=pivot_high,
        breakout_index=index,
        breakout_date=START + timedelta(days=index),
        activated_index=index,
        activated_date=START + timedelta(days=index),
        version=version,
    )


def process(
    engine: DualMABacktestEngine,
    result: BacktestResult,
    state: _StrategyState,
    index: int,
    close: str,
    *,
    low: str | None = None,
    sma5: str = "110",
    sma21: str = "100",
    sma144: str = "90",
    dow_events: list | None = None,
) -> None:
    engine._process_strategy_day(
        result=result,
        state=state,
        bar=make_bar(index, close, low),
        index=index,
        sma5=Decimal(sma5),
        sma21=Decimal(sma21),
        sma144=Decimal(sma144),
        pivots=[],
        dow_events=dow_events or [],
        instrument_type=InstrumentType.STOCK,
    )


def test_close_equal_to_effective_defense_does_not_stop_out() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)
    state = _StrategyState(StrategyKind.MEDIUM)
    leg = make_leg(engine, "L1", "100", "90")
    leg.dow_stop = Decimal("95")
    leg.effective_stop = Decimal("95")
    state.open_legs.append(leg)
    result = empty_result(engine)

    process(engine, result, state, 1, "95", sma5="110", sma21="100")

    assert leg.is_open
    assert not [tx for tx in result.transactions if tx.get("exit_reason")]


def test_add_does_not_loosen_an_existing_leg_stop() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)
    state = _StrategyState(StrategyKind.MEDIUM)
    old = make_leg(engine, "L1", "100", "90")
    old.dow_stop = Decimal("105")
    old.effective_stop = Decimal("105")
    state.open_legs.append(old)
    state.episode_sequence = 1
    state.active_episode_id = old.episode_id
    state.episode_leg_sequence = 1
    state.pullback = Pullback(0, START, Decimal("100"))
    result = empty_result(engine)

    process(engine, result, state, 1, "120", low="110")

    assert len(state.open_legs) == 2
    assert old.effective_stop == Decimal("105")
    assert state.open_legs[1].initial_stop == Decimal("100")


def test_new_leg_uses_latest_confirmed_dow_defense() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)
    state = _StrategyState(
        StrategyKind.MEDIUM,
        pullback=Pullback(0, START, Decimal("90")),
        active_dow_defense=Decimal("95"),
        active_dow_version=1,
        dow_lifecycle_sequence=1,
    )
    result = empty_result(engine)

    process(
        engine,
        result,
        state,
        1,
        "120",
        low="110",
    )

    assert state.open_legs[0].initial_stop == Decimal("90")
    assert state.open_legs[0].dow_stop == Decimal("95")
    assert state.open_legs[0].effective_stop == Decimal("95")
    decision = result.decisions[-1]
    assert decision["before_actions"]["open_leg_count"] == 0
    assert decision["after_actions"]["open_leg_count"] == 1
    assert decision["after_actions"]["aggregate_initial_r"] == state.open_legs[0].initial_r


def test_episode_limit_is_cumulative_and_exited_leg_does_not_restore_slot() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)
    state = _StrategyState(
        StrategyKind.MEDIUM,
        pullback=Pullback(0, START, Decimal("90")),
        episode_sequence=1,
        active_episode_id="MEDIUM_5_21-E1",
        episode_leg_sequence=4,
    )
    state.open_legs.extend(
        make_leg(engine, f"L{number}", "100", "90") for number in range(1, 4)
    )
    result = empty_result(engine)

    process(engine, result, state, 1, "120", low="110")

    assert len(state.open_legs) == 3
    assert state.episode_leg_sequence == 4
    assert any(
        event.get("status") == "MAX_LEGS_REJECTED"
        for event in result.decisions[-1]["events"]
    )

    # Finish the remaining legs; a subsequent reclaim starts a fresh episode.
    for leg in state.open_legs:
        leg.dow_stop = Decimal("115")
        leg.effective_stop = Decimal("115")
    process(engine, result, state, 2, "110", low="109", sma5="120", sma21="100")
    assert not state.open_legs
    assert state.active_episode_id is None
    process(engine, result, state, 3, "130", low="125", sma5="120", sma21="100")

    assert len(state.open_legs) == 1
    assert state.open_legs[0].episode_id == "MEDIUM_5_21-E2"
    assert state.open_legs[0].leg_number == 1
    assert state.episode_leg_sequence == 1


def test_broken_dow_defense_is_not_inherited_and_new_cycle_can_start_lower() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)
    state = _StrategyState(
        StrategyKind.MEDIUM,
        pullback=Pullback(0, START, Decimal("2")),
        active_dow_defense=Decimal("5"),
        active_dow_version=1,
        dow_lifecycle_sequence=1,
    )
    result = empty_result(engine)

    # Main-controller reproducer: close/entry 4 must not inherit stale defense 5.
    process(
        engine,
        result,
        state,
        1,
        "4",
        low="3",
        sma5="3.5",
        sma21="3",
        sma144="2",
    )

    leg = state.open_legs[0]
    assert state.active_dow_defense is None
    assert leg.dow_stop is None
    assert leg.effective_stop == Decimal("2")
    assert leg.effective_stop <= leg.entry_fill_price
    assert any(
        event.get("reason") == "CLOSE_BREAK"
        for event in result.decisions[-1]["events"]
    )

    process(
        engine,
        result,
        state,
        2,
        "6",
        sma5="5",
        sma21="4",
        sma144="3",
        dow_events=[dow_event("3", 2, 2)],
    )
    assert state.active_dow_defense == Decimal("3")
    assert state.dow_lifecycle_sequence == 2
    assert leg.effective_stop == Decimal("3")


def test_regime_end_clears_defense_before_later_reentry() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)
    state = _StrategyState(
        StrategyKind.MEDIUM,
        active_dow_defense=Decimal("5"),
        active_dow_version=1,
        dow_lifecycle_sequence=1,
    )
    result = empty_result(engine)

    process(engine, result, state, 1, "6", sma5="3", sma21="4", sma144="2")
    assert state.active_dow_defense is None
    assert any(
        event.get("reason") == "REGIME_END"
        for event in result.decisions[-1]["events"]
    )

    state.pullback = Pullback(1, START + timedelta(days=1), Decimal("2"))
    process(engine, result, state, 2, "4", low="3", sma5="3.5", sma21="3")
    assert state.open_legs[0].dow_stop is None
    assert state.open_legs[0].effective_stop <= state.open_legs[0].entry_fill_price


def test_all_leg_episode_end_clears_unbroken_dow_defense() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.R2_DOW_TRAIL)
    state = _StrategyState(
        StrategyKind.MEDIUM,
        episode_sequence=1,
        active_episode_id="MEDIUM_5_21-E1",
        episode_leg_sequence=1,
        active_dow_defense=Decimal("95"),
        active_dow_version=1,
        dow_lifecycle_sequence=1,
    )
    leg = make_leg(engine, "L1", "100", "90")
    leg.breakeven_stop = Decimal("105")
    leg.effective_stop = Decimal("105")
    state.open_legs.append(leg)
    result = empty_result(engine)

    process(engine, result, state, 1, "100", sma5="110", sma21="90")

    assert not state.open_legs
    assert state.active_dow_defense is None
    assert any(
        event.get("reason") == "EPISODE_END"
        for event in result.decisions[-1]["events"]
    )


def test_medium_and_long_dow_lifecycles_are_independent() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)
    medium = _StrategyState(
        StrategyKind.MEDIUM,
        active_dow_defense=Decimal("5"),
        active_dow_version=1,
        dow_lifecycle_sequence=1,
    )
    long = _StrategyState(
        StrategyKind.LONG,
        active_dow_defense=Decimal("5"),
        active_dow_version=1,
        dow_lifecycle_sequence=1,
    )

    process(
        engine,
        empty_result(engine),
        medium,
        1,
        "6",
        sma5="3",
        sma21="4",
        sma144="2",
    )
    process(
        engine,
        empty_result(engine),
        long,
        1,
        "6",
        sma5="3",
        sma21="4",
        sma144="2",
    )

    assert medium.active_dow_defense is None
    assert long.active_dow_defense == Decimal("5")


def test_lower_dow_event_does_not_loosen_same_active_lifecycle() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)
    state = _StrategyState(
        StrategyKind.MEDIUM,
        active_dow_defense=Decimal("5"),
        active_dow_version=1,
        dow_lifecycle_sequence=1,
    )
    result = empty_result(engine)

    process(engine, result, state, 1, "10", dow_events=[dow_event("4", 2, 1)])

    assert state.active_dow_defense == Decimal("5")
    assert any(
        event["type"] == "DOW_DEFENSE_NOT_RAISED"
        for event in result.decisions[-1]["events"]
    )


def test_each_leg_arms_2r_on_its_own_date() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.R2_DOW_TRAIL)
    state = _StrategyState(StrategyKind.MEDIUM)
    first = make_leg(engine, "L1", "100", "95")
    second = make_leg(engine, "L2", "110", "80")
    state.open_legs.extend([first, second])
    result = empty_result(engine)

    process(engine, result, state, 1, "112")
    assert first.two_r_date == START + timedelta(days=1)
    assert second.two_r_date is None

    process(engine, result, state, 2, "180")
    assert second.two_r_date == START + timedelta(days=2)


def test_21ma_two_close_counter_resets_between_breaches() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.R2_21MA_2CLOSE)
    state = _StrategyState(StrategyKind.MEDIUM, aggregate_2r_armed=True)
    leg = make_leg(engine, "L1", "60", "50")
    state.open_legs.append(leg)
    result = empty_result(engine)

    process(engine, result, state, 1, "99")
    assert state.below_21_count == 1 and leg.is_open
    process(engine, result, state, 2, "101")
    assert state.below_21_count == 0 and leg.is_open
    process(engine, result, state, 3, "99")
    assert state.below_21_count == 1 and leg.is_open
    process(engine, result, state, 4, "98")

    assert not leg.is_open
    assert leg.exit_reason == ExitReason.PROFIT_EXIT


def test_structural_stop_has_priority_over_profit_and_regime_exit() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.R2_21MA_2CLOSE)
    state = _StrategyState(
        StrategyKind.MEDIUM,
        aggregate_2r_armed=True,
        below_21_count=1,
    )
    leg = make_leg(engine, "L1", "100", "95")
    state.open_legs.append(leg)
    result = empty_result(engine)

    process(engine, result, state, 1, "94", sma5="90", sma21="100")

    assert leg.exit_reason == ExitReason.STRUCTURAL_STOP
    exits = [tx for tx in result.transactions if tx.get("exit_reason")]
    assert [tx["exit_reason"] for tx in exits] == [ExitReason.STRUCTURAL_STOP.value]


def test_r1_add_gate_rejects_a_losing_add_but_signal_only_accepts_it() -> None:
    states = []
    for variant in (AddVariant.R1_AGGREGATE, AddVariant.SIGNAL_ONLY):
        candidate = DualMABacktestEngine(variant, ExitVariant.DOW_TRAIL)
        state = _StrategyState(StrategyKind.MEDIUM)
        seeded = make_leg(candidate, "L1", "100", "90")
        state.open_legs.append(seeded)
        state.episode_sequence = 1
        state.active_episode_id = seeded.episode_id
        state.episode_leg_sequence = 1
        state.pullback = Pullback(0, START, Decimal("90"))
        result = empty_result(candidate)
        process(candidate, result, state, 1, "100", low="95", sma5="95", sma21="80")
        states.append((result, state))

    rejected_events = states[0][0].decisions[-1]["events"]
    assert any(event.get("status") == "ADD_GATE_REJECTED" for event in rejected_events)
    assert len(states[0][1].open_legs) == 1
    assert len(states[1][1].open_legs) == 2


def test_decision_snapshots_match_partial_exit_state() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)
    state = _StrategyState(
        StrategyKind.MEDIUM,
        episode_sequence=1,
        active_episode_id="MEDIUM_5_21-E1",
        episode_leg_sequence=2,
    )
    first = make_leg(engine, "L1", "100", "95")
    second = make_leg(engine, "L2", "100", "90")
    state.open_legs.extend([first, second])
    result = empty_result(engine)

    process(engine, result, state, 1, "94")
    decision = result.decisions[-1]

    assert decision["before_actions"]["open_leg_count"] == 2
    assert decision["after_actions"]["open_leg_count"] == 1
    assert decision["after_actions"]["open_legs"][0]["leg_id"] == "L2"
    assert decision["after_actions"]["aggregate_initial_r"] == second.initial_r
    assert decision["after_actions"]["aggregate_unrealized_net_pnl"] == (
        engine.cost_model.immediate_sale_pnl(
            Decimal("94"), second.shares, second.buy_total, InstrumentType.STOCK
        )
    )


def test_decision_snapshots_match_full_exit_state() -> None:
    engine = DualMABacktestEngine(AddVariant.SIGNAL_ONLY, ExitVariant.DOW_TRAIL)
    state = _StrategyState(
        StrategyKind.MEDIUM,
        episode_sequence=1,
        active_episode_id="MEDIUM_5_21-E1",
        episode_leg_sequence=1,
    )
    state.open_legs.append(make_leg(engine, "L1", "100", "95"))
    result = empty_result(engine)

    process(engine, result, state, 1, "94")
    decision = result.decisions[-1]

    assert decision["before_actions"]["open_leg_count"] == 1
    assert decision["after_actions"]["open_leg_count"] == 0
    assert decision["after_actions"]["aggregate_initial_r"] == Decimal("0")
    assert decision["after_actions"]["aggregate_unrealized_net_pnl"] == Decimal("0")
    assert decision["after_actions"]["open_legs"] == []
