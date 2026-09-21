"""Validate candidate V2 R1 output beyond what JSON Schema can express."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


SCENARIOS = (
    "MATURE_TREND_PULLBACK",
    "MACRO_COPY_RESONANCE",
    "BEAR_REVERSAL_LEFT_RIGHT",
    "FRESH_Q1_EXPANSION",
)
ROUTE_PREFIX = {
    "MATURE_TREND_PULLBACK": "MATURE_",
    "MACRO_COPY_RESONANCE": "MACRO_",
    "BEAR_REVERSAL_LEFT_RIGHT": "BEAR_",
    "FRESH_Q1_EXPANSION": "FRESH_",
}


def path_text(parts: list[Any]) -> str:
    return ".".join(str(part) for part in parts) or "$"


def expected_overall(gates: dict[str, Any]) -> str:
    results = [str((payload or {}).get("result")) for payload in gates.values()]
    if any(result == "FAIL" for result in results):
        return "FAIL"
    if any(result == "UNKNOWN" for result in results):
        return "UNKNOWN"
    return "PASS" if results and all(result == "PASS" for result in results) else "UNKNOWN"


def validate_record(record: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    errors = [
        f"SCHEMA:{path_text(list(error.absolute_path))}:{error.message}"
        for error in Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(record)
    ]
    if errors:
        return sorted(errors)

    as_of = str(record["as_of"])
    anchors = record["anchors"]
    stops = record["stops"]
    anchor_ids = [anchor["anchor_id"] for anchor in anchors]
    stop_ids = [stop["stop_id"] for stop in stops]
    if len(anchor_ids) != len(set(anchor_ids)):
        errors.append("SEMANTIC:ANCHOR_IDS_NOT_UNIQUE")
    if len(stop_ids) != len(set(stop_ids)):
        errors.append("SEMANTIC:STOP_IDS_NOT_UNIQUE")
    if sum(anchor["role"] == "CONTROLLING" for anchor in anchors) != 1:
        errors.append("SEMANTIC:EXACTLY_ONE_CONTROLLING_ANCHOR_REQUIRED")

    stop_set = set(stop_ids)
    anchor_set = set(anchor_ids)
    for anchor in anchors:
        if anchor["start_date"] > as_of or (anchor["end_date"] and anchor["end_date"] > as_of):
            errors.append(f"CAUSAL:{anchor['anchor_id']}:ANCHOR_DATE_AFTER_AS_OF")
        if anchor["status"] == "FORMING" and (anchor["end_date"] is not None or anchor["end_price"] is not None):
            errors.append(f"SEMANTIC:{anchor['anchor_id']}:FORMING_ANCHOR_MUST_HAVE_NULL_END")
        if anchor["status"] == "CONFIRMED" and (anchor["end_date"] is None or anchor["end_price"] is None):
            errors.append(f"SEMANTIC:{anchor['anchor_id']}:CONFIRMED_ANCHOR_REQUIRES_END")
        if anchor["invalidation_stop_id"] is not None and anchor["invalidation_stop_id"] not in stop_set:
            errors.append(f"REFERENCE:{anchor['anchor_id']}:UNKNOWN_INVALIDATION_STOP")

    for stop in stops:
        if stop["source_date"] > as_of or stop["confirmed_on"] > as_of:
            errors.append(f"CAUSAL:{stop['stop_id']}:STOP_DATE_AFTER_AS_OF")
        if stop["confirmed_on"] < stop["source_date"]:
            errors.append(f"CAUSAL:{stop['stop_id']}:CONFIRMED_BEFORE_SOURCE")
        if not stop["supporting_evidence_refs"]:
            errors.append(f"EVIDENCE:{stop['stop_id']}:STOP_EVIDENCE_REQUIRED")

    for relation in record["relations"]:
        if relation["from_anchor_id"] not in anchor_set or relation["to_anchor_id"] not in anchor_set:
            errors.append(f"REFERENCE:{relation['relation_id']}:UNKNOWN_ANCHOR")

    shared = record["shared_structure"]
    if shared["controlling_anchor_id"] not in anchor_set:
        errors.append("REFERENCE:SHARED_STRUCTURE_UNKNOWN_CONTROLLING_ANCHOR")
    if any(anchor_id not in anchor_set for anchor_id in shared["alternative_anchor_ids"]):
        errors.append("REFERENCE:SHARED_STRUCTURE_UNKNOWN_ALTERNATIVE_ANCHOR")
    for scale in ("large_scale", "small_scale"):
        stop_id = shared[scale]["controlling_stop_id"]
        if stop_id is not None and stop_id not in stop_set:
            errors.append(f"REFERENCE:{scale}:UNKNOWN_CONTROLLING_STOP")

    metrics = shared["location_metrics"]
    metric_stop_id = metrics["episode_stop_id"]
    if metric_stop_id is not None and metric_stop_id not in stop_set:
        errors.append("REFERENCE:LOCATION_METRICS_UNKNOWN_EPISODE_STOP")
    resistance_date = metrics["nearest_overhead_resistance_date"]
    if resistance_date and resistance_date > as_of:
        errors.append("CAUSAL:OVERHEAD_RESISTANCE_DATE_AFTER_AS_OF")
    risk = metrics["episode_risk_points"]
    remaining = metrics["remaining_space_points"]
    ratio = metrics["space_to_risk"]
    close = float(metrics["signal_close"])
    resistance = metrics["nearest_overhead_resistance_price"]
    if metric_stop_id in stop_set:
        metric_stop = next(item for item in stops if item["stop_id"] == metric_stop_id)
        expected_risk = close - float(metric_stop["price"])
        if risk is None or abs(float(risk) - expected_risk) > 0.0002:
            errors.append("SEMANTIC:EPISODE_RISK_POINTS_ARITHMETIC_MISMATCH")
    if resistance is not None:
        expected_remaining = float(resistance) - close
        if remaining is None or abs(float(remaining) - expected_remaining) > 0.0002:
            errors.append("SEMANTIC:REMAINING_SPACE_POINTS_ARITHMETIC_MISMATCH")
    if risk is not None and remaining is not None:
        expected_ratio = float(remaining) / float(risk)
        if ratio is None or abs(float(ratio) - expected_ratio) > 0.0002:
            errors.append("SEMANTIC:SPACE_TO_RISK_ARITHMETIC_MISMATCH")
    if not metrics["pressure_broken_on_as_of"] and ratio is not None and float(ratio) < 1.0:
        if shared["location_remaining_space"]["result"] == "PASS":
            errors.append("SEMANTIC:SPACE_BELOW_1R_CANNOT_PASS")

    evaluations = record["scenario_evaluations"]
    for scenario in SCENARIOS:
        evaluation = evaluations[scenario]
        derived = expected_overall(evaluation["gates"])
        if evaluation["overall"] != derived:
            errors.append(f"SEMANTIC:{scenario}:OVERALL_{evaluation['overall']}_BUT_GATES_{derived}")

    decision = record["decision"]
    primary = decision["primary_scenario"]
    secondary = decision["secondary_scenarios"]
    if primary in secondary:
        errors.append("SEMANTIC:PRIMARY_SCENARIO_REPEATED_AS_SECONDARY")
    if primary == "UNRESOLVED_NO_TRADE" and decision["permission"] == "TRADE_APPROVED":
        errors.append("SEMANTIC:UNRESOLVED_SCENARIO_CANNOT_TRADE")
    if primary in SCENARIOS and evaluations[primary]["overall"] != "PASS" and decision["permission"] == "TRADE_APPROVED":
        errors.append("SEMANTIC:TRADE_REQUIRES_PRIMARY_SCENARIO_PASS")
    if primary in SCENARIOS and decision["canonical_trigger_route"] != "UNRESOLVED_ROUTE":
        if not decision["canonical_trigger_route"].startswith(ROUTE_PREFIX[primary]):
            errors.append("SEMANTIC:ROUTE_PREFIX_DOES_NOT_MATCH_PRIMARY_SCENARIO")

    if decision["permission"] == "TRADE_APPROVED":
        if record["data_sufficiency"]["result"] != "PASS":
            errors.append("SEMANTIC:TRADE_REQUIRES_DATA_SUFFICIENCY_PASS")
        if decision["alternative_conflict"]:
            errors.append("SEMANTIC:TRADE_FORBIDDEN_WHEN_ALTERNATIVE_CONFLICTS")
        if decision["trigger_status"] != "TRIGGERED" or decision["trigger_date"] != as_of:
            errors.append("SEMANTIC:TRADE_REQUIRES_AS_OF_CLOSE_TRIGGER")
        if decision["episode_stop_id"] not in stop_set:
            errors.append("REFERENCE:TRADE_REQUIRES_KNOWN_EPISODE_STOP")
        else:
            stop = next(item for item in stops if item["stop_id"] == decision["episode_stop_id"])
            if stop["scope"] not in {"TRADE_EPISODE", "BEAR_PHASE"}:
                errors.append("SEMANTIC:TRADE_STOP_HAS_WRONG_SCOPE")
        if metric_stop_id != decision["episode_stop_id"]:
            errors.append("SEMANTIC:LOCATION_METRICS_STOP_MUST_MATCH_DECISION_STOP")
        if shared["location_remaining_space"]["result"] != "PASS":
            errors.append("SEMANTIC:TRADE_REQUIRES_REMAINING_SPACE_PASS")

    lifecycle = record["position_lifecycle"]
    if lifecycle["position_role"] in {"ADD_1", "ADD_2"}:
        expected_add = 1 if lifecycle["position_role"] == "ADD_1" else 2
        if lifecycle["existing_position_profitable"] != "YES" or lifecycle["add_number"] != expected_add:
            errors.append("SEMANTIC:ADD_REQUIRES_PROFIT_AND_MATCHING_ADD_NUMBER")
    elif lifecycle["add_number"] is not None:
        errors.append("SEMANTIC:NON_ADD_ROLE_MUST_NOT_HAVE_ADD_NUMBER")

    attestation = record["causal_attestation"]
    if attestation["latest_visible_date"] != as_of:
        errors.append("CAUSAL:LATEST_VISIBLE_DATE_MUST_EQUAL_AS_OF")
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args()
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    record = json.loads(args.record.read_text(encoding="utf-8"))
    errors = validate_record(record, schema)
    print(json.dumps({"status": "PASS" if not errors else "FAIL", "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
