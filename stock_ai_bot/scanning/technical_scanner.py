from __future__ import annotations

import time
import re
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from stock_ai_bot.data_sources.fugle_data import fetch_fugle_history
from stock_ai_bot.scanning.stock_scanner import (
    DEFAULT_SCAN_SETTINGS,
    UNCLASSIFIED_INDUSTRY,
    load_price_metrics,
    load_recent_revenue_history,
    load_stock_universe,
)
from stock_ai_bot.scanning.technical_strategy_engine import detect_technical_strategies

from candidate_filter_service import apply_basic_hard_filter, resolve_hard_filter_settings
from progress_logger import now_timestamp
from stock_ai_bot.telegram.telegram_stock_formatting import mark_stock_text
from stock_ai_bot.scanning.technical_indicator_service import (
    INDICATOR_VERSION,
    KD_D_PERIOD,
    KD_K_PERIOD,
    KD_RSV_PERIOD,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    apply_technical_indicators,
)


ROOT_DIR = Path(__file__).resolve().parent
TECH_CACHE_DIR = ROOT_DIR / ".cache" / "technical_daily"
TECH_CACHE_TTL_SECONDS = 12 * 60 * 60
HISTORY_DAYS = 320
MIN_HISTORY_ROWS = 120
YFINANCE_PAUSE_SECONDS = 0.2

MA_SHORT = 21
MA_LONG = 105
MA_BREAKOUT_PERIODS = (5, 13, 21, 55, 105, 144)
MA_BREAKOUT_SIGNAL_LABELS = {period: f"突破 {period}MA" for period in MA_BREAKOUT_PERIODS}
MA_RECLAIM_SIGNAL_LABELS = {period: f"跌破後收復 {period}MA" for period in MA_BREAKOUT_PERIODS}
MA_SIGNAL_TRIGGER_BREAKOUT = "突破"
MA_SIGNAL_TRIGGER_RECLAIM = "跌破後收復"
ENABLE_MACD_PULLBACK_BREAKOUT = False
ENABLE_DIVERGENCE_SIGNALS = False

BULLISH_MA_SIGNAL_ORDER = [
    label
    for period in MA_BREAKOUT_PERIODS
    for label in (MA_BREAKOUT_SIGNAL_LABELS[period], MA_RECLAIM_SIGNAL_LABELS[period])
]
BULLISH_SIGNAL_ORDER = [
    *BULLISH_MA_SIGNAL_ORDER,
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
TECHNICAL_MESSAGE_MAX_CHARS = 3500


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
    strategy_signals: dict[str, list[dict]] = field(default_factory=dict)
    dual_ma_signals: list[dict[str, Any]] = field(default_factory=list)
    kd_ma_signals: list[dict[str, Any]] = field(default_factory=list)


def _print_progress(label: str, progress: float, message: str) -> None:
    print(f"[{now_timestamp()}] [選股進度][{label}] {progress:.2f}% {message}", flush=True)


def _ensure_cache_dir() -> None:
    TECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(symbol: str) -> Path:
    safe_symbol = symbol.replace(".", "_")
    return TECH_CACHE_DIR / f"{safe_symbol}.csv"


def _cache_meta_path(symbol: str) -> Path:
    return _cache_path(symbol).with_suffix(".meta.json")


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
        "Adj Close": "adj_close",
        "Adj_Close": "adj_close",
        "Volume": "volume",
    }
    history = history.reset_index().rename(columns=rename_map)
    required = ["date", "open", "high", "low", "close", "volume"]
    if not set(required).issubset(history.columns):
        return pd.DataFrame()
    source_history = history
    history = pd.DataFrame(index=source_history.index)
    for column in required:
        history[column] = _column_as_series(source_history, column)
    if "adj_close" in source_history.columns:
        history["adj_close"] = _column_as_series(source_history, "adj_close")
    history["date"] = pd.to_datetime(history["date"], errors="coerce").dt.normalize()
    for column in ["open", "high", "low", "close", "volume"]:
        history[column] = pd.to_numeric(history[column], errors="coerce")
    if "adj_close" in history.columns:
        history["adj_close"] = pd.to_numeric(history["adj_close"], errors="coerce")
    history = history.dropna(subset=["date", "open", "high", "low", "close"])
    return history.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def _column_as_series(frame: pd.DataFrame, column: str) -> pd.Series:
    value = frame[column]
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return pd.Series(pd.NA, index=frame.index)
        value = value.iloc[:, 0]
    if isinstance(value, pd.Series):
        return value
    return pd.Series(value, index=frame.index)


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


