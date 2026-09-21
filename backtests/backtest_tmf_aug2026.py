from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable


DAY = "day"
NIGHT_PRE_US = "night_pre_us"
NIGHT_US_TO_0100 = "night_us_to_0100"
BUCKET_ORDER = (DAY, NIGHT_PRE_US, NIGHT_US_TO_0100)
BUCKET_LABELS = {
    DAY: "日盤 08:45-13:45",
    NIGHT_PRE_US: "夜盤（美股開盤前）15:00-21:30",
    NIGHT_US_TO_0100: "夜盤（美股開盤後至01:00）21:30-01:00",
}


@dataclass
class Signal:
    confirm_idx: int
    pivot_idx: int
    entry_idx: int
    direction: int
    setup: str
    grade: str
    stop: float
    entry_reference: float
    risk: float
    bucket: str
    bucket_date: str
    obstacle: float | None


@dataclass
class Trade:
    scenario: str
    grade: str
    setup: str
    direction: str
    bucket: str
    bucket_date: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry_price: float
    initial_stop: float
    exit_price: float
    gross_points: float
    risk_points: float
    gross_r: float
    mfe_points: float
    mae_points: float
    exit_reason: str


def load_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    marker = "const payload = "
    start = text.index(marker) + len(marker)
    end = text.index(";", start)
    return json.loads(text[start:end])


def wall_clock(timestamp: int) -> datetime:
    # The chart deliberately formats Unix seconds with UTC getters.  Therefore
    # the UTC clock fields are the intended Taiwan wall-clock fields.
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)


def minute_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def bucket_for(dt: datetime) -> str | None:
    minute = minute_of_day(dt)
    if 8 * 60 + 45 <= minute < 13 * 60 + 45:
        return DAY
    if 15 * 60 <= minute < 21 * 60 + 30:
        return NIGHT_PRE_US
    if minute >= 21 * 60 + 30 or minute < 60:
        return NIGHT_US_TO_0100
    return None


def bucket_date_for(dt: datetime, bucket: str) -> date:
    if bucket == NIGHT_US_TO_0100 and minute_of_day(dt) < 60:
        return dt.date() - timedelta(days=1)
    return dt.date()


def same_bucket(i: int, j: int, bars: list[dict[str, Any]]) -> bool:
    bi = bars[i]["bucket"]
    bj = bars[j]["bucket"]
    return (
        bi is not None
        and bi == bj
        and bars[i]["bucket_date"] == bars[j]["bucket_date"]
        and int(bars[j]["time"]) - int(bars[i]["time"]) == (j - i) * 60
    )


def wilder_atr(bars: list[dict[str, Any]], period: int = 14) -> list[float]:
    true_ranges: list[float] = []
    for i, bar in enumerate(bars):
        high = float(bar["high"])
        low = float(bar["low"])
        if i == 0:
            true_ranges.append(high - low)
        else:
            prev_close = float(bars[i - 1]["close"])
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    values: list[float] = [math.nan] * len(bars)
    if len(bars) < period:
        return values
    first = sum(true_ranges[:period]) / period
    values[period - 1] = first
    for i in range(period, len(bars)):
        values[i] = ((period - 1) * values[i - 1] + true_ranges[i]) / period
    return values


def indicator_values(payload: dict[str, Any], key: str) -> list[float]:
    return [float(item["value"]) for item in payload[key]]


