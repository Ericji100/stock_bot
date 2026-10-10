from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit


CONTRACT_VERSION = "stock-causal-price-contract-v2"
RAW_SNAPSHOT_SCHEMA = "stock-causal-raw-snapshot-v1"
ACTION_LEDGER_SCHEMA = "stock-corporate-action-ledger-v2"
MARKET_METADATA_RECONCILIATION_SCHEMA = (
    "stock-provider-market-metadata-reconciliation-v2"
)
MARKET_METADATA_CONFLICT_REASON = "PROVIDER_MARKET_METADATA_CONFLICT"
OFFICIAL_SUBMARKET_ROUTE_REASON = "OFFICIAL_SUBMARKET_ROUTE"
TRANSFER_ROUTE_STATUSES = frozenset(
    {"TRANSFERRED_TO_TWSE", "TRANSFERRED_TO_TPEX"}
)
OFFICIAL_EXCHANGE_HOSTS = {
    "TWSE": "www.twse.com.tw",
    "TPEX": "www.tpex.org.tw",
}
PROVIDER_REQUEST_FIELDS = ("open", "high", "low", "close", "volume")


class CausalPriceContractError(ValueError):
    """Base exception for a causal-price contract violation."""


class SchemaVersionError(CausalPriceContractError):
    """Raised when a producer or consumer uses an unsupported schema."""


class DataQualityError(CausalPriceContractError):
    """Raised when source data cannot safely enter selection or monitoring."""


class CorporateActionUnresolvedError(DataQualityError):
    """Raised when the effective-dated action ledger is absent or incomplete."""


class AvailabilityStatus(str, Enum):
    TRADABLE = "TRADABLE"
    SUSPENDED = "SUSPENDED"
    NO_TRANSACTION = "NO_TRANSACTION"
    PRE_LISTING = "PRE_LISTING"
    POST_DELISTING = "POST_DELISTING"
    VENDOR_MISSING = "VENDOR_MISSING"
    EXCHANGE_CLOSED = "EXCHANGE_CLOSED"


class ActionKind(str, Enum):
    CASH = "CASH"
    SHARE = "SHARE"
    RIGHTS = "RIGHTS"
    COMBINED = "COMBINED"
    OTHER = "OTHER"


