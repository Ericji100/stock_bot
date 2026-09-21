from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest

from scripts.v2_core_stage_b1a_codex_runner_v1 import (
    MODEL,
    REASONING,
    StageB1aRunnerError,
    invalid_output_path,
    raw_output_path,
    run_stage_b1a_case,
    sha256_file,
    validate_existing_stage_b1a_artifacts,
)


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


def _paths():
    manifest = json.loads(
        (ARTIFACT_DIR / "stage_b1a_input_manifest_candidate_r1.json").read_text(
            encoding="utf-8"
        )
    )
    row = manifest["rows"][0]
    review_id = row["review_id"]
    return {
        "input_packet": ARTIFACT_DIR
        / "stage_b1a_input_packets_candidate_r1"
        / row["input_packet_file"],
        "candidates": ARTIFACT_DIR
        / "objective_candidate_catalogs_r1"
        / f"{review_id}.json",
        "focus": ARTIFACT_DIR / "objective_focus_catalogs_r1" / f"{review_id}.json",
        "schema": ARTIFACT_DIR / "v2_core_stage_b1a.schema.candidate_r1.json",
        "prompt": ARTIFACT_DIR / "v2_core_stage_b1a.prompt.candidate_r1.md",
    }


def _unknown_atomic():
    return {
        "result": "UNKNOWN",
        "supporting_evidence_refs": [],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": ["OTHER_REQUIRED_EVIDENCE"],
        "reason_code": "REQUIRED_EVIDENCE_MISSING",
    }


def _valid_response(focus_path: Path):
    focus = json.loads(focus_path.read_text(encoding="utf-8"))
    return {
        "schema_version": "v2-core-stage-b1a-r1-candidate",
        "segment_assessments": [
            {
                "candidate_id": row["segment_id"],
                "coarse_anchor_fit": _unknown_atomic(),
                "as_of_structural_relevance": _unknown_atomic(),
                "role_assignability": _unknown_atomic(),
                "role_candidates": [],
            }
            for row in focus["focus_segments"]
        ],
    }


def _attestation(path: Path, remaining: float):
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 100 - remaining,
                "remaining_percent": remaining,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": "test-attestation",
            }
        ),
        encoding="utf-8",
    )


def test_fake_transport_preserves_raw_and_revalidates(tmp_path: Path):
    paths = _paths()
    response = _valid_response(paths["focus"])
    usage = tmp_path / "usage.json"
    _attestation(usage, 72)
    output = tmp_path / "stage_b1a" / "case.json"
    receipt = tmp_path / "receipts" / "case.json"
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        target = Path(command[command.index("--output-last-message") + 1])
        target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = run_stage_b1a_case(
        input_packet_path=paths["input_packet"],
        candidate_catalog_path=paths["candidates"],
        focus_catalog_path=paths["focus"],
        schema_path=paths["schema"],
        prompt_path=paths["prompt"],
        usage_attestation_path=usage,
        stop_remaining_percent_lte=70,
        output_path=output,
        receipt_path=receipt,
        timeout_seconds=30,
        run_command=fake_run,
    )
    assert result["status"] == "VALID"
    assert result["deep_review_pool_count"] == 0
    assert raw_output_path(output).is_file()
    assert receipt.is_file()
    assert validate_existing_stage_b1a_artifacts(
        input_packet_path=paths["input_packet"],
        candidate_catalog_path=paths["candidates"],
        focus_catalog_path=paths["focus"],
        schema_path=paths["schema"],
        prompt_path=paths["prompt"],
        output_path=output,
        receipt_path=receipt,
    ) == result
    assert commands[0][commands[0].index("--model") + 1] == MODEL
    assert f'model_reasoning_effort="{REASONING}"' in commands[0]


def test_local_semantic_failure_is_preserved_without_repair(tmp_path: Path):
    paths = _paths()
    response = _valid_response(paths["focus"])
    response["segment_assessments"].append(dict(response["segment_assessments"][0]))
    usage = tmp_path / "usage.json"
    _attestation(usage, 72)
    output = tmp_path / "stage_b1a" / "invalid.json"

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        target = Path(command[command.index("--output-last-message") + 1])
        target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(StageB1aRunnerError, match="local validation failed"):
        run_stage_b1a_case(
            input_packet_path=paths["input_packet"],
            candidate_catalog_path=paths["candidates"],
            focus_catalog_path=paths["focus"],
            schema_path=paths["schema"],
            prompt_path=paths["prompt"],
            usage_attestation_path=usage,
            stop_remaining_percent_lte=70,
            output_path=output,
            receipt_path=tmp_path / "receipt.json",
            timeout_seconds=30,
            run_command=fake_run,
        )
    assert invalid_output_path(output).is_file()
    assert not output.exists()


