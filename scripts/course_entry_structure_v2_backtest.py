"""Causal replay of the revised course entry/structure state machine.

This research-only adapter keeps the existing corporate-action, position,
cost and exit simulator unchanged.  It changes only the entry discovery:

    radar monitoring -> large context -> pullback zone -> small close trigger
    -> next-session open

Q1 momentum breakouts are kept as a separate signal family.  A moving-average
touch is a zone observation, never an entry by itself.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_add_vs_no_add_backtest import Costs, MODELS, simulate_trade, summarize  # noqa: E402
from scripts.course_corporate_action_backtest import (  # noqa: E402
    adjusted_history,
    event_dates,
    load_inputs,
    transform_price,
)
from scripts.course_corporate_action_data import AS_OF, RUN, read, save  # noqa: E402
from scripts.course_daily_screen_trial import THRESHOLDS, _candles  # noqa: E402
from scripts.course_dual_scale_entry_backtest import _history_for_epoch  # noqa: E402
from scripts.course_radar_trigger_backtest import _index_selections  # noqa: E402
from scripts.course_watchlist_backtest import enlightenment_snapshot  # noqa: E402
from trade_monitor.pivot_replay import detect_local_pivots, pair_pivots, secondary_pivots  # noqa: E402


METHOD_VERSION = "course-entry-structure-v2"
SELECTION_SOURCE = RUN / "selection_snapshot.json"
CONTROL_SOURCE = RUN / "quality_pullback_resonance_v1" / "comparison.json"
OUTPUT_DIR = RUN / "entry_structure_v2"
EARLIEST_SAVED_RADAR_DATE = "2026-05-21"

STATE_LABELS = {
    "MONITORING": "MONITORING（監控中）",
    "LEFT_CENSORED": "LEFT_CENSORED（監控起點遭截斷）",
    "LARGE_CONTEXT_READY": "LARGE_CONTEXT_READY（大結構背景成立）",
    "PULLBACK_ZONE": "PULLBACK_ZONE（大級拉回區）",
    "SMALL_TRIGGER_FORMING": "SMALL_TRIGGER_FORMING（小級觸發形成中）",
    "CONFIRMED_PENDING_ENTRY": "CONFIRMED_PENDING_ENTRY（收盤確認待進場）",
    "REANCHOR_REQUIRED": "REANCHOR_REQUIRED（需重新定錨）",
    "LATE_CYCLE": "LATE_CYCLE（末段降級）",
    "INVALIDATED": "INVALIDATED（結構失效）",
    "EXPIRED": "EXPIRED（監控逾期）",
}

FAMILY_LABELS = {
    "EARLY_TAIJI_PULLBACK": "EARLY_TAIJI_PULLBACK（早期太極修正後轉強）",
    "LONG_MA_PULLBACK": "LONG_MA_PULLBACK（長均線拉回後轉強）",
    "Q1_MOMENTUM_BREAKOUT": "Q1_MOMENTUM_BREAKOUT（第一象限動能突破）",
}

VARIANT_LABELS = {
    "CONTROL_V1": "CONTROL_V1（舊版高品質拉回＋大小級共振）",
    "PULLBACK_V2": "PULLBACK_V2（新版拉回區＋小結構確認）",
    "LONG_MA_V2": "LONG_MA_V2（只取55／105／144MA拉回）",
    "Q1_BREAKOUT_V2": "Q1_BREAKOUT_V2（新版第一象限突破）",
    "COMBINED_V2": "COMBINED_V2（新版拉回＋第一象限突破）",
}


@dataclass(frozen=True)
class EntryStructureSpec:
    monitor_bars: int = 60
    left_censored_warmup_bars: int = 5
    zone_max_age_bars: int = 12
    zone_tolerance_pct: float = 0.02
    zone_close_floor_pct: float = 0.03
    min_volume_ratio: float = 0.60
    q1_min_volume_ratio: float = 1.20
    max_risk_pct: float = 8.0
    max_risk_atr: float = 2.0
    next_open_chase_atr: float = 0.50
    max_signal_range_atr: float = 2.0
    min_avg_volume_lots: float = 500.0
    boundary_lookback: int = 120
    platform_lookback: int = 55


SPEC = EntryStructureSpec()


def _mean(values: list[float]) -> float | None:
    return None if not values else statistics.fmean(values)


def _f(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _sequence_number(snapshot: dict[str, Any]) -> int | None:
    sequence = str(snapshot["taiji"].get("sequence") or "")
    if sequence.startswith("LEG_"):
        try:
            return int(sequence.split("_", 1)[1])
        except ValueError:
            return None
    return None


def _taiji_audit(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Make dynasty failures persistent until the underlying anchor changes."""
    taiji = snapshot["taiji"]
    legs = list(taiji.get("legs") or [])
    failed = [
        leg for leg in legs
        if bool(leg.get("confirmed")) and str(leg.get("quality")) == "FAILED"
    ]
    sequence_number = _sequence_number(snapshot)
    late = bool(
        taiji.get("late_generation")
        or sequence_number is not None and sequence_number >= 5
        or taiji.get("state") == "POST_5"
    )
    current_failure = taiji.get("state") in {"COPY_FAILED", "CORRECTION_FAILED", "POST_5"}
    return {
        "failed_completed_legs": [
            {
                "sequence_number": leg.get("sequence_number"),
                "role": leg.get("role"),
                "start_date": leg.get("start_date"),
                "end_date": leg.get("end_date"),
            }
            for leg in failed
        ],
        "reanchor_required": bool(failed or current_failure),
        "late_cycle": late,
        "sequence_number": sequence_number,
        "state": taiji.get("state"),
        "anchor_start_date": (taiji.get("anchor") or {}).get("start_date"),
    }


