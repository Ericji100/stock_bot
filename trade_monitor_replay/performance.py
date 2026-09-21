"""Read-only accounting for isolated replay runs, never an execution engine.

Entries must have programmatic fills. A model STOP/EXIT is a close-confirmed
decision, accounted at the following available open (not its already seen close).
Open positions at the requested cutoff remain unrealized. No future bar enters AI.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .reproducibility import build_execution_decision_trace

POINT_VALUE = 10.0
TAX_RATE = 0.00002


def _setup_family(fill: dict[str, Any]) -> str:
    strategy = str(fill.get("entry_strategy") or "")
    source = str(fill.get("candidate_source") or "")
    if strategy in {
        "Q2_FALSE_BREAK_RECLAIM",
        "Q2_FAILED_COUNTERTREND_REVERSAL",
        "Q2_SLOW_OUTER_EXPANSION_FAILURE",
    }:
        return "Q2"
    if source in {
        "FALSE_BREAK_RECLAIM",
        "Q2_FAILED_REVERSE_CANDIDATE",
        "Q2_SLOW_OUTER_EXPANSION_FAILURE",
    }:
        return "Q2"
    if strategy in {"Q4_PULLBACK_CONTINUATION", "Q4_AGGRESSIVE_PULLBACK_REVERSAL"}:
        return "Q4"
    if source in {
        "ANCHOR_LEG_SEQUENCE",
        "CONFIRMED_PULLBACK_ENDPOINT_N2",
        "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
    }:
        return "Q4"
    return "OTHER"


def _closed_trade_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    nets = [float(item["net_ntd"]) for item in trades]
    winners = [value for value in nets if value > 0]
    losers = [value for value in nets if value < 0]
    equity = peak = drawdown = 0.0
    streak = longest_streak = 0
    for net in nets:
        equity += net
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        streak = streak + 1 if net < 0 else 0
        longest_streak = max(longest_streak, streak)
    gross_profit = sum(winners)
    gross_loss = -sum(losers)
    average_win = gross_profit / len(winners) if winners else None
    average_loss = gross_loss / len(losers) if losers else None
    return {
        "trade_count": len(trades),
        "wins_after_cost": len(winners),
        "losses_after_cost": len(losers),
        "breakeven_after_cost": sum(value == 0 for value in nets),
        "win_rate_after_cost": len(winners) / len(nets) if nets else None,
        "gross_points": sum(float(item["gross_points"]) for item in trades),
        "gross_ntd": sum(float(item["gross_ntd"]) for item in trades),
        "net_ntd": sum(nets),
        "average_win_net_ntd": average_win,
        "average_loss_net_ntd": average_loss,
        "payoff_ratio_after_cost": (
            average_win / average_loss
            if average_win is not None and average_loss not in {None, 0}
            else None
        ),
        "expectancy_net_ntd_per_trade": sum(nets) / len(nets) if nets else None,
        "profit_factor_after_cost": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown_ntd": drawdown,
        "max_consecutive_losses_after_cost": longest_streak,
        "average_mae_points": (
            sum(float(item["mae_points"]) for item in trades) / len(trades)
            if trades
            else None
        ),
        "average_mfe_points": (
            sum(float(item["mfe_points"]) for item in trades) / len(trades)
            if trades
            else None
        ),
        "reentry_trade_count": sum(item.get("entry_role") == "REENTRY" for item in trades),
        "reentry_net_ntd": sum(
            float(item["net_ntd"])
            for item in trades
            if item.get("entry_role") == "REENTRY"
        ),
    }


def account_trades(
    bars: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    cutoff: str,
    fee_per_side: float,
    slippage_points_per_side: float,
) -> dict[str, Any]:
    if fee_per_side < 0 or slippage_points_per_side < 0:
        raise ValueError("Costs must be nonnegative.")
    end = datetime.fromisoformat(cutoff)
    visible = sorted(
        [b for b in bars if datetime.fromisoformat(b["bar_time"]) <= end],
        key=lambda b: b["bar_time"],
    )
    exits = sorted(
        [d for d in decisions if d["action"] in {"STOP", "EXIT"}
         and datetime.fromisoformat(d["bar_time"]) <= end],
        key=lambda d: d["bar_time"],
    )
    actual_fills = sorted(
        [f for f in fills if f.get("event_type") == "ENTRY_FILLED"
         and datetime.fromisoformat(f["fill_time"]) <= end],
        key=lambda f: f["fill_time"],
    )
    unique_fills = {}
    for fill in actual_fills:
        key = fill.get("event_id") or (fill.get("setup_key"), fill["fill_time"], fill["direction"])
        previous = unique_fills.get(key)
        if previous is not None and any(previous.get(k) != fill.get(k)
                for k in ("fill_time", "fill_price", "direction", "stop_price")):
            raise ValueError("Conflicting duplicate fill; audit the rewind before accounting.")
        unique_fills[key] = fill
    actual_fills = list(unique_fills.values())
    stop_events = sorted(
        [
            f
            for f in fills
            if f.get("event_type") == "STOP_FILLED"
            and f.get("fill_time")
            and datetime.fromisoformat(f["fill_time"]) <= end
        ],
        key=lambda f: f["fill_time"],
    )
    unique_stops: dict[Any, dict[str, Any]] = {}
    for stop_event in stop_events:
        key = stop_event.get("event_id") or (
            stop_event.get("setup_key"),
            stop_event["fill_time"],
            stop_event.get("direction"),
        )
        previous = unique_stops.get(key)
        if previous is not None and any(
            previous.get(k) != stop_event.get(k)
            for k in ("fill_time", "fill_price", "direction", "stop_price")
        ):
            raise ValueError("Conflicting duplicate stop fill; audit the rewind before accounting.")
        unique_stops[key] = stop_event
    stop_events = list(unique_stops.values())

    program_exit_events = sorted(
        [
            f
            for f in fills
            if f.get("event_type") == "EXIT_FILLED"
            and f.get("fill_time")
            and datetime.fromisoformat(f["fill_time"]) <= end
        ],
        key=lambda f: f["fill_time"],
    )
    unique_program_exits: dict[Any, dict[str, Any]] = {}
    for exit_event in program_exit_events:
        key = exit_event.get("event_id") or (
            exit_event.get("setup_key"),
            exit_event.get("entry_time"),
            exit_event["fill_time"],
        )
        previous = unique_program_exits.get(key)
        if previous is not None and any(
            previous.get(k) != exit_event.get(k)
            for k in ("signal_time", "fill_time", "fill_price", "reason_code")
        ):
            raise ValueError("Conflicting duplicate program exit; audit the rewind before accounting.")
        unique_program_exits[key] = exit_event
    program_exit_events = list(unique_program_exits.values())

    closed, opened, used_exits, used_stops, used_program_exits = [], [], set(), set(), set()
    for i, fill in enumerate(actual_fills):
        at = fill["fill_time"]
        sign = 1 if fill["direction"] == "LONG" else -1
        entry = float(fill["fill_price"])
        stop = float(fill["stop_price"])
        exit_signal = next((d for j, d in enumerate(exits)
                            if j not in used_exits and d["bar_time"] >= at), None)
        exit_bar = next((b for b in visible
                         if exit_signal and b["bar_time"] > exit_signal["bar_time"]), None)
        stop_event = None
        if exit_signal and exit_signal["action"] == "STOP":
            stop_event = next(
                (
                    event
                    for index, event in enumerate(stop_events)
                    if index not in used_stops
                    and event["fill_time"] >= at
                    and event["fill_time"] <= exit_signal["bar_time"]
                    and (
                        not event.get("setup_key")
                        or not fill.get("setup_key")
                        or event.get("setup_key") == fill.get("setup_key")
                    )
                ),
                None,
            )
        program_exit = next(
            (
                event
                for index, event in enumerate(program_exit_events)
                if index not in used_program_exits
                and event["fill_time"] >= at
                and (
                    not event.get("setup_key")
                    or not fill.get("setup_key")
                    or event.get("setup_key") == fill.get("setup_key")
                )
                and (
                    not event.get("entry_time")
                    or event.get("entry_time") == at
                )
            ),
            None,
        )
        record = {
            "entry_time": at, "direction": fill["direction"],
            "setup_key": fill.get("setup_key"), "entry_price": entry,
            "setup_name": fill.get("setup_name"),
            "setup_family": _setup_family(fill),
            "candidate_source": fill.get("candidate_source"),
            "entry_strategy": fill.get("entry_strategy"),
            "entry_role": fill.get("entry_role"),
            "initial_stop": stop, "initial_risk_points": abs(entry - stop),
            "exit_signal_time": (
                program_exit.get("signal_time")
                if program_exit is not None
                else exit_signal["bar_time"] if exit_signal else None
            ),
            "exit_reason": (
                program_exit.get("reason_code")
                if program_exit is not None
                else exit_signal.get("reason") if exit_signal else None
            ),
        }
        stop_price_fallback = (
            float(exit_signal["stop_price"])
            if exit_signal
            and exit_signal["action"] == "STOP"
            and isinstance(exit_signal.get("stop_price"), (int, float))
            else None
        )
        if not exit_bar and stop_event is None and program_exit is None and stop_price_fallback is None:
            if i + 1 < len(actual_fills):
                raise ValueError("Overlapping fills or missing exit decisions.")
            mark = float(visible[-1]["close"]) if visible else entry
            record.update(status="OPEN", mark_price=mark,
                          unrealized_points=(mark-entry)*sign,
                          unrealized_gross_ntd=(mark-entry)*sign*POINT_VALUE)
            opened.append(record)
            continue
        if exit_signal is not None:
            used_exits.add(exits.index(exit_signal))
        if program_exit is not None and (
            stop_event is None or program_exit["fill_time"] < stop_event["fill_time"]
        ):
            used_program_exits.add(program_exit_events.index(program_exit))
            exit_time = program_exit["fill_time"]
            price = float(program_exit["fill_price"])
            exit_source = "PROGRAMMATIC_BEHAVIOR_EXIT"
            exit_action = "EXIT"
        elif stop_event is not None:
            used_stops.add(stop_events.index(stop_event))
            exit_time = stop_event["fill_time"]
            price = float(stop_event["fill_price"])
            exit_source = "PROGRAMMATIC_PROTECTIVE_STOP"
            exit_action = "STOP"
            record["exit_signal_time"] = stop_event.get("trigger_time") or stop_event["fill_time"]
            record["exit_reason"] = "PROTECTIVE_STOP_TOUCHED"
        elif stop_price_fallback is not None:
            exit_time = exit_signal["bar_time"]
            price = stop_price_fallback
            exit_source = "DECLARED_PROTECTIVE_STOP_FALLBACK"
            exit_action = "STOP"
        else:
            exit_time = exit_bar["bar_time"]
            price = float(exit_bar["open"])
            exit_source = "NEXT_OPEN_AFTER_CLOSE_DECISION"
            exit_action = str(exit_signal["action"])
        if i + 1 < len(actual_fills) and exit_time > actual_fills[i + 1]["fill_time"]:
            raise ValueError("Accounting exit overlaps the next entry.")
        points = (price-entry)*sign
        # Estimated mathematical tax, not a broker invoice/rounding convention.
        slipped_entry = entry + sign*slippage_points_per_side
        slipped_exit = price - sign*slippage_points_per_side
        tax = (slipped_entry+slipped_exit)*POINT_VALUE*TAX_RATE
        cost = 2*fee_per_side + tax + 2*slippage_points_per_side*POINT_VALUE
        held = [b for b in visible if at <= b["bar_time"] < exit_time]
        favorable = [0.0, points] + [((float(b["high"]) if sign > 0 else float(b["low"]))-entry)*sign for b in held]
        adverse = [0.0, points] + [((float(b["low"]) if sign > 0 else float(b["high"]))-entry)*sign for b in held]
        record.update(
            status="CLOSED", exit_time=exit_time, exit_price=price,
            exit_source=exit_source,
            exit_action=exit_action, gross_points=points,
            gross_ntd=points*POINT_VALUE, estimated_tax_ntd=tax,
            fee_ntd=2*fee_per_side, slippage_ntd=2*slippage_points_per_side*POINT_VALUE,
            net_ntd=points*POINT_VALUE-cost,
            gross_r=points/abs(entry-stop) if entry != stop else None,
            mfe_points=max(favorable), mae_points=-min(adverse),
        )
        closed.append(record)
    nets = [t["net_ntd"] for t in closed]
    equity = peak = drawdown = 0.0
    streak = longest_streak = 0
    for net in nets:
        equity += net
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
        streak = streak+1 if net < 0 else 0
        longest_streak = max(longest_streak, streak)
    profit = sum(n for n in nets if n > 0)
    loss = -sum(n for n in nets if n < 0)
    q2_trades = [item for item in closed if item.get("setup_family") == "Q2"]
    q4_trades = [item for item in closed if item.get("setup_family") == "Q4"]
    other_trades = [item for item in closed if item.get("setup_family") == "OTHER"]
    return {
        "accounting_policy": "one_TMF; accepted_entry_next_open; protective_stop_at_program_fill; discretionary_exit_next_open; no_cutoff_forced_exit",
        "sample_type": "in_sample_development_not_long_term_expectancy",
        "cutoff": cutoff, "fee_per_side_ntd": fee_per_side,
        "slippage_points_per_side": slippage_points_per_side,
        "point_value_ntd": POINT_VALUE, "estimated_tax_rate": TAX_RATE,
        "trades": closed, "open_positions": opened,
        "entry_count": len(actual_fills), "closed_count": len(closed),
        "wins_after_cost": sum(n > 0 for n in nets), "losses_after_cost": sum(n < 0 for n in nets),
        "breakeven_after_cost": sum(n == 0 for n in nets),
        "win_rate_after_cost": sum(n > 0 for n in nets)/len(nets) if nets else None,
        "gross_points": sum(t["gross_points"] for t in closed),
        "gross_ntd": sum(t["gross_ntd"] for t in closed),
        "net_ntd": sum(nets), "mean_net_per_closed_trade": sum(nets)/len(nets) if nets else None,
        "profit_factor_after_cost": profit/loss if loss else None,
        "closed_equity_max_drawdown_ntd": drawdown,
        "max_consecutive_losses_after_cost": longest_streak,
        "strategy_breakdown": {
            "Q2": _closed_trade_metrics(q2_trades),
            "Q4": _closed_trade_metrics(q4_trades),
            "Q2_Q4_COMBINED": _closed_trade_metrics(q2_trades + q4_trades),
            "OTHER": _closed_trade_metrics(other_trades),
        },
        "not_mark_to_market_drawdown": True,
        "execution_rejections": sum(f.get("event_type") in {"ENTRY_EXPIRED", "ENTRY_REJECTED_GAP"} for f in fills),
        "limitations": ["OHLC cannot determine whether the bar high or low occurred first", "Stop gaps fill at the observed bar open; no order-book latency or queue simulation", "Tax is estimated, not broker-rounded", "No account balance/return percentage"],
    }


def load_run_report(run_dir: Path, *, fee: float, slippage: float) -> dict[str, Any]:
    manifest = json.loads((run_dir/"manifest.json").read_text(encoding="utf-8-sig"))
    stage_one_v34 = (
        manifest.get("analysis_mode") == "AI_HYBRID"
        and manifest.get("trade_direction_policy") == "LONG_ONLY"
        and manifest.get("trade_setup_policy") == "LONG_Q2_Q4_ONLY"
    )
    # Stage-one reports are admissible only from a fully completed, unmigrated
    # run whose content-addressed execution identity is intact.  Legacy report
    # readers retain their historical partial-run behavior for compatibility.
    decision_trace = (
        build_execution_decision_trace(run_dir, require_complete=True)
        if stage_one_v34
        else None
    )
    completed = int(manifest["next_day_index"])
    if not completed:
        raise ValueError("No completed day analysis; do not report this as a trading day.")
    times = manifest["selected_bar_times"][:completed]
    with (run_dir/"bars.csv").open(encoding="utf-8-sig", newline="") as handle:
        bars = list(csv.DictReader(handle))
    for bar in bars:
        bar["bar_time"] = datetime.fromisoformat(bar["bar_time"]).isoformat()
    event_path = run_dir/"execution-events.jsonl"
    fills = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line] if event_path.exists() else []
    decisions = []
    canonical_points: list[dict[str, Any]] = []
    for at in times:
        key = datetime.fromisoformat(at).strftime("day-%Y%m%d-%H%M")
        paths = list((run_dir/"analysis").glob(key+"*-validated.json"))
        if not paths:
            raise ValueError("Missing validated analysis at "+at)
        validated_path = max(paths, key=lambda p:p.stat().st_mtime_ns)
        data = json.loads(validated_path.read_text(encoding="utf-8-sig"))
        analysis = data["analysis"]
        decision = {
            "bar_time": at,
            "action": analysis["action"]["position_action"],
            "reason": analysis["notification_reason"],
            "stop_price": analysis["action"].get("stop_price"),
            "setup_key": analysis["action"].get("setup_key"),
        }
        decisions.append(decision)
        checkpoint_path = validated_path.with_name(
            validated_path.name.replace("-validated.json", "-state-checkpoint.json")
        )
        checkpoint = (
            json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
            if checkpoint_path.exists()
            else {}
        )
        canonical_points.append(
            {
                "bar_time": at,
                "analysis": analysis,
                "position_state": checkpoint.get("position_state"),
            }
        )
    fills = _canonical_execution_events(fills, canonical_points)
    report = account_trades(bars, fills, decisions, cutoff=times[-1], fee_per_side=fee, slippage_points_per_side=slippage)
    completed_presentation = int(
        manifest.get("completed_presentation_bars", manifest.get("completed_day_bars", completed))
    )
    total_presentation = len(
        manifest.get("presentation_bar_times") or manifest["selected_bar_times"]
    )
    report.update(
        run_id=run_dir.name,
        target_date=manifest.get("target_date"),
        execution_version=manifest["execution_version"],
        execution_implementation_sha256=manifest.get(
            "execution_implementation_sha256"
        ),
        execution_prompt_sha256=manifest.get("execution_prompt_sha256"),
        rule_sha256=manifest.get("rule_sha256"),
        model_rules_sha256=manifest.get("model_rules_sha256"),
        schema_sha256=manifest.get("schema_sha256"),
        ai_provider=manifest.get("ai_provider"),
        ai_model=manifest.get("ai_model"),
        ai_reasoning_effort=manifest.get("ai_reasoning_effort"),
        execution_spec_sha256=(
            _stage_one_execution_spec_sha256(manifest)
            if stage_one_v34
            else None
        ),
        input_plan_sha256=(
            _sha256_json(decision_trace["input_identity"])
            if isinstance(decision_trace, dict)
            else None
        ),
        decision_trace_sha256=(
            decision_trace["decision_trace_sha256"]
            if isinstance(decision_trace, dict)
            else None
        ),
        course_state_trace_sha256=(
            decision_trace["course_state_trace_sha256"]
            if isinstance(decision_trace, dict)
            else None
        ),
        completed_program_tick_count=completed,
        total_planned_program_tick_count=len(manifest["selected_bar_times"]),
        completed_presentation_count=completed_presentation,
        total_planned_presentation_count=total_presentation,
        # Backward-compatible name now means user-facing analysis points, not
        # the hidden one-minute state-machine ticks.
        completed_analysis_count=completed_presentation,
        total_planned_analysis_count=total_presentation,
        complete=completed == len(manifest["selected_bar_times"]),
    )
    return report


def aggregate_stage_one_run_reports(
    run_dirs: list[Path],
    *,
    fee: float,
    slippage: float,
) -> dict[str, Any]:
    """Aggregate distinct replay days only when the full execution spec matches.

    Repeated consistency runs of the same trading day are evidence about model
    stability, not independent performance observations.  Rejecting duplicate
    dates prevents an attractive run from being counted two or three times.
    Every constituent is loaded through the strict v34 report path first, so a
    partial or migrated run fails before any PnL is combined.
    """

    if not run_dirs:
        raise ValueError("At least one complete stage-one run is required.")
    reports = [
        load_run_report(Path(run_dir), fee=fee, slippage=slippage)
        for run_dir in run_dirs
    ]
    run_ids = [str(item.get("run_id") or "") for item in reports]
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("Duplicate run_id cannot be counted twice.")
    dates = [str(item.get("target_date") or "") for item in reports]
    if not all(dates) or len(set(dates)) != len(dates):
        raise ValueError(
            "Stage-one performance requires one accepted run per distinct target_date."
        )
    specs = [item.get("execution_spec_sha256") for item in reports]
    if any(not isinstance(value, str) or len(value) != 64 for value in specs):
        raise ValueError("Every aggregate member must be a strict stage-one v34 run.")
    if len(set(specs)) != 1:
        raise ValueError("Cannot mix different execution/model/rule specifications.")

    trades: list[dict[str, Any]] = []
    open_positions: list[dict[str, Any]] = []
    for report in reports:
        identity = {
            "run_id": report["run_id"],
            "target_date": report["target_date"],
        }
        trades.extend({**item, **identity} for item in report.get("trades", []))
        open_positions.extend(
            {**item, **identity} for item in report.get("open_positions", [])
        )
    trades.sort(key=lambda item: (str(item.get("entry_time") or ""), str(item["run_id"])))
    q2 = [item for item in trades if item.get("setup_family") == "Q2"]
    q4 = [item for item in trades if item.get("setup_family") == "Q4"]
    other = [item for item in trades if item.get("setup_family") == "OTHER"]
    return {
        "sample_type": "stage_one_multi_day_development_not_live_readiness",
        "complete": True,
        "run_count": len(reports),
        "target_dates": sorted(dates),
        "run_ids": run_ids,
        "execution_spec_sha256": specs[0],
        "input_plan_sha256": [item["input_plan_sha256"] for item in reports],
        "decision_trace_sha256": [
            item["decision_trace_sha256"] for item in reports
        ],
        "course_state_trace_sha256": [
            item["course_state_trace_sha256"] for item in reports
        ],
        "fee_per_side_ntd": fee,
        "slippage_points_per_side": slippage,
        "trades": trades,
        "open_positions": open_positions,
        "closed_trade_metrics": _closed_trade_metrics(trades),
        "strategy_breakdown": {
            "Q2": _closed_trade_metrics(q2),
            "Q4": _closed_trade_metrics(q4),
            "Q2_Q4_COMBINED": _closed_trade_metrics(q2 + q4),
            "OTHER": _closed_trade_metrics(other),
        },
        "limitations": [
            "Repeated runs of the same target_date are rejected, not counted as independent trades.",
            "Open positions are reported separately and excluded from closed-trade expectancy.",
            "Development samples do not establish live positive expectancy.",
        ],
    }


def _stage_one_execution_spec_sha256(manifest: dict[str, Any]) -> str:
    fields = (
        "rule_version",
        "rule_sha256",
        "model_rules_sha256",
        "schema_sha256",
        "execution_version",
        "execution_prompt_sha256",
        "execution_implementation_sha256",
        "deterministic_timeline_engine_version",
        "deterministic_timeline_engine_sha256",
        "analysis_mode",
        "decision_authority",
        "execution_profile",
        "ai_input_view_version",
        "trade_direction_policy",
        "trade_setup_policy",
        "ai_provider",
        "ai_model",
        "ai_reasoning_effort",
    )
    return _sha256_json({field: manifest.get(field) for field in fields})


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_execution_events(
    events: list[dict[str, Any]],
    canonical_points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Discard append-only execution events left by superseded rewind branches.

    A programmatic fill is canonical only when the first later validated point
    either still carries that exact entry in its position state or closes that
    exact setup.  A stop fill likewise requires the corresponding canonical
    STOP action.  This preserves the immutable audit log while keeping the
    read-only performance report on the final replay branch.
    """

    points = sorted(canonical_points, key=lambda item: str(item.get("bar_time")))
    entries = [item for item in events if item.get("event_type") == "ENTRY_FILLED"]
    stops = [item for item in events if item.get("event_type") == "STOP_FILLED"]
    program_exits = [item for item in events if item.get("event_type") == "EXIT_FILLED"]
    accepted_ids: set[Any] = set()
    previous_at: datetime | None = None
    previous_position: dict[str, Any] = {}
    for point in points:
        point_at = datetime.fromisoformat(str(point["bar_time"]))
        analysis = point.get("analysis")
        action = analysis.get("action") if isinstance(analysis, dict) else {}
        position = point.get("position_state")
        position = position if isinstance(position, dict) else {}
        interval_entries = [
            item
            for item in entries
            if item.get("fill_time")
            and (fill_at := datetime.fromisoformat(str(item["fill_time"]))) <= point_at
            and (previous_at is None or fill_at > previous_at)
        ]
        for entry in interval_entries:
            setup_key = entry.get("setup_key")
            carried = (
                position.get("status") in {"LONG", "SHORT"}
                and position.get("active_setup_key") == setup_key
                and position.get("entry_time") == entry.get("fill_time")
            )
            closed_here = (
                isinstance(action, dict)
                and action.get("position_action") in {"STOP", "EXIT"}
                and action.get("setup_key") == setup_key
            )
            if carried or closed_here:
                accepted_ids.add(entry.get("event_id") or id(entry))
        interval_stops = [
            item
            for item in stops
            if item.get("fill_time")
            and (fill_at := datetime.fromisoformat(str(item["fill_time"]))) <= point_at
            and (previous_at is None or fill_at > previous_at)
        ]
        if isinstance(action, dict) and action.get("position_action") == "STOP":
            for stop in interval_stops:
                if stop.get("setup_key") == action.get("setup_key"):
                    accepted_ids.add(stop.get("event_id") or id(stop))
        interval_program_exits = [
            item
            for item in program_exits
            if item.get("fill_time")
            and (fill_at := datetime.fromisoformat(str(item["fill_time"]))) <= point_at
            and (previous_at is None or fill_at > previous_at)
        ]
        if isinstance(action, dict) and action.get("position_action") == "EXIT":
            for exit_event in interval_program_exits:
                if exit_event.get("setup_key") == action.get("setup_key"):
                    accepted_ids.add(exit_event.get("event_id") or id(exit_event))
        prior_pending_exit = previous_position.get("pending_exit")
        if isinstance(prior_pending_exit, dict):
            for exit_event in interval_program_exits:
                if (
                    exit_event.get("setup_key") == prior_pending_exit.get("setup_key")
                    and exit_event.get("signal_time") == prior_pending_exit.get("signal_time")
                    and exit_event.get("direction") == prior_pending_exit.get("direction")
                ):
                    accepted_ids.add(exit_event.get("event_id") or id(exit_event))
        previous_at = point_at
        previous_position = position

    return [
        item
        for item in events
        if item.get("event_type") not in {"ENTRY_FILLED", "STOP_FILLED", "EXIT_FILLED"}
        or (item.get("event_id") or id(item)) in accepted_ids
    ]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, nargs="+")
    parser.add_argument("--fee", type=float, required=True, help="NTD per contract per side, excluding tax")
    parser.add_argument("--slippage", type=float, default=1.0, help="adverse points per side")
    args = parser.parse_args()
    result = (
        load_run_report(args.run_dir[0], fee=args.fee, slippage=args.slippage)
        if len(args.run_dir) == 1
        else aggregate_stage_one_run_reports(
            args.run_dir,
            fee=args.fee,
            slippage=args.slippage,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
