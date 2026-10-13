from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


REGISTRY_SCHEMA = "stock-ai-bot-etf-market-data-registry-v1"


class EtfSnapshotRegistryError(ValueError):
    """Raised when an ETF snapshot registry or package is not self-consistent."""


@dataclass(frozen=True)
class EtfSnapshot:
    symbol: str
    snapshot_id: str
    package_root: Path
    package_manifest: Path
    package_manifest_sha256: str
    metadata: Mapping[str, Any]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def default_market_data_root() -> Path:
    configured = os.environ.get("STOCK_AI_BOT_MARKET_DATA_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    workdata_root = os.environ.get("STOCK_AI_BOT_WORKDATA_ROOT")
    if workdata_root:
        return (Path(workdata_root).expanduser().resolve() / "market-data")
    repository_root = Path(__file__).resolve().parents[2]
    return (repository_root / ".workdata" / "market-data").resolve()


def _json_object(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EtfSnapshotRegistryError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, Mapping):
        raise EtfSnapshotRegistryError(f"JSON root is not an object: {path}")
    return value


def _contained_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise EtfSnapshotRegistryError("snapshot path escapes market-data root") from exc
    return candidate


def load_registry(market_data_root: Path | None = None) -> Mapping[str, Any]:
    root = (market_data_root or default_market_data_root()).resolve()
    registry = _json_object(root / "etf" / "registry.json")
    if registry.get("schema_version") != REGISTRY_SCHEMA:
        raise EtfSnapshotRegistryError("unsupported ETF registry schema")
    if not isinstance(registry.get("symbols"), Mapping):
        raise EtfSnapshotRegistryError("ETF registry has no symbols mapping")
    return registry


def resolve_snapshot(
    symbol: str,
    snapshot_id: str | None = None,
    *,
    market_data_root: Path | None = None,
) -> EtfSnapshot:
    root = (market_data_root or default_market_data_root()).resolve()
    registry = load_registry(root)
    normalized_symbol = symbol.strip().upper()
    symbol_row = registry["symbols"].get(normalized_symbol)
    if not isinstance(symbol_row, Mapping):
        raise EtfSnapshotRegistryError(f"ETF symbol is not registered: {normalized_symbol}")
    selected_id = snapshot_id or symbol_row.get("current_snapshot_id")
    if not isinstance(selected_id, str) or not selected_id:
        raise EtfSnapshotRegistryError(f"ETF symbol has no current snapshot: {normalized_symbol}")
    snapshots = symbol_row.get("snapshots")
    row = snapshots.get(selected_id) if isinstance(snapshots, Mapping) else None
    if not isinstance(row, Mapping):
        raise EtfSnapshotRegistryError(
            f"ETF snapshot is not registered: {normalized_symbol}/{selected_id}"
        )
    relative_package = row.get("package_path")
    manifest_name = row.get("package_manifest", "artifact_manifest.json")
    expected_sha = str(row.get("package_manifest_sha256") or "").upper()
    if not isinstance(relative_package, str) or not relative_package:
        raise EtfSnapshotRegistryError("snapshot package_path is missing")
    if len(expected_sha) != 64:
        raise EtfSnapshotRegistryError("snapshot package manifest SHA-256 is invalid")
    package_root = _contained_path(root, relative_package)
    return EtfSnapshot(
        symbol=normalized_symbol,
        snapshot_id=selected_id,
        package_root=package_root,
        package_manifest=package_root / str(manifest_name),
        package_manifest_sha256=expected_sha,
        metadata=row,
    )


def validate_snapshot(snapshot: EtfSnapshot, *, verify_artifacts: bool = True) -> dict[str, Any]:
    if not snapshot.package_root.is_dir():
        raise EtfSnapshotRegistryError(f"snapshot package is missing: {snapshot.package_root}")
    actual_manifest_sha = sha256_file(snapshot.package_manifest)
    if actual_manifest_sha != snapshot.package_manifest_sha256:
        raise EtfSnapshotRegistryError("snapshot package manifest SHA-256 mismatch")
    manifest = _json_object(snapshot.package_manifest)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise EtfSnapshotRegistryError("snapshot artifact manifest has no artifacts list")
    verified = 0
    if verify_artifacts:
        for row in artifacts:
            if not isinstance(row, Mapping):
                raise EtfSnapshotRegistryError("snapshot artifact row is malformed")
            relative_path = row.get("relative_path")
            expected_sha = str(row.get("sha256") or "").upper()
            if not isinstance(relative_path, str) or len(expected_sha) != 64:
                raise EtfSnapshotRegistryError("snapshot artifact identity is malformed")
            artifact = _contained_path(snapshot.package_root, relative_path)
            if not artifact.is_file() or sha256_file(artifact) != expected_sha:
                raise EtfSnapshotRegistryError(
                    f"snapshot artifact failed SHA verification: {relative_path}"
                )
            expected_bytes = row.get("bytes")
            if expected_bytes is not None and artifact.stat().st_size != int(expected_bytes):
                raise EtfSnapshotRegistryError(
                    f"snapshot artifact failed byte-count verification: {relative_path}"
                )
            verified += 1
    return {
        "status": "PASS",
        "symbol": snapshot.symbol,
        "snapshot_id": snapshot.snapshot_id,
        "package_root": str(snapshot.package_root),
        "package_manifest_sha256": actual_manifest_sha,
        "artifact_count": len(artifacts),
        "artifacts_verified": verified,
    }
