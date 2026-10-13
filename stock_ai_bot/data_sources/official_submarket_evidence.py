from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable
from urllib.parse import urlsplit

from stock_ai_bot.data_sources.causal_price_contract import (
    OFFICIAL_EXCHANGE_HOSTS,
    OFFICIAL_SUBMARKET_ROUTE_REASON,
    PROVIDER_REQUEST_FIELDS,
    DataQualityError,
    InstrumentIdentity,
    OfficialOHLCVComparison,
    RawDailyBar,
    canonical_sha256,
    require_schema,
)


OFFICIAL_SUBMARKET_EVIDENCE_SCHEMA = "stock-official-submarket-evidence-v1"


def _require_sha256(value: str, field: str) -> None:
    if len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value.lower()
    ):
        raise DataQualityError(f"{field} is not a SHA-256 hex digest")


def _require_official_endpoint(endpoint: str, exchange: str, field: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != OFFICIAL_EXCHANGE_HOSTS[exchange]
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DataQualityError(f"{field} is outside the exchange allowlist")


@dataclass(frozen=True)
class OfficialSubmarketRouteEvidence:
    schema_version: str
    status: str
    reason_code: str
    instrument_id: str
    symbol: str
    official_exchange: str
    official_submarket: str
    route_status: str
    route_start: date
    route_end_exclusive: date | None
    route_source_endpoint: str
    route_source_artifact_sha256: str
    query_from: date
    query_to: date
    provider_name: str
    provider_endpoint: str
    provider_exchange: str
    provider_market: str
    provider_request_identity_sha256: str
    provider_raw_sha256: str
    provider_row_count: int
    official_price_endpoint: str
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
            "official_submarket": self.official_submarket,
            "route_status": self.route_status,
            "route_start": self.route_start,
            "route_end_exclusive": self.route_end_exclusive,
            "route_source_endpoint": self.route_source_endpoint,
            "route_source_artifact_sha256": self.route_source_artifact_sha256,
            "query_from": self.query_from,
            "query_to": self.query_to,
            "provider_name": self.provider_name,
            "provider_endpoint": self.provider_endpoint,
            "provider_exchange": self.provider_exchange,
            "provider_market": self.provider_market,
            "provider_request_identity_sha256": self.provider_request_identity_sha256,
            "provider_raw_sha256": self.provider_raw_sha256,
            "provider_row_count": self.provider_row_count,
            "official_price_endpoint": self.official_price_endpoint,
            "official_artifact_manifest_sha256": self.official_artifact_manifest_sha256,
            "official_row_count": self.official_row_count,
            "official_trade_dates_sha256": self.official_trade_dates_sha256,
            "comparison_policy": (
                "DECIMAL_EXACT_AFTER_VOLUME_NORMALIZATION_TO_SHARES"
            ),
            "comparisons": [item.content_payload() for item in self.comparisons],
        }

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.content_payload())

    @property
    def evidence_id(self) -> str:
        return f"official-submarket-evidence#{self.content_sha256[:16]}"

    def validate(self) -> None:
        require_schema(self.schema_version, OFFICIAL_SUBMARKET_EVIDENCE_SCHEMA)
        if self.status != "PASS" or self.reason_code != OFFICIAL_SUBMARKET_ROUTE_REASON:
            raise DataQualityError("official submarket evidence is not PASS")
        if self.official_exchange != "TWSE" or self.official_submarket != "TIB":
            raise DataQualityError("unsupported official submarket route")
        if self.route_status != "TWSE_TIB_FIRST_LISTING":
            raise DataQualityError("unsupported official submarket route status")
        if self.provider_exchange.casefold() != "twse" or self.provider_market != "TIB":
            raise DataQualityError("provider metadata does not match the official submarket")
        if not self.route_start <= self.query_from <= self.query_to:
            raise DataQualityError("provider query is outside the official submarket route")
        if (
            self.route_end_exclusive is not None
            and self.query_to >= self.route_end_exclusive
        ):
            raise DataQualityError("provider query crosses the official submarket boundary")
        _require_official_endpoint(
            self.route_source_endpoint, "TWSE", "route source endpoint"
        )
        _require_official_endpoint(
            self.official_price_endpoint, "TWSE", "official price endpoint"
        )
        for field in (
            "route_source_artifact_sha256",
            "provider_request_identity_sha256",
            "provider_raw_sha256",
            "official_artifact_manifest_sha256",
            "official_trade_dates_sha256",
        ):
            _require_sha256(str(getattr(self, field)), field)
        if self.provider_request_identity_sha256 != canonical_sha256(
            self.provider_request_identity_payload()
        ):
            raise DataQualityError("provider request identity hash mismatch")
        if self.provider_row_count <= 0 or self.provider_row_count != len(
            self.comparisons
        ):
            raise DataQualityError("provider submarket row coverage is incomplete")
        if self.official_row_count != len(self.comparisons):
            raise DataQualityError("official submarket row coverage is incomplete")
        dates = [item.trade_date for item in self.comparisons]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise DataQualityError("official submarket dates are not unique ascending")
        if any(not self.query_from <= item <= self.query_to for item in dates):
            raise DataQualityError("official submarket date is outside the query")
        if self.official_trade_dates_sha256 != canonical_sha256(
            self.official_trade_dates_payload()
        ):
            raise DataQualityError("official submarket trade-date coverage hash mismatch")
        for comparison in self.comparisons:
            comparison.validate()

    def validate_identity(self, identity: InstrumentIdentity) -> None:
        self.validate()
        identity.validate()
        if (
            identity.instrument_id != self.instrument_id
            or identity.symbol != self.symbol
            or identity.exchange != self.official_exchange
            or identity.market != self.official_submarket
            or identity.mainboard_start != self.route_start
            or identity.route_status != self.route_status
        ):
            raise DataQualityError(
                "official submarket evidence conflicts with frozen identity"
            )

    def validate_provider_rows(self, rows: Iterable[dict[str, Any]]) -> None:
        self.validate()
        observed = list(rows)
        if len(observed) != self.provider_row_count:
            raise DataQualityError("provider submarket row count differs")
        by_date = {item.trade_date: item for item in self.comparisons}
        for row in observed:
            try:
                comparison = by_date.pop(date.fromisoformat(str(row["date"])))
            except (KeyError, ValueError) as exc:
                raise DataQualityError("provider submarket dates differ") from exc
            for field in ("open", "high", "low", "close", "volume"):
                if Decimal(str(row[field])) != Decimal(
                    str(getattr(comparison, f"provider_{field}"))
                ):
                    raise DataQualityError(
                        f"provider submarket {field} differs from evidence"
                    )
        if by_date:
            raise DataQualityError("official submarket evidence has unmatched dates")

    def validate_bar(self, bar: RawDailyBar, identity: InstrumentIdentity) -> None:
        self.validate_identity(identity)
        if (
            bar.source_name != self.provider_name
            or bar.source_endpoint != self.provider_endpoint
            or bar.symbol != self.symbol
            or (bar.source_market or "").upper() != self.provider_market.upper()
            or bar.query_from != self.query_from
            or bar.query_to != self.query_to
            or bar.raw_response_sha256 != self.provider_raw_sha256
        ):
            raise DataQualityError(
                "provider submarket request differs from frozen evidence"
            )
        matches = [item for item in self.comparisons if item.trade_date == bar.trade_date]
        if len(matches) != 1:
            raise DataQualityError("raw bar date is absent from submarket evidence")
        comparison = matches[0]
        for field in ("open", "high", "low", "close", "volume"):
            if Decimal(str(getattr(bar, f"raw_{field}"))) != Decimal(
                str(getattr(comparison, f"provider_{field}"))
            ):
                raise DataQualityError(
                    f"raw bar {field} differs from submarket evidence"
                )
