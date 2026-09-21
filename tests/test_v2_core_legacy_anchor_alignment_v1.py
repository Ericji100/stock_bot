from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone

import pytest

from scripts import v2_core_legacy_anchor_alignment_compare_v1 as comparator
from scripts.v2_core_legacy_anchor_alignment_validator_v1 import validate_response
from scripts.v2_core_legacy_anchor_alignment_runner_v1 import (
    MODEL,
    REASONING,
    run_case,
    validate_existing_artifacts,
)


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atom(judgement: str, option: str) -> dict:
    return {"judgement": judgement, "evidence_option_id": option}


def selected_response(packet: dict) -> dict:
    candidate = packet["candidate_pool"][0]
    option = candidate["evidence_option_id"]
    if candidate["direction"] == "DOWN":
        scenario = "BEAR_REVERSAL_LEFT_RIGHT"
        reason = "ACTIVE_DOWN_ANCHOR_CONTROLS"
    else:
        scenario = "FRESH_Q1_EXPANSION"
        reason = "FORMING_UP_REPLACES_DOWN_CONTROL"
    return {
        "schema_version": "v2-core-legacy-anchor-alignment-r1-candidate",
        "selection": {
            "selection_status": "SELECTED",
            "data_sufficiency": atom("PASS", option),
            "controlling_candidate_id": candidate["candidate_id"],
            "controlling_evidence_option_id": option,
            "alternative_candidate_ids": [],
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
            "alternative_conflict": False,
            "confidence": "MEDIUM",
            "uncertainty_codes": ["NONE"],
            "causal_attestation": {
                "as_of_only": True,
                "no_identity": True,
                "no_legacy_answer": True,
                "no_future_performance": True,
                "fixed_candidates_only": True,
            },
        },
    }


def unresolved_response() -> dict:
    unknown = atom("UNKNOWN", "NONE")
    return {
        "schema_version": "v2-core-legacy-anchor-alignment-r1-candidate",
        "selection": {
            "selection_status": "UNRESOLVED",
            "data_sufficiency": unknown,
            "controlling_candidate_id": "NONE",
            "controlling_evidence_option_id": "NONE",
            "alternative_candidate_ids": [],
            "anchor_quality": {
                "direction_coherent": unknown,
                "anchor_clean": unknown,
                "anchor_meaty": unknown,
                "anchor_destructive": unknown,
                "anchor_traceable": unknown,
                "anchor_completed_fit": unknown,
                "controls_current_context": unknown,
            },
            "control_reason": "INSUFFICIENT_CAUSAL_EVIDENCE",
            "recommended_scenario_family": "UNRESOLVED_NO_TRADE",
            "alternative_conflict": True,
            "confidence": "LOW",
            "uncertainty_codes": ["CURRENT_CONTROL_AMBIGUOUS"],
            "causal_attestation": {
                "as_of_only": True,
                "no_identity": True,
                "no_legacy_answer": True,
                "no_future_performance": True,
                "fixed_candidates_only": True,
            },
        },
    }


def test_alignment_input_manifest_is_blind_and_hash_bound() -> None:
    manifest = load_json(
        ARTIFACT_DIR / "legacy_anchor_alignment_input_manifest_candidate_r1.json"
    )
    assert manifest["case_count"] == 14
    assert manifest["required_rounds"] == 1
    assert manifest["legacy_answer_values_exposed_to_ai"] is False
    assert manifest["future_performance_used"] is False
    assert manifest["identity_used"] is False
    assert manifest["locked_reproduction_set_opened"] is False
    forbidden = {
        "code",
        "name",
        "symbol",
        "case_role",
        "intended_scenario",
        "expected_permission",
        "legacy_v2_trigger",
        "mfe",
        "mae",
        "pnl",
        "profit",
        "return_pct",
        "exit_date",
        "exit_price",
    }
    for row in manifest["rows"]:
        path = ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
        assert sha256(path) == row["input_packet_sha256"]
        packet = load_json(path)
        assert packet["review_id"] == row["review_id"]
        assert len(packet["candidate_pool"]) == row["candidate_count"]
        assert max(value["date"] for value in packet["daily_context_to_as_of"]) <= packet["as_of"]
        text = path.read_text(encoding="utf-8").lower()
        for key in forbidden:
            assert f'"{key}"' not in text


