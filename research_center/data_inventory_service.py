from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import ROOT_DIR
from .models import CommandRequest


def attach_data_inventory(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    structured_data["data_coverage"] = build_data_inventory(request, structured_data)
    return structured_data


def build_data_inventory(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    required = _required_fields(request.command)
    coverage = []
    for key in required:
        value = structured_data.get(key)
        coverage.append({
            "field": key,
            "available": _has_value(value),
            "count": _value_count(value),
        })
    missing = [row["field"] for row in coverage if not row["available"]]
    return {
        "command": request.command,
        "target": request.target or request.market_scope or request.theme_scope,
        "mode": request.mode,
        "report_date": request.report_date.isoformat() if request.report_date else None,
        "coverage": coverage,
        "missing_fields": missing,
        "status": "complete" if not missing else "partial" if len(missing) < len(required) else "insufficient",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def build_data_status(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    return build_data_inventory(request, structured_data)


def format_data_status(status: dict[str, Any]) -> str:
    lines = [
        f"Data status: {status.get('command')} {status.get('target') or ''}".strip(),
        f"- status: {status.get('status')}",
    ]
    for row in status.get("coverage", []):
        mark = "OK" if row.get("available") else "MISSING"
        lines.append(f"- {row.get('field')}: {mark} ({row.get('count')})")
    return "\n".join(lines)


def build_backfill_status(report_date: date | None = None) -> dict[str, Any]:
    selected = report_date or _latest_backfill_date()
    marker_path = ROOT_DIR / ".cache" / "backfill" / selected.isoformat() / "complete.json" if selected else None
    marker: dict[str, Any] | None = None
    if marker_path and marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            marker = {"error": str(exc)}
    return {
        "report_date": selected.isoformat() if selected else None,
        "marker_exists": bool(marker_path and marker_path.exists()),
        "marker_path": str(marker_path) if marker_path else "",
        "marker": marker or {},
    }


def format_backfill_status(status: dict[str, Any]) -> str:
    marker = status.get("marker") or {}
    health = marker.get("health") if isinstance(marker, dict) else {}
    lines = [
        f"Backfill status: {status.get('report_date') or 'none'}",
        f"- marker_exists: {status.get('marker_exists')}",
    ]
    if isinstance(health, dict):
        for key in (
            "backfill_ready_for_scan",
            "backfill_ready_for_research",
            "chip_coverage_days",
            "chip_candidate_coverage_pct",
            "curated_scan_ready",
        ):
            if key in health:
                lines.append(f"- {key}: {health.get(key)}")
    elif marker:
        for key in ("universe_count", "candidate_count", "curated_scan_count", "research_structured_count"):
            if key in marker:
                lines.append(f"- {key}: {marker.get(key)}")
    return "\n".join(lines)


def _required_fields(command: str) -> list[str]:
    if command == "research":
        return [
            "stock",
            "price_data",
            "institutional_data",
            "revenue_data",
            "financial_data",
            "local_rerating_snapshot",
            "news_context",
            "feature_pack",
        ]
    if command == "value_scan":
        return ["ai_candidates", "ai_candidate_evidence_pack", "news_context", "feature_pack"]
    if command == "macro":
        return ["quantitative_market", "market_score", "news_context", "feature_pack"]
    if command in {"theme", "theme_radar", "theme_flow", "sector_strength"}:
        return ["news_context", "feature_pack"]
    return ["news_context", "feature_pack"]


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, dict, str)):
        return bool(value)
    return True


def _value_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, set, dict, str)):
        return len(value)
    return 1


def _latest_backfill_date() -> date | None:
    root = ROOT_DIR / ".cache" / "backfill"
    if not root.exists():
        return None
    dates: list[date] = []
    for path in root.iterdir():
        if not path.is_dir():
            continue
        try:
            dates.append(date.fromisoformat(path.name))
        except ValueError:
            continue
    return max(dates) if dates else None
