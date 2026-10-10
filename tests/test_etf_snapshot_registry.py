from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from stock_ai_bot.data_sources.etf_snapshot_registry import (
    EtfSnapshotRegistryError,
    default_market_data_root,
    resolve_snapshot,
    validate_snapshot,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _fixture(root: Path) -> str:
    snapshot_id = "2005-2026-test"
    package = root / "etf" / "0050" / "snapshots" / snapshot_id / "package"
    data = package / "data" / "0050" / "daily_raw.json"
    _write_json(data, [{"date": "2005-01-03", "close": 40}])
    manifest = package / "artifact_manifest.json"
    _write_json(
        manifest,
        {
            "artifacts": [
                {
                    "relative_path": "data/0050/daily_raw.json",
                    "bytes": data.stat().st_size,
                    "sha256": _sha(data),
                }
            ]
        },
    )
    _write_json(
        root / "etf" / "registry.json",
        {
            "schema_version": "stock-ai-bot-etf-market-data-registry-v1",
            "symbols": {
                "0050": {
                    "current_snapshot_id": snapshot_id,
                    "snapshots": {
                        snapshot_id: {
                            "package_path": f"etf/0050/snapshots/{snapshot_id}/package",
                            "package_manifest": "artifact_manifest.json",
                            "package_manifest_sha256": _sha(manifest),
                        }
                    },
                }
            },
        },
    )
    return snapshot_id


def test_resolve_and_validate_snapshot(tmp_path: Path) -> None:
    snapshot_id = _fixture(tmp_path)
    snapshot = resolve_snapshot("0050", market_data_root=tmp_path)
    assert snapshot.snapshot_id == snapshot_id
    result = validate_snapshot(snapshot)
    assert result["status"] == "PASS"
    assert result["artifacts_verified"] == 1


def test_registry_rejects_path_escape(tmp_path: Path) -> None:
    snapshot_id = _fixture(tmp_path)
    registry_path = tmp_path / "etf" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["symbols"]["0050"]["snapshots"][snapshot_id]["package_path"] = "../escape"
    _write_json(registry_path, registry)
    with pytest.raises(EtfSnapshotRegistryError, match="escapes"):
        resolve_snapshot("0050", market_data_root=tmp_path)


def test_default_root_follows_shared_workdata_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STOCK_AI_BOT_MARKET_DATA_ROOT", raising=False)
    monkeypatch.setenv("STOCK_AI_BOT_WORKDATA_ROOT", str(tmp_path))
    assert default_market_data_root() == (tmp_path / "market-data").resolve()
