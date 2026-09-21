from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_triplicate_preflight_v1 import OUT, validate


MANIFEST_PATH = OUT / "feasibility_probe_execution_manifest_v6.json"


def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_triplicate_preflight_is_frozen_and_valid() -> None:
    value = manifest()
    assert validate(value, OUT) == []
    assert value["status"] == "PREFLIGHT_READY_AI_NOT_STARTED（前置就緒、AI尚未開始）"
    assert value["case_count"] == 48
    assert value["round_count"] == 3
    assert len(value["shards"]) == 12


def test_all_rounds_have_identical_input_matrix_hash() -> None:
    value = manifest()
    hashes = {row["input_matrix_sha256"] for row in value["rounds"]}
    assert hashes == {value["cross_round_input_matrix_sha256"]}
    assert all(row["status"] == "NOT_STARTED（尚未開始）" for row in value["rounds"])


def test_preflight_never_exposes_sealed_labels() -> None:
    text = MANIFEST_PATH.read_text(encoding="utf-8").lower()
    assert "intended_scenario" not in text
    assert "case_role" not in text
    assert "expected_permission" not in text
    assert manifest()["selection_labels_visible"] is False
    assert manifest()["future_performance_visible"] is False


def test_component_or_packet_change_fails_closed() -> None:
    value = manifest()
    value["components"]["permission_truth_table.json"] = "0" * 64
    assert "COMPONENT_HASH_MISMATCH:permission_truth_table.json" in validate(value, OUT)

    value = manifest()
    value["cases"][0]["packet_sha256"] = "0" * 64
    assert any(error.startswith("PACKET_HASH_MISMATCH:") for error in validate(value, OUT))

    value = manifest()
    value["repo_components"]["scripts/v2_core_codex_staged_runner_v1.py"] = "0" * 64
    assert any(error.startswith("REPO_COMPONENT_HASH_MISMATCH:") for error in validate(value, OUT))


def test_budget_and_model_are_exact() -> None:
    value = manifest()
    assert value["formal_model"] == "gpt-5.6-sol"
    assert value["reasoning_effort"] == "xhigh"
    assert value["budget_stop_remaining_percent_lte"] == 5
    assert value["usage_check_required_before_each_shard"] is True
    assert value["execution_manifest_version"] == "v2-core-m2a-triplicate-execution-r6-candidate"
    assert "scripts/v2_core_codex_staged_runner_v1.py" in value["repo_components"]
    assert value["run_directory"] == "feasibility_probe_runs_r6"
    assert all(row["stage_b_output_dir"].startswith("feasibility_probe_runs_r6/") for row in value["rounds"])
