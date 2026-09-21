"""R10B V2 trigger atoms with separately bound broad/tactical/defense roles.

Program checks AS-OF prices, object bindings, references and Boolean gate
composition.  AI still decides course semantics; no fixed RR or ATR threshold is
silently introduced.  No next-open fill or portfolio outcome is computed.
"""

from __future__ import annotations

import copy
from datetime import date
import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts import v2_core_trigger_judgement_r9d as r9d
from scripts.v2_core_legacy_role_qualification_r7 import transport_schema


VERSION = "v2-core-trigger-judgement-r10b-candidate-r1"
SCENARIOS = ("FRESH_Q1_EXPANSION", "MACRO_COPY_RESONANCE")
GENERATIONS = ("ANCHOR_LEG_1", "CORRECTION", "COPY_LEG_3", "COPY_LEG_5", "LATER_GENERATION", "UNRESOLVED")
QUADRANTS = ("Q1", "Q2", "Q3", "Q4", "UNKNOWN")
confirmed_high_options = r9d.confirmed_high_options
parse_raw_without_duplicate_keys = r9d.parse_raw_without_duplicate_keys


def obstacle_high_options(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    close = packet["daily_context_to_as_of"][-1]["close"]
    as_of = packet["as_of"]
    rows = []
    for pivot in packet["confirmed_pivots_to_as_of"]:
        if pivot["side"] != "HIGH" or pivot["confirmation_date"] > as_of or pivot["source_date"] >= as_of:
            continue
        if pivot["price"] <= close:
            continue
        item = {key: pivot[key] for key in ("scale", "source_date", "confirmation_date", "price")}
        pivot_id = "OBS-" + hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
        rows.append((item["price"] - close, pivot_id, item))
    rows.sort(key=lambda row: (row[0], row[2]["source_date"]))
    return {pivot_id: {**item, "distance_from_close": round(distance, 4)} for distance, pivot_id, item in rows[:30]}


def objective_risk_context(packet: dict[str, Any], defense_row: dict[str, Any]) -> dict[str, Any]:
    last = packet["daily_context_to_as_of"][-1]
    close = float(last["close"])
    stop = float(defense_row["price"])
    atr = float(last["atr14"])
    if stop >= close or atr <= 0:
        raise ValueError("R10B invalid episode defense/risk context")
    return {
        "signal_close": close,
        "frozen_episode_stop": stop,
        "stop_source_date": defense_row["source_date"],
        "stop_confirmation_date": defense_row["confirmation_date"],
        "close_to_stop_absolute": round(close - stop, 4),
        "close_to_stop_percent": round(100 * (close - stop) / close, 4),
        "close_to_stop_atr": round((close - stop) / atr, 4),
        "atr14": atr,
        "not_an_execution_price": True,
    }


def build_schema(packet: dict[str, Any], defense_id: str, broad_id: str, tactical_id: str, scenario: str) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError("R10B pilot scenario unsupported")
    schema = copy.deepcopy(r9d.build_schema(packet, defense_id, broad_id, scenario))
    schema["properties"]["schema_version"]["const"] = VERSION
    schema["required"].extend((
        "bound_tactical_cycle_anchor_id", "taiji_generation", "large_quadrant", "small_quadrant",
        "correction_pressure_pivot_id", "nearest_meaningful_obstacle_id", "space_and_generation_reason",
    ))
    schema["properties"].update({
        "bound_tactical_cycle_anchor_id": {"type": "string", "const": tactical_id},
        "taiji_generation": {"type": "string", "enum": list(GENERATIONS)},
        "large_quadrant": {"type": "string", "enum": list(QUADRANTS)},
        "small_quadrant": {"type": "string", "enum": list(QUADRANTS)},
        "correction_pressure_pivot_id": {"type": "string", "enum": [*sorted(confirmed_high_options(packet)), "NONE"]},
        "nearest_meaningful_obstacle_id": {"type": "string", "enum": [*sorted(obstacle_high_options(packet)), "NONE"]},
        "space_and_generation_reason": {"type": "string", "minLength": 100, "maxLength": 1800},
    })
    return schema


def validate_and_gate(
    response: dict[str, Any], *, packet: dict[str, Any], defense_id: str,
    defense_row: dict[str, Any], broad_id: str, tactical_id: str, scenario: str,
) -> dict[str, Any]:
    schema = build_schema(packet, defense_id, broad_id, tactical_id, scenario)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda error: list(map(str, error.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    verification_copy = copy.deepcopy(response)
    for key in (
        "bound_tactical_cycle_anchor_id", "taiji_generation", "large_quadrant", "small_quadrant",
        "correction_pressure_pivot_id", "nearest_meaningful_obstacle_id", "space_and_generation_reason",
    ):
        verification_copy.pop(key)
    verification_copy["schema_version"] = r9d.VERSION
    inherited = r9d.validate_and_gate(
        verification_copy, packet=packet, defense_id=defense_id,
        working_id=broad_id, scenario=scenario,
    )
    if inherited["status"] != "VALID":
        return inherited
    pressure = response["correction_pressure_pivot_id"]
    obstacle = response["nearest_meaningful_obstacle_id"]
    path = response["proposed_trigger_path"]
    errors = []
    if scenario == "MACRO_COPY_RESONANCE" and path != "NONE" and pressure == "NONE":
        errors.append("macro triggered path requires a confirmed correction pressure pivot")
    if scenario == "FRESH_Q1_EXPANSION" and pressure != "NONE":
        errors.append("fresh trigger cannot claim a macro correction pressure pivot")
    if response["taiji_generation"] == "UNRESOLVED" and inherited["signal_disposition"] == "TRIGGERED_SIGNAL_CANDIDATE":
        errors.append("unresolved Taiji generation cannot trigger")
    if "Q3" in (response["large_quadrant"], response["small_quadrant"]) and inherited["signal_disposition"] == "TRIGGERED_SIGNAL_CANDIDATE":
        errors.append("Q3 cannot trigger")
    if errors:
        return {"status": "INVALID", "errors": errors}
    risk = objective_risk_context(packet, defense_row)
    obstacle_row = obstacle_high_options(packet).get(obstacle)
    if obstacle_row:
        gap = obstacle_row["price"] - risk["signal_close"]
        reward_to_risk = gap / risk["close_to_stop_absolute"]
    else:
        reward_to_risk = None
    return {
        **inherited,
        "bound_tactical_cycle_anchor_id": tactical_id,
        "taiji_generation": response["taiji_generation"],
        "large_quadrant": response["large_quadrant"],
        "small_quadrant": response["small_quadrant"],
        "correction_pressure_pivot_id": None if pressure == "NONE" else pressure,
        "nearest_meaningful_obstacle_id": None if obstacle == "NONE" else obstacle,
        "obstacle_row": obstacle_row,
        "objective_risk_context": risk,
        "reward_to_selected_obstacle_over_initial_risk": None if reward_to_risk is None else round(reward_to_risk, 4),
        "fixed_reward_to_risk_threshold_applied": False,
        "raw_response_unchanged": True,
    }


def transport_for(packet: dict[str, Any], defense_id: str, broad_id: str, tactical_id: str, scenario: str) -> dict[str, Any]:
    return transport_schema(build_schema(packet, defense_id, broad_id, tactical_id, scenario))
