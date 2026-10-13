from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from stock_ai_bot.data_sources.causal_price_contract import (
    ACTION_LEDGER_SCHEMA,
    MARKET_METADATA_RECONCILIATION_SCHEMA,
    ActionKind,
    CorporateActionEvent,
    CorporateActionLedger,
    CorporateActionType,
    DataQualityError,
    OfficialOHLCVComparison,
    ProviderMarketMetadataReconciliation,
    RawDailyBar,
    SchemaVersionError,
    canonical_bytes,
    canonical_sha256,
    require_schema,
)
from stock_ai_bot.data_sources.official_submarket_evidence import (
    OFFICIAL_SUBMARKET_EVIDENCE_SCHEMA,
    OfficialSubmarketRouteEvidence,
)


def write_immutable_json(path: Path, payload: Any) -> str:
    encoded = canonical_bytes(payload) + b"\n"
    if path.exists():
        if not path.is_file() or path.read_bytes() != encoded:
            raise FileExistsError(f"refusing to overwrite immutable artifact: {path}")
        return canonical_sha256(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)
    return canonical_sha256(payload)


def load_versioned_manifest(path: Path, *, expected_schema: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise SchemaVersionError("versioned manifest root is not an object")
    require_schema(str(value.get("schema_version")), expected_schema)
    return value


def write_market_metadata_reconciliation(
    path: Path, evidence: ProviderMarketMetadataReconciliation
) -> str:
    evidence.validate()
    payload = {
        **evidence.content_payload(),
        "evidence_id": evidence.evidence_id,
        "content_sha256": evidence.content_sha256,
    }
    return write_immutable_json(path, payload)


def load_market_metadata_reconciliation(
    path: Path,
) -> ProviderMarketMetadataReconciliation:
    payload = load_versioned_manifest(
        path, expected_schema=MARKET_METADATA_RECONCILIATION_SCHEMA
    )
    comparisons = tuple(
        OfficialOHLCVComparison(
            trade_date=date.fromisoformat(str(item["trade_date"])),
            provider_open=float(item["provider_open"]),
            provider_high=float(item["provider_high"]),
            provider_low=float(item["provider_low"]),
            provider_close=float(item["provider_close"]),
            provider_volume=float(item["provider_volume"]),
            official_open=float(item["official_open"]),
            official_high=float(item["official_high"]),
            official_low=float(item["official_low"]),
            official_close=float(item["official_close"]),
            official_volume=float(item["official_volume"]),
            official_volume_unit=str(item["official_volume_unit"]),
            official_raw_sha256=str(item["official_raw_sha256"]),
            official_row_sha256=str(item["official_row_sha256"]),
        )
        for item in payload.get("comparisons") or []
    )
    evidence = ProviderMarketMetadataReconciliation(
        schema_version=str(payload["schema_version"]),
        status=str(payload["status"]),
        reason_code=str(payload["reason_code"]),
        instrument_id=str(payload["instrument_id"]),
        symbol=str(payload["symbol"]),
        official_exchange=str(payload["official_exchange"]),
        provider_exchange=str(payload["provider_exchange"]),
        provider_market=str(payload["provider_market"]),
        route_start=date.fromisoformat(str(payload["route_start"])),
        route_end_exclusive=date.fromisoformat(
            str(payload["route_end_exclusive"])
        ),
        route_status=str(payload["route_status"]),
        successor_instrument_id=(
            str(payload["successor_instrument_id"])
            if payload.get("successor_instrument_id")
            else None
        ),
        successor_symbol=(
            str(payload["successor_symbol"])
            if payload.get("successor_symbol")
            else None
        ),
        successor_exchange=(
            str(payload["successor_exchange"])
            if payload.get("successor_exchange")
            else None
        ),
        successor_route_start=(
            date.fromisoformat(str(payload["successor_route_start"]))
            if payload.get("successor_route_start")
            else None
        ),
        route_transition_source_sha256=str(
            payload["route_transition_source_sha256"]
        ),
        query_from=date.fromisoformat(str(payload["query_from"])),
        query_to=date.fromisoformat(str(payload["query_to"])),
        provider_name=str(payload["provider_name"]),
        provider_endpoint=str(payload["provider_endpoint"]),
        provider_request_identity_sha256=str(
            payload["provider_request_identity_sha256"]
        ),
        provider_raw_sha256=str(payload["provider_raw_sha256"]),
        provider_row_count=int(payload["provider_row_count"]),
        official_endpoint=str(payload["official_endpoint"]),
        official_artifact_manifest_sha256=str(
            payload["official_artifact_manifest_sha256"]
        ),
        official_row_count=int(payload["official_row_count"]),
        official_trade_dates_sha256=str(payload["official_trade_dates_sha256"]),
        comparisons=comparisons,
    )
    evidence.validate()
    if payload.get("content_sha256") != evidence.content_sha256:
        raise DataQualityError("provider market reconciliation hash mismatch")
    if payload.get("evidence_id") != evidence.evidence_id:
        raise DataQualityError("provider market reconciliation id mismatch")
    return evidence


def write_official_submarket_evidence(
    path: Path, evidence: OfficialSubmarketRouteEvidence
) -> str:
    evidence.validate()
    payload = {
        **evidence.content_payload(),
        "evidence_id": evidence.evidence_id,
        "content_sha256": evidence.content_sha256,
    }
    return write_immutable_json(path, payload)


def load_official_submarket_evidence(
    path: Path,
) -> OfficialSubmarketRouteEvidence:
    payload = load_versioned_manifest(
        path, expected_schema=OFFICIAL_SUBMARKET_EVIDENCE_SCHEMA
    )
    comparisons = tuple(
        OfficialOHLCVComparison(
            trade_date=date.fromisoformat(str(item["trade_date"])),
            provider_open=float(item["provider_open"]),
            provider_high=float(item["provider_high"]),
            provider_low=float(item["provider_low"]),
            provider_close=float(item["provider_close"]),
            provider_volume=float(item["provider_volume"]),
            official_open=float(item["official_open"]),
            official_high=float(item["official_high"]),
            official_low=float(item["official_low"]),
            official_close=float(item["official_close"]),
            official_volume=float(item["official_volume"]),
            official_volume_unit=str(item["official_volume_unit"]),
            official_raw_sha256=str(item["official_raw_sha256"]),
            official_row_sha256=str(item["official_row_sha256"]),
        )
        for item in payload.get("comparisons") or []
    )
    evidence = OfficialSubmarketRouteEvidence(
        schema_version=str(payload["schema_version"]),
        status=str(payload["status"]),
        reason_code=str(payload["reason_code"]),
        instrument_id=str(payload["instrument_id"]),
        symbol=str(payload["symbol"]),
        official_exchange=str(payload["official_exchange"]),
        official_submarket=str(payload["official_submarket"]),
        route_status=str(payload["route_status"]),
        route_start=date.fromisoformat(str(payload["route_start"])),
        route_end_exclusive=(
            date.fromisoformat(str(payload["route_end_exclusive"]))
            if payload.get("route_end_exclusive")
            else None
        ),
        route_source_endpoint=str(payload["route_source_endpoint"]),
        route_source_artifact_sha256=str(payload["route_source_artifact_sha256"]),
        query_from=date.fromisoformat(str(payload["query_from"])),
        query_to=date.fromisoformat(str(payload["query_to"])),
        provider_name=str(payload["provider_name"]),
        provider_endpoint=str(payload["provider_endpoint"]),
        provider_exchange=str(payload["provider_exchange"]),
        provider_market=str(payload["provider_market"]),
        provider_request_identity_sha256=str(
            payload["provider_request_identity_sha256"]
        ),
        provider_raw_sha256=str(payload["provider_raw_sha256"]),
        provider_row_count=int(payload["provider_row_count"]),
        official_price_endpoint=str(payload["official_price_endpoint"]),
        official_artifact_manifest_sha256=str(
            payload["official_artifact_manifest_sha256"]
        ),
        official_row_count=int(payload["official_row_count"]),
        official_trade_dates_sha256=str(payload["official_trade_dates_sha256"]),
        comparisons=comparisons,
    )
    evidence.validate()
    if payload.get("content_sha256") != evidence.content_sha256:
        raise DataQualityError("official submarket evidence hash mismatch")
    if payload.get("evidence_id") != evidence.evidence_id:
        raise DataQualityError("official submarket evidence id mismatch")
    return evidence


def validate_source_segments(bars: Iterable[RawDailyBar]) -> None:
    grouped: dict[str, list[RawDailyBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.instrument_id, []).append(bar)
    for instrument_id, rows in grouped.items():
        ordered = sorted(rows, key=lambda item: item.trade_date)
        seen_segments: set[str] = set()
        prior_segment: str | None = None
        prior_source: tuple[str, str] | None = None
        for row in ordered:
            current_source = (row.source_name, row.source_endpoint)
            if prior_source is not None and current_source != prior_source:
                if row.source_segment_id == prior_segment:
                    raise DataQualityError(
                        f"silent source change inside segment for {instrument_id}"
                    )
            if row.source_segment_id != prior_segment:
                if row.source_segment_id in seen_segments:
                    raise DataQualityError(
                        f"non-contiguous reused source segment for {instrument_id}"
                    )
                seen_segments.add(row.source_segment_id)
            prior_segment = row.source_segment_id
            prior_source = current_source


def _optional_date(value: Any) -> date | None:
    return date.fromisoformat(str(value)) if value else None


def _event_from_dict(value: dict[str, Any]) -> CorporateActionEvent:
    converted = dict(value)
    converted["action_type"] = CorporateActionType(str(converted["action_type"]))
    converted["action_kind"] = ActionKind(str(converted["action_kind"]))
    for field in (
        "effective_date",
        "ex_date",
        "available_on",
        "announcement_date",
        "entitlement_date",
        "payment_date",
        "halt_date",
        "resume_date",
    ):
        converted[field] = _optional_date(converted.get(field))
    converted["fetched_at"] = datetime.fromisoformat(str(converted["fetched_at"]))
    return CorporateActionEvent(**converted)


def load_corporate_action_ledger(manifest_path: Path) -> CorporateActionLedger:
    manifest = load_versioned_manifest(manifest_path, expected_schema=ACTION_LEDGER_SCHEMA)
    content_sha256 = str(manifest.get("content_sha256") or "")
    core = {
        key: value
        for key, value in manifest.items()
        if key not in {"snapshot_id", "content_sha256"}
    }
    if canonical_sha256(core) != content_sha256:
        raise DataQualityError("corporate-action ledger manifest content hash mismatch")
    events_path = manifest_path.parent / str(manifest["events_path"])
    event_payload = json.loads(events_path.read_text(encoding="utf-8-sig"))
    if not isinstance(event_payload, list):
        raise DataQualityError("corporate-action events root is not a list")
    if canonical_sha256(event_payload) != manifest.get("events_sha256"):
        raise DataQualityError("corporate-action events hash mismatch")
    events = tuple(_event_from_dict(item) for item in event_payload)
    if len(events) != int(manifest["event_count"]):
        raise DataQualityError("corporate-action event count disagrees with manifest")
    technical = manifest.get("technical_resolution") or {}
    unresolved = [
        event.action_id
        for event in events
        if event.technical_resolution_status != "RESOLVED_FOR_TECHNICAL"
    ]
    if sorted(unresolved) != sorted(technical.get("unresolved_event_ids") or []):
        raise DataQualityError("technical unresolved event ids disagree with manifest")
    position = manifest.get("position_lifecycle") or {}
    quarantined = [
        event.action_id
        for event in events
        if event.position_lifecycle_status == "QUARANTINED_UNSUPPORTED"
    ]
    if sorted(quarantined) != sorted(position.get("quarantined_event_ids") or []):
        raise DataQualityError("position quarantine event ids disagree with manifest")
    ledger = CorporateActionLedger(
        schema_version=str(manifest["schema_version"]),
        snapshot_id=str(manifest["snapshot_id"]),
        coverage_from=date.fromisoformat(str(manifest["coverage"]["from"])),
        coverage_through=date.fromisoformat(str(manifest["coverage"]["through"])),
        status=str(manifest["status"]),
        source_names=tuple(str(item) for item in manifest["source_names"]),
        events=events,
        manifest_sha256=content_sha256,
    )
    if ledger.status == "PASS":
        ledger.validate_for(
            from_date=ledger.coverage_from, through_date=ledger.coverage_through
        )
    return ledger
