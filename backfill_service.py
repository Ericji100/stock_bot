"""Backfill service: builds a candidate pool from multiple sources and warms up local structured data caches.

The /backfill command does NOT pre-run AI search or model analysis. It only:
1. Builds a candidate pool (hard-filter + portfolio + monitor + recent scans + recent research)
2. Warms up local structured data caches for candidate stocks
3. Runs chip/warmup for the candidate universe

AI search (Tavily/Gemini) and model analysis (Gemini/DeepSeek) remain real-time during /research.

Three-tier backfill strategy:
  Tier 1 (market-wide, lightweight): revenue, price/volume, technical daily for all ~1700 stocks
  Tier 2 (candidate pool, medium): institutional, margin, gross margin, chip, curated scan for candidates
  Tier 3 (core research, full): collect_research_data() only for core pool (≤80 stocks by default)
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
from pathlib import Path
from typing import Any, Callable

DEFAULT_CORE_RESEARCH_LIMIT = 80
DEFAULT_STRUCTURED_TIMEOUT_SECONDS = 30
DEFAULT_STRUCTURED_TOTAL_BUDGET_SECONDS = 300
DEFAULT_BACKFILL_THROTTLE_BATCH_SIZE = 20
DEFAULT_BACKFILL_THROTTLE_SLEEP_SECONDS = 0.02
SCAN_HEALTH_THRESHOLDS = {
    "technical": 0.9,
    "revenue": 0.8,
    "financial": 0.5,
    "chip": 0.8,
}

from chip_strategies import get_tw_today, warmup_chip_data_cache, TARGET_DAILY_TRADING_DAYS
import pandas as pd
from portfolio_manager import load_portfolio
from research_center.data_services import collect_research_data

# Marker root for backfill complete markers
BACKFILL_MARKER_ROOT = Path(".cache/backfill")
from research_center.models import CommandRequest
from research_center.artifact_registry import build_artifact_record, register_artifact
from research_center.backfill_dag_service import (
    build_backfill_dag,
    create_backfill_dag_event,
    summarize_backfill_dag,
    summarize_backfill_events,
)
from research_center.backfill_scheduler_service import build_backfill_priority_plan
from research_center.recent_scans import load_recent_scan_results
from research_center.structured_cache import load_research_structured_cache
from curated_scan_service import CURATED_SCAN_TYPE, build_curated_scan_result, find_cached_curated_scan
from stock_scanner import load_gross_margin_series, load_recent_revenue_history, load_price_metrics, load_stock_universe
from technical_scanner import fetch_daily_history
from backfill_gap_service import build_backfill_gap_report, write_gap_report


@dataclass
class BackfillCandidate:
    code: str
    name: str = ""
    symbol: str = ""
    market: str = ""
    sources: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class BackfillThrottle:
    batch_size: int = DEFAULT_BACKFILL_THROTTLE_BATCH_SIZE
    sleep_seconds: float = DEFAULT_BACKFILL_THROTTLE_SLEEP_SECONDS


def _maybe_throttle(index: int, throttle: BackfillThrottle | None, stop_event: threading.Event | None = None) -> bool:
    """Yield briefly during long backfill loops.  Return False when stopped."""
    if stop_event and stop_event.is_set():
        return False
    if not throttle or throttle.batch_size <= 0 or throttle.sleep_seconds <= 0:
        return True
    if index > 0 and index % throttle.batch_size == 0:
        time.sleep(throttle.sleep_seconds)
    if stop_event and stop_event.is_set():
        return False
    return True


def _health_coverage(health: dict[str, Any], key: str) -> float:
    section = health.get(key) if isinstance(health, dict) else {}
    if not isinstance(section, dict):
        return 0.0
    try:
        return float(section.get("coverage_pct") or 0.0)
    except Exception:
        return 0.0


def _is_technical_history_ready(symbol: str, report_date: date, min_rows: int = 120) -> bool:
    """Return True when local technical cache already covers report_date."""
    try:
        from technical_scanner import _load_cached_history
        cached = _load_cached_history(symbol, require_fresh=False)
        if cached.empty or len(cached) < min_rows:
            return False
        return bool(cached["date"].dt.date.max() >= report_date)
    except Exception:
        return False


def evaluate_backfill_readiness(
    *,
    health: dict[str, Any] | None,
    curated_scan_count: int,
    priority_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate scan readiness from the gap report health sections."""
    health = health if isinstance(health, dict) else {}
    priority_health = priority_health if isinstance(priority_health, dict) else {}
    health_ready = {
        key: _health_coverage(health, key) >= threshold
        for key, threshold in SCAN_HEALTH_THRESHOLDS.items()
    }
    priority_chip = priority_health.get("chip") if isinstance(priority_health.get("chip"), dict) else {}
    priority_tdcc = priority_health.get("tdcc") if isinstance(priority_health.get("tdcc"), dict) else {}
    chip_threshold = SCAN_HEALTH_THRESHOLDS.get("chip", 0.8)
    chip_readiness_basis = "all_candidates"
    if not health_ready.get("chip", False):
        priority_chip_coverage = float(priority_chip.get("coverage_pct") or 0.0)
        priority_chip_count = int(priority_chip.get("candidate_count") or 0)
        if priority_chip_count > 0 and priority_chip_coverage >= chip_threshold:
            health_ready["chip"] = True
            chip_readiness_basis = "priority_pool"
    curated_ready = curated_scan_count > 0
    scan_data_ready = all(health_ready.values())
    return {
        "health_ready": health_ready,
        "scan_data_ready": scan_data_ready,
        "curated_scan_cache_ready": curated_ready,
        "curated_scan_ready": curated_ready,
        "backfill_ready_for_scan": bool(scan_data_ready and curated_ready),
        "priority_chip_coverage_pct": float(priority_chip.get("coverage_pct") or 0.0),
        "priority_tdcc_coverage_pct": float(priority_tdcc.get("coverage_pct") or 0.0),
        "chip_readiness_basis": chip_readiness_basis,
    }


@dataclass
class BackfillResult:
    report_date: date
    # Tier counts
    universe_count: int = 0
    candidate_count: int = 0
    core_research_count: int = 0
    # Screening / lightweight counts
    revenue_count: int = 0
    price_metric_count: int = 0
    technical_count: int = 0
    # Candidate pool / medium counts
    research_structured_count: int = 0
    gross_margin_count: int = 0
    curated_scan_count: int = 0
    chip_candidate_count: int = 0
    screening_revenue_count: int = 0
    screening_price_metric_count: int = 0
    screening_technical_count: int = 0
    screening_warning_count: int = 0
    # Metadata
    latest_trading_date: date | None = None
    curated_scan_codes: list[str] = field(default_factory=list)
    candidate_source_counts: dict[str, int] = field(default_factory=dict)
    used_cache: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    # Counts and warnings
    research_structured_timeout_count: int = 0
    warnings: list[str] = field(default_factory=list)
    # Chip/cache health fields
    chip_coverage_days: int = 0
    chip_target_days: int = TARGET_DAILY_TRADING_DAYS
    chip_candidate_coverage_pct: float = 0.0
    chip_coverage_ok: bool = False
    priority_pool_count: int = 0
    priority_chip_coverage_pct: float = 0.0
    priority_tdcc_coverage_pct: float = 0.0
    scan_data_ready: bool = False
    curated_scan_cache_ready: bool = False
    curated_scan_ready: bool = False
    backfill_ready_for_scan: bool = False
    backfill_ready_for_research: str = "partial"
    gap_report: dict[str, Any] = field(default_factory=dict)
    gap_report_path: str = ""
    backfill_dag_events: list[dict[str, Any]] = field(default_factory=list)


