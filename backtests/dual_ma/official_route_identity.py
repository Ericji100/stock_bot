from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from stock_ai_bot.data_sources.causal_price_contract import (
    OFFICIAL_SUBMARKET_ROUTE_REASON,
    DataQualityError,
    canonical_sha256,
)


ROUTE_IDENTITY_SCHEMA = "dual-ma-official-route-identity-v1"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _roc_date(value: str) -> date:
    parts = str(value).strip().split(".")
    if len(parts) != 3:
        raise ValueError("official TWSE listing date is invalid")
    return date(int(parts[0]) + 1911, int(parts[1]), int(parts[2]))


@dataclass(frozen=True)
class OfficialRouteIdentity:
    instrument_id: str
    symbol: str
    exchange: str
    market: str
    effective_start: date
    effective_end_exclusive: date | None
    route_status: str
    source_endpoint: str
    source_path: str
    source_sha256: str

    @property
    def reason_code(self) -> str:
        return OFFICIAL_SUBMARKET_ROUTE_REASON

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": ROUTE_IDENTITY_SCHEMA,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "market": self.market,
            "effective_start": self.effective_start,
            "effective_end_exclusive": self.effective_end_exclusive,
            "route_status": self.route_status,
            "source_endpoint": self.source_endpoint,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
        }

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.content_payload())

    @property
    def identity_id(self) -> str:
        return f"official-route-identity#{self.content_sha256[:16]}"

    def as_dict(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["effective_start"] = self.effective_start.isoformat()
        payload["effective_end_exclusive"] = (
            self.effective_end_exclusive.isoformat()
            if self.effective_end_exclusive else None
        )
        return {
            **payload,
            "identity_id": self.identity_id,
            "content_sha256": self.content_sha256,
        }

    def validate_bar(self, bar: Any, identity: Any) -> None:
        try:
            frozen = load_tib_route_identities_from_universe_source(
                Path(self.source_path), expected_sha256=self.source_sha256
            )[self.symbol]
        except (KeyError, OSError, ValueError) as exc:
            raise DataQualityError("official route-only proof is not reproducible") from exc
        if frozen != self:
            raise DataQualityError("official route-only proof conflicts with its source")
        if (
            identity.instrument_id != self.instrument_id
            or identity.symbol != self.symbol
            or identity.exchange != self.exchange
            or identity.market != self.market
            or identity.mainboard_start != self.effective_start
            or identity.route_status != self.route_status
            or identity.identity_source_sha256 != self.source_sha256
        ):
            raise DataQualityError("official route-only proof conflicts with identity")
        if (
            bar.instrument_id != self.instrument_id
            or bar.symbol != self.symbol
            or bar.exchange != self.exchange
            or (bar.source_market or "").upper() != self.market
            or bar.query_from < self.effective_start
            or bar.trade_date < self.effective_start
            or (
                self.effective_end_exclusive is not None
                and (
                    bar.query_to >= self.effective_end_exclusive
                    or bar.trade_date >= self.effective_end_exclusive
                )
            )
        ):
            raise DataQualityError("raw bar conflicts with official route-only proof")


def load_tib_route_identities_from_universe_source(
    source_path: Path, *, expected_sha256: str | None = None
) -> dict[str, OfficialRouteIdentity]:
    source_path = source_path.resolve()
    expected_sha = expected_sha256 or _sha256_file(source_path)
    if not source_path.is_file() or _sha256_file(source_path) != expected_sha:
        raise ValueError("official universe source path/hash mismatch")
    payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    endpoint = str((payload.get("source_urls") or {}).get("twse_new_listings") or "")
    tib_rows = [
        row for row in payload.get("twse_new_listings") or []
        if "創新板" in str(row.get("備註") or "")
    ]
    if tib_rows and not endpoint.startswith("https://www.twse.com.tw/"):
        raise ValueError("official TWSE listing route is outside the allowlist")
    results: dict[str, OfficialRouteIdentity] = {}
    for row in tib_rows:
        code = str(row.get("公司代號") or "").strip()
        start = _roc_date(str(row.get("股票上市買賣日期") or ""))
        current = [
            item for item in payload.get("twse_current") or []
            if str(item.get("公司代號") or "").strip() == code
        ]
        if len(current) != 1 or str(current[0].get("上市日期") or "") != start.strftime("%Y%m%d"):
            # The source also carries applicants not present in the current-listing
            # table.  They are not usable point-in-time identities.
            continue
        symbol = f"{code}.TW"
        results[symbol] = OfficialRouteIdentity(
            instrument_id=f"TWSE:{code}", symbol=symbol, exchange="TWSE", market="TIB",
            effective_start=start, effective_end_exclusive=None,
            route_status="TWSE_TIB_FIRST_LISTING", source_endpoint=endpoint,
            source_path=str(source_path), source_sha256=expected_sha,
        )
    return results


def load_tib_route_identities(scope_manifest_path: Path) -> dict[str, OfficialRouteIdentity]:
    scope = json.loads(scope_manifest_path.resolve().read_text(encoding="utf-8-sig"))
    source = (scope.get("source_files") or {}).get("universe_source") or {}
    return load_tib_route_identities_from_universe_source(
        Path(str(source.get("path") or "")),
        expected_sha256=str(source.get("sha256") or ""),
    )