class CorporateActionType(str, Enum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    CASH_AND_STOCK_DIVIDEND = "CASH_AND_STOCK_DIVIDEND"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    CAPITAL_REDUCTION = "CAPITAL_REDUCTION"
    PAR_VALUE_CHANGE = "PAR_VALUE_CHANGE"
    CASH_CAPITAL_INCREASE_RIGHTS = "CASH_CAPITAL_INCREASE_RIGHTS"
    OTHER = "OTHER"


class PriceBasis(str, Enum):
    RAW_EXECUTION = "UNADJUSTED_MAINBOARD_OHLCV"
    TECHNICAL_CONTINUITY = "CAUSAL_TECHNICAL_CONTINUITY_AS_OF"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def canonical_bytes(value: Any) -> bytes:
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def require_schema(actual: str, expected: str) -> None:
    if actual != expected:
        raise SchemaVersionError(f"unsupported schema {actual!r}; expected {expected!r}")


def _require_sha256(value: str, field: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise DataQualityError(f"{field} is not a SHA-256 hex digest")


def _finite_positive(value: float, field: str) -> None:
    if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
        raise DataQualityError(f"{field} must be finite and positive")


@dataclass(frozen=True)
class InstrumentIdentity:
    instrument_id: str
    code: str
    symbol: str
    exchange: str
    market: str
    instrument_type: str
    mainboard_start: date
    mainboard_end: date | None
    route_status: str
    identity_source: str
    identity_source_sha256: str
    observed_at: datetime

    def validate(self) -> None:
        for field in (
            "instrument_id",
            "code",
            "symbol",
            "exchange",
            "market",
            "instrument_type",
            "route_status",
            "identity_source",
        ):
            if not str(getattr(self, field)).strip():
                raise DataQualityError(f"identity field {field} is empty")
        if self.mainboard_end is not None and self.mainboard_end < self.mainboard_start:
            raise DataQualityError("mainboard_end precedes mainboard_start")
        _require_sha256(self.identity_source_sha256, "identity_source_sha256")


class OfficialSubmarketEvidence(Protocol):
    reason_code: str

    def validate_bar(
        self, bar: "RawDailyBar", identity: InstrumentIdentity
    ) -> None: ...


@dataclass(frozen=True)
class OfficialOHLCVComparison:
    trade_date: date
    provider_open: float
    provider_high: float
    provider_low: float
    provider_close: float
    provider_volume: float
    official_open: float
    official_high: float
    official_low: float
    official_close: float
    official_volume: float
    official_volume_unit: str
    official_raw_sha256: str
    official_row_sha256: str

    def content_payload(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            **{
                f"{prefix}_{field}": float(getattr(self, f"{prefix}_{field}"))
                for prefix in ("provider", "official")
                for field in ("open", "high", "low", "close", "volume")
            },
            "official_volume_unit": self.official_volume_unit,
            "official_raw_sha256": self.official_raw_sha256,
            "official_row_sha256": self.official_row_sha256,
        }

    def official_row_payload(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "open": float(self.official_open),
            "high": float(self.official_high),
            "low": float(self.official_low),
            "close": float(self.official_close),
            "volume": float(self.official_volume),
            "volume_unit": self.official_volume_unit,
            "official_raw_sha256": self.official_raw_sha256,
        }

    def validate(self) -> None:
        if self.official_volume_unit != "SHARES":
            raise DataQualityError("official OHLCV volume is not normalized to shares")
        _require_sha256(self.official_raw_sha256, "official_raw_sha256")
        _require_sha256(self.official_row_sha256, "official_row_sha256")
        if self.official_row_sha256 != canonical_sha256(self.official_row_payload()):
            raise DataQualityError("official OHLCV row hash disagrees with canonical row")
        for prefix in ("provider", "official"):
            values = {
                field: float(getattr(self, f"{prefix}_{field}"))
                for field in ("open", "high", "low", "close", "volume")
            }
            for field in ("open", "high", "low", "close"):
                _finite_positive(values[field], f"{prefix}_{field}")
            if not math.isfinite(values["volume"]) or values["volume"] < 0:
                raise DataQualityError(f"{prefix}_volume must be finite and non-negative")
            if values["volume"] != int(values["volume"]):
                raise DataQualityError(f"{prefix}_volume must be exact whole shares")
            if values["high"] < max(
                values["open"], values["low"], values["close"]
            ):
                raise DataQualityError(f"{prefix}_high does not envelope OHLC")
            if values["low"] > min(
                values["open"], values["high"], values["close"]
            ):
                raise DataQualityError(f"{prefix}_low does not envelope OHLC")
        for field in ("open", "high", "low", "close", "volume"):
            if Decimal(str(getattr(self, f"provider_{field}"))) != Decimal(
                str(getattr(self, f"official_{field}"))
            ):
                raise DataQualityError(
                    f"provider and official {field} do not match exactly"
                )


@dataclass(frozen=True)
class ProviderMarketMetadataReconciliation:
    schema_version: str
    status: str
    reason_code: str
    instrument_id: str
    symbol: str
    official_exchange: str
    provider_exchange: str
    provider_market: str
    route_start: date
    route_end_exclusive: date
    route_status: str
    successor_instrument_id: str | None
    successor_symbol: str | None
    successor_exchange: str | None
    successor_route_start: date | None
    route_transition_source_sha256: str
    query_from: date
    query_to: date
    provider_name: str
    provider_endpoint: str
    provider_request_identity_sha256: str
    provider_raw_sha256: str
    provider_row_count: int
    official_endpoint: str
    official_artifact_manifest_sha256: str
    official_row_count: int
    official_trade_dates_sha256: str
    comparisons: tuple[OfficialOHLCVComparison, ...]

    def provider_request_identity_payload(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "provider_endpoint": self.provider_endpoint,
            "symbol": self.symbol,
            "from": self.query_from,
            "to": self.query_to,
            "timeframe": "D",
            "adjusted": False,
            "fields": list(PROVIDER_REQUEST_FIELDS),
            "sort": "asc",
        }

    def official_trade_dates_payload(self) -> list[str]:
        return [item.trade_date.isoformat() for item in self.comparisons]

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "reason_code": self.reason_code,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "official_exchange": self.official_exchange,
            "provider_exchange": self.provider_exchange,
            "provider_market": self.provider_market,
            "route_start": self.route_start,
            "route_end_exclusive": self.route_end_exclusive,
            "route_status": self.route_status,
            "successor_instrument_id": self.successor_instrument_id,
            "successor_symbol": self.successor_symbol,
            "successor_exchange": self.successor_exchange,
            "successor_route_start": self.successor_route_start,
            "route_transition_source_sha256": self.route_transition_source_sha256,
            "query_from": self.query_from,
            "query_to": self.query_to,
            "provider_name": self.provider_name,
            "provider_endpoint": self.provider_endpoint,
            "provider_request_identity_sha256": self.provider_request_identity_sha256,
            "provider_raw_sha256": self.provider_raw_sha256,
            "provider_row_count": self.provider_row_count,
            "official_endpoint": self.official_endpoint,
            "official_artifact_manifest_sha256": self.official_artifact_manifest_sha256,
            "official_row_count": self.official_row_count,
            "official_trade_dates_sha256": self.official_trade_dates_sha256,
            "comparison_policy": "DECIMAL_EXACT_AFTER_VOLUME_NORMALIZATION_TO_SHARES",
            "comparisons": [item.content_payload() for item in self.comparisons],
        }

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.content_payload())

    @property
    def evidence_id(self) -> str:
        return f"provider-market-reconciliation#{self.content_sha256[:16]}"

    def validate(self) -> None:
        require_schema(self.schema_version, MARKET_METADATA_RECONCILIATION_SCHEMA)
        if self.status != "PASS":
            raise DataQualityError("provider market reconciliation is not PASS")
        if self.reason_code != MARKET_METADATA_CONFLICT_REASON:
            raise DataQualityError("provider market reconciliation reason is unsupported")
        if not self.instrument_id or not self.symbol:
            raise DataQualityError("provider market reconciliation identity is empty")
        if self.route_end_exclusive <= self.route_start:
            raise DataQualityError("provider market reconciliation is not a finite route")
        if self.route_status not in TRANSFER_ROUTE_STATUSES:
            raise DataQualityError(
                "provider market metadata exception is not an official transfer route"
            )
        target_exchange = self.route_status.removeprefix("TRANSFERRED_TO_")
        successor_values = (
            self.successor_instrument_id,
            self.successor_symbol,
            self.successor_exchange,
            self.successor_route_start,
        )
        if any(value is not None for value in successor_values):
            if any(value is None for value in successor_values):
                raise DataQualityError("successor route evidence is incomplete")
            if (
                str(self.successor_exchange).upper() != target_exchange
                or str(self.successor_exchange).upper()
                == self.official_exchange.upper()
                or self.successor_route_start != self.route_end_exclusive
                or str(self.successor_instrument_id)
                != f"{str(self.successor_exchange).upper()}:{self.symbol.split('.', 1)[0]}"
                or str(self.successor_symbol).split(".", 1)[0]
                != self.symbol.split(".", 1)[0]
            ):
                raise DataQualityError("successor route evidence is not a paired transfer")
        _require_sha256(
            self.route_transition_source_sha256,
            "route_transition_source_sha256",
        )
        if not (
            self.route_start <= self.query_from <= self.query_to
            and self.query_to < self.route_end_exclusive
        ):
            raise DataQualityError("provider query is outside the official route segment")
        expected_market = {"TWSE": "TSE", "TPEX": "OTC"}.get(
            self.official_exchange.upper()
        )
        if expected_market is None:
            raise DataQualityError("provider reconciliation official exchange is unsupported")
        if self.provider_exchange.casefold() != self.official_exchange.casefold():
            raise DataQualityError("provider exchange conflicts with the official route")
        if self.provider_market.upper() in {
            expected_market,
            self.official_exchange.upper(),
        }:
            raise DataQualityError("provider market reconciliation has no metadata conflict")
        _require_sha256(self.provider_raw_sha256, "provider_raw_sha256")
        if not self.provider_name.strip() or not self.provider_endpoint.startswith(
            "https://"
        ):
            raise DataQualityError("provider request source identity is invalid")
        _require_sha256(
            self.provider_request_identity_sha256,
            "provider_request_identity_sha256",
        )
        if self.provider_request_identity_sha256 != canonical_sha256(
            self.provider_request_identity_payload()
        ):
            raise DataQualityError("provider request identity hash mismatch")
        _require_sha256(
            self.official_artifact_manifest_sha256,
            "official_artifact_manifest_sha256",
        )
        parsed_official = urlsplit(self.official_endpoint)
        expected_host = OFFICIAL_EXCHANGE_HOSTS[self.official_exchange.upper()]
        if (
            parsed_official.scheme != "https"
            or parsed_official.hostname != expected_host
            or parsed_official.username is not None
            or parsed_official.password is not None
        ):
            raise DataQualityError(
                "official reconciliation endpoint is outside the exchange allowlist"
            )
        if self.provider_row_count <= 0 or self.provider_row_count != len(
            self.comparisons
        ):
            raise DataQualityError("provider reconciliation row coverage is incomplete")
        if self.official_row_count != len(self.comparisons):
            raise DataQualityError("official reconciliation row coverage is incomplete")
        dates = [item.trade_date for item in self.comparisons]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise DataQualityError("provider reconciliation dates are not unique ascending")
        if any(not self.query_from <= value <= self.query_to for value in dates):
            raise DataQualityError("provider reconciliation date is outside the query")
        _require_sha256(
            self.official_trade_dates_sha256,
            "official_trade_dates_sha256",
        )
        if self.official_trade_dates_sha256 != canonical_sha256(
            self.official_trade_dates_payload()
        ):
            raise DataQualityError("official trade-date coverage hash mismatch")
        for comparison in self.comparisons:
            comparison.validate()

    def validate_identity(self, identity: InstrumentIdentity) -> None:
        self.validate()
        identity.validate()
        identity_is_transfer = identity.route_status in TRANSFER_ROUTE_STATUSES
        has_paired_successor = all(
            value is not None
            for value in (
                self.successor_instrument_id,
                self.successor_symbol,
                self.successor_exchange,
                self.successor_route_start,
            )
        )
        generic_historical_route = identity.route_status == (
            "HISTORICAL_MAINBOARD_INTERVAL"
        )
        if identity.mainboard_end is None or not (
            identity_is_transfer
            or (generic_historical_route and has_paired_successor)
        ):
            raise DataQualityError(
                "provider market metadata exception is limited to historical transfer routes"
            )
        if identity_is_transfer and identity.route_status != self.route_status:
            raise DataQualityError("provider transfer status conflicts with official identity")
        if (
            self.instrument_id != identity.instrument_id
            or self.symbol != identity.symbol
            or self.official_exchange.casefold() != identity.exchange.casefold()
            or self.route_start != identity.mainboard_start
            or self.route_end_exclusive != identity.mainboard_end
        ):
            raise DataQualityError(
                "provider market reconciliation conflicts with official identity"
            )

    def validate_provider_rows(self, rows: Iterable[dict[str, Any]]) -> None:
        self.validate()
        observed = list(rows)
        if len(observed) != self.provider_row_count:
            raise DataQualityError("provider row count differs from official reconciliation")
        by_date = {item.trade_date: item for item in self.comparisons}
        if len(by_date) != len(self.comparisons):
            raise DataQualityError("provider reconciliation contains duplicate dates")
        for row in observed:
            try:
                observed_on = date.fromisoformat(str(row["date"]))
                comparison = by_date.pop(observed_on)
            except (KeyError, ValueError) as exc:
                raise DataQualityError(
                    "provider dates differ from official reconciliation"
                ) from exc
            for field in ("open", "high", "low", "close", "volume"):
                if Decimal(str(row[field])) != Decimal(
                    str(getattr(comparison, f"provider_{field}"))
                ):
                    raise DataQualityError(
                        f"provider {field} differs from frozen reconciliation"
                    )
        if by_date:
            raise DataQualityError("official reconciliation contains unmatched dates")

    def validate_provider_request(
        self,
        *,
        provider_name: str,
        provider_endpoint: str,
        symbol: str,
        query_from: date,
        query_to: date,
        raw_response_sha256: str,
    ) -> None:
        self.validate()
        if (
            provider_name != self.provider_name
            or provider_endpoint != self.provider_endpoint
            or symbol != self.symbol
            or query_from != self.query_from
            or query_to != self.query_to
            or raw_response_sha256 != self.provider_raw_sha256
        ):
            raise DataQualityError(
                "provider request identity differs from frozen reconciliation"
            )

    def validate_bar(self, bar: "RawDailyBar", identity: InstrumentIdentity) -> None:
        self.validate_identity(identity)
        self.validate_provider_request(
            provider_name=bar.source_name,
            provider_endpoint=bar.source_endpoint,
            symbol=bar.symbol,
            query_from=bar.query_from,
            query_to=bar.query_to,
            raw_response_sha256=bar.raw_response_sha256,
        )
        matches = [item for item in self.comparisons if item.trade_date == bar.trade_date]
        if len(matches) != 1:
            raise DataQualityError("raw bar date is absent from reconciliation")
        comparison = matches[0]
        for field in ("open", "high", "low", "close", "volume"):
            if Decimal(str(getattr(bar, f"raw_{field}"))) != Decimal(
                str(getattr(comparison, f"provider_{field}"))
            ):
                raise DataQualityError(
                    f"raw bar {field} differs from reconciliation"
                )


