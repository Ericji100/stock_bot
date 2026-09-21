"""Validate and execute the deterministic M2A candidate contracts.

The module never creates subjective course atoms.  It consumes AI-supplied
PASS/FAIL/UNKNOWN atoms, applies the versioned route table, and derives the
only legal course permission before the separate execution engine runs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)
VERDICTS = {"PASS", "FAIL", "UNKNOWN"}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def scenario_gates_from_candidate_schema(schema: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    evaluations = schema["$defs"]["scenarioEvaluations"]
    for scenario in evaluations["required"]:
        ref = evaluations["properties"][scenario]["$ref"].split("/")[-1]
        gate_schema = schema["$defs"][ref]["allOf"][1]["properties"]["gates"]
        result[scenario] = list(gate_schema["required"])
    return result


def validate_artifacts(artifact_dir: Path = ARTIFACT_DIR) -> list[str]:
    errors: list[str] = []
    dictionary = load_json(artifact_dir / "subjective_term_evidence_dictionary.json")
    truth = load_json(artifact_dir / "permission_truth_table.json")
    policy = load_json(artifact_dir / "model_execution_policy.json")
    schema = load_json(artifact_dir / "v2_core_reproducible_r1.schema.candidate.json")
    stage_b_schema = load_json(artifact_dir / "v2_core_stage_b.schema.candidate.json")
    stage_d_schema = load_json(artifact_dir / "v2_core_stage_d.schema.candidate.json")
    stage_b_prompt = (artifact_dir / "v2_core_stage_b.prompt.candidate.md").read_text(
        encoding="utf-8"
    )
    stage_d_prompt = (artifact_dir / "v2_core_stage_d.prompt.candidate.md").read_text(
        encoding="utf-8"
    )
    staged_contract = (artifact_dir / "staged_prompt_contract.md").read_text(
        encoding="utf-8"
    )

    for name, candidate in (
        ("LEGACY_CANDIDATE", schema),
        ("STAGE_B", stage_b_schema),
        ("STAGE_D", stage_d_schema),
    ):
        try:
            Draft202012Validator.check_schema(candidate)
        except Exception as exc:  # pragma: no cover - exact library wording varies
            errors.append(f"SCHEMA:{name}:INVALID:{exc}")

    formal = policy.get("formal_ai") or {}
    if formal.get("model") != "gpt-5.6-sol":
        errors.append("MODEL_POLICY:FORMAL_MODEL_MUST_BE_GPT_5_6_SOL")
    if formal.get("reasoning_effort") != "xhigh":
        errors.append("MODEL_POLICY:FORMAL_REASONING_MUST_BE_XHIGH")
    if (policy.get("budget_policy") or {}).get(
        "stop_new_ai_calls_when_remaining_percent_lte"
    ) != 5:
        errors.append("MODEL_POLICY:BUDGET_STOP_MUST_BE_REMAINING_5_PERCENT")

    terms = dictionary.get("terms") or []
    term_ids = [str(term.get("term_id")) for term in terms]
    if len(term_ids) != len(set(term_ids)):
        errors.append("EVIDENCE_DICTIONARY:TERM_IDS_NOT_UNIQUE")
    required_terms = {
        "ANCHOR_CLEAN",
        "ANCHOR_MEATY",
        "ANCHOR_DESTRUCTIVE",
        "TREND_WEAKENED",
        "INTERNAL_TOO_MESSY",
        "TIME_SUFFICIENT",
        "STOP_NEARBY",
        "EARLY_STAGE",
        "SPACE_ADEQUATE",
        "RISK_REWARD_DAMAGED",
    }
    if set(term_ids) != required_terms:
        errors.append("EVIDENCE_DICTIONARY:REQUIRED_TERM_SET_MISMATCH")
    for term in terms:
        term_id = str(term.get("term_id"))
        proxies = term.get("proxy_evidence") or []
        if not 2 <= len(proxies) <= 3:
            errors.append(f"EVIDENCE_DICTIONARY:{term_id}:REQUIRES_2_TO_3_PROXIES")
        proxy_fields = [str(proxy.get("field")) for proxy in proxies]
        if len(proxy_fields) != len(set(proxy_fields)):
            errors.append(f"EVIDENCE_DICTIONARY:{term_id}:PROXY_FIELDS_NOT_UNIQUE")
        for field in (
            "course_semantics",
            "subjective_remainder",
            "pass_minimum",
            "fail_minimum",
            "unknown_minimum",
        ):
            if not str(term.get(field) or "").strip():
                errors.append(f"EVIDENCE_DICTIONARY:{term_id}:MISSING_{field.upper()}")

    schema_gates = scenario_gates_from_candidate_schema(schema)
    truth_gates = truth.get("scenario_required_gates") or {}
    if set(schema_gates) != set(truth_gates):
        errors.append("SYNC:SCENARIO_SET_MISMATCH")
    for scenario, gates in schema_gates.items():
        if gates != list(truth_gates.get(scenario) or []):
            errors.append(f"SYNC:{scenario}:GATE_LIST_OR_ORDER_MISMATCH")

    routing = truth.get("routing") or {}
    route_atoms = list(routing.get("route_atom_names") or [])
    if len(route_atoms) != len(set(route_atoms)):
        errors.append("TRUTH_TABLE:ROUTE_ATOMS_NOT_UNIQUE")
    for rule in routing.get("rules_in_priority_order") or []:
        for atom in list(rule.get("requires_pass") or []) + list(
            rule.get("requires_fail") or []
        ):
            if atom not in route_atoms:
                errors.append(f"TRUTH_TABLE:UNKNOWN_ROUTE_ATOM:{atom}")

    stage_b_route_atoms = list(
        stage_b_schema["$defs"]["routingAtoms"].get("required") or []
    )
    if stage_b_route_atoms != route_atoms:
        errors.append("SYNC:STAGE_B_ROUTE_ATOMS_MISMATCH")
    for atom in route_atoms:
        if atom not in stage_b_prompt:
            errors.append(f"SYNC:STAGE_B_PROMPT_MISSING_ROUTE_ATOM:{atom}")
    if "permission" in (stage_b_schema.get("properties") or {}):
        errors.append("STAGE_BOUNDARY:STAGE_B_MUST_NOT_OUTPUT_PERMISSION")
    if (stage_b_schema["properties"].get("model") or {}).get("const") != "gpt-5.6-sol":
        errors.append("MODEL_POLICY:STAGE_B_MODEL_MISMATCH")
    if (stage_d_schema["properties"].get("model") or {}).get("const") != "gpt-5.6-sol":
        errors.append("MODEL_POLICY:STAGE_D_MODEL_MISMATCH")
    if "final_permission" in (stage_d_schema.get("properties") or {}):
        errors.append("STAGE_BOUNDARY:STAGE_D_MUST_NOT_OUTPUT_FINAL_PERMISSION")
    stage_d_refs = {
        "MATURE_TREND_PULLBACK": "matureEvaluation",
        "MACRO_COPY_RESONANCE": "macroEvaluation",
        "BEAR_REVERSAL_LEFT_RIGHT": "bearEvaluation",
        "FRESH_Q1_EXPANSION": "freshEvaluation",
    }
    for scenario, ref in stage_d_refs.items():
        gates = list(
            stage_d_schema["$defs"][ref]["properties"]["gates"].get("required")
            or []
        )
        if gates != list(truth_gates.get(scenario) or []):
            errors.append(f"SYNC:STAGE_D:{scenario}:GATE_LIST_OR_ORDER_MISMATCH")
        if scenario not in stage_d_prompt:
            errors.append(f"SYNC:STAGE_D_PROMPT_MISSING_SCENARIO:{scenario}")
        for gate in gates:
            if gate not in stage_d_prompt:
                errors.append(f"SYNC:STAGE_D_PROMPT_MISSING_GATE:{scenario}:{gate}")
    routes = stage_d_schema["$defs"]["trigger"]["properties"]["canonical_route"]["enum"]
    for route in routes:
        if route not in stage_d_prompt:
            errors.append(f"SYNC:STAGE_D_PROMPT_MISSING_ROUTE:{route}")
    for filename in (
        "v2_core_stage_b.schema.candidate.json",
        "v2_core_stage_d.schema.candidate.json",
        "v2_core_stage_b.prompt.candidate.md",
        "v2_core_stage_d.prompt.candidate.md",
    ):
        if filename not in staged_contract:
            errors.append(f"SYNC:STAGED_CONTRACT_MISSING_ARTIFACT:{filename}")
    expected_priorities = list(range(1, len(truth.get("permission_precedence") or []) + 1))
    actual_priorities = [
        int(rule.get("priority")) for rule in truth.get("permission_precedence") or []
    ]
    if actual_priorities != expected_priorities:
        errors.append("TRUTH_TABLE:PERMISSION_PRECEDENCE_NOT_CONTIGUOUS")
    return sorted(set(errors))


def derive_scenario(
    route_atoms: dict[str, str], truth: dict[str, Any]
) -> tuple[str, str]:
    routing = truth["routing"]
    expected = list(routing["route_atom_names"])
    if set(route_atoms) != set(expected):
        return "UNRESOLVED_NO_TRADE", "INVALID_ROUTE_ATOM_SET"
    if any(value not in VERDICTS for value in route_atoms.values()):
        return "UNRESOLVED_NO_TRADE", "INVALID_ROUTE_ATOM_VERDICT"

    for left, right in routing["conflict_pairs"]:
        if route_atoms[left] == route_atoms[right] == "PASS":
            return "UNRESOLVED_NO_TRADE", "ADJUDICATION_REQUIRED"

    for rule in routing["rules_in_priority_order"]:
        required_pass = list(rule.get("requires_pass") or [])
        required_fail = list(rule.get("requires_fail") or [])
        impossible = any(route_atoms[name] == "FAIL" for name in required_pass) or any(
            route_atoms[name] == "PASS" for name in required_fail
        )
        if impossible:
            continue
        unresolved = any(route_atoms[name] == "UNKNOWN" for name in required_pass) or any(
            route_atoms[name] == "UNKNOWN" for name in required_fail
        )
        if unresolved:
            return "UNRESOLVED_NO_TRADE", "ADJUDICATION_REQUIRED"
        if all(route_atoms[name] == "PASS" for name in required_pass) and all(
            route_atoms[name] == "FAIL" for name in required_fail
        ):
            return str(rule["scenario"]), "ROUTED"
    return str(routing["no_match_scenario"]), "NO_RULE_MATCH"


def derive_permission(
    *,
    truth: dict[str, Any],
    primary_scenario: str,
    gate_results: dict[str, str],
    data_sufficiency: str,
    trigger_status: str,
    trigger_date_equals_as_of: bool,
    episode_stop_causal: str,
    location_remaining_space: str,
    signal_has_independent_structure: str,
    blockers_absent: dict[str, str],
    alternative_conflict: bool = False,
    schema_valid: bool = True,
    as_of_valid: bool = True,
    evidence_refs_valid: bool = True,
    data_integrity: str = "PASS",
    watchlist_active: bool = True,
    campaign_invalidated: bool = False,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not schema_valid:
        reasons.append("SCHEMA_INVALID")
    if not as_of_valid:
        reasons.append("AS_OF_INVALID")
    if not evidence_refs_valid:
        reasons.append("EVIDENCE_REFERENCE_INVALID")
    if data_integrity == "FAIL":
        reasons.append("DATA_INTEGRITY_FAIL")
    if reasons:
        return "INVALID", reasons

    if not watchlist_active:
        return "REMOVE", ["WATCHLIST_INACTIVE"]
    if campaign_invalidated:
        return "REMOVE", ["CAMPAIGN_INVALIDATED"]

    scenarios = truth["scenario_required_gates"]
    if primary_scenario not in scenarios:
        return "UNKNOWN", ["ROUTING_UNRESOLVED"]
    expected_gates = list(scenarios[primary_scenario])
    if set(gate_results) != set(expected_gates):
        return "INVALID", ["SCENARIO_GATE_SET_MISMATCH"]

    tri_values = list(gate_results.values()) + [
        data_sufficiency,
        episode_stop_causal,
        location_remaining_space,
        signal_has_independent_structure,
        *blockers_absent.values(),
    ]
    if any(value not in VERDICTS for value in tri_values):
        return "INVALID", ["INVALID_ATOM_VERDICT"]
    if alternative_conflict:
        return "UNKNOWN", ["ALTERNATIVE_CONFLICT_TRUE"]
    if any(value == "UNKNOWN" for value in tri_values):
        return "UNKNOWN", ["REQUIRED_ATOM_UNKNOWN"]

    if data_sufficiency == "FAIL":
        reasons.append("DATA_SUFFICIENCY_FAIL")
    if any(value == "FAIL" for value in gate_results.values()):
        reasons.append("REQUIRED_GATE_FAIL")
    if episode_stop_causal == "FAIL" or location_remaining_space == "FAIL":
        reasons.append("LOCATION_OR_STOP_FAIL")
    if signal_has_independent_structure == "FAIL":
        reasons.append("REQUIRED_GATE_FAIL")
    if any(value == "FAIL" for value in blockers_absent.values()):
        reasons.append("BLOCKING_CONDITION_PRESENT")
    if trigger_status != "TRIGGERED" or not trigger_date_equals_as_of:
        reasons.append("TRIGGER_NOT_COMPLETE")
    if reasons:
        return "WAIT", sorted(set(reasons))
    return "TRADE_APPROVED", ["ALL_REQUIRED_CONDITIONS_PASS"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    errors = validate_artifacts(args.artifact_dir)
    print(
        json.dumps(
            {
                "status": "PASS" if not errors else "FAIL",
                "artifact_dir": str(args.artifact_dir.resolve()),
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