def _record_backfill_dag_event(
    events: list[dict[str, Any]],
    node_id: str,
    status: str,
    *,
    message: str | None = None,
    failure_reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    events.append(
        create_backfill_dag_event(
            node_id,
            status,
            message=message,
            failure_reason=failure_reason,
            metadata=metadata,
        )
    )


def _add_candidate(
    pool: dict[str, BackfillCandidate],
    universe_by_code: dict[str, Any],
    code: str,
    source: str,
    name: str = "",
    symbol: str = "",
    market: str = "",
) -> None:
    """Add a stock code to the candidate pool with source tracking."""
    code = str(code).strip()
    if not code or not code.isdigit() or len(code) != 4:
        return
    if code not in pool:
        entry = universe_by_code.get(code)
        pool[code] = BackfillCandidate(
            code=code,
            name=name or (entry.name if entry else ""),
            symbol=symbol or (entry.symbol if entry else ""),
            market=market or (entry.market if entry else ""),
            sources=set(),
        )
    pool[code].sources.add(source)


def _load_recent_research_codes(limit_files: int = 80) -> set[str]:
    """Extract stock codes from recent research and value_scan report JSONs."""
    report_root = Path("reports")
    if not report_root.exists():
        return set()

    codes: set[str] = set()
    try:
        paths = sorted(report_root.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return set()

    for path in paths[:limit_files]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        # Collect stock IDs from various locations in the report structure
        candidates = [
            data.get("stock_id"),
            (data.get("metadata") or {}).get("stock_id"),
            ((data.get("metadata") or {}).get("stock") or {}).get("code"),
            ((data.get("structured_data") or {}).get("stock") or {}).get("code"),
        ]

        # value_scan candidates
        for row in (data.get("structured_data") or {}).get("candidates") or []:
            candidates.append(row.get("code"))

        for candidate_code in candidates:
            candidate_code = str(candidate_code or "").strip()
            if candidate_code.isdigit() and len(candidate_code) == 4:
                codes.add(candidate_code)

    return codes


def build_backfill_candidate_pool(
    report_date: date,
    force_refresh: bool = False,
    progress: Callable[[str], None] | None = None,
    preloaded_universe: list[Any] | None = None,
) -> tuple[dict[str, BackfillCandidate], list[Any], list[str]]:
    """Build the backfill candidate pool from multiple sources.

    Returns:
        (candidates dict, universe list, warnings list)
    """
    warnings: list[str] = []

    def emit(message: str) -> None:
        if progress:
            progress(message)

    # 1. Load stock universe
    emit("候選池：載入股票宇宙")
    universe = preloaded_universe or load_stock_universe()
    universe_by_code = {entry.code: entry for entry in universe}

    # Load config for scan_settings
    config: dict[str, Any] = {}
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as exc:
        warnings.append(f"config.json 載入失敗，硬篩選使用預設值: {exc}")

    scan_settings = config.get("scan_settings") or {}
    min_price = float(scan_settings.get("min_price", 0))
    max_price = float(scan_settings.get("max_price", 10**9))
    min_avg_volume_20d = float(scan_settings.get("min_avg_volume_20d", 0))
    min_monthly_revenue = float(scan_settings.get("min_monthly_revenue", 0))

    pool: dict[str, BackfillCandidate] = {}

    # 2. Hard filter: revenue-positive stocks with price/volume constraints
    emit("候選池：篩選營收正面 + 價量合理股")
    revenue_history: dict[str, Any] = {}
    try:
        revenue_history = load_recent_revenue_history(universe)
        for entry in universe:
            points = revenue_history.get(entry.code) or []
            if not points:
                continue
            latest = points[0]
            # Revenue YoY > 0
            if latest.yoy is not None and latest.yoy > 0:
                _add_candidate(pool, universe_by_code, entry.code, "hard_filter_revenue", entry.name, entry.symbol, entry.market)
            # Revenue YoY improving (latest YoY > previous YoY)
            if len(points) >= 2:
                previous = points[1]
                if latest.yoy is not None and previous.yoy is not None and latest.yoy > previous.yoy:
                    _add_candidate(
                        pool,
                        universe_by_code,
                        entry.code,
                        "hard_filter_revenue_improving",
                        entry.name,
                        entry.symbol,
                        entry.market,
                    )
    except Exception as exc:
        warnings.append(f"營收歷史載入失敗: {exc}")

    # 2b. Hard filter: price/volume from config scan_settings
    emit("候選池：篩選價量合理股")
    try:
        price_metrics = load_price_metrics(universe, force_refresh=False)
        for entry in universe:
            metric = price_metrics.get(entry.symbol)
            if not metric:
                continue
            price = metric.get("price")
            avg_volume = metric.get("avg_volume_20d")
            if (
                price is not None
                and avg_volume is not None
                and min_price <= price <= max_price
                and avg_volume >= min_avg_volume_20d
            ):
                _add_candidate(pool, universe_by_code, entry.code, "hard_filter_price_volume", entry.name, entry.symbol, entry.market)
    except Exception as exc:
        warnings.append(f"價量篩選失敗: {exc}")

    # 2c. Hard filter: revenue size (min monthly revenue from scan_settings)
    emit("候選池：篩選營收規模股")
    try:
        for entry in universe:
            if entry.code in revenue_history:
                points = revenue_history[entry.code]
            else:
                continue
            if not points:
                continue
            latest_revenue = points[0].revenue
            if latest_revenue is not None and latest_revenue >= min_monthly_revenue:
                _add_candidate(pool, universe_by_code, entry.code, "hard_filter_revenue_size", entry.name, entry.symbol, entry.market)
    except Exception as exc:
        warnings.append(f"營收規模篩選失敗: {exc}")

    # 3. Portfolio positions
    emit("候選池：加入個人庫存")
    try:
        portfolio = load_portfolio()
        for code, name in portfolio.items():
            _add_candidate(pool, universe_by_code, code, "portfolio", name=name)
    except Exception as exc:
        warnings.append(f"個人庫存載入失敗: {exc}")

    # 4. Monitor list from config
    emit("候選池：加入監控清單")
    try:
        monitor_stocks = config.get("monitor_stocks") or []
        for entry in monitor_stocks:
            symbol = str(entry.get("symbol", "") or "").strip()
            name = str(entry.get("name", "") or "").strip()
            code = symbol.split(".")[0] if symbol else ""
            if code.isdigit() and len(code) == 4:
                _add_candidate(pool, universe_by_code, code, "monitor_list", name=name, symbol=symbol)
    except Exception as exc:
        warnings.append(f"監控清單載入失敗: {exc}")

    # 5. Recent scan results
    emit("候選池：加入最近掃描結果")
    try:
        recent_scans = load_recent_scan_results(limit=10)
        for scan in recent_scans:
            for code in (scan.get("selected_codes") or []):
                _add_candidate(pool, universe_by_code, str(code), "recent_scan")
    except Exception as exc:
        warnings.append(f"最近掃描結果載入失敗: {exc}")

    # 6. Recent research / value_scan stock codes from report JSONs
    emit("候選池：加入最近報告股票")
    try:
        research_codes = _load_recent_research_codes(limit_files=80)
        for code in research_codes:
            _add_candidate(pool, universe_by_code, code, "recent_research")
    except Exception as exc:
        warnings.append(f"最近報告解析失敗: {exc}")

    # Count sources
    candidate_source_counts: dict[str, int] = {}
    for candidate in pool.values():
        for source in candidate.sources:
            candidate_source_counts[source] = candidate_source_counts.get(source, 0) + 1

    emit(f"候選池建立完成：{len(pool)} 檔，來源分布 {candidate_source_counts}")
    return pool, universe, warnings


def _sub_progress(stock_label: str, source: str, elapsed: float) -> str:
    """Build per-source progress message."""
    return f"{stock_label}｜{source}完成，用時 {elapsed:.1f} 秒"


def _call_collect_research_data(
    code: str,
    name: str,
    report_date: date,
    force_refresh: bool,
    progress: Callable[[str], None] | None,
) -> tuple[bool, str | None, str | None]:
    """Call collect_research_data with sub-progress.

    Returns (success, error_str, warning_str).
    """
    from research_center.models import CommandRequest

    stock_label = f"{code} {name}".strip() if name else code
    start = time.perf_counter()

    def emit_sub(msg: str) -> None:
        if progress:
            sub_elapsed = time.perf_counter() - start
            progress(f"投研結構化資料：{stock_label}｜{msg}，用時 {sub_elapsed:.1f} 秒")

    try:
        request = CommandRequest(
            command="research",
            raw_text=f"/research {code}",
            target=code,
            report_date=report_date,
        )

        def sub_progress(msg: str) -> None:
            emit_sub(msg)

        collect_research_data(request, progress=sub_progress)
        emit_sub("完成")
        return True, None, None

    except Exception as exc:
        emit_sub(f"失敗")
        return False, str(exc), None


def warmup_research_structured_data(
    core_pool: dict[str, BackfillCandidate],
    report_date: date,
    force_refresh: bool = False,
    progress: Callable[[str], None] | None = None,
    timeout_sec: float = DEFAULT_STRUCTURED_TIMEOUT_SECONDS,
    stop_event: threading.Event | None = None,
    throttle: BackfillThrottle | None = None,
) -> tuple[int, list[str], list[str], int]:
    """Warm up structured data caches for the core research pool.

    Only stocks in core_pool receive full collect_research_data().
    Other stocks are handled at the candidate or market level.

    Returns:
        (count of successfully fetched stocks, list of cache-hit stocks, list of warning messages, timeout count)
    """
    if not core_pool:
        if progress:
            progress("[完整回補] 核心股完整投研回補略過：核心池為空")
        return 0, [], [], 0

    count = 0
    used_cache: list[str] = []
    warnings: list[str] = []
    total = len(core_pool)
    timeout_count = 0

    if progress:
        progress(f"[完整回補] 核心股完整投研回補開始：{total} 檔")

    for index, candidate in enumerate(core_pool.values(), start=1):
        name = getattr(candidate, "name", "") or ""
        label = f"{candidate.code} {name}".strip() if name else candidate.code

        if progress:
            progress(f"投研結構化資料 {index}/{total} 開始：{label}")

        # Check cache first unless force_refresh
        if not force_refresh:
            cached = load_research_structured_cache(candidate.code, report_date)
            if cached is not None:
                used_cache.append(candidate.code)
                count += 1
                if progress:
                    progress(f"投研結構化資料 {index}/{total} 快取命中：{label}")
                continue

        # Run with timeout
        result_holder: list = [None]  # type: ignore

        def target() -> None:
            result_holder[0] = _call_collect_research_data(
                candidate.code,
                name,
                report_date,
                force_refresh,
                progress,
            )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=timeout_sec)

        if thread.is_alive():
            # Timeout - thread still running
            timeout_count += 1
            error_msg = f"超過 {timeout_sec} 秒"
            warnings.append(f"投研結構化資料逾時 {candidate.code}: {error_msg}")
            if progress:
                progress(f"投研結構化資料 {index}/{total} 逾時跳過：{label}，超過 {timeout_sec} 秒")
        elif result_holder[0] is not None:
            success, err_str, warn_str = result_holder[0]  # type: ignore
            if success:
                count += 1
            else:
                warnings.append(f"投研結構化資料回補失敗 {candidate.code}: {err_str}")
                if progress:
                    progress(f"投研結構化資料 {index}/{total} 失敗：{label}，原因：{err_str}")
            if warn_str:
                warnings.append(warn_str)

        if not _maybe_throttle(index, throttle, stop_event):
            warnings.append("回補已收到停止指令。")
            return count, used_cache, warnings, timeout_count

    if progress:
        progress(f"投研結構化資料完成：{count} 檔成功，{timeout_count} 檔逾時")

    return count, used_cache, warnings, timeout_count


def warmup_research_structured_data(
    core_pool: dict[str, BackfillCandidate],
    report_date: date,
    force_refresh: bool = False,
    progress: Callable[[str], None] | None = None,
    timeout_sec: float = DEFAULT_STRUCTURED_TIMEOUT_SECONDS,
    stop_event: threading.Event | None = None,
    throttle: BackfillThrottle | None = None,
    max_total_sec: float | None = DEFAULT_STRUCTURED_TOTAL_BUDGET_SECONDS,
) -> tuple[int, list[str], list[str], int]:
    """Warm structured research caches with per-stock and total time budgets."""
    if not core_pool:
        if progress:
            progress("[回補] 核心池為空，略過投研結構化快取")
        return 0, [], [], 0

    count = 0
    used_cache: list[str] = []
    warnings: list[str] = []
    total = len(core_pool)
    timeout_count = 0
    started_at = time.perf_counter()

    if progress:
        budget_text = "不限" if max_total_sec is None or max_total_sec <= 0 else f"{max_total_sec:.0f} 秒"
        progress(f"[回補] 核心股完整投研回補開始：核心股票 {total} 檔，單檔逾時 {timeout_sec:.0f} 秒，總預算 {budget_text}")

    for index, candidate in enumerate(core_pool.values(), start=1):
        elapsed = time.perf_counter() - started_at
        if max_total_sec is not None and max_total_sec > 0 and elapsed >= max_total_sec:
            warning = f"投研結構化快取達總時間預算 {max_total_sec:.0f} 秒，已處理 {index - 1}/{total} 檔，剩餘留待下次回補"
            warnings.append(warning)
            if progress:
                progress(f"[回補] {warning}")
            break

        name = getattr(candidate, "name", "") or ""
        label = f"{candidate.code} {name}".strip() if name else candidate.code

        if progress:
            progress(f"投研結構化資料 {index}/{total} 開始：{label}")

        if not force_refresh:
            cached = load_research_structured_cache(candidate.code, report_date)
            if cached is not None:
                used_cache.append(candidate.code)
                count += 1
                if progress:
                    progress(f"投研結構化資料 {index}/{total} 快取命中：{label}")
                continue

        result_holder: list[Any] = [None]

        def target() -> None:
            result_holder[0] = _call_collect_research_data(
                candidate.code,
                name,
                report_date,
                force_refresh,
                progress,
            )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=timeout_sec)

        if thread.is_alive():
            timeout_count += 1
            error_msg = f"單檔逾時 {timeout_sec:.0f} 秒"
            warnings.append(f"投研結構化資料逾時 {candidate.code}: {error_msg}")
            if progress:
                progress(f"投研結構化資料 {index}/{total} 逾時跳過：{label}，已等待 {timeout_sec:.0f} 秒")
        elif result_holder[0] is not None:
            success, err_str, warn_str = result_holder[0]
            if success:
                count += 1
                if progress:
                    progress(f"投研結構化資料 {index}/{total} 完成：{label}")
            else:
                warnings.append(f"投研結構化資料失敗 {candidate.code}: {err_str}")
                if progress:
                    progress(f"投研結構化資料 {index}/{total} 失敗：{label}，原因：{err_str}")
            if warn_str:
                warnings.append(warn_str)

        if not _maybe_throttle(index, throttle, stop_event):
            warnings.append("回補已收到停止訊號，投研結構化快取提前結束")
            return count, used_cache, warnings, timeout_count

    if progress:
        progress(f"投研結構化資料完成：{count} 檔成功，{timeout_count} 檔逾時")

    return count, used_cache, warnings, timeout_count


