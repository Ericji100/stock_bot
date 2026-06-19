from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifact_registry import DEFAULT_REGISTRY_ROOT, build_artifact_inventory, summarize_artifact_inventory
from .command_runtime_service import GLOBAL_COMMAND_RUNTIME, CommandRuntimeService
from .data_source_gateway import DEFAULT_GATEWAY_SOURCE_NAMES, build_data_source_gateway_snapshot
from .resource_guard_service import DEFAULT_RESOURCE_GUARD, ResourceGuardService

SYSTEM_HEALTH_SCHEMA_VERSION = "system_health_v1"

DEFAULT_SOURCE_NAMES = DEFAULT_GATEWAY_SOURCE_NAMES


def build_system_health_snapshot(
    *,
    runtime: CommandRuntimeService | None = None,
    resource_guard: ResourceGuardService | None = None,
    artifact_registry_root: Path | None = None,
    source_names: tuple[str, ...] = DEFAULT_SOURCE_NAMES,
) -> dict[str, Any]:
    runtime = runtime or GLOBAL_COMMAND_RUNTIME
    resource_guard = resource_guard or DEFAULT_RESOURCE_GUARD
    return {
        "schema_version": SYSTEM_HEALTH_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runtime": _runtime_health(runtime),
        "resources": resource_guard.snapshot(),
        "data_sources": _data_source_health(source_names),
        "artifacts": _artifact_registry_health(artifact_registry_root or DEFAULT_REGISTRY_ROOT),
        "artifact_inventory": summarize_artifact_inventory(build_artifact_inventory()),
    }


def format_system_health_snapshot(snapshot: dict[str, Any]) -> str:
    runtime = snapshot.get("runtime") or {}
    resources = snapshot.get("resources") or {}
    sources = snapshot.get("data_sources") or {}
    quota = sources.get("quota") or {}
    artifacts = snapshot.get("artifacts") or {}
    inventory = snapshot.get("artifact_inventory") or {}
    lines = [
        "【系統健康狀態】",
        f"- 執行中任務：{runtime.get('active_task_count', 0)}",
        f"- 資源池：{_format_resource_pools(resources)}",
        f"- 冷卻來源：{_format_list(sources.get('cooling_sources') or [])}",
        f"- FinMind 安全剩餘額度：{quota.get('finmind_hourly_remaining', 'unknown')}",
        f"- Fugle historical 剩餘額度：{quota.get('fugle_historical_remaining', 'unknown')}/min",
        f"- Artifact Registry 紀錄數：{artifacts.get('record_count', 0)}",
        f"- 本機資料盤點目標：{inventory.get('target_count', 0)}，可用：{inventory.get('usable_count', 0)}",
    ]
    by_type = artifacts.get("by_type") or {}
    if by_type:
        rendered = "、".join(f"{key}={value}" for key, value in sorted(by_type.items()))
        lines.append(f"- Artifact 類型：{rendered}")
    return "\n".join(lines)


def _runtime_health(runtime: CommandRuntimeService) -> dict[str, Any]:
    active = runtime.active_tasks()
    return {
        "active_task_count": len(active),
        "active_tasks": active,
    }


def _data_source_health(source_names: tuple[str, ...]) -> dict[str, Any]:
    return build_data_source_gateway_snapshot(source_names=source_names)


def _artifact_registry_health(registry_root: Path) -> dict[str, Any]:
    if not registry_root.exists():
        return {
            "registry_root": str(registry_root),
            "record_count": 0,
            "by_type": {},
        }
    records = list(registry_root.glob("*/*.json"))
    by_type = Counter(path.parent.name for path in records)
    return {
        "registry_root": str(registry_root),
        "record_count": len(records),
        "by_type": dict(sorted(by_type.items())),
    }


def _format_list(values: list[Any]) -> str:
    return "、".join(str(value) for value in values) if values else "無"


def _format_resource_pools(resources: dict[str, Any]) -> str:
    pools = resources.get("pools") if isinstance(resources, dict) else None
    if not isinstance(pools, dict) or not pools:
        return "無"
    return "、".join(
        f"{name} {int(pool.get('active') or 0)}/{int(pool.get('limit') or 0)}"
        for name, pool in sorted(pools.items())
        if isinstance(pool, dict)
    )
