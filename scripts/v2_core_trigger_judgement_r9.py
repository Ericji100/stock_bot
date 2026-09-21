"""R9C route-specific V2 trigger atoms and deterministic signal legality gate.

AI answers course gates using AS-OF evidence.  Program checks bindings,
confirmed pivot break, complete PASS gates and derives a signal candidate;
actual next-open fill/risk/portfolio performance are outside this stage.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from jsonschema import Draft202012Validator

from scripts.v2_core_legacy_role_qualification_r7 import _known_refs, transport_schema


VERSION = "v2-core-trigger-judgement-r9-candidate-r1"
SCENARIO_BY_RELATION = {
    "FRESH_FORMING_UP_ANCHOR": "FRESH_Q1_EXPANSION",
    "COMPLETED_MACRO_UP_PARENT": "MACRO_COPY_RESONANCE",
    "MATURE_UP_CAMPAIGN": "MATURE_TREND_PULLBACK",
    "ACTIVE_BEAR_CONTROL": "BEAR_REVERSAL_LEFT_RIGHT",
}
GATES = {
    "FRESH_Q1_EXPANSION": (
        "VALID_FRESH_UP_ANCHOR",
        "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE",
        "DYNAMIC_Q1_WITH_PRICE_CONTINUATION",
        "EARLY_TAIJI_GENERATION",
        "MACD_SUPPORT_NOT_SOLE_BASIS",
        "EARLY_LOCATION_AND_SPACE",
        "CAUSAL_EPISODE_DEFENSE",
    ),
    "MACRO_COPY_RESONANCE": (
        "COMPLETED_EFFECTIVE_UP_PARENT",
        "MACRO_CORRECTION_INTACT",
        "PARENT_CORRECTION_COPY_GENERATION_MAPPED",
        "CAUSAL_CORRECTION_BEAR_DOW_LINE",
        "SMALL_UP_REANCHOR_BREAK",
        "DUAL_SCALE_LONG_ALIGNMENT",
        "NOT_Q3_OR_EXHAUSTED",
        "CAUSAL_EPISODE_DEFENSE",
    ),
}
TRIGGER_PATHS = {
    "FRESH_Q1_EXPANSION": (
        "INITIAL_DESTRUCTIVE_EXPANSION", "FIRST_SHALLOW_CORRECTION_RELAUNCH",
    ),
    "MACRO_COPY_RESONANCE": (
        "BREAK_THEN_RETEST", "SMALL_REANCHOR_RELAUNCH", "DIRECT_TO_RIGHT",
    ),
}


def confirmed_high_options(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    as_of = packet["as_of"]
    as_of_date = date.fromisoformat(as_of)
    result = {}
    for pivot in packet["confirmed_pivots_to_as_of"]:
        if pivot["side"] != "HIGH" or pivot["confirmation_date"] > as_of or pivot["source_date"] >= as_of:
            continue
        if (as_of_date - date.fromisoformat(pivot["source_date"])).days > 120:
            continue
        item = {key: pivot[key] for key in ("scale", "source_date", "confirmation_date", "price")}
        pivot_id = "BRK-" + hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
        result[pivot_id] = {**item, "evidence_ref": f"TRIGGER_PIVOT:{pivot_id}"}
    return result


def build_schema(packet: dict[str, Any], defense_id: str, working_id: str, scenario: str) -> dict[str, Any]:
    if scenario not in GATES:
        raise ValueError("R9C scenario route not implemented in this two-path pilot")
    high_options = confirmed_high_options(packet)
    if not high_options:
        raise ValueError("no AS-OF confirmed high options")
    refs = sorted(set(_known_refs(packet)) | {row["evidence_ref"] for row in high_options.values()})
    ref_array = {
        "type": "array", "minItems": 1, "maxItems": 10, "uniqueItems": True,
        "items": {"type": "string", "enum": refs},
    }
    gate_item = {
        "type": "object", "additionalProperties": False,
        "required": ["judgement", "supporting_evidence_refs", "explanation"],
        "properties": {
            "judgement": {"type": "string", "enum": ["PASS", "FAIL", "UNKNOWN"]},
            "supporting_evidence_refs": ref_array,
            "explanation": {"type": "string", "minLength": 20, "maxLength": 700},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "review_id", "as_of", "bound_defense_candidate_id", "bound_working_anchor_id", "program_scenario", "gate_assessments", "proposed_trigger_path", "break_pivot_id", "trigger_evidence_refs", "trigger_reason", "causal_attestation"],
        "properties": {
            "schema_version": {"type": "string", "const": VERSION},
            "review_id": {"type": "string", "const": packet["review_id"]},
            "as_of": {"type": "string", "const": packet["as_of"]},
            "bound_defense_candidate_id": {"type": "string", "const": defense_id},
            "bound_working_anchor_id": {"type": "string", "const": working_id},
            "program_scenario": {"type": "string", "const": scenario},
            "gate_assessments": {
                "type": "object", "additionalProperties": False,
                "required": list(GATES[scenario]), "properties": {gate: gate_item for gate in GATES[scenario]},
            },
            "proposed_trigger_path": {"type": "string", "enum": [*TRIGGER_PATHS[scenario], "NONE"]},
            "break_pivot_id": {"type": "string", "enum": [*sorted(high_options), "NONE"]},
            "trigger_evidence_refs": ref_array,
            "trigger_reason": {"type": "string", "minLength": 50, "maxLength": 1000},
            "causal_attestation": {
                "type": "object", "additionalProperties": False,
                "required": ["as_of_only", "no_identity", "no_teacher_answer", "no_future_performance", "defense_and_anchor_not_changed", "no_actual_fill_or_portfolio_claim"],
                "properties": {key: {"type": "boolean", "const": True} for key in (
                    "as_of_only", "no_identity", "no_teacher_answer", "no_future_performance",
                    "defense_and_anchor_not_changed", "no_actual_fill_or_portfolio_claim",
                )},
            },
        },
    }


def validate_and_gate(
    response: dict[str, Any], *, packet: dict[str, Any], defense_id: str,
    working_id: str, scenario: str,
) -> dict[str, Any]:
    schema = build_schema(packet, defense_id, working_id, scenario)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda error: list(map(str, error.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    gates = {gate: response["gate_assessments"][gate]["judgement"] for gate in GATES[scenario]}
    path = response["proposed_trigger_path"]
    pivot_id = response["break_pivot_id"]
    errors = []
    if (path == "NONE") != (pivot_id == "NONE"):
        errors.append("trigger path and break pivot must both be NONE or both specified")
    high_options = confirmed_high_options(packet)
    break_pivot = high_options.get(pivot_id)
    if break_pivot:
        if break_pivot["evidence_ref"] not in response["trigger_evidence_refs"]:
            errors.append("trigger evidence lacks selected pivot ref")
    if errors:
        return {"status": "INVALID", "errors": errors}
    last = packet["daily_context_to_as_of"][-1]
    previous = packet["daily_context_to_as_of"][-2]
    if last["date"] != packet["as_of"]:
        return {"status": "INVALID", "errors": ["AS-OF day not last daily context"]}
    all_pass = all(value == "PASS" for value in gates.values())
    actual_price_break = bool(break_pivot and previous["close"] <= break_pivot["price"] < last["close"])
    if all_pass and path != "NONE" and actual_price_break:
        disposition = "TRIGGERED_SIGNAL_CANDIDATE"
    elif "FAIL" in gates.values() or (path != "NONE" and not actual_price_break):
        disposition = "NO_TRADE_GATE_FAIL"
    else:
        disposition = "WAIT_UNRESOLVED"
    return {
        "status": "VALID",
        "program_scenario": scenario,
        "gate_judgements": gates,
        "all_gates_pass": all_pass,
        "proposed_trigger_path": path,
        "break_pivot_id": None if pivot_id == "NONE" else pivot_id,
        "confirmed_break_pivot": break_pivot,
        "actual_price_break": actual_price_break,
        "signal_disposition": disposition,
        "bound_defense_candidate_id": defense_id,
        "bound_working_anchor_id": working_id,
        "actual_entry_or_fill_computed": False,
    }


def transport_for(packet: dict[str, Any], defense_id: str, working_id: str, scenario: str) -> dict[str, Any]:
    return transport_schema(build_schema(packet, defense_id, working_id, scenario))


def parse_raw_without_duplicate_keys(raw_bytes: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    result = json.loads(raw_bytes.decode("utf-8-sig"), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError("R9C trigger output root must be object")
    return result
