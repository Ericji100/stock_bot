from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts import v2_core_legacy_role_alignment_compare_v3 as comparator
from scripts.v2_core_legacy_role_alignment_runner_v3 import (
    MODEL,
    REASONING,
    run_case,
    validate_existing_artifacts,
)
from scripts.v2_core_legacy_role_alignment_validator_v3 import validate_response


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atom(judgement: str, option: str) -> dict:
    return {
        "judgement": judgement,
        "evidence_option_id": option,
        "explanation": "AS-OF fixed candidate evidence supports this role atom.",
    }


def role(candidate: dict | None, *, status: str = "SELECTED", conflict: bool = False, alt: str | None = None) -> dict:
    if candidate is None:
        unknown = atom("UNKNOWN", "NONE")
        return {
            "selection_status": status,
            "candidate_id": "NONE",
            "evidence_option_id": "NONE",
            "alternative_candidate_ids": [],
            "alternative_changes_role_conclusion": False,
            "data_sufficiency": unknown,
            "role_fit": unknown,
            "structural_corroboration": unknown,
            "relationship_to_current_context": unknown,
            "explanation": "No applicable fixed candidate is required for this lifecycle role.",
        }
    option = candidate["evidence_option_id"]
    return {
        "selection_status": "SELECTED",
        "candidate_id": candidate["candidate_id"],
        "evidence_option_id": option,
        "alternative_candidate_ids": [alt] if alt else [],
        "alternative_changes_role_conclusion": conflict,
        "data_sufficiency": atom("PASS", option),
        "role_fit": atom("PASS", option),
        "structural_corroboration": atom("PASS", option),
        "relationship_to_current_context": atom("PASS", option),
        "explanation": "This fixed segment is the preferred role object under the visible lifecycle evidence.",
    }


def lifecycle_atom(judgement: str, role_name: str, option: str) -> dict:
    return {
        "judgement": judgement,
        "supporting_role": role_name,
        "evidence_option_id": option,
        "explanation": "The selected role and AS-OF path provide the lifecycle evidence.",
    }


def fresh_response(packet: dict, *, working_conflict: bool = False) -> dict:
    working = next(
        row for row in packet["candidate_pool"] if row["direction"] == "UP" and row["status"] == "FORMING"
    )
    alternate = next(
        row for row in packet["candidate_pool"] if row["candidate_id"] != working["candidate_id"]
    )
    option = working["evidence_option_id"]
    return {
        "schema_version": "v2-core-legacy-role-alignment-r3-candidate",
        "roles": {
            "campaign_context": role(working),
            "scenario_working": role(
                working,
                conflict=working_conflict,
                alt=alternate["candidate_id"] if working_conflict else None,
            ),
            "parent": role(None, status="NOT_APPLICABLE"),
        },
        "lifecycle_atoms": {
            "active_large_down_still_controls": lifecycle_atom("FAIL", "SCENARIO_WORKING", option),
            "fresh_up_anchor_without_completed_parent": lifecycle_atom("PASS", "SCENARIO_WORKING", option),
            "completed_up_parent_controls_copy": lifecycle_atom("FAIL", "SCENARIO_WORKING", option),
            "current_is_first_independent_copy": lifecycle_atom("FAIL", "SCENARIO_WORKING", option),
            "mature_campaign_repeated_success": lifecycle_atom("FAIL", "CAMPAIGN_CONTEXT", option),
        },
        "recommended_scenario_family": "FRESH_Q1_EXPANSION",
        "confidence": "MEDIUM",
        "uncertainty_codes": ["WORKING_ANCHOR_AMBIGUOUS"] if working_conflict else ["NONE"],
        "causal_attestation": {
            "as_of_only": True,
            "no_identity": True,
            "no_legacy_answer": True,
            "no_future_performance": True,
            "fixed_candidates_only": True,
        },
    }


def first_packet() -> tuple[dict, dict]:
    manifest = load_json(ARTIFACT_DIR / "legacy_anchor_alignment_input_manifest_candidate_r1.json")
    row = manifest["rows"][0]
    packet = load_json(
        ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
    )
    return row, packet


def test_r3_fresh_roles_derive_unique_program_scenario() -> None:
    _, packet = first_packet()
    schema = load_json(ARTIFACT_DIR / "v2_core_legacy_role_alignment.schema.candidate_r3.json")
    result = validate_response(response=fresh_response(packet), schema=schema, packet=packet)
    assert result["status"] == "VALID"
    assert result["program_derived_scenario_family"] == "FRESH_Q1_EXPANSION"
    assert result["route_status"] == "ROUTED"
    assert result["recommendation_matches_program"] is True


