"""Fail-closed adapter from H1 lifecycle/engine LEG events to costed LegInput.

The adapter only moves evidence that the frozen inputs actually contain.  It
never derives a lifecycle episode id from an economic key, never invents a price
or a corporate-action basis, and holds (rather than guesses) any leg whose
episode, price evidence, transaction linkage or cost reconciliation cannot be
resolved uniquely.

Inputs it must be given explicitly:

* lifecycle episodes with their own frozen lifecycle_episode_id and window;
* engine LEG and TRANSACTION events from a frozen run;
* market evidence: official sessions, per-symbol bar dates, officially explained
  non-trading sessions, the price basis, the corporate-action ledger hash, the
  availability snapshot hash and the causal decision cutoff.

No performance quantity is read or computed here; any performance key that
appears in an input is itself a HOLD condition.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .approx_h1_cost_report_core import (
    LegInput,
    canonical_bytes,
    canonical_sha256,
)
from .costs import CATHAY_WEB_028_TW_V1
from .models import InstrumentType


ADAPTER_VERSION = "APPROX_H1_EVENT_COST_ADAPTER_V0_1"

HOLD_MISSING_LIFECYCLE_EPISODE = "HOLD_MISSING_LIFECYCLE_EPISODE_ID"
HOLD_AMBIGUOUS_LIFECYCLE_EPISODE = "HOLD_AMBIGUOUS_LIFECYCLE_EPISODE_MATCH"
HOLD_EPISODE_NOT_NEXT_SESSION = "HOLD_EPISODE_NOT_STARTED_AT_NEXT_OFFICIAL_SESSION"
HOLD_NON_OFFICIAL_SESSION = "HOLD_NON_OFFICIAL_SESSION"
HOLD_FUTURE_EVIDENCE = "HOLD_FUTURE_EVIDENCE_BEYOND_CUTOFF"
HOLD_MISSING_PRICE = "HOLD_MISSING_PRICE"
HOLD_MISSING_PRICE_BASIS = "HOLD_MISSING_PRICE_BASIS_EVIDENCE"
HOLD_UNEXPLAINED_MISSING_BAR = "HOLD_UNEXPLAINED_MISSING_BAR"
HOLD_MISSING_INITIAL_R = "HOLD_MISSING_INITIAL_R"
HOLD_MISSING_TRANSACTION_LINK = "HOLD_MISSING_TRANSACTION_LINK"
HOLD_TRANSACTION_RECONCILIATION = "HOLD_TRANSACTION_RECONCILIATION"
HOLD_ENGINE_COST_MISMATCH = "HOLD_ENGINE_COST_MISMATCH"
HOLD_MARK_NOT_LAST_AVAILABLE = "HOLD_MARK_NOT_LAST_AVAILABLE_CLOSE"
HOLD_PERFORMANCE_FIELD = "HOLD_PERFORMANCE_FIELD_PRESENT"
HOLD_MISSING_INSTRUMENT_TYPE = "HOLD_MISSING_INSTRUMENT_TYPE"
HOLD_MISSING_ECONOMIC_SIGNAL_KEY = "HOLD_MISSING_ECONOMIC_SIGNAL_KEY"
HOLD_CONFLICTING_ECONOMIC_SIGNAL_KEY = "HOLD_CONFLICTING_ECONOMIC_SIGNAL_KEY"
HOLD_INVALID_SESSION_CALENDAR = "HOLD_INVALID_SESSION_CALENDAR"
HOLD_ENTRY_BEFORE_EFFECTIVE = "HOLD_ENTRY_BEFORE_EFFECTIVE_SESSION"
HOLD_ENTRY_AFTER_WINDOW_END = "HOLD_ENTRY_AFTER_EPISODE_WINDOW_END"
HOLD_AMBIGUOUS_TRANSACTION_LINK = "HOLD_AMBIGUOUS_TRANSACTION_LINK"
HOLD_TRANSACTION_DATE_MISMATCH = "HOLD_TRANSACTION_DATE_MISMATCH"
HOLD_TRANSACTION_SHARES_MISMATCH = "HOLD_TRANSACTION_SHARES_MISMATCH"
HOLD_MARK_EVIDENCE_INCOMPLETE = "HOLD_MARK_EVIDENCE_INCOMPLETE"
HOLD_MARK_PRICE_CONTRADICTS_OFFICIAL_BAR = "HOLD_MARK_PRICE_CONTRADICTS_OFFICIAL_BAR"
HOLD_REFERENCE_CLOSE_CONTRADICTS_OFFICIAL_BAR = "HOLD_REFERENCE_CLOSE_CONTRADICTS_OFFICIAL_BAR"

HOLD_CODES: tuple[str, ...] = (
    HOLD_MISSING_LIFECYCLE_EPISODE,
    HOLD_AMBIGUOUS_LIFECYCLE_EPISODE,
    HOLD_EPISODE_NOT_NEXT_SESSION,
    HOLD_NON_OFFICIAL_SESSION,
    HOLD_FUTURE_EVIDENCE,
    HOLD_MISSING_PRICE,
    HOLD_MISSING_PRICE_BASIS,
    HOLD_UNEXPLAINED_MISSING_BAR,
    HOLD_MISSING_INITIAL_R,
    HOLD_MISSING_TRANSACTION_LINK,
    HOLD_TRANSACTION_RECONCILIATION,
    HOLD_ENGINE_COST_MISMATCH,
    HOLD_MARK_NOT_LAST_AVAILABLE,
    HOLD_PERFORMANCE_FIELD,
    HOLD_MISSING_INSTRUMENT_TYPE,
    HOLD_MISSING_ECONOMIC_SIGNAL_KEY,
    HOLD_CONFLICTING_ECONOMIC_SIGNAL_KEY,
    HOLD_INVALID_SESSION_CALENDAR,
    HOLD_ENTRY_BEFORE_EFFECTIVE,
    HOLD_ENTRY_AFTER_WINDOW_END,
    HOLD_AMBIGUOUS_TRANSACTION_LINK,
    HOLD_TRANSACTION_DATE_MISMATCH,
    HOLD_TRANSACTION_SHARES_MISMATCH,
    HOLD_MARK_EVIDENCE_INCOMPLETE,
    HOLD_MARK_PRICE_CONTRADICTS_OFFICIAL_BAR,
    HOLD_REFERENCE_CLOSE_CONTRADICTS_OFFICIAL_BAR,
)

PERFORMANCE_TOKENS = ("pnl", "profit", "expectancy", "win_rate", "mfe", "mae")
PRICE_BASIS_ALLOWED = ("UNADJUSTED_OFFICIAL_CLOSE_PROXY",)
REQUIRED_LEG_FIELDS = (
    "symbol",
    "strategy",
    "matrix_id",
    "slippage_bps",
    "leg_id",
    "leg_number",
    "entry_date",
    "entry_fill_price",
    "entry_reference_close",
    "initial_stop",
    "initial_r",
    "shares",
    "buy_total",
    "monitor_effective_from",
    "regime_window_start",
)


class AdapterContractError(RuntimeError):
    code = "APPROX_H1_EVENT_COST_ADAPTER_ERROR"


def _as_date(value: Any, *, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise AdapterContractError(f"unparsable {field}: {value!r}") from exc


def _optional_date(value: Any) -> date | None:
    return None if value in (None, "") else date.fromisoformat(str(value))


@dataclass(frozen=True)
class LifecycleEpisode:
    """Frozen lifecycle episode identity supplied by the caller, never derived."""

    lifecycle_episode_id: str
    symbol: str
    strategy: str
    source_selection_date: date
    monitor_effective_from: date
    regime_window_start: date
    regime_window_end: date
    ended_by_regime: bool

    def __post_init__(self) -> None:
        if not self.lifecycle_episode_id.strip():
            raise AdapterContractError("lifecycle_episode_id must be explicit")
        if self.monitor_effective_from != self.regime_window_start:
            raise AdapterContractError(
                "an episode only starts on its effective session in the frozen life-cycle"
            )
        if self.regime_window_end < self.regime_window_start:
            raise AdapterContractError("episode window is inverted")


@dataclass(frozen=True)
class MarketEvidence:
    """Causal market facts the adapter may rely on, all supplied explicitly.

    official_closes binds each (symbol, session) to the verified official close
    and price_source_sha256 to the frozen source that produced it, so a caller
    cannot pass an arbitrary mark value.
    """

    official_sessions: tuple[date, ...]
    bar_dates_by_symbol: Mapping[str, tuple[date, ...]]
    explained_non_trading: frozenset[tuple[str, date]]
    official_closes: Mapping[tuple[str, date], Decimal]
    price_basis: str
    corporate_action_ledger_sha256: str
    availability_sha256: str
    price_source_sha256: str
    decision_cutoff: date

    def validate(self) -> None:
        if not self.official_sessions:
            raise AdapterContractError("official session list is empty")
        if list(self.official_sessions) != sorted(set(self.official_sessions)):
            raise AdapterContractError(
                "official sessions must be strictly increasing and unique"
            )
        session_set = set(self.official_sessions)
        for (symbol, day), value in self.official_closes.items():
            if day not in session_set:
                raise AdapterContractError(
                    f"official close for non-session {symbol} {day}"
                )
            if value <= 0:
                raise AdapterContractError("official close must be positive")

    def sessions(self) -> set[date]:
        return set(self.official_sessions)

    def official_close(self, symbol: str, day: date) -> Decimal | None:
        return self.official_closes.get((symbol, day))

    def next_session(self, day: date) -> date | None:
        later = [item for item in self.official_sessions if item > day]
        return later[0] if later else None

    def last_bar_on_or_before(self, symbol: str, day: date) -> date | None:
        days = [item for item in self.bar_dates_by_symbol.get(symbol, ()) if item <= day]
        return max(days) if days else None

    def missing_bar_sessions(self, symbol: str, start: date, end: date) -> tuple[date, ...]:
        bars = set(self.bar_dates_by_symbol.get(symbol, ()))
        return tuple(
            day
            for day in self.official_sessions
            if start <= day <= end
            and day not in bars
            and (symbol, day) not in self.explained_non_trading
        )


@dataclass(frozen=True)
class MarkEvidence:
    """Verified official close used to mark an open position."""

    symbol: str
    session: date
    close: Decimal
    source_sha256: str


@dataclass(frozen=True)
class LegEvent:
    """One engine LEG row, projected without loss of the fields used here."""

    symbol: str
    strategy: str
    matrix_id: str
    slippage_bps: int
    leg_id: str
    leg_number: int
    monitor_effective_from: date
    regime_window_start: date
    regime_window_end: date
    entry_date: date
    entry_fill_price: Decimal
    entry_reference_close: Decimal
    initial_stop: Decimal
    initial_r: Decimal
    shares: int
    buy_total: Decimal
    exit_date: date | None
    exit_fill_price: Decimal | None
    exit_reference_close: Decimal | None
    sell_net: Decimal | None
    open_censored: bool
    episode_key: str
    raw: Mapping[str, Any]

    @classmethod
    def from_engine_event(cls, row: Mapping[str, Any]) -> "LegEvent":
        for token in PERFORMANCE_TOKENS:
            if any(token in str(name).lower() for name in row):
                raise AdapterContractError(
                    f"leg event exposes performance field matching {token!r}"
                )
        missing = [field for field in REQUIRED_LEG_FIELDS if row.get(field) in (None, "")]
        if missing:
            raise AdapterContractError(f"leg event is missing fields: {missing}")
        return cls(
            symbol=str(row["symbol"]),
            strategy=str(row["strategy"]),
            matrix_id=str(row["matrix_id"]),
            slippage_bps=int(row["slippage_bps"]),
            leg_id=str(row["leg_id"]),
            leg_number=int(row["leg_number"]),
            monitor_effective_from=_as_date(
                row["monitor_effective_from"], field="monitor_effective_from"
            ),
            regime_window_start=_as_date(
                row["regime_window_start"], field="regime_window_start"
            ),
            regime_window_end=_as_date(
                row["regime_window_end"], field="regime_window_end"
            ),
            entry_date=_as_date(row["entry_date"], field="entry_date"),
            entry_fill_price=Decimal(str(row["entry_fill_price"])),
            entry_reference_close=Decimal(str(row["entry_reference_close"])),
            initial_stop=Decimal(str(row["initial_stop"])),
            initial_r=Decimal(str(row["initial_r"])),
            shares=int(row["shares"]),
            buy_total=Decimal(str(row["buy_total"])),
            exit_date=_optional_date(row.get("exit_date")),
            exit_fill_price=(
                Decimal(str(row["exit_fill_price"])) if row.get("exit_fill_price") else None
            ),
            exit_reference_close=(
                Decimal(str(row["exit_reference_close"]))
                if row.get("exit_reference_close")
                else None
            ),
            sell_net=Decimal(str(row["sell_net"])) if row.get("sell_net") else None,
            open_censored=bool(row.get("open_censored")),
            episode_key=str(row.get("episode_key") or ""),
            raw=dict(row),
        )


@dataclass(frozen=True)
class TransactionEvent:
    leg_id: str
    role: str
    day: date
    shares: int
    cash_amount: Decimal
    symbol: str
    strategy: str
    matrix_id: str
    slippage_bps: int

    @classmethod
    def from_engine_event(cls, row: Mapping[str, Any]) -> "TransactionEvent":
        return cls(
            leg_id=str(row["leg_id"]),
            role=str(row["role"]),
            day=_as_date(row["date"], field="transaction date"),
            shares=int(row["shares"]),
            cash_amount=Decimal(str(row["cash_amount"])),
            symbol=str(row["symbol"]),
            strategy=str(row["strategy"]),
            matrix_id=str(row["matrix_id"]),
            slippage_bps=int(row["slippage_bps"]),
        )


@dataclass(frozen=True)
class AdaptedLeg:
    """Either a ready LegInput with mapping evidence, or a HOLD record."""

    status: str
    hold_code: str | None
    reason: str
    mapping: Mapping[str, Any]
    leg_input: LegInput | None

    @property
    def is_hold(self) -> bool:
        return self.status == "HOLD"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "hold_code": self.hold_code,
            "reason": self.reason,
            "mapping": dict(self.mapping),
        }


def _hold(
    code: str, reason: str, *, mapping: Mapping[str, Any] | None = None
) -> AdaptedLeg:
    if code not in HOLD_CODES:
        raise AdapterContractError(f"unknown hold code: {code}")
    return AdaptedLeg("HOLD", code, reason, dict(mapping or {}), None)


def adapt_leg(
    *,
    leg: LegEvent,
    episodes: Sequence[LifecycleEpisode],
    market: MarketEvidence,
    transactions: Sequence[TransactionEvent],
    mark: MarkEvidence | None = None,
    conflicting_economic_keys: frozenset[str] = frozenset(),
) -> AdaptedLeg:
    """Map one engine leg to a LegInput, or hold it with a reason code.

    The adapter checks identity, provenance, dates and amounts only; it never
    computes or emits a PnL, R multiple or any other performance value.
    """

    mapping: dict[str, Any] = {
        "symbol": leg.symbol,
        "strategy": leg.strategy,
        "matrix_id": leg.matrix_id,
        "slippage_bps": leg.slippage_bps,
        "leg_id": leg.leg_id,
        "leg_number": leg.leg_number,
        "engine_episode_key": leg.episode_key,
    }

    try:
        market.validate()
    except AdapterContractError as exc:
        return _hold(HOLD_INVALID_SESSION_CALENDAR, str(exc), mapping=mapping)

    if market.price_basis not in PRICE_BASIS_ALLOWED:
        return _hold(
            HOLD_MISSING_PRICE_BASIS,
            f"unsupported price basis {market.price_basis!r}",
            mapping=mapping,
        )
    if (
        not market.corporate_action_ledger_sha256
        or not market.availability_sha256
        or not market.price_source_sha256
    ):
        return _hold(
            HOLD_MISSING_PRICE_BASIS,
            "corporate action, availability and price source hashes are required",
            mapping=mapping,
        )

    instrument_raw = leg.raw.get("instrument_type")
    if instrument_raw in (None, ""):
        return _hold(
            HOLD_MISSING_INSTRUMENT_TYPE,
            "the leg does not state its instrument identity",
            mapping=mapping,
        )
    try:
        instrument = InstrumentType(str(instrument_raw))
    except ValueError:
        return _hold(
            HOLD_MISSING_INSTRUMENT_TYPE,
            f"unknown instrument type {instrument_raw!r}",
            mapping=mapping,
        )
    mapping["instrument_type"] = instrument.value

    economic_key = str(leg.raw.get("economic_key") or "").strip()
    if not economic_key:
        return _hold(
            HOLD_MISSING_ECONOMIC_SIGNAL_KEY,
            "the leg does not carry its verified economic signal key",
            mapping=mapping,
        )
    mapping["economic_key"] = economic_key
    if economic_key in conflicting_economic_keys:
        return _hold(
            HOLD_CONFLICTING_ECONOMIC_SIGNAL_KEY,
            "the same economic signal key is claimed by different entries",
            mapping=mapping,
        )

    candidates = [
        episode
        for episode in episodes
        if episode.symbol == leg.symbol
        and episode.strategy == leg.strategy
        and episode.monitor_effective_from == leg.monitor_effective_from
        and episode.regime_window_start == leg.regime_window_start
        and episode.regime_window_end == leg.regime_window_end
    ]
    if not candidates:
        return _hold(
            HOLD_MISSING_LIFECYCLE_EPISODE,
            "no explicit lifecycle episode matches this leg",
            mapping=mapping,
        )
    if len(candidates) > 1:
        return _hold(
            HOLD_AMBIGUOUS_LIFECYCLE_EPISODE,
            f"{len(candidates)} lifecycle episodes match this leg",
            mapping=mapping,
        )
    episode = candidates[0]
    mapping["lifecycle_episode_id"] = episode.lifecycle_episode_id
    mapping["source_selection_date"] = episode.source_selection_date

    expected_effective = market.next_session(episode.source_selection_date)
    if expected_effective is None or expected_effective != episode.monitor_effective_from:
        return _hold(
            HOLD_EPISODE_NOT_NEXT_SESSION,
            "episode does not start on the next official session after its selection",
            mapping=mapping,
        )

    sessions = market.sessions()
    if leg.entry_date not in sessions:
        return _hold(
            HOLD_NON_OFFICIAL_SESSION,
            f"entry {leg.entry_date} is not a session",
            mapping=mapping,
        )
    if leg.entry_date < episode.monitor_effective_from:
        return _hold(
            HOLD_ENTRY_BEFORE_EFFECTIVE,
            "entry precedes the episode's effective session",
            mapping=mapping,
        )
    if leg.entry_date > episode.regime_window_end:
        return _hold(
            HOLD_ENTRY_AFTER_WINDOW_END,
            "entry is after the episode window end",
            mapping=mapping,
        )
    if leg.entry_date > market.decision_cutoff:
        return _hold(HOLD_FUTURE_EVIDENCE, "entry is after the decision cutoff", mapping=mapping)

    entry_official_close = market.official_close(leg.symbol, leg.entry_date)
    if entry_official_close is None:
        return _hold(
            HOLD_MARK_EVIDENCE_INCOMPLETE,
            "no verified official close for the entry session",
            mapping=mapping,
        )
    if entry_official_close != leg.entry_reference_close:
        return _hold(
            HOLD_REFERENCE_CLOSE_CONTRADICTS_OFFICIAL_BAR,
            f"entry reference close {leg.entry_reference_close} != official "
            f"{entry_official_close}",
            mapping=mapping,
        )

    mark_session: date | None = None
    if leg.exit_date is not None:
        if leg.exit_fill_price is None or leg.exit_reference_close is None:
            return _hold(HOLD_MISSING_PRICE, "closed leg lacks an exit price", mapping=mapping)
        if leg.exit_date not in sessions:
            return _hold(
                HOLD_NON_OFFICIAL_SESSION,
                f"exit {leg.exit_date} is not a session",
                mapping=mapping,
            )
        if leg.exit_date < leg.entry_date:
            return _hold(HOLD_NON_OFFICIAL_SESSION, "exit precedes entry", mapping=mapping)
        if leg.exit_date > episode.regime_window_end:
            return _hold(
                HOLD_ENTRY_AFTER_WINDOW_END,
                "exit is after the episode window end",
                mapping=mapping,
            )
        if leg.exit_date > market.decision_cutoff:
            return _hold(HOLD_FUTURE_EVIDENCE, "exit is after the decision cutoff", mapping=mapping)
        exit_official_close = market.official_close(leg.symbol, leg.exit_date)
        if exit_official_close is None:
            return _hold(
                HOLD_MARK_EVIDENCE_INCOMPLETE,
                "no verified official close for the exit session",
                mapping=mapping,
            )
        if exit_official_close != leg.exit_reference_close:
            return _hold(
                HOLD_REFERENCE_CLOSE_CONTRADICTS_OFFICIAL_BAR,
                f"exit reference close {leg.exit_reference_close} != official "
                f"{exit_official_close}",
                mapping=mapping,
            )
        missing = market.missing_bar_sessions(leg.symbol, leg.entry_date, leg.exit_date)
        if missing:
            return _hold(
                HOLD_UNEXPLAINED_MISSING_BAR,
                f"unexplained missing bars inside the leg: {[d.isoformat() for d in missing[:3]]}",
                mapping=mapping,
            )
        mapping["exit_date"] = leg.exit_date
    else:
        if mark is None:
            return _hold(
                HOLD_MARK_EVIDENCE_INCOMPLETE,
                "open leg needs (symbol, session, close, source SHA) mark evidence",
                mapping=mapping,
            )
        if mark.symbol != leg.symbol:
            return _hold(HOLD_MARK_EVIDENCE_INCOMPLETE, "mark belongs to another symbol", mapping=mapping)
        if mark.session not in sessions:
            return _hold(
                HOLD_NON_OFFICIAL_SESSION,
                f"mark session {mark.session} is not a session",
                mapping=mapping,
            )
        if mark.session > market.decision_cutoff:
            return _hold(HOLD_FUTURE_EVIDENCE, "mark is after the decision cutoff", mapping=mapping)
        if mark.source_sha256 != market.price_source_sha256:
            return _hold(
                HOLD_MARK_EVIDENCE_INCOMPLETE,
                "mark source hash differs from the bound price source",
                mapping=mapping,
            )
        mark_official_close = market.official_close(leg.symbol, mark.session)
        if mark_official_close is None:
            return _hold(
                HOLD_MARK_EVIDENCE_INCOMPLETE,
                "no verified official close for the mark session",
                mapping=mapping,
            )
        if mark_official_close != mark.close:
            return _hold(
                HOLD_MARK_PRICE_CONTRADICTS_OFFICIAL_BAR,
                f"mark close {mark.close} != official {mark_official_close}",
                mapping=mapping,
            )
        last_available = market.last_bar_on_or_before(leg.symbol, market.decision_cutoff)
        if last_available is None or last_available < leg.entry_date:
            return _hold(HOLD_MISSING_PRICE, "no causally available close to mark", mapping=mapping)
        if mark.session != last_available:
            return _hold(
                HOLD_MARK_NOT_LAST_AVAILABLE,
                f"mark session {mark.session} is not the last available close {last_available}",
                mapping=mapping,
            )
        missing = market.missing_bar_sessions(leg.symbol, leg.entry_date, mark.session)
        if missing:
            return _hold(
                HOLD_UNEXPLAINED_MISSING_BAR,
                f"unexplained missing bars inside the leg: {[d.isoformat() for d in missing[:3]]}",
                mapping=mapping,
            )
        mark_session = mark.session
        mapping["mark_session"] = mark_session
        mapping["mark_close"] = mark.close
        mapping["mark_source_sha256"] = mark.source_sha256

    if leg.initial_r is None or leg.initial_r <= 0:
        return _hold(HOLD_MISSING_INITIAL_R, "initial R is missing or not positive", mapping=mapping)

    linked = [
        item
        for item in transactions
        if item.leg_id == leg.leg_id
        and item.symbol == leg.symbol
        and item.strategy == leg.strategy
        and item.matrix_id == leg.matrix_id
        and item.slippage_bps == leg.slippage_bps
    ]
    entry_role = "MOTHER" if leg.leg_number == 1 else "ADD"
    entries = [item for item in linked if item.role == entry_role]
    if not entries:
        return _hold(
            HOLD_MISSING_TRANSACTION_LINK,
            f"no linked {entry_role} transaction for this leg",
            mapping=mapping,
        )
    if len(entries) > 1:
        return _hold(
            HOLD_AMBIGUOUS_TRANSACTION_LINK,
            f"{len(entries)} {entry_role} transactions match this leg",
            mapping=mapping,
        )
    entry_tx = entries[0]
    if entry_tx.day != leg.entry_date:
        return _hold(
            HOLD_TRANSACTION_DATE_MISMATCH,
            f"{entry_role} date {entry_tx.day} != entry {leg.entry_date}",
            mapping=mapping,
        )
    if entry_tx.shares != leg.shares:
        return _hold(
            HOLD_TRANSACTION_SHARES_MISMATCH,
            f"{entry_role} shares {entry_tx.shares} != leg shares {leg.shares}",
            mapping=mapping,
        )
    if entry_tx.cash_amount != leg.buy_total:
        return _hold(
            HOLD_TRANSACTION_RECONCILIATION,
            f"engine buy_total {leg.buy_total} != {entry_role} cash {entry_tx.cash_amount}",
            mapping=mapping,
        )
    if leg.exit_date is not None:
        exits = [item for item in linked if item.role == "EXIT"]
        if not exits:
            return _hold(
                HOLD_MISSING_TRANSACTION_LINK,
                "closed leg lacks an exit transaction",
                mapping=mapping,
            )
        if len(exits) > 1:
            return _hold(
                HOLD_AMBIGUOUS_TRANSACTION_LINK,
                f"{len(exits)} EXIT transactions match this leg",
                mapping=mapping,
            )
        exit_tx = exits[0]
        if exit_tx.day != leg.exit_date:
            return _hold(
                HOLD_TRANSACTION_DATE_MISMATCH,
                f"EXIT date {exit_tx.day} != exit {leg.exit_date}",
                mapping=mapping,
            )
        if exit_tx.shares != leg.shares:
            return _hold(
                HOLD_TRANSACTION_SHARES_MISMATCH,
                f"EXIT shares {exit_tx.shares} != leg shares {leg.shares}",
                mapping=mapping,
            )
        if leg.sell_net is None or exit_tx.cash_amount != leg.sell_net:
            return _hold(
                HOLD_TRANSACTION_RECONCILIATION,
                f"engine sell_net {leg.sell_net} != EXIT cash {exit_tx.cash_amount}",
                mapping=mapping,
            )

    model = CATHAY_WEB_028_TW_V1.with_slippage(leg.slippage_bps)
    recomputed_buy = model.buy(leg.entry_reference_close, leg.shares)
    if recomputed_buy.cash_amount != leg.buy_total:
        return _hold(
            HOLD_ENGINE_COST_MISMATCH,
            f"recomputed buy {recomputed_buy.cash_amount} != engine {leg.buy_total}",
            mapping=mapping,
        )
    if leg.exit_date is not None:
        recomputed_sell = model.sell(leg.exit_reference_close, leg.shares, instrument)
        if leg.sell_net is None or recomputed_sell.cash_amount != leg.sell_net:
            return _hold(
                HOLD_ENGINE_COST_MISMATCH,
                f"recomputed sell {recomputed_sell.cash_amount} != engine {leg.sell_net}",
                mapping=mapping,
            )
    mapping["cost_reconciliation"] = "MATCH"

    leg_input = LegInput(
        matrix_id=leg.matrix_id,
        slippage_bps=leg.slippage_bps,
        symbol=leg.symbol,
        strategy=leg.strategy,
        leg_id=leg.leg_id,
        leg_number=leg.leg_number,
        episode_key=leg.episode_key,
        economic_key=economic_key,
        lifecycle_episode_id=episode.lifecycle_episode_id,
        instrument_type=instrument.value,
        entry_reference_close=leg.entry_reference_close,
        shares=leg.shares,
        initial_stop=leg.initial_stop,
        initial_r=leg.initial_r,
        entry_date=leg.entry_date,
        exit_date=leg.exit_date,
        exit_reason=(str(leg.raw.get("exit_reason") or "CLOSED") if leg.exit_date else None),
        exit_reference_close=leg.exit_reference_close,
        last_available_session=mark_session,
        last_available_close=(mark.close if mark is not None and leg.exit_date is None else None),
        decision_cutoff=market.decision_cutoff,
        engine_buy_total=leg.buy_total,
        engine_sell_net=leg.sell_net,
    )
    return AdaptedLeg("OK", None, "MAPPED", mapping, leg_input)


def adapt_events(
    *,
    leg_events: Iterable[Mapping[str, Any]],
    transaction_events: Iterable[Mapping[str, Any]],
    episodes: Sequence[LifecycleEpisode],
    market: MarketEvidence,
    mark_evidence: Mapping[tuple[str, int, str], MarkEvidence] | None = None,
) -> dict[str, Any]:
    """Adapt every leg deterministically; HOLD rows never reach the ledger."""

    transactions = [TransactionEvent.from_engine_event(row) for row in transaction_events]
    ordered = sorted(
        leg_events,
        key=lambda item: (
            str(item.get("strategy")),
            str(item.get("matrix_id")),
            int(item.get("slippage_bps", 0)),
            int(item.get("leg_number", 0)),
            str(item.get("leg_id")),
        ),
    )
    key_fingerprints: dict[str, tuple[Any, ...]] = {}
    conflicts: set[str] = set()
    for row in ordered:
        key = str(row.get("economic_key") or "").strip()
        if not key:
            continue
        if int(row.get("leg_number", 1)) != 1:
            # An add-on inherits its trade episode's entry signal. A lifecycle
            # episode may contain multiple trade episodes after full exits.
            continue
        fingerprint = (
            str(row.get("symbol")),
            str(row.get("strategy")),
            str(row.get("entry_date")),
            str(row.get("entry_reference_close")),
            str(row.get("initial_stop")),
        )
        previous = key_fingerprints.get(key)
        if previous is None:
            key_fingerprints[key] = fingerprint
        elif previous != fingerprint:
            conflicts.add(key)
    adapted: list[AdaptedLeg] = []
    for row in ordered:
        leg = LegEvent.from_engine_event(row)
        mark = None
        if mark_evidence is not None:
            mark = mark_evidence.get((leg.symbol, leg.slippage_bps, leg.matrix_id))
        adapted.append(
            adapt_leg(
                leg=leg,
                episodes=episodes,
                market=market,
                transactions=transactions,
                mark=mark,
                conflicting_economic_keys=frozenset(conflicts),
            )
        )
    holds = [item for item in adapted if item.is_hold]
    return {
        "adapter_version": ADAPTER_VERSION,
        "leg_count": len(adapted),
        "mapped_count": len(adapted) - len(holds),
        "hold_count": len(holds),
        "hold_codes": sorted({item.hold_code for item in holds if item.hold_code}),
        "mapping_table": [item.to_dict() for item in adapted],
        "holds": [item.to_dict() for item in holds],
        "leg_inputs": [item.leg_input for item in adapted if item.leg_input is not None],
        "conflicting_economic_keys": sorted(conflicts),
        "mark_evidence_used": {
            f"{symbol}|{slippage}|{matrix}": {
                "session": mark.session,
                "close": mark.close,
                "source_sha256": mark.source_sha256,
            }
            for (symbol, slippage, matrix), mark in sorted((mark_evidence or {}).items())
        },
    }


def write_adapter_artifact(*, output_dir: Path, report: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical artifact with its own manifest and hash readback."""

    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite adapter artifact: {output_dir}")
    payload = {
        "mapping_table.json": canonical_bytes(
            {
                "adapter_version": ADAPTER_VERSION,
                "leg_count": report["leg_count"],
                "mapped_count": report["mapped_count"],
                "hold_count": report["hold_count"],
                "hold_codes": report["hold_codes"],
                "rows": report["mapping_table"],
            }
        )
        + b"\n",
        "hold_ledger.json": canonical_bytes(
            {"count": report["hold_count"], "rows": report["holds"]}
        )
        + b"\n",
    }
    output_dir.mkdir(parents=True)
    for name, blob in sorted(payload.items()):
        (output_dir / name).write_bytes(blob)
    manifest_rows = [
        {
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    ]
    (output_dir / "artifact_manifest.json").write_text(
        json.dumps(
            sorted(manifest_rows, key=lambda row: row["path"]),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    readback = {
        "status": "PASS",
        "verified": [
            {
                "path": row["path"],
                "sha256": hashlib.sha256(
                    (output_dir / row["path"]).read_bytes()
                ).hexdigest(),
            }
            for row in manifest_rows
        ],
    }
    (output_dir / "readback_verification.json").write_text(
        json.dumps(readback, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "adapter_version": ADAPTER_VERSION,
        "artifact_manifest_sha256": hashlib.sha256(
            (output_dir / "artifact_manifest.json").read_bytes()
        ).hexdigest(),
        "output_dir": str(output_dir.resolve()),
    }
