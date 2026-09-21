"""Build anonymous, causal, atomic-question packets for monitoring V2.

The formal path scans every monitored stock-day from the frozen 1,029-stock
input.  Review points are rebuilt from full visible price history; the old V1
10,476 policy-boundary dates are never the formal source of review points.
This module does not call AI, read performance, or classify a trade.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

try:
    from .hybrid_v3_sharding_v2 import (
        _publish_immutable,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )
    from .hybrid_v3_objective_state_v2 import (
        ENGINE_STATUS, ENGINE_VERSION, PIVOT_DEFINITION, derive_objective_state,
    )
except ImportError:
    from hybrid_v3_sharding_v2 import (
        _publish_immutable,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )
    from hybrid_v3_objective_state_v2 import (
        ENGINE_STATUS, ENGINE_VERSION, PIVOT_DEFINITION, derive_objective_state,
    )


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2"
DAILY = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan"
DEFAULT_INPUT_MANIFEST = PARENT / "input_manifest.json"
DEFAULT_PACKET_MANIFEST = PARENT / "packet_manifest.json"
DEFAULT_IDENTITY_MAP = DAILY / "sealed_identity_map.json"
DEFAULT_V1_BOUNDARIES = DAILY / "hybrid_monitoring_v1/anonymous_policy_boundary_packets.jsonl"
DEFAULT_ATOMIC_SCHEMA = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"
DEFAULT_FREEZE_SOURCE_MANIFEST = DAILY / "packet_manifest.json"

PACKET_VERSION = "hybrid-v3-atomic-question-packet-v2"
BUILDER_VERSION = "hybrid-v3-atomic-packets-v2"
BUILDER_STATUS = "FINAL"
FORMAL_REVIEW_POINT_POLICY = "V2_FULL_DAILY_SCAN_LOSSLESS_LAZY_POLICY_BOUNDARIES"
PIVOT_SCALE_DISCLOSURE = PIVOT_DEFINITION
MA_WINDOWS = (5, 13, 21, 55, 105, 144)
MACD_PARAMETERS = (21, 55, 55)
PREFERRED_STRUCTURE_BARS = 750
INDICATOR_WARMUP_BARS = 200
AI_VISIBLE_RECENT_BAR_WINDOW_DRAFT = 60
AI_VISIBLE_SELECTION_WINDOW = 10
MAX_ANCHOR_CANDIDATES = 16
MAX_RELATION_CANDIDATES = 24
MAX_STOP_CANDIDATES = 12
FORMAL_STOCK_COUNT = 1029
FORMAL_MONITORED_STOCK_DAYS = 151804
FORMAL_BUILD_SHARD_COUNT = 3
FORMAL_SOURCE_FILE = "review_points.jsonl"
FORMAL_SOURCE_MANIFEST_FILE = "review_point_manifest.json"
FORMAL_PLAN_FILE = "build_plan.manifest.json"
SCENARIOS = (
    "MATURE_TREND_PULLBACK",
    "MACRO_COPY_RESONANCE",
    "BEAR_REVERSAL_LEFT_RIGHT",
    "FRESH_Q1_EXPANSION",
)
HYPOTHESIS_ENUMERATION_POLICY = "CAUSAL_GRAPH_V2_ALTERNATING_CAMPAIGN_TRIPLES_AND_CURRENT_CONTEXT"
SAMPLING_STRATUM_PRIORITY = (
    "MACRO_DEFENSE_REMOVE_PROXY",
    "BEAR_REVERSAL_OBJECTIVE_PROXY",
    "MACRO_COPY_OBJECTIVE_PROXY",
    "FRESH_Q1_OBJECTIVE_PROXY",
    "V2_CORE_OBJECTIVE_PROXY",
    "WAIT_POLICY_BOUNDARY",
)

# These fields can only be known after the decision cutoff (or directly reveal
# trade outcomes).  They must never enter an AI-visible packet, even when a
# caller injects an otherwise well-formed evidence row.
FORBIDDEN_OUTCOME_FIELDS = {
    "mfe", "mae", "pnl", "profit", "return_after", "forward_return",
    "future_return", "future_open", "future_high", "future_low", "future_close",
    "exit", "exit_date", "exit_price", "outcome", "realized_return",
    "unrealized_return", "max_favorable_excursion", "max_adverse_excursion",
}

class AtomicPacketError(ValueError):
    pass


class FormalBuildIntegrityError(AtomicPacketError):
    """Raised when a frozen source, resumable fragment, or merge is inconsistent."""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise AtomicPacketError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _number(value: Any, digits: int = 5) -> float | None:
    if value is None or pd.isna(value) or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def add_causal_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Rebuild rolling/EMA state from the complete ordered adjusted-price CSV."""
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise AtomicPacketError(f"price frame is missing columns: {missing}")
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"], errors="raise")
    data = data.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    for column in ("open", "high", "low", "close", "volume"):
        data[column] = pd.to_numeric(data[column], errors="raise")
    for window in MA_WINDOWS:
        data[f"ma{window}"] = data["close"].rolling(window).mean()
    previous_close = data["close"].shift(1)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    data["atr14"] = true_range.rolling(14).mean()
    data["volume_ma20"] = data["volume"].rolling(20).mean()
    fast, slow, signal = MACD_PARAMETERS
    ema21 = data["close"].ewm(span=fast, adjust=False).mean()
    ema55 = data["close"].ewm(span=slow, adjust=False).mean()
    data["macd"] = ema21 - ema55
    data["macd_signal"] = data["macd"].ewm(span=signal, adjust=False).mean()
    data["macd_hist"] = data["macd"] - data["macd_signal"]
    data["macd_hist_delta"] = data["macd_hist"].diff()
    data["return_1d_pct"] = data["close"].pct_change() * 100.0
    return data


def _regime(close: float, first: Any, second: Any) -> str:
    if pd.isna(first) or pd.isna(second):
        return "UNAVAILABLE"
    low, high = sorted((float(first), float(second)))
    if close >= high:
        return "ABOVE_BOTH"
    if close < low:
        return "BELOW_BOTH"
    return "BETWEEN"


def _pivot_ref(pivot: dict[str, Any]) -> str:
    return "PIVOT:{scale}:{side}:{source_date}:{confirmation_date}".format(**pivot)


def _bar_ref(day: str) -> str:
    return f"BAR:{day}"


def _cycle_ref(cycle: dict[str, Any]) -> str:
    return f"MACD:{cycle['sign']}:{cycle['start']}:{cycle.get('end') or 'FORMING'}"


def causal_pivots_as_of(review_packet: dict[str, Any], as_of: str) -> list[dict[str, Any]]:
    rows = []
    for source in review_packet.get("confirmed_pivots") or []:
        pivot = dict(source)
        if str(pivot.get("confirmation_date") or "") > as_of:
            continue
        if str(pivot.get("source_date") or "") > as_of:
            raise AtomicPacketError("pivot source_date is after as_of")
        pivot["ref"] = _pivot_ref(pivot)
        pivot["scale_semantics"] = PIVOT_SCALE_DISCLOSURE
        rows.append(pivot)
    return sorted(rows, key=lambda row: (str(row["confirmation_date"]), str(row["scale"]), str(row["side"])))


def causal_cycles_as_of(
    review_packet: dict[str, Any],
    visible_frame: pd.DataFrame,
    as_of: str,
) -> list[dict[str, Any]]:
    """Clip cycle facts to what was knowable at ``as_of``.

    A completed MACD sign-run is not knowable on its final same-sign bar.  It is
    confirmed only when the first opposite-sign bar closes.  The legacy source
    records store the endpoint but not that causal confirmation date, so derive
    it from the following run's start and keep the ending run FORMING before
    that date.
    """
    rows: list[dict[str, Any]] = []
    cutoff = pd.Timestamp(as_of)
    sources = sorted(
        (dict(row) for row in (review_packet.get("macd_21_55_55_cycles") or [])),
        key=lambda row: str(row.get("start") or ""),
    )
    for index, source in enumerate(sources):
        start = str(source.get("start") or "")
        if not start or start > as_of:
            continue
        end = source.get("end")
        next_start = (
            str(sources[index + 1].get("start") or "")
            if index + 1 < len(sources)
            else ""
        )
        confirmed_on = str(
            source.get("confirmed_on")
            or source.get("confirmation_date")
            or next_start
            or ""
        )
        causally_completed = bool(
            end is not None
            and str(end) <= as_of
            and confirmed_on
            and confirmed_on <= as_of
        )
        if causally_completed:
            cycle = dict(source)
            cycle["status"] = "CONFIRMED"
            cycle["confirmed_on"] = confirmed_on
        else:
            part = visible_frame[
                (visible_frame["date"] >= pd.Timestamp(start)) & (visible_frame["date"] <= cutoff)
            ]
            if part.empty:
                continue
            low_index = part["low"].astype(float).idxmin()
            high_index = part["high"].astype(float).idxmax()
            cycle = {
                "sign": str(source["sign"]),
                "start": start,
                "end": None,
                "status": "FORMING",
                "bars": int(len(part)),
                "low_date": part.loc[low_index, "date"].date().isoformat(),
                "low": _number(part.loc[low_index, "low"], 4),
                "high_date": part.loc[high_index, "date"].date().isoformat(),
                "high": _number(part.loc[high_index, "high"], 4),
                "start_close": _number(part.iloc[0]["close"], 4),
                "last_close": _number(part.iloc[-1]["close"], 4),
                "confirmed_on": None,
            }
        for field in ("start", "end", "low_date", "high_date", "confirmed_on"):
            if cycle.get(field) is not None and str(cycle[field]) > as_of:
                raise AtomicPacketError(f"cycle {field} is after as_of")
        cycle["ref"] = _cycle_ref(cycle)
        rows.append(cycle)
    return rows


