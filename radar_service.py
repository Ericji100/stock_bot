from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import curated_scan_service
import laoxiao_scan_service
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
from research_center.ai_workflow_service import build_ai_workflow_coverage, run_low_model_digest_for_payload
from research_center.data_services import collect_structured_data
from research_center.evidence_pack_service import build_ai_compact_context, build_three_layer_evidence_context
from research_center.news_repository import NewsRepository
from research_center.recent_scans import load_recent_scan_results, save_recent_scan_result
from research_center.convergence_service import candidate_snapshot_from_row
from research_center.structured_cache import load_latest_research_structured_cache, load_research_structured_cache, save_research_structured_cache
from research_center.topic_context import build_stock_topic_context
from research_center.web_fetch_enrichment import _enrich_sources_with_web_fetch
from research_center.tavily_search_service import TavilyQuotaError, TavilySearchService
from research_center.free_sources import build_valuation_context_map
from data_fetcher import StockDataFetcher
from stock_scanner import load_recent_revenue_history, load_stock_universe, scan_tw_market
from stock_ai_bot.telegram.telegram_stock_formatting import mark_stock_text, strip_stock_markers
from technical_indicator_service import apply_point_in_time_adjustment
from unified_financial_scoring import effective_revenue_rows, score_unified_financial, score_unified_revenue