def make_bars(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    keys = ("ma21", "ma105", "k", "d", "histogram")
    vectors = {key: indicator_values(payload, key) for key in keys}
    for i, candle in enumerate(payload["candles"]):
        dt = wall_clock(int(candle["time"]))
        bucket = bucket_for(dt)
        row = dict(candle)
        row["dt"] = dt
        row["bucket"] = bucket
        row["bucket_date"] = bucket_date_for(dt, bucket).isoformat() if bucket else None
        for key in keys:
            row[key] = vectors[key][i]
        bars.append(row)
    atr = wilder_atr(bars)
    for row, value in zip(bars, atr):
        row["atr"] = value
    return bars


def groups_for(bars: list[dict[str, Any]]) -> dict[tuple[str, str], list[int]]:
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, bar in enumerate(bars):
        if bar["bucket"]:
            groups[(bar["bucket"], bar["bucket_date"])].append(i)
    return dict(groups)


def side_flips(bars: list[dict[str, Any]], end: int, lookback: int = 20) -> int:
    start = max(0, end - lookback + 1)
    sides: list[int] = []
    for i in range(start, end + 1):
        diff = float(bars[i]["close"]) - float(bars[i]["ma21"])
        side = 1 if diff > 0 else -1 if diff < 0 else 0
        if side:
            sides.append(side)
    return sum(1 for a, b in zip(sides, sides[1:]) if a != b)


def interval_overlap_ratio(a_low: float, a_high: float, b_low: float, b_high: float) -> float:
    overlap = max(0.0, min(a_high, b_high) - max(a_low, b_low))
    base = max(1.0, min(a_high - a_low, b_high - b_low))
    return overlap / base


def range_state(bars: list[dict[str, Any]], i: int) -> tuple[bool, float, float]:
    if i < 30 or math.isnan(float(bars[i]["atr"])):
        return False, math.nan, math.nan
    atr = float(bars[i]["atr"])
    crosses = side_flips(bars, i, 20) >= 4
    flat105 = abs(float(bars[i]["ma105"]) - float(bars[i - 10]["ma105"])) < 0.2 * atr
    first = bars[i - 19 : i - 9]
    second = bars[i - 9 : i + 1]
    overlap = interval_overlap_ratio(
        min(float(x["low"]) for x in first),
        max(float(x["high"]) for x in first),
        min(float(x["low"]) for x in second),
        max(float(x["high"]) for x in second),
    ) >= 0.5
    is_range = sum((crosses, flat105, overlap)) >= 2
    window = bars[i - 29 : i + 1]
    return (
        is_range,
        min(float(x["low"]) for x in window),
        max(float(x["high"]) for x in window),
    )


def macd_filter(bars: list[dict[str, Any]], i: int, direction: int) -> bool:
    hist = float(bars[i]["histogram"])
    prev = float(bars[i - 1]["histogram"])
    if direction == 1:
        return hist >= 0 or hist >= prev
    return hist <= 0 or hist <= prev


def recent_obstacle(
    bars: list[dict[str, Any]],
    confirm_idx: int,
    entry_price: float,
    direction: int,
    lookback: int = 120,
) -> float | None:
    start = max(2, confirm_idx - lookback)
    levels: list[float] = []
    for p in range(start, confirm_idx - 1):
        if p + 2 > confirm_idx:
            break
        if direction == 1:
            value = float(bars[p]["high"])
            if value >= max(float(bars[p - 1]["high"]), float(bars[p - 2]["high"])) and value > max(
                float(bars[p + 1]["high"]), float(bars[p + 2]["high"])
            ):
                if value > entry_price:
                    levels.append(value)
        else:
            value = float(bars[p]["low"])
            if value <= min(float(bars[p - 1]["low"]), float(bars[p - 2]["low"])) and value < min(
                float(bars[p + 1]["low"]), float(bars[p + 2]["low"])
            ):
                if value < entry_price:
                    levels.append(value)
    if not levels:
        return None
    return min(levels) if direction == 1 else max(levels)


def finalize_signal(
    bars: list[dict[str, Any]],
    confirm_idx: int,
    pivot_idx: int,
    direction: int,
    setup: str,
    grade: str,
    pivot_extreme: float,
    max_stop_atr: float = 1.2,
    min_obstacle_r: float | None = 1.5,
) -> Signal | None:
    entry_idx = confirm_idx + 1
    if entry_idx >= len(bars) or not same_bucket(confirm_idx, entry_idx, bars):
        return None
    atr = float(bars[confirm_idx]["atr"])
    if not math.isfinite(atr) or atr <= 0:
        return None
    if float(bars[confirm_idx]["high"]) - float(bars[confirm_idx]["low"]) > 2.0 * atr:
        return None
    buffer = max(2.0, 0.2 * atr)
    stop = pivot_extreme - buffer if direction == 1 else pivot_extreme + buffer
    entry_price = float(bars[entry_idx]["open"])
    risk = direction * (entry_price - stop)
    if risk <= 0 or risk > max_stop_atr * atr:
        return None
    obstacle = recent_obstacle(bars, confirm_idx, entry_price, direction)
    if min_obstacle_r is not None and obstacle is not None and direction * (obstacle - entry_price) < min_obstacle_r * risk:
        return None
    return Signal(
        confirm_idx=confirm_idx,
        pivot_idx=pivot_idx,
        entry_idx=entry_idx,
        direction=direction,
        setup=setup,
        grade=grade,
        stop=stop,
        entry_reference=entry_price,
        risk=risk,
        bucket=str(bars[entry_idx]["bucket"]),
        bucket_date=str(bars[entry_idx]["bucket_date"]),
        obstacle=obstacle,
    )


def environment(
    bars: list[dict[str, Any]],
    i: int,
    direction: int,
    current_extreme: float,
    previous_extreme: float | None,
) -> bool:
    if i < 10 or previous_extreme is None:
        return False
    atr = float(bars[i]["atr"])
    if not math.isfinite(atr):
        return False
    close = float(bars[i]["close"])
    ma21 = float(bars[i]["ma21"])
    ma105 = float(bars[i]["ma105"])
    slope = float(bars[i]["ma105"]) - float(bars[i - 10]["ma105"])
    if direction == 1:
        return close > ma105 and ma21 > ma105 and slope >= 0.2 * atr and current_extreme > previous_extreme
    return close < ma105 and ma21 < ma105 and slope <= -0.2 * atr and current_extreme < previous_extreme


def classify_setup(
    bars: list[dict[str, Any]],
    p: int,
    c: int,
    direction: int,
    trend_environment: bool,
) -> str | None:
    atr = float(bars[c]["atr"])
    if not math.isfinite(atr):
        return None
    is_range, box_low, box_high = range_state(bars, c)
    box_height = box_high - box_low if math.isfinite(box_low) else math.nan
    if p < 20:
        return None

    if direction == 1:
        support = min(float(x["low"]) for x in bars[p - 20 : p])
        in_box_bottom = is_range and float(bars[p]["low"]) <= box_low + 0.25 * box_height
        fake = (
            float(bars[p]["low"]) < support
            and float(bars[c]["close"]) > support
            and float(bars[p]["k"]) <= 35.0
            and float(bars[c]["k"]) > float(bars[p]["k"])
            and (trend_environment or in_box_bottom)
        )
        if fake:
            return "假跌破反轉多"
        touched21 = (
            float(bars[p]["low"]) <= float(bars[p]["ma21"]) + 0.2 * atr
            and float(bars[p]["high"]) >= float(bars[p]["ma21"]) - 0.2 * atr
        )
        trend = (
            trend_environment
            and touched21
            and float(bars[p]["close"]) >= float(bars[p]["ma105"])
            and float(bars[c]["close"]) > float(bars[c]["ma21"])
            and float(bars[c]["k"]) > float(bars[p]["k"])
            and macd_filter(bars, c, direction)
        )
        return "趨勢拉回多" if trend else None

    resistance = max(float(x["high"]) for x in bars[p - 20 : p])
    in_box_top = is_range and float(bars[p]["high"]) >= box_high - 0.25 * box_height
    fake = (
        float(bars[p]["high"]) > resistance
        and float(bars[c]["close"]) < resistance
        and float(bars[p]["k"]) >= 65.0
        and float(bars[c]["k"]) < float(bars[p]["k"])
        and (trend_environment or in_box_top)
    )
    if fake:
        return "假突破反轉空"
    touched21 = (
        float(bars[p]["high"]) >= float(bars[p]["ma21"]) - 0.2 * atr
        and float(bars[p]["low"]) <= float(bars[p]["ma21"]) + 0.2 * atr
    )
    trend = (
        trend_environment
        and touched21
        and float(bars[p]["close"]) <= float(bars[p]["ma105"])
        and float(bars[c]["close"]) < float(bars[c]["ma21"])
        and float(bars[c]["k"]) < float(bars[p]["k"])
        and macd_filter(bars, c, direction)
    )
    return "反彈失敗空" if trend else None


def generate_a_signals(
    bars: list[dict[str, Any]],
    pivot_lookback: int = 5,
    max_stop_atr: float = 1.2,
    min_obstacle_r: float | None = 1.5,
) -> list[Signal]:
    signals: list[Signal] = []
    bull_extremes: list[float] = []
    bear_extremes: list[float] = []
    for p in range(max(pivot_lookback, 20), len(bars) - 1):
        c = p + 1
        if not same_bucket(p, c, bars):
            continue
        current = bars[p]
        left = bars[p - 1]
        right = bars[c]
        recent = bars[p - pivot_lookback + 1 : p + 1]

        bull = (
            float(current["low"]) <= min(float(x["low"]) for x in recent)
            and float(left["open"]) > float(current["high"])
            and float(right["close"]) > float(current["high"])
        )
        bear = (
            float(current["high"]) >= max(float(x["high"]) for x in recent)
            and float(left["open"]) < float(current["low"])
            and float(right["close"]) < float(current["low"])
        )

        if bull:
            extreme = float(current["low"])
            trend_env = environment(bars, c, 1, extreme, bull_extremes[-1] if bull_extremes else None)
            setup = classify_setup(bars, p, c, 1, trend_env)
            if setup:
                signal = finalize_signal(
                    bars, c, p, 1, setup, "A1", extreme,
                    max_stop_atr=max_stop_atr, min_obstacle_r=min_obstacle_r,
                )
                if signal:
                    signals.append(signal)
            bull_extremes.append(extreme)

        if bear:
            extreme = float(current["high"])
            trend_env = environment(bars, c, -1, extreme, bear_extremes[-1] if bear_extremes else None)
            setup = classify_setup(bars, p, c, -1, trend_env)
            if setup:
                signal = finalize_signal(
                    bars, c, p, -1, setup, "A1", extreme,
                    max_stop_atr=max_stop_atr, min_obstacle_r=min_obstacle_r,
                )
                if signal:
                    signals.append(signal)
            bear_extremes.append(extreme)
    return signals


def diagnose_a_pipeline(bars: list[dict[str, Any]], pivot_lookback: int = 5) -> dict[str, int]:
    counts = defaultdict(int)
    bull_extremes: list[float] = []
    bear_extremes: list[float] = []
    for p in range(max(pivot_lookback, 20), len(bars) - 1):
        c = p + 1
        if not same_bucket(p, c, bars):
            continue
        current, left, right = bars[p], bars[p - 1], bars[c]
        recent = bars[p - pivot_lookback + 1 : p + 1]
        bull = (
            float(current["low"]) <= min(float(x["low"]) for x in recent)
            and float(left["open"]) > float(current["high"])
            and float(right["close"]) > float(current["high"])
        )
        bear = (
            float(current["high"]) >= max(float(x["high"]) for x in recent)
            and float(left["open"]) < float(current["low"])
            and float(right["close"]) < float(current["low"])
        )
        if bull:
            counts["strict_bull_pivots"] += 1
            extreme = float(current["low"])
            env = environment(bars, c, 1, extreme, bull_extremes[-1] if bull_extremes else None)
            if env:
                counts["strict_bull_trend_environment"] += 1
            setup = classify_setup(bars, p, c, 1, env)
            if setup:
                counts["strict_bull_classified_setup"] += 1
                if finalize_signal(bars, c, p, 1, setup, "A1", extreme):
                    counts["strict_bull_final_signal"] += 1
            bull_extremes.append(extreme)
        if bear:
            counts["strict_bear_pivots"] += 1
            extreme = float(current["high"])
            env = environment(bars, c, -1, extreme, bear_extremes[-1] if bear_extremes else None)
            if env:
                counts["strict_bear_trend_environment"] += 1
            setup = classify_setup(bars, p, c, -1, env)
            if setup:
                counts["strict_bear_classified_setup"] += 1
                if finalize_signal(bars, c, p, -1, setup, "A1", extreme):
                    counts["strict_bear_final_signal"] += 1
            bear_extremes.append(extreme)
    return dict(counts)


def diagnose_trend_pullback_long(
    bars: list[dict[str, Any]], pivot_lookback: int = 5
) -> dict[str, int]:
    """Show the sequential funnel used by the mechanical long-pullback proxy."""
    counts = defaultdict(int)
    bull_extremes: list[float] = []
    for p in range(max(pivot_lookback, 20), len(bars) - 1):
        c = p + 1
        if not same_bucket(p, c, bars):
            continue
        current, left, right = bars[p], bars[p - 1], bars[c]
        recent = bars[p - pivot_lookback + 1 : p + 1]
        bull = (
            float(current["low"]) <= min(float(x["low"]) for x in recent)
            and float(left["open"]) > float(current["high"])
            and float(right["close"]) > float(current["high"])
        )
        if not bull:
            continue
        counts["strict_bull_pivot"] += 1
        extreme = float(current["low"])
        trend_env = environment(
            bars, c, 1, extreme, bull_extremes[-1] if bull_extremes else None
        )
        bull_extremes.append(extreme)
        if not trend_env:
            continue
        counts["trend_environment"] += 1

        atr = float(bars[c]["atr"])
        is_range, box_low, box_high = range_state(bars, c)
        box_height = box_high - box_low if math.isfinite(box_low) else math.nan
        support = min(float(x["low"]) for x in bars[p - 20 : p])
        in_box_bottom = (
            is_range and float(bars[p]["low"]) <= box_low + 0.25 * box_height
        )
        fake = (
            float(bars[p]["low"]) < support
            and float(bars[c]["close"]) > support
            and float(bars[p]["k"]) <= 35.0
            and float(bars[c]["k"]) > float(bars[p]["k"])
            and (trend_env or in_box_bottom)
        )
        if fake:
            counts["routed_to_false_breakdown_long"] += 1
            continue
        counts["not_false_breakdown"] += 1

        touched21 = (
            float(bars[p]["low"]) <= float(bars[p]["ma21"]) + 0.2 * atr
            and float(bars[p]["high"]) >= float(bars[p]["ma21"]) - 0.2 * atr
        )
        if not touched21:
            continue
        counts["touched_ma21_band"] += 1
        if float(bars[p]["close"]) < float(bars[p]["ma105"]):
            continue
        counts["pivot_closed_above_ma105"] += 1
        if float(bars[c]["close"]) <= float(bars[c]["ma21"]):
            continue
        counts["confirmation_closed_above_ma21"] += 1
        if float(bars[c]["k"]) <= float(bars[p]["k"]):
            continue
        counts["kd_turned_up"] += 1
        if not macd_filter(bars, c, 1):
            continue
        counts["macd_not_opposed"] += 1
        if float(bars[c]["high"]) - float(bars[c]["low"]) > 2.0 * atr:
            continue
        counts["signal_bar_le_2atr"] += 1
    return dict(counts)


def diagnose_a_final_filters(bars: list[dict[str, Any]], pivot_lookback: int = 5) -> dict[str, Any]:
    totals = defaultdict(int)
    by_setup: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bull_extremes: list[float] = []
    bear_extremes: list[float] = []

    def inspect_candidate(p: int, c: int, direction: int, setup: str, extreme: float) -> None:
        totals["classified"] += 1
        by_setup[setup]["classified"] += 1
        atr = float(bars[c]["atr"])
        if float(bars[c]["high"]) - float(bars[c]["low"]) > 2.0 * atr:
            totals["rejected_signal_bar_gt_2atr"] += 1
            by_setup[setup]["rejected_signal_bar_gt_2atr"] += 1
            return
        entry_idx = c + 1
        if entry_idx >= len(bars) or not same_bucket(c, entry_idx, bars):
            totals["rejected_no_next_bar_same_bucket"] += 1
            by_setup[setup]["rejected_no_next_bar_same_bucket"] += 1
            return
        buffer = max(2.0, 0.2 * atr)
        stop = extreme - buffer if direction == 1 else extreme + buffer
        entry = float(bars[entry_idx]["open"])
        risk = direction * (entry - stop)
        if risk <= 0:
            totals["rejected_invalid_stop_side"] += 1
            by_setup[setup]["rejected_invalid_stop_side"] += 1
            return
        if risk > 1.2 * atr:
            totals["rejected_stop_gt_1_2atr"] += 1
            by_setup[setup]["rejected_stop_gt_1_2atr"] += 1
            return
        obstacle = recent_obstacle(bars, c, entry, direction)
        if obstacle is not None and direction * (obstacle - entry) < 1.5 * risk:
            totals["rejected_obstacle_lt_1_5r"] += 1
            by_setup[setup]["rejected_obstacle_lt_1_5r"] += 1
            return
        totals["passed"] += 1
        by_setup[setup]["passed"] += 1

    for p in range(max(pivot_lookback, 20), len(bars) - 1):
        c = p + 1
        if not same_bucket(p, c, bars):
            continue
        current, left, right = bars[p], bars[p - 1], bars[c]
        recent = bars[p - pivot_lookback + 1 : p + 1]
        bull = (
            float(current["low"]) <= min(float(x["low"]) for x in recent)
            and float(left["open"]) > float(current["high"])
            and float(right["close"]) > float(current["high"])
        )
        bear = (
            float(current["high"]) >= max(float(x["high"]) for x in recent)
            and float(left["open"]) < float(current["low"])
            and float(right["close"]) < float(current["low"])
        )
        if bull:
            extreme = float(current["low"])
            env = environment(bars, c, 1, extreme, bull_extremes[-1] if bull_extremes else None)
            setup = classify_setup(bars, p, c, 1, env)
            if setup:
                inspect_candidate(p, c, 1, setup, extreme)
            bull_extremes.append(extreme)
        if bear:
            extreme = float(current["high"])
            env = environment(bars, c, -1, extreme, bear_extremes[-1] if bear_extremes else None)
            setup = classify_setup(bars, p, c, -1, env)
            if setup:
                inspect_candidate(p, c, -1, setup, extreme)
            bear_extremes.append(extreme)
    return {
        "totals": dict(totals),
        "by_setup": {setup: dict(values) for setup, values in by_setup.items()},
    }


def generic_pivots(bars: list[dict[str, Any]], direction: int) -> list[int]:
    pivots: list[int] = []
    for p in range(2, len(bars) - 1):
        if not same_bucket(p, p + 1, bars) or not same_bucket(p - 2, p, bars):
            continue
        if direction == 1:
            ok = float(bars[p]["low"]) < min(float(bars[p - 1]["low"]), float(bars[p - 2]["low"])) and float(
                bars[p + 1]["low"]
            ) >= float(bars[p]["low"])
        else:
            ok = float(bars[p]["high"]) > max(float(bars[p - 1]["high"]), float(bars[p - 2]["high"])) and float(
                bars[p + 1]["high"]
            ) <= float(bars[p]["high"])
        if ok:
            pivots.append(p)
    return pivots


def generate_b_signals(
    bars: list[dict[str, Any]],
    max_stop_atr: float = 1.2,
    min_obstacle_r: float | None = 1.5,
) -> list[Signal]:
    signals: list[Signal] = []
    for direction in (1, -1):
        pivots = generic_pivots(bars, direction)
        previous: float | None = None
        for p in pivots:
            extreme = float(bars[p]["low"] if direction == 1 else bars[p]["high"])
            trend_env_at: dict[int, bool] = {}
            trigger = float(bars[p - 1]["high"] if direction == 1 else bars[p - 1]["low"])
            chosen: int | None = None
            for c in range(p + 1, min(p + 4, len(bars))):
                if not same_bucket(p, c, bars):
                    break
                if direction == 1 and min(float(bars[j]["low"]) for j in range(p + 1, c + 1)) < extreme:
                    break
                if direction == -1 and max(float(bars[j]["high"]) for j in range(p + 1, c + 1)) > extreme:
                    break
                trend_env_at[c] = environment(bars, c, direction, extreme, previous)
                if direction == 1:
                    confirmed = float(bars[c]["close"]) > trigger and float(bars[c]["close"]) > float(bars[c]["ma21"])
                else:
                    confirmed = float(bars[c]["close"]) < trigger and float(bars[c]["close"]) < float(bars[c]["ma21"])
                if confirmed:
                    chosen = c
                    break
            if chosen is not None:
                setup = classify_setup(bars, p, chosen, direction, trend_env_at[chosen])
                if setup:
                    signal = finalize_signal(
                        bars,
                        chosen,
                        p,
                        direction,
                        setup,
                        "B",
                        extreme,
                        max_stop_atr=max_stop_atr,
                        min_obstacle_r=min_obstacle_r,
                    )
                    if signal:
                        signals.append(signal)
            previous = extreme
    return signals


def generate_b2_signals(bars: list[dict[str, Any]], groups: dict[tuple[str, str], list[int]]) -> list[Signal]:
    signals: list[Signal] = []
    for (bucket, bucket_date), indices in groups.items():
        if bucket != DAY:
            continue
        by_clock = {bars[i]["dt"].strftime("%H:%M"): i for i in indices}
        opening = [by_clock.get("08:45"), by_clock.get("08:46"), by_clock.get("08:47")]
        if any(i is None for i in opening):
            continue
        opening_indices = [int(i) for i in opening]
        range_high = max(float(bars[i]["high"]) for i in opening_indices)
        range_low = min(float(bars[i]["low"]) for i in opening_indices)
        candidates = [i for i in indices if "08:48" <= bars[i]["dt"].strftime("%H:%M") < "09:15"]
        for direction in (1, -1):
            breakout: int | None = None
            for b in candidates:
                if b + 2 >= len(bars) or not same_bucket(b, b + 2, bars):
                    continue
                atr = float(bars[b]["atr"])
                if not math.isfinite(atr) or float(bars[b]["high"]) - float(bars[b]["low"]) > 2.0 * atr:
                    continue
                if direction == 1:
                    broke = float(bars[b]["close"]) > range_high
                    trend = float(bars[b]["ma21"]) > float(bars[b - 3]["ma21"]) and float(bars[b]["histogram"]) >= float(
                        bars[b - 1]["histogram"]
                    )
                    held = float(bars[b + 1]["close"]) > range_high
                else:
                    broke = float(bars[b]["close"]) < range_low
                    trend = float(bars[b]["ma21"]) < float(bars[b - 3]["ma21"]) and float(bars[b]["histogram"]) <= float(
                        bars[b - 1]["histogram"]
                    )
                    held = float(bars[b + 1]["close"]) < range_low
                if broke and trend and held:
                    breakout = b
                    break
            if breakout is None:
                continue
            confirm = breakout + 1
            if direction == 1:
                extreme = min(float(bars[breakout]["low"]), float(bars[confirm]["low"]))
                setup = "B2開盤趨勢啟動多"
            else:
                extreme = max(float(bars[breakout]["high"]), float(bars[confirm]["high"]))
                setup = "B2開盤趨勢啟動空"
            signal = finalize_signal(bars, confirm, breakout, direction, setup, "B2", extreme)
            if signal:
                signals.append(signal)
    return signals


def five_minute_events(
    bars: list[dict[str, Any]], groups: dict[tuple[str, str], list[int]]
) -> dict[int, list[tuple[str, float]]]:
    events: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for indices in groups.values():
        chunks: list[list[int]] = []
        current: list[int] = []
        for idx in indices:
            if current and int(bars[idx]["time"]) - int(bars[current[-1]]["time"]) != 60:
                if len(current) == 5:
                    chunks.append(current)
                current = []
            current.append(idx)
            if len(current) == 5:
                chunks.append(current)
                current = []
        lows = [min(float(bars[i]["low"]) for i in chunk) for chunk in chunks]
        highs = [max(float(bars[i]["high"]) for i in chunk) for chunk in chunks]
        previous_low: float | None = None
        previous_high: float | None = None
        for j in range(2, len(chunks) - 2):
            confirm_idx = chunks[j + 2][-1]
            low_pivot = lows[j] < min(lows[j - 1], lows[j - 2]) and lows[j] <= min(lows[j + 1], lows[j + 2])
            high_pivot = highs[j] > max(highs[j - 1], highs[j - 2]) and highs[j] >= max(highs[j + 1], highs[j + 2])
            if low_pivot:
                if previous_low is not None and lows[j] > previous_low:
                    events[confirm_idx].append(("higher_low", lows[j]))
                previous_low = lows[j]
            if high_pivot:
                if previous_high is not None and highs[j] < previous_high:
                    events[confirm_idx].append(("lower_high", highs[j]))
                previous_high = highs[j]
    return dict(events)


def dedupe_signals(signals: Iterable[Signal]) -> list[Signal]:
    priority = {"A1": 0, "A2": 0, "B": 1, "B2": 2}
    chosen: dict[tuple[int, int], Signal] = {}
    for signal in signals:
        key = (signal.entry_idx, signal.direction)
        current = chosen.get(key)
        if current is None or priority[signal.grade] < priority[current.grade]:
            chosen[key] = signal
    return sorted(chosen.values(), key=lambda s: (s.entry_idx, priority[s.grade]))


def simulate(
    bars: list[dict[str, Any]],
    groups: dict[tuple[str, str], list[int]],
    events_5m: dict[int, list[tuple[str, float]]],
    signals: list[Signal],
    scenario: str,
    baseline_cost: float = 4.0,
) -> list[Trade]:
    signal_map: dict[int, list[Signal]] = defaultdict(list)
    for signal in dedupe_signals(signals):
        signal_map[signal.entry_idx].append(signal)
    trades: list[Trade] = []

    for (_, _), indices in sorted(groups.items(), key=lambda item: item[1][0]):
        if not indices:
            continue
        position: dict[str, Any] | None = None
        cumulative_r = 0.0
        consecutive_losses = 0
        cooldown_until = -1
        pending_time_exit = False

        for seq, i in enumerate(indices):
            bar = bars[i]
            # A close-confirmed signal enters at this bar's open, so this same
            # bar must immediately be eligible to hit the stop.  Opening the
            # position after bar processing would introduce survivorship bias.
            if position is None and cumulative_r > -2.0 and i >= cooldown_until:
                candidates = signal_map.get(i, [])
                if candidates:
                    signal = sorted(candidates, key=lambda s: {"A1": 0, "A2": 0, "B": 1, "B2": 2}[s.grade])[0]
                    position = {
                        "signal": signal,
                        "direction": signal.direction,
                        "entry_idx": i,
                        "entry": float(bar["open"]),
                        "initial_stop": signal.stop,
                        "stop": signal.stop,
                        "risk": signal.risk,
                        "mfe": 0.0,
                        "mae": 0.0,
                        "reached_1r": False,
                        "reached_1r_idx": -1,
                        "used_5m_trail": False,
                    }
                    pending_time_exit = False

            if position is not None:
                direction = int(position["direction"])
                entry = float(position["entry"])
                stop = float(position["stop"])
                risk = float(position["risk"])

                if pending_time_exit:
                    exit_price = float(bar["open"])
                    exit_reason = "時間停損"
                    should_exit = True
                    pending_time_exit = False
                else:
                    should_exit = False
                    exit_price = math.nan
                    exit_reason = ""
                    if direction == 1 and float(bar["low"]) <= stop:
                        exit_price = min(float(bar["open"]), stop)
                        exit_reason = "初始/移動停損"
                        should_exit = True
                    elif direction == -1 and float(bar["high"]) >= stop:
                        exit_price = max(float(bar["open"]), stop)
                        exit_reason = "初始/移動停損"
                        should_exit = True

                favorable = direction * (float(bar["high"] if direction == 1 else bar["low"]) - entry)
                adverse = direction * (entry - float(bar["low"] if direction == 1 else bar["high"]))
                position["mfe"] = max(float(position["mfe"]), favorable)
                position["mae"] = max(float(position["mae"]), adverse)

                if not should_exit:
                    if not position["reached_1r"] and favorable >= risk:
                        position["reached_1r"] = True
                        position["reached_1r_idx"] = i
                    if (
                        favorable >= 1.5 * risk
                        and position["reached_1r"]
                        and not position["used_5m_trail"]
                    ):
                        position["stop"] = max(float(position["stop"]), entry) if direction == 1 else min(
                            float(position["stop"]), entry
                        )

                    if position["reached_1r"] and i > int(position["reached_1r_idx"]):
                        for event, level in events_5m.get(i, []):
                            atr = float(bar["atr"])
                            buffer = max(2.0, 0.2 * atr)
                            if direction == 1 and event == "higher_low":
                                position["stop"] = max(float(position["stop"]), level - buffer)
                                position["used_5m_trail"] = True
                            elif direction == -1 and event == "lower_high":
                                position["stop"] = min(float(position["stop"]), level + buffer)
                                position["used_5m_trail"] = True

                    held = i - int(position["entry_idx"]) + 1
                    if held >= 10 and float(position["mfe"]) < 0.5 * risk:
                        start = max(int(position["entry_idx"]), i - 9)
                        sides = []
                        for j in range(start, i + 1):
                            diff = float(bars[j]["close"]) - float(bars[j]["ma21"])
                            sides.append(1 if diff > 0 else -1 if diff < 0 else 0)
                        sides = [value for value in sides if value]
                        flips = sum(1 for a, b in zip(sides, sides[1:]) if a != b)
                        if flips >= 2:
                            pending_time_exit = True

                if should_exit:
                    signal = position["signal"]
                    gross = direction * (exit_price - entry)
                    gross_r = gross / risk if risk else math.nan
                    trade = Trade(
                        scenario=scenario,
                        grade=signal.grade,
                        setup=signal.setup,
                        direction="多" if direction == 1 else "空",
                        bucket=signal.bucket,
                        bucket_date=signal.bucket_date,
                        signal_time=bars[signal.confirm_idx]["dt"].isoformat(sep=" ", timespec="minutes"),
                        entry_time=bars[position["entry_idx"]]["dt"].isoformat(sep=" ", timespec="minutes"),
                        exit_time=bar["dt"].isoformat(sep=" ", timespec="minutes"),
                        entry_price=entry,
                        initial_stop=float(position["initial_stop"]),
                        exit_price=exit_price,
                        gross_points=gross,
                        risk_points=risk,
                        gross_r=gross_r,
                        mfe_points=float(position["mfe"]),
                        mae_points=float(position["mae"]),
                        exit_reason=exit_reason,
                    )
                    trades.append(trade)
                    net_r = (gross - baseline_cost) / (risk + baseline_cost)
                    cumulative_r += net_r
                    if gross - baseline_cost < 0:
                        consecutive_losses += 1
                        if consecutive_losses >= 2:
                            cooldown_until = i + 20
                    else:
                        consecutive_losses = 0
                    position = None

        if position is not None:
            i = indices[-1]
            bar = bars[i]
            signal = position["signal"]
            direction = int(position["direction"])
            entry = float(position["entry"])
            exit_price = float(bar["close"])
            risk = float(position["risk"])
            gross = direction * (exit_price - entry)
            trades.append(
                Trade(
                    scenario=scenario,
                    grade=signal.grade,
                    setup=signal.setup,
                    direction="多" if direction == 1 else "空",
                    bucket=signal.bucket,
                    bucket_date=signal.bucket_date,
                    signal_time=bars[signal.confirm_idx]["dt"].isoformat(sep=" ", timespec="minutes"),
                    entry_time=bars[position["entry_idx"]]["dt"].isoformat(sep=" ", timespec="minutes"),
                    exit_time=bar["dt"].isoformat(sep=" ", timespec="minutes"),
                    entry_price=entry,
                    initial_stop=float(position["initial_stop"]),
                    exit_price=exit_price,
                    gross_points=gross,
                    risk_points=risk,
                    gross_r=gross / risk if risk else math.nan,
                    mfe_points=float(position["mfe"]),
                    mae_points=float(position["mae"]),
                    exit_reason="時段結束強制平倉",
                )
            )
    return sorted(trades, key=lambda t: t.entry_time)


def max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def longest_loss_streak(values: list[float]) -> int:
    longest = 0
    current = 0
    for value in values:
        if value < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def metrics(trades: list[Trade], cost: float) -> dict[str, Any]:
    net = [trade.gross_points - cost for trade in trades]
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value < 0]
    risks = [trade.risk_points + cost for trade in trades]
    net_r = [value / risk for value, risk in zip(net, risks)]
    result = {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(trades), 2) if trades else None,
        "net_points": round(sum(net), 2),
        "tmf_net_twd": round(sum(net) * 10.0, 2),
        "avg_points": round(sum(net) / len(net), 2) if net else None,
        "median_points": round(median(net), 2) if net else None,
        "avg_r": round(sum(net_r) / len(net_r), 3) if net_r else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
        "max_drawdown_points": round(max_drawdown(net), 2),
        "max_drawdown_tmf_twd": round(max_drawdown(net) * 10.0, 2),
        "max_single_loss_points": round(min(net), 2) if net else None,
        "max_consecutive_losses": longest_loss_streak(net),
    }
    return result


