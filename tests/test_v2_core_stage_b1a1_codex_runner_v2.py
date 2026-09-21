from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest

from scripts.v2_core_stage_b1a1_candidate_validator_v2 import ATOMIC_FIELDS
from scripts.v2_core_stage_b1a1_codex_runner_v2 import (
    MODEL,
    REASONING,
    StageB1a1RunnerError,
    run_stage_b1a1_case,
    validate_existing_stage_b1a1_artifacts,
)
from scripts.v2_core_stage_b1a_codex_runner_v1 import raw_output_path


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
        (ARTIFACT_DIR / "stage_b1a1_input_manifest_candidate_r2.json").read_text(
            encoding="utf-8"
        )
    )
    row = manifest["rows"][0]
    review_id = row["review_id"]
    return {
        "input": ARTIFACT_DIR / "stage_b1a1_input_packets_candidate_r2" / row[
            "input_packet_file"
        ],
        "source": ARTIFACT_DIR
        / "stage_b1a_input_packets_candidate_r1"
        / f"{review_id}.json",
        "relation": ARTIFACT_DIR / "objective_relation_catalogs_r1" / f"{review_id}.json",
        "focus": ARTIFACT_DIR / "objective_focus_catalogs_r1" / f"{review_id}.json",
        "schema": ARTIFACT_DIR / "v2_core_stage_b1a1.schema.candidate_r2.json",
        "prompt": ARTIFACT_DIR / "v2_core_stage_b1a1.prompt.candidate_r2.md",
    }


def _unknown():
    return {
        "result": "UNKNOWN",
        "primary_evidence_refs": [],
        "supporting_evidence_refs": [],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": ["MISSING_FIXED_RELATION"],
        "reason_code": "REQUIRED_EVIDENCE_MISSING",
    }


def _response(input_path: Path):
    packet = json.loads(input_path.read_text(encoding="utf-8"))
    return {
        "schema_version": "v2-core-stage-b1a1-r2-candidate",
        "candidate_assessments": [
            {
                "candidate_id": row["candidate_id"],
                **{field: _unknown() for field in ATOMIC_FIELDS},
            }
            for row in packet["focus_segments"]
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
                "check_id": "test-b1a1-r2",
            }
        ),
        encoding="utf-8",
    )


def test_fake_transport_persists_and_revalidates_r2(tmp_path: Path):
    paths = _paths()
    response = _response(paths["input"])
    usage = tmp_path / "usage.json"
    _attestation(usage, 25)
    output = tmp_path / "stage_b1a1" / "case.json"
    receipt = tmp_path / "receipts" / "case.json"
    commands = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        target = Path(command[command.index("--output-last-message") + 1])
        target.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = run_stage_b1a1_case(
        input_packet_path=paths["input"],
        source_b1a_input_path=paths["source"],
        relation_catalog_path=paths["relation"],
        focus_catalog_path=paths["focus"],
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
    assert result["eligible_count"] == 0
    assert raw_output_path(output).is_file()
    assert validate_existing_stage_b1a1_artifacts(
        input_packet_path=paths["input"],
        source_b1a_input_path=paths["source"],
        relation_catalog_path=paths["relation"],
        focus_catalog_path=paths["focus"],
        schema_path=paths["schema"],
        prompt_path=paths["prompt"],
        output_path=output,
        receipt_path=receipt,
    ) == result
    assert commands[0][commands[0].index("--model") + 1] == MODEL
    assert f'model_reasoning_effort="{REASONING}"' in commands[0]


def test_budget_threshold_stops_before_r2_transport(tmp_path: Path):
    paths = _paths()
    usage = tmp_path / "usage.json"
    _attestation(usage, 20)
    called = False

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(StageB1a1RunnerError, match="BUDGET_STOP"):
        run_stage_b1a1_case(
            input_packet_path=paths["input"],
            source_b1a_input_path=paths["source"],
            relation_catalog_path=paths["relation"],
            focus_catalog_path=paths["focus"],
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
