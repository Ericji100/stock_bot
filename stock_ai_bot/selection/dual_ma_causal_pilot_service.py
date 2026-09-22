"""Build an immutable, event-level causal-replay pilot for dual-MA research.

This module is intentionally isolated from the live scanner, scheduler, Telegram
handlers, and monitor list.  It only reads already frozen daily selection
manifests and the historical-only price cache.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from stock_ai_bot.data_sources import historical_price_service


SCHEMA_VERSION = "dual-ma-causal-replay-pilot-v1"
CONTRACT_VERSION = "dual-ma-01-to-02-market-data-v1"
DEFAULT_SELECTION_ROOT = Path("data") / "dual_ma" / "selection_manifests"
DEFAULT_BACKFILL_ROOT = Path("data") / "dual_ma" / "data_backfills"
DEFAULT_OUTPUT_ROOT = Path("data") / "dual_ma" / "pilots" / "causal_replay_v1"
TARGET_WARMUP_BARS = 250
HARD_MINIMUM_PRIOR_BARS = 143
CORE_EVENT_COUNT = 16
MAX_EVENT_COUNT = 20


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    frozen = {
        key: value
        for key, value in payload.items()
        if key not in {"manifest_id", "content_sha256", "generated_at"}
    }
    return _sha256_json(frozen)


def _validated_daily_manifest(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    digest = _manifest_digest(payload)
    report_date = str(payload.get("report_date") or "")
    expected_id = f"dual-ma-selection@{report_date}#{digest[:16]}"
    if payload.get("content_sha256") != digest:
        raise ValueError(f"selection manifest content hash mismatch: {path}")
    if payload.get("manifest_id") != expected_id or path.stem != expected_id:
        raise ValueError(f"selection manifest id mismatch: {path}")
    if payload.get("historical_replay") is not True:
        raise ValueError(f"selection manifest is not a historical replay: {path}")
    return payload


@dataclass(frozen=True)
class SelectionDay:
    report_date: date
    path: Path
    payload: Mapping[str, Any]

    @property
    def union_codes(self) -> frozenset[str]:
        return frozenset(str(value) for value in self.payload.get("union_codes") or ())

    @property
    def candidates(self) -> Mapping[str, Mapping[str, Any]]:
        return {
            str(row.get("code")): row
            for row in self.payload.get("candidates") or ()
            if isinstance(row, dict) and row.get("code")
        }


@dataclass(frozen=True)
class EntryEvent:
    code: str
    selected_on: date
    effective_on: date
    previous_completed_on: date
    programs: tuple[str, ...]
    current_day: SelectionDay
    previous_day: SelectionDay
    symbol: str
    market: str
    name: str
    instrument_type: str = "EQUITY"


def load_selection_days(
    selection_root: str | Path = DEFAULT_SELECTION_ROOT,
    *,
    through: date | None = None,
) -> list[SelectionDay]:
    root = Path(selection_root)
    grouped: dict[date, list[Path]] = {}
    for path in sorted(root.glob("*/*/dual-ma-selection@*.json")):
        try:
            report_date = date.fromisoformat(path.parent.name)
        except ValueError:
            continue
        if through is not None and report_date > through:
            continue
        grouped.setdefault(report_date, []).append(path)

    days: list[SelectionDay] = []
    for report_date, paths in sorted(grouped.items()):
        valid = [(path, _validated_daily_manifest(path)) for path in paths]
        if len(valid) != 1:
            raise ValueError(
                f"expected exactly one frozen selection manifest for {report_date}, found {len(valid)}"
            )
        path, payload = valid[0]
        days.append(SelectionDay(report_date, path.resolve(), payload))
    if len(days) < 2:
        raise ValueError("at least two completed selection days are required")
    return days


def load_trading_dates(end_date: date) -> list[date]:
    frame = historical_price_service.load_cached_history("^TWII", end_date)
    if frame.empty:
        raise ValueError("frozen ^TWII trading calendar is unavailable")
    return sorted({value.date() for value in frame["date"]})


def _next_trading_day(value: date, trading_dates: Sequence[date]) -> date:
    for candidate in trading_dates:
        if candidate > value:
            return candidate
    raise ValueError(f"no trading day is available after {value}")


def _snapshot_members(day: SelectionDay) -> Mapping[str, Mapping[str, Any]]:
    universe = (day.payload.get("provenance") or {}).get("universe") or {}
    raw_path = universe.get("snapshot_path")
    if not raw_path:
        raise ValueError(f"selection manifest lacks universe snapshot path: {day.path}")
    path = Path(str(raw_path))
    if not path.exists():
        raise ValueError(f"universe snapshot is unavailable: {path}")
    payload = _read_json(path)
    expected = str(payload.get("content_sha256") or "")
    frozen = {
        key: value
        for key, value in payload.items()
        if key not in {"snapshot_id", "content_sha256", "member_count"}
    }
    digest = _sha256_json(frozen)
    if digest != expected:
        raise ValueError(f"universe snapshot content hash mismatch: {path}")
    return {
        str(row.get("code")): row
        for row in payload.get("members") or ()
        if isinstance(row, dict) and row.get("code")
    }


def find_entry_events(
    days: Sequence[SelectionDay],
    trading_dates: Sequence[date],
) -> list[EntryEvent]:
    """Return inactive-to-active union transitions, excluding the left-censored first day."""
    events: list[EntryEvent] = []
    for previous, current in zip(days, days[1:]):
        if previous.report_date >= current.report_date:
            raise ValueError("selection days must be strictly increasing")
        members = _snapshot_members(current)
        for code in sorted(current.union_codes - previous.union_codes):
            candidate = current.candidates.get(code) or {}
            member = members.get(code)
            if member is None:
                raise ValueError(f"{code} is selected but absent from {current.report_date} universe")
            events.append(
                EntryEvent(
                    code=code,
                    selected_on=current.report_date,
                    effective_on=_next_trading_day(current.report_date, trading_dates),
                    previous_completed_on=previous.report_date,
                    programs=tuple(str(value) for value in candidate.get("programs") or ()),
                    current_day=current,
                    previous_day=previous,
                    symbol=str(member.get("symbol") or ""),
                    market=str(member.get("market") or ""),
                    name=str(member.get("name") or ""),
                )
            )
    return sorted(events, key=lambda event: (event.selected_on, event.code))


def _price_frame(symbol: str) -> pd.DataFrame:
    path = historical_price_service.historical_price_cache_path(symbol)
    if not path.exists():
        return pd.DataFrame(columns=[*historical_price_service.REQUIRED_COLUMNS, "adj_close"])
    return historical_price_service.standardize_history(pd.read_csv(path))


def _eligible_for_target_warmup(event: EntryEvent, frame: pd.DataFrame) -> bool:
    if frame.empty:
        return False
    dates = frame["date"].dt.date
    return int((dates < event.effective_on).sum()) >= TARGET_WARMUP_BARS and bool(
        (dates == event.effective_on).any()
    )


def select_pilot_events(
    events: Sequence[EntryEvent],
    price_frames: Mapping[str, pd.DataFrame],
    *,
    core_count: int = CORE_EVENT_COUNT,
    max_count: int = MAX_EVENT_COUNT,
) -> tuple[list[EntryEvent], Mapping[str, str], Mapping[str, Any]]:
    if not 12 <= core_count <= max_count <= 20:
        raise ValueError("pilot size must remain within 12 to 20 events")
    eligible = [
        event
        for event in events
        if _eligible_for_target_warmup(event, price_frames.get(event.symbol, pd.DataFrame()))
    ]
    if len(eligible) < core_count:
        raise ValueError(f"only {len(eligible)} events satisfy the 250-bar pilot requirement")

    selected = list(eligible[:core_count])
    roles: dict[str, str] = {
        f"{event.selected_on.isoformat()}:{event.code}": "CORE_FIXED"
        for event in selected
    }
    available_markets = {event.market for event in eligible if event.market}
    available_programs = {program for event in eligible for program in event.programs}
    covered_markets = {event.market for event in selected if event.market}
    covered_programs = {program for event in selected for program in event.programs}
    missing_tokens = {
        *(f"market:{value}" for value in available_markets - covered_markets),
        *(f"program:{value}" for value in available_programs - covered_programs),
    }

    while missing_tokens and len(selected) < max_count:
        best: EntryEvent | None = None
        best_gain: set[str] = set()
        for event in eligible:
            if event in selected:
                continue
            tokens = {f"market:{event.market}", *(f"program:{value}" for value in event.programs)}
            gain = tokens & missing_tokens
            if len(gain) > len(best_gain):
                best, best_gain = event, gain
        if best is None or not best_gain:
            break
        selected.append(best)
        roles[f"{best.selected_on.isoformat()}:{best.code}"] = "COVERAGE_SUPPLEMENT"
        missing_tokens -= best_gain

    selected.sort(key=lambda event: (event.selected_on, event.code))
    coverage = {
        "eligible_event_count": len(eligible),
        "available_markets": sorted(available_markets),
        "available_programs": sorted(available_programs),
        "selected_markets": sorted({event.market for event in selected if event.market}),
        "selected_programs": sorted({program for event in selected for program in event.programs}),
        "uncovered_tokens": sorted(missing_tokens),
    }
    return selected, roles, coverage


def _source_lineage(backfill_root: Path, symbol: str) -> list[dict[str, Any]]:
    lineage: list[dict[str, Any]] = []
    for path in sorted(backfill_root.glob("**/*.json")):
        payload = _read_json(path)
        for row in payload.get("results") or ():
            if not isinstance(row, dict) or row.get("status") != "SUCCESS":
                continue
            if str(row.get("symbol") or "") != symbol:
                continue
            lineage.append(
                {
                    "source": row.get("source") or payload.get("kind"),
                    "source_url": row.get("source_url"),
                    "requested_start": row.get("requested_start"),
                    "requested_end": row.get("requested_end"),
                    "first_trade_date": row.get("first_trade_date"),
                    "last_trade_date": row.get("last_trade_date"),
                    "lineage_manifest": str(path.resolve()),
                    "lineage_manifest_sha256": _sha256_bytes(path.read_bytes()),
                }
            )
    unique: dict[str, dict[str, Any]] = {}
    for row in lineage:
        unique[_canonical_json(row)] = row
    return [unique[key] for key in sorted(unique)]


def _coverage_metadata(symbol: str) -> dict[str, Any]:
    path = historical_price_service.historical_price_cache_path(symbol).with_suffix(".coverage.json")
    if not path.exists():
        return {"status": "MISSING"}
    payload = _read_json(path)
    return {
        "status": "PRESENT",
        "path": str(path.resolve()),
        "sha256": _sha256_bytes(path.read_bytes()),
        **payload,
    }


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    ordered_columns = [
        column
        for column in ("date", "open", "high", "low", "close", "volume", "adj_close")
        if column in frame.columns
    ]
    export = frame[ordered_columns].copy()
    export["date"] = export["date"].dt.strftime("%Y-%m-%d")
    return export.to_csv(index=False, lineterminator="\n", float_format="%.10g").encode("utf-8")


def _write_immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"immutable output collision: {path}")
        return
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _event_payload(
    event: EntryEvent,
    role: str,
    frame: pd.DataFrame,
    trading_dates: Sequence[date],
    output_root: Path,
    backfill_root: Path,
) -> dict[str, Any]:
    if frame.empty:
        raise ValueError(f"historical price data unavailable for {event.symbol}")
    if not frame["date"].is_monotonic_increasing or frame["date"].duplicated().any():
        raise ValueError(f"historical price dates are not strictly increasing for {event.symbol}")
    dates = frame["date"].dt.date
    prior = frame[dates < event.effective_on]
    prior_count = len(prior)
    if prior_count < HARD_MINIMUM_PRIOR_BARS:
        warmup_status = "INSUFFICIENT_WARMUP_LONG"
    elif prior_count < TARGET_WARMUP_BARS:
        warmup_status = "ABOVE_HARD_MIN_BELOW_TARGET"
    else:
        warmup_status = "TARGET_MET"
    warmup = prior.tail(min(TARGET_WARMUP_BARS, prior_count))
    if warmup.empty:
        raise ValueError(f"no pre-effective warmup data for {event.symbol}")
    pack = frame[dates >= warmup["date"].iloc[0].date()].copy()
    content = _csv_bytes(pack)
    bars_sha = _sha256_bytes(content)
    event_id = f"{event.selected_on.isoformat()}-{event.code}"
    bars_path = output_root / "event_bars" / f"dual-ma-event-bars@{event_id}#{bars_sha[:16]}.csv"
    _write_immutable(bars_path, content)

    pack_dates = set(pack["date"].dt.date)
    calendar_window = [
        value
        for value in trading_dates
        if pack["date"].iloc[0].date() <= value <= pack["date"].iloc[-1].date()
    ]
    missing_market_dates = [value.isoformat() for value in calendar_window if value not in pack_dates]
    required_nulls = int(pack[["open", "high", "low", "close"]].isna().sum().sum())
    effective_bar_present = event.effective_on in pack_dates
    can_handoff = warmup_status == "TARGET_MET" and effective_bar_present and required_nulls == 0

    current_hash = str(event.current_day.payload.get("content_sha256") or "")
    previous_hash = str(event.previous_day.payload.get("content_sha256") or "")
    return {
        "event_id": event_id,
        "sample_role": role,
        "code": event.code,
        "symbol": event.symbol,
        "name": event.name,
        "market": event.market,
        "instrument_type": event.instrument_type,
        "selected_on": event.selected_on.isoformat(),
        "effective_on": event.effective_on.isoformat(),
        "programs": list(event.programs),
        "entry_evidence": {
            "previous_completed_trading_day": event.previous_completed_on.isoformat(),
            "present_in_previous_union": False,
            "previous_selection_manifest": str(event.previous_day.path),
            "previous_selection_content_sha256": previous_hash,
            "selected_selection_manifest": str(event.current_day.path),
            "selected_selection_content_sha256": current_hash,
            "dataset_first_day_left_censored": False,
        },
        "warmup": {
            "status": warmup_status,
            "target_prior_bar_count": TARGET_WARMUP_BARS,
            "hard_minimum_prior_bar_count": HARD_MINIMUM_PRIOR_BARS,
            "available_prior_bar_count": prior_count,
            "warmup_bar_count": len(warmup),
            "warmup_first_date": warmup["date"].iloc[0].date().isoformat(),
            "warmup_last_date": warmup["date"].iloc[-1].date().isoformat(),
            "strictly_before_effective_on": True,
            "signal_and_trade_prohibited_during_warmup": True,
        },
        "market_data": {
            "bars_path": str(bars_path.resolve()),
            "bars_sha256": bars_sha,
            "bar_count": len(pack),
            "actual_first_date": pack["date"].iloc[0].date().isoformat(),
            "actual_last_date": pack["date"].iloc[-1].date().isoformat(),
            "available_through": pack["date"].iloc[-1].date().isoformat(),
            "columns": list(pack.columns),
            "dates_strictly_increasing": True,
            "duplicate_date_count": 0,
            "required_ohlc_null_count": required_nulls,
            "effective_on_bar_present": effective_bar_present,
            "missing_market_date_count": len(missing_market_dates),
            "missing_market_dates_sample": missing_market_dates[:20],
            "suspension_or_missing_date_note": (
                "Dates absent versus the ^TWII calendar may be suspensions, listing-state gaps, "
                "or source gaps; no synthetic bars were inserted."
            ),
            "source_lineage": _source_lineage(backfill_root, event.symbol),
            "cache_coverage": _coverage_metadata(event.symbol),
            "price_adjustment_and_corporate_action_policy": {
                "ohlc": "source raw/unadjusted OHLC",
                "adj_close": "preserved as a separate source field when available; never substituted into OHLC",
                "corporate_actions": "no synthetic back-adjustment and no forward/back fill",
            },
        },
        "can_handoff_to_02": can_handoff,
        "handoff_status": "READY" if can_handoff else warmup_status,
    }


def build_and_freeze_pilot(
    *,
    selection_root: str | Path = DEFAULT_SELECTION_ROOT,
    backfill_root: str | Path = DEFAULT_BACKFILL_ROOT,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    through: date | None = None,
    core_count: int = CORE_EVENT_COUNT,
    max_count: int = MAX_EVENT_COUNT,
) -> tuple[dict[str, Any], Path]:
    days = load_selection_days(selection_root, through=through)
    trading_dates = load_trading_dates(date(2100, 1, 1))
    events = find_entry_events(days, trading_dates)
    frames = {symbol: _price_frame(symbol) for symbol in sorted({event.symbol for event in events})}
    selected, roles, coverage = select_pilot_events(
        events,
        frames,
        core_count=core_count,
        max_count=max_count,
    )
    output = Path(output_root)
    backfills = Path(backfill_root)
    event_payloads = [
        _event_payload(
            event,
            roles[f"{event.selected_on.isoformat()}:{event.code}"],
            frames[event.symbol],
            trading_dates,
            output,
            backfills,
        )
        for event in selected
    ]
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "selection_coverage": {
            "first_completed_day": days[0].report_date.isoformat(),
            "last_completed_day": days[-1].report_date.isoformat(),
            "completed_day_count": len(days),
            "first_day_treated_as_left_censored": True,
        },
        "causal_contract": {
            "selected_on": "D-day list produced only after the complete D-day close",
            "effective_on": "next actual ^TWII trading day",
            "trade_before_effective_on": "PROHIBITED",
            "post_entry_signal_reset": (
                "From effective_on, each strategy episode must newly complete break-below-fast-MA "
                "then reclaim-fast-MA; pre-selection entry signals are not inherited."
            ),
            "episode_ownership": "02 independently evaluates 5/21 and 21/144 episodes",
            "sample_selection_uses_future_performance": False,
        },
        "sample_rule": {
            "core": (
                f"Earliest {core_count} eligible inactive-to-active union transitions ordered by "
                "selected_on then code, excluding the dataset first day and requiring 250 prior bars "
                "plus an effective_on bar."
            ),
            "supplement": (
                "Greedily add earliest eligible events that cover markets or selection programs absent "
                f"from the core, capped at {max_count} total; no future return or strategy outcome is used."
            ),
            "event_count": len(event_payloads),
        },
        "coverage": coverage,
        "known_limitations": [
            "The frozen historical universe contains listed-company equities only; no ETF or warrant product type is present.",
            "chip_4 remains MISSING_SOURCE because historical TDCC weekly data is unavailable.",
            "curated remains MISSING_DEPENDENCY because it requires chip_4.",
            "Source lineage is cache/backfill-manifest level rather than per-row vendor lineage.",
        ],
        "all_events_ready_for_02": all(row["can_handoff_to_02"] for row in event_payloads),
        "events": event_payloads,
    }
    digest = _sha256_json(payload)
    pilot_id = f"dual-ma-causal-pilot#{digest[:16]}"
    frozen = {**payload, "pilot_id": pilot_id, "content_sha256": digest}
    path = output / "manifests" / f"{pilot_id}.json"
    _write_immutable(
        path,
        (json.dumps(frozen, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return frozen, path