def test_selected_and_unresolved_contracts_validate() -> None:
    manifest = load_json(
        ARTIFACT_DIR / "legacy_anchor_alignment_input_manifest_candidate_r1.json"
    )
    row = manifest["rows"][0]
    packet = load_json(
        ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
    )
    schema = load_json(
        ARTIFACT_DIR / "v2_core_legacy_anchor_alignment.schema.candidate_r1.json"
    )
    assert validate_response(
        response=selected_response(packet), schema=schema, packet=packet
    )["status"] == "VALID"
    assert validate_response(
        response=unresolved_response(), schema=schema, packet=packet
    )["status"] == "VALID"


def test_validator_rejects_candidate_option_mismatch() -> None:
    manifest = load_json(
        ARTIFACT_DIR / "legacy_anchor_alignment_input_manifest_candidate_r1.json"
    )
    row = manifest["rows"][0]
    packet = load_json(
        ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
    )
    schema = load_json(
        ARTIFACT_DIR / "v2_core_legacy_anchor_alignment.schema.candidate_r1.json"
    )
    response = selected_response(packet)
    response["selection"]["controlling_evidence_option_id"] = packet["candidate_pool"][1][
        "evidence_option_id"
    ]
    result = validate_response(response=response, schema=schema, packet=packet)
    assert result["status"] == "INVALID"
    assert any("does not match" in value for value in result["errors"])


def test_execution_manifest_binds_all_contract_files() -> None:
    manifest = load_json(
        ARTIFACT_DIR / "legacy_anchor_alignment_execution_manifest_candidate_r1.json"
    )
    assert manifest["formal_model"] == "gpt-5.6-sol"
    assert manifest["reasoning_effort"] == "xhigh"
    assert manifest["required_rounds"] == 1
    assert manifest["expected_case_rounds"] == 14
    assert manifest["stop_remaining_percent_lte"] == 60.0
    for key in (
        "design",
        "selection",
        "input_manifest",
        "prompt",
        "schema",
        "operational_policy",
    ):
        path = ARTIFACT_DIR / manifest[f"{key}_file"]
        assert sha256(path) == manifest[f"{key}_sha256"]
    for key in ("runner", "validator", "comparator"):
        path = ROOT / manifest[f"{key}_file"]
        assert sha256(path) == manifest[f"{key}_sha256"]


def test_fake_transport_persists_and_revalidates(tmp_path: Path) -> None:
    execution = load_json(
        ARTIFACT_DIR / "legacy_anchor_alignment_execution_manifest_candidate_r1.json"
    )
    row = execution["rows"][0]
    input_path = ARTIFACT_DIR / execution["input_packet_directory"] / row["input_packet_file"]
    packet = load_json(input_path)
    response = selected_response(packet)
    usage = tmp_path / "usage.json"
    usage.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 10,
                "remaining_percent": 90,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "legacy-anchor-test",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "outputs" / f"{row['review_id']}.json"
    receipt = tmp_path / "receipts" / f"{row['review_id']}.legacy_anchor_alignment.json"
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


def test_incomplete_comparator_does_not_reveal_legacy_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_loader(_: Path) -> dict:
        raise AssertionError("legacy answers must not be loaded before all outputs freeze")

    monkeypatch.setattr(comparator, "load_reference_cases", forbidden_loader)
    report = comparator.build_report(ARTIFACT_DIR)
    assert report["status"].startswith("NOT_READY_OUTPUTS_INCOMPLETE")
    assert report["legacy_answers_loaded"] is False
    assert report["completed_valid_case_count"] == 7
