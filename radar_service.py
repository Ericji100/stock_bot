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
from research_center.structured_cache import load_latest_research_structured_cache, load_research_structured_cache
from research_center.web_fetch_enrichment import _enrich_sources_with_web_fetch
from research_center.tavily_search_service import TavilyQuotaError, TavilySearchService
from stock_scanner import load_recent_revenue_history, load_stock_universe, scan_tw_market


ROOT_DIR = Path(__file__).resolve().parent
RADAR_CACHE_PATH = ROOT_DIR / ".cache" / "radar_results.json"
RADAR_REPORT_DIR = ROOT_DIR / "reports" / "radar"
RADAR_CACHE_MAX_BYTES = 50 * 1024 * 1024
RADAR_PROMPT_DIR = ROOT_DIR / "prompt" / "radar"
DEFAULT_SOURCE = "combined"
DEFAULT_AI_TOP = 15
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
RADAR_TECHNICAL_CACHE_READY_HOUR = 15
RADAR_TECHNICAL_CACHE_READY_MINUTE = 0
MAIN_SOURCES = {"combined", "technical", "curated", "financial", "chip", "monitor", "portfolio"}
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
_RADAR_CHIP_GRADE_CACHE: dict[str, dict[str, dict[str, str]]] = {}


@dataclass(frozen=True)
class RadarRequest:
    source: str = DEFAULT_SOURCE
    report_date: date | None = None
    ai_top: int = DEFAULT_AI_TOP
    model: str | None = "minimax"
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
    _score_candidates(candidates, target_date)
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
        _score_candidates(candidates, target_date)
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
            lines.append(f"{item.code} {item.name}｜{item.total_score}分｜{tag}｜{reasons}")
        lines.append("")

    for rank, item in enumerate(result.candidates[:limit], 1):
        strategy = "/".join(sorted(item.strategy_codes)) if item.strategy_codes else "-"
        ai_badge = _ai_badge(item)
        labels = "、".join(_display_source_labels(item)[:3])
        components = item.score_components
        evidence = _candidate_evidence_line(item)
        technical_line = _technical_signal_line(item)
        chip_line = _chip_grade_line(item)
        ai_lines = _ai_comment_lines(item)
        tag = _radar_candidate_tag(item)
        tag_text = f"｜{tag}" if tag else ""
        lines.extend(
            [
                f"{rank}. {item.code} {item.name}｜{item.total_score}分｜策略 {strategy}{ai_badge}{tag_text}",
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
        lines.extend(
            [
                f"{rank}. {item.code} {item.name}｜{item.total_score}分{tag_text}",
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


def _model_label(model: str | None) -> str:
    return {"gemini": "Gemini", "deepseek": "DeepSeek", "minimax": "MiniMax"}.get(str(model or ""), str(model or ""))


def _ai_badge(item: RadarCandidate) -> str:
    comment = item.ai_comment or {}
    if comment.get("status") == "ok":
        return f"｜AI {comment.get('priority') or '中'}"
    if comment.get("status") in {"failed", "missing"}:
        return "｜AI 未完成"
    return ""


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
    artifacts["summary"].write_text(format_radar_report(result, limit=max(50, len(result.candidates))), encoding="utf-8")
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
    if source == "financial":
        return _financial_candidates(scan_settings)
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
        ("financial", lambda: _financial_candidates(scan_settings)),
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


def _attach_revenue_scores(candidates: list[RadarCandidate]) -> None:
    universe = load_stock_universe(False)
    code_map = {entry.code: entry for entry in universe}
    selected = [code_map[item.code] for item in candidates if item.code in code_map]
    revenue = load_recent_revenue_history(selected)
    for item in candidates:
        points = revenue.get(item.code) or []
        latest = points[0] if points else None
        yoy = getattr(latest, "yoy", None) if latest else None
        item.revenue_history = [
            {"month": point.month, "revenue": point.revenue, "yoy": point.yoy}
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
        "technical_signal_count": len(item.technical_signals),
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
        "technical": "ok" if item.technical_signals else "missing",
        "revenue": "ok" if item.revenue_history else "missing",
        "chip": "ok" if item.chip_grades else "missing",
        "local_news": "ok" if item.news_items else "missing",
        "external_sources": "ok" if (item.ai_sources or item.web_sources) else "missing",
        "source_sufficiency": "ok" if external_source_count >= RADAR_MIN_EXTERNAL_SOURCES else "insufficient",
        "research_structured_data": "ok" if research_structured_data else ("error" if error else "not_requested"),
        "financial": _coverage_status(structured.get("financial_data")),
        "margin": _coverage_status(structured.get("margin_data")),
        "institutional": _coverage_status(structured.get("institutional_data")),
        "tdcc": _coverage_status(structured.get("tdcc_data")),
        "topic_context": _coverage_status(structured.get("topic_context")),
        "feature_pack": _coverage_status(structured.get("feature_pack")),
        "unified_evidence_pack": _coverage_status(structured.get("unified_evidence_pack")),
    }
    if structured.get("radar_research_mode") == "light_generated":
        for key in ("financial", "margin", "institutional", "tdcc", "unified_evidence_pack"):
            if checks.get(key) in {"missing", "empty"}:
                checks[key] = "limited_by_light_research"
    missing = [key for key, value in checks.items() if value in {"missing", "empty", "error", "insufficient"}]
    return {
        "schema_version": "radar_data_coverage_v1",
        "checks": checks,
        "external_source_count": external_source_count,
        "min_external_sources": RADAR_MIN_EXTERNAL_SOURCES,
        "missing_or_weak_fields": missing,
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


def _score_candidates(candidates: list[RadarCandidate], analysis_date: date | None = None) -> None:
    industry_counts: dict[str, int] = {}
    for item in candidates:
        if item.industry:
            industry_counts[item.industry] = industry_counts.get(item.industry, 0) + 1
    for item in candidates:
        snapshot = _build_radar_feature_snapshot(item, candidates, industry_counts, analysis_date)
        details = {
            "technical": _score_technical_detail(item, snapshot),
            "revenue": _score_revenue_detail(item, snapshot),
            "financial": _score_financial_detail(item, snapshot),
            "chip": _score_chip_detail(item, snapshot),
            "theme": _score_theme_news_detail(item, snapshot),
            "sector": _score_sector_detail(item, snapshot),
        }
        components = {key: int(detail.get("score") or 0) for key, detail in details.items()}
        total = min(100, sum(components.values()))
        total, caps = _apply_radar_score_caps(total, components, details)
        item.radar_feature_snapshot = snapshot
        item.score_details = details
        item.score_components = components
        item.score_caps_applied = caps
        item.key_reasons = _top_unique_reasons(details, limit=5)
        item.risk_flags = _top_unique_risks(details, caps, limit=4)
        item.total_score = int(total)


def _build_radar_feature_snapshot(
    item: RadarCandidate,
    candidates: list[RadarCandidate],
    industry_counts: dict[str, int],
    analysis_date: date | None,
) -> dict[str, Any]:
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
    return {
        "analysis_date": analysis_date.isoformat() if analysis_date else None,
        "structured_cache_date": structured_date.isoformat() if structured_date else None,
        "technical": technical,
        "revenue": {"history": revenue_rows},
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
        "sector": {
            "industry": item.industry,
            "industry_candidate_count": industry_counts.get(item.industry, 0),
            "same_industry_codes": sector_peers[:20],
        },
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


def _build_technical_snapshot(item: RadarCandidate, analysis_date: date | None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "strategies": sorted(item.strategy_codes),
        "signals": item.technical_signals[:12],
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
    vol20 = _to_float(frame["volume"].rolling(20).mean().iloc[-1]) if "volume" in frame else None
    vol60 = _to_float(frame["volume"].rolling(60).mean().iloc[-1]) if "volume" in frame else None
    high20 = _to_float(frame["high"].rolling(20).max().iloc[-2]) if len(frame) >= 2 and "high" in frame else None
    high60 = _to_float(frame["high"].rolling(60).max().iloc[-2]) if len(frame) >= 2 and "high" in frame else None
    low60 = _to_float(frame["low"].rolling(60).min().iloc[-1]) if "low" in frame else None
    low120 = _to_float(frame["low"].rolling(120).min().iloc[-1]) if "low" in frame else None
    volume_ratio = volume / vol20 if volume is not None and vol20 and vol20 > 0 else None
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
            "close": close,
            "previous_close": prev_close,
            "volume": volume,
            "ma": ma,
            "previous_ma": prev_ma,
            "ma_slopes": slopes,
            "volume_avg20": vol20,
            "volume_avg60": vol60,
            "volume_ratio": volume_ratio,
            "above_ma": {key: close is not None and value is not None and close >= value for key, value in ma.items()},
            "reclaim_ma": {
                key: close is not None and value is not None and prev_close is not None and prev_ma.get(key) is not None and close >= value and prev_close < prev_ma[key]
                for key, value in ma.items()
            },
            "distance_from_60d_low_pct": _pct_from_low(close, low60),
            "distance_from_120d_low_pct": _pct_from_low(close, low120),
            "breakout_20d": close is not None and high20 is not None and close > high20,
            "breakout_60d": close is not None and high60 is not None and close > high60,
            "price_up_volume_up": close is not None and prev_close is not None and volume is not None and close > prev_close and (volume_ratio or 0) > 1,
            "recent_break_low_recover": recent_break_low,
            "long_lower_shadow": bool(lower_shadow is not None and lower_shadow >= 0.35),
            "ma20_deviation_pct": ((close / ma["ma20"] - 1) * 100) if close and ma.get("ma20") else None,
        }
    )
    return snapshot


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
        for column in ("open", "high", "low", "close", "volume"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.tail(160)
    return None


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

    volume = 0.0
    volume_ratio = _to_float(tech.get("volume_ratio")) or _to_float((tech.get("price_metrics") or {}).get("volume_ratio"))
    if volume_ratio is not None:
        if volume_ratio > 3:
            volume += 3; reasons.append("量比大於 3")
        elif volume_ratio > 2:
            volume += 2; reasons.append("量比大於 2")
        elif volume_ratio > 1.5:
            volume += 1; reasons.append("量比大於 1.5")
        if 1.2 <= volume_ratio <= 3:
            volume += 1; reasons.append("量增但未爆量過熱")
    if tech.get("volume") and tech.get("volume_avg20") and _to_float(tech.get("volume")) > _to_float(tech.get("volume_avg20")):
        volume += 1; reasons.append("成交量突破 20 日均量")
    if tech.get("volume") and tech.get("volume_avg60") and _to_float(tech.get("volume")) > _to_float(tech.get("volume_avg60")):
        volume += 1; reasons.append("成交量突破 60 日均量")
    if tech.get("price_up_volume_up"):
        volume += 1; reasons.append("價漲量增")
    volume = min(5, volume)

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

    score = trend + reversal + volume + shakeout + breakout + cross
    deviation = _to_float(tech.get("ma20_deviation_pct"))
    if deviation is not None and deviation > 25:
        score -= 2; risks.append("乖離 MA20 超過 25%")
    if volume_ratio is not None and volume_ratio > 5 and tech.get("price_up_volume_up") is False:
        score -= 2; risks.append("爆量但價格未同步轉強")
    if any("高風險" in str(signal.get("notes") or "") for signal in item.technical_signals):
        score -= 2; risks.append("既有技術訊號標記高風險")
    details.update({"trend": trend, "reversal": reversal, "volume": volume, "shakeout": shakeout, "breakout": breakout, "strategy_cross": cross})
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
    score = 0.0
    all_titles = " ".join(str(row.get("title") or row.get("snippet") or "") for row in [*local_news, *web_sources, *ai_sources])
    hot_terms = ("AI", "BBU", "CPO", "CoWoS", "高速傳輸", "散熱", "電動車", "機器人", "伺服器", "重電", "儲能", "半導體")
    if any(term in all_titles for term in hot_terms):
        score += 3; reasons.append("新聞/來源出現熱門題材關鍵字")
    if local_news:
        score += min(3, len(local_news) * 1.2); reasons.append("本地新聞資料有題材線索")
    external_count = len(web_sources) + len(ai_sources)
    if external_count:
        score += min(3, external_count * 0.8); reasons.append("外部來源可交叉驗證")
    matched_topics = topic_context.get("matched_topics") if isinstance(topic_context, dict) else []
    if matched_topics:
        score += 2; reasons.append("題材庫有對應主題")
    company_rel = topic_context.get("company_topic_relations") if isinstance(topic_context, dict) else {}
    if isinstance(company_rel, dict) and (_to_float(company_rel.get("direct_matches")) or 0) > 0:
        score += 2; reasons.append("題材與公司關聯有直接匹配")
    revenue_score = (snapshot.get("revenue") or {}).get("score")
    if revenue_score is None:
        revenue_rows = _normalise_revenue_rows((snapshot.get("revenue") or {}).get("history"))
        revenue_score = 1 if revenue_rows and (_to_float(revenue_rows[-1].get("yoy") or revenue_rows[-1].get("YoY") or revenue_rows[-1].get("YoY%")) or 0) > 0 else 0
    if revenue_score:
        score += 2; reasons.append("題材可與營收改善交叉觀察")
    revenue_rows = _normalise_revenue_rows((snapshot.get("revenue") or {}).get("history"))
    latest_yoy = _to_float(revenue_rows[-1].get("yoy") or revenue_rows[-1].get("YoY") or revenue_rows[-1].get("YoY%")) if revenue_rows else None
    if latest_yoy is not None and latest_yoy >= 15 and not matched_topics:
        score += 1.5; reasons.append("營收改善但題材庫尚未明確映射，列為重估觀察")
        risks.append("題材重估仍需補官方或產業鏈證據")
    technical = snapshot.get("technical") or {}
    chip_grades = (snapshot.get("chip") or {}).get("grades") or {}
    if (
        latest_yoy is not None
        and latest_yoy >= 15
        and (technical.get("status") == "ok")
        and ((technical.get("volume_ratio") or 0) >= 1.2 or technical.get("price_up_volume_up"))
        and any(str(grade).upper() in {"S", "A", "B"} for grade in chip_grades.values())
    ):
        score += 3
        reasons.append("營收、價量與籌碼同步出現早期重估線索")
    if any("注意" in str(row.get("title") or "") or "處置" in str(row.get("title") or "") for row in local_news):
        risks.append("新聞含注意股/處置訊息")
    if score > 0 and not local_news and external_count == 0 and not matched_topics:
        risks.append("題材缺少可驗證來源")
    return _score_detail(score, 15, reasons, risks, {"local_news_count": len(local_news), "external_source_count": external_count})


def _score_sector_detail(item: RadarCandidate, snapshot: dict[str, Any]) -> dict[str, Any]:
    sector = snapshot.get("sector") or {}
    count = int(sector.get("industry_candidate_count") or 0)
    reasons: list[str] = []
    score = 0.0
    if count >= 2:
        score += 1; reasons.append("同產業多檔同步進入候選")
    if count >= 4:
        score += 1; reasons.append("族群候選擴散")
    if item.industry:
        score += 0.5; reasons.append("具明確產業分類")
    if (snapshot.get("theme_news") or {}).get("local_news") or (snapshot.get("theme_news") or {}).get("web_sources"):
        score += 1; reasons.append("族群/題材新聞密度增加")
    if (snapshot.get("chip") or {}).get("grades"):
        score += 0.5; reasons.append("族群候選具籌碼評級輔助")
    if (snapshot.get("revenue") or {}).get("history"):
        score += 0.5; reasons.append("族群候選具營收資料支撐")
    return _score_detail(score, 5, reasons, [], {"industry_candidate_count": count})


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
