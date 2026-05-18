from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
from typing import Any

import pandas as pd

from chip_strategies import (
    CHIP_STRATEGY_NAMES,
    build_chip_grade_maps,
    build_market_context,
    get_tw_today,
)
from stock_scanner import scan_tw_market
import technical_scanner as ts


ROOT_DIR = Path(__file__).resolve().parent
RECENT_SCAN_PATH = ROOT_DIR / ".cache" / "recent_scan_results.json"
CURATED_SCAN_TYPE = "精選選股"
CURATED_SCAN_ALIASES = {CURATED_SCAN_TYPE, "精選選股交叉命中", "curated"}


def _is_backfill_ready_for_scan(report_date: date) -> bool:
    marker = ROOT_DIR / ".cache" / "backfill" / report_date.isoformat() / "complete.json"
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return False
    # Accept schema v2 marker with explicit flag
    if data.get("schema_version") != 2:
        return False
    return bool(data.get("backfill_ready_for_scan"))


@dataclass(frozen=True)
class CuratedScanResult:
    report_date: date
    selected_codes: list[str]
    selected_by_signal: dict[str, list[str]]
    stock_info: dict[str, dict[str, object]]
    hits: dict[str, list[str]]
    report_text: str


def build_curated_scan_result(
    scan_settings: dict[str, float] | None = None,
    report_date: date | None = None,
) -> CuratedScanResult:
    settings = scan_settings or {}
    target_date = report_date or get_tw_today()
    financial_report = scan_tw_market(False, None, settings)
    chip_context = build_market_context(False, target_date, include_daily_data=True)
    chip_grade_maps = build_chip_grade_maps(chip_context, ["chip_1", "chip_2", "chip_3", "chip_4"])
    technical_result = ts.run_technical_scan(settings, target_date)
    technical_signal_codes = _collect_technical_signal_codes(technical_result)

    stock_info: dict[str, dict[str, object]] = {}
    hits: dict[str, list[str]] = {}

    for candidate in financial_report.candidates:
        stock_info[candidate.code] = {
            "code": candidate.code,
            "name": candidate.name,
            "industry": candidate.industry,
            "price": candidate.price,
            "avg_volume_20d": candidate.avg_volume_20d,
            "monthly_revenue": candidate.latest_monthly_revenue,
            "financial_group": candidate.revenue_group,
            "gross_margin_rating": candidate.gross_margin_rating,
        }
        hits.setdefault(candidate.code, []).append(
            _financial_hit_label(candidate.revenue_group, candidate.gross_margin_rating)
        )

    if not chip_context.candidates.empty:
        for _, row in chip_context.candidates.iterrows():
            code = str(row["code"])
            stock_info.setdefault(
                code,
                {
                    "code": code,
                    "name": str(row.get("name", "")),
                    "industry": str(row.get("industry", "")),
                    "price": float(row["price"]) if pd.notna(row.get("price")) else None,
                    "avg_volume_20d": float(row["avg_volume_20d"]) if pd.notna(row.get("avg_volume_20d")) else None,
                    "monthly_revenue": float(row["monthly_revenue"]) if pd.notna(row.get("monthly_revenue")) else None,
                    "financial_group": None,
                    "gross_margin_rating": None,
                },
            )

    for strategy_key, grade_map in chip_grade_maps.items():
        strategy_name = CHIP_STRATEGY_NAMES.get(strategy_key, strategy_key)
        for code, grade in grade_map.items():
            hits.setdefault(code, []).append(f"{strategy_name}({grade}級)")

    selected_by_signal: dict[str, list[str]] = {}
    selected_codes: list[str] = []
    seen: set[str] = set()
    for signal in ts.BULLISH_SIGNAL_ORDER:
        signal_codes = technical_signal_codes.get(signal, set())
        codes = [code for code in signal_codes if len(hits.get(code, [])) >= 2]
        codes.sort(
            key=lambda code: (
                -len(hits.get(code, [])),
                stock_info.get(code, {}).get("industry") or "",
                code,
            )
        )
        if codes:
            selected_by_signal[signal] = codes
            for code in codes:
                if code not in seen:
                    seen.add(code)
                    selected_codes.append(code)

    report_text = _format_curated_scan_report(
        target_date=target_date,
        selected_by_signal=selected_by_signal,
        selected_codes=selected_codes,
        stock_info=stock_info,
        hits=hits,
        financial_candidate_count=len(financial_report.candidates),
        chip_candidate_count=len(chip_context.candidates),
        technical_hard_filter_passed=technical_result.hard_filter_passed,
        technical_matched_symbols=technical_result.matched_symbols,
        technical_sources=sorted(technical_result.sources),
        scan_settings=chip_context.scan_settings,
    )
    return CuratedScanResult(
        report_date=target_date,
        selected_codes=selected_codes,
        selected_by_signal=selected_by_signal,
        stock_info=stock_info,
        hits=hits,
        report_text=report_text,
    )


def build_curated_scan_report(
    scan_settings: dict[str, float] | None = None,
    report_date: date | None = None,
) -> str:
    return build_curated_scan_result(scan_settings, report_date).report_text


