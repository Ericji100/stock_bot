from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest

from scripts.v2_core_stage_b1a_codex_runner_v1 import raw_output_path
from scripts.v2_core_stage_b1a1e_primary_gate_compare_v1 import compare
from scripts.v2_core_stage_b1a1e_primary_gate_input_v1 import generate_all
from scripts.v2_core_stage_b1a1e_primary_gate_runner_v1 import (
    MODEL,
    REASONING,
    RunnerError,
    rendered_prompt,
    run_case,
    validate_existing_artifacts,
)
from scripts.v2_core_stage_b1a1e_primary_gate_validator_v1 import (
    load_json,
    validate_response,
)


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


def _valid_response(packet: dict) -> dict:
    option_by_candidate = {
        row["candidate_id"]: row["evidence_option_id"]
        for row in packet["evidence_options"]
    }
    return {
        "schema_version": "v2-core-stage-b1a1e-primary-gate-r4-candidate",
        "candidate_perceptions": [
            {
                "candidate_id": row["candidate_id"],
                "directional_path": {
                    "evidence_option_id": option_by_candidate[row["candidate_id"]],
                    "judgement": "SUPPORTS",
                    "reason_code": "VISIBLE_EVIDENCE_SUPPORTS",
                },
            }
            for row in packet["selected_focus_segments"]
        ],
    }


def _formal_paths() -> dict[str, Path]:
    manifest = load_json(
        ARTIFACT_DIR / "stage_b1a1e_primary_gate_execution_manifest_candidate_r4.json"
    )
    row = manifest["rows"][0]
    return {
        "manifest": ARTIFACT_DIR
        / "stage_b1a1e_primary_gate_input_manifest_candidate_r4.json",
        "input": ARTIFACT_DIR
        / manifest["input_packet_directory"]
        / row["input_packet_file"],
        "gate": ARTIFACT_DIR
        / manifest["program_gate_directory"]
        / row["program_gate_file"],
        "schema": ARTIFACT_DIR
        / "v2_core_stage_b1a1e_primary_gate.schema.candidate_r4.json",
        "prompt": ARTIFACT_DIR
        / "v2_core_stage_b1a1e_primary_gate.prompt.candidate_r4.md",
    }


def _attestation(path: Path, remaining: float) -> None:
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 100 - remaining,
                "remaining_percent": remaining,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "test-primary-gate-r4",
            }
        ),
        encoding="utf-8",
    )


def test_r4_inputs_are_fresh_direction_only_and_gate_is_separate() -> None:
    manifest = generate_all(ARTIFACT_DIR)
    assert manifest["case_count"] == 4
    assert manifest["selected_candidate_count"] == 24
    prior_ids = set()
    for name in (
        "stage_b1a1e_smoke_execution_manifest_candidate_r2.json",
        "stage_b1a1e_validation_execution_manifest_r1.json",
        "stage_b1a1e_primary_execution_manifest_candidate_r3.json",
    ):
        prior_ids.update(row["review_id"] for row in load_json(ARTIFACT_DIR / name)["rows"])
    assert not prior_ids.intersection(row["review_id"] for row in manifest["rows"])
    for row in manifest["rows"]:
        packet = load_json(
            ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
        )
        gate = load_json(
            ARTIFACT_DIR / manifest["program_gate_directory"] / row["program_gate_file"]
        )
        assert len(packet["selected_focus_segments"]) == 6
        assert {value["scale"] for value in packet["selected_focus_segments"]} <= {
            "LARGE",
            "SMALL",
        }
        assert len(packet["evidence_options"]) == 6
        assert {value["atom_name"] for value in packet["evidence_options"]} == {
            "directional_coherence"
        }
        assert "candidate_rows" not in packet
        assert {value["candidate_id"] for value in gate["candidate_rows"]} == {
            value["candidate_id"] for value in packet["selected_focus_segments"]
        }


