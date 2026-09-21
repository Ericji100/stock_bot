from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from scripts.v2_core_m2a_contracts_v1 import (
    ARTIFACT_DIR,
    derive_permission,
    derive_scenario,
    load_json,
    validate_artifacts,
)


TRUTH = load_json(ARTIFACT_DIR / "permission_truth_table.json")


def route_atoms(**overrides: str) -> dict[str, str]:
    atoms = {name: "FAIL" for name in TRUTH["routing"]["route_atom_names"]}
    atoms.update(overrides)
    return atoms


def passing_permission_inputs(scenario: str) -> dict:
    return {
        "truth": TRUTH,
        "primary_scenario": scenario,
        "gate_results": {
            gate: "PASS" for gate in TRUTH["scenario_required_gates"][scenario]
        },
        "data_sufficiency": "PASS",
        "trigger_status": "TRIGGERED",
        "trigger_date_equals_as_of": True,
        "episode_stop_causal": "PASS",
        "location_remaining_space": "PASS",
        "signal_has_independent_structure": "PASS",
        "blockers_absent": {
            "Q3_ABSENT": "PASS",
            "EXHAUSTION_ABSENT": "PASS",
            "OVEREXTENSION_ABSENT": "PASS",
            "UNRESOLVED_CORPORATE_ACTION_ABSENT": "PASS",
        },
    }


def test_candidate_artifacts_are_synchronized() -> None:
    assert validate_artifacts() == []


def test_staged_schemas_are_valid_and_preserve_ai_program_boundary() -> None:
    stage_b = json.loads(
        (ARTIFACT_DIR / "v2_core_stage_b.schema.candidate.json").read_text(
            encoding="utf-8"
        )
    )
    stage_d = json.loads(
        (ARTIFACT_DIR / "v2_core_stage_d.schema.candidate.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(stage_b)
    Draft202012Validator.check_schema(stage_d)
    assert "permission" not in stage_b["properties"]
    assert "final_permission" not in stage_d["properties"]
    assert "ai_recommended_permission" in stage_d["properties"]


def test_prompts_name_every_machine_gate_and_route() -> None:
    assert validate_artifacts() == []
    stage_b_prompt = (ARTIFACT_DIR / "v2_core_stage_b.prompt.candidate.md").read_text(
        encoding="utf-8"
    )
    stage_d_prompt = (ARTIFACT_DIR / "v2_core_stage_d.prompt.candidate.md").read_text(
        encoding="utf-8"
    )
    for atom in TRUTH["routing"]["route_atom_names"]:
        assert atom in stage_b_prompt
    for scenario, gates in TRUTH["scenario_required_gates"].items():
        assert scenario in stage_d_prompt
        assert all(gate in stage_d_prompt for gate in gates)


def test_route_prioritizes_active_bear_control() -> None:
    scenario, status = derive_scenario(
        route_atoms(ACTIVE_LARGE_BEAR_ANCHOR="PASS"), TRUTH
    )
    assert (scenario, status) == ("BEAR_REVERSAL_LEFT_RIGHT", "ROUTED")


def test_route_separates_fresh_macro_and_mature_lifecycle() -> None:
    fresh = route_atoms(FRESH_UP_ANCHOR_CONTROLS="PASS")
    assert derive_scenario(fresh, TRUTH)[0] == "FRESH_Q1_EXPANSION"

    macro = route_atoms(
        COMPLETED_PARENT_ANCHOR="PASS",
        FIRST_INDEPENDENT_REPLICATION="PASS",
    )
    assert derive_scenario(macro, TRUTH)[0] == "MACRO_COPY_RESONANCE"

    mature = route_atoms(
        LONG_CAMPAIGN_REPEATED_SUCCESS="PASS",
        LONG_MA_HABIT_SUPPORTED="PASS",
        CURRENT_CORRECTION_WITHIN_CAMPAIGN="PASS",
    )
    assert derive_scenario(mature, TRUTH)[0] == "MATURE_TREND_PULLBACK"


def test_route_conflict_or_required_unknown_never_falls_through() -> None:
    conflict = route_atoms(
        ACTIVE_LARGE_BEAR_ANCHOR="PASS",
        FRESH_UP_ANCHOR_CONTROLS="PASS",
    )
    assert derive_scenario(conflict, TRUTH) == (
        "UNRESOLVED_NO_TRADE",
        "ADJUDICATION_REQUIRED",
    )
    unknown = route_atoms(ACTIVE_LARGE_BEAR_ANCHOR="UNKNOWN")
    assert derive_scenario(unknown, TRUTH) == (
        "UNRESOLVED_NO_TRADE",
        "ADJUDICATION_REQUIRED",
    )


def test_all_pass_is_trade_and_ai_recommendation_is_not_an_input() -> None:
    result, reasons = derive_permission(
        **passing_permission_inputs("MATURE_TREND_PULLBACK")
    )
    assert result == "TRADE_APPROVED"
    assert reasons == ["ALL_REQUIRED_CONDITIONS_PASS"]


def test_unknown_fail_remove_and_invalid_precedence() -> None:
    payload = passing_permission_inputs("MACRO_COPY_RESONANCE")
    payload["gate_results"]["COMPLETED_PARENT_ANCHOR"] = "UNKNOWN"
    assert derive_permission(**payload)[0] == "UNKNOWN"

    payload = passing_permission_inputs("MACRO_COPY_RESONANCE")
    payload["gate_results"]["COMPLETED_PARENT_ANCHOR"] = "FAIL"
    assert derive_permission(**payload)[0] == "WAIT"

    payload = passing_permission_inputs("MACRO_COPY_RESONANCE")
    payload["campaign_invalidated"] = True
    assert derive_permission(**payload)[0] == "REMOVE"

    payload = passing_permission_inputs("MACRO_COPY_RESONANCE")
    payload["schema_valid"] = False
    payload["campaign_invalidated"] = True
    assert derive_permission(**payload)[0] == "INVALID"


def test_trigger_and_blockers_are_program_gates() -> None:
    payload = passing_permission_inputs("FRESH_Q1_EXPANSION")
    payload["trigger_status"] = "ARMED"
    assert derive_permission(**payload)[0] == "WAIT"

    payload = passing_permission_inputs("FRESH_Q1_EXPANSION")
    payload["blockers_absent"]["Q3_ABSENT"] = "FAIL"
    assert derive_permission(**payload)[0] == "WAIT"


def test_exact_scenario_gate_set_is_required() -> None:
    payload = passing_permission_inputs("BEAR_REVERSAL_LEFT_RIGHT")
    payload["gate_results"].pop("PHASE_STOP_CAUSAL")
    assert derive_permission(**payload) == (
        "INVALID",
        ["SCENARIO_GATE_SET_MISMATCH"],
    )