def summarize_scenario(trades: list[Trade], costs: list[float]) -> dict[str, Any]:
    output: dict[str, Any] = {"overall": {}, "by_bucket": {}, "by_grade": {}, "by_setup": {}}
    for cost in costs:
        output["overall"][str(cost)] = metrics(trades, cost)
    for bucket in BUCKET_ORDER:
        selected = [trade for trade in trades if trade.bucket == bucket]
        output["by_bucket"][bucket] = {str(cost): metrics(selected, cost) for cost in costs}
    for grade in sorted({trade.grade for trade in trades}):
        selected = [trade for trade in trades if trade.grade == grade]
        output["by_grade"][grade] = {str(cost): metrics(selected, cost) for cost in costs}
    for setup in sorted({trade.setup for trade in trades}):
        selected = [trade for trade in trades if trade.setup == setup]
        output["by_setup"][setup] = {str(cost): metrics(selected, cost) for cost in costs}
    return output


def data_quality(bars: list[dict[str, Any]], groups: dict[tuple[str, str], list[int]]) -> dict[str, Any]:
    bucket_info: dict[str, Any] = {}
    expected_per_group = {DAY: 300, NIGHT_PRE_US: 390, NIGHT_US_TO_0100: 210}
    for bucket in BUCKET_ORDER:
        relevant = [indices for (name, _), indices in groups.items() if name == bucket]
        counts = [len(indices) for indices in relevant]
        expected = expected_per_group[bucket]
        bucket_info[bucket] = {
            "groups": len(relevant),
            "bars": sum(counts),
            "complete_groups": sum(1 for count in counts if count == expected),
            "min_bars": min(counts) if counts else 0,
            "max_bars": max(counts) if counts else 0,
        }
    included = [bar for bar in bars if bar["bucket"]]
    excluded = [bar for bar in bars if bar["bucket"] is None]
    return {
        "total_bars": len(bars),
        "included_bars": len(included),
        "excluded_0100_to_0500_and_breaks": len(excluded),
        "first_wall_clock": bars[0]["dt"].isoformat(sep=" ", timespec="minutes"),
        "last_wall_clock": bars[-1]["dt"].isoformat(sep=" ", timespec="minutes"),
        "buckets": bucket_info,
    }


