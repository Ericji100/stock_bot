from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import ROOT_DIR

ARTIFACT_REGISTRY_SCHEMA_VERSION = "artifact_registry_v1"
DEFAULT_REGISTRY_ROOT = ROOT_DIR / ".cache" / "artifact_registry"
ARTIFACT_INVENTORY_SCHEMA_VERSION = "artifact_inventory_v1"

DEFAULT_INVENTORY_TARGETS = (
    ".cache",
    "reports",
    "database",
    "data/topic/backup",
    "backups",
    "backup",
)


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    artifact_type: str
    path: str
    schema_version: str
    data_date: str | None
    created_at: str
    source: str
    completeness: float
    expires_at: str | None
    usable: bool
    metadata: dict[str, Any]


def build_artifact_record(
    *,
    artifact_type: str,
    path: str | Path,
    schema_version: str,
    data_date: date | str | None = None,
    source: str = "local",
    completeness: float | int | None = None,
    ttl_days: int | None = None,
    usable: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> ArtifactRecord:
    created = datetime.now().astimezone()
    normalized_path = str(path)
    date_value = data_date.isoformat() if isinstance(data_date, date) else data_date
    completeness_value = _clamp_completeness(completeness)
    expires = (created + timedelta(days=ttl_days)).isoformat(timespec="seconds") if ttl_days else None
    inferred_usable = completeness_value > 0 and bool(normalized_path)
    return ArtifactRecord(
        artifact_id=_artifact_id(artifact_type, normalized_path, date_value),
        artifact_type=artifact_type,
        path=normalized_path,
        schema_version=schema_version,
        data_date=date_value,
        created_at=created.isoformat(timespec="seconds"),
        source=source,
        completeness=completeness_value,
        expires_at=expires,
        usable=inferred_usable if usable is None else bool(usable),
        metadata=metadata or {},
    )


def register_artifact(record: ArtifactRecord, registry_root: Path | None = None) -> Path:
    root = registry_root or DEFAULT_REGISTRY_ROOT
    target = root / f"{_safe(record.artifact_type)}" / f"{record.artifact_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"registry_schema_version": ARTIFACT_REGISTRY_SCHEMA_VERSION, **asdict(record)}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return target


def load_artifact_record(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if data.get("registry_schema_version") != ARTIFACT_REGISTRY_SCHEMA_VERSION:
        data["registry_warning"] = "schema_version_mismatch"
    return data


def is_artifact_usable(record: dict[str, Any] | ArtifactRecord, *, now: datetime | None = None) -> tuple[bool, str]:
    data = asdict(record) if isinstance(record, ArtifactRecord) else record
    if not data.get("usable"):
        return False, "marked_unusable"
    if float(data.get("completeness") or 0) <= 0:
        return False, "empty_or_zero_completeness"
    expires_at = data.get("expires_at")
    if expires_at:
        try:
            expires = datetime.fromisoformat(str(expires_at))
            current = now or datetime.now(expires.tzinfo).astimezone()
            if current > expires:
                return False, "expired"
        except Exception:
            return False, "invalid_expires_at"
    return True, "usable"


def summarize_artifact(record: dict[str, Any] | ArtifactRecord) -> dict[str, Any]:
    data = asdict(record) if isinstance(record, ArtifactRecord) else record
    usable, reason = is_artifact_usable(data)
    return {
        "artifact_id": data.get("artifact_id"),
        "artifact_type": data.get("artifact_type"),
        "schema_version": data.get("schema_version"),
        "data_date": data.get("data_date"),
        "source": data.get("source"),
        "completeness": data.get("completeness"),
        "usable": usable,
        "usable_reason": reason,
        "path": data.get("path"),
    }


def build_artifact_inventory(
    *,
    root_dir: Path | None = None,
    targets: tuple[str, ...] = DEFAULT_INVENTORY_TARGETS,
    include_manual_dirs: bool = True,
    max_depth: int = 3,
) -> dict[str, Any]:
    root = root_dir or ROOT_DIR
    records: list[dict[str, Any]] = []
    for target in targets:
        path = root / target
        if path.exists():
            records.append(_inventory_record(root, path, max_depth=max_depth))
    if include_manual_dirs:
        for path in sorted(root.glob("manual_*")):
            if path.exists():
                records.append(_inventory_record(root, path, max_depth=max_depth))
    usable_count = sum(1 for record in records if record.get("usable"))
    return {
        "schema_version": ARTIFACT_INVENTORY_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": str(root),
        "target_count": len(records),
        "usable_count": usable_count,
        "records": records,
    }


def summarize_artifact_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    records = inventory.get("records") or []
    by_type: dict[str, int] = {}
    total_size = 0
    for record in records:
        artifact_type = str(record.get("artifact_type") or "unknown")
        by_type[artifact_type] = by_type.get(artifact_type, 0) + 1
        total_size += int(record.get("metadata", {}).get("total_size_bytes") or 0)
    return {
        "schema_version": inventory.get("schema_version"),
        "target_count": len(records),
        "usable_count": inventory.get("usable_count", 0),
        "by_type": dict(sorted(by_type.items())),
        "total_size_bytes": total_size,
    }


def _artifact_id(artifact_type: str, path: str, data_date: str | None) -> str:
    import hashlib

    raw = f"{artifact_type}|{path}|{data_date or ''}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{_safe(artifact_type)}_{digest}"


def _safe(value: str) -> str:
    import re

    return re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value)).strip("_")[:80] or "artifact"