def test_budget_at_threshold_stops_before_transport(tmp_path: Path):
    paths = _paths()
    usage = tmp_path / "usage.json"
    _attestation(usage, 70)
    called = False

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(StageB1aRunnerError, match="BUDGET_STOP"):
        run_stage_b1a_case(
            input_packet_path=paths["input_packet"],
            candidate_catalog_path=paths["candidates"],
            focus_catalog_path=paths["focus"],
            schema_path=paths["schema"],
            prompt_path=paths["prompt"],
            usage_attestation_path=usage,
            stop_remaining_percent_lte=70,
            output_path=tmp_path / "output.json",
            receipt_path=tmp_path / "receipt.json",
            timeout_seconds=30,
            run_command=fake_run,
        )
    assert called is False


def test_tampered_normalized_output_fails_receipt_revalidation(tmp_path: Path):
    paths = _paths()
    response = _valid_response(paths["focus"])
    usage = tmp_path / "usage.json"
    _attestation(usage, 72)
    output = tmp_path / "stage_b1a" / "case.json"
    receipt = tmp_path / "receipts" / "case.json"

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        target = Path(command[command.index("--output-last-message") + 1])
        target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    run_stage_b1a_case(
        input_packet_path=paths["input_packet"],
        candidate_catalog_path=paths["candidates"],
        focus_catalog_path=paths["focus"],
        schema_path=paths["schema"],
        prompt_path=paths["prompt"],
        usage_attestation_path=usage,
        stop_remaining_percent_lte=70,
        output_path=output,
        receipt_path=receipt,
        timeout_seconds=30,
        run_command=fake_run,
    )
    changed = json.loads(output.read_text(encoding="utf-8"))
    changed["segment_assessments"].reverse()
    output.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(StageB1aRunnerError, match="normalized_output_sha256"):
        validate_existing_stage_b1a_artifacts(
            input_packet_path=paths["input_packet"],
            candidate_catalog_path=paths["candidates"],
            focus_catalog_path=paths["focus"],
            schema_path=paths["schema"],
            prompt_path=paths["prompt"],
            output_path=output,
            receipt_path=receipt,
        )


def test_smoke_manifest_is_hash_bound_and_not_started():
    manifest = json.loads(
        (ARTIFACT_DIR / "stage_b1a_smoke_execution_manifest_candidate_r1.json").read_text(
            encoding="utf-8"
        )
    )
    input_manifest = json.loads(
        (ARTIFACT_DIR / "stage_b1a_input_manifest_candidate_r1.json").read_text(
            encoding="utf-8"
        )
    )
    expected = sorted(input_manifest["rows"], key=lambda row: row["review_id"])[:4]
    assert manifest["ready_for_formal_ai"] is False
    assert manifest["completed_case_rounds"] == 0
    assert manifest["expected_case_rounds"] == 12
    assert [row["review_id"] for row in manifest["rows"]] == [
        row["review_id"] for row in expected
    ]
    assert sha256_file(ARTIFACT_DIR / manifest["prompt_file"]) == manifest["prompt_sha256"]
    assert sha256_file(ARTIFACT_DIR / manifest["output_schema_file"]) == manifest[
        "output_schema_sha256"
    ]
    assert sha256_file(ARTIFACT_DIR / manifest["input_manifest_file"]) == manifest[
        "input_manifest_sha256"
    ]
    assert sha256_file(ARTIFACT_DIR / manifest["smoke_gate_file"]) == manifest[
        "smoke_gate_sha256"
    ]
    for filename, expected_hash in manifest["repo_components"].items():
        assert sha256_file(ROOT / filename) == expected_hash
    for row in manifest["rows"]:
        assert sha256_file(
            ARTIFACT_DIR / "stage_b1a_input_packets_candidate_r1" / row["input_packet_file"]
        ) == row["input_packet_sha256"]
