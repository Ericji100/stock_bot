"""Read frozen, source-backed non-trading classifications for v2 replay.

This reader does not infer a suspension from a missing vendor bar. A producer
must supply both an exchange-calendar receipt and an official suspension
receipt, with immutable bytes under the isolated workdata tree.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

from stock_ai_bot.data_sources.causal_price_contract import (
    AvailabilityStatus,
    DailyAvailability,
    DataQualityError,
    SchemaVersionError,
    canonical_sha256,
)


AVAILABILITY_SCHEMA = "dual-ma-causal-availability-v1"
_STATUS_ROLES = {
    AvailabilityStatus.SUSPENDED: frozenset(
        {"EXCHANGE_CALENDAR", "OFFICIAL_SUSPENSION"}
    ),
    AvailabilityStatus.NO_TRANSACTION: frozenset(
        {"EXCHANGE_CALENDAR", "OFFICIAL_NO_TRANSACTION"}
    ),
}


@dataclass(frozen=True)
class FrozenAvailability:
    content_sha256: str
    by_symbol_date: Mapping[tuple[str, date], DailyAvailability]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _immutable_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise DataQualityError(
                f"refusing to overwrite immutable availability artifact: {path}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def load_frozen_availability(
    path: str | Path, *, workdata_root: str | Path
) -> FrozenAvailability:
    """Reject schema, content, receipt, or classification mismatches."""

    root = Path(workdata_root).resolve()
    manifest = Path(path).resolve()
    if not manifest.is_relative_to(root):
        raise DataQualityError("availability snapshot must be inside isolated workdata")
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != AVAILABILITY_SCHEMA:
        raise SchemaVersionError("unsupported causal availability snapshot schema")
    if payload.get("status") != "PASS" or not isinstance(payload.get("records"), list):
        raise DataQualityError("availability snapshot is not PASS with records")
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    digest = canonical_sha256(content)
    if payload.get("content_sha256") != digest:
        raise DataQualityError("availability snapshot content hash mismatch")
    records: dict[tuple[str, date], DailyAvailability] = {}
    for item in payload["records"]:
        if not isinstance(item, dict):
            raise DataQualityError("availability record must be an object")
        symbol = str(item["symbol"])
        day = date.fromisoformat(str(item["trade_date"]))
        key = (symbol, day)
        if key in records:
            raise DataQualityError(f"duplicate availability classification: {key}")
        try:
            status = AvailabilityStatus(str(item.get("status")))
        except ValueError as exc:
            raise DataQualityError(
                "only verified non-trading states may explain absent raw bars"
            ) from exc
        roles = _STATUS_ROLES.get(status)
        if roles is None:
            raise DataQualityError(
                "only verified non-trading states may explain absent raw bars"
            )
        if item.get("exchange_calendar_open") is not True:
            raise DataQualityError("suspension evidence requires an open exchange calendar")
        artifacts = item.get("source_artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != len(roles):
            raise DataQualityError(
                "non-trading evidence needs calendar and matching official receipts"
            )
        by_role: dict[str, str] = {}
        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get("role") not in roles:
                raise DataQualityError("unrecognized non-trading evidence role")
            role = str(artifact["role"])
            if role in by_role:
                raise DataQualityError("duplicate suspension evidence role")
            receipt = (root / str(artifact["path"])).resolve()
            if not receipt.is_relative_to(root) or not receipt.is_file():
                raise DataQualityError("suspension receipt is absent or outside workdata")
            actual = hashlib.sha256(receipt.read_bytes()).hexdigest()
            if actual != artifact.get("sha256"):
                raise DataQualityError("suspension receipt hash mismatch")
            by_role[role] = actual
        if set(by_role) != roles:
            raise DataQualityError("non-trading evidence roles are incomplete")
        evidence = DailyAvailability(
            instrument_id=str(item["instrument_id"]),
            trade_date=day,
            status=status,
            exchange_calendar_open=True,
            evidence_sources=tuple(f"{role}@{by_role[role]}" for role in sorted(roles)),
            evidence_sha256=canonical_sha256(dict(sorted(by_role.items()))),
            reason=str(item["reason"]),
        )
        evidence.validate()
        if not symbol or not evidence.instrument_id or not evidence.reason.strip():
            raise DataQualityError("suspension classification identity or reason is empty")
        records[key] = evidence
    return FrozenAvailability(content_sha256=digest, by_symbol_date=records)


def freeze_merged_availability_snapshots(
    paths: Iterable[str | Path],
    *,
    output_dir: str | Path,
    workdata_root: str | Path,
) -> Path:
    """Freeze a verified, duplicate-free union of causal availability snapshots."""

    root = Path(workdata_root).resolve()
    sources: list[dict[str, str]] = []
    records: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    status_counts: dict[str, int] = {}
    resolved_paths = tuple(Path(path).resolve() for path in paths)
    if len(resolved_paths) < 2:
        raise DataQualityError("availability merge requires at least two snapshots")
    for path in resolved_paths:
        verified = load_frozen_availability(path, workdata_root=root)
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        sources.append(
            {
                "path": path.relative_to(root).as_posix(),
                "file_sha256": _sha256_file(path),
                "content_sha256": verified.content_sha256,
            }
        )
        for item in payload["records"]:
            key = (str(item["symbol"]), str(item["trade_date"]))
            if key in seen:
                raise DataQualityError(
                    f"availability snapshots overlap at {key[0]} {key[1]}"
                )
            seen.add(key)
            status = str(item["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
            records.append(dict(item))
    records.sort(key=lambda item: (str(item["trade_date"]), str(item["symbol"])))
    core: dict[str, object] = {
        "schema_version": AVAILABILITY_SCHEMA,
        "status": "PASS",
        "source_snapshots": sorted(sources, key=lambda item: item["path"]),
        "coverage": {
            "classified_count": len(records),
            "status_counts": dict(sorted(status_counts.items())),
            "unresolved_count": 0,
        },
        "records": records,
        "performance_read_or_computed": False,
    }
    digest = canonical_sha256(core)
    payload = {**core, "content_sha256": digest}
    destination = Path(output_dir).resolve()
    if not destination.is_relative_to(root):
        raise DataQualityError("merged availability output must remain inside workdata")
    path = destination / f"dual-ma-causal-availability#{digest[:16]}.json"
    _immutable_json(path, payload)
    loaded = load_frozen_availability(path, workdata_root=root)
    if loaded.content_sha256 != digest or len(loaded.by_symbol_date) != len(records):
        raise DataQualityError("merged availability readback mismatch")
    return path
