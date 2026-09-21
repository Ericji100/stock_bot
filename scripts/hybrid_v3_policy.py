"""Validation, V3 policy reduction, and consistency merge for the hybrid protocol."""
from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config/hybrid_common_structure_v1.schema.json"
PROTOCOL_PATH = ROOT / "config/hybrid_monitoring_protocol_v1.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def gate_map(semantic: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for gate in semantic.get("semantic_gates") or []:
        gate_id = str(gate.get("gate_id") or "")
        if gate_id in output:
            raise ValueError(f"duplicate semantic gate: {gate_id}")
        output[gate_id] = gate
    return output


def evidence_map(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["ref"]): item for item in packet.get("evidence") or []}


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def validate_semantic(packet: dict[str, Any], semantic: dict[str, Any]) -> list[str]:
    schema = read_json(SCHEMA_PATH)
    protocol = read_json(PROTOCOL_PATH)
    errors = [error.message for error in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(semantic)]
    if semantic.get("review_id") != packet.get("review_id"):
        errors.append("review_id differs from packet")
    if semantic.get("anonymous_stock_id") != packet.get("anonymous_stock_id"):
        errors.append("anonymous_stock_id differs from packet")
    if semantic.get("as_of") != packet.get("as_of"):
        errors.append("as_of differs from packet")
    attestation = semantic.get("causal_attestation") or {}
    if attestation.get("latest_visible_bar") != packet.get("as_of"):
        errors.append("latest_visible_bar differs from packet as_of")

    forbidden = {str(value).upper() for value in protocol["ai_boundary"]["forbidden_keys"]}
    for key in _walk_keys(semantic):
        if key.upper() in forbidden:
            errors.append(f"AI output contains forbidden key: {key}")

    refs = evidence_map(packet)
    used: list[str] = []
    for anchor in semantic.get("anchors") or []:
        used.append(str(anchor.get("start_ref") or ""))
        if anchor.get("end_ref") is not None:
            used.append(str(anchor["end_ref"]))
    for name in ("large", "small"):
        ref = ((semantic.get("scales") or {}).get(name) or {}).get("dow_defense_ref")
        if ref is not None:
            used.append(str(ref))
    try:
        gates = gate_map(semantic)
    except ValueError as exc:
        errors.append(str(exc))
        gates = {}
    for gate in gates.values():
        used.extend(str(ref) for ref in gate.get("evidence_refs") or [])
    for ref in used:
        if ref not in refs:
            errors.append(f"unknown evidence_ref: {ref}")
    return sorted(set(errors))


def _all(gates: dict[str, dict[str, Any]], names: Iterable[str], result: str = "PASS") -> bool:
    return all(name in gates and gates[name].get("result") == result for name in names)


def _results(gates: dict[str, dict[str, Any]], names: Iterable[str]) -> list[str]:
    return [str((gates.get(name) or {}).get("result") or "MISSING") for name in names]


def _semantic_blockers(semantic: dict[str, Any]) -> list[str]:
    flags = semantic["semantic_flags"]
    blockers = []
    if flags["q3"]:
        blockers.append("Q3")
    if flags["exhausted"] or semantic["stage_location"] == "EXHAUSTED":
        blockers.append("EXHAUSTED")
    if flags["scale_conflict"] or semantic["scales"]["relationship"] == "CONFLICT":
        blockers.append("SCALE_CONFLICT")
    if flags["single_indicator_only"]:
        blockers.append("SINGLE_INDICATOR_ONLY")
    for alt in semantic["alternative_scenarios"]:
        if alt["changes_direction"] or alt["changes_invalidation"] or alt["changes_position_role"]:
            blockers.append("MATERIAL_ALTERNATIVE")
    return blockers


