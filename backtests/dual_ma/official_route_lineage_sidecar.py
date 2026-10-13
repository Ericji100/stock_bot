from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from backtests.dual_ma.official_route_identity import (
    OfficialRouteIdentity,
    load_tib_route_identities_from_universe_source,
)
from stock_ai_bot.data_sources.causal_price_contract import (
    OFFICIAL_SUBMARKET_ROUTE_REASON,
    DataQualityError,
    canonical_sha256,
)
from stock_ai_bot.selection.historical_universe_service import (
    load_official_universe_sources,
)


SCHEMA_VERSION = "dual-ma-official-route-lineage-sidecar-v1"
IDENTITY_SCHEMA_VERSION = "dual-ma-official-route-lineage-identity-v1"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _current_listing_dates(source: Any) -> dict[str, date]:
    result: dict[str, date] = {}
    for row in source.twse_current:
        code = str(row.get("公司代號") or "").strip()
        raw = str(row.get("上市日期") or "").strip()
        if not code or len(raw) != 8 or not raw.isdigit():
            continue
        listed = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        if code in result and result[code] != listed:
            raise DataQualityError(f"ambiguous target TWSE listing date: {code}")
        result[code] = listed
    return result


def _source_lineage(path: Path, source: Any) -> dict[str, str]:
    return {
        "path": str(path.resolve()),
        "file_sha256": _sha256_file(path),
        "content_sha256": source.content_sha256,
        "source_id": source.source_id,
        "collected_at": source.collected_at,
        "coverage_start": source.coverage_start.isoformat(),
        "coverage_end": source.coverage_end.isoformat(),
    }


def _route_row(route: OfficialRouteIdentity) -> dict[str, Any]:
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "instrument_id": route.instrument_id,
        "symbol": route.symbol,
        "exchange": route.exchange,
        "market": route.market,
        "effective_start": route.effective_start.isoformat(),
        "effective_end_exclusive": (
            route.effective_end_exclusive.isoformat()
            if route.effective_end_exclusive else None
        ),
        "route_status": route.route_status,
        "reason_code": route.reason_code,
        "source_endpoint": route.source_endpoint,
        "legacy_route_identity_id": route.identity_id,
        "legacy_route_identity_sha256": route.content_sha256,
    }


def freeze_tib_route_lineage_sidecar(
    *,
    evidence_source_path: Path,
    target_source_path: Path,
    output_root: Path,
) -> Path:
    """Bridge a lossy rolling endpoint to prior immutable official route evidence."""

    evidence_path = evidence_source_path.resolve()
    target_path = target_source_path.resolve()
    evidence_source = load_official_universe_sources(evidence_path)
    target_source = load_official_universe_sources(target_path)
    if evidence_source.coverage_start != target_source.coverage_start:
        raise DataQualityError("route evidence and target source coverage starts differ")
    if evidence_source.collected_at >= target_source.collected_at:
        raise DataQualityError("route evidence must predate the target source")
    routes = load_tib_route_identities_from_universe_source(
        evidence_path, expected_sha256=_sha256_file(evidence_path)
    )
    if not routes:
        raise DataQualityError("prior official source contains no verified TIB routes")
    current_dates = _current_listing_dates(target_source)
    rows: list[dict[str, Any]] = []
    for symbol, route in sorted(routes.items()):
        if route.exchange != "TWSE" or route.market != "TIB":
            raise DataQualityError(f"unsupported route in lineage sidecar: {symbol}")
        if current_dates.get(route.instrument_id.split(":", 1)[1]) != route.effective_start:
            raise DataQualityError(f"target source cannot corroborate route start: {symbol}")
        rows.append(_route_row(route))
    core = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS_VERIFIED_PRIOR_ROUTE_LINEAGE",
        "route_evidence_source": _source_lineage(evidence_path, evidence_source),
        "target_source": _source_lineage(target_path, target_source),
        "route_count": len(rows),
        "routes": rows,
    }
    digest = canonical_sha256(core)
    artifact_id = f"dual-ma-official-route-lineage#{digest[:16]}"
    path = output_root.resolve() / f"{artifact_id}.json"
    payload = {**core, "artifact_id": artifact_id, "content_sha256": digest}
    path.parent.mkdir(parents=True, exist_ok=True)
    others = sorted(path.parent.glob("dual-ma-official-route-lineage#*.json"))
    if others and others != [path]:
        raise DataQualityError("conflicting immutable route lineage sidecar exists")
    if not path.exists():
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            pass
    loaded = _load_payload(path)
    if loaded != payload:
        raise DataQualityError("immutable route lineage sidecar changed")
    return path


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        core = {
            key: value for key, value in payload.items()
            if key not in {"artifact_id", "content_sha256"}
        }
        digest = canonical_sha256(core)
    except (OSError, ValueError, TypeError) as exc:
        raise DataQualityError("route lineage sidecar is unreadable") from exc
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("status") != "PASS_VERIFIED_PRIOR_ROUTE_LINEAGE"
        or payload.get("content_sha256") != digest
        or payload.get("artifact_id") != f"dual-ma-official-route-lineage#{digest[:16]}"
        or path.stem != payload.get("artifact_id")
    ):
        raise DataQualityError("route lineage sidecar identity or hash differs")
    return payload


