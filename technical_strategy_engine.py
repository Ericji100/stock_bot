"""Technical strategy detection engine for strategies A/B/C/D.

This module provides detect_technical_strategies() which accepts a DataFrame
that has already been processed by technical_scanner.apply_indicators().
It returns a list of signal dicts WITHOUT modifying existing signal logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


# -------------------------------------------------------------------
# Strategy A: bull_pullback_ma21_breakout
# -------------------------------------------------------------------
def _idx_date_str(frame: pd.DataFrame, idx: int) -> str:
    """Return date string for a given row index, using frame['date'] column."""
    value = frame["date"].iloc[idx]
    return str(value.date()) if hasattr(value, "date") else str(value)[:10]


def _find_macd_wave_cycle(frame: pd.DataFrame) -> dict | None:
    """Find the most recent completed MACD wave cycle.

    A completed wave cycle consists of:
      1. A green zone (MACD_HIST < 0) — the "wave" before the advance
      2. A red zone (MACD_HIST > 0) — the advance that just ended

    The red zone is considered COMPLETED when MACD_HIST crosses back to <= 0.

    If the current MACD is still positive (red zone ongoing), that zone is NOT
    used — we require a completed red zone so wave_high does not include any
    part of the ongoing pullback.

    Returns a dict with wave cycle metadata, or None if not enough data.
    """
    if len(frame) < 60:
        return None

    macd_col = "MACD_HIST"

    # Find all transitions from <=0 to >0 (red zone starts)
    red_starts: list[int] = []
    for i in range(1, len(frame)):
        if frame[macd_col].iloc[i] > 0 and frame[macd_col].iloc[i - 1] <= 0:
            red_starts.append(i)

    if not red_starts:
        return None

    # Find all transitions from >0 to <=0 (red zone ends)
    red_ends: list[int] = []
    for i in range(1, len(frame)):
        if frame[macd_col].iloc[i] <= 0 and frame[macd_col].iloc[i - 1] > 0:
            red_ends.append(i)

    # The most recent completed red zone is one where we have both a start
    # AND an end that comes after it.
    completed_red: list[tuple[int, int]] = []
    for rs in red_starts:
        for re in red_ends:
            if re > rs:
                completed_red.append((rs, re))
                break  # each start pairs with its first following end

    if not completed_red:
        return None

    # Take the most recent completed red zone
    last_red_start, last_red_end = completed_red[-1]

    # Green zone just before this red zone: from red_starts[-1]'s previous index to last_red_start
    green_start = last_red_start - 1
    while green_start >= 0 and frame[macd_col].iloc[green_start] > 0:
        green_start -= 1
    if green_start < 0:
        green_start = 0

    wave_zone = frame.iloc[green_start:last_red_start]
    red_zone = frame.iloc[last_red_start:last_red_end]

    if wave_zone.empty or red_zone.empty:
        return None

    # wave_low: lowest price in green (pre-wave trough)
    # wave_high: highest price in red (the advance that followed)
    wave_low = float(wave_zone["low"].min())
    wave_high = float(red_zone["high"].max())

    return {
        "wave_start_idx": green_start,
        "wave_end_idx": last_red_start - 1,   # 綠柱最後一行（紅柱前一日）
        "wave_red_start_idx": last_red_start,
        "wave_end_idx_red": last_red_end - 1,
        "wave_low": wave_low,
        "wave_high": wave_high,
        "wave_return_pct": (wave_high - wave_low) / wave_low * 100 if wave_low > 0 else 0.0,
        "wave_start_date": _idx_date_str(frame, green_start),
        "wave_green_end_idx": last_red_start - 1,  # 綠柱最後一行索引（與 wave_end_idx 相同）
        "wave_green_end_date": _idx_date_str(frame, last_red_start - 1),
        "wave_red_start_date": _idx_date_str(frame, last_red_start),
        "wave_red_end_date": _idx_date_str(frame, last_red_end - 1),
        # wave_end_date: 红柱结束日（回档开始日），等同于 wave_red_end_date
        "wave_end_date": _idx_date_str(frame, last_red_end - 1),
    }


def _find_pivot_low_in_zone(
    frame: pd.DataFrame,
    zone_start: int,
    zone_end: int,
) -> tuple[float, str, int] | tuple[None, None, None]:
    """Find a pivot low in [zone_start, zone_end] that has right-side confirmation.

    A pivot low at index `t` requires:
      - t-3 <= t <= zone_end-3  (needs right-side bars)
      - at least one bar in [t-3, t-1] with low > low[t]
      - at least one bar in [t+1, t+3] with low > low[t]

    Returns (pivot_low_price, pivot_date_str, pivot_idx) or (None, None, None).
    """
    if zone_end - zone_start < 5:
        return None, None, None

    for t in range(zone_start + 3, zone_end - 3):
        t_low = float(frame["low"].iloc[t])
        # Left confirmation: any bar in [t-3, t-1] with low > t_low
        left_ok = any(float(frame["low"].iloc[j]) > t_low for j in range(t - 3, t))
        # Right confirmation: any bar in [t+1, t+3] with low > t_low
        right_ok = any(float(frame["low"].iloc[j]) > t_low for j in range(t + 1, t + 4))
        if left_ok and right_ok:
            date_str = str(frame["date"].iloc[t].date()) if hasattr(frame["date"].iloc[t], "date") else str(frame["date"].iloc[t])[:10]
            return t_low, date_str, t

    return None, None, None


def _ma_support_context(frame: pd.DataFrame) -> dict:
    """Return MA105 and MA144 status for last row."""
    latest = frame.iloc[-1]
    return {
        "ma105_above": latest["MA105"] > latest["MA144"] if pd.notna(latest["MA105"]) and pd.notna(latest["MA144"]) else False,
        "ma105_support": latest["close"] >= latest["MA105"] if pd.notna(latest["MA105"]) else False,
        "ma144_support": latest["close"] >= latest["MA144"] if pd.notna(latest["MA144"]) else False,
    }


def _score_technical_setup(frame: pd.DataFrame) -> int:
    score = 0
    latest = frame.iloc[-1]
    if pd.notna(latest["MA5"]) and latest["close"] > latest["MA5"]:
        score += 1
    if pd.notna(latest["MA13"]) and latest["close"] > latest["MA13"]:
        score += 1
    if pd.notna(latest["MA21"]) and latest["close"] > latest["MA21"]:
        score += 2
    if pd.notna(latest["MA60"]) and latest["close"] > latest["MA60"]:
        score += 2
    if pd.notna(latest["ATR14"]) and latest["ATR14"] > 0:
        score += 1
    if pd.notna(latest["volume_ma20"]) and latest["volume"] > latest["volume_ma20"]:
        score += 1
    return min(score, 10)


def _build_signal_base(
    stock_id: str,
    stock_name: str,
    frame: pd.DataFrame,
    strategy_code: str,
    technical_signal_type: str,
    sub_signal_type: str,
) -> dict:
    latest = frame.iloc[-1]
    ma_ctx = _ma_support_context(frame)
    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "signal_date": str(latest["date"].date()) if hasattr(latest["date"], "date") else str(latest["date"])[:10],
        "strategy_code": strategy_code,
        "technical_signal_type": technical_signal_type,
        "sub_signal_type": sub_signal_type,
        "close": round(float(latest["close"]), 2),
        "ma_context": ma_ctx,
        "macd_context": {
            "dif": round(float(latest["DIF"]), 4) if pd.notna(latest["DIF"]) else None,
            "dea": round(float(latest["DEA"]), 4) if pd.notna(latest["DEA"]) else None,
            "hist": round(float(latest["MACD_HIST"]), 4) if pd.notna(latest["MACD_HIST"]) else None,
        },
        "kd_context": {
            "k": round(float(latest["K"]), 2) if pd.notna(latest["K"]) else None,
            "d": round(float(latest["D"]), 2) if pd.notna(latest["D"]) else None,
        },
        "volume_quality": bool(latest["volume"] > latest["volume_ma20"]) if pd.notna(latest["volume"]) and pd.notna(latest["volume_ma20"]) and latest["volume_ma20"] > 0 else False,
        "technical_setup_score": _score_technical_setup(frame),
        "initial_invalid_price": round(float(latest["MA21"]), 2) if pd.notna(latest["MA21"]) else None,
        "structural_invalid_price": round(float(latest["MA60"]), 2) if pd.notna(latest["MA60"]) else None,
        "risk_distance_pct": None,
        "risk_distance_atr": round(float(latest["ATR14"]), 4) if pd.notna(latest["ATR14"]) else None,
        "notes": "",
        "features": {},
    }


def _find_two_pivot_lows_in_zone(
    frame: pd.DataFrame,
    zone_start: int,
    zone_end: int,
) -> list[tuple[float, str, int]]:
    """Find up to two pivot lows in [zone_start, zone_end] with right-side confirmation.

    Returns list of (price, date_str, idx) sorted by index ascending (oldest first).
    A pivot low at index t requires both left and right confirmation bars.
    """
    if zone_end - zone_start < 7:
        return []

    pivots: list[tuple[float, str, int]] = []
    for t in range(zone_start + 3, zone_end - 3):
        t_low = float(frame["low"].iloc[t])
        left_ok = any(float(frame["low"].iloc[j]) > t_low for j in range(t - 3, t))
        right_ok = any(float(frame["low"].iloc[j]) > t_low for j in range(t + 1, t + 4))
        if left_ok and right_ok:
            date_str = str(frame["date"].iloc[t].date()) if hasattr(frame["date"].iloc[t], "date") else str(frame["date"].iloc[t])[:10]
            pivots.append((t_low, date_str, t))
    return pivots


def _detect_strategy_a(frame: pd.DataFrame, stock_id: str, stock_name: str) -> list[dict]:
    """Strategy A: bull pullback MA21 breakout (reclaiming MA21 after pullback).

    Wave-cycle flow:
      1. Find last COMPLETED red zone (MACD_HIST > 0 that has already turned <= 0).
      2. Wave zone = green zone immediately before that red zone.
      3. pullback_zone = rows after wave_red_end up to (but not including) signal day.
      4. A1: Direct MA21 break — pullback may or may not break below wave_low.
      5. A2: Two pivot lows in pullback; second pivot >= first pivot; today breaks MA21.
      6. A3: Pullback broke MA105/MA144; today reclaims MA21 AND MA105/MA144 same day.
    """
    if len(frame) < 2:
        return []

    prev = frame.iloc[-2]
    latest = frame.iloc[-1]

    # Strategy A is pullback recovery — reject stocks already in MACD red for multiple days
    if latest["MACD_HIST"] > 0 and prev["MACD_HIST"] > 0:
        return []

    wave_data = _find_macd_wave_cycle(frame)
    if wave_data is None:
        return []

    wave_low = wave_data["wave_low"]
    wave_high = wave_data["wave_high"]
    wave_return_pct = wave_data["wave_return_pct"]

    # Minimum prior wave return threshold: filter out tiny advances
    if wave_return_pct < 5.0:
        return []

    wave_red_end_idx = wave_data["wave_end_idx_red"]
    pullback_start = wave_red_end_idx + 1
    pullback_end = len(frame) - 1

    if pullback_start >= pullback_end:
        return []

    pullback_zone = frame.iloc[pullback_start:pullback_end]
    pullback_low = float(pullback_zone["low"].min())
    pullback_low_idx = int(pullback_zone["low"].idxmin())
    pullback_low_date = str(frame["date"].iloc[pullback_low_idx].date()) if hasattr(frame["date"].iloc[pullback_low_idx], "date") else str(frame["date"].iloc[pullback_low_idx])[:10]
    # Recalculate pullback_low from original idx
    pullback_low = float(frame["low"].iloc[pullback_low_idx])

    wave_height = wave_high - wave_low
    if wave_height <= 0:
        return []
    retracement_ratio = (wave_high - pullback_low) / wave_height
    # Sanity check: reject abnormal retracement ratios from broken wave cycles
    if retracement_ratio < 0 or retracement_ratio > 2.0:
        return []

    close = float(latest["close"])
    prev_close = float(prev["close"])
    ma21 = float(latest["MA21"]) if pd.notna(latest["MA21"]) else None
    prev_ma21 = float(prev["MA21"]) if pd.notna(prev["MA21"]) else None
    ma105 = float(latest["MA105"]) if pd.notna(latest["MA105"]) else None
    prev_ma105 = float(prev["MA105"]) if pd.notna(prev["MA105"]) else None
    ma144 = float(latest["MA144"]) if pd.notna(latest["MA144"]) else None
    prev_ma144 = float(prev["MA144"]) if pd.notna(prev["MA144"]) else None

    pullback_ma105_broken = ma105 is not None and any(float(frame["low"].iloc[j]) < ma105 for j in range(pullback_start, pullback_end))
    pullback_ma144_broken = ma144 is not None and any(float(frame["low"].iloc[j]) < ma144 for j in range(pullback_start, pullback_end))

    def _sig(sub: str) -> dict:
        sig = _build_signal_base(stock_id, stock_name, frame, "A", "bull_pullback_ma21_breakout", sub)
        sig["features"] = {
            "wave_start_date": wave_data["wave_start_date"],
            "wave_green_end_date": wave_data["wave_green_end_date"],
            "wave_red_start_date": wave_data["wave_red_start_date"],
            "wave_red_end_date": wave_data["wave_red_end_date"],
            "wave_end_date": wave_data["wave_end_date"],
            "wave_low": wave_low,
            "wave_high": wave_high,
            "pullback_low": pullback_low,
            "pullback_low_date": pullback_low_date,
            "retracement_ratio": retracement_ratio,
            "pullback_ma105_broken": pullback_ma105_broken,
            "pullback_ma144_broken": pullback_ma144_broken,
        }
        return sig

    signals: list[dict] = []

    # A1: Direct MA21 breakout (prev below MA21, today above MA21)
    # pullback may or may not break below wave_low; check is informational only
    if ma21 is not None and prev_ma21 is not None:
        if prev_close <= prev_ma21 and close > ma21:
            sig = _sig("A1_direct_ma21_breakout")
            sig["notes"] = f"wave_return={wave_return_pct:.1f}%, retracement={retracement_ratio:.2f}"
            signals.append(sig)

    # A2: Two pivot lows in pullback zone; 2nd >= 1st (no new low); today breaks MA21
    # pullback may break wave_low; pivot check ensures 2nd pivot doesn't undercut 1st
    pivots = _find_two_pivot_lows_in_zone(frame, pullback_start, pullback_end)
    if len(pivots) >= 2 and ma21 is not None:
        p1_price, p1_date, p1_idx = pivots[-2]
        p2_price, p2_date, p2_idx = pivots[-1]
        if p2_price >= p1_price and prev_close <= prev_ma21 and close > ma21:
            sig = _sig("A2_pivot_low_reclaim_ma21")
            sig["notes"] = f"pivot1={p1_date}@{p1_price:.2f}, pivot2={p2_date}@{p2_price:.2f}, retracement={retracement_ratio:.2f}"
            sig["features"]["pivot1_low"] = p1_price
            sig["features"]["pivot1_date"] = p1_date
            sig["features"]["pivot2_low"] = p2_price
            sig["features"]["pivot2_date"] = p2_date
            signals.append(sig)

    # A3: Pullback broke MA105/MA144; today reclaims MA21 AND (MA105 or MA144) same day
    ma105_reclaimed = ma105 is not None and prev_ma105 is not None and prev_close <= prev_ma105 and close > ma105
    ma144_reclaimed = ma144 is not None and prev_ma144 is not None and prev_close <= prev_ma144 and close > ma144
    ma21_break = ma21 is not None and prev_ma21 is not None and prev_close <= prev_ma21 and close > ma21

    if ma21_break:
        if ma105_reclaimed and pullback_ma105_broken:
            sig = _sig("A3_reclaim_ma21_and_long_ma")
            sig["notes"] = "reclaimed MA21 and MA105 same day"
            signals.append(sig)
        elif ma144_reclaimed and pullback_ma144_broken:
            sig = _sig("A3_reclaim_ma21_and_long_ma")
            sig["notes"] = "reclaimed MA21 and MA144 same day"
            signals.append(sig)

    return signals


# -------------------------------------------------------------------
# Strategy B: strong_trend_retest_breakout
# -------------------------------------------------------------------


def _detect_strategy_b(frame: pd.DataFrame, stock_id: str, stock_name: str) -> list[dict]:
    """Strategy B: strong MACD red-zone retest breakout.

    Requires MACD_HIST > 0 throughout.
    B1: Today low touches/broks MA13 or MA21, close reclaims.
    B2: 1-3 days ago broke MA13/MA21, today reclaims.
    B3: Retest MA13/MA21 then break MACD red-zone high (or retest high) during red zone.
    """
    if len(frame) < 5:
        return []

    latest = frame.iloc[-1]
    prev = frame.iloc[-2]
    prev2 = frame.iloc[-3] if len(frame) >= 3 else None

    if latest["MACD_HIST"] <= 0:
        return []

    ma13 = float(latest["MA13"]) if pd.notna(latest["MA13"]) else None
    ma21 = float(latest["MA21"]) if pd.notna(latest["MA21"]) else None
    ma60 = float(latest["MA60"]) if pd.notna(latest["MA60"]) else None
    prev_ma13 = float(prev["MA13"]) if pd.notna(prev["MA13"]) else None
    prev_ma21 = float(prev["MA21"]) if pd.notna(prev["MA21"]) else None

    close = float(latest["close"])
    prev_close = float(prev["close"])

    signals: list[dict] = []

    def _sig(sub: str, notes: str = "") -> dict:
        s = _build_signal_base(stock_id, stock_name, frame, "B", "strong_trend_retest_breakout", sub)
        s["notes"] = notes
        return s

    # B1: Intraday touch — today's low touched MA13 or MA21, close reclaims same MA
    # Does NOT require yesterday close was below MA; today's low touching is sufficient
    # Additional strength requirement: close > prev_close AND close > MA21
    today_low_touch_ma13 = ma13 is not None and float(latest["low"]) <= ma13
    today_low_touch_ma21 = ma21 is not None and float(latest["low"]) <= ma21
    if today_low_touch_ma13 and prev_ma13 is not None and close > ma13:
        if close > prev_close and ma21 is not None and close > ma21:
            sig = _sig("B1_intraday_retest_reclaim_ma", "B1｜當日低點碰觸 MA13 後收復")
            sig["features"] = {"retest_ma": "MA13"}
            signals.append(sig)
    elif today_low_touch_ma21 and prev_ma21 is not None and close > ma21:
        if close > prev_close and close > ma21:
            sig = _sig("B1_intraday_retest_reclaim_ma", "B1｜當日低點碰觸 MA21 後收復")
            sig["features"] = {"retest_ma": "MA21"}
            signals.append(sig)

    # B2: Short-term reclaim — within 1-3 days, broken MA13/MA21, today reclaimed
    # Check rows -2, -3, -4 for break; only one of them needs to be a break
    # Additional requirements: MACD_HIST>0, close>prev_close, close>MA21, not broken MA60
    for lookback in range(2, 5):  # 2=prev day, 3=2 days ago, 4=3 days ago
        if lookback >= len(frame):
            continue
        row_lb = frame.iloc[-lookback]
        ma13_lb = float(row_lb["MA13"]) if pd.notna(row_lb["MA13"]) else None
        ma21_lb = float(row_lb["MA21"]) if pd.notna(row_lb["MA21"]) else None
        ma60_lb = float(row_lb["MA60"]) if pd.notna(row_lb["MA60"]) else None
        close_lb = float(row_lb["close"])
        broken_ma13 = ma13_lb is not None and close_lb <= ma13_lb
        broken_ma21 = ma21_lb is not None and close_lb <= ma21_lb
        broken_ma60 = ma60_lb is not None and close_lb <= ma60_lb
        # Reject if MA60 was broken in the same lookback window
        if broken_ma60:
            continue
        if broken_ma13 and close > ma13 and latest["MACD_HIST"] > 0:
            if close > prev_close and ma21 is not None and close > ma21:
                sig = _sig("B2_short_reclaim_after_break_ma", f"B2｜{lookback - 1}日前跌破 MA13 後今日收復")
                sig["features"] = {"broken_ma": "MA13", "days_since_break": lookback - 1}
                signals.append(sig)
                break
        elif broken_ma21 and close > ma21 and latest["MACD_HIST"] > 0:
            if close > prev_close and close > ma21:
                sig = _sig("B2_short_reclaim_after_break_ma", f"B2｜{lookback - 1}日前跌破 MA21 後今日收復")
                sig["features"] = {"broken_ma": "MA21", "days_since_break": lookback - 1}
                signals.append(sig)
                break

    # B3: Retest then break MACD red-zone high (or retest high) — supports MA13 or MA21
    # Find current red zone start: transition from non-positive (green/zero) to positive (red)
    # non_pos = indices where MACD_HIST <= 0; red starts at i where i-1 <= 0 and i > 0
    non_pos = [i for i in range(1, len(frame)) if frame["MACD_HIST"].iloc[i - 1] <= 0 < frame["MACD_HIST"].iloc[i]]
    if not non_pos:
        return signals
    red_start = non_pos[-1]  # most recent green->red transition is start of current red zone
    red_zone = frame.iloc[red_start:-1]
    if red_zone.empty or len(red_zone) < 2:
        return signals

    # Check: did price retest MA13 or MA21 during this red zone (using per-day MA values)?
    ma13_retest = any(
        pd.notna(red_zone["MA13"].iloc[j]) and float(red_zone["low"].iloc[j]) <= float(red_zone["MA13"].iloc[j])
        for j in range(len(red_zone))
    )
    ma21_retest = any(
        pd.notna(red_zone["MA21"].iloc[j]) and float(red_zone["low"].iloc[j]) <= float(red_zone["MA21"].iloc[j])
        for j in range(len(red_zone))
    )

    # B3 MA60 exclusion: skip the red-zone start day (day 0).
    # Only check from day 1 onward, using close < MA60 (not low < MA60).
    ma60_broken_in_red = False
    for j in range(1, len(red_zone)):
        if pd.notna(red_zone["MA60"].iloc[j]) and float(red_zone["close"].iloc[j]) < float(red_zone["MA60"].iloc[j]):
            ma60_broken_in_red = True
            break

    retest_ma = None
    if ma13_retest and not ma60_broken_in_red:
        retest_ma = "MA13"
    elif ma21_retest and not ma60_broken_in_red:
        retest_ma = "MA21"

    if retest_ma is None:
        return signals

    # Breakout high = highest high in the red zone BEFORE the last retest cluster.
    # Touches are grouped into clusters: adjacent touches within 3 bars belong to the same cluster.
    # The last cluster's first touch is the reference point; breakout_high uses the high before it.
    if retest_ma == "MA13":
        touch_idx_list = [
            j for j in range(len(red_zone))
            if pd.notna(red_zone["MA13"].iloc[j])
            and float(red_zone["low"].iloc[j]) <= float(red_zone["MA13"].iloc[j])
        ]
    else:
        touch_idx_list = [
            j for j in range(len(red_zone))
            if pd.notna(red_zone["MA21"].iloc[j])
            and float(red_zone["low"].iloc[j]) <= float(red_zone["MA21"].iloc[j])
        ]

    if not touch_idx_list:
        return signals

    # Group touches into clusters: touches within 3 bars belong to same retest cluster.
    # Use the START of the LAST cluster as the reference point for breakout_high.
    last_cluster_start = touch_idx_list[0]
    last_cluster_end = touch_idx_list[0]
    prev_idx = touch_idx_list[0]
    for idx in touch_idx_list[1:]:
        if idx - prev_idx > 3:
            last_cluster_start = idx
            last_cluster_end = idx
        else:
            last_cluster_end = idx
        prev_idx = idx

    pre_touch = red_zone.iloc[:last_cluster_start]
    if pre_touch.empty:
        return signals

    breakout_high = float(pre_touch["high"].max())

    # B3 must be the FIRST close breakout after the last retest cluster.
    # If any close after the last cluster (up to yesterday) already exceeded breakout_high, skip.
    post_retest_before_today = red_zone.iloc[last_cluster_end + 1 :]
    if not post_retest_before_today.empty:
        already_broke = (post_retest_before_today["close"].astype(float) > breakout_high).any()
        if already_broke:
            return signals

    # Today breaks that high and today close is above retest MA (no requirement of MA21 break today)
    today_above_retest_ma = False
    if retest_ma == "MA13" and ma13 is not None:
        today_above_retest_ma = close > ma13
    elif retest_ma == "MA21" and ma21 is not None:
        today_above_retest_ma = close > ma21

    if float(prev["close"]) <= breakout_high and close > breakout_high and today_above_retest_ma:
        sig = _sig("B3_breakout_after_retest", f"B3｜回測 {retest_ma} 後突破前高")
        sig["features"] = {"retest_ma": retest_ma, "breakout_high": breakout_high}
        signals.append(sig)

    return signals


# -------------------------------------------------------------------
# Strategy C: bullish_divergence_reversal
# -------------------------------------------------------------------
def _find_green_zones_for_divergence(frame: pd.DataFrame) -> list[dict]:
    """Find MACD green-zone sections (negative histogram) with their price lows.

    Strategy C uses the two most recent completed MACD_HIST < 0 zones
    to detect bullish divergence: Zone2 (most recent) price low must be
    lower than Zone1 (previous) price low, while Zone2 histogram low
    must be higher than Zone1 histogram low.
    """
    if len(frame) < 30:
        return []
    zones: list[dict] = []
    in_zone = False
    zone_start = 0
    zone_low = float("inf")
    zone_end = 0
    for i in range(len(frame)):
        is_green = frame["MACD_HIST"].iloc[i] < 0
        if is_green and not in_zone:
            in_zone = True
            zone_start = i
            zone_low = frame["low"].iloc[i]
        elif is_green and in_zone:
            zone_low = min(zone_low, frame["low"].iloc[i])
            zone_end = i
        elif not is_green and in_zone:
            in_zone = False
            zones.append(
                {
                    "start": zone_start,
                    "end": zone_end,
                    "low": zone_low,
                    "hist_min": frame["MACD_HIST"].iloc[zone_start:zone_end + 1].min(),
                }
            )
    return zones


def _detect_strategy_c(frame: pd.DataFrame, stock_id: str, stock_name: str) -> list[dict]:
    """Strategy C: bullish divergence / below-zero red-zone breakout.

    C1: MACD bullish divergence (DIF<0, zone2 lower than zone1, histogram divergence) + MA21 break.
    C2: Below zero MACD red-zone consolidation breakout — DIF<0, MACD_HIST>0, price
        consolidates during red zone, MACD not deteriorating, today breaks MA21 with bullish candle.
    C3 (KD divergence) is not output as a standalone scan signal.
    """
    if len(frame) < 30:
        return []

    latest = frame.iloc[-1]
    prev = frame.iloc[-2]

    close = float(latest["close"])
    prev_close = float(prev["close"])
    ma21 = float(latest["MA21"]) if pd.notna(latest["MA21"]) else None
    prev_ma21 = float(prev["MA21"]) if pd.notna(prev["MA21"]) else None

    signals: list[dict] = []

    # --- C1: MACD bullish divergence + MA21 break ---
    if pd.notna(latest["DIF"]) and latest["DIF"] < 0:
        zones = _find_green_zones_for_divergence(frame)
        if len(zones) >= 2:
            zone1 = zones[-2]
            zone2 = zones[-1]
            # Divergence: zone2 price lower, zone2 hist higher
            if zone2["low"] < zone1["low"] and zone2["hist_min"] > zone1["hist_min"]:
                ma21_broken = ma21 is not None and prev_ma21 is not None and prev_close <= prev_ma21 and close > ma21
                if ma21_broken:
                    sig = _build_signal_base(stock_id, stock_name, frame, "C", "bullish_divergence_reversal", "C1_macd_bullish_divergence_break_ma21")
                    sig["notes"] = f"DIF<0, 底背離, zone2低點={zone2['low']:.2f} < zone1低點={zone1['low']:.2f}"
                    sig["features"] = {
                        "zone1_low": zone1["low"], "zone2_low": zone2["low"],
                        "zone1_hist_min": zone1["hist_min"], "zone2_hist_min": zone2["hist_min"],
                    }
                    signals.append(sig)

    # --- C2: Below-zero MACD red-zone consolidation breakout ---
    # DIF < 0, MACD_HIST > 0 (red zone below zero), price consolidating, today breaks MA21
    if latest["MACD_HIST"] > 0 and pd.notna(latest["DIF"]) and latest["DIF"] < 0:
        # Count consecutive red-zone days before today
        red_days = 0
        for i in range(len(frame) - 2, -1, -1):
            if frame["MACD_HIST"].iloc[i] > 0:
                red_days += 1
            else:
                break
        if red_days >= 3:
            ma21_broken = ma21 is not None and prev_ma21 is not None and prev_close <= prev_ma21 and close > ma21
            is_bullish = close > float(latest["open"])
            if ma21_broken and is_bullish:
                sig = _build_signal_base(stock_id, stock_name, frame, "C", "below_zero_red_consolidation_breakout", "C2_below_zero_red_histogram_breakout")
                sig["notes"] = f"C2｜0軸下紅柱鈍化突破，red_days={red_days}"
                sig["features"] = {"red_zone_days": red_days}
                signals.append(sig)

    return signals


# -------------------------------------------------------------------
# Strategy D: strong_stock_shakeout_reclaim
# -------------------------------------------------------------------
def _detect_strategy_d(frame: pd.DataFrame, stock_id: str, stock_name: str) -> list[dict]:
    """Strategy D: strong stock shakeout reversal.

    Background check: close above 60MA/105MA OR DIF>0 OR prior MACD red zone.
    D1: Broke MA5/MA13/MA21 within 1-3 days, today reclaims.
    D2: MACD high column flip to green (shakeout from strength), quick reversal.
    D3: KD death cross, 1-3 days, quick reversal.
    D4: Hammer candle reclaim.
    All D sub-signals include 高風險短線策略 in notes.
    """
    if len(frame) < 5:
        return []

    latest = frame.iloc[-1]
    prev = frame.iloc[-2]

    # --- Strong background check ---
    close = float(latest["close"])
    ma60 = float(latest["MA60"]) if pd.notna(latest["MA60"]) else None
    ma105 = float(latest["MA105"]) if pd.notna(latest["MA105"]) else None
    dif = float(latest["DIF"]) if pd.notna(latest["DIF"]) else None
    above_ma = (ma60 is not None and close > ma60) or (ma105 is not None and close > ma105)
    dif_pos = dif is not None and dif > 0
    # Prior MACD red zone: was red in last 5 days
    prior_red_zone = any(frame["MACD_HIST"].iloc[j] > 0 for j in range(max(0, len(frame) - 6), len(frame) - 1))
    # Background must be: price above MA60/MA105 AND (DIF>0 OR prior red zone in last 5 days)
    if not (above_ma and (dif_pos or prior_red_zone)):
        return []

    close = float(latest["close"])
    prev_close = float(prev["close"])
    ma5 = float(latest["MA5"]) if pd.notna(latest["MA5"]) else None
    ma13 = float(latest["MA13"]) if pd.notna(latest["MA13"]) else None
    ma21 = float(latest["MA21"]) if pd.notna(latest["MA21"]) else None
    prev_ma5 = float(prev["MA5"]) if pd.notna(prev["MA5"]) else None
    prev_ma13 = float(prev["MA13"]) if pd.notna(prev["MA13"]) else None
    prev_ma21 = float(prev["MA21"]) if pd.notna(prev["MA21"]) else None

    RISK = "高風險短線策略"
    signals: list[dict] = []

    def _sig(sub: str, notes: str) -> dict:
        s = _build_signal_base(stock_id, stock_name, frame, "D", "strong_stock_shakeout_reclaim", sub)
        s["notes"] = notes
        return s

    # D1: Broke MA13/MA21 within 1-3 days, today reclaims same MA
    # MA5 is auxiliary (not primary break target for D1)
    # Requires bullish candle today (close > open) and close > prev_close
    is_bullish_today = close > float(latest["open"])
    for lookback in range(2, 5):  # 2=yesterday, 3=2 days ago, 4=3 days ago
        if lookback >= len(frame):
            continue
        row_lb = frame.iloc[-lookback]
        ma13_lb = float(row_lb["MA13"]) if pd.notna(row_lb["MA13"]) else None
        ma21_lb = float(row_lb["MA21"]) if pd.notna(row_lb["MA21"]) else None
        close_lb = float(row_lb["close"])
        if ma13_lb is not None and close_lb <= ma13_lb and close > ma13:
            if close > prev_close and is_bullish_today:
                sig = _sig("D1_reclaim_ma_after_break", f"{RISK}｜{lookback - 1}日前跌破 MA13 後今日收復")
                sig["features"] = {"reclaimed_ma": "MA13", "days_since_break": lookback - 1}
                signals.append(sig)
            break
        elif ma21_lb is not None and close_lb <= ma21_lb and close > ma21:
            if close > prev_close and is_bullish_today:
                sig = _sig("D1_reclaim_ma_after_break", f"{RISK}｜{lookback - 1}日前跌破 MA21 後今日收復")
                sig["features"] = {"reclaimed_ma": "MA21", "days_since_break": lookback - 1}
                signals.append(sig)
            break

    # D2: MACD high column flip to green (just turned from high red to green, quick reversal)
    # Flip: prev MACD_HIST > 0, today <= 0. Quick reversal requires DIF still > 0 (above zero axis)
    # and today shows strength: red K reclaimed MA5/MA13, or just bullish candle.
    if len(frame) >= 3:
        prev_hist = float(frame["MACD_HIST"].iloc[-2])
        curr_hist = float(latest["MACD_HIST"])
        dif = float(latest["DIF"]) if pd.notna(latest["DIF"]) else None
        # DIF must still be above zero axis for "high column flip" context
        if prev_hist > 0 and curr_hist <= 0 and dif is not None and dif > 0:
            # Quick reversal: today closes above MA5/MA13 OR bullish candle
            close_above_ma5 = ma5 is not None and close > ma5
            close_above_ma13_ma = ma13 is not None and close > ma13
            is_bullish = close > float(latest["open"])
            if close_above_ma5 or close_above_ma13_ma or is_bullish:
                sig = _sig("D2_macd_high_column_flip_green", f"{RISK}｜MACD 高檔紅柱翻綠後快速反轉")
                sig["features"] = {"prev_hist": prev_hist, "curr_hist": curr_hist, "dif": dif, "reclaim_ma": "MA5" if close_above_ma5 else ("MA13" if close_above_ma13_ma else "bullish_candle")}
                signals.append(sig)

    # D3: KD death cross quick reversal — KD death cross within 1-3 days, quick reversal today
    # Death cross = K >= D then K < D (K crosses DOWN through D)
    # Use explicit days_ago (1=yesterday, 2=2days ago, 3=3days ago)
    kd_death_days = None
    for days_ago in range(1, 4):
        death_idx = len(frame) - 1 - days_ago
        prev_idx = death_idx - 1
        if prev_idx < 0:
            continue
        prev_bar = frame.iloc[prev_idx]
        death_bar = frame.iloc[death_idx]
        if pd.notna(prev_bar["K"]) and pd.notna(prev_bar["D"]) and pd.notna(death_bar["K"]) and pd.notna(death_bar["D"]):
            if prev_bar["K"] >= prev_bar["D"] and death_bar["K"] < death_bar["D"]:
                kd_death_days = days_ago
                break
    if kd_death_days is not None:
        # Quick reversal today: K>D (golden cross), OR red K, OR reclaim MA5/MA13
        kd_quick = pd.notna(latest["K"]) and pd.notna(latest["D"]) and latest["K"] > latest["D"]
        close_above_ma5 = ma5 is not None and close > ma5
        close_above_ma13_ma = ma13 is not None and close > ma13
        is_bullish = close > float(latest["open"])
        if kd_quick or close_above_ma5 or close_above_ma13_ma or is_bullish:
            sig = _sig("D3_kd_death_cross_quick_reversal", f"{RISK}｜KD 死叉後 {kd_death_days} 日快速轉強")
            sig["features"] = {"days_since_kd_death_cross": kd_death_days, "quick_reversal_type": "KD_golden_cross" if kd_quick else ("MA_reclaim" if (close_above_ma5 or close_above_ma13_ma) else "bullish_candle")}
            signals.append(sig)

    # D4: Shakeout / long shadow reclaim MA13 (not just MA5)
    # Requires strong background AND at least one shakeout condition:
    #   (a) recent 1-3 day break of MA13/MA21, OR (b) long lower shadow today
    # Must also reclaim MA13 (not just MA5) AND bullish candle (close > open)
    open_p = float(latest["open"])
    low_p = float(latest["low"])
    body = abs(close - open_p)
    lower_shadow = min(open_p, close) - low_p
    is_hammer = lower_shadow > body * 2 and close > open_p
    # Check for recent MA13/MA21 break (shakeout signal)
    recent_ma_break = False
    recent_ma_type = None
    for lookback in range(2, 5):
        if lookback >= len(frame):
            continue
        row_lb = frame.iloc[-lookback]
        ma13_lb = float(row_lb["MA13"]) if pd.notna(row_lb["MA13"]) else None
        ma21_lb = float(row_lb["MA21"]) if pd.notna(row_lb["MA21"]) else None
        close_lb = float(row_lb["close"])
        if ma13_lb is not None and close_lb <= ma13_lb:
            recent_ma_break = True
            recent_ma_type = "MA13"
            break
        elif ma21_lb is not None and close_lb <= ma21_lb:
            recent_ma_break = True
            recent_ma_type = "MA21"
            break
    # MA13 reclaim required (not just MA5); also require bullish candle today
    ma13_reclaim = ma13 is not None and close > ma13
    is_bullish_today = close > open_p
    shakeout_ok = recent_ma_break or is_hammer
    if shakeout_ok and ma13_reclaim and is_bullish_today:
        sig = _sig("D4_hammer_candle_reclaim", f"{RISK}｜急跌或長下影後收復 MA5/MA13")
        sig["features"] = {
            "lower_shadow": lower_shadow,
            "body": body,
            "shakeout_ma": recent_ma_type,
            "reclaim_ma": "MA13",
        }
        signals.append(sig)

    return signals


# -------------------------------------------------------------------
# Main entry point
# -------------------------------------------------------------------
def detect_technical_strategies(
    frame: pd.DataFrame,
    stock_id: str = "",
    stock_name: str = "",
) -> list[dict]:
    """Detect all four technical strategies (A/B/C/D) from a prepared DataFrame.

    Args:
        frame: DataFrame already processed by technical_scanner.apply_indicators().
               Must contain columns: date, open, high, low, close, volume,
               MA5, MA13, MA21, MA60, MA105, MA144, DIF, DEA, MACD_HIST,
               K, D, ATR14, volume_ma20.
        stock_id: Stock code (e.g. "2330")
        stock_name: Stock name (e.g. "台積電")

    Returns:
        List of signal dicts. Empty list if no signals or insufficient data.
        Each dict contains:
            stock_id, stock_name, signal_date, strategy_code (A/B/C/D),
            technical_signal_type, sub_signal_type, close, ma_context,
            macd_context, kd_context, volume_quality, technical_setup_score,
            initial_invalid_price, structural_invalid_price, risk_distance_pct,
            risk_distance_atr, notes, features.
    """
    if frame is None or frame.empty:
        return []

    required_cols = ["date", "open", "high", "low", "close", "volume", "MACD_HIST", "K", "D"]
    if not all(c in frame.columns for c in required_cols):
        return []

    try:
        # Dropna on essential columns, keep enough rows
        work = frame.dropna(subset=["close", "MACD_HIST", "K", "D"]).reset_index(drop=True)
        if len(work) < 5:
            return []

        signals: list[dict] = []
        signals.extend(_detect_strategy_a(work, stock_id, stock_name))
        signals.extend(_detect_strategy_b(work, stock_id, stock_name))
        signals.extend(_detect_strategy_c(work, stock_id, stock_name))
        signals.extend(_detect_strategy_d(work, stock_id, stock_name))
        return signals

    except Exception:
        return []