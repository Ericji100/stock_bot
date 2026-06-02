from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import CommandRequest


CONFIDENCE_SCHEMA_VERSION = "report_confidence_v1"


def build_report_confidence(
    request: CommandRequest,
    *,
    ai_input_audit: dict[str, Any],
) -> dict[str, Any]:
    source = ai_input_audit.get("source_coverage") or {}
    structured = ai_input_audit.get("structured_coverage") or {}
    score = 0
    reasons: list[str] = []
    warnings: list[str] = []

    official = int(source.get("official_sources") or 0)
    media = int(source.get("media_sources") or 0)
    risk = int(source.get("risk_or_counter_sources") or 0)
    dated = int(source.get("dated_sources") or 0)
    total = int(source.get("total_sources") or 0)
    coverage_ratio = float(structured.get("coverage_ratio") or 0)

    score += min(25, official * 8)
    score += min(20, media * 3)
    score += min(15, risk * 5)
    score += int(coverage_ratio * 30)
    score += 10 if total >= _target_source_count(request) else max(0, int(total / max(1, _target_source_count(request)) * 10))
    if total and dated / total >= 0.7:
        score += 10
    elif total:
        warnings.append("部分來源缺少可驗證日期")

    if official:
        reasons.append(f"官方來源 {official} 筆")
    else:
        warnings.append("官方來源不足")
    if risk:
        reasons.append(f"反證或風險來源 {risk} 筆")
    else:
        warnings.append("反證來源不足")
    if coverage_ratio >= 0.75:
        reasons.append("結構化資料覆蓋度良好")
    else:
        warnings.append("結構化資料覆蓋度不足")

    score = max(0, min(100, score))
    level = _confidence_level(score, warnings)
    return {
        "schema_version": CONFIDENCE_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "confidence_score": score,
        "confidence_level": level,
        "confidence_label": _confidence_label(level),
        "reasons": reasons,
        "warnings": warnings,
        "policy": "可信度由本地依來源品質、反證覆蓋、資料完整度與日期可靠性計算；AI 可引用但不得改寫為更高可信度。",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _target_source_count(request: CommandRequest) -> int:
    if request.command == "research":
        return 14 if request.mode == "deep" else 8
    if request.command == "value_scan":
        return 20 if request.mode == "deep" else 12
    if request.command in {"theme", "theme_radar", "theme_flow", "sector_strength", "macro"}:
        return 16 if request.mode == "deep" else 10
    return 6


def _confidence_level(score: int, warnings: list[str]) -> str:
    if score >= 80 and len(warnings) <= 1:
        return "high"
    if score >= 60:
        return "medium"
    if score >= 40:
        return "low"
    return "insufficient"


def _confidence_label(level: str) -> str:
    return {
        "high": "高可信",
        "medium": "中可信",
        "low": "低可信",
        "insufficient": "資料不足",
    }.get(level, "資料不足")
