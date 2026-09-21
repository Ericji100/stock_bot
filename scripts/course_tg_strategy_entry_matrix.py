"""Backtest entry families against every strategy recovered from Telegram.

All entry families are discovered in one causal daily replay.  A proposal is
tagged with every selection strategy that still had that stock under active
monitoring on the signal date.  Strategy/rule recommendations are chosen only
on the May-June calibration window and then reported on a separate matured
July-August validation window.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_add_vs_no_add_backtest import Costs, MODELS, simulate_trade, summarize  # noqa: E402
from scripts.course_corporate_action_backtest import event_dates, load_inputs  # noqa: E402
from scripts.course_corporate_action_data import AS_OF, read, save  # noqa: E402
from scripts.course_daily_screen_trial import THRESHOLDS  # noqa: E402
from scripts.course_dual_scale_entry_backtest import (  # noqa: E402
    SPEC as LARGE_SPEC,
    _execute_next_session as _large_execute,
    _history_for_epoch,
    _large_plan,
)
from scripts.course_entry_structure_v2_backtest import (  # noqa: E402
    FAMILY_LABELS as V2_FAMILY_LABELS,
    SPEC as V2_SPEC,
    STATE_LABELS,
    _fully_bearish,
    _large_context,
    _next_open_entry,
    _pullback_zone,
    _q1_close_trigger,
    _small_close_trigger,
    _taiji_audit,
)
from scripts.course_tg_selection_catalog import STRATEGY_LABELS  # noqa: E402
from scripts.course_watchlist_backtest import enlightenment_snapshot  # noqa: E402


METHOD_VERSION = "course-tg-strategy-entry-matrix-v1"
CATALOG_PATH = ROOT / "reports/course_backtest/2026-09-05/tg_selection_catalog_v1/catalog.json"
MANIFEST_PATH = ROOT / "reports/course_backtest/2026-09-05/tg_strategy_entry_matrix_v1/input_manifest.json"
OUTPUT_DIR = MANIFEST_PATH.parent
DISCOVERY_CACHE = OUTPUT_DIR / "discovery_cache.json"
CALIBRATION_END = "2026-06-30"
VALIDATION_START = "2026-07-01"
VALIDATION_SIGNAL_END = "2026-08-07"

FAMILY_LABELS = {
    "OLD_MA_RECLAIM": "OLD_MA_RECLAIM（舊版均線收復）",
    "OLD_PULLBACK_RELAUNCH": "OLD_PULLBACK_RELAUNCH（舊版拉回再發動）",
    "OLD_LARGE_BREAKOUT": "OLD_LARGE_BREAKOUT（舊版大結構突破）",
    "OLD_DUAL_RESONANCE": "OLD_DUAL_RESONANCE（舊版大小級共振）",
    "V2_EARLY_TAIJI_PULLBACK": "V2_EARLY_TAIJI_PULLBACK（新版早期太極拉回）",
    "V2_LONG_MA_PULLBACK": "V2_LONG_MA_PULLBACK（新版長均線拉回）",
    "V2_Q1_BREAKOUT": "V2_Q1_BREAKOUT（新版Q1未消耗邊界突破）",
}

VARIANT_LABELS = {
    **FAMILY_LABELS,
    "OLD_PULLBACK_PLUS_DUAL": "OLD_PULLBACK_PLUS_DUAL（舊版高品質拉回＋大小級共振）",
    "OLD_ALL": "OLD_ALL（舊版四類合併）",
    "V2_ALL": "V2_ALL（新版三類合併）",
    "CANDIDATE_ROUTER_V1": "CANDIDATE_ROUTER_V1（候選路由：大戶用共振，其餘用拉回）",
}

TRADE_REPORT_VARIANTS = [
    "OLD_MA_RECLAIM",
    "OLD_PULLBACK_RELAUNCH",
    "OLD_LARGE_BREAKOUT",
    "OLD_DUAL_RESONANCE",
    "OLD_PULLBACK_PLUS_DUAL",
    "OLD_ALL",
    "V2_ALL",
    "CANDIDATE_ROUTER_V1",
]

OLD_FAMILIES = {
    "OLD_MA_RECLAIM", "OLD_PULLBACK_RELAUNCH", "OLD_LARGE_BREAKOUT", "OLD_DUAL_RESONANCE",
}
V2_FAMILIES = {
    "V2_EARLY_TAIJI_PULLBACK", "V2_LONG_MA_PULLBACK", "V2_Q1_BREAKOUT",
}
ATOMIC_FAMILIES = list(FAMILY_LABELS)


def _mean(values: list[float]) -> float | None:
    return None if not values else statistics.fmean(values)


def _median(values: list[float]) -> float | None:
    return None if not values else statistics.median(values)


def _pf(values: list[float]) -> float | None:
    positive = sum(max(0.0, value) for value in values)
    negative = -sum(min(0.0, value) for value in values)
    if negative == 0:
        return None
    return positive / negative


def _f(value: float | None) -> str:
    if value is None:
        return "—"
    if math.isinf(value):
        return "∞"
    return f"{value:,.2f}"


def _selection_rows(catalog: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in catalog["events"]:
        if row["long_eligible"]:
            grouped[str(row["code"])].append(row)
    return grouped


def _map_selections_to_sessions(
    raw: pd.DataFrame, rows: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    days = list(raw.date.dt.strftime("%Y-%m-%d"))
    mapped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        index = bisect.bisect_left(days, str(row["date"]))
        if index < len(days):
            mapped[index].append(row)
    return mapped


def _active_context(
    last_selected: dict[str, tuple[int, str]], index: int, monitor_bars: int,
) -> tuple[list[str], dict[str, str]]:
    active = {
        strategy: selected_day
        for strategy, (selected_index, selected_day) in last_selected.items()
        if index - selected_index <= monitor_bars
    }
    return sorted(active), dict(sorted(active.items()))


def _adjust_plan(plan: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    from scripts.course_corporate_action_backtest import transform_price
    adjusted = dict(plan)
    for action in actions:
        for key in ("trigger", "chase_cap", "defense"):
            adjusted[key] = transform_price(float(adjusted[key]), action)
    return adjusted


def _old_small_entry(
    *, plan: dict[str, Any], next_row: pd.Series, next_atr: float,
    actions: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    plan = _adjust_plan(plan, actions)
    open_price = float(next_row.open)
    high = float(next_row.high)
    trigger = float(plan["trigger"])
    cap = float(plan["chase_cap"])
    defense = float(plan["defense"])
    if open_price > cap:
        return None, "OPEN_ABOVE_CHASE_CAP（開盤超過追價上限）"
    if open_price <= defense:
        return None, "OPEN_BELOW_DEFENSE（開盤跌破防線）"
    if trigger > cap or high < trigger:
        return None, "TRIGGER_NOT_TOUCHED（下一交易日未觸價）"
    entry = max(open_price, trigger)
    risk = entry - defense
    risk_pct = risk / entry * 100.0
    risk_atr = math.inf if next_atr <= 0 else risk / next_atr
    if risk <= 0 or risk_pct > 10.0 or risk_atr > 2.5:
        return None, "ACTUAL_RISK_TOO_WIDE（實際成交風險過大）"
    return {
        **plan,
        "entry_date": next_row.date.date().isoformat(),
        "entry_price": entry,
        "initial_defense": defense,
        "actual_risk_pct": risk_pct,
        "actual_risk_atr": risk_atr,
    }, "TRIGGERED（已觸發進場）"


def _proposal(
    *, item: dict[str, Any], family: str, entry: dict[str, Any],
    active: list[str], active_dates: dict[str, str], monitor_added: str,
) -> dict[str, Any]:
    signal_day = str(entry.get("signal_date") or entry.get("armed_date"))
    proposal_id = f"TG-{family}-{item['code']}-{entry['entry_date']}-{signal_day}"
    return {
        "proposal_id": proposal_id,
        "trade_id": proposal_id,
        "code": str(item["code"]),
        "name": str(item["name"]),
        "trigger_family": family,
        "monitor_added_date": monitor_added,
        "active_strategies": active,
        "active_strategy_last_selected_dates": active_dates,
        **entry,
    }


def discover_all(
    *, item: dict[str, Any], selections: list[dict[str, Any]],
) -> dict[str, Any]:
    raw, actions, quality = load_inputs(item)
    mapped_actions = event_dates(actions, raw)
    mapped_selections = _map_selections_to_sessions(raw, selections)
    if not mapped_selections:
        return {"code": item["code"], "proposals": [], "skips": {}, "quality": quality}
    start = min(mapped_selections)
    last_selected: dict[str, tuple[int, str]] = {}
    monitor_added = str(selections[0]["date"])
    cache: dict[tuple[str, ...], pd.DataFrame] = {}
    zone: dict[str, Any] | None = None
    zone_index: int | None = None
    proposals: list[dict[str, Any]] = []
    skips: Counter[str] = Counter()

    for index in range(start, len(raw) - 1):
        for selection in mapped_selections.get(index, []):
            last_selected[str(selection["strategy"])] = (index, str(selection["date"]))
        active, active_dates = _active_context(last_selected, index, V2_SPEC.monitor_bars)
        if not active:
            zone = None
            zone_index = None
            continue
        day = raw.iloc[index].date.date().isoformat()
        history = _history_for_epoch(raw, actions, day, cache)
        if len(history) < 165:
            skips["INSUFFICIENT_HISTORY（歷史資料不足）"] += 1
            continue
        try:
            snapshot = enlightenment_snapshot(history, THRESHOLDS)
        except ValueError:
            skips["INSUFFICIENT_HISTORY（歷史資料不足）"] += 1
            continue
        next_row = raw.iloc[index + 1]
        next_day = next_row.date.date().isoformat()
        next_history = _history_for_epoch(raw, actions, next_day, cache)
        next_atr = float(next_history.iloc[-1].ATR14)
        actions_next = mapped_actions.get(next_day, [])

        # Old small-chart setup: the setup close creates an ARMED plan and the
        # following session may fill on an intraday touch, exactly as v1 did.
        pattern = str(snapshot["setup"].get("pattern_code"))
        if snapshot["armed"] and pattern in {"RECLAIM", "PULLBACK_RELAUNCH"}:
            family = "OLD_MA_RECLAIM" if pattern == "RECLAIM" else "OLD_PULLBACK_RELAUNCH"
            plan = {
                "armed_date": day,
                "signal_date": day,
                "trigger": float(snapshot["setup"]["trigger_price"]),
                "chase_cap": float(snapshot["risk"]["chase_cap"]),
                "defense": float(snapshot["risk"]["defense"]),
                "setup_pattern": pattern,
                "structural_lens": snapshot["structural_lens"],
                "taiji_state": snapshot["taiji"]["state"],
                "taiji_sequence": snapshot["taiji"]["sequence"],
            }
            entry, reason = _old_small_entry(
                plan=plan, next_row=next_row, next_atr=next_atr, actions=actions_next,
            )
            if entry is not None:
                proposals.append(_proposal(
                    item=item, family=family, entry=entry, active=active,
                    active_dates=active_dates, monitor_added=monitor_added,
                ))
            else:
                skips[f"{family}:{reason}"] += 1

        # Old large-boundary channel.  DUAL is separated from non-resonant
        # large breakout so their conditional value can be measured.
        if not snapshot["structural_invalid"]:
            large_plan, reason = _large_plan(history, snapshot, LARGE_SPEC)
            if large_plan is not None:
                entry, execution = _large_execute(
                    plan=large_plan, next_row=next_row, next_atr=next_atr,
                    actions=actions_next, spec=LARGE_SPEC,
                )
                if entry is not None:
                    family = "OLD_DUAL_RESONANCE" if entry["dual_resonance"] else "OLD_LARGE_BREAKOUT"
                    entry["signal_date"] = entry["armed_date"]
                    proposals.append(_proposal(
                        item=item, family=family, entry=entry, active=active,
                        active_dates=active_dates, monitor_added=monitor_added,
                    ))
                else:
                    skips[f"OLD_LARGE:{execution}"] += 1
            else:
                skips[f"OLD_LARGE:{reason}"] += 1

        # New state model starts with a five-session warmup for the cohort that
        # first appears on 5/1, because earlier Telegram selections are absent.
        first_event_day = min(str(row["date"]) for row in selections)
        if first_event_day == "2026-05-01" and index - start < V2_SPEC.left_censored_warmup_bars:
            skips[STATE_LABELS["LEFT_CENSORED"]] += 1
            continue
        audit = _taiji_audit(snapshot)
        if _fully_bearish(history, snapshot):
            zone = None
            zone_index = None
            skips[STATE_LABELS["INVALIDATED"]] += 1
            continue
        if audit["reanchor_required"]:
            zone = None
            zone_index = None
            skips[STATE_LABELS["REANCHOR_REQUIRED"]] += 1
            continue
        if audit["late_cycle"]:
            zone = None
            zone_index = None
            skips[STATE_LABELS["LATE_CYCLE"]] += 1
            continue
        context_ok, context = _large_context(history, snapshot)
        if not context_ok:
            skips["V2_LARGE_CONTEXT_NOT_READY（新版大結構背景未成立）"] += 1
            continue
        if float(history.volume.iloc[-20:].mean()) / 1000.0 < V2_SPEC.min_avg_volume_lots:
            skips["V2_LIQUIDITY_FAILED（新版流動性不足）"] += 1
            continue
        new_zone, zone_reason = _pullback_zone(history, snapshot, audit, V2_SPEC)
        if new_zone is not None:
            zone = {**new_zone, "large_context": context}
            zone_index = index
        elif zone is None:
            skips[f"V2_ZONE:{zone_reason}"] += 1
        if zone is not None and zone_index is not None and index - zone_index > V2_SPEC.zone_max_age_bars:
            zone = None
            zone_index = None

        if zone is not None and zone_index is not None:
            signal, reason = _small_close_trigger(history, snapshot, zone, index - zone_index, V2_SPEC)
            if signal is not None:
                entry, execution = _next_open_entry(signal, next_row, next_atr, actions_next, V2_SPEC)
                if entry is not None:
                    family = (
                        "V2_EARLY_TAIJI_PULLBACK"
                        if signal["family"] == "EARLY_TAIJI_PULLBACK"
                        else "V2_LONG_MA_PULLBACK"
                    )
                    proposals.append(_proposal(
                        item=item, family=family, entry=entry, active=active,
                        active_dates=active_dates, monitor_added=monitor_added,
                    ))
                    zone = None
                    zone_index = None
                else:
                    skips[f"V2_PULLBACK:{execution}"] += 1
            else:
                skips[f"V2_PULLBACK:{reason}"] += 1
        q1, q1_reason = _q1_close_trigger(history, snapshot, audit, V2_SPEC)
        if q1 is not None:
            entry, execution = _next_open_entry(q1, next_row, next_atr, actions_next, V2_SPEC)
            if entry is not None:
                proposals.append(_proposal(
                    item=item, family="V2_Q1_BREAKOUT", entry=entry, active=active,
                    active_dates=active_dates, monitor_added=monitor_added,
                ))
            else:
                skips[f"V2_Q1:{execution}"] += 1
        else:
            skips[f"V2_Q1:{q1_reason}"] += 1
    return {
        "code": str(item["code"]), "name": str(item["name"]),
        "proposals": proposals, "skips": dict(skips), "quality": quality,
    }


def _job(args: tuple[dict[str, Any], list[dict[str, Any]]]) -> dict[str, Any]:
    return discover_all(item=args[0], selections=args[1])


def _proposal_input(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": proposal["proposal_id"], "code": proposal["code"], "name": proposal["name"],
        "entry_date": proposal["entry_date"], "entry_price": float(proposal["entry_price"]),
        "initial_defense": float(proposal["initial_defense"]),
        "defense": float(proposal["initial_defense"]), "mfe_pct": 0.0,
    }


def _run_variant(
    *, code: str, proposals: list[dict[str, Any]],
    inputs: dict[str, tuple[pd.DataFrame, list[dict[str, Any]]]], costs: Costs,
) -> dict[str, Any]:
    priority = {family: index for index, family in enumerate(ATOMIC_FAMILIES)}
    ordered = sorted(proposals, key=lambda row: (
        row["entry_date"], priority[row["trigger_family"]], row["code"], row["proposal_id"],
    ))
    accepted = []
    skipped = []
    active_by_code: dict[str, dict[str, Any]] = {}
    same_day: set[tuple[str, str]] = set()
    for proposal in ordered:
        key = (proposal["code"], proposal["entry_date"])
        if key in same_day:
            skipped.append({**proposal, "skip_reason": "DUPLICATE_SAME_DAY（同股同日重複訊號）"})
            continue
        prior = active_by_code.get(proposal["code"])
        if prior and (not prior["exit_date"] or prior["exit_date"] >= proposal["entry_date"]):
            skipped.append({**proposal, "skip_reason": "DUPLICATE_OPEN（同股前一交易仍持有）"})
            continue
        raw, actions = inputs[proposal["code"]]
        trade = simulate_trade(
            trade=_proposal_input(proposal), raw=raw, actions=actions,
            model=MODELS["ONE_SHOT"], costs=costs, as_of=AS_OF,
        )
        trade["reference_fixed_defense_mfe_pct"] = float(trade["max_campaign_gross_return_pct"])
        trade["entry_variant"] = code
        trade["entry_variant_label"] = VARIANT_LABELS[code]
        trade["trigger_family"] = proposal["trigger_family"]
        trade["entry_proposal"] = proposal
        trade["fixed_20_session_net_return_pct"] = fixed_horizon_return(trade, 20)
        accepted.append(trade)
        active_by_code[proposal["code"]] = trade
        same_day.add(key)
    summary = summarize(accepted)
    summary.update({
        "candidate_proposal_count": len(proposals),
        "executed_trade_count": len(accepted),
        "deduplicated_count": len(skipped),
        "dedup_reason_counts": dict(Counter(row["skip_reason"] for row in skipped)),
        "family_counts": dict(Counter(row["trigger_family"] for row in accepted)),
        "average_mfe_pct": _mean([float(row["max_campaign_gross_return_pct"]) for row in accepted]),
        "mfe20_count": sum(float(row["max_campaign_gross_return_pct"]) >= 20.0 for row in accepted),
    })
    return {"summary": summary, "trades": accepted, "skipped": skipped}


def fixed_horizon_return(trade: dict[str, Any], sessions: int = 20) -> float | None:
    """Net 10k-budget return at a fixed horizon; an earlier exit stays final."""
    daily = list(trade.get("daily") or [])
    if trade.get("status") == "CLOSED" and len(daily) < sessions:
        return float(trade["net_pnl"]) / 10_000.0 * 100.0
    if len(daily) < sessions:
        return None
    return float(daily[sessions - 1]["net_liquidation_pnl"]) / 10_000.0 * 100.0


def _window_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["fixed_20_session_net_return_pct"]) for row in rows if row["fixed_20_session_net_return_pct"] is not None]
    return {
        "trade_count": len(values),
        "mean_return_pct": _mean(values),
        "median_return_pct": _median(values),
        "positive_rate_pct": None if not values else sum(value > 0 for value in values) / len(values) * 100.0,
        "profit_factor": _pf(values),
        "no_losing_trade": bool(values) and not any(value < 0 for value in values),
        "net_return_points": sum(values),
    }


def strategy_matrix(atomic_results: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    family_baselines: dict[str, dict[str, dict[str, Any]]] = {}
    for family in ATOMIC_FAMILIES:
        trades = atomic_results[family]["trades"]
        family_baselines[family] = {
            "calibration": _window_stats([
                row for row in trades if row["entry_proposal"]["signal_date"] <= CALIBRATION_END
            ]),
            "validation": _window_stats([
                row for row in trades
                if VALIDATION_START <= row["entry_proposal"]["signal_date"] <= VALIDATION_SIGNAL_END
            ]),
        }
    matrix = []
    for strategy in STRATEGY_LABELS:
        for family in ATOMIC_FAMILIES:
            rows = [
                trade for trade in atomic_results[family]["trades"]
                if strategy in trade["entry_proposal"]["active_strategies"]
            ]
            calibration = [row for row in rows if row["entry_proposal"]["signal_date"] <= CALIBRATION_END]
            validation = [
                row for row in rows
                if VALIDATION_START <= row["entry_proposal"]["signal_date"] <= VALIDATION_SIGNAL_END
            ]
            calibration_stats = _window_stats(calibration)
            validation_stats = _window_stats(validation)
            calibration_baseline = family_baselines[family]["calibration"]
            validation_baseline = family_baselines[family]["validation"]
            calibration_lift = (
                None if calibration_stats["mean_return_pct"] is None or calibration_baseline["mean_return_pct"] is None
                else calibration_stats["mean_return_pct"] - calibration_baseline["mean_return_pct"]
            )
            validation_lift = (
                None if validation_stats["mean_return_pct"] is None or validation_baseline["mean_return_pct"] is None
                else validation_stats["mean_return_pct"] - validation_baseline["mean_return_pct"]
            )
            matrix.append({
                "strategy": strategy,
                "strategy_label": STRATEGY_LABELS[strategy],
                "entry_family": family,
                "entry_family_label": FAMILY_LABELS[family],
                "all_as_of": _window_stats(rows),
                "calibration_20_session": calibration_stats,
                "validation_20_session": validation_stats,
                "family_calibration_baseline": calibration_baseline,
                "family_validation_baseline": validation_baseline,
                "calibration_lift_pct": calibration_lift,
                "validation_lift_pct": validation_lift,
                "trade_ids": [row["trade_id"] for row in rows],
            })

    recommendations = []
    for strategy in STRATEGY_LABELS:
        candidates = []
        for row in matrix:
            if row["strategy"] != strategy:
                continue
            stats = row["calibration_20_session"]
            if (
                stats["trade_count"] >= 4
                and stats["mean_return_pct"] is not None and stats["mean_return_pct"] > 0
                and (stats["no_losing_trade"] or stats["profit_factor"] is not None and stats["profit_factor"] > 1)
                and row["calibration_lift_pct"] is not None and row["calibration_lift_pct"] > 0
            ):
                score = float(row["calibration_lift_pct"]) * math.sqrt(stats["trade_count"])
                candidates.append((score, row))
        if not candidates:
            recommendations.append({
                "strategy": strategy, "strategy_label": STRATEGY_LABELS[strategy],
                "selected_family": None, "selected_family_label": None,
                "status": "NO_POSITIVE_CALIBRATION（校準期沒有合格正向規則）",
                "calibration": None, "validation": None,
            })
            continue
        chosen = max(candidates, key=lambda item: (item[0], item[1]["entry_family"]))[1]
        validation = chosen["validation_20_session"]
        if validation["trade_count"] < 3:
            status = "INSUFFICIENT_VALIDATION（驗證樣本不足）"
        elif (
            validation["mean_return_pct"] is not None and validation["mean_return_pct"] > 0
            and (validation["no_losing_trade"] or validation["profit_factor"] is not None and validation["profit_factor"] > 1)
            and chosen["validation_lift_pct"] is not None and chosen["validation_lift_pct"] > 0
        ):
            status = "SUPPORTED（獨立驗證支持）"
        else:
            status = "NOT_CONFIRMED（獨立驗證未支持）"
        recommendations.append({
            "strategy": strategy, "strategy_label": STRATEGY_LABELS[strategy],
            "selected_family": chosen["entry_family"],
            "selected_family_label": chosen["entry_family_label"],
            "status": status,
            "calibration": chosen["calibration_20_session"],
            "validation": validation,
            "calibration_lift_pct": chosen["calibration_lift_pct"],
            "validation_lift_pct": chosen["validation_lift_pct"],
        })
    return matrix, recommendations


def build(workers: int = 4) -> dict[str, Any]:
    catalog = read(CATALOG_PATH)
    selections = _selection_rows(catalog)
    manifest = read(MANIFEST_PATH)
    if manifest["fetch_errors"]:
        raise RuntimeError("input manifest contains fetch errors")
    items = {str(row["code"]): row for row in manifest["items"]}
    if set(selections) != set(items):
        raise AssertionError(f"catalog/manifest mismatch: {len(selections)} vs {len(items)}")
    if DISCOVERY_CACHE.exists():
        cached = read(DISCOVERY_CACHE)
        discoveries = cached["discoveries"]
        errors = cached["errors"]
        print(f"Load discovery cache stocks={len(discoveries)} errors={len(errors)}", flush=True)
    else:
        discoveries = []
        errors = []
        jobs = [(items[code], rows) for code, rows in selections.items()]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_job, job): str(job[0]["code"]) for job in jobs}
            for number, future in enumerate(as_completed(futures), 1):
                try:
                    discoveries.append(future.result())
                except Exception as exc:  # pragma: no cover
                    errors.append({"code": futures[future], "error": str(exc)})
                if number % 25 == 0 or number == len(futures):
                    count = sum(len(row["proposals"]) for row in discoveries)
                    print(f"TG matrix scan {number}/{len(futures)} proposals={count} errors={len(errors)}", flush=True)
        discoveries.sort(key=lambda row: row["code"])
        save(DISCOVERY_CACHE, {
            "method_version": METHOD_VERSION,
            "catalog_event_count": catalog["event_count"],
            "manifest_cohort_count": manifest["cohort_count"],
            "discoveries": discoveries,
            "errors": errors,
        })
    proposals = [proposal for row in discoveries for proposal in row["proposals"]]
    needed_codes = {row["code"] for row in proposals}
    inputs = {}
    for code in sorted(needed_codes):
        raw, actions, _ = load_inputs(items[code])
        inputs[code] = (raw, actions)
    by_family = {family: [row for row in proposals if row["trigger_family"] == family] for family in ATOMIC_FAMILIES}
    variants = {
        **by_family,
        "OLD_PULLBACK_PLUS_DUAL": by_family["OLD_PULLBACK_RELAUNCH"] + by_family["OLD_DUAL_RESONANCE"],
        "OLD_ALL": [row for row in proposals if row["trigger_family"] in OLD_FAMILIES],
        "V2_ALL": [row for row in proposals if row["trigger_family"] in V2_FAMILIES],
        "CANDIDATE_ROUTER_V1": [
            row for row in by_family["OLD_PULLBACK_RELAUNCH"]
            if "CHIP_LARGE_HOLDER_WEEKLY" not in row["active_strategies"]
        ] + [
            row for row in by_family["OLD_DUAL_RESONANCE"]
            if "CHIP_LARGE_HOLDER_WEEKLY" in row["active_strategies"]
        ],
    }
    costs = Costs()
    results = {}
    for code, rows in variants.items():
        print(f"Simulate {code} proposals={len(rows)}", flush=True)
        results[code] = _run_variant(code=code, proposals=rows, inputs=inputs, costs=costs)
    atomic = {family: results[family] for family in ATOMIC_FAMILIES}
    matrix, recommendations = strategy_matrix(atomic)
    scan_skips: Counter[str] = Counter()
    for row in discoveries:
        scan_skips.update(row["skips"])
    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": AS_OF,
        "catalog": {
            "path": str(CATALOG_PATH.resolve()),
            "event_count": catalog["event_count"],
            "stock_count": catalog["monitor_stock_count"],
            "strategy_counts": catalog["strategy_counts"],
        },
        "data": {
            "manifest_path": str(MANIFEST_PATH.resolve()),
            "reused_corrected_count": manifest["reused_corrected_count"],
            "newly_frozen_count": manifest["newly_frozen_count"],
        },
        "parameters": {
            "monitor_bars": V2_SPEC.monitor_bars,
            "budget_per_trade": 10_000.0,
            "position": "ONE_SHOT（一次進場、不加碼）",
            "exit": "所有版本共用既有狀態切換出場",
            "costs": costs.__dict__,
            "large_spec": asdict(LARGE_SPEC),
            "v2_spec": asdict(V2_SPEC),
            "calibration_end": CALIBRATION_END,
            "validation_start": VALIDATION_START,
            "validation_signal_end": VALIDATION_SIGNAL_END,
            "mapping_metric": "20交易日淨報酬；若此前依共同出場規則離場則採實際離場淨報酬",
            "mapping_selection": "校準期平均報酬及PF為正，且平均報酬優於同進場法全體基準；按提升幅度乘樣本數平方根選擇",
        },
        "proposal_count": len(proposals),
        "proposal_family_counts": dict(Counter(row["trigger_family"] for row in proposals)),
        "scan_errors": errors,
        "scan_skip_counts": dict(scan_skips),
        "results": results,
        "strategy_entry_matrix": matrix,
        "recommendations": recommendations,
        "discoveries": discoveries,
    }


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    checks = []

    def add(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    add("all_catalog_stocks_loaded", payload["catalog"]["stock_count"] == 747, payload["catalog"]["stock_count"])
    add("no_scan_errors", not payload["scan_errors"], payload["scan_errors"][:5])
    add("all_families_present", set(payload["proposal_family_counts"]) <= set(ATOMIC_FAMILIES), payload["proposal_family_counts"])
    for code, result in payload["results"].items():
        ids = [row["trade_id"] for row in result["trades"]]
        add(f"{code}.unique_ids", len(ids) == len(set(ids)), len(ids))
        add(f"{code}.funded", result["summary"]["funding_shortfall"] == 0, result["summary"]["funding_shortfall"])
        add(f"{code}.attributed", all(row["entry_proposal"]["active_strategies"] for row in result["trades"]), len(ids))
    failures = [row for row in checks if not row["passed"]]
    return {"check_count": len(checks), "passed_count": len(checks) - len(failures), "failed_count": len(failures), "checks": checks}


def render(payload: dict[str, Any]) -> str:
    lines = [
        "# TG全策略 × 啟蒙進場規則回測", "",
        f"> 來源為TG 2026/05/01～2026/09/04全部可辨識選股；共{payload['catalog']['event_count']:,}筆策略命中、{payload['catalog']['stock_count']}檔。每筆1萬元、一次進場、同一成本與出場規則。", "",
        "## 新舊版本總比較", "",
        "| 版本 | 成交 | 已出場/持有 | 淨損益 | 50萬報酬 | 最大回撤 | 勝率 | PF | 平均MFE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    display = [
        "OLD_MA_RECLAIM", "OLD_PULLBACK_RELAUNCH", "OLD_LARGE_BREAKOUT", "OLD_DUAL_RESONANCE",
        "OLD_PULLBACK_PLUS_DUAL", "OLD_ALL", "V2_EARLY_TAIJI_PULLBACK",
        "V2_LONG_MA_PULLBACK", "V2_Q1_BREAKOUT", "V2_ALL", "CANDIDATE_ROUTER_V1",
    ]
    for code in display:
        summary = payload["results"][code]["summary"]
        lines.append(
            f"| {VARIANT_LABELS[code]} | {summary['trade_count']} | {summary['closed_count']}/{summary['open_or_triggered_count']} | "
            f"{summary['net_pnl']:,.0f} | {summary['return_on_500k_pct']:+.2f}% | {summary['max_drawdown_pct']:.2f}% | "
            f"{_f(summary['positive_trade_rate_pct'])}% | {_f(summary['profit_factor'])} | {_f(summary['average_mfe_pct'])}% |"
        )
    lines += [
        "", "## 策略對應規則（先校準、後驗證）", "",
        f"校準期訊號截至 {CALIBRATION_END}；獨立驗證只使用 {VALIDATION_START}～{VALIDATION_SIGNAL_END} 且已有20交易日結果的訊號。規則只由校準期選出，驗證期不回頭改答案。", "",
        "| 選股策略 | 校準期選出規則 | 校準 n/平均/提升/PF | 驗證 n/平均/提升/PF | 判定 |",
        "|---|---|---:|---:|---|",
    ]
    for row in payload["recommendations"]:
        if row["selected_family"] is None:
            lines.append(f"| {row['strategy']}（{row['strategy_label']}） | — | — | — | {row['status']} |")
            continue
        cal = row["calibration"]
        val = row["validation"]
        lines.append(
            f"| {row['strategy']}（{row['strategy_label']}） | {row['selected_family_label']} | "
            f"{cal['trade_count']} / {_f(cal['mean_return_pct'])}% / {_f(row['calibration_lift_pct'])}% / {_f(cal['profit_factor'])} | "
            f"{val['trade_count']} / {_f(val['mean_return_pct'])}% / {_f(row['validation_lift_pct'])}% / {_f(val['profit_factor'])} | {row['status']} |"
        )
    lines += [
        "", "## 判讀限制", "",
        "- 一筆股票可同時被多個選股策略選到；策略矩陣是條件歸因，各列不能相加成投資組合。",
        "- 雷達與精選交叉屬聚合策略，與原始技術／籌碼／營收策略有重疊。",
        "- 『最好』只在校準期決定，且必須優於同進場法的全體基準；若驗證未支持，就不能指定成正式規則。",
        "- 新版條件若交易極少，代表目前代理過嚴，不以零虧損視為優勝。",
        "- CANDIDATE_ROUTER_V1是看完本次校準與驗證後才形成的候選規則；其全期間績效屬樣本內描述，仍須新的前向資料驗證。",
        "- 期間只有四個月；任何支持結果仍須前向觀察，不能當成穩定獲利保證。",
        "- 本研究沒有修改正式Telegram機器人或自動交易。", "",
        f"方法版本：`{payload['method_version']}`",
    ]
    return "\n".join(lines) + "\n"


def _return_distribution(trades: list[dict[str, Any]]) -> list[tuple[str, int]]:
    buckets = [
        ("低於 -10%", lambda value: value < -10.0),
        ("-10%～低於 0%", lambda value: -10.0 <= value < 0.0),
        ("0%～低於 10%", lambda value: 0.0 <= value < 10.0),
        ("10%～低於 20%", lambda value: 10.0 <= value < 20.0),
        ("20%以上", lambda value: value >= 20.0),
    ]
    values = [float(row["net_return_on_campaign_budget_pct"]) for row in trades]
    return [(label, sum(predicate(value) for value in values)) for label, predicate in buckets]


def _latest_trade_price(trade: dict[str, Any]) -> float | None:
    if trade.get("status") == "CLOSED":
        return None if trade.get("exit_price") is None else float(trade["exit_price"])
    daily = list(trade.get("daily") or [])
    if not daily:
        return None
    return float(daily[-1]["raw_close"])


def render_trade_report(payload: dict[str, Any], variant: str) -> str:
    result = payload["results"][variant]
    summary = result["summary"]
    trades = list(result["trades"])
    closed = [row for row in trades if row.get("status") == "CLOSED"]
    open_rows = [row for row in trades if row.get("status") != "CLOSED"]
    realized = sum(float(row["net_pnl"]) for row in closed)
    unrealized = sum(float(row["net_pnl"]) for row in open_rows)

    def outcomes(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
        values = [float(row["net_pnl"]) for row in rows]
        return (
            sum(value > 0 for value in values),
            sum(value < 0 for value in values),
            sum(value == 0 for value in values),
        )

    closed_outcomes = outcomes(closed)
    open_outcomes = outcomes(open_rows)
    lines = [
        f"# {VARIANT_LABELS[variant]}｜逐筆交易",
        "",
        f"> TG全策略選股 2026/05/01～2026/09/04；績效截至 {payload['as_of']}。每筆上限1萬元、一次進場、不加碼，所有版本使用相同成本、除權息與狀態切換出場。",
        "",
        "## 損益摘要",
        "",
        "| 交易 | 已出場 | 持有中 | 已實現損益 | 持有中估值損益 | 合計淨損益 | 50萬報酬 | 最大同時持倉 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {len(trades)} | {len(closed)} | {len(open_rows)} | {realized:,.2f} | {unrealized:,.2f} | {summary['net_pnl']:,.2f} | {summary['return_on_500k_pct']:+.2f}% | {summary['max_concurrent_positions']} |",
        "",
        f"- 已出場：正 {closed_outcomes[0]}、負 {closed_outcomes[1]}、零 {closed_outcomes[2]}。",
        f"- 持有中：正 {open_outcomes[0]}、負 {open_outcomes[1]}、零 {open_outcomes[2]}；損益為截至日估值並估列賣出成本。",
        f"- 全部交易平均 MFE：{_f(summary['average_mfe_pct'])}%；MFE 至少20%：{summary['mfe20_count']}筆。",
        "",
        "### 最終損益分布",
        "",
        "| 淨報酬區間 | 筆數 |",
        "|---|---:|",
    ]
    for label, count in _return_distribution(trades):
        lines.append(f"| {label} | {count} |")

    table_header = [
        "| 股票 | 來源進場型態 | 來源選股策略 | 訊號日 | 進場日／價 | 初始防線 | 出場／估值日與價格 | MFE／日期 | 淨損益 | 淨報酬 | 狀態 |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]

    def append_trade_table(title: str, rows: list[dict[str, Any]]) -> None:
        lines.extend(["", f"## {title}", "", *table_header])
        if not rows:
            lines.append("| — | — | — | — | — | — | — | — | — | — | — |")
            return
        for trade in sorted(rows, key=lambda row: (row["entry_date"], row["code"], row["trade_id"])):
            proposal = trade["entry_proposal"]
            source = "、".join(proposal["active_strategies"])
            family = FAMILY_LABELS[trade["trigger_family"]]
            last_price = _latest_trade_price(trade)
            if trade.get("status") == "CLOSED":
                performance = f"{trade['exit_date']}／{_f(last_price)}；{trade['exit_reason_label']}"
            else:
                performance = f"{trade['performance_date']}／{_f(last_price)}"
            lines.append(
                f"| {trade['code']} {trade['name']} | {family} | {source} | {proposal['signal_date']} | "
                f"{trade['entry_date']}／{float(trade['entry_price']):,.2f} | {float(trade['initial_defense']):,.2f} | "
                f"{performance} | {float(trade['max_campaign_gross_return_pct']):+.2f}%／{trade['max_campaign_date']} | "
                f"{float(trade['net_pnl']):+,.2f} | {float(trade['net_return_on_campaign_budget_pct']):+.2f}% | {trade['status_label']} |"
            )

    append_trade_table("CLOSED（交易已結束／已實現）", closed)
    append_trade_table("OPEN（持有中／未實現）", open_rows)
    lines.extend([
        "",
        "## 口徑",
        "",
        "- MFE 是持有期間最高價相對進場價的最大有利幅度，不代表能在最高點成交。",
        "- 已實現損益採實際模擬出場；持有中損益以截至日收盤估值，並估列賣出成本。",
        "- 組合版會在版本內排除同股同日重複訊號，以及前一筆尚未出場時的重複開倉。",
        "- 來源選股策略是一筆交易觸發時仍在60交易日監控窗內的策略，不表示每個來源各買一份。",
        "- 報告是研究回測，不是實際成交紀錄或獲利保證。",
        "",
        f"方法版本：`{payload['method_version']}`",
    ])
    return "\n".join(lines) + "\n"


def _write_flat_outputs(output_dir: Path, payload: dict[str, Any]) -> None:
    matrix_columns = [
        "strategy", "strategy_label", "entry_family", "entry_family_label",
        "all_trade_count", "all_mean_return_pct", "calibration_trade_count",
        "calibration_mean_return_pct", "calibration_lift_pct", "calibration_profit_factor",
        "validation_trade_count", "validation_mean_return_pct", "validation_lift_pct",
        "validation_profit_factor",
    ]
    with (output_dir / "strategy_entry_matrix.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=matrix_columns)
        writer.writeheader()
        for row in payload["strategy_entry_matrix"]:
            writer.writerow({
                "strategy": row["strategy"], "strategy_label": row["strategy_label"],
                "entry_family": row["entry_family"], "entry_family_label": row["entry_family_label"],
                "all_trade_count": row["all_as_of"]["trade_count"],
                "all_mean_return_pct": row["all_as_of"]["mean_return_pct"],
                "calibration_trade_count": row["calibration_20_session"]["trade_count"],
                "calibration_mean_return_pct": row["calibration_20_session"]["mean_return_pct"],
                "calibration_lift_pct": row["calibration_lift_pct"],
                "calibration_profit_factor": row["calibration_20_session"]["profit_factor"],
                "validation_trade_count": row["validation_20_session"]["trade_count"],
                "validation_mean_return_pct": row["validation_20_session"]["mean_return_pct"],
                "validation_lift_pct": row["validation_lift_pct"],
                "validation_profit_factor": row["validation_20_session"]["profit_factor"],
            })
    recommendation_columns = [
        "strategy", "strategy_label", "selected_family", "selected_family_label", "status",
        "calibration_trade_count", "calibration_mean_return_pct", "calibration_lift_pct",
        "validation_trade_count", "validation_mean_return_pct", "validation_lift_pct",
    ]
    with (output_dir / "strategy_rule_recommendations.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=recommendation_columns)
        writer.writeheader()
        for row in payload["recommendations"]:
            cal = row.get("calibration") or {}
            val = row.get("validation") or {}
            writer.writerow({
                "strategy": row["strategy"], "strategy_label": row["strategy_label"],
                "selected_family": row["selected_family"], "selected_family_label": row["selected_family_label"],
                "status": row["status"], "calibration_trade_count": cal.get("trade_count"),
                "calibration_mean_return_pct": cal.get("mean_return_pct"),
                "calibration_lift_pct": row.get("calibration_lift_pct"),
                "validation_trade_count": val.get("trade_count"),
                "validation_mean_return_pct": val.get("mean_return_pct"),
                "validation_lift_pct": row.get("validation_lift_pct"),
            })
    trade_columns = [
        "variant", "family", "code", "name", "signal_date", "entry_date", "entry_price",
        "initial_defense", "status", "exit_date", "net_pnl", "net_return_pct", "mfe_pct",
        "fixed_20_session_net_return_pct", "active_strategies",
    ]
    with (output_dir / "executed_trades.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=trade_columns)
        writer.writeheader()
        for variant in ATOMIC_FAMILIES:
            for trade in payload["results"][variant]["trades"]:
                proposal = trade["entry_proposal"]
                writer.writerow({
                    "variant": variant, "family": trade["trigger_family"], "code": trade["code"],
                    "name": trade["name"], "signal_date": proposal["signal_date"],
                    "entry_date": trade["entry_date"], "entry_price": trade["entry_price"],
                    "initial_defense": trade["initial_defense"], "status": trade["status_label"],
                    "exit_date": trade["exit_date"], "net_pnl": trade["net_pnl"],
                    "net_return_pct": trade["net_return_on_campaign_budget_pct"],
                    "mfe_pct": trade["max_campaign_gross_return_pct"],
                    "fixed_20_session_net_return_pct": trade["fixed_20_session_net_return_pct"],
                    "active_strategies": "|".join(proposal["active_strategies"]),
                })


def _write_trade_reports(output_dir: Path, payload: dict[str, Any]) -> None:
    report_dir = output_dir / "trade_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    for index, variant in enumerate(TRADE_REPORT_VARIANTS, 1):
        filename = f"{index:02d}_{variant.lower()}.md"
        (report_dir / filename).write_text(
            render_trade_report(payload, variant), encoding="utf-8",
        )


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
    _write_flat_outputs(args.output_dir, payload)
    _write_trade_reports(args.output_dir, payload)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(render(payload))
    if validation["failed_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