def warmup_gross_margin_cache(
    candidates: dict[str, BackfillCandidate],
    progress: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
    throttle: BackfillThrottle | None = None,
) -> tuple[int, list[str]]:
    """Warm up gross margin cache for candidate stocks.

    Returns:
        (count of successfully loaded stocks, list of warning messages)
    """
    from stock_scanner import _load_gross_margin_cache, _save_gross_margin_cache

    metrics = _load_gross_margin_cache()
    count = 0
    warnings: list[str] = []
    total = len(candidates)

    for index, candidate in enumerate(candidates.values(), start=1):
        if stop_event and stop_event.is_set():
            warnings.append("??????????")
            break
        if candidate.symbol and candidate.symbol not in metrics:
            try:
                series = load_gross_margin_series(candidate.symbol, metrics)
                if series:
                    count += 1
            except Exception as exc:
                warnings.append(f"毛利率快取失敗 {candidate.code}: {exc}")

        if progress and index % 20 == 0:
            progress(f"毛利率快取進度 {index}/{total}")
        _maybe_throttle(index, throttle, stop_event)

    _save_gross_margin_cache(metrics)
    if progress:
        progress(f"毛利率快取完成：{count} 檔更新")
    return count, warnings


def build_and_save_curated_scan_cache(
    report_date: date,
    force_refresh: bool = False,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[str], int]:
    """Build curated scan result and save to cache.

    Returns:
        (list of selected codes, count of selected codes)
    """
    from research_center.recent_scans import save_recent_scan_result

    if not force_refresh:
        cached = find_cached_curated_scan(report_date)
        if cached and cached.get("codes"):
            # Validate backfill complete marker before trusting curated cache
            marker_path = BACKFILL_MARKER_ROOT / report_date.isoformat() / "complete.json"
            marker_valid = False
            try:
                if marker_path.exists():
                    m = json.loads(marker_path.read_text(encoding="utf-8"))
                    marker_valid = bool(m.get("backfill_ready_for_scan") is True)
            except Exception:
                marker_valid = False

            if marker_valid:
                if progress:
                    progress(f"精選選股快取命中：{len(cached['codes'])} 檔")
                return cached["codes"], len(cached["codes"])
            else:
                if progress:
                    progress("[精選選股] backfill marker 無效，忽略快取並重新建立必要資料")

    try:
        if progress:
            progress("精選選股：執行交叉命中掃描")
        result = build_curated_scan_result(report_date=report_date)
        save_recent_scan_result(CURATED_SCAN_TYPE, report_date, result.report_text, result.selected_codes)
        if progress:
            progress(f"精選選股完成：{len(result.selected_codes)} 檔")
        return result.selected_codes, len(result.selected_codes)
    except Exception as exc:
        if progress:
            progress(f"精選選股快取建立失敗: {exc}")
        return [], 0