@dataclass(frozen=True)
class OfficialRouteLineageIdentity:
    instrument_id: str
    symbol: str
    exchange: str
    market: str
    effective_start: date
    effective_end_exclusive: date | None
    route_status: str
    source_endpoint: str
    legacy_route_identity_id: str
    legacy_route_identity_sha256: str
    source_path: str
    source_sha256: str
    evidence_source_path: str
    evidence_source_file_sha256: str

    @property
    def reason_code(self) -> str:
        return OFFICIAL_SUBMARKET_ROUTE_REASON

    @property
    def identity_id(self) -> str:
        """Receipt-compatible identity frozen by the original raw snapshot."""
        return self.legacy_route_identity_id

    @property
    def lineage_identity_id(self) -> str:
        return f"official-route-lineage-identity#{self.content_sha256[:16]}"

    @property
    def content_sha256(self) -> str:
        return canonical_sha256({
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "market": self.market,
            "effective_start": self.effective_start,
            "effective_end_exclusive": self.effective_end_exclusive,
            "route_status": self.route_status,
            "source_endpoint": self.source_endpoint,
            "legacy_route_identity_id": self.legacy_route_identity_id,
            "legacy_route_identity_sha256": self.legacy_route_identity_sha256,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "evidence_source_path": self.evidence_source_path,
            "evidence_source_file_sha256": self.evidence_source_file_sha256,
        })

    def validate_bar(self, bar: Any, identity: Any) -> None:
        frozen = load_tib_route_lineage_sidecar(
            Path(self.source_path), expected_sha256=self.source_sha256
        ).get(self.symbol)
        if frozen != self:
            raise DataQualityError("route lineage proof is not reproducible")
        if (
            identity.instrument_id != self.instrument_id
            or identity.symbol != self.symbol
            or identity.exchange != self.exchange
            or identity.market != self.market
            or identity.mainboard_start != self.effective_start
            or identity.route_status != self.route_status
            or identity.identity_source_sha256 != self.source_sha256
        ):
            raise DataQualityError("route lineage proof conflicts with identity")
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
            raise DataQualityError("raw bar conflicts with route lineage proof")


def _verify_source(lineage: dict[str, str]) -> Any:
    path = Path(lineage["path"]).resolve()
    if not path.is_file() or _sha256_file(path) != lineage["file_sha256"]:
        raise DataQualityError("route lineage source file hash differs")
    source = load_official_universe_sources(path)
    if (
        source.content_sha256 != lineage["content_sha256"]
        or source.source_id != lineage["source_id"]
        or source.collected_at != lineage["collected_at"]
        or source.coverage_start.isoformat() != lineage["coverage_start"]
        or source.coverage_end.isoformat() != lineage["coverage_end"]
    ):
        raise DataQualityError("route lineage source identity differs")
    return source


def load_tib_route_lineage_sidecar(
    sidecar_path: Path,
    *,
    expected_sha256: str | None = None,
    target_source_path: Path | None = None,
    as_of_date: date | None = None,
) -> dict[str, OfficialRouteLineageIdentity]:
    path = sidecar_path.resolve()
    payload = _load_payload(path)
    digest = str(payload["content_sha256"])
    if expected_sha256 is not None and digest != expected_sha256:
        raise DataQualityError("route lineage sidecar expected hash differs")
    evidence_source = _verify_source(payload["route_evidence_source"])
    target_source = _verify_source(payload["target_source"])
    if target_source_path is not None and target_source_path.resolve() != Path(payload["target_source"]["path"]):
        raise DataQualityError("route lineage target source path differs")
    legacy = load_tib_route_identities_from_universe_source(
        Path(payload["route_evidence_source"]["path"]),
        expected_sha256=payload["route_evidence_source"]["file_sha256"],
    )
    current_dates = _current_listing_dates(target_source)
    rows = payload.get("routes") or []
    if payload.get("route_count") != len(rows) or len(rows) != len(legacy):
        raise DataQualityError("route lineage sidecar count differs")
    results: dict[str, OfficialRouteLineageIdentity] = {}
    for row in rows:
        symbol = str(row["symbol"])
        original = legacy.get(symbol)
        if original is None or row != _route_row(original):
            raise DataQualityError(f"route lineage row differs from frozen evidence: {symbol}")
        start = date.fromisoformat(str(row["effective_start"]))
        code = str(row["instrument_id"]).split(":", 1)[1]
        if current_dates.get(code) != start:
            raise DataQualityError(f"target source no longer corroborates route: {symbol}")
        if as_of_date is not None and start > as_of_date:
            continue
        identity = OfficialRouteLineageIdentity(
            instrument_id=str(row["instrument_id"]),
            symbol=symbol,
            exchange=str(row["exchange"]),
            market=str(row["market"]),
            effective_start=start,
            effective_end_exclusive=(
                date.fromisoformat(str(row["effective_end_exclusive"]))
                if row.get("effective_end_exclusive") else None
            ),
            route_status=str(row["route_status"]),
            source_endpoint=str(row["source_endpoint"]),
            legacy_route_identity_id=str(row["legacy_route_identity_id"]),
            legacy_route_identity_sha256=str(row["legacy_route_identity_sha256"]),
            source_path=str(path),
            source_sha256=digest,
            evidence_source_path=str(Path(payload["route_evidence_source"]["path"])),
            evidence_source_file_sha256=str(payload["route_evidence_source"]["file_sha256"]),
        )
        if symbol in results:
            raise DataQualityError(f"duplicate route lineage symbol: {symbol}")
        results[symbol] = identity
    return results
