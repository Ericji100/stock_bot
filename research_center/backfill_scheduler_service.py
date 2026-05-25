from __future__ import annotations

from datetime import date, datetime
from typing import Any

BACKFILL_PLAN_SCHEMA_VERSION = "backfill_priority_v1"


def build_backfill_priority_plan(
    report_date: date,
    *,
    health: dict[str, Any] | None = None,
    gap_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    health = health or {}
    gap_summary = gap_summary or {}

    _append_health_task(tasks, "technical", health.get("technical"), "warmup_technical_cache")
    _append_health_task(tasks, "revenue", health.get("revenue"), "warmup_revenue_cache")
    _append_health_task(tasks, "financial", health.get("financial"), "warmup_financial_cache")
    _append_health_task(tasks, "chip", health.get("chip"), "warmup_chip_cache")
    _append_health_task(tasks, "tdcc", health.get("tdcc"), "warmup_tdcc_cache")

    for gap in gap_summary.get("priority_gaps") or []:
        if not isinstance(gap, dict):
            continue
        tasks.append({
            "task": gap.get("recommended_action") or "collect_missing_structured_data",
            "source": "data_gap_summary",
            "field": gap.get("field"),
            "priority": gap.get("priority") or "medium",
        })

    tasks = _dedupe_tasks(tasks)
    return {
        "schema_version": BACKFILL_PLAN_SCHEMA_VERSION,
        "report_date": report_date.isoformat(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "task_count": len(tasks),
        "tasks": tasks,
    }


def _append_health_task(tasks: list[dict[str, Any]], name: str, section: Any, action: str) -> None:
    if not isinstance(section, dict):
        return
    coverage = float(section.get("coverage_pct") or 0.0)
    missing = int(section.get("missing_count") or 0)
    if coverage >= 0.95 and missing == 0:
        return
    priority = "high" if coverage < 0.8 else "medium"
    tasks.append({
        "task": action,
        "source": "backfill_health",
        "field": name,
        "priority": priority,
        "coverage_pct": coverage,
        "missing_count": missing,
        "missing_codes": list(section.get("missing_codes") or [])[:30],
    })


def _dedupe_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    rank = {"high": 0, "medium": 1, "low": 2}
    for task in sorted(tasks, key=lambda item: rank.get(str(item.get("priority")), 9)):
        key = (str(task.get("task")), str(task.get("field")))
        if key in seen:
            continue
        seen.add(key)
        result.append(task)
    return result
