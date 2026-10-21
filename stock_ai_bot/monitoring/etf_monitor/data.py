from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests

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
from .errors import EtfMonitorDataError
from .models import IntradayQuote, MarketDataSnapshot, MonitorPhase, stable_sha256


TAIPEI = ZoneInfo("Asia/Taipei")
TWSE_MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
RAW_EXECUTION_BASIS = "UNADJUSTED_MAINBOARD_OHLCV"


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
    """Loads only hash-verified snapshots from the shared ETF registry."""

    def __init__(self, config: EtfMonitorConfig, *, market_data_root: Path | None = None):
        self.config = config
        self.market_data_root = market_data_root

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
        validation["dividends_artifact_validated"] = True
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
        snapshot, rows, events, validation = self._base(as_of)
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


def _price(value: Any, field: str) -> Decimal:
    text = str(value or "").replace(",", "").strip()
    if text in {"", "-", "--"}:
        raise EtfMonitorDataError(f"TWSE MIS quote has no {field}")
    result = Decimal(text)
    if result <= 0:
        raise EtfMonitorDataError(f"TWSE MIS quote has invalid {field}")
    return result


class OfficialTwseQuoteProvider:
    def __init__(self, *, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds

    def _fetch(self, observed_at: datetime) -> IntradayQuote:
        try:
            response = requests.get(
                TWSE_MIS_URL,
                params={"ex_ch": "tse_0050.tw", "json": "1", "delay": "0"},
                headers={"Referer": "https://mis.twse.com.tw/stock/fibest.jsp"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise EtfMonitorDataError("official TWSE MIS quote request failed") from exc
        rows = payload.get("msgArray") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise EtfMonitorDataError("official TWSE MIS returned an unexpected quote set")
        row = rows[0]
        if str(row.get("c") or "") != "0050":
            raise EtfMonitorDataError("official TWSE MIS returned the wrong symbol")
        raw_date = str(row.get("d") or "")
        try:
            trade_date = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError as exc:
            raise EtfMonitorDataError("official TWSE MIS returned an invalid trade date") from exc
        local_observed_at = observed_at.astimezone(TAIPEI)
        raw = response.content
        return IntradayQuote(
            symbol="0050",
            observed_at=local_observed_at,
            trade_date=trade_date,
            raw_open=_price(row.get("o"), "open"),
            raw_high=_price(row.get("h"), "high"),
            raw_low=_price(row.get("l"), "low"),
            raw_last=_price(row.get("z"), "last"),
            raw_volume=_price(row.get("v"), "volume"),
            source="TWSE_MIS_OFFICIAL",
            source_endpoint=TWSE_MIS_URL,
            receipt_sha256=hashlib.sha256(raw).hexdigest(),
            is_market_closed=(
                trade_date == local_observed_at.date()
                and local_observed_at.time() >= time(13, 30)
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
    ):
        self.registry = registry
        self.quote_provider = quote_provider

    async def load(self, phase: MonitorPhase, as_of: date) -> MarketDataSnapshot:
        if phase is MonitorPhase.RECONCILIATION:
            return await asyncio.to_thread(self.registry.load_complete, as_of)
        quote = await self.quote_provider.fetch()
        if phase is MonitorPhase.DECISION and not quote.is_market_closed:
            raise EtfMonitorDataError("13:55 decision requires a closed official TWSE quote")
        return await asyncio.to_thread(self.registry.load_provisional, as_of, quote)
