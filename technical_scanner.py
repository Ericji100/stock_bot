from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from fugle_data import fetch_fugle_history
from stock_scanner import (
    DEFAULT_SCAN_SETTINGS,
    UNCLASSIFIED_INDUSTRY,
    load_price_metrics,
    load_recent_revenue_history,
    load_stock_universe,
)


ROOT_DIR = Path(__file__).resolve().parent
TECH_CACHE_DIR = ROOT_DIR / ".cache" / "technical_daily"
TECH_CACHE_TTL_SECONDS = 12 * 60 * 60
HISTORY_DAYS = 260
MIN_HISTORY_ROWS = 120
YFINANCE_PAUSE_SECONDS = 0.2

MACD_FAST = 21
MACD_SLOW = 55
MACD_SIGNAL = 55
KD_RSV_PERIOD = 9
KD_K_PERIOD = 9
KD_D_PERIOD = 55
MA_SHORT = 21
MA_LONG = 105
ENABLE_MACD_PULLBACK_BREAKOUT = False
ENABLE_DIVERGENCE_SIGNALS = False

BULLISH_SIGNAL_ORDER = [
    "突破 21MA",
    "突破 105MA",
    "MACD 回測突破",
    "MACD 黃金交叉",
    "KD 黃金交叉",
    "MACD 低檔背離",
    "KD 低檔背離",
]
BEARISH_SIGNAL_ORDER = [
    "MACD 死亡交叉",
    "KD 死亡交叉",
    "MACD 高檔背離",
    "KD 高檔背離",
]


@dataclass(frozen=True)
class TechnicalCandidate:
    code: str
    symbol: str
    market: str
    name: str
    industry: str
    price: float
    avg_volume_20d: float
    monthly_revenue: float


@dataclass(frozen=True)
class TechnicalScanResult:
    report_date: date
    total_symbols: int
    hard_filter_passed: int
    matched_symbols: int
    bullish: dict[str, dict[str, list[str]]]
    bearish: dict[str, dict[str, list[str]]]
    sources: set[str]


def _print_progress(label: str, progress: float, message: str) -> None:
    print(f"[選股進度][{label}] {progress:.2f}% {message}", flush=True)


def _ensure_cache_dir() -> None:
    TECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(symbol: str) -> Path:
    safe_symbol = symbol.replace(".", "_")
    return TECH_CACHE_DIR / f"{safe_symbol}.csv"


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    return time.time() - path.stat().st_mtime < TECH_CACHE_TTL_SECONDS


