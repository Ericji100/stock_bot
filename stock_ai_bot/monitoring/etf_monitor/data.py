from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from stock_ai_bot.workdata import get_workdata_path

from stock_ai_bot.data_sources.causal_price_contract import (
    ActionKind,
    CorporateActionEvent,
    CorporateActionType,
)
from stock_ai_bot.data_sources.etf_snapshot_registry import (
    EtfSnapshotRegistryError,
    resolve_snapshot,
    validate_snapshot,
)

from .config import EtfMonitorConfig
from .delta_pipeline import (
    EMERGENCY_CLOSURE_URL,
    HOLIDAY_URL,
    build_daily_delta_bundle,
    cached_emergency_notice,
    parse_emergency_closures,
    parse_holidays,
    verify_delta_receipts,
)
from .errors import EtfMonitorDataError
from .models import IntradayQuote, MarketDataSnapshot, MonitorPhase, stable_sha256


TAIPEI = ZoneInfo("Asia/Taipei")
TWSE_MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
RAW_EXECUTION_BASIS = "UNADJUSTED_MAINBOARD_OHLCV"
DAILY_DELTA_SCHEMA = "stock-ai-bot-etf-daily-delta-v1"
FROZEN_MARKET_DATA_ROOT_ENV = "STOCK_AI_BOT_ETF_FROZEN_MARKET_DATA_ROOT"

_EXISTING_IDENTICAL = "IDENTICAL"
_EXISTING_INVALID = "INVALID"
_EXISTING_VALID_DIFFERENT = "VALID_DIFFERENT"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EtfMonitorDataError(f"cannot read accepted snapshot artifact: {path}") from exc