def _large_context(history: pd.DataFrame, snapshot: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    latest = history.iloc[-1]
    before10 = history.iloc[-11]
    before20 = history.iloc[-21]
    aligned = bool(latest.MA55 > latest.MA105 > latest.MA144)
    rising = bool(latest.MA55 > before10.MA55 and latest.MA105 > before20.MA105 and latest.MA144 >= before20.MA144)
    above_floor = bool(latest.close >= latest.MA144 * 0.97)
    direction_ok = snapshot["direction"]["large"] == "BULL"
    valid = bool(aligned and rising and above_floor and direction_ok)
    return valid, {
        "large_direction": snapshot["direction"]["large"],
        "small_direction": snapshot["direction"]["small"],
        "grade_relation": snapshot["direction"]["relation"],
        "ma_aligned": aligned,
        "ma_rising": rising,
        "above_ma144_floor": above_floor,
        "ma55": float(latest.MA55),
        "ma105": float(latest.MA105),
        "ma144": float(latest.MA144),
    }


def _fully_bearish(history: pd.DataFrame, snapshot: dict[str, Any]) -> bool:
    latest = history.iloc[-1]
    return bool(
        snapshot["direction"]["large"] == "BEAR"
        and snapshot["direction"]["small"] == "BEAR"
        and float(latest.close) < float(latest.MA144)
    )


def _intersects_line(row: pd.Series, line: float, spec: EntryStructureSpec = SPEC) -> bool:
    return bool(
        float(row.low) <= line * (1.0 + spec.zone_tolerance_pct)
        and float(row.high) >= line * (1.0 - spec.zone_tolerance_pct)
        and float(row.close) >= line * (1.0 - spec.zone_close_floor_pct)
    )


def _pullback_zone(
    history: pd.DataFrame,
    snapshot: dict[str, Any],
    taiji_audit: dict[str, Any],
    spec: EntryStructureSpec = SPEC,
) -> tuple[dict[str, Any] | None, str]:
    latest = history.iloc[-1]
    quadrant = snapshot["quadrant"]
    if quadrant.get("working_quadrant") != "Q4" or quadrant.get("state") != "CONFIRMED":
        return None, "Q4_NOT_CONFIRMED（第四象限尚未確認）"
    if taiji_audit["reanchor_required"]:
        return None, STATE_LABELS["REANCHOR_REQUIRED"]
    if taiji_audit["late_cycle"]:
        return None, STATE_LABELS["LATE_CYCLE"]

    sequence = taiji_audit["sequence_number"]
    active = (snapshot["taiji"].get("legs") or [{}])[-1]
    correction_ok = bool(
        sequence in {2, 4}
        and active.get("role") == "CORRECTION"
        and active.get("quality") in {"STRONG", "ACCEPTABLE"}
    )
    early_lines = ["MA21", "MA55"] if correction_ok else []
    deep_lines = ["MA55", "MA105", "MA144"]
    choices: list[dict[str, Any]] = []
    for line_name in dict.fromkeys(early_lines + deep_lines):
        line = float(latest[line_name])
        if not _intersects_line(latest, line, spec):
            continue
        family = "EARLY_TAIJI_PULLBACK" if correction_ok and line_name in early_lines else "LONG_MA_PULLBACK"
        choices.append({
            "family": family,
            "support_line": line_name,
            "support_price": line,
            "distance_pct": abs(float(latest.close) / line - 1.0) * 100.0,
        })
    if not choices:
        return None, "NO_PULLBACK_ZONE（尚未進入合格拉回區）"
    priority = {"MA144": 0, "MA105": 1, "MA55": 2, "MA21": 3}
    choices.sort(key=lambda row: (row["distance_pct"], priority[row["support_line"]]))
    selected = choices[0]
    return {
        **selected,
        "zone_date": latest.date.date().isoformat(),
        "quadrant": "Q4",
        "quadrant_state": "CONFIRMED",
        "taiji_state": taiji_audit["state"],
        "taiji_sequence": snapshot["taiji"].get("sequence"),
        "anchor_start_date": taiji_audit["anchor_start_date"],
    }, STATE_LABELS["PULLBACK_ZONE"]


def _small_close_trigger(
    history: pd.DataFrame,
    snapshot: dict[str, Any],
    zone: dict[str, Any],
    zone_age: int,
    spec: EntryStructureSpec = SPEC,
) -> tuple[dict[str, Any] | None, str]:
    if zone_age <= 0:
        return None, STATE_LABELS["SMALL_TRIGGER_FORMING"]
    latest = history.iloc[-1]
    previous = history.iloc[-2]
    atr = float(latest.ATR14)
    volume_ratio = 0.0 if float(latest.VOL_MA20) <= 0 else float(latest.volume / latest.VOL_MA20)
    range_atr = math.inf if atr <= 0 else float(latest.high - latest.low) / atr
    trigger_ok = bool(
        float(latest.close) > float(latest.open)
        and float(latest.close) > float(previous.high)
        and float(latest.close) > float(latest.MA13)
        and float(latest.MA5) > float(previous.MA5)
        and volume_ratio >= spec.min_volume_ratio
        and snapshot["direction"]["small"] != "BEAR"
        and range_atr <= spec.max_signal_range_atr
    )
    if not trigger_ok:
        return None, STATE_LABELS["SMALL_TRIGGER_FORMING"]
    defense_record = snapshot["structure"].get("defense")
    if not defense_record:
        return None, "NO_CONFIRMED_DEFENSE（沒有已確認小級防線）"
    day = latest.date.date().isoformat()
    if str(defense_record["confirmed_date"]) > day or str(defense_record["bar_date"]) < zone["zone_date"]:
        return None, "DEFENSE_NOT_AFTER_ZONE（拉回後尚無確認防線）"
    defense = float(defense_record["price"])
    risk = float(latest.close) - defense
    risk_pct = math.inf if latest.close <= 0 else risk / float(latest.close) * 100.0
    risk_atr = math.inf if atr <= 0 else risk / atr
    if risk <= 0 or risk_pct > spec.max_risk_pct or risk_atr > spec.max_risk_atr:
        return None, "SIGNAL_RISK_TOO_WIDE（收盤確認風險過大）"
    return {
        **zone,
        "signal_date": day,
        "signal_close": float(latest.close),
        "signal_atr": atr,
        "signal_volume_ratio": volume_ratio,
        "signal_range_atr": range_atr,
        "defense": defense,
        "defense_bar_date": defense_record["bar_date"],
        "defense_confirmed_date": defense_record["confirmed_date"],
        "planned_risk_pct": risk_pct,
        "planned_risk_atr": risk_atr,
    }, STATE_LABELS["CONFIRMED_PENDING_ENTRY"]


def _unbroken_boundary(history: pd.DataFrame, boundary: dict[str, Any]) -> bool:
    """A confirmed resistance is stale after any later close already cleared it."""
    confirmed = boundary.get("confirmed_date") or boundary.get("bar_date")
    if not confirmed:
        return False
    closes = history.loc[
        (history.date.dt.strftime("%Y-%m-%d") > str(confirmed))
        & (history.date < history.iloc[-1].date),
        "close",
    ]
    return bool(closes.empty or not (closes.astype(float) > float(boundary["price"])).any())


def _q1_boundary(history: pd.DataFrame, spec: EntryStructureSpec = SPEC) -> dict[str, Any] | None:
    if len(history) < max(145, spec.platform_lookback + 2):
        return None
    latest = history.iloc[-1]
    previous = history.iloc[-2]
    prior = history.iloc[-1 - spec.platform_lookback : -1]
    platform_index = int(prior.high.astype(float).idxmax())
    candidates = [{
        "price": float(history.loc[platform_index, "high"]),
        "source": "PRIOR_55D_HIGH（前55日平台上緣）",
        "bar_date": history.loc[platform_index, "date"].date().isoformat(),
        "confirmed_date": history.loc[platform_index, "date"].date().isoformat(),
    }]
    local, _ = detect_local_pivots(_candles(history), THRESHOLDS.pivot_n)
    paired, _, _ = pair_pivots(local)
    floor = len(history) - 1 - spec.boundary_lookback
    for pivot in secondary_pivots(paired):
        if pivot.kind == "HIGH" and pivot.bar_index >= floor and pivot.confirmation_index < len(history) - 1:
            candidates.append({
                "price": float(pivot.price),
                "source": "P2_HIGH（已確認大級樞紐高）",
                "bar_date": pivot.bar_time[:10],
                "confirmed_date": pivot.confirmation_time[:10],
            })
    crossed = [
        row for row in candidates
        if float(previous.close) <= float(row["price"]) < float(latest.close)
        and _unbroken_boundary(history, row)
    ]
    if not crossed:
        return None
    crossed.sort(key=lambda row: (abs(float(row["price"]) - float(previous.close)), 0 if row["source"].startswith("P2") else 1))
    return crossed[0]


def _q1_close_trigger(
    history: pd.DataFrame,
    snapshot: dict[str, Any],
    taiji_audit: dict[str, Any],
    spec: EntryStructureSpec = SPEC,
) -> tuple[dict[str, Any] | None, str]:
    latest = history.iloc[-1]
    quadrant = snapshot["quadrant"]
    if quadrant.get("working_quadrant") != "Q1" or quadrant.get("state") != "CONFIRMED":
        return None, "Q1_NOT_CONFIRMED（第一象限尚未確認）"
    if taiji_audit["reanchor_required"]:
        return None, STATE_LABELS["REANCHOR_REQUIRED"]
    if taiji_audit["late_cycle"]:
        return None, STATE_LABELS["LATE_CYCLE"]
    if snapshot["direction"]["small"] != "BULL":
        return None, "SMALL_NOT_BULL（小結構尚未轉多）"
    boundary = _q1_boundary(history, spec)
    if boundary is None:
        return None, "NO_FRESH_BOUNDARY_BREAK（沒有未消耗邊界突破）"
    atr = float(latest.ATR14)
    volume_ratio = 0.0 if float(latest.VOL_MA20) <= 0 else float(latest.volume / latest.VOL_MA20)
    candle_range = float(latest.high - latest.low)
    close_location = 1.0 if candle_range <= 0 else (float(latest.close) - float(latest.low)) / candle_range
    range_atr = math.inf if atr <= 0 else candle_range / atr
    if volume_ratio < spec.q1_min_volume_ratio or close_location < 0.65 or range_atr > spec.max_signal_range_atr:
        return None, "Q1_KBAR_FAILED（突破K量價品質不足）"
    defense_record = snapshot["structure"].get("defense")
    if not defense_record:
        return None, "NO_CONFIRMED_DEFENSE（沒有已確認小級防線）"
    defense = float(defense_record["price"])
    risk = float(latest.close) - defense
    risk_pct = risk / float(latest.close) * 100.0
    risk_atr = math.inf if atr <= 0 else risk / atr
    if risk <= 0 or risk_pct > spec.max_risk_pct or risk_atr > spec.max_risk_atr:
        return None, "SIGNAL_RISK_TOO_WIDE（收盤確認風險過大）"
    return {
        "family": "Q1_MOMENTUM_BREAKOUT",
        "signal_date": latest.date.date().isoformat(),
        "signal_close": float(latest.close),
        "signal_atr": atr,
        "signal_volume_ratio": volume_ratio,
        "signal_range_atr": range_atr,
        "close_location": close_location,
        "boundary": float(boundary["price"]),
        "boundary_source": boundary["source"],
        "boundary_bar_date": boundary["bar_date"],
        "boundary_confirmed_date": boundary["confirmed_date"],
        "defense": defense,
        "defense_bar_date": defense_record["bar_date"],
        "defense_confirmed_date": defense_record["confirmed_date"],
        "planned_risk_pct": risk_pct,
        "planned_risk_atr": risk_atr,
        "quadrant": "Q1",
        "quadrant_state": "CONFIRMED",
        "taiji_state": taiji_audit["state"],
        "taiji_sequence": snapshot["taiji"].get("sequence"),
        "anchor_start_date": taiji_audit["anchor_start_date"],
    }, STATE_LABELS["CONFIRMED_PENDING_ENTRY"]


def _adjust_signal_for_entry_day(signal: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    adjusted = dict(signal)
    for action in actions:
        for key in ("signal_close", "defense", "support_price", "boundary"):
            if adjusted.get(key) is not None:
                adjusted[key] = transform_price(float(adjusted[key]), action)
    return adjusted


def _next_open_entry(
    signal: dict[str, Any],
    next_row: pd.Series,
    next_atr: float,
    actions: list[dict[str, Any]],
    spec: EntryStructureSpec = SPEC,
) -> tuple[dict[str, Any] | None, str]:
    signal = _adjust_signal_for_entry_day(signal, actions)
    entry = float(next_row.open)
    defense = float(signal["defense"])
    if entry <= defense:
        return None, "OPEN_BELOW_DEFENSE（次日開盤已跌破防線）"
    if entry > float(signal["signal_close"]) + spec.next_open_chase_atr * next_atr:
        return None, "OPEN_ABOVE_CHASE_CAP（次日開盤超過追價上限）"
    risk = entry - defense
    risk_pct = risk / entry * 100.0
    risk_atr = math.inf if next_atr <= 0 else risk / next_atr
    if risk_pct > spec.max_risk_pct or risk_atr > spec.max_risk_atr:
        return None, "ACTUAL_RISK_TOO_WIDE（實際成交風險過大）"
    return {
        **signal,
        "entry_date": next_row.date.date().isoformat(),
        "entry_price": entry,
        "initial_defense": defense,
        "actual_risk_pct": risk_pct,
        "actual_risk_atr": risk_atr,
    }, "TRIGGERED（已觸發進場）"


def _record_state(events: list[dict[str, Any]], day: str, state: str, note: str | None = None) -> None:
    if events and events[-1]["state"] == state:
        return
    event = {"date": day, "state": state}
    if note:
        event["note"] = note
    events.append(event)


def discover_stock(
    *,
    item: dict[str, Any],
    selections: list[dict[str, Any]],
    spec: EntryStructureSpec = SPEC,
) -> dict[str, Any]:
    raw, actions, quality = load_inputs(item)
    mapped_actions = event_dates(actions, raw)
    selected = {row["date"].isoformat(): row for row in selections}
    day_to_index = {value.date().isoformat(): index for index, value in enumerate(raw.date)}
    usable = [day for day in selected if day in day_to_index]
    if not usable:
        return {"code": str(item["code"]), "proposals": [], "states": [], "skips": {}, "quality": quality}
    start = min(day_to_index[day] for day in usable)
    first_saved = min(usable)
    last_selected: int | None = None
    monitor_added: str | None = None
    episode = 0
    zone: dict[str, Any] | None = None
    zone_index: int | None = None
    cache: dict[tuple[str, ...], pd.DataFrame] = {}
    proposals: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    skips: Counter[str] = Counter()

    for index in range(start, len(raw) - 1):
        day = raw.iloc[index].date.date().isoformat()
        if day in selected:
            if last_selected is None or index - last_selected > spec.monitor_bars:
                episode += 1
                monitor_added = day
                zone = None
                zone_index = None
            last_selected = index
        if last_selected is None:
            continue
        if index - last_selected > spec.monitor_bars:
            _record_state(states, day, STATE_LABELS["EXPIRED"])
            zone = None
            zone_index = None
            continue
        if first_saved == EARLIEST_SAVED_RADAR_DATE and index - start < spec.left_censored_warmup_bars:
            _record_state(states, day, STATE_LABELS["LEFT_CENSORED"], "最早保存名單缺少加入監控前歷史，先觀察5根日K")
            skips[STATE_LABELS["LEFT_CENSORED"]] += 1
            continue

        history = _history_for_epoch(raw, actions, day, cache)
        if len(history) < 165:
            skips["INSUFFICIENT_HISTORY（歷史資料不足）"] += 1
            continue
        try:
            snapshot = enlightenment_snapshot(history, THRESHOLDS)
        except ValueError:
            skips["INSUFFICIENT_HISTORY（歷史資料不足）"] += 1
            continue
        audit = _taiji_audit(snapshot)
        if _fully_bearish(history, snapshot):
            _record_state(states, day, STATE_LABELS["INVALIDATED"])
            zone = None
            zone_index = None
            skips[STATE_LABELS["INVALIDATED"]] += 1
            continue
        if audit["reanchor_required"]:
            _record_state(states, day, STATE_LABELS["REANCHOR_REQUIRED"])
            zone = None
            zone_index = None
            skips[STATE_LABELS["REANCHOR_REQUIRED"]] += 1
            continue
        if audit["late_cycle"]:
            _record_state(states, day, STATE_LABELS["LATE_CYCLE"])
            zone = None
            zone_index = None
            skips[STATE_LABELS["LATE_CYCLE"]] += 1
            continue
        context_ok, context = _large_context(history, snapshot)
        if not context_ok:
            _record_state(states, day, STATE_LABELS["MONITORING"])
            skips["LARGE_CONTEXT_NOT_READY（大結構背景未成立）"] += 1
            continue
        avg_volume_lots = float(history.volume.iloc[-20:].mean()) / 1000.0
        if avg_volume_lots < spec.min_avg_volume_lots:
            skips["LIQUIDITY_FAILED（流動性不足）"] += 1
            continue
        _record_state(states, day, STATE_LABELS["LARGE_CONTEXT_READY"])

        new_zone, zone_reason = _pullback_zone(history, snapshot, audit, spec)
        if new_zone is not None:
            zone = {**new_zone, "large_context": context}
            zone_index = index
            _record_state(states, day, STATE_LABELS["PULLBACK_ZONE"], f"{zone['support_line']}只作拉回區，不直接進場")
        elif zone is None:
            skips[zone_reason] += 1
        if zone is not None and zone_index is not None and index - zone_index > spec.zone_max_age_bars:
            zone = None
            zone_index = None

        signals: list[dict[str, Any]] = []
        if zone is not None and zone_index is not None:
            signal, reason = _small_close_trigger(history, snapshot, zone, index - zone_index, spec)
            if signal is not None:
                signals.append(signal)
                _record_state(states, day, STATE_LABELS["CONFIRMED_PENDING_ENTRY"], FAMILY_LABELS[signal["family"]])
            else:
                _record_state(states, day, STATE_LABELS["SMALL_TRIGGER_FORMING"])
                skips[reason] += 1
        q1, q1_reason = _q1_close_trigger(history, snapshot, audit, spec)
        if q1 is not None:
            signals.append(q1)
            _record_state(states, day, STATE_LABELS["CONFIRMED_PENDING_ENTRY"], FAMILY_LABELS[q1["family"]])
        else:
            skips[q1_reason] += 1

        for signal in signals:
            next_row = raw.iloc[index + 1]
            next_day = next_row.date.date().isoformat()
            next_history = _history_for_epoch(raw, actions, next_day, cache)
            entry, reason = _next_open_entry(
                signal, next_row, float(next_history.iloc[-1].ATR14), mapped_actions.get(next_day, []), spec
            )
            if entry is None:
                skips[reason] += 1
                continue
            proposal_id = f"V2-{signal['family']}-{item['code']}-{next_day}-{day}"
            proposals.append({
                "proposal_id": proposal_id,
                "trade_id": proposal_id,
                "code": str(item["code"]),
                "name": str(item["name"]),
                "monitor_added_date": monitor_added,
                "episode_sequence": episode,
                "trigger_family": signal["family"],
                "entry_state": STATE_LABELS["CONFIRMED_PENDING_ENTRY"],
                **entry,
            })
            if signal["family"] != "Q1_MOMENTUM_BREAKOUT":
                zone = None
                zone_index = None

    return {
        "code": str(item["code"]),
        "name": str(item["name"]),
        "proposals": proposals,
        "states": states,
        "skips": dict(skips),
        "quality": quality,
    }


def _job(args: tuple[dict[str, Any], list[dict[str, Any]], EntryStructureSpec]) -> dict[str, Any]:
    return discover_stock(item=args[0], selections=args[1], spec=args[2])


def _load_selections() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    snapshot = read(SELECTION_SOURCE)
    rows = [{**row, "date": date.fromisoformat(row["date"])} for row in snapshot["rows"]]
    return _index_selections(rows), snapshot


def _proposal_input(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": proposal["proposal_id"],
        "code": proposal["code"],
        "name": proposal["name"],
        "entry_date": proposal["entry_date"],
        "entry_price": float(proposal["entry_price"]),
        "initial_defense": float(proposal["initial_defense"]),
        "defense": float(proposal["initial_defense"]),
        "mfe_pct": 0.0,
    }


def _run_variant(
    *, code: str,
    proposals: list[dict[str, Any]],
    inputs: dict[str, tuple[pd.DataFrame, list[dict[str, Any]]]],
    costs: Costs,
) -> dict[str, Any]:
    priority = {"EARLY_TAIJI_PULLBACK": 0, "LONG_MA_PULLBACK": 1, "Q1_MOMENTUM_BREAKOUT": 2}
    ordered = sorted(proposals, key=lambda row: (
        row["entry_date"], priority[row["trigger_family"]], row["code"], row["proposal_id"]
    ))
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    active_by_code: dict[str, dict[str, Any]] = {}
    same_day_seen: set[tuple[str, str]] = set()
    for proposal in ordered:
        key = (proposal["code"], proposal["entry_date"])
        if key in same_day_seen:
            skipped.append({**proposal, "skip_reason": "DUPLICATE_SAME_DAY（同股同日重複訊號）"})
            continue
        prior = active_by_code.get(proposal["code"])
        if prior and (not prior["exit_date"] or prior["exit_date"] >= proposal["entry_date"]):
            skipped.append({**proposal, "skip_reason": "DUPLICATE_OPEN（同股前一交易仍持有）"})
            continue
        raw, actions = inputs[proposal["code"]]
        result = simulate_trade(
            trade=_proposal_input(proposal), raw=raw, actions=actions,
            model=MODELS["ONE_SHOT"], costs=costs, as_of=AS_OF,
        )
        result["reference_fixed_defense_mfe_pct"] = float(result["max_campaign_gross_return_pct"])
        result["entry_variant"] = code
        result["entry_variant_label"] = VARIANT_LABELS[code]
        result["trigger_family"] = proposal["trigger_family"]
        result["entry_proposal"] = proposal
        accepted.append(result)
        active_by_code[proposal["code"]] = result
        same_day_seen.add(key)
    summary = summarize(accepted)
    closed = [row for row in accepted if row["status"] == "CLOSED"]
    open_rows = [row for row in accepted if row["status"] != "CLOSED"]
    closed_pnls = [float(row["net_pnl"]) for row in closed]
    positive = sum(max(0.0, value) for value in closed_pnls)
    negative = -sum(min(0.0, value) for value in closed_pnls)
    summary.update({
        "candidate_proposal_count": len(proposals),
        "executed_trade_count": len(accepted),
        "deduplicated_count": len(skipped),
        "dedup_reason_counts": dict(Counter(row["skip_reason"] for row in skipped)),
        "family_counts": dict(Counter(row["trigger_family"] for row in accepted)),
        "closed_net_pnl": sum(closed_pnls),
        "open_net_pnl": sum(float(row["net_pnl"]) for row in open_rows),
        "closed_profit_factor": None if negative == 0 else positive / negative,
        "average_mfe_pct": _mean([float(row["max_campaign_gross_return_pct"]) for row in accepted]),
        "mfe20_count": sum(float(row["max_campaign_gross_return_pct"]) >= 20.0 for row in accepted),
    })
    return {"summary": summary, "trades": accepted, "skipped": skipped}


def _control() -> dict[str, Any]:
    source = read(CONTROL_SOURCE)
    return source["results"]["HYBRID_RESONANCE"]


def build(workers: int = 4) -> dict[str, Any]:
    selections, selection_snapshot = _load_selections()
    manifest = read(RUN / "input_manifest.json")
    items = {str(item["code"]): item for item in manifest["items"]}
    jobs = [(items[code], rows, SPEC) for code, rows in selections.items()]
    discoveries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_job, job): str(job[0]["code"]) for job in jobs}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                discoveries.append(future.result())
            except Exception as exc:  # pragma: no cover
                errors.append({"code": futures[future], "error": str(exc)})
            if number % 25 == 0 or number == len(futures):
                found = sum(len(row["proposals"]) for row in discoveries)
                print(f"V2 scan {number}/{len(futures)} proposals={found} errors={len(errors)}", flush=True)
    discoveries.sort(key=lambda row: row["code"])
    proposals = [proposal for row in discoveries for proposal in row["proposals"]]
    inputs = {}
    for code in sorted({row["code"] for row in proposals}):
        raw, actions, _ = load_inputs(items[code])
        inputs[code] = (raw, actions)
    pullbacks = [row for row in proposals if row["trigger_family"] != "Q1_MOMENTUM_BREAKOUT"]
    long_ma = [row for row in proposals if row["trigger_family"] == "LONG_MA_PULLBACK"]
    q1 = [row for row in proposals if row["trigger_family"] == "Q1_MOMENTUM_BREAKOUT"]
    costs = Costs()
    results = {
        "CONTROL_V1": _control(),
        "PULLBACK_V2": _run_variant(code="PULLBACK_V2", proposals=pullbacks, inputs=inputs, costs=costs),
        "LONG_MA_V2": _run_variant(code="LONG_MA_V2", proposals=long_ma, inputs=inputs, costs=costs),
        "Q1_BREAKOUT_V2": _run_variant(code="Q1_BREAKOUT_V2", proposals=q1, inputs=inputs, costs=costs),
        "COMBINED_V2": _run_variant(code="COMBINED_V2", proposals=proposals, inputs=inputs, costs=costs),
    }
    scan_skips: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    for row in discoveries:
        scan_skips.update(row["skips"])
        state_counts.update(event["state"] for event in row["states"])
    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": AS_OF,
        "parameters": asdict(SPEC),
        "causal_semantics": {
            "monitoring": "Selection is known after its daily report; a new selection refreshes a 60-session monitor.",
            "zone": "MA21/55/105/144 touch is only a pullback-zone observation, never an entry.",
            "confirmation": "A later daily close must turn the small structure upward and create a confirmed defense.",
            "execution": "A confirmed signal on D may enter only at D+1 open, subject to chase/risk gates.",
            "taiji": "Any completed failed leg in the active dynasty forces re-anchoring; leg 5+ is downgraded and blocked.",
            "q1": "Momentum breakout requires confirmed Q1 and an unconsumed boundary; it remains a separate family.",
            "left_censor": "The 2026-05-21 first saved cohort waits five bars because its true monitor-add date is unknown.",
            "unchanged": "Corporate actions, 10,000 TWD one-shot sizing, costs and state-switch exit are unchanged.",
        },
        "selection": {
            "row_count": len(selection_snapshot["rows"]),
            "stock_count": len(selections),
            "first_date": min(row["date"] for row in selection_snapshot["rows"]),
            "last_date": max(row["date"] for row in selection_snapshot["rows"]),
        },
        "proposal_count": len(proposals),
        "proposal_family_counts": dict(Counter(row["trigger_family"] for row in proposals)),
        "proposal_support_counts": dict(Counter(row.get("support_line", "Q1_BOUNDARY") for row in proposals)),
        "state_transition_counts": dict(state_counts),
        "scan_skip_counts": dict(scan_skips),
        "scan_errors": errors,
        "results": results,
        "discoveries": discoveries,
    }


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    add("no_scan_errors", not payload["scan_errors"], payload["scan_errors"][:5])
    proposals = [p for d in payload["discoveries"] for p in d["proposals"]]
    add("next_session_only", all(p["entry_date"] > p["signal_date"] for p in proposals), len(proposals))
    add("risk_bounded", all(p["actual_risk_pct"] <= SPEC.max_risk_pct + 1e-9 and p["actual_risk_atr"] <= SPEC.max_risk_atr + 1e-9 for p in proposals), len(proposals))
    add("pullbacks_have_zone", all(p.get("zone_date") and p.get("support_line") for p in proposals if p["trigger_family"] != "Q1_MOMENTUM_BREAKOUT"), len(proposals))
    add("q1_boundaries_unlabeled_as_ma", all(p.get("boundary") for p in proposals if p["trigger_family"] == "Q1_MOMENTUM_BREAKOUT"), len(proposals))
    for code in ("PULLBACK_V2", "LONG_MA_V2", "Q1_BREAKOUT_V2", "COMBINED_V2"):
        result = payload["results"][code]
        ids = [row["trade_id"] for row in result["trades"]]
        add(f"{code}.unique_ids", len(ids) == len(set(ids)), len(ids))
        add(f"{code}.funded", result["summary"]["funding_shortfall"] == 0, result["summary"]["funding_shortfall"])
    failures = [row for row in checks if not row["passed"]]
    return {"check_count": len(checks), "passed_count": len(checks) - len(failures), "failed_count": len(failures), "checks": checks}