def build_daily_objective_states(
    price_frame: pd.DataFrame,
    review_packet: dict[str, Any],
    *,
    monitor_on: str,
    as_of: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Scan every monitored day using ``derive_objective_state`` as authority."""
    data = add_causal_indicators(price_frame)
    visible = data[data["date"] <= pd.Timestamp(as_of)].copy()
    monitored = visible[visible["date"] >= pd.Timestamp(monitor_on)]
    if monitored.empty:
        return visible, []
    timeline = {
        str(day): sorted(set(str(source) for source in sources))
        for day, sources in (review_packet.get("selection_timeline") or {}).items()
        if monitor_on <= str(day) <= as_of
    }
    full_bar_rows = [
        {"date": row["date"].date().isoformat(), "close": float(row["close"])}
        for _, row in visible.iterrows()
    ]
    previous_state: dict[str, Any] | None = None
    previous_signature: str | None = None
    previous_attack_ids: set[str] = set()
    previous_defense_event_keys: set[tuple[str, str, str]] = set()
    previous_cycle_refs: set[str] = set()
    previous_semantic_signature: str | None = None
    semantic_baseline_exists = False
    semantic_dirty_pending = False
    watchlist_active = True
    campaign_generation = 1
    campaign_started_on = monitor_on
    removed_on: str | None = None
    pre_monitor = visible[visible["date"] < pd.Timestamp(monitor_on)]
    if not pre_monitor.empty:
        prior_day = pre_monitor.iloc[-1]["date"].date().isoformat()
        prior_pivots = causal_pivots_as_of(review_packet, prior_day)
        prior_cycles = causal_cycles_as_of(review_packet, pre_monitor, prior_day)
        prior_completed = [cycle for cycle in prior_cycles if cycle.get("status") == "CONFIRMED" and cycle.get("end")]
        previous_state = derive_objective_state(
            as_of=prior_day,
            bars=[bar for bar in full_bar_rows if bar["date"] <= prior_day],
            confirmed_pivots=prior_pivots,
            completed_macd_cycles=prior_completed,
            selection_active=False,
            pivot_definition=PIVOT_SCALE_DISCLOSURE,
        )
        previous_attack_ids = {str(attack["attack_id"]) for attack in previous_state["attacks"]}
        previous_defense_event_keys = {
            (str(event["event"]), str(event["date"]), str(event.get("defense_id") or event.get("attack_id") or ""))
            for event in previous_state["defense_events"]
        }
        previous_cycle_refs = {str(cycle["ref"]) for cycle in prior_completed}
        previous_semantic_signature = canonical_sha256(
            {
                "dow": previous_state["dow"],
                "controls": previous_state["controls"],
                "left_right": previous_state["left_right"],
                "active_defenses": sorted(
                    row["defense_id"] for row in previous_state["defenses"] if row["status"] == "ACTIVE"
                ),
                "hypotheses": [row["hypothesis_id"] for row in previous_state["candidate_hypotheses"]],
            }
        )
    days: list[dict[str, Any]] = []
    for monitored_ordinal, (index, row) in enumerate(monitored.iterrows()):
        day = row["date"].date().isoformat()
        close = float(row["close"])
        selected_sources = timeline.get(day, [])
        reselected_today = bool(not watchlist_active and selected_sources)
        if reselected_today:
            watchlist_active = True
            campaign_generation += 1
            campaign_started_on = day
            removed_on = None
        long_regime = _regime(close, row.get("ma105"), row.get("ma144"))
        working_regime = _regime(close, row.get("ma21"), row.get("ma55"))
        causal_pivots = causal_pivots_as_of(review_packet, day)
        prefix = visible[visible["date"] <= pd.Timestamp(day)]
        cycles = causal_cycles_as_of(review_packet, prefix, day)
        completed_cycles = [cycle for cycle in cycles if cycle.get("status") == "CONFIRMED" and cycle.get("end")]
        objective = derive_objective_state(
            as_of=day,
            bars=[bar for bar in full_bar_rows if bar["date"] <= day],
            confirmed_pivots=causal_pivots,
            completed_macd_cycles=completed_cycles,
            selection_active=watchlist_active,
            pivot_definition=PIVOT_SCALE_DISCLOSURE,
        )
        events: list[str] = []
        if monitored_ordinal == 0:
            events.append("INITIAL_SELECTION")
        elif reselected_today:
            events.append("RESELECTION_AFTER_REMOVAL")
        elif selected_sources:
            events.append("UPSTREAM_SELECTION_REFRESH")
        if any(pivot["confirmation_date"] == day for pivot in causal_pivots):
            events.append("PIVOT_CONFIRMED")
        current_attack_ids = {str(attack["attack_id"]) for attack in objective["attacks"]}
        new_attacks = [attack for attack in objective["attacks"] if attack["attack_id"] not in previous_attack_ids]
        for attack in new_attacks:
            events.append(f"{attack['scale']}_{attack['direction']}_ATTACK_CONFIRMED")
        defense_by_id = {str(defense["defense_id"]): defense for defense in objective["defenses"]}
        current_defense_event_keys = {
            (str(event["event"]), str(event["date"]), str(event.get("defense_id") or event.get("attack_id") or ""))
            for event in objective["defense_events"]
        }
        new_defense_events = [
            event for event in objective["defense_events"]
            if (str(event["event"]), str(event["date"]), str(event.get("defense_id") or event.get("attack_id") or ""))
            not in previous_defense_event_keys
        ]
        for event in new_defense_events:
            if event["event"] == "DEFENSE_BREACHED":
                defense = defense_by_id[str(event["defense_id"])]
                events.append(f"{defense['scale']}_{defense['side']}_DEFENSE_BREACHED")
            elif event["event"] == "DEFENSE_ESTABLISHED":
                defense = defense_by_id[str(event["defense_id"])]
                events.append(f"{defense['scale']}_{defense['side']}_DEFENSE_ESTABLISHED")
        current_cycle_refs = {str(cycle["ref"]) for cycle in completed_cycles}
        if current_cycle_refs - previous_cycle_refs:
            events.append("MACD_CYCLE_COMPLETED")
        structural = {
            "dow": {key: value["dow_state"] for key, value in objective["dow"].items()},
            "controls": {key: value["state"] for key, value in objective["controls"].items()},
            "scale_relationship": objective["scale_relationship"],
            "left_right_phase": objective["left_right"]["phase"],
            "active_defenses": sorted(
                defense["defense_id"] for defense in objective["defenses"] if defense["status"] == "ACTIVE"
            ),
            "hypotheses": [row["hypothesis_id"] for row in objective["candidate_hypotheses"]],
            "long_regime": long_regime,
            "working_regime": working_regime,
        }
        signature = canonical_sha256(structural)
        if previous_state is not None and objective["left_right"]["phase"] != previous_state["left_right"]["phase"]:
            events.append("LEFT_RIGHT_PHASE_CHANGE")
        if previous_signature is not None and signature != previous_signature:
            events.append("OBJECTIVE_STATE_CHANGE")
        events = list(dict.fromkeys(events))
        large_bull_breach = "LARGE_BULLISH_DEFENSE_BREACHED" in events
        static_review_events = [event for event in events if event != "UPSTREAM_SELECTION_REFRESH"]
        semantic_signature = canonical_sha256(
            {
                "dow": objective["dow"],
                "controls": objective["controls"],
                "left_right": objective["left_right"],
                "active_defenses": sorted(
                    defense["defense_id"] for defense in objective["defenses"] if defense["status"] == "ACTIVE"
                ),
                "hypotheses": [item["hypothesis_id"] for item in objective["candidate_hypotheses"]],
            }
        )
        new_up_attack = any(
            attack["confirmed_on"] == day and attack["direction"] == "UP"
            for attack in objective["attacks"]
        )
        causal_small_bull_stop = any(
            defense["status"] == "ACTIVE"
            and defense["side"] == "BULLISH"
            and defense["scale"] == "SMALL"
            and float(defense["price"]) < close
            for defense in objective["defenses"]
        )
        atr_ready = not pd.isna(row.get("atr14")) and float(row["atr14"]) > 0
        program_trade_frontier = bool(
            watchlist_active
            and len(prefix) >= INDICATOR_WARMUP_BARS
            and new_up_attack
            and causal_small_bull_stop
            and atr_ready
        )
        causal_semantic_event = any(
            event.endswith("_ATTACK_CONFIRMED")
            or "_DEFENSE_ESTABLISHED" in event
            or "_DEFENSE_BREACHED" in event
            or event == "LEFT_RIGHT_PHASE_CHANGE"
            for event in events
        )
        semantic_dirty_event = bool(
            semantic_baseline_exists
            and previous_semantic_signature is not None
            and semantic_signature != previous_semantic_signature
            and causal_semantic_event
        )
        if semantic_dirty_event:
            semantic_dirty_pending = True
        remove_frontier = bool(watchlist_active and large_bull_breach)
        lazy_reasons = [
            reason
            for condition, reason in (
                (program_trade_frontier, "PROGRAM_TRADE_FRONTIER"),
                (remove_frontier, "CAMPAIGN_DEFENSE_REMOVE_BOUNDARY"),
            )
            if condition
        ]
        review_required = bool(lazy_reasons)
        semantic_dirty_consumed = bool(review_required and semantic_dirty_pending)
        if review_required:
            semantic_baseline_exists = True
            semantic_dirty_pending = False
        if review_required:
            skip_reason = None
        elif not watchlist_active:
            skip_reason = "WATCHLIST_INACTIVE_AWAIT_RESELECTION"
        elif len(prefix) < INDICATOR_WARMUP_BARS:
            skip_reason = "INSUFFICIENT_HISTORY_PROGRAM_GUARD"
        elif new_up_attack and not causal_small_bull_stop:
            skip_reason = "NO_CAUSAL_EPISODE_OR_PHASE_STOP_GUARD"
        elif static_review_events:
            skip_reason = "OBJECTIVE_CHANGE_CANNOT_CHANGE_PERMISSION"
        else:
            skip_reason = "NO_PERMISSION_RELEVANT_CHANGE"
        days.append(
            {
                "as_of": day,
                "source_row_ordinal": int(index),
                "monitored_ordinal": monitored_ordinal,
                "events": events,
                "review_required_before_lazy": bool(static_review_events),
                "review_required": review_required,
                "lifecycle_conditional_review": bool(
                    "UPSTREAM_SELECTION_REFRESH" in events or reselected_today
                ),
                "lazy_review_reasons": lazy_reasons,
                "skip_reason": skip_reason,
                "program_trade_frontier": program_trade_frontier,
                "semantic_dirty": semantic_dirty_event,
                "semantic_dirty_carried_to_next_boundary": semantic_dirty_pending,
                "semantic_dirty_consumed_at_boundary": semantic_dirty_consumed,
                "remove_frontier": remove_frontier,
                "active_watchlist": watchlist_active,
                "watchlist_state": (
                    "REMOVE_BOUNDARY" if remove_frontier
                    else "RESELECTED_WATCHING" if reselected_today
                    else "WATCHING" if watchlist_active
                    else "REMOVED_AWAIT_RESELECTION"
                ),
                "campaign_generation": campaign_generation,
                "campaign_started_on": campaign_started_on,
                "prior_removed_on": removed_on,
                "long_regime": long_regime,
                "working_regime": working_regime,
                "below_macro_defense": large_bull_breach,
                "selected_today": selected_sources,
                "objective_signature_sha256": signature,
                "objective_state": objective,
            }
        )
        previous_state = objective
        previous_signature = signature
        previous_attack_ids = current_attack_ids
        previous_defense_event_keys = current_defense_event_keys
        previous_cycle_refs = current_cycle_refs
        previous_semantic_signature = semantic_signature
        if remove_frontier:
            watchlist_active = False
            removed_on = day
    return visible, days


def build_required_question_manifest(
    schema_metadata: dict[str, Any],
    candidates: dict[str, Any],
    scenario_hypotheses: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Derive subject-specific atoms solely from frozen gate dependencies."""
    groups = schema_metadata.get("x-question-groups") or {}
    required_groups = {"anchor", "relation", "stop", "global"}
    if set(groups) != required_groups:
        raise AtomicPacketError(f"schema x-question-groups must equal {sorted(required_groups)}")
    normalized: dict[str, list[str]] = {}
    for group_name in sorted(required_groups):
        identifiers = [str(value) for value in groups[group_name]]
        if not identifiers or len(identifiers) != len(set(identifiers)):
            raise AtomicPacketError(f"schema question group {group_name} is empty or duplicated")
        normalized[group_name] = identifiers
    anchor_refs = [str(row["candidate_id"]) for row in candidates["anchor_candidates"]]
    relation_refs = [str(row["candidate_id"]) for row in candidates["relation_candidates"]]
    stop_refs = [str(row["candidate_id"]) for row in candidates["stop_candidates"]]
    if len(anchor_refs) > MAX_ANCHOR_CANDIDATES or len(relation_refs) > MAX_RELATION_CANDIDATES:
        raise AtomicPacketError("candidate enumeration exceeds semantic schema limits")
    if len(stop_refs) > MAX_STOP_CANDIDATES:
        raise AtomicPacketError("stop enumeration exceeds semantic schema limits")
    if scenario_hypotheses is None:
        return {
            "anchor_candidates": [
                {"subject_ref": ref, "required_question_ids": normalized["anchor"]} for ref in anchor_refs
            ],
            "relation_candidates": [
                {"subject_ref": ref, "required_question_ids": normalized["relation"]} for ref in relation_refs
            ],
            "stop_candidates": [
                {"subject_ref": ref, "required_question_ids": normalized["stop"]} for ref in stop_refs
            ],
            "global_question_ids": normalized["global"],
        }

    question_group = {
        question_id: group for group, question_ids in normalized.items() for question_id in question_ids
    }
    dependencies = schema_metadata.get("x-program-atomic-dependencies") or {}
    matrix = schema_metadata.get("x-four-scenario-gate-matrix") or {}
    common = schema_metadata.get("x-v3-common-hard-guards") or {}
    required: dict[str, dict[str, set[str]]] = {"anchor": {}, "relation": {}, "stop": {}}
    global_ids: set[str] = set()

    def add(group: str, subject: Any, question: str) -> None:
        if group == "global":
            global_ids.add(question)
        elif isinstance(subject, str) and subject not in {"", "UNKNOWN", "UNRESOLVED"}:
            required[group].setdefault(subject, set()).add(question)

    for scenario in SCENARIOS:
        sources = [source for values in (matrix.get(scenario) or {}).values() for source in values]
        sources.extend(source for values in common.values() for source in values)
        expanded: list[str] = []
        for source in sources:
            if source.startswith("PROGRAM:"):
                expanded.extend(dependencies.get(source.split(":", 1)[1]) or [])
            else:
                expanded.append(source)
        for hypothesis in scenario_hypotheses.get(scenario) or []:
            for question in expanded:
                group = question_group.get(question)
                if group == "anchor":
                    add(group, hypothesis.get("anchor_ref"), question)
                elif group == "relation":
                    add(group, hypothesis.get("relation_ref"), question)
                elif group == "stop":
                    subject = (
                        hypothesis.get("episode_stop_ref")
                        if question == "STOP_BELONGS_TO_CURRENT_EPISODE"
                        else hypothesis.get("campaign_stop_ref")
                    )
                    add(group, subject, question)
                elif group == "global":
                    add(group, None, question)

    output: dict[str, Any] = {}
    for group, key in (
        ("anchor", "anchor_candidates"),
        ("relation", "relation_candidates"),
        ("stop", "stop_candidates"),
    ):
        output[key] = [
            {
                "subject_ref": subject,
                "required_question_ids": [
                    question for question in normalized[group] if question in required[group][subject]
                ],
            }
            for subject in sorted(required[group])
        ]
    output["global_question_ids"] = [
        question for question in normalized["global"] if question in global_ids
    ]
    return output


def _visible_bar_evidence(
    visible: pd.DataFrame,
    *,
    required_dates: set[str],
    recent_window: int = AI_VISIBLE_RECENT_BAR_WINDOW_DRAFT,
) -> list[dict[str, Any]]:
    rows = []
    recent_dates = {
        value.date().isoformat()
        for value in visible["date"].iloc[-recent_window:]
    }
    keep_dates = recent_dates | required_dates
    for _, row in visible.iterrows():
        day = row["date"].date().isoformat()
        if day not in keep_dates:
            continue
        values = {
            "open": _number(row["open"]),
            "high": _number(row["high"]),
            "low": _number(row["low"]),
            "close": _number(row["close"]),
            "volume": _number(row["volume"], 0),
            "return_1d_pct": _number(row["return_1d_pct"], 3),
            "volume_ratio_20": _number(row["volume"] / row["volume_ma20"], 3)
            if not pd.isna(row["volume_ma20"]) and float(row["volume_ma20"]) else None,
            "atr14": _number(row["atr14"]),
            "macd_hist": _number(row["macd_hist"]),
            "macd_hist_delta": _number(row["macd_hist_delta"]),
        }
        values.update({f"ma{window}": _number(row[f"ma{window}"]) for window in MA_WINDOWS})
        rows.append({"ref": _bar_ref(day), "kind": "BAR", "date": day, "values": values})
    return rows


def _full_history_input_sha256(visible: pd.DataFrame) -> str:
    rows = [
        {
            "date": row["date"].date().isoformat(),
            "open": _number(row["open"]),
            "high": _number(row["high"]),
            "low": _number(row["low"]),
            "close": _number(row["close"]),
            "volume": _number(row["volume"], 0),
        }
        for _, row in visible.iterrows()
    ]
    return canonical_sha256(rows)


def _bounded_pivots(pivots: Sequence[dict[str, Any]], per_group: int = 6) -> list[dict[str, Any]]:
    rows = []
    for scale in ("LARGE", "SMALL"):
        for side in ("LOW", "HIGH"):
            group = [row for row in pivots if row["scale"] == scale and row["side"] == side]
            rows.extend(group[-per_group:])
    return sorted(rows, key=lambda row: (str(row["confirmation_date"]), str(row["scale"]), str(row["side"])))


def _comparison_leg(
    *,
    start: dict[str, Any],
    end: dict[str, Any],
    visible: pd.DataFrame,
) -> dict[str, Any]:
    start_date = str(start["source_date"])
    end_date = str(end["source_date"])
    part = visible[
        (visible["date"] >= pd.Timestamp(start_date))
        & (visible["date"] <= pd.Timestamp(end_date))
    ]
    if part.empty:
        raise AtomicPacketError("comparison-window endpoints are outside visible price history")
    start_price = float(start["price"])
    end_price = float(end["price"])
    price_change_pct = (end_price / start_price - 1.0) * 100.0 if start_price else None
    close_returns = part["close"].astype(float).pct_change().dropna() * 100.0
    true_range_pct = (
        (part["high"].astype(float) - part["low"].astype(float))
        / part["close"].astype(float).replace(0, float("nan"))
        * 100.0
    )
    return {
        "start_date": start_date,
        "end_date": end_date,
        "start_ref": str(start["ref"]),
        "end_ref": str(end["ref"]),
        "start_price": _number(start_price),
        "end_price": _number(end_price),
        "bar_count": int(len(part)),
        "price_change_pct": _number(price_change_pct, 4),
        "slope_pct_per_bar": _number(
            None if price_change_pct is None else price_change_pct / max(1, len(part) - 1), 6
        ),
        "mean_true_range_pct": _number(true_range_pct.mean(), 4),
        "realized_close_volatility_pct": _number(
            close_returns.std(ddof=0) if not close_returns.empty else 0.0, 4
        ),
    }


def build_working_comparison_windows(
    *,
    objective: dict[str, Any],
    confirmed_pivots: Sequence[dict[str, Any]],
    visible: pd.DataFrame,
    as_of: str,
) -> dict[str, dict[str, Any] | None]:
    """Choose exactly one causal baseline/current pair for each objective scale."""
    output: dict[str, dict[str, Any] | None] = {"LARGE": None, "SMALL": None}
    for scale in ("LARGE", "SMALL"):
        pivots = sorted(
            (row for row in confirmed_pivots if row["scale"] == scale),
            key=lambda row: (str(row["source_date"]), str(row["confirmation_date"]), str(row["ref"])),
        )
        legs: list[dict[str, Any]] = []
        for start, end in zip(pivots, pivots[1:]):
            if start["side"] == end["side"] or str(end["source_date"]) > as_of:
                continue
            legs.append(
                {
                    "direction": "UP" if start["side"] == "LOW" else "DOWN",
                    "start": start,
                    "end": end,
                    "available_on": max(str(start["confirmation_date"]), str(end["confirmation_date"])),
                }
            )
        control_state = str((objective.get("controls", {}).get(scale.lower()) or {}).get("state") or "")
        direction = {"UP_CONTROL": "UP", "DOWN_CONTROL": "DOWN"}.get(control_state)
        if direction is None and legs:
            direction = str(legs[-1]["direction"])
        comparable = [row for row in legs if row["direction"] == direction]
        if len(comparable) < 2:
            continue
        baseline, current = comparable[-2:]
        available_on = max(str(baseline["available_on"]), str(current["available_on"]))
        if available_on > as_of:
            raise AtomicPacketError("comparison window is not causally available at as_of")
        output[scale] = {
            "scale": scale,
            "direction": direction,
            "selection_policy": "LATEST_TWO_COMPLETED_SAME_DIRECTION_LEGACY_PIVOT_LEGS",
            "baseline": _comparison_leg(start=baseline["start"], end=baseline["end"], visible=visible),
            "current": _comparison_leg(start=current["start"], end=current["end"], visible=visible),
            "baseline_available_on": str(baseline["available_on"]),
            "current_available_on": str(current["available_on"]),
            "available_on": available_on,
        }
    return output


def _comparison_window_evidence(
    windows: dict[str, dict[str, Any] | None],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scale in ("LARGE", "SMALL"):
        window = windows[scale]
        if window is None:
            continue
        rows.append(
            {
                "ref": f"COMPARISON_WINDOW:{scale}:{canonical_sha256(window)[:16]}",
                "kind": "CAUSAL_COMPARISON_WINDOW",
                "date": str(window["available_on"]),
                "values": window,
            }
        )
    return rows


def _anchor_candidates(
    objective: dict[str, Any],
    all_pivots: Sequence[dict[str, Any]],
    completed_cycles: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build full campaign ordinals first, then bound AI-visible candidates."""
    pivot_by_ref = {str(row["ref"]): row for row in all_pivots}
    boundaries: dict[str, list[tuple[str, str]]] = {"UP": [], "DOWN": []}
    for defense in objective.get("defenses") or []:
        direction = "UP" if defense["side"] == "BULLISH" else "DOWN"
        origin = pivot_by_ref.get(str(defense["source_pivot_ref"]))
        if origin is None:
            continue
        item = (str(origin["source_date"]), str(defense["campaign_id"]))
        if item not in boundaries[direction]:
            boundaries[direction].append(item)
    for direction in boundaries:
        boundaries[direction].sort()

    def campaign_for(direction: str, start_date: str) -> str:
        eligible = [campaign for day, campaign in boundaries[direction] if day <= start_date]
        return eligible[-1] if eligible else f"PRE_CAUSAL_{direction}"

    full: list[dict[str, Any]] = []
    for scale in ("SMALL", "LARGE"):
        stream = sorted(
            (row for row in all_pivots if row["scale"] == scale),
            key=lambda row: (str(row["source_date"]), str(row["confirmation_date"]), str(row["ref"])),
        )
        for left, right in zip(stream, stream[1:]):
            if left["side"] == right["side"]:
                continue
            direction = "UP" if left["side"] == "LOW" else "DOWN"
            raw = {
                "hypothesis_type": "PIVOT_LEG",
                "scale": scale,
                "direction": direction,
                "start_ref": left["ref"],
                "end_ref": right["ref"],
                "start_date": str(left["source_date"]),
                "end_date": str(right["source_date"]),
                "available_on": max(str(left["confirmation_date"]), str(right["confirmation_date"])),
                "amplitude": abs(float(right["price"]) - float(left["price"])),
                "macd_support_only": False,
                "campaign_id": campaign_for(direction, str(left["source_date"])),
            }
            raw["objective_hypothesis_id"] = "H-" + canonical_sha256(raw)[:20]
            full.append(raw)
    for cycle in completed_cycles:
        if cycle.get("status") != "CONFIRMED" or not cycle.get("end"):
            continue
        direction = "UP" if str(cycle["sign"]).upper() == "POSITIVE" else "DOWN"
        start_ref = _bar_ref(str(cycle["low_date"] if direction == "UP" else cycle["high_date"]))
        end_ref = _bar_ref(str(cycle["high_date"] if direction == "UP" else cycle["low_date"]))
        raw = {
            "hypothesis_type": "MACD_SKELETON",
            "scale": "UNASSIGNED",
            "direction": direction,
            "start_ref": start_ref,
            "end_ref": end_ref,
            "start_date": str(cycle["start"]),
            "end_date": str(cycle["end"]),
            "available_on": str(cycle["end"]),
            "amplitude": abs(float(cycle["high"]) - float(cycle["low"])),
            "macd_support_only": True,
            "campaign_id": campaign_for(direction, str(cycle["start"])),
        }
        raw["objective_hypothesis_id"] = "H-" + canonical_sha256(raw)[:20]
        full.append(raw)

    full.sort(key=lambda row: (str(row["available_on"]), str(row["objective_hypothesis_id"])))
    counters: Counter[tuple[str, str, str]] = Counter()
    for row in full:
        key = (str(row["scale"]), str(row["campaign_id"]), str(row["direction"]))
        counters[key] += 1
        number = counters[key]
        row["same_direction_attack_number"] = number
        row["taiji_generation"] = {
            1: "ANCHOR_LEG_1", 2: "COPY_LEG_3", 3: "COPY_LEG_5",
        }.get(number, "LATER_GENERATION")
        row["completed_prior_copy_count"] = max(0, number - 2)

    source: list[dict[str, Any]] = []
    for hypothesis_type, scale, limit in (
        ("PIVOT_LEG", "SMALL", 4),
        ("PIVOT_LEG", "LARGE", 4),
        ("MACD_SKELETON", "UNASSIGNED", 2),
    ):
        group = [
            row for row in full
            if row.get("hypothesis_type") == hypothesis_type and row.get("scale") == scale
        ]
        source.extend(group[-limit:])
    source = sorted(source, key=lambda row: (str(row["available_on"]), str(row["objective_hypothesis_id"])))
    rows: list[dict[str, Any]] = []
    for hypothesis in source:
        hypothesis_id = str(hypothesis["objective_hypothesis_id"])
        rows.append(
            {
                "candidate_id": "ANCHOR_CANDIDATE:A-" + canonical_sha256(hypothesis_id)[:16],
                "objective_hypothesis_id": hypothesis_id,
                "hypothesis_type": str(hypothesis["hypothesis_type"]),
                "scale": str(hypothesis["scale"]),
                "direction_hint": str(hypothesis["direction"]),
                "start_ref": str(hypothesis["start_ref"]),
                "end_ref": str(hypothesis["end_ref"]),
                "available_on": str(hypothesis["available_on"]),
                "amplitude": _number(hypothesis.get("amplitude")),
                "macd_support_only": bool(hypothesis.get("macd_support_only", False)),
                "campaign_id": str(hypothesis["campaign_id"]),
                "taiji_generation": str(hypothesis["taiji_generation"]),
                "same_direction_attack_number": int(hypothesis["same_direction_attack_number"]),
                "completed_prior_copy_count": int(hypothesis["completed_prior_copy_count"]),
                "pivot_definition": PIVOT_SCALE_DISCLOSURE,
                "asserted_valid_anchor": False,
            }
        )
    return rows


def _relation_candidates(
    anchors: Sequence[dict[str, Any]], objective: dict[str, Any], as_of: str
) -> list[dict[str, Any]]:
    """Build same-stream parent/correction/current-leg hypotheses.

    Cross-scale adjacency is never treated as a causal relation.  A completed
    replication requires three time-ordered alternating legs; otherwise the
    current leg is explicitly unresolved CURRENT_CONTEXT.
    """
    rows: list[dict[str, Any]] = []
    up_trigger_today = any(
        attack["direction"] == "UP" and attack["confirmed_on"] == as_of
        for attack in objective.get("attacks") or []
    )
    streams: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for anchor in anchors:
        streams.setdefault((anchor["hypothesis_type"], anchor["scale"]), []).append(anchor)
    for stream in streams.values():
        stream = sorted(stream, key=lambda row: (str(row["available_on"]), str(row["candidate_id"])))
        for index in range(2, len(stream)):
            parent, correction, current = stream[index - 2:index + 1]
            if not (
                parent["direction_hint"] == current["direction_hint"]
                and correction["direction_hint"] != parent["direction_hint"]
                and parent["campaign_id"] == current["campaign_id"]
            ):
                continue
            values = {
                "parent_anchor_ref": parent["candidate_id"],
                "correction_anchor_ref": correction["candidate_id"],
                "current_leg_ref": current["candidate_id"],
                "parent_scale": parent["scale"],
                "correction_scale": correction["scale"],
                "current_scale": current["scale"],
                "parent_available_on": parent["available_on"],
                "correction_available_on": correction["available_on"],
                "current_available_on": current["available_on"],
                "available_on": current["available_on"],
                "parent_direction": parent["direction_hint"],
                "correction_direction": correction["direction_hint"],
                "current_direction": current["direction_hint"],
                "campaign_id": current["campaign_id"],
                "taiji_generation": current["taiji_generation"],
                "same_direction_attack_number": current["same_direction_attack_number"],
                "completed_prior_copy_count": current["completed_prior_copy_count"],
                "relation_state": "COMPLETED_ALTERNATING_TRIPLE",
                "enumeration_policy": HYPOTHESIS_ENUMERATION_POLICY,
            }
            rows.append(
                {
                    "candidate_id": "RELATION_CANDIDATE:R-" + canonical_sha256(values)[:16],
                    **values,
                }
            )
        if stream:
            parent = stream[-1]
            correction = None
            current_anchor_completed_now = bool(
                up_trigger_today
                and stream[-1]["direction_hint"] == "UP"
                and stream[-1]["available_on"] == as_of
            )
            if (
                not current_anchor_completed_now
                and len(stream) >= 2
                and stream[-2]["direction_hint"] != stream[-1]["direction_hint"]
            ):
                parent, correction = stream[-2], stream[-1]
            current_direction = "UNKNOWN"
            generation = "UNKNOWN"
            current_number = None
            prior_copy_count = None
            current_leg_ref = "CURRENT_CONTEXT"
            if up_trigger_today and parent["direction_hint"] == "UP" and (
                correction is None or correction["direction_hint"] == "DOWN"
            ):
                current_direction = "UP"
                if current_anchor_completed_now:
                    current_number = int(parent["same_direction_attack_number"])
                    current_leg_ref = parent["candidate_id"]
                else:
                    current_number = int(parent["same_direction_attack_number"]) + 1
                generation = {
                    1: "ANCHOR_LEG_1", 2: "COPY_LEG_3", 3: "COPY_LEG_5",
                }.get(current_number, "LATER_GENERATION")
                prior_copy_count = max(0, current_number - 2)
            values = {
                "parent_anchor_ref": parent["candidate_id"],
                "correction_anchor_ref": None if correction is None else correction["candidate_id"],
                "current_leg_ref": current_leg_ref,
                "parent_scale": parent["scale"],
                "correction_scale": None if correction is None else correction["scale"],
                "current_scale": parent["scale"],
                "parent_available_on": parent["available_on"],
                "correction_available_on": None if correction is None else correction["available_on"],
                "current_available_on": as_of if current_direction == "UP" else None,
                "available_on": (correction or parent)["available_on"],
                "parent_direction": parent["direction_hint"],
                "correction_direction": None if correction is None else correction["direction_hint"],
                "current_direction": current_direction,
                "taiji_generation": generation,
                "same_direction_attack_number": current_number,
                "completed_prior_copy_count": prior_copy_count,
                "relation_state": "CURRENT_CONTEXT_UNRESOLVED",
                "campaign_id": parent["campaign_id"],
                "enumeration_policy": HYPOTHESIS_ENUMERATION_POLICY,
            }
            rows.append(
                {
                    "candidate_id": "RELATION_CANDIDATE:CURRENT_CONTEXT-" + canonical_sha256(values)[:12],
                    **values,
                }
            )
    if not rows:
        rows.append(
            {
                "candidate_id": "RELATION_CANDIDATE:CURRENT_CONTEXT",
                "parent_anchor_ref": None,
                "correction_anchor_ref": None,
                "current_leg_ref": "CURRENT_CONTEXT",
                "parent_scale": None,
                "correction_scale": None,
                "current_scale": None,
                "parent_available_on": None,
                "correction_available_on": None,
                "current_available_on": None,
                "available_on": None,
                "parent_direction": "UNKNOWN",
                "correction_direction": "UNKNOWN",
                "current_direction": "UNKNOWN",
                "taiji_generation": "UNKNOWN",
                "same_direction_attack_number": None,
                "completed_prior_copy_count": None,
                "relation_state": "CURRENT_CONTEXT_WITHOUT_CAUSAL_CHAIN",
                "enumeration_policy": HYPOTHESIS_ENUMERATION_POLICY,
            }
        )
    ordered = sorted(rows, key=lambda row: (str(row.get("available_on") or ""), str(row["candidate_id"])))
    return ordered[-MAX_RELATION_CANDIDATES:]


def _stop_candidates(objective: dict[str, Any], close: float) -> list[dict[str, Any]]:
    """Only causal active defenses may become stop candidates."""
    rows = []
    defenses = sorted(
        (row for row in objective.get("defenses") or [] if row.get("status") == "ACTIVE"),
        key=lambda row: (str(row["established_on"]), str(row["defense_id"])),
    )
    for defense in defenses:
        price = float(defense["price"])
        if defense["side"] == "BULLISH" and price >= close:
            continue
        if defense["side"] == "BEARISH" and price <= close:
            continue
        rows.append(
            {
                "candidate_id": "STOP_CANDIDATE:S-" + canonical_sha256(defense["defense_id"])[:16],
                "defense_ref": str(defense["defense_id"]),
                "source_pivot_ref": str(defense["source_pivot_ref"]),
                "control_pivot_ref": str(defense["control_pivot_ref"]),
                "campaign_id": str(defense["campaign_id"]),
                "scale": str(defense["scale"]),
                "side": str(defense["side"]),
                "established_on": str(defense["established_on"]),
                "price": _number(price),
                "distance_pct": _number(abs(close - price) / close * 100.0, 4),
                "scale_semantics": PIVOT_SCALE_DISCLOSURE,
            }
        )
    return rows[-MAX_STOP_CANDIDATES:]


def _latest_defense(objective: dict[str, Any], scale: str, side: str) -> dict[str, Any] | None:
    rows = [
        row for row in objective.get("defenses") or []
        if row.get("scale") == scale and row.get("side") == side
    ]
    return max(rows, key=lambda row: (str(row["established_on"]), str(row["defense_id"]))) if rows else None


def _taiji_state(objective: dict[str, Any]) -> tuple[str, int | None, int | None]:
    attacks = sorted(
        objective.get("attacks") or [],
        key=lambda row: (str(row["confirmed_on"]), str(row["attack_id"])),
    )
    if not attacks:
        return "UNKNOWN", None, None
    direction = str(attacks[-1]["direction"])
    current_run: list[dict[str, Any]] = []
    for attack in reversed(attacks):
        if attack.get("intrabar_order_unknown") or str(attack["direction"]) != direction:
            break
        current_run.append(attack)
    count = len({str(row["control_pivot_ref"]) for row in current_run})
    if direction != "UP" or count < 1:
        return "UNKNOWN", count or None, None
    generation = {1: "ANCHOR_LEG_1", 2: "COPY_LEG_3", 3: "COPY_LEG_5"}.get(count, "LATER_GENERATION")
    return generation, count, max(0, count - 2)


def _scenario_hypotheses(
    candidates: dict[str, Any],
    *,
    position_role: str,
    objective: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Enumerate every graph-linked subject set; do not choose a winner."""
    anchors = {row["candidate_id"]: row for row in candidates["anchor_candidates"]}
    relations = candidates["relation_candidates"]
    bullish_small = [
        row["candidate_id"] for row in candidates["stop_candidates"]
        if row["side"] == "BULLISH" and row["scale"] == "SMALL"
    ]
    bullish_large = [
        row["candidate_id"] for row in candidates["stop_candidates"]
        if row["side"] == "BULLISH" and row["scale"] == "LARGE"
    ]
    episode_ref = bullish_small[-1] if bullish_small else None
    campaign_ref = bullish_large[-1] if bullish_large else None
    result = {scenario: [] for scenario in SCENARIOS}
    for scenario in SCENARIOS:
        for relation in relations:
            parent_direction = relation.get("parent_direction")
            correction_direction = relation.get("correction_direction")
            current_direction = relation.get("current_direction")
            prior_copy_count = relation.get("completed_prior_copy_count")
            up_chain = (
                parent_direction == "UP"
                and correction_direction in {"DOWN", None}
                and current_direction in {"UP", "UNKNOWN"}
            )
            bear_context = bool(
                objective["dow"]["large"]["dow_state"] == "BEAR"
                or objective["left_right"]["phase"] != "NONE"
            )
            possible = {
                "BEAR_REVERSAL_LEFT_RIGHT": bear_context and parent_direction == "DOWN",
                "MACRO_COPY_RESONANCE": up_chain and prior_copy_count in {0, None},
                "MATURE_TREND_PULLBACK": up_chain and (
                    prior_copy_count is None or (isinstance(prior_copy_count, int) and prior_copy_count >= 1)
                ),
                "FRESH_Q1_EXPANSION": (
                    parent_direction == "UP"
                    and current_direction in {"UP", "UNKNOWN"}
                    and prior_copy_count in {0, None}
                ),
            }[scenario]
            if not possible:
                continue
            parent_ref = relation.get("parent_anchor_ref")
            current_ref = relation.get("current_leg_ref")
            if scenario == "FRESH_Q1_EXPANSION" and current_ref in anchors:
                anchor_ref = current_ref
            else:
                anchor_ref = parent_ref if parent_ref in anchors else None
            scenario_campaign_ref = (
                episode_ref if scenario in {"BEAR_REVERSAL_LEFT_RIGHT", "FRESH_Q1_EXPANSION"}
                else campaign_ref
            )
            values = {
                "scenario": scenario,
                "anchor_ref": anchor_ref,
                "relation_ref": relation["candidate_id"],
                "episode_stop_ref": episode_ref,
                "campaign_stop_ref": scenario_campaign_ref,
                "position_role": position_role,
                "taiji_generation": relation["taiji_generation"],
                "same_direction_attack_number": relation["same_direction_attack_number"],
                "completed_prior_copy_count": relation["completed_prior_copy_count"],
            }
            result[scenario].append(
                {
                    "hypothesis_id": "SH-" + canonical_sha256(values)[:20],
                    **{key: value for key, value in values.items() if key != "scenario"},
                }
            )
    return result


def _objective_facts(
    *,
    as_of: str,
    visible: pd.DataFrame,
    daily_state: dict[str, Any],
    candidates: dict[str, Any],
    working_comparison_windows: dict[str, dict[str, Any] | None],
) -> dict[str, Any]:
    events = set(daily_state["events"])
    objective = daily_state["objective_state"]
    close = float(visible.iloc[-1]["close"])
    atr = _number(visible.iloc[-1]["atr14"])
    stops = candidates["stop_candidates"]
    large_bull = _latest_defense(objective, "LARGE", "BULLISH")
    large_bear = _latest_defense(objective, "LARGE", "BEARISH")
    small_bear = _latest_defense(objective, "SMALL", "BEARISH")
    active_small_bull_stops = [
        row for row in stops if row["side"] == "BULLISH" and row["scale"] == "SMALL"
    ]
    up_attacks_today = [
        row for row in objective.get("attacks") or []
        if row["confirmed_on"] == as_of and row["direction"] == "UP"
    ]
    small_up_today = [row for row in up_attacks_today if row["scale"] == "SMALL"]
    signal_attack = (small_up_today or up_attacks_today)
    signal_ref = str(signal_attack[-1]["attack_id"]) if signal_attack else _bar_ref(as_of)
    large_dow = str(objective["dow"]["large"]["dow_state"])
    small_dow = str(objective["dow"]["small"]["dow_state"])
    small_control = str(objective["controls"]["small"]["state"])
    left_right = str(objective["left_right"]["phase"])
    trigger = bool(up_attacks_today)
    position_role = "MOTHER" if trigger else "UNRESOLVED"
    hypotheses = _scenario_hypotheses(
        candidates,
        position_role=position_role,
        objective=objective,
    )
    actual_visible_bars = int(len(visible))
    data_sufficiency_by_route: dict[str, dict[str, Any]] = {}
    for scenario in SCENARIOS:
        required_visible_bars = (
            INDICATOR_WARMUP_BARS if scenario == "FRESH_Q1_EXPANSION" else PREFERRED_STRUCTURE_BARS
        )
        windows_ready = all(
            working_comparison_windows[scale] is not None for scale in ("LARGE", "SMALL")
        )
        route_objective_ready = bool(windows_ready and hypotheses[scenario])
        if scenario == "FRESH_Q1_EXPANSION":
            route_objective_ready = bool(route_objective_ready and active_small_bull_stops)
        if actual_visible_bars < required_visible_bars:
            status: bool | None = False
            reason_code = "INSUFFICIENT_VISIBLE_BARS"
        elif not route_objective_ready:
            status = False
            reason_code = "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE"
        else:
            status = True
            reason_code = "SUFFICIENT_ROUTE_HISTORY_AND_OBJECTIVE_EVIDENCE"
        data_sufficiency_by_route[scenario] = {
            "status": status,
            "required_visible_bars": required_visible_bars,
            "actual_visible_bars": actual_visible_bars,
            "reason_code": reason_code,
        }
    large_bull_intact: bool | None = None if large_bull is None else large_bull["status"] == "ACTIVE"
    large_bear_broken: bool | None = None if large_bear is None else large_bear["status"] == "BREACHED"
    phase_stop = any(row["scale"] == "SMALL" and row["side"] == "BULLISH" for row in stops)
    return {
        "active_watchlist": bool(daily_state.get("active_watchlist")),
        "data_sufficiency_by_route": data_sufficiency_by_route,
        "working_comparison_windows": working_comparison_windows,
        "trigger_completed": trigger,
        "risk_executable": bool(active_small_bull_stops and atr and atr > 0),
        "macro_defense_alert": bool(daily_state.get("remove_frontier")),
        "parent_campaign_invalidated": bool(daily_state.get("remove_frontier")),
        "large_dow_state": large_dow,
        "small_dow_state": small_dow,
        "bear_reversal_context": bool(large_dow == "BEAR" or left_right not in {"NONE"}),
        "large_bear_defense_broken": large_bear_broken,
        "small_bull_control": small_control == "UP_CONTROL",
        "first_retest_after_large_break_held": True if left_right in {"RL", "RR"} else None,
        "rr_break_completed": left_right == "RR",
        "direct_same_clean_impulse": None if left_right == "DIRECT_TO_RIGHT" else False,
        "taiji_generation": "UNKNOWN",
        "same_direction_attack_number": None,
        "completed_prior_copy_count": None,
        "signal_event_ref": signal_ref,
        "position_role": position_role,
        "large_bull_defense_intact": large_bull_intact,
        "correction_bear_dow_line_causal": bool(small_bear),
        "small_up_control_break": bool(small_up_today),
        "stop_causal_fields_valid": bool(active_small_bull_stops),
        "large_bear_dow_defense_causal": bool(large_bear),
        "phase_stop_causal": phase_stop,
        "macd_is_support_only": bool(trigger),
        "fresh_anchor_stop_causal": phase_stop,
        "scenario_hypotheses": hypotheses,
        "scenario_hypothesis_counts": {
            scenario: len(rows) for scenario, rows in hypotheses.items()
        },
        "semantic_dirty_since_prior_review": bool(daily_state["semantic_dirty_consumed_at_boundary"]),
    }


def _candidate_evidence(candidates: dict[str, Any], as_of: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for group, kind in (
        ("anchor_candidates", "ANCHOR_CANDIDATE"),
        ("relation_candidates", "RELATION_CANDIDATE"),
        ("stop_candidates", "STOP_CANDIDATE"),
    ):
        for row in candidates[group]:
            values = {key: value for key, value in row.items() if key != "candidate_id"}
            date_value = (
                values.get("available_on")
                or values.get("established_on")
                or as_of
            )
            output.append(
                {"ref": row["candidate_id"], "kind": kind, "date": str(date_value), "values": values}
            )
    return output


def _structural_evidence(
    *,
    pivots: Sequence[dict[str, Any]],
    cycles: Sequence[dict[str, Any]],
    objective: dict[str, Any],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for pivot in pivots:
        output.append(
            {
                "ref": pivot["ref"],
                "kind": "CONFIRMED_PIVOT",
                "date": str(pivot["confirmation_date"]),
                "values": {key: value for key, value in pivot.items() if key != "ref"},
            }
        )
    for cycle in cycles:
        output.append(
            {
                "ref": cycle["ref"],
                "kind": "MACD_CYCLE",
                "date": str(cycle.get("confirmed_on") or cycle["start"]),
                "values": {key: value for key, value in cycle.items() if key != "ref"},
            }
        )
    attacks = list(objective.get("attacks") or [])[-12:]
    relevant_defense_ids = {
        str(row["defense_id"])
        for row in objective.get("defenses") or []
        if row.get("status") == "ACTIVE"
    }
    relevant_defense_ids.update(str(row["defense_id"]) for row in (objective.get("defenses") or [])[-8:])
    for attack in attacks:
        output.append(
            {
                "ref": str(attack["attack_id"]),
                "kind": "CAUSAL_CONTROL_ATTACK",
                "date": str(attack["confirmed_on"]),
                "values": {key: value for key, value in attack.items() if key != "attack_id"},
            }
        )
    for defense in objective.get("defenses") or []:
        if str(defense["defense_id"]) not in relevant_defense_ids:
            continue
        output.append(
            {
                "ref": str(defense["defense_id"]),
                "kind": "CAUSAL_DEFENSE",
                "date": str(defense["established_on"]),
                "values": {key: value for key, value in defense.items() if key != "defense_id"},
            }
        )
    return output


def _deduplicate_evidence(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ref: dict[str, dict[str, Any]] = {}
    for row in rows:
        ref = str(row["ref"])
        if ref in by_ref and by_ref[ref] != row:
            raise AtomicPacketError(f"conflicting evidence ref: {ref}")
        by_ref[ref] = row
    return sorted(by_ref.values(), key=lambda row: (str(row.get("date") or ""), str(row["ref"])))


def build_atomic_packet(
    *,
    anonymous_stock_id: str,
    review_id: str,
    as_of: str,
    visible_frame: pd.DataFrame,
    daily_state: dict[str, Any],
    review_packet: dict[str, Any],
    required_question_manifest: dict[str, Any] | None = None,
    atomic_schema_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one identity-blind packet using no bar or source dated after as_of."""
    visible = visible_frame[visible_frame["date"] <= pd.Timestamp(as_of)].copy()
    if visible.empty or visible.iloc[-1]["date"].date().isoformat() != as_of:
        raise AtomicPacketError(f"as_of is not a visible trading day: {as_of}")
    objective = daily_state["objective_state"]
    all_pivots = causal_pivots_as_of(review_packet, as_of)
    all_cycles = causal_cycles_as_of(review_packet, visible, as_of)
    completed_cycles = [
        row for row in all_cycles if row.get("status") == "CONFIRMED" and row.get("end")
    ]
    cycles = all_cycles[-10:]
    anchors = _anchor_candidates(objective, all_pivots, completed_cycles)
    relations = _relation_candidates(anchors, objective, as_of)
    stops = _stop_candidates(objective, float(visible.iloc[-1]["close"]))
    candidates = {
        "anchor_candidates": anchors,
        "relation_candidates": relations,
        "stop_candidates": stops,
    }
    working_comparison_windows = build_working_comparison_windows(
        objective=objective,
        confirmed_pivots=all_pivots,
        visible=visible,
        as_of=as_of,
    )
    objective_facts = _objective_facts(
        as_of=as_of,
        visible=visible,
        daily_state=daily_state,
        candidates=candidates,
        working_comparison_windows=working_comparison_windows,
    )
    all_hypotheses = [
        hypothesis
        for rows in objective_facts["scenario_hypotheses"].values()
        for hypothesis in rows
    ]
    used_relation_refs = {str(row["relation_ref"]) for row in all_hypotheses}
    used_stop_refs = {
        str(value)
        for row in all_hypotheses
        for value in (row.get("episode_stop_ref"), row.get("campaign_stop_ref"))
        if value not in {None, "", "UNKNOWN", "UNRESOLVED"}
    }
    used_relations = [row for row in relations if row["candidate_id"] in used_relation_refs]
    graph_anchor_refs = {
        str(value)
        for relation in used_relations
        for value in (
            relation.get("parent_anchor_ref"), relation.get("correction_anchor_ref"),
            relation.get("current_leg_ref"),
        )
        if value not in {None, "", "CURRENT_CONTEXT", "UNKNOWN", "UNRESOLVED"}
    }
    graph_anchor_refs.update(
        str(row["anchor_ref"])
        for row in all_hypotheses
        if row.get("anchor_ref") not in {None, "", "UNKNOWN", "UNRESOLVED"}
    )
    candidates = {
        "anchor_candidates": [row for row in anchors if row["candidate_id"] in graph_anchor_refs],
        "relation_candidates": used_relations,
        "stop_candidates": [row for row in stops if row["candidate_id"] in used_stop_refs],
    }
    endpoint_refs = {
        str(value)
        for row in candidates["anchor_candidates"]
        for value in (row.get("start_ref"), row.get("end_ref"))
        if value is not None
    }
    endpoint_pivot_refs = {ref for ref in endpoint_refs if ref.startswith("PIVOT:")}
    comparison_pivot_refs = {
        str(leg[key])
        for window in working_comparison_windows.values()
        if window is not None
        for leg in (window["baseline"], window["current"])
        for key in ("start_ref", "end_ref")
        if str(leg[key]).startswith("PIVOT:")
    }
    pivots = _bounded_pivots(all_pivots)
    pivots = list({
        row["ref"]: row
        for row in [
            *pivots,
            *(p for p in all_pivots if p["ref"] in endpoint_pivot_refs | comparison_pivot_refs),
        ]
    }.values())
    pivots = sorted(pivots, key=lambda row: (str(row["confirmation_date"]), str(row["ref"])))
    all_selections = [
        {"ref": f"SELECTION:{day}", "kind": "SELECTION", "date": str(day), "values": {"sources": sorted(set(sources))}}
        for day, sources in sorted((review_packet.get("selection_timeline") or {}).items())
        if str(day) <= as_of
    ]
    selections = all_selections[-AI_VISIBLE_SELECTION_WINDOW:]
    required_bar_dates = {
        str(value)
        for row in [*pivots, *cycles]
        for value in (
            row.get("source_date"), row.get("confirmation_date"), row.get("start"), row.get("end"),
            row.get("low_date"), row.get("high_date"), row.get("confirmed_on"),
        )
        if value is not None
    }
    required_bar_dates.update(ref.split(":", 1)[1] for ref in endpoint_refs if ref.startswith("BAR:"))
    required_bar_dates.update(
        str(leg[key])
        for window in working_comparison_windows.values()
        if window is not None
        for leg in (window["baseline"], window["current"])
        for key in ("start_date", "end_date")
    )
    evidence = _deduplicate_evidence(
        [
            *_visible_bar_evidence(visible, required_dates=required_bar_dates),
            *_structural_evidence(pivots=pivots, cycles=cycles, objective=objective),
            *_comparison_window_evidence(working_comparison_windows),
            *selections,
            *_candidate_evidence(candidates, as_of),
        ]
    )
    schema_metadata = atomic_schema_metadata or _read_json(DEFAULT_ATOMIC_SCHEMA)
    expected_questions = build_required_question_manifest(
        schema_metadata, candidates, objective_facts["scenario_hypotheses"]
    )
    questions = required_question_manifest if required_question_manifest is not None else expected_questions
    if questions != expected_questions:
        raise AtomicPacketError("explicit question manifest does not match dynamically expanded schema candidates")
    objective_facts.update(
        {
            "builder_version": BUILDER_VERSION,
            "builder_status": BUILDER_STATUS,
            "pivot_scale_disclosure": PIVOT_SCALE_DISCLOSURE,
            "objective_engine_source": "FULL_PRICE_CSV_VISIBLE_THROUGH_AS_OF_NOT_V1_RECENT_30",
            "full_history_visible_bar_count": int(len(visible)),
            "full_history_input_sha256": _full_history_input_sha256(visible),
            "causal_cutoff_as_of": as_of,
            "ai_visible_recent_bar_window_draft": AI_VISIBLE_RECENT_BAR_WINDOW_DRAFT,
            "ai_visible_bar_count": sum(row["kind"] == "BAR" for row in evidence),
            "ai_visible_evidence_sha256": canonical_sha256(evidence),
            "candidate_enumeration_policy": HYPOTHESIS_ENUMERATION_POLICY,
            "performance_used_for_ordering_or_truncation": False,
        }
    )
    packet = {
        "review_id": str(review_id),
        "anonymous_stock_id": str(anonymous_stock_id),
        "as_of": as_of,
        "question_manifest": questions,
        "evidence": evidence,
        "objective_facts": objective_facts,
        "question_manifest_sha256": canonical_sha256(questions),
        "evidence_catalog_sha256": canonical_sha256(evidence),
    }
    packet["input_packet_sha256"] = canonical_sha256(packet)
    assert_anonymous_and_causal(packet, as_of=as_of)
    return packet


def assert_anonymous_and_causal(packet: dict[str, Any], *, as_of: str) -> None:
    forbidden_keys = {"code", "name", "symbol", "market", *FORBIDDEN_OUTCOME_FIELDS}

    try:
        cutoff = pd.Timestamp(as_of)
    except (TypeError, ValueError) as exc:
        raise AtomicPacketError(f"invalid as_of cutoff: {as_of}") from exc
    if cutoff.time() != pd.Timestamp(as_of).normalize().time() or cutoff.date().isoformat() != as_of:
        raise AtomicPacketError(f"as_of cutoff must be an ISO calendar date: {as_of}")
    if packet.get("as_of") != as_of:
        raise AtomicPacketError("packet as_of differs from enforced causal cutoff")
    objective_facts = packet.get("objective_facts") or {}
    if objective_facts.get("causal_cutoff_as_of") != as_of:
        raise AtomicPacketError("packet causal_cutoff_as_of differs from enforced causal cutoff")

    def assert_visible_date(value: Any, label: str) -> None:
        if value is None:
            return
        try:
            parsed = pd.Timestamp(str(value))
        except (TypeError, ValueError) as exc:
            raise AtomicPacketError(f"invalid evidence date in {label}: {value}") from exc
        if parsed > cutoff:
            raise AtomicPacketError(f"future date in packet: {value} > {as_of}")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized_key = str(key).lower()
                if (
                    normalized_key in forbidden_keys
                    or normalized_key.startswith("future_")
                    or normalized_key.startswith("forward_")
                ):
                    raise AtomicPacketError(f"forbidden identity/performance key in packet: {key}")
                if normalized_key in {
                    "date", "start", "end", "low_date", "high_date", "source_date",
                    "confirmation_date", "available_on", "confirmed_on", "breached_on",
                    "origin_pivot_source_date", "parent_available_on", "correction_available_on",
                    "current_available_on", "baseline_available_on", "causal_cutoff_as_of",
                    "start_date", "end_date",
                }:
                    assert_visible_date(nested, normalized_key)
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(packet)
    evidence = packet.get("evidence")
    if not isinstance(evidence, list):
        raise AtomicPacketError("packet evidence must be a list")
    for row in evidence:
        if not isinstance(row, dict) or "date" not in row:
            raise AtomicPacketError("every evidence row requires a machine-verifiable date")
        assert_visible_date(row["date"], str(row.get("ref") or "evidence"))
        ref = str(row.get("ref") or "")
        if ref.startswith("BAR:") and ref.split(":", 1)[1] != str(row["date"]):
            raise AtomicPacketError("BAR evidence ref date differs from evidence date")
    if packet.get("question_manifest_sha256") != canonical_sha256(packet["question_manifest"]):
        raise AtomicPacketError("packet question manifest hash does not match")
    if packet.get("evidence_catalog_sha256") != canonical_sha256(packet["evidence"]):
        raise AtomicPacketError("packet evidence catalog hash does not match")
    if (packet.get("objective_facts") or {}).get("ai_visible_evidence_sha256") != canonical_sha256(packet["evidence"]):
        raise AtomicPacketError("packet AI-visible evidence hash does not match")
    core = dict(packet)
    supplied_input_hash = core.pop("input_packet_sha256", None)
    if supplied_input_hash != canonical_sha256(core):
        raise AtomicPacketError("packet integrity hashes do not match")


def derive_review_points(
    daily_states: Sequence[dict[str, Any]],
    *,
    legacy_dates: set[str] | None = None,
) -> list[dict[str, Any]]:
    if legacy_dates is not None:
        return [row for row in daily_states if row["as_of"] in legacy_dates]
    return [row for row in daily_states if row["review_required"]]


def build_stock_packets(
    *,
    price_frame: pd.DataFrame,
    review_packet: dict[str, Any],
    anonymous_stock_id: str,
    identity_salt: bytes,
    private_code: str,
    monitor_on: str,
    as_of: str,
    required_question_manifest: dict[str, Any] | None = None,
    atomic_schema_metadata: dict[str, Any] | None = None,
    legacy_dates: set[str] | None = None,
    limit_review_points: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    visible, daily_states = build_daily_objective_states(
        price_frame, review_packet, monitor_on=monitor_on, as_of=as_of
    )
    points = derive_review_points(daily_states, legacy_dates=legacy_dates)
    if limit_review_points is not None:
        points = points[: max(0, int(limit_review_points))]
    packets = []
    for state in points:
        day = str(state["as_of"])
        review_id = "D-" + hmac.new(identity_salt, f"{private_code}|{day}|V2".encode(), hashlib.sha256).hexdigest()[:24]
        packets.append(
            build_atomic_packet(
                anonymous_stock_id=anonymous_stock_id,
                review_id=review_id,
                as_of=day,
                visible_frame=visible,
                daily_state=state,
                review_packet=review_packet,
                required_question_manifest=required_question_manifest,
                atomic_schema_metadata=atomic_schema_metadata,
            )
        )
    event_counts = Counter(event for state in daily_states for event in state["events"])
    lazy_reason_counts = Counter(reason for state in daily_states for reason in state["lazy_review_reasons"])
    skip_reason_counts = Counter(
        str(state["skip_reason"]) for state in daily_states if state["skip_reason"] is not None
    )
    packet_bytes = [len(canonical_json_bytes(packet)) for packet in packets]
    candidate_counts = [
        {
            "anchor": len(packet["question_manifest"]["anchor_candidates"]),
            "relation": len(packet["question_manifest"]["relation_candidates"]),
            "stop": len(packet["question_manifest"]["stop_candidates"]),
        }
        for packet in packets
    ]
    hypothesis_counts = {
        scenario: [
            len(packet["objective_facts"]["scenario_hypotheses"][scenario])
            for packet in packets
        ]
        for scenario in SCENARIOS
    }
    verdict_counts = [
        len(packet["question_manifest"]["global_question_ids"])
        + sum(
            len(entry["required_question_ids"])
            for group in ("anchor_candidates", "relation_candidates", "stop_candidates")
            for entry in packet["question_manifest"][group]
        )
        for packet in packets
    ]
    return packets, {
        "stock_days_scanned": len(daily_states),
        "review_points_before_lazy": sum(bool(state["review_required_before_lazy"]) for state in daily_states),
        "review_points": len(points),
        "review_point_policy": "LEGACY_V1_BOUNDARY_DATES_BEHAVIOR_COMPARISON_ONLY"
        if legacy_dates is not None else FORMAL_REVIEW_POINT_POLICY,
        "as_of_ceiling": as_of,
        "objective_event_counts": dict(sorted(event_counts.items())),
        "lazy_review_reason_counts": dict(sorted(lazy_reason_counts.items())),
        "skip_reason_counts": dict(sorted(skip_reason_counts.items())),
        "packet_bytes": {
            "average": round(sum(packet_bytes) / len(packet_bytes), 2) if packet_bytes else 0,
            "maximum": max(packet_bytes, default=0),
        },
        "required_verdict_counts": {
            "average": round(sum(verdict_counts) / len(verdict_counts), 2) if verdict_counts else 0,
            "maximum": max(verdict_counts, default=0),
        },
        "candidate_counts": {
            group: {
                "average": round(sum(row[group] for row in candidate_counts) / len(candidate_counts), 2)
                if candidate_counts else 0,
                "maximum": max((row[group] for row in candidate_counts), default=0),
            }
            for group in ("anchor", "relation", "stop")
        },
        "scenario_hypothesis_counts": {
            scenario: {
                "average": round(sum(values) / len(values), 2) if values else 0,
                "maximum": max(values, default=0),
            }
            for scenario, values in hypothesis_counts.items()
        },
    }


def _canonical_jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in rows)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _assert_identity_map_isolated(identity_map_path: Path, *public_roots: Path) -> None:
    identity = Path(identity_map_path).resolve()
    for public_root in public_roots:
        if _is_within(identity, Path(public_root)):
            raise FormalBuildIntegrityError(
                "sealed identity map must be outside public build and AI-source directories"
            )


def load_formal_inventory(
    *,
    input_manifest_path: Path,
    packet_manifest_path: Path,
    identity_map_path: Path,
    expected_stock_count: int = FORMAL_STOCK_COUNT,
    expected_stock_days: int = FORMAL_MONITORED_STOCK_DAYS,
    verify_all_source_files: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the private inventory while returning only anonymous public metadata.

    `private_code` and source paths exist only in the in-process return value;
    neither is serialized by the formal build plan or review-point source.
    """
    input_manifest_path = Path(input_manifest_path)
    packet_manifest_path = Path(packet_manifest_path)
    identity_map_path = Path(identity_map_path)
    inputs = _read_json(input_manifest_path)
    packets = _read_json(packet_manifest_path)
    identity = _read_json(identity_map_path)
    input_rows = list(inputs.get("items") or [])
    packet_rows = list(packets.get("items") or [])
    identity_rows = list(identity.get("mapping") or [])
    if int(inputs.get("prepared_stock_count", -1)) != expected_stock_count:
        raise FormalBuildIntegrityError("frozen input manifest stock count differs from formal expectation")
    if len(input_rows) != expected_stock_count or len(packet_rows) != expected_stock_count:
        raise FormalBuildIntegrityError("frozen source manifests do not contain the expected stock count")
    if len(identity_rows) != expected_stock_count:
        raise FormalBuildIntegrityError("sealed identity map does not contain the expected stock count")
    input_codes = [str(row.get("code") or "") for row in input_rows]
    if not all(input_codes) or len(input_codes) != len(set(input_codes)):
        raise FormalBuildIntegrityError("input manifest has empty or duplicate private stock codes")
    packet_by_code = {str(row.get("code") or ""): row for row in packet_rows}
    identity_by_code = {str(row.get("code") or ""): row for row in identity_rows}
    if set(packet_by_code) != set(input_codes) or set(identity_by_code) != set(input_codes):
        raise FormalBuildIntegrityError("frozen packet/identity coverage differs from input manifest")
    if len(packet_by_code) != expected_stock_count or len(identity_by_code) != expected_stock_count:
        raise FormalBuildIntegrityError("frozen packet/identity sources contain duplicate codes")
    if str(inputs.get("as_of") or "") != str(packets.get("as_of") or ""):
        raise FormalBuildIntegrityError("input and packet manifests have different as_of cutoffs")

    inventory: list[dict[str, Any]] = []
    observed_stock_days = 0
    anonymous_ids: set[str] = set()
    for stock_ordinal, input_row in enumerate(input_rows):
        private_code = input_codes[stock_ordinal]
        packet_row = packet_by_code[private_code]
        identity_row = identity_by_code[private_code]
        anonymous_id = str(identity_row.get("anonymous_id") or "")
        if not anonymous_id.startswith("S-") or len(anonymous_id) != 18:
            raise FormalBuildIntegrityError("sealed identity map contains an invalid anonymous id")
        if anonymous_id in anonymous_ids:
            raise FormalBuildIntegrityError("sealed identity map contains duplicate anonymous ids")
        anonymous_ids.add(anonymous_id)
        monitor_on = str(input_row.get("monitor_on") or "")
        as_of = str(inputs.get("as_of") or "")
        monitored_sessions = int(packet_row.get("monitored_sessions", -1))
        if str(packet_row.get("monitor_on") or "") != monitor_on:
            raise FormalBuildIntegrityError("packet monitor_on differs from frozen input manifest")
        if monitored_sessions < 0:
            raise FormalBuildIntegrityError("packet manifest has invalid monitored_sessions")
        observed_stock_days += monitored_sessions
        price_path = Path(str(input_row.get("price_path") or ""))
        review_packet_path = Path(str(packet_row.get("packet") or ""))
        price_sha256 = str(input_row.get("price_sha256") or "")
        review_packet_sha256 = str(packet_row.get("packet_sha256") or "")
        if verify_all_source_files:
            if file_sha256(price_path) != price_sha256:
                raise FormalBuildIntegrityError("frozen price source hash mismatch")
            if file_sha256(review_packet_path) != review_packet_sha256:
                raise FormalBuildIntegrityError("frozen review packet hash mismatch")
            dates = pd.to_datetime(pd.read_csv(price_path, usecols=["date"])["date"], errors="raise")
            actual_sessions = int(((dates >= pd.Timestamp(monitor_on)) & (dates <= pd.Timestamp(as_of))).sum())
            if actual_sessions != monitored_sessions:
                raise FormalBuildIntegrityError("frozen price sessions differ from packet manifest")
            _read_json(review_packet_path)
        inventory.append(
            {
                "stock_source_ordinal": stock_ordinal,
                "private_code": private_code,
                "anonymous_stock_id": anonymous_id,
                "monitor_on": monitor_on,
                "as_of": as_of,
                "monitored_sessions": monitored_sessions,
                "price_path": price_path,
                "price_sha256": price_sha256,
                "review_packet_path": review_packet_path,
                "review_packet_sha256": review_packet_sha256,
            }
        )
    if observed_stock_days != expected_stock_days:
        raise FormalBuildIntegrityError(
            f"frozen monitored stock-days {observed_stock_days} differ from expected {expected_stock_days}"
        )
    public_catalog = [
        {
            "stock_source_ordinal": row["stock_source_ordinal"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "monitor_on": row["monitor_on"],
            "as_of": row["as_of"],
            "monitored_sessions": row["monitored_sessions"],
            "price_sha256": row["price_sha256"],
            "review_packet_sha256": row["review_packet_sha256"],
            "build_shard_id": int(row["stock_source_ordinal"]) % FORMAL_BUILD_SHARD_COUNT,
        }
        for row in inventory
    ]
    metadata = {
        "input_manifest_sha256": file_sha256(input_manifest_path),
        "packet_manifest_sha256": file_sha256(packet_manifest_path),
        "sealed_identity_map_sha256": file_sha256(identity_map_path),
        "as_of_ceiling": str(inputs["as_of"]),
        "expected_stocks": expected_stock_count,
        "expected_monitored_stock_days": expected_stock_days,
        "public_catalog": public_catalog,
        "public_catalog_sha256": canonical_sha256(public_catalog),
    }
    return inventory, metadata


def prepare_formal_build_plan(
    *,
    input_manifest_path: Path,
    packet_manifest_path: Path,
    identity_map_path: Path,
    atomic_schema_path: Path,
    freeze_source_manifest_path: Path,
    work_dir: Path,
    formal_output_dir: Path,
    expected_stock_count: int = FORMAL_STOCK_COUNT,
    expected_stock_days: int = FORMAL_MONITORED_STOCK_DAYS,
) -> dict[str, Any]:
    """Verify every frozen source once and publish an immutable anonymous build plan."""
    _assert_identity_map_isolated(identity_map_path, work_dir, formal_output_dir)
    _, metadata = load_formal_inventory(
        input_manifest_path=input_manifest_path,
        packet_manifest_path=packet_manifest_path,
        identity_map_path=identity_map_path,
        expected_stock_count=expected_stock_count,
        expected_stock_days=expected_stock_days,
        verify_all_source_files=True,
    )
    freeze_source_manifest_path = Path(freeze_source_manifest_path)
    freeze_source = _read_json(freeze_source_manifest_path)
    if (
        freeze_source.get("stocks") != expected_stock_count
        or freeze_source.get("stock_days") != expected_stock_days
    ):
        raise FormalBuildIntegrityError("freeze source manifest scope differs from formal inventory")
    plan = {
        "builder_version": BUILDER_VERSION,
        "builder_status": BUILDER_STATUS,
        "status": "OUTCOME_BLIND_SOURCE_VALIDATED",
        "formal_build_shard_count": FORMAL_BUILD_SHARD_COUNT,
        "causal_data_policy": "ALL_PACKET_EVIDENCE_DATES_AT_OR_BEFORE_AS_OF_NO_OUTCOME_FIELDS",
        "identity_isolated": True,
        "builder_code_sha256": file_sha256(Path(__file__)),
        "objective_engine_version": ENGINE_VERSION,
        "objective_engine_status": ENGINE_STATUS,
        "objective_engine_code_sha256": file_sha256(ROOT / "scripts/hybrid_v3_objective_state_v2.py"),
        "pivot_scale_disclosure": PIVOT_SCALE_DISCLOSURE,
        "course_l1_l2_used": False,
        "macd_parameters": list(MACD_PARAMETERS),
        "preferred_structure_bars": PREFERRED_STRUCTURE_BARS,
        "indicator_warmup_bars": INDICATOR_WARMUP_BARS,
        "atomic_schema_sha256": file_sha256(Path(atomic_schema_path)),
        "freeze_source_manifest_sha256": file_sha256(freeze_source_manifest_path),
        "freeze_source_manifest_path": str(freeze_source_manifest_path.resolve()),
        **metadata,
    }
    path = Path(work_dir) / FORMAL_PLAN_FILE
    _publish_immutable(path, canonical_json_bytes(plan) + b"\n")
    return plan


def _read_and_validate_formal_plan(
    work_dir: Path,
    metadata: dict[str, Any],
    atomic_schema_path: Path,
    freeze_source_manifest_path: Path,
) -> tuple[dict[str, Any], str]:
    path = Path(work_dir) / FORMAL_PLAN_FILE
    if not path.is_file():
        raise FormalBuildIntegrityError("formal build plan is missing; run source validation first")
    plan = _read_json(path)
    expected = {
        "input_manifest_sha256": metadata["input_manifest_sha256"],
        "packet_manifest_sha256": metadata["packet_manifest_sha256"],
        "sealed_identity_map_sha256": metadata["sealed_identity_map_sha256"],
        "public_catalog_sha256": metadata["public_catalog_sha256"],
        "expected_stocks": metadata["expected_stocks"],
        "expected_monitored_stock_days": metadata["expected_monitored_stock_days"],
        "as_of_ceiling": metadata["as_of_ceiling"],
        "builder_version": BUILDER_VERSION,
        "builder_status": BUILDER_STATUS,
        "builder_code_sha256": file_sha256(Path(__file__)),
        "objective_engine_version": ENGINE_VERSION,
        "objective_engine_status": ENGINE_STATUS,
        "objective_engine_code_sha256": file_sha256(ROOT / "scripts/hybrid_v3_objective_state_v2.py"),
        "pivot_scale_disclosure": PIVOT_SCALE_DISCLOSURE,
        "course_l1_l2_used": False,
        "macd_parameters": list(MACD_PARAMETERS),
        "preferred_structure_bars": PREFERRED_STRUCTURE_BARS,
        "indicator_warmup_bars": INDICATOR_WARMUP_BARS,
        "atomic_schema_sha256": file_sha256(Path(atomic_schema_path)),
        "freeze_source_manifest_sha256": file_sha256(Path(freeze_source_manifest_path)),
        "freeze_source_manifest_path": str(Path(freeze_source_manifest_path).resolve()),
    }
    for field, value in expected.items():
        if plan.get(field) != value:
            raise FormalBuildIntegrityError(f"formal build plan changed frozen field: {field}")
    if plan.get("public_catalog") != metadata["public_catalog"]:
        raise FormalBuildIntegrityError("formal build plan public catalog changed")
    if plan.get("formal_build_shard_count") != FORMAL_BUILD_SHARD_COUNT:
        raise FormalBuildIntegrityError("formal build plan shard count changed")
    return plan, file_sha256(path)


def _formal_fragment_paths(work_dir: Path, stock_ordinal: int, anonymous_id: str) -> tuple[Path, Path]:
    shard_id = int(stock_ordinal) % FORMAL_BUILD_SHARD_COUNT
    base = Path(work_dir) / f"shard_{shard_id:02d}" / "stocks" / f"stock_{stock_ordinal:04d}_{anonymous_id}"
    return base.with_suffix(".jsonl"), base.with_suffix(".manifest.json")


def _validate_fragment(
    *,
    fragment_path: Path,
    manifest_path: Path,
    expected_stock: dict[str, Any],
    plan_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not fragment_path.is_file() or not manifest_path.is_file():
        raise FormalBuildIntegrityError("resumable stock fragment or its manifest is missing")
    manifest = _read_json(manifest_path)
    checks = {
        "builder_version": BUILDER_VERSION,
        "builder_status": BUILDER_STATUS,
        "plan_sha256": plan_sha256,
        "stock_source_ordinal": expected_stock["stock_source_ordinal"],
        "anonymous_stock_id": expected_stock["anonymous_stock_id"],
        "price_sha256": expected_stock["price_sha256"],
        "review_packet_sha256": expected_stock["review_packet_sha256"],
        "monitored_sessions": expected_stock["monitored_sessions"],
    }
    for field, value in checks.items():
        if manifest.get(field) != value:
            raise FormalBuildIntegrityError(f"stock fragment manifest changed field: {field}")
    if file_sha256(fragment_path) != manifest.get("fragment_sha256"):
        raise FormalBuildIntegrityError("stock fragment hash mismatch")
    rows = _read_jsonl(fragment_path)
    if len(rows) != int(manifest.get("review_points", -1)):
        raise FormalBuildIntegrityError("stock fragment row count differs from manifest")
    expected_keys = {
        "stock_source_ordinal", "stock_review_ordinal", "review_id", "packet_sha256", "packet",
    }
    for review_ordinal, row in enumerate(rows):
        if set(row) != expected_keys:
            raise FormalBuildIntegrityError("stock fragment row has unexpected fields")
        if row["stock_source_ordinal"] != expected_stock["stock_source_ordinal"]:
            raise FormalBuildIntegrityError("stock fragment changed stock_source_ordinal")
        if row["stock_review_ordinal"] != review_ordinal:
            raise FormalBuildIntegrityError("stock fragment review ordinals are not exact")
        packet = row.get("packet")
        if not isinstance(packet, dict) or canonical_sha256(packet) != row.get("packet_sha256"):
            raise FormalBuildIntegrityError("stock fragment packet hash mismatch")
        if packet.get("review_id") != row.get("review_id"):
            raise FormalBuildIntegrityError("stock fragment review_id mismatch")
        if packet.get("anonymous_stock_id") != expected_stock["anonymous_stock_id"]:
            raise FormalBuildIntegrityError("stock fragment anonymous id mismatch")
        assert_anonymous_and_causal(packet, as_of=str(packet.get("as_of") or ""))
    if [row["packet"]["as_of"] for row in rows] != sorted(row["packet"]["as_of"] for row in rows):
        raise FormalBuildIntegrityError("stock fragment packets are not in causal date order")
    return rows, manifest


def build_formal_shard(
    *,
    input_manifest_path: Path,
    packet_manifest_path: Path,
    identity_map_path: Path,
    atomic_schema_path: Path,
    freeze_source_manifest_path: Path,
    work_dir: Path,
    formal_output_dir: Path,
    shard_id: int,
    expected_stock_count: int = FORMAL_STOCK_COUNT,
    expected_stock_days: int = FORMAL_MONITORED_STOCK_DAYS,
) -> dict[str, Any]:
    """Build one deterministic stock shard with immutable per-stock resume points."""
    if shard_id not in range(FORMAL_BUILD_SHARD_COUNT):
        raise FormalBuildIntegrityError("formal shard_id must be 0, 1, or 2")
    _assert_identity_map_isolated(identity_map_path, work_dir, formal_output_dir)
    inventory, metadata = load_formal_inventory(
        input_manifest_path=input_manifest_path,
        packet_manifest_path=packet_manifest_path,
        identity_map_path=identity_map_path,
        expected_stock_count=expected_stock_count,
        expected_stock_days=expected_stock_days,
        verify_all_source_files=False,
    )
    plan, plan_sha256 = _read_and_validate_formal_plan(
        work_dir, metadata, atomic_schema_path, freeze_source_manifest_path
    )
    schema = _read_json(Path(atomic_schema_path))
    salt = bytes.fromhex(str(_read_json(Path(identity_map_path))["salt_hex"]))
    assigned = [row for row in inventory if row["stock_source_ordinal"] % FORMAL_BUILD_SHARD_COUNT == shard_id]
    fragment_entries: list[dict[str, Any]] = []
    resumed_stocks = 0
    for stock in assigned:
        fragment_path, fragment_manifest_path = _formal_fragment_paths(
            work_dir, stock["stock_source_ordinal"], stock["anonymous_stock_id"]
        )
        if fragment_path.exists() and fragment_manifest_path.exists():
            rows, fragment_manifest = _validate_fragment(
                fragment_path=fragment_path,
                manifest_path=fragment_manifest_path,
                expected_stock=stock,
                plan_sha256=plan_sha256,
            )
            resumed_stocks += 1
        else:
            if file_sha256(stock["price_path"]) != stock["price_sha256"]:
                raise FormalBuildIntegrityError("assigned frozen price source hash mismatch")
            if file_sha256(stock["review_packet_path"]) != stock["review_packet_sha256"]:
                raise FormalBuildIntegrityError("assigned frozen review packet hash mismatch")
            packets, summary = build_stock_packets(
                price_frame=pd.read_csv(stock["price_path"]),
                review_packet=_read_json(stock["review_packet_path"]),
                anonymous_stock_id=stock["anonymous_stock_id"],
                identity_salt=salt,
                private_code=stock["private_code"],
                monitor_on=stock["monitor_on"],
                as_of=stock["as_of"],
                atomic_schema_metadata=schema,
            )
            if summary["stock_days_scanned"] != stock["monitored_sessions"]:
                raise FormalBuildIntegrityError("builder stock-day coverage differs from frozen manifest")
            rows = [
                {
                    "stock_source_ordinal": stock["stock_source_ordinal"],
                    "stock_review_ordinal": review_ordinal,
                    "review_id": packet["review_id"],
                    "packet_sha256": canonical_sha256(packet),
                    "packet": packet,
                }
                for review_ordinal, packet in enumerate(packets)
            ]
            payload = _canonical_jsonl_bytes(rows)
            fragment_manifest = {
                "builder_version": BUILDER_VERSION,
                "builder_status": BUILDER_STATUS,
                "status": "COMPLETE_ANONYMOUS_STOCK_FRAGMENT",
                "plan_sha256": plan_sha256,
                "build_shard_id": shard_id,
                "stock_source_ordinal": stock["stock_source_ordinal"],
                "anonymous_stock_id": stock["anonymous_stock_id"],
                "price_sha256": stock["price_sha256"],
                "review_packet_sha256": stock["review_packet_sha256"],
                "monitored_sessions": stock["monitored_sessions"],
                "review_points": len(rows),
                "review_ids_sha256": canonical_sha256([row["review_id"] for row in rows]),
                "packet_hashes_sha256": canonical_sha256([row["packet_sha256"] for row in rows]),
                "fragment_sha256": hashlib.sha256(payload).hexdigest(),
            }
            _publish_immutable(fragment_path, payload)
            _publish_immutable(fragment_manifest_path, canonical_json_bytes(fragment_manifest) + b"\n")
            rows, fragment_manifest = _validate_fragment(
                fragment_path=fragment_path,
                manifest_path=fragment_manifest_path,
                expected_stock=stock,
                plan_sha256=plan_sha256,
            )
        fragment_entries.append(
            {
                "stock_source_ordinal": stock["stock_source_ordinal"],
                "anonymous_stock_id": stock["anonymous_stock_id"],
                "fragment_file": str(fragment_path.relative_to(Path(work_dir))),
                "fragment_manifest_file": str(fragment_manifest_path.relative_to(Path(work_dir))),
                "review_points": len(rows),
                "fragment_sha256": fragment_manifest["fragment_sha256"],
            }
        )
    shard_manifest = {
        "builder_version": BUILDER_VERSION,
        "builder_status": BUILDER_STATUS,
        "status": "COMPLETE_ANONYMOUS_BUILD_SHARD",
        "plan_sha256": plan_sha256,
        "build_shard_id": shard_id,
        "formal_build_shard_count": FORMAL_BUILD_SHARD_COUNT,
        "expected_stocks": len(assigned),
        "covered_stocks": len(fragment_entries),
        "review_points": sum(row["review_points"] for row in fragment_entries),
        "stock_ordinals_sha256": canonical_sha256([row["stock_source_ordinal"] for row in fragment_entries]),
        "fragments": fragment_entries,
    }
    shard_manifest_path = Path(work_dir) / f"shard_{shard_id:02d}" / "build.manifest.json"
    _publish_immutable(shard_manifest_path, canonical_json_bytes(shard_manifest) + b"\n")
    return {**shard_manifest, "resumed_stocks": resumed_stocks}


def _atomic_publish_formal_bundle(
    formal_output_dir: Path,
    source_payload: bytes,
    manifest: dict[str, Any],
) -> bool:
    """Publish source+manifest as one directory rename; return True on resume."""
    formal_output_dir = Path(formal_output_dir)
    source_name = FORMAL_SOURCE_FILE
    manifest_name = FORMAL_SOURCE_MANIFEST_FILE
    expected_manifest_payload = canonical_json_bytes(manifest) + b"\n"
    if formal_output_dir.exists():
        source_path = formal_output_dir / source_name
        manifest_path = formal_output_dir / manifest_name
        if not source_path.is_file() or not manifest_path.is_file():
            raise FormalBuildIntegrityError("formal output directory is incomplete")
        if source_path.read_bytes() != source_payload or manifest_path.read_bytes() != expected_manifest_payload:
            raise FormalBuildIntegrityError("refusing to change an already frozen formal review source")
        return True
    formal_output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{formal_output_dir.name}.", dir=formal_output_dir.parent))
    try:
        (temporary / source_name).write_bytes(source_payload)
        (temporary / manifest_name).write_bytes(expected_manifest_payload)
        os.rename(temporary, formal_output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return False


def objective_sampling_stratum(packet: dict[str, Any]) -> str:
    """Assign one outcome-blind stratum from program facts only."""
    facts = packet.get("objective_facts") or {}
    hypotheses = facts.get("scenario_hypotheses") or {}
    if facts.get("parent_campaign_invalidated") is True or facts.get("macro_defense_alert") is True:
        return "MACRO_DEFENSE_REMOVE_PROXY"
    if hypotheses.get("BEAR_REVERSAL_LEFT_RIGHT"):
        return "BEAR_REVERSAL_OBJECTIVE_PROXY"
    if hypotheses.get("MACRO_COPY_RESONANCE"):
        return "MACRO_COPY_OBJECTIVE_PROXY"
    if hypotheses.get("FRESH_Q1_EXPANSION"):
        return "FRESH_Q1_OBJECTIVE_PROXY"
    if hypotheses.get("MATURE_TREND_PULLBACK"):
        return "V2_CORE_OBJECTIVE_PROXY"
    return "WAIT_POLICY_BOUNDARY"


def _review_manifest_state(builder_status: str) -> tuple[str, bool]:
    normalized_status = str(builder_status).upper()
    if normalized_status == "FINAL":
        return "LOCKED_OUTCOME_BLIND", True
    if normalized_status == "DRAFT_NOT_FINAL":
        return "BUILT_OUTCOME_BLIND_DRAFT", False
    raise FormalBuildIntegrityError(f"unsupported builder status: {builder_status}")


def build_review_point_manifest(
    *,
    plan_sha256: str,
    metadata: dict[str, Any],
    atomic_schema_path: Path,
    freeze_source_manifest_path: Path,
    review_points_path: Path,
    source_payload: bytes,
    source_index: list[dict[str, Any]],
    review_ids: list[str],
    packet_hashes: list[str],
) -> dict[str, Any]:
    """Build the consumer contract; only a FINAL builder may claim LOCKED."""
    normalized_status = str(BUILDER_STATUS).upper()
    status, source_order_locked = _review_manifest_state(normalized_status)
    expected_review_points = len(source_index)
    if not (
        expected_review_points == len(review_ids) == len(packet_hashes)
    ):
        raise FormalBuildIntegrityError("review manifest inputs have inconsistent row counts")
    freeze_source_manifest_path = Path(freeze_source_manifest_path).resolve()
    review_points_path = Path(review_points_path).resolve()
    return {
        "manifest_version": "hybrid-v3-review-points-v2",
        "builder_version": BUILDER_VERSION,
        "builder_status": normalized_status,
        "status": status,
        "outcome_blind": True,
        "identity_visible": False,
        "performance_visible": False,
        "future_data_visible": False,
        "source_order_locked": source_order_locked,
        "outcome_excluded_codes": [],
        "plan_sha256": plan_sha256,
        "formal_build_shard_count": FORMAL_BUILD_SHARD_COUNT,
        "input_manifest_sha256": metadata["input_manifest_sha256"],
        "packet_manifest_sha256": metadata["packet_manifest_sha256"],
        "atomic_schema_sha256": file_sha256(Path(atomic_schema_path)),
        "builder_code_sha256": file_sha256(Path(__file__)),
        "sealed_identity_map_sha256": metadata["sealed_identity_map_sha256"],
        "identity_isolated": True,
        "as_of_ceiling": metadata["as_of_ceiling"],
        "stocks": metadata["expected_stocks"],
        "stock_days_scanned": metadata["expected_monitored_stock_days"],
        "review_points": expected_review_points,
        "expected_v2_review_points": expected_review_points,
        "universe_scope": "FULL_V2_REVIEW_POINT_UNIVERSE",
        "sampling_strata_basis": "OBJECTIVE_ONLY_AS_OF",
        "sampling_stratum_priority": list(SAMPLING_STRATUM_PRIORITY),
        "sampling_stratum_counts": dict(
            sorted(Counter(str(row["sampling_stratum"]) for row in source_index).items())
        ),
        "source_manifest": {
            "path": str(freeze_source_manifest_path),
            "sha256": file_sha256(freeze_source_manifest_path),
        },
        "review_points_artifact": {
            "path": str(review_points_path),
            "sha256": hashlib.sha256(source_payload).hexdigest(),
            "rows": expected_review_points,
        },
        "artifact_sha256": hashlib.sha256(source_payload).hexdigest(),
        "source_ordinal_coverage": {
            "expected": expected_review_points,
            "covered": expected_review_points,
            "missing": 0,
            "overlap": 0,
            "unexpected": 0,
            "first": 0 if expected_review_points else None,
            "last": expected_review_points - 1 if expected_review_points else None,
        },
        "source_ordinal_index_sha256": canonical_sha256(source_index),
        "review_ids_sha256": canonical_sha256(review_ids),
        "packet_hashes_sha256": canonical_sha256(packet_hashes),
        "causal_data_policy": "ALL_PACKET_EVIDENCE_DATES_AT_OR_BEFORE_AS_OF_NO_OUTCOME_FIELDS",
        "pivot_scale_disclosure": PIVOT_SCALE_DISCLOSURE,
        "course_l1_l2_used": False,
        "macd_parameters": list(MACD_PARAMETERS),
        "preferred_structure_bars": PREFERRED_STRUCTURE_BARS,
        "indicator_warmup_bars": INDICATOR_WARMUP_BARS,
    }


def merge_formal_build_shards(
    *,
    input_manifest_path: Path,
    packet_manifest_path: Path,
    identity_map_path: Path,
    atomic_schema_path: Path,
    freeze_source_manifest_path: Path,
    work_dir: Path,
    formal_output_dir: Path,
    expected_stock_count: int = FORMAL_STOCK_COUNT,
    expected_stock_days: int = FORMAL_MONITORED_STOCK_DAYS,
) -> dict[str, Any]:
    """Merge the three complete anonymous shards and freeze exact review points."""
    _assert_identity_map_isolated(identity_map_path, work_dir, formal_output_dir)
    inventory, metadata = load_formal_inventory(
        input_manifest_path=input_manifest_path,
        packet_manifest_path=packet_manifest_path,
        identity_map_path=identity_map_path,
        expected_stock_count=expected_stock_count,
        expected_stock_days=expected_stock_days,
        verify_all_source_files=False,
    )
    plan, plan_sha256 = _read_and_validate_formal_plan(
        work_dir, metadata, atomic_schema_path, freeze_source_manifest_path
    )
    expected_by_ordinal = {int(row["stock_source_ordinal"]): row for row in inventory}
    found_stock_ordinals: set[int] = set()
    wrapper_rows: list[dict[str, Any]] = []
    for shard_id in range(FORMAL_BUILD_SHARD_COUNT):
        shard_manifest_path = Path(work_dir) / f"shard_{shard_id:02d}" / "build.manifest.json"
        if not shard_manifest_path.is_file():
            raise FormalBuildIntegrityError(f"formal build shard {shard_id} is incomplete")
        shard_manifest = _read_json(shard_manifest_path)
        if shard_manifest.get("plan_sha256") != plan_sha256 or shard_manifest.get("build_shard_id") != shard_id:
            raise FormalBuildIntegrityError("formal shard manifest changed plan or shard identity")
        expected_ordinals = sorted(
            ordinal for ordinal in expected_by_ordinal if ordinal % FORMAL_BUILD_SHARD_COUNT == shard_id
        )
        manifest_ordinals = [int(row["stock_source_ordinal"]) for row in shard_manifest.get("fragments") or []]
        if manifest_ordinals != expected_ordinals:
            raise FormalBuildIntegrityError("formal shard has missing, duplicate, or unexpected stocks")
        listed_fragment_files: set[Path] = set()
        for entry in shard_manifest["fragments"]:
            ordinal = int(entry["stock_source_ordinal"])
            if ordinal in found_stock_ordinals:
                raise FormalBuildIntegrityError("formal shard stock overlap detected")
            found_stock_ordinals.add(ordinal)
            fragment_path = Path(work_dir) / str(entry["fragment_file"])
            fragment_manifest_path = Path(work_dir) / str(entry["fragment_manifest_file"])
            listed_fragment_files.add(fragment_path.resolve())
            rows, fragment_manifest = _validate_fragment(
                fragment_path=fragment_path,
                manifest_path=fragment_manifest_path,
                expected_stock=expected_by_ordinal[ordinal],
                plan_sha256=plan_sha256,
            )
            if entry.get("fragment_sha256") != fragment_manifest.get("fragment_sha256"):
                raise FormalBuildIntegrityError("shard fragment hash differs from shard manifest")
            wrapper_rows.extend(rows)
        actual_fragment_files = {
            path.resolve() for path in (Path(work_dir) / f"shard_{shard_id:02d}" / "stocks").glob("*.jsonl")
        }
        if actual_fragment_files != listed_fragment_files:
            raise FormalBuildIntegrityError("formal shard contains an unlisted or missing fragment")
    if found_stock_ordinals != set(expected_by_ordinal):
        raise FormalBuildIntegrityError("formal shard union does not exactly cover frozen stocks")

    wrapper_rows.sort(key=lambda row: (int(row["stock_source_ordinal"]), int(row["stock_review_ordinal"])))
    review_ids = [str(row["review_id"]) for row in wrapper_rows]
    if len(review_ids) != len(set(review_ids)):
        raise FormalBuildIntegrityError("formal review source contains duplicate review_id")
    packets = [row["packet"] for row in wrapper_rows]
    for packet in packets:
        assert_anonymous_and_causal(packet, as_of=str(packet.get("as_of") or ""))
    source_index = [
        {
            "source_ordinal": source_ordinal,
            "review_id": row["review_id"],
            "anonymous_stock_id": row["packet"]["anonymous_stock_id"],
            "sampling_stratum": objective_sampling_stratum(row["packet"]),
            "packet_sha256": row["packet_sha256"],
        }
        for source_ordinal, row in enumerate(wrapper_rows)
    ]
    source_records = [
        {**identity, "packet": wrapper_rows[identity["source_ordinal"]]["packet"]}
        for identity in source_index
    ]
    source_payload = _canonical_jsonl_bytes(source_records)
    manifest = build_review_point_manifest(
        plan_sha256=plan_sha256,
        metadata=metadata,
        atomic_schema_path=atomic_schema_path,
        freeze_source_manifest_path=freeze_source_manifest_path,
        review_points_path=Path(formal_output_dir) / FORMAL_SOURCE_FILE,
        source_payload=source_payload,
        source_index=source_index,
        review_ids=review_ids,
        packet_hashes=[row["packet_sha256"] for row in wrapper_rows],
    )
    resumed = _atomic_publish_formal_bundle(Path(formal_output_dir), source_payload, manifest)
    return {**manifest, "resumed": resumed}


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> str:
    payload = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-manifest", type=Path, default=DEFAULT_INPUT_MANIFEST)
    parser.add_argument("--packet-manifest", type=Path, default=DEFAULT_PACKET_MANIFEST)
    parser.add_argument("--identity-map", type=Path, default=DEFAULT_IDENTITY_MAP)
    parser.add_argument("--question-manifest", type=Path)
    parser.add_argument("--atomic-schema", type=Path, default=DEFAULT_ATOMIC_SCHEMA)
    parser.add_argument("--code", action="append", help="Limit private source lookup; codes never enter output")
    parser.add_argument("--as-of")
    parser.add_argument("--limit-stocks", type=int)
    parser.add_argument("--limit-review-points", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reuse-v1-boundary-dates", action="store_true")
    parser.add_argument("--formal-prepare", action="store_true")
    parser.add_argument("--formal-build-shard", type=int)
    parser.add_argument("--formal-merge", action="store_true")
    parser.add_argument("--formal-work-dir", type=Path)
    parser.add_argument("--formal-output-dir", type=Path)
    parser.add_argument(
        "--freeze-source-manifest", type=Path, default=DEFAULT_FREEZE_SOURCE_MANIFEST
    )
    args = parser.parse_args()
    formal_modes = sum(
        [bool(args.formal_prepare), args.formal_build_shard is not None, bool(args.formal_merge)]
    )
    if formal_modes > 1:
        parser.error("choose exactly one formal prepare/build-shard/merge operation")
    if formal_modes:
        if args.formal_work_dir is None or args.formal_output_dir is None:
            parser.error("formal operations require --formal-work-dir and --formal-output-dir")
        common = {
            "input_manifest_path": args.input_manifest,
            "packet_manifest_path": args.packet_manifest,
            "identity_map_path": args.identity_map,
            "atomic_schema_path": args.atomic_schema,
            "freeze_source_manifest_path": args.freeze_source_manifest,
            "work_dir": args.formal_work_dir,
            "formal_output_dir": args.formal_output_dir,
        }
        if args.formal_prepare:
            result = prepare_formal_build_plan(**common)
        elif args.formal_build_shard is not None:
            result = build_formal_shard(**common, shard_id=args.formal_build_shard)
        else:
            result = merge_formal_build_shards(**common)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.dry_run and not (args.code or args.limit_stocks or args.limit_review_points):
        parser.error("this initial CLI only permits dry-run or explicitly limited cases")

    inputs = _read_json(args.input_manifest)
    packet_manifest = _read_json(args.packet_manifest)
    identity = _read_json(args.identity_map)
    if int(inputs.get("prepared_stock_count", 0)) != 1029:
        raise AtomicPacketError("frozen input manifest does not contain 1,029 prepared stocks")
    input_items = {str(row["code"]): row for row in inputs["items"]}
    packet_items = {str(row["code"]): row for row in packet_manifest["items"]}
    identities = {str(row["code"]): str(row["anonymous_id"]) for row in identity["mapping"]}
    salt = bytes.fromhex(str(identity["salt_hex"]))
    codes = list(input_items)
    if args.code:
        requested = set(str(value) for value in args.code)
        codes = [code for code in codes if code in requested]
    if args.limit_stocks is not None:
        codes = codes[: max(0, args.limit_stocks)]
    explicit_questions = _read_json(args.question_manifest) if args.question_manifest else None
    schema_metadata = _read_json(args.atomic_schema)
    legacy_by_stock: dict[str, set[str]] = {}
    if args.reuse_v1_boundary_dates:
        for row in _read_jsonl(DEFAULT_V1_BOUNDARIES):
            legacy_by_stock.setdefault(str(row["anonymous_stock_id"]), set()).add(str(row["as_of"]))

    output_rows: list[dict[str, Any]] = []
    packet_count = 0
    stock_days = review_points = review_points_before_lazy = 0
    all_event_counts: Counter[str] = Counter()
    all_lazy_counts: Counter[str] = Counter()
    all_skip_counts: Counter[str] = Counter()
    all_packet_bytes: list[int] = []
    all_verdict_counts: list[int] = []
    all_candidate_counts: dict[str, list[int]] = {key: [] for key in ("anchor", "relation", "stop")}
    all_hypothesis_counts: dict[str, list[int]] = {scenario: [] for scenario in SCENARIOS}
    for code in codes:
        item = input_items[code]
        packet_item = packet_items[code]
        price_path = Path(item["price_path"])
        packet_path = Path(packet_item["packet"])
        if file_sha256(price_path) != str(item["price_sha256"]):
            raise AtomicPacketError("frozen price source hash mismatch")
        if file_sha256(packet_path) != str(packet_item["packet_sha256"]):
            raise AtomicPacketError("frozen review packet hash mismatch")
        review_packet = _read_json(packet_path)
        anonymous_id = identities[code]
        rows, summary = build_stock_packets(
            price_frame=pd.read_csv(price_path),
            review_packet=review_packet,
            anonymous_stock_id=anonymous_id,
            identity_salt=salt,
            private_code=code,
            monitor_on=str(item["monitor_on"]),
            as_of=str(args.as_of or inputs["as_of"]),
            required_question_manifest=explicit_questions,
            atomic_schema_metadata=schema_metadata,
            legacy_dates=legacy_by_stock.get(anonymous_id, set()) if args.reuse_v1_boundary_dates else None,
            limit_review_points=args.limit_review_points,
        )
        packet_count += len(rows)
        if not args.dry_run:
            output_rows.extend(rows)
        stock_days += int(summary["stock_days_scanned"])
        review_points_before_lazy += int(summary["review_points_before_lazy"])
        review_points += int(summary["review_points"])
        all_event_counts.update(summary["objective_event_counts"])
        all_lazy_counts.update(summary["lazy_review_reason_counts"])
        all_skip_counts.update(summary["skip_reason_counts"])
        for packet in rows:
            all_packet_bytes.append(len(canonical_json_bytes(packet)))
            all_verdict_counts.append(
                len(packet["question_manifest"]["global_question_ids"])
                + sum(
                    len(entry["required_question_ids"])
                    for group in ("anchor_candidates", "relation_candidates", "stop_candidates")
                    for entry in packet["question_manifest"][group]
                )
            )
            all_candidate_counts["anchor"].append(len(packet["question_manifest"]["anchor_candidates"]))
            all_candidate_counts["relation"].append(len(packet["question_manifest"]["relation_candidates"]))
            all_candidate_counts["stop"].append(len(packet["question_manifest"]["stop_candidates"]))
            for scenario in SCENARIOS:
                all_hypothesis_counts[scenario].append(
                    len(packet["objective_facts"]["scenario_hypotheses"][scenario])
                )
    result = {
        "builder_version": BUILDER_VERSION,
        "builder_status": BUILDER_STATUS,
        "mode": "DRY_RUN" if args.dry_run else "LIMITED_WRITE",
        "formal_eligible": not args.reuse_v1_boundary_dates,
        "review_point_policy": "LEGACY_V1_BOUNDARY_DATES_BEHAVIOR_COMPARISON_ONLY"
        if args.reuse_v1_boundary_dates else FORMAL_REVIEW_POINT_POLICY,
        "as_of_ceiling": str(args.as_of or inputs["as_of"]),
        "causal_data_policy": "ALL_PACKET_EVIDENCE_DATES_AT_OR_BEFORE_AS_OF_NO_OUTCOME_FIELDS",
        "stocks_scanned": len(codes),
        "stock_days_scanned": stock_days,
        "review_points_before_lazy": review_points_before_lazy,
        "review_points": review_points,
        "packets": packet_count,
        "objective_event_counts": dict(sorted(all_event_counts.items())),
        "lazy_review_reason_counts": dict(sorted(all_lazy_counts.items())),
        "skip_reason_counts": dict(sorted(all_skip_counts.items())),
        "packet_bytes": {
            "average": round(sum(all_packet_bytes) / len(all_packet_bytes), 2) if all_packet_bytes else 0,
            "maximum": max(all_packet_bytes, default=0),
        },
        "required_verdict_counts": {
            "average": round(sum(all_verdict_counts) / len(all_verdict_counts), 2)
            if all_verdict_counts else 0,
            "maximum": max(all_verdict_counts, default=0),
        },
        "candidate_counts": {
            group: {
                "average": round(sum(values) / len(values), 2) if values else 0,
                "maximum": max(values, default=0),
            }
            for group, values in all_candidate_counts.items()
        },
        "scenario_hypothesis_counts": {
            scenario: {
                "average": round(sum(values) / len(values), 2) if values else 0,
                "maximum": max(values, default=0),
            }
            for scenario, values in all_hypothesis_counts.items()
        },
        "pivot_scale_disclosure": PIVOT_SCALE_DISCLOSURE,
        "input_manifest_sha256": file_sha256(args.input_manifest),
        "packet_manifest_sha256": file_sha256(args.packet_manifest),
        "identity_map_sha256": file_sha256(args.identity_map),
        "atomic_schema_sha256": file_sha256(args.atomic_schema),
    }
    if args.output and not args.dry_run:
        result["output"] = str(args.output.resolve())
        result["output_sha256"] = _write_jsonl(args.output, output_rows)
        manifest_path = args.output.with_suffix(".manifest.json")
        manifest_path.write_bytes(canonical_json_bytes(result) + b"\n")
        result["manifest"] = str(manifest_path.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
