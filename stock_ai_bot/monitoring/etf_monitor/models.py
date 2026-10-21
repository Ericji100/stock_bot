from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping


class MonitorPhase(str, Enum):
    PREVIEW = "preview_1245"
    DECISION = "decision_1355"
    RECONCILIATION = "reconciliation_2200"
    TEST = "offline_test"


class Decision(str, Enum):
    BUY_NEXT_OPEN = "BUY_NEXT_OPEN"
    SELL_NEXT_OPEN = "SELL_NEXT_OPEN"
    HOLD = "HOLD"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value)!r}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def stable_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_event_id(
    *,
    symbol: str,
    strategy: str,
    phase: MonitorPhase | str,
    signal_date: date,
    decision: Decision | str,
    suffix: str | None = None,
) -> str:
    phase_value = phase.value if isinstance(phase, MonitorPhase) else str(phase)
    decision_value = decision.value if isinstance(decision, Decision) else str(decision)
    parts = [symbol, strategy, phase_value, signal_date.isoformat(), decision_value]
    if suffix:
        parts.append(str(suffix))
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"etf-{symbol}-{digest}"


@dataclass(frozen=True)
class IntradayQuote:
    symbol: str
    observed_at: datetime
    trade_date: date
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_last: Decimal
    raw_volume: Decimal
    source: str
    source_endpoint: str
    receipt_sha256: str
    is_market_closed: bool = False


@dataclass(frozen=True)
class MarketDataSnapshot:
    symbol: str
    as_of: date
    raw_rows: tuple[Mapping[str, Any], ...]
    corporate_actions: tuple[Any, ...]
    source: str
    price_basis: str
    snapshot_id: str
    snapshot_version: str
    snapshot_sha256: str
    fetched_at: datetime
    is_complete: bool
    is_provisional: bool = False
    quote: IntradayQuote | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def latest_date(self) -> date | None:
        if not self.raw_rows:
            return None
        value = self.raw_rows[-1]["date"]
        return value if isinstance(value, date) else date.fromisoformat(str(value))

    def raw_row_for(self, day: date) -> Mapping[str, Any] | None:
        for row in reversed(self.raw_rows):
            value = row["date"]
            row_date = value if isinstance(value, date) else date.fromisoformat(str(value))
            if row_date == day:
                return row
        return None


@dataclass(frozen=True)
class AcceptedCoreState:
    symbol: str
    strategy: str
    as_of: date
    decision: Decision
    regime: str
    raw_close: Decimal
    technical_close: Decimal
    sma21: Decimal | None
    sma144: Decimal | None
    histogram: Decimal
    previous_histogram: Decimal | None
    active_position: bool
    active_leg_id: str | None
    fixed_initial_stop: Decimal | None
    macd_armed: bool
    exit_reason: str | None
    signal_reference_price: Decimal | None
    signal_transaction: Mapping[str, Any] | None
    core_daily_row: Mapping[str, Any]
    core_events: tuple[Mapping[str, Any], ...]
    core_transactions: tuple[Mapping[str, Any], ...]


def accepted_core_payload(core: AcceptedCoreState) -> dict[str, Any]:
    """Return the compact durable view; full replay arrays remain in memory."""

    return {
        "symbol": core.symbol,
        "strategy": core.strategy,
        "as_of": core.as_of,
        "decision": core.decision,
        "regime": core.regime,
        "raw_close": core.raw_close,
        "technical_close": core.technical_close,
        "sma21": core.sma21,
        "sma144": core.sma144,
        "histogram": core.histogram,
        "previous_histogram": core.previous_histogram,
        "active_position": core.active_position,
        "active_leg_id": core.active_leg_id,
        "fixed_initial_stop": core.fixed_initial_stop,
        "macd_armed": core.macd_armed,
        "exit_reason": core.exit_reason,
        "signal_reference_price": core.signal_reference_price,
        "signal_transaction": core.signal_transaction,
        "core_daily_row": core.core_daily_row,
        "core_event_count": len(core.core_events),
        "core_transaction_count": len(core.core_transactions),
        "core_events_sha256": stable_sha256(core.core_events),
        "core_transactions_sha256": stable_sha256(core.core_transactions),
    }


@dataclass(frozen=True)
class PhaseOutcome:
    phase: MonitorPhase
    trading_date: date
    status: str
    event_ids: tuple[str, ...] = ()
    decision: Decision | None = None
    message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboxItem:
    event_id: str
    target_chat_id: str
    body: str
    body_sha256: str
    attempts: int
    status: str