def _standardize_history(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    history = frame.copy()
    if isinstance(history.columns, pd.MultiIndex):
        history.columns = history.columns.get_level_values(0)
    rename_map = {
        "Date": "date",
        "Datetime": "date",
        "datetime": "date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    }
    history = history.reset_index().rename(columns=rename_map)
    required = ["date", "open", "high", "low", "close", "volume"]
    if not set(required).issubset(history.columns):
        return pd.DataFrame()
    history = history[required].copy()
    history["date"] = pd.to_datetime(history["date"], errors="coerce").dt.normalize()
    for column in ["open", "high", "low", "close", "volume"]:
        history[column] = pd.to_numeric(history[column], errors="coerce")
    history = history.dropna(subset=["date", "open", "high", "low", "close"])
    return history.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def _load_cached_history(symbol: str, require_fresh: bool = True) -> pd.DataFrame:
    path = _cache_path(symbol)
    if require_fresh and not _is_fresh(path):
        return pd.DataFrame()
    if not path.exists():
        return pd.DataFrame()
    try:
        return _standardize_history(pd.read_csv(path))
    except Exception:
        return pd.DataFrame()


def _save_history(symbol: str, history: pd.DataFrame) -> None:
    if history.empty:
        return
    _ensure_cache_dir()
    history.to_csv(_cache_path(symbol), index=False, encoding="utf-8")


def fetch_daily_history(symbol: str, end_date: date) -> tuple[pd.DataFrame, str]:
    require_fresh = end_date >= datetime.now().date()
    cached = _load_cached_history(symbol, require_fresh=require_fresh)
    if not cached.empty and cached["date"].dt.date.max() >= end_date:
        return cached[cached["date"].dt.date <= end_date].copy(), "本機快取"

    start_date = end_date - timedelta(days=HISTORY_DAYS + 80)
    try:
        raw = yf.download(
            symbol,
            start=start_date,
            end=end_date + timedelta(days=1),
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
    except Exception:
        raw = pd.DataFrame()
    history = _standardize_history(raw)
    source = "Yahoo Finance"

    if history.empty:
        fugle_history = fetch_fugle_history(symbol, start_date, end_date, "1d")
        history = _standardize_history(fugle_history)
        source = "Fugle"

    if not history.empty:
        _save_history(symbol, history)
    time.sleep(YFINANCE_PAUSE_SECONDS)
    return history, source


def build_hard_filter_candidates(scan_settings: dict[str, float] | None = None) -> tuple[list[TechnicalCandidate], int]:
    settings = dict(DEFAULT_SCAN_SETTINGS)
    if scan_settings:
        settings.update(scan_settings)
    settings["max_price"] = 80.0

    universe = load_stock_universe(False)
    revenue_history = load_recent_revenue_history(universe)
    price_metrics = load_price_metrics(universe)

    candidates: list[TechnicalCandidate] = []
    for entry in universe:
        revenue_points = revenue_history.get(entry.code)
        price_metric = price_metrics.get(entry.symbol)
        if not revenue_points or not price_metric:
            continue
        latest_revenue = float(revenue_points[0].revenue)
        price = price_metric.get("price")
        avg_volume = price_metric.get("avg_volume_20d")
        if price is None or avg_volume is None:
            continue
        price = float(price)
        avg_volume = float(avg_volume)
        if not (float(settings["min_price"]) <= price <= 80.0):
            continue
        if avg_volume <= float(settings["min_avg_volume_20d"]):
            continue
        if latest_revenue <= float(settings["min_monthly_revenue"]):
            continue
        candidates.append(
            TechnicalCandidate(
                code=entry.code,
                symbol=entry.symbol,
                market=entry.market,
                name=entry.name,
                industry=entry.industry or UNCLASSIFIED_INDUSTRY,
                price=round(price, 2),
                avg_volume_20d=round(avg_volume, 2),
                monthly_revenue=latest_revenue,
            )
        )
    return candidates, len(universe)


def apply_indicators(history: pd.DataFrame) -> pd.DataFrame:
    frame = history.copy()
    frame[f"MA{MA_SHORT}"] = frame["close"].rolling(MA_SHORT).mean()
    frame[f"MA{MA_LONG}"] = frame["close"].rolling(MA_LONG).mean()

    ema_fast = frame["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = frame["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    frame["DIF"] = ema_fast - ema_slow
    frame["DEA"] = frame["DIF"].ewm(span=MACD_SIGNAL, adjust=False).mean()
    frame["MACD_HIST"] = frame["DIF"] - frame["DEA"]

    low_min = frame["low"].rolling(KD_RSV_PERIOD).min()
    high_max = frame["high"].rolling(KD_RSV_PERIOD).max()
    frame["RSV"] = ((frame["close"] - low_min) / (high_max - low_min) * 100).clip(0, 100)
    frame["K"] = frame["RSV"].ewm(span=KD_K_PERIOD, adjust=False).mean()
    frame["D"] = frame["K"].ewm(span=KD_D_PERIOD, adjust=False).mean()
    return frame


def _cross_up(prev_left: float, prev_right: float, now_left: float, now_right: float) -> bool:
    return pd.notna(prev_left) and pd.notna(prev_right) and pd.notna(now_left) and pd.notna(now_right) and prev_left < prev_right and now_left > now_right


def _cross_down(prev_left: float, prev_right: float, now_left: float, now_right: float) -> bool:
    return pd.notna(prev_left) and pd.notna(prev_right) and pd.notna(now_left) and pd.notna(now_right) and prev_left > prev_right and now_left < now_right


def _zone_summary(frame: pd.DataFrame, mask: pd.Series, price_column: str, indicator_column: str) -> pd.DataFrame:
    zones = frame.loc[mask].copy()
    if zones.empty:
        return pd.DataFrame()
    zone_id = mask.ne(mask.shift(fill_value=False)).cumsum()
    zones["zone_id"] = zone_id.loc[zones.index]
    summary = zones.groupby("zone_id").agg(
        start=("date", "min"),
        end=("date", "max"),
        price_min=(price_column, "min"),
        price_max=(price_column, "max"),
        indicator_min=(indicator_column, "min"),
        indicator_max=(indicator_column, "max"),
        k_min=("K", "min"),
        k_max=("K", "max"),
    )
    return summary.sort_values("end")


def _latest_two_zones(summary: pd.DataFrame) -> tuple[pd.Series | None, pd.Series | None]:
    if len(summary) < 2:
        return None, None
    return summary.iloc[-2], summary.iloc[-1]


def is_macd_low_divergence(frame: pd.DataFrame) -> bool:
    summary = _zone_summary(frame, frame["MACD_HIST"] < 0, "low", "MACD_HIST")
    previous, latest = _latest_two_zones(summary)
    if previous is None or latest is None:
        return False
    return latest["price_min"] < previous["price_min"] and latest["indicator_min"] > previous["indicator_min"]


def is_macd_high_divergence(frame: pd.DataFrame) -> bool:
    summary = _zone_summary(frame, frame["MACD_HIST"] > 0, "high", "MACD_HIST")
    previous, latest = _latest_two_zones(summary)
    if previous is None or latest is None:
        return False
    return latest["price_max"] > previous["price_max"] and latest["indicator_max"] < previous["indicator_max"]


def is_kd_low_divergence(frame: pd.DataFrame) -> bool:
    summary = _zone_summary(frame, frame["K"] < frame["D"], "low", "K")
    previous, latest = _latest_two_zones(summary)
    if previous is None or latest is None or latest["k_min"] >= 50:
        return False
    return latest["price_min"] < previous["price_min"] and latest["indicator_min"] > previous["indicator_min"]


def is_kd_high_divergence(frame: pd.DataFrame) -> bool:
    summary = _zone_summary(frame, frame["K"] > frame["D"], "high", "K")
    previous, latest = _latest_two_zones(summary)
    if previous is None or latest is None or latest["k_max"] <= 50:
        return False
    return latest["price_max"] > previous["price_max"] and latest["indicator_max"] < previous["indicator_max"]


def is_macd_pullback_breakout(frame: pd.DataFrame) -> bool:
    if frame["MACD_HIST"].iloc[-1] <= 0:
        return False

    non_positive_days = frame.index[frame["MACD_HIST"] <= 0]
    if len(non_positive_days) == 0:
        return False

    red_start_pos = int(non_positive_days[-1]) + 1
    red_zone_past = frame.iloc[red_start_pos:-1]
    if red_zone_past.empty:
        return False

    touch_days = red_zone_past.index[red_zone_past["low"] <= red_zone_past[f"MA{MA_SHORT}"]]
    if len(touch_days) == 0:
        return False

    touch_pos = red_zone_past.index.get_loc(touch_days[-1])
    pre_touch_zone = red_zone_past.iloc[:touch_pos]
    if pre_touch_zone.empty:
        return False

    breakout_high = pre_touch_zone["high"].max()
    previous = frame.iloc[-2]
    latest = frame.iloc[-1]
    return bool(
        pd.notna(breakout_high)
        and pd.notna(latest[f"MA{MA_SHORT}"])
        and previous["close"] <= breakout_high
        and latest["close"] > breakout_high
        and latest["close"] > latest[f"MA{MA_SHORT}"]
    )


def detect_signals(frame: pd.DataFrame) -> tuple[list[str], list[str]]:
    if len(frame) < MIN_HISTORY_ROWS:
        return [], []
    frame = apply_indicators(frame).dropna(subset=["close"]).reset_index(drop=True)
    if len(frame) < MIN_HISTORY_ROWS:
        return [], []

    previous = frame.iloc[-2]
    latest = frame.iloc[-1]
    bullish: list[str] = []
    bearish: list[str] = []

    if _cross_up(previous["close"], previous[f"MA{MA_SHORT}"], latest["close"], latest[f"MA{MA_SHORT}"]):
        bullish.append("突破 21MA")
    if _cross_up(previous["close"], previous[f"MA{MA_LONG}"], latest["close"], latest[f"MA{MA_LONG}"]):
        bullish.append("突破 105MA")
    if ENABLE_MACD_PULLBACK_BREAKOUT and is_macd_pullback_breakout(frame):
        bullish.append("MACD 回測突破")
    if _cross_up(previous["DIF"], previous["DEA"], latest["DIF"], latest["DEA"]):
        bullish.append("MACD 黃金交叉")
    if _cross_up(previous["K"], previous["D"], latest["K"], latest["D"]):
        bullish.append("KD 黃金交叉")
    if ENABLE_DIVERGENCE_SIGNALS:
        if is_macd_low_divergence(frame):
            bullish.append("MACD 低檔背離")
        if is_kd_low_divergence(frame):
            bullish.append("KD 低檔背離")

    if _cross_down(previous["DIF"], previous["DEA"], latest["DIF"], latest["DEA"]):
        bearish.append("MACD 死亡交叉")
    if _cross_down(previous["K"], previous["D"], latest["K"], latest["D"]):
        bearish.append("KD 死亡交叉")
    if ENABLE_DIVERGENCE_SIGNALS:
        if is_macd_high_divergence(frame):
            bearish.append("MACD 高檔背離")
        if is_kd_high_divergence(frame):
            bearish.append("KD 高檔背離")

    return bullish, bearish


def _add_signal(target: dict[str, dict[str, list[str]]], signal: str, candidate: TechnicalCandidate) -> None:
    item = f"{candidate.code} {candidate.name} ({candidate.price:.1f})"
    target.setdefault(signal, {}).setdefault(candidate.industry or UNCLASSIFIED_INDUSTRY, []).append(item)


def run_technical_scan(scan_settings: dict[str, float] | None = None, report_date: date | None = None) -> TechnicalScanResult:
    label = "技術面選股"
    target_date = report_date or datetime.now().date()
    _print_progress(label, 0.0, "初始化全市場技術面掃描")
    candidates, total_symbols = build_hard_filter_candidates(scan_settings)
    _print_progress(label, 20.0, f"完成硬篩，通過 {len(candidates)}/{total_symbols} 檔")

    bullish: dict[str, dict[str, list[str]]] = {}
    bearish: dict[str, dict[str, list[str]]] = {}
    sources: set[str] = set()
    matched_codes: set[str] = set()

    for index, candidate in enumerate(candidates, start=1):
        progress = 20.0 + index / max(1, len(candidates)) * 70.0
        if index == 1 or index % 10 == 0 or index == len(candidates):
            _print_progress(label, progress, f"計算技術指標 {index}/{len(candidates)} {candidate.code} {candidate.name}")
        history, source = fetch_daily_history(candidate.symbol, target_date)
        if source:
            sources.add(source)
        bullish_signals, bearish_signals = detect_signals(history)
        if bullish_signals or bearish_signals:
            matched_codes.add(candidate.code)
        for signal in bullish_signals:
            _add_signal(bullish, signal, candidate)
        for signal in bearish_signals:
            _add_signal(bearish, signal, candidate)

    _print_progress(label, 100.0, f"完成，符合技術邏輯 {len(matched_codes)} 檔")
    return TechnicalScanResult(
        report_date=target_date,
        total_symbols=total_symbols,
        hard_filter_passed=len(candidates),
        matched_symbols=len(matched_codes),
        bullish=bullish,
        bearish=bearish,
        sources=sources or {"Yahoo Finance", "Fugle", "本機快取"},
    )


def _render_signal_groups(lines: list[str], groups: dict[str, dict[str, list[str]]], order: list[str]) -> None:
    has_any = False
    for signal in order:
        industries = groups.get(signal)
        if not industries:
            continue
        has_any = True
        lines.append(f"📂 {signal}")
        for industry in sorted(industries):
            stocks = " | ".join(sorted(industries[industry]))
            lines.append(f"【{industry}】 {stocks}")
        lines.append("")
    if not has_any:
        lines.append("目前無符合標的。")
        lines.append("")


def format_technical_report(result: TechnicalScanResult) -> str:
    lines = [
        "🔍 今日技術面選股掃描報告",
        f"📅 日期：{result.report_date.isoformat()}",
        "",
        "📌 分類定義說明：",
        "* 正面訊號：包含均線突破及指標金叉，代表短中期趨勢轉強。",
        "* 負面訊號：包含 MACD / KD 指標死叉，代表多頭動能衰退，需留意風險。",
        "* 暫停策略：MACD 回測突破、MACD / KD 高檔背離與低檔背離暫不執行。",
        "",
        "=========================",
        "🟢 【正面訊號標的】",
        "=========================",
    ]
    _render_signal_groups(lines, result.bullish, BULLISH_SIGNAL_ORDER)
    lines.extend(
        [
            "=========================",
            "🔴 【負面訊號標的】",
            "=========================",
        ]
    )
    _render_signal_groups(lines, result.bearish, BEARISH_SIGNAL_ORDER)
    lines.extend(
        [
            "=========================",
            "📊 掃描統計",
            f"* 總掃描範圍：{result.total_symbols} 檔",
            f"* 通過硬篩標的：{result.hard_filter_passed} 檔",
            f"* 符合技術選股邏輯：{result.matched_symbols} 檔",
            f"* 資料日期：{result.report_date.isoformat()}",
            f"* 資料來源：{' / '.join(sorted(result.sources))}",
        ]
    )
    return "\n".join(lines).strip()


def build_technical_scan_report(scan_settings: dict[str, float] | None = None, report_date: date | None = None) -> str:
    return format_technical_report(run_technical_scan(scan_settings, report_date))
