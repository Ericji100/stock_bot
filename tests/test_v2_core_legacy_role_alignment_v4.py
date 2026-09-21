from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts import v2_core_legacy_role_alignment_compare_v4 as comparator
from scripts.v2_core_legacy_role_alignment_runner_v4 import (
    MODEL,
    REASONING,
    run_case,
    validate_existing_artifacts,
)
from scripts.v2_core_legacy_role_alignment_validator_v4 import validate_response


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)
FAILED_REVIEW_ID = "FP-756c61fe9663a90e7cf3187c"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def packet_for(review_id: str) -> tuple[dict, dict]:
    manifest = load_json(ARTIFACT_DIR / "legacy_anchor_alignment_input_manifest_candidate_r1.json")
    row = next(item for item in manifest["rows"] if item["review_id"] == review_id)
    packet = load_json(
        ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
    )
    return row, packet


def r3_failed_response_as_r4() -> dict:
    response = load_json(
        ARTIFACT_DIR
        / "legacy_role_alignment_runs_candidate_r3"
        / "outputs"
        / f"{FAILED_REVIEW_ID}.invalid.raw"
    )
    response["schema_version"] = "v2-core-legacy-role-alignment-r4-candidate"
    return response


def r4_schema() -> dict:
    return load_json(ARTIFACT_DIR / "v2_core_legacy_role_alignment.schema.candidate_r4.json")


def test_r4_accepts_evidence_bearing_parent_not_applicable() -> None:
    _, packet = packet_for(FAILED_REVIEW_ID)
    result = validate_response(
        response=r3_failed_response_as_r4(), schema=r4_schema(), packet=packet
    )
    assert result["status"] == "VALID"
    assert result["role_statuses"]["PARENT"] == "NOT_APPLICABLE"
    assert result["program_derived_scenario_family"] == "FRESH_Q1_EXPANSION"
    assert result["route_status"] == "ROUTED"


def test_r4_rejects_parent_not_applicable_evidence_from_unselected_candidate() -> None:
    _, packet = packet_for(FAILED_REVIEW_ID)
    response = r3_failed_response_as_r4()
    selected_ids = {
        response["roles"]["campaign_context"]["candidate_id"],
        response["roles"]["scenario_working"]["candidate_id"],
    }
    unselected = next(
        row for row in packet["candidate_pool"] if row["candidate_id"] not in selected_ids
    )
    wrong_option = unselected["evidence_option_id"]
    parent = response["roles"]["parent"]
    parent["evidence_option_id"] = wrong_option
    for key in (
        "data_sufficiency",
        "role_fit",
        "structural_corroboration",
        "relationship_to_current_context",
    ):
        parent[key]["evidence_option_id"] = wrong_option
    for atom in response["lifecycle_atoms"].values():
        if atom["supporting_role"] == "PARENT":
            atom["evidence_option_id"] = wrong_option
    result = validate_response(response=response, schema=r4_schema(), packet=packet)
    assert result["status"] == "INVALID"
    assert any("selected context/working role" in error for error in result["errors"])


def test_r4_unresolved_parent_remains_strict_unknown_none() -> None:
    _, packet = packet_for(FAILED_REVIEW_ID)
    response = r3_failed_response_as_r4()
    response["roles"]["parent"]["selection_status"] = "UNRESOLVED"
    result = validate_response(response=response, schema=r4_schema(), packet=packet)
    assert result["status"] == "INVALID"
    assert any("UNRESOLVED must use NONE candidate/option" in error for error in result["errors"])
    assert any("UNRESOLVED atoms must be UNKNOWN/NONE" in error for error in result["errors"])


def test_r4_manifest_binds_dependencies_and_contract_files() -> None:
    manifest = load_json(
        ARTIFACT_DIR / "legacy_role_alignment_execution_manifest_candidate_r4.json"
    )
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["expected_case_rounds"] == 14
    assert manifest["stop_remaining_percent_lte"] == 60.0
    assert manifest["r1_r2_r3_outputs_reused"] is False
    artifact_keys = (
        "design",
        "r2_failure_analysis",
        "r3_failure_analysis",
        "selection",
        "input_manifest",
        "prompt",
        "schema",
        "operational_policy",
    )
    for key in artifact_keys:
        path = ARTIFACT_DIR / manifest[f"{key}_file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[f"{key}_sha256"]
    for key in ("runner", "validator", "comparator", "base_runner", "base_validator"):
        path = ROOT / manifest[f"{key}_file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[f"{key}_sha256"]


def test_r4_fake_transport_persists_and_revalidates(tmp_path: Path) -> None:
    execution = load_json(
        ARTIFACT_DIR / "legacy_role_alignment_execution_manifest_candidate_r4.json"
    )
    row, _ = packet_for(FAILED_REVIEW_ID)
    input_path = ARTIFACT_DIR / execution["input_packet_directory"] / row["input_packet_file"]
    response = r3_failed_response_as_r4()
    usage = tmp_path / "usage.json"
    usage.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 10,
                "remaining_percent": 90,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "legacy-role-r4-test",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "outputs" / f"{row['review_id']}.json"
    receipt = tmp_path / "receipts" / f"{row['review_id']}.legacy_role_alignment_r4.json"
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


def test_r4_incomplete_comparator_keeps_legacy_sealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_loader(_: Path) -> dict:
        raise AssertionError("legacy answers must remain sealed until all outputs freeze")

    monkeypatch.setattr(comparator, "load_reference_cases", forbidden_loader)
    source_manifest = ARTIFACT_DIR / "legacy_role_alignment_execution_manifest_candidate_r4.json"
    (tmp_path / source_manifest.name).write_bytes(source_manifest.read_bytes())
    report = comparator.build_report(tmp_path)
    assert report["status"].startswith("NOT_READY_OUTPUTS_INCOMPLETE")
    assert report["legacy_answers_loaded"] is False
    assert report["completed_valid_case_count"] == 0


def test_r4_case_payload_is_not_repaired_by_runner() -> None:
    response = r3_failed_response_as_r4()
    cloned = deepcopy(response)
    _, packet = packet_for(FAILED_REVIEW_ID)
    validate_response(response=response, schema=r4_schema(), packet=packet)
    assert response == cloned
