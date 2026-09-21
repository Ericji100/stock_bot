from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts import v2_core_legacy_role_alignment_compare_v5 as comparator
from scripts.v2_core_legacy_role_alignment_runner_v5 import (
    MODEL,
    REASONING,
    run_case,
    validate_existing_artifacts,
)
from scripts.v2_core_legacy_role_alignment_validator_v5 import validate_response


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)
PACKET = ARTIFACT_DIR / "legacy_anchor_alignment_input_packets_candidate_r1" / "FP-18b86f08f05563ff5886097b.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def role(candidate: dict, option: str) -> dict:
    atom = {"judgement": "PASS", "evidence_option_id": option, "explanation": "fixed evidence supports this role"}
    return {
        "selection_status": "SELECTED",
        "candidate_id": candidate["candidate_id"],
        "evidence_option_id": option,
        "alternative_candidate_ids": [],
        "alternative_changes_role_conclusion": False,
        "data_sufficiency": deepcopy(atom),
        "role_fit": deepcopy(atom),
        "structural_corroboration": deepcopy(atom),
        "relationship_to_current_context": deepcopy(atom),
        "explanation": "fixed candidate has the required structural role and current relation",
    }


def lifecycle(judgement: str, role_name: str, option: str) -> dict:
    return {
        "judgement": judgement,
        "supporting_role": role_name,
        "evidence_option_id": option,
        "explanation": "fixed evidence supports this lifecycle judgement",
    }


def check(result: str, candidate: dict | None, option: str = "NONE") -> dict:
    return {
        "result": result,
        "candidate_id": candidate["candidate_id"] if candidate else "NONE",
        "evidence_option_id": option,
        "explanation": "fixed evidence supports this anti drift check",
    }


def fresh_response() -> tuple[dict, dict, dict]:
    packet = load_json(PACKET)
    schema = load_json(ARTIFACT_DIR / "v2_core_legacy_role_alignment.schema.candidate_r5.json")
    options = {row["candidate_id"]: row["evidence_option_id"] for row in packet["candidate_evidence_options"]}
    working = next(
        row for row in packet["candidate_pool"]
        if row["direction"] == "UP" and row["status"] == "FORMING" and row["basis"].startswith("MACD_")
    )
    pivot = next(row for row in packet["candidate_pool"] if row["basis"].startswith("PIVOT_"))
    working_option = options[working["candidate_id"]]
    parent = role(working, working_option)
    parent.update(
        {
            "selection_status": "NOT_APPLICABLE",
            "candidate_id": "NONE",
            "alternative_candidate_ids": [],
        }
    )
    response = {
        "schema_version": "v2-core-legacy-role-alignment-r5-candidate",
        "provisional_scenario_family": "FRESH_Q1_EXPANSION",
        "working_role_class": "FORMING_UP_NEW_ANCHOR_REFERENCE",
        "roles": {
            "campaign_context": role(working, working_option),
            "scenario_working": role(working, working_option),
            "parent": parent,
            "episode_structure": role(working, working_option),
        },
        "basis_comparison": {
            "availability": "BOTH",
            "macd_candidate_id": working["candidate_id"],
            "pivot_candidate_id": pivot["candidate_id"],
            "selected_working_candidate_id": working["candidate_id"],
            "conclusion": "MACD_BETTER_ROLE_FIT",
            "explanation": "the MACD boundary and price structure fit the forming new anchor role better",
        },
        "lifecycle_atoms": {
            "active_large_down_still_controls": lifecycle("FAIL", "SCENARIO_WORKING", working_option),
            "up_episode_has_not_replaced_down_control": lifecycle("FAIL", "SCENARIO_WORKING", working_option),
            "fresh_up_anchor_without_completed_parent": lifecycle("PASS", "SCENARIO_WORKING", working_option),
            "completed_up_parent_controls_copy": lifecycle("FAIL", "PARENT", working_option),
            "current_is_first_independent_copy": lifecycle("FAIL", "PARENT", working_option),
            "mature_campaign_repeated_success": lifecycle("FAIL", "CAMPAIGN_CONTEXT", working_option),
            "long_trend_habit_preexists_current_episode": lifecycle("FAIL", "CAMPAIGN_CONTEXT", working_option),
        },
        "anti_drift_checks": {
            "no_recency_default": check("PASS", working, working_option),
            "cross_family_comparison_completed": check("PASS", working, working_option),
            "parent_episode_separated": check("NOT_APPLICABLE", None),
            "mature_campaign_episode_separated": check("NOT_APPLICABLE", None),
            "bear_control_transfer_resolved": check("NOT_APPLICABLE", None),
            "fresh_positive_anchor_evidence_present": check("PASS", working, working_option),
        },
        "recommended_scenario_family": "FRESH_Q1_EXPANSION",
        "confidence": "HIGH",
        "uncertainty_codes": ["NONE"],
        "causal_attestation": {
            "as_of_only": True,
            "no_identity": True,
            "no_legacy_answer": True,
            "no_future_performance": True,
            "fixed_candidates_only": True,
        },
    }
    return response, schema, packet


def test_r5_valid_fresh_response_routes_without_trade_permission() -> None:
    response, schema, packet = fresh_response()
    result = validate_response(response=response, schema=schema, packet=packet)
    assert result["status"] == "VALID"
    assert result["program_derived_scenario_family"] == "FRESH_Q1_EXPANSION"
    assert result["route_status"] == "ROUTED"
    assert result["validator_contract"]["episode_structure_separate"] is True
    assert result["validator_contract"]["trade_permission_granted"] is False


