"""Fixture-safe 00635U Strategy A/B engine.

This module deliberately contains no market-data downloader and no formal-result
entry point.  Formal runs are gated by :mod:`etf_00635u_prep_v1` until 00 accepts
the immutable data manifest.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .costs import CATHAY_WEB_028_TW_V1, CostModel, OrderCost, size_entry
from .models import InstrumentType, decimal


LISTING_DATE = date(2015, 4, 15)
INITIAL_CAPITAL = Decimal("500000")
MAIN_SLIPPAGE_BPS = 20
STRESS_SLIPPAGE_BPS = (20, 40, 60)
CORE_ALLOCATIONS = (0, 20, 40, 60)
GRID_SIZE = 243
MACD_FAST = 21
MACD_SLOW = 55
MACD_SIGNAL = 55
KD_RSV_PERIOD = 9
KD_K_PERIOD = 9
KD_D_PERIOD = 55


class StrategyVersion(str, Enum):
    A = "A_FIXED_STOP_MACD_21_55_55"
    B = "B_FIXED_STOP_2R_KD_9_9_55"


class ExecutionTiming(str, Enum):
    SIGNAL_CLOSE = "SIGNAL_CLOSE"
    NEXT_SESSION_OPEN = "NEXT_SESSION_OPEN"


class EntryType(str, Enum):
    HIGH_MA21 = "HIGH_REGIME_MA21_BREAKOUT"
    LOW_MACD = "LOW_REGIME_MACD_GREEN_TO_RED"


class ExitReason(str, Enum):
    INITIAL_STOP = "INITIAL_PROTECTIVE_STOP"
    MACD = "MACD_RED_TO_GREEN"
    KD_2R = "KD_2R_DEATH_CROSS_EXIT"


@dataclass(frozen=True)
class ETFResearchConfig:
    symbol: str = "00635U"
    name_zh: str = "期元大S&P黃金"
    listing_date: date = LISTING_DATE
    initial_capital: Decimal = INITIAL_CAPITAL
    instrument_type: InstrumentType = InstrumentType.ETF
    slippage_bps: int = MAIN_SLIPPAGE_BPS
    strategy: StrategyVersion = StrategyVersion.B
    timing: ExecutionTiming = ExecutionTiming.NEXT_SESSION_OPEN

    @property
    def cost_model(self) -> CostModel:
        return CATHAY_WEB_028_TW_V1.with_research_slippage(self.slippage_bps)


@dataclass(frozen=True)
class IndicatorRow:
    day: date
    raw_open: Decimal | None
    raw_close: Decimal
    technical_close: Decimal
    technical_low: Decimal
    sma21: Decimal | None
    sma144: Decimal | None
    macd_histogram: Decimal | None
    k: Decimal | None
    d: Decimal | None
    price_rebase_factor: Decimal = Decimal("1")
    share_factor: Decimal = Decimal("1")
    cash_dividend_per_share: Decimal = Decimal("0")
    dividend_event_id: str = ""
    dividend_stage: str = ""
    volume: Decimal = Decimal("1")
    official_session: bool = True
    tradable: bool = True
    tradability_reason: str = ""

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "IndicatorRow":
        def optional(name: str) -> Decimal | None:
            value = row.get(name)
            return None if value is None else decimal(value)

        return cls(
            day=row.get("day", row.get("date")),
            raw_open=(None if row.get("raw_open") is None else decimal(row["raw_open"])),
            raw_close=decimal(row["raw_close"]),
            technical_close=decimal(row["technical_close"]),
            technical_low=decimal(row["technical_low"]),
            sma21=optional("sma21"),
            sma144=optional("sma144"),
            macd_histogram=optional("macd_histogram"),
            k=optional("k"),
            d=optional("d"),
            price_rebase_factor=decimal(row.get("price_rebase_factor", 1)),
            share_factor=decimal(row.get("share_factor", 1)),
            cash_dividend_per_share=decimal(row.get("cash_dividend_per_share", 0)),
            dividend_event_id=str(row.get("dividend_event_id", "")),
            dividend_stage=str(row.get("dividend_stage", "")),
            volume=decimal(row.get("volume", 1)),
            official_session=bool(row.get("official_session", True)),
            tradable=bool(row.get("tradable", True)),
            tradability_reason=str(row.get("tradability_reason", "")),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.day, date):
            raise TypeError("row day/date must be datetime.date")
        prices = (self.raw_close, self.technical_close, self.technical_low)
        if any(value <= 0 for value in prices):
            raise ValueError("prices must be positive")
        if self.raw_open is not None and self.raw_open <= 0:
            raise ValueError("raw_open must be positive when present")
        if self.technical_low > self.technical_close:
            raise ValueError("technical_low cannot exceed technical_close")
        if self.price_rebase_factor <= 0 or self.share_factor <= 0:
            raise ValueError("corporate-action factors must be positive")
        if self.cash_dividend_per_share < 0:
            raise ValueError("cash dividend cannot be negative")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")
        if self.dividend_stage not in ("", "ENTITLEMENT", "PAYMENT"):
            raise ValueError("dividend_stage must be ENTITLEMENT or PAYMENT")
        if self.dividend_stage and not self.dividend_event_id:
            raise ValueError("staged dividend requires dividend_event_id")


@dataclass(frozen=True)
class RawETFRow:
    day: date
    open: Decimal | None
    high: Decimal
    low: Decimal
    close: Decimal
    price_rebase_factor: Decimal = Decimal("1")
    share_factor: Decimal = Decimal("1")
    cash_dividend_per_share: Decimal = Decimal("0")
    dividend_event_id: str = ""
    dividend_stage: str = ""
    volume: Decimal = Decimal("1")
    official_session: bool = True
    tradable: bool = True
    tradability_reason: str = ""

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "RawETFRow":
        day = row.get("day", row.get("date"))
        if isinstance(day, str):
            day = date.fromisoformat(day)
        return cls(
            day=day,
            open=None if row.get("open") is None else decimal(row["open"]),
            high=decimal(row["high"]),
            low=decimal(row["low"]),
            close=decimal(row["close"]),
            price_rebase_factor=decimal(row.get("price_rebase_factor", 1)),
            share_factor=decimal(row.get("share_factor", 1)),
            cash_dividend_per_share=decimal(row.get("cash_dividend_per_share", 0)),
            dividend_event_id=str(row.get("dividend_event_id", "")),
            dividend_stage=str(row.get("dividend_stage", "")),
            volume=decimal(row.get("volume", 1)),
            official_session=bool(row.get("official_session", True)),
            tradable=bool(row.get("tradable", True)),
            tradability_reason=str(row.get("tradability_reason", "")),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.day, date):
            raise TypeError("row day/date must be datetime.date")
        if self.low > self.close or self.high < self.close:
            raise ValueError("raw OHLC values are inconsistent")
        if self.open is not None and (
            self.low > self.open or self.high < self.open
        ):
            raise ValueError("raw OHLC values are inconsistent")
        if self.low <= 0 or self.price_rebase_factor <= 0 or self.share_factor <= 0:
            raise ValueError("raw prices and corporate-action factors must be positive")


def _ema(previous: Decimal | None, value: Decimal, span: int) -> Decimal:
    if previous is None:
        return value
    alpha = Decimal("2") / Decimal(span + 1)
    return value * alpha + previous * (Decimal("1") - alpha)


def build_causal_indicator_rows(
    rows: Sequence[RawETFRow | Mapping[str, Any]],
    *,
    ma_fast: int = 21,
    ma_slow: int = 144,
    macd_fast: int = MACD_FAST,
    macd_slow: int = MACD_SLOW,
    macd_signal: int = MACD_SIGNAL,
) -> list[IndicatorRow]:
    """Build prefix-stable MA/MACD and canonical KD(9,9,55) values."""

    normalized = [row if isinstance(row, RawETFRow) else RawETFRow.from_mapping(row) for row in rows]
    if any(left.day >= right.day for left, right in zip(normalized, normalized[1:])):
        raise ValueError("raw rows must be strictly increasing by date")
    if min(ma_fast, ma_slow, macd_fast, macd_slow, macd_signal) < 1:
        raise ValueError("indicator periods must be positive")
    closes: list[Decimal] = []
    kd_highs: list[Decimal] = []
    kd_lows: list[Decimal] = []
    ema_fast_value: Decimal | None = None
    ema_slow_value: Decimal | None = None
    dea: Decimal | None = None
    k_value: Decimal | None = None
    d_value: Decimal | None = None
    output: list[IndicatorRow] = []
    for raw in normalized:
        factor = raw.price_rebase_factor
        if factor != 1:
            closes = [value * factor for value in closes]
            kd_highs = [value * factor for value in kd_highs]
            kd_lows = [value * factor for value in kd_lows]
            if ema_fast_value is not None:
                ema_fast_value *= factor
            if ema_slow_value is not None:
                ema_slow_value *= factor
            if dea is not None:
                dea *= factor
        closes.append(raw.close)
        kd_highs.append(raw.high)
        kd_lows.append(raw.low)
        kd_highs = kd_highs[-KD_RSV_PERIOD:]
        kd_lows = kd_lows[-KD_RSV_PERIOD:]
        ema_fast_value = _ema(ema_fast_value, raw.close, macd_fast)
        ema_slow_value = _ema(ema_slow_value, raw.close, macd_slow)
        dif = ema_fast_value - ema_slow_value
        dea = _ema(dea, dif, macd_signal)
        histogram = dif - dea
        if len(kd_highs) == KD_RSV_PERIOD:
            highest, lowest = max(kd_highs), min(kd_lows)
            if highest != lowest:
                rsv = (raw.close - lowest) / (highest - lowest) * Decimal("100")
                rsv = max(Decimal("0"), min(Decimal("100"), rsv))
                k_value = (
                    rsv
                    if k_value is None
                    else rsv / Decimal(KD_K_PERIOD)
                    + k_value * (Decimal("1") - Decimal("1") / Decimal(KD_K_PERIOD))
                )
                d_value = (
                    k_value
                    if d_value is None
                    else k_value / Decimal(KD_D_PERIOD)
                    + d_value * (Decimal("1") - Decimal("1") / Decimal(KD_D_PERIOD))
                )
        output.append(
            IndicatorRow(
                day=raw.day,
                raw_open=raw.open,
                raw_close=raw.close,
                technical_close=raw.close,
                technical_low=raw.low,
                sma21=(
                    sum(closes[-ma_fast:], Decimal("0")) / Decimal(ma_fast)
                    if len(closes) >= ma_fast
                    else None
                ),
                sma144=(
                    sum(closes[-ma_slow:], Decimal("0")) / Decimal(ma_slow)
                    if len(closes) >= ma_slow
                    else None
                ),
                macd_histogram=histogram,
                k=k_value,
                d=d_value,
                price_rebase_factor=raw.price_rebase_factor,
                share_factor=raw.share_factor,
                cash_dividend_per_share=raw.cash_dividend_per_share,
                dividend_event_id=raw.dividend_event_id,
                dividend_stage=raw.dividend_stage,
                volume=raw.volume,
                official_session=raw.official_session,
                tradable=raw.tradable,
                tradability_reason=raw.tradability_reason,
            )
        )
    return output


@dataclass
class Position:
    position_id: str
    entry_type: EntryType
    signal_date: date
    entry_date: date
    shares: int
    buy: OrderCost
    technical_entry: Decimal
    initial_stop: Decimal
    initial_r: Decimal
    macd_armed: bool
    cash_distributions: Decimal = Decimal("0")
    kd_2r_armed: bool = False
    kd_2r_arm_date: date | None = None


@dataclass
class PendingOrder:
    side: str
    signal_date: date
    entry_type: EntryType | None = None
    technical_entry: Decimal | None = None
    initial_stop: Decimal | None = None
    histogram_at_signal: Decimal | None = None
    exit_reason: ExitReason | None = None


@dataclass
class EngineState:
    cash: Decimal = INITIAL_CAPITAL
    position: Position | None = None
    pending: PendingOrder | None = None
    previous: IndicatorRow | None = None
    last_processed_day: date | None = None
    phase_low: Decimal | None = None
    position_sequence: int = 0
    event_sequence: int = 0
    dividend_receivables: dict[str, dict[str, Any]] = field(default_factory=dict)
    paid_dividend_event_ids: list[str] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class RunResult:
    config: ETFResearchConfig
    state: EngineState

    @property
    def final_equity(self) -> Decimal:
        if not self.state.daily:
            return self.state.cash
        return self.state.daily[-1]["equity"]

    def checkpoint(self) -> dict[str, Any]:
        return state_to_dict(self.state)


def _order_dict(order: OrderCost) -> dict[str, Any]:
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


def _emit_event(state: EngineState, payload: Mapping[str, Any]) -> dict[str, Any]:
    state.event_sequence += 1
    event = {"event_id": f"EVT{state.event_sequence:08d}", **dict(payload)}
    state.events.append(event)
    return event


def _ready(row: IndicatorRow, strategy: StrategyVersion) -> bool:
    common = (
        row.sma21 is not None
        and row.sma144 is not None
        and row.macd_histogram is not None
    )
    if strategy == StrategyVersion.B:
        return common and row.k is not None and row.d is not None
    return common


def _signal_observable(row: IndicatorRow) -> bool:
    return row.official_session and row.tradable and row.volume > 0


def _can_execute_open(row: IndicatorRow) -> bool:
    return _signal_observable(row) and row.raw_open is not None


def _can_execute_close(row: IndicatorRow) -> bool:
    return _signal_observable(row)


def _regime(row: IndicatorRow) -> str:
    if row.sma21 is None or row.sma144 is None:
        return "WARMUP"
    return "HIGH" if row.sma21 >= row.sma144 else "LOW"


def _entry_signal(
    previous: IndicatorRow | None,
    row: IndicatorRow,
    strategy: StrategyVersion,
    listing_date: date,
) -> EntryType | None:
    if (
        previous is None
        or not _signal_observable(row)
        or not _ready(row, strategy)
        or not _ready(previous, strategy)
    ):
        return None
    if row.day < listing_date:
        return None
    if _regime(row) == "HIGH":
        if previous.technical_close <= previous.sma21 and row.technical_close > row.sma21:
            return EntryType.HIGH_MA21
    elif previous.macd_histogram <= 0 and row.macd_histogram > 0:
        return EntryType.LOW_MACD
    return None


def _candidate_stop(
    phase_low: Decimal | None,
    previous: IndicatorRow | None,
    row: IndicatorRow,
) -> Decimal:
    candidates = [row.technical_low]
    if phase_low is not None:
        candidates.append(phase_low)
    if previous is not None:
        candidates.append(previous.technical_low)
    return min(candidates)


def _maximum_affordable_shares(
    cash: Decimal, reference_price: Decimal, cost_model: CostModel
) -> int:
    upper = int(cash / cost_model._fill_price(reference_price, "BUY"))
    while upper > 0 and cost_model.buy(reference_price, upper).cash_amount > cash:
        upper -= 1
    return upper


def _record_entry(
    state: EngineState,
    config: ETFResearchConfig,
    *,
    signal_date: date,
    fill_date: date,
    reference_price: Decimal,
    technical_entry: Decimal,
    initial_stop: Decimal,
    entry_type: EntryType,
    histogram: Decimal,
) -> None:
    if initial_stop >= technical_entry:
        _emit_event(state, {
            "date": fill_date,
            "event": "ENTRY_REJECTED_INVALID_TECHNICAL_STOP",
            "initial_stop": initial_stop,
        })
        return
    if reference_price <= initial_stop:
        _emit_event(state, {
            "date": fill_date,
            "event": "ENTRY_REJECTED_GAP_AT_OR_BELOW_STOP",
            "signal_date": signal_date,
            "fill_reference_price": reference_price,
            "initial_stop": initial_stop,
        })
        return
    sized = size_entry(
        reference_price,
        initial_stop,
        config.instrument_type,
        cost_model=config.cost_model,
        max_total_cost=state.cash,
        max_initial_r=state.cash * Decimal("0.20"),
        max_shares=1_000_000,
    )
    if sized is None:
        one_share_cost = config.cost_model.buy(reference_price, 1).cash_amount
        reason = (
            "ENTRY_REJECTED_NO_CASH"
            if one_share_cost > state.cash
            else "ENTRY_REJECTED_RISK_LIMIT"
        )
        _emit_event(state, {
            "date": fill_date,
            "event": reason,
            "available_cash": state.cash,
            "one_share_cost": one_share_cost,
        })
        return
    shares = sized.shares
    buy = sized.buy
    initial_r = sized.initial_r
    state.cash -= buy.cash_amount
    state.position_sequence += 1
    state.position = Position(
        position_id=f"P{state.position_sequence:06d}",
        entry_type=entry_type,
        signal_date=signal_date,
        entry_date=fill_date,
        shares=shares,
        buy=buy,
        technical_entry=technical_entry,
        initial_stop=initial_stop,
        initial_r=initial_r,
        macd_armed=entry_type == EntryType.LOW_MACD or histogram >= 0,
    )
    state.trades.append({
        "role": "ENTRY",
        "signal_date": signal_date,
        "fill_date": fill_date,
        "entry_type": entry_type.value,
        "position_id": state.position.position_id,
        **_order_dict(buy),
        "initial_stop": initial_stop,
        "initial_r": initial_r,
        "timing": config.timing.value,
    })
    _emit_event(state, {
        "date": fill_date,
        "signal_date": signal_date,
        "event": "ENTRY_FILLED",
        "position_id": state.position.position_id,
        "entry_type": entry_type.value,
        "shares": shares,
    })


def _record_exit(
    state: EngineState,
    config: ETFResearchConfig,
    *,
    signal_date: date,
    fill_date: date,
    reference_price: Decimal,
    reason: ExitReason,
) -> None:
    position = state.position
    if position is None:
        raise AssertionError("cannot sell without a position")
    sale = config.cost_model.sell(reference_price, position.shares, config.instrument_type)
    state.cash += sale.cash_amount
    state.trades.append({
        "role": "EXIT",
        "signal_date": signal_date,
        "fill_date": fill_date,
        "entry_type": position.entry_type.value,
        "position_id": position.position_id,
        "exit_reason": reason.value,
        **_order_dict(sale),
        "cash_distributions": position.cash_distributions,
        "kd_2r_arm_date": position.kd_2r_arm_date,
        "net_pnl": (
            sale.cash_amount
            + position.cash_distributions
            - position.buy.cash_amount
        ),
        "timing": config.timing.value,
    })
    _emit_event(state, {
        "date": fill_date,
        "signal_date": signal_date,
        "event": "EXIT_FILLED",
        "position_id": position.position_id,
        "exit_reason": reason.value,
        "kd_2r_arm_date": position.kd_2r_arm_date,
        "shares": position.shares,
    })
    state.position = None


def _apply_corporate_action(state: EngineState, row: IndicatorRow) -> None:
    if row.price_rebase_factor == 1 and row.share_factor == 1:
        return
    if state.position is not None:
        new_shares = decimal(state.position.shares) * row.share_factor
        if new_shares != new_shares.to_integral_value():
            raise ValueError("share factor must preserve whole shares")
        state.position.shares = int(new_shares)
        state.position.initial_stop *= row.price_rebase_factor
        state.position.technical_entry *= row.price_rebase_factor
    if state.pending is not None and state.pending.initial_stop is not None:
        state.pending.initial_stop *= row.price_rebase_factor
        if state.pending.technical_entry is not None:
            state.pending.technical_entry *= row.price_rebase_factor
    if state.phase_low is not None:
        state.phase_low *= row.price_rebase_factor
    _emit_event(state, {
        "date": row.day,
        "event": "CORPORATE_ACTION_REBASE",
        "position_id": "" if state.position is None else state.position.position_id,
        "price_rebase_factor": row.price_rebase_factor,
        "share_factor": row.share_factor,
    })


def _apply_dividend_ledger(state: EngineState, row: IndicatorRow) -> None:
    if row.dividend_stage == "ENTITLEMENT":
        if (
            row.dividend_event_id in state.dividend_receivables
            or row.dividend_event_id in state.paid_dividend_event_ids
        ):
            raise ValueError("duplicate dividend entitlement id")
        position = state.position
        cash = (
            Decimal("0")
            if position is None
            else row.cash_dividend_per_share * position.shares
        )
        state.dividend_receivables[row.dividend_event_id] = {
            "cash": cash,
            "position_id": "" if position is None else position.position_id,
        }
        _emit_event(state, {
            "date": row.day,
            "event": "DIVIDEND_ENTITLEMENT_LOCKED",
            "dividend_event_id": row.dividend_event_id,
            "position_id": "" if position is None else position.position_id,
            "cash": cash,
        })
    elif row.dividend_stage == "PAYMENT":
        if row.dividend_event_id in state.paid_dividend_event_ids:
            raise ValueError("duplicate dividend payment id")
        if row.dividend_event_id not in state.dividend_receivables:
            raise ValueError("dividend payment has no prior entitlement")
        right = state.dividend_receivables.pop(row.dividend_event_id)
        state.paid_dividend_event_ids.append(row.dividend_event_id)
        cash = decimal(right["cash"])
        state.cash += cash
        owner = str(right["position_id"])
        if state.position is not None and state.position.position_id == owner:
            state.position.cash_distributions += cash
        elif owner:
            for trade in reversed(state.trades):
                if trade.get("role") == "EXIT" and trade.get("position_id") == owner:
                    trade["cash_distributions"] = decimal(
                        trade.get("cash_distributions", 0)
                    ) + cash
                    trade["net_pnl"] = decimal(trade["net_pnl"]) + cash
                    break
        _emit_event(state, {
            "date": row.day,
            "event": "DIVIDEND_CASH_PAID",
            "dividend_event_id": row.dividend_event_id,
            "position_id": owner,
            "cash": cash,
        })


def _execute_pending_open(
    state: EngineState, config: ETFResearchConfig, row: IndicatorRow
) -> str | None:
    pending = state.pending
    if pending is None:
        return None
    if not _can_execute_open(row):
        _emit_event(state, {
            "date": row.day,
            "event": "PENDING_ORDER_DEFERRED_NON_TRADABLE",
            "pending_side": pending.side,
            "signal_date": pending.signal_date,
            "tradability_reason": (
                row.tradability_reason
                or ("MISSING_OPEN" if row.raw_open is None else "NON_TRADABLE_SESSION")
            ),
        })
        return None
    state.pending = None
    if pending.side == "ENTRY":
        _record_entry(
            state,
            config,
            signal_date=pending.signal_date,
            fill_date=row.day,
            reference_price=row.raw_open,
            technical_entry=pending.technical_entry,
            initial_stop=pending.initial_stop,
            entry_type=pending.entry_type,
            histogram=pending.histogram_at_signal,
        )
    else:
        _record_exit(
            state,
            config,
            signal_date=pending.signal_date,
            fill_date=row.day,
            reference_price=row.raw_open,
            reason=pending.exit_reason,
        )
    return pending.side


def _exit_signal(
    state: EngineState,
    config: ETFResearchConfig,
    previous: IndicatorRow | None,
    row: IndicatorRow,
) -> ExitReason | None:
    position = state.position
    if (
        position is None
        or previous is None
        or not _signal_observable(row)
        or not _ready(row, config.strategy)
    ):
        return None
    histogram = row.macd_histogram
    prior_histogram = previous.macd_histogram
    macd_cross = prior_histogram is not None and prior_histogram >= 0 and histogram < 0
    stop_hit = row.technical_close < position.initial_stop

    if config.strategy == StrategyVersion.B and position.entry_type == EntryType.HIGH_MA21:
        if stop_hit:
            if position.kd_2r_armed and previous.k >= previous.d and row.k < row.d:
                _emit_event(state, {"date": row.day, "event": "STOP_PRIORITY_OVER_KD", "position_id": position.position_id})
            return ExitReason.INITIAL_STOP
        marked_sale = config.cost_model.sell(
            row.raw_close, position.shares, config.instrument_type
        )
        net_pnl = (
            marked_sale.cash_amount
            + position.cash_distributions
            - position.buy.cash_amount
        )
        if not position.kd_2r_armed and net_pnl >= Decimal("2") * position.initial_r:
            position.kd_2r_armed = True
            position.kd_2r_arm_date = row.day
            _emit_event(state, {
                "date": row.day,
                "event": "KD_2R_PERMANENTLY_ARMED",
                "position_id": position.position_id,
                "net_pnl": net_pnl,
                "two_r": Decimal("2") * position.initial_r,
            })
        kd_cross = previous.k >= previous.d and row.k < row.d
        if position.kd_2r_armed and kd_cross:
            return ExitReason.KD_2R
        if kd_cross:
            _emit_event(state, {"date": row.day, "event": "KD_DEATH_CROSS_IGNORED_BEFORE_2R", "position_id": position.position_id})
        return None

    if not position.macd_armed and histogram >= 0:
        position.macd_armed = True
        _emit_event(state, {"date": row.day, "event": "MACD_PERMANENTLY_ARMED", "position_id": position.position_id})
    if stop_hit and _regime(row) == "HIGH":
        if macd_cross and position.macd_armed:
            _emit_event(state, {"date": row.day, "event": "STOP_PRIORITY_OVER_MACD", "position_id": position.position_id})
        return ExitReason.INITIAL_STOP
    if stop_hit and _regime(row) == "LOW":
        _emit_event(state, {"date": row.day, "event": "STOP_EXIT_SUPPRESSED_LOW_REGIME", "position_id": position.position_id})
    if macd_cross and position.macd_armed:
        return ExitReason.MACD
    return None


def run_strategy(
    rows: Sequence[IndicatorRow | Mapping[str, Any]],
    config: ETFResearchConfig = ETFResearchConfig(),
    *,
    initial_state: EngineState | Mapping[str, Any] | None = None,
) -> RunResult:
    """Run deterministic supplied rows; no I/O and no hidden warm-up data."""

    normalized = [row if isinstance(row, IndicatorRow) else IndicatorRow.from_mapping(row) for row in rows]
    if any(left.day >= right.day for left, right in zip(normalized, normalized[1:])):
        raise ValueError("rows must be strictly increasing by date")
    if initial_state is None:
        state = EngineState(cash=config.initial_capital)
    elif isinstance(initial_state, EngineState):
        state = initial_state
    else:
        state = state_from_dict(initial_state)

    checkpoint_last = state.last_processed_day
    if state.daily:
        daily_last = state.daily[-1].get("date")
        if not isinstance(daily_last, date):
            raise ValueError("checkpoint daily date schema is invalid")
        if checkpoint_last is None:
            checkpoint_last = daily_last
            state.last_processed_day = daily_last
        elif checkpoint_last != daily_last:
            raise ValueError("checkpoint last processed day is inconsistent")
    if state.previous is not None:
        if checkpoint_last is None or state.previous.day > checkpoint_last:
            raise ValueError("checkpoint previous signal row exceeds processed boundary")
    elif checkpoint_last is not None:
        if any(
            item.get("signal_ready") and item.get("signal_observable", True)
            for item in state.daily
        ):
            raise ValueError("checkpoint has ready daily rows without previous signal state")
    if normalized and checkpoint_last is not None and normalized[0].day <= checkpoint_last:
        raise ValueError("resume rows must start strictly after checkpoint day")

    for row in normalized:
        if row.day < config.listing_date:
            raise ValueError("pre-listing 00635U prices are forbidden")
        _apply_corporate_action(state, row)
        _apply_dividend_ledger(state, row)
        open_execution = None
        if config.timing == ExecutionTiming.NEXT_SESSION_OPEN:
            open_execution = _execute_pending_open(state, config, row)

        if (
            state.position is not None
            and row.cash_dividend_per_share
            and not row.dividend_stage
        ):
            dividend = row.cash_dividend_per_share * state.position.shares
            state.cash += dividend
            state.position.cash_distributions += dividend
            _emit_event(state, {
                "date": row.day,
                "event": "CASH_DIVIDEND",
                "position_id": state.position.position_id,
                "cash": dividend,
            })

        previous = state.previous
        exited_today = False
        reason = (
            None
            if state.pending is not None
            else _exit_signal(state, config, previous, row)
        )
        if reason is not None:
            if config.timing == ExecutionTiming.SIGNAL_CLOSE:
                _record_exit(
                    state,
                    config,
                    signal_date=row.day,
                    fill_date=row.day,
                    reference_price=row.raw_close,
                    reason=reason,
                )
                exited_today = True
            else:
                state.pending = PendingOrder("EXIT", row.day, exit_reason=reason)

        selected = _entry_signal(previous, row, config.strategy, config.listing_date)
        candidate_stop = None
        if selected is not None:
            candidate_stop = _candidate_stop(state.phase_low, previous, row)
            blocked = (
                state.position is not None
                or state.pending is not None
                or exited_today
                or open_execution == "EXIT"
            )
            if not blocked and candidate_stop < row.technical_close:
                if config.timing == ExecutionTiming.SIGNAL_CLOSE:
                    _record_entry(
                        state,
                        config,
                        signal_date=row.day,
                        fill_date=row.day,
                        reference_price=row.raw_close,
                        technical_entry=row.technical_close,
                        initial_stop=candidate_stop,
                        entry_type=selected,
                        histogram=row.macd_histogram,
                    )
                else:
                    state.pending = PendingOrder(
                        "ENTRY",
                        row.day,
                        entry_type=selected,
                        technical_entry=row.technical_close,
                        initial_stop=candidate_stop,
                        histogram_at_signal=row.macd_histogram,
                    )
            elif not blocked:
                _emit_event(state, {"date": row.day, "event": "ENTRY_REJECTED_INVALID_STOP"})
            state.phase_low = None
        elif (
            _signal_observable(row)
            and row.sma21 is not None
            and row.technical_close <= row.sma21
        ):
            state.phase_low = min(
                value for value in (state.phase_low, row.technical_low) if value is not None
            )
        elif _signal_observable(row) and row.sma21 is not None:
            state.phase_low = None

        liquidation = Decimal("0")
        if state.position is not None:
            liquidation = config.cost_model.sell(
                row.raw_close, state.position.shares, config.instrument_type
            ).cash_amount
        equity = state.cash + liquidation
        receivable_cash = sum(
            (decimal(item["cash"]) for item in state.dividend_receivables.values()),
            Decimal("0"),
        )
        if state.cash < 0 or equity != state.cash + liquidation:
            raise AssertionError("daily accounting invariant violated")
        state.daily.append({
            "date": row.day,
            "cash": state.cash,
            "liquidation_value": liquidation,
            "equity": equity,
            "dividend_receivable_cash_excluded": receivable_cash,
            "equity_including_receivables_audit": equity + receivable_cash,
            "shares": 0 if state.position is None else state.position.shares,
            "pending": "" if state.pending is None else state.pending.side,
            "regime": _regime(row),
            "signal_ready": _ready(row, config.strategy),
            "signal_observable": _signal_observable(row),
            "official_session": row.official_session,
            "tradable": row.tradable,
            "volume": row.volume,
            "tradability_reason": row.tradability_reason,
            "open_execution": "" if open_execution is None else open_execution,
        })
        if _signal_observable(row):
            state.previous = row
        state.last_processed_day = row.day
    return RunResult(config=config, state=state)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def state_to_dict(state: EngineState) -> dict[str, Any]:
    return _jsonable(asdict(state))


_RECORD_DATE_FIELDS = {
    "date", "signal_date", "fill_date", "entry_date", "kd_2r_arm_date",
}
_RECORD_DECIMAL_FIELDS = {
    "reference_price", "fill_price", "gross", "commission", "tax",
    "cash_amount", "initial_stop", "initial_r", "net_pnl",
    "cash_distributions", "cash", "liquidation_value", "equity",
    "dividend_receivable_cash_excluded", "equity_including_receivables_audit",
    "price_rebase_factor", "share_factor", "technical_close", "net_pnl",
    "two_r", "available_cash", "one_share_cost", "fill_reference_price",
}


def _restore_record(record: Mapping[str, Any]) -> dict[str, Any]:
    restored: dict[str, Any] = {}
    for key, value in record.items():
        if value is None:
            restored[key] = None
        elif key in _RECORD_DATE_FIELDS and isinstance(value, str):
            restored[key] = date.fromisoformat(value)
        elif key in _RECORD_DECIMAL_FIELDS and isinstance(value, str):
            restored[key] = decimal(value)
        elif isinstance(value, dict):
            restored[key] = _restore_record(value)
        elif isinstance(value, list):
            restored[key] = [
                _restore_record(item) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            restored[key] = value
    return restored


def state_from_dict(payload: Mapping[str, Any]) -> EngineState:
    def row(value: Mapping[str, Any] | None) -> IndicatorRow | None:
        if value is None:
            return None
        converted = dict(value)
        converted["day"] = date.fromisoformat(converted["day"])
        return IndicatorRow.from_mapping(converted)

    position_payload = payload.get("position")
    position = None
    if position_payload is not None:
        buy_payload = position_payload["buy"]
        buy = OrderCost(
            side=buy_payload["side"],
            reference_price=decimal(buy_payload["reference_price"]),
            fill_price=decimal(buy_payload["fill_price"]),
            shares=int(buy_payload["shares"]),
            gross=decimal(buy_payload["gross"]),
            commission=decimal(buy_payload["commission"]),
            tax=decimal(buy_payload["tax"]),
            cash_amount=decimal(buy_payload["cash_amount"]),
        )
        position = Position(
            position_id=str(position_payload["position_id"]),
            entry_type=EntryType(position_payload["entry_type"]),
            signal_date=date.fromisoformat(position_payload["signal_date"]),
            entry_date=date.fromisoformat(position_payload["entry_date"]),
            shares=int(position_payload["shares"]),
            buy=buy,
            technical_entry=decimal(position_payload["technical_entry"]),
            initial_stop=decimal(position_payload["initial_stop"]),
            initial_r=decimal(position_payload["initial_r"]),
            macd_armed=bool(position_payload["macd_armed"]),
            cash_distributions=decimal(position_payload.get("cash_distributions", "0")),
            kd_2r_armed=bool(position_payload["kd_2r_armed"]),
            kd_2r_arm_date=(
                None
                if position_payload.get("kd_2r_arm_date") is None
                else date.fromisoformat(position_payload["kd_2r_arm_date"])
            ),
        )
    pending_payload = payload.get("pending")
    pending = None
    if pending_payload is not None:
        pending = PendingOrder(
            side=pending_payload["side"],
            signal_date=date.fromisoformat(pending_payload["signal_date"]),
            entry_type=(
                None
                if pending_payload.get("entry_type") is None
                else EntryType(pending_payload["entry_type"])
            ),
            technical_entry=(
                None
                if pending_payload.get("technical_entry") is None
                else decimal(pending_payload["technical_entry"])
            ),
            initial_stop=(
                None
                if pending_payload.get("initial_stop") is None
                else decimal(pending_payload["initial_stop"])
            ),
            histogram_at_signal=(
                None
                if pending_payload.get("histogram_at_signal") is None
                else decimal(pending_payload["histogram_at_signal"])
            ),
            exit_reason=(
                None
                if pending_payload.get("exit_reason") is None
                else ExitReason(pending_payload["exit_reason"])
            ),
        )
    restored_daily = [_restore_record(item) for item in payload.get("daily", [])]
    last_processed = payload.get("last_processed_day")
    if isinstance(last_processed, str):
        last_processed = date.fromisoformat(last_processed)
    if last_processed is None and restored_daily:
        last_processed = restored_daily[-1].get("date")
    return EngineState(
        cash=decimal(payload["cash"]),
        position=position,
        pending=pending,
        previous=row(payload.get("previous")),
        last_processed_day=last_processed,
        phase_low=None if payload.get("phase_low") is None else decimal(payload["phase_low"]),
        position_sequence=int(payload.get("position_sequence", 0)),
        event_sequence=int(payload.get("event_sequence", 0)),
        dividend_receivables={
            str(key): {
                "cash": decimal(value["cash"]),
                "position_id": str(value["position_id"]),
            }
            for key, value in payload.get("dividend_receivables", {}).items()
        },
        paid_dividend_event_ids=[str(item) for item in payload.get("paid_dividend_event_ids", [])],
        trades=[_restore_record(item) for item in payload.get("trades", [])],
        events=[_restore_record(item) for item in payload.get("events", [])],
        daily=restored_daily,
    )


def expected_comparison_lines() -> tuple[str, ...]:
    return (
        "STRATEGY_A_SIGNAL_CLOSE",
        "STRATEGY_A_NEXT_SESSION_OPEN",
        "STRATEGY_B_SIGNAL_CLOSE",
        "STRATEGY_B_NEXT_SESSION_OPEN",
        "00635U_BUY_HOLD_CASH",
        "00635U_BUY_HOLD_REINVEST",
        "GOLD_ER_OFFICIAL_TRACKED_INDEX",
    )
