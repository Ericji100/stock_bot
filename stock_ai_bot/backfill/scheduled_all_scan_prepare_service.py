from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from stock_ai_bot.backfill.backfill_service import warmup_market_screening_cache
from chip_strategies import TARGET_DAILY_TRADING_DAYS, warmup_chip_data_cache
from candidate_filter_service import resolve_hard_filter_settings
from stock_ai_bot.scanning.stock_scanner import load_stock_universe, scan_tw_market


ROOT_DIR = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT_DIR / ".cache"


@dataclass
class ScheduledAllScanPrepareResult:
    report_date: date
    ok: bool = True
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    counts: dict[str, int | float | str | None] = field(default_factory=dict)

    @property
    def has_issues(self) -> bool:
        return bool(self.warnings or self.errors or not self.ok)


def prepare_scheduled_all_scan_data(
    report_date: date,
    scan_settings: dict[str, Any] | None = None,
    *,
    force_refresh: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ScheduledAllScanPrepareResult:
    """Warm up data needed by the scheduled all-scan before running selection.

    This is intentionally a thin orchestration layer over existing cache/backfill
    functions. It does not change scan candidate generation or scoring logic.
    """

    result = ScheduledAllScanPrepareResult(report_date=report_date)
    settings = resolve_hard_filter_settings(scan_settings or {})

    def emit(message: str) -> None:
        if progress:
            progress(message)

    universe: list[Any] = []
    try:
        emit("前置資料準備：讀取股票清單")
        universe = load_stock_universe(force_refresh=False)
        result.counts["universe"] = len(universe)
        result.steps.append("股票清單")
    except Exception as exc:
        _record_error(result, f"股票清單讀取失敗：{exc}")

    if universe:
        try:
            emit("前置資料準備：補齊價量、月營收與技術日線快取")
            warmup = warmup_market_screening_cache(
                universe,
                report_date,
                force_refresh=force_refresh,
                progress=emit,
            )
            result.counts["monthly_revenue"] = int(warmup.get("revenue_count") or 0)
            result.counts["price_metrics"] = int(warmup.get("price_metric_count") or 0)
            result.counts["technical_history"] = int(warmup.get("technical_count") or 0)
            result.counts["technical_adjusted_history"] = int(warmup.get("technical_adjusted_count") or 0)
            result.counts["market_risk_flags"] = int(warmup.get("market_risk_count") or 0)
            result.counts["market_risk_complete"] = "是" if warmup.get("market_risk_complete") else "否"
            result.warnings.extend(str(item) for item in warmup.get("warnings") or [])
            result.steps.append("價量/月營收/技術日線")
        except Exception as exc:
            _record_error(result, f"價量、月營收或技術日線快取補齊失敗：{exc}")

    try:
        emit("前置資料準備：補齊財報營收與毛利率候選快取")
        financial_report = scan_tw_market(
            force_refresh,
            None,
            settings,
            report_date=report_date,
            historical_replay=False,
        )
        result.counts["financial_candidates"] = len(getattr(financial_report, "candidates", []) or [])
        result.steps.append("財報營收/毛利率")
    except Exception as exc:
        _record_error(result, f"財報營收或毛利率快取補齊失敗：{exc}")

    try:
        emit("前置資料準備：補齊近 60 日法人、持股比例與大戶快取")
        chip_context = warmup_chip_data_cache(
            report_date=report_date,
            full_backfill=True,
            force_refresh=force_refresh,
            progress_label="20:30 全部選股前置資料",
            scope="scheduled_all_scan_prepare",
            scan_settings=settings,
        )
        result.counts["chip_candidates"] = _safe_len(getattr(chip_context, "candidates", None))
        latest_trading_date = getattr(chip_context, "latest_trading_date", None)
        result.counts["chip_latest_trading_date"] = latest_trading_date.isoformat() if latest_trading_date else None
        _add_chip_coverage(result, chip_context)
        result.steps.append("近 60 日籌碼/法人/大戶")
    except Exception as exc:
        _record_error(result, f"近 60 日籌碼、法人或大戶快取補齊失敗：{exc}")

    _add_file_status(result, "price_metrics_cache", CACHE_DIR / "price_metrics.json")
    _add_file_status(result, "gross_margin_cache", CACHE_DIR / "gross_margin.json")
    _add_file_status(result, "monthly_revenue_cache", CACHE_DIR / "monthly_revenue")
    _add_file_status(result, "chip_daily_cache", CACHE_DIR / "chip_daily")

    if result.errors:
        result.ok = False
    return result


def format_scheduled_all_scan_prepare_message(result: ScheduledAllScanPrepareResult) -> str:
    status = "完成" if result.ok else "部分完成"
    lines = [
        f"20:30 全部選股前置資料準備：{status}",
        f"資料日期：{result.report_date.isoformat()}",
    ]
    if result.steps:
        lines.append(f"已處理：{'、'.join(result.steps)}")

    count_labels = [
        ("universe", "股票清單"),
        ("monthly_revenue", "月營收"),
        ("price_metrics", "價量"),
        ("technical_history", "技術日線"),
        ("technical_adjusted_history", "還原價日線"),
        ("market_risk_flags", "注意/處置旗標"),
        ("market_risk_complete", "風險來源完整"),
        ("financial_candidates", "財報營收候選"),
        ("chip_candidates", "籌碼候選"),
        ("chip_coverage_days", "籌碼天數"),
        ("chip_latest_trading_date", "籌碼最新日"),
    ]
    count_parts = []
    for key, label in count_labels:
        value = result.counts.get(key)
        if value not in (None, ""):
            count_parts.append(f"{label} {value}")
    if count_parts:
        lines.append("資料狀態：" + "、".join(count_parts))

    issues = [*result.errors, *result.warnings]
    if issues:
        lines.append("提醒：" + "；".join(str(item) for item in issues[:4]))
    else:
        lines.append("提醒：未偵測到明顯資料缺口")
    return "\n".join(lines)


def _record_error(result: ScheduledAllScanPrepareResult, message: str) -> None:
    result.errors.append(message)
    result.ok = False


def _safe_len(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(len(value))
    except TypeError:
        return 0


def _add_chip_coverage(result: ScheduledAllScanPrepareResult, chip_context: Any) -> None:
    daily_data = getattr(chip_context, "daily_data", None)
    if daily_data is None or getattr(daily_data, "empty", True):
        result.warnings.append("籌碼日資料缺口：未取得近 60 日法人日資料")
        return
    try:
        dates = daily_data["date"].astype(str).unique()
        day_count = len(dates)
        result.counts["chip_coverage_days"] = day_count
        if day_count < max(55, int(TARGET_DAILY_TRADING_DAYS * 0.9)):
            result.warnings.append(f"籌碼日資料僅 {day_count}/{TARGET_DAILY_TRADING_DAYS} 日，精選結果可能偏保守")
    except Exception as exc:
        result.warnings.append(f"籌碼日資料完整度檢查失敗：{exc}")


def _add_file_status(result: ScheduledAllScanPrepareResult, key: str, path: Path) -> None:
    if path.exists():
        try:
            result.counts[key] = int(path.stat().st_size)
        except OSError:
            result.counts[key] = "exists"
    else:
        result.warnings.append(f"{path.name} 快取不存在")