def test_r5_recency_check_failure_downgrades_without_repairing_ai() -> None:
    response, schema, packet = fresh_response()
    response["anti_drift_checks"]["no_recency_default"]["result"] = "FAIL"
    result = validate_response(response=response, schema=schema, packet=packet)
    assert result["status"] == "VALID"
    assert result["program_derived_scenario_family"] == "UNRESOLVED_NO_TRADE"
    assert result["ai_recommended_scenario_family"] == "FRESH_Q1_EXPANSION"
    assert result["validator_contract"]["ai_roles_repaired"] is False


def test_r5_rejects_cross_family_candidate_mislabelling() -> None:
    response, schema, packet = fresh_response()
    response["basis_comparison"]["pivot_candidate_id"] = response["basis_comparison"]["macd_candidate_id"]
    result = validate_response(response=response, schema=schema, packet=packet)
    assert result["status"] == "INVALID"
    assert any("invalid pivot candidate" in error for error in result["errors"])


def test_r5_rejects_scenario_role_class_mismatch() -> None:
    response, schema, packet = fresh_response()
    response["working_role_class"] = "PERSISTENT_UP_CAMPAIGN_REFERENCE"
    result = validate_response(response=response, schema=schema, packet=packet)
    assert result["status"] == "INVALID"
    assert "provisional scenario and working role class mismatch" in result["errors"]


def test_r5_schema_and_prompt_reference_all_required_roles() -> None:
    schema = load_json(ARTIFACT_DIR / "v2_core_legacy_role_alignment.schema.candidate_r5.json")
    prompt = (ARTIFACT_DIR / "v2_core_legacy_role_alignment.prompt.candidate_r5.md").read_text(encoding="utf-8-sig")
    assert set(schema["properties"]["roles"]["required"]) == {
        "campaign_context",
        "scenario_working",
        "parent",
        "episode_structure",
    }
    for role_name in ("CAMPAIGN_CONTEXT", "SCENARIO_WORKING", "PARENT", "EPISODE_STRUCTURE"):
        assert role_name in prompt


def test_r5_manifest_binds_candidate_contract_and_current_model() -> None:
    manifest = load_json(
        ARTIFACT_DIR / "legacy_role_alignment_execution_manifest_candidate_r5.json"
    )
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["expected_case_rounds"] == 14
    assert manifest["stop_remaining_percent_lte"] == 5.0
    assert manifest["teacher_labels_used_for_offline_contract_distillation"] is True
    assert manifest["legacy_answers_available_to_ai"] is False
    assert manifest["locked_reproduction_set_opened"] is False
    artifact_keys = (
        "distillation_design",
        "role_contract_md",
        "role_contract_json",
        "pairwise_audit",
        "contract_fit_audit",
        "selection",
        "input_manifest",
        "prompt",
        "schema",
        "operational_policy",
    )
    for key in artifact_keys:
        path = ARTIFACT_DIR / manifest[f"{key}_file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[f"{key}_sha256"]
    for key in ("runner", "validator", "comparator"):
        path = ROOT / manifest[f"{key}_file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[f"{key}_sha256"]


def test_r5_fake_transport_persists_and_revalidates(tmp_path: Path) -> None:
    execution = load_json(
        ARTIFACT_DIR / "legacy_role_alignment_execution_manifest_candidate_r5.json"
    )
    row = next(
        row
        for row in execution["rows"]
        if row["review_id"] == "FP-18b86f08f05563ff5886097b"
    )
    response, _, _ = fresh_response()
    input_path = ARTIFACT_DIR / execution["input_packet_directory"] / row["input_packet_file"]
    usage = tmp_path / "usage.json"
    usage.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 10,
                "remaining_percent": 90,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "legacy-role-r5-test",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "outputs" / f"{row['review_id']}.json"
    receipt = tmp_path / "receipts" / f"{row['review_id']}.legacy_role_alignment_r5.json"
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        target = Path(command[command.index("--output-last-message") + 1])
        target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    validation = run_case(
        input_packet_path=input_path,
        input_manifest_path=ARTIFACT_DIR / execution["input_manifest_file"],
        schema_path=ARTIFACT_DIR / execution["schema_file"],
        prompt_path=ARTIFACT_DIR / execution["prompt_file"],
        usage_attestation_path=usage,
        stop_remaining_percent_lte=5,
        output_path=output,
        receipt_path=receipt,
        timeout_seconds=30,
        run_command=fake_run,
    )
    assert validation["status"] == "VALID"
    assert validate_existing_artifacts(
        input_packet_path=input_path,
        input_manifest_path=ARTIFACT_DIR / execution["input_manifest_file"],
        schema_path=ARTIFACT_DIR / execution["schema_file"],
        prompt_path=ARTIFACT_DIR / execution["prompt_file"],
        output_path=output,
        receipt_path=receipt,
    ) == validation
    assert commands[0][commands[0].index("--model") + 1] == MODEL
    assert f'model_reasoning_effort="{REASONING}"' in commands[0]


def test_r5_incomplete_comparator_keeps_teacher_answers_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_loader(_: Path) -> dict:
        raise AssertionError("teacher answers must remain sealed until all outputs freeze")

    monkeypatch.setattr(comparator, "load_reference_cases", forbidden_loader)
    source_manifest = ARTIFACT_DIR / "legacy_role_alignment_execution_manifest_candidate_r5.json"
    (tmp_path / source_manifest.name).write_bytes(source_manifest.read_bytes())
    report = comparator.build_report(tmp_path)
    assert report["status"].startswith("NOT_READY_OUTPUTS_INCOMPLETE")
    assert report["legacy_answers_loaded"] is False
    assert report["completed_valid_case_count"] == 0
