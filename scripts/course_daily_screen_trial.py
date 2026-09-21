"""Causal daily-K course screening trial for curated Taiwan stock candidates.

This is an isolated research adapter.  It does not modify the production
one-minute futures monitor, fetch network data, or place orders.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trade_monitor.pivot_replay import (
    Candle,
    detect_local_pivots,
    dow_state,
    pair_pivots,
    secondary_pivots,
)


TAIPEI = ZoneInfo("Asia/Taipei")
CURATED_ALIASES = {"精選選股", "精選", "curated"}
METHOD_VERSION = "course-daily-screen-trial-v2-integrated-courses-taiji"
CURATED_LINE_RE = re.compile(
    r"(?m)^(?P<code>\d{4,6})\s+(?P<name>[^|\r\n]+?)\s*\|\s*"
    r"(?P<total>\d+)分（技術(?P<technical>\d+)/營收(?P<revenue>\d+)/"
    r"財報(?P<financial>\d+)/籌碼(?P<chip>\d+)/題材(?P<theme>\d+)/"
    r"族群(?P<sector>\d+)）"
)


@dataclass(frozen=True)
class TrialThresholds:
    pivot_n: int = 2
    recent_window: int = 10
    breakout_window: int = 20
    defense_lookback: int = 90
    quadrant_axis_margin: float = 0.05
    risk_max_pct: float = 10.0
    risk_max_atr: float = 2.5
    no_chase_risk_pct: float = 12.0
    no_chase_risk_atr: float = 3.0
    no_chase_extension_atr: float = 2.0
    no_chase_signal_range_atr: float = 2.0
    next_session_chase_cap_atr: float = 0.5
    market_min_rows: int = 60
    market_optimistic_score: float = 65.0
    market_positive_score: float = 55.0
    market_neutral_score: float = 45.0
    market_cautious_score: float = 35.0
    liquidity_min_avg_volume_lots: float = 500.0
    laoxiao_stage_lookback: int = 120
    taiji_anchor_lookback: int = 120
    taiji_anchor_min_atr: float = 1.5
    taiji_anchor_min_efficiency: float = 0.55
    taiji_correction_strong_ratio: float = 0.382
    taiji_correction_acceptable_ratio: float = 0.618
    taiji_correction_failure_ratio: float = 0.70
    taiji_copy_acceptable_amplitude_ratio: float = 0.75
    taiji_copy_acceptable_slope_ratio: float = 0.80


THRESHOLDS = TrialThresholds()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _parse_datetime(value: Any) -> datetime:
    text = str(value or "")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.min


def _latest_curated_record(records: list[dict[str, Any]], target: date) -> dict[str, Any]:
    matches = [
        item
        for item in records
        if isinstance(item, dict)
        and str(item.get("scan_type") or "") in CURATED_ALIASES
        and str(item.get("report_date") or "") == target.isoformat()
        and int(item.get("candidate_count") or 0) > 0
        and item.get("selected_codes")
    ]
    if not matches:
        raise ValueError(f"找不到 {target.isoformat()} 的有效精選選股紀錄")
    return max(matches, key=lambda item: _parse_datetime(item.get("created_at")))


def _latest_radar_record(records: list[dict[str, Any]], target: date) -> dict[str, Any] | None:
    matches = [
        item
        for item in records
        if isinstance(item, dict)
        and str(item.get("report_date") or "") == target.isoformat()
        and int(item.get("candidate_count") or 0) > 0
        and isinstance(item.get("candidate_snapshot"), list)
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: _parse_datetime(item.get("created_at")))


def _parse_curated_summary(summary: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for match in CURATED_LINE_RE.finditer(summary):
        values = match.groupdict()
        code = values["code"]
        if code in result:
            continue
        result[code] = {
            "code": code,
            "name": values["name"].strip(),
            "total_score": int(values["total"]),
            "score_components": {
                "technical": int(values["technical"]),
                "revenue": int(values["revenue"]),
                "financial": int(values["financial"]),
                "chip": int(values["chip"]),
                "theme": int(values["theme"]),
                "sector": int(values["sector"]),
            },
        }
    return result


def _stock_map() -> dict[str, dict[str, Any]]:
    payload = _read_json(ROOT / "stock_list.json")
    return {
        str(item.get("code") or ""): item
        for item in payload.get("stocks", [])
        if isinstance(item, dict) and item.get("code")
    }


def _load_daily_frame(symbol: str, target: date) -> tuple[pd.DataFrame, Path]:
    path = ROOT / ".cache" / "technical_daily" / f"{symbol.replace('.', '_')}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    frame = pd.read_csv(path)
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path.name} 缺少欄位：{sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = (
        frame.dropna(subset=["date", "open", "high", "low", "close"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
    )
    frame = frame[frame["date"].dt.date <= target].reset_index(drop=True)
    if frame.empty or frame.iloc[-1]["date"].date() != target:
        actual = None if frame.empty else frame.iloc[-1]["date"].date().isoformat()
        raise ValueError(f"{symbol} 日 K 未更新至 {target.isoformat()}（目前 {actual}）")
    if len(frame) < 145:
        raise ValueError(f"{symbol} 僅有 {len(frame)} 根日 K，不足以計算 144 日均線")
    return _add_indicators(frame), path


def _add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for period in (5, 13, 21, 55, 60, 105, 144):
        result[f"MA{period}"] = result["close"].rolling(period).mean()
    previous_close = result["close"].shift(1)
    result["TR"] = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["ATR14"] = result["TR"].rolling(14).mean()
    result["ATR_PCT"] = result["ATR14"] / result["close"] * 100.0
    result["VOL_MA20"] = result["volume"].rolling(20).mean()
    return result


def _market_breadth(target: date, thresholds: TrialThresholds) -> dict[str, Any]:
    """Build an Elson-style five-level environment from local breadth.

    The repository does not currently retain a point-in-time TAIEX series.  The
    breadth proxy is therefore explicit about its sample and must not be
    presented as the official index state.
    """

    observations: list[dict[str, float]] = []
    for path in sorted((ROOT / ".cache" / "technical_daily").glob("*.csv")):
        try:
            frame = pd.read_csv(path, usecols=["date", "close"])
        except (OSError, ValueError, pd.errors.ParserError):
            continue
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = (
            frame.dropna(subset=["date", "close"])
            .sort_values("date")
            .drop_duplicates("date", keep="last")
        )
        frame = frame[frame["date"].dt.date <= target]
        if len(frame) < thresholds.market_min_rows or frame.iloc[-1]["date"].date() != target:
            continue
        close = float(frame.iloc[-1]["close"])
        ma20 = float(frame["close"].iloc[-20:].mean())
        ma60 = float(frame["close"].iloc[-60:].mean())
        return20 = (close / float(frame.iloc[-21]["close"]) - 1.0) * 100.0
        observations.append(
            {
                "above_ma20": float(close > ma20),
                "above_ma60": float(close > ma60),
                "positive_return20": float(return20 > 0),
                "return20": return20,
            }
        )

    if not observations:
        return {
            "level": "UNKNOWN",
            "label": "資料不足",
            "breadth_score": None,
            "sample_size": 0,
            "new_entry_policy": "REVIEW_MANUALLY",
            "method": "local-stock-breadth-proxy-v1",
        }

    count = len(observations)
    above20 = sum(item["above_ma20"] for item in observations) / count * 100.0
    above60 = sum(item["above_ma60"] for item in observations) / count * 100.0
    positive20 = sum(item["positive_return20"] for item in observations) / count * 100.0
    median_return20 = float(pd.Series([item["return20"] for item in observations]).median())
    return_component = max(0.0, min(100.0, 50.0 + median_return20 * 5.0))
    score = 0.35 * above20 + 0.35 * above60 + 0.20 * positive20 + 0.10 * return_component
    if score >= thresholds.market_optimistic_score:
        level, label, policy = "OPTIMISTIC", "樂觀", "NORMAL"
    elif score >= thresholds.market_positive_score:
        level, label, policy = "POSITIVE", "偏樂觀", "NORMAL"
    elif score >= thresholds.market_neutral_score:
        level, label, policy = "NEUTRAL", "中性", "REDUCED"
    elif score >= thresholds.market_cautious_score:
        level, label, policy = "CAUTIOUS", "偏保守", "HIGHEST_QUALITY_ONLY"
    else:
        level, label, policy = "CONSERVATIVE", "保守", "PAUSE_GENERAL_NEW_ENTRIES"
    return {
        "level": level,
        "label": label,
        "breadth_score": _round(score),
        "sample_size": count,
        "above_ma20_pct": _round(above20),
        "above_ma60_pct": _round(above60),
        "positive_20d_return_pct": _round(positive20),
        "median_20d_return_pct": _round(median_return20),
        "new_entry_policy": policy,
        "method": "local-stock-breadth-proxy-v1",
        "limitation": "本地現存股票樣本的廣度代理，不是加權指數，且可能有倖存者偏差。",
    }


def _leg_efficiency(frame: pd.DataFrame, start_index: int, end_index: int) -> float:
    closes = frame["close"].iloc[start_index : end_index + 1].astype(float)
    if len(closes) < 2:
        return 0.0
    travel = float(closes.diff().abs().sum())
    return 0.0 if travel == 0 else abs(float(closes.iloc[-1] - closes.iloc[0])) / travel


def _taiji_leg(
    frame: pd.DataFrame,
    *,
    start_index: int,
    end_index: int,
    start_price: float,
    end_price: float,
    confirmed: bool,
) -> dict[str, Any] | None:
    if end_index <= start_index or end_price == start_price:
        return None
    direction = "BULL" if end_price > start_price else "BEAR"
    amplitude = abs(end_price - start_price)
    atr = float(frame.iloc[end_index]["ATR14"])
    amplitude_atr = 0.0 if not math.isfinite(atr) or atr <= 0 else amplitude / atr
    duration = end_index - start_index
    efficiency = _leg_efficiency(frame, start_index, end_index)
    previous = frame.iloc[max(0, start_index - 20) : start_index]
    destructive = False
    if not previous.empty:
        destructive = bool(
            end_price > float(previous["high"].max())
            if direction == "BULL"
            else end_price < float(previous["low"].min())
        )
    return {
        "direction": direction,
        "start_index": start_index,
        "end_index": end_index,
        "start_date": frame.iloc[start_index]["date"].date().isoformat(),
        "end_date": frame.iloc[end_index]["date"].date().isoformat(),
        "start_price": _round(start_price),
        "end_price": _round(end_price),
        "amplitude": _round(amplitude),
        "amplitude_atr": _round(amplitude_atr, 3),
        "duration_bars": duration,
        "slope_atr_per_bar": _round(amplitude_atr / max(duration, 1), 4),
        "efficiency": _round(efficiency, 4),
        "destructive": destructive,
        "confirmed": confirmed,
    }


def _taiji_copy_quality(
    child: dict[str, Any], parent: dict[str, Any], thresholds: TrialThresholds
) -> dict[str, Any]:
    same_direction = child["direction"] == parent["direction"]
    if child["direction"] == "BULL":
        broke_parent_extreme = float(child["end_price"]) > float(parent["end_price"])
    else:
        broke_parent_extreme = float(child["end_price"]) < float(parent["end_price"])
    amplitude_ratio = float(child["amplitude"]) / max(float(parent["amplitude"]), 1e-9)
    duration_ratio = float(child["duration_bars"]) / max(float(parent["duration_bars"]), 1.0)
    slope_ratio = float(child["slope_atr_per_bar"]) / max(float(parent["slope_atr_per_bar"]), 1e-9)
    efficiency_delta = float(child["efficiency"]) - float(parent["efficiency"])
    if not same_direction:
        quality = "FAILED"
    elif broke_parent_extreme and amplitude_ratio >= 1.0 and slope_ratio >= 1.0:
        quality = "STRONG"
    elif broke_parent_extreme and (
        amplitude_ratio >= thresholds.taiji_copy_acceptable_amplitude_ratio
        or slope_ratio >= thresholds.taiji_copy_acceptable_slope_ratio
        or efficiency_delta >= 0
    ):
        quality = "ACCEPTABLE"
    elif not child["confirmed"]:
        quality = "DEVELOPING"
    elif not broke_parent_extreme:
        quality = "FAILED"
    else:
        quality = "WEAKENING"
    return {
        "quality": quality,
        "broke_parent_extreme": broke_parent_extreme,
        "amplitude_ratio": _round(amplitude_ratio, 3),
        "duration_ratio": _round(duration_ratio, 3),
        "slope_ratio": _round(slope_ratio, 3),
        "efficiency_delta": _round(efficiency_delta, 4),
    }


def _taiji_correction_quality(
    correction: dict[str, Any], parent: dict[str, Any], thresholds: TrialThresholds
) -> dict[str, Any]:
    retracement = float(correction["amplitude"]) / max(float(parent["amplitude"]), 1e-9)
    duration_ratio = float(correction["duration_bars"]) / max(float(parent["duration_bars"]), 1.0)
    holds_origin = bool(
        float(correction["end_price"]) > float(parent["start_price"])
        if parent["direction"] == "BULL"
        else float(correction["end_price"]) < float(parent["start_price"])
    )
    opposite_direction = correction["direction"] != parent["direction"]
    if not opposite_direction or not holds_origin or retracement > thresholds.taiji_correction_failure_ratio:
        quality = "FAILED"
    elif retracement <= thresholds.taiji_correction_strong_ratio and duration_ratio <= 1.5:
        quality = "STRONG"
    elif retracement <= thresholds.taiji_correction_acceptable_ratio:
        quality = "ACCEPTABLE"
    else:
        quality = "WEAKENING"
    return {
        "quality": quality,
        "retracement_ratio": _round(retracement, 3),
        "duration_ratio": _round(duration_ratio, 3),
        "holds_parent_origin": holds_origin,
    }


def _taiji(frame: pd.DataFrame, direction: dict[str, str], thresholds: TrialThresholds) -> dict[str, Any]:
    local, _ = detect_local_pivots(_candles(frame), thresholds.pivot_n)
    chronological = sorted(local, key=lambda item: (item.bar_index, item.confirmation_index, item.kind))
    paired, _, _ = pair_pivots(chronological)
    completed: list[dict[str, Any]] = []
    for left, right in zip(paired, paired[1:]):
        leg = _taiji_leg(
            frame,
            start_index=left.bar_index,
            end_index=right.bar_index,
            start_price=float(left.price),
            end_price=float(right.price),
            confirmed=True,
        )
        if leg is not None:
            completed.append(leg)

    legs = list(completed)
    if paired:
        last = paired[-1]
        active = _taiji_leg(
            frame,
            start_index=last.bar_index,
            end_index=len(frame) - 1,
            start_price=float(last.price),
            end_price=float(frame.iloc[-1]["close"]),
            confirmed=False,
        )
        if active is not None:
            legs.append(active)

    target_direction = direction["large"] if direction["large"] in {"BULL", "BEAR"} else direction["small"]
    recent_floor = max(0, len(frame) - thresholds.taiji_anchor_lookback)
    candidates = [
        index
        for index, leg in enumerate(completed)
        if leg["start_index"] >= recent_floor
        and leg["direction"] == target_direction
        and float(leg["amplitude_atr"]) >= thresholds.taiji_anchor_min_atr
        and float(leg["efficiency"]) >= thresholds.taiji_anchor_min_efficiency
        and leg["destructive"]
    ]
    if not candidates:
        return {
            "state": "UNDEFINED",
            "direction": target_direction if target_direction in {"BULL", "BEAR"} else None,
            "sequence": "NONE",
            "anchor": None,
            "legs": [],
            "entry_support": False,
            "failure": False,
            "late_generation": False,
            "reason": "最近120根日K沒有同時符合幅度、效率與破壞性的太極定錨代理。",
        }

    within_five = [index for index in candidates if len(legs) - index <= 5]
    anchor_index = min(within_five) if within_five else max(candidates)
    sequence_legs = [dict(item) for item in legs[anchor_index:]]
    for offset, leg in enumerate(sequence_legs, 1):
        leg["sequence_number"] = offset
        leg["role"] = "ANCHOR" if offset == 1 else "CORRECTION" if offset % 2 == 0 else "COPY"
        leg["quality"] = None
        leg["comparison"] = None
        if leg["role"] == "CORRECTION":
            parent = sequence_legs[offset - 2]
            comparison = _taiji_correction_quality(leg, parent, thresholds)
            leg["quality"] = comparison["quality"]
            leg["comparison"] = comparison
        elif leg["role"] == "COPY":
            parent = sequence_legs[offset - 3]
            comparison = _taiji_copy_quality(leg, parent, thresholds)
            leg["quality"] = comparison["quality"]
            leg["comparison"] = comparison

    active = sequence_legs[-1]
    sequence_number = int(active["sequence_number"])
    if sequence_number > 5:
        state = "POST_5"
    elif active["role"] == "ANCHOR":
        state = "WAIT_CORRECTION"
    elif active["role"] == "CORRECTION":
        state = "CORRECTION_FAILED" if active["quality"] == "FAILED" else (
            "COPY_ARMED" if active["quality"] in {"STRONG", "ACCEPTABLE"} else "COPY_WEAKENING"
        )
    else:
        state = {
            "STRONG": "COPY_CONFIRMED",
            "ACCEPTABLE": "COPY_CONFIRMED",
            "FAILED": "COPY_FAILED",
        }.get(str(active["quality"]), "COPY_WEAKENING")
    late_generation = sequence_number >= 5
    entry_support = bool(
        target_direction == "BULL"
        and state in {"COPY_ARMED", "COPY_CONFIRMED"}
    )
    failure = state in {"COPY_FAILED", "CORRECTION_FAILED", "POST_5"}
    role_zh = {"ANCHOR": "定錨", "CORRECTION": "修正", "COPY": "複製"}[active["role"]]
    quality_zh = {
        None: "尚無品質比較",
        "STRONG": "強",
        "ACCEPTABLE": "可接受",
        "DEVELOPING": "形成中",
        "WEAKENING": "轉弱",
        "FAILED": "失敗",
    }.get(active["quality"], str(active["quality"]))
    return {
        "state": state,
        "direction": target_direction,
        "sequence": f"LEG_{sequence_number}" if sequence_number <= 5 else "POST_5",
        "anchor": sequence_legs[0],
        "legs": sequence_legs[:6],
        "entry_support": entry_support,
        "failure": failure,
        "late_generation": late_generation,
        "reason": f"{target_direction} 朝代第{sequence_number}段（{role_zh}），品質{quality_zh}。",
    }


def _candles(frame: pd.DataFrame) -> list[Candle]:
    return [
        Candle(
            at=datetime.combine(row.date.date(), time(13, 30), tzinfo=TAIPEI),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
        )
        for row in frame.itertuples(index=False)
    ]


def _round(value: Any, digits: int = 2) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _directional_strength(frame: pd.DataFrame, end_index: int, period: int = 10) -> float:
    start_index = end_index - period
    if start_index < 0:
        return 0.0
    closes = frame["close"].iloc[start_index : end_index + 1].astype(float)
    travel = float(closes.diff().abs().sum())
    efficiency = 0.0 if travel == 0 else abs(float(closes.iloc[-1] - closes.iloc[0])) / travel
    atr = float(frame["ATR14"].iloc[end_index])
    normalized_move = 0.0 if atr <= 0 else min(abs(float(closes.iloc[-1] - closes.iloc[0])) / (atr * math.sqrt(period)), 1.0)
    return 0.65 * efficiency + 0.35 * normalized_move


def _pivot_structure(frame: pd.DataFrame, thresholds: TrialThresholds) -> dict[str, Any]:
    local, stats = detect_local_pivots(_candles(frame), thresholds.pivot_n)
    paired, replacements, ignored = pair_pivots(local)
    large = secondary_pivots(paired)
    close = float(frame.iloc[-1]["close"])
    recent_floor = max(0, len(frame) - thresholds.defense_lookback)
    recent_lows = [
        item
        for item in paired
        if item.kind == "LOW" and item.bar_index >= recent_floor and item.price < close
    ]
    recent_highs = [
        item
        for item in paired
        if item.kind == "HIGH" and item.bar_index >= recent_floor
    ]
    defense = max(recent_lows, key=lambda item: item.bar_index) if recent_lows else None
    resistance = max(recent_highs, key=lambda item: item.bar_index) if recent_highs else None
    return {
        "small_dow": dow_state(paired),
        "large_dow": dow_state(large),
        "confirmed_local_pivots": len(local),
        "paired_pivots": len(paired),
        "secondary_pivots": len(large),
        "same_side_replacements": replacements,
        "same_side_ignored": ignored,
        "pivot_candidate_stats": stats,
        "defense": None
        if defense is None
        else {
            "price": _round(defense.price),
            "bar_date": defense.bar_time[:10],
            "confirmed_date": defense.confirmation_time[:10],
        },
        "resistance": None
        if resistance is None
        else {
            "price": _round(resistance.price),
            "bar_date": resistance.bar_time[:10],
            "confirmed_date": resistance.confirmation_time[:10],
        },
    }


def _axis_direction(current: float, previous: float, margin: float) -> tuple[str, str, float]:
    denominator = max(abs(previous), 1e-9)
    change = (current - previous) / denominator
    direction = "INCREASING" if change >= 0 else "DECREASING"
    confidence = "CLEAR" if abs(change) >= margin else "MARGINAL"
    return direction, confidence, change


def _quadrant(frame: pd.DataFrame, thresholds: TrialThresholds) -> dict[str, Any]:
    current_trend = _directional_strength(frame, len(frame) - 1, thresholds.recent_window)
    previous_trend = _directional_strength(frame, len(frame) - 1 - thresholds.recent_window, thresholds.recent_window)
    recent_volatility = float(frame["ATR_PCT"].iloc[-5:].mean())
    previous_volatility = float(frame["ATR_PCT"].iloc[-10:-5].mean())
    trend_direction, trend_confidence, trend_change = _axis_direction(
        current_trend, previous_trend, thresholds.quadrant_axis_margin
    )
    volatility_direction, volatility_confidence, volatility_change = _axis_direction(
        recent_volatility, previous_volatility, thresholds.quadrant_axis_margin
    )
    if trend_direction == "INCREASING" and volatility_direction == "INCREASING":
        quadrant = "Q1"
    elif trend_direction == "DECREASING" and volatility_direction == "INCREASING":
        quadrant = "Q2"
    elif trend_direction == "DECREASING" and volatility_direction == "DECREASING":
        quadrant = "Q3"
    else:
        quadrant = "Q4"
    confidence = "CONFIRMED" if trend_confidence == volatility_confidence == "CLEAR" else "CANDIDATE"
    return {
        "working_quadrant": quadrant,
        "state": confidence,
        "trend_dynamics": trend_direction,
        "volatility_dynamics": volatility_direction,
        "trend_strength": _round(current_trend, 4),
        "previous_trend_strength": _round(previous_trend, 4),
        "trend_change_pct": _round(trend_change * 100.0),
        "atr_pct_recent": _round(recent_volatility),
        "atr_pct_previous": _round(previous_volatility),
        "volatility_change_pct": _round(volatility_change * 100.0),
    }


def _resolve_direction(frame: pd.DataFrame, structure: dict[str, Any]) -> dict[str, str]:
    latest = frame.iloc[-1]
    ma21_rising = float(latest["MA21"]) > float(frame.iloc[-6]["MA21"])
    ma55_rising = float(latest["MA55"]) > float(frame.iloc[-11]["MA55"])
    ma_big_bull = bool(latest["MA21"] > latest["MA55"] > latest["MA105"] and ma21_rising and ma55_rising)
    ma_big_bear = bool(latest["MA21"] < latest["MA55"] < latest["MA105"] and not ma21_rising and not ma55_rising)
    ma_small_bull = bool(latest["close"] > latest["MA13"] and latest["MA5"] >= latest["MA13"] and ma21_rising)
    ma_small_bear = bool(latest["close"] < latest["MA13"] and latest["MA5"] <= latest["MA13"] and not ma21_rising)

    large_dow = structure["large_dow"]
    small_dow = structure["small_dow"]
    large = large_dow if large_dow in {"BULL", "BEAR"} else ("BULL" if ma_big_bull else "BEAR" if ma_big_bear else "TRANSITION")
    small = small_dow if small_dow in {"BULL", "BEAR"} else ("BULL" if ma_small_bull else "BEAR" if ma_small_bear else "TRANSITION")

    if large == small == "BULL":
        relation = "多方共振"
    elif large == small == "BEAR":
        relation = "空方共振"
    elif large == "BULL" and small == "BEAR":
        relation = "大多小空／大小打架"
    elif large == "BEAR" and small == "BULL":
        relation = "大空小多／反彈"
    elif large == "BULL":
        relation = "大多小級未定"
    elif small == "BULL":
        relation = "小多大級未定"
    elif large == "BEAR" or small == "BEAR":
        relation = "偏空／結構未共振"
    else:
        relation = "方向未定"
    return {"large": large, "small": small, "relation": relation}


def _setup_pattern(frame: pd.DataFrame, direction: dict[str, str], thresholds: TrialThresholds) -> dict[str, Any]:
    latest = frame.iloc[-1]
    previous = frame.iloc[-2]
    prior20 = frame.iloc[-1 - thresholds.breakout_window : -1]
    prior10 = frame.iloc[-1 - thresholds.recent_window : -1]
    prior20_high = float(prior20["high"].max())
    prior10_high = float(prior10["high"].max())
    prior10_low = float(prior10["low"].min())
    atr = float(latest["ATR14"])
    volume_ratio = 0.0 if latest["VOL_MA20"] <= 0 else float(latest["volume"] / latest["VOL_MA20"])
    crossed_ma21 = bool(previous["close"] <= previous["MA21"] and latest["close"] > latest["MA21"])
    crossed_ma13 = bool(previous["close"] <= previous["MA13"] and latest["close"] > latest["MA13"])
    breakout = bool(latest["close"] > prior20_high and latest["close"] >= latest["open"])
    pullback_relaunch = bool(
        direction["large"] == "BULL"
        and latest["low"] <= max(latest["MA13"], latest["MA21"]) * 1.015
        and latest["close"] > latest["MA13"]
        and latest["close"] > latest["open"]
        and latest["close"] <= prior20_high * 1.03
    )
    compression_breakout = bool(
        latest["close"] > prior10_high
        and (prior10_high - prior10_low) / atr <= 4.0
        and volume_ratio >= 1.0
    )
    if breakout:
        pattern = "20日區間收盤突破"
        pattern_code = "BREAKOUT"
        confirmed = True
        trigger = max(float(latest["high"]), prior20_high)
    elif crossed_ma21 or crossed_ma13:
        pattern = "跌破後收復均線"
        pattern_code = "RECLAIM"
        confirmed = True
        trigger = float(latest["high"])
    elif pullback_relaunch:
        pattern = "多頭回檔重新發動"
        pattern_code = "PULLBACK_RELAUNCH"
        confirmed = True
        trigger = float(latest["high"])
    elif compression_breakout:
        pattern = "壓縮後突破"
        pattern_code = "COMPRESSION_BREAKOUT"
        confirmed = True
        trigger = float(latest["high"])
    else:
        pattern = "尚無日K確認型態"
        pattern_code = "NONE"
        confirmed = False
        trigger = prior20_high
    return {
        "pattern": pattern,
        "pattern_code": pattern_code,
        "confirmed": confirmed,
        "trigger_price": _round(trigger),
        "prior_20d_high": _round(prior20_high),
        "prior_10d_high": _round(prior10_high),
        "volume_ratio": _round(volume_ratio),
        "crossed_ma13": crossed_ma13,
        "crossed_ma21": crossed_ma21,
    }


def _sstv(frame: pd.DataFrame, direction: dict[str, str]) -> dict[str, Any]:
    latest = frame.iloc[-1]
    recent = frame.iloc[-10:]
    atr = float(latest["ATR14"])
    range_atr = float((recent["high"].max() - recent["low"].min()) / atr)
    tr_mean = float(recent["TR"].mean())
    tr_cv = 0.0 if tr_mean == 0 else float(recent["TR"].std(ddof=0) / tr_mean)
    tr_median = float(recent["TR"].median())
    range_outlier = 0.0 if tr_median == 0 else float(recent["TR"].max() / tr_median)
    size = "PASS" if 0.6 <= range_atr <= 4.5 else "WARN" if range_atr <= 6.0 else "FAIL"
    stability = "PASS" if tr_cv <= 0.55 else "WARN" if tr_cv <= 0.85 else "FAIL"
    trend = "PASS" if direction["relation"] == "多方共振" else "WARN" if direction["large"] == "BULL" or direction["small"] == "BULL" else "FAIL"
    volatility = "PASS" if range_outlier <= 2.5 else "WARN" if range_outlier <= 3.5 else "FAIL"
    axes = {"S1_size": size, "S2_stability": stability, "T_trend": trend, "V_volatility": volatility}
    quality = "不合格" if "FAIL" in axes.values() else "警戒" if "WARN" in axes.values() else "合格"
    return {
        "quality": quality,
        "axes": axes,
        "range_10d_atr": _round(range_atr),
        "true_range_cv_10d": _round(tr_cv, 3),
        "largest_range_vs_median_10d": _round(range_outlier),
    }


def _elson_context(
    frame: pd.DataFrame,
    direction: dict[str, str],
    setup: dict[str, Any],
    sstv: dict[str, Any],
) -> dict[str, Any]:
    latest = frame.iloc[-1]
    ma21_rising = float(latest["MA21"]) > float(frame.iloc[-6]["MA21"])
    plane = "PASS" if direction["large"] == "BULL" and sstv["quality"] != "不合格" else (
        "WARN" if direction["large"] in {"BULL", "TRANSITION"} else "FAIL"
    )
    if latest["close"] > latest["MA21"] > latest["MA55"] and ma21_rising:
        line = "PASS"
        habitual_line = "MA21"
    elif latest["close"] > latest["MA55"]:
        line = "WARN"
        habitual_line = "MA55_PROXY"
    else:
        line = "FAIL"
        habitual_line = "UNCONFIRMED"
    point = "PASS" if setup["confirmed"] else "WAIT"
    if plane == line == point == "PASS":
        alignment = "ALIGNED"
    elif plane == "FAIL" or line == "FAIL":
        alignment = "CONFLICT"
    else:
        alignment = "FORMING"
    return {
        "plane": plane,
        "line": line,
        "point": point,
        "alignment": alignment,
        "habitual_line_proxy": habitual_line,
        "stock_type": "UNCLASSIFIED",
        "stock_type_reason": "缺少可稽核的發布日基本面序列，不由價格圖猜成長／轉機／循環／題材分類。",
    }


def _laoxiao_context(
    frame: pd.DataFrame,
    direction: dict[str, str],
    setup: dict[str, Any],
    thresholds: TrialThresholds,
) -> dict[str, Any]:
    latest = frame.iloc[-1]
    history = frame.iloc[-1 - thresholds.laoxiao_stage_lookback : -1]
    prior_high = float(history["high"].max())
    structural_low = float(history["low"].min())
    close = float(latest["close"])
    denominator = max(prior_high - structural_low, 1e-9)
    progress = (close - structural_low) / denominator
    if close >= prior_high:
        zone = "ZONE_3_AFTER_HIGH"
        zone_label = "第三區／過前高"
        remaining_slots = 1
    elif progress <= 1 / 3:
        zone = "ZONE_1_LOW"
        zone_label = "第一區／相對低位"
        remaining_slots = 3
    else:
        zone = "ZONE_2_BEFORE_HIGH"
        zone_label = "第二區／過高前"
        remaining_slots = 2

    high_120 = float(frame["high"].iloc[-thresholds.laoxiao_stage_lookback :].max())
    drawdown = max(0.0, (high_120 - close) / high_120 * 100.0)
    if drawdown > 30.0:
        drawdown_state = "ORIGINAL_STRENGTH_BROKEN"
    elif drawdown >= 20.0:
        drawdown_state = "WARNING_20_TO_30"
    else:
        drawdown_state = "WITHIN_20"
    avg_volume_lots = float(frame["volume"].iloc[-20:].mean()) / 1000.0
    liquidity_pass = avg_volume_lots >= thresholds.liquidity_min_avg_volume_lots
    near_high = close >= prior_high * 0.95
    if (
        setup["pattern_code"] in {"BREAKOUT", "COMPRESSION_BREAKOUT"}
        and float(setup["volume_ratio"]) >= 1.2
        and near_high
    ):
        stock_character = "SMALL_MISTRESS_PROXY"
        character_label = "小三代理／強勢突破型"
    elif (
        direction["large"] == "BULL"
        and setup["pattern_code"] in {"RECLAIM", "PULLBACK_RELAUNCH"}
        and drawdown <= 20.0
    ):
        stock_character = "MAIN_WIFE_PROXY"
        character_label = "正宮代理／多頭拉回型"
    else:
        stock_character = "UNDEFINED"
        character_label = "股性未定"
    return {
        "stock_character": stock_character,
        "stock_character_label": character_label,
        "stage_zone": zone,
        "stage_zone_label": zone_label,
        "remaining_stage_slots": remaining_slots,
        "prior_120d_high": _round(prior_high),
        "structural_120d_low": _round(structural_low),
        "stage_progress_ratio": _round(progress, 3),
        "drawdown_from_120d_high_pct": _round(drawdown),
        "drawdown_state": drawdown_state,
        "avg_volume_20d_lots": _round(avg_volume_lots),
        "liquidity_pass": liquidity_pass,
        "sector_candidate_count": None,
        "sector_leadership": "UNKNOWN_UNTIL_CROSS_SECTION",
    }


def _decision(
    *,
    frame: pd.DataFrame,
    selection_score: int | None,
    radar_member: bool,
    structure: dict[str, Any],
    direction: dict[str, str],
    quadrant: dict[str, Any],
    setup: dict[str, Any],
    sstv: dict[str, Any],
    taiji: dict[str, Any],
    elson: dict[str, Any],
    laoxiao: dict[str, Any],
    market_context: dict[str, Any],
    thresholds: TrialThresholds,
) -> dict[str, Any]:
    latest = frame.iloc[-1]
    close = float(latest["close"])
    atr = float(latest["ATR14"])
    defense = structure.get("defense")
    defense_price = None if not defense else float(defense["price"])
    risk_pct = None if defense_price is None else max(0.0, (close - defense_price) / close * 100.0)
    risk_atr = None if defense_price is None or atr <= 0 else max(0.0, (close - defense_price) / atr)
    extension_atr = max(0.0, (close - float(latest["MA21"])) / atr) if atr > 0 else 0.0
    signal_range_atr = float((latest["high"] - latest["low"]) / atr) if atr > 0 else 0.0
    risk_manageable = bool(
        risk_pct is not None
        and risk_atr is not None
        and risk_pct <= thresholds.risk_max_pct
        and risk_atr <= thresholds.risk_max_atr
    )
    no_chase = bool(
        extension_atr > thresholds.no_chase_extension_atr
        or signal_range_atr > thresholds.no_chase_signal_range_atr
        or (risk_pct is not None and risk_pct > thresholds.no_chase_risk_pct)
        or (risk_atr is not None and risk_atr > thresholds.no_chase_risk_atr)
    )
    structural_invalid = bool(
        direction["relation"] == "空方共振"
        or (taiji["state"] == "CORRECTION_FAILED" and taiji["direction"] == "BULL")
        or (
            direction["large"] == "BEAR"
            and latest["close"] < latest["MA55"]
            and structure["small_dow"] == "BEAR"
        )
    )
    quadrant_ok = quadrant["working_quadrant"] in {"Q1", "Q4"}
    direction_ok = direction["large"] == "BULL" and direction["small"] != "BEAR"
    taiji_blocks_entry = taiji["state"] in {"COPY_FAILED", "CORRECTION_FAILED", "POST_5"}
    taiji_primary = taiji["sequence"] in {"LEG_2", "LEG_3", "LEG_4"}
    analysis_lens = "TAIJI_PRIMARY" if taiji_primary else "QUADRANT_PRIMARY"
    structural_lens_ok = taiji["entry_support"] if analysis_lens == "TAIJI_PRIMARY" else quadrant_ok
    elson_ok = elson["alignment"] == "ALIGNED"
    liquidity_ok = bool(laoxiao["liquidity_pass"])
    market_ok = market_context.get("new_entry_policy") != "PAUSE_GENERAL_NEW_ENTRIES"
    checks = {
        "direction_ok": direction_ok,
        "structural_lens_ok": structural_lens_ok,
        "taiji_not_blocking": not taiji_blocks_entry,
        "setup_confirmed": bool(setup["confirmed"]),
        "elson_point_line_plane_aligned": elson_ok,
        "sstv_not_failed": sstv["quality"] != "不合格",
        "risk_manageable": risk_manageable,
        "liquidity_ok": liquidity_ok,
        "market_allows_general_entry": market_ok,
        "not_no_chase": not no_chase,
        "structure_not_invalid": not structural_invalid,
    }
    check_labels = {
        "direction_ok": "大級偏多且小級非空",
        "structural_lens_ok": "太極或四象限主鏡頭可執行",
        "taiji_not_blocking": "太極未失敗／未超過第五段",
        "setup_confirmed": "日K點已確認",
        "elson_point_line_plane_aligned": "Elson面線點同向",
        "sstv_not_failed": "S/S/T/V未失敗",
        "risk_manageable": "防線距離在10%及2.5ATR內",
        "liquidity_ok": "20日均量至少500張",
        "market_allows_general_entry": "大盤環境允許一般新倉",
        "not_no_chase": "未觸發不可追價",
        "structure_not_invalid": "結構未失效",
    }
    unmet_conditions = [check_labels[key] for key, passed in checks.items() if not passed]
    if structural_invalid:
        status = "結構失效"
    elif no_chase:
        status = "不宜追價"
    elif (
        setup["confirmed"]
        and structural_lens_ok
        and direction_ok
        and risk_manageable
        and sstv["quality"] != "不合格"
        and not taiji_blocks_entry
        and elson_ok
        and liquidity_ok
        and market_ok
    ):
        status = "可評估進場"
    else:
        status = "等待觸發"

    reasons: list[str] = []
    if radar_member:
        reasons.append("精選與雷達交集")
    reasons.append(direction["relation"])
    reasons.append(f"{quadrant['working_quadrant']}{'候選' if quadrant['state'] == 'CANDIDATE' else ''}")
    reasons.append(f"主鏡頭 {analysis_lens}")
    reasons.append(f"太極 {taiji['state']}／{taiji['sequence']}")
    reasons.append(f"Elson面線點 {elson['alignment']}")
    reasons.append(f"老蕭 {laoxiao['stock_character_label']}、{laoxiao['stage_zone_label']}")
    reasons.append(setup["pattern"])
    if defense_price is None:
        reasons.append("缺少90日內合格樞紐防線")
    elif not risk_manageable:
        reasons.append("結構防線距離偏大")
    if no_chase:
        reasons.append("延伸、單日波幅或防線距離超出試跑上限")
    if sstv["quality"] != "合格":
        reasons.append(f"S/S/T/V {sstv['quality']}")
    if taiji_blocks_entry:
        reasons.append("太極複製／修正失敗或朝代已超過第五段，等待重新定錨")
    if not liquidity_ok:
        reasons.append("20日均量未達試跑流動性底線500張")
    if not market_ok:
        reasons.append("Elson大盤環境代理為保守，暫停一般新倉")

    # A truncated curated summary may not retain every candidate's detailed
    # score.  Membership still proves that the stock passed the curated scan;
    # use a neutral ranking-only fallback and never invent a persisted score.
    priority = (50 if selection_score is None else selection_score) * 0.35
    priority += 10 if radar_member else 3
    priority += 24 if direction["relation"] == "多方共振" else 14 if direction["large"] == "BULL" else 6
    priority += {"Q1": 14, "Q4": 13, "Q2": 7, "Q3": 3}[quadrant["working_quadrant"]]
    priority += 14 if setup["confirmed"] else 4
    priority += 10 if risk_manageable else 2
    priority += 8 if taiji["entry_support"] else -8 if taiji_blocks_entry else 0
    priority += 6 if elson_ok else -4 if elson["alignment"] == "CONFLICT" else 0
    priority += 4 if laoxiao["stock_character"] != "UNDEFINED" else 0
    priority += 3 if liquidity_ok else -20
    priority += {"OPTIMISTIC": 4, "POSITIVE": 3, "NEUTRAL": 0, "CAUTIOUS": -3, "CONSERVATIVE": -10}.get(
        str(market_context.get("level")), 0
    )
    if taiji["late_generation"]:
        priority -= 5
    if sstv["quality"] == "不合格":
        priority -= 10
    if no_chase:
        priority -= 12
    if structural_invalid:
        priority -= 20
    priority = max(0, min(100, round(priority)))

    trigger = float(setup["trigger_price"])
    chase_cap = close + thresholds.next_session_chase_cap_atr * atr
    if status == "可評估進場":
        stage = "ARMED"
        next_action = f"依{analysis_lens}等待下一根已收盤日K站上／守住 {trigger:.2f}，且收盤未超過 {chase_cap:.2f}；確認後才於再下一交易日評估執行，超過上限則等待回測"
    elif status == "等待觸發":
        stage = "FORMING"
        next_action = f"等待日K收盤確認型態或太極重新定錨；初步觀察價 {trigger:.2f}，最多觀察5根日K後重算"
    elif status == "不宜追價":
        stage = "NO_CHASE"
        next_action = "不追價；等待回測、形成新樞紐與較近防線後重算"
    else:
        stage = "INVALIDATED"
        next_action = "取消原偏多候選；除非重新定錨並形成多方結構，否則不進場"
    exit_plan = (
        "若進場後日K收盤跌破樞紐防線即退出；觸發後3根日K未出現延續則降級，5根後重新建案"
        if defense_price is not None
        else "尚無可驗證樞紐防線，不建立交易計畫"
    )
    return {
        "status": status,
        "stage": stage,
        "priority_score": priority,
        "analysis_lens": analysis_lens,
        "checks": checks,
        "unmet_conditions": unmet_conditions,
        "reasons": reasons,
        "risk": {
            "defense_price": _round(defense_price),
            "risk_distance_pct": _round(risk_pct),
            "risk_distance_atr": _round(risk_atr),
            "extension_from_ma21_atr": _round(extension_atr),
            "signal_range_atr": _round(signal_range_atr),
            "risk_manageable": risk_manageable,
            "no_chase": no_chase,
            "next_session_chase_cap": _round(chase_cap),
        },
        "next_action": next_action,
        "exit_plan": exit_plan,
    }


def analyze_candidate(
    *,
    code: str,
    target: date,
    stock: dict[str, Any],
    curated: dict[str, Any],
    radar: dict[str, Any] | None,
    market_context: dict[str, Any],
    thresholds: TrialThresholds,
) -> dict[str, Any]:
    symbol = str(stock.get("symbol") or "")
    frame, source_path = _load_daily_frame(symbol, target)
    latest = frame.iloc[-1]
    structure = _pivot_structure(frame, thresholds)
    direction = _resolve_direction(frame, structure)
    quadrant = _quadrant(frame, thresholds)
    setup = _setup_pattern(frame, direction, thresholds)
    sstv = _sstv(frame, direction)
    taiji = _taiji(frame, direction, thresholds)
    elson = _elson_context(frame, direction, setup, sstv)
    laoxiao = _laoxiao_context(frame, direction, setup, thresholds)
    curated_score = None if curated.get("total_score") is None else int(curated["total_score"])
    radar_score = None if radar is None else int(radar.get("total_score") or 0)
    selection_score = radar_score if radar_score is not None else curated_score
    decision = _decision(
        frame=frame,
        selection_score=selection_score,
        radar_member=radar is not None,
        structure=structure,
        direction=direction,
        quadrant=quadrant,
        setup=setup,
        sstv=sstv,
        taiji=taiji,
        elson=elson,
        laoxiao=laoxiao,
        market_context=market_context,
        thresholds=thresholds,
    )
    return {
        "code": code,
        "name": str(stock.get("name") or curated.get("name") or ""),
        "symbol": symbol,
        "market": stock.get("market"),
        "industry": stock.get("industry"),
        "as_of": target.isoformat(),
        "bar_count": len(frame),
        "source_path": str(source_path.resolve()),
        "source_membership": {"curated": True, "radar": radar is not None},
        "selection": {
            "score_used": selection_score,
            "curated_score": curated_score,
            "curated_score_persisted": curated_score is not None,
            "curated_components": curated.get("score_components") or {},
            "radar_score": radar_score,
            "radar_components": {} if radar is None else radar.get("score_components") or {},
            "radar_tag": None if radar is None else radar.get("tag"),
            "radar_key_reasons": [] if radar is None else radar.get("key_reasons") or [],
            "radar_risk_flags": [] if radar is None else radar.get("risk_flags") or [],
        },
        "market_data": {
            "date": latest["date"].date().isoformat(),
            "open": _round(latest["open"]),
            "high": _round(latest["high"]),
            "low": _round(latest["low"]),
            "close": _round(latest["close"]),
            "volume": _round(latest["volume"], 0),
            "ma5": _round(latest["MA5"]),
            "ma13": _round(latest["MA13"]),
            "ma21": _round(latest["MA21"]),
            "ma55": _round(latest["MA55"]),
            "ma105": _round(latest["MA105"]),
            "atr14": _round(latest["ATR14"]),
            "atr_pct": _round(latest["ATR_PCT"]),
            "volume_ratio_20d": setup["volume_ratio"],
        },
        "structure": structure,
        "direction": direction,
        "quadrant": quadrant,
        "setup": setup,
        "sstv": sstv,
        "taiji": taiji,
        "elson": elson,
        "laoxiao": laoxiao,
        "decision": decision,
    }


def _format_price(value: Any) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def _markdown_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 排名 | 股票 | 狀態 | 優先分 | 主鏡頭 | 太極 | 面線點 | 老蕭股性／區位 | 型態 | 收盤 | 觸發 | 防線 | 風險% | 雷達 |",
        "|---:|---|---|---:|---|---|---|---|---|---:|---:|---:|---:|:---:|",
    ]
    for index, item in enumerate(rows, 1):
        decision = item["decision"]
        risk = decision["risk"]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    f"{item['code']} {item['name']}",
                    decision["status"],
                    str(decision["priority_score"]),
                    decision["analysis_lens"],
                    f"{item['taiji']['sequence']}/{item['taiji']['state']}",
                    item["elson"]["alignment"],
                    f"{item['laoxiao']['stock_character_label']}／{item['laoxiao']['stage_zone_label']}",
                    item["setup"]["pattern"],
                    _format_price(item["market_data"]["close"]),
                    _format_price(item["setup"]["trigger_price"]),
                    _format_price(risk["defense_price"]),
                    _format_price(risk["risk_distance_pct"]),
                    "✓" if item["source_membership"]["radar"] else "",
                ]
            )
            + " |"
        )
    return lines


def render_markdown(payload: dict[str, Any]) -> str:
    candidates = payload["candidates"]
    counts = payload["summary"]["decision_counts"]
    lines = [
        f"# 課程整合個股日 K 篩選試跑｜{payload['as_of']}",
        "",
        "> 研究用途、台股現股偏多假設。所有判斷只使用訊號日收盤前已存在的本地資料；不是下單指令或獲利保證。",
        "",
        "## 結論",
        "",
        f"- Elson 大盤環境代理：{payload['market_context']['label']}（廣度分數 {_format_price(payload['market_context']['breadth_score'])}，樣本 {payload['market_context']['sample_size']} 檔，新倉政策 `{payload['market_context']['new_entry_policy']}`）。",
        f"- 原始精選選股：{payload['summary']['candidate_count']} 檔。",
        f"- 精選與雷達交集：{payload['summary']['radar_overlap_count']} 檔。",
        f"- 精選快取保留完整分數：{payload['summary']['curated_score_available_count']} 檔；其餘不臆造分數，只以入選資格與日 K 結構判讀。",
        f"- 可評估進場：{counts.get('可評估進場', 0)} 檔。",
        f"- 等待觸發：{counts.get('等待觸發', 0)} 檔。",
        f"- 不宜追價：{counts.get('不宜追價', 0)} 檔。",
        f"- 結構失效：{counts.get('結構失效', 0)} 檔。",
        "",
        "`優先分` 是本次試跑的排序值，不是勝率或預期報酬。`TAIJI_PRIMARY` 表示太極修正／複製比象限更適合作主要鏡頭；否則由四象限主判。`可評估進場` 目前只是 ARMED（已有計畫、仍待下一根日 K 觸發），不是立即買進。",
        "",
    ]
    for status in ("可評估進場", "等待觸發", "不宜追價", "結構失效"):
        selected = [item for item in candidates if item["decision"]["status"] == status]
        if not selected:
            continue
        lines += [f"## {status}", "", *_markdown_table(selected), ""]

    lines += ["## 可評估進場個股的下一步", ""]
    actionable = [item for item in candidates if item["decision"]["status"] == "可評估進場"]
    if not actionable:
        lines.append("本輪沒有同時通過方向、象限、型態、S/S/T/V 與防線距離的個股。")
        lines.append("")
    for item in actionable:
        defense = item["structure"].get("defense") or {}
        lines += [
            f"### {item['code']} {item['name']}",
            "",
            f"- 判斷：{'；'.join(item['decision']['reasons'])}。",
            f"- 太極：{item['taiji']['reason']}",
            f"- Elson：面 `{item['elson']['plane']}`、線 `{item['elson']['line']}`、點 `{item['elson']['point']}`。",
            f"- 老蕭：{item['laoxiao']['stock_character_label']}；{item['laoxiao']['stage_zone_label']}；20 日均量 {_format_price(item['laoxiao']['avg_volume_20d_lots'])} 張。",
            f"- 下一步：{item['decision']['next_action']}。",
            f"- 防線：{_format_price(defense.get('price'))}（樞紐日 {defense.get('bar_date', '—')}，確認日 {defense.get('confirmed_date', '—')}）。",
            f"- 管理：{item['decision']['exit_plan']}。",
            "",
        ]

    lines += [
        "## 試跑轉譯規則",
        "",
        "- 單根決策資料：只使用已收盤日 K；訊號日之後的資料一律排除。",
        "- 樞紐：沿用正式監控的因果 `n=2` 樞紐與收盤突破確認，不使用未來 K 回填。",
        "- 大小級：小級以一級道氏樞紐與短均線交叉驗證；大級以二級樞紐與 21／55／105 日均線交叉驗證。",
        "- 象限：把最近 10 日方向效率與 ATR% 分別和前一窗口比較；變化不到 5% 時只標候選象限。",
        "- 太極：用因果樞紐切出定錨、修正與複製；比較幅度、時間、斜率、效率與結構破壞。修正失敗或複製失敗先停止追價，不直接反手。",
        "- Elson：大盤以本地股票廣度作五級代理；個股須面、線、點同向。成長／轉機／循環分類因缺少可稽核發布日資料而維持 UNCLASSIFIED。",
        "- 老蕭：20 日均量至少 500 張；正宮／小三與三區均為日 K 代理。越晚發現只保留後續區段額度，不補回錯過的低位額度。",
        "- 型態：僅承認 20 日區間收盤突破、均線收復、多頭回檔重新發動、壓縮後突破。",
        "- 防線：優先使用最近 90 日內已因果確認的多方低點樞紐，不以任意均線冒充結構停損。",
        "- 可評估進場：須同時符合偏多大級、非空方小級、太極主鏡頭或 Q1／Q4 主鏡頭、Elson 面線點同向、老蕭流動性、已確認型態、S/S/T/V 非不合格，且防線距離不超過 10% 與 2.5 ATR。",
        "- 不宜追價：MA21 延伸超過 2 ATR、訊號日振幅超過 2 ATR，或防線距離超過 12%／3 ATR。",
        "- 執行代理值：下一交易日追價上限暫設訊號收盤價加 0.5 ATR；觸發後 3 根日 K 無延續即降級，5 根後重算。這些是待前向驗證的試跑值，不是課程原始數字。",
        "",
        "## 限制",
        "",
        "- 日 K 無法證明盤中先突破還是先回測，因此只做收盤確認，不能重建盤中成交順序。",
        "- 本次假設現股偏多，未處理融券、當沖、除權息價格調整與個人資金部位。",
        "- 雷達分數用於候選排序；課程結構與風險條件擁有否決權。",
        "- 族群同步目前只統計候選池，不能視為全市場相對強度；大盤廣度亦可能有倖存者偏差。",
        "- 本輪尚未計算交易成本、滑價、成交量衝擊與實際部位；正式交易前必須進行歷史、樣本外和前向模擬驗證。",
        "",
    ]
    return "\n".join(lines)


def build_report(target: date, thresholds: TrialThresholds = THRESHOLDS) -> dict[str, Any]:
    recent_records = _read_json(ROOT / ".cache" / "recent_scan_results.json")
    radar_records = _read_json(ROOT / ".cache" / "radar_results.json")
    curated_record = _latest_curated_record(recent_records, target)
    radar_record = _latest_radar_record(radar_records, target)
    curated_details = _parse_curated_summary(str(curated_record.get("summary") or ""))
    stocks = _stock_map()
    radar_candidates = {
        str(item.get("code") or ""): item
        for item in (radar_record or {}).get("candidate_snapshot", [])
        if isinstance(item, dict) and item.get("code")
    }
    codes = [str(code) for code in curated_record.get("selected_codes") or []]
    if len(codes) != len(set(codes)):
        raise ValueError("精選選股代號含重複值")
    missing_stock = [code for code in codes if code not in stocks]
    if missing_stock:
        raise ValueError(f"候選 stock_list metadata 不完整：{missing_stock}")

    market_context = _market_breadth(target, thresholds)
    candidates = [
        analyze_candidate(
            code=code,
            target=target,
            stock=stocks[code],
            curated=curated_details.get(code, {"code": code, "name": stocks[code].get("name"), "total_score": None, "score_components": {}}),
            radar=radar_candidates.get(code),
            market_context=market_context,
            thresholds=thresholds,
        )
        for code in codes
    ]
    industry_counts = Counter(str(item.get("industry") or "未分類") for item in candidates)
    for item in candidates:
        peer_count = industry_counts[str(item.get("industry") or "未分類")]
        item["laoxiao"]["sector_candidate_count"] = peer_count
        item["laoxiao"]["sector_leadership"] = (
            "CANDIDATE_GROUP_PRESENT" if peer_count >= 2 else "UNKNOWN_IN_CANDIDATE_POOL"
        )
    status_order = {"可評估進場": 0, "等待觸發": 1, "不宜追價": 2, "結構失效": 3}
    candidates.sort(key=lambda item: (status_order[item["decision"]["status"]], -item["decision"]["priority_score"], item["code"]))
    counts = Counter(item["decision"]["status"] for item in candidates)
    payload = {
        "schema_version": "course_daily_screen_trial_v2",
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "as_of": target.isoformat(),
        "assumptions": {
            "market": "Taiwan listed/OTC cash equities",
            "direction": "long_only",
            "frequency": "1d_closed_bars",
            "network_access": False,
            "execution": "research_only_no_orders",
            "integrated_courses": ["啟蒙交易班", "道氏三兄弟", "X戰法班", "戰法C班太極", "Elson股票班", "老蕭股票課程"],
        },
        "source": {
            "curated_scan_id": curated_record.get("scan_id"),
            "curated_created_at": curated_record.get("created_at"),
            "curated_scoring_version": curated_record.get("scoring_version"),
            "radar_id": None if radar_record is None else radar_record.get("radar_id"),
            "radar_created_at": None if radar_record is None else radar_record.get("created_at"),
            "radar_scoring_version": None if radar_record is None else radar_record.get("scoring_version"),
        },
        "market_context": market_context,
        "thresholds": asdict(thresholds),
        "summary": {
            "candidate_count": len(candidates),
            "radar_overlap_count": sum(item["source_membership"]["radar"] for item in candidates),
            "curated_score_available_count": sum(item["selection"]["curated_score_persisted"] for item in candidates),
            "decision_counts": dict(counts),
            "taiji_state_counts": dict(Counter(item["taiji"]["state"] for item in candidates)),
            "laoxiao_character_counts": dict(Counter(item["laoxiao"]["stock_character"] for item in candidates)),
        },
        "candidates": candidates,
    }
    _validate_report(payload, expected_codes=set(codes), target=target)
    return payload


def _validate_report(payload: dict[str, Any], *, expected_codes: set[str], target: date) -> None:
    candidates = payload.get("candidates") or []
    actual_codes = {item["code"] for item in candidates}
    if actual_codes != expected_codes or len(candidates) != len(expected_codes):
        raise ValueError("輸出候選集合與精選選股不一致")
    if any(item["as_of"] != target.isoformat() or item["market_data"]["date"] != target.isoformat() for item in candidates):
        raise ValueError("輸出含有非訊號日資料")
    allowed = {"可評估進場", "等待觸發", "不宜追價", "結構失效"}
    if any(item["decision"]["status"] not in allowed for item in candidates):
        raise ValueError("輸出含未知決策狀態")
    if any(item["decision"]["status"] == "可評估進場" and item["decision"]["risk"]["defense_price"] is None for item in candidates):
        raise ValueError("可評估進場個股缺少結構防線")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="課程整合個股日 K 篩選試跑")
    parser.add_argument("--date", required=True, help="訊號日 YYYY-MM-DD")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    target = date.fromisoformat(args.date)
    output_dir = args.output_dir or ROOT / "reports" / "course_daily_screen" / target.isoformat() / "trial_v2"
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    payload = build_report(target)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "screening.json"
    markdown_path = output_dir / "screening.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "as_of": payload["as_of"],
                "summary": payload["summary"],
                "json": str(json_path.resolve()),
                "markdown": str(markdown_path.resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