def test_r4_validator_derives_gate_without_upgrading_ai() -> None:
    manifest = load_json(
        ARTIFACT_DIR / "stage_b1a1e_primary_gate_input_manifest_candidate_r4.json"
    )
    row = manifest["rows"][0]
    packet = load_json(
        ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
    )
    gate = load_json(
        ARTIFACT_DIR / manifest["program_gate_directory"] / row["program_gate_file"]
    )
    schema = load_json(
        ARTIFACT_DIR / "v2_core_stage_b1a1e_primary_gate.schema.candidate_r4.json"
    )
    result = validate_response(
        response=_valid_response(packet),
        schema=schema,
        packet=packet,
        program_gate=gate,
    )
    assert result["status"] == "VALID"
    assert result["validator_contract"]["program_gate_hidden_from_ai"] is True
    expected = sorted(
        value["candidate_id"]
        for value in gate["candidate_rows"]
        if value["objective_same_scale_contact"]
    )
    assert result["partial_eligible_ids"] == expected

    bad_gate = deepcopy(gate)
    bad_gate["candidate_rows"][0]["candidate_scale"] = "AUXILIARY"
    invalid = validate_response(
        response=_valid_response(packet),
        schema=schema,
        packet=packet,
        program_gate=bad_gate,
    )
    assert invalid["status"] == "INVALID"
    assert any("forbidden scale" in error for error in invalid["errors"])


def test_r4_prompt_view_cannot_see_program_gate() -> None:
    manifest = load_json(
        ARTIFACT_DIR / "stage_b1a1e_primary_gate_input_manifest_candidate_r4.json"
    )
    row = manifest["rows"][0]
    packet = load_json(
        ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"]
    )
    prompt = rendered_prompt("PROMPT", packet)
    assert "objective_same_scale_contact" not in prompt
    assert "supporting_evidence_option_ids" not in prompt
    assert "program_gate_version" not in prompt


def test_r4_report_matches_actual_progress_and_never_publishes_partial_metrics() -> None:
    report = compare(
        artifact_dir=ARTIFACT_DIR,
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1e_primary_gate_execution_manifest_candidate_r4.json",
    )
    if report["completed_case_rounds"] < report["expected_case_rounds"]:
        assert report["status"] == "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）"
        assert report["metrics_published"] is False
        assert report["metrics"] is None
    else:
        assert report["completed_case_rounds"] == report["expected_case_rounds"]
        assert report["status"] in {
            "SMOKE_PASSED（Smoke通過）",
            "SMOKE_REVISION_REQUIRED（Smoke需要修訂）",
            "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）",
        }
        assert report["metrics_published"] is True
        assert report["metrics"] is not None


def test_r4_fake_transport_persists_and_revalidates(tmp_path: Path) -> None:
    paths = _formal_paths()
    packet = load_json(paths["input"])
    response = _valid_response(packet)
    usage = tmp_path / "usage.json"
    _attestation(usage, 23)
    output = tmp_path / "stage_b1a1e_primary_gate" / "case.json"
    receipt = tmp_path / "receipts" / "case.json"
    commands = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        target = Path(command[command.index("--output-last-message") + 1])
        target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = run_case(
        input_packet_path=paths["input"],
        program_gate_path=paths["gate"],
        input_manifest_path=paths["manifest"],
        schema_path=paths["schema"],
        prompt_path=paths["prompt"],
        usage_attestation_path=usage,
        stop_remaining_percent_lte=20,
        output_path=output,
        receipt_path=receipt,
        timeout_seconds=30,
        run_command=fake_run,
    )
    assert result["status"] == "VALID"
    assert raw_output_path(output).is_file()
    assert validate_existing_artifacts(
        input_packet_path=paths["input"],
        program_gate_path=paths["gate"],
        input_manifest_path=paths["manifest"],
        schema_path=paths["schema"],
        prompt_path=paths["prompt"],
        output_path=output,
        receipt_path=receipt,
    ) == result
    assert commands[0][commands[0].index("--model") + 1] == MODEL
    assert f'model_reasoning_effort="{REASONING}"' in commands[0]


def test_r4_budget_stop_happens_before_transport(tmp_path: Path) -> None:
    paths = _formal_paths()
    usage = tmp_path / "usage.json"
    _attestation(usage, 20)
    called = False

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_case(
            input_packet_path=paths["input"],
            program_gate_path=paths["gate"],
            input_manifest_path=paths["manifest"],
            schema_path=paths["schema"],
            prompt_path=paths["prompt"],
            usage_attestation_path=usage,
            stop_remaining_percent_lte=20,
            output_path=tmp_path / "output.json",
            receipt_path=tmp_path / "receipt.json",
            timeout_seconds=30,
            run_command=fake_run,
        )
    assert called is False
