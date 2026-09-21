from scripts.v2_core_triplicate_preflight_v3 import OUT, build, validate


def test_v8_preflight_is_isolated_and_recomputes_all_fingerprints() -> None:
    manifest = build(OUT)
    assert validate(manifest, OUT) == []
    assert manifest["execution_manifest_version"] == "v2-core-m2a-triplicate-execution-r8-candidate"
    assert manifest["run_directory"] == "feasibility_probe_runs_r8"
    assert manifest["case_count"] == 48
    assert len(manifest["shards"]) == 12
    assert manifest["stage_runners"]["STAGE_D"].endswith("v2_core_codex_stage_d_runner_v3.py")
    assert len({row["frozen_input_fingerprint"] for row in manifest["cases"]}) == 48
    assert len({row["input_matrix_sha256"] for row in manifest["rounds"]}) == 1
