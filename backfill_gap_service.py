"""Backfill gap analysis utilities.

This module does not fetch network data. It only inspects the data already
loaded or cached by the existing backfill flow and produces a compact health
and gap report.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parent
TECH_CACHE_DIR = ROOT_DIR / ".cache" / "technical_daily"
GROSS_MARGIN_CACHE_PATH = ROOT_DIR / ".cache" / "gross_margin.json"
RESEARCH_STRUCTURED_DIR = ROOT_DIR / ".cache" / "research_structured"

MIN_TECHNICAL_ROWS = 120
TARGET_TDCC_WEEKS = 8


@dataclass
class GapSection:
    name: str
    candidate_count: int = 0
    ready_count: int = 0
    missing_count: int = 0
    coverage_pct: float = 0.0
    missing_codes: list[str] = field(default_factory=list)
    reason_by_code: dict[str, list[str]] = field(default_factory=dict)
    market_coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    last_checked_at: str = ""


def normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:4] if len(digits) >= 4 else digits


def candidates_to_rows(candidates: dict[str, Any] | Iterable[Any]) -> list[dict[str, Any]]:
    if isinstance(candidates, dict):
        values = candidates.values()
    else:
        values = candidates
    rows: list[dict[str, Any]] = []
    for item in values:
        code = normalize_code(getattr(item, "code", None) or (item.get("code") if isinstance(item, dict) else None))
        if not code:
            continue
        rows.append(
            {
                "code": code,
                "name": getattr(item, "name", "") or (item.get("name", "") if isinstance(item, dict) else ""),
                "symbol": getattr(item, "symbol", "") or (item.get("symbol", "") if isinstance(item, dict) else ""),
                "market": getattr(item, "market", "") or (item.get("market", "") if isinstance(item, dict) else ""),
            }
        )
    return rows


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _finalize_section(name: str, candidate_rows: list[dict[str, Any]], reason_by_code: dict[str, list[str]], details: dict[str, Any] | None = None) -> GapSection:
    total = len(candidate_rows)
    missing_codes = sorted(reason_by_code)
    missing = len(missing_codes)
    ready = max(0, total - missing)
    market_coverage: dict[str, dict[str, Any]] = {}
    by_market: dict[str, list[str]] = {}
    for row in candidate_rows:
        market = str(row.get("market") or "UNKNOWN")
        by_market.setdefault(market, []).append(row["code"])
    for market, codes in by_market.items():
        miss = [code for code in codes if code in reason_by_code]
        market_coverage[market] = {
            "candidate_count": len(codes),
            "ready_count": len(codes) - len(miss),
            "missing_count": len(miss),
            "coverage_pct": round((len(codes) - len(miss)) / max(1, len(codes)), 4),
        }
    return GapSection(
        name=name,
        candidate_count=total,
        ready_count=ready,
        missing_count=missing,
        coverage_pct=round(ready / max(1, total), 4),
        missing_codes=missing_codes,
        reason_by_code={code: sorted(set(reasons)) for code, reasons in reason_by_code.items()},
        market_coverage=market_coverage,
        details=details or {},
        last_checked_at=_now_iso(),
    )


def _technical_cache_path(symbol: str) -> Path:
    return TECH_CACHE_DIR / f"{str(symbol).replace('.', '_')}.csv"


def analyze_technical_gaps(candidate_rows: list[dict[str, Any]], report_date: date, min_rows: int = MIN_TECHNICAL_ROWS) -> GapSection:
    reasons: dict[str, list[str]] = {}
    for row in candidate_rows:
        code = row["code"]
        symbol = row.get("symbol") or code
        path = _technical_cache_path(symbol)
        code_reasons: list[str] = []
        if not path.exists():
            code_reasons.append("daily_history_missing")
        else:
            try:
                frame = pd.read_csv(path)
                if frame.empty:
                    code_reasons.append("daily_history_empty")
                if len(frame) < min_rows:
                    code_reasons.append("daily_history_too_short")
                lower_cols = {str(col).lower() for col in frame.columns}
                if "volume" not in lower_cols:
                    code_reasons.append("volume_missing")
                date_col = next((col for col in frame.columns if str(col).lower() in {"date", "datetime"}), None)
                if date_col is None:
                    code_reasons.append("date_missing")
                else:
                    latest = pd.to_datetime(frame[date_col], errors="coerce").dropna()
                    if latest.empty:
                        code_reasons.append("date_unparseable")
                    elif latest.dt.date.max() > report_date:
                        code_reasons.append("future_date_in_cache")
            except Exception:
                code_reasons.append("daily_history_read_failed")
        if code_reasons:
            reasons[code] = code_reasons
    return _finalize_section("technical", candidate_rows, reasons, {"min_rows": min_rows})


def analyze_revenue_gaps(candidate_rows: list[dict[str, Any]], revenue_history: dict[str, Any] | None, min_months: int = 4) -> GapSection:
    revenue_history = revenue_history or {}
    reasons: dict[str, list[str]] = {}
    for row in candidate_rows:
        code = row["code"]
        points = revenue_history.get(code) or []
        code_reasons: list[str] = []
        if not points:
            code_reasons.append("recent_monthly_revenue_missing")
        if len(points) < min_months:
            code_reasons.append("monthly_revenue_history_too_short")
        if points:
            latest = points[0]
            if getattr(latest, "revenue", None) is None:
                code_reasons.append("latest_revenue_missing")
            if getattr(latest, "yoy", None) is None:
                code_reasons.append("revenue_yoy_missing")
        if code_reasons:
            reasons[code] = code_reasons
    return _finalize_section("revenue", candidate_rows, reasons, {"min_months": min_months})


def _load_gross_margin_metrics() -> dict[str, Any]:
    if not GROSS_MARGIN_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(GROSS_MARGIN_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    metrics = payload.get("metrics")
    return metrics if isinstance(metrics, dict) else {}


def analyze_financial_gaps(candidate_rows: list[dict[str, Any]], min_quarters: int = 3) -> GapSection:
    metrics = _load_gross_margin_metrics()
    reasons: dict[str, list[str]] = {}
    for row in candidate_rows:
        code = row["code"]
        symbol = row.get("symbol") or code
        item = metrics.get(symbol) or metrics.get(code) or {}
        series = item.get("series") if isinstance(item, dict) else []
        code_reasons: list[str] = []
        if not series:
            code_reasons.append("gross_margin_missing")
        elif len(series) < min_quarters:
            code_reasons.append("gross_margin_history_too_short")
        if code_reasons:
            reasons[code] = code_reasons
    return _finalize_section("financial", candidate_rows, reasons, {"min_quarters": min_quarters})


def analyze_chip_gaps(candidate_rows: list[dict[str, Any]], daily_data: pd.DataFrame | None, target_days: int) -> GapSection:
    reasons: dict[str, list[str]] = {}
    details: dict[str, Any] = {"target_days": target_days, "source_counts": {}}
    if daily_data is None or daily_data.empty:
        return _finalize_section(
            "chip",
            candidate_rows,
            {row["code"]: ["institutional_daily_missing"] for row in candidate_rows},
            details,
        )

    frame = daily_data.copy()
    if "code" not in frame.columns:
        return _finalize_section(
            "chip",
            candidate_rows,
            {row["code"]: ["code_column_missing"] for row in candidate_rows},
            details,
        )
    frame["code"] = frame["code"].map(normalize_code)
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    if "source" in frame.columns:
        source_counts: dict[str, int] = {}
        for raw in frame["source"].dropna().astype(str):
            for source in raw.replace("/", "+").split("+"):
                source = source.strip()
                if source:
                    source_counts[source] = source_counts.get(source, 0) + 1
        details["source_counts"] = source_counts

    for row in candidate_rows:
        code = row["code"]
        subset = frame[frame["code"] == code]
        code_reasons: list[str] = []
        if subset.empty:
            code_reasons.append("institutional_daily_missing")
        else:
            unique_days = subset["date"].dropna().nunique() if "date" in subset.columns else 0
            if unique_days < target_days:
                code_reasons.append("institutional_days_too_short")
            if "foreign_net_lots" not in subset.columns or subset["foreign_net_lots"].notna().sum() == 0:
                code_reasons.append("foreign_net_missing")
            if "trust_net_lots" not in subset.columns or subset["trust_net_lots"].notna().sum() == 0:
                code_reasons.append("trust_net_missing")
            if "foreign_ratio_pct" not in subset.columns or subset["foreign_ratio_pct"].notna().sum() == 0:
                code_reasons.append("foreign_ratio_missing")
        if code_reasons:
            reasons[code] = code_reasons
    return _finalize_section("chip", candidate_rows, reasons, details)


def analyze_tdcc_gaps(candidate_rows: list[dict[str, Any]], weekly_data: pd.DataFrame | None, min_weeks: int = TARGET_TDCC_WEEKS) -> GapSection:
    reasons: dict[str, list[str]] = {}
    if weekly_data is None or weekly_data.empty or "code" not in weekly_data.columns:
        return _finalize_section("tdcc", candidate_rows, {row["code"]: ["tdcc_weekly_missing"] for row in candidate_rows}, {"min_weeks": min_weeks})
    frame = weekly_data.copy()
    frame["code"] = frame["code"].map(normalize_code)
    for row in candidate_rows:
        code = row["code"]
        subset = frame[frame["code"] == code]
        code_reasons: list[str] = []
        if subset.empty:
            code_reasons.append("tdcc_weekly_missing")
        elif len(subset) < min_weeks:
            code_reasons.append("tdcc_weeks_too_short")
        if code_reasons:
            reasons[code] = code_reasons
    return _finalize_section("tdcc", candidate_rows, reasons, {"min_weeks": min_weeks})


def analyze_research_structured_gaps(core_rows: list[dict[str, Any]], report_date: date) -> GapSection:
    reasons: dict[str, list[str]] = {}
    folder = RESEARCH_STRUCTURED_DIR / report_date.strftime("%Y%m%d")
    for row in core_rows:
        code = row["code"]
        path = folder / f"{code}.json"
        code_reasons: list[str] = []
        if not path.exists():
            code_reasons.append("research_structured_cache_missing")
        else:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, dict):
                    code_reasons.append("research_structured_cache_invalid")
                else:
                    required_keys: tuple[str | tuple[str, ...], ...] = (
                        "stock",
                        ("revenue", "revenue_data"),
                        "chip_backup_data",
                        "free_public_sources",
                    )
                    for key in required_keys:
                        if isinstance(key, tuple):
                            if not any(candidate_key in data for candidate_key in key):
                                code_reasons.append(f"{key[0]}_missing")
                        elif key not in data:
                            code_reasons.append(f"{key}_missing")
            except Exception:
                code_reasons.append("research_structured_cache_read_failed")
        if code_reasons:
            reasons[code] = code_reasons
    return _finalize_section("research_structured", core_rows, reasons)


def build_backfill_gap_report(
    *,
    report_date: date,
    candidates: dict[str, Any] | Iterable[Any],
    core_pool: dict[str, Any] | Iterable[Any],
    revenue_history: dict[str, Any] | None = None,
    chip_context: Any | None = None,
    priority_codes: Iterable[str] | None = None,
) -> dict[str, Any]:
    candidate_rows = candidates_to_rows(candidates)
    core_rows = candidates_to_rows(core_pool)
    priority_code_set = {normalize_code(code) for code in (priority_codes or [])}
    priority_rows = [row for row in candidate_rows if row["code"] in priority_code_set]
    target_days = int(getattr(chip_context, "scan_settings", {}).get("target_trading_days", 60)) if chip_context is not None else 60
    sections = {
        "technical": analyze_technical_gaps(candidate_rows, report_date),
        "revenue": analyze_revenue_gaps(candidate_rows, revenue_history),
        "financial": analyze_financial_gaps(candidate_rows),
        "chip": analyze_chip_gaps(candidate_rows, getattr(chip_context, "daily_data", None), target_days),
        "tdcc": analyze_tdcc_gaps(candidate_rows, getattr(chip_context, "weekly_data", None)),
        "research_structured": analyze_research_structured_gaps(core_rows, report_date),
    }
    health = {key: asdict(value) for key, value in sections.items()}
    priority_health: dict[str, Any] = {}
    if priority_rows:
        priority_sections = {
            "chip": analyze_chip_gaps(priority_rows, getattr(chip_context, "daily_data", None), target_days),
            "tdcc": analyze_tdcc_gaps(priority_rows, getattr(chip_context, "weekly_data", None)),
        }
        priority_health = {key: asdict(value) for key, value in priority_sections.items()}
    still_missing: dict[str, list[str]] = {}
    reason_by_code: dict[str, dict[str, list[str]]] = {}
    for key, section in health.items():
        still_missing[key] = section.get("missing_codes", [])
        for code, reasons in section.get("reason_by_code", {}).items():
            reason_by_code.setdefault(code, {})[key] = reasons
    return {
        "schema_version": 1,
        "report_date": report_date.isoformat(),
        "generated_at": _now_iso(),
        "candidate_count": len(candidate_rows),
        "core_research_count": len(core_rows),
        "priority_pool": {
            "candidate_count": len(priority_rows),
            "codes": [row["code"] for row in priority_rows],
        },
        "health": health,
        "priority_health": priority_health,
        "still_missing": still_missing,
        "reason_by_code": reason_by_code,
    }


def write_gap_report(report_date: date, gap_report: dict[str, Any], marker_root: Path) -> Path:
    folder = marker_root / report_date.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "gaps.json"
    path.write_text(json.dumps(gap_report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
