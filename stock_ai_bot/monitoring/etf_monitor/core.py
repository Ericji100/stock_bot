from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from backtests.dual_ma import etf_0050_ma21_ma144_regime_v1 as accepted
from backtests.dual_ma.models import Bar

from .errors import EtfMonitorDataError
from .models import AcceptedCoreState, Decision, MarketDataSnapshot


STRATEGY_CORE_ID = accepted.FIXED_ID
ACCEPTED_START_DATE = date(2006, 1, 2)
RESEARCH_SLIPPAGE_BPS = 20


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _row_date(row: Mapping[str, Any]) -> date:
    value = row["date"]
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _value(row: Mapping[str, Any], raw_name: str, short_name: str) -> Any:
    value = row.get(raw_name)
    return row.get(short_name) if value is None else value


def _bar(row: Mapping[str, Any]) -> Bar:
    return Bar.from_values(
        _row_date(row),
        _value(row, "raw_open", "open"),
        _value(row, "raw_high", "high"),
        _value(row, "raw_low", "low"),
        _value(row, "raw_close", "close"),
    )


def _active_position(
    transactions: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, Decimal | None, bool]:
    active: Mapping[str, Any] | None = None
    for transaction in transactions:
        role = transaction.get("role")
        if role == "MOTHER":
            active = transaction
        elif role == "EXIT" and active is not None:
            if transaction.get("leg_id") == active.get("leg_id"):
                active = None
    if active is None:
        return None, None, False

    leg_id = active.get("leg_id")
    stop = _decimal(active["initial_stop"])
    armed = False
    for event in events:
        if event.get("leg_id") != leg_id:
            continue
        name = event.get("event")
        if name == "ENTRY":
            stop = _decimal(event["effective_stop"])
            armed = bool(event.get("armed_on_entry"))
        elif name == "CORPORATE_ACTION_REBASE":
            stop *= _decimal(event["factor"])
        elif name == "MACD_ARMED":
            armed = True
    return active, stop, armed


def _state_from_day(
    *,
    daily_row: Mapping[str, Any],
    transactions: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    raw_bar: Bar,
) -> AcceptedCoreState:
    day = _row_date(daily_row)
    today_transactions = [row for row in transactions if _row_date(row) == day]
    if len(today_transactions) > 1:
        raise EtfMonitorDataError("accepted core produced multiple same-day transactions")
    signal_transaction = today_transactions[0] if today_transactions else None
    if signal_transaction is None:
        decision = Decision.HOLD
    elif signal_transaction["role"] == "MOTHER":
        decision = Decision.BUY_NEXT_OPEN
    elif signal_transaction["role"] == "EXIT":
        decision = Decision.SELL_NEXT_OPEN
    else:
        raise EtfMonitorDataError("accepted core produced an unsupported transaction role")
    active, fixed_stop, armed = _active_position(transactions, events)
    row = dict(daily_row)
    return AcceptedCoreState(
        symbol="0050",
        strategy=STRATEGY_CORE_ID,
        as_of=day,
        decision=decision,
        regime=str(row["regime"]),
        raw_close=raw_bar.close,
        technical_close=_decimal(row["technical_close"]),
        sma21=None if row.get("sma21") is None else _decimal(row["sma21"]),
        sma144=None if row.get("sma144") is None else _decimal(row["sma144"]),
        histogram=_decimal(row["histogram"]),
        previous_histogram=(
            None if row.get("previous_histogram") is None else _decimal(row["previous_histogram"])
        ),
        active_position=active is not None,
        active_leg_id=None if active is None else str(active.get("leg_id") or ""),
        fixed_initial_stop=fixed_stop,
        macd_armed=armed,
        exit_reason=(
            str(signal_transaction.get("exit_reason"))
            if signal_transaction and signal_transaction.get("role") == "EXIT"
            else None
        ),
        signal_reference_price=(
            None if signal_transaction is None else _decimal(signal_transaction["reference_price"])
        ),
        signal_transaction=signal_transaction,
        core_daily_row=row,
        core_events=tuple(events),
        core_transactions=tuple(transactions),
    )