@dataclass(frozen=True)
class RawDailyBar:
    instrument_id: str
    symbol: str
    exchange: str
    trade_date: date
    raw_open: float
    raw_high: float
    raw_low: float
    raw_close: float
    raw_volume: float
    source_name: str
    source_endpoint: str
    source_market: str | None
    source_segment_id: str
    fetched_at: datetime
    query_from: date
    query_to: date
    raw_response_sha256: str
    row_sha256: str
    snapshot_id: str
    snapshot_sha256: str
    mainboard_start: date
    adjusted: bool = False
    execution_price_basis: str = PriceBasis.RAW_EXECUTION.value

    def row_lineage_payload(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "trade_date": self.trade_date,
            "raw_open": self.raw_open,
            "raw_high": self.raw_high,
            "raw_low": self.raw_low,
            "raw_close": self.raw_close,
            "raw_volume": self.raw_volume,
            "source_name": self.source_name,
            "source_endpoint": self.source_endpoint,
            "source_market": self.source_market,
            "source_segment_id": self.source_segment_id,
            "raw_response_sha256": self.raw_response_sha256,
            "snapshot_id": self.snapshot_id,
            "mainboard_start": self.mainboard_start,
            "adjusted": self.adjusted,
            "execution_price_basis": self.execution_price_basis,
        }

    def validate(
        self,
        identity: InstrumentIdentity | None = None,
        market_conflict_evidence: ProviderMarketMetadataReconciliation | None = None,
        official_submarket_evidence: OfficialSubmarketEvidence | None = None,
        *,
        source_lineage_prevalidated: bool = False,
    ) -> tuple[str, ...]:
        for field in (
            "instrument_id",
            "symbol",
            "exchange",
            "source_name",
            "source_endpoint",
            "source_segment_id",
            "snapshot_id",
        ):
            if not str(getattr(self, field)).strip():
                raise DataQualityError(f"raw bar field {field} is empty")
        if self.adjusted is not False:
            raise DataQualityError("raw daily bar is adjusted")
        if self.execution_price_basis != PriceBasis.RAW_EXECUTION.value:
            raise DataQualityError("raw daily bar has the wrong execution basis")
        for field in ("raw_open", "raw_high", "raw_low", "raw_close"):
            _finite_positive(float(getattr(self, field)), field)
        if not math.isfinite(float(self.raw_volume)) or float(self.raw_volume) < 0:
            raise DataQualityError("raw_volume must be finite and non-negative")
        if self.raw_high < max(self.raw_open, self.raw_low, self.raw_close):
            raise DataQualityError("raw_high does not envelope OHLC")
        if self.raw_low > min(self.raw_open, self.raw_high, self.raw_close):
            raise DataQualityError("raw_low does not envelope OHLC")
        if not self.query_from <= self.trade_date <= self.query_to:
            raise DataQualityError("trade_date is outside the source query interval")
        if self.query_from > self.query_to:
            raise DataQualityError("query_from is after query_to")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise DataQualityError("fetched_at must be timezone-aware")
        if self.trade_date < self.mainboard_start:
            raise DataQualityError("pre-mainboard OHLC is prohibited")
        for field in ("raw_response_sha256", "row_sha256", "snapshot_sha256"):
            _require_sha256(str(getattr(self, field)), field)
        if self.row_sha256 != canonical_sha256(self.row_lineage_payload()):
            raise DataQualityError("row_sha256 disagrees with canonical row lineage")
        if "#" in self.snapshot_id:
            suffix = self.snapshot_id.rsplit("#", 1)[1]
            if suffix and not self.snapshot_sha256.startswith(suffix):
                raise DataQualityError("snapshot_id suffix disagrees with snapshot_sha256")
        warnings: list[str] = []
        standard_market = {"TWSE": "TSE", "TPEX": "OTC"}.get(self.exchange.upper())
        if standard_market is None:
            raise DataQualityError(f"unsupported official exchange {self.exchange!r}")
        expected_market = (
            identity.market.upper()
            if identity is not None and identity.exchange.upper() == self.exchange.upper()
            else standard_market
        )
        is_official_submarket = expected_market != standard_market
        accepted_source_markets = (
            {expected_market}
            if is_official_submarket
            else {expected_market, self.exchange.upper()}
        )
        if source_lineage_prevalidated and (
            identity is not None
            or market_conflict_evidence is not None
            or official_submarket_evidence is not None
        ):
            raise DataQualityError(
                "prevalidated source lineage cannot be combined with route evidence"
            )
        if (
            not source_lineage_prevalidated
            and market_conflict_evidence is not None
            and official_submarket_evidence is not None
        ):
            raise DataQualityError("multiple market-route evidence objects were supplied")
        if source_lineage_prevalidated:
            pass
        elif self.source_market is None:
            if is_official_submarket:
                raise DataQualityError(
                    "official submarket metadata is absent from the provider response"
                )
            if (
                market_conflict_evidence is not None
                or official_submarket_evidence is not None
            ):
                raise DataQualityError(
                    "market conflict evidence was supplied without an observed conflict"
                )
            warnings.append("SOURCE_METADATA_MISSING:source_market")
        elif self.source_market.upper() not in accepted_source_markets:
            if identity is None or (
                market_conflict_evidence is None
                and official_submarket_evidence is None
            ):
                raise DataQualityError(
                    "source_market conflicts with the official frozen route"
                )
            if market_conflict_evidence is not None:
                market_conflict_evidence.validate_bar(self, identity)
                warnings.append(MARKET_METADATA_CONFLICT_REASON)
            else:
                raise DataQualityError(
                    "official submarket evidence cannot reconcile a different market"
                )
        elif official_submarket_evidence is not None:
            if not is_official_submarket or identity is None:
                raise DataQualityError(
                    "official submarket evidence was supplied without a submarket route"
                )
            official_submarket_evidence.validate_bar(self, identity)
            if official_submarket_evidence.reason_code != OFFICIAL_SUBMARKET_ROUTE_REASON:
                raise DataQualityError(
                    "official submarket evidence has an unsupported reason"
                )
        elif is_official_submarket:
            raise DataQualityError("official submarket route evidence is required")
        elif market_conflict_evidence is not None:
            raise DataQualityError(
                "market conflict evidence was supplied without an observed conflict"
            )
        if identity is not None:
            identity.validate()
            if (
                self.instrument_id != identity.instrument_id
                or self.symbol != identity.symbol
                or self.exchange.upper() != identity.exchange.upper()
                or self.mainboard_start != identity.mainboard_start
            ):
                raise DataQualityError("raw bar identity conflicts with official frozen route")
            if identity.mainboard_end is not None and self.trade_date >= identity.mainboard_end:
                raise DataQualityError("post-mainboard-end OHLC is prohibited")
        return tuple(warnings)


