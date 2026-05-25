from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import curated_scan_service
import technical_scanner as ts
from chip_strategies import build_chip_grade_maps, build_market_context, get_tw_today, is_possible_trading_day
from monitor_service import get_monitor_stocks
from portfolio_manager import list_portfolio
from research_center.config import load_research_config
from research_center.date_aware_context import (
    augment_discovery_tasks_with_date_context,
    filter_and_sort_sources_for_analysis_date,
    parse_date_like,
)
from research_center.models import CommandRequest, SourceItem
from research_center.orchestrator import ResearchCenter
from research_center.news_repository import NewsRepository
from research_center.recent_scans import load_recent_scan_results, save_recent_scan_result
from research_center.web_fetch_enrichment import _enrich_sources_with_web_fetch
from research_center.tavily_search_service import TavilyQuotaError, TavilySearchService
from stock_scanner import load_recent_revenue_history, load_stock_universe, scan_tw_market


ROOT_DIR = Path(__file__).resolve().parent
RADAR_CACHE_PATH = ROOT_DIR / ".cache" / "radar_results.json"
DEFAULT_SOURCE = "technical"
DEFAULT_AI_TOP = 5
MAIN_SOURCES = {"technical", "curated", "financial", "chip", "monitor", "portfolio"}
CHIP_KEYS = ["chip_1", "chip_2", "chip_3", "chip_4"]
TECHNICAL_STRATEGY_LABELS = {
    "A": "多頭延續回檔突破",
    "B": "強勢紅柱回測突破",
    "C": "低檔背離反轉突破",
    "D": "強勢股急跌收復",
}
TECHNICAL_SUB_SIGNAL_LABELS = {
    "A1_direct_ma21_breakout": "A1 直接突破 21MA",
    "A2_pivot_low_reclaim_ma21": "A2 低點墊高後收復 21MA",
    "A3_reclaim_ma21_and_long_ma": "A3 同日收復 21MA 與長均線",
    "B1_intraday_retest_reclaim_ma": "B1 盤中回測後收復 MA13/MA21",
    "B2_short_reclaim_after_break_ma": "B2 跌破後收復短均線",
    "B3_breakout_after_retest": "B3 回測 MA13/MA21 後突破前高",
    "C1_macd_bullish_divergence_break_ma21": "C1 MACD 低檔背離突破 21MA",
    "C2_below_zero_red_histogram_breakout": "C2 零軸下紅柱鈍化突破",
    "D1_reclaim_ma_after_break": "D1 跌破後收復均線",
    "D2_macd_high_column_flip_green": "D2 MACD 高檔紅柱翻綠後快速轉強",
    "D3_kd_death_cross_quick_reversal": "D3 KD 死叉後快速轉強",
    "D4_hammer_candle_reclaim": "D4 急跌或長下影後收復均線",
}
CHIP_STRATEGY_LABELS = {
    "chip_1": "60日法人動態",
    "chip_2": "投信認養",
    "chip_3": "法人持股比例增加",
    "chip_4": "每週大戶持股",
}


@dataclass(frozen=True)
class RadarRequest:
    source: str = DEFAULT_SOURCE
    report_date: date | None = None
    ai_top: int = DEFAULT_AI_TOP
    model: str | None = None
    ai_comment_enabled: bool = True


@dataclass
class RadarCandidate:
    code: str
    name: str = ""
    symbol: str = ""
    industry: str = ""
    price: float | None = None
    source_labels: list[str] = field(default_factory=list)
    strategy_codes: set[str] = field(default_factory=set)
    technical_signals: list[dict[str, Any]] = field(default_factory=list)
    chip_grades: dict[str, str] = field(default_factory=dict)
    news_items: list[dict[str, Any]] = field(default_factory=list)
    web_sources: list[dict[str, Any]] = field(default_factory=list)
    ai_sources: list[dict[str, Any]] = field(default_factory=list)
    ai_comment: dict[str, Any] = field(default_factory=dict)
    score_components: dict[str, int] = field(default_factory=dict)
    total_score: int = 0


@dataclass(frozen=True)
class RadarResult:
    request: RadarRequest
    report_date: date
    candidates: list[RadarCandidate]
    ai_enriched_codes: list[str]
    diagnostics: dict[str, Any]


def parse_radar_args(args: list[str] | tuple[str, ...] | None) -> RadarRequest:
    values = list(args or [])
    source = DEFAULT_SOURCE
    report_date: date | None = None
    ai_top = DEFAULT_AI_TOP
    model: str | None = None
    ai_comment_enabled = True
    index = 0
    while index < len(values):
        item = values[index].strip()
        if item == "--source":
            index += 1
            if index >= len(values):
                raise ValueError("--source 需要來源，例如 technical")
            source = _normalise_source(values[index])
        elif item == "--date":
            index += 1
            if index >= len(values):
                raise ValueError("--date 需要日期，例如 2026-05-20")
            report_date = _parse_date(values[index])
        elif item == "--ai-top":
            index += 1
            if index >= len(values):
                raise ValueError("--ai-top 需要數字，例如 5")
            ai_top = max(0, int(values[index]))
        elif item == "--model":
            index += 1
            if index >= len(values):
                raise ValueError("--model 需要模型名稱，例如 deepseek")
            model = _normalise_model(values[index])
        elif item == "--no-ai-comment":
            ai_comment_enabled = False
        elif re.fullmatch(r"\d{4}[-/]?\d{2}[-/]?\d{2}", item):
            report_date = _parse_date(item)
        elif item.startswith("--"):
            raise ValueError(f"不支援的 Radar 參數：{item}")
        else:
            source = _normalise_source(item)
        index += 1
    return RadarRequest(source=source, report_date=report_date, ai_top=ai_top, model=model, ai_comment_enabled=ai_comment_enabled)