def find_cached_curated_scan(report_date: date) -> dict[str, Any] | None:
    target = report_date.isoformat()
    # Only trust cached curated scan if backfill marker indicates readiness
    try:
        if not _is_backfill_ready_for_scan(report_date):
            return None
    except Exception:
        return None
    for record in _load_recent_scan_results(limit=30):
        if str(record.get("scan_type") or "") not in CURATED_SCAN_ALIASES:
            continue
        if str(record.get("report_date") or "") != target:
            continue
        if record.get("selected_codes"):
            codes = _normalise_codes(record.get("selected_codes") or [])
        else:
            codes = _extract_curated_codes_from_summary(str(record.get("summary") or ""))
            if not codes:
                codes = _normalise_codes(record.get("codes") or [])
        if not codes:
            continue
        return {**record, "codes": codes}
    return None


def _load_recent_scan_results(limit: int = 30) -> list[dict[str, Any]]:
    if not RECENT_SCAN_PATH.exists():
        return []
    try:
        data = json.loads(RECENT_SCAN_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    return [item for item in data if isinstance(item, dict)][:limit]


def _format_curated_scan_report(
    *,
    target_date: date,
    selected_by_signal: dict[str, list[str]],
    selected_codes: list[str],
    stock_info: dict[str, dict[str, object]],
    hits: dict[str, list[str]],
    financial_candidate_count: int,
    chip_candidate_count: int,
    technical_hard_filter_passed: int,
    technical_matched_symbols: int,
    technical_sources: list[str],
    scan_settings: dict[str, Any],
) -> str:
    lines = [
        "⭐ 精選選股交叉命中報告",
        f"📅 日期：{target_date.isoformat()}",
        "",
        "篩選邏輯：以技術面正面訊號為主要分類，列出同時命中營收財報或法人大戶 2 個以上策略的股票。",
        "",
    ]

    if not selected_by_signal:
        lines.append("目前沒有技術面訊號且重複命中的股票。")
    else:
        for signal in ts.BULLISH_SIGNAL_ORDER:
            codes = selected_by_signal.get(signal)
            if not codes:
                continue
            lines.extend(["", f"📂 {signal}", ""])
            current_hit_count: int | None = None
            for code in codes:
                info = stock_info.get(code, {})
                code_hits = hits.get(code, [])
                hit_count = len(code_hits)
                if current_hit_count != hit_count:
                    current_hit_count = hit_count
                    lines.extend(["", f"【命中 {hit_count} 個策略】", ""])
                lines.append(
                    (
                        f"{code} {info.get('name', '')} | "
                        f"產業：{info.get('industry') or '未分類'} | "
                        f"股價：{_format_compact_price(info.get('price'))} | "
                        f"20日均量：{_format_compact_number(info.get('avg_volume_20d'))} 張 | "
                        f"月營收：{_format_compact_number(info.get('monthly_revenue'))} | "
                        f"命中：{', '.join(code_hits)}"
                    )
                )
                lines.append("")

    lines.extend(
        [
            "",
            "掃描統計",
            f"營收財報選股命中：{financial_candidate_count} 檔",
            f"法人大戶硬篩標的：{chip_candidate_count} 檔 (股價 {int(scan_settings['min_price'])}~{int(scan_settings['max_price'])} / 均量 > {int(scan_settings['min_avg_volume_20d'])})",
            f"技術面硬篩標的：{technical_hard_filter_passed} 檔",
            f"技術面訊號命中：{technical_matched_symbols} 檔",
            f"重複命中精選：{len(selected_codes)} 檔",
            f"資料日期：{target_date.isoformat()}",
            f"資料來源：本機快取 / TWSE / TPEX / FinMind / 估算 / {' / '.join(technical_sources)}",
        ]
    )
    return "\n".join(lines).strip()


def _format_compact_number(value: object) -> str:
    if value is None:
        return "無資料"
    number = float(value)
    if abs(number) >= 100_000_000:
        return f"{number / 100_000_000:.2f}億"
    if abs(number) >= 10_000:
        return f"{number / 10_000:.2f}萬"
    return f"{number:,.0f}"


def _format_compact_price(value: object) -> str:
    if value is None:
        return "無資料"
    return f"{float(value):,.2f}".rstrip("0").rstrip(".")


def _financial_hit_label(revenue_group: str, gross_margin_rating: str) -> str:
    group_label = {"group_1": "G1營收連續成長", "group_2": "G2營收轉強"}.get(revenue_group, revenue_group)
    rating_label = {
        "A": "毛利率A",
        "B": "毛利率B",
        "C": "毛利率C",
        "D": "毛利率D",
    }.get(gross_margin_rating, gross_margin_rating)
    return f"營收財報選股({group_label}/{rating_label})"


def _collect_technical_signal_codes(result: ts.TechnicalScanResult) -> dict[str, set[str]]:
    signal_codes: dict[str, set[str]] = {}
    for signal in ts.BULLISH_SIGNAL_ORDER:
        industries = result.bullish.get(signal)
        if not industries:
            continue
        codes: set[str] = set()
        for displays in industries.values():
            for display in displays:
                code = str(display).strip().split(maxsplit=1)[0]
                if code:
                    codes.add(code)
        if codes:
            signal_codes[signal] = codes
    return signal_codes


def _normalise_codes(values: list[Any]) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for value in values:
        code = str(value).strip()
        if not code or code in seen:
            continue
        if not code.isdigit() or len(code) != 4:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _extract_curated_codes_from_summary(text: str) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped[:4].isdigit():
            continue
        parts = stripped.split(maxsplit=1)
        if not parts:
            continue
        code = parts[0]
        if len(code) != 4 or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes
