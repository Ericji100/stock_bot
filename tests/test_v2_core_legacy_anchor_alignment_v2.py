from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts import v2_core_legacy_anchor_alignment_compare_v2 as comparator
from scripts.v2_core_legacy_anchor_alignment_runner_v2 import (
    MODEL,
    REASONING,
    run_case,
    validate_existing_artifacts,
)
from scripts.v2_core_legacy_anchor_alignment_validator_v2 import validate_response


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atom(judgement: str, option: str) -> dict:
    return {"judgement": judgement, "evidence_option_id": option}


def selected_response(packet: dict, *, conflict: bool) -> dict:
    candidate = packet["candidate_pool"][0]
    option = candidate["evidence_option_id"]
    alternatives = []
    if conflict:
        alternatives = [packet["candidate_pool"][1]["candidate_id"]]
    if candidate["direction"] == "DOWN":
        scenario = "BEAR_REVERSAL_LEFT_RIGHT"
        reason = "ACTIVE_DOWN_ANCHOR_CONTROLS"
    else:
        scenario = "FRESH_Q1_EXPANSION"
        reason = "FORMING_UP_REPLACES_DOWN_CONTROL"
    return {
        "schema_version": "v2-core-legacy-anchor-alignment-r2-candidate",
        "selection": {
            "selection_status": "SELECTED",
            "data_sufficiency": atom("PASS", option),
            "controlling_candidate_id": candidate["candidate_id"],
            "controlling_evidence_option_id": option,
            "alternative_candidate_ids": alternatives,
            "anchor_quality": {
                "direction_coherent": atom("PASS", option),
                "anchor_clean": atom("PASS", option),
                "anchor_meaty": atom("PASS", option),
                "anchor_destructive": atom("PASS", option),
                "anchor_traceable": atom("PASS", option),
                "anchor_completed_fit": atom("PASS", option),
                "controls_current_context": atom("PASS", option),
            },
            "control_reason": reason,
            "recommended_scenario_family": scenario,
            "alternative_changes_control_conclusion": conflict,
            "confidence": "MEDIUM",
            "uncertainty_codes": (
                ["CURRENT_CONTROL_AMBIGUOUS"] if conflict else ["NONE"]
            ),
            "causal_attestation": {
                "as_of_only": True,
                "no_identity": True,
                "no_legacy_answer": True,
                "no_future_performance": True,
                "fixed_candidates_only": True,
            },
        },
    }


def test_r2_conflict_preserves_ai_preference_but_program_downgrades() -> None:
    manifest = load_json(ARTIFACT_DIR / "legacy_anchor_alignment_input_manifest_candidate_r1.json")
    row = manifest["rows"][0]
    packet = load_json(
        ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
    )
    schema = load_json(
        ARTIFACT_DIR / "v2_core_legacy_anchor_alignment.schema.candidate_r2.json"
    )
    result = validate_response(
        response=selected_response(packet, conflict=True), schema=schema, packet=packet
    )
    assert result["status"] == "VALID"
    assert result["ai_selected_candidate"] is not None
    assert result["program_usable_selection_status"] == "UNRESOLVED_CONFLICT"
    assert result["program_usable_candidate"] is None
    assert result["program_usable_scenario_family"] == "UNRESOLVED_NO_TRADE"


def test_r2_nonconflict_selection_remains_program_usable() -> None:
    manifest = load_json(ARTIFACT_DIR / "legacy_anchor_alignment_input_manifest_candidate_r1.json")
    row = manifest["rows"][0]
    packet = load_json(
        ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
    )
    schema = load_json(
        ARTIFACT_DIR / "v2_core_legacy_anchor_alignment.schema.candidate_r2.json"
    )
    result = validate_response(
        response=selected_response(packet, conflict=False), schema=schema, packet=packet
    )
    assert result["status"] == "VALID"
    assert result["program_usable_selection_status"] == "SELECTED"
    assert result["program_usable_candidate"] == result["ai_selected_candidate"]


def test_r2_manifest_binds_contract_and_reuses_no_r1_outputs() -> None:
    manifest = load_json(
        ARTIFACT_DIR / "legacy_anchor_alignment_execution_manifest_candidate_r2.json"
    )
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["expected_case_rounds"] == 14
    assert manifest["stop_remaining_percent_lte"] == 60.0
    assert manifest["r1_outputs_reused"] is False
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


def test_r2_fake_transport_persists_and_revalidates(tmp_path: Path) -> None:
    execution = load_json(
        ARTIFACT_DIR / "legacy_anchor_alignment_execution_manifest_candidate_r2.json"
    )
    row = execution["rows"][0]
    input_path = ARTIFACT_DIR / execution["input_packet_directory"] / row["input_packet_file"]
    packet = load_json(input_path)
    response = selected_response(packet, conflict=True)
    usage = tmp_path / "usage.json"
    usage.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 10,
                "remaining_percent": 90,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "legacy-anchor-r2-test",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "outputs" / f"{row['review_id']}.json"
    receipt = tmp_path / "receipts" / f"{row['review_id']}.legacy_anchor_alignment_r2.json"
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


def test_r2_incomplete_comparator_keeps_legacy_sealed(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_loader(_: Path) -> dict:
        raise AssertionError("legacy answers must remain sealed until all outputs freeze")

    execution = load_json(
        ARTIFACT_DIR / "legacy_anchor_alignment_execution_manifest_candidate_r2.json"
    )
    missing_id = execution["rows"][0]["review_id"]
    missing_output = (
        ARTIFACT_DIR / execution["run_directory"] / "outputs" / f"{missing_id}.json"
    )
    real_is_file = Path.is_file

    def one_output_missing(path: Path) -> bool:
        return False if path == missing_output else real_is_file(path)

    monkeypatch.setattr(Path, "is_file", one_output_missing)
    monkeypatch.setattr(comparator, "load_reference_cases", forbidden_loader)
    report = comparator.build_report(ARTIFACT_DIR)
    assert report["status"].startswith("NOT_READY_OUTPUTS_INCOMPLETE")
    assert report["legacy_answers_loaded"] is False
    assert report["completed_valid_case_count"] == 13