@dataclass(frozen=True)
class CorporateActionEvent:
    action_id: str
    instrument_id: str
    action_type: CorporateActionType
    action_kind: ActionKind
    effective_date: date
    ex_date: date | None
    available_on: date
    announcement_date: date | None
    entitlement_date: date | None
    payment_date: date | None
    position_lifecycle_status: str
    position_lifecycle_reason: str | None
    rights_policy: str
    halt_date: date | None
    resume_date: date | None
    cash_dividend: float | None
    stock_dividend_shares: float | None
    split_ratio: float | None
    cash_price_factor: float | None
    share_price_factor: float | None
    share_volume_factor: float | None
    rights_price_factor: float | None
    previous_close: float | None
    reference_price: float | None
    technical_resolution_status: str
    technical_resolution_reason: str | None
    source_name: str
    source_endpoint: str
    fetched_at: datetime
    raw_response_sha256: str
    event_sha256: str

    def event_lineage_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("event_sha256")
        return payload

    @property
    def event_date(self) -> date:
        return self.ex_date or self.effective_date

    def validate(self) -> None:
        if not isinstance(self.action_type, CorporateActionType):
            raise DataQualityError("action_type is not an approved CorporateActionType")
        approved_kinds = {
            CorporateActionType.CASH_DIVIDEND: {ActionKind.CASH},
            CorporateActionType.STOCK_DIVIDEND: {ActionKind.SHARE},
            CorporateActionType.CASH_AND_STOCK_DIVIDEND: {ActionKind.COMBINED},
            CorporateActionType.SPLIT: {ActionKind.SHARE},
            CorporateActionType.REVERSE_SPLIT: {ActionKind.SHARE},
            CorporateActionType.CAPITAL_REDUCTION: {ActionKind.SHARE, ActionKind.COMBINED},
            CorporateActionType.PAR_VALUE_CHANGE: {ActionKind.SHARE},
            CorporateActionType.CASH_CAPITAL_INCREASE_RIGHTS: {
                ActionKind.RIGHTS,
                ActionKind.COMBINED,
            },
            CorporateActionType.OTHER: set(),
        }
        if self.action_kind not in approved_kinds[self.action_type]:
            raise CorporateActionUnresolvedError(
                f"corporate action {self.action_id} kind/type mapping is not approved"
            )
        if self.action_type is CorporateActionType.OTHER:
            raise CorporateActionUnresolvedError(
                f"corporate action {self.action_id} has no approved technical transform"
            )
        if self.rights_policy not in {
            "NONE",
            "SUPPORTED",
            "QUARANTINED_UNSUPPORTED_POSITION",
        }:
            raise DataQualityError("corporate action rights_policy is not recognized")
        for field in (
            "action_id",
            "instrument_id",
            "position_lifecycle_status",
            "rights_policy",
            "source_name",
            "source_endpoint",
        ):
            if not str(getattr(self, field)).strip():
                raise DataQualityError(f"corporate action field {field} is empty")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise DataQualityError("corporate action fetched_at must be timezone-aware")
        if self.available_on > self.event_date:
            raise DataQualityError("corporate action was not available by its event date")
        for field in ("raw_response_sha256", "event_sha256"):
            _require_sha256(str(getattr(self, field)), field)
        if self.event_sha256 != canonical_sha256(self.event_lineage_payload()):
            raise DataQualityError("event_sha256 disagrees with canonical event lineage")
        if self.technical_resolution_status != "RESOLVED_FOR_TECHNICAL":
            raise CorporateActionUnresolvedError(
                f"corporate action {self.action_id} is UNRESOLVED_FOR_TECHNICAL: "
                f"{self.technical_resolution_reason or 'missing factor'}"
            )
        if self.action_kind is ActionKind.CASH:
            _finite_positive(float(self.cash_price_factor or 0), "cash_price_factor")
        elif self.action_kind is ActionKind.SHARE:
            _finite_positive(float(self.share_price_factor or 0), "share_price_factor")
            _finite_positive(float(self.share_volume_factor or 0), "share_volume_factor")
        elif self.action_kind is ActionKind.RIGHTS:
            _finite_positive(float(self.rights_price_factor or 0), "rights_price_factor")
        elif self.action_kind is ActionKind.COMBINED:
            component_count = sum(
                value is not None
                for value in (
                    self.cash_price_factor,
                    self.share_price_factor,
                    self.rights_price_factor,
                )
            )
            if component_count < 2:
                raise DataQualityError("combined event must contain at least two transforms")
            if self.cash_price_factor is not None:
                _finite_positive(self.cash_price_factor, "cash_price_factor")
            if self.share_price_factor is not None:
                _finite_positive(self.share_price_factor, "share_price_factor")
                _finite_positive(float(self.share_volume_factor or 0), "share_volume_factor")
            if self.rights_price_factor is not None:
                _finite_positive(self.rights_price_factor, "rights_price_factor")
        if self.action_kind is ActionKind.OTHER:
            raise CorporateActionUnresolvedError(
                f"corporate action {self.action_id} has no approved transform"
            )
        if self.previous_close is not None and self.reference_price is not None:
            _finite_positive(self.previous_close, "previous_close")
            _finite_positive(self.reference_price, "reference_price")
            cash = float(self.cash_price_factor or 1.0)
            share = float(self.share_price_factor or 1.0)
            rights = float(self.rights_price_factor or 1.0)
            implied_reference = self.previous_close * cash * share * rights
            tolerance = max(0.02, self.reference_price * 1e-6)
            if abs(implied_reference - self.reference_price) > tolerance:
                raise DataQualityError(
                    "cash/share/rights factor product disagrees with the official reference price"
                )

    def validate_position_lifecycle(self) -> None:
        self.validate()
        if self.position_lifecycle_status not in {"SETTLED", "RECEIVABLE_TRACKED"}:
            raise CorporateActionUnresolvedError(
                f"corporate action {self.action_id} position lifecycle is "
                f"{self.position_lifecycle_status}: "
                f"{self.position_lifecycle_reason or 'unsupported lifecycle'}"
            )


