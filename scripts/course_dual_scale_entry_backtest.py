"""Backtest daily-K large/small structure entry channels on the frozen radar universe.

The existing risk-checked touch cohort is retained as SMALL_BASELINE.  New
large-structure opportunities are discovered causally while each radar name is
still monitored.  A plan made after session D may only fill during D+1.

Large structure decides the boundary and directional thesis.  The nearest
confirmed small-grade pivot low remains the executable defense so a large
chart thesis cannot silently create an unbounded initial loss.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_add_vs_no_add_backtest import (  # noqa: E402
    Costs,
    MODELS,
    simulate_trade,
    summarize,
)
from scripts.course_corporate_action_backtest import (  # noqa: E402
    adjusted_history,
    event_dates,
    load_inputs,
    transform_price,
)
from scripts.course_corporate_action_data import AS_OF, RUN, read  # noqa: E402
from scripts.course_daily_screen_trial import THRESHOLDS, _candles  # noqa: E402
from scripts.course_radar_trigger_backtest import _index_selections  # noqa: E402
from scripts.course_watchlist_backtest import enlightenment_snapshot  # noqa: E402
from trade_monitor.pivot_replay import (  # noqa: E402
    detect_local_pivots,
    pair_pivots,
    secondary_pivots,
)


METHOD_VERSION = "course-dual-scale-entry-v1"
ENTRY_SOURCE = RUN / "entry_variants_v1" / "comparison.json"
SELECTION_SOURCE = RUN / "selection_snapshot.json"
LABELS = {
    "SMALL_BASELINE": "SMALL_BASELINE（現行小結構基準）",
    "LARGE_BREAKOUT": "LARGE_BREAKOUT（大結構突破）",
    "DUAL_RESONANCE": "DUAL_RESONANCE（大小級共振）",
    "HYBRID": "HYBRID（小結構＋大結構混合）",
}


@dataclass(frozen=True)
class LargeScaleSpec:
    platform_lookback: int = 55
    pivot_lookback: int = 120
    monitor_bars: int = 20
    ready_below_atr: float = 0.50
    confirmed_above_atr: float = 0.25
    chase_cap_atr: float = 0.50
    max_risk_pct: float = 10.0
    max_risk_atr: float = 2.5
    max_extension_atr: float = 2.0
    max_signal_range_atr: float = 2.0
    min_avg_volume_lots: float = 500.0


SPEC = LargeScaleSpec()


def _mean(values: list[float]) -> float | None:
    return None if not values else statistics.fmean(values)


def _median(values: list[float]) -> float | None:
    return None if not values else statistics.median(values)


def _f(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _select_boundary(
    *,
    platform_high: float,
    confirmed_large_highs: list[dict[str, Any]],
    close: float,
    atr: float,
) -> dict[str, Any] | None:
    """Pick the nearest known large boundary; no later pivot may be supplied."""
    candidates = [{"price": platform_high, "source": "PRIOR_55D_HIGH（前55日平台上緣）",
                   "bar_date": None, "confirmed_date": None}]
    candidates.extend(confirmed_large_highs)
    lower = close - SPEC.confirmed_above_atr * atr
    upper = close + SPEC.ready_below_atr * atr
    nearby = [row for row in candidates if lower <= float(row["price"]) <= upper]
    if not nearby:
        return None
    # Prefer a true second-grade pivot at the same distance; otherwise choose
    # the closest executable resistance rather than an arbitrary oldest high.
    nearby.sort(key=lambda row: (
        abs(float(row["price"]) - close),
        0 if str(row["source"]).startswith("P2") else 1,
        str(row.get("confirmed_date") or ""),
    ))
    return nearby[0]


def _large_boundaries(history: pd.DataFrame, spec: LargeScaleSpec = SPEC) -> dict[str, Any] | None:
    if len(history) < max(145, spec.platform_lookback + 1):
        return None
    latest = history.iloc[-1]
    close = float(latest.close)
    atr = float(latest.ATR14)
    if atr <= 0:
        return None
    prior = history.iloc[-1 - spec.platform_lookback : -1]
    platform_position = int(prior["high"].astype(float).idxmax())
    platform_high = float(history.loc[platform_position, "high"])
    platform_date = history.loc[platform_position, "date"].date().isoformat()

    local, _ = detect_local_pivots(_candles(history), THRESHOLDS.pivot_n)
    paired, _, _ = pair_pivots(local)
    large = secondary_pivots(paired)
    floor = len(history) - 1 - spec.pivot_lookback
    confirmed_highs = [
        {
            "price": float(pivot.price),
            "source": "P2_HIGH（已確認大級樞紐高）",
            "bar_date": pivot.bar_time[:10],
            "confirmed_date": pivot.confirmation_time[:10],
        }
        for pivot in large
        if pivot.kind == "HIGH"
        and pivot.bar_index >= floor
        and pivot.confirmation_index <= len(history) - 1
    ]
    boundary = _select_boundary(
        platform_high=platform_high,
        confirmed_large_highs=confirmed_highs,
        close=close,
        atr=atr,
    )
    if boundary and boundary["source"].startswith("PRIOR"):
        boundary["bar_date"] = platform_date
        boundary["confirmed_date"] = history.iloc[-1].date.date().isoformat()
    return {
        "boundary": boundary,
        "platform_high": platform_high,
        "platform_bar_date": platform_date,
        "confirmed_large_high_count": len(confirmed_highs),
    }


def _large_plan(
    history: pd.DataFrame,
    snapshot: dict[str, Any],
    spec: LargeScaleSpec = SPEC,
) -> tuple[dict[str, Any] | None, str]:
    """Return one causal large-boundary plan made with data through D."""
    latest = history.iloc[-1]
    close = float(latest.close)
    atr = float(latest.ATR14)
    if atr <= 0:
        return None, "INVALID_ATR（ATR無效）"
    structure = _large_boundaries(history, spec)
    if not structure or not structure["boundary"]:
        return None, "NO_NEAR_LARGE_BOUNDARY（尚未接近大結構邊界）"
    boundary = structure["boundary"]
    level = float(boundary["price"])

    direction = snapshot["direction"]
    large = str(direction["large"])
    small = str(direction["small"])
    ma21_rising = float(latest.MA21) > float(history.iloc[-6].MA21)
    line_ok = close > float(latest.MA21) > float(latest.MA55) and ma21_rising
    transition_breakout = large == "TRANSITION" and line_ok
    if not (large == "BULL" or transition_breakout):
        return None, "LARGE_DIRECTION_NOT_READY（大級方向尚未轉多）"
    if small == "BEAR":
        return None, "SMALL_BEAR_BLOCKS（小級空方阻擋）"
    if not line_ok:
        return None, "ELSON_LINE_NOT_ALIGNED（Elson線未同向）"
    if snapshot["sstv"]["quality"] == "不合格":
        return None, "SSTV_FAILED（箱型品質不合格）"
    if snapshot["taiji"]["state"] in {"COPY_FAILED", "CORRECTION_FAILED", "POST_5"}:
        return None, "TAIJI_BLOCKS（太極失敗或朝代衰減）"
    if not bool(snapshot["checks"]["structural_lens"]):
        return None, "STRUCTURAL_LENS_NOT_EXECUTABLE（主結構鏡頭不可執行）"

    avg_volume_lots = float(history["volume"].iloc[-20:].mean()) / 1000.0
    if avg_volume_lots < spec.min_avg_volume_lots:
        return None, "LIQUIDITY_FAILED（流動性不足）"
    extension_atr = max(0.0, (close - float(latest.MA21)) / atr)
    range_atr = float((latest.high - latest.low) / atr)
    if extension_atr > spec.max_extension_atr or range_atr > spec.max_signal_range_atr:
        return None, "NO_CHASE（過度延伸或訊號K過長）"

    defense_record = snapshot["structure"].get("defense")
    if not defense_record:
        return None, "NO_SMALL_DEFENSE（沒有已確認小級防線）"
    defense = float(defense_record["price"])
    stage = "PRE_BREAK（突破前預備）" if close <= level else "FOLLOW_THROUGH（突破後延續）"
    trigger = level if close <= level else max(level, float(latest.high))
    chase_cap = close + spec.chase_cap_atr * atr
    if trigger > chase_cap:
        return None, "TRIGGER_ABOVE_CHASE_CAP（觸發價高於追價上限）"
    risk = trigger - defense
    if risk <= 0:
        return None, "INVALID_RISK（觸發價不在防線上方）"
    risk_pct = risk / trigger * 100.0
    risk_atr = risk / atr
    if risk_pct > spec.max_risk_pct or risk_atr > spec.max_risk_atr:
        return None, "PLANNED_RISK_TOO_WIDE（計畫風險過大）"

    dual = bool(
        large == "BULL"
        and small == "BULL"
        and direction["relation"] == "多方共振"
    )
    return {
        "armed_date": latest.date.date().isoformat(),
        "trigger": trigger,
        "chase_cap": chase_cap,
        "defense": defense,
        "defense_bar_date": defense_record["bar_date"],
        "defense_confirmed_date": defense_record["confirmed_date"],
        "boundary": level,
        "boundary_source": boundary["source"],
        "boundary_bar_date": boundary["bar_date"],
        "boundary_confirmed_date": boundary["confirmed_date"],
        "stage": stage,
        "large_direction": large,
        "small_direction": small,
        "grade_relation": direction["relation"],
        "dual_resonance": dual,
        "planned_risk_pct": risk_pct,
        "planned_risk_atr": risk_atr,
        "atr": atr,
        "extension_atr": extension_atr,
        "signal_range_atr": range_atr,
        "avg_volume_20d_lots": avg_volume_lots,
        "structural_lens": snapshot["structural_lens"],
        "taiji_state": snapshot["taiji"]["state"],
        "taiji_sequence": snapshot["taiji"]["sequence"],
        "quadrant": snapshot["quadrant"]["working_quadrant"],
    }, "ARMED（已建立大結構進場計畫）"


def _adjust_plan_for_entry_day(
    plan: dict[str, Any],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    adjusted = dict(plan)
    for action in actions:
        for key in ("trigger", "chase_cap", "defense", "boundary"):
            adjusted[key] = transform_price(float(adjusted[key]), action)
    return adjusted


def _execute_next_session(
    *,
    plan: dict[str, Any],
    next_row: pd.Series,
    next_atr: float,
    actions: list[dict[str, Any]],
    spec: LargeScaleSpec = SPEC,
) -> tuple[dict[str, Any] | None, str]:
    plan = _adjust_plan_for_entry_day(plan, actions)
    open_price = float(next_row.open)
    high = float(next_row.high)
    trigger = float(plan["trigger"])
    cap = float(plan["chase_cap"])
    defense = float(plan["defense"])
    if open_price > cap:
        return None, "OPEN_ABOVE_CHASE_CAP（開盤超過追價上限）"
    if open_price <= defense:
        return None, "OPEN_BELOW_DEFENSE（開盤跌破小級防線）"
    if trigger > cap:
        return None, "TRIGGER_ABOVE_CHASE_CAP（觸發價高於追價上限）"
    if high < trigger:
        return None, "TRIGGER_NOT_TOUCHED（下一交易日未觸價）"
    entry = max(open_price, trigger)
    risk = entry - defense
    risk_pct = risk / entry * 100.0
    risk_atr = math.inf if next_atr <= 0 else risk / next_atr
    if risk <= 0 or risk_pct > spec.max_risk_pct or risk_atr > spec.max_risk_atr:
        return None, "ACTUAL_RISK_TOO_WIDE（實際成交風險過大）"
    return {
        **plan,
        "entry_date": next_row.date.date().isoformat(),
        "entry_price": entry,
        "initial_defense": defense,
        "actual_risk_pct": risk_pct,
        "actual_risk_atr": risk_atr,
    }, "TRIGGERED（已觸發進場）"


def _history_for_epoch(
    raw: pd.DataFrame,
    actions: list[dict[str, Any]],
    day: str,
    cache: dict[tuple[str, ...], pd.DataFrame],
) -> pd.DataFrame:
    epoch = tuple(event["date"] for event in actions if event["date"] <= day)
    if epoch not in cache:
        next_action = next((event["date"] for event in actions if event["date"] > day), "9999-12-31")
        eligible = raw.loc[raw.date.dt.strftime("%Y-%m-%d") < next_action, "date"]
        epoch_end = eligible.iloc[-1].date().isoformat()
        cache[epoch] = adjusted_history(raw, actions, epoch_end)
    target = pd.Timestamp(day)
    return cache[epoch].loc[lambda frame: frame.date <= target].copy()


def discover_large_proposals(
    *,
    item: dict[str, Any],
    selections: list[dict[str, Any]],
    spec: LargeScaleSpec = SPEC,
) -> dict[str, Any]:
    raw, actions, quality = load_inputs(item)
    mapped_actions = event_dates(actions, raw)
    selected = {row["date"].isoformat(): row for row in selections}
    day_to_index = {value.date().isoformat(): index for index, value in enumerate(raw.date)}
    usable_days = [day for day in selected if day in day_to_index]
    if not usable_days:
        return {"code": item["code"], "proposals": [], "plans": [], "skips": [], "quality": quality}
    start = min(day_to_index[day] for day in usable_days)
    end = len(raw) - 1
    last_selected: int | None = None
    monitor_added: str | None = None
    episode = 0
    cache: dict[tuple[str, ...], pd.DataFrame] = {}
    proposals: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    skips: Counter[str] = Counter()

    for index in range(start, end):
        row = raw.iloc[index]
        day = row.date.date().isoformat()
        if day in selected:
            if last_selected is None or index - last_selected > spec.monitor_bars:
                episode += 1
                monitor_added = day
            last_selected = index
        if last_selected is None or index - last_selected > spec.monitor_bars:
            continue
        history = _history_for_epoch(raw, actions, day, cache)
        try:
            snapshot = enlightenment_snapshot(history, THRESHOLDS)
        except ValueError:
            skips["INSUFFICIENT_HISTORY（歷史資料不足）"] += 1
            continue
        if snapshot["structural_invalid"]:
            skips["STRUCTURE_INVALID（結構失效）"] += 1
            continue
        plan, reason = _large_plan(history, snapshot, spec)
        if plan is None:
            skips[reason] += 1
            continue
        plans.append({"code": item["code"], "name": item["name"], **plan})
        next_row = raw.iloc[index + 1]
        next_day = next_row.date.date().isoformat()
        next_history = _history_for_epoch(raw, actions, next_day, cache)
        next_atr = float(next_history.iloc[-1].ATR14)
        proposal, execution = _execute_next_session(
            plan=plan,
            next_row=next_row,
            next_atr=next_atr,
            actions=mapped_actions.get(next_day, []),
            spec=spec,
        )
        if proposal is None:
            skips[execution] += 1
            continue
        proposal_id = f"LARGE-{item['code']}-{proposal['entry_date']}-{plan['armed_date']}"
        proposals.append({
            "proposal_id": proposal_id,
            "trade_id": proposal_id,
            "code": str(item["code"]),
            "name": str(item["name"]),
            "monitor_added_date": monitor_added,
            "episode_sequence": episode,
            "trigger_family": "DUAL_RESONANCE" if proposal["dual_resonance"] else "LARGE_BREAKOUT",
            **proposal,
        })
    return {
        "code": str(item["code"]),
        "proposals": proposals,
        "plans": plans,
        "skips": dict(skips),
        "quality": quality,
    }


def _large_job(job: tuple[dict[str, Any], list[dict[str, Any]], LargeScaleSpec]) -> dict[str, Any]:
    return discover_large_proposals(item=job[0], selections=job[1], spec=job[2])


def _load_selections() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    snapshot = read(SELECTION_SOURCE)
    rows = [{**row, "date": date.fromisoformat(row["date"])} for row in snapshot["rows"]]
    return _index_selections(rows), snapshot


def _baseline_proposals() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = read(ENTRY_SOURCE)
    baseline = source["results"]["TOUCH_RISK"]
    proposals = []
    for trade in baseline["trades"]:
        proposals.append({
            "proposal_id": f"SMALL-{trade['trade_id']}",
            "trade_id": trade["trade_id"],
            "code": str(trade["code"]),
            "name": str(trade["name"]),
            "armed_date": None,
            "entry_date": trade["entry_date"],
            "entry_price": float(trade["entry_price"]),
            "initial_defense": float(trade["initial_defense"]),
            "actual_risk_pct": float(trade["initial_risk_pct"]),
            "actual_risk_atr": float(trade["entry_proposal"]["quality"]["risk_atr"]),
            "trigger_family": "SMALL_BASELINE",
            "small_pattern": trade.get("entry_proposal", {}).get("trigger_type"),
            "source_net_pnl": float(trade["net_pnl"]),
            "source_reference_mfe_pct": float(trade["reference_fixed_defense_mfe_pct"]),
        })
    return proposals, baseline


def _proposal_input(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": proposal["proposal_id"],
        "code": proposal["code"],
        "name": proposal["name"],
        "entry_date": proposal["entry_date"],
        "entry_price": float(proposal["entry_price"]),
        "initial_defense": float(proposal["initial_defense"]),
        "defense": float(proposal["initial_defense"]),
        "mfe_pct": float(proposal.get("source_reference_mfe_pct", 0.0)),
    }


def _run_variant(
    *,
    code: str,
    proposals: list[dict[str, Any]],
    inputs: dict[str, tuple[pd.DataFrame, list[dict[str, Any]]]],
    costs: Costs,
) -> dict[str, Any]:
    # HYBRID adds a new channel to the old one.  When both propose the same
    # stock/date, preserve the baseline execution instead of retrospectively
    # picking the better-looking fill.
    priority = {"SMALL_BASELINE": 0, "DUAL_RESONANCE": 1, "LARGE_BREAKOUT": 2}
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
            trade=_proposal_input(proposal),
            raw=raw,
            actions=actions,
            model=MODELS["ONE_SHOT"],
            costs=costs,
            as_of=AS_OF,
        )
        result["reference_fixed_defense_mfe_pct"] = float(result["max_campaign_gross_return_pct"])
        result["entry_variant"] = code
        result["entry_variant_label"] = LABELS[code]
        result["trigger_family"] = proposal["trigger_family"]
        result["entry_proposal"] = proposal
        accepted.append(result)
        active_by_code[proposal["code"]] = result
        same_day_seen.add(key)

    summary = summarize(accepted)
    returns = [float(row["net_return_on_campaign_budget_pct"]) for row in accepted]
    winners = [value for value in returns if value > 0]
    losers = [value for value in returns if value < 0]
    closed = [row for row in accepted if row["status"] == "CLOSED"]
    open_rows = [row for row in accepted if row["status"] != "CLOSED"]
    closed_pnls = [float(row["net_pnl"]) for row in closed]
    closed_profits = sum(max(0.0, value) for value in closed_pnls)
    closed_losses = -sum(min(0.0, value) for value in closed_pnls)
    summary.update({
        "candidate_proposal_count": len(proposals),
        "executed_trade_count": len(accepted),
        "deduplicated_count": len(skipped),
        "dedup_reason_counts": dict(Counter(row["skip_reason"] for row in skipped)),
        "family_counts": dict(Counter(row["trigger_family"] for row in accepted)),
        "average_winner_pct": _mean(winners),
        "average_loser_pct": _mean(losers),
        "payoff_ratio": None if not winners or not losers else _mean(winners) / abs(_mean(losers)),
        "closed_net_pnl": sum(float(row["net_pnl"]) for row in closed),
        "open_net_pnl": sum(float(row["net_pnl"]) for row in open_rows),
        "closed_positive_count": sum(value > 0 for value in closed_pnls),
        "closed_negative_count": sum(value < 0 for value in closed_pnls),
        "closed_positive_rate_pct": None if not closed else sum(value > 0 for value in closed_pnls) / len(closed) * 100.0,
        "closed_profit_factor": None if closed_losses == 0 else closed_profits / closed_losses,
        "open_positive_count": sum(float(row["net_pnl"]) > 0 for row in open_rows),
        "open_negative_count": sum(float(row["net_pnl"]) < 0 for row in open_rows),
        "mfe20_count": sum(float(row["max_campaign_gross_return_pct"]) >= 20.0 for row in accepted),
        "mfe30_count": sum(float(row["max_campaign_gross_return_pct"]) >= 30.0 for row in accepted),
        "reached_2r_count": sum(any("TREND_RUNNER" in event["state"] for event in row["state_events"])
                                 for row in accepted),
    })
    return {"summary": summary, "trades": accepted, "skipped": skipped}


def build(workers: int = 4) -> dict[str, Any]:
    selections, selection_snapshot = _load_selections()
    manifest = read(RUN / "input_manifest.json")
    items = {str(item["code"]): item for item in manifest["items"]}
    if len(selections) != 695 or sum(len(rows) for rows in selections.values()) != 8467:
        raise AssertionError("frozen radar selection snapshot changed")

    discoveries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    jobs = [(items[code], rows, SPEC) for code, rows in selections.items()]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_large_job, job): str(job[0]["code"]) for job in jobs}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                discoveries.append(future.result())
            except Exception as exc:  # pragma: no cover - surfaced in validation/report
                errors.append({"code": futures[future], "error": str(exc)})
            if number % 25 == 0 or number == len(futures):
                found = sum(len(row["proposals"]) for row in discoveries)
                print(f"Large-scale scan {number}/{len(futures)} proposals={found} errors={len(errors)}", flush=True)
    discoveries.sort(key=lambda row: row["code"])
    large_proposals = [proposal for row in discoveries for proposal in row["proposals"]]
    plan_count = sum(len(row["plans"]) for row in discoveries)
    scan_skips: Counter[str] = Counter()
    for row in discoveries:
        scan_skips.update(row["skips"])

    baseline_proposals, baseline_source = _baseline_proposals()
    needed_codes = {row["code"] for row in baseline_proposals + large_proposals}
    loaded = {}
    for number, code in enumerate(sorted(needed_codes), 1):
        raw, actions, _ = load_inputs(items[code])
        loaded[code] = (raw, actions)
        if number % 50 == 0 or number == len(needed_codes):
            print(f"Load execution inputs {number}/{len(needed_codes)}", flush=True)

    costs = Costs()
    variants = {
        "SMALL_BASELINE": baseline_proposals,
        "LARGE_BREAKOUT": large_proposals,
        "DUAL_RESONANCE": [row for row in large_proposals if row["dual_resonance"]],
        "HYBRID": baseline_proposals + large_proposals,
    }
    results = {}
    for code, proposals in variants.items():
        print(f"Simulate {code} candidates={len(proposals)}", flush=True)
        results[code] = _run_variant(
            code=code, proposals=proposals, inputs=loaded, costs=costs
        )

    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": AS_OF,
        "selection_scope": {
            "radar_records": 8467,
            "unique_stocks": 695,
            "first_date": min(row["date"] for row in selection_snapshot["rows"]),
            "last_date": max(row["date"] for row in selection_snapshot["rows"]),
            "monitor_bars_after_last_selection": SPEC.monitor_bars,
        },
        "parameters": {
            "spec": asdict(SPEC),
            "large_boundary": "nearest causal confirmed P2 high or prior 55-session high",
            "execution": "ARMED after D close; D+1 intraday touch; fill=max(open, trigger)",
            "defense": "nearest confirmed small-grade pivot low",
            "actual_risk_gate": "fill-to-small-defense <=10% and <=2.5 ATR",
            "position": "ONE_SHOT（不加碼、每筆1萬元上限、整股數零股）",
            "exit": "same +2R state-switch exit used by entry_variants_v1",
            "costs": asdict(costs),
            "hybrid_collision": "same stock/date keeps SMALL_BASELINE; an open same-stock campaign blocks later entries",
        },
        "large_scan": {
            "armed_plan_count": plan_count,
            "triggered_actual_risk_pass_count": len(large_proposals),
            "dual_resonance_proposal_count": sum(row["dual_resonance"] for row in large_proposals),
            "skip_reason_counts": dict(scan_skips),
            "errors": errors,
        },
        "baseline_source_summary": baseline_source["summary"],
        "results": results,
    }


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    add("no_scan_errors", not payload["large_scan"]["errors"], payload["large_scan"]["errors"])
    source = payload["baseline_source_summary"]
    baseline = payload["results"]["SMALL_BASELINE"]
    add("baseline_trade_count_reproduced", baseline["summary"]["trade_count"] == source["trade_count"],
        baseline["summary"]["trade_count"])
    add("baseline_net_pnl_reproduced", abs(baseline["summary"]["net_pnl"] - source["net_pnl"]) < 1e-6,
        baseline["summary"]["net_pnl"] - source["net_pnl"])
    for code, result in payload["results"].items():
        trades = result["trades"]
        ids = [row["trade_id"] for row in trades]
        add(f"{code}.unique_trade_ids", len(ids) == len(set(ids)), len(ids))
        risks = [
            row["entry_proposal"] for row in trades
            if row["trigger_family"] != "SMALL_BASELINE"
        ]
        bad_risk = [row["proposal_id"] for row in risks
                    if row["actual_risk_pct"] > SPEC.max_risk_pct + 1e-9
                    or row["actual_risk_atr"] > SPEC.max_risk_atr + 1e-9]
        add(f"{code}.actual_risk", not bad_risk, bad_risk[:5])
        bad_time = [row["trade_id"] for row in trades
                    if row["entry_proposal"].get("armed_date")
                    and row["entry_date"] <= row["entry_proposal"]["armed_date"]]
        add(f"{code}.next_session_only", not bad_time, bad_time[:5])
        bad_causal_dates = [
            row["trade_id"] for row in trades
            if row["trigger_family"] != "SMALL_BASELINE"
            and (
                row["entry_proposal"].get("boundary_confirmed_date") > row["entry_proposal"]["armed_date"]
                or row["entry_proposal"].get("defense_confirmed_date") > row["entry_proposal"]["armed_date"]
            )
        ]
        add(f"{code}.causal_structure_dates", not bad_causal_dates, bad_causal_dates[:5])
        by_stock: dict[str, list[dict[str, Any]]] = {}
        for row in trades:
            by_stock.setdefault(row["code"], []).append(row)
        overlaps = []
        for stock, rows in by_stock.items():
            ordered = sorted(rows, key=lambda row: row["entry_date"])
            for left, right in zip(ordered, ordered[1:]):
                if not left["exit_date"] or left["exit_date"] >= right["entry_date"]:
                    overlaps.append((stock, left["trade_id"], right["trade_id"]))
        add(f"{code}.no_same_stock_overlap", not overlaps, overlaps[:5])
        reconciled = 500_000.0 + sum(float(row["net_pnl"]) for row in trades)
        add(f"{code}.equity_reconciles",
            abs(result["summary"]["final_net_liquidation_equity"] - reconciled) < 1e-6,
            result["summary"]["final_net_liquidation_equity"] - reconciled)
    failures = [row for row in checks if not row["passed"]]
    return {
        "check_count": len(checks),
        "passed_count": len(checks) - len(failures),
        "failed_count": len(failures),
        "checks": checks,
    }


def render(payload: dict[str, Any]) -> str:
    lines = [
        f"# 大小結構日K進場回測｜截至 {payload['as_of']}", "",
        "> 同一份凍結雷達名單、除權息修正、每筆1萬元、不加碼、相同交易成本與+2R狀態切換出場。大結構訊號只使用當時已知資料。", "",
        "## 四版本結果", "",
        "| 版本 | 實際交易 | 已出場/持有中 | 帳戶淨損益 | 50萬報酬 | 最大回撤 | 正報酬率 | 平均贏/輸 | 賺賠比 | PF | MFE≥20% | 達+2R | 最大同時持倉 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for code in LABELS:
        summary = payload["results"][code]["summary"]
        lines.append(
            f"| {LABELS[code]} | {summary['trade_count']} | {summary['closed_count']}/{summary['open_or_triggered_count']} | "
            f"{summary['net_pnl']:,.0f} | {summary['return_on_500k_pct']:+.2f}% | {summary['max_drawdown_pct']:.2f}% | "
            f"{_f(summary['positive_trade_rate_pct'])}% | {_f(summary['average_winner_pct'])}% / {_f(summary['average_loser_pct'])}% | "
            f"{_f(summary['payoff_ratio'])} | {_f(summary['profit_factor'])} | {summary['mfe20_count']} | "
            f"{summary['reached_2r_count']} | {summary['max_concurrent_positions']} |"
        )
    lines += [
        "", "## 訊號漏斗", "",
        f"- 大結構 `ARMED（已建立進場計畫）`：{payload['large_scan']['armed_plan_count']} 次。",
        f"- 下一交易日觸價且實際風險合格：{payload['large_scan']['triggered_actual_risk_pass_count']} 次。",
        f"- 其中屬於 `DUAL_RESONANCE（大小級共振）`：{payload['large_scan']['dual_resonance_proposal_count']} 次。",
        "- 最終交易筆數會再扣除同一股票仍有持倉或同日重複訊號。", "",
        "## 實現／未實現損益", "",
        "| 版本 | 已出場損益 | 已出場勝/敗、PF | 持有中估值損益 | 持有中正/負 | 訊號來源 | 資金缺口 |",
        "|---|---:|---:|---:|---:|---|---:|",
    ]
    for code in LABELS:
        summary = payload["results"][code]["summary"]
        family = "、".join(f"{key} {value}" for key, value in sorted(summary["family_counts"].items()))
        lines.append(
            f"| {LABELS[code]} | {summary['closed_net_pnl']:,.0f} | "
            f"{summary['closed_positive_count']}/{summary['closed_negative_count']}、{_f(summary['closed_profit_factor'])} | "
            f"{summary['open_net_pnl']:,.0f} | {summary['open_positive_count']}/{summary['open_negative_count']} | "
            f"{family or '—'} | {summary['funding_shortfall']:,.0f} |"
        )
    lines += [
        "", "## 規則口徑", "",
        "- `SMALL_BASELINE（現行小結構基準）`：完整重現前次 `TOUCH_RISK（觸價＋實際風險複核）` 交易。",
        "- `LARGE_BREAKOUT（大結構突破）`：監控有效期間內，價格接近當時已確認的大級二級樞紐高或前55日平台上緣；大級已多方，或轉折且Elson均線同向；小級不得空方。",
        "- `DUAL_RESONANCE（大小級共振）`：大結構突破條件之外，大小級道氏方向必須同為多方。",
        "- `HYBRID（混合）`：加入所有大結構觸發；若同股同日也有原小結構訊號，保留原小結構成交，不以事後績效挑選。",
        "- 大結構決定突破邊界；初始防線使用最近已確認小級樞紐。計畫與實際成交都須在10%及2.5ATR內。",
        "- D日收盤建立計畫，僅D+1有效；D+1開盤不得超過追價上限，盤中最高價觸及才以 `max(開盤, 觸發價)` 模擬成交。", "",
        "## 限制", "",
        "- 55日平台、0.5ATR接近區及二級樞紐是本次預先固定的日K代理，不是課程宣稱的唯一參數；應看敏感度與樣本外結果。",
        "- 樣本只有2026/05/21～2026/09/03的已保存雷達，且截至日仍有持倉；總損益必須拆開已實現與未實現解讀。",
        "- 日K無法知道盤中高低先後，也未模擬零股委託簿、漲跌停排隊、最低手續費與額外滑價。",
        "- 本次是研究回測，沒有修改正式機器人規則或自動下單。", "",
        f"方法版本：`{payload['method_version']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-dir", type=Path, default=RUN / "dual_scale_entry_v1")
    args = parser.parse_args()
    payload = build(workers=args.workers)
    validation = validate(payload)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / "comparison.md").write_text(render(payload), encoding="utf-8")
    (args.output_dir / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if validation["failed_count"]:
        raise AssertionError(f"validation failed: {validation['failed_count']}")
    print((args.output_dir / "comparison.md").resolve())
    for code in LABELS:
        summary = dict(payload["results"][code]["summary"])
        summary.pop("equity_curve", None)
        print(code, json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