ROOT_DIR = Path(__file__).resolve().parent
RADAR_CACHE_PATH = ROOT_DIR / ".cache" / "radar_results.json"
RADAR_REPORT_DIR = ROOT_DIR / "reports" / "radar"
RADAR_CACHE_MAX_BYTES = 50 * 1024 * 1024
RADAR_PROMPT_DIR = ROOT_DIR / "prompt" / "radar"
RADAR_SCORING_CONFIG_PATH = ROOT_DIR / "config" / "radar_scoring.json"
DEFAULT_SOURCE = "combined"
DEFAULT_AI_TOP = 15
DEFAULT_SCORING_VERSION = "v3"
SUPPORTED_SCORING_VERSIONS = {"v1", "v2", "v3"}
RADAR_AI_CHUNK_SIZE = 5
RADAR_AI_PROMPT_MAX_CHARS = 90_000
RADAR_AI_COMPACT_SOURCE_LIMIT = 10
RADAR_AI_COMPACT_LIST_LIMIT = 12
RADAR_AI_COMPACT_STRING_LIMIT = 300
RADAR_AI_TIGHT_SOURCE_LIMIT = 5
RADAR_AI_TIGHT_LIST_LIMIT = 8
RADAR_AI_TIGHT_STRING_LIMIT = 180
RADAR_AI_MINIMAL_SOURCE_LIMIT = 3
RADAR_AI_MINIMAL_LIST_LIMIT = 5
RADAR_AI_MINIMAL_STRING_LIMIT = 120
RADAR_TELEGRAM_AI_TEXT_LIMIT = 160
RADAR_MIN_EXTERNAL_SOURCES = 8
RADAR_EVIDENCE_PACK_TIMEOUT_SECONDS = 120.0
RADAR_FULL_RESEARCH_CACHE_MAX_AGE_DAYS = 5
RADAR_LIGHT_RESEARCH_CACHE_DIR = ROOT_DIR / ".cache" / "radar_research_light"
RADAR_PRE_SCORE_FINANCIAL_FETCH_LIMIT = 12
RADAR_PRE_SCORE_MARGIN_FETCH_LIMIT = 12
RADAR_PRE_SCORE_FETCH_TIMEOUT_SECONDS = 90.0
RADAR_PRE_SCORE_WORKERS = 4
RADAR_TECHNICAL_CACHE_READY_HOUR = 15
RADAR_TECHNICAL_CACHE_READY_MINUTE = 0
RADAR_SECTOR_CONTEXT_TTL_SECONDS = 10 * 60
RADAR_SECTOR_MIN_GROUP_SIZE = 4
RADAR_SECTOR_MIN_COVERAGE = 0.70
MAIN_SOURCES = {"combined", "technical", "curated", "laoxiao", "financial", "chip", "monitor", "portfolio"}
CHIP_KEYS = ["chip_1", "chip_2", "chip_3", "chip_4"]
TECHNICAL_STRATEGY_LABELS = {
    "A": "多頭延續回檔突破",
    "B": "強勢紅柱回測突破",
    "C": "低檔背離反轉突破",
    "D": "動能背景短線轉強",
}
TECHNICAL_SUB_SIGNAL_LABELS = {
    "A1_direct_ma21_breakout": "A1 直接突破 21MA",
    "A2_pivot_low_reclaim_ma21": "A2 低點墊高後收復 21MA",
    "A3_reclaim_ma21_and_long_ma": "A3 同日收復 21MA 與長均線",
    "B1_intraday_retest_reclaim_ma": "B1 盤中回測後收復 MA5/MA13/MA21",
    "B2_short_reclaim_after_break_ma": "B2 紅柱期間收盤突破 MA5/MA13/MA21",
    "B3_breakout_after_retest": "B3 回測 MA13/MA21 後突破前高",
    "C1_macd_bullish_divergence_break_ma21": "C1 MACD 低檔背離突破 21MA",
    "C2_below_zero_red_histogram_breakout": "C2 零軸下紅柱鈍化突破",
    "D1_above_zero_short_ma_reclaim": "D1 零軸上短均線收復",
    "D2_below_zero_short_ma_reclaim": "D2 零軸下短均線收復",
    "D3_kd_death_cross_first_reversal": "D3 KD 死叉後首次轉強",
    "D1_first_short_ma_reclaim": "D1 短均線首次收復",
    "D2_kd_death_cross_first_reversal": "D2 KD 死叉後首次轉強",
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
_RADAR_CHIP_GRADE_CACHE: dict[str, dict[str, dict[str, str]]] = {}
_RADAR_SECTOR_CONTEXT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class RadarRequest:
    source: str = DEFAULT_SOURCE
    report_date: date | None = None
    ai_top: int = DEFAULT_AI_TOP
    model: str | None = "minimax"
    ai_comment_enabled: bool = True
    scoring_version: str | None = None


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
    dual_ma_signals: list[dict[str, Any]] = field(default_factory=list)
    kd_ma_signals: list[dict[str, Any]] = field(default_factory=list)
    chip_grades: dict[str, str] = field(default_factory=dict)
    revenue_history: list[dict[str, Any]] = field(default_factory=list)
    news_items: list[dict[str, Any]] = field(default_factory=list)
    web_sources: list[dict[str, Any]] = field(default_factory=list)
    ai_sources: list[dict[str, Any]] = field(default_factory=list)
    evidence_pack: dict[str, Any] = field(default_factory=dict)
    data_coverage: dict[str, Any] = field(default_factory=dict)
    ai_comment: dict[str, Any] = field(default_factory=dict)
    score_components: dict[str, int] = field(default_factory=dict)
    score_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    key_reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    score_caps_applied: list[str] = field(default_factory=list)
    radar_feature_snapshot: dict[str, Any] = field(default_factory=dict)
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
    model: str | None = "minimax"
    ai_comment_enabled = True
    scoring_version: str | None = None
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
        elif item == "--scoring":
            index += 1
            if index >= len(values):
                raise ValueError("--scoring 需要版本，例如 v1、v2 或 v3")
            scoring_version = _normalise_scoring_version(values[index])
        elif item == "--no-ai-comment":
            ai_comment_enabled = False
        elif re.fullmatch(r"\d{4}[-/]?\d{2}[-/]?\d{2}", item):
            report_date = _parse_date(item)
        elif item.startswith("--"):
            raise ValueError(f"不支援的 Radar 參數：{item}")
        else:
            source = _normalise_source(item)
        index += 1
    return RadarRequest(
        source=source,
        report_date=report_date,
        ai_top=ai_top,
        model=model,
        ai_comment_enabled=ai_comment_enabled,
        scoring_version=_resolve_scoring_version(scoring_version),
    )


def _normalise_radar_request(request: RadarRequest | list[str] | tuple[str, ...] | None) -> RadarRequest:
    if isinstance(request, RadarRequest):
        return RadarRequest(
            source=request.source,
            report_date=request.report_date,
            ai_top=request.ai_top,
            model=request.model,
            ai_comment_enabled=request.ai_comment_enabled,
            scoring_version=_resolve_scoring_version(request.scoring_version),
        )
    if isinstance(request, (list, tuple)):
        return parse_radar_args(request)
    if request is None:
        return RadarRequest(scoring_version=_resolve_scoring_version(None))
    raise TypeError(f"unsupported Radar request type: {type(request).__name__}")


def _normalise_scoring_version(value: Any) -> str:
    version = str(value or "").strip().lower()
    if version not in SUPPORTED_SCORING_VERSIONS:
        raise ValueError(f"不支援的 Radar 評分版本：{value}，請使用 v1、v2 或 v3")
    return version


def _default_scoring_version() -> str:
    if RADAR_SCORING_CONFIG_PATH.exists():
        try:
            payload = json.loads(RADAR_SCORING_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        value = payload.get("default_version") if isinstance(payload, dict) else None
        try:
            return _normalise_scoring_version(value)
        except ValueError:
            return DEFAULT_SCORING_VERSION
    return DEFAULT_SCORING_VERSION


def _resolve_scoring_version(value: Any) -> str:
    if value in (None, ""):
        return _default_scoring_version()
    return _normalise_scoring_version(value)


def resolve_radar_scoring_version(value: Any = None) -> str:
    return _resolve_scoring_version(value)


def score_radar_candidates(
    candidates: list[RadarCandidate],
    analysis_date: date | None = None,
    *,
    scoring_version: str | None = None,
    reason_limit: int = 5,
    risk_limit: int = 4,
) -> list[RadarCandidate]:
    """Apply the shared Radar scoring core to candidate objects."""

    version = _resolve_scoring_version(scoring_version)
    industry_counts: dict[str, int] = {}
    for item in candidates:
        if item.industry:
            industry_counts[item.industry] = industry_counts.get(item.industry, 0) + 1
    for item in candidates:
        snapshot = _build_radar_feature_snapshot(item, candidates, industry_counts, analysis_date)
        score_radar_candidate_from_snapshot(
            item,
            snapshot,
            scoring_version=version,
            reason_limit=reason_limit,
            risk_limit=risk_limit,
        )
    return candidates


def score_radar_candidate_from_snapshot(
    item: RadarCandidate,
    snapshot: dict[str, Any],
    *,
    scoring_version: str | None = None,
    reason_limit: int = 5,
    risk_limit: int = 4,
) -> RadarCandidate:
    """Score one candidate from an already-built feature snapshot."""

    version = _resolve_scoring_version(scoring_version)
    details = {
        "technical": _score_technical_detail(item, snapshot),
        "revenue": score_unified_revenue(item, snapshot) if version == "v3" else _score_revenue_detail(item, snapshot),
        "financial": score_unified_financial(item, snapshot) if version == "v3" else _score_financial_detail(item, snapshot),
        "chip": _score_chip_detail(item, snapshot),
        "theme": _score_theme_news_detail(item, snapshot),
        "sector": _score_sector_detail(item, snapshot),
    }
    if version == "v2":
        _apply_radar_v2_overlays(item, snapshot, details)
    components = {key: int(detail.get("score") or 0) for key, detail in details.items()}
    total = min(100, sum(components.values()))
    total, caps = _apply_radar_score_caps(total, components, details)
    item.radar_feature_snapshot = snapshot
    item.score_details = details
    item.score_components = components
    item.score_caps_applied = caps
    item.key_reasons = _top_unique_reasons(details, limit=reason_limit)
    item.risk_flags = _top_unique_risks(details, caps, limit=risk_limit)
    item.total_score = int(total)
    return item


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

    _attach_revenue_scores(candidates, target_date)
    _attach_chip_scores(candidates, target_date, progress)
    _attach_local_news(candidates, target_date)
    prepare_radar_scoring_data(candidates, target_date, progress, scoring_version=radar_request.scoring_version)
    _score_candidates(candidates, target_date, scoring_version=radar_request.scoring_version)
    candidates.sort(key=lambda item: (item.total_score, len(item.strategy_codes), item.code), reverse=True)
    top_structured_count = min(len(candidates), max(30, radar_request.ai_top, DEFAULT_AI_TOP))
    if top_structured_count:
        _emit(progress, f"Radar：初評 Top{top_structured_count} 結構化資料二次補齊後重新評分")
        prepare_radar_scoring_data(
            candidates[:top_structured_count],
            target_date,
            progress,
            scoring_version=radar_request.scoring_version,
        )
        _score_candidates(candidates, target_date, scoring_version=radar_request.scoring_version)
    _attach_base_evidence_packs(candidates, target_date)

    ai_analysis_meta: dict[str, Any] = {}
    ai_codes = _select_ai_enrichment_codes(candidates, radar_request.ai_top)
    if ai_codes:
        if radar_request.ai_comment_enabled and radar_request.model:
            _emit(progress, f"Radar：AI補強 Top{radar_request.ai_top} 外部來源與 AI 短評 {len(ai_codes)} 檔")
            _attach_research_center_sources(candidates, ai_codes, target_date, progress)
            _ensure_radar_source_sufficiency(candidates, ai_codes, target_date, progress)
            _attach_research_evidence_packs(candidates, ai_codes, target_date, progress)
            ai_analysis_meta = _attach_ai_comments(candidates, ai_codes, radar_request.model, target_date, progress)
        else:
            _emit(progress, f"Radar：Top{radar_request.ai_top} 外部來源補強 {len(ai_codes)} 檔")
            _attach_web_sources(candidates, ai_codes, target_date, progress)
        _score_candidates(candidates, target_date, scoring_version=radar_request.scoring_version)
        _attach_base_evidence_packs(candidates, target_date)

    candidates.sort(key=lambda item: (item.total_score, len(item.strategy_codes), item.code), reverse=True)
    result = RadarResult(
        radar_request,
        target_date,
        candidates,
        ai_codes,
        {
            "source_policy": source_policy,
            "candidate_count": len(candidates),
            "ai_top": radar_request.ai_top,
            "date_note": date_note,
            "ai_analysis": ai_analysis_meta,
            "evidence_pack_status": _radar_evidence_pack_status(candidates, ai_codes),
            "scoring_version": radar_request.scoring_version,
        },
    )
    save_radar_result(result)
    return result


def _radar_evidence_pack_status(candidates: list[RadarCandidate], ai_codes: list[str]) -> dict[str, Any]:
    by_code = {item.code: item for item in candidates}
    selected = [by_code[code] for code in ai_codes if code in by_code]
    success = 0
    timeout = 0
    failed = 0
    for item in selected:
        pack = item.evidence_pack if isinstance(item.evidence_pack, dict) else {}
        if pack.get("research_structured_timeout"):
            timeout += 1
        elif pack.get("research_structured_data"):
            success += 1
        elif pack.get("research_structured_error"):
            failed += 1
    return {
        "selected": len(selected),
        "success": success,
        "timeout": timeout,
        "failed": failed,
    }


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


def format_radar_report(result: RadarResult, *, limit: int = 15) -> str:
    date_text = result.report_date.isoformat()
    lines = [
        f"📡 每日選股雷達 {date_text}",
        _radar_mode_line(result.request),
        f"評分版本：{_scoring_version_label(result.request.scoring_version)}",
        "",
    ]
    date_note = str((result.diagnostics or {}).get("date_note") or "")
    if date_note:
        lines.extend([f"提示：{date_note}", ""])
    evidence_status = (result.diagnostics or {}).get("evidence_pack_status") or {}
    if evidence_status.get("selected"):
        lines.extend(
            [
                "Evidence Pack："
                f"{evidence_status.get('success', 0)}/{evidence_status.get('selected', 0)} 成功，"
                f"{evidence_status.get('timeout', 0)} 檔逾時，"
                f"{evidence_status.get('failed', 0)} 檔失敗",
                "",
            ]
        )
    if not result.candidates:
        lines.append("目前沒有可評分候選股。")
        return "\n".join(lines)

    early_candidates = [
        item for item in result.candidates[:limit]
        if _radar_candidate_tag(item) in {"早期轉強", "轉機波段", "題材重估", "籌碼轉強"}
    ][:3]
    if early_candidates:
        lines.append("📈 早期波段候選")
        for item in early_candidates:
            tag = _radar_candidate_tag(item)
            reasons = "、".join(item.key_reasons[:3]) if item.key_reasons else "技術/籌碼/基本面轉強"
            stock_label = mark_stock_text(f"{item.code} {item.name}".strip())
            lines.append(f"{stock_label}｜{item.total_score}分｜{tag}｜{reasons}")
        lines.append("")

    for rank, item in enumerate(result.candidates[:limit], 1):
        strategy = _strategy_codes_label(item.strategy_codes)
        ai_badge = _ai_badge(item)
        labels = "、".join(_display_source_labels(item)[:3])
        components = item.score_components
        evidence = _candidate_evidence_line(item)
        technical_line = _technical_signal_line(item)
        chip_line = _chip_grade_line(item)
        ai_lines = _ai_comment_lines(item)
        tag = _radar_candidate_tag(item)
        tag_text = f"｜{tag}" if tag else ""
        stock_label = mark_stock_text(f"{item.code} {item.name}".strip())
        lines.extend(
            [
                f"{rank}. {stock_label}｜{item.total_score}分｜技術策略：{strategy}{ai_badge}{tag_text}",
                f"   技術 {components.get('technical', 0)}｜營收 {components.get('revenue', 0)}｜財報 {components.get('financial', 0)}｜籌碼 {components.get('chip', 0)}｜題材 {components.get('theme', 0)}｜族群 {_component_sector_score(components)}",
                f"   {item.industry or '未分類'}｜{labels or '候選來源'}",
            ]
        )
        if technical_line:
            lines.append(f"   技術訊號：{technical_line}")
        if evidence:
            lines.append(f"   {evidence}")
        if chip_line:
            lines.append(f"   籌碼：{chip_line}")
        if item.key_reasons:
            lines.append(f"   關鍵線索：{'、'.join(item.key_reasons[:5])}")
        if item.risk_flags:
            lines.append(f"   風險：{'、'.join(item.risk_flags[:3])}")
        lines.extend(f"   {line}" for line in ai_lines)
        lines.append("")

    if len(result.candidates) > limit:
        lines.append(f"完整名單共 {len(result.candidates)} 檔，可用 /radar_more 查看。")
    lines.append("資料來源：既有選股流程 / 本地新聞資料庫 / 外部搜尋來源（若已設定）")
    return "\n".join(lines).strip()


def format_radar_push_summary(result: RadarResult, *, limit: int = 15) -> str:
    """Format a concise scheduled Radar push while preserving the full artifact separately."""
    date_text = result.report_date.isoformat()
    lines = [
        f"📡 每日選股雷達 {date_text}",
        _radar_mode_line(result.request),
        f"評分版本：{_scoring_version_label(result.request.scoring_version)}",
        "",
    ]
    evidence_status = (result.diagnostics or {}).get("evidence_pack_status") or {}
    if evidence_status.get("selected"):
        lines.append(
            "外部證據："
            f"{evidence_status.get('success', 0)}/{evidence_status.get('selected', 0)} 成功，"
            f"{evidence_status.get('timeout', 0)} 檔逾時，"
            f"{evidence_status.get('failed', 0)} 檔失敗"
        )
        lines.append("")
    if not result.candidates:
        lines.append("目前沒有可評分候選股。")
        return "\n".join(lines).strip()

    top_limit = max(1, min(limit, len(result.candidates)))
    lines.append(f"Top {top_limit} 重點候選：")
    for rank, item in enumerate(result.candidates[:top_limit], 1):
        components = item.score_components or {}
        tag = _radar_candidate_tag(item)
        tag_text = f"｜{tag}" if tag else ""
        reasons = "、".join(item.key_reasons[:3]) if item.key_reasons else "技術、籌碼或題材轉強"
        stock_label = mark_stock_text(f"{item.code} {item.name}".strip())
        lines.extend(
            [
                f"{rank}. {stock_label}｜{item.total_score}分{tag_text}",
                f"   技術 {components.get('technical', 0)}｜營收 {components.get('revenue', 0)}｜財報 {components.get('financial', 0)}｜籌碼 {components.get('chip', 0)}｜題材 {components.get('theme', 0)}｜族群 {_component_sector_score(components)}",
                f"   關鍵線索：{_truncate_radar_text(reasons, limit=120)}",
            ]
        )
        if item.risk_flags:
            lines.append(f"   主要風險：{_truncate_radar_text('、'.join(item.risk_flags[:2]), limit=100)}")
        ai_lines = _ai_comment_lines(item)[:2]
        lines.extend(f"   {line}" for line in ai_lines)
        lines.append("")

    if len(result.candidates) > top_limit:
        lines.append(f"完整名單共 {len(result.candidates)} 檔，請用 /radar_more 查看完整雷達報告。")
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
        return f"來源：{_source_label(request.source)}｜AI短評：{_model_label(request.model)}｜AI補強 Top {request.ai_top}"
    if request.ai_comment_enabled:
        return f"來源：{_source_label(request.source)}｜外部來源補強：AI補強 Top {request.ai_top}"
    return f"來源：{_source_label(request.source)}｜AI短評：略過"


def _scoring_version_label(version: str | None) -> str:
    return _resolve_scoring_version(version)


def _model_label(model: str | None) -> str:
    return {"gemini": "Gemini", "deepseek": "DeepSeek", "minimax": "MiniMax"}.get(str(model or ""), str(model or ""))


def _strategy_codes_label(strategy_codes: set[str] | list[str] | tuple[str, ...] | None) -> str:
    codes = [str(code).strip() for code in sorted(strategy_codes or []) if str(code).strip()]
    if not codes:
        return "未標示"
    labels = []
    for code in codes:
        label = TECHNICAL_STRATEGY_LABELS.get(code)
        labels.append(f"{code}（{label}）" if label else f"{code}（技術策略）")
    return "、".join(labels)


def _ai_badge(item: RadarCandidate) -> str:
    comment = item.ai_comment or {}
    if comment.get("status") == "ok":
        return f"｜AI短評信心：{_priority_label(comment.get('priority') or '中')}"
    if comment.get("status") in {"failed", "missing"}:
        return "｜AI短評：未完成"
    return ""


def _priority_label(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return {
        "high": "高",
        "medium": "中",
        "low": "低",
        "高": "高",
        "中": "中",
        "低": "低",
    }.get(raw, str(value or "中"))


def _component_sector_score(components: dict[str, Any]) -> int:
    return int(components.get("sector", components.get("market", 0)) or 0)


def _radar_candidate_tag(item: RadarCandidate) -> str:
    components = item.score_components or {}
    technical = int(components.get("technical", 0) or 0)
    revenue = int(components.get("revenue", 0) or 0)
    financial = int(components.get("financial", 0) or 0)
    chip = int(components.get("chip", 0) or 0)
    theme = int(components.get("theme", 0) or 0)
    risks = " ".join(item.risk_flags or [])
    if "短線過熱" in risks or "乖離" in risks:
        return "短線過熱"
    if technical >= 16 and revenue < 6 and financial < 4 and theme < 5:
        return "僅技術反彈"
    if theme >= 9 and revenue >= 6:
        return "題材重估"
    if revenue >= 10 and financial >= 6:
        return "轉機波段"
    if chip >= 8:
        return "籌碼轉強"
    if theme >= 6 and "題材無法驗證" in risks:
        return "題材未驗證"
    if technical >= 16 and item.total_score >= 60:
        return "早期轉強"
    return ""


def _ai_comment_lines(item: RadarCandidate) -> list[str]:
    comment = item.ai_comment or {}
    if not comment:
        return []
    if comment.get("status") != "ok":
        return ["AI短評：本次模型分析失敗，保留本地 Radar 評分。"]
    lines = []
    if comment.get("reason"):
        lines.append(f"AI短評：{_truncate_radar_text(_clean_radar_ai_display_text(comment['reason']))}")
    if comment.get("risk"):
        lines.append(f"風險：{_truncate_radar_text(_clean_radar_ai_display_text(comment['risk']))}")
    if comment.get("watch"):
        lines.append(f"觀察：{_truncate_radar_text(_clean_radar_ai_display_text(comment['watch']))}")
    return lines


def _clean_radar_ai_display_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""

    # Remove evidence-label wrappers that were useful to prompts but noisy in Telegram.
    evidence_labels = r"(?:verified_fact|reasoned_inference|market_hypothesis|sentiment_signal|insufficient)"
    text = re.sub(rf"[\[(（]\s*{evidence_labels}(?:\s*[+,/]\s*{evidence_labels})*\s*[\])）]", "", text, flags=re.IGNORECASE)

    replacements = [
        (r"\bvolume_quality\s*=\s*false\b", "量能未配合"),
        (r"\bvolume_quality\s*=\s*true\b", "量能配合"),
        (r"\bchip/institutional/margin\b", "籌碼、法人與融資券"),
        (r"\brow_count\s*=\s*0\b", "資料缺漏"),
        (r"\brow_count\s*=\s*(\d+)\b", r"資料筆數 \1"),
        (r"\blimited_by_light_research\b", "輕量資料限制"),
        (r"\bsetup\s+score\b", "技術型態分數"),
        (r"\bscore_components\b", "分數細項"),
        (r"\bdata_coverage\b", "資料覆蓋狀況"),
        (r"\bverified_fact\b", "已驗證"),
        (r"\breasoned_inference\b", "推論"),
        (r"\bmarket_hypothesis\b", "市場假設"),
        (r"\bsentiment_signal\b", "情緒訊號"),
        (r"\binsufficient\b", "資料不足"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    for code, label in TECHNICAL_STRATEGY_LABELS.items():
        text = re.sub(rf"\b策略\s*{re.escape(str(code))}\b", f"技術策略{code}（{label}）", text, flags=re.IGNORECASE)
        text = re.sub(rf"\bstrategy\s*{re.escape(str(code))}\b", f"技術策略{code}（{label}）", text, flags=re.IGNORECASE)

    text = re.sub(r"[\[(（]\s*[\])）]", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([，。；、：])", r"\1", text)
    return text.strip()


def _truncate_radar_text(value: Any, *, limit: int = RADAR_TELEGRAM_AI_TEXT_LIMIT) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def save_radar_result(result: RadarResult) -> dict[str, Any]:
    records = _load_radar_records(limit=30)
    payload = _json_safe(_result_to_record(result))
    payload["artifact_paths"] = _save_radar_artifacts(result, payload)
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


def _save_radar_artifacts(result: RadarResult, record: dict[str, Any]) -> dict[str, str]:
    radar_id = str(record.get("radar_id") or f"radar_{result.report_date.strftime('%Y%m%d')}")
    output_dir = RADAR_REPORT_DIR / result.report_date.isoformat() / radar_id
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "summary": output_dir / "radar_summary.md",
        "candidates": output_dir / "radar_candidates.json",
        "evidence_pack": output_dir / "evidence_pack.json",
        "ai_analysis": output_dir / "ai_analysis.json",
        "sources": output_dir / "sources.json",
    }
    artifacts["summary"].write_text(
        strip_stock_markers(format_radar_report(result, limit=max(50, len(result.candidates)))),
        encoding="utf-8",
    )
    artifacts["candidates"].write_text(
        json.dumps(_json_safe([_candidate_to_dict(item) for item in result.candidates]), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifacts["evidence_pack"].write_text(
        json.dumps(_json_safe([item.evidence_pack for item in result.candidates]), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifacts["ai_analysis"].write_text(
        json.dumps(_json_safe(result.diagnostics.get("ai_analysis") or {}), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    sources = []
    for item in result.candidates:
        for source in [*item.web_sources, *item.ai_sources]:
            if isinstance(source, dict):
                sources.append({"code": item.code, **source})
    artifacts["sources"].write_text(json.dumps(_json_safe(sources), ensure_ascii=False, indent=2), encoding="utf-8")
    return {key: str(path) for key, path in artifacts.items()}


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
    if source == "combined":
        return _combined_candidates(target_date, scan_settings, config or {}, progress)
    if source == "technical":
        return _technical_candidates_for_radar(target_date, scan_settings, progress)
    if source == "curated":
        return _curated_candidates(target_date, scan_settings, progress)
    if source == "laoxiao":
        return _laoxiao_candidates(target_date, scan_settings, progress)
    if source == "financial":
        return _financial_candidates(scan_settings, target_date)
    if source == "chip":
        return _chip_candidates(target_date)
    if source == "monitor":
        return _monitor_candidates(config or {})
    if source == "portfolio":
        return _portfolio_candidates()
    raise ValueError(f"不支援的 Radar 來源：{source}")


def _combined_candidates(
    target_date: date,
    scan_settings: dict[str, float] | None,
    config: dict[str, Any],
    progress: Callable[[str], None] | None,
) -> tuple[list[RadarCandidate], dict[str, Any]]:
    loaders: list[tuple[str, Callable[[], tuple[list[RadarCandidate], dict[str, Any]]]]] = [
        ("technical", lambda: _technical_candidates_for_radar(target_date, scan_settings, progress)),
        ("chip", lambda: _chip_candidates(target_date)),
        ("financial", lambda: _financial_candidates(scan_settings, target_date)),
        ("curated", lambda: _curated_candidates(target_date, scan_settings, progress)),
    ]
    merged: dict[str, RadarCandidate] = {}
    policies: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for source_name, loader in loaders:
        try:
            candidates, policy = loader()
        except Exception as exc:
            failures.append({"source": source_name, "error": str(exc)})
            _emit(progress, f"Radar：combined 來源 {source_name} 載入失敗，略過：{exc}")
            continue
        policy = dict(policy or {})
        policy["source_key"] = source_name
        policies.append(policy)
        for candidate in candidates:
            _merge_radar_candidate(merged, candidate, source_name)
    _emit(progress, f"Radar：combined 跨來源候選 {len(merged)} 檔，來源 {len(policies)} 組")
    return list(merged.values()), {
        "source": "跨來源候選池",
        "status": "combined",
        "candidate_count": len(merged),
        "sources": policies,
        "failures": failures,
    }


def _merge_radar_candidate(merged: dict[str, RadarCandidate], incoming: RadarCandidate, source_name: str) -> None:
    if not incoming.code:
        return
    item = merged.get(incoming.code)
    if item is None:
        merged[incoming.code] = incoming
        _add_label(incoming, f"跨來源/{_source_label(source_name)}")
        return
    if not item.name and incoming.name:
        item.name = incoming.name
    if not item.symbol and incoming.symbol:
        item.symbol = incoming.symbol
    if not item.industry and incoming.industry:
        item.industry = incoming.industry
    if item.price is None and incoming.price is not None:
        item.price = incoming.price
    item.strategy_codes.update(incoming.strategy_codes)
    item.technical_signals.extend(incoming.technical_signals)
    item.dual_ma_signals.extend(incoming.dual_ma_signals)
    item.kd_ma_signals.extend(incoming.kd_ma_signals)
    item.chip_grades.update(incoming.chip_grades)
    if incoming.revenue_history and not item.revenue_history:
        item.revenue_history = list(incoming.revenue_history)
    item.news_items.extend(incoming.news_items)
    item.web_sources.extend(incoming.web_sources)
    item.ai_sources.extend(incoming.ai_sources)
    for label in incoming.source_labels:
        _add_label(item, label)
    _add_label(item, f"跨來源/{_source_label(source_name)}")


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
    if cached and _is_stale_technical_scan_cache(cached, target_date):
        _emit(progress, _technical_scan_cache_stale_message(cached, target_date))
        cached = None
    if cached:
        raw_codes = cached.get("radar_candidate_codes")
        if not isinstance(raw_codes, list):
            raw_codes = cached.get("selected_codes") or cached.get("codes") or []
        codes = [str(code) for code in raw_codes]
        by_code = _stock_meta_by_code()
        candidates = [_with_label(_candidate_from_meta(code, by_code), "技術面選股快取") for code in codes]
        signals = _normalise_strategy_signals(cached.get("strategy_signals"))
        dual_ma_signals = _normalise_dual_ma_signals(cached.get("dual_ma_signals"))
        kd_ma_signals = _normalise_kd_ma_signals(cached.get("kd_ma_signals"))
        refreshed_result = None
        if (not isinstance(cached.get("strategy_signals"), dict)
                or not isinstance(cached.get("dual_ma_signals"), list)
                or not isinstance(cached.get("kd_ma_signals"), list)):
            _emit(progress, "Radar：技術面快取缺少策略明細，重跑技術掃描補齊訊號")
            scan_result = ts.run_technical_scan(scan_settings, target_date)
            signals = _normalise_strategy_signals(scan_result.strategy_signals)
            dual_ma_signals = _normalise_dual_ma_signals(getattr(scan_result, "dual_ma_signals", []))
            kd_ma_signals = _normalise_kd_ma_signals(getattr(scan_result, "kd_ma_signals", []))
            refreshed_result = scan_result if isinstance(scan_result, ts.TechnicalScanResult) else None
        _apply_strategy_signals(candidates, signals)
        _apply_dual_ma_signals(candidates, dual_ma_signals, by_code)
        _apply_kd_ma_signals(candidates, kd_ma_signals, by_code)
        if refreshed_result is not None:
            save_recent_scan_result(
                "技術面選股",
                target_date,
                ts.format_technical_report(refreshed_result),
                metadata={
                    "strategy_signals": _json_safe(refreshed_result.strategy_signals),
                    "dual_ma_signals": _json_safe(refreshed_result.dual_ma_signals),
                    "kd_ma_signals": _json_safe(refreshed_result.kd_ma_signals),
                    "radar_candidate_codes": [item.code for item in candidates],
                },
            )
        _emit(progress, f"Radar：使用技術面選股快取 {len(candidates)} 檔")
        return candidates, {
            "source": "技術面選股結果",
            "status": "cached",
            "candidate_count": len(candidates),
            "strategy_signal_count": _strategy_signal_count(signals),
            "dual_ma_signal_count": len(dual_ma_signals),
            "kd_ma_signal_count": len(kd_ma_signals),
        }

    result = ts.run_technical_scan(scan_settings, target_date)
    report_text = ts.format_technical_report(result)
    by_code = _stock_meta_by_code()
    candidates = _candidates_from_strategy_signals(result.strategy_signals, by_code)
    dual_ma_signals = _normalise_dual_ma_signals(getattr(result, "dual_ma_signals", []))
    kd_ma_signals = _normalise_kd_ma_signals(getattr(result, "kd_ma_signals", []))
    candidate_list = list(candidates.values())
    _apply_dual_ma_signals(candidate_list, dual_ma_signals, by_code)
    _apply_kd_ma_signals(candidate_list, kd_ma_signals, by_code)
    save_recent_scan_result(
        "技術面選股",
        target_date,
        report_text,
        metadata={
            "strategy_signals": _json_safe(result.strategy_signals),
            "dual_ma_signals": _json_safe(dual_ma_signals),
            "kd_ma_signals": _json_safe(kd_ma_signals),
            "radar_candidate_codes": [item.code for item in candidate_list],
        },
    )
    _emit(progress, f"Radar：技術面選股產生 {len(candidate_list)} 檔")
    return candidate_list, {
        "source": "技術面選股結果",
        "status": "generated",
        "candidate_count": len(candidate_list),
        "strategy_signal_count": _strategy_signal_count(_normalise_strategy_signals(result.strategy_signals)),
        "dual_ma_signal_count": len(dual_ma_signals),
        "kd_ma_signal_count": len(kd_ma_signals),
    }


def _find_technical_scan_cache(target_date: date) -> dict[str, Any] | None:
    for record in load_recent_scan_results(limit=30):
        if str(record.get("report_date")) != target_date.isoformat():
            continue
        scan_type = str(record.get("scan_type") or "")
        if "技術" in scan_type and "選股" in scan_type:
            return record
    return None


def _parse_scan_created_at(record: dict[str, Any]) -> datetime | None:
    raw = str(record.get("created_at") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_stale_technical_scan_cache(record: dict[str, Any], target_date: date) -> bool:
    created_at = _parse_scan_created_at(record)
    if created_at is None:
        return False
    created_date = created_at.date()
    if created_date < target_date:
        return True
    if created_date > target_date:
        return False
    ready_time = (RADAR_TECHNICAL_CACHE_READY_HOUR, RADAR_TECHNICAL_CACHE_READY_MINUTE)
    return (created_at.hour, created_at.minute) < ready_time


def _technical_scan_cache_stale_message(record: dict[str, Any], target_date: date) -> str:
    created_at = str(record.get("created_at") or "未知")
    return (
        "Radar：略過收盤前技術面選股快取，"
        f"資料日期 {target_date.isoformat()}，建立時間 {created_at}，將重新執行技術面掃描"
    )


def _normalise_strategy_signals(value: Any) -> dict[str, list[dict[str, Any]]]:
    signals: dict[str, list[dict[str, Any]]] = {"A": [], "B": [], "C": [], "D": []}
    if not isinstance(value, dict):
        return signals
    for strategy in signals:
        raw_items = value.get(strategy) or []
        if isinstance(raw_items, list):
            signals[strategy] = [dict(item) for item in raw_items if isinstance(item, dict)]
    return signals


def _normalise_dual_ma_signals(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _normalise_kd_ma_signals(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


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


def _apply_dual_ma_signals(
    candidates: list[RadarCandidate],
    signals: list[dict[str, Any]],
    by_code: dict[str, Any],
) -> None:
    candidates_by_code = {item.code: item for item in candidates}
    for signal in signals:
        code = str(signal.get("stock_id") or signal.get("code") or "")
        if not code:
            continue
        item = candidates_by_code.get(code)
        if item is None:
            item = _candidate_from_meta(code, by_code)
            candidates_by_code[code] = item
            candidates.append(item)
        if not any(existing.get("signal_date") == signal.get("signal_date") for existing in item.dual_ma_signals):
            item.dual_ma_signals.append(signal)
        _add_label(item, ts.dual_ma_signal_label(signal))


def _apply_kd_ma_signals(
    candidates: list[RadarCandidate],
    signals: list[dict[str, Any]],
    by_code: dict[str, Any],
) -> None:
    candidates_by_code = {item.code: item for item in candidates}
    for signal in signals:
        code = str(signal.get("stock_id") or signal.get("code") or "")
        if not code:
            continue
        item = candidates_by_code.get(code)
        if item is None:
            item = _candidate_from_meta(code, by_code)
            candidates_by_code[code] = item
            candidates.append(item)
        key = (signal.get("signal_date"), signal.get("primary_group"))
        if not any((existing.get("signal_date"), existing.get("primary_group")) == key for existing in item.kd_ma_signals):
            item.kd_ma_signals.append(signal)
        _add_label(item, ts.kd_ma_signal_label(signal))


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
        save_recent_scan_result(
            "精選選股",
            target_date,
            curated.report_text,
            curated.selected_codes,
            metadata={"scoring_version": resolve_radar_scoring_version()},
        )
        status = "generated"
    by_code = _stock_meta_by_code()
    return [_with_label(_candidate_from_meta(code, by_code), "精選選股") for code in codes], {
        "source": "精選選股結果",
        "status": status,
        "candidate_count": len(codes),
    }


def _financial_candidates(
    scan_settings: dict[str, float] | None,
    target_date: date | None = None,
) -> tuple[list[RadarCandidate], dict[str, Any]]:
    report = scan_tw_market(False, None, scan_settings, report_date=target_date)
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


def _laoxiao_candidates(
    target_date: date,
    scan_settings: dict[str, float] | None,
    progress: Callable[[str], None] | None,
) -> tuple[list[RadarCandidate], dict[str, Any]]:
    cached = laoxiao_scan_service.find_cached_laoxiao_scan(target_date)
    by_code = _stock_meta_by_code()
    if cached:
        codes = [str(code) for code in cached.get("selected_codes") or cached.get("codes") or []]
        candidates = [_with_label(_candidate_from_meta(code, by_code), "老蕭選股快取") for code in codes]
        _emit(progress, f"Radar：使用老蕭選股快取 {len(candidates)} 檔")
        return candidates, {
            "source": "老蕭選股結果",
            "status": "cached",
            "candidate_count": len(candidates),
            "strategy_scoring_version": laoxiao_scan_service.SCORING_VERSION,
        }

    _emit(progress, "Radar：沒有老蕭選股快取，執行老蕭選股流程")
    result = laoxiao_scan_service.build_laoxiao_scan_result(scan_settings, target_date, progress=progress)
    save_recent_scan_result(
        laoxiao_scan_service.SCAN_TYPE,
        target_date,
        result.report_text,
        result.selected_codes,
        metadata={
            "scoring_version": laoxiao_scan_service.SCORING_VERSION,
            "diagnostics": result.diagnostics,
        },
    )
    selected_by_code = {item.code: item for item in result.candidates}
    candidates = []
    for code in result.selected_codes:
        candidate = _candidate_from_meta(code, by_code)
        selected = selected_by_code.get(code)
        setup_label = "強勢突破型" if selected and selected.setup_type == "strong_breakout" else "拉回轉強型"
        _add_label(candidate, f"老蕭選股/{setup_label}")
        if selected:
            candidate.revenue_history = list(selected.revenue_history)
        candidates.append(candidate)
    return candidates, {
        "source": "老蕭選股結果",
        "status": "generated",
        "candidate_count": len(candidates),
        "strategy_scoring_version": laoxiao_scan_service.SCORING_VERSION,
        "diagnostics": result.diagnostics,
    }


def _chip_candidates(target_date: date) -> tuple[list[RadarCandidate], dict[str, Any]]:
    grade_maps = _load_radar_chip_grade_maps(target_date)
    by_code = _stock_meta_by_code()
    candidates: dict[str, RadarCandidate] = {}
    for key, grades in grade_maps.items():
        for code, grade in grades.items():
            item = candidates.setdefault(code, _candidate_from_meta(code, by_code))
            item.chip_grades[key] = grade
            _add_label(item, f"籌碼/{key}:{grade}")
    return list(candidates.values()), {"source": "法人籌碼 / 大戶選股結果", "status": "generated", "candidate_count": len(candidates)}


def _load_radar_chip_grade_maps(target_date: date) -> dict[str, dict[str, str]]:
    cache_key = target_date.isoformat()
    if cache_key in _RADAR_CHIP_GRADE_CACHE:
        return _RADAR_CHIP_GRADE_CACHE[cache_key]
    context = build_market_context(
        False,
        target_date,
        include_daily_data=True,
        include_foreign_ratio=False,
        target_trading_days=5,
        scope="radar",
    )
    grade_maps = build_chip_grade_maps(context, CHIP_KEYS)
    _RADAR_CHIP_GRADE_CACHE[cache_key] = grade_maps
    return grade_maps


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
        grade_maps = _load_radar_chip_grade_maps(target_date)
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


def _attach_revenue_scores(candidates: list[RadarCandidate], analysis_date: date | None = None) -> None:
    universe = load_stock_universe(False)
    code_map = {entry.code: entry for entry in universe}
    selected = [code_map[item.code] for item in candidates if item.code in code_map]
    revenue = load_recent_revenue_history(selected, as_of_date=analysis_date)
    for item in candidates:
        points = revenue.get(item.code) or []
        latest = points[0] if points else None
        yoy = getattr(latest, "yoy", None) if latest else None
        item.revenue_history = [
            {
                "month": point.month,
                "revenue": point.revenue,
                "yoy": point.yoy,
                "published_at": point.published_at,
                "published_at_source": point.published_at_source,
            }
            for point in points
        ]
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


def prepare_radar_scoring_data(
    candidates: list[RadarCandidate],
    analysis_date: date,
    progress: Callable[[str], None] | None = None,
    *,
    financial_fetch_limit: int | None = None,
    scoring_version: str | None = None,
) -> None:
    """Attach structured data used by Radar scoring before the first score pass."""

    if not candidates:
        return
    _emit(progress, f"Radar：評分前資料補齊 {len(candidates)} 檔")
    structured_by_code: dict[str, dict[str, Any]] = {}
    for item in candidates:
        structured, structured_date = _load_radar_structured_snapshot(item.code, analysis_date)
        structured_data = dict(structured or {})
        if structured_date:
            structured_data.setdefault("structured_cache_date", structured_date.isoformat())
        structured_data.setdefault("stock", {"code": item.code, "name": item.name, "symbol": item.symbol, "industry": item.industry})
        structured_data.setdefault("report_date", analysis_date.isoformat())
        structured_data.setdefault("radar_research_mode", "pre_score_prepared")
        structured_data.setdefault("revenue_data", item.revenue_history[:24])
        structured_by_code[item.code] = structured_data

    _merge_radar_chip_cache_data(candidates, structured_by_code, analysis_date)
    _merge_radar_topic_context(candidates, structured_by_code)
    resolved_scoring_version = _resolve_scoring_version(scoring_version)
    if resolved_scoring_version == "v3":
        _merge_radar_revenue_peer_context(candidates, structured_by_code, analysis_date, progress)
        _merge_radar_valuation_context(candidates, structured_by_code, analysis_date, progress)
    financial_stats = _merge_radar_financial_data(
        candidates,
        structured_by_code,
        analysis_date,
        progress,
        fetch_limit=financial_fetch_limit,
        require_unified_fields=resolved_scoring_version == "v3",
    )
    margin_stats = _merge_radar_margin_data(candidates, structured_by_code, analysis_date, progress)

    for item in candidates:
        structured_data = structured_by_code.get(item.code) or {}
        structured_data["unified_financial_field_coverage"] = _unified_financial_field_coverage(
            _structured_rows(structured_data, "financial_data")
        )
        item.evidence_pack["research_structured_data"] = structured_data
        item.data_coverage = _build_radar_data_coverage(item, structured_data)

    _emit(
        progress,
        "Radar：評分前資料補齊完成，"
        f"財報 {financial_stats.get('covered', 0)}/{financial_stats.get('attempted', 0)}、"
        f"融資券 {margin_stats.get('covered', 0)}/{margin_stats.get('attempted', 0)}",
    )


def _merge_radar_chip_cache_data(
    candidates: list[RadarCandidate],
    structured_by_code: dict[str, dict[str, Any]],
    analysis_date: date,
) -> None:
    codes = {item.code for item in candidates}
    institutional_map = _load_radar_institutional_cache(codes, analysis_date)
    tdcc_map = _load_radar_tdcc_cache(codes, analysis_date)
    for item in candidates:
        structured = structured_by_code[item.code]
        if not _structured_rows(structured, "institutional_data"):
            rows = institutional_map.get(item.code) or []
            if rows:
                structured["institutional_data"] = rows
        if not structured.get("tdcc_data"):
            tdcc = tdcc_map.get(item.code) or {}
            if tdcc:
                structured["tdcc_data"] = tdcc


def _merge_radar_topic_context(candidates: list[RadarCandidate], structured_by_code: dict[str, dict[str, Any]]) -> None:
    for item in candidates:
        structured = structured_by_code[item.code]
        if structured.get("topic_context"):
            continue
        try:
            topic_context = build_stock_topic_context(item.code, item.name)
        except Exception as exc:
            topic_context = {"status": "unavailable", "error": str(exc)[:200]}
        if topic_context:
            structured["topic_context"] = topic_context


def _merge_radar_revenue_peer_context(
    candidates: list[RadarCandidate],
    structured_by_code: dict[str, dict[str, Any]],
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> None:
    """Attach same-industry revenue breadth using the official industry map."""

    missing = [item for item in candidates if not structured_by_code[item.code].get("peer_revenue_context")]
    if not missing:
        return
    try:
        universe = load_stock_universe(False)
        history_map = load_recent_revenue_history(universe, months_to_fetch=24, as_of_date=analysis_date)
    except Exception as exc:
        for item in missing:
            structured_by_code[item.code]["peer_revenue_context"] = {
                "status": "unavailable",
                "reason": str(exc)[:200],
            }
        return

    entry_by_code = {entry.code: entry for entry in universe}
    effective_by_code: dict[str, list[dict[str, Any]]] = {}
    for code, points in history_map.items():
        rows = [
            {
                "month": point.month,
                "revenue": point.revenue,
                "yoy": point.yoy,
                "published_at": point.published_at,
                "published_at_source": point.published_at_source,
            }
            for point in points
        ]
        effective_by_code[code] = effective_revenue_rows(rows)

    for item in missing:
        own_rows = effective_by_code.get(item.code) or effective_revenue_rows(item.revenue_history)
        latest = own_rows[-1] if own_rows else {}
        latest_month = str(latest.get("month") or latest.get("Month") or "")[:7]
        positives = 0
        valid = 0
        for code, rows in effective_by_code.items():
            if code == item.code:
                continue
            entry = entry_by_code.get(code)
            if entry is None or entry.industry != item.industry or not rows:
                continue
            peer_latest = rows[-1]
            peer_month = str(peer_latest.get("month") or peer_latest.get("Month") or "")[:7]
            if not latest_month or peer_month != latest_month:
                continue
            yoy = _to_float(peer_latest.get("yoy"))
            if yoy is None:
                continue
            valid += 1
            positives += int(yoy > 0)
        structured_by_code[item.code]["peer_revenue_context"] = {
            "status": "official_industry" if valid else "insufficient",
            "industry": item.industry,
            "period": latest_month or None,
            "valid_peer_count": valid,
            "positive_peer_count": positives,
            "positive_ratio": positives / valid if valid else None,
        }
    _emit(progress, f"Radar：同產業營收廣度補齊 {len(missing)} 檔")


def _merge_radar_valuation_context(
    candidates: list[RadarCandidate],
    structured_by_code: dict[str, dict[str, Any]],
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> None:
    """Attach official PE/PB history and same-industry peers for v3 scoring."""

    missing = []
    for item in candidates:
        valuation = structured_by_code[item.code].get("valuation_data") or {}
        history_count = len(valuation.get("history") or []) if isinstance(valuation, dict) else 0
        peer_count = len(valuation.get("peers") or []) if isinstance(valuation, dict) else 0
        if history_count < 36 or peer_count < 3:
            missing.append(item)
    if not missing:
        return
    try:
        universe = load_stock_universe(False)
        contexts = build_valuation_context_map([item.code for item in missing], universe, analysis_date, months=60)
    except Exception as exc:
        contexts = {
            item.code: {"status": "unavailable", "reason": str(exc)[:200]}
            for item in missing
        }
    for item in missing:
        structured_by_code[item.code]["valuation_data"] = contexts.get(item.code) or {"status": "unavailable"}
    _emit(progress, f"Radar：官方 PE/PB 歷史與同業估值補齊 {len(missing)} 檔")


def _merge_radar_financial_data(
    candidates: list[RadarCandidate],
    structured_by_code: dict[str, dict[str, Any]],
    analysis_date: date,
    progress: Callable[[str], None] | None,
    *,
    fetch_limit: int | None = None,
    require_unified_fields: bool = False,
) -> dict[str, int]:
    missing = [
        item
        for item in _radar_pre_score_priority(candidates)
        if not _structured_rows(structured_by_code[item.code], "financial_data")
        or (
            require_unified_fields
            and structured_by_code[item.code].get("financial_data_schema_version") != "unified_v3"
        )
    ]
    limit = RADAR_PRE_SCORE_FINANCIAL_FETCH_LIMIT if fetch_limit is None else max(0, int(fetch_limit))
    selected = missing[:limit]
    stats = {"attempted": len(selected), "covered": 0}
    if not selected:
        return stats
    _emit(progress, f"Radar：評分前補財報 {len(selected)} 檔（timeout {RADAR_PRE_SCORE_FETCH_TIMEOUT_SECONDS:.0f}s）")

    def fetch(item: RadarCandidate) -> tuple[str, list[dict[str, Any]], str | None]:
        try:
            fetcher = StockDataFetcher()
            meta = fetcher.resolve_stock(item.code)
            frame = fetcher.fetch_quarterly_financials(meta)
            rows = _financial_frame_to_records(frame, analysis_date)
            return item.code, rows, None
        except Exception as exc:
            return item.code, [], str(exc)[:200]

    results = _run_radar_pre_score_fetches(selected, fetch, RADAR_PRE_SCORE_FETCH_TIMEOUT_SECONDS)
    for code, rows, error in results:
        structured = structured_by_code.get(code)
        if not structured:
            continue
        if rows:
            structured["financial_data"] = rows
            structured["financial_data_schema_version"] = "unified_v3"
            stats["covered"] += 1
            _save_radar_pre_score_structured_cache(code, analysis_date, structured)
        elif error:
            structured.setdefault("data_gap_summary", {}).setdefault("pre_score_errors", {})["financial_data"] = error
        else:
            structured.setdefault("data_gap_summary", {}).setdefault("pre_score_errors", {})[
                "financial_data"
            ] = "official_financial_fetch_returned_empty"
    return stats


def _merge_radar_margin_data(
    candidates: list[RadarCandidate],
    structured_by_code: dict[str, dict[str, Any]],
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> dict[str, int]:
    missing = [item for item in _radar_pre_score_priority(candidates) if not _structured_rows(structured_by_code[item.code], "margin_data")]
    selected = missing[:RADAR_PRE_SCORE_MARGIN_FETCH_LIMIT]
    stats = {"attempted": len(selected), "covered": 0}
    if not selected:
        return stats
    _emit(progress, f"Radar：評分前補融資券 {len(selected)} 檔（timeout {RADAR_PRE_SCORE_FETCH_TIMEOUT_SECONDS:.0f}s）")

    def fetch(item: RadarCandidate) -> tuple[str, list[dict[str, Any]], str | None]:
        try:
            fetcher = StockDataFetcher()
            meta = fetcher.resolve_stock(item.code)
            trading_dates = _radar_trading_dates_for_margin(structured_by_code.get(item.code) or {}, analysis_date)
            frame = fetcher.fetch_margin_daily(meta, trading_dates)
            rows = _margin_frame_to_records(frame, analysis_date)
            return item.code, rows, None
        except Exception as exc:
            return item.code, [], str(exc)[:200]

    results = _run_radar_pre_score_fetches(selected, fetch, min(45.0, RADAR_PRE_SCORE_FETCH_TIMEOUT_SECONDS))
    for code, rows, error in results:
        structured = structured_by_code.get(code)
        if not structured:
            continue
        if rows:
            structured["margin_data"] = rows
            stats["covered"] += 1
            _save_radar_pre_score_structured_cache(code, analysis_date, structured)
        elif error:
            structured.setdefault("data_gap_summary", {}).setdefault("pre_score_errors", {})["margin_data"] = error
        else:
            structured.setdefault("data_gap_summary", {}).setdefault("pre_score_errors", {})[
                "margin_data"
            ] = "official_margin_fetch_returned_empty"
    return stats


def _run_radar_pre_score_fetches(
    selected: list[RadarCandidate],
    fetch: Callable[[RadarCandidate], tuple[str, list[dict[str, Any]], str | None]],
    timeout_seconds: float,
) -> list[tuple[str, list[dict[str, Any]], str | None]]:
    results: list[tuple[str, list[dict[str, Any]], str | None]] = []
    executor = ThreadPoolExecutor(max_workers=max(1, min(RADAR_PRE_SCORE_WORKERS, len(selected))))
    futures = {executor.submit(fetch, item): item for item in selected}
    deadline = time.monotonic() + timeout_seconds
    try:
        for future, item in list(futures.items()):
            remaining = max(0.1, deadline - time.monotonic())
            try:
                results.append(future.result(timeout=remaining))
            except FuturesTimeoutError:
                results.append((item.code, [], "pre_score_fetch_timeout"))
            except Exception as exc:
                results.append((item.code, [], str(exc)[:200]))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def _radar_pre_score_priority(candidates: list[RadarCandidate]) -> list[RadarCandidate]:
    return sorted(
        candidates,
        key=lambda item: (
            -int(item.total_score or 0),
            -len(item.strategy_codes),
            -len(item.technical_signals),
            -sum({"S": 4, "A": 3, "B": 2, "C": 1}.get(str(value).upper(), 0) for value in item.chip_grades.values()),
            item.code,
        ),
    )


def _load_radar_institutional_cache(codes: set[str], analysis_date: date, days: int = 60) -> dict[str, list[dict[str, Any]]]:
    cache_dir = ROOT_DIR / ".cache" / "chip_daily"
    rows_by_code: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
    if not cache_dir.exists():
        return {}
    cutoff = analysis_date.strftime("%Y%m%d")
    for path in sorted(cache_dir.glob("*.csv"), key=lambda item: item.stem, reverse=True):
        if path.stem > cutoff:
            continue
        if all(len(rows) >= days for rows in rows_by_code.values()):
            break
        try:
            frame = pd.read_csv(path, dtype={"code": str})
        except Exception:
            continue
        if "code" not in frame.columns:
            continue
        subset = frame[frame["code"].astype(str).str.strip().isin(codes)]
        for _, raw in subset.iterrows():
            code = str(raw.get("code") or "").strip()
            if code not in rows_by_code or len(rows_by_code[code]) >= days:
                continue
            row_date = str(raw.get("date") or _date_from_cache_path(path))
            foreign = _to_float(raw.get("foreign_net_lots"))
            trust = _to_float(raw.get("trust_net_lots"))
            rows_by_code[code].append(
                {
                    "Date": row_date,
                    "code": code,
                    "Foreign_Net_Lots": foreign,
                    "foreign_net_lots": foreign,
                    "Investment_Trust_Net_Lots": trust,
                    "trust_net_lots": trust,
                    "Dealer_Net_Lots": _to_float(raw.get("dealer_net_lots")) or 0.0,
                    "dealer_net_lots": _to_float(raw.get("dealer_net_lots")) or 0.0,
                    "foreign_ratio_pct": _to_float(raw.get("foreign_ratio_pct")),
                    "source": raw.get("source"),
                }
            )
    return {code: sorted(rows, key=lambda item: str(item.get("Date") or "")) for code, rows in rows_by_code.items() if rows}


def _load_radar_tdcc_cache(codes: set[str], analysis_date: date, weeks: int = 8) -> dict[str, dict[str, Any]]:
    cache_dir = ROOT_DIR / ".cache" / "tdcc"
    if not cache_dir.exists():
        return {}
    rows_by_code: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
    cutoff = analysis_date.strftime("%Y%m%d")
    for path in sorted(cache_dir.glob("*.csv"), key=lambda item: item.stem, reverse=True):
        if path.stem > cutoff:
            continue
        if all(len(rows) >= weeks for rows in rows_by_code.values()):
            break
        try:
            frame = pd.read_csv(path, dtype=str)
        except Exception:
            continue
        code_col = _find_radar_column(frame, ("證券代號", "stock", "code"))
        level_col = _find_radar_column(frame, ("持股分級", "level"))
        pct_col = _find_radar_column(frame, ("占集保庫存數比例", "比例", "%"))
        people_col = _find_radar_column(frame, ("人數", "people"))
        if not code_col or not level_col:
            continue
        subset = frame[frame[code_col].astype(str).str.strip().isin(codes)]
        for code, group in subset.groupby(subset[code_col].astype(str).str.strip()):
            if code not in rows_by_code or len(rows_by_code[code]) >= weeks:
                continue
            big_pct = 0.0
            retail_pct = 0.0
            total_people = 0
            for _, raw in group.iterrows():
                level = int(_to_float(raw.get(level_col)) or -1)
                pct = _to_float(raw.get(pct_col)) or 0.0
                total_people += int(_to_float(raw.get(people_col)) or 0) if people_col else 0
                if level in {12, 13, 14, 15}:
                    big_pct += pct
                if level in {1, 2, 3, 4, 5, 6, 7, 8}:
                    retail_pct += pct
            rows_by_code[code].append(
                {
                    "snapshot_date": _date_from_cache_path(path),
                    "big_holder_pct": round(big_pct, 2),
                    "large_holder_pct": round(big_pct, 2),
                    "retail_holder_pct": round(retail_pct, 2),
                    "total_people": total_people,
                    "source_file": path.name,
                }
            )
    result: dict[str, dict[str, Any]] = {}
    for code, rows in rows_by_code.items():
        if not rows:
            continue
        latest = rows[0]
        result[code] = {**latest, "status": "covered", "rows": rows}
    return result


def _financial_frame_to_records(frame: pd.DataFrame, analysis_date: date) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    records = [_json_safe(row) for row in frame.to_dict(orient="records")]
    return [row for row in records if isinstance(row, dict) and _financial_quarter_available(row.get("Quarter"), analysis_date)]


def _financial_quarter_available(quarter: Any, analysis_date: date) -> bool:
    match = re.match(r"^(\d{4})Q([1-4])$", str(quarter or "").strip().upper())
    if not match:
        return False
    year = int(match.group(1))
    q = int(match.group(2))
    due_dates = {
        1: date(year, 5, 15),
        2: date(year, 8, 14),
        3: date(year, 11, 14),
        4: date(year + 1, 3, 31),
    }
    return due_dates[q] <= analysis_date


def _margin_frame_to_records(frame: pd.DataFrame, analysis_date: date) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    records = [_json_safe(row) for row in frame.to_dict(orient="records")]
    return [
        row
        for row in records
        if isinstance(row, dict) and str(row.get("Date") or "")[:10] <= analysis_date.isoformat()
    ][-60:]


def _radar_trading_dates_for_margin(structured: dict[str, Any], analysis_date: date) -> list[date]:
    rows = _structured_rows(structured, "institutional_data")
    dates: list[date] = []
    for row in rows[-20:]:
        parsed = _parse_date(str(row.get("Date") or row.get("date") or ""))
        if parsed and parsed <= analysis_date:
            dates.append(parsed)
    if dates:
        return dates[-10:]
    result = []
    cursor = analysis_date
    while len(result) < 10:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(result)


def _save_radar_pre_score_structured_cache(code: str, analysis_date: date, structured: dict[str, Any]) -> None:
    try:
        save_research_structured_cache(code, analysis_date, structured)
    except Exception:
        return


def _find_radar_column(frame: pd.DataFrame, keywords: tuple[str, ...]) -> str | None:
    for column in frame.columns:
        text = str(column).lower()
        if any(keyword.lower() in text for keyword in keywords):
            return str(column)
    return None


def _date_from_cache_path(path: Path) -> str:
    try:
        return datetime.strptime(path.stem[:8], "%Y%m%d").date().isoformat()
    except Exception:
        return path.stem


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
        _merge_source_dicts(item.web_sources, [_source_to_dict(source) for source in sources])


def _ensure_radar_source_sufficiency(
    candidates: list[RadarCandidate],
    ai_codes: list[str],
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> None:
    by_code = {item.code: item for item in candidates}
    lacking = [
        code
        for code in ai_codes
        if code in by_code and _candidate_external_source_count(by_code[code]) < RADAR_MIN_EXTERNAL_SOURCES
    ]
    if not lacking:
        return
    _emit(progress, f"Radar：{len(lacking)} 檔外部來源不足，追加補搜")
    _attach_web_sources(candidates, lacking, analysis_date, progress)
    for code in lacking:
        item = by_code.get(code)
        if item is None:
            continue
        count = _candidate_external_source_count(item)
        if count < RADAR_MIN_EXTERNAL_SOURCES:
            _emit(progress, f"Radar：{code} 外部來源仍不足 {count}/{RADAR_MIN_EXTERNAL_SOURCES}，保留不足標記")


def _candidate_external_source_count(item: RadarCandidate) -> int:
    seen: set[str] = set()
    count = 0
    for source in [*item.ai_sources, *item.web_sources]:
        if not isinstance(source, dict):
            continue
        key = str(source.get("url") or source.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        count += 1
    return count


def _merge_source_dicts(target: list[dict[str, Any]], additions: list[dict[str, Any]]) -> None:
    seen = {str(item.get("url") or item.get("title") or "").strip() for item in target if isinstance(item, dict)}
    for source in additions:
        key = str(source.get("url") or source.get("title") or "").strip()
        if key and key in seen:
            continue
        target.append(source)
        if key:
            seen.add(key)


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
            if len(sources) < 8:
                runner._run_tavily(request, tasks, sources, structured_data, progress)
            if len(sources) < 8 and runner._should_run_gemini(request, sources):
                discovery_sources: list[SourceItem] = []
                discovery_runs: list[dict[str, Any]] = []
                runner._run_gemini(request, tasks, sources, structured_data, discovery_sources, discovery_runs, progress)
            sources, _dropped_sources = filter_and_sort_sources_for_analysis_date(sources, request)
            _enrich_sources_with_web_fetch(request, sources, structured_data, progress)
        except Exception as exc:
            _emit(progress, f"Radar：{item.code} 外部來源補強失敗：{exc}")
            continue

        item.web_sources = [_source_to_dict(source) for source in sources]
        item.ai_sources = _normalise_ai_sources(sources, structured_data)


def _radar_discovery_task(item: RadarCandidate, analysis_date: date) -> dict[str, Any]:
    target = f"{item.code} {item.name}".strip()
    industry = item.industry or "台股"
    date_text = analysis_date.isoformat()
    queries = [
        f"{target} 新聞 訂單 法說會 新產品 政策題材 {date_text}",
        f"{target} 月營收 毛利率 EPS 財報 公告 {analysis_date.year}",
        f"{target} 外資 投信 自營商 融資融券 TDCC 大戶籌碼 {analysis_date.year}",
        f"{target} 營收衰退 毛利下滑 庫存 砍單 股價過熱 風險",
        f"{target} {industry} 產業趨勢 供應鏈 關鍵客戶 價值重估",
    ]
    prompt = (
        "請使用搜尋工具尋找下列台股候選股在分析日期以前的可驗證來源。\n"
        "只補 Radar AI 短評來源，不要新增候選股票，不要產生買賣建議。\n"
        "必須盡量覆蓋五類：催化劑、營收與基本面、籌碼資金、反證退燒、題材想像空間。\n"
        "題材想像空間只能標示為推論型資料，不能當成已驗證事實。\n"
        f"候選股：{target}\n"
        f"產業：{industry}\n"
        f"analysis_date：{date_text}\n"
        "不得使用晚於 analysis_date 的來源。"
    )
    return {
        "label": "radar_ai_sources",
        "objective": "補充 Radar AI 短評來源：催化劑、營收基本面、籌碼資金、反證退燒、題材想像空間",
        "queries": queries,
        "prompt": prompt,
    }


def _normalise_ai_sources(sources: list[SourceItem], structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source in sources:
        items.append(
            {
                "title": source.title,
                "url": source.url,
                "published_date": source.published_date,
                "provider": source.provider,
                "provider_detail": source.provider_detail,
                "fetch_provider": source.fetch_provider,
                "fetch_status": source.fetch_status,
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
                "provider_detail": source.get("provider_detail"),
                "fetch_provider": source.get("fetch_provider"),
                "fetch_status": source.get("fetch_status"),
                "source_level": source.get("source_level"),
                "snippet": source.get("snippet") or source.get("content", "")[:300],
                "content": source.get("content"),
            }
        )
    return items


def _attach_base_evidence_packs(candidates: list[RadarCandidate], analysis_date: date) -> None:
    for item in candidates:
        current_pack = item.evidence_pack if isinstance(item.evidence_pack, dict) else {}
        research_pack = current_pack.get("research_structured_data")
        research_sources = current_pack.get("research_sources")
        research_error = current_pack.get("research_structured_error")
        item.data_coverage = _build_radar_data_coverage(item, research_pack, error=research_error)
        item.evidence_pack = _build_radar_evidence_pack(item, analysis_date, research_pack)
        if research_sources:
            item.evidence_pack["research_sources"] = research_sources
        if research_error:
            item.evidence_pack["research_structured_error"] = research_error
        _refresh_radar_three_layer_context(item, analysis_date)


def _attach_research_evidence_packs(
    candidates: list[RadarCandidate],
    ai_codes: list[str],
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> None:
    by_code = {item.code: item for item in candidates}
    selected = [by_code[code] for code in ai_codes if code in by_code]
    if not selected:
        return
    _emit(progress, f"Radar：建立 AI Evidence Pack {len(selected)} 檔")
    for index, item in enumerate(selected, 1):
        started_at = time.monotonic()
        _emit(progress, f"Radar Evidence Pack {index}/{len(selected)} 開始：{item.code} {item.name}".strip())
        request = CommandRequest(
            command="research",
            raw_text=f"/research {item.code} --date {analysis_date.isoformat()}",
            target=item.code,
            report_date=analysis_date,
            mode="deep",
        )
        try:
            structured_data, sources = _collect_structured_data_with_timeout(
                request,
                progress=lambda message, code=item.code: _emit(progress, f"Radar Evidence Pack {code}：{message}"),
                timeout_seconds=RADAR_EVIDENCE_PACK_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            elapsed = time.monotonic() - started_at
            _emit(
                progress,
                f"Radar Evidence Pack {index}/{len(selected)} 逾時跳過：{item.code}，耗時 {elapsed:.1f}s，保留本地資料：{exc}",
            )
            item.data_coverage = _build_radar_data_coverage(item, None, error=str(exc))
            item.evidence_pack = _build_radar_evidence_pack(item, analysis_date, None)
            item.evidence_pack["research_structured_error"] = str(exc)
            item.evidence_pack["research_structured_timeout"] = True
            continue
        except Exception as exc:
            _emit(progress, f"Radar Evidence Pack {item.code} 失敗，保留本地資料：{exc}")
            item.data_coverage = _build_radar_data_coverage(item, None, error=str(exc))
            item.evidence_pack = _build_radar_evidence_pack(item, analysis_date, None)
            item.evidence_pack["research_structured_error"] = str(exc)
            continue
        item.data_coverage = _build_radar_data_coverage(item, structured_data)
        item.evidence_pack = _build_radar_evidence_pack(item, analysis_date, structured_data)
        item.evidence_pack["research_sources"] = [_source_to_dict(source) for source in sources]
        _refresh_radar_three_layer_context(item, analysis_date)
        elapsed = time.monotonic() - started_at
        _emit(progress, f"Radar Evidence Pack {index}/{len(selected)} 完成：{item.code}，耗時 {elapsed:.1f}s")


def _attach_research_evidence_packs(
    candidates: list[RadarCandidate],
    ai_codes: list[str],
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> None:
    by_code = {item.code: item for item in candidates}
    selected = [by_code[code] for code in ai_codes if code in by_code]
    if not selected:
        return
    _emit(progress, f"Radar：準備 AI 輕量 Evidence Pack {len(selected)} 檔")
    stats = {"same_day_cache": 0, "recent_cache": 0, "light_cache": 0, "light_generated": 0}
    for index, item in enumerate(selected, 1):
        started_at = time.monotonic()
        _emit(progress, f"Radar Evidence Pack {index}/{len(selected)} 輕量整理：{item.code} {item.name}".strip())
        structured_data, sources, mode = _load_or_build_radar_light_research(item, analysis_date)
        stats[mode] = stats.get(mode, 0) + 1
        item.data_coverage = _build_radar_data_coverage(item, structured_data)
        item.evidence_pack = _build_radar_evidence_pack(item, analysis_date, structured_data)
        item.evidence_pack["research_pack_mode"] = mode
        item.evidence_pack["research_sources"] = sources
        _refresh_radar_three_layer_context(item, analysis_date)
        elapsed = time.monotonic() - started_at
        _emit(progress, f"Radar Evidence Pack {index}/{len(selected)} 完成：{item.code}｜{mode}｜{elapsed:.1f}s")
    _emit(
        progress,
        "Radar Evidence Pack 來源："
        f"同日快取 {stats.get('same_day_cache', 0)}、"
        f"最近快取 {stats.get('recent_cache', 0)}、"
        f"輕量快取 {stats.get('light_cache', 0)}、"
        f"輕量新建 {stats.get('light_generated', 0)}",
    )


def _radar_light_cache_path(code: str, analysis_date: date) -> Path:
    return RADAR_LIGHT_RESEARCH_CACHE_DIR / analysis_date.strftime("%Y%m%d") / f"{code}.json"


def _load_radar_light_cache(code: str, analysis_date: date) -> dict[str, Any] | None:
    path = _radar_light_cache_path(code, analysis_date)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _save_radar_light_cache(code: str, analysis_date: date, data: dict[str, Any]) -> None:
    path = _radar_light_cache_path(code, analysis_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stock_code": code,
        "report_date": analysis_date.isoformat(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
    }
    path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_or_build_radar_light_research(
    item: RadarCandidate,
    analysis_date: date,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    same_day_cache = load_research_structured_cache(item.code, analysis_date)
    if isinstance(same_day_cache, dict):
        return _with_radar_cache_meta(same_day_cache, "same_day_cache", analysis_date), _research_sources_from_item(item, analysis_date), "same_day_cache"

    latest_cache = load_latest_research_structured_cache(
        item.code,
        before_or_on=analysis_date,
        max_age_days=RADAR_FULL_RESEARCH_CACHE_MAX_AGE_DAYS,
    )
    if latest_cache is not None:
        cached_data, cache_date = latest_cache
        return _with_radar_cache_meta(cached_data, "recent_cache", cache_date), _research_sources_from_item(item, cache_date), "recent_cache"

    light_cache = _load_radar_light_cache(item.code, analysis_date)
    if isinstance(light_cache, dict):
        return light_cache, _research_sources_from_item(item, analysis_date), "light_cache"

    light_data = _build_radar_light_research_data(item, analysis_date)
    _save_radar_light_cache(item.code, analysis_date, light_data)
    return light_data, _research_sources_from_item(item, analysis_date), "light_generated"


def _with_radar_cache_meta(data: dict[str, Any], mode: str, data_date: date) -> dict[str, Any]:
    result = dict(data)
    result["radar_research_mode"] = mode
    result["radar_research_data_date"] = data_date.isoformat()
    notes = list(result.get("notes") or [])
    if mode == "recent_cache":
        notes.append(f"Radar 使用最近完整 research 快取，資料日期 {data_date.isoformat()}。")
    else:
        notes.append(f"Radar 使用同日完整 research 快取，資料日期 {data_date.isoformat()}。")
    result["notes"] = notes
    return result


def _research_sources_from_item(item: RadarCandidate, analysis_date: date | None = None) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    _merge_source_dicts(sources, item.ai_sources[:12])
    _merge_source_dicts(sources, item.web_sources[:12])
    evidence_pack = item.evidence_pack if isinstance(item.evidence_pack, dict) else {}
    _merge_source_dicts(sources, list(evidence_pack.get("raw_sources") or [])[:12])
    if analysis_date is not None and not any(str(source.get("source_level") or "").startswith("L1") or str(source.get("source_level") or "").startswith("Level 1") for source in sources):
        _merge_source_dicts(sources, _radar_official_basis_sources(analysis_date))
    return sources[:16]


def _build_radar_light_research_data(item: RadarCandidate, analysis_date: date) -> dict[str, Any]:
    source_count = _candidate_external_source_count(item)
    latest_chip_date = _latest_chip_cache_date(analysis_date)
    notes = [
        "Radar 輕量 research：未找到完整 research 快取，改用本地 Radar 評分、技術訊號、營收、籌碼、新聞與外部來源摘要。",
        "本資料包只供 Radar AI 短評使用；完整深度分析請使用 /research。",
    ]
    data_limits = []
    if latest_chip_date and latest_chip_date < analysis_date:
        data_limits.append(f"法人籌碼資料使用最近可用交易日 {latest_chip_date.isoformat()}，非 {analysis_date.isoformat()} 當日完整公告。")
    if source_count < RADAR_MIN_EXTERNAL_SOURCES:
        data_limits.append(f"外部來源不足 {RADAR_MIN_EXTERNAL_SOURCES} 則，目前 {source_count} 則。")
    return {
        "stock": {"code": item.code, "name": item.name, "symbol": item.symbol, "industry": item.industry},
        "report_date": analysis_date.isoformat(),
        "radar_research_mode": "light_generated",
        "radar_research_data_date": analysis_date.isoformat(),
        "notes": notes,
        "technical_data": {
            "strategies": sorted(item.strategy_codes),
            "signals": item.technical_signals[:12],
            "summary": _technical_signal_line(item),
        },
        "revenue_data": item.revenue_history[:12],
        "institutional_data": [],
        "margin_data": [],
        "tdcc_data": [],
        "financial_data": [],
        "topic_context": {
            "theme_score": item.score_components.get("theme", 0),
            "market_score": _component_sector_score(item.score_components),
            "local_news_titles": [news.get("title") for news in item.news_items[:5] if isinstance(news, dict)],
        },
        "news_context": {
            "local_news": item.news_items[:5],
            "external_sources": _research_sources_from_item(item),
        },
        "feature_pack": {
            "scope": "radar_light",
            "total_score": item.total_score,
            "score_components": item.score_components,
            "score_details": item.score_details,
            "key_reasons": item.key_reasons,
            "risk_flags": item.risk_flags,
            "chip_grades": item.chip_grades,
            "chip_summary": _chip_grade_line(item),
            "data_limits": data_limits,
        },
        "data_gap_summary": {
            "mode": "radar_light",
            "limits": data_limits,
            "missing_fields": ["financial_data", "margin_data", "institutional_data", "tdcc_data"],
            "message": "Radar 輕量資料包未現場抓完整 research 資料。",
        },
    }


def _latest_chip_cache_date(analysis_date: date) -> date | None:
    cache_dir = ROOT_DIR / ".cache" / "chip_daily"
    if not cache_dir.exists():
        return None
    latest: date | None = None
    for path in cache_dir.glob("*.csv"):
        try:
            item_date = datetime.strptime(path.stem, "%Y%m%d").date()
        except ValueError:
            continue
        if item_date <= analysis_date and (latest is None or item_date > latest):
            latest = item_date
    return latest


def _collect_structured_data_with_timeout(
    request: CommandRequest,
    *,
    progress: Callable[[str], None] | None,
    timeout_seconds: float,
) -> tuple[dict[str, Any], list[SourceItem]]:
    timeout = max(1.0, float(timeout_seconds))
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="radar-evidence-pack")
    future = executor.submit(collect_structured_data, request, progress=progress)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"單檔 Evidence Pack 超過 {timeout:.0f} 秒") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _build_radar_evidence_pack(
    item: RadarCandidate,
    analysis_date: date,
    research_structured_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pack = {
        "schema_version": "radar_evidence_pack_v1",
        "analysis_date": analysis_date.isoformat(),
        "candidate": {
            "code": item.code,
            "name": item.name,
            "symbol": item.symbol,
            "industry": item.industry,
            "price": item.price,
            "source_labels": item.source_labels,
        },
        "radar_scores": {
            "total_score": item.total_score,
            "score_components": item.score_components,
            "score_details": item.score_details,
            "key_reasons": item.key_reasons,
            "risk_flags": item.risk_flags,
            "score_caps_applied": item.score_caps_applied,
            "policy": "本地 Radar 分數只供 AI 參考，不得由 AI 改寫。",
        },
        "technical": {
            "strategies": sorted(item.strategy_codes),
            "signals": item.technical_signals,
            "dual_ma_signals": item.dual_ma_signals,
            "kd_ma_signals": item.kd_ma_signals,
            "summary": _technical_signal_line(item),
        },
        "revenue": {
            "score": item.score_components.get("revenue", 0),
            "history": item.revenue_history,
        },
        "financial": {
            "score": item.score_components.get("financial", 0),
            "detail": item.score_details.get("financial", {}),
        },
        "chip": {
            "score": item.score_components.get("chip", 0),
            "grades": item.chip_grades,
            "summary": _chip_grade_line(item),
        },
        "theme_and_market": {
            "theme_score": item.score_components.get("theme", 0),
            "market_score": _component_sector_score(item.score_components),
            "local_news": item.news_items,
            "web_sources": item.web_sources,
            "ai_sources": item.ai_sources,
        },
        "data_coverage": item.data_coverage,
        "radar_feature_snapshot": item.radar_feature_snapshot,
    }
    if research_structured_data:
        pack["research_structured_data"] = research_structured_data
    raw_sources = _radar_raw_sources(item, pack, analysis_date)
    pack["raw_sources"] = raw_sources
    pack["final_context"] = _radar_final_context(item, analysis_date, raw_sources)
    pack["three_layer_context"] = build_three_layer_evidence_context(
        raw_sources=raw_sources,
        evidence_pack=pack,
        final_context=pack["final_context"],
        min_source_count=RADAR_MIN_EXTERNAL_SOURCES,
    )
    return pack


def _refresh_radar_three_layer_context(item: RadarCandidate, analysis_date: date) -> None:
    if not isinstance(item.evidence_pack, dict):
        return
    raw_sources = _radar_raw_sources(item, item.evidence_pack, analysis_date)
    item.evidence_pack["raw_sources"] = raw_sources
    item.evidence_pack["final_context"] = _radar_final_context(item, analysis_date, raw_sources)
    item.evidence_pack["three_layer_context"] = build_three_layer_evidence_context(
        raw_sources=raw_sources,
        evidence_pack={key: value for key, value in item.evidence_pack.items() if key != "three_layer_context"},
        final_context=item.evidence_pack["final_context"],
        min_source_count=RADAR_MIN_EXTERNAL_SOURCES,
    )
    item.evidence_pack["ai_compact_pack"] = _build_radar_ai_compact_pack(item, analysis_date)


def _radar_official_basis_sources(analysis_date: date) -> list[dict[str, Any]]:
    published_date = analysis_date.isoformat()
    return [
        {
            "source_id": "RADAR_OFFICIAL_PRICE_VOLUME",
            "title": "TWSE / TPEx 官方價量與交易資訊快取",
            "url": "https://www.twse.com.tw/",
            "source_level": "L1_official",
            "published_date": published_date,
            "provider": "local_official_cache",
            "provider_detail": "radar_price_volume_basis",
            "source_type": "official_basis",
            "snippet": "Radar 技術面與價量條件使用本地快取的 TWSE / TPEx 官方交易資料作為基礎；完整逐檔資料保存在本地快取與 Radar evidence pack。",
            "found_by": ["radar_official_basis"],
        },
        {
            "source_id": "RADAR_OFFICIAL_REVENUE_FINANCIAL",
            "title": "MOPS 公開資訊觀測站營收與財報快取",
            "url": "https://mops.twse.com.tw/",
            "source_level": "L1_official",
            "published_date": published_date,
            "provider": "local_official_cache",
            "provider_detail": "radar_revenue_financial_basis",
            "source_type": "official_basis",
            "snippet": "Radar 營收、財報與公司公告相關底稿使用本地快取的 MOPS 公開資訊作為基礎；若個股資料不足，報告會在資料缺口中標示。",
            "found_by": ["radar_official_basis"],
        },
        {
            "source_id": "RADAR_OFFICIAL_CHIP",
            "title": "TWSE / TPEx / TDCC 法人籌碼與集保資料快取",
            "url": "https://www.tpex.org.tw/",
            "source_level": "L1_official",
            "published_date": published_date,
            "provider": "local_official_cache",
            "provider_detail": "radar_chip_basis",
            "source_type": "official_basis",
            "snippet": "Radar 籌碼條件使用 TWSE、TPEx、TDCC 或其本地快取資料作為基礎；FinMind / Fugle 僅作缺口備援時會另行記錄。",
            "found_by": ["radar_official_basis"],
        },
    ]


def _radar_raw_sources(item: RadarCandidate, pack: dict[str, Any], analysis_date: date | None = None) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    if analysis_date is not None:
        sources.extend(_radar_official_basis_sources(analysis_date))
    for source_type, items in (
        ("web_sources", item.web_sources),
        ("ai_sources", item.ai_sources),
        ("research_sources", pack.get("research_sources") if isinstance(pack, dict) else []),
    ):
        if not isinstance(items, list):
            continue
        for source in items:
            if isinstance(source, dict):
                sources.append({"source_type": source_type, **source})
    return sources


def _radar_final_context(item: RadarCandidate, analysis_date: date, raw_sources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "radar_final_context_v1",
        "analysis_date": analysis_date.isoformat(),
        "candidate": {
            "code": item.code,
            "name": item.name,
            "industry": item.industry,
            "price": item.price,
        },
        "radar_scores": {
            "total_score": item.total_score,
            "score_components": item.score_components,
            "score_details": item.score_details,
            "key_reasons": item.key_reasons,
            "risk_flags": item.risk_flags,
            "strategies": sorted(item.strategy_codes),
        },
        "coverage": item.data_coverage,
        "source_count": len(raw_sources),
        "source_preview": raw_sources[: min(12, len(raw_sources))],
        "local_news_count": len(item.news_items),
        "technical_signal_count": len(item.technical_signals) + len(item.dual_ma_signals) + len(item.kd_ma_signals),
        "revenue_points": len(item.revenue_history),
    }


def _build_radar_data_coverage(
    item: RadarCandidate,
    research_structured_data: dict[str, Any] | None = None,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    structured = research_structured_data or {}
    external_source_count = _candidate_external_source_count(item)
    checks = {
        "technical": "ok" if item.technical_signals or item.dual_ma_signals or item.kd_ma_signals else "missing",
        "revenue": "ok" if item.revenue_history else "missing",
        "chip": "ok" if item.chip_grades else "missing",
        "local_news": "ok" if item.news_items else "missing",
        "external_sources": "ok" if (item.ai_sources or item.web_sources) else "missing",
        "source_sufficiency": "ok" if external_source_count >= RADAR_MIN_EXTERNAL_SOURCES else "insufficient",
        "research_structured_data": "ok" if research_structured_data else ("error" if error else "not_requested"),
        "financial": _coverage_status(structured.get("financial_data")),
        "revenue_peer_context": _coverage_status(structured.get("peer_revenue_context")),
        "valuation": _coverage_status(structured.get("valuation_data")),
        "margin": _coverage_status(structured.get("margin_data")),
        "institutional": _coverage_status(structured.get("institutional_data")),
        "tdcc": _coverage_status(structured.get("tdcc_data")),
        "topic_context": _coverage_status(structured.get("topic_context")),
        "feature_pack": _coverage_status(structured.get("feature_pack")),
        "unified_evidence_pack": _coverage_status(structured.get("unified_evidence_pack")),
    }
    financial_field_coverage = structured.get("unified_financial_field_coverage") or {}
    checks["financial_required_fields"] = (
        "partial"
        if any(status == "unknown" for status in financial_field_coverage.values())
        else "ok"
        if financial_field_coverage
        else "missing"
    )
    if structured.get("radar_research_mode") == "light_generated":
        for key in ("financial", "margin", "institutional", "tdcc", "unified_evidence_pack"):
            if checks.get(key) in {"missing", "empty"}:
                checks[key] = "limited_by_light_research"
    missing = [key for key, value in checks.items() if value in {"missing", "empty", "error", "insufficient", "partial"}]
    return {
        "schema_version": "radar_data_coverage_v1",
        "checks": checks,
        "external_source_count": external_source_count,
        "min_external_sources": RADAR_MIN_EXTERNAL_SOURCES,
        "missing_or_weak_fields": missing,
        "unified_financial_field_coverage": financial_field_coverage,
        "error": error,
    }


def _coverage_status(value: Any) -> str:
    if value is None:
        return "missing"
    if isinstance(value, (list, tuple, set, dict)) and not value:
        return "empty"
    return "ok"


def _attach_ai_comments(
    candidates: list[RadarCandidate],
    ai_codes: list[str],
    model: str,
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> dict[str, Any]:
    by_code = {item.code: item for item in candidates}
    selected = [by_code[code] for code in ai_codes if code in by_code]
    if not selected:
        return {"mode": "radar_compact_ai", "chunks": [], "comment_count": 0}

    chunk_records: list[dict[str, Any]] = []
    comments: dict[str, dict[str, Any]] = {}
    low_model_digest = _attach_radar_low_model_digest(selected, analysis_date, progress)
    if not low_model_digest:
        low_model_digest = {
            "schema_version": "low_model_digest_v1",
            "status": "skipped",
            "model": "MiniMax-M3",
            "reason": "radar_low_model_digest_not_available",
        }
    for chunk_index, chunk in enumerate(_chunks(selected, RADAR_AI_CHUNK_SIZE), 1):
        prompt_jobs = _build_ai_comment_prompt_jobs(chunk, analysis_date, low_model_digest=low_model_digest)
        record: dict[str, Any] = {
            "chunk_index": chunk_index,
            "codes": [item.code for item in chunk],
            "status": "pending",
            "jobs": [],
        }
        for job_index, job in enumerate(prompt_jobs, 1):
            prompt = str(job["prompt"])
            job_record = {
                "job_index": job_index,
                "codes": job["codes"],
                "profile": job["profile"],
                "prompt_chars": len(prompt),
                "status": "pending",
            }
            try:
                if len(prompt) > RADAR_AI_PROMPT_MAX_CHARS:
                    raise ValueError(f"radar compact prompt too large: {len(prompt)} chars")
                _emit(
                    progress,
                    f"Radar AI 短評 chunk {chunk_index}.{job_index} 開始，{len(job['codes'])} 檔，profile={job['profile']}，prompt={len(prompt)} chars",
                )
                raw_text = _call_ai_comment_model(model, prompt)
                parsed = _parse_ai_comment_response(raw_text)
                job_comments = _normalise_ai_comment_items(parsed)
                comments.update(job_comments)
                job_record.update({"status": "ok", "output_chars": len(str(raw_text or "")), "comment_count": len(job_comments)})
                _emit(progress, f"Radar AI 短評 chunk {chunk_index}.{job_index} 完成，comments={len(job_comments)}")
            except Exception as exc:
                job_record.update({"status": "failed", "error": str(exc)})
                _emit(progress, f"Radar AI 短評 chunk {chunk_index}.{job_index} 失敗：{exc}")
                for code in job["codes"]:
                    item = by_code.get(code)
                    if item is not None:
                        item.ai_comment = {"status": "failed", "model": model, "error": str(exc), "chunk_index": chunk_index}
            record["jobs"].append(job_record)
        ok_jobs = [job for job in record["jobs"] if job.get("status") == "ok"]
        record.update(
            {
                "status": "ok" if len(ok_jobs) == len(record["jobs"]) else ("partial" if ok_jobs else "failed"),
                "prompt_chars": sum(int(job.get("prompt_chars") or 0) for job in record["jobs"]),
                "comment_count": sum(int(job.get("comment_count") or 0) for job in record["jobs"]),
            }
        )
        chunk_records.append(record)

    for item in selected:
        if item.ai_comment.get("status") == "failed":
            continue
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
    diagnostics = {
        "chunk_count": len(chunk_records),
        "prompt_chars": sum(int(record.get("prompt_chars") or 0) for record in chunk_records),
        "comment_count": sum(1 for item in selected if item.ai_comment.get("status") == "ok"),
        "candidate_count": len(selected),
        "prompt_max_chars": RADAR_AI_PROMPT_MAX_CHARS,
    }
    coverage = build_ai_workflow_coverage(
        "radar",
        local_data_package=True,
        low_model_digest=low_model_digest,
        high_model_input_package=True,
        dedupe_strategy="radar_candidate_compact_pack",
        source_index=True,
        input_audit=True,
        html_sections=True,
        diagnostics=diagnostics,
        notes=["Radar 是短評型 AI 流程，使用候選股 compact pack 與分批 prompt。"],
    )
    return {
        "mode": "radar_compact_ai",
        "model": model,
        "chunk_size": RADAR_AI_CHUNK_SIZE,
        "prompt_max_chars": RADAR_AI_PROMPT_MAX_CHARS,
        "chunk_count": len(chunk_records),
        "chunks": chunk_records,
        "comment_count": sum(1 for item in selected if item.ai_comment.get("status") == "ok"),
        "ai_workflow_coverage": coverage,
        "low_model_digest": {
            "status": low_model_digest.get("status"),
            "model": low_model_digest.get("model"),
            "prompt_path": low_model_digest.get("prompt_path"),
            "facts_count": len(low_model_digest.get("facts") or []),
            "warnings_count": len(low_model_digest.get("warnings") or []),
        } if low_model_digest else {},
    }


def _attach_radar_low_model_digest(
    selected: list[RadarCandidate],
    analysis_date: date,
    progress: Callable[[str], None] | None,
) -> dict[str, Any]:
    try:
        center = ResearchCenter()
        low_model = getattr(center, "low_model_minimax", None)
        enabled = bool(getattr(center.config, "enable_low_model_digest", True))
        if low_model is None:
            return {}
        request = CommandRequest(
            command="radar",
            raw_text="/radar low-model digest",
            target="選股雷達候選股",
            report_date=analysis_date,
        )
        payload = {
            "command": "radar",
            "analysis_date": analysis_date.isoformat(),
            "candidate_count": len(selected),
            "candidates": [
                _build_ai_comment_payload(item, analysis_date, compact_profile="tight")
                for item in selected
            ],
            "rule": "只整理候選股證據、風險、缺口與來源對照，不輸出買賣建議或最終短評。",
        }
        return run_low_model_digest_for_payload(
            request,
            payload,
            sources=[],
            minimax=low_model,
            enabled=enabled,
            progress=progress,
            purpose="radar_low_model_batch_digest",
            max_sources=60,
            max_list=60,
            max_keys=120,
            max_string=700,
            depth=6,
        )
    except Exception as exc:
        _emit(progress, f"Radar：MiniMax M3 批次資料整理略過：{exc}")
        return {
            "schema_version": "low_model_digest_v1",
            "status": "failed",
            "model": "MiniMax-M3",
            "error": str(exc),
        }


def _score_candidates(
    candidates: list[RadarCandidate],
    analysis_date: date | None = None,
    *,
    scoring_version: str | None = None,
) -> None:
    score_radar_candidates(candidates, analysis_date, scoring_version=scoring_version)


def _apply_radar_v2_overlays(item: RadarCandidate, snapshot: dict[str, Any], details: dict[str, dict[str, Any]]) -> None:
    tech = snapshot.get("technical") or {}
    revenue_rows = _normalise_revenue_rows((snapshot.get("revenue") or {}).get("history") or item.revenue_history)
    chip = snapshot.get("chip") or {}
    theme = snapshot.get("theme_news") or {}
    financial_rows = _normalise_financial_rows((snapshot.get("financial") or {}).get("financial_data"))

    technical_bonus, technical_reasons, technical_risks, technical_meta = _radar_v2_technical_overlay(item, tech)
    _add_score_overlay(details, "technical", technical_bonus, technical_reasons, technical_risks, technical_meta)

    revenue_bonus, revenue_reasons, revenue_risks, revenue_meta = _radar_v2_revenue_overlay(revenue_rows, tech)
    _add_score_overlay(details, "revenue", revenue_bonus, revenue_reasons, revenue_risks, revenue_meta)

    financial_bonus, financial_reasons, financial_risks, financial_meta = _radar_v2_financial_overlay(financial_rows, revenue_rows, tech, chip)
    _add_score_overlay(details, "financial", financial_bonus, financial_reasons, financial_risks, financial_meta)

    chip_bonus, chip_reasons, chip_risks, chip_meta = _radar_v2_chip_overlay(chip)
    _add_score_overlay(details, "chip", chip_bonus, chip_reasons, chip_risks, chip_meta)

    theme_bonus, theme_reasons, theme_risks, theme_meta = _radar_v2_theme_overlay(theme, revenue_rows, chip_meta)
    _add_score_overlay(details, "theme", theme_bonus, theme_reasons, theme_risks, theme_meta)

    sector_bonus, sector_reasons, sector_risks, sector_meta = _radar_v2_sector_overlay(snapshot.get("sector") or {}, revenue_rows, chip_meta, theme_meta)
    _add_score_overlay(details, "sector", sector_bonus, sector_reasons, sector_risks, sector_meta)

    _apply_radar_v2_cross_confirmations(details, tech, revenue_rows, chip_meta, theme_meta)


def _add_score_overlay(
    details: dict[str, dict[str, Any]],
    key: str,
    delta: float,
    reasons: list[str],
    risks: list[str] | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    if key not in details:
        return
    detail = details[key]
    max_score = {"technical": 30, "revenue": 20, "financial": 15, "chip": 15, "theme": 15, "sector": 5}.get(key, 100)
    original = _to_float(detail.get("score")) or 0.0
    detail["score"] = int(max(0, min(max_score, round(original + delta))))
    detail["reasons"] = _unique_texts([*(detail.get("reasons") or []), *reasons])
    detail["risks"] = _unique_texts([*(detail.get("risks") or []), *(risks or [])])
    merged = dict(detail.get("details") or {})
    overlays = list(merged.get("v2_overlays") or [])
    if delta or reasons or risks:
        overlays.append({"delta": delta, "reasons": reasons, "risks": risks or [], "meta": meta or {}})
    merged["v2_overlays"] = overlays
    detail["details"] = merged


def _radar_v2_technical_overlay(item: RadarCandidate, tech: dict[str, Any]) -> tuple[float, list[str], list[str], dict[str, Any]]:
    reasons: list[str] = []
    risks: list[str] = []
    bonus = 0.0
    above = tech.get("above_ma") or {}
    reclaim = tech.get("reclaim_ma") or {}
    d60 = _to_float(tech.get("distance_from_60d_low_pct"))
    d120 = _to_float(tech.get("distance_from_120d_low_pct"))
    volume_ratio = _to_float(tech.get("volume_ratio")) or _to_float((tech.get("price_metrics") or {}).get("volume_ratio"))
    change20 = _to_float(tech.get("change_pct_20d")) or _to_float((tech.get("price_metrics") or {}).get("change_pct_20d"))
    ma20_deviation = _to_float(tech.get("ma20_deviation_pct"))
    below_ma21_streak = int(_to_float(tech.get("below_ma21_streak")) or 0)
    if (reclaim.get("ma21") or reclaim.get("ma20")) and (d60 is None or d60 <= 35):
        bonus += 2; reasons.append("v2 低位階站回/突破 21MA")
    if below_ma21_streak >= 20:
        bonus += 3; reasons.append("v2 MA21 下方整理 20 日以上後收復")
    elif below_ma21_streak >= 8:
        bonus += 2; reasons.append("v2 MA21 下方整理 8 日以上後收復")
    if d60 is not None and d60 < 20 and (above.get("ma21") or reclaim.get("ma21")):
        bonus += 1; reasons.append("v2 距 60 日低點 20% 內且站回 MA21")
    if d60 is not None and d60 < 20 and d120 is not None and d120 < 30:
        bonus += 1; reasons.append("v2 距 60/120 日低點仍近")
    if tech.get("recent_break_low_recover"):
        bonus += 2; reasons.append("v2 洗盤後收復轉折低點")
    if reclaim.get("ma20") or reclaim.get("ma21"):
        bonus += 1; reasons.append("v2 跌破 MA20/MA21 後快速站回")
    if tech.get("long_lower_shadow") and (above.get("ma20") or above.get("ma21")):
        bonus += 1; reasons.append("v2 長下影後收回 MA20/MA21")
    if volume_ratio is not None:
        if 1.2 <= volume_ratio <= 3:
            bonus += 1; reasons.append("v2 溫和放量")
        elif 1.0 <= volume_ratio < 1.2:
            bonus += 1; reasons.append("v2 量能初步回溫")
        elif volume_ratio > 5 and not tech.get("price_up_volume_up"):
            bonus -= 2; risks.append("v2 爆量但價格未同步轉強")
    strategies = set(item.strategy_codes)
    if len(strategies) >= 2:
        bonus += 1; reasons.append("v2 A/B/C/D 多策略交叉命中")
    if strategies & {"A", "B"} and strategies & {"C", "D"}:
        bonus += 2; reasons.append("v2 趨勢策略與反轉/收復策略接力")
    recent_counts = _recent_strategy_signal_counts(item.technical_signals)
    repeated_bonus = 0.0
    if recent_counts.get("20d", 0) >= 2:
        repeated_bonus += 1; reasons.append("v2 近 20 日重複觸發技術策略")
    if recent_counts.get("60d", 0) >= 2:
        repeated_bonus += 2; reasons.append("v2 近 60 日重複觸發技術策略")
    if recent_counts.get("relay_60d"):
        repeated_bonus += 2; reasons.append("v2 近 60 日不同策略接力")
    if repeated_bonus and change20 is not None and change20 > 30:
        repeated_bonus *= 0.5
        risks.append("v2 多次觸發但 20 日漲幅已過熱，加分減半")
    bonus += repeated_bonus
    if ma20_deviation is not None and ma20_deviation > 20:
        bonus -= 2; risks.append("v2 MA20 乖離偏高")
    return bonus, reasons, risks, {
        "below_ma21_streak": below_ma21_streak,
        "recent_strategy_counts": recent_counts,
        "change_pct_20d": change20,
        "ma20_deviation_pct": ma20_deviation,
    }


def _radar_v2_revenue_overlay(rows: list[dict[str, Any]], tech: dict[str, Any]) -> tuple[float, list[str], list[str], dict[str, Any]]:
    if not rows:
        return 0.0, [], [], {"row_count": 0}
    reasons: list[str] = []
    risks: list[str] = []
    bonus = 0.0
    yoy_values = [_to_float(row.get("yoy") or row.get("YoY") or row.get("YoY%") or row.get("revenue_yoy")) for row in rows]
    yoy_present = [value for value in yoy_values if value is not None]
    latest_yoy = yoy_values[-1] if yoy_values else None
    previous_yoy = yoy_values[-2] if len(yoy_values) >= 2 else None
    change20 = _to_float(tech.get("change_pct_20d")) or _to_float((tech.get("price_metrics") or {}).get("change_pct_20d"))
    d60 = _to_float(tech.get("distance_from_60d_low_pct"))
    if previous_yoy is not None and latest_yoy is not None and previous_yoy < 0 <= latest_yoy:
        bonus += 1; reasons.append("v2 營收 YoY 由負轉正加強")
    if len(yoy_present) >= 3 and latest_yoy is not None and latest_yoy < 0 and yoy_present[-3] < yoy_present[-2] < yoy_present[-1]:
        bonus += 2; reasons.append("v2 YoY 仍負但連續收斂")
    if latest_yoy is not None and latest_yoy > 10 and (change20 is None or change20 < 20):
        bonus += 2; reasons.append("v2 營收轉強但 20 日漲幅仍低")
    if latest_yoy is not None and latest_yoy > 15 and (d60 is None or d60 < 35):
        bonus += 2; reasons.append("v2 營收轉強且股價仍在低位階")
    if len(yoy_present) >= 3 and yoy_present[-3] > yoy_present[-2] > yoy_present[-1]:
        risks.append("v2 YoY 連續惡化，不給營收確認分")
    return bonus, reasons, risks, {"latest_yoy": latest_yoy, "change_pct_20d": change20, "distance_from_60d_low_pct": d60}


def _radar_v2_financial_overlay(
    rows: list[dict[str, Any]],
    revenue_rows: list[dict[str, Any]],
    tech: dict[str, Any],
    chip: dict[str, Any],
) -> tuple[float, list[str], list[str], dict[str, Any]]:
    if rows:
        return 0.0, [], [], {"row_count": len(rows)}
    latest_yoy = _latest_revenue_yoy(revenue_rows)
    institutional_confirmed = _institutional_confirmation(_chip_flow_meta(chip)).get("confirmed")
    technical_confirmed = tech.get("status") == "ok" and ((tech.get("volume_ratio") or 0) >= 1.2 or tech.get("price_up_volume_up"))
    bonus = 0.0
    reasons: list[str] = []
    if latest_yoy is not None and latest_yoy >= 10:
        bonus += 1; reasons.append("v2 財報空窗但月營收已轉強")
    if latest_yoy is not None and latest_yoy >= 10 and technical_confirmed and institutional_confirmed:
        bonus += 2; reasons.append("v2 財報空窗但技術、營收與籌碼同步轉強")
    return bonus, reasons, [], {"row_count": 0, "latest_yoy": latest_yoy, "technical_confirmed": technical_confirmed, "institutional_confirmed": institutional_confirmed}


def _radar_v2_chip_overlay(chip: dict[str, Any]) -> tuple[float, list[str], list[str], dict[str, Any]]:
    meta = _chip_flow_meta(chip)
    reasons: list[str] = []
    risks: list[str] = []
    bonus = 0.0
    if meta["institutional_rows"] <= 0:
        risks.append("v2 法人資料缺口，交叉確認不加分")
    if meta["foreign5"] > 0:
        bonus += 1; reasons.append("v2 外資近 5 日買超")
    if meta["trust5"] > 0 and meta["foreign5"] >= -300:
        bonus += 2; reasons.append("v2 投信買超且外資未明顯倒貨")
    elif meta["trust5"] > 0 and meta["foreign5"] < -300:
        bonus += 1; reasons.append("v2 投信初買但外資仍賣，僅列低度確認")
        risks.append("v2 外資賣壓抵銷投信買盤")
    if meta["total5"] > 0 and meta["total10"] > 0:
        bonus += 2; reasons.append("v2 三大法人 5/10 日同步偏買")
    if meta["foreign5"] < -1000 and meta["trust5"] <= 0:
        risks.append("v2 外資 5 日大賣且投信未連續買，不給法人確認分")
    if meta["financing5"] is not None:
        if meta["financing5"] <= 0:
            bonus += 1; reasons.append("v2 融資近 5 日未增加")
        if meta["financing10"] is not None and meta["financing10"] <= 0:
            bonus += 1; reasons.append("v2 融資近 10 日未明顯增加")
        if meta["total5"] > 0 and meta["financing5"] <= 0:
            bonus += 2; reasons.append("v2 法人買且融資未增")
        if meta["financing5"] > 500 and meta["total5"] <= 0:
            bonus -= 3; risks.append("v2 融資 5 日大增但法人未買")
        if meta["financing10"] is not None and meta["financing10"] > 1200 and meta["total10"] <= 0:
            bonus -= 2; risks.append("v2 融資 10 日增加過快但法人未買")
    else:
        risks.append("v2 融資券資料缺口，融資健康不加分")
    if meta["short_margin_ratio"] is not None and meta["short_margin_ratio"] >= 30:
        bonus += 1; reasons.append("v2 券資比偏高具軋空彈性")
    tdcc = chip.get("tdcc_data") or {}
    if isinstance(tdcc, dict) and tdcc:
        large = _to_float(tdcc.get("large_holder_pct") or tdcc.get("big_holder_pct"))
        retail = _to_float(tdcc.get("retail_holder_pct"))
        if large is not None and large >= 60:
            bonus += 1; reasons.append("v2 TDCC 大戶集中")
        if retail is not None and retail <= 35:
            bonus += 1; reasons.append("v2 TDCC 散戶占比偏低")
    else:
        risks.append("v2 TDCC 資料缺口，不倒灌今日資料")
    meta["institutional_confirmed"] = bool(meta["total5"] > 0 and meta["total10"] > 0)
    meta["margin_healthy"] = bool(meta["financing5"] is not None and meta["financing5"] <= 0)
    return bonus, reasons, risks, meta


def _radar_v2_theme_overlay(theme: dict[str, Any], revenue_rows: list[dict[str, Any]], chip_meta: dict[str, Any]) -> tuple[float, list[str], list[str], dict[str, Any]]:
    local_news = [row for row in theme.get("local_news") or [] if isinstance(row, dict)]
    web_sources = [row for row in theme.get("web_sources") or [] if isinstance(row, dict)]
    ai_sources = [row for row in theme.get("ai_sources") or [] if isinstance(row, dict)]
    topic_context = theme.get("topic_context") if isinstance(theme.get("topic_context"), dict) else {}
    matched_topics = topic_context.get("matched_topics") if isinstance(topic_context, dict) else []
    company_rel = topic_context.get("company_topic_relations") if isinstance(topic_context, dict) else {}
    all_titles = " ".join(str(row.get("title") or row.get("snippet") or "") for row in [*local_news, *web_sources, *ai_sources])
    reasons: list[str] = []
    risks: list[str] = []
    bonus = 0.0
    direct_match = isinstance(company_rel, dict) and (_to_float(company_rel.get("direct_matches")) or 0) > 0
    has_sources = bool(local_news or web_sources or ai_sources)
    if has_sources and (matched_topics or direct_match):
        bonus += 2; reasons.append("v2 題材有來源且能對應公司")
    if direct_match:
        bonus += 1; reasons.append("v2 題材與產品/客戶/產業鏈直接匹配")
    hot_terms = ("AI", "BBU", "CPO", "CoWoS", "高速傳輸", "散熱", "電動車", "機器人", "伺服器", "重電", "儲能", "半導體")
    if any(term in all_titles for term in hot_terms):
        bonus += 1; reasons.append("v2 熱門題材關鍵字出現")
    latest_yoy = _latest_revenue_yoy(revenue_rows)
    if latest_yoy is not None and latest_yoy > 0 and (matched_topics or direct_match):
        bonus += 2; reasons.append("v2 題材可與營收改善交叉驗證")
    if chip_meta.get("institutional_confirmed") and (matched_topics or direct_match):
        bonus += 2; reasons.append("v2 題材可與法人買盤交叉驗證")
    if has_sources and latest_yoy is not None and latest_yoy <= 0:
        risks.append("v2 有新聞但營收尚未跟上，題材分保守")
    if bonus > 0 and not (matched_topics or direct_match):
        risks.append("v2 題材缺少可驗證公司關聯")
    if any("注意" in str(row.get("title") or "") or "處置" in str(row.get("title") or "") for row in local_news):
        risks.append("v2 注意股/處置新聞")
    return bonus, reasons, risks, {"has_verified_theme": bool(has_sources and (matched_topics or direct_match)), "matched_topics": matched_topics, "direct_match": direct_match}


def _radar_v2_sector_overlay(
    sector: dict[str, Any],
    revenue_rows: list[dict[str, Any]],
    chip_meta: dict[str, Any],
    theme_meta: dict[str, Any],
) -> tuple[float, list[str], list[str], dict[str, Any]]:
    count = int(sector.get("industry_candidate_count") or 0)
    bonus = 0.0
    reasons: list[str] = []
    risks: list[str] = []
    if count >= 4 and (_latest_revenue_yoy(revenue_rows) or 0) > 0:
        bonus += 1; reasons.append("v2 族群擴散且個股營收轉強")
    if count >= 4 and chip_meta.get("institutional_confirmed"):
        bonus += 1; reasons.append("v2 族群擴散且個股法人轉強")
    if theme_meta.get("has_verified_theme"):
        bonus += 1; reasons.append("v2 同題材個股具可驗證關聯")
    if count >= 4 and not (chip_meta.get("institutional_confirmed") or (_latest_revenue_yoy(revenue_rows) or 0) > 0 or theme_meta.get("has_verified_theme")):
        risks.append("v2 族群強但個股缺少營收/法人/題材確認")
    return bonus, reasons, risks, {"industry_candidate_count": count}


def _apply_radar_v2_cross_confirmations(
    details: dict[str, dict[str, Any]],
    tech: dict[str, Any],
    revenue_rows: list[dict[str, Any]],
    chip_meta: dict[str, Any],
    theme_meta: dict[str, Any],
) -> None:
    institutional_confirmed = bool(chip_meta.get("institutional_confirmed"))
    margin_healthy = bool(chip_meta.get("margin_healthy"))
    latest_yoy = _latest_revenue_yoy(revenue_rows)
    revenue_turnaround = latest_yoy is not None and latest_yoy > 0
    theme_verified = bool(theme_meta.get("has_verified_theme"))
    technical_triggered = tech.get("status") == "ok"
    change20 = _to_float(tech.get("change_pct_20d")) or _to_float((tech.get("price_metrics") or {}).get("change_pct_20d"))
    multiplier = 0.5 if change20 is not None and change20 > 30 else 1.0
    if institutional_confirmed and margin_healthy:
        _add_score_overlay(details, "chip", 2 * multiplier, ["v2 交叉確認：法人買 + 融資健康"], [], {"cross_confirmation": True})
    if institutional_confirmed and revenue_turnaround:
        _add_score_overlay(details, "revenue", 2 * multiplier, ["v2 交叉確認：法人買 + 營收轉折"], [], {"cross_confirmation": True})
    if institutional_confirmed and theme_verified:
        _add_score_overlay(details, "theme", 2 * multiplier, ["v2 交叉確認：法人買 + 題材可驗證"], [], {"cross_confirmation": True})
    if institutional_confirmed and margin_healthy and (revenue_turnaround or theme_verified):
        _add_score_overlay(details, "chip", 1 * multiplier, ["v2 交叉確認：法人、融資、營收/題材同步"], [], {"cross_confirmation": True})
    if technical_triggered and revenue_turnaround and institutional_confirmed:
        _add_score_overlay(details, "revenue", 1 * multiplier, ["v2 技術觸發 + 營收轉折 + 法人買"], [], {"cross_confirmation": True})
    if technical_triggered and theme_verified and institutional_confirmed:
        _add_score_overlay(details, "theme", 1 * multiplier, ["v2 技術觸發 + 題材可驗證 + 法人買"], [], {"cross_confirmation": True})
    if multiplier < 1:
        for key in ("technical", "chip", "revenue", "theme"):
            _add_score_overlay(details, key, 0, [], ["v2 20 日漲幅過熱，交叉確認加分減半"], {"change_pct_20d": change20})


def _build_radar_feature_snapshot(
    item: RadarCandidate,
    candidates: list[RadarCandidate],
    industry_counts: dict[str, int],
    analysis_date: date | None,
) -> dict[str, Any]:
    prepared_pack = item.evidence_pack.get("research_structured_data") if isinstance(item.evidence_pack, dict) else None
    if isinstance(prepared_pack, dict) and prepared_pack:
        structured = prepared_pack
        structured_date = parse_date_like(prepared_pack.get("structured_cache_date") or prepared_pack.get("report_date"))
    else:
        structured, structured_date = _load_radar_structured_snapshot(item.code, analysis_date)
    technical = _build_technical_snapshot(item, analysis_date)
    structured_revenue_rows = _structured_rows(structured, "revenue_data")
    revenue_rows = structured_revenue_rows or item.revenue_history
    financial_rows = _structured_rows(structured, "financial_data")
    chip = {
        "grades": dict(item.chip_grades),
        "institutional_data": _structured_rows(structured, "institutional_data"),
        "margin_data": _structured_rows(structured, "margin_data"),
        "tdcc_data": structured.get("tdcc_data") if isinstance(structured, dict) else {},
    }
    theme_context = structured.get("topic_context") if isinstance(structured, dict) else {}
    sector_peers = [candidate.code for candidate in candidates if candidate.industry and candidate.industry == item.industry]
    sector_snapshot = _build_sector_snapshot(item, analysis_date)
    sector_snapshot.update(
        {
            "industry_candidate_count": industry_counts.get(item.industry, 0),
            "same_industry_codes": sector_peers[:20],
        }
    )
    return {
        "analysis_date": analysis_date.isoformat() if analysis_date else None,
        "structured_cache_date": structured_date.isoformat() if structured_date else None,
        "technical": technical,
        "revenue": {
            "history": revenue_rows,
            "peer_context": structured.get("peer_revenue_context") if isinstance(structured, dict) else {},
            "preannouncement_price": structured.get("preannouncement_price") if isinstance(structured, dict) else {},
        },
        "financial": {
            "financial_data": financial_rows,
            "gross_margin_cache": structured.get("gross_margin_cache") if isinstance(structured, dict) else {},
            "valuation_data": structured.get("valuation_data") if isinstance(structured, dict) else {},
        },
        "chip": chip,
        "theme_news": {
            "local_news": item.news_items,
            "web_sources": item.web_sources,
            "ai_sources": item.ai_sources,
            "topic_context": theme_context,
        },
        "sector": sector_snapshot,
    }


def _load_radar_structured_snapshot(stock_code: str, analysis_date: date | None) -> tuple[dict[str, Any], date | None]:
    if not analysis_date:
        return {}, None
    exact = load_research_structured_cache(stock_code, analysis_date, max_age_hours=24 * 90)
    if isinstance(exact, dict):
        return exact, analysis_date
    latest = load_latest_research_structured_cache(
        stock_code,
        before_or_on=analysis_date,
        max_age_days=max(10, RADAR_FULL_RESEARCH_CACHE_MAX_AGE_DAYS),
    )
    if latest:
        return latest
    return {}, None


def _structured_rows(structured: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = structured.get(key) if isinstance(structured, dict) else None
    return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _unified_financial_field_coverage(rows: list[dict[str, Any]]) -> dict[str, str]:
    field_keys = {
        "revenue": ("Revenue", "revenue"),
        "gross_profit": ("Gross_Profit", "gross_profit"),
        "operating_expenses": ("Operating_Expenses", "operating_expenses"),
        "operating_income": ("Operating_Income", "operating_income"),
        "non_operating_income": ("Non_Operating_Income", "non_operating_income"),
        "pre_tax_income": ("Pre_Tax_Income", "pre_tax_income"),
        "net_income": ("Net_Income", "net_income"),
        "eps": ("EPS", "eps"),
        "operating_cash_flow": ("Operating_Cash_Flow", "operating_cash_flow"),
        "free_cash_flow": ("Free_Cash_Flow", "free_cash_flow"),
        "inventory": ("Inventory", "inventory"),
        "contract_liabilities": ("Contract_Liabilities", "contract_liabilities"),
        "paid_in_capital": ("Paid_In_Capital", "paid_in_capital"),
        "total_assets": ("Total_Assets", "total_assets"),
        "total_liabilities": ("Total_Liabilities", "total_liabilities"),
        "current_assets": ("Current_Assets", "current_assets"),
        "current_liabilities": ("Current_Liabilities", "current_liabilities"),
        "quick_assets": ("Quick_Assets", "quick_assets"),
        "accounts_payable": ("Accounts_Payable", "accounts_payable"),
        "interest_bearing_debt": ("Interest_Bearing_Debt", "interest_bearing_debt"),
    }
    if not rows:
        return {field: "unknown" for field in field_keys}
    coverage: dict[str, str] = {}
    for field, keys in field_keys.items():
        if field == "contract_liabilities" and any(
            str(row.get("Contract_Liabilities_Status") or "") == "not_reported" for row in rows
        ) and not any(str(row.get("Contract_Liabilities_Status") or "") == "reported" for row in rows):
            coverage[field] = "not_reported"
            continue
        coverage[field] = "reported" if any(
            any(row.get(key) is not None for key in keys) for row in rows
        ) else "unknown"
    return coverage


def _build_technical_snapshot(item: RadarCandidate, analysis_date: date | None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "strategies": sorted(item.strategy_codes),
        "signals": item.technical_signals[:12],
        "dual_ma_signals": item.dual_ma_signals,
        "kd_ma_signals": item.kd_ma_signals,
        "price_metrics": _load_price_metric_for_item(item),
    }
    frame = _load_technical_daily_frame(item, analysis_date)
    if frame is None or frame.empty:
        snapshot["status"] = "missing_daily"
        return snapshot
    current = frame.iloc[-1]
    previous = frame.iloc[-2] if len(frame) >= 2 else current
    close = _to_float(current.get("close"))
    prev_close = _to_float(previous.get("close"))
    open_price = _to_float(current.get("open"))
    high_price = _to_float(current.get("high"))
    low_price = _to_float(current.get("low"))
    volume = _to_float(current.get("volume"))
    ma: dict[str, float | None] = {}
    prev_ma: dict[str, float | None] = {}
    slopes: dict[str, str] = {}
    for window in (5, 10, 13, 20, 21, 60, 105):
        series = frame["close"].rolling(window).mean()
        ma_key = f"ma{window}"
        ma[ma_key] = _to_float(series.iloc[-1])
        prev_ma[ma_key] = _to_float(series.iloc[-2]) if len(series) >= 2 else None
        if len(series) > window + 3 and pd.notna(series.iloc[-1]) and pd.notna(series.iloc[-4]):
            slopes[ma_key] = "up" if float(series.iloc[-1]) > float(series.iloc[-4]) else "flat_or_down"
    prior_volume = frame["volume"].iloc[:-1] if "volume" in frame else pd.Series(dtype=float)
    vol20 = _to_float(prior_volume.tail(20).mean()) if len(prior_volume) >= 20 else None
    vol60 = _to_float(prior_volume.tail(60).mean()) if len(prior_volume) >= 60 else None
    high20 = _to_float(frame["high"].rolling(20).max().iloc[-2]) if len(frame) >= 2 and "high" in frame else None
    high60 = _to_float(frame["high"].rolling(60).max().iloc[-2]) if len(frame) >= 2 and "high" in frame else None
    low60 = _to_float(frame["low"].rolling(60).min().iloc[-1]) if "low" in frame else None
    low120 = _to_float(frame["low"].rolling(120).min().iloc[-1]) if "low" in frame else None
    close20 = _to_float(frame["close"].iloc[-21]) if len(frame) >= 21 else None
    change20 = ((close / close20 - 1) * 100) if close and close20 else None
    below_ma21_streak = _below_ma_streak(frame, 21)
    volume_ratio = volume / vol20 if volume is not None and vol20 and vol20 > 0 else None
    pretrigger_volume_ratio = None
    if len(prior_volume) >= 25:
        prior5_avg = _to_float(prior_volume.iloc[-5:].mean())
        preceding20_avg = _to_float(prior_volume.iloc[-25:-5].mean())
        if prior5_avg is not None and preceding20_avg and preceding20_avg > 0:
            pretrigger_volume_ratio = prior5_avg / preceding20_avg
    close_location_value = None
    if None not in (close, high_price, low_price) and high_price > low_price:
        close_location_value = (close - low_price) / (high_price - low_price)
    large_volume_reference = _recent_large_volume_reference(frame)
    closes_above_large_volume_high = bool(
        close is not None
        and large_volume_reference.get("high") is not None
        and close > large_volume_reference["high"]
    )
    closes_below_large_volume_low = bool(
        close is not None
        and large_volume_reference.get("low") is not None
        and close < large_volume_reference["low"]
    )
    recent_lows = frame["low"].iloc[-12:-1] if len(frame) > 12 and "low" in frame else pd.Series(dtype=float)
    recent_prior_low = _to_float(recent_lows.min()) if not recent_lows.empty else None
    recent_break_low = False
    if recent_prior_low is not None and len(frame) >= 4 and "low" in frame:
        recent_break_low = bool((frame["low"].iloc[-4:-1] < recent_prior_low).any() and close and close > recent_prior_low)
    candle_range = _to_float(current.get("high")) - _to_float(current.get("low")) if _to_float(current.get("high")) is not None and _to_float(current.get("low")) is not None else None
    lower_shadow = None
    if candle_range and candle_range > 0 and close is not None:
        lower_shadow = (min(close, _to_float(current.get("open")) or close) - (_to_float(current.get("low")) or close)) / candle_range
    snapshot.update(
        {
            "status": "ok",
            "last_date": str(current.get("date")),
            "row_count": len(frame),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close,
            "previous_close": prev_close,
            "volume": volume,
            "ma": ma,
            "previous_ma": prev_ma,
            "ma_slopes": slopes,
            "volume_avg20": vol20,
            "volume_avg60": vol60,
            "volume_ratio": volume_ratio,
            "volume_context_status": "ok" if len(frame) >= 30 and volume_ratio is not None else "insufficient",
            "pretrigger_volume_ratio": pretrigger_volume_ratio,
            "close_location_value": close_location_value,
            "large_volume_reference": large_volume_reference,
            "closes_above_large_volume_high": closes_above_large_volume_high,
            "closes_below_large_volume_low": closes_below_large_volume_low,
            "above_ma": {key: close is not None and value is not None and close >= value for key, value in ma.items()},
            "reclaim_ma": {
                key: close is not None and value is not None and prev_close is not None and prev_ma.get(key) is not None and close >= value and prev_close < prev_ma[key]
                for key, value in ma.items()
            },
            "distance_from_60d_low_pct": _pct_from_low(close, low60),
            "distance_from_120d_low_pct": _pct_from_low(close, low120),
            "breakout_20d": close is not None and high20 is not None and close > high20,
            "breakout_60d": close is not None and high60 is not None and close > high60,
            "intraday_breakout_failed": bool(
                high_price is not None
                and high20 is not None
                and high_price > high20
                and (close is None or close <= high20)
            ),
            "price_up_volume_up": close is not None and prev_close is not None and volume is not None and close > prev_close and (volume_ratio or 0) > 1,
            "recent_break_low_recover": recent_break_low,
            "long_lower_shadow": bool(lower_shadow is not None and lower_shadow >= 0.35),
            "ma20_deviation_pct": ((close / ma["ma20"] - 1) * 100) if close and ma.get("ma20") else None,
            "change_pct_20d": change20,
            "below_ma21_streak": below_ma21_streak,
        }
    )
    return snapshot


def _recent_large_volume_reference(frame: pd.DataFrame) -> dict[str, Any]:
    """Find the latest prior 10-day candle whose volume is >= 1.8x its prior-20 average."""

    if len(frame) < 30 or not {"date", "high", "low", "volume"}.issubset(frame.columns):
        return {}
    latest: dict[str, Any] = {}
    start = max(20, len(frame) - 11)
    for index in range(start, len(frame) - 1):
        current_volume = _to_float(frame["volume"].iloc[index])
        prior_avg = _to_float(frame["volume"].iloc[index - 20:index].mean())
        if current_volume is None or not prior_avg or prior_avg <= 0 or current_volume / prior_avg < 1.8:
            continue
        latest = {
            "date": str(frame["date"].iloc[index]),
            "high": _to_float(frame["high"].iloc[index]),
            "low": _to_float(frame["low"].iloc[index]),
            "volume_ratio": current_volume / prior_avg,
        }
    return latest


def _load_technical_daily_frame(item: RadarCandidate, analysis_date: date | None) -> pd.DataFrame | None:
    candidates = [item.symbol, f"{item.code}.TW", f"{item.code}.TWO", f"{item.code}_TW", f"{item.code}_TWO"]
    for symbol in candidates:
        if not symbol:
            continue
        stem = str(symbol).replace(".", "_")
        path = ROOT_DIR / ".cache" / "technical_daily" / f"{stem}.csv"
        if not path.exists():
            continue
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        if "date" not in frame.columns:
            continue
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).sort_values("date")
        if analysis_date:
            frame = frame[frame["date"].dt.date <= analysis_date]
        for column in ("open", "high", "low", "close", "volume", "adj_close"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = apply_point_in_time_adjustment(frame)
        return frame.tail(160)
    return None


def _build_sector_snapshot(item: RadarCandidate, analysis_date: date | None) -> dict[str, Any]:
    """Build point-in-time sector breadth from the full cached market universe."""

    target_date = analysis_date or get_tw_today()
    market = _load_market_sector_context(target_date)
    by_code = market.get("by_code") or {}
    theme_by_code = market.get("theme_by_code") or {}
    industry = str(item.industry or (by_code.get(item.code) or {}).get("industry") or "").strip()
    primary_theme = str(theme_by_code.get(item.code) or "").strip()
    combined_key = f"{industry}::{primary_theme}" if industry and primary_theme else ""
    combined_members = (market.get("combined_members") or {}).get(combined_key) or []
    if combined_key and len(combined_members) >= RADAR_SECTOR_MIN_GROUP_SIZE:
        peer_group = combined_key
        peer_group_label = f"{industry}／{primary_theme}"
        group_source = "official_industry_and_formal_primary_theme"
        members = combined_members
    else:
        peer_group = industry
        peer_group_label = industry
        group_source = "official_industry"
        members = (market.get("industry_members") or {}).get(industry) or []

    metrics = [by_code[code] for code in members if code in by_code]
    total_count = len(members)
    valid_count = len(metrics)
    coverage = valid_count / total_count if total_count else 0.0
    result: dict[str, Any] = {
        "industry": industry,
        "primary_theme": primary_theme or None,
        "peer_group": peer_group,
        "peer_group_label": peer_group_label,
        "group_source": group_source,
        "group_total_count": total_count,
        "group_valid_count": valid_count,
        "coverage_ratio": coverage,
        "as_of_date": target_date.isoformat(),
    }
    if total_count < RADAR_SECTOR_MIN_GROUP_SIZE:
        result["status"] = "insufficient_group_size"
        return result
    if coverage < RADAR_SECTOR_MIN_COVERAGE:
        result["status"] = "insufficient_coverage"
        return result

    candidate_metric = by_code.get(item.code)
    candidate_return = _to_float((candidate_metric or {}).get("return_20d"))
    peer_returns = sorted(
        value
        for value in (_to_float(metric.get("return_20d")) for metric in metrics)
        if value is not None
    )
    percentile = None
    if candidate_return is not None and peer_returns:
        percentile = 100.0 * sum(value <= candidate_return for value in peer_returns) / len(peer_returns)
    result.update(
        {
            "status": "covered" if percentile is not None else "candidate_metric_missing",
            "positive_20d_ratio": sum((_to_float(metric.get("return_20d")) or 0) > 0 for metric in metrics) / valid_count,
            "above_ma20_ratio": sum(bool(metric.get("above_ma20")) for metric in metrics) / valid_count,
            "new_high_breakout_ratio": sum(bool(metric.get("new_high_or_breakout")) for metric in metrics) / valid_count,
            "median_return_5d": float(pd.Series([metric["return_5d"] for metric in metrics]).median()),
            "volume_surge_ratio": sum((_to_float(metric.get("volume_ratio")) or 0) >= 1.5 for metric in metrics) / valid_count,
            "median_return_1d": float(pd.Series([metric["return_1d"] for metric in metrics]).median()),
            "candidate_return_20d": candidate_return,
            "candidate_relative_strength_percentile": percentile,
        }
    )
    return result


def _load_market_sector_context(target_date: date) -> dict[str, Any]:
    cache_key = target_date.isoformat()
    cached = _RADAR_SECTOR_CONTEXT_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] <= RADAR_SECTOR_CONTEXT_TTL_SECONDS:
        return cached[1]

    stock_path = ROOT_DIR / "stock_list.json"
    theme_path = ROOT_DIR / "config" / "company_theme_map.json"
    try:
        stock_payload = json.loads(stock_path.read_text(encoding="utf-8-sig"))
    except Exception:
        stock_payload = {}
    try:
        theme_payload = json.loads(theme_path.read_text(encoding="utf-8-sig"))
    except Exception:
        theme_payload = {}
    stocks = stock_payload.get("stocks") if isinstance(stock_payload, dict) else []
    stocks = stocks if isinstance(stocks, list) else []
    theme_by_code = _formal_primary_theme_map(theme_payload)
    by_code: dict[str, dict[str, Any]] = {}
    industry_members: dict[str, list[str]] = {}
    combined_members: dict[str, list[str]] = {}
    for row in stocks:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        industry = str(row.get("industry") or "").strip()
        if not code or not industry:
            continue
        industry_members.setdefault(industry, []).append(code)
        primary_theme = theme_by_code.get(code)
        if primary_theme:
            combined_members.setdefault(f"{industry}::{primary_theme}", []).append(code)
        metrics = _sector_price_metrics(code, symbol, industry, target_date)
        if metrics:
            by_code[code] = metrics
    context = {
        "by_code": by_code,
        "industry_members": industry_members,
        "combined_members": combined_members,
        "theme_by_code": theme_by_code,
    }
    _RADAR_SECTOR_CONTEXT_CACHE[cache_key] = (now, context)
    if len(_RADAR_SECTOR_CONTEXT_CACHE) > 8:
        oldest_key = min(_RADAR_SECTOR_CONTEXT_CACHE, key=lambda key: _RADAR_SECTOR_CONTEXT_CACHE[key][0])
        _RADAR_SECTOR_CONTEXT_CACHE.pop(oldest_key, None)
    return context


def _formal_primary_theme_map(payload: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(payload, dict):
        return result
    for raw_code, raw_entry in payload.items():
        if not isinstance(raw_entry, dict):
            continue
        theme = str(raw_entry.get("primary_theme") or "").strip()
        if not theme:
            continue
        statuses = raw_entry.get("theme_statuses") if isinstance(raw_entry.get("theme_statuses"), dict) else {}
        status = str(statuses.get(theme) or "formal").strip().lower()
        if status in {"candidate", "hypothesis", "hypothesis_only", "weak", "rejected"}:
            continue
        result[str(raw_code).strip()] = theme
    return result


def _sector_price_metrics(code: str, symbol: str, industry: str, target_date: date) -> dict[str, Any] | None:
    item = RadarCandidate(code=code, symbol=symbol, industry=industry)
    frame = _load_technical_daily_frame(item, target_date)
    if frame is None or len(frame) < 21 or not {"date", "high", "close", "volume"}.issubset(frame.columns):
        return None
    last_timestamp = pd.to_datetime(frame["date"].iloc[-1], errors="coerce")
    if pd.isna(last_timestamp) or last_timestamp.date() != target_date:
        return None
    close = _to_float(frame["close"].iloc[-1])
    previous_close = _to_float(frame["close"].iloc[-2])
    close5 = _to_float(frame["close"].iloc[-6]) if len(frame) >= 6 else None
    close20 = _to_float(frame["close"].iloc[-21])
    ma20 = _to_float(frame["close"].tail(20).mean())
    prior_high20 = _to_float(frame["high"].iloc[-21:-1].max())
    volume = _to_float(frame["volume"].iloc[-1])
    prior_volume20 = _to_float(frame["volume"].iloc[-21:-1].mean())
    required = (close, previous_close, close5, close20, ma20, prior_high20, volume, prior_volume20)
    if any(value is None for value in required) or not close20 or not close5 or not previous_close or not prior_volume20:
        return None
    return {
        "code": code,
        "industry": industry,
        "return_1d": (close / previous_close - 1) * 100,
        "return_5d": (close / close5 - 1) * 100,
        "return_20d": (close / close20 - 1) * 100,
        "above_ma20": close >= ma20,
        "new_high_or_breakout": close > prior_high20,
        "volume_ratio": volume / prior_volume20,
    }


def _load_price_metric_for_item(item: RadarCandidate) -> dict[str, Any]:
    path = ROOT_DIR / ".cache" / "price_metrics.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    if not isinstance(metrics, dict):
        return {}
    for key in (item.symbol, f"{item.code}.TW", f"{item.code}.TWO", item.code):
        value = metrics.get(key)
        if isinstance(value, dict):
            return dict(value)
    return {}


def _below_ma_streak(frame: pd.DataFrame, window: int) -> int:
    if frame.empty or "close" not in frame:
        return 0
    ma = frame["close"].rolling(window).mean()
    count = 0
    for close_value, ma_value in zip(reversed(frame["close"].iloc[:-1]), reversed(ma.iloc[:-1])):
        if pd.isna(close_value) or pd.isna(ma_value) or float(close_value) >= float(ma_value):
            break
        count += 1
    return count


def _recent_strategy_signal_counts(signals: list[dict[str, Any]]) -> dict[str, Any]:
    dated: list[tuple[date, str]] = []
    undated_codes: list[str] = []
    for signal in signals or []:
        code = str(signal.get("strategy_code") or "")
        if not code:
            continue
        signal_date = _parse_signal_date(signal.get("signal_date") or signal.get("date"))
        if signal_date:
            dated.append((signal_date, code))
        else:
            undated_codes.append(code)
    if not dated:
        return {
            "20d": len(undated_codes),
            "60d": len(undated_codes),
            "relay_60d": bool({"A", "B"} & set(undated_codes) and {"C", "D"} & set(undated_codes)),
        }
    latest = max(day for day, _code in dated)
    codes20 = [code for day, code in dated if (latest - day).days <= 30]
    codes60 = [code for day, code in dated if (latest - day).days <= 90]
    return {
        "20d": len(codes20),
        "60d": len(codes60),
        "relay_60d": bool({"A", "B"} & set(codes60) and {"C", "D"} & set(codes60)),
    }


def _parse_signal_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def _latest_revenue_yoy(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    return _to_float(rows[-1].get("yoy") or rows[-1].get("YoY") or rows[-1].get("YoY%") or rows[-1].get("revenue_yoy"))


def _chip_flow_meta(chip: dict[str, Any]) -> dict[str, Any]:
    institutional = _normalise_date_rows(chip.get("institutional_data") or [], "Date")
    recent5 = institutional[-5:]
    recent10 = institutional[-10:]
    previous_foreign10 = institutional[-20:-10]
    foreign5 = sum(_to_float(row.get("Foreign_Net_Lots")) or _to_float(row.get("foreign_net_lots")) or 0 for row in recent5)
    foreign10 = sum(_to_float(row.get("Foreign_Net_Lots")) or _to_float(row.get("foreign_net_lots")) or 0 for row in recent10)
    previous_foreign = sum(_to_float(row.get("Foreign_Net_Lots")) or _to_float(row.get("foreign_net_lots")) or 0 for row in previous_foreign10)
    trust5 = sum(_to_float(row.get("Investment_Trust_Net_Lots")) or _to_float(row.get("trust_net_lots")) or 0 for row in recent5)
    dealer5 = sum(_to_float(row.get("Dealer_Net_Lots")) or _to_float(row.get("dealer_net_lots")) or 0 for row in recent5)
    dealer10 = sum(_to_float(row.get("Dealer_Net_Lots")) or _to_float(row.get("dealer_net_lots")) or 0 for row in recent10)
    trust10 = sum(_to_float(row.get("Investment_Trust_Net_Lots")) or _to_float(row.get("trust_net_lots")) or 0 for row in recent10)
    total5 = foreign5 + trust5 + dealer5
    total10 = foreign10 + trust10 + dealer10
    margin = _normalise_date_rows(chip.get("margin_data") or [], "Date") or _normalise_date_rows(chip.get("margin_data") or [], "date")
    margin5 = margin[-5:]
    margin10 = margin[-10:]
    financing5 = None
    financing10 = None
    short_margin_ratio = None
    if margin:
        financing5 = sum(_to_float(row.get("Financing_Net_Change_Lots")) or _to_float(row.get("financing_net")) or 0 for row in margin5)
        financing10 = sum(_to_float(row.get("Financing_Net_Change_Lots")) or _to_float(row.get("financing_net")) or 0 for row in margin10)
        latest_margin = margin[-1]
        short_margin_ratio = _to_float(latest_margin.get("Short_Margin_Ratio") or latest_margin.get("short_margin_ratio"))
        if short_margin_ratio is None:
            financing_balance = _to_float(latest_margin.get("financing_balance"))
            short_balance = _to_float(latest_margin.get("short_balance"))
            if financing_balance and short_balance is not None:
                short_margin_ratio = short_balance / financing_balance * 100
    return {
        "institutional_rows": len(institutional),
        "foreign5": foreign5,
        "foreign10": foreign10,
        "previous_foreign10": previous_foreign,
        "trust5": trust5,
        "trust10": trust10,
        "dealer5": dealer5,
        "total5": total5,
        "total10": total10,
        "financing5": financing5,
        "financing10": financing10,
        "short_margin_ratio": short_margin_ratio,
    }


def _institutional_confirmation(meta: dict[str, Any]) -> dict[str, bool]:
    return {
        "confirmed": bool(meta.get("total5", 0) > 0 and meta.get("total10", 0) > 0),
        "foreign_turnaround": bool(meta.get("foreign10", 0) > 0 and meta.get("previous_foreign10", 0) < 0),
    }


def _score_detail(score: float, max_score: int, reasons: list[str], risks: list[str] | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "score": int(max(0, min(max_score, round(score)))),
        "reasons": _unique_texts(reasons),
        "risks": _unique_texts(risks or []),
        "details": details or {},
    }


def _score_technical_detail(item: RadarCandidate, snapshot: dict[str, Any]) -> dict[str, Any]:
    tech = snapshot.get("technical") or {}
    reasons: list[str] = []
    risks: list[str] = []
    details: dict[str, Any] = {}
    trend = 0.0
    above = tech.get("above_ma") or {}
    reclaim = tech.get("reclaim_ma") or {}
    slopes = tech.get("ma_slopes") or {}
    if above.get("ma5") or reclaim.get("ma5"):
        trend += 1; reasons.append("收盤站回 MA5")
    if above.get("ma10") or above.get("ma13") or reclaim.get("ma10") or reclaim.get("ma13"):
        trend += 1; reasons.append("收盤站回 MA10/MA13")
    if above.get("ma20") or above.get("ma21") or reclaim.get("ma20") or reclaim.get("ma21"):
        trend += 2; reasons.append("收盤站回 MA20/MA21")
    if above.get("ma60") or reclaim.get("ma60"):
        trend += 2; reasons.append("收盤站回 MA60")
    if above.get("ma105") or reclaim.get("ma105"):
        trend += 2; reasons.append("收盤站回 MA105")
    if slopes.get("ma5") == "up":
        trend += 1; reasons.append("MA5 上彎")
    if slopes.get("ma10") == "up" or slopes.get("ma13") == "up":
        trend += 1; reasons.append("MA10/MA13 上彎")
    if slopes.get("ma20") == "up" or slopes.get("ma21") == "up":
        trend += 1; reasons.append("MA20/MA21 走平轉上")
    trend = min(8, trend)

    reversal = 0.0
    d60 = _to_float(tech.get("distance_from_60d_low_pct"))
    d120 = _to_float(tech.get("distance_from_120d_low_pct"))
    if d60 is not None and d60 < 20 and (above.get("ma20") or above.get("ma21")):
        reversal += 1; reasons.append("距 60 日低點 20% 內轉強")
    if d120 is not None and d120 < 30 and (above.get("ma20") or above.get("ma21")):
        reversal += 1; reasons.append("距 120 日低點 30% 內轉強")
    if item.technical_signals:
        reversal += 1; reasons.append("既有技術策略觸發")
    if any(str(signal.get("strategy_code") or "") == "C" for signal in item.technical_signals):
        reversal += 1.5; reasons.append("低檔背離/反轉策略觸發")
    if any(_has_kd_low_divergence(signal) for signal in item.technical_signals):
        reversal += 1; reasons.append("KD 低檔背離或轉強")
    if any(_has_dif_support(signal) for signal in item.technical_signals):
        reversal += 1; reasons.append("DIF 接近零軸或轉強")
    reversal = min(5, reversal)

    # Volume is evidence for a price trigger, not a standalone bullish signal.
    volume = 0.0
    volume_penalty = 0.0
    volume_ratio = _to_float(tech.get("volume_ratio"))
    pretrigger_ratio = _to_float(tech.get("pretrigger_volume_ratio"))
    close_location = _to_float(tech.get("close_location_value"))
    close = _to_float(tech.get("close"))
    previous_close = _to_float(tech.get("previous_close"))
    open_price = _to_float(tech.get("open"))
    price_trigger = bool(
        tech.get("breakout_20d")
        or tech.get("breakout_60d")
        or any(bool(value) for value in reclaim.values())
        or item.technical_signals
        or item.strategy_codes
        or tech.get("closes_above_large_volume_high")
    )
    volume_context_ok = tech.get("volume_context_status") == "ok"
    if tech.get("status") == "ok" and not volume_context_ok:
        risks.append("量能歷史不足 30 日，量能分項不加分")
    if volume_context_ok and price_trigger:
        if pretrigger_ratio is not None and pretrigger_ratio <= 0.8:
            volume += 1
            reasons.append("觸發前五日量縮至前期均量八成以下")
        if volume_ratio is not None and close is not None and previous_close is not None and close > previous_close:
            if 1.2 <= volume_ratio <= 3:
                volume += 2
                reasons.append("價位觸發且量比 1.2 至 3 倍")
            elif 1 <= volume_ratio < 1.2:
                volume += 1
                reasons.append("價位觸發且量能溫和增加")
            elif volume_ratio > 3 and close_location is not None and close_location >= 0.67:
                volume += 1
                reasons.append("爆量觸發但收盤仍守在當日高檔")
        if close_location is not None and close_location >= 0.67:
            volume += 1
            reasons.append("觸發日收盤位於當日振幅上三分之一")
        if tech.get("closes_above_large_volume_high"):
            volume += 1
            reasons.append("收盤站上近期大量 K 棒高點")
    volume = min(5, volume)

    if volume_context_ok and volume_ratio is not None:
        weak_close = bool(
            (close_location is not None and close_location < 0.5)
            or (close is not None and open_price is not None and close <= open_price)
            or tech.get("intraday_breakout_failed")
        )
        if volume_ratio >= 5 and not tech.get("price_up_volume_up"):
            volume_penalty = max(volume_penalty, 3)
            risks.append("量比至少 5 倍但價格未正向反應")
        elif volume_ratio >= 3 and weak_close:
            volume_penalty = max(volume_penalty, 2)
            risks.append("爆量但收盤轉弱或突破失敗")
        if tech.get("closes_below_large_volume_low"):
            volume_penalty = max(volume_penalty, 2)
            risks.append("跌破近期大量 K 棒低點")
    volume_penalty = min(3, volume_penalty)

    shakeout = 0.0
    if tech.get("recent_break_low_recover"):
        shakeout += 2; reasons.append("跌破轉折低點後快速收復")
    if tech.get("long_lower_shadow") and (above.get("ma20") or above.get("ma21")):
        shakeout += 1.5; reasons.append("長下影後收回 MA20/MA21")
    if reclaim.get("ma20") or reclaim.get("ma21"):
        shakeout += 1.5; reasons.append("跌破 MA20/MA21 後快速站回")
    if reclaim.get("ma60"):
        shakeout += 2; reasons.append("跌破 MA60 後快速站回")
    if any(str(signal.get("strategy_code") or "") == "D" for signal in item.technical_signals):
        shakeout += 1.5; reasons.append("急跌收復策略觸發")
    shakeout = min(5, shakeout)

    breakout = 0.0
    if tech.get("breakout_20d"):
        breakout += 1; reasons.append("突破近 20 日高點")
    if tech.get("breakout_60d"):
        breakout += 1.5; reasons.append("突破近 60 日高點")
    if any(str(signal.get("strategy_code") or "") in {"A", "B"} for signal in item.technical_signals):
        breakout += 1; reasons.append("回測突破策略觸發")
    breakout = min(4, breakout)

    cross = 0.0
    strategies = set(item.strategy_codes)
    if strategies:
        cross += 1; reasons.append("命中 A/B/C/D 技術策略")
    if len(strategies) >= 2:
        cross += 1; reasons.append("多策略族群交叉確認")
    if strategies & {"A", "B"} and strategies & {"C", "D"}:
        cross += 1; reasons.append("趨勢策略與反轉/收復策略交叉")
    cross = min(3, cross)

    score = trend + reversal + volume + shakeout + breakout + cross - volume_penalty
    deviation = _to_float(tech.get("ma20_deviation_pct"))
    if deviation is not None and deviation > 25:
        score -= 2; risks.append("乖離 MA20 超過 25%")
    if any("高風險" in str(signal.get("notes") or "") for signal in item.technical_signals):
        score -= 2; risks.append("既有技術訊號標記高風險")
    details.update(
        {
            "trend": trend,
            "reversal": reversal,
            "volume": volume,
            "volume_penalty": volume_penalty,
            "volume_triggered": price_trigger,
            "volume_context_status": tech.get("volume_context_status") or "unknown",
            "shakeout": shakeout,
            "breakout": breakout,
            "strategy_cross": cross,
        }
    )
    return _score_detail(score, 30, reasons, risks, details)


def _score_revenue_detail(item: RadarCandidate, snapshot: dict[str, Any]) -> dict[str, Any]:
    rows = _normalise_revenue_rows((snapshot.get("revenue") or {}).get("history") or item.revenue_history)
    reasons: list[str] = []
    risks: list[str] = []
    if not rows:
        return _score_detail(0, 20, [], ["營收資料缺漏"], {"row_count": 0})
    latest = rows[-1]
    yoy_values = [_to_float(row.get("yoy") or row.get("YoY") or row.get("YoY%") or row.get("revenue_yoy")) for row in rows]
    mom_values = [_to_float(row.get("mom") or row.get("MoM%")) for row in rows]
    latest_yoy = yoy_values[-1]
    latest_mom = mom_values[-1] if mom_values else None
    score = 0.0
    if latest_yoy is not None:
        if latest_yoy > 50:
            score += 4; reasons.append("最新月營收 YoY 大於 50%")
        elif latest_yoy >= 30:
            score += 3; reasons.append("最新月營收 YoY 30% 以上")
        elif latest_yoy >= 10:
            score += 2; reasons.append("最新月營收 YoY 10% 以上")
    recent3 = [value for value in yoy_values[-3:] if value is not None]
    recent5 = [value for value in yoy_values[-5:] if value is not None]
    if sum(1 for value in recent3 if value > 0) >= 2:
        score += 2; reasons.append("近 3 月 YoY 有 2 月以上為正")
    if sum(1 for value in recent5 if value > 0) >= 4:
        score += 2; reasons.append("近 5 月 YoY 有 4 月以上為正")
    if len(yoy_values) >= 2 and yoy_values[-2] is not None and latest_yoy is not None and yoy_values[-2] < 0 <= latest_yoy:
        score += 2; reasons.append("營收 YoY 由負轉正")
    acceleration = _consecutive_increase_count([value for value in yoy_values if value is not None])
    if acceleration >= 3:
        score += 3; reasons.append("YoY 連續 3 個月加速")
    elif acceleration >= 2:
        score += 2; reasons.append("YoY 連續 2 個月加速")
    revenues = [_to_float(row.get("revenue") or row.get("Monthly_Revenue") or row.get("monthly_revenue")) for row in rows]
    if revenues[-1] is not None and len([value for value in revenues[-6:] if value is not None]) >= 3 and revenues[-1] >= max(value for value in revenues[-6:] if value is not None):
        score += 2; reasons.append("最新月營收創 6 個月高")
    if revenues[-1] is not None and len([value for value in revenues[-12:] if value is not None]) >= 6 and revenues[-1] >= max(value for value in revenues[-12:] if value is not None):
        score += 3; reasons.append("最新月營收創 12 個月高")
    if latest_mom is not None:
        if latest_mom > 20:
            score += 2; reasons.append("最新月 MoM 大於 20%")
        elif latest_mom > 0:
            score += 1; reasons.append("最新月 MoM 為正")
        if latest_yoy is not None and latest_yoy > 0 and latest_mom > 0:
            score += 1; reasons.append("YoY 與 MoM 同步轉正")
    if latest_yoy is not None and latest_yoy > 0 and ((snapshot.get("theme_news") or {}).get("local_news") or (snapshot.get("theme_news") or {}).get("web_sources")):
        score += 2; reasons.append("營收轉強與題材/新聞可交叉觀察")
    price_metrics = (snapshot.get("technical") or {}).get("price_metrics") or {}
    change20 = _to_float(price_metrics.get("change_pct_20d"))
    if latest_yoy is not None and latest_yoy > 20 and (change20 is None or change20 < 30):
        score += 2; reasons.append("營收轉強但股價尚未大幅反映")
    if latest_yoy is not None and latest_yoy >= 15 and (change20 is None or change20 < 18):
        prior_negative_count = sum(1 for value in recent5[:-1] if value is not None and value <= 0)
        if prior_negative_count >= 2 or acceleration >= 2:
            score += 2; reasons.append("早期營收轉機，市場可能尚未完全定價")
    if latest_yoy is not None and latest_yoy > 30 and latest_mom is not None and latest_mom < -30:
        score -= 2; risks.append("單月 YoY 強但 MoM 大幅下滑")
    if len(recent5) >= 4 and sum(1 for value in recent5[:-1] if value <= 0) >= 3 and latest_yoy and latest_yoy > 50:
        score -= 2; risks.append("營收可能只是單月跳動")
    return _score_detail(score, 20, reasons, risks, {"latest": latest, "latest_yoy": latest_yoy, "latest_mom": latest_mom})


def _score_financial_detail(item: RadarCandidate, snapshot: dict[str, Any]) -> dict[str, Any]:
    financial = snapshot.get("financial") or {}
    rows = _normalise_financial_rows(financial.get("financial_data"))
    reasons: list[str] = []
    risks: list[str] = []
    if not rows:
        revenue_rows = _normalise_revenue_rows((snapshot.get("revenue") or {}).get("history") or item.revenue_history)
        latest_yoy = _to_float(revenue_rows[-1].get("yoy") or revenue_rows[-1].get("YoY") or revenue_rows[-1].get("YoY%")) if revenue_rows else None
        technical = snapshot.get("technical") or {}
        chip_grades = (snapshot.get("chip") or {}).get("grades") or {}
        provisional = 0.0
        if latest_yoy is not None and latest_yoy >= 15:
            provisional += 3.0
            reasons.append("財報尚未反映，但月營收已有早期轉機線索")
        if technical.get("status") == "ok" and (technical.get("volume_ratio") or 0) >= 1.2:
            provisional += 2.0
            reasons.append("價量已有初步反應，財報空窗期採觀察分")
        if any(str(grade).upper() in {"S", "A", "B"} for grade in chip_grades.values()):
            provisional += 2.0
            reasons.append("籌碼評級提供財報前的輔助驗證")
        return _score_detail(provisional, 15, reasons, ["財報資料缺漏，需等待下一季財報驗證"], {"row_count": 0, "latest_yoy": latest_yoy, "provisional": True})
    latest = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else {}
    score = 0.0
    eps_values = [_to_float(row.get("EPS") or row.get("eps")) for row in rows]
    latest_eps = eps_values[-1]
    prev_eps = eps_values[-2] if len(eps_values) >= 2 else None
    if latest_eps is not None and prev_eps is not None:
        if prev_eps < 0 <= latest_eps:
            score += 3; reasons.append("EPS 由虧轉盈")
        elif latest_eps < 0 and prev_eps < 0 and latest_eps > prev_eps and abs(prev_eps) > 0 and (abs(prev_eps) - abs(latest_eps)) / abs(prev_eps) >= 0.5:
            score += 2; reasons.append("EPS 虧損收斂超過 50%")
    eps_improve = _consecutive_increase_count([value for value in eps_values if value is not None])
    if eps_improve >= 3:
        score += 3; reasons.append("EPS 連續 3 季改善")
    elif eps_improve >= 2:
        score += 2; reasons.append("EPS 連續 2 季改善")
    gm_latest = _to_float(latest.get("Gross_Margin") or latest.get("gross_margin"))
    gm_prev = _to_float(prev.get("Gross_Margin") or prev.get("gross_margin"))
    if gm_latest is not None and gm_prev is not None and gm_latest > gm_prev:
        score += 1.5; reasons.append("毛利率季增")
    gm_improve = _consecutive_increase_count([_to_float(row.get("Gross_Margin") or row.get("gross_margin")) for row in rows if _to_float(row.get("Gross_Margin") or row.get("gross_margin")) is not None])
    if gm_improve >= 2:
        score += 2; reasons.append("毛利率連續改善")
    op_latest = _to_float(latest.get("Operating_Margin") or latest.get("operating_margin"))
    op_prev = _to_float(prev.get("Operating_Margin") or prev.get("operating_margin"))
    if op_latest is not None and op_prev is not None:
        if op_prev < 0 <= op_latest:
            score += 3; reasons.append("營益率由負轉正")
        elif op_latest < 0 and op_latest > op_prev:
            score += 2; reasons.append("營益率虧損收斂")
    net_latest = _to_float(latest.get("Net_Income") or latest.get("net_income"))
    net_prev = _to_float(prev.get("Net_Income") or prev.get("net_income"))
    if net_latest is not None and net_prev is not None and net_prev < 0 <= net_latest:
        score += 2; reasons.append("稅後淨利轉正")
    ocf = _to_float(latest.get("Operating_Cash_Flow") or latest.get("operating_cash_flow"))
    fcf = _to_float(latest.get("Free_Cash_Flow") or latest.get("free_cash_flow"))
    if ocf is not None and ocf > 0:
        score += 2; reasons.append("營業現金流為正")
    if fcf is not None and fcf > 0:
        score += 1; reasons.append("自由現金流為正")
    valuation = financial.get("valuation_data") or {}
    latest_val = valuation.get("latest") if isinstance(valuation, dict) else {}
    pb = _to_float((latest_val or {}).get("pb_ratio")) if isinstance(latest_val, dict) else None
    if pb is not None and pb < 2 and (latest_eps is not None and (latest_eps > 0 or eps_improve >= 2)):
        score += 1.5; reasons.append("PB 偏低且財報改善")
    if ocf is not None and ocf < 0 and latest_eps is not None and latest_eps > 0:
        score -= 2; risks.append("EPS 改善但營業現金流為負")
    if gm_latest is not None and gm_prev is not None and gm_latest < gm_prev:
        risks.append("毛利率下滑")
    return _score_detail(score, 15, reasons, risks, {"latest": latest, "latest_eps": latest_eps, "pb_ratio": pb})


def _score_chip_detail(item: RadarCandidate, snapshot: dict[str, Any]) -> dict[str, Any]:
    chip = snapshot.get("chip") or {}
    reasons: list[str] = []
    risks: list[str] = []
    score = 0.0
    for key, grade in (chip.get("grades") or {}).items():
        grade_value = str(grade).upper()
        score += {"S": 3.5, "A": 3.0, "B": 2.0, "C": 0.5}.get(grade_value, 0.5)
        if grade_value in {"S", "A", "B"}:
            reasons.append(f"{CHIP_STRATEGY_LABELS.get(key, key)} {grade_value}級")
    revenue_rows = _normalise_revenue_rows((snapshot.get("revenue") or {}).get("history") or item.revenue_history)
    latest_yoy = _to_float(revenue_rows[-1].get("yoy") or revenue_rows[-1].get("YoY") or revenue_rows[-1].get("YoY%")) if revenue_rows else None
    if latest_yoy is not None and latest_yoy >= 15 and any(str(grade).upper() in {"S", "A", "B"} for grade in (chip.get("grades") or {}).values()):
        score += 1.5; reasons.append("營收轉強與籌碼初動同步")
    institutional = _normalise_date_rows(chip.get("institutional_data") or [], "Date")
    recent5 = institutional[-5:]
    recent10 = institutional[-10:]
    foreign5 = sum(_to_float(row.get("Foreign_Net_Lots")) or 0 for row in recent5)
    foreign10 = sum(_to_float(row.get("Foreign_Net_Lots")) or 0 for row in recent10)
    trust5 = sum(_to_float(row.get("Investment_Trust_Net_Lots")) or 0 for row in recent5)
    dealer5 = sum(_to_float(row.get("Dealer_Net_Lots")) or 0 for row in recent5)
    total5 = foreign5 + trust5 + dealer5
    if foreign5 > 0:
        score += 2; reasons.append("外資近 5 日買超")
    if foreign10 > 0 and sum(_to_float(row.get("Foreign_Net_Lots")) or 0 for row in institutional[-20:-10]) < 0:
        score += 2; reasons.append("外資近 10 日由賣轉買")
    if trust5 > 0:
        score += 2; reasons.append("投信近 5 日買超")
    if dealer5 > 0:
        score += 1; reasons.append("自營商近 5 日買超")
    if total5 > 0:
        score += 2; reasons.append("三大法人合計轉買")
    tdcc = chip.get("tdcc_data") or {}
    if isinstance(tdcc, dict):
        large = _to_float(tdcc.get("large_holder_pct"))
        retail = _to_float(tdcc.get("retail_holder_pct"))
        if large is not None and large >= 60:
            score += 2; reasons.append("TDCC 大戶持股集中")
        if retail is not None and retail <= 35:
            score += 1; reasons.append("散戶持股比偏低")
    margin = _normalise_date_rows(chip.get("margin_data") or [], "Date")
    if margin:
        latest_margin = margin[-1]
        financing_change = _to_float(latest_margin.get("Financing_Net_Change_Lots"))
        short_ratio = _to_float(latest_margin.get("Short_Margin_Ratio"))
        if financing_change is not None and financing_change <= 0:
            score += 1; reasons.append("融資未明顯增加")
        elif financing_change is not None and financing_change > 500 and total5 <= 0:
            score -= 3; risks.append("融資暴增但法人未買")
        if short_ratio is not None and short_ratio >= 30:
            score += 1; reasons.append("券資比偏高具軋空可能")
    if foreign5 < 0 and trust5 < 0 and total5 < 0:
        score -= 2; risks.append("法人近期合計偏賣")
    return _score_detail(score, 15, reasons, risks, {"foreign5": foreign5, "trust5": trust5, "dealer5": dealer5, "total5": total5})


def _score_theme_news_detail(item: RadarCandidate, snapshot: dict[str, Any]) -> dict[str, Any]:
    theme = snapshot.get("theme_news") or {}
    local_news = [row for row in theme.get("local_news") or [] if isinstance(row, dict)]
    web_sources = [row for row in theme.get("web_sources") or [] if isinstance(row, dict)]
    ai_sources = [row for row in theme.get("ai_sources") or [] if isinstance(row, dict)]
    topic_context = theme.get("topic_context") if isinstance(theme.get("topic_context"), dict) else {}
    reasons: list[str] = []
    risks: list[str] = []
    analysis_date = _theme_analysis_date(snapshot)
    formal_topics = _formal_theme_topics(topic_context)
    formal_theme_ids = {str(row.get("theme_id") or "") for row in formal_topics}
    nodes = [
        row for row in topic_context.get("supply_chain_nodes") or []
        if isinstance(row, dict) and (not formal_theme_ids or str(row.get("theme_id") or "") in formal_theme_ids)
    ]

    relation_score = 0
    if formal_topics:
        relation_score = 3
        reasons.append("正式題材關聯已建立")
        if any(_is_specific_verified_theme_node(row) for row in nodes):
            relation_score = 5
            reasons.append("公司角色、產品或客戶鏈結具正式證據")
    else:
        weak_topics = topic_context.get("matched_topics") or []
        if weak_topics:
            risks.append("僅有候選、推測或弱關鍵字題材，不列入正式評分")

    evidence = _normalise_theme_evidence(
        item,
        analysis_date,
        local_news,
        web_sources,
        ai_sources,
        formal_topics,
        nodes,
    )
    current_evidence = [row for row in evidence if row["age_days"] is not None and row["age_days"] <= 90]
    stale_evidence = [row for row in evidence if row["age_days"] is not None and 90 < row["age_days"] <= 180]
    current_l1 = [row for row in current_evidence if row["level"] == 1 and row["has_body"]]
    current_l2 = [row for row in current_evidence if row["level"] == 2 and row["has_body"]]
    source_score = 0
    if current_l1:
        source_score = 4
        reasons.append("近期具日期的官方一級來源直接佐證")
    elif len({row["publisher"] for row in current_l2 if row["publisher"]}) >= 2:
        source_score = 3
        reasons.append("近期至少兩個獨立二級來源交叉佐證")
    elif current_l2:
        source_score = 2
        reasons.append("近期具日期的二級來源直接佐證")
    elif any(row["level"] in {1, 2} and row["has_body"] for row in stale_evidence):
        source_score = 2
        reasons.append("來源已逾 90 日，僅保留部分證據分")

    usable_current = [row for row in current_evidence if row["level"] in {1, 2} and row["has_body"]]
    evidence_text = " ".join(row["text"] for row in usable_current)
    specific_action = bool(re.search(r"量產|出貨|接單|訂單|得標|投產|擴產|認證|導入|上修|啟用|合作", evidence_text))
    company_link = bool(
        (item.code and item.code in evidence_text)
        or (item.name and item.name in evidence_text)
        or any(_is_specific_verified_theme_node(row) for row in nodes)
    )
    quantified = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|％|億|萬|千|百萬|兆|台|套|顆|GW|MW)", evidence_text, re.IGNORECASE))
    timed = bool(re.search(r"20\d{2}|第?[一二三四1-4]季|Q[1-4]|上半年|下半年|\d+月|量產|出貨|啟用", evidence_text, re.IGNORECASE))
    catalyst_score = 0
    if specific_action and company_link and quantified and timed:
        catalyst_score = 4
        reasons.append("催化事件含數字、時程與公司影響")
    elif specific_action and company_link:
        catalyst_score = 2
        reasons.append("已有公司層級具體行動，但數字或時程仍不完整")

    materiality_score = _theme_materiality_score(nodes, evidence_text)
    if materiality_score == 2:
        reasons.append("題材營收曝險具量化或高占比依據")
    elif materiality_score == 1:
        reasons.append("題材已確認為公司主要產品或營運區段")
    else:
        risks.append("題材對公司營收或獲利的重要性尚不明")

    score = relation_score + source_score + catalyst_score + materiality_score
    cap: int | None = None
    if not formal_topics:
        score = 0
        cap = 0
    elif any(_official_theme_contradiction(row) for row in current_l1):
        score = 0
        cap = 0
        risks.append("近期官方來源否認、取消或終止該題材關聯")
    else:
        if not current_l1 and not current_l2:
            cap = 5
            risks.append("正式題材缺少近 90 日一級或二級來源，題材分上限 5")
        elif evidence and not any(row["level"] in {1, 2} and row["has_body"] for row in current_evidence):
            cap = 5
            risks.append("題材來源僅有三級、標題、無日期或過期資料，題材分上限 5")
        if materiality_score == 0:
            cap = min(cap, 10) if cap is not None else 10
        if cap is not None:
            score = min(score, cap)
    if any("注意" in str(row.get("title") or "") or "處置" in str(row.get("title") or "") for row in local_news):
        risks.append("新聞含注意股/處置訊息")
    return _score_detail(
        score,
        15,
        reasons,
        risks,
        {
            "relation": relation_score,
            "source_quality": source_score,
            "catalyst_specificity": catalyst_score,
            "materiality": materiality_score,
            "score_cap": cap,
            "formal_topic_count": len(formal_topics),
            "eligible_evidence_count": len(evidence),
            "current_l1_count": len(current_l1),
            "current_l2_count": len(current_l2),
            "ignored_top_only_source_count": sum(
                not bool(row.get("formal_scoring_eligible") or row.get("uniform_coverage"))
                for row in [*web_sources, *ai_sources]
            ),
        },
    )


def _theme_analysis_date(snapshot: dict[str, Any]) -> date:
    parsed = parse_date_like(snapshot.get("analysis_date"))
    return parsed or get_tw_today()


def _formal_theme_topics(topic_context: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in topic_context.get("matched_topics") or []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("verification_status") or "").strip().lower()
        confidence = str(row.get("confidence") or "").strip().lower()
        policy = str(row.get("usage_policy") or "").strip().lower()
        if row.get("not_representative") or status == "candidate" or policy == "hypothesis_only":
            continue
        if confidence == "high" or status in {"formal", "verified", "confirmed"} or policy == "formal_topic_reference":
            result.append(row)
    return result


def _is_specific_verified_theme_node(row: dict[str, Any]) -> bool:
    status = str(row.get("verification_status") or "").strip().lower()
    confidence = str(row.get("confidence") or "").strip().lower()
    level = _theme_source_level(row.get("source_level"))
    verified = status in {"formal", "verified", "confirmed"} or (confidence == "high" and level == 1)
    role = bool(str(row.get("role") or "").strip())
    products = row.get("product_keywords") or row.get("products") or []
    customers = row.get("customers") or []
    return bool(verified and role and (products or customers))


def _normalise_theme_evidence(
    item: RadarCandidate,
    analysis_date: date,
    local_news: list[dict[str, Any]],
    web_sources: list[dict[str, Any]],
    ai_sources: list[dict[str, Any]],
    formal_topics: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = list(local_news)
    raw.extend(
        row for row in [*web_sources, *ai_sources]
        if row.get("formal_scoring_eligible") or row.get("uniform_coverage")
    )
    for owner in [*formal_topics, *nodes]:
        raw.extend(row for row in owner.get("evidence") or [] if isinstance(row, dict))

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in raw:
        published = parse_date_like(
            source.get("publish_date")
            or source.get("published_at")
            or source.get("published_date")
            or source.get("date")
        )
        if published and published > analysis_date:
            continue
        title = str(source.get("title") or "").strip()
        body = str(source.get("content") or source.get("summary") or source.get("snippet") or "").strip()
        url = str(source.get("canonical_url") or source.get("url") or "").strip()
        publisher = str(source.get("source") or source.get("provider") or "").strip().lower()
        if not publisher and url:
            domain = re.search(r"https?://(?:www\.)?([^/]+)", url, re.IGNORECASE)
            publisher = domain.group(1).lower() if domain else ""
        event_id = str(source.get("event_id") or source.get("canonical_event_id") or "").strip().lower()
        normalized_title = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title.lower())
        dedupe_key = event_id or url.lower() or f"{normalized_title}:{published.isoformat() if published else ''}"
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        age_days = (analysis_date - published).days if published else None
        result.append(
            {
                "title": title,
                "body": body,
                "text": " ".join(value for value in (title, body) if value),
                "url": url,
                "publisher": publisher,
                "published_at": published.isoformat() if published else None,
                "age_days": age_days,
                "level": _theme_source_level(source.get("source_level"), source=source),
                "has_body": bool(body),
                "stock_code": item.code,
            }
        )
    return result


def _theme_source_level(value: Any, *, source: dict[str, Any] | None = None) -> int:
    text = str(value or "").strip().lower().replace("_", " ")
    if "l1" in text or "level 1" in text or "official" in text:
        return 1
    if "l2" in text or "level 2" in text or "media" in text:
        return 2
    if "l3" in text or "level 3" in text:
        return 3
    source = source or {}
    identity = " ".join(
        str(source.get(key) or "").lower()
        for key in ("source", "provider", "url")
    )
    if any(term in identity for term in ("mops", "twse", "tpex", "gov.tw", "公司官網", "法說", "press release", "newsroom")):
        return 1
    return 3


def _theme_materiality_score(nodes: list[dict[str, Any]], evidence_text: str) -> int:
    for row in nodes:
        exposure = row.get("revenue_exposure")
        if isinstance(exposure, dict):
            level = str(exposure.get("level") or "").strip().lower()
            description = str(exposure.get("description") or "")
        else:
            level = str(exposure or "").strip().lower()
            description = str(exposure or "")
        if level in {"high", "material", "significant", "major", "core"}:
            return 2
        if re.search(r"\d+(?:\.\d+)?\s*(?:%|％)", description):
            return 2
    if re.search(r"(?:營收|業務|產品).{0,12}\d+(?:\.\d+)?\s*(?:%|％)", evidence_text):
        return 2
    if any(_is_specific_verified_theme_node(row) for row in nodes):
        return 1
    return 0


def _official_theme_contradiction(row: dict[str, Any]) -> bool:
    if row.get("level") != 1:
        return False
    text = str(row.get("text") or "")
    return bool(re.search(r"否認|澄清.{0,12}(?:不實|未參與|無此)|取消|終止|未參與|無合作", text))


def _score_sector_detail(item: RadarCandidate, snapshot: dict[str, Any]) -> dict[str, Any]:
    sector = snapshot.get("sector") or {}
    reasons: list[str] = []
    risks: list[str] = []
    score = 0.0
    status = str(sector.get("status") or "unknown")
    if status != "covered":
        if status == "insufficient_group_size":
            risks.append("正式族群樣本少於 4 檔，族群分不加分")
        elif status == "insufficient_coverage":
            risks.append("族群同日價量資料覆蓋率低於 70%，族群分不加分")
        elif status == "candidate_metric_missing":
            risks.append("候選股缺少同日 20 日報酬，無法計算族群相對強弱")
        else:
            risks.append("缺少全市場同日族群資料，族群分不加分")
        return _score_detail(0, 5, reasons, risks, dict(sector))
    if (_to_float(sector.get("positive_20d_ratio")) or 0) >= 0.60:
        score += 1
        reasons.append("族群至少六成個股近 20 日上漲")
    if (_to_float(sector.get("above_ma20_ratio")) or 0) >= 0.60:
        score += 1
        reasons.append("族群至少六成個股站上 MA20")
    if (
        (_to_float(sector.get("new_high_breakout_ratio")) or 0) >= 0.15
        and (_to_float(sector.get("median_return_5d")) or 0) > 0
    ):
        score += 1
        reasons.append("族群創高擴散且近 5 日中位數報酬為正")
    if (
        (_to_float(sector.get("volume_surge_ratio")) or 0) >= 0.20
        and (_to_float(sector.get("median_return_1d")) or 0) > 0
    ):
        score += 1
        reasons.append("族群量增擴散且當日中位數報酬為正")
    if (_to_float(sector.get("candidate_relative_strength_percentile")) or 0) >= 75:
        score += 1
        reasons.append("個股近 20 日相對強度位於族群前 25%")
    return _score_detail(score, 5, reasons, risks, dict(sector))


def _apply_radar_score_caps(
    total: int,
    components: dict[str, int],
    details: dict[str, dict[str, Any]],
) -> tuple[int, list[str]]:
    caps: list[tuple[int, str]] = []
    technical = components.get("technical", 0)
    revenue = components.get("revenue", 0)
    financial = components.get("financial", 0)
    theme = components.get("theme", 0)
    chip = components.get("chip", 0)
    if technical <= 0:
        caps.append((60, "技術未觸發，總分最高 60"))
    if technical >= 18 and revenue < 6 and financial < 4:
        caps.append((65, "只有技術轉強，營收/財報弱，總分最高 65"))
    if theme >= 8 and revenue < 6 and financial < 4:
        caps.append((60, "只有題材，營收/財報弱，總分最高 60"))
    theme_risks = " ".join(details.get("theme", {}).get("risks") or [])
    if "缺少可驗證來源" in theme_risks:
        caps.append((70, "題材無法驗證，總分最高 70"))
    chip_risks = " ".join(details.get("chip", {}).get("risks") or [])
    if "融資暴增" in chip_risks or (chip <= 3 and technical >= 20):
        caps.append((70, "籌碼散戶化或缺乏法人支撐，總分最高 70"))
    financial_risks = " ".join(details.get("financial", {}).get("risks") or [])
    if "現金流" in financial_risks:
        caps.append((75, "財報品質或現金流有疑慮，總分最高 75"))
    technical_risks = " ".join(details.get("technical", {}).get("risks") or [])
    if "乖離" in technical_risks:
        caps.append((75, "技術短線過熱，總分最高 75"))
    if technical >= 24 and revenue < 6 and financial < 4 and theme < 5:
        caps.append((70, "短線大漲但基本面支撐不足，總分最高 70"))
    if not caps:
        return total, []
    cap_value = min(cap for cap, _reason in caps)
    return min(total, cap_value), [reason for _cap, reason in caps if _cap == cap_value or total > _cap]


def _top_unique_reasons(details: dict[str, dict[str, Any]], limit: int) -> list[str]:
    ordered: list[str] = []
    for key in ("technical", "revenue", "financial", "chip", "theme", "sector"):
        ordered.extend(str(reason) for reason in details.get(key, {}).get("reasons") or [])
    return _unique_texts(ordered)[:limit]


def _top_unique_risks(details: dict[str, dict[str, Any]], caps: list[str], limit: int) -> list[str]:
    ordered: list[str] = []
    for key in ("technical", "revenue", "financial", "chip", "theme", "sector"):
        ordered.extend(str(risk) for risk in details.get(key, {}).get("risks") or [])
    ordered.extend(caps)
    return _unique_texts(ordered)[:limit]


def _unique_texts(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _to_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct_from_low(close: float | None, low: float | None) -> float | None:
    if close is None or low is None or low <= 0:
        return None
    return (close / low - 1) * 100


def _parse_any_date(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def _normalise_date_rows(rows: list[dict[str, Any]], date_key: str) -> list[dict[str, Any]]:
    sortable: list[tuple[datetime, dict[str, Any]]] = []
    fallback: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _parse_any_date(row.get(date_key) or row.get(date_key.lower()) or row.get("date"))
        if parsed:
            sortable.append((parsed, dict(row)))
        else:
            fallback.append(dict(row))
    return [row for _date, row in sorted(sortable, key=lambda item: item[0])] + fallback


def _normalise_revenue_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    sortable: list[tuple[datetime, dict[str, Any]]] = []
    fallback: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        if "yoy" not in item:
            for key in ("YoY", "YoY%", "revenue_yoy"):
                if key in item:
                    item["yoy"] = item.get(key)
                    break
        parsed = _parse_any_date(item.get("month") or item.get("Month") or item.get("date"))
        if parsed:
            sortable.append((parsed, item))
        else:
            fallback.append(item)
    return [row for _date, row in sorted(sortable, key=lambda item: item[0])] + fallback


def _normalise_financial_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    sortable: list[tuple[str, dict[str, Any]]] = []
    fallback: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        quarter = str(row.get("Quarter") or row.get("quarter") or "")
        if quarter:
            sortable.append((quarter, dict(row)))
        else:
            fallback.append(dict(row))
    return [row for _quarter, row in sorted(sortable, key=lambda item: item[0])] + fallback


def _consecutive_increase_count(values: list[float | None]) -> int:
    clean = [value for value in values if value is not None]
    if len(clean) < 2:
        return 0
    count = 0
    for index in range(len(clean) - 1, 0, -1):
        if clean[index] > clean[index - 1]:
            count += 1
        else:
            break
    return count


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
    by_code: set[str] = set()

    def add(item: RadarCandidate) -> None:
        if len(selected) >= ai_top:
            return
        if item.code in by_code:
            return
        selected.append(item.code)
        by_code.add(item.code)

    # Keep strategy diversity, but treat ai_top as the total AI-enrichment budget.
    for strategy in ["A", "B", "C", "D"]:
        group = [item for item in candidates if strategy in item.strategy_codes]
        group.sort(key=lambda item: (item.total_score, len(item.strategy_codes), item.code), reverse=True)
        if group:
            add(group[0])
    for item in sorted(candidates, key=lambda item: (item.total_score, len(item.strategy_codes), item.code), reverse=True):
        add(item)
        if len(selected) >= ai_top:
            break
    return selected[:ai_top]


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
            if "技術" in scan_type and "選股" in scan_type and _is_stale_technical_scan_cache(record, target_date):
                continue
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
    parts.extend(ts.dual_ma_signal_label(signal) for signal in item.dual_ma_signals)
    parts.extend(ts.kd_ma_signal_label(signal) for signal in item.kd_ma_signals)
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
        "source_id": source.source_id,
        "title": source.title,
        "url": source.url,
        "source_level": source.source_level,
        "published_date": source.published_date,
        "provider": source.provider,
        "provider_detail": source.provider_detail,
        "fetch_provider": source.fetch_provider,
        "fetch_status": source.fetch_status,
        "failure_reason": source.failure_reason,
        "found_by": source.found_by,
        "snippet": source.snippet,
    }


def _build_ai_comment_payload(
    item: RadarCandidate,
    analysis_date: date,
    *,
    compact_profile: str = "normal",
) -> dict[str, Any]:
    components = item.score_components or {}
    compact_pack = _build_radar_ai_compact_pack(item, analysis_date, compact_profile=compact_profile)
    payload = {
        "code": item.code,
        "name": item.name,
        "industry": item.industry,
        "analysis_date": analysis_date.isoformat(),
        "total_score": item.total_score,
        "score_components": {
            "technical": components.get("technical", 0),
            "revenue": components.get("revenue", 0),
            "financial": components.get("financial", 0),
            "chip": components.get("chip", 0),
            "theme": components.get("theme", 0),
            "sector": _component_sector_score(components),
        },
        "key_reasons": item.key_reasons[:5],
        "risk_flags": item.risk_flags[:4],
        "strategies": sorted(item.strategy_codes),
        "technical_signal_summary": _technical_signal_line(item),
        "chip_summary": _chip_grade_line(item),
        "local_news": [news.get("title") for news in item.news_items[:5] if isinstance(news, dict)],
        "data_coverage": item.data_coverage,
        "ai_compact_pack": compact_pack,
        "full_evidence_pack_location": "local artifacts/cache only; not embedded in AI prompt",
    }
    return _replace_internal_truncation_markers(payload)


def _replace_internal_truncation_markers(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _replace_internal_truncation_markers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_internal_truncation_markers(item) for item in value]
    if value == "<dict truncated>":
        return {
            "資料狀態": "深層欄位未放入 AI 短評 prompt",
            "原因": "避免 Radar 短評反覆展開龐大巢狀資料；完整細項仍保留於本地證據包與快取。",
        }
    if value == "<list truncated>":
        return {
            "資料狀態": "長清單未放入 AI 短評 prompt",
            "原因": "避免 Radar 短評反覆展開龐大清單；完整細項仍保留於本地證據包與快取。",
        }
    return value


def _build_radar_ai_compact_pack(
    item: RadarCandidate,
    analysis_date: date,
    *,
    compact_profile: str = "normal",
) -> dict[str, Any]:
    limits = _radar_compact_limits(compact_profile)
    pack = item.evidence_pack if isinstance(item.evidence_pack, dict) else {}
    structured = pack.get("research_structured_data") if isinstance(pack, dict) else {}
    compact_input = {
        "analysis_date": analysis_date.isoformat(),
        "candidate": {
            "code": item.code,
            "name": item.name,
            "industry": item.industry,
            "price": item.price,
        },
        "radar_scores": {
            "total_score": item.total_score,
            "score_components": item.score_components,
            "strategies": sorted(item.strategy_codes),
        },
        "technical": {
            "summary": _technical_signal_line(item),
            "signals": item.technical_signals,
            "dual_ma_signals": item.dual_ma_signals,
            "kd_ma_signals": item.kd_ma_signals,
        },
        "revenue": {
            "history": item.revenue_history[:6],
            "score": item.score_components.get("revenue", 0),
        },
        "financial": {
            "score": item.score_components.get("financial", 0),
            "detail": item.score_details.get("financial", {}),
        },
        "chip": {
            "summary": _chip_grade_line(item),
            "grades": item.chip_grades,
            "score": item.score_components.get("chip", 0),
        },
        "news": {
            "local_news": item.news_items,
            "web_sources": item.web_sources,
            "ai_sources": item.ai_sources,
            "research_sources": pack.get("research_sources") if isinstance(pack, dict) else [],
        },
        "research_summary": _compact_research_structured_data(structured if isinstance(structured, dict) else {}),
        "data_coverage": item.data_coverage,
    }
    return build_ai_compact_context(
        compact_input,
        max_sources=limits["source_limit"],
        max_list=limits["list_limit"],
        max_string=limits["string_limit"],
    )


def _radar_compact_limits(profile: str) -> dict[str, int]:
    if profile == "minimal":
        return {
            "source_limit": RADAR_AI_MINIMAL_SOURCE_LIMIT,
            "list_limit": RADAR_AI_MINIMAL_LIST_LIMIT,
            "string_limit": RADAR_AI_MINIMAL_STRING_LIMIT,
        }
    if profile == "tight":
        return {
            "source_limit": RADAR_AI_TIGHT_SOURCE_LIMIT,
            "list_limit": RADAR_AI_TIGHT_LIST_LIMIT,
            "string_limit": RADAR_AI_TIGHT_STRING_LIMIT,
        }
    return {
        "source_limit": RADAR_AI_COMPACT_SOURCE_LIMIT,
        "list_limit": RADAR_AI_COMPACT_LIST_LIMIT,
        "string_limit": RADAR_AI_COMPACT_STRING_LIMIT,
    }


def _compact_research_structured_data(structured: dict[str, Any]) -> dict[str, Any]:
    return {
        "radar_research_mode": structured.get("radar_research_mode"),
        "radar_research_data_date": structured.get("radar_research_data_date"),
        "notes": _limit_rows(structured.get("notes"), 4),
        "feature_pack": structured.get("feature_pack"),
        "unified_evidence_pack": structured.get("unified_evidence_pack"),
        "data_gap_summary": structured.get("data_gap_summary"),
        "financial_data": _limit_rows(structured.get("financial_data"), 4),
        "margin_data": _limit_rows(structured.get("margin_data"), 20),
        "institutional_data": _limit_rows(structured.get("institutional_data"), 20),
        "tdcc_data": _limit_rows(structured.get("tdcc_data"), 8),
        "topic_context": structured.get("topic_context"),
        "news_context": structured.get("news_context"),
        "news_events": _limit_rows(structured.get("news_events"), 12),
    }


def _limit_rows(value: Any, limit: int) -> Any:
    if isinstance(value, list):
        return value[:limit]
    return value


def _build_ai_comment_prompt(
    candidates: list[RadarCandidate],
    analysis_date: date,
    *,
    compact_profile: str = "normal",
    low_model_digest: dict[str, Any] | None = None,
) -> str:
    payloads = [_build_ai_comment_payload(item, analysis_date, compact_profile=compact_profile) for item in candidates]
    template = _read_radar_prompt("radar_ai_comment.md")
    candidate_payload_json = json.dumps(_json_safe(payloads), ensure_ascii=False)
    low_digest_json = json.dumps(
        _json_safe(_filter_low_model_digest_for_codes(low_model_digest or {}, [item.code for item in candidates])),
        ensure_ascii=False,
    )
    rendered = template.replace("{analysis_date}", analysis_date.isoformat()).replace(
        "{candidate_payload_json}",
        "候選股資料 JSON 見文末唯一區塊。",
    )
    if "{low_model_digest_json}" in rendered:
        final_prompt = rendered.replace("{low_model_digest_json}", low_digest_json)
    else:
        final_prompt = (
        rendered
        + "\n\nMiniMax M3 批次資料整理底稿：\n"
        + low_digest_json
        )
    final_prompt = final_prompt.replace("候選股資料：\n", "候選股資料（模板段落）：\n")
    final_prompt = final_prompt.rstrip() + "\n\n候選股資料：\n" + candidate_payload_json
    return final_prompt


def _read_radar_prompt(name: str) -> str:
    path = RADAR_PROMPT_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8-sig")
    return (
        "你是台股選股雷達分析員。請根據候選股資料輸出繁體中文 JSON，不要使用 Markdown 或 code fence。\n"
        "不得新增候選股票，不得改變本地分數。請優先使用 ai_compact_pack 與 data_coverage。\n"
        "若資料不足，priority 與 confidence 請降低。\n"
        "候選股資料：\n{candidate_payload_json}"
    )


def _filter_low_model_digest_for_codes(digest: dict[str, Any], codes: list[str]) -> dict[str, Any]:
    if not digest:
        return {}
    code_set = {str(code) for code in codes if code}
    if not code_set:
        return digest
    result = dict(digest)
    for key in ("facts", "events", "risk_evidence", "counter_evidence", "source_map"):
        values = result.get(key)
        if not isinstance(values, list):
            continue
        filtered = []
        for item in values:
            text = json.dumps(item, ensure_ascii=False, default=str) if isinstance(item, dict) else str(item)
            if any(code in text for code in code_set):
                filtered.append(item)
        result[key] = filtered or values[:5]
    return result


def _build_ai_comment_prompt_jobs(
    chunk: list[RadarCandidate],
    analysis_date: date,
    *,
    low_model_digest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normal_prompt = _build_ai_comment_prompt(chunk, analysis_date, compact_profile="normal", low_model_digest=low_model_digest)
    if len(normal_prompt) <= RADAR_AI_PROMPT_MAX_CHARS:
        return [{"codes": [item.code for item in chunk], "profile": "normal", "prompt": normal_prompt}]

    jobs: list[dict[str, Any]] = []
    for item in chunk:
        for profile in ("normal", "tight", "minimal"):
            prompt = _build_ai_comment_prompt([item], analysis_date, compact_profile=profile, low_model_digest=low_model_digest)
            if len(prompt) <= RADAR_AI_PROMPT_MAX_CHARS or profile == "minimal":
                jobs.append({"codes": [item.code], "profile": profile, "prompt": prompt})
                break
    return jobs


def _call_ai_comment_model(model: str, prompt: str) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _call_ai_comment_model_once(model, prompt)
        except Exception as exc:
            last_error = exc
            if not _is_retryable_ai_error(exc) or attempt >= 2:
                raise
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"AI 短評呼叫失敗：{last_error}")


def _is_retryable_ai_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("529", "overloaded", "rate", "timeout", "temporarily", "high load"))


def _call_ai_comment_model_once(model: str, prompt: str) -> str:
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


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    chunk_size = max(1, int(size or 1))
    return [items[index:index + chunk_size] for index in range(0, len(items), chunk_size)]


def _result_to_record(result: RadarResult) -> dict[str, Any]:
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "schema_version": "radar_cache_index_v2",
        "radar_id": f"radar_{result.report_date.strftime('%Y%m%d')}_{datetime.now().strftime('%H%M%S')}",
        "report_date": result.report_date.isoformat(),
        "source": result.request.source,
        "ai_top": result.request.ai_top,
        "model": result.request.model,
        "ai_comment_enabled": result.request.ai_comment_enabled,
        "scoring_version": _resolve_scoring_version(result.request.scoring_version),
        "created_at": created,
        "ai_enriched_codes": result.ai_enriched_codes,
        "diagnostics": _radar_diagnostics_cache_summary(result.diagnostics),
        "candidate_count": len(result.candidates),
        "candidate_snapshot": [_radar_candidate_cache_summary(item) for item in result.candidates],
    }


def _radar_diagnostics_cache_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    payload = dict(diagnostics or {})
    ai_analysis = payload.get("ai_analysis")
    if isinstance(ai_analysis, dict):
        payload["ai_analysis"] = {
            key: ai_analysis.get(key)
            for key in (
                "chunk_count",
                "success_count",
                "failed_count",
                "model",
                "ai_workflow_coverage",
            )
            if key in ai_analysis
        }
    return payload


def _radar_candidate_cache_summary(item: RadarCandidate) -> dict[str, Any]:
    return {
        "code": item.code,
        "name": item.name,
        "symbol": item.symbol,
        "industry": item.industry,
        "price": item.price,
        "source_labels": item.source_labels[:8],
        "strategy_codes": sorted(item.strategy_codes),
        "score_components": dict(item.score_components or {}),
        "total_score": item.total_score,
        "tag": _radar_candidate_tag(item),
        "key_reasons": item.key_reasons[:6],
        "risk_flags": item.risk_flags[:4],
        "ai_comment_status": (item.ai_comment or {}).get("status"),
    }


def _candidate_to_dict(item: RadarCandidate) -> dict[str, Any]:
    payload = {
        "code": item.code,
        "name": item.name,
        "symbol": item.symbol,
        "industry": item.industry,
        "price": item.price,
        "source_labels": item.source_labels,
        "strategy_codes": sorted(item.strategy_codes),
        "technical_signals": item.technical_signals,
        "dual_ma_signals": item.dual_ma_signals,
        "kd_ma_signals": item.kd_ma_signals,
        "chip_grades": item.chip_grades,
        "revenue_history": item.revenue_history,
        "news_items": item.news_items,
        "web_sources": item.web_sources,
        "ai_sources": item.ai_sources,
        "data_coverage": item.data_coverage,
        "evidence_pack": item.evidence_pack,
        "ai_comment": item.ai_comment,
        "score_components": item.score_components,
        "score_details": item.score_details,
        "key_reasons": item.key_reasons,
        "risk_flags": item.risk_flags,
        "score_caps_applied": item.score_caps_applied,
        "radar_feature_snapshot": item.radar_feature_snapshot,
        "total_score": item.total_score,
    }
    payload["candidate_snapshot"] = candidate_snapshot_from_row(
        payload,
        source_command="radar",
        source_pool="radar",
    )
    return payload


def _radar_candidate_snapshot(item: RadarCandidate, result: RadarResult) -> dict[str, Any]:
    return candidate_snapshot_from_row(
        _candidate_to_dict(item),
        source_command="radar",
        source_pool=result.request.source,
        data_date=result.report_date.isoformat(),
    )


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
        scoring_version=_resolve_scoring_version(record.get("scoring_version") or (record.get("diagnostics") or {}).get("scoring_version")),
    )
    candidates = []
    raw_candidates = record.get("candidates")
    if not raw_candidates:
        raw_candidates = _load_radar_candidates_from_artifact(record)
    if not raw_candidates:
        raw_candidates = record.get("candidate_snapshot") or []
    for raw in raw_candidates or []:
        item = RadarCandidate(
            code=str(raw.get("code") or ""),
            name=str(raw.get("name") or ""),
            symbol=str(raw.get("symbol") or ""),
            industry=str(raw.get("industry") or ""),
            price=raw.get("price"),
            source_labels=list(raw.get("source_labels") or []),
            strategy_codes=set(raw.get("strategy_codes") or []),
            technical_signals=list(raw.get("technical_signals") or []),
            dual_ma_signals=list(raw.get("dual_ma_signals") or []),
            kd_ma_signals=list(raw.get("kd_ma_signals") or []),
            chip_grades=dict(raw.get("chip_grades") or {}),
            revenue_history=list(raw.get("revenue_history") or []),
            news_items=list(raw.get("news_items") or []),
            web_sources=list(raw.get("web_sources") or []),
            ai_sources=list(raw.get("ai_sources") or []),
            data_coverage=dict(raw.get("data_coverage") or {}),
            evidence_pack=dict(raw.get("evidence_pack") or {}),
            ai_comment=dict(raw.get("ai_comment") or {}),
            score_components=dict(raw.get("score_components") or {}),
            score_details=dict(raw.get("score_details") or {}),
            key_reasons=list(raw.get("key_reasons") or []),
            risk_flags=list(raw.get("risk_flags") or []),
            score_caps_applied=list(raw.get("score_caps_applied") or []),
            radar_feature_snapshot=dict(raw.get("radar_feature_snapshot") or {}),
            total_score=int(raw.get("total_score") or 0),
        )
        if "financial" not in item.score_components:
            item.score_components["financial"] = 0
        if "sector" not in item.score_components and "market" in item.score_components:
            item.score_components["sector"] = int(item.score_components.get("market") or 0)
        candidates.append(item)
    return RadarResult(
        request=request,
        report_date=_parse_date(str(record.get("report_date"))),
        candidates=candidates,
        ai_enriched_codes=list(record.get("ai_enriched_codes") or []),
        diagnostics=dict(record.get("diagnostics") or {}),
    )


def _load_radar_candidates_from_artifact(record: dict[str, Any]) -> list[dict[str, Any]]:
    artifact_paths = record.get("artifact_paths") if isinstance(record.get("artifact_paths"), dict) else {}
    candidates_path = artifact_paths.get("candidates")
    if not candidates_path:
        candidates_path = _find_radar_candidates_artifact(record)
    if not candidates_path:
        return []
    try:
        data = json.loads(Path(candidates_path).read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _find_radar_candidates_artifact(record: dict[str, Any]) -> str | None:
    radar_id = str(record.get("radar_id") or "").strip()
    report_date = str(record.get("report_date") or "").strip()
    if not radar_id or not report_date:
        return None
    path = RADAR_REPORT_DIR / report_date / radar_id / "radar_candidates.json"
    return str(path) if path.exists() else None


def _load_radar_records(limit: int = 10) -> list[dict[str, Any]]:
    if not RADAR_CACHE_PATH.exists():
        return []
    try:
        if RADAR_CACHE_PATH.stat().st_size > RADAR_CACHE_MAX_BYTES:
            return _rebuild_radar_cache_index_from_artifacts(limit=limit)
    except OSError:
        return []
    try:
        data = json.loads(RADAR_CACHE_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    return [item for item in data if isinstance(item, dict)][:limit]


def _rebuild_radar_cache_index_from_artifacts(limit: int = 30) -> list[dict[str, Any]]:
    candidates_files = sorted(
        RADAR_REPORT_DIR.glob("*/*/radar_candidates.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    records: list[dict[str, Any]] = []
    for candidates_path in candidates_files[: max(1, limit)]:
        radar_dir = candidates_path.parent
        date_dir = radar_dir.parent
        record = {
            "schema_version": "radar_cache_index_v2",
            "radar_id": radar_dir.name,
            "report_date": date_dir.name,
            "source": DEFAULT_SOURCE,
            "ai_top": DEFAULT_AI_TOP,
            "model": None,
            "ai_comment_enabled": True,
            "scoring_version": _default_scoring_version(),
            "created_at": datetime.fromtimestamp(candidates_path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            "ai_enriched_codes": [],
            "diagnostics": {"cache_rebuilt_from_artifacts": True},
            "candidate_count": None,
            "candidate_snapshot": [],
            "artifact_paths": {
                "summary": str(radar_dir / "radar_summary.md"),
                "candidates": str(candidates_path),
                "evidence_pack": str(radar_dir / "evidence_pack.json"),
                "ai_analysis": str(radar_dir / "ai_analysis.json"),
                "sources": str(radar_dir / "sources.json"),
            },
        }
        records.append(record)
    if records:
        try:
            RADAR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            RADAR_CACHE_PATH.write_text(json.dumps(_json_safe(records), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return records[:limit]


def _normalise_source(value: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "combined": "combined",
        "all": "combined",
        "default": "combined",
        "綜合": "combined",
        "跨來源": "combined",
        "跨來源候選池": "combined",
        "技術面選股結果": "technical",
        "技術面": "technical",
        "technical": "technical",
        "精選選股結果": "curated",
        "精選": "curated",
        "curated": "curated",
        "老蕭選股結果": "laoxiao",
        "老蕭": "laoxiao",
        "laoxiao": "laoxiao",
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
        "minimax-m3": "minimax",
        "m3": "minimax",
    }
    model = aliases.get(text, text)
    if model not in {"gemini", "deepseek", "minimax"}:
        raise ValueError("--model 僅支援 gemini、deepseek 或 minimax")
    return model


def _source_label(source: str) -> str:
    return {
        "combined": "跨來源候選池",
        "technical": "技術面選股結果",
        "curated": "精選選股結果",
        "laoxiao": "老蕭選股結果",
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