class AcceptedFinalV17Core:
    """Thin live adapter around the accepted final-v1.7 research engine.

    No signal rule is reimplemented here.  The adapter only translates the
    accepted engine's signal-day transaction into a next-session advisory.
    """

    strategy_id = STRATEGY_CORE_ID

    def stream_causal_history(
        self,
        *,
        raw_rows: Iterable[Mapping[str, Any]],
        corporate_actions: Sequence[Any],
        end: date,
        on_state: Callable[[AcceptedCoreState, tuple[Mapping[str, Any], ...]], None],
    ) -> None:
        """Consume one raw row, emit its state, then request the next row.

        The guarded ``raw_by_date`` mapping contains only already-consumed bars,
        so the accepted engine cannot read a later session from the source.
        """

        revealed_rows: list[Mapping[str, Any]] = []
        raw_by_date: dict[date, Bar] = {}
        prior: date | None = None

        def reveal() -> Iterator[Mapping[str, Any]]:
            nonlocal prior
            for source in raw_rows:
                row = dict(source, date=_row_date(source))
                day = _row_date(row)
                if prior is not None and day <= prior:
                    raise EtfMonitorDataError("causal raw rows are not unique ascending")
                raw_by_date[day] = _bar(row)
                revealed_rows.append(row)
                prior = day
                yield row

        def emit(
            daily_row: Mapping[str, Any],
            transactions: tuple[Mapping[str, Any], ...],
            events: tuple[Mapping[str, Any], ...],
        ) -> None:
            day = _row_date(daily_row)
            if day != prior or day not in raw_by_date:
                raise EtfMonitorDataError("accepted stream requested a non-revealed raw bar")
            state = _state_from_day(
                daily_row=daily_row,
                transactions=transactions,
                events=events,
                raw_bar=raw_by_date[day],
            )
            on_state(state, tuple(revealed_rows))

        indicators = accepted.iter_indicator_rows(reveal(), corporate_actions)
        _run, daily, _events = accepted.build_regime_run(
            indicator_rows=indicators,
            raw_by_date=raw_by_date,
            strategy_id=STRATEGY_CORE_ID,
            moving_stop=False,
            start=ACCEPTED_START_DATE,
            end=end,
            slippage_bps=RESEARCH_SLIPPAGE_BPS,
            on_day=emit,
        )
        if not daily or _row_date(daily[-1]) != end:
            raise EtfMonitorDataError("accepted causal stream did not reach requested end date")

    def _accepted_run(self, snapshot: MarketDataSnapshot):
        if snapshot.symbol != "0050":
            raise EtfMonitorDataError("accepted final-v1.7 core only supports 0050")
        if snapshot.latest_date != snapshot.as_of:
            raise EtfMonitorDataError("snapshot latest date does not equal as_of")
        if snapshot.as_of < ACCEPTED_START_DATE:
            raise EtfMonitorDataError("snapshot is earlier than accepted start date")

        raw_rows = tuple(dict(row, date=_row_date(row)) for row in snapshot.raw_rows)
        raw_by_date = {_row_date(row): _bar(row) for row in raw_rows}
        if len(raw_by_date) != len(raw_rows):
            raise EtfMonitorDataError("snapshot contains duplicate trading dates")

        indicator_rows = accepted.build_indicator_rows(raw_rows, snapshot.corporate_actions)
        run, daily, events = accepted.build_regime_run(
            indicator_rows=indicator_rows,
            raw_by_date=raw_by_date,
            strategy_id=STRATEGY_CORE_ID,
            moving_stop=False,
            start=ACCEPTED_START_DATE,
            end=snapshot.as_of,
            slippage_bps=RESEARCH_SLIPPAGE_BPS,
        )
        if not daily or daily[-1]["date"] != snapshot.as_of:
            raise EtfMonitorDataError("accepted core did not produce an as-of daily row")

        return raw_by_date, run, daily, events

    def iter_history(self, snapshot: MarketDataSnapshot) -> Iterator[AcceptedCoreState]:
        """Replay every official day through the same causal engine used by LIVE."""

        states: list[AcceptedCoreState] = []
        self.stream_causal_history(
            raw_rows=snapshot.raw_rows,
            corporate_actions=snapshot.corporate_actions,
            end=snapshot.as_of,
            on_state=lambda state, _prefix: states.append(state),
        )
        yield from states

    def evaluate(self, snapshot: MarketDataSnapshot) -> AcceptedCoreState:
        latest: AcceptedCoreState | None = None
        for latest in self.iter_history(snapshot):
            pass
        if latest is None:
            raise EtfMonitorDataError("accepted core produced no monitor state")
        return latest