def render(payload: dict[str, Any]) -> str:
    lines = [
        f"# 啟蒙進場與結構狀態 V2｜截至 {payload['as_of']}", "",
        "> 本版重做進場判讀；舊版保留作控制組。長均線只定義拉回區，必須等小結構日K收盤重新轉強，下一交易日開盤才成交。", "",
        "## 結果", "",
        "| 版本 | 交易 | 已出場/持有中 | 總淨損益 | 50萬報酬 | 最大回撤 | 勝率 | 中位報酬 | PF | 平均MFE | MFE>=20% |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for code, label in VARIANT_LABELS.items():
        summary = payload["results"][code]["summary"]
        lines.append(
            f"| {label} | {summary['trade_count']} | {summary['closed_count']}/{summary['open_or_triggered_count']} | "
            f"{summary['net_pnl']:,.0f} | {summary['return_on_500k_pct']:+.2f}% | {summary['max_drawdown_pct']:.2f}% | "
            f"{_f(summary['positive_trade_rate_pct'])}% | {_f(summary['median_trade_return_on_campaign_budget_pct'])}% | "
            f"{_f(summary['profit_factor'])} | {_f(summary.get('average_mfe_pct'))}% | {summary.get('mfe20_count', '—')} |"
        )
    lines += [
        "", "## 新版訊號組成", "",
        f"- 事前訊號共 {payload['proposal_count']} 筆：" + "、".join(f"{FAMILY_LABELS[k]} {v}筆" for k, v in sorted(payload["proposal_family_counts"].items())) + "。",
        "- 支撐／邊界：" + "、".join(f"{k} {v}筆" for k, v in sorted(payload["proposal_support_counts"].items())) + "。",
        "- 同股仍持有時，後續訊號不重複開倉；這項去重在各版本內獨立執行。", "",
        "## 新版逐筆交易", "",
        "| 股票 | 訊號 | 拉回區/邊界 | 確認日 | 進場日/價 | 防線 | 狀態 | 截至日淨損益 | MFE |",
        "|---|---|---|---|---:|---:|---|---:|---:|",
    ]
    for trade in payload["results"]["COMBINED_V2"]["trades"]:
        proposal = trade["entry_proposal"]
        reference = proposal.get("support_line") or proposal.get("boundary_source")
        lines.append(
            f"| {trade['code']} {trade['name']} | {FAMILY_LABELS[trade['trigger_family']]} | {reference} | "
            f"{proposal['signal_date']} | {trade['entry_date']} / {trade['entry_price']:.2f} | "
            f"{trade['initial_defense']:.2f} | {trade['status_label']} | {trade['net_pnl']:,.0f} | "
            f"{trade['max_campaign_gross_return_pct']:+.2f}% |"
        )
    if not payload["results"]["COMBINED_V2"]["trades"]:
        lines.append("| — | — | — | — | — | — | — | — | — |")
    lines += [
        "", "## 主要阻擋原因", "",
    ]
    for reason, count in sorted(payload["scan_skip_counts"].items(), key=lambda item: (-item[1], item[0]))[:10]:
        lines.append(f"- {reason}：{count:,}個股票日。")
    lines += [
        "",
        "## 結構狀態", "",
    ]
    for key in ("MONITORING", "LEFT_CENSORED", "LARGE_CONTEXT_READY", "PULLBACK_ZONE", "SMALL_TRIGGER_FORMING", "CONFIRMED_PENDING_ENTRY", "REANCHOR_REQUIRED", "LATE_CYCLE", "INVALIDATED", "EXPIRED"):
        lines.append(f"- `{STATE_LABELS[key]}`")
    lines += [
        "", "## 主要修正", "",
        "- 大、小結構只提供背景，不再直接觸發進場。",
        "- 太極同一朝代只要出現已確認失敗段，就維持 `REANCHOR_REQUIRED（需重新定錨）`；不讓下一段把失敗洗掉。",
        "- 第5段以上為 `LATE_CYCLE（末段降級）`，不開新倉。",
        "- Q4 必須為 `CONFIRMED（已確認）` 才能建立拉回區；105MA／144MA碰觸本身不成交。",
        "- Q1 突破只接受尚未被收盤突破過的邊界，且要求量、收盤位置與小級防線。",
        "- 最早5/21名單屬左設限樣本，先等待5根日K，避免把早已走一段的股票誤當剛加入監控。", "",
        "## 限制", "",
        "- 這仍是規則化代理，不是老師逐張圖人工標註；尤其大／小級切分尚無課程中的唯一數學定義。",
        "- 樣本截至9/4，持有中部位按截至日估值；不能把短期正負直接視為長期穩定性。",
        "- Q1理應另有時間停損；本次為隔離進場差異，仍沿用相同出場模型，因此Q1績效只作診斷。",
        "- 沒有修改正式機器人、選股策略或自動下單。", "",
        f"方法版本：`{payload['method_version']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    payload = build(workers=args.workers)
    validation = validate(payload)
    payload["validation"] = validation
    args.output_dir.mkdir(parents=True, exist_ok=True)
    save(args.output_dir / "comparison.json", payload)
    (args.output_dir / "comparison.md").write_text(render(payload), encoding="utf-8")
    (args.output_dir / "validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(render(payload))
    if validation["failed_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
