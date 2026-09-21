from __future__ import annotations

from scripts.v2_core_triplicate_preflight_v2 import OUT, build, validate


def test_v7_preflight_freezes_same_48_cases_with_split_stage_runners() -> None:
    manifest = build(OUT)
    assert validate(manifest, OUT) == []
    assert manifest["execution_manifest_version"] == "v2-core-m2a-triplicate-execution-r7-candidate"
    assert manifest["run_directory"] == "feasibility_probe_runs_r7"
    assert manifest["case_count"] == 48
    assert manifest["round_count"] == 3
    assert len(manifest["shards"]) == 12
    assert manifest["stage_runners"]["STAGE_B"].endswith("v2_core_codex_staged_runner_v1.py")
    assert manifest["stage_runners"]["STAGE_D"].endswith("v2_core_codex_stage_d_runner_v2.py")
    assert "stage_b_output_sha256" in manifest["program_bound_stage_d_fields"]


def test_v7_preflight_keeps_labels_and_future_performance_hidden() -> None:
    manifest = build(OUT)
    assert manifest["selection_labels_visible"] is False
    assert manifest["future_performance_visible"] is False
    assert len({row["frozen_input_fingerprint"] for row in manifest["cases"]}) == 48
    assert len({row["input_matrix_sha256"] for row in manifest["rounds"]}) == 1