def _clamp_completeness(value: float | int | None) -> float:
    if value is None:
        return 1.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _inventory_record(root: Path, path: Path, *, max_depth: int) -> dict[str, Any]:
    rel_path = _relative_path(root, path)
    stats = _path_stats(path, max_depth=max_depth)
    artifact_type = _artifact_type_for_path(rel_path, path)
    completeness = 1.0 if stats["file_count"] > 0 or path.is_file() else 0.0
    record = build_artifact_record(
        artifact_type=artifact_type,
        path=rel_path,
        schema_version=ARTIFACT_INVENTORY_SCHEMA_VERSION,
        data_date=_infer_data_date(path),
        source="artifact_inventory",
        completeness=completeness,
        usable=completeness > 0,
        metadata=stats,
    )
    return summarize_artifact(record) | {
        "metadata": stats,
    }


def _path_stats(path: Path, *, max_depth: int) -> dict[str, Any]:
    if path.is_file():
        stat = path.stat()
        return {
            "kind": "file",
            "file_count": 1,
            "directory_count": 0,
            "total_size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        }
    file_count = 0
    directory_count = 0
    total_size = 0
    latest_mtime = path.stat().st_mtime
    base_depth = len(path.parts)
    for child in path.rglob("*"):
        depth = len(child.parts) - base_depth
        if depth > max_depth:
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        latest_mtime = max(latest_mtime, stat.st_mtime)
        if child.is_file():
            file_count += 1
            total_size += stat.st_size
        elif child.is_dir():
            directory_count += 1
    return {
        "kind": "directory",
        "file_count": file_count,
        "directory_count": directory_count,
        "total_size_bytes": total_size,
        "max_depth": max_depth,
        "modified_at": datetime.fromtimestamp(latest_mtime).astimezone().isoformat(timespec="seconds"),
    }


def _artifact_type_for_path(rel_path: str, path: Path) -> str:
    normalized = rel_path.replace("\\", "/")
    if normalized == ".cache" or normalized.startswith(".cache/"):
        return "cache_directory" if path.is_dir() else "cache_file"
    if normalized == "reports" or normalized.startswith("reports/"):
        return "report_directory" if path.is_dir() else "report_file"
    if normalized == "database" or normalized.startswith("database/"):
        return "database_artifact"
    if normalized.startswith("data/topic/backup"):
        return "topic_backup"
    if normalized.startswith("backups/") or normalized.startswith("backup/"):
        return "backup_artifact"
    if Path(normalized).name.startswith("manual_") or normalized.startswith("manual_"):
        return "manual_artifact"
    return "local_artifact"


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _infer_data_date(path: Path) -> str | None:
    import re

    match = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", path.as_posix())
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