def warmup_market_screening_cache(
    universe: list[Any],
    report_date: date,
    force_refresh: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Warm up all lightweight data used by hard filters across the entire market.

    This ensures /scan and scans can read from local cache instead of fetching live.
    Returns a dict with counts and warnings.
    """

    def emit(message: str) -> None:
        if progress:
            progress(message)

    warnings: list[str] = []

    # 1. Monthly revenue for entire market
    emit("全市場月營收快取回補")
    revenue_count = 0
    try:
        revenue_history = load_recent_revenue_history(universe)
        revenue_count = len(revenue_history)
    except Exception as exc:
        warnings.append(f"全市場月營收快取失敗: {exc}")

    # 2. Price metrics for entire market
    emit("全市場價量快取回補")
    price_metric_count = 0
    try:
        price_metrics = load_price_metrics(universe, force_refresh=force_refresh)
        price_metric_count = len(price_metrics)
    except Exception as exc:
        warnings.append(f"全市場價量快取失敗: {exc}")

    # 3. Technical daily history for entire market
    emit("全市場技術日線快取檢查")
    technical_count = 0
    technical_cache_hits = 0
    total_symbols = sum(1 for entry in universe if entry.symbol)
    batch_size = 50
    for index, entry in enumerate(universe, start=1):
        if not entry.symbol:
            continue
        try:
            if not force_refresh and _is_technical_history_ready(entry.symbol, report_date):
                technical_cache_hits += 1
            else:
                fetch_daily_history(entry.symbol, report_date)
            technical_count += 1
        except Exception as exc:
            warnings.append(f"技術日線快取失敗 {entry.code}: {exc}")
        if index % batch_size == 0:
            emit(f"全市場技術日線快取進度 {index}/{total_symbols}，快取命中 {technical_cache_hits} 檔")
    emit(f"全市場技術日線快取完成：可用 {technical_count} 檔，快取命中 {technical_cache_hits} 檔")

    # 4. Ensure gross margin base cache file exists (don't full-scan per-stock)
    try:
        from stock_scanner import _load_gross_margin_cache
        _load_gross_margin_cache()
        # Don't brute-force per-stock gross margin for the entire market in this phase.
        # Candidate stocks will get full gross margin series in warmup_gross_margin_cache().
    except Exception as exc:
        warnings.append(f"毛利率基礎快取載入失敗: {exc}")

    return {
        "revenue_count": revenue_count,
        "price_metric_count": price_metric_count,
        "technical_count": technical_count,
        "warnings": warnings,
    }


def build_core_research_pool(
    candidates: dict[str, BackfillCandidate],
    config: dict[str, Any],
    progress: Callable[[str], None] | None = None,
) -> dict[str, BackfillCandidate]:
    """Build the core research pool from the candidate pool.

    Core pool = portfolio + monitor_list + recent_scans + recent_research
                + top N candidates by source count.

    Size is limited by DEFAULT_CORE_RESEARCH_LIMIT (80).
    """
    core: dict[str, BackfillCandidate] = {}
    limit = int(config.get("backfill_core_research_limit") or DEFAULT_CORE_RESEARCH_LIMIT)
    limit = max(0, limit)

    def add_to_core(code: str) -> None:
        if len(core) >= limit:
            return
        if code in candidates:
            core[code] = candidates[code]

    for source in ("portfolio", "monitor_list", "recent_scan", "recent_research"):
        for code, candidate in candidates.items():
            if source in candidate.sources:
                add_to_core(code)

    # If core is still under limit, add top candidates by source count
    if len(core) < limit:
        sorted_candidates = sorted(
            candidates.items(),
            key=lambda item: len(item[1].sources),
            reverse=True,
        )
        for code, candidate in sorted_candidates:
            if code not in core:
                core[code] = candidate
                if len(core) >= limit:
                    break

    if progress:
        progress(f"[完整回補] 核心股池建立：{len(core)} 檔（上限 {limit}）")

    return core


def build_backfill_priority_codes(
    candidates: dict[str, BackfillCandidate],
    curated_codes: list[str] | None = None,
) -> list[str]:
    """Build the priority pool used for backfill health visibility."""
    priority_sources = {"portfolio", "monitor_list", "recent_scan", "recent_research"}
    selected: dict[str, None] = {}
    for code, candidate in candidates.items():
        if candidate.sources & priority_sources:
            selected[code] = None
    for code in curated_codes or []:
        text = str(code).strip()
        if text in candidates:
            selected[text] = None
    return list(selected)


def backfill_candidate_data(
    candidates: dict[str, BackfillCandidate],
    core_pool: dict[str, BackfillCandidate],
    universe: list[Any],
    report_date: date,
    force_refresh: bool = False,
    progress: Callable[[str], None] | None = None,
    timeout_sec: float = DEFAULT_STRUCTURED_TIMEOUT_SECONDS,
    stop_event: threading.Event | None = None,
    throttle: BackfillThrottle | None = None,
    structured_total_budget_sec: float | None = DEFAULT_STRUCTURED_TOTAL_BUDGET_SECONDS,
) -> BackfillResult:
    """Warm up all cached data for the candidate pool (medium) and core research pool (full).

    This does NOT run AI search or model analysis.
    """
    def emit(message: str) -> None:
        if progress:
            progress(message)

    result = BackfillResult(report_date=report_date)
    result.universe_count = len(universe)
    result.candidate_count = len(candidates)
    result.core_research_count = len(core_pool)
    revenue_history: dict[str, Any] = {}
    chip_context: Any | None = None

    # Check stop before revenue
    if stop_event and stop_event.is_set():
        result.warnings.append("回補被使用者停止")
        return result

    # 1. Monthly revenue for candidates
    emit("回補月營收快取")
    candidate_universe = [entry for entry in universe if entry.code in candidates]
    try:
        revenue_history = load_recent_revenue_history(candidate_universe)
        result.revenue_count = len(revenue_history)
    except Exception as exc:
        result.warnings.append(f"月營收快取失敗: {exc}")

    # Check stop before price metrics
    if stop_event and stop_event.is_set():
        result.warnings.append("回補被使用者停止")
        return result

    # 2. Price metrics
    emit("回補價量快取")
    try:
        price_metrics = load_price_metrics(candidate_universe, force_refresh=force_refresh)
        result.price_metric_count = len(price_metrics)
    except Exception as exc:
        result.warnings.append(f"價量快取失敗: {exc}")

    # Check stop before technical
    if stop_event and stop_event.is_set():
        result.warnings.append("回補被使用者停止")
        return result

    # 3. Technical daily history
    emit("候選股技術日線快取檢查")
    technical_count = 0
    technical_cache_hits = 0
    for index, candidate in enumerate(candidates.values(), start=1):
        if not candidate.symbol:
            continue
        try:
            if not force_refresh and _is_technical_history_ready(candidate.symbol, report_date):
                technical_cache_hits += 1
            else:
                fetch_daily_history(candidate.symbol, report_date)
            technical_count += 1
        except Exception as exc:
            result.warnings.append(f"候選股技術日線快取失敗 {candidate.code}: {exc}")
        if emit and index % 20 == 0:
            emit(f"候選股技術日線快取進度 {index}/{len(candidates)}，快取命中 {technical_cache_hits} 檔")
        _maybe_throttle(index, throttle, stop_event)
        # Check stop every 20 stocks
        if stop_event and stop_event.is_set():
            result.warnings.append("回補被使用者停止")
            return result
    result.technical_count = technical_count
    emit(f"候選股技術日線快取完成：可用 {technical_count} 檔，快取命中 {technical_cache_hits} 檔")

    # Check stop before research structured
    if stop_event and stop_event.is_set():
        result.warnings.append("回補被使用者停止")
        return result

    # 4. Research structured data - only for core pool (full research)
    emit("回補投研結構化資料")
    research_count, used_cache, research_warnings, timeout_count = warmup_research_structured_data(
        core_pool,
        report_date,
        force_refresh,
        emit,
        timeout_sec,
        stop_event,
        throttle,
        max_total_sec=structured_total_budget_sec,
    )
    result.research_structured_count = research_count
    result.used_cache = used_cache
    result.warnings.extend(research_warnings)
    result.research_structured_timeout_count = timeout_count

    # Check stop before gross margin
    if stop_event and stop_event.is_set():
        result.warnings.append("回補被使用者停止")
        return result

    # 5. Gross margin cache
    emit("回補毛利率快取")
    try:
        gm_count, gm_warnings = warmup_gross_margin_cache(candidates, emit, stop_event, throttle)
        result.gross_margin_count = gm_count
        result.warnings.extend(gm_warnings)
    except Exception as exc:
        result.warnings.append(f"毛利率快取回補失敗: {exc}")

    # Check stop before chip
    if stop_event and stop_event.is_set():
        result.warnings.append("回補被使用者停止")
        return result

    # 6. Chip data cache
    emit("回補籌碼資料快取")
    try:
        priority_chip_codes = build_backfill_priority_codes(candidates)
        priority_chip_candidates = [candidates[code] for code in priority_chip_codes if code in candidates]
        emit(f"籌碼資料回補聚焦優先池：{len(priority_chip_candidates)}/{len(candidates)} 檔")
        extra_chip_candidates = [
            {
                "code": candidate.code,
                "symbol": candidate.symbol,
                "market": candidate.market,
                "name": candidate.name,
            }
            for candidate in priority_chip_candidates
        ]
        chip_context = warmup_chip_data_cache(
            report_date=report_date,
            full_backfill=True,
            force_refresh=force_refresh,
            progress_label="手動完整回補",
            scope="backfill",
            extra_candidates=extra_chip_candidates,
        )
        result.chip_candidate_count = len(chip_context.candidates) if chip_context.candidates is not None else 0
        result.latest_trading_date = chip_context.latest_trading_date
        # Extract chip coverage statistics
        try:
            if getattr(chip_context, "daily_data", None) is not None and not chip_context.daily_data.empty:
                daily = chip_context.daily_data
                # normalize date values
                try:
                    dates = pd.to_datetime(daily["date"]).dt.date.unique()
                except Exception:
                    dates = set(daily["date"].tolist())
                chip_days = len(dates)
                result.chip_coverage_days = int(chip_days)
                result.chip_target_days = int(getattr(chip_context, "scan_settings", {}).get("target_trading_days", TARGET_DAILY_TRADING_DAYS)) if hasattr(chip_context, "scan_settings") else TARGET_DAILY_TRADING_DAYS
                codes_in_daily = set(daily["code"].astype(str).unique())
                result.chip_candidate_coverage_pct = float(len(codes_in_daily) / max(1, result.candidate_count))
                result.chip_coverage_ok = (result.chip_coverage_days >= max(55, int(TARGET_DAILY_TRADING_DAYS * 0.9))) and (result.chip_candidate_coverage_pct >= 0.8)
        except Exception:
            # Safe defaults already set
            pass
        # Do NOT compute curated/backfill readiness here — wait until curated scan cache is built
    except Exception as exc:
        result.warnings.append(f"籌碼快取回補失敗: {exc}")

    # 7. Curated scan cache
    emit("建立精選選股快取")
    try:
        curated_codes, curated_count = build_and_save_curated_scan_cache(report_date, force_refresh, emit)
        result.curated_scan_codes = curated_codes
        result.curated_scan_count = curated_count
        result.curated_scan_cache_ready = bool(result.curated_scan_count > 0)
        result.curated_scan_ready = result.curated_scan_cache_ready
    except Exception as exc:
        result.warnings.append(f"精選選股快取回補失敗: {exc}")

    # 8. Gap analysis / health report
    emit("建立回補缺口健康度報告")
    try:
        priority_codes = build_backfill_priority_codes(candidates, result.curated_scan_codes)
        result.priority_pool_count = len(priority_codes)
        gap_report = build_backfill_gap_report(
            report_date=report_date,
            candidates=candidates,
            core_pool=core_pool,
            revenue_history=revenue_history,
            chip_context=chip_context,
            priority_codes=priority_codes,
        )
        result.gap_report = gap_report
        result.gap_report_path = str(write_gap_report(report_date, gap_report, BACKFILL_MARKER_ROOT))

        health = gap_report.get("health") or {}
        chip_health = health.get("chip") or {}
        if chip_health:
            result.chip_candidate_coverage_pct = float(chip_health.get("coverage_pct") or result.chip_candidate_coverage_pct)
        readiness = evaluate_backfill_readiness(
            health=health,
            curated_scan_count=result.curated_scan_count,
            priority_health=gap_report.get("priority_health") or {},
        )
        result.scan_data_ready = bool(readiness["scan_data_ready"])
        result.curated_scan_cache_ready = bool(readiness["curated_scan_cache_ready"])
        result.curated_scan_ready = result.curated_scan_cache_ready
        result.backfill_ready_for_scan = bool(readiness["backfill_ready_for_scan"])
        result.chip_coverage_ok = bool(readiness["health_ready"].get("chip", False))
        result.priority_chip_coverage_pct = float(readiness.get("priority_chip_coverage_pct") or 0.0)
        result.priority_tdcc_coverage_pct = float(readiness.get("priority_tdcc_coverage_pct") or 0.0)
        result.backfill_ready_for_research = "ready" if result.research_structured_timeout_count == 0 else "partial"
        emit(f"回補缺口健康度報告完成：{result.gap_report_path}")
    except Exception as exc:
        result.warnings.append(f"回補缺口健康度報告建立失敗: {exc}")

    return result


def run_full_backfill(
    report_date: date | None = None,
    force_refresh: bool = False,
    progress: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> BackfillResult:
    """Main entry point for /backfill command.

    Three-phase approach:
    1. Warm up market-wide screening data (revenue, price/volume, technical) — ~1700 stocks
    2. Build candidate pool and warm up medium data for candidates (~hundreds of stocks)
    3. Build core research pool and warm up full collect_research_data for core (≤80 stocks)

    AI search and model analysis remain real-time during /research.
    """
    target_date = report_date or get_tw_today()
    dag_events: list[dict[str, Any]] = []

    def emit(message: str) -> None:
        if progress:
            progress(message)

    emit(f"0% 開始完整回補，資料日期 {target_date.isoformat()}，force={force_refresh}")

    # Check stop before phase 1
    if stop_event and stop_event.is_set():
        result = BackfillResult(report_date=target_date)
        _record_backfill_dag_event(dag_events, "market_universe", "skipped", message="stopped_before_start")
        result.backfill_dag_events = dag_events
        result.warnings.append("回補被使用者停止")
        return result

    # Load config for backfill settings
    config: dict[str, Any] = {}
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        pass

    timeout_sec = float(config.get("backfill_structured_timeout_seconds") or DEFAULT_STRUCTURED_TIMEOUT_SECONDS)
    structured_total_budget_sec = float(config.get("backfill_structured_total_budget_seconds") or DEFAULT_STRUCTURED_TOTAL_BUDGET_SECONDS)
    throttle = BackfillThrottle(
        batch_size=int(config.get("backfill_throttle_batch_size") or DEFAULT_BACKFILL_THROTTLE_BATCH_SIZE),
        sleep_seconds=float(config.get("backfill_throttle_sleep_seconds") or DEFAULT_BACKFILL_THROTTLE_SLEEP_SECONDS),
    )

    # Phase 1: Market-wide screening data warmup (Tier 1: lightweight)
    emit("5% 載入股票宇宙")
    _record_backfill_dag_event(dag_events, "market_universe", "started", message="load_stock_universe")
    universe = load_stock_universe(force_refresh=force_refresh)
    _record_backfill_dag_event(dag_events, "market_universe", "completed", metadata={"universe_count": len(universe)})

    # Check stop before phase 1 warmup
    if stop_event and stop_event.is_set():
        result = BackfillResult(report_date=target_date)
        result.universe_count = len(universe)
        _record_backfill_dag_event(dag_events, "financial_cache", "skipped", message="stopped_before_market_screening")
        result.backfill_dag_events = dag_events
        result.warnings.append("回補被使用者停止")
        return result

    emit("10% 回補全市場硬篩基礎資料")
    _record_backfill_dag_event(dag_events, "financial_cache", "started", message="warmup_market_screening_cache")
    _record_backfill_dag_event(dag_events, "technical_cache", "started", message="warmup_market_screening_cache")
    screening_result = warmup_market_screening_cache(
        universe,
        target_date,
        force_refresh,
        emit,
    )
    _record_backfill_dag_event(
        dag_events,
        "financial_cache",
        "completed",
        metadata={
            "revenue_count": screening_result.get("revenue_count", 0),
            "price_metric_count": screening_result.get("price_metric_count", 0),
            "warning_count": len(screening_result.get("warnings", [])),
        },
    )
    _record_backfill_dag_event(
        dag_events,
        "technical_cache",
        "completed",
        metadata={
            "technical_count": screening_result.get("technical_count", 0),
            "warning_count": len(screening_result.get("warnings", [])),
        },
    )

    # Check stop before phase 2
    if stop_event and stop_event.is_set():
        result = BackfillResult(report_date=target_date)
        result.universe_count = len(universe)
        _record_backfill_dag_event(dag_events, "chip_cache", "skipped", message="stopped_before_candidate_pool")
        result.backfill_dag_events = dag_events
        result.warnings.append("回補被使用者停止")
        return result

    # Phase 2: Build candidate pool and warm up medium data (Tier 2: candidate pool)
    emit("35% 建立選股 + 投研候選池")
    candidates, universe, pool_warnings = build_backfill_candidate_pool(
        target_date, force_refresh, emit, preloaded_universe=universe,
    )

    emit(f"50% 回補候選股中量資料（候選池 {len(candidates)} 檔）")

    # Build core research pool (Tier 3: full research, limited)
    core_pool = build_core_research_pool(candidates, config, emit)

    emit(f"[完整回補] 三層回補：")
    emit(f"  全市場輕量回補：{len(universe)} 檔")
    emit(f"  候選股中量回補：{len(candidates)} 檔")
    emit(f"  核心股完整投研回補：{len(core_pool)} 檔")

    result = backfill_candidate_data(
        candidates,
        core_pool,
        universe,
        target_date,
        force_refresh,
        emit,
        timeout_sec,
        stop_event,
        throttle,
        structured_total_budget_sec=structured_total_budget_sec,
    )
    result.warnings.extend(pool_warnings)

    # Populate screening counts from phase 1
    result.screening_revenue_count = screening_result.get("revenue_count", 0)
    result.screening_price_metric_count = screening_result.get("price_metric_count", 0)
    result.screening_technical_count = screening_result.get("technical_count", 0)
    result.screening_warning_count = len(screening_result.get("warnings", []))
    result.warnings.extend(screening_result.get("warnings", []))
    if result.chip_candidate_count > 0 or result.chip_coverage_ok:
        _record_backfill_dag_event(
            dag_events,
            "chip_cache",
            "completed",
            metadata={
                "chip_candidate_count": result.chip_candidate_count,
                "chip_coverage_ok": result.chip_coverage_ok,
                "chip_candidate_coverage_pct": result.chip_candidate_coverage_pct,
            },
        )
    else:
        _record_backfill_dag_event(dag_events, "chip_cache", "skipped", message="no_chip_cache_data")
    if result.curated_scan_count > 0:
        _record_backfill_dag_event(dag_events, "curated_scan_cache", "completed", metadata={"curated_scan_count": result.curated_scan_count})
    else:
        _record_backfill_dag_event(dag_events, "curated_scan_cache", "skipped", message="no_curated_scan_cache")
    if result.research_structured_count > 0:
        _record_backfill_dag_event(
            dag_events,
            "research_feature_pack",
            "completed",
            metadata={
                "research_structured_count": result.research_structured_count,
                "timeout_count": result.research_structured_timeout_count,
            },
        )
    else:
        _record_backfill_dag_event(dag_events, "research_feature_pack", "skipped", message="no_research_structured_cache")
    _record_backfill_dag_event(dag_events, "news_event_store", "skipped", message="not_part_of_current_backfill")
    result.backfill_dag_events = dag_events

    emit("100% 完整回補完成")
    return result


def resolve_backfill_report_date(now: datetime | None = None) -> date:
    """Resolve the target report date for backfill based on current time (Taipei time).

    Rules:
    - Before 15:00 Taipei time: previous trading day
    - After 15:00 Taipei time: today (if today is a trading day)

    Previous trading day logic:
    - Monday -> Friday (last week)
    - Tuesday-Friday -> previous calendar day
    - Saturday/Sunday -> Friday
    """
    if now is None:
        now = datetime.now(TAIPEI_TZ)

    tw_hour = now.hour
    tw_weekday = now.weekday()

    # Weekend is always treated as previous Friday
    if tw_weekday == 6:  # Sunday
        prev = now - timedelta(days=2)
        return prev.date()
    if tw_weekday == 5:  # Saturday
        prev = now - timedelta(days=1)
        return prev.date()

    if tw_hour < 15:
        # Return previous trading day
        prev = now - timedelta(days=1)
        prev_weekday = prev.weekday()
        if prev_weekday == 6:  # Went back to Sunday
            prev = prev - timedelta(days=2)
        elif prev_weekday == 5:  # Went back to Saturday
            prev = prev - timedelta(days=1)
        return prev.date()
    else:
        return now.date()


def is_market_data_available(report_date: date, now: datetime | None = None) -> tuple[bool, str]:
    """Check if market data for the given date is available.

    Returns (True, "historical_date") for past dates.
    For today, checks if current time is after 15:00 and if data appears available.
    Only confirms availability when date field is explicitly equal to report_date.
    """
    if now is None:
        now = datetime.now(TAIPEI_TZ)

    today = now.date()

    if report_date < today:
        return (True, "historical_date")

    if report_date == today:
        tw_hour = now.hour
        if tw_hour < 15:
            return (False, "today_before_1500")

        # After 15:00, do a lightweight check using price_metrics
        try:
            from stock_scanner import load_stock_universe
            universe = load_stock_universe(force_refresh=False)
            # Pick a few representative stocks to check
            sample = universe[:3] if len(universe) >= 3 else universe
            from stock_scanner import load_price_metrics
            metrics = load_price_metrics(sample, force_refresh=False)
            # Only confirm availability if date field explicitly equals report_date
            for entry in sample:
                key = f"{entry.code}.TW" if entry.market == "TWSE" else f"{entry.code}.TWO"
                if key in metrics:
                    m = metrics[key]
                    if isinstance(m, dict) and "date" in m:
                        if str(m["date"])[:10] == str(report_date):
                            return (True, "today_data_available")
            # Some price metric sources do not expose an explicit date. In that case,
            # confirm with a few daily history samples before deciding today's data is unavailable.
            for entry in sample:
                symbol = getattr(entry, "symbol", None) or (f"{entry.code}.TW" if entry.market == "TWSE" else f"{entry.code}.TWO")
                try:
                    history_result = fetch_daily_history(symbol, report_date)
                    history = history_result[0] if isinstance(history_result, tuple) else history_result
                    if history is not None and not getattr(history, "empty", True) and "date" in history.columns:
                        latest_date = history["date"].dt.date.max()
                        if latest_date >= report_date:
                            return (True, "today_daily_history_available")
                except Exception:
                    continue
            # Metrics exist but no confirmed date for this report_date -> unavailable
            return (False, "today_data_date_unconfirmed")
        except Exception:
            return (False, "today_data_check_failed")

    # Future dates
    return (False, "future_date")


def is_backfill_cache_complete(report_date: date) -> tuple[bool, str]:
    """Check if backfill complete marker exists and is valid for the given date.

    Returns (True, "cache_complete") only when marker exists and passes health checks.
    Otherwise returns (False, reason).
    """
    marker_path = BACKFILL_MARKER_ROOT / report_date.isoformat() / "complete.json"
    if not marker_path.exists():
        return (False, "cache_incomplete")

    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception:
        return (False, "cache_marker_invalid")

    # Basic presence checks
    universe = int(payload.get("universe_count") or 0)
    candidate = int(payload.get("candidate_count") or 0)
    chip_candidate = int(payload.get("chip_candidate_count") or 0)
    curated = int(payload.get("curated_scan_count") or 0)

    if (universe == 0 and candidate == 0 and chip_candidate == 0 and curated == 0):
        return (False, "cache_incomplete")

    if universe <= 1000:
        return (False, "cache_universe_invalid")

    if candidate <= 0:
        return (False, "cache_candidate_invalid")

    health = payload.get("health") if isinstance(payload.get("health"), dict) else {}
    priority_health = payload.get("priority_health") if isinstance(payload.get("priority_health"), dict) else {}
    if health:
        for key, threshold in SCAN_HEALTH_THRESHOLDS.items():
            if _health_coverage(health, key) < threshold:
                if key == "chip" and _health_coverage(priority_health, "chip") >= threshold:
                    continue
                return (False, f"cache_{key}_gaps")

    if payload.get("curated_scan_cache_ready") is False:
        return (False, "cache_curated_scan_missing")

    if payload.get("scan_data_ready") is False:
        return (False, "cache_scan_data_gaps")

    if payload.get("backfill_ready_for_scan") is not True:
        return (False, "cache_not_ready_for_scan")

    return (True, "cache_complete")


def write_backfill_complete_marker(report_date: date, result: "BackfillResult") -> None:
    """Write the complete marker after successful backfill."""
    marker_dir = BACKFILL_MARKER_ROOT / report_date.isoformat()
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_path = marker_dir / "complete.json"
    import json

    # Gather quota and health status for marker
    finmind_remaining = 500
    fugle_remaining = 60
    cooling_sources: list[str] = []
    try:
        from data_source_manager import FinMindQuotaManager, FugleRateLimiter, SourceHealthManager
        fm = FinMindQuotaManager()
        fg = FugleRateLimiter()
        sh = SourceHealthManager()
        finmind_remaining = fm.hourly_remaining()
        fugle_remaining = fg.remaining_quota("historical")
        cooling_sources = sh.get_cooling_sources()
    except Exception:
        pass

    gap_report = getattr(result, "gap_report", {}) or {}
    health = gap_report.get("health", {}) if isinstance(gap_report, dict) else {}
    priority_health = gap_report.get("priority_health", {}) if isinstance(gap_report, dict) else {}
    readiness = evaluate_backfill_readiness(
        health=health,
        curated_scan_count=int(result.curated_scan_count),
        priority_health=priority_health,
    )
    backfill_dag_events = list(getattr(result, "backfill_dag_events", []) or [])
    backfill_dag = build_backfill_dag(
        report_date,
        marker={
            "health": health,
            "universe_count": int(result.universe_count),
            "news_event_count": int((getattr(result, "gap_report", {}) or {}).get("news_event_count") or 0),
            "backfill_dag_events": backfill_dag_events,
        },
    )
    payload = {
        "schema_version": 2,
        "report_date": report_date.isoformat(),
        "completed_at": datetime.now(TAIPEI_TZ).isoformat(),
        "universe_count": int(result.universe_count),
        "candidate_count": int(result.candidate_count),
        "core_research_count": int(result.core_research_count),
        "research_structured_count": int(result.research_structured_count),
        "research_structured_timeout_count": int(result.research_structured_timeout_count),
        "warnings_count": len(result.warnings),
        # Screening/caching health
        "screening_cache_ok": bool(result.universe_count > 1000 and result.screening_price_metric_count > 0 and result.screening_technical_count > 0),
        "chip_coverage_days": int(getattr(result, "chip_coverage_days", 0)),
        "chip_target_days": int(getattr(result, "chip_target_days", TARGET_DAILY_TRADING_DAYS)),
        "chip_candidate_coverage_pct": float(getattr(result, "chip_candidate_coverage_pct", 0.0)),
        "chip_coverage_ok": bool(readiness["health_ready"].get("chip", False)),
        "priority_pool_count": int(getattr(result, "priority_pool_count", 0)),
        "priority_chip_coverage_pct": float(readiness.get("priority_chip_coverage_pct") or getattr(result, "priority_chip_coverage_pct", 0.0)),
        "priority_tdcc_coverage_pct": float(readiness.get("priority_tdcc_coverage_pct") or getattr(result, "priority_tdcc_coverage_pct", 0.0)),
        "chip_readiness_basis": str(readiness.get("chip_readiness_basis") or "all_candidates"),
        "scan_data_ready": bool(readiness["scan_data_ready"]),
        "curated_scan_cache_ready": bool(readiness["curated_scan_cache_ready"]),
        "curated_scan_ready": bool(readiness["curated_scan_ready"]),
        "backfill_ready_for_scan": bool(readiness["backfill_ready_for_scan"]),
        "backfill_ready_for_research": str(getattr(result, "backfill_ready_for_research", "partial")),
        "health": health,
        "priority_health": priority_health,
        "backfill_priority_plan": build_backfill_priority_plan(report_date, health=health),
        "backfill_dag_events": backfill_dag_events,
        "backfill_dag_event_summary": summarize_backfill_events(backfill_dag_events),
        "backfill_dag": backfill_dag,
        "backfill_dag_summary": summarize_backfill_dag(backfill_dag),
        "gap_report_path": str(getattr(result, "gap_report_path", "")),
        # Source quota and health
        "finmind_quota_remaining": finmind_remaining,
        "fugle_historical_remaining": fugle_remaining,
        "cooling_sources": cooling_sources,
    }
    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    try:
        register_artifact(
            build_artifact_record(
                artifact_type="backfill_complete_marker",
                path=marker_path,
                schema_version="backfill_marker_v2",
                data_date=report_date,
                source="backfill",
                completeness=1.0 if payload.get("backfill_ready_for_scan") else 0.5,
                usable=bool(payload.get("backfill_ready_for_scan") or payload.get("scan_data_ready")),
                metadata={
                    "candidate_count": payload.get("candidate_count"),
                    "backfill_ready_for_scan": payload.get("backfill_ready_for_scan"),
                    "backfill_ready_for_research": payload.get("backfill_ready_for_research"),
                },
            )
        )
    except Exception:
        pass


def _format_backfill_health_summary_legacy(result: "BackfillResult") -> str:
    try:
        lines = ["【快取健康度】"]
        ready = "是" if bool(getattr(result, "backfill_ready_for_scan", False)) else "否"
        lines.append(f"- 選股快取可用：{ready}")
        gap_report = getattr(result, "gap_report", {}) or {}
        health = gap_report.get("health") if isinstance(gap_report, dict) else {}
        if isinstance(health, dict) and health:
            labels = {
                "technical": "技術面",
                "revenue": "月營收",
                "financial": "財報/毛利率",
                "chip": "籌碼法人",
                "tdcc": "TDCC 大戶",
                "research_structured": "投研結構化",
            }
            for key, label in labels.items():
                section = health.get(key) or {}
                pct = int(round(float(section.get("coverage_pct") or 0.0) * 100))
                missing = int(section.get("missing_count") or 0)
                ready_count = int(section.get("ready_count") or 0)
                total = int(section.get("candidate_count") or 0)
                lines.append(f"- {label}：{pct}%（可用 {ready_count}/{total}，缺 {missing} 檔）")
                missing_codes = section.get("missing_codes") or []
                if missing_codes:
                    preview = ", ".join(str(code) for code in missing_codes[:20])
                    lines.append(f"  缺口前 20 檔：{preview}")
        else:
            days = int(getattr(result, "chip_coverage_days", 0))
            target = int(getattr(result, "chip_target_days", TARGET_DAILY_TRADING_DAYS))
            lines.append(f"- 籌碼覆蓋：{days}/{target} 交易日")
            pct = float(getattr(result, "chip_candidate_coverage_pct", 0.0))
            lines.append(f"- 候選股籌碼覆蓋率：{int(round(pct * 100))}%")
        curated = "已建立" if bool(getattr(result, "curated_scan_ready", False)) else "未建立"
        lines.append(f"- 精選選股快取：{curated}")
        gap_path = str(getattr(result, "gap_report_path", "") or "")
        if gap_path:
            lines.append(f"- 缺口明細：{gap_path}")

        # Add quota and health status
        try:
            from data_source_manager import FinMindQuotaManager, FugleRateLimiter, SourceHealthManager
            fm = FinMindQuotaManager()
            fg = FugleRateLimiter()
            sh = SourceHealthManager()
            lines.append(f"- FinMind 安全剩餘額度：{fm.hourly_remaining()}")
            lines.append(f"- Fugle historical 剩餘額度：{fg.remaining_quota('historical')}/min")
            cooling = sh.get_cooling_sources()
            if cooling:
                lines.append(f"- 冷卻來源：{', '.join(cooling)}")
            else:
                lines.append(f"- 冷卻來源：無")
        except Exception:
            pass

        return "\n".join(lines)
    except Exception:
        return "【快取健康度】無資料"


def format_backfill_health_summary(result: "BackfillResult") -> str:
    try:
        lines = ["【快取健康度】"]
        scan_ready = "是" if bool(getattr(result, "backfill_ready_for_scan", False)) else "否"
        data_ready = "是" if bool(getattr(result, "scan_data_ready", False)) else "否"
        curated_ready = "是" if bool(getattr(result, "curated_scan_cache_ready", False)) else "否"
        lines.append(f"- 選股快取可用：{scan_ready}")
        lines.append(f"- 選股依賴資料達標：{data_ready}")
        lines.append(f"- 精選選股快取：{curated_ready}")
        lines.append(
            "- 本次補齊數量："
            f"月營收 {int(getattr(result, 'revenue_count', 0))}、"
            f"價量 {int(getattr(result, 'price_metric_count', 0))}、"
            f"技術 {int(getattr(result, 'technical_count', 0))}、"
            f"毛利率 {int(getattr(result, 'gross_margin_count', 0))}、"
            f"研究底稿 {int(getattr(result, 'research_structured_count', 0))}"
        )

        gap_report = getattr(result, "gap_report", {}) or {}
        health = gap_report.get("health") if isinstance(gap_report, dict) else {}
        priority_health = gap_report.get("priority_health") if isinstance(gap_report, dict) else {}
        labels = {
            "technical": "技術日線",
            "revenue": "月營收",
            "financial": "財報/毛利率",
            "chip": "法人籌碼",
            "tdcc": "TDCC 集保",
            "research_structured": "研究底稿",
        }
        if isinstance(health, dict) and health:
            for key, label in labels.items():
                section = health.get(key) or {}
                pct = int(round(float(section.get("coverage_pct") or 0.0) * 100))
                ready_count = int(section.get("ready_count") or 0)
                total = int(section.get("candidate_count") or 0)
                missing = int(section.get("missing_count") or 0)
                lines.append(f"- {label}覆蓋率：{pct}%（{ready_count}/{total}，缺 {missing} 檔）")
                missing_codes = section.get("missing_codes") or []
                if missing_codes:
                    preview = ", ".join(str(code) for code in missing_codes[:20])
                    lines.append(f"  缺資料前 20 檔：{preview}")
        else:
            days = int(getattr(result, "chip_coverage_days", 0))
            target = int(getattr(result, "chip_target_days", TARGET_DAILY_TRADING_DAYS))
            pct = float(getattr(result, "chip_candidate_coverage_pct", 0.0))
            lines.append(f"- 籌碼交易日覆蓋：{days}/{target}")
            lines.append(f"- 候選股籌碼覆蓋率：{int(round(pct * 100))}%")

        priority_count = int(getattr(result, "priority_pool_count", 0))
        if priority_count:
            lines.append(f"- 優先股票池：{priority_count} 檔")
            for key, label in (("chip", "優先池法人籌碼"), ("tdcc", "優先池 TDCC")):
                section = priority_health.get(key) if isinstance(priority_health, dict) else {}
                if isinstance(section, dict) and section:
                    pct = int(round(float(section.get("coverage_pct") or 0.0) * 100))
                    ready_count = int(section.get("ready_count") or 0)
                    total = int(section.get("candidate_count") or 0)
                    lines.append(f"- {label}覆蓋率：{pct}%（{ready_count}/{total}）")

        gap_path = str(getattr(result, "gap_report_path", "") or "")
        if gap_path:
            lines.append(f"- 缺口報告：{gap_path}")

        try:
            from data_source_manager import FinMindQuotaManager, FugleRateLimiter, SourceHealthManager
            fm = FinMindQuotaManager()
            fg = FugleRateLimiter()
            sh = SourceHealthManager()
            lines.append(f"- FinMind 安全剩餘額度：{fm.hourly_remaining()}")
            lines.append(f"- Fugle historical 剩餘額度：{fg.remaining_quota('historical')}/min")
            cooling = sh.get_cooling_sources()
            lines.append(f"- 冷卻來源：{', '.join(cooling) if cooling else '無'}")
        except Exception:
            pass

        return "\n".join(lines)
    except Exception:
        return "【快取健康度】無法取得資料"


# Module-level lock to prevent concurrent backfill runs
BACKFILL_RUNNING = threading.Lock()


def is_backfill_running() -> bool:
    """Return whether the full backfill lock is currently held."""
    return BACKFILL_RUNNING.locked()


@dataclass
class BackfillRunDecision:
    """Result of a backfill policy decision."""
    status: str  # "completed", "skipped", "already_running"
    report_date: date | None
    reason: str | None = None
    result: "BackfillResult | None" = None


def run_backfill_if_needed(
    report_date: date | None = None,
    force_refresh: bool = False,
    progress: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> BackfillRunDecision:
    """Policy-driven backfill runner with lock, cache check, and data availability check.

    Returns a BackfillRunDecision describing what happened.
    """
    # Resolve report date
    if report_date is None:
        report_date = resolve_backfill_report_date()

    # If stop event already set before we start, skip entirely
    if stop_event and stop_event.is_set():
        return BackfillRunDecision(
            status="skipped",
            report_date=report_date,
            reason="stopped_before_start",
        )

    # Try to acquire lock
    if not BACKFILL_RUNNING.acquire(blocking=False):
        return BackfillRunDecision(
            status="skipped",
            report_date=report_date,
            reason="already_running",
        )

    try:
        # Check cache completeness (skip unless force)
        if not force_refresh:
            complete, reason = is_backfill_cache_complete(report_date)
            if complete:
                return BackfillRunDecision(
                    status="skipped",
                    report_date=report_date,
                    reason=reason,
                )

        # Check market data availability
        available, avail_reason = is_market_data_available(report_date)
        if not available:
            return BackfillRunDecision(
                status="skipped",
                report_date=report_date,
                reason=avail_reason,
            )

        # Run the actual backfill
        result = run_full_backfill(report_date, force_refresh, progress, stop_event)

        # Check if stopped after backfill returns
        if stop_event and stop_event.is_set():
            result.warnings.append("回補被使用者停止")
            return BackfillRunDecision(
                status="stopped",
                report_date=report_date,
                reason="stopped_during_execution",
                result=result,
            )

        # Write complete marker on success
        write_backfill_complete_marker(report_date, result)

        return BackfillRunDecision(
            status="completed",
            report_date=report_date,
            result=result,
        )
    finally:
        BACKFILL_RUNNING.release()


def parse_backfill_args(args: list[str]) -> tuple[date | None, bool]:
    """Parse /backfill command arguments.

    Supported formats:
        /backfill
        /backfill 2026-05-15
        /backfill force
        /backfill 2026-05-15 force

    Returns:
        (report_date or None, force_refresh).
        If no date specified, returns None; backfill policy decides the date.
    """
    report_date = None  # Will be resolved by policy if not specified
    force_refresh = False

    for raw in args:
        token = str(raw).strip().lower()
        if not token:
            continue
        if token in {"force", "refresh", "強制", "強制刷新"}:
            force_refresh = True
            continue
        if token in {"today", "今日", "今天"}:
            report_date = get_tw_today()
            continue
        try:
            report_date = datetime.strptime(token, "%Y-%m-%d").date()
            continue
        except ValueError:
            raise ValueError("參數格式錯誤。用法：/backfill [YYYY-MM-DD] [force]")

    return report_date, force_refresh
