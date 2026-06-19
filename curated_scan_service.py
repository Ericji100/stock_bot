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


def _is_curated_cache_ready(report_date: date) -> bool:
    marker = ROOT_DIR / ".cache" / "backfill" / report_date.isoformat() / "complete.json"
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return False
    if data.get("schema_version") != 2:
        return False
    return bool(
        data.get("curated_scan_cache_ready")
        or data.get("curated_scan_ready")
        or data.get("backfill_ready_for_scan")
    )


@dataclass(frozen=True)
class CuratedScanResult:
    report_date: date
    selected_codes: list[str]
    selected_by_signal: dict[str, list[str]]
    early_single_signal_candidates: list[dict[str, Any]]
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

    early_single_signal_candidates = _build_early_single_signal_candidates(
        selected_codes=selected_codes,
        stock_info=stock_info,
        hits=hits,
        technical_signal_codes=technical_signal_codes,
    )

    report_text = _format_curated_scan_report(
        target_date=target_date,
        selected_by_signal=selected_by_signal,
        selected_codes=selected_codes,
        early_single_signal_candidates=early_single_signal_candidates,
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
        early_single_signal_candidates=early_single_signal_candidates,
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
    # Reading an existing curated list only requires the curated cache marker.
    # Full scan readiness is checked by callers that rebuild scan data.
    try:
        if not _is_curated_cache_ready(report_date):
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


def find_latest_cached_curated_scan(max_date: date | None = None, limit: int = 500) -> dict[str, Any] | None:
    """Return the latest backfill-ready curated scan not newer than max_date."""

    best_record: dict[str, Any] | None = None
    best_date: date | None = None
    for record in _load_recent_scan_results(limit=limit):
        if str(record.get("scan_type") or "") not in CURATED_SCAN_ALIASES:
            continue
        report_date_text = str(record.get("report_date") or "")
        try:
            record_date = date.fromisoformat(report_date_text)
        except ValueError:
            continue
        if max_date is not None and record_date > max_date:
            continue
        try:
            if not _is_curated_cache_ready(record_date):
                continue
        except Exception:
            continue
        if record.get("selected_codes"):
            codes = _normalise_codes(record.get("selected_codes") or [])
        else:
            codes = _extract_curated_codes_from_summary(str(record.get("summary") or ""))
            if not codes:
                codes = _normalise_codes(record.get("codes") or [])
        if not codes:
            continue
        if best_date is None or record_date > best_date:
            best_date = record_date
            best_record = {**record, "codes": codes}
    return best_record


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
    early_single_signal_candidates: list[dict[str, Any]],
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

    if early_single_signal_candidates:
        lines.extend(
            [
                "",
                "早期單點異動觀察（未交叉確認）",
                "以下股票只代表劇本開端線索，尚未達成精選選股交叉命中；後續需觀察籌碼、營收、題材催化或反證。",
                "",
            ]
        )
        for item in early_single_signal_candidates[:20]:
            info = stock_info.get(str(item.get("code") or ""), {})
            lines.append(
                (
                    f"{item.get('code')} {info.get('name', '')} | "
                    f"{item.get('early_type')} | "
                    f"訊號：{', '.join(item.get('signals') or [])} | "
                    f"待驗證：{', '.join(item.get('validation_needed') or [])}"
                )
            )

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


def _build_early_single_signal_candidates(
    *,
    selected_codes: list[str],
    stock_info: dict[str, dict[str, object]],
    hits: dict[str, list[str]],
    technical_signal_codes: dict[str, set[str]],
) -> list[dict[str, Any]]:
    selected = set(selected_codes)
    by_code: dict[str, dict[str, Any]] = {}

    def add(code: str, early_type: str, signal: str, validation_needed: list[str]) -> None:
        if not code or code in selected:
            return
        item = by_code.setdefault(
            code,
            {
                "code": code,
                "early_type": early_type,
                "signals": [],
                "validation_needed": [],
                "hit_count": len(hits.get(code, [])),
            },
        )
        if signal and signal not in item["signals"]:
            item["signals"].append(signal)
        for need in validation_needed:
            if need not in item["validation_needed"]:
                item["validation_needed"].append(need)
        item["hit_count"] = max(item["hit_count"], len(hits.get(code, [])))

    for signal, codes in technical_signal_codes.items():
        for code in codes:
            if len(hits.get(code, [])) < 2:
                add(code, "技術先動型", signal, ["營收斜率", "法人/大戶籌碼", "題材催化"])

    for code, code_hits in hits.items():
        if len(code_hits) == 1:
            hit = code_hits[0]
            early_type = "營收先動型" if "營收" in hit else "籌碼先動型"
            add(code, early_type, hit, ["技術型態", "題材劇本", "反證/失效條件"])

    def sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
        info = stock_info.get(str(item.get("code") or ""), {})
        return (-int(item.get("hit_count") or 0), str(info.get("industry") or ""), str(item.get("code") or ""))

    return sorted(by_code.values(), key=sort_key)[:30]


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