def _objective(packet: dict[str, Any]) -> dict[str, Any]:
    evidence = evidence_map(packet)
    stop_refs = [ref for ref in packet.get("candidate_stop_refs") or [] if ref in evidence]
    latest_bar = evidence.get(f"BAR:{packet['as_of']}") or {}
    close = float((latest_bar.get("values") or {}).get("close") or 0)
    stop_ref = stop_refs[-1] if stop_refs else None
    stop_price = None if stop_ref is None else float(evidence[stop_ref]["values"]["price"])
    atr = float((latest_bar.get("values") or {}).get("atr14") or 0)
    stop_pct = None if stop_price is None or close <= 0 else (close - stop_price) / close * 100
    stop_atr = None if stop_price is None or atr <= 0 else (close - stop_price) / atr
    trigger = bool({"SMALL_CONTROL_BREAK", "LARGE_CONTROL_BREAK"}.intersection(packet.get("event_types") or []))
    data = packet.get("data_quality") or {}
    return {
        "active_watchlist": bool((packet.get("selection_asof") or {}).get("selected_before_or_on_day")),
        "preferred_750_met": bool(data.get("preferred_750_met")),
        "pre_monitor_bars": int(data.get("pre_monitor_bars") or 0),
        "trigger_completed": trigger,
        "stop_ref": stop_ref,
        "stop_price": stop_price,
        "signal_close": close,
        "stop_distance_pct": stop_pct,
        "stop_distance_atr": stop_atr,
        "risk_executable": bool(stop_price is not None and stop_price < close and stop_pct is not None and stop_atr is not None),
        "macro_defense_alert": "MACRO_DEFENSE_ALERT" in (packet.get("event_types") or []),
    }


def reduce_v3(packet: dict[str, Any], semantic: dict[str, Any]) -> dict[str, Any]:
    errors = validate_semantic(packet, semantic)
    if errors:
        return {"permission": "WAIT", "route": "NO_TRADE", "reason_codes": ["INVALID_AI_OUTPUT"], "validation_errors": errors}
    protocol = read_json(PROTOCOL_PATH)
    gates = gate_map(semantic)
    objective = _objective(packet)
    blockers = _semantic_blockers(semantic)
    primary = semantic["primary_scenario"]

    invalidated_large = any(
        anchor["role"] in {"PRIMARY", "PARENT"} and anchor["scale"] == "LARGE" and anchor["status"] == "INVALIDATED"
        for anchor in semantic["anchors"]
    )
    if objective["macro_defense_alert"] and invalidated_large:
        return {"permission": "REMOVE", "route": "NO_TRADE", "reason_codes": ["LARGE_CAMPAIGN_INVALIDATED"], "objective": objective}

    common_ok = (
        objective["active_watchlist"] and objective["trigger_completed"] and objective["risk_executable"]
        and not blockers and primary not in {"NO_TRADE", "UNRESOLVED"}
    )
    if not common_ok:
        codes = list(blockers)
        if not objective["trigger_completed"]:
            codes.append("TRIGGER_NOT_COMPLETED")
        if not objective["risk_executable"]:
            codes.append("RISK_NOT_EXECUTABLE")
        if not objective["active_watchlist"]:
            codes.append("WATCHLIST_INACTIVE")
        return {"permission": "WAIT", "route": "NO_TRADE", "reason_codes": codes or ["NO_ELIGIBLE_SCENARIO"], "objective": objective}

    required = protocol["v2_required_semantic_gates"].get(primary) or []
    if required and _all(gates, required):
        return {"permission": "TRADE", "route": "V2_CORE", "scenario": primary, "unknown_gate": None, "stop_ref": objective["stop_ref"], "objective": objective}

    route_name = {
        "MACRO_COPY_RESONANCE": "NEAR_PASS_MACRO_COPY",
        "FRESH_Q1_EXPANSION": "NEAR_PASS_FRESH_Q1",
        "BEAR_REVERSAL_LEFT_RIGHT": "BEAR_REVERSAL_PROBE",
    }.get(primary)
    if route_name:
        rule = protocol["v3_near_pass"][route_name]
        hard = rule["hard"]
        soft = rule["soft"]
        results = _results(gates, [*hard, *soft])
        unknowns = [name for name in soft if (gates.get(name) or {}).get("result") == "UNKNOWN"]
        if _all(gates, hard) and results.count("FAIL") == 0 and results.count("MISSING") == 0 and len(unknowns) == 1 and _all(gates, [name for name in soft if name != unknowns[0]]):
            if route_name == "BEAR_REVERSAL_PROBE":
                if unknowns != ["BEAR_LATE_STAGE_EVIDENCE"] or semantic["left_right_phase"] == "LL" or not semantic["semantic_flags"]["bear_late_stage_clues"]:
                    return {"permission": "WAIT", "route": "NO_TRADE", "reason_codes": ["BEAR_PROBE_RESIDUAL_INVALID"], "objective": objective}
            if route_name == "NEAR_PASS_FRESH_Q1":
                if semantic["stage_location"] != "EARLY" or (semantic["taiji"]["same_direction_attack_number"] or 99) > 2 or semantic["taiji"]["generation"] in {"COPY_LEG_5", "LATER_GENERATION"}:
                    return {"permission": "WAIT", "route": "NO_TRADE", "reason_codes": ["FRESH_Q1_RESIDUAL_INVALID"], "objective": objective}
            return {"permission": "TRADE", "route": route_name, "scenario": primary, "unknown_gate": unknowns[0], "stop_ref": objective["stop_ref"], "objective": objective}

    return {"permission": "WAIT", "route": "NO_TRADE", "reason_codes": ["V3_GATE_REQUIREMENTS_NOT_MET"], "objective": objective}