def _normalise_radar_request(request: RadarRequest | list[str] | tuple[str, ...] | None) -> RadarRequest:
    if isinstance(request, RadarRequest):
        return request
    if isinstance(request, (list, tuple)):
        return parse_radar_args(request)
    if request is None:
        return RadarRequest()
    raise TypeError(f"unsupported Radar request type: {type(request).__name__}")


def run_radar(
    request: RadarRequest | list[str] | tuple[str, ...] | None = None,
    *,
    scan_settings: dict[str, float] | None = None,
    config: dict[str, Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> RadarResult:
    radar_request = _normalise_radar_request(request)
    target_date, date_note = resolve_radar_report_date(radar_request.report_date)
    _emit(progress, f"Radar：建立候選名單 source={radar_request.source} date={target_date.isoformat()}")
    if date_note:
        _emit(progress, f"Radar：{date_note}")
    candidates, source_policy = _load_candidates(radar_request.source, target_date, scan_settings, config, progress)
    if not candidates:
        result = RadarResult(radar_request, target_date, [], [], {"source_policy": source_policy, "note": "no_candidates", "date_note": date_note})
        save_radar_result(result)
        return result

    _attach_revenue_scores(candidates)
    _attach_chip_scores(candidates, target_date, progress)
    _attach_local_news(candidates, target_date)
    _score_candidates(candidates)

    ai_codes = _select_ai_enrichment_codes(candidates, radar_request.ai_top)
    if ai_codes:
        if radar_request.ai_comment_enabled and radar_request.model:
            _emit(progress, f"Radar：每策略 Top{radar_request.ai_top} 外部來源與 AI 短評 {len(ai_codes)} 檔")
            _attach_research_center_sources(candidates, ai_codes, target_date, progress)
            _attach_ai_comments(candidates, ai_codes, radar_request.model, target_date, progress)
        else:
            _emit(progress, f"Radar：Top{radar_request.ai_top} 外部來源補強 {len(ai_codes)} 檔")
            _attach_web_sources(candidates, ai_codes, target_date, progress)
        _score_candidates(candidates)

    candidates.sort(key=lambda item: (item.total_score, len(item.strategy_codes), item.code), reverse=True)
    result = RadarResult(
        radar_request,
        target_date,
        candidates,
        ai_codes,
        {"source_policy": source_policy, "candidate_count": len(candidates), "ai_top": radar_request.ai_top, "date_note": date_note},
    )
    save_radar_result(result)
    return result


def resolve_radar_report_date(report_date: date | None = None) -> tuple[date, str]:
    if report_date is not None:
        return report_date, ""
    today = get_tw_today()
    candidate = today
    for _ in range(10):
        if is_possible_trading_day(candidate):
            if candidate != today:
                return candidate, f"今天 {today.isoformat()} 不是交易日，已改用最新可用交易日 {candidate.isoformat()}。"
            return candidate, ""
        candidate -= timedelta(days=1)
    return today, "無法確認最新交易日，暫以今天日期執行。"


def format_radar_report(result: RadarResult, *, limit: int = 10) -> str:
    date_text = result.report_date.isoformat()
    lines = [
        f"📡 每日選股雷達 {date_text}",
        _radar_mode_line(result.request),
        "",
    ]
    date_note = str((result.diagnostics or {}).get("date_note") or "")
    if date_note:
        lines.extend([f"提示：{date_note}", ""])
    if not result.candidates:
        lines.append("目前沒有可評分候選股。")
        return "\n".join(lines)

    for rank, item in enumerate(result.candidates[:limit], 1):
        strategy = "/".join(sorted(item.strategy_codes)) if item.strategy_codes else "-"
        ai_badge = _ai_badge(item)
        labels = "、".join(_display_source_labels(item)[:3])
        components = item.score_components
        evidence = _candidate_evidence_line(item)
        technical_line = _technical_signal_line(item)
        chip_line = _chip_grade_line(item)
        ai_lines = _ai_comment_lines(item)
        lines.extend(
            [
                f"{rank}. {item.code} {item.name}｜{item.total_score}分｜策略 {strategy}{ai_badge}",
                f"   技術 {components.get('technical', 0)}｜營收 {components.get('revenue', 0)}｜籌碼 {components.get('chip', 0)}｜題材 {components.get('theme', 0)}｜族群 {components.get('market', 0)}",
                f"   {item.industry or '未分類'}｜{labels or '候選來源'}",
            ]
        )
        if technical_line:
            lines.append(f"   技術訊號：{technical_line}")
        if evidence:
            lines.append(f"   {evidence}")
        if chip_line:
            lines.append(f"   籌碼：{chip_line}")
        lines.extend(f"   {line}" for line in ai_lines)
        lines.append("")

    if len(result.candidates) > limit:
        lines.append(f"完整名單共 {len(result.candidates)} 檔，可用 /radar_more 查看。")
    lines.append("資料來源：既有選股流程 / 本地新聞資料庫 / 外部搜尋來源（若已設定）")
    return "\n".join(lines).strip()


def format_radar_more(report_date: date | None = None) -> str:
    result = load_radar_result(report_date)
    if result is None:
        if report_date:
            return f"找不到 {report_date.isoformat()} 的 Radar 結果，請先執行 /radar --date {report_date.isoformat()}。"
        return "找不到最近一次 Radar 結果，請先執行 /radar。"
    return format_radar_report(result, limit=max(50, len(result.candidates)))


def _radar_mode_line(request: RadarRequest) -> str:
    if request.ai_comment_enabled and request.model:
        return f"來源：{_source_label(request.source)}｜AI短評：{_model_label(request.model)}｜每策略 Top {request.ai_top}"
    if request.ai_comment_enabled:
        return f"來源：{_source_label(request.source)}｜外部來源補強：每策略 Top {request.ai_top}"
    return f"來源：{_source_label(request.source)}｜AI短評：略過"


def _model_label(model: str | None) -> str:
    return {"gemini": "Gemini", "deepseek": "DeepSeek", "minimax": "MiniMax"}.get(str(model or ""), str(model or ""))


def _ai_badge(item: RadarCandidate) -> str:
    comment = item.ai_comment or {}
    if comment.get("status") == "ok":
        return f"｜AI {comment.get('priority') or '中'}"
    if comment.get("status") in {"failed", "missing"}:
        return "｜AI 未完成"
    return ""


def _ai_comment_lines(item: RadarCandidate) -> list[str]:
    comment = item.ai_comment or {}
    if not comment:
        return []
    if comment.get("status") != "ok":
        return ["AI短評：本次模型分析失敗，保留本地 Radar 評分。"]
    lines = []
    if comment.get("reason"):
        lines.append(f"AI短評：{comment['reason']}")
    if comment.get("risk"):
        lines.append(f"風險：{comment['risk']}")
    if comment.get("watch"):
        lines.append(f"觀察：{comment['watch']}")
    return lines


def save_radar_result(result: RadarResult) -> dict[str, Any]:
    records = _load_radar_records(limit=30)
    payload = _json_safe(_result_to_record(result))
    records.insert(0, payload)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in records:
        key = str(item.get("radar_id") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    RADAR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RADAR_CACHE_PATH.write_text(json.dumps(_json_safe(deduped[:30]), ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_radar_result(report_date: date | None = None) -> RadarResult | None:
    for record in _load_radar_records(limit=30):
        if report_date and record.get("report_date") != report_date.isoformat():
            continue
        return _record_to_result(record)
    return None


def _load_candidates(
    source: str,
    target_date: date,
    scan_settings: dict[str, float] | None,
    config: dict[str, Any] | None,
    progress: Callable[[str], None] | None,
) -> tuple[list[RadarCandidate], dict[str, Any]]:
    source = _normalise_source(source)
    if source == "technical":
        return _technical_candidates_for_radar(target_date, scan_settings, progress)
    if source == "curated":
        return _curated_candidates(target_date, scan_settings, progress)
    if source == "financial":
        return _financial_candidates(scan_settings)
    if source == "chip":
        return _chip_candidates(target_date)
    if source == "monitor":
        return _monitor_candidates(config or {})
    if source == "portfolio":
        return _portfolio_candidates()
    raise ValueError(f"不支援的 Radar 來源：{source}")


def _technical_candidates(
    target_date: date,
    scan_settings: dict[str, float] | None,
    progress: Callable[[str], None] | None,
) -> tuple[list[RadarCandidate], dict[str, Any]]:
    cached = _find_recent_scan_by_type("技術面選股", target_date)
    if cached:
        codes = [str(code) for code in cached.get("selected_codes") or cached.get("codes") or []]
        by_code = _stock_meta_by_code()
        candidates = [_with_label(_candidate_from_meta(code, by_code), "技術面選股快取") for code in codes]
        _emit(progress, f"Radar：使用技術面選股快取 {len(candidates)} 檔")
        return candidates, {"source": "技術面選股結果", "status": "cached", "candidate_count": len(candidates)}

    policy = {"source": "技術面選股結果", "status": "generated"}
    result = ts.run_technical_scan(scan_settings, target_date)
    report_text = ts.format_technical_report(result)
    save_recent_scan_result("技術面選股", target_date, report_text)
    by_code = _stock_meta_by_code()
    candidates: dict[str, RadarCandidate] = {}
    for strategy, signals in result.strategy_signals.items():
        for signal in signals:
            code = str(signal.get("stock_id") or "")
            if not code:
                continue
            item = candidates.setdefault(code, _candidate_from_meta(code, by_code))
            item.strategy_codes.add(strategy)
            item.technical_signals.append(signal)
            _add_label(item, _strategy_label(strategy, signal.get("sub_signal_type")))
    _emit(progress, f"Radar：技術策略候選 {len(candidates)} 檔")
    return list(candidates.values()), policy


def _technical_candidates_for_radar(
    target_date: date,
    scan_settings: dict[str, float] | None,
    progress: Callable[[str], None] | None,
) -> tuple[list[RadarCandidate], dict[str, Any]]:
    cached = _find_technical_scan_cache(target_date)
    if cached:
        codes = [str(code) for code in cached.get("selected_codes") or cached.get("codes") or []]
        by_code = _stock_meta_by_code()
        candidates = [_with_label(_candidate_from_meta(code, by_code), "技術面選股快取") for code in codes]
        signals = _normalise_strategy_signals(cached.get("strategy_signals"))
        if not _has_strategy_signals(signals):
            _emit(progress, "Radar：技術面快取缺少策略明細，重跑既有技術掃描補策略訊號")
            scan_result = ts.run_technical_scan(scan_settings, target_date)
            signals = _normalise_strategy_signals(scan_result.strategy_signals)
        _apply_strategy_signals(candidates, signals)
        _emit(progress, f"Radar：使用技術面選股快取 {len(candidates)} 檔")
        return candidates, {
            "source": "技術面選股結果",
            "status": "cached",
            "candidate_count": len(candidates),
            "strategy_signal_count": _strategy_signal_count(signals),
        }

    result = ts.run_technical_scan(scan_settings, target_date)
    report_text = ts.format_technical_report(result)
    save_recent_scan_result("技術面選股", target_date, report_text)
    by_code = _stock_meta_by_code()
    candidates = _candidates_from_strategy_signals(result.strategy_signals, by_code)
    _emit(progress, f"Radar：技術面選股產生 {len(candidates)} 檔")
    return list(candidates.values()), {
        "source": "技術面選股結果",
        "status": "generated",
        "candidate_count": len(candidates),
        "strategy_signal_count": _strategy_signal_count(_normalise_strategy_signals(result.strategy_signals)),
    }


def _find_technical_scan_cache(target_date: date) -> dict[str, Any] | None:
    for record in load_recent_scan_results(limit=30):
        if str(record.get("report_date")) != target_date.isoformat():
            continue
        scan_type = str(record.get("scan_type") or "")
        if "技術" in scan_type and "選股" in scan_type:
            return record
    return None


def _normalise_strategy_signals(value: Any) -> dict[str, list[dict[str, Any]]]:
    signals: dict[str, list[dict[str, Any]]] = {"A": [], "B": [], "C": [], "D": []}
    if not isinstance(value, dict):
        return signals
    for strategy in signals:
        raw_items = value.get(strategy) or []
        if isinstance(raw_items, list):
            signals[strategy] = [dict(item) for item in raw_items if isinstance(item, dict)]
    return signals


def _has_strategy_signals(signals: dict[str, list[dict[str, Any]]]) -> bool:
    return any(signals.get(strategy) for strategy in ("A", "B", "C", "D"))


def _strategy_signal_count(signals: dict[str, list[dict[str, Any]]]) -> int:
    return sum(len(signals.get(strategy) or []) for strategy in ("A", "B", "C", "D"))


def _apply_strategy_signals(candidates: list[RadarCandidate], signals: dict[str, list[dict[str, Any]]]) -> None:
    by_code = {item.code: item for item in candidates}
    for strategy, items in signals.items():
        for signal in items:
            code = str(signal.get("stock_id") or signal.get("code") or "")
            item = by_code.get(code)
            if item is None:
                continue
            item.strategy_codes.add(strategy)
            item.technical_signals.append(signal)
            _add_label(item, _strategy_label(strategy, signal.get("sub_signal_type")))


def _candidates_from_strategy_signals(
    strategy_signals: dict[str, list[dict[str, Any]]],
    by_code: dict[str, Any],
) -> dict[str, RadarCandidate]:
    candidates: dict[str, RadarCandidate] = {}
    signals = _normalise_strategy_signals(strategy_signals)
    for strategy, items in signals.items():
        for signal in items:
            code = str(signal.get("stock_id") or signal.get("code") or "")
            if not code:
                continue
            item = candidates.setdefault(code, _candidate_from_meta(code, by_code))
            item.strategy_codes.add(strategy)
            item.technical_signals.append(signal)
            _add_label(item, _strategy_label(strategy, signal.get("sub_signal_type")))
    return candidates


def _curated_candidates(
    target_date: date,
    scan_settings: dict[str, float] | None,
    progress: Callable[[str], None] | None,
) -> tuple[list[RadarCandidate], dict[str, Any]]:
    cached = curated_scan_service.find_cached_curated_scan(target_date)
    if cached:
        codes = [str(code) for code in cached.get("codes") or []]
        status = "cached"
    else:
        _emit(progress, "Radar：沒有精選選股快取，呼叫既有精選選股流程")
        curated = curated_scan_service.build_curated_scan_result(scan_settings, target_date)
        codes = curated.selected_codes
        save_recent_scan_result("精選選股", target_date, curated.report_text, curated.selected_codes)
        status = "generated"
    by_code = _stock_meta_by_code()
    return [_with_label(_candidate_from_meta(code, by_code), "精選選股") for code in codes], {
        "source": "精選選股結果",
        "status": status,
        "candidate_count": len(codes),
    }


def _financial_candidates(scan_settings: dict[str, float] | None) -> tuple[list[RadarCandidate], dict[str, Any]]:
    report = scan_tw_market(False, None, scan_settings)
    candidates = []
    for row in report.candidates:
        candidates.append(
            RadarCandidate(
                code=row.code,
                name=row.name,
                symbol=row.symbol,
                industry=row.industry,
                price=row.price,
                source_labels=["財報營收選股"],
            )
        )
    return candidates, {"source": "財報營收選股結果", "status": "generated", "candidate_count": len(candidates)}


def _chip_candidates(target_date: date) -> tuple[list[RadarCandidate], dict[str, Any]]:
    context = build_market_context(False, target_date, include_daily_data=True)
    grade_maps = build_chip_grade_maps(context, CHIP_KEYS)
    by_code = _stock_meta_by_code()
    candidates: dict[str, RadarCandidate] = {}
    for key, grades in grade_maps.items():
        for code, grade in grades.items():
            item = candidates.setdefault(code, _candidate_from_meta(code, by_code))
            item.chip_grades[key] = grade
            _add_label(item, f"籌碼/{key}:{grade}")
    return list(candidates.values()), {"source": "法人籌碼 / 大戶選股結果", "status": "generated", "candidate_count": len(candidates)}


def _monitor_candidates(config: dict[str, Any]) -> tuple[list[RadarCandidate], dict[str, Any]]:
    by_code = _stock_meta_by_code()
    codes = [_base_code(item.get("symbol", "")) for item in get_monitor_stocks(config)]
    return [_with_label(_candidate_from_meta(code, by_code), "監控清單") for code in codes if code], {
        "source": "監控清單",
        "status": "loaded",
        "candidate_count": len(codes),
    }


def _portfolio_candidates() -> tuple[list[RadarCandidate], dict[str, Any]]:
    by_code = _stock_meta_by_code()
    codes = [item.code for item in list_portfolio()]
    return [_with_label(_candidate_from_meta(code, by_code), "持股清單") for code in codes], {
        "source": "持股清單",
        "status": "loaded",
        "candidate_count": len(codes),
    }


def _attach_chip_scores(
    candidates: list[RadarCandidate],
    target_date: date,
    progress: Callable[[str], None] | None,
) -> None:
    if not candidates:
        return
    try:
        context = build_market_context(False, target_date, include_daily_data=True, scope="radar")
        grade_maps = build_chip_grade_maps(context, CHIP_KEYS)
    except Exception as exc:
        _emit(progress, f"Radar：籌碼評級補強略過：{exc}")
        return
    by_code = {item.code: item for item in candidates}
    matched = 0
    for key, grades in grade_maps.items():
        for code, grade in grades.items():
            item = by_code.get(str(code))
            if item is None:
                continue
            if key not in item.chip_grades:
                matched += 1
            item.chip_grades[key] = str(grade)
    _emit(progress, f"Radar：籌碼評級補強完成，命中 {matched} 筆")


def _attach_revenue_scores(candidates: list[RadarCandidate]) -> None:
    universe = load_stock_universe(False)
    code_map = {entry.code: entry for entry in universe}
    selected = [code_map[item.code] for item in candidates if item.code in code_map]
    revenue = load_recent_revenue_history(selected)
    for item in candidates:
        points = revenue.get(item.code) or []
        latest = points[0] if points else None
        yoy = getattr(latest, "yoy", None) if latest else None
        item.score_components["revenue"] = _score_revenue(yoy)


def _attach_local_news(candidates: list[RadarCandidate], analysis_date: date) -> None:
    try:
        repository = NewsRepository()
        items = repository.query_all_recent(hours=24 * 180)
    except Exception:
        items = []
    for candidate in candidates:
        terms = {candidate.code, candidate.name, candidate.industry}
        matched = []
        for item in items:
            published = parse_date_like(item.published_at) or parse_date_like(item.created_at)
            if published and published > analysis_date:
                continue
            haystack = " ".join(
                [
                    item.title or "",
                    item.summary or "",
                    " ".join(item.related_symbols or []),
                    " ".join(item.related_topics or []),
                ]
            )
            if any(term and term in haystack for term in terms):
                matched.append(
                    {
                        "title": item.title,
                        "source": item.source,
                        "published_at": item.published_at,
                        "importance_score": item.importance_score,
                    }
                )
        candidate.news_items = matched[:5]


def _attach_web_sources(
    candidates: list[RadarCandidate],
    ai_codes: list[str],
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> None:
    config = load_research_config()
    service = TavilySearchService(
        config.tavily_api_key,
        enable_search=config.enable_tavily_search,
        enable_extract=False,
        search_depth=config.tavily_search_depth,
        max_results_per_query=min(config.tavily_max_results_per_query, 3),
    )
    if not service.is_configured():
        return
    by_code = {item.code: item for item in candidates}
    for code in ai_codes:
        item = by_code.get(code)
        if item is None:
            continue
        request = CommandRequest(command="research", raw_text="/radar", target=f"{item.code} {item.name}", report_date=analysis_date)
        tasks = [
            {
                "label": "radar_freshness",
                "objective": "搜尋指定日期當天與之前近期的台股新聞、題材、族群熱度與營收訂單資訊。",
                "queries": [
                    f"{item.code} {item.name} 台股 新聞 題材 營收 訂單",
                    f"{item.name} {item.industry} 題材 族群 熱度 台股",
                ],
            }
        ]
        tasks = augment_discovery_tasks_with_date_context(request, {}, tasks)
        try:
            result = service.discover(request, tasks, progress=progress)
        except TavilyQuotaError:
            break
        except Exception:
            continue
        sources, _dropped_sources = filter_and_sort_sources_for_analysis_date(result.sources, request)
        item.web_sources = [_source_to_dict(source) for source in sources[:5]]


def _attach_research_center_sources(
    candidates: list[RadarCandidate],
    ai_codes: list[str],
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> None:
    try:
        center = ResearchCenter()
    except Exception as exc:
        _emit(progress, f"Radar：Research Center 初始化失敗，略過外部來源：{exc}")
        return

    by_code = {item.code: item for item in candidates}
    runner = getattr(center, "_gemini_discovery_runner", None)
    if runner is None:
        _emit(progress, "Radar：Research Center 搜尋鏈不可用，略過外部來源")
        return

    for code in ai_codes:
        item = by_code.get(code)
        if item is None:
            continue
        request = CommandRequest(command="research", raw_text="/radar", target=f"{item.code} {item.name}", report_date=analysis_date)
        structured_data = {"radar_candidate": _build_ai_comment_payload(item, analysis_date)}
        sources: list[SourceItem] = []
        tasks = [_radar_discovery_task(item, analysis_date)]
        try:
            runner._run_minimax_mcp(request, tasks, sources, structured_data, progress)
            runner._run_tavily(request, tasks, sources, structured_data, progress)
            if runner._should_run_gemini(request, sources):
                discovery_sources: list[SourceItem] = []
                discovery_runs: list[dict[str, Any]] = []
                runner._run_gemini(request, tasks, sources, structured_data, discovery_sources, discovery_runs, progress)
            sources, _dropped_sources = filter_and_sort_sources_for_analysis_date(sources, request)
            _enrich_sources_with_web_fetch(request, sources, structured_data, progress)
        except Exception as exc:
            _emit(progress, f"Radar：{item.code} 外部來源補強失敗：{exc}")
            continue

        item.web_sources = [_source_to_dict(source) for source in sources[:5]]
        item.ai_sources = _normalise_ai_sources(sources, structured_data)


def _radar_discovery_task(item: RadarCandidate, analysis_date: date) -> dict[str, Any]:
    target = f"{item.code} {item.name}".strip()
    industry = item.industry or "台股"
    date_text = analysis_date.isoformat()
    queries = [
        f"{target} 新聞 題材 法人 {date_text}",
        f"{target} {industry} 營收 籌碼 產業 {date_text}",
        f"{target} 股價 題材 外資 投信 {analysis_date.year}",
    ]
    prompt = (
        "請使用 Google Search 尋找下列台股候選股在分析日期以前的可驗證來源。\n"
        "只找新聞、公告、法人籌碼、營收、產業題材或市場關注來源；不要新增候選股票。\n"
        f"候選股：{target}\n"
        f"產業：{industry}\n"
        f"analysis_date：{date_text}\n"
        "不得使用晚於 analysis_date 的來源。"
    )
    return {"label": "radar_ai_sources", "objective": "補充 Radar AI 短評來源", "queries": queries, "prompt": prompt}


def _normalise_ai_sources(sources: list[SourceItem], structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source in sources[:8]:
        items.append(
            {
                "title": source.title,
                "url": source.url,
                "published_date": source.published_date,
                "provider": source.provider,
                "source_level": source.source_level,
                "snippet": source.snippet,
            }
        )
    for source in structured_data.get("web_fetched_sources") or []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "")
        if url and any(item.get("url") == url for item in items):
            continue
        items.append(
            {
                "title": source.get("title"),
                "url": url,
                "published_date": source.get("published_date"),
                "provider": source.get("provider"),
                "source_level": source.get("source_level"),
                "snippet": source.get("snippet") or source.get("content", "")[:300],
            }
        )
        if len(items) >= 8:
            break
    return items


def _attach_ai_comments(
    candidates: list[RadarCandidate],
    ai_codes: list[str],
    model: str,
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> None:
    by_code = {item.code: item for item in candidates}
    selected = [by_code[code] for code in ai_codes if code in by_code]
    if not selected:
        return
    prompt = _build_ai_comment_prompt(selected, analysis_date)
    try:
        raw_text = _call_ai_comment_model(model, prompt)
        parsed = _parse_ai_comment_response(raw_text)
        comments = _normalise_ai_comment_items(parsed)
    except Exception as exc:
        _emit(progress, f"Radar：AI 短評失敗，保留本地評分：{exc}")
        for item in selected:
            item.ai_comment = {"status": "failed", "model": model, "error": str(exc)}
        return

    for item in selected:
        comment = comments.get(item.code)
        if not comment:
            item.ai_comment = {"status": "missing", "model": model}
            continue
        item.ai_comment = {
            "status": "ok",
            "model": model,
            "priority": str(comment.get("priority") or comment.get("ai_priority") or "中"),
            "confidence": str(comment.get("confidence") or "中"),
            "reason": str(comment.get("reason") or comment.get("recommendation") or ""),
            "risk": str(comment.get("risk") or ""),
            "watch": str(comment.get("watch") or comment.get("watch_point") or ""),
        }
    _emit(progress, f"Radar：AI 短評完成 {sum(1 for item in selected if item.ai_comment.get('status') == 'ok')} 檔")


def _score_candidates(candidates: list[RadarCandidate]) -> None:
    industry_counts: dict[str, int] = {}
    for item in candidates:
        if item.industry:
            industry_counts[item.industry] = industry_counts.get(item.industry, 0) + 1
    for item in candidates:
        item.score_components["technical"] = _score_technical(item)
        item.score_components["chip"] = _score_chip(item)
        item.score_components["theme"] = _score_theme(item)
        item.score_components["market"] = min(10, industry_counts.get(item.industry, 0) * 2)
        item.total_score = min(100, sum(item.score_components.values()))


def _score_technical(item: RadarCandidate) -> int:
    if not item.technical_signals:
        return 0
    best = 0
    for signal in item.technical_signals:
        strategy = str(signal.get("strategy_code") or "")
        setup = int(signal.get("technical_setup_score") or 0)
        score = min(20, setup * 2)
        score += {"A": 8, "B": 8, "C": 10, "D": 6}.get(strategy, 0)
        features = signal.get("features") or {}
        if strategy == "C":
            if features.get("zone2_hist_min") is not None:
                score += 3
            if _has_kd_low_divergence(signal):
                score += 3
            if _has_dif_support(signal):
                score += 2
        if strategy == "D" and "高風險" in str(signal.get("notes") or ""):
            score -= 3
        best = max(best, score)
    return max(0, min(40, best))


def _score_revenue(yoy: Any) -> int:
    try:
        value = float(yoy)
    except (TypeError, ValueError):
        return 0
    if value >= 30:
        return 15
    if value >= 10:
        return 10
    if value > 0:
        return 6
    return 0


def _score_chip(item: RadarCandidate) -> int:
    score = 0
    for key, grade in item.chip_grades.items():
        score += {"A": 5, "B": 4, "C": 2}.get(str(grade).upper(), 1)
        if key == "chip_4":
            score += 1
    return min(15, score)


def _score_theme(item: RadarCandidate) -> int:
    local_score = min(10, len(item.news_items) * 3)
    web_score = min(10, len(item.web_sources) * 2)
    return min(20, local_score + web_score)


def _select_ai_enrichment_codes(candidates: list[RadarCandidate], ai_top: int) -> list[str]:
    if ai_top <= 0:
        return []
    selected: list[str] = []
    for strategy in ["A", "B", "C", "D"]:
        group = [item for item in candidates if strategy in item.strategy_codes]
        group.sort(key=lambda item: item.total_score, reverse=True)
        for item in group[:ai_top]:
            if item.code not in selected:
                selected.append(item.code)
    if not selected:
        for item in sorted(candidates, key=lambda item: item.total_score, reverse=True)[:ai_top]:
            selected.append(item.code)
    return selected


def _stock_meta_by_code() -> dict[str, Any]:
    return {entry.code: entry for entry in load_stock_universe(False)}


def _candidate_from_meta(code: str, by_code: dict[str, Any]) -> RadarCandidate:
    entry = by_code.get(code)
    if entry is None:
        return RadarCandidate(code=code)
    return RadarCandidate(code=entry.code, name=entry.name, symbol=entry.symbol, industry=entry.industry)


def _with_label(candidate: RadarCandidate, label: str) -> RadarCandidate:
    _add_label(candidate, label)
    return candidate


def _add_label(candidate: RadarCandidate, label: str | None) -> None:
    text = str(label or "").strip()
    if text and text not in candidate.source_labels:
        candidate.source_labels.append(text)


def _find_recent_scan_by_type(scan_type: str, target_date: date) -> dict[str, Any] | None:
    for record in load_recent_scan_results(limit=30):
        if str(record.get("report_date")) == target_date.isoformat() and scan_type in str(record.get("scan_type") or ""):
            return record
    return None


def _candidate_evidence_line(item: RadarCandidate) -> str:
    parts = []
    if item.news_items:
        parts.append(f"本地新聞 {len(item.news_items)} 則")
    if item.web_sources:
        parts.append(f"外部來源 {len(item.web_sources)} 則")
    return "｜".join(parts)


def _display_source_labels(item: RadarCandidate) -> list[str]:
    labels = []
    for label in item.source_labels:
        text = str(label or "").strip()
        if not text:
            continue
        if text.startswith("策略"):
            continue
        if text not in labels:
            labels.append(text)
    return labels


def _technical_signal_line(item: RadarCandidate) -> str:
    parts = []
    seen = set()
    for signal in item.technical_signals:
        strategy = str(signal.get("strategy_code") or "")
        if not strategy:
            continue
        sub_signal = str(signal.get("sub_signal_type") or "")
        key = (strategy, sub_signal)
        if key in seen:
            continue
        seen.add(key)
        strategy_label = TECHNICAL_STRATEGY_LABELS.get(strategy, f"策略 {strategy}")
        sub_label = TECHNICAL_SUB_SIGNAL_LABELS.get(sub_signal)
        if sub_label:
            parts.append(f"{strategy} {strategy_label}：{sub_label}")
        else:
            parts.append(f"{strategy} {strategy_label}：其他技術訊號")
    return "；".join(parts)


def _chip_grade_line(item: RadarCandidate) -> str:
    parts = []
    for key, grade in sorted(item.chip_grades.items()):
        label = CHIP_STRATEGY_LABELS.get(str(key), "籌碼策略")
        grade_text = str(grade).upper()
        parts.append(f"{label} {grade_text}級")
    return "、".join(parts)


def _source_to_dict(source: SourceItem) -> dict[str, Any]:
    return {
        "title": source.title,
        "url": source.url,
        "published_date": source.published_date,
        "provider": source.provider,
    }


def _build_ai_comment_payload(item: RadarCandidate, analysis_date: date) -> dict[str, Any]:
    components = item.score_components or {}
    return {
        "code": item.code,
        "name": item.name,
        "industry": item.industry,
        "analysis_date": analysis_date.isoformat(),
        "total_score": item.total_score,
        "score_components": {
            "technical": components.get("technical", 0),
            "revenue": components.get("revenue", 0),
            "chip": components.get("chip", 0),
            "theme": components.get("theme", 0),
            "market": components.get("market", 0),
        },
        "strategies": sorted(item.strategy_codes),
        "technical_signal_summary": _technical_signal_line(item),
        "chip_summary": _chip_grade_line(item),
        "local_news": [news.get("title") for news in item.news_items[:5] if isinstance(news, dict)],
        "external_sources": item.ai_sources[:8] or item.web_sources[:8],
    }


def _build_ai_comment_prompt(candidates: list[RadarCandidate], analysis_date: date) -> str:
    payloads = [_build_ai_comment_payload(item, analysis_date) for item in candidates]
    return (
        "你是台股選股雷達的短評助手。請只根據輸入資料做判斷，不得新增股票，不得改變本地分數。\n"
        "請輸出嚴格 JSON，不要 Markdown，不要 code fence。\n"
        "JSON 格式：{\"comments\":[{\"code\":\"2330\",\"priority\":\"高|中|低\",\"confidence\":\"高|中|低\",\"reason\":\"一句推薦理由\",\"risk\":\"一句主要風險\",\"watch\":\"一句觀察重點\"}]}\n"
        "若資料不足，priority 與 confidence 請降低，並在 reason 或 risk 說明資料不足。\n"
        f"analysis_date：{analysis_date.isoformat()}\n"
        "候選股資料：\n"
        f"{json.dumps(_json_safe(payloads), ensure_ascii=False)}"
    )


def _call_ai_comment_model(model: str, prompt: str) -> str:
    center = ResearchCenter()
    selected = _normalise_model(model)
    if selected == "deepseek":
        if not center.opencode.is_configured():
            raise RuntimeError("DeepSeek / OpenCode Go API Key 尚未設定")
        return center.opencode.generate_report(prompt).markdown
    if selected == "minimax":
        if not center.minimax.is_configured():
            raise RuntimeError("MiniMax API Key 尚未設定")
        return center.minimax.generate_json(prompt).markdown
    if not center.gemini.is_configured():
        raise RuntimeError("Gemini API Key 尚未設定")
    return center.gemini.generate_report(prompt, enable_grounding=False).markdown


def _parse_ai_comment_response(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("AI 短評 JSON 必須是 object")
    return parsed


def _normalise_ai_comment_items(parsed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_comments = parsed.get("comments")
    if isinstance(raw_comments, dict):
        iterable = raw_comments.values()
    elif isinstance(raw_comments, list):
        iterable = raw_comments
    else:
        iterable = []
    comments: dict[str, dict[str, Any]] = {}
    for item in iterable:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code:
            comments[code] = item
    return comments


def _result_to_record(result: RadarResult) -> dict[str, Any]:
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "radar_id": f"radar_{result.report_date.strftime('%Y%m%d')}_{datetime.now().strftime('%H%M%S')}",
        "report_date": result.report_date.isoformat(),
        "source": result.request.source,
        "ai_top": result.request.ai_top,
        "model": result.request.model,
        "ai_comment_enabled": result.request.ai_comment_enabled,
        "created_at": created,
        "ai_enriched_codes": result.ai_enriched_codes,
        "diagnostics": result.diagnostics,
        "candidates": [_candidate_to_dict(item) for item in result.candidates],
    }


def _candidate_to_dict(item: RadarCandidate) -> dict[str, Any]:
    return {
        "code": item.code,
        "name": item.name,
        "symbol": item.symbol,
        "industry": item.industry,
        "price": item.price,
        "source_labels": item.source_labels,
        "strategy_codes": sorted(item.strategy_codes),
        "technical_signals": item.technical_signals,
        "chip_grades": item.chip_grades,
        "news_items": item.news_items,
        "web_sources": item.web_sources,
        "ai_sources": item.ai_sources,
        "ai_comment": item.ai_comment,
        "score_components": item.score_components,
        "total_score": item.total_score,
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if pd.isna(value) else value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(_json_safe(key)): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            return str(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _record_to_result(record: dict[str, Any]) -> RadarResult:
    request = RadarRequest(
        source=str(record.get("source") or DEFAULT_SOURCE),
        report_date=_parse_date(str(record.get("report_date"))),
        ai_top=int(record.get("ai_top") or DEFAULT_AI_TOP),
        model=str(record.get("model")) if record.get("model") else None,
        ai_comment_enabled=bool(record.get("ai_comment_enabled", True)),
    )
    candidates = []
    for raw in record.get("candidates") or []:
        item = RadarCandidate(
            code=str(raw.get("code") or ""),
            name=str(raw.get("name") or ""),
            symbol=str(raw.get("symbol") or ""),
            industry=str(raw.get("industry") or ""),
            price=raw.get("price"),
            source_labels=list(raw.get("source_labels") or []),
            strategy_codes=set(raw.get("strategy_codes") or []),
            technical_signals=list(raw.get("technical_signals") or []),
            chip_grades=dict(raw.get("chip_grades") or {}),
            news_items=list(raw.get("news_items") or []),
            web_sources=list(raw.get("web_sources") or []),
            ai_sources=list(raw.get("ai_sources") or []),
            ai_comment=dict(raw.get("ai_comment") or {}),
            score_components=dict(raw.get("score_components") or {}),
            total_score=int(raw.get("total_score") or 0),
        )
        candidates.append(item)
    return RadarResult(
        request=request,
        report_date=_parse_date(str(record.get("report_date"))),
        candidates=candidates,
        ai_enriched_codes=list(record.get("ai_enriched_codes") or []),
        diagnostics=dict(record.get("diagnostics") or {}),
    )


def _load_radar_records(limit: int = 10) -> list[dict[str, Any]]:
    if not RADAR_CACHE_PATH.exists():
        return []
    try:
        data = json.loads(RADAR_CACHE_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    return [item for item in data if isinstance(item, dict)][:limit]


def _normalise_source(value: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "技術面選股結果": "technical",
        "技術面": "technical",
        "technical": "technical",
        "精選選股結果": "curated",
        "精選": "curated",
        "curated": "curated",
        "財報營收選股結果": "financial",
        "營收": "financial",
        "financial": "financial",
        "法人籌碼": "chip",
        "大戶": "chip",
        "chip": "chip",
        "監控清單": "monitor",
        "monitor": "monitor",
        "持股清單": "portfolio",
        "portfolio": "portfolio",
    }
    source = aliases.get(text, text)
    if source not in MAIN_SOURCES:
        raise ValueError(f"不支援的 Radar 來源：{value}")
    return source


def _normalise_model(value: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "gemini": "gemini",
        "google": "gemini",
        "deepseek": "deepseek",
        "opencode": "deepseek",
        "opencode-go": "deepseek",
        "deepseek-v4-pro": "deepseek",
        "minimax": "minimax",
        "minimax-m2.7": "minimax",
    }
    model = aliases.get(text, text)
    if model not in {"gemini", "deepseek", "minimax"}:
        raise ValueError("--model 僅支援 gemini、deepseek 或 minimax")
    return model


def _source_label(source: str) -> str:
    return {
        "technical": "技術面選股結果",
        "curated": "精選選股結果",
        "financial": "財報營收選股結果",
        "chip": "法人籌碼 / 大戶選股結果",
        "monitor": "監控清單",
        "portfolio": "持股清單",
    }.get(source, source)


def _strategy_label(strategy: str, sub_signal: Any) -> str:
    strategy_label = TECHNICAL_STRATEGY_LABELS.get(str(strategy), f"策略 {strategy}")
    sub_label = TECHNICAL_SUB_SIGNAL_LABELS.get(str(sub_signal or ""), "其他技術訊號")
    return f"策略 {strategy}：{strategy_label}（{sub_label}）"


def _parse_date(value: str) -> date:
    text = str(value or "").strip().replace("/", "-")
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return datetime.strptime(text, "%Y-%m-%d").date()


def _base_code(value: str) -> str:
    return str(value or "").strip().upper().split(".", 1)[0]


def _has_kd_low_divergence(signal: dict[str, Any]) -> bool:
    kd = signal.get("kd_context") or {}
    try:
        return float(kd.get("k")) < 50 and float(kd.get("k")) > float(kd.get("d"))
    except (TypeError, ValueError):
        return False


def _has_dif_support(signal: dict[str, Any]) -> bool:
    macd = signal.get("macd_context") or {}
    try:
        dif = float(macd.get("dif"))
        dea = float(macd.get("dea"))
    except (TypeError, ValueError):
        return False
    return dif > dea or dif < 0


def _emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