def test_r3_working_role_conflict_downgrades_program_route() -> None:
    _, packet = first_packet()
    schema = load_json(ARTIFACT_DIR / "v2_core_legacy_role_alignment.schema.candidate_r3.json")
    result = validate_response(
        response=fresh_response(packet, working_conflict=True), schema=schema, packet=packet
    )
    assert result["status"] == "VALID"
    assert result["program_derived_scenario_family"] == "UNRESOLVED_NO_TRADE"
    assert result["program_usable_scenario_working_anchor"] is None
    assert result["route_status"] == "UNRESOLVED"


def test_r3_rejects_lifecycle_option_not_bound_to_role() -> None:
    _, packet = first_packet()
    schema = load_json(ARTIFACT_DIR / "v2_core_legacy_role_alignment.schema.candidate_r3.json")
    response = fresh_response(packet)
    other = next(
        row
        for row in packet["candidate_pool"]
        if row["evidence_option_id"]
        != response["roles"]["scenario_working"]["evidence_option_id"]
    )
    response["lifecycle_atoms"]["fresh_up_anchor_without_completed_parent"][
        "evidence_option_id"
    ] = other["evidence_option_id"]
    result = validate_response(response=response, schema=schema, packet=packet)
    assert result["status"] == "INVALID"
    assert any("does not match supporting role" in error for error in result["errors"])


def test_r3_manifest_binds_all_contract_files() -> None:
    manifest = load_json(
        ARTIFACT_DIR / "legacy_role_alignment_execution_manifest_candidate_r3.json"
    )
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["expected_case_rounds"] == 14
    assert manifest["stop_remaining_percent_lte"] == 60.0
    assert manifest["r1_or_r2_outputs_reused"] is False
    for key in (
        "design",
        "failure_analysis",
        "selection",
        "input_manifest",
        "prompt",
        "schema",
        "operational_policy",
    ):
        path = ARTIFACT_DIR / manifest[f"{key}_file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[f"{key}_sha256"]
    for key in ("runner", "validator", "comparator"):
        path = ROOT / manifest[f"{key}_file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[f"{key}_sha256"]


def test_r3_fake_transport_persists_and_revalidates(tmp_path: Path) -> None:
    execution = load_json(
        ARTIFACT_DIR / "legacy_role_alignment_execution_manifest_candidate_r3.json"
    )
    row = execution["rows"][0]
    input_path = ARTIFACT_DIR / execution["input_packet_directory"] / row["input_packet_file"]
    packet = load_json(input_path)
    response = fresh_response(packet)
    usage = tmp_path / "usage.json"
    usage.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 10,
                "remaining_percent": 90,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "legacy-role-r3-test",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "outputs" / f"{row['review_id']}.json"
    receipt = tmp_path / "receipts" / f"{row['review_id']}.legacy_role_alignment_r3.json"
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        target = Path(command[command.index("--output-last-message") + 1])
        target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = run_case(
        input_packet_path=input_path,
        input_manifest_path=ARTIFACT_DIR / execution["input_manifest_file"],
        schema_path=ARTIFACT_DIR / execution["schema_file"],
        prompt_path=ARTIFACT_DIR / execution["prompt_file"],
        usage_attestation_path=usage,
        stop_remaining_percent_lte=60,
        output_path=output,
        receipt_path=receipt,
        timeout_seconds=30,
        run_command=fake_run,
    )
    assert result["status"] == "VALID"
    assert validate_existing_artifacts(
        input_packet_path=input_path,
        input_manifest_path=ARTIFACT_DIR / execution["input_manifest_file"],
        schema_path=ARTIFACT_DIR / execution["schema_file"],
        prompt_path=ARTIFACT_DIR / execution["prompt_file"],
        output_path=output,
        receipt_path=receipt,
    ) == result
    assert commands[0][commands[0].index("--model") + 1] == MODEL
    assert f'model_reasoning_effort="{REASONING}"' in commands[0]


def test_r3_incomplete_comparator_keeps_legacy_sealed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_loader(_: Path) -> dict:
        raise AssertionError("legacy answers must remain sealed until all outputs freeze")

    monkeypatch.setattr(comparator, "load_reference_cases", forbidden_loader)
    report = comparator.build_report(ARTIFACT_DIR)
    assert report["status"].startswith("NOT_READY_OUTPUTS_INCOMPLETE")
    assert report["legacy_answers_loaded"] is False
    assert report["completed_valid_case_count"] < report["expected_case_count"]
