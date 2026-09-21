from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_stage_b1a_candidate_validator_v1 import sha256_file


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


def test_r2_only_changes_operational_threshold_and_output_directory():
    r1 = json.loads(
        (ARTIFACT_DIR / "stage_b1a_smoke_execution_manifest_candidate_r1.json").read_text(
            encoding="utf-8"
        )
    )
    r2 = json.loads(
        (ARTIFACT_DIR / "stage_b1a_smoke_execution_manifest_candidate_r2.json").read_text(
            encoding="utf-8"
        )
    )
    policy_path = ARTIFACT_DIR / r2["operational_policy_file"]
    policy = json.loads(policy_path.read_text(encoding="utf-8"))

    assert r2["ready_for_formal_ai"] is True
    assert r2["status"] == "READY_FOR_FORMAL_AI（已準備正式AI）"
    assert r2["completed_case_rounds"] == 0
    assert r2["stop_remaining_percent_lte"] == 20
    assert r2["run_directory"] != r1["run_directory"]
    assert sha256_file(ARTIFACT_DIR / r2["supersedes_manifest_file"]) == r2[
        "supersedes_manifest_sha256"
    ]
    assert sha256_file(policy_path) == r2["operational_policy_sha256"]
    assert policy["stop_new_ai_calls_when_remaining_percent_lte"] == 20
    assert policy["scope"] == "stage_b1a_smoke_execution_manifest_candidate_r2.json"

    stable_keys = {
        "formal_model",
        "reasoning_effort",
        "required_rounds",
        "case_count",
        "expected_case_rounds",
        "selection_method",
        "prompt_file",
        "prompt_sha256",
        "output_schema_file",
        "output_schema_sha256",
        "input_manifest_file",
        "input_manifest_sha256",
        "smoke_gate_file",
        "smoke_gate_sha256",
        "repo_components",
        "execution_contract",
        "rows",
    }
    for key in stable_keys:
        assert r2[key] == r1[key]