@dataclass(frozen=True)
class CorporateActionLedger:
    schema_version: str
    snapshot_id: str
    coverage_from: date
    coverage_through: date
    status: str
    source_names: tuple[str, ...]
    events: tuple[CorporateActionEvent, ...]
    manifest_sha256: str

    def validate_for(self, *, from_date: date, through_date: date) -> None:
        require_schema(self.schema_version, ACTION_LEDGER_SCHEMA)
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        if self.status != "PASS":
            raise CorporateActionUnresolvedError(
                f"corporate-action ledger status is {self.status}, expected PASS"
            )
        if self.coverage_from > from_date or self.coverage_through < through_date:
            raise CorporateActionUnresolvedError(
                "corporate-action ledger does not cover the requested raw/as-of interval"
            )
        if not self.source_names:
            raise CorporateActionUnresolvedError("corporate-action ledger has no source")
        for event in self.events:
            if from_date <= event.event_date <= through_date:
                event.validate()

    def validate_position_lifecycle_for(
        self, *, from_date: date, through_date: date, instrument_id: str | None = None
    ) -> None:
        self.validate_for(from_date=from_date, through_date=through_date)
        for event in self.events:
            if instrument_id is not None and event.instrument_id != instrument_id:
                continue
            if from_date <= event.event_date <= through_date:
                event.validate_position_lifecycle()