def _contained(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise EtfMonitorDataError("configured snapshot artifact escapes package root") from exc
    return candidate


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _parse_event(row: Mapping[str, Any]) -> CorporateActionEvent:
    payload = dict(row)
    try:
        payload["action_type"] = CorporateActionType(payload["action_type"])
        payload["action_kind"] = ActionKind(payload["action_kind"])
        for key in (
            "effective_date",
            "ex_date",
            "available_on",
            "announcement_date",
            "entitlement_date",
            "payment_date",
            "halt_date",
            "resume_date",
        ):
            if payload.get(key):
                payload[key] = date.fromisoformat(str(payload[key]))
        payload["fetched_at"] = datetime.fromisoformat(str(payload["fetched_at"]))
        event = CorporateActionEvent(**payload)
        event.validate()
        return event
    except (KeyError, TypeError, ValueError) as exc:
        raise EtfMonitorDataError("accepted corporate-action event is invalid") from exc


def resolve_frozen_market_data_root(config: EtfMonitorConfig) -> Path:
    """Resolve the immutable accepted registry independently of mutable workdata."""

    configured = os.environ.get(FROZEN_MARKET_DATA_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    repository_root = Path(__file__).resolve().parents[3]
    return (repository_root / config.frozen_market_data_relative_path).resolve()


def validate_daily_delta_payload(
    payload: Mapping[str, Any],
    *,
    expected_date: date,
    snapshot_id: str,
    manifest_sha256: str,
    receipt_root: Path,
) -> dict[str, Any]:
    """Validate one immutable calendar-day delta before it can extend the frozen base."""

    if payload.get("schema_version") != DAILY_DELTA_SCHEMA:
        raise EtfMonitorDataError("unsupported ETF daily delta schema")
    if str(payload.get("symbol") or "") != "0050":
        raise EtfMonitorDataError("daily delta symbol is not 0050")
    if str(payload.get("trade_date") or "") != expected_date.isoformat():
        raise EtfMonitorDataError("daily delta date does not match its chain position")
    if str(payload.get("frozen_snapshot_id") or "") != snapshot_id:
        raise EtfMonitorDataError("daily delta frozen snapshot id mismatch")
    if str(payload.get("frozen_manifest_sha256") or "").upper() != manifest_sha256.upper():
        raise EtfMonitorDataError("daily delta frozen manifest SHA-256 mismatch")
    expected_previous = (expected_date - timedelta(days=1)).isoformat()
    if str(payload.get("previous_calendar_date") or "") != expected_previous:
        raise EtfMonitorDataError("daily delta calendar chain is not contiguous")
    supplied_hash = str(payload.get("content_sha256") or "").lower()
    hash_input = {key: value for key, value in payload.items() if key != "content_sha256"}
    if supplied_hash != stable_sha256(hash_input):
        raise EtfMonitorDataError("daily delta content SHA-256 mismatch")
    status = str(payload.get("market_status") or "")
    if status not in {"OPEN", "CLOSED"}:
        raise EtfMonitorDataError("daily delta market_status must be OPEN or CLOSED")
    bar = payload.get("bar")
    if status == "CLOSED":
        if bar is not None:
            raise EtfMonitorDataError("closed daily delta must not contain a bar")
    else:
        if not isinstance(bar, Mapping):
            raise EtfMonitorDataError("open daily delta has no complete bar")
        for field in ("open", "high", "low", "close", "volume"):
            try:
                value = Decimal(str(bar[field]))
            except (KeyError, ValueError) as exc:
                raise EtfMonitorDataError(f"daily delta bar has invalid {field}") from exc
            if value < 0 or (field != "volume" and value == 0):
                raise EtfMonitorDataError(f"daily delta bar has invalid {field}")
        if Decimal(str(bar["high"])) < max(
            Decimal(str(bar["open"])), Decimal(str(bar["close"])), Decimal(str(bar["low"]))
        ) or Decimal(str(bar["low"])) > min(
            Decimal(str(bar["open"])), Decimal(str(bar["close"])), Decimal(str(bar["high"]))
        ):
            raise EtfMonitorDataError("daily delta bar violates OHLC ordering")
    actions = payload.get("corporate_actions", [])
    if not isinstance(actions, list):
        raise EtfMonitorDataError("daily delta corporate_actions must be a list")
    for event in actions:
        if not isinstance(event, Mapping):
            raise EtfMonitorDataError("daily delta corporate action is malformed")
        _parse_event(event)
    verify_delta_receipts(
        payload,
        expected_date=expected_date,
        receipt_root=receipt_root,
    )
    return dict(payload)


def load_daily_delta_file(
    path: Path,
    *,
    expected_date: date,
    snapshot_id: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise EtfMonitorDataError("daily delta root is not an object")
    return validate_daily_delta_payload(
        payload,
        expected_date=expected_date,
        snapshot_id=snapshot_id,
        manifest_sha256=manifest_sha256,
        receipt_root=path.parent,
    )


def _fsync_directory(path: Path) -> None:
    """Best-effort durability barrier for a same-directory metadata update."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Windows does not expose a portable directory fsync. The file itself
        # is still fsynced before it becomes visible at the immutable target.
        pass
    finally:
        os.close(descriptor)


def _write_fully_synced_temp(target: Path, raw: bytes) -> Path:
    """Write a complete same-directory temp file without exposing the target."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _classify_content_addressed_receipt(target: Path, expected: bytes) -> str:
    if target.is_symlink():
        return _EXISTING_VALID_DIFFERENT
    try:
        current = target.read_bytes()
    except OSError as exc:
        raise EtfMonitorDataError(
            "cannot inspect an existing immutable receipt"
        ) from exc
    if current == expected:
        return _EXISTING_IDENTICAL
    if hashlib.sha256(current).hexdigest() == target.stem.lower():
        return _EXISTING_VALID_DIFFERENT
    return _EXISTING_INVALID


def _classify_self_hashed_json(
    target: Path,
    expected: bytes,
    *,
    schema_version: str,
) -> str:
    if target.is_symlink():
        return _EXISTING_VALID_DIFFERENT
    try:
        current = target.read_bytes()
    except OSError as exc:
        raise EtfMonitorDataError(
            "cannot inspect an existing immutable JSON artifact"
        ) from exc
    if current == expected:
        return _EXISTING_IDENTICAL
    try:
        payload = json.loads(current.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _EXISTING_INVALID
    try:
        expected_payload = json.loads(expected.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EtfMonitorDataError(
            "new immutable JSON artifact is not readable"
        ) from exc
    if payload == expected_payload:
        return _EXISTING_IDENTICAL
    if not isinstance(payload, Mapping) or payload.get("schema_version") != schema_version:
        return _EXISTING_INVALID
    supplied_hash = str(payload.get("content_sha256") or "").lower()
    hash_input = {key: value for key, value in payload.items() if key != "content_sha256"}
    if supplied_hash and supplied_hash == stable_sha256(hash_input):
        return _EXISTING_VALID_DIFFERENT
    return _EXISTING_INVALID


def _quarantine_invalid_artifact(target: Path) -> None:
    quarantine = target.with_name(f".{target.name}.invalid-{uuid.uuid4().hex}")
    try:
        os.replace(target, quarantine)
    except OSError as exc:
        raise EtfMonitorDataError(
            "cannot quarantine an interrupted immutable artifact"
        ) from exc
    _fsync_directory(target.parent)


def _atomic_install_immutable_bytes(
    target: Path,
    raw: bytes,
    *,
    classify_existing: Any,
    collision_message: str,
) -> None:
    """Publish fully durable bytes at a new immutable name, never overwriting it."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target):
        state = classify_existing(target, raw)
        if state == _EXISTING_IDENTICAL:
            return
        if state == _EXISTING_VALID_DIFFERENT:
            raise EtfMonitorDataError(collision_message)
        _quarantine_invalid_artifact(target)

    temporary: Path | None = None
    try:
        temporary = _write_fully_synced_temp(target, raw)
        try:
            # The temp and target share a directory/filesystem. A hard link is
            # an atomic create-if-absent operation and cannot replace a target.
            os.link(temporary, target)
        except FileExistsError as exc:
            state = classify_existing(target, raw)
            if state == _EXISTING_IDENTICAL:
                return
            raise EtfMonitorDataError(collision_message) from exc
        except OSError as exc:
            raise EtfMonitorDataError(
                "cannot atomically publish immutable ETF evidence"
            ) from exc
        _fsync_directory(target.parent)
        try:
            installed = target.read_bytes()
        except OSError as exc:
            raise EtfMonitorDataError(
                "cannot read back installed immutable ETF evidence"
            ) from exc
        if installed != raw:
            raise EtfMonitorDataError(
                "immutable ETF evidence failed post-install readback"
            )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
            _fsync_directory(target.parent)


def install_daily_delta_file(
    path: Path,
    *,
    destination_root: Path,
    expected_date: date,
    snapshot_id: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    """Validate and immutably install one delta and all referenced raw receipts."""

    payload = load_daily_delta_file(
        path,
        expected_date=expected_date,
        snapshot_id=snapshot_id,
        manifest_sha256=manifest_sha256,
    )
    destination_root.mkdir(parents=True, exist_ok=True)
    for evidence in (
        payload["calendar_evidence"],
        payload["bar_evidence"],
        *payload["corporate_action_evidence"],
        *([payload["emergency_closure_evidence"]] if payload.get("emergency_closure_evidence") else []),
    ):
        relative = Path(str(evidence["receipt_relative_path"]))
        source_receipt = path.parent / relative
        destination_receipt = destination_root / relative
        raw = source_receipt.read_bytes()
        _atomic_install_immutable_bytes(
            destination_receipt,
            raw,
            classify_existing=_classify_content_addressed_receipt,
            collision_message="refusing to replace content-addressed receipt",
        )
    destination = destination_root / f"{expected_date.isoformat()}.json"
    canonical = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_install_immutable_bytes(
        destination,
        canonical,
        classify_existing=lambda target, raw: _classify_self_hashed_json(
            target,
            raw,
            schema_version=DAILY_DELTA_SCHEMA,
        ),
        collision_message="refusing to replace immutable delta",
    )
    return payload


class OfficialTwseTradingCalendar:
    """ETF-only official calendar gate with no empty-calendar fallback."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 15,
        receipt_root: Path | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.receipt_root = None if receipt_root is None else receipt_root.resolve()
        self._cache: dict[int, tuple[set[date], dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._notice_cache: tuple[set[date], dict[str, Any]] | None = None

    def _emergency_evidence(self, raw: bytes) -> dict[str, Any]:
        return {
            "source": "TWSE_EMERGENCY_CLOSURE_OFFICIAL",
            "source_endpoint": EMERGENCY_CLOSURE_URL,
            "request_url": EMERGENCY_CLOSURE_URL,
            "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def _emergency_notice(self) -> tuple[set[date], dict[str, Any]]:
        now = datetime.now(tz=TAIPEI)
        # Do not retain a morning 'no closure' result for the whole day.
        if self._notice_cache is not None and (
            now - datetime.fromisoformat(self._notice_cache[1]["fetched_at"])
        ).total_seconds() < 60:
            return self._notice_cache
        try:
            response = requests.get(EMERGENCY_CLOSURE_URL, timeout=self.timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise EtfMonitorDataError("official emergency notice request failed") from exc
        if str(getattr(response, "url", "")) != EMERGENCY_CLOSURE_URL:
            raise EtfMonitorDataError("official emergency notice endpoint identity changed")
        raw = response.content
        days = parse_emergency_closures(raw)
        evidence = {**self._emergency_evidence(raw), "fetched_at": now.isoformat()}
        if self.receipt_root is not None:
            self.receipt_root.mkdir(parents=True, exist_ok=True)
            _atomic_install_immutable_bytes(
                self.receipt_root / f"{evidence['receipt_sha256']}.raw", raw,
                classify_existing=_classify_content_addressed_receipt,
                collision_message="official emergency notice receipt identity collision",
            )
        self._notice_cache = days, evidence
        return self._notice_cache

    def _load(self, year: int) -> tuple[set[date], dict[str, Any]]:
        try:
            response = requests.get(HOLIDAY_URL, timeout=self.timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise EtfMonitorDataError("official TWSE holiday request failed") from exc
        final_url = str(getattr(response, "url", HOLIDAY_URL) or HOLIDAY_URL)
        expected = urlparse(HOLIDAY_URL)
        actual = urlparse(final_url)
        if (
            actual.scheme != expected.scheme
            or actual.netloc != expected.netloc
            or actual.path != expected.path
            or actual.query
            or actual.fragment
        ):
            raise EtfMonitorDataError("official TWSE holiday endpoint identity changed")
        raw = response.content
        holidays = parse_holidays(raw)
        if not any(day.year == year for day in holidays):
            raise EtfMonitorDataError("official TWSE holiday receipt does not cover target year")
        digest = hashlib.sha256(raw).hexdigest()
        if self.receipt_root is not None:
            self.receipt_root.mkdir(parents=True, exist_ok=True)
            receipt = self.receipt_root / f"{digest}.raw"
            _atomic_install_immutable_bytes(
                receipt,
                raw,
                classify_existing=_classify_content_addressed_receipt,
                collision_message="official calendar receipt identity collision",
            )
        evidence = {
            "source": "TWSE_HOLIDAY_SCHEDULE_OFFICIAL",
            "source_endpoint": HOLIDAY_URL,
            "request_url": final_url,
            "fetched_at": datetime.now(tz=TAIPEI).isoformat(),
            "receipt_sha256": digest,
            "target_year": year,
        }
        return holidays, evidence

    def verify(self, trading_date: date) -> Mapping[str, Any]:
        with self._lock:
            stored = cached_emergency_notice(self.receipt_root, trading_date)
            if stored is not None:
                return {
                    **self._emergency_evidence(stored),
                    "trading_date": trading_date.isoformat(),
                    "is_trading_day": False,
                    "calendar_reason": "OFFICIAL_EMERGENCY_CLOSURE",
                }
            cached = self._cache.get(trading_date.year)
            today = datetime.now(tz=TAIPEI).date()
            if (
                cached is None
                or datetime.fromisoformat(cached[1]["fetched_at"]).date() != today
            ):
                cached = self._load(trading_date.year)
                self._cache[trading_date.year] = cached
        holidays, evidence = cached
        if trading_date.weekday() < 5 and trading_date not in holidays:
            with self._lock:
                closure_days, closure_evidence = self._emergency_notice()
            if trading_date in closure_days:
                return {
                    **closure_evidence,
                    "trading_date": trading_date.isoformat(),
                    "is_trading_day": False,
                    "calendar_reason": "OFFICIAL_EMERGENCY_CLOSURE",
                }
        return {
            **evidence,
            "trading_date": trading_date.isoformat(),
            "is_trading_day": (
                trading_date.weekday() < 5 and trading_date not in holidays
            ),
            "calendar_reason": (
                "WEEKEND"
                if trading_date.weekday() >= 5
                else "OFFICIAL_HOLIDAY"
                if trading_date in holidays
                else "OFFICIAL_WEEKDAY_SESSION"
            ),
        }


def _normalize_rows(rows: Sequence[Mapping[str, Any]], *, through: date) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    prior: date | None = None
    for source in rows:
        row = dict(source)
        day = _parse_date(row["date"])
        if day > through:
            continue
        if prior is not None and day <= prior:
            raise EtfMonitorDataError("accepted snapshot dates are not unique ascending")
        for field in ("open", "high", "low", "close"):
            value = row.get(field, row.get(f"raw_{field}"))
            if value is None or Decimal(str(value)) <= 0:
                raise EtfMonitorDataError(f"accepted raw row has invalid {field}")
            row[field] = value
        row["volume"] = row.get("volume", row.get("raw_volume", 0))
        row["date"] = day
        normalized.append(row)
        prior = day
    if not normalized:
        raise EtfMonitorDataError("accepted snapshot has no usable rows")
    return tuple(normalized)


class RegistrySnapshotProvider:
    """Loads an immutable accepted base plus a contiguous verified daily ledger."""

    def __init__(
        self,
        config: EtfMonitorConfig,
        *,
        market_data_root: Path | None = None,
        delta_root: Path | None = None,
    ):
        self.config = config
        self.market_data_root = (market_data_root or resolve_frozen_market_data_root(config)).resolve()
        self.delta_root = (
            delta_root
            or get_workdata_path(config.delta_relative_path)
        ).resolve()

    def _extend_with_deltas(
        self,
        *,
        snapshot: Any,
        rows: tuple[dict[str, Any], ...],
        events: tuple[CorporateActionEvent, ...],
        through: date,
    ) -> tuple[tuple[dict[str, Any], ...], tuple[CorporateActionEvent, ...], dict[str, Any]]:
        base_last = rows[-1]["date"]
        if through <= base_last:
            return rows, events, {"delta_count": 0, "delta_through": None}
        extended = list(rows)
        events_by_id = {event.action_id: event for event in events}
        cursor = base_last + timedelta(days=1)
        count = 0
        latest_hash: str | None = None
        while cursor <= through:
            path = self.delta_root / f"{cursor.isoformat()}.json"
            if not path.is_file():
                raise EtfMonitorDataError(
                    f"daily delta chain is missing {cursor.isoformat()}: {path}"
                )
            payload = load_daily_delta_file(
                path,
                expected_date=cursor,
                snapshot_id=snapshot.snapshot_id,
                manifest_sha256=snapshot.package_manifest_sha256,
            )
            if payload["market_status"] == "OPEN":
                bar = dict(payload["bar"])
                bar["date"] = cursor
                bar["source"] = "TWSE_OFFICIAL_DAILY_DELTA"
                bar["raw_response_sha256"] = payload["bar_evidence"]["receipt_sha256"]
                extended.append(bar)
            for raw_event in payload.get("corporate_actions", []):
                parsed = _parse_event(raw_event)
                existing = events_by_id.get(parsed.action_id)
                if existing is not None and existing != parsed:
                    raise EtfMonitorDataError("daily delta corporate-action identity collision")
                events_by_id[parsed.action_id] = parsed
            latest_hash = str(payload["content_sha256"])
            count += 1
            cursor += timedelta(days=1)
        return (
            tuple(extended),
            tuple(sorted(events_by_id.values(), key=lambda event: (event.event_date, event.action_id))),
            {
                "delta_count": count,
                "delta_through": through.isoformat(),
                "delta_latest_sha256": latest_hash,
                "delta_root": str(self.delta_root),
            },
        )

    def _base(
        self, through: date
    ) -> tuple[Any, tuple[dict[str, Any], ...], tuple[Any, ...], dict[str, Any]]:
        try:
            snapshot = resolve_snapshot(
                self.config.symbol,
                self.config.snapshot_id,
                market_data_root=self.market_data_root,
            )
            validation = validate_snapshot(snapshot, verify_artifacts=True)
        except EtfSnapshotRegistryError as exc:
            raise EtfMonitorDataError(f"accepted ETF snapshot unavailable: {exc}") from exc
        daily_payload = _read_json(
            _contained(snapshot.package_root, self.config.raw_daily_relative_path)
        )
        if not isinstance(daily_payload, Mapping) or not isinstance(daily_payload.get("rows"), list):
            raise EtfMonitorDataError("accepted raw daily artifact has no rows")
        rows = _normalize_rows(daily_payload["rows"], through=through)
        event_payload = _read_json(
            _contained(snapshot.package_root, self.config.corporate_actions_relative_path)
        )
        if not isinstance(event_payload, list):
            raise EtfMonitorDataError("accepted corporate-action artifact is not a list")
        parsed_events = tuple(_parse_event(row) for row in event_payload)
        events = tuple(event for event in parsed_events if event.event_date <= through)
        dividends_payload = _read_json(
            _contained(snapshot.package_root, self.config.dividends_relative_path)
        )
        if not isinstance(dividends_payload, Mapping):
            raise EtfMonitorDataError("accepted dividend artifact is not an object")
        rows, events, delta_validation = self._extend_with_deltas(
            snapshot=snapshot,
            rows=rows,
            events=events,
            through=through,
        )
        validation["dividends_artifact_validated"] = True
        validation["frozen_market_data_root"] = str(self.market_data_root)
        validation.update(delta_validation)
        return (snapshot, rows, events, validation)

    def load_complete(self, as_of: date) -> MarketDataSnapshot:
        snapshot, rows, events, validation = self._base(as_of)
        if rows[-1]["date"] != as_of:
            raise EtfMonitorDataError(
                f"accepted full-day snapshot is stale: {rows[-1]['date']} != {as_of}"
            )
        return MarketDataSnapshot(
            symbol="0050",
            as_of=as_of,
            raw_rows=rows,
            corporate_actions=events,
            source="HASH_VERIFIED_ETF_SNAPSHOT_REGISTRY",
            price_basis=RAW_EXECUTION_BASIS,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=str(snapshot.metadata.get("version") or snapshot.snapshot_id),
            snapshot_sha256=snapshot.package_manifest_sha256,
            fetched_at=datetime.now(tz=TAIPEI),
            is_complete=True,
            metadata=validation,
        )

    def load_provisional(self, as_of: date, quote: IntradayQuote) -> MarketDataSnapshot:
        if quote.symbol != "0050" or quote.trade_date != as_of:
            raise EtfMonitorDataError("official quote identity/date does not match 0050 run")
        prior_day = as_of - timedelta(days=1)
        snapshot, rows, events, validation = self._base(prior_day)
        without_today = tuple(row for row in rows if row["date"] != as_of)
        if not without_today or without_today[-1]["date"] >= as_of:
            raise EtfMonitorDataError("cannot append provisional official quote")
        quote_row = {
            "date": as_of,
            "open": quote.raw_open,
            "high": quote.raw_high,
            "low": quote.raw_low,
            "close": quote.raw_last,
            "volume": quote.raw_volume,
            "source": quote.source,
            "raw_response_sha256": quote.receipt_sha256,
        }
        derived_sha = stable_sha256(
            {
                "package_manifest_sha256": snapshot.package_manifest_sha256,
                "quote_receipt_sha256": quote.receipt_sha256,
                "as_of": as_of,
            }
        )
        return MarketDataSnapshot(
            symbol="0050",
            as_of=as_of,
            raw_rows=without_today + (quote_row,),
            corporate_actions=events,
            source="HASH_VERIFIED_REGISTRY_PLUS_TWSE_MIS",
            price_basis=RAW_EXECUTION_BASIS,
            snapshot_id=f"{snapshot.snapshot_id}+mis-{as_of.isoformat()}",
            snapshot_version=str(snapshot.metadata.get("version") or snapshot.snapshot_id),
            snapshot_sha256=derived_sha,
            fetched_at=quote.observed_at,
            is_complete=False,
            is_provisional=True,
            quote=quote,
            metadata=validation,
        )


class OfficialDailyDeltaUpdater:
    """Capture, validate and immutably install ordinary TWSE daily deltas.

    The capture pipeline refuses unresolved weekday gaps, source disagreement,
    and all corporate-action days.  Those cases remain fail-closed for an
    operator-reviewed evidence package; there is no alternate data fallback.
    """

    def __init__(
        self,
        registry: RegistrySnapshotProvider,
        *,
        session_factory: Any = requests.Session,
    ):
        self.registry = registry
        self.session_factory = session_factory
        self._lock = threading.Lock()

    def _identity(self) -> tuple[Any, date]:
        try:
            snapshot = resolve_snapshot(
                self.registry.config.symbol,
                self.registry.config.snapshot_id,
                market_data_root=self.registry.market_data_root,
            )
            validate_snapshot(snapshot, verify_artifacts=True)
        except EtfSnapshotRegistryError as exc:
            raise EtfMonitorDataError(
                f"accepted ETF snapshot unavailable for automatic delta update: {exc}"
            ) from exc
        daily = _read_json(
            _contained(
                snapshot.package_root,
                self.registry.config.raw_daily_relative_path,
            )
        )
        if not isinstance(daily, Mapping) or not isinstance(daily.get("rows"), list):
            raise EtfMonitorDataError("accepted raw daily artifact has no rows")
        try:
            base_last = max(_parse_date(row["date"]) for row in daily["rows"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EtfMonitorDataError("accepted raw daily artifact has invalid dates") from exc
        return snapshot, base_last

    def _verify_installed_range(
        self,
        *,
        start: date,
        through: date,
        snapshot: Any,
    ) -> date | None:
        cursor = start
        while cursor <= through:
            path = self.registry.delta_root / f"{cursor.isoformat()}.json"
            if not path.is_file():
                return cursor
            try:
                load_daily_delta_file(
                    path,
                    expected_date=cursor,
                    snapshot_id=snapshot.snapshot_id,
                    manifest_sha256=snapshot.package_manifest_sha256,
                )
            except EtfMonitorDataError:
                # A prior process may have exposed a partial final with the
                # legacy direct writer, or a valid delta may reference a
                # partial receipt. Treat it as needing recapture so the
                # immutable installer can quarantine invalid bytes, repair
                # receipts, or refuse a different valid delta without overwrite.
                return cursor
            cursor += timedelta(days=1)
        return None

    def ensure_through(self, through: date) -> Mapping[str, Any]:
        with self._lock:
            snapshot, base_last = self._identity()
            chain_start = base_last + timedelta(days=1)
            if through < chain_start:
                return {
                    "status": "FROZEN_BASE_COVERS_TARGET",
                    "through": through.isoformat(),
                    "installed": 0,
                }
            first_missing = self._verify_installed_range(
                start=chain_start,
                through=through,
                snapshot=snapshot,
            )
            if first_missing is None:
                return {
                    "status": "VERIFIED_ALREADY_COMPLETE",
                    "through": through.isoformat(),
                    "installed": 0,
                }

            staging_parent = self.registry.delta_root.parent / ".capture-staging"
            staging_parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="0050-official-delta-",
                dir=staging_parent,
            ) as temporary:
                bundle_root = Path(temporary) / "bundle"
                manifest = build_daily_delta_bundle(
                    start=first_missing,
                    through=through,
                    output=bundle_root,
                    frozen_snapshot_id=snapshot.snapshot_id,
                    frozen_manifest_sha256=snapshot.package_manifest_sha256,
                    session_factory=self.session_factory,
                    closure_receipt_root=self.registry.delta_root / "calendar-gate-receipts",
                )
                cursor = first_missing
                validated: list[tuple[date, Path]] = []
                while cursor <= through:
                    path = bundle_root / f"{cursor.isoformat()}.json"
                    load_daily_delta_file(
                        path,
                        expected_date=cursor,
                        snapshot_id=snapshot.snapshot_id,
                        manifest_sha256=snapshot.package_manifest_sha256,
                    )
                    validated.append((cursor, path))
                    cursor += timedelta(days=1)
                for day, path in validated:
                    install_daily_delta_file(
                        path,
                        destination_root=self.registry.delta_root,
                        expected_date=day,
                        snapshot_id=snapshot.snapshot_id,
                        manifest_sha256=snapshot.package_manifest_sha256,
                    )
                manifests = self.registry.delta_root / "manifests"
                manifests.mkdir(parents=True, exist_ok=True)
                bundle_manifest = bundle_root / "bundle-manifest.json"
                raw_manifest = bundle_manifest.read_bytes()
                destination = manifests / f"{manifest['content_sha256']}.json"
                _atomic_install_immutable_bytes(
                    destination,
                    raw_manifest,
                    classify_existing=lambda target, raw: _classify_self_hashed_json(
                        target,
                        raw,
                        schema_version="stock-ai-bot-etf-daily-delta-bundle-v1",
                    ),
                    collision_message=(
                        "automatic delta bundle manifest identity collision"
                    ),
                )

            remaining = self._verify_installed_range(
                start=chain_start,
                through=through,
                snapshot=snapshot,
            )
            if remaining is not None:
                raise EtfMonitorDataError(
                    f"automatic delta install did not close the chain at {remaining}"
                )
            return {
                "status": "CAPTURED_VALIDATED_IMMUTABLY_INSTALLED",
                "from": first_missing.isoformat(),
                "through": through.isoformat(),
                "installed": len(validated),
                "bundle_manifest_sha256": manifest["content_sha256"],
            }


def _price(value: Any, field: str) -> Decimal:
    text = str(value or "").replace(",", "").strip()
    if text in {"", "-", "--"}:
        raise EtfMonitorDataError(
            f"TWSE MIS quote has no {field}",
            reason_code=f"MIS_MISSING_{field.upper()}_PRICE",
        )
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise EtfMonitorDataError(
            f"TWSE MIS quote has invalid {field}",
            reason_code=f"MIS_INVALID_{field.upper()}_PRICE",
        ) from exc
    if not result.is_finite() or result <= 0:
        raise EtfMonitorDataError(
            f"TWSE MIS quote has invalid {field}",
            reason_code=f"MIS_INVALID_{field.upper()}_PRICE",
        )
    return result


class OfficialTwseQuoteProvider:
    def __init__(self, *, timeout_seconds: int = 15, max_source_age_seconds: int = 300):
        self.timeout_seconds = timeout_seconds
        self.max_source_age_seconds = max_source_age_seconds

    def _fetch(self, observed_at: datetime) -> IntradayQuote:
        response = None
        try:
            response = requests.get(
                TWSE_MIS_URL,
                params={
                    "ex_ch": "tse_0050.tw", "json": "1", "delay": "0",
                    "_": str(int(observed_at.timestamp() * 1000)),
                },
                headers={"Referer": "https://mis.twse.com.tw/stock/"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise EtfMonitorDataError(
                "official TWSE MIS quote request failed",
                reason_code="MIS_REQUEST_FAILED",
                details={
                    "source_endpoint": TWSE_MIS_URL,
                    "observed_at": observed_at.isoformat(),
                    "http_status": getattr(response, "status_code", None),
                    "cause_type": type(exc).__name__,
                },
            ) from exc
        receipt_sha256 = hashlib.sha256(response.content).hexdigest()
        diagnostics: dict[str, Any] = {
            "source_endpoint": TWSE_MIS_URL,
            "observed_at": observed_at.isoformat(),
            "http_status": getattr(response, "status_code", None),
            "receipt_sha256": receipt_sha256,
        }
        if isinstance(payload, Mapping):
            diagnostics.update({key: payload.get(key) for key in ("rtcode", "rtmessage", "queryTime")})
            rows = payload.get("msgArray")
            if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], Mapping):
                diagnostics["quote"] = {
                    key: rows[0].get(key)
                    for key in ("c", "d", "t", "ts", "mt", "o", "h", "l", "z", "v")
                }
                trade = rows[0].get("trade")
                if isinstance(trade, Mapping):
                    diagnostics["quote"]["trade"] = {key: trade.get(key) for key in ("t", "z")}
        try:
            return self._parse(payload, observed_at, receipt_sha256)
        except EtfMonitorDataError as exc:
            exc.details = {**diagnostics, **exc.details}
            raise

    def _parse(self, payload: Any, observed_at: datetime, receipt_sha256: str) -> IntradayQuote:
        rows = payload.get("msgArray") if isinstance(payload, Mapping) else None
        if not isinstance(payload, Mapping) or payload.get("rtcode") != "0000" or payload.get("rtmessage") != "OK":
            raise EtfMonitorDataError("official TWSE MIS status is not OK", reason_code="MIS_STATUS_NOT_OK")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise EtfMonitorDataError("official TWSE MIS returned an unexpected quote set", reason_code="MIS_QUOTE_SET_INVALID")
        row = rows[0]
        if str(row.get("c") or "") != "0050":
            raise EtfMonitorDataError("official TWSE MIS returned the wrong symbol", reason_code="MIS_SYMBOL_MISMATCH")
        raw_date = str(row.get("d") or "")
        try:
            trade_date = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError as exc:
            raise EtfMonitorDataError("official TWSE MIS returned an invalid trade date", reason_code="MIS_DATE_INVALID") from exc
        local_observed_at = observed_at.astimezone(TAIPEI)
        if trade_date != local_observed_at.date():
            raise EtfMonitorDataError("official TWSE MIS clocks disagree on the trading date", reason_code="MIS_DATE_MISMATCH")
        # ts identifies a reference/trial quote. mt is not this status flag:
        # normal intraday responses legitimately contain a nonzero mt.
        if str(row.get("ts") or "") != "0":
            raise EtfMonitorDataError("official TWSE MIS quote is not an actual trade", reason_code="MIS_REFERENCE_QUOTE")
        last_price, trade_time = row.get("z"), row.get("t")
        if last_price is None or str(last_price).strip() in {"", "-", "--"}:
            # Same-response actual-trade object, as used by TWSE's own
            # tradeFallback frontend. Never use pz, bid/ask or yesterday's close.
            trade = row.get("trade")
            if isinstance(trade, Mapping) and trade.get("z") is not None:
                last_price, trade_time = trade.get("z"), trade.get("t")
        query_time = payload.get("queryTime")
        if not isinstance(query_time, Mapping):
            raise EtfMonitorDataError("official TWSE MIS has no source query clock", reason_code="MIS_SOURCE_TIME_INVALID")
        try:
            source_timestamp = datetime.strptime(
                f"{query_time['sysDate']} {query_time['sysTime']}",
                "%Y%m%d %H:%M:%S",
            ).replace(tzinfo=TAIPEI)
        except (KeyError, TypeError, ValueError) as exc:
            raise EtfMonitorDataError("official TWSE MIS source clock is invalid", reason_code="MIS_SOURCE_TIME_INVALID") from exc
        source_age = (local_observed_at - source_timestamp).total_seconds()
        if source_age < -30 or source_age > self.max_source_age_seconds:
            raise EtfMonitorDataError("official TWSE MIS response is stale", reason_code="MIS_STALE_SOURCE")
        if source_timestamp.date() != trade_date or trade_date != local_observed_at.date():
            raise EtfMonitorDataError("official TWSE MIS clocks disagree on the trading date", reason_code="MIS_DATE_MISMATCH")
        try:
            last_trade_at = datetime.strptime(
                f"{raw_date} {trade_time}", "%Y%m%d %H:%M:%S"
            ).replace(tzinfo=TAIPEI)
        except (TypeError, ValueError) as exc:
            raise EtfMonitorDataError("official TWSE MIS trade clock is invalid", reason_code="MIS_TRADE_TIME_INVALID") from exc
        if not time(9) <= last_trade_at.time() <= time(13, 35) or last_trade_at > source_timestamp:
            raise EtfMonitorDataError("official TWSE MIS trade clock is outside the actual session/source time", reason_code="MIS_TRADE_TIME_INVALID")
        regular_close_confirmed = (
            source_timestamp.time() >= time(13, 35)
            and time(13, 30) <= last_trade_at.time() <= time(13, 35)
        )
        if not regular_close_confirmed and (source_timestamp - last_trade_at).total_seconds() > self.max_source_age_seconds:
            raise EtfMonitorDataError("official TWSE MIS last actual trade is stale", reason_code="MIS_STALE_TRADE")
        raw_open = _price(row.get("o"), "open")
        raw_high = _price(row.get("h"), "high")
        raw_low = _price(row.get("l"), "low")
        raw_last = _price(last_price, "last")
        raw_volume = _price(row.get("v"), "volume")
        if not raw_low <= min(raw_open, raw_last) <= max(raw_open, raw_last) <= raw_high:
            raise EtfMonitorDataError("official TWSE MIS OHLC values are inconsistent", reason_code="MIS_OHLC_INCONSISTENT")
        return IntradayQuote(
            symbol="0050",
            observed_at=local_observed_at,
            trade_date=trade_date,
            raw_open=raw_open,
            raw_high=raw_high,
            raw_low=raw_low,
            raw_last=raw_last,
            raw_volume=raw_volume,
            source="TWSE_MIS_OFFICIAL",
            source_endpoint=TWSE_MIS_URL,
            receipt_sha256=receipt_sha256,
            is_market_closed=regular_close_confirmed,
            source_timestamp=source_timestamp,
            last_trade_at=last_trade_at,
            market_status=(
                "REGULAR_SESSION_CLOSED_CONFIRMED"
                if regular_close_confirmed
                else "REGULAR_SESSION_OPEN_OR_UNCONFIRMED"
            ),
        )

    async def fetch(self, *, now: datetime | None = None) -> IntradayQuote:
        observed_at = (now or datetime.now(tz=TAIPEI)).astimezone(TAIPEI)
        return await asyncio.to_thread(self._fetch, observed_at)


class MonitorMarketDataProvider:
    def __init__(
        self,
        registry: RegistrySnapshotProvider,
        quote_provider: OfficialTwseQuoteProvider,
        delta_updater: OfficialDailyDeltaUpdater | None = None,
    ):
        self.registry = registry
        self.quote_provider = quote_provider
        self.delta_updater = delta_updater

    async def load(self, phase: MonitorPhase, as_of: date) -> MarketDataSnapshot:
        if phase is MonitorPhase.RECONCILIATION:
            if self.delta_updater is not None:
                await asyncio.to_thread(self.delta_updater.ensure_through, as_of)
            return await asyncio.to_thread(self.registry.load_complete, as_of)
        if self.delta_updater is not None:
            await asyncio.to_thread(
                self.delta_updater.ensure_through,
                as_of - timedelta(days=1),
            )
        quote = await self.quote_provider.fetch()
        if phase is MonitorPhase.DECISION and not quote.is_market_closed:
            raise EtfMonitorDataError("13:55 decision requires a closed official TWSE quote")
        return await asyncio.to_thread(self.registry.load_provisional, as_of, quote)
