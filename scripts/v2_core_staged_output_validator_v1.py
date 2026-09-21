"""Validate raw STAGE_B/STAGE_D AI outputs and derive deterministic results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from scripts.v2_core_m2a_contracts_v1 import derive_permission, derive_scenario
from scripts.v2_core_source_audit_v1 import sha256


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def schema_errors(record: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    return sorted(
        f"SCHEMA:{'.'.join(str(part) for part in error.absolute_path) or '$'}:{error.message}"
        for error in validator.iter_errors(record)
    )


def evidence_refs(packet: dict[str, Any]) -> set[str]:
    return {str(item["ref"]) for item in packet.get("evidence_catalog") or []}


def submitted_evidence_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("evidence_refs") and isinstance(child, list):
                yield from (str(item) for item in child)
            else:
                yield from submitted_evidence_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from submitted_evidence_refs(child)


def expected_overall(gates: dict[str, Any]) -> str:
    results = [str((gate or {}).get("result")) for gate in gates.values()]
    if any(result == "FAIL" for result in results):
        return "FAIL"
    if any(result == "UNKNOWN" for result in results):
        return "UNKNOWN"
    return "PASS" if results and all(result == "PASS" for result in results) else "UNKNOWN"


def metadata_errors(
    record: dict[str, Any],
    *,
    packet: dict[str, Any],
    packet_path: Path,
    schema_path: Path,
    prompt_path: Path,
) -> list[str]:
    errors: list[str] = []
    expected = {
        "review_id": packet.get("review_id"),
        "anonymous_stock_id": packet.get("anonymous_stock_id"),
        "as_of": packet.get("as_of"),
        "packet_sha256": sha256(packet_path),
        "schema_sha256": sha256(schema_path),
        "prompt_sha256": sha256(prompt_path),
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
    }
    for field, expected_value in expected.items():
        if record.get(field) != expected_value:
            errors.append(f"METADATA:{field}:MISMATCH")
    if (record.get("causal_attestation") or {}).get("latest_visible_date") != packet.get(
        "as_of"
    ):
        errors.append("CAUSAL:LATEST_VISIBLE_DATE_MUST_EQUAL_AS_OF")
    unknown_refs = sorted(set(submitted_evidence_refs(record)) - evidence_refs(packet))
    errors.extend(f"EVIDENCE:UNKNOWN_REF:{ref}" for ref in unknown_refs)
    return errors


def validate_stage_b(
    record: dict[str, Any],
    *,
    packet_path: Path,
    schema_path: Path,
    prompt_path: Path,
    truth_table_path: Path,
) -> dict[str, Any]:
    packet = load_json(packet_path)
    schema = load_json(schema_path)
    truth = load_json(truth_table_path)
    errors = schema_errors(record, schema)
    if errors:
        return {
            "status": "SCHEMA_INVALID",
            "errors": errors,
            "program_derived_scenario": "UNRESOLVED_NO_TRADE",
            "routing_status": "INVALID_STAGE_B",
        }
    errors.extend(
        metadata_errors(
            record,
            packet=packet,
            packet_path=packet_path,
            schema_path=schema_path,
            prompt_path=prompt_path,
        )
    )
    as_of = str(record["as_of"])
    anchors = record["anchors"]
    stops = record["stops"]
    anchor_ids = [str(anchor["anchor_id"]) for anchor in anchors]
    stop_ids = [str(stop["stop_id"]) for stop in stops]
    if len(anchor_ids) != len(set(anchor_ids)):
        errors.append("SEMANTIC:ANCHOR_IDS_NOT_UNIQUE")
    if len(stop_ids) != len(set(stop_ids)):
        errors.append("SEMANTIC:STOP_IDS_NOT_UNIQUE")
    if sum(anchor["role"] == "CONTROLLING" for anchor in anchors) != 1:
        errors.append("SEMANTIC:EXACTLY_ONE_CONTROLLING_ANCHOR_REQUIRED")
    for anchor in anchors:
        if str(anchor["start_date"]) > as_of or (
            anchor["end_date"] is not None and str(anchor["end_date"]) > as_of
        ):
            errors.append(f"CAUSAL:{anchor['anchor_id']}:ANCHOR_DATE_AFTER_AS_OF")
        if anchor["status"] == "FORMING" and (
            anchor["end_date"] is not None or anchor["end_price"] is not None
        ):
            errors.append(f"SEMANTIC:{anchor['anchor_id']}:FORMING_ANCHOR_HAS_END")
        if anchor["status"] == "CONFIRMED" and (
            anchor["end_date"] is None or anchor["end_price"] is None
        ):
            errors.append(f"SEMANTIC:{anchor['anchor_id']}:CONFIRMED_ANCHOR_MISSING_END")
        if anchor["invalidation_stop_id"] is not None and anchor[
            "invalidation_stop_id"
        ] not in stop_ids:
            errors.append(f"REFERENCE:{anchor['anchor_id']}:UNKNOWN_STOP")
    for stop in stops:
        if str(stop["source_date"]) > as_of or str(stop["confirmed_on"]) > as_of:
            errors.append(f"CAUSAL:{stop['stop_id']}:STOP_DATE_AFTER_AS_OF")
        if str(stop["confirmed_on"]) < str(stop["source_date"]):
            errors.append(f"CAUSAL:{stop['stop_id']}:CONFIRMED_BEFORE_SOURCE")
    for relation in record["relations"]:
        if relation["from_anchor_id"] not in anchor_ids or relation[
            "to_anchor_id"
        ] not in anchor_ids:
            errors.append(f"REFERENCE:{relation['relation_id']}:UNKNOWN_ANCHOR")
    shared = record["shared_structure"]
    if shared["controlling_anchor_id"] not in anchor_ids:
        errors.append("REFERENCE:SHARED_CONTROLLING_ANCHOR_UNKNOWN")
    if any(item not in anchor_ids for item in shared["alternative_anchor_ids"]):
        errors.append("REFERENCE:SHARED_ALTERNATIVE_ANCHOR_UNKNOWN")
    for scale in ("large_scale", "small_scale"):
        stop_id = shared[scale]["controlling_stop_id"]
        if stop_id is not None and stop_id not in stop_ids:
            errors.append(f"REFERENCE:{scale}:UNKNOWN_STOP")

    route_values = {
        atom: str(payload["result"])
        for atom, payload in record["routing_atoms"].items()
    }
    scenario, routing_status = derive_scenario(route_values, truth)
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": sorted(set(errors)),
        "program_derived_scenario": scenario,
        "routing_status": routing_status,
        "ai_recommended_scenario": record["ai_recommended_scenario"],
        "recommendation_matches_program": record["ai_recommended_scenario"] == scenario,
    }


def validate_stage_d(
    record: dict[str, Any],
    *,
    packet_path: Path,
    stage_b_path: Path,
    stage_b_schema_path: Path,
    stage_b_prompt_path: Path,
    schema_path: Path,
    prompt_path: Path,
    truth_table_path: Path,
) -> dict[str, Any]:
    packet = load_json(packet_path)
    stage_b = load_json(stage_b_path)
    schema = load_json(schema_path)
    truth = load_json(truth_table_path)
    errors = schema_errors(record, schema)
    if errors:
        return {
            "status": "SCHEMA_INVALID",
            "errors": errors,
            "program_derived_permission": "INVALID",
        }
    errors.extend(
        metadata_errors(
            record,
            packet=packet,
            packet_path=packet_path,
            schema_path=schema_path,
            prompt_path=prompt_path,
        )
    )
    if record["stage_b_output_sha256"] != sha256(stage_b_path):
        errors.append("METADATA:stage_b_output_sha256:MISMATCH")
    if record["truth_table_sha256"] != sha256(truth_table_path):
        errors.append("METADATA:truth_table_sha256:MISMATCH")

    stage_b_result = validate_stage_b(
        stage_b,
        packet_path=packet_path,
        schema_path=stage_b_schema_path,
        prompt_path=stage_b_prompt_path,
        truth_table_path=truth_table_path,
    )
    if stage_b_result["status"] != "PASS":
        errors.append("DEPENDENCY:STAGE_B_NOT_VALID")
    primary = str(record["primary_scenario"])
    if primary != stage_b_result["program_derived_scenario"]:
        errors.append("SEMANTIC:PRIMARY_SCENARIO_NOT_PROGRAM_ROUTE")
    evaluation = record["scenario_evaluation"]
    if str(evaluation["scenario"]) != primary:
        errors.append("SEMANTIC:EVALUATION_SCENARIO_MISMATCH")
    derived_overall = expected_overall(evaluation["gates"])
    if str(evaluation["overall"]) != derived_overall:
        errors.append("SEMANTIC:SCENARIO_OVERALL_MISMATCH")

    route = str(record["trigger"]["canonical_route"])
    prefix = {
        "MATURE_TREND_PULLBACK": "MATURE_",
        "MACRO_COPY_RESONANCE": "MACRO_",
        "BEAR_REVERSAL_LEFT_RIGHT": "BEAR_",
        "FRESH_Q1_EXPANSION": "FRESH_",
    }[primary]
    if route != "UNRESOLVED_ROUTE" and not route.startswith(prefix):
        errors.append("SEMANTIC:TRIGGER_ROUTE_PREFIX_MISMATCH")
    trigger = record["trigger"]
    if trigger["trigger_date"] is not None and str(trigger["trigger_date"]) > str(
        record["as_of"]
    ):
        errors.append("CAUSAL:TRIGGER_DATE_AFTER_AS_OF")
    if trigger["status"] == "TRIGGERED" and trigger["trigger_date"] != record["as_of"]:
        errors.append("SEMANTIC:TRIGGERED_DATE_MUST_EQUAL_AS_OF")
    stop_ids = {str(stop["stop_id"]) for stop in stage_b["stops"]}
    if trigger["episode_stop_id"] is not None and trigger["episode_stop_id"] not in stop_ids:
        errors.append("REFERENCE:UNKNOWN_EPISODE_STOP")
    if trigger["campaign_stop_id"] is not None and trigger["campaign_stop_id"] not in stop_ids:
        errors.append("REFERENCE:UNKNOWN_CAMPAIGN_STOP")

    if errors:
        return {
            "status": "FAIL",
            "errors": sorted(set(errors)),
            "program_derived_permission": "INVALID",
        }
    stop_gate = {
        "MATURE_TREND_PULLBACK": "EPISODE_STOP_CAUSAL",
        "MACRO_COPY_RESONANCE": "EPISODE_STOP_CAUSAL",
        "BEAR_REVERSAL_LEFT_RIGHT": "PHASE_STOP_CAUSAL",
        "FRESH_Q1_EXPANSION": "FRESH_ANCHOR_STOP_CAUSAL",
    }[primary]
    permission, reasons = derive_permission(
        truth=truth,
        primary_scenario=primary,
        gate_results={
            gate: str(payload["result"])
            for gate, payload in evaluation["gates"].items()
        },
        data_sufficiency=str(stage_b["data_sufficiency"]["result"]),
        trigger_status=str(trigger["status"]),
        trigger_date_equals_as_of=trigger["trigger_date"] == record["as_of"],
        episode_stop_causal=str(evaluation["gates"][stop_gate]["result"]),
        location_remaining_space=str(record["location_remaining_space"]["result"]),
        signal_has_independent_structure=str(
            record["signal_has_independent_structure"]["result"]
        ),
        blockers_absent={
            name: str(payload["result"])
            for name, payload in record["blocking_atoms"].items()
        },
        alternative_conflict=stage_b_result["routing_status"]
        == "ADJUDICATION_REQUIRED",
    )
    return {
        "status": "PASS",
        "errors": [],
        "program_derived_permission": permission,
        "permission_reasons": reasons,
        "ai_recommended_permission": record["ai_recommended_permission"],
        "recommendation_matches_program": record["ai_recommended_permission"]
        == permission,
    }
