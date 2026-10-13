"""Causal availability v2: snapshots that carry their own calendar evidence.

Contract: docs/strategies/dual_ma/causal-availability-calendar-evidence-v2-contract.md

The v1 loader in backtests.dual_ma.causal_availability is deliberately left
untouched and keeps failing closed on drifted receipts.  This module adds:

* dual-ma-causal-availability-v2 snapshots whose EXCHANGE_CALENDAR artifact
  points at an immutable, self-contained calendar evidence pack;
* a strict v1 -> v2 reissue path that re-verifies manifest self-integrity and
  every OFFICIAL_* receipt, replaces only the calendar artifact, and proves the
  semantic fingerprint is unchanged;
* a v2 merge that refuses duplicate keys, mixed schemas and unverified sources.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from backtests.dual_ma.causal_availability import (
    AVAILABILITY_SCHEMA as AVAILABILITY_SCHEMA_V1,
    FrozenAvailability,
    load_frozen_availability,
)
from backtests.dual_ma.causal_calendar_evidence_v2 import (
    FrozenCalendarEvidence,
    freeze_exchange_calendar_evidence,
    load_frozen_calendar_evidence,
)
from backtests.dual_ma.official_trading_calendar_v1 import (
    OFFICIAL_CALENDAR_SCHEMA,
    load_official_calendar,
)
from stock_ai_bot.data_sources.causal_price_contract import (
    AvailabilityStatus,
    DailyAvailability,
    DataQualityError,
    SchemaVersionError,
    canonical_sha256,
)


AVAILABILITY_SCHEMA_V2 = "dual-ma-causal-availability-v2"
MIGRATION_AUDIT_SCHEMA = "dual-ma-availability-v1-to-v2-migration-v1"
CALENDAR_ROLE = "EXCHANGE_CALENDAR"
OFFICIAL_ROLE_BY_STATUS = {
    AvailabilityStatus.SUSPENDED: "OFFICIAL_SUSPENSION",
    AvailabilityStatus.NO_TRANSACTION: "OFFICIAL_NO_TRANSACTION",
}
FINGERPRINT_EXCLUDED_ROLE = CALENDAR_ROLE


@dataclass(frozen=True)
class LoadedAvailability:
    """Normalized view over a verified v1 or v2 availability snapshot."""

    schema_version: str
    content_sha256: str
    file_sha256: str
    relative_path: str
    by_symbol_date: Mapping[tuple[str, date], DailyAvailability]
    calendar: FrozenCalendarEvidence | None
    calendar_authority: str = "NONE"
    calendar_coverage_start: date | None = None
    calendar_coverage_end: date | None = None
    trading_calendar: tuple[date, ...] | None = None
    calendar_content_sha256: str | None = None
    calendar_file_sha256: str | None = None
    calendar_relative_path: str | None = None
    calendar_open_dates_sha256: str | None = None



def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise DataQualityError(f"refusing to overwrite immutable artifact: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def semantic_fingerprint(records: Iterable[Mapping[str, Any]]) -> str:
    """Hash classification semantics, excluding the calendar artifact pair."""

    items: list[dict[str, Any]] = []
    for record in records:
        official = sorted(
            (
                {
                    "role": str(artifact["role"]),
                    "path": str(artifact["path"]),
                    "sha256": str(artifact["sha256"]),
                }
                for artifact in record["source_artifacts"]
                if str(artifact.get("role")) != FINGERPRINT_EXCLUDED_ROLE
            ),
            key=lambda item: item["role"],
        )
        items.append(
            {
                "symbol": str(record["symbol"]),
                "instrument_id": str(record["instrument_id"]),
                "trade_date": str(record["trade_date"]),
                "status": str(record["status"]),
                "exchange_calendar_open": record.get("exchange_calendar_open") is True,
                "reason": str(record["reason"]),
                "official_artifacts": official,
            }
        )
    items.sort(key=lambda item: (item["trade_date"], item["symbol"]))
    return canonical_sha256(items)


def _status_counts(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record["status"])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _record_key(record: Mapping[str, Any]) -> tuple[str, str]:
    return (str(record["symbol"]), str(record["trade_date"]))


def _verify_official_receipts(
    records: Sequence[Mapping[str, Any]], *, root: Path
) -> tuple[dict[str, int], dict[tuple[str, str], dict[str, str]]]:
    """Re-verify every OFFICIAL_* receipt of a v1 manifest without the v1 gate."""

    role_counts: dict[str, int] = {}
    verified: dict[tuple[str, str], dict[str, str]] = {}
    for record in records:
        try:
            status = AvailabilityStatus(str(record["status"]))
        except ValueError as exc:
            raise DataQualityError(
                "only verified non-trading states may explain absent raw bars"
            ) from exc
        expected_role = OFFICIAL_ROLE_BY_STATUS.get(status)
        if expected_role is None:
            raise DataQualityError(
                "only verified non-trading states may explain absent raw bars"
            )
        if record.get("exchange_calendar_open") is not True:
            raise DataQualityError(
                "non-trading evidence requires an open exchange calendar"
            )
        calendar_artifacts = [
            item
            for item in record["source_artifacts"]
            if str(item.get("role")) == CALENDAR_ROLE
        ]
        official_artifacts = [
            item
            for item in record["source_artifacts"]
            if str(item.get("role")) == expected_role
        ]
        if len(calendar_artifacts) != 1 or len(official_artifacts) != 1:
            raise DataQualityError("non-trading evidence roles are incomplete")
        receipt = (root / str(official_artifacts[0]["path"])).resolve()
        if not receipt.is_relative_to(root) or not receipt.is_file():
            raise DataQualityError("official receipt is absent or outside workdata")
        actual = _sha256_file(receipt)
        if actual != official_artifacts[0].get("sha256"):
            raise DataQualityError("official receipt hash mismatch")
        role_counts[expected_role] = role_counts.get(expected_role, 0) + 1
        verified[_record_key(record)] = {expected_role: actual}
    return dict(sorted(role_counts.items())), verified


def load_frozen_availability_v2(
    path: str | Path, *, workdata_root: str | Path
) -> LoadedAvailability:
    """Verify a v2 snapshot, its frozen calendar pack and every receipt."""

    root = Path(workdata_root).resolve()
    manifest = Path(path).resolve()
    if not manifest.is_relative_to(root):
        raise DataQualityError("availability snapshot must be inside isolated workdata")
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != AVAILABILITY_SCHEMA_V2
    ):
        raise SchemaVersionError("unsupported causal availability snapshot schema")
    if payload.get("status") != "PASS" or not isinstance(payload.get("records"), list):
        raise DataQualityError("availability snapshot is not PASS with records")
    core = {
        key: value
        for key, value in payload.items()
        if key not in {"content_sha256", "snapshot_id"}
    }
    digest = canonical_sha256(core)
    if payload.get("content_sha256") != digest:
        raise DataQualityError("availability snapshot content hash mismatch")
    calendar_block = payload.get("calendar_evidence")
    if not isinstance(calendar_block, dict):
        raise DataQualityError("availability snapshot lacks frozen calendar evidence")
    evidence_path = (root / str(calendar_block.get("path") or "")).resolve()
    if not evidence_path.is_relative_to(root):
        raise DataQualityError("calendar evidence is outside workdata")
    evidence_schema = json.loads(evidence_path.read_text(encoding="utf-8-sig")).get(
        "schema_version"
    )
    if evidence_schema == OFFICIAL_CALENDAR_SCHEMA:
        official = load_official_calendar(evidence_path, workdata_root=root)
        if official.file_sha256 != calendar_block.get("file_sha256"):
            raise DataQualityError("official calendar file hash mismatch")
        if official.content_sha256 != calendar_block.get("content_sha256"):
            raise DataQualityError("official calendar content hash disagrees with snapshot")
        if official.union_sha256 != calendar_block.get("open_dates_sha256"):
            raise DataQualityError(
                "official calendar semantic hash disagrees with snapshot"
            )
        calendar = None
        open_dates = set(official.open_dates_union)
        official_content_sha256 = official.content_sha256
        official_file_sha256 = official.file_sha256
        official_relative_path = official.relative_path
        calendar_authority = "OFFICIAL_TWSE_TPEX"
        coverage_start = official.coverage_start
        coverage_end = official.coverage_end
        trading_calendar = official.open_dates_union
        open_dates_sha256 = official.union_sha256
        calendar_file_sha256 = official.file_sha256
    else:
        calendar = load_frozen_calendar_evidence(evidence_path, workdata_root=root)
        if calendar.file_sha256 != calendar_block.get("file_sha256"):
            raise DataQualityError("calendar evidence file hash mismatch")
        if calendar.content_sha256 != calendar_block.get("content_sha256"):
            raise DataQualityError("calendar evidence content hash disagrees with snapshot")
        if calendar.open_dates_sha256 != calendar_block.get("open_dates_sha256"):
            raise DataQualityError(
                "calendar evidence semantic hash disagrees with snapshot"
            )
        open_dates = set(calendar.open_dates)
        calendar_authority = "TWII_SECONDARY"
        coverage_start = calendar.window_start
        coverage_end = calendar.window_end
        trading_calendar = None
        open_dates_sha256 = calendar.open_dates_sha256
        calendar_file_sha256 = calendar.file_sha256
        official_content_sha256 = calendar.content_sha256
        official_file_sha256 = calendar.file_sha256
        official_relative_path = calendar.relative_path
    declared_authority = payload.get("calendar_authority")
    if declared_authority is not None and declared_authority != calendar_authority:
        raise DataQualityError(
            "availability snapshot calendar authority disagrees with its evidence"
        )
    declared_source = payload.get("official_calendar_source_sha256")
    if declared_source is not None and declared_source != calendar_block.get(
        "content_sha256"
    ):
        raise DataQualityError(
            "availability snapshot official calendar source hash mismatch"
        )
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
        expected_role = OFFICIAL_ROLE_BY_STATUS.get(status)
        if expected_role is None:
            raise DataQualityError(
                "only verified non-trading states may explain absent raw bars"
            )
        if item.get("exchange_calendar_open") is not True:
            raise DataQualityError(
                "non-trading evidence requires an open exchange calendar"
            )
        if day not in open_dates:
            raise DataQualityError("classification date is not a frozen open date")
        artifacts = item.get("source_artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 2:
            raise DataQualityError(
                "non-trading evidence needs calendar and matching official receipts"
            )
        by_role: dict[str, str] = {}
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise DataQualityError("availability evidence must be an object")
            role = str(artifact.get("role"))
            if role in by_role:
                raise DataQualityError("duplicate suspension evidence role")
            if role not in {CALENDAR_ROLE, expected_role}:
                raise DataQualityError("unrecognized non-trading evidence role")
            receipt = (root / str(artifact["path"])).resolve()
            if not receipt.is_relative_to(root) or not receipt.is_file():
                raise DataQualityError(
                    "suspension receipt is absent or outside workdata"
                )
            actual = _sha256_file(receipt)
            if actual != artifact.get("sha256"):
                raise DataQualityError("suspension receipt hash mismatch")
            if role == CALENDAR_ROLE and (
                receipt != evidence_path or actual != calendar_file_sha256
            ):
                raise DataQualityError(
                    "exchange calendar artifact must be this snapshot's frozen evidence"
                )
            by_role[role] = actual
        if set(by_role) != {CALENDAR_ROLE, expected_role}:
            raise DataQualityError("non-trading evidence roles are incomplete")
        evidence = DailyAvailability(
            instrument_id=str(item["instrument_id"]),
            trade_date=day,
            status=status,
            exchange_calendar_open=True,
            evidence_sources=tuple(
                f"{role}@{by_role[role]}" for role in sorted(by_role)
            ),
            evidence_sha256=canonical_sha256(dict(sorted(by_role.items()))),
            reason=str(item["reason"]),
        )
        evidence.validate()
        if not symbol or not evidence.instrument_id or not evidence.reason.strip():
            raise DataQualityError(
                "suspension classification identity or reason is empty"
            )
        records[key] = evidence
    expected_fingerprint = semantic_fingerprint(payload["records"])
    if payload.get("semantic_fingerprint_sha256") != expected_fingerprint:
        raise DataQualityError("availability snapshot semantic fingerprint mismatch")
    return LoadedAvailability(
        schema_version=AVAILABILITY_SCHEMA_V2,
        content_sha256=digest,
        file_sha256=_sha256_file(manifest),
        relative_path=manifest.relative_to(root).as_posix(),
        by_symbol_date=records,
        calendar=calendar,
        calendar_authority=calendar_authority,
        calendar_coverage_start=coverage_start,
        calendar_coverage_end=coverage_end,
        trading_calendar=trading_calendar,
        calendar_content_sha256=official_content_sha256,
        calendar_file_sha256=official_file_sha256,
        calendar_relative_path=official_relative_path,
        calendar_open_dates_sha256=open_dates_sha256,
    )


def load_availability_snapshot(
    path: str | Path, *, workdata_root: str | Path
) -> LoadedAvailability:
    """Load either schema; the v1 branch keeps its unmodified fail-closed rules."""

    root = Path(workdata_root).resolve()
    manifest = Path(path).resolve()
    if not manifest.is_relative_to(root):
        raise DataQualityError("availability snapshot must be inside isolated workdata")
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    schema = payload.get("schema_version") if isinstance(payload, dict) else None
    if schema == AVAILABILITY_SCHEMA_V2:
        return load_frozen_availability_v2(manifest, workdata_root=root)
    if schema == AVAILABILITY_SCHEMA_V1:
        frozen: FrozenAvailability = load_frozen_availability(
            manifest, workdata_root=root
        )
        return LoadedAvailability(
            schema_version=AVAILABILITY_SCHEMA_V1,
            content_sha256=frozen.content_sha256,
            file_sha256=_sha256_file(manifest),
            relative_path=manifest.relative_to(root).as_posix(),
            by_symbol_date=frozen.by_symbol_date,
            calendar=None,
        )
    raise SchemaVersionError(
        f"unsupported causal availability snapshot schema {schema!r}"
    )


def _rewrite_calendar_artifact(
    record: Mapping[str, Any], *, evidence_relative: str, evidence_file_sha256: str
) -> dict[str, Any]:
    rewritten = dict(record)
    artifacts: list[dict[str, Any]] = []
    seen = False
    for artifact in record["source_artifacts"]:
        if str(artifact.get("role")) == CALENDAR_ROLE:
            if seen:
                raise DataQualityError("duplicate exchange calendar artifact")
            seen = True
            artifacts.append(
                {
                    "role": CALENDAR_ROLE,
                    "path": evidence_relative,
                    "sha256": evidence_file_sha256,
                }
            )
            continue
        artifacts.append(dict(artifact))
    if not seen:
        raise DataQualityError("availability record lacks an exchange calendar artifact")
    rewritten["source_artifacts"] = artifacts
    return rewritten


def _window_of(records: Sequence[Mapping[str, Any]]) -> tuple[date, date]:
    days = sorted(date.fromisoformat(str(record["trade_date"])) for record in records)
    if not days:
        raise DataQualityError("availability snapshot has no records")
    return days[0], days[-1]


@dataclass(frozen=True)
class _CalendarBinding:
    """Uniform view over either calendar evidence schema."""

    authority: str
    relative_path: str
    file_sha256: str
    content_sha256: str
    open_dates_sha256: str
    calendar_id: str
    window_start: date
    window_end: date
    open_dates: tuple[date, ...]
    source_path_at_freeze: str
    source_file_sha256_at_freeze: str
    source_window_rows_sha256: str


def _bind_calendar_evidence(path: str | Path, *, root: Path) -> _CalendarBinding:
    """Bind a frozen calendar pack, accepting the legacy or official schema."""

    manifest = Path(path).resolve()
    if not manifest.is_relative_to(root):
        raise DataQualityError("calendar evidence must live inside workdata")
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") == OFFICIAL_CALENDAR_SCHEMA:
        official = load_official_calendar(manifest, workdata_root=root)
        return _CalendarBinding(
            authority="OFFICIAL_TWSE_TPEX",
            relative_path=official.relative_path,
            file_sha256=official.file_sha256,
            content_sha256=official.content_sha256,
            open_dates_sha256=official.union_sha256,
            calendar_id=str(official.payload["calendar_id"]),
            window_start=official.coverage_start,
            window_end=official.coverage_end,
            open_dates=official.open_dates_union,
            source_path_at_freeze=official.relative_path,
            source_file_sha256_at_freeze=official.file_sha256,
            source_window_rows_sha256=official.union_sha256,
        )
    legacy = load_frozen_calendar_evidence(manifest, workdata_root=root)
    return _CalendarBinding(
        authority="TWII_SECONDARY",
        relative_path=legacy.relative_path,
        file_sha256=legacy.file_sha256,
        content_sha256=legacy.content_sha256,
        open_dates_sha256=legacy.open_dates_sha256,
        calendar_id=legacy.calendar_id,
        window_start=legacy.window_start,
        window_end=legacy.window_end,
        open_dates=legacy.open_dates,
        source_path_at_freeze=legacy.source_path_at_freeze,
        source_file_sha256_at_freeze=legacy.source_file_sha256_at_freeze,
        source_window_rows_sha256=legacy.source_window_rows_sha256,
    )

def reissue_availability_v2(
    *,
    v1_path: str | Path,
    calendar_evidence_path: str | Path,
    output_dir: str | Path,
    audit_dir: str | Path,
    workdata_root: str | Path,
    git_commit: str = "UNKNOWN",
    producer_version: str = "dual-ma-availability-v2-1",
) -> tuple[Path, Path]:
    """Reissue one v1 snapshot as v2 and write its migration audit report."""

    root = Path(workdata_root).resolve()
    source = Path(v1_path).resolve()
    if not source.is_relative_to(root):
        raise DataQualityError(
            "v1 availability snapshot must be inside isolated workdata"
        )
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") != AVAILABILITY_SCHEMA_V1:
        raise SchemaVersionError(
            f"reissue expects {AVAILABILITY_SCHEMA_V1}, "
            f"got {payload.get('schema_version')!r}"
        )
    if payload.get("status") != "PASS" or not isinstance(payload.get("records"), list):
        raise DataQualityError("v1 availability snapshot is not PASS with records")
    v1_core = {key: value for key, value in payload.items() if key != "content_sha256"}
    v1_digest = canonical_sha256(v1_core)
    if payload.get("content_sha256") != v1_digest:
        raise DataQualityError("v1 availability snapshot content hash mismatch")
    v1_records: list[dict[str, Any]] = [dict(item) for item in payload["records"]]
    role_counts, _verified = _verify_official_receipts(v1_records, root=root)
    calendar = _bind_calendar_evidence(calendar_evidence_path, root=root)
    window_start, window_end = _window_of(v1_records)
    if calendar.window_start > window_start or calendar.window_end < window_end:
        raise DataQualityError("calendar evidence does not cover the v1 window")
    open_dates = set(calendar.open_dates)
    outside = sorted(
        {
            str(item["trade_date"])
            for item in v1_records
            if date.fromisoformat(str(item["trade_date"])) not in open_dates
        }
    )
    if outside:
        raise DataQualityError(
            f"{len(outside)} classification dates fall outside frozen open dates"
        )
    v2_records = [
        _rewrite_calendar_artifact(
            record,
            evidence_relative=calendar.relative_path,
            evidence_file_sha256=calendar.file_sha256,
        )
        for record in v1_records
    ]
    v2_records.sort(key=lambda item: (str(item["trade_date"]), str(item["symbol"])))
    v1_keys = sorted(_record_key(item) for item in v1_records)
    v2_keys = sorted(_record_key(item) for item in v2_records)
    if v1_keys != v2_keys or len(set(v2_keys)) != len(v2_keys):
        raise DataQualityError("v1/v2 key sets differ during reissue")
    v1_counts = _status_counts(v1_records)
    v2_counts = _status_counts(v2_records)
    if v1_counts != v2_counts:
        raise DataQualityError("v1/v2 status distributions differ during reissue")
    v1_fingerprint = semantic_fingerprint(v1_records)
    v2_fingerprint = semantic_fingerprint(v2_records)
    if v1_fingerprint != v2_fingerprint:
        raise DataQualityError("v1/v2 semantic fingerprints differ during reissue")
    legacy_calendar = sorted(
        {
            (str(item["path"]), str(item["sha256"]))
            for record in v1_records
            for item in record["source_artifacts"]
            if str(item.get("role")) == CALENDAR_ROLE
        }
    )
    if len(legacy_calendar) != 1:
        raise DataQualityError("v1 snapshot has inconsistent calendar receipts")
    legacy_path, legacy_hash = legacy_calendar[0]
    legacy_receipt = root / legacy_path
    recoverable = legacy_receipt.is_file() and (
        _sha256_file(legacy_receipt) == legacy_hash
    )
    trading_days = sorted({str(item["trade_date"]) for item in v2_records})
    core: dict[str, Any] = {
        "schema_version": AVAILABILITY_SCHEMA_V2,
        "status": "PASS",
        "calendar_evidence": {
            "path": calendar.relative_path,
            "file_sha256": calendar.file_sha256,
            "content_sha256": calendar.content_sha256,
            "open_dates_sha256": calendar.open_dates_sha256,
            "calendar_id": calendar.calendar_id,
            "window_start": calendar.window_start.isoformat(),
            "window_end": calendar.window_end.isoformat(),
        },
        "calendar_authority": calendar.authority,
        "official_calendar_source_sha256": (
            calendar.content_sha256
            if calendar.authority == "OFFICIAL_TWSE_TPEX"
            else None
        ),
        "coverage": {
            "classified_count": len(v2_records),
            "status_counts": v2_counts,
            "unresolved_count": 0,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "trading_day_count": len(trading_days),
            "open_date_count": len(calendar.open_dates),
        },
        "records": v2_records,
        "semantic_fingerprint_sha256": v2_fingerprint,
        "reissued_from": {
            "path": source.relative_to(root).as_posix(),
            "schema_version": AVAILABILITY_SCHEMA_V1,
            "file_sha256": _sha256_file(source),
            "content_sha256": v1_digest,
            "legacy_calendar_artifact": {
                "path": legacy_path,
                "sha256": legacy_hash,
                "bytes_recoverable": recoverable,
                "note": (
                    "legacy shared-cache bytes still match"
                    if recoverable
                    else "legacy shared-cache bytes were rewritten and are "
                    "not recoverable"
                ),
            },
            "official_receipts_reverified": sum(role_counts.values()),
            "official_receipt_role_counts": role_counts,
        },
        "producer_version": producer_version,
        "git_commit": git_commit,
        "performance_read_or_computed": False,
    }
    digest = canonical_sha256(core)
    snapshot = {
        **core,
        "snapshot_id": f"dual-ma-causal-availability#{digest[:16]}",
        "content_sha256": digest,
    }
    destination = Path(output_dir).resolve()
    if not destination.is_relative_to(root):
        raise DataQualityError("v2 availability output must remain inside workdata")
    snapshot_path = destination / f"dual-ma-causal-availability#{digest[:16]}.json"
    _immutable_json(snapshot_path, snapshot)
    loaded = load_frozen_availability_v2(snapshot_path, workdata_root=root)
    if loaded.content_sha256 != digest or len(loaded.by_symbol_date) != len(v2_records):
        raise DataQualityError("v2 availability readback mismatch after reissue")
    audit_core: dict[str, Any] = {
        "schema_version": MIGRATION_AUDIT_SCHEMA,
        "status": "PASS",
        "source_snapshot": {
            "path": source.relative_to(root).as_posix(),
            "file_sha256": _sha256_file(source),
            "content_sha256": v1_digest,
            "schema_version": AVAILABILITY_SCHEMA_V1,
            "record_count": len(v1_records),
            "status_counts": v1_counts,
            "legacy_calendar_path": legacy_path,
            "legacy_calendar_sha256": legacy_hash,
            "legacy_calendar_bytes_recoverable": recoverable,
        },
        "reissued_snapshot": {
            "path": loaded.relative_path,
            "file_sha256": loaded.file_sha256,
            "content_sha256": loaded.content_sha256,
            "schema_version": AVAILABILITY_SCHEMA_V2,
            "calendar_authority": loaded.calendar_authority,
            "record_count": len(v2_records),
            "status_counts": v2_counts,
        },
        "calendar_evidence": {
            "path": calendar.relative_path,
            "file_sha256": calendar.file_sha256,
            "content_sha256": calendar.content_sha256,
            "open_dates_sha256": calendar.open_dates_sha256,
            "open_date_count": len(calendar.open_dates),
            "window_start": calendar.window_start.isoformat(),
            "window_end": calendar.window_end.isoformat(),
            "source_path_at_freeze": calendar.source_path_at_freeze,
            "source_file_sha256_at_freeze": calendar.source_file_sha256_at_freeze,
            "source_window_rows_sha256": calendar.source_window_rows_sha256,
        },
        "comparison": {
            "record_count_equal": len(v1_records) == len(v2_records),
            "key_set_equal": v1_keys == v2_keys,
            "status_counts_equal": v1_counts == v2_counts,
            "semantic_fingerprint_equal": v1_fingerprint == v2_fingerprint,
            "v1_semantic_fingerprint_sha256": v1_fingerprint,
            "v2_semantic_fingerprint_sha256": v2_fingerprint,
            "dates_within_frozen_open_dates": True,
            "official_receipt_role_counts": role_counts,
            "trading_day_count": len(trading_days),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
        "producer_version": producer_version,
        "git_commit": git_commit,
        "performance_read_or_computed": False,
    }
    audit_digest = canonical_sha256(audit_core)
    audit_payload = {
        **audit_core,
        "audit_id": f"dual-ma-availability-migration#{audit_digest[:16]}",
        "content_sha256": audit_digest,
    }
    audit_root = Path(audit_dir).resolve()
    if not audit_root.is_relative_to(root):
        raise DataQualityError("migration audit must remain inside workdata")
    audit_path = (
        audit_root / f"dual-ma-availability-v1-to-v2-migration#{audit_digest[:16]}.json"
    )
    _immutable_json(audit_path, audit_payload)
    return snapshot_path, audit_path


def freeze_merged_availability_snapshots_v2(
    paths: Sequence[str | Path],
    *,
    output_dir: str | Path,
    workdata_root: str | Path,
    calendar_source_path: str | Path | None = None,
    calendar_output_dir: str | Path | None = None,
    official_calendar_path: str | Path | None = None,
    git_commit: str = "UNKNOWN",
    producer_version: str = "dual-ma-availability-v2-merge-1",
    frozen_at: datetime | None = None,
) -> Path:
    """Freeze a verified, duplicate-free union of v2 availability snapshots."""

    root = Path(workdata_root).resolve()
    if len(paths) < 2:
        raise DataQualityError("availability merge requires at least two snapshots")
    source_payloads = [
        json.loads(Path(path).resolve().read_text(encoding="utf-8-sig"))
        for path in paths
    ]
    loaded_sources = [
        load_availability_snapshot(path, workdata_root=root) for path in paths
    ]
    for loaded in loaded_sources:
        if loaded.schema_version != AVAILABILITY_SCHEMA_V2:
            raise SchemaVersionError("availability merge rejects mixed or v1 schemas")
        if loaded.calendar_content_sha256 is None:
            raise SchemaVersionError(
                "availability merge requires snapshots with a bound calendar"
            )
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    source_snapshots: list[dict[str, Any]] = []
    per_source_keys: list[set[tuple[str, str]]] = []
    for payload, loaded in zip(source_payloads, loaded_sources, strict=True):
        calendar_payload = payload["calendar_evidence"]
        source_snapshots.append(
            {
                "path": loaded.relative_path,
                "file_sha256": loaded.file_sha256,
                "content_sha256": loaded.content_sha256,
                "schema_version": loaded.schema_version,
                "semantic_fingerprint_sha256": payload[
                    "semantic_fingerprint_sha256"
                ],
                "record_count": len(payload["records"]),
                "status_counts": _status_counts(payload["records"]),
                "calendar_evidence": {
                    "path": calendar_payload["path"],
                    "file_sha256": calendar_payload["file_sha256"],
                    "content_sha256": calendar_payload["content_sha256"],
                    "open_dates_sha256": calendar_payload["open_dates_sha256"],
                    "window_start": calendar_payload["window_start"],
                    "window_end": calendar_payload["window_end"],
                },
            }
        )
        keys: set[tuple[str, str]] = set()
        for item in payload["records"]:
            key = _record_key(item)
            if key in seen:
                raise DataQualityError(
                    f"availability snapshots overlap at {key[0]} {key[1]}"
                )
            seen.add(key)
            keys.add(key)
            records.append(dict(item))
        per_source_keys.append(keys)
    records.sort(key=lambda item: (str(item["trade_date"]), str(item["symbol"])))
    window_start, window_end = _window_of(records)
    if official_calendar_path is not None:
        calendar = _bind_calendar_evidence(official_calendar_path, root=root)
        if calendar.authority != "OFFICIAL_TWSE_TPEX":
            raise DataQualityError(
                "official merge input is not a formal official calendar"
            )
        declared = sorted({item.calendar_content_sha256 for item in loaded_sources})
        if declared != [calendar.content_sha256]:
            raise DataQualityError(
                "availability sources are bound to a different calendar than the merge input"
            )
    else:
        if any(item.calendar_authority != "TWII_SECONDARY" for item in loaded_sources):
            raise DataQualityError(
                "official calendar availability requires an official calendar merge input"
            )
        if calendar_source_path is None or calendar_output_dir is None:
            raise DataQualityError(
                "legacy merge requires a secondary calendar source and output directory"
            )
        evidence_path = freeze_exchange_calendar_evidence(
            source_path=calendar_source_path,
            window_start=window_start,
            window_end=window_end,
            output_dir=calendar_output_dir,
            workdata_root=root,
            git_commit=git_commit,
            frozen_at=frozen_at,
        )
        calendar = _bind_calendar_evidence(evidence_path, root=root)
    open_dates = set(calendar.open_dates)
    outside = sorted(
        {
            str(item["trade_date"])
            for item in records
            if date.fromisoformat(str(item["trade_date"])) not in open_dates
        }
    )
    if outside:
        raise DataQualityError(
            f"{len(outside)} merged classification dates fall outside frozen open dates"
        )
    merged_records = [
        _rewrite_calendar_artifact(
            record,
            evidence_relative=calendar.relative_path,
            evidence_file_sha256=calendar.file_sha256,
        )
        for record in records
    ]
    merged_fingerprint = semantic_fingerprint(merged_records)
    for payload, loaded, keys in zip(
        source_payloads, loaded_sources, per_source_keys, strict=True
    ):
        subset = [item for item in merged_records if _record_key(item) in keys]
        if semantic_fingerprint(subset) != payload["semantic_fingerprint_sha256"]:
            raise DataQualityError(
                f"merged records change source semantics: {loaded.relative_path}"
            )
    status_counts = _status_counts(merged_records)
    trading_days = sorted({str(item["trade_date"]) for item in merged_records})
    core: dict[str, Any] = {
        "schema_version": AVAILABILITY_SCHEMA_V2,
        "status": "PASS",
        "calendar_evidence": {
            "path": calendar.relative_path,
            "file_sha256": calendar.file_sha256,
            "content_sha256": calendar.content_sha256,
            "open_dates_sha256": calendar.open_dates_sha256,
            "calendar_id": calendar.calendar_id,
            "window_start": calendar.window_start.isoformat(),
            "window_end": calendar.window_end.isoformat(),
        },
        "calendar_authority": calendar.authority,
        "official_calendar_source_sha256": (
            calendar.content_sha256
            if calendar.authority == "OFFICIAL_TWSE_TPEX"
            else None
        ),
        "coverage": {
            "classified_count": len(merged_records),
            "status_counts": status_counts,
            "unresolved_count": 0,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "trading_day_count": len(trading_days),
            "open_date_count": len(calendar.open_dates),
        },
        "records": merged_records,
        "source_snapshots": sorted(source_snapshots, key=lambda item: item["path"]),
        "semantic_fingerprint_sha256": merged_fingerprint,
        "producer_version": producer_version,
        "git_commit": git_commit,
        "performance_read_or_computed": False,
    }
    digest = canonical_sha256(core)
    payload = {
        **core,
        "snapshot_id": f"dual-ma-causal-availability#{digest[:16]}",
        "content_sha256": digest,
    }
    destination = Path(output_dir).resolve()
    if not destination.is_relative_to(root):
        raise DataQualityError("merged availability output must remain inside workdata")
    path = destination / f"dual-ma-causal-availability#{digest[:16]}.json"
    _immutable_json(path, payload)
    loaded_merged = load_frozen_availability_v2(path, workdata_root=root)
    if (
        loaded_merged.content_sha256 != digest
        or len(loaded_merged.by_symbol_date) != len(merged_records)
    ):
        raise DataQualityError("merged availability readback mismatch")
    return path
