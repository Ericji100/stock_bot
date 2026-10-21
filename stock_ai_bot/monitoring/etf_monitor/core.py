from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence

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


class AcceptedFinalV17Core:
    """Thin live adapter around the accepted final-v1.7 research engine.

    No signal rule is reimplemented here.  The adapter only translates the
    accepted engine's signal-day transaction into a next-session advisory.
    """

    strategy_id = STRATEGY_CORE_ID

    def evaluate(self, snapshot: MarketDataSnapshot) -> AcceptedCoreState:
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

        transactions = tuple(dict(row) for row in run.transactions)
        core_events = tuple(dict(row) for row in events)
        today_transactions = [row for row in transactions if row["date"] == snapshot.as_of]
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

        active, fixed_stop, armed = _active_position(transactions, core_events)
        daily_row = dict(daily[-1])
        raw_close = raw_by_date[snapshot.as_of].close
        return AcceptedCoreState(
            symbol="0050",
            strategy=STRATEGY_CORE_ID,
            as_of=snapshot.as_of,
            decision=decision,
            regime=str(daily_row["regime"]),
            raw_close=raw_close,
            technical_close=_decimal(daily_row["technical_close"]),
            sma21=None if daily_row.get("sma21") is None else _decimal(daily_row["sma21"]),
            sma144=None if daily_row.get("sma144") is None else _decimal(daily_row["sma144"]),
            histogram=_decimal(daily_row["histogram"]),
            previous_histogram=(
                None
                if daily_row.get("previous_histogram") is None
                else _decimal(daily_row["previous_histogram"])
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
                None
                if signal_transaction is None
                else _decimal(signal_transaction["reference_price"])
            ),
            signal_transaction=signal_transaction,
            core_daily_row=daily_row,
            core_events=core_events,
            core_transactions=transactions,
        )
