from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
from typing import Any

import pandas as pd

from candidate_filter_service import hard_filter_display_text, resolve_hard_filter_settings
from chip_strategies import (
    CHIP_STRATEGY_NAMES,
    build_chip_grade_maps,
    build_market_context,
    get_tw_today,
)
from stock_ai_bot.scanning.stock_scanner import StockUniverseEntry, load_recent_revenue_history, scan_tw_market
import stock_ai_bot.scanning.technical_scanner as ts
from stock_ai_bot.telegram.telegram_stock_formatting import mark_stock_text


ROOT_DIR = Path(__file__).resolve().parent
RECENT_SCAN_PATH = ROOT_DIR / ".cache" / "recent_scan_results.json"
CURATED_SCAN_TYPE = "精選選股"
CURATED_SCAN_ALIASES = {CURATED_SCAN_TYPE, "精選選股交叉命中", "curated"}


def _active_curated_scoring_version() -> str:
    path = ROOT_DIR / "config" / "radar_scoring.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        payload = {}
    return str(payload.get("default_version") or "v3").strip().lower()


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
    scores: dict[str, dict[str, Any]]
    report_text: str


def build_curated_scan_result(
    scan_settings: dict[str, float] | None = None,
    report_date: date | None = None,
    *,
    financial_report: Any | None = None,
    chip_context: Any | None = None,
    technical_result: ts.TechnicalScanResult | None = None,
    include_scoring: bool = True,
    historical_replay: bool = False,
) -> CuratedScanResult:
    settings = resolve_hard_filter_settings(scan_settings or {})
    target_date = report_date or get_tw_today()
    financial_report = financial_report or scan_tw_market(
        False,
        None,
        settings,
        report_date=target_date,
        historical_replay=historical_replay,
    )
    chip_context = chip_context or build_market_context(
        False,
        target_date,
        include_daily_data=True,
        scan_settings=settings,
        historical_replay=historical_replay,
    )
    chip_grade_maps = build_chip_grade_maps(chip_context, ["chip_1", "chip_2", "chip_3", "chip_4"])
    technical_result = technical_result or ts.run_technical_scan(
        settings,
        target_date,
        historical_replay=historical_replay,
    )
    technical_signal_codes = _collect_technical_signal_codes(technical_result)
    technical_signal_codes.update(_collect_strategy_signal_codes(technical_result))

    stock_info: dict[str, dict[str, object]] = {}
    hits: dict[str, list[str]] = {}

    for candidate in financial_report.candidates:
        stock_info[candidate.code] = {
            "code": candidate.code,
            "name": candidate.name,
            "symbol": str(getattr(candidate, "symbol", "") or ""),
            "industry": candidate.industry,
            "price": candidate.price,
            "avg_volume_20d": candidate.avg_volume_20d,
            "monthly_revenue": candidate.latest_monthly_revenue,
            "financial_group": candidate.revenue_group,
            "gross_margin_rating": candidate.gross_margin_rating,
            "revenue_history": _normalise_revenue_history(getattr(candidate, "revenue_history", [])),
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
                    "symbol": str(row.get("symbol", "")),
                    "industry": str(row.get("industry", "")),
                    "price": float(row["price"]) if pd.notna(row.get("price")) else None,
                    "avg_volume_20d": float(row["avg_volume_20d"]) if pd.notna(row.get("avg_volume_20d")) else None,
                    "monthly_revenue": float(row["monthly_revenue"]) if pd.notna(row.get("monthly_revenue")) else None,
                    "financial_group": None,
                    "gross_margin_rating": None,
                    "revenue_history": [],
                },
            )

    for strategy_key, grade_map in chip_grade_maps.items():
        strategy_name = CHIP_STRATEGY_NAMES.get(strategy_key, strategy_key)
        for code, grade in grade_map.items():
            hits.setdefault(code, []).append(f"{strategy_name}({grade}級)")

    selected_by_signal: dict[str, list[str]] = {}
    selected_codes: list[str] = []
    seen: set[str] = set()
    for signal in technical_signal_codes:
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
    _backfill_selected_revenue_history(selected_codes, stock_info, target_date)

    scores = (
        _score_curated_candidates(
            target_date=target_date,
            selected_codes=selected_codes,
            selected_by_signal=selected_by_signal,
            stock_info=stock_info,
            hits=hits,
            chip_grade_maps=chip_grade_maps,
            technical_result=technical_result,
        )
        if include_scoring
        else {}
    )
    selected_by_signal = _sort_selected_by_signal(selected_by_signal, hits, scores)
    selected_codes = _ordered_unique(code for codes in selected_by_signal.values() for code in codes)

    report_text = _format_curated_scan_report(
        target_date=target_date,
        selected_by_signal=selected_by_signal,
        selected_codes=selected_codes,
        early_single_signal_candidates=early_single_signal_candidates,
        stock_info=stock_info,
        hits=hits,
        scores=scores,
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
        scores=scores,
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
        if str(record.get("scoring_version") or "").lower() != _active_curated_scoring_version():
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
        if str(record.get("scoring_version") or "").lower() != _active_curated_scoring_version():
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


def _score_curated_candidates(
    *,
    target_date: date,
    selected_codes: list[str],
    selected_by_signal: dict[str, list[str]],
    stock_info: dict[str, dict[str, object]],
    hits: dict[str, list[str]],
    chip_grade_maps: dict[str, dict[str, str]],
    technical_result: ts.TechnicalScanResult,
) -> dict[str, dict[str, Any]]:
    if not selected_codes:
        return {}
    from radar_service import (
        RadarCandidate,
        prepare_radar_scoring_data,
        resolve_radar_scoring_version,
        score_radar_candidates,
    )

    signal_by_code: dict[str, list[str]] = {}
    for signal, codes in selected_by_signal.items():
        for code in codes:
            signal_by_code.setdefault(code, []).append(signal)

    strategy_by_code: dict[str, list[dict[str, Any]]] = {}
    for signals in (getattr(technical_result, "strategy_signals", {}) or {}).values():
        for signal in signals:
            code = str(signal.get("stock_id") or signal.get("code") or "")
            if code:
                strategy_by_code.setdefault(code, []).append(signal)
    dual_ma_by_code: dict[str, list[dict[str, Any]]] = {}
    for signal in getattr(technical_result, "dual_ma_signals", []) or []:
        code = str(signal.get("stock_id") or signal.get("code") or "")
        if code:
            dual_ma_by_code.setdefault(code, []).append(signal)
    kd_ma_by_code: dict[str, list[dict[str, Any]]] = {}
    for signal in getattr(technical_result, "kd_ma_signals", []) or []:
        code = str(signal.get("stock_id") or signal.get("code") or "")
        if code:
            kd_ma_by_code.setdefault(code, []).append(signal)

    candidates: list[RadarCandidate] = []
    for code in selected_codes:
        info = stock_info.get(code, {})
        candidate = RadarCandidate(
            code=code,
            name=str(info.get("name") or ""),
            symbol=str(info.get("symbol") or ""),
            industry=str(info.get("industry") or ""),
            price=_safe_float(info.get("price")),
            source_labels=["精選選股", *signal_by_code.get(code, [])],
            chip_grades={
                key: grade_map[code]
                for key, grade_map in chip_grade_maps.items()
                if code in grade_map
            },
        )
        candidate.technical_signals = [
            {
                "strategy_code": _technical_strategy_code_from_signal(signal),
                "technical_signal_type": "curated_scan_signal",
                "sub_signal_type": signal,
                "signal_date": target_date.isoformat(),
                "notes": signal,
            }
            for signal in signal_by_code.get(code, [])
            if signal in ts.BULLISH_SIGNAL_ORDER
        ]
        candidate.technical_signals.extend(strategy_by_code.get(code, []))
        candidate.strategy_codes.update(
            str(signal.get("strategy_code"))
            for signal in strategy_by_code.get(code, [])
            if signal.get("strategy_code") in {"A", "B", "C", "D"}
        )
        candidate.dual_ma_signals = list(dual_ma_by_code.get(code, []))
        candidate.kd_ma_signals = list(kd_ma_by_code.get(code, []))
        if info.get("financial_group"):
            candidate.source_labels.append(str(info.get("financial_group")))
        candidate.revenue_history = list(info.get("revenue_history") or [])
        candidate.news_items = [{"title": hit} for hit in hits.get(code, [])]
        candidates.append(candidate)

    scoring_version = resolve_radar_scoring_version()
    prepare_radar_scoring_data(
        candidates,
        target_date,
        financial_fetch_limit=len(candidates),
        scoring_version=scoring_version,
    )
    score_radar_candidates(
        candidates,
        target_date,
        scoring_version=scoring_version,
        reason_limit=4,
        risk_limit=3,
    )
    return {
        item.code: {
            "total_score": item.total_score,
            "components": dict(item.score_components),
            "reasons": list(item.key_reasons),
            "risks": list(item.risk_flags),
            "caps": list(item.score_caps_applied),
        }
        for item in candidates
    }


def _technical_strategy_code_from_signal(signal: str) -> str:
    text = str(signal or "")
    for period in ts.MA_BREAKOUT_PERIODS:
        if ts.ma_breakout_signal_label(period) == text or ts.ma_reclaim_signal_label(period) == text:
            return f"MA{period}"
    if "MACD" in text:
        return "MACD"
    if "KD" in text:
        return "KD"
    return "TECHNICAL"


def _backfill_selected_revenue_history(
    selected_codes: list[str],
    stock_info: dict[str, dict[str, object]],
    report_date: date | None = None,
) -> None:
    missing_entries: list[StockUniverseEntry] = []
    for code in selected_codes:
        info = stock_info.get(code) or {}
        if info.get("revenue_history"):
            continue
        symbol = str(info.get("symbol") or "")
        if not symbol:
            continue
        missing_entries.append(
            StockUniverseEntry(
                code=code,
                symbol=symbol,
                market=_market_from_symbol(symbol),
                name=str(info.get("name") or ""),
                industry=str(info.get("industry") or ""),
            )
        )
    if not missing_entries:
        return
    try:
        history_by_code = load_recent_revenue_history(missing_entries, as_of_date=report_date)
    except Exception:
        return
    for code in selected_codes:
        info = stock_info.get(code)
        if not info or info.get("revenue_history"):
            continue
        rows = _normalise_revenue_history(history_by_code.get(code) or [])
        if rows:
            info["revenue_history"] = rows
            latest = rows[-1] if rows else {}
            if info.get("monthly_revenue") in (None, ""):
                info["monthly_revenue"] = latest.get("revenue")


def _normalise_revenue_history(points: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for point in points or []:
        if isinstance(point, dict):
            month = point.get("month") or point.get("Month")
            revenue = point.get("revenue") or point.get("Monthly_Revenue") or point.get("monthly_revenue")
            yoy = point.get("yoy") if "yoy" in point else point.get("YoY") or point.get("YoY%") or point.get("revenue_yoy")
            mom = point.get("mom") if "mom" in point else point.get("MoM") or point.get("MoM%") or point.get("revenue_mom")
            published_at = point.get("published_at") or point.get("Published_At")
            published_at_source = point.get("published_at_source")
        else:
            month = getattr(point, "month", None)
            revenue = getattr(point, "revenue", None)
            yoy = getattr(point, "yoy", None)
            mom = getattr(point, "mom", None)
            published_at = getattr(point, "published_at", None)
            published_at_source = getattr(point, "published_at_source", None)
        if not month:
            continue
        row = {
            "month": str(month),
            "revenue": _safe_float(revenue),
            "yoy": _safe_float(yoy),
        }
        mom_value = _safe_float(mom)
        if mom_value is not None:
            row["mom"] = mom_value
        if published_at:
            row["published_at"] = str(published_at)
        if published_at_source:
            row["published_at_source"] = str(published_at_source)
        rows.append(row)
    return sorted(rows, key=lambda item: str(item.get("month") or ""))


def _market_from_symbol(symbol: str) -> str:
    upper = symbol.upper()
    if upper.endswith(".TWO"):
        return "TPEX"
    if upper.endswith(".TW"):
        return "TWSE"
    return ""


def _sort_selected_by_signal(
    selected_by_signal: dict[str, list[str]],
    hits: dict[str, list[str]],
    scores: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    return {
        signal: sorted(
            codes,
            key=lambda code: (
                -int((scores.get(code) or {}).get("total_score") or 0),
                -len(hits.get(code, [])),
                code,
            ),
        )
        for signal, codes in selected_by_signal.items()
    }


def _ordered_unique(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        code = str(value)
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return result


def _format_curated_scan_report(
    *,
    target_date: date,
    selected_by_signal: dict[str, list[str]],
    selected_codes: list[str],
    early_single_signal_candidates: list[dict[str, Any]],
    stock_info: dict[str, dict[str, object]],
    hits: dict[str, list[str]],
    scores: dict[str, dict[str, Any]],
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
        "篩選邏輯：以技術面正面訊號、MACD 動能策略或雙均線結構為觸發，列出同時命中營收財報或法人大戶 2 個以上策略的股票。",
        "",
    ]

    if not selected_codes:
        lines.append("目前沒有技術面訊號且重複命中的股票。")
    else:
        signals_by_code = _signals_by_code(selected_by_signal)
        lines.extend(["", f"📂 技術訊號精選｜共 {len(selected_codes)} 檔", ""])
        for code in selected_codes:
            info = stock_info.get(code, {})
            code_hits = hits.get(code, [])
            stock_label = mark_stock_text(f"{code} {info.get('name', '')}".strip())
            lines.append(
                (
                    f"{stock_label} | "
                    f"{_format_curated_score_summary(scores.get(code))} | "
                    f"產業：{info.get('industry') or '未分類'} | "
                    f"股價：{_format_compact_price(info.get('price'))} | "
                    f"20日均量：{_format_compact_number(info.get('avg_volume_20d'))} 張 | "
                    f"月營收：{_format_compact_number(info.get('monthly_revenue'))} | "
                    f"命中：{', '.join(code_hits)}"
                )
            )
            lines.append(f"  訊號：{_format_signal_list(signals_by_code.get(code) or [])}")
            extra_lines = _format_curated_score_detail(scores.get(code))
            if extra_lines:
                lines.extend(extra_lines)
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
            stock_label = mark_stock_text(f"{item.get('code')} {info.get('name', '')}".strip())
            lines.append(
                (
                    f"{stock_label} | "
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
            f"法人大戶硬篩標的：{chip_candidate_count} 檔 ({hard_filter_display_text(scan_settings)})",
            f"技術面硬篩標的：{technical_hard_filter_passed} 檔",
            f"技術面訊號命中：{technical_matched_symbols} 檔",
            f"重複命中精選：{len(selected_codes)} 檔",
            f"資料日期：{target_date.isoformat()}",
            f"資料來源：本機快取 / TWSE / TPEX / FinMind / 估算 / {' / '.join(technical_sources)}",
        ]
    )
    return "\n".join(lines).strip()


def _signals_by_code(selected_by_signal: dict[str, list[str]]) -> dict[str, list[str]]:
    signals: dict[str, list[str]] = {}
    ordered_signals = [
        *[signal for signal in ts.BULLISH_SIGNAL_ORDER if signal in selected_by_signal],
        *[signal for signal in selected_by_signal if signal not in ts.BULLISH_SIGNAL_ORDER],
    ]
    for signal in ordered_signals:
        for code in selected_by_signal.get(signal) or []:
            code_signals = signals.setdefault(code, [])
            if signal not in code_signals:
                code_signals.append(signal)
    return signals


def _format_signal_list(signals: list[str]) -> str:
    cleaned = [str(signal).strip() for signal in signals if str(signal).strip()]
    return "、".join(cleaned) if cleaned else "未標示"


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


def _format_curated_score_summary(score: dict[str, Any] | None) -> str:
    if not score:
        return "評分：待補"
    components = score.get("components") if isinstance(score.get("components"), dict) else {}
    return (
        f"{int(score.get('total_score') or 0)}分"
        f"（技術{int(components.get('technical') or 0)}"
        f"/營收{int(components.get('revenue') or 0)}"
        f"/財報{int(components.get('financial') or 0)}"
        f"/籌碼{int(components.get('chip') or 0)}"
        f"/題材{int(components.get('theme') or 0)}"
        f"/族群{int(components.get('sector') or 0)}）"
    )


def _format_curated_score_detail(score: dict[str, Any] | None) -> list[str]:
    if not score:
        return []
    lines: list[str] = []
    reasons = _clean_display_items(score.get("reasons") or [], limit=3)
    risks = _clean_display_items([*(score.get("risks") or []), *(score.get("caps") or [])], limit=2)
    if reasons:
        lines.append(f"  加分：{'、'.join(reasons)}")
    if risks:
        lines.append(f"  風險/缺口：{'、'.join(risks)}")
    return lines


def _clean_display_items(values: list[Any], *, limit: int) -> list[str]:
    cleaned: list[str] = []
    forbidden = ("true", "false", "_", "{", "}", "[", "]")
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if any(token in lowered for token in forbidden):
            continue
        if text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _collect_strategy_signal_codes(result: ts.TechnicalScanResult) -> dict[str, set[str]]:
    signal_codes: dict[str, set[str]] = {}
    for strategy in ("A", "B", "C", "D"):
        for signal in (getattr(result, "strategy_signals", {}) or {}).get(strategy, []):
            code = str(signal.get("stock_id") or signal.get("code") or "")
            if code:
                sub = str(signal.get("sub_signal_type") or "")
                label = ts.STRATEGY_SUB_SIGNAL_LABELS.get(sub)
                signal_codes.setdefault(f"策略 {strategy}｜{label or strategy}", set()).add(code)
    for signal in getattr(result, "dual_ma_signals", []) or []:
        code = str(signal.get("stock_id") or signal.get("code") or "")
        if code:
            signal_codes.setdefault(ts.dual_ma_signal_label(signal), set()).add(code)
    for signal in getattr(result, "kd_ma_signals", []) or []:
        code = str(signal.get("stock_id") or signal.get("code") or "")
        if code:
            signal_codes.setdefault(ts.kd_ma_signal_label(signal), set()).add(code)
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