def _save_history(symbol: str, history: pd.DataFrame, *, source: str = "") -> None:
    if history.empty:
        return
    _ensure_cache_dir()
    history.to_csv(_cache_path(symbol), index=False, encoding="utf-8")
    _cache_meta_path(symbol).write_text(
        json.dumps(
            {
                "symbol": symbol,
                "source": source,
                "price_basis_source": (
                    "source_ohlc_with_adj_close" if _has_adjusted_history(history) else "source_ohlc_only"
                ),
                "indicator_version": INDICATOR_VERSION,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def fetch_daily_history(
    symbol: str,
    end_date: date,
    *,
    min_rows: int | None = None,
    require_adjusted: bool = False,
) -> tuple[pd.DataFrame, str]:
    if end_date < datetime.now().date():
        from stock_ai_bot.data_sources.historical_price_service import fetch_history as fetch_historical_history

        return fetch_historical_history(
            symbol,
            end_date,
            min_rows=min_rows,
            require_adjusted=require_adjusted,
        )

    require_fresh = end_date >= datetime.now().date()
    cached = _load_cached_history(symbol, require_fresh=require_fresh)
    cached_ready = (
        not cached.empty
        and cached["date"].dt.date.max() >= end_date
        and (min_rows is None or len(cached[cached["date"].dt.date <= end_date]) >= min_rows)
        and (not require_adjusted or _has_adjusted_history(cached))
    )
    if cached_ready:
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
        _save_history(symbol, history, source=source)
    time.sleep(YFINANCE_PAUSE_SECONDS)
    return history, source


def _has_adjusted_history(history: pd.DataFrame, min_ratio: float = 0.9) -> bool:
    if history.empty or "adj_close" not in history.columns:
        return False
    values = pd.to_numeric(history["adj_close"], errors="coerce")
    return bool(len(values) and values.notna().mean() >= min_ratio and (values.dropna() > 0).all())


def build_hard_filter_candidates(
    scan_settings: dict[str, float] | None = None,
    report_date: date | None = None,
    *,
    historical_replay: bool = False,
) -> tuple[list[TechnicalCandidate], int]:
    settings = resolve_hard_filter_settings(scan_settings, defaults=DEFAULT_SCAN_SETTINGS)

    universe = load_stock_universe(False)
    point_in_time_date = report_date if historical_replay else None
    revenue_history = load_recent_revenue_history(universe, as_of_date=point_in_time_date)
    price_metrics = load_price_metrics(universe, as_of_date=point_in_time_date)

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
        hard_filter = apply_basic_hard_filter(
            price=price,
            avg_volume_20d=avg_volume,
            latest_monthly_revenue=latest_revenue,
            settings=settings,
        )
        if not hard_filter.passed:
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
    return apply_technical_indicators(history)


def _cross_up(prev_left: float, prev_right: float, now_left: float, now_right: float) -> bool:
    return pd.notna(prev_left) and pd.notna(prev_right) and pd.notna(now_left) and pd.notna(now_right) and prev_left < prev_right and now_left > now_right


def _cross_down(prev_left: float, prev_right: float, now_left: float, now_right: float) -> bool:
    return pd.notna(prev_left) and pd.notna(prev_right) and pd.notna(now_left) and pd.notna(now_right) and prev_left > prev_right and now_left < now_right


def ma_breakout_signal_label(period: int) -> str:
    return MA_BREAKOUT_SIGNAL_LABELS.get(int(period), f"突破 {int(period)}MA")


def ma_reclaim_signal_label(period: int) -> str:
    return MA_RECLAIM_SIGNAL_LABELS.get(int(period), f"跌破後收復 {int(period)}MA")


def ma_signal_label_from_triggers(period: int, triggers: list[str] | tuple[str, ...] | set[str]) -> str:
    trigger_set = {str(trigger) for trigger in triggers}
    if MA_SIGNAL_TRIGGER_BREAKOUT in trigger_set:
        return ma_breakout_signal_label(period)
    if MA_SIGNAL_TRIGGER_RECLAIM in trigger_set:
        return ma_reclaim_signal_label(period)
    return ma_breakout_signal_label(period)


def detect_ma_breakout_signal_details(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Detect same-day MA breakout or intraday-break reclaim signals."""

    if frame is None or len(frame) < 2:
        return []
    previous = frame.iloc[-2]
    latest = frame.iloc[-1]
    signals: list[dict[str, object]] = []
    for period in MA_BREAKOUT_PERIODS:
        ma_column = f"MA{period}"
        if ma_column not in frame.columns:
            continue
        previous_close = previous.get("close")
        previous_ma = previous.get(ma_column)
        latest_close = latest.get("close")
        latest_low = latest.get("low")
        latest_ma = latest.get(ma_column)
        if not all(pd.notna(value) for value in (previous_close, previous_ma, latest_close, latest_low, latest_ma)):
            continue
        triggers: list[str] = []
        if float(previous_close) <= float(previous_ma) and float(latest_close) > float(latest_ma):
            triggers.append(MA_SIGNAL_TRIGGER_BREAKOUT)
        if float(latest_low) < float(latest_ma) and float(latest_close) > float(latest_ma):
            triggers.append(MA_SIGNAL_TRIGGER_RECLAIM)
        if triggers:
            signals.append(
                {
                    "period": period,
                    "label": ma_signal_label_from_triggers(period, triggers),
                    "triggers": triggers,
                    "ma_value": float(latest_ma),
                    "close": float(latest_close),
                    "low": float(latest_low),
                }
            )
    return signals


def detect_ma_breakout_signals(frame: pd.DataFrame) -> list[str]:
    return [str(item["label"]) for item in detect_ma_breakout_signal_details(frame)]


def _first_ma_trigger_in_above_episode(frame: pd.DataFrame, period: int) -> bool:
    """Allow one signal per uninterrupted close-above-MA episode."""
    column = f"MA{period}"
    for index in range(len(frame) - 2, -1, -1):
        row = frame.iloc[index]
        close, ma = row.get("close"), row.get(column)
        if pd.isna(close) or pd.isna(ma):
            break
        if float(close) <= float(ma):
            return index == len(frame) - 2
        low = row.get("low")
        if pd.notna(low) and float(low) < float(ma):
            return False
    return True


def detect_dual_ma_structure(
    frame: pd.DataFrame, stock_id: str, stock_name: str = ""
) -> list[dict[str, Any]]:
    """Return at most one MA-structure signal for today's price trigger."""
    if frame is None or len(frame) < 2:
        return []
    latest = frame.iloc[-1]
    close = latest.get("close")
    ma5, ma21 = latest.get("MA5"), latest.get("MA21")
    if not all(pd.notna(value) for value in (close, ma5, ma21)):
        return []
    close = float(close)
    long_mas = {
        period: float(latest[f"MA{period}"])
        for period in (105, 144)
        if f"MA{period}" in frame and pd.notna(latest.get(f"MA{period}"))
    }
    if not long_mas:
        return []
    details = {int(item["period"]): item for item in detect_ma_breakout_signal_details(frame)}
    ma5_match = (
        float(ma5) > float(ma21)
        and 5 in details
        and _first_ma_trigger_in_above_episode(frame, 5)
    )
    long_alignment = [period for period, value in long_mas.items() if float(ma21) > value]
    ma21_match = (
        bool(long_alignment)
        and 21 in details
        and _first_ma_trigger_in_above_episode(frame, 21)
    )
    if not ma5_match and not ma21_match:
        return []

    above = [period for period, value in long_mas.items() if close > value]
    below = [period for period, value in long_mas.items() if close < value]
    long_position = "mixed" if above and below else "above" if above else "below" if below else ""
    if ma5_match and not ma21_match and not long_position:
        return []
    matched = [period for period, matches in ((5, ma5_match), (21, ma21_match)) if matches]
    long_relation = "、".join(
        f"{'高於' if close > value else '低於' if close < value else '等於'} MA{period}"
        for period, value in long_mas.items()
    )
    signal = {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "signal_date": pd.Timestamp(latest["date"]).date().isoformat(),
        "close": close,
        "primary_group": "ma21_long" if ma21_match else "ma5_ma21",
        "matched_target_mas": matched,
        "triggers": {f"MA{period}": list(details[period]["triggers"]) for period in matched},
        "aligned_long_mas": long_alignment,
        "long_position": long_position,
        "long_relation": long_relation,
    }
    return [signal]


def detect_kd_ma_strategy(
    frame: pd.DataFrame, stock_id: str, stock_name: str = ""
) -> list[dict[str, Any]]:
    """Detect KD momentum confirmation with a same-day MA5/MA21 trigger."""
    if frame is None or len(frame) < 2:
        return []
    latest = frame.iloc[-1]
    if any(pd.isna(latest.get(column)) for column in ("date", "close", "K", "D")):
        return []

    details = {int(item["period"]): item for item in detect_ma_breakout_signal_details(frame)}
    ma5_match = 5 in details and _first_ma_trigger_in_above_episode(frame, 5)
    ma21_match = 21 in details and _first_ma_trigger_in_above_episode(frame, 21)

    recent_golden_cross_days: int | None = None
    for days_ago in range(1, 9):
        cross_index = len(frame) - 1 - days_ago
        previous_index = cross_index - 1
        if previous_index < 0:
            break
        previous = frame.iloc[previous_index]
        cross_day = frame.iloc[cross_index]
        if _cross_up(previous.get("K"), previous.get("D"), cross_day.get("K"), cross_day.get("D")):
            recent_golden_cross_days = days_ago
            break

    k1_match = ma5_match and recent_golden_cross_days is not None
    k2_match = ma21_match and float(latest["K"]) > float(latest["D"])
    if not k1_match and not k2_match:
        return []

    matched_groups = [group for group, matched in (("K1", k1_match), ("K2", k2_match)) if matched]
    matched_target_mas = [period for period, matched in ((5, k1_match), (21, k2_match)) if matched]
    primary_group = "K2" if k2_match else "K1"
    return [
        {
            "stock_id": stock_id,
            "stock_name": stock_name,
            "signal_date": pd.Timestamp(latest["date"]).date().isoformat(),
            "close": float(latest["close"]),
            "primary_group": primary_group,
            "matched_groups": matched_groups,
            "matched_target_mas": matched_target_mas,
            "triggers": {f"MA{period}": list(details[period]["triggers"]) for period in matched_target_mas},
            "recent_golden_cross_days": recent_golden_cross_days,
            "k": float(latest["K"]),
            "d": float(latest["D"]),
        }
    ]


def kd_ma_signal_label(signal: dict[str, Any]) -> str:
    if signal.get("primary_group") == "K2":
        return "K2 KD 多方排列突破或收復 MA21"
    return "K1 KD 金叉後突破或收復 MA5"


def dual_ma_signal_label(signal: dict[str, Any]) -> str:
    primary_ma = 21 if signal.get("primary_group") == "ma21_long" else 5
    triggers = (signal.get("triggers") or {}).get(f"MA{primary_ma}") or []
    action = "突破" if MA_SIGNAL_TRIGGER_BREAKOUT in triggers else "回踩收復"
    if signal.get("primary_group") == "ma21_long":
        label = f"雙均線｜MA21 高於 MA105 或 MA144｜{action} MA21"
    else:
        position = {"above": "長均線上方", "below": "長均線下方", "mixed": "長均線交錯"}.get(
            signal.get("long_position"), "長均線位置未明"
        )
        label = f"雙均線｜MA5 高於 MA21｜{position}｜{action} MA5"
    if len(signal.get("matched_target_mas") or []) > 1:
        label += "（同步命中 MA5）"
    return label


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

    bullish.extend(detect_ma_breakout_signals(frame))
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


def collect_technical_selected_codes(result: TechnicalScanResult) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        code = str(value or "").strip().split(maxsplit=1)[0]
        if not re.fullmatch(r"\d{4}", code) or code in seen:
            return
        seen.add(code)
        codes.append(code)

    for groups in (result.bullish, result.bearish):
        for industries in groups.values():
            for displays in industries.values():
                for display in displays:
                    add(display)
    for signals in result.strategy_signals.values():
        for signal in signals:
            add(signal.get("stock_id") or signal.get("code"))
    for signal in result.dual_ma_signals:
        add(signal.get("stock_id") or signal.get("code"))
    for signal in result.kd_ma_signals:
        add(signal.get("stock_id") or signal.get("code"))
    return codes


def run_technical_scan(
    scan_settings: dict[str, float] | None = None,
    report_date: date | None = None,
    *,
    historical_replay: bool = False,
) -> TechnicalScanResult:
    label = "技術面選股"
    target_date = report_date or datetime.now().date()
    _print_progress(label, 0.0, "初始化全市場技術面掃描")
    candidates, total_symbols = build_hard_filter_candidates(
        scan_settings,
        target_date,
        historical_replay=historical_replay,
    )
    _print_progress(label, 20.0, f"完成硬篩，通過 {len(candidates)}/{total_symbols} 檔")

    bullish: dict[str, dict[str, list[str]]] = {}
    bearish: dict[str, dict[str, list[str]]] = {}
    sources: set[str] = set()
    matched_codes: set[str] = set()
    strategy_signals: dict[str, list[dict]] = {"A": [], "B": [], "C": [], "D": []}
    dual_ma_signals: list[dict[str, Any]] = []
    kd_ma_signals: list[dict[str, Any]] = []

    for index, candidate in enumerate(candidates, start=1):
        progress = 20.0 + index / max(1, len(candidates)) * 70.0
        if index == 1 or index % 10 == 0 or index == len(candidates):
            _print_progress(label, progress, f"計算技術指標 {index}/{len(candidates)} {candidate.code} {candidate.name}")
        history, source = fetch_daily_history(candidate.symbol, target_date, require_adjusted=True)
        if source:
            sources.add(source)
        bullish_signals, bearish_signals = detect_signals(history)
        if bullish_signals or bearish_signals:
            matched_codes.add(candidate.code)
        for signal in bullish_signals:
            _add_signal(bullish, signal, candidate)
        for signal in bearish_signals:
            _add_signal(bearish, signal, candidate)

        # --- Strategy detection (A/B/C/D and dual-MA structure) ---
        strategy_progress = 90.0 + index / max(1, len(candidates)) * 5.0
        if index == 1 or index % 10 == 0 or index == len(candidates):
            _print_progress(label, strategy_progress, f"偵測技術策略 {index}/{len(candidates)} {candidate.code}")
        if history.empty or "close" not in history.columns:
            continue
        frame_with_indicators = apply_indicators(history).dropna(subset=["close"]).reset_index(drop=True)
        if len(frame_with_indicators) < MIN_HISTORY_ROWS:
            continue
        strat_sigs = detect_technical_strategies(frame_with_indicators, candidate.code, candidate.name)
        for sig in strat_sigs:
            sig["industry"] = candidate.industry or UNCLASSIFIED_INDUSTRY
            code = sig.get("strategy_code")
            if code in strategy_signals:
                strategy_signals[code].append(sig)
        if strat_sigs:
            matched_codes.add(candidate.code)
        dual_sigs = detect_dual_ma_structure(frame_with_indicators, candidate.code, candidate.name)
        for sig in dual_sigs:
            sig["industry"] = candidate.industry or UNCLASSIFIED_INDUSTRY
            dual_ma_signals.append(sig)
        if dual_sigs:
            matched_codes.add(candidate.code)
        kd_sigs = detect_kd_ma_strategy(frame_with_indicators, candidate.code, candidate.name)
        for sig in kd_sigs:
            sig["industry"] = candidate.industry or UNCLASSIFIED_INDUSTRY
            kd_ma_signals.append(sig)
        if kd_sigs:
            matched_codes.add(candidate.code)

    _print_progress(label, 95.0, "技術策略偵測完成")
    _print_progress(label, 100.0, f"完成，符合技術邏輯 {len(matched_codes)} 檔")
    return TechnicalScanResult(
        report_date=target_date,
        total_symbols=total_symbols,
        hard_filter_passed=len(candidates),
        matched_symbols=len(matched_codes),
        bullish=bullish,
        bearish=bearish,
        sources=sources or {"Yahoo Finance", "Fugle", "本機快取"},
        strategy_signals=strategy_signals,
        dual_ma_signals=dual_ma_signals,
        kd_ma_signals=kd_ma_signals,
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
            stocks = " | ".join(_mark_stock_member(item) for item in sorted(industries[industry]))
            lines.append(f"【{industry}】 {stocks}")
        lines.append("")
    if not has_any:
        lines.append("目前無符合標的。")
        lines.append("")


def _count_signal_group_stocks(industries: dict[str, list[str]]) -> int:
    return sum(len(stocks) for stocks in industries.values())


def _signal_group_body_lines(industries: dict[str, list[str]]) -> list[str]:
    lines: list[str] = []
    for industry in sorted(industries):
        stocks = sorted(industries[industry])
        if not stocks:
            continue
        lines.append(f"【{industry}】")
        lines.extend(_mark_stock_member(item) for item in stocks)
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _technical_message_header(title: str, total_count: int, report_date: date, page: int, total_pages: int) -> str:
    return f"📂 {title}｜共 {total_count} 檔｜第 {page}/{total_pages} 則\n資料日期：{report_date.isoformat()}"


def _paginate_technical_body(
    *,
    title: str,
    total_count: int,
    report_date: date,
    body_lines: list[str],
    max_chars: int,
) -> list[str]:
    safe_max_chars = max(500, int(max_chars or TECHNICAL_MESSAGE_MAX_CHARS))
    chunks: list[list[str]] = []
    current: list[str] = []
    reserve_header = _technical_message_header(title, total_count, report_date, 999, 999)
    reserve_len = len(reserve_header) + 2
    for line in body_lines:
        candidate = [*current, line]
        candidate_text = "\n".join(candidate).strip()
        if current and reserve_len + len(candidate_text) > safe_max_chars:
            chunks.append(current)
            current = [line]
        else:
            current = candidate
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [[]]

    total_pages = len(chunks)
    messages: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        header = _technical_message_header(title, total_count, report_date, index, total_pages)
        body = "\n".join(chunk).strip()
        messages.append(f"{header}\n\n{body}".strip())
    return messages


def _signal_group_messages(
    *,
    result: TechnicalScanResult,
    signal: str,
    industries: dict[str, list[str]],
    max_chars: int,
) -> list[str]:
    total_count = _count_signal_group_stocks(industries)
    if total_count <= 0:
        return []
    return _paginate_technical_body(
        title=signal,
        total_count=total_count,
        report_date=result.report_date,
        body_lines=_signal_group_body_lines(industries),
        max_chars=max_chars,
    )


def _strategy_stock_count(signals: list[dict]) -> int:
    return len({sig.get("stock_id") or i for i, sig in enumerate(signals)})


def _strategy_lines(code: str, label: str, signals: list[dict]) -> list[str]:
    if not signals:
        return ["", f"策略 {code}：無符合標的", ""]

    # Group signals by sub_signal_type, preserving order
    from collections import OrderedDict
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for sig in signals:
        sub = sig.get("sub_signal_type", "")
        if sub not in groups:
            groups[sub] = []
        groups[sub].append(sig)

    lines = ["", f"策略 {code}：{label}", ""]
    for sub, sigs in groups.items():
        sub_label = STRATEGY_SUB_SIGNAL_LABELS.get(sub, sub)
        lines.append(f"{sub_label}")
        lines.append("")
        # Group stocks by industry within this sub-signal group
        industry_stocks: dict[str, list[str]] = {}
        for sig in sigs:
            industry = sig.get("industry") or UNCLASSIFIED_INDUSTRY
            close = sig.get("close", 0)
            note_summary = _format_strategy_signal_summary(sig)
            stock_id = sig.get("stock_id", "")
            stock_name = sig.get("stock_name", "")
            stock_label = mark_stock_text(f"{stock_id} {stock_name}".strip())
            if note_summary:
                item = f"{stock_label} ({close:.1f})｜{note_summary}"
            else:
                item = f"{stock_label} ({close:.1f})"
            industry_stocks.setdefault(industry, []).append(item)
        for industry in sorted(industry_stocks):
            lines.append(f"[{industry}]")
            lines.extend(industry_stocks[industry])
            lines.append("")
        lines.append("")

    return lines


def _mark_stock_member(member: object) -> str:
    text = str(member or "").strip()
    if not text:
        return ""
    if " (" in text:
        stock_text, suffix = text.split(" (", 1)
        return f"{mark_stock_text(stock_text)} ({suffix}"
    return mark_stock_text(text)


def _strategy_body_lines(code: str, label: str, signals: list[dict]) -> list[str]:
    body: list[str] = []
    header = f"策略 {code}：{label}"
    for line in _strategy_lines(code, label, signals):
        text = str(line or "").strip()
        if text == header:
            continue
        if not text:
            if body and body[-1] != "":
                body.append("")
            continue
        body.append(text)
    while body and body[-1] == "":
        body.pop()
    return body


DUAL_MA_GROUP_LABELS = {
    "ma21_long": "MA21 > MA105 或 MA144",
    "above": "MA5 > MA21｜長均線上方",
    "mixed": "MA5 > MA21｜長均線交錯",
    "below": "MA5 > MA21｜長均線下方",
}

KD_MA_GROUP_LABELS = {
    "K1": "K1｜KD 金叉後 MA5 轉強",
    "K2": "K2｜KD 多方排列突破或收復 MA21",
}


def _dual_ma_signal_group_and_summary(signal: dict[str, Any]) -> tuple[str, str]:
    key = "ma21_long" if signal.get("primary_group") == "ma21_long" else str(signal.get("long_position") or "")
    if key not in DUAL_MA_GROUP_LABELS:
        return "", ""
    primary_ma = 21 if key == "ma21_long" else 5
    triggers = (signal.get("triggers") or {}).get(f"MA{primary_ma}") or []
    action = "突破" if MA_SIGNAL_TRIGGER_BREAKOUT in triggers else "回踩收復"
    summary = f"今日{action} MA{primary_ma}"
    if len(signal.get("matched_target_mas") or []) > 1:
        summary += "、同步命中 MA5"
    if key != "ma21_long" and signal.get("long_relation"):
        summary += f"、{signal['long_relation']}"
    return key, summary


def _dual_ma_body_lines(signals: list[dict[str, Any]]) -> list[str]:
    grouped: dict[str, dict[str, list[str]]] = {}
    for signal in signals:
        key, summary = _dual_ma_signal_group_and_summary(signal)
        if not key:
            continue
        industry = str(signal.get("industry") or UNCLASSIFIED_INDUSTRY)
        stock = mark_stock_text(f"{signal.get('stock_id', '')} {signal.get('stock_name', '')}".strip())
        line = f"{stock} ({float(signal.get('close') or 0):.1f})｜{summary}"
        grouped.setdefault(key, {}).setdefault(industry, []).append(line)

    lines: list[str] = []
    for key in ("ma21_long", "above", "mixed", "below"):
        industries = grouped.get(key)
        if not industries:
            continue
        lines.extend([DUAL_MA_GROUP_LABELS[key], ""])
        for industry in sorted(industries):
            lines.append(f"[{industry}]")
            lines.extend(industries[industry])
            lines.append("")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _kd_ma_signal_group_and_summary(signal: dict[str, Any]) -> tuple[str, str]:
    group = str(signal.get("primary_group") or "")
    if group not in KD_MA_GROUP_LABELS:
        return "", ""
    target_ma = 21 if group == "K2" else 5
    triggers = (signal.get("triggers") or {}).get(f"MA{target_ma}") or []
    action = "突破" if MA_SIGNAL_TRIGGER_BREAKOUT in triggers else "回踩收復"
    if group == "K1":
        summary = f"KD 金叉後 {int(signal.get('recent_golden_cross_days') or 0)} 日、今日{action} MA5"
    else:
        summary = f"K {float(signal.get('k') or 0):.2f} > D {float(signal.get('d') or 0):.2f}、今日{action} MA21"
        if "K1" in (signal.get("matched_groups") or []):
            days = int(signal.get("recent_golden_cross_days") or 0)
            summary += f"、同步符合 K1（金叉後 {days} 日）"
    return group, summary


def _kd_ma_body_lines(signals: list[dict[str, Any]]) -> list[str]:
    grouped: dict[str, dict[str, list[str]]] = {}
    for signal in signals:
        group, summary = _kd_ma_signal_group_and_summary(signal)
        if not group:
            continue
        industry = str(signal.get("industry") or UNCLASSIFIED_INDUSTRY)
        stock = mark_stock_text(f"{signal.get('stock_id', '')} {signal.get('stock_name', '')}".strip())
        line = f"{stock} ({float(signal.get('close') or 0):.1f})｜{summary}"
        grouped.setdefault(group, {}).setdefault(industry, []).append(line)

    lines: list[str] = []
    for group in ("K1", "K2"):
        industries = grouped.get(group)
        if not industries:
            continue
        lines.extend([KD_MA_GROUP_LABELS[group], ""])
        for industry in sorted(industries):
            lines.append(f"[{industry}]")
            lines.extend(industries[industry])
            lines.append("")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _parse_technical_stock_display(display: object) -> tuple[str, str, float | None]:
    text = str(display or "").strip()
    match = re.fullmatch(r"(\d{4})\s+(.+?)\s+\((-?\d+(?:\.\d+)?)\)", text)
    if not match:
        return "", "", None
    return match.group(1), match.group(2).strip(), float(match.group(3))


def _append_unique_reason(record: dict[str, Any], category: str, reason: str) -> None:
    text = str(reason or "").strip()
    if text and text not in record[category]:
        record[category].append(text)


def _build_technical_dedup_records(result: TechnicalScanResult) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}

    def ensure_record(
        code: object,
        *,
        name: object = "",
        price: object = None,
        industry: object = "",
    ) -> dict[str, Any] | None:
        stock_id = str(code or "").strip()
        if not re.fullmatch(r"\d{4}", stock_id):
            return None
        record = records.setdefault(
            stock_id,
            {
                "stock_id": stock_id,
                "stock_name": "",
                "close": None,
                "industry": UNCLASSIFIED_INDUSTRY,
                "positive_reasons": [],
                "negative_reasons": [],
            },
        )
        stock_name = str(name or "").strip()
        if stock_name:
            record["stock_name"] = stock_name
        try:
            if price is not None:
                record["close"] = float(price)
        except (TypeError, ValueError):
            pass
        stock_industry = str(industry or "").strip()
        if stock_industry and stock_industry != UNCLASSIFIED_INDUSTRY:
            record["industry"] = stock_industry
        return record

    for category, groups in (("positive_reasons", result.bullish), ("negative_reasons", result.bearish)):
        for reason, industries in groups.items():
            for industry, displays in industries.items():
                for display in displays:
                    code, name, price = _parse_technical_stock_display(display)
                    record = ensure_record(code, name=name, price=price, industry=industry)
                    if record is not None:
                        _append_unique_reason(record, category, reason)

    for strategy_code, signals in result.strategy_signals.items():
        for signal in signals:
            record = ensure_record(
                signal.get("stock_id") or signal.get("code"),
                name=signal.get("stock_name"),
                price=signal.get("close"),
                industry=signal.get("industry"),
            )
            if record is None:
                continue
            label = STRATEGY_SUB_SIGNAL_LABELS.get(
                str(signal.get("sub_signal_type") or ""),
                f"策略 {strategy_code} 訊號",
            )
            summary = _format_strategy_signal_summary(signal)
            reason = f"{label}（{summary}）" if summary else label
            _append_unique_reason(record, "positive_reasons", reason)

    for signal in result.dual_ma_signals:
        record = ensure_record(
            signal.get("stock_id") or signal.get("code"),
            name=signal.get("stock_name"),
            price=signal.get("close"),
            industry=signal.get("industry"),
        )
        key, summary = _dual_ma_signal_group_and_summary(signal)
        if record is not None and key:
            reason = f"雙均線｜{DUAL_MA_GROUP_LABELS[key]}（{summary}）"
            _append_unique_reason(record, "positive_reasons", reason)

    for signal in result.kd_ma_signals:
        record = ensure_record(
            signal.get("stock_id") or signal.get("code"),
            name=signal.get("stock_name"),
            price=signal.get("close"),
            industry=signal.get("industry"),
        )
        group, summary = _kd_ma_signal_group_and_summary(signal)
        if record is not None and group:
            reason = f"{KD_MA_GROUP_LABELS[group]}（{summary}）"
            _append_unique_reason(record, "positive_reasons", reason)

    return records


def _technical_dedup_body_lines(records: dict[str, dict[str, Any]]) -> list[str]:
    section_specs = (
        ("多方／策略觸發", lambda item: bool(item["positive_reasons"]) and not item["negative_reasons"]),
        ("多空訊號並存", lambda item: bool(item["positive_reasons"]) and bool(item["negative_reasons"])),
        ("純風險訊號", lambda item: bool(item["negative_reasons"]) and not item["positive_reasons"]),
    )
    lines: list[str] = []
    for section_label, predicate in section_specs:
        section_records = [record for record in records.values() if predicate(record)]
        if not section_records:
            continue
        lines.extend([f"【{section_label}】", ""])
        industries = sorted({str(record["industry"] or UNCLASSIFIED_INDUSTRY) for record in section_records})
        for industry in industries:
            lines.append(f"[{industry}]")
            industry_records = sorted(
                (record for record in section_records if str(record["industry"] or UNCLASSIFIED_INDUSTRY) == industry),
                key=lambda record: record["stock_id"],
            )
            for record in industry_records:
                stock = mark_stock_text(f"{record['stock_id']} {record['stock_name']}".strip())
                close = record.get("close")
                stock_display = f"{stock} ({float(close):.1f})" if close is not None else stock
                positive = "；".join(record["positive_reasons"])
                negative = "；".join(record["negative_reasons"])
                if positive and negative:
                    lines.append(f"{stock_display}｜多方：{positive}｜風險：{negative}")
                elif positive:
                    lines.append(f"{stock_display}｜觸發：{positive}")
                else:
                    lines.append(f"{stock_display}｜風險：{negative}")
            lines.append("")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _technical_dedup_summary_messages(
    result: TechnicalScanResult,
    *,
    max_chars: int,
) -> list[str]:
    records = _build_technical_dedup_records(result)
    if not records:
        return []
    return _paginate_technical_body(
        title="技術選股去重彙整",
        total_count=len(records),
        report_date=result.report_date,
        body_lines=_technical_dedup_body_lines(records),
        max_chars=max_chars,
    )


def format_technical_report_messages(
    result: TechnicalScanResult,
    *,
    max_chars: int = TECHNICAL_MESSAGE_MAX_CHARS,
) -> list[str]:
    messages: list[str] = []
    for signal in BULLISH_SIGNAL_ORDER:
        industries = result.bullish.get(signal)
        if industries:
            messages.extend(_signal_group_messages(result=result, signal=signal, industries=industries, max_chars=max_chars))
    for signal in BEARISH_SIGNAL_ORDER:
        industries = result.bearish.get(signal)
        if industries:
            messages.extend(_signal_group_messages(result=result, signal=signal, industries=industries, max_chars=max_chars))

    strategy_blocks = [
        ("A", "多頭延續回檔突破", result.strategy_signals.get("A", [])),
        ("B", "強勢紅柱回測突破", result.strategy_signals.get("B", [])),
        ("C", "低檔背離反轉突破", result.strategy_signals.get("C", [])),
        ("D", "動能背景短線轉強", result.strategy_signals.get("D", [])),
    ]
    for code, label, signals in strategy_blocks:
        if not signals:
            continue
        messages.extend(
            _paginate_technical_body(
                title=f"MACD 動能策略（A–D）｜策略 {code}：{label}",
                total_count=_strategy_stock_count(signals),
                report_date=result.report_date,
                body_lines=_strategy_body_lines(code, label, signals),
                max_chars=max_chars,
            )
        )

    if result.dual_ma_signals:
        messages.extend(
            _paginate_technical_body(
                title="雙均線結構策略",
                total_count=len({signal.get("stock_id") for signal in result.dual_ma_signals}),
                report_date=result.report_date,
                body_lines=_dual_ma_body_lines(result.dual_ma_signals),
                max_chars=max_chars,
            )
        )

    if result.kd_ma_signals:
        messages.extend(
            _paginate_technical_body(
                title="KD 動能均線策略",
                total_count=len({signal.get("stock_id") for signal in result.kd_ma_signals}),
                report_date=result.report_date,
                body_lines=_kd_ma_body_lines(result.kd_ma_signals),
                max_chars=max_chars,
            )
        )

    messages.extend(_technical_dedup_summary_messages(result, max_chars=max_chars))

    if not messages:
        return [
            "\n".join(
                [
                    "🔍 今日技術面選股掃描報告",
                    f"📅 日期：{result.report_date.isoformat()}",
                    "",
                    "目前沒有符合技術條件股票。",
                ]
            )
        ]
    return messages


STRATEGY_SUB_SIGNAL_LABELS: dict[str, str] = {
    "A1_direct_ma21_breakout": "A1｜直接突破型",
    "A2_pivot_low_reclaim_ma21": "A2｜轉折不破低再站上 21MA",
    "A3_reclaim_ma21_and_long_ma": "A3｜同日收復 21MA 與長均線",
    "B1_intraday_retest_reclaim_ma": "B1｜當日低點碰觸 MA5/MA13/MA21 後收復",
    "B2_short_reclaim_after_break_ma": "B2｜紅柱期間收盤突破 MA5/MA13/MA21",
    "B3_breakout_after_retest": "B3｜回測 MA13/MA21 後突破前高",
    "C1_macd_bullish_divergence_break_ma21": "C1｜MACD 低檔背離突破 21MA",
    "C2_below_zero_red_histogram_breakout": "C2｜0軸下紅柱鈍化突破",
    "D1_above_zero_short_ma_reclaim": "D1｜零軸上短均線收復",
    "D2_below_zero_short_ma_reclaim": "D2｜零軸下短均線收復",
    "D3_kd_death_cross_first_reversal": "D3｜KD 死叉後首次轉強",
}


def _format_strategy_signal_summary(sig: dict) -> str:
    """From sig features/notes, build Chinese summary without raw English."""
    feat = sig.get("features", {})
    notes = sig.get("notes", "")

    parts = []

    # wave_return -> 前波漲幅
    if "wave_return" in notes:
        import re
        m = re.search(r"wave_return=([0-9.]+)%", notes)
        if m:
            parts.append(f"前波漲幅 {m.group(1)}%")

    # retracement / retracement_ratio
    rr = feat.get("retracement_ratio", None)
    if rr is not None:
        if rr < 0:
            parts.append("回檔比例資料異常，需人工確認")
        else:
            parts.append(f"回檔比例 {rr:.0%}")

    # pivot low
    if "pivot_low" in feat:
        parts.append(f"轉折低點 {feat['pivot_low']:.2f}")

    # ma105/ma144 reclaimed
    if feat.get("ma105_reclaimed"):
        parts.append("收復 MA105")
    if feat.get("ma144_reclaimed"):
        parts.append("收復 MA144")

    # ma105/ma144 broken in pullback
    if feat.get("ma105_broken_in_pullback"):
        parts.append("回檔中曾跌破 MA105")
    if feat.get("ma144_broken_in_pullback"):
        parts.append("回檔中曾跌破 MA144")

    # ma5/13/21 broken
    if feat.get("ma5_broken"):
        parts.append("突破 MA5")
    if feat.get("ma13_broken"):
        parts.append("突破 MA13")
    if feat.get("ma21_broken"):
        parts.append("突破 MA21")

    if sig.get("sub_signal_type") == "B1_intraday_retest_reclaim_ma" and feat.get("retest_ma"):
        parts.append(f"碰觸並收復 {feat['retest_ma']}")
    if sig.get("sub_signal_type") == "B2_short_reclaim_after_break_ma" and feat.get("crossed_mas"):
        parts.append(f"今日收盤突破 {'、'.join(feat['crossed_mas'])}")

    if sig.get("sub_signal_type") in {"D1_above_zero_short_ma_reclaim", "D2_below_zero_short_ma_reclaim"}:
        mas = feat.get("reclaimed_mas") or []
        if mas:
            parts.append(f"收復 {'、'.join(mas)}")
        shadow_mas = feat.get("long_lower_shadow_mas") or []
        if shadow_mas:
            parts.append(f"長下影回踩 {'、'.join(shadow_mas)}")
        if sig.get("sub_signal_type") == "D2_below_zero_short_ma_reclaim":
            parts.append("近 5 日曾有 MACD 紅柱")
        if feat.get("dif_zero_origin"):
            direction = "上" if feat["dif_zero_origin"] == "above" else "下"
            parts.append(f"DIF 由{direction}方到達零軸")

    return "、".join(parts)


def format_technical_report(result: TechnicalScanResult) -> str:
    lines = [
        "🔍 今日技術面選股掃描報告",
        f"📅 日期：{result.report_date.isoformat()}",
        "",
        "📌 分類定義說明：",
        "* 正面訊號：包含均線突破及指標金叉，代表短中期趨勢轉強。",
        "* 負面訊號：包含 MACD / KD 指標死叉，代表多頭動能衰退，需留意風險。",
        "* MACD 動能策略（A–D）：依波段、紅柱或 DIF 動能判斷，部分訊號搭配 KD 與均線。",
        "* 雙均線結構策略：觀察均線排列與股價當日首次突破或收復。",
        "* KD 動能均線策略：結合 KD 9/9/55 狀態與 MA5／MA21 當日轉強。",
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

    # --- MACD 動能策略區塊 ---
    strategy_blocks = [
        ("A", "多頭延續回檔突破", result.strategy_signals.get("A", [])),
        ("B", "強勢紅柱回測突破", result.strategy_signals.get("B", [])),
        ("C", "低檔背離反轉突破", result.strategy_signals.get("C", [])),
        ("D", "動能背景短線轉強", result.strategy_signals.get("D", [])),
    ]
    lines.extend(
        [
            "",
            "=========================",
            "📈 【MACD 動能策略（A–D）】",
            "=========================",
        ]
    )
    for code, label, signals in strategy_blocks:
        lines.extend(_strategy_lines(code, label, signals))
        lines.append("")

    lines.extend(["", "=========================", "📈 【雙均線結構策略】", "=========================", ""])
    lines.extend(_dual_ma_body_lines(result.dual_ma_signals) or ["目前無符合標的。"])
    lines.append("")

    lines.extend(["", "=========================", "📈 【KD 動能均線策略】", "=========================", ""])
    lines.extend(_kd_ma_body_lines(result.kd_ma_signals) or ["目前無符合標的。"])
    lines.append("")

    lines.extend(
        [
            "=========================",
            "📊 掃描統計",
            f"* 總掃描範圍：{result.total_symbols} 檔",
            f"* 通過硬篩標的：{result.hard_filter_passed} 檔",
            f"* 符合技術選股邏輯：{result.matched_symbols} 檔",
            f"* 策略 A：{_strategy_stock_count(result.strategy_signals.get('A', []))} 檔",
            f"* 策略 B：{_strategy_stock_count(result.strategy_signals.get('B', []))} 檔",
            f"* 策略 C：{_strategy_stock_count(result.strategy_signals.get('C', []))} 檔",
            f"* 策略 D：{_strategy_stock_count(result.strategy_signals.get('D', []))} 檔",
            f"* 雙均線結構：{len({signal.get('stock_id') for signal in result.dual_ma_signals})} 檔",
            f"* KD 動能均線策略：{len({signal.get('stock_id') for signal in result.kd_ma_signals})} 檔",
            f"* 資料日期：{result.report_date.isoformat()}",
            f"* 資料來源：{' / '.join(sorted(result.sources))}",
        ]
    )
    return "\n".join(lines).strip()


def build_technical_scan_report(
    scan_settings: dict[str, float] | None = None,
    report_date: date | None = None,
    *,
    historical_replay: bool = False,
) -> str:
    return format_technical_report(
        run_technical_scan(scan_settings, report_date, historical_replay=historical_replay)
    )


def build_technical_scan_messages(
    scan_settings: dict[str, float] | None = None,
    report_date: date | None = None,
    *,
    max_chars: int = TECHNICAL_MESSAGE_MAX_CHARS,
    historical_replay: bool = False,
) -> list[str]:
    return format_technical_report_messages(
        run_technical_scan(scan_settings, report_date, historical_replay=historical_replay),
        max_chars=max_chars,
    )