@dataclass(frozen=True)
class CausalTechnicalBar:
    instrument_id: str
    symbol: str
    trade_date: date
    as_of_date: date
    technical_open: float
    technical_high: float
    technical_low: float
    technical_close: float
    technical_volume: float
    cash_price_factor: float
    share_price_factor: float
    share_volume_factor: float
    rights_price_factor: float
    price_factor: float
    volume_factor: float
    factor_event_ids: tuple[str, ...]
    raw_row_sha256: str
    contract_version: str
    lineage_sha256: str
    price_basis: str = PriceBasis.TECHNICAL_CONTINUITY.value

    def validate(self) -> None:
        for field in ("instrument_id", "symbol", "contract_version", "lineage_sha256"):
            if not str(getattr(self, field)).strip():
                raise DataQualityError(f"technical bar field {field} is empty")
        if self.trade_date > self.as_of_date:
            raise DataQualityError("technical bar is later than as_of_date")
        if self.contract_version != CONTRACT_VERSION:
            raise SchemaVersionError("technical bar contract_version is unsupported")
        if self.price_basis != PriceBasis.TECHNICAL_CONTINUITY.value:
            raise DataQualityError("technical bar has the wrong basis")
        for field in (
            "technical_open",
            "technical_high",
            "technical_low",
            "technical_close",
            "cash_price_factor",
            "share_price_factor",
            "share_volume_factor",
            "rights_price_factor",
            "price_factor",
            "volume_factor",
        ):
            _finite_positive(float(getattr(self, field)), field)
        if not math.isfinite(float(self.technical_volume)) or self.technical_volume < 0:
            raise DataQualityError("technical_volume must be finite and non-negative")
        if self.technical_high < max(
            self.technical_open, self.technical_low, self.technical_close
        ):
            raise DataQualityError("technical_high does not envelope OHLC")
        if self.technical_low > min(
            self.technical_open, self.technical_high, self.technical_close
        ):
            raise DataQualityError("technical_low does not envelope OHLC")
        if not math.isclose(
            self.price_factor,
            self.cash_price_factor * self.share_price_factor * self.rights_price_factor,
            rel_tol=1e-12,
        ):
            raise DataQualityError("price_factor does not equal cash x share x rights factors")
        if not math.isclose(self.volume_factor, self.share_volume_factor, rel_tol=1e-12):
            raise DataQualityError("technical volume was affected by a non-share factor")
        _require_sha256(self.raw_row_sha256, "raw_row_sha256")
        _require_sha256(self.lineage_sha256, "lineage_sha256")


