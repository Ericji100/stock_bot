from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest

from scripts.v2_core_stage_b1a_codex_runner_v1 import raw_output_path
from scripts.v2_core_stage_b1a2_active_link_compare_v1 import compare
from scripts.v2_core_stage_b1a2_active_link_input_v1 import generate_all
from scripts.v2_core_stage_b1a2_active_link_manifest_v1 import generate as generate_manifest
from scripts.v2_core_stage_b1a2_active_link_runner_v1 import (
    MODEL,
    REASONING,
    RunnerError,
    run_case,
    validate_existing_artifacts,
)
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json, validate_response


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)


def valid_response(packet: dict) -> dict:
    rows = []
    for option in packet["role_link_evidence_options"]:
        judgement = "INSUFFICIENT" if option["no_fixed_path_found"] else "SUPPORTS"
        rows.append(
            {
                "candidate_id": option["candidate_id"],
                "active_campaign_link": {
                    "evidence_option_id": option["evidence_option_id"],
                    "judgement": judgement,
                    "reason_code": (
                        "FIXED_CHAIN_EVIDENCE_INSUFFICIENT"
                        if judgement == "INSUFFICIENT"
                        else "VISIBLE_CHAIN_SUPPORTS_ACTIVE_LINK"
                    ),
                },
            }
        )
    return {
        "schema_version": "v2-core-stage-b1a2-active-link-r1-candidate",
        "candidate_link_perceptions": rows,
    }


def formal_paths() -> dict[str, Path]:
    manifest = load_json(ARTIFACT_DIR / "stage_b1a2_active_link_execution_manifest_candidate_r1.json")
    row = manifest["rows"][0]
    return {
        "manifest": ARTIFACT_DIR / manifest["input_manifest_file"],
        "input": ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"],
        "schema": ARTIFACT_DIR / manifest["schema_file"],
        "prompt": ARTIFACT_DIR / manifest["prompt_file"],
    }


def attestation(path: Path, remaining: float) -> None:
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 100 - remaining,
                "remaining_percent": remaining,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "test-b1a2-active-link",
            }
        ),
        encoding="utf-8",
    )


def test_inputs_are_bound_to_passed_r4_and_contain_only_fixed_paths() -> None:
    manifest = generate_all(ARTIFACT_DIR)
    assert manifest["case_count"] == 4
    assert manifest["candidate_count"] == 16
    assert manifest["source_r4_final_report_sha256"] == load_json(
        ARTIFACT_DIR / "stage_b1a2_active_link_input_manifest_candidate_r1.json"
    )["source_r4_final_report_sha256"]
    for row in manifest["rows"]:
        packet = load_json(ARTIFACT_DIR / manifest["input_packet_directory"] / row["input_packet_file"])
        assert set(packet["upstream_partial_eligible_candidate_ids"]) == {
            value["candidate_id"] for value in packet["candidate_segments"]
        }
        terminal_ids = {value["candidate_id"] for value in packet["current_context_segments"]}
        for option in packet["role_link_evidence_options"]:
            for path in option["fixed_relation_paths"]:
                assert path["segment_ids"][0] == option["candidate_id"]
                assert path["terminal_segment_id"] in terminal_ids
                assert len(path["relation_ids"]) <= 4
        serialized = json.dumps(packet, ensure_ascii=False)
        assert "MFE" not in serialized and "MAE" not in serialized


def test_validator_derives_shortlist_without_assigning_roles() -> None:
    paths = formal_paths()
    packet = load_json(paths["input"])
    result = validate_response(
        response=valid_response(packet), schema=load_json(paths["schema"]), packet=packet
    )
    assert result["status"] == "VALID"
    assert result["validator_contract"]["course_role_assigned"] is False
    assert result["validator_contract"]["controlling_anchor_decided"] is False
    expected = sorted(
        value["candidate_id"]
        for value in packet["role_link_evidence_options"]
        if not value["no_fixed_path_found"]
    )
    assert result["b1a2_shortlist_ids"] == expected


def test_validator_rejects_free_support_when_fixed_path_is_missing() -> None:
    paths = formal_paths()
    packet = load_json(paths["input"])
    packet = deepcopy(packet)
    option = packet["role_link_evidence_options"][0]
    option["fixed_relation_paths"] = []
    option["no_fixed_path_found"] = True
    response = valid_response(packet)
    response["candidate_link_perceptions"][0]["active_campaign_link"] = {
        "evidence_option_id": option["evidence_option_id"],
        "judgement": "SUPPORTS",
        "reason_code": "VISIBLE_CHAIN_SUPPORTS_ACTIVE_LINK",
    }
    result = validate_response(response=response, schema=load_json(paths["schema"]), packet=packet)
    assert result["status"] == "INVALID"
    assert any("without a fixed path" in error for error in result["errors"])


def test_fake_transport_and_budget_gate(tmp_path: Path) -> None:
    paths = formal_paths()
    packet = load_json(paths["input"])
    response = valid_response(packet)
    usage = tmp_path / "usage.json"
    attestation(usage, 7)
    output = tmp_path / "stage_b1a2_active_link" / "case.json"
    receipt = tmp_path / "receipts" / "case.json"
    commands = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        target = Path(command[command.index("--output-last-message") + 1])
        target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = run_case(
        input_packet_path=paths["input"],
        input_manifest_path=paths["manifest"],
        schema_path=paths["schema"],
        prompt_path=paths["prompt"],
        usage_attestation_path=usage,
        stop_remaining_percent_lte=3,
        output_path=output,
        receipt_path=receipt,
        timeout_seconds=30,
        run_command=fake_run,
    )
    assert result["status"] == "VALID"
    assert raw_output_path(output).is_file()
    assert validate_existing_artifacts(
        input_packet_path=paths["input"],
        input_manifest_path=paths["manifest"],
        schema_path=paths["schema"],
        prompt_path=paths["prompt"],
        output_path=output,
        receipt_path=receipt,
    ) == result
    assert commands[0][commands[0].index("--model") + 1] == MODEL
    assert f'model_reasoning_effort="{REASONING}"' in commands[0]

    stopped = tmp_path / "stopped.json"
    attestation(stopped, 3)
    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_case(
            input_packet_path=paths["input"],
            input_manifest_path=paths["manifest"],
            schema_path=paths["schema"],
            prompt_path=paths["prompt"],
            usage_attestation_path=stopped,
            stop_remaining_percent_lte=3,
            output_path=tmp_path / "never.json",
            receipt_path=tmp_path / "never-receipt.json",
            timeout_seconds=30,
            run_command=fake_run,
        )


def test_report_matches_actual_progress_and_never_publishes_partial_metrics() -> None:
    generate_manifest(ARTIFACT_DIR)
    report = compare(
        artifact_dir=ARTIFACT_DIR,
        manifest_path=ARTIFACT_DIR / "stage_b1a2_active_link_execution_manifest_candidate_r1.json",
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