def write_trades(path: Path, trades_by_scenario: dict[str, list[Trade]]) -> None:
    rows: list[dict[str, Any]] = []
    for trades in trades_by_scenario.values():
        for trade in trades:
            row = asdict(trade)
            for cost in (0.0, 2.0, 4.0, 6.0):
                row[f"net_points_cost_{int(cost)}"] = round(trade.gross_points - cost, 2)
            rows.append(row)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("html", type=Path)
    parser.add_argument("--output", type=Path, default=Path("tmf_aug2026_backtest_results.json"))
    parser.add_argument("--trades", type=Path, default=Path("tmf_aug2026_backtest_trades.csv"))
    args = parser.parse_args()

    payload = load_payload(args.html)
    bars = make_bars(payload)
    groups = groups_for(bars)
    events = five_minute_events(bars, groups)

    a_signals = generate_a_signals(bars, pivot_lookback=5)
    a_no_obstacle_signals = generate_a_signals(
        bars, pivot_lookback=5, max_stop_atr=1.2, min_obstacle_r=None
    )
    a_one_r_obstacle_signals = generate_a_signals(
        bars, pivot_lookback=5, max_stop_atr=1.2, min_obstacle_r=1.0
    )
    b_signals = generate_b_signals(bars)
    b2_signals = generate_b2_signals(bars, groups)

    scenarios = {
        "A_formal": a_signals,
        "A_no_obstacle_diagnostic": a_no_obstacle_signals,
        "A_one_r_obstacle_diagnostic": a_one_r_obstacle_signals,
        "A_plus_B_research": a_signals + b_signals,
        "A_plus_B_plus_B2_research": a_signals + b_signals + b2_signals,
    }
    trades_by_scenario = {
        name: simulate(bars, groups, events, signals, name, baseline_cost=4.0)
        for name, signals in scenarios.items()
    }

    sensitivity: dict[str, Any] = {}
    for lookback in (3, 5, 8):
        signals = generate_a_signals(bars, pivot_lookback=lookback)
        trades = simulate(bars, groups, events, signals, f"A_lookback_{lookback}", baseline_cost=4.0)
        sensitivity[str(lookback)] = {
            "signals": len(signals),
            "metrics_cost_4": metrics(trades, 4.0),
        }

    execution_filter_sensitivity: dict[str, Any] = {}
    for max_stop_atr in (1.2, 1.5, 2.0):
        for min_obstacle_r in (None, 1.0, 1.5):
            key = f"stop_{max_stop_atr}_obstacle_{'off' if min_obstacle_r is None else min_obstacle_r}"
            signals = generate_a_signals(
                bars,
                pivot_lookback=5,
                max_stop_atr=max_stop_atr,
                min_obstacle_r=min_obstacle_r,
            )
            trades = simulate(bars, groups, events, signals, key, baseline_cost=4.0)
            execution_filter_sensitivity[key] = {
                "signals": len(signals),
                "metrics_cost_4": metrics(trades, 4.0),
            }

    costs = [0.0, 2.0, 4.0, 6.0]
    result = {
        "source_meta": payload["meta"],
        "assumptions": {
            "time_encoding": "Unix seconds interpreted with UTC clock fields as Taiwan wall-clock, matching the HTML formatter",
            "buckets": BUCKET_LABELS,
            "excluded": "01:00-05:00 is excluded from the requested three buckets",
            "entry": "next one-minute bar open after close confirmation",
            "bucket_exit": "forced flat at each requested bucket end for clean attribution",
            "atr": "Wilder ATR(14)",
            "formal_signal": "A1 strict adjacent-bar pivot only; B and B2 remain research-only",
            "pivot_segment_proxy": "P must be the lowest/highest of the trailing five bars including P",
            "five_minute_trail": "2-left/2-right confirmed five-minute higher-low/lower-high; no look-ahead",
            "cost_scenarios_points_round_trip": costs,
            "cash_conversion": "TMF is NT$10 per index point",
        },
        "data_quality": data_quality(bars, groups),
        "signal_counts": {
            "A": len(a_signals),
            "B": len(b_signals),
            "B2": len(b2_signals),
        },
        "a_pipeline_diagnostics": diagnose_a_pipeline(bars, pivot_lookback=5),
        "a_final_filter_diagnostics": diagnose_a_final_filters(bars, pivot_lookback=5),
        "scenarios": {
            name: summarize_scenario(trades, costs) for name, trades in trades_by_scenario.items()
        },
        "a_pivot_lookback_sensitivity": sensitivity,
        "a_execution_filter_sensitivity": execution_filter_sensitivity,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_trades(args.trades, trades_by_scenario)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