@dataclass(frozen=True)
class DailyAvailability:
    instrument_id: str
    trade_date: date
    status: AvailabilityStatus
    exchange_calendar_open: bool
    evidence_sources: tuple[str, ...]
    evidence_sha256: str
    reason: str

    def validate(self) -> None:
        _require_sha256(self.evidence_sha256, "evidence_sha256")
        if self.status is AvailabilityStatus.VENDOR_MISSING:
            raise DataQualityError(
                f"unresolved vendor-missing bar for {self.instrument_id} on {self.trade_date}"
            )
        if self.status is AvailabilityStatus.TRADABLE and not self.exchange_calendar_open:
            raise DataQualityError("TRADABLE is incompatible with a closed exchange calendar")
        if self.status in {
            AvailabilityStatus.SUSPENDED,
            AvailabilityStatus.NO_TRANSACTION,
        } and len(self.evidence_sources) < 2:
            raise DataQualityError(
                f"{self.status.value} requires exchange calendar plus official evidence"
            )


@dataclass(frozen=True)
class StoredPriceCoordinate:
    value: float
    basis: PriceBasis
    anchor_trade_date: date
    as_of_date: date
    price_factor_at_storage: float
    lineage_sha256: str

    def validate(self) -> None:
        if self.basis is not PriceBasis.TECHNICAL_CONTINUITY:
            raise DataQualityError(
                "structural prices must use TECHNICAL_CONTINUITY; fills/costs belong "
                "to the position book"
            )
        _finite_positive(self.value, "stored price")
        _finite_positive(self.price_factor_at_storage, "price_factor_at_storage")
        _require_sha256(self.lineage_sha256, "lineage_sha256")


def validate_unique_ascending_bars(bars: Iterable[RawDailyBar]) -> None:
    grouped: dict[str, list[RawDailyBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.instrument_id, []).append(bar)
    for instrument_id, rows in grouped.items():
        dates = [row.trade_date for row in rows]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise DataQualityError(
                f"raw dates are not unique ascending for {instrument_id}"
            )