def _majority(values: list[Any], unresolved: Any) -> Any:
    counts = Counter(json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values)
    encoded, count = counts.most_common(1)[0]
    return json.loads(encoded) if count >= 2 else unresolved


def merge_three(packet: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    if len(runs) != 3:
        raise ValueError("exactly three consistency runs are required")
    for run in runs:
        errors = validate_semantic(packet, run)
        if errors:
            raise ValueError("cannot merge invalid semantic output: " + "; ".join(errors))
    merged = copy.deepcopy(runs[0])
    merged["primary_scenario"] = _majority([row["primary_scenario"] for row in runs], "UNRESOLVED")
    merged["left_right_phase"] = _majority([row["left_right_phase"] for row in runs], "UNRESOLVED")
    merged["stage_location"] = _majority([row["stage_location"] for row in runs], "UNRESOLVED")
    for field in ("q3", "exhausted", "scale_conflict", "single_indicator_only"):
        merged["semantic_flags"][field] = bool(_majority([row["semantic_flags"][field] for row in runs], True))
    all_gate_ids = sorted(set().union(*(set(gate_map(row)) for row in runs)))
    merged_gates = []
    for gate_id in all_gate_ids:
        candidates = [gate_map(row).get(gate_id) for row in runs]
        if any(value is None for value in candidates):
            available_refs = sorted(set().union(*(set((value or {}).get("evidence_refs") or []) for value in candidates)))
            merged_gates.append({"gate_id": gate_id, "result": "UNKNOWN", "evidence_refs": available_refs or [f"BAR:{packet['as_of']}"], "reason": "三次判讀未完整提交同一gate，保守降為UNKNOWN。"})
            continue
        results = [value["result"] for value in candidates if value]
        result = results[0] if len(set(results)) == 1 else "UNKNOWN"
        refs = sorted(set.intersection(*(set(value["evidence_refs"]) for value in candidates if value)))
        if not refs:
            refs = sorted(set().union(*(set(value["evidence_refs"]) for value in candidates if value)))
        merged_gates.append({"gate_id": gate_id, "result": result, "evidence_refs": refs, "reason": "三次一致。" if len(set(results)) == 1 else "三次結果不一致，依協定降為UNKNOWN。"})
    merged["semantic_gates"] = merged_gates
    merged["reason"] = "三次匿名獨立判讀依 HYBRID_MONITORING_PROTOCOL_V1 合併；有歧義欄位採保守 UNKNOWN。"
    return merged


def consistency_metrics(packets: dict[str, dict[str, Any]], runs: list[dict[str, dict[str, Any]]]) -> dict[str, Any]:
    if len(runs) != 3:
        raise ValueError("exactly three runs are required")
    ids = sorted(packets)
    valid = 0
    permission_same = 0
    scenario_phase_same = 0
    details = []
    for review_id in ids:
        rows = [run.get(review_id) for run in runs]
        errors = ["missing"] if any(row is None for row in rows) else sum((validate_semantic(packets[review_id], row) for row in rows if row), [])
        is_valid = not errors
        valid += int(is_valid)
        if is_valid:
            decisions = [reduce_v3(packets[review_id], row) for row in rows if row]
            permission_equal = len({row["permission"] for row in decisions}) == 1
            scenario_equal = len({row["primary_scenario"] for row in rows if row}) == 1 and len({row["left_right_phase"] for row in rows if row}) == 1
        else:
            permission_equal = False
            scenario_equal = False
        permission_same += int(permission_equal)
        scenario_phase_same += int(scenario_equal)
        details.append({"review_id": review_id, "valid": is_valid, "permission_same": permission_equal, "scenario_phase_same": scenario_equal, "errors": errors})
    denominator = len(ids) or 1
    return {
        "sample_rows": len(ids),
        "schema_and_causality": valid / denominator,
        "permission": permission_same / denominator,
        "scenario_and_phase": scenario_phase_same / denominator,
        "details": details,
    }
