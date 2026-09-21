from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from scripts import v2_core_legacy_lifecycle_runner_v6 as r6
from scripts.v2_core_legacy_lifecycle_manifest_v6b import build_manifest
from scripts.v2_core_legacy_lifecycle_runner_v6b import (
    MODEL,
    REASONING,
    RUNNER_VERSION,
    run_b0a_case,
    run_b0b_case,
    validate_existing_b0a,
    validate_existing_b0b,
)
from tests.test_v2_core_legacy_lifecycle_b0a_r6 import (
    ARTIFACT_DIR,
    B0B_PROMPT,
    B0B_SCHEMA,
    B0B_TRUTH_TABLE,
    PROMPT,
    SCHEMA,
    TRUTH_TABLE,
    _write_runner_inputs,
    _write_usage,
    fresh_response,
    fresh_roles_response,
    load_json,
)


ROOT = Path(__file__).resolve().parents[1]


def test_r6b_direct_cli_bootstraps_from_script_entry() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "v2_core_legacy_lifecycle_runner_v6b.py"),
            "--help",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "{b0a,b0b}" in completed.stdout


def test_r6b_two_stage_direct_contract_revalidates(tmp_path: Path) -> None:
    b0a_response, packet_value = fresh_response()
    b0b_response = fresh_roles_response(packet_value)
    packet_path, manifest_path = _write_runner_inputs(tmp_path, packet_value)
    b0a_output = tmp_path / "b0a" / "FP-synthetic-r6b.json"
    b0a_receipt = tmp_path / "b0a-receipts" / "FP-synthetic-r6b.json"
    b0b_output = tmp_path / "b0b" / "FP-synthetic-r6b.json"
    b0b_receipt = tmp_path / "b0b-receipts" / "FP-synthetic-r6b.json"

    def fake_for(payload: dict):
        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            target = Path(command[command.index("--output-last-message") + 1])
            target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        return fake_run

    b0a_validation = run_b0a_case(
        input_packet_path=packet_path,
        input_manifest_path=manifest_path,
        schema_path=SCHEMA,
        prompt_path=PROMPT,
        lifecycle_truth_table_path=TRUTH_TABLE,
        usage_attestation_path=_write_usage(tmp_path, "r6b-b0a"),
        stop_remaining_percent_lte=5,
        output_path=b0a_output,
        receipt_path=b0a_receipt,
        timeout_seconds=30,
        run_command=fake_for(b0a_response),
    )
    assert load_json(b0a_receipt)["runner_version"] == RUNNER_VERSION
    assert validate_existing_b0a(
        input_packet_path=packet_path,
        input_manifest_path=manifest_path,
        schema_path=SCHEMA,
        prompt_path=PROMPT,
        lifecycle_truth_table_path=TRUTH_TABLE,
        output_path=b0a_output,
        receipt_path=b0a_receipt,
    ) == b0a_validation

    b0b_validation = run_b0b_case(
        input_packet_path=packet_path,
        input_manifest_path=manifest_path,
        b0a_schema_path=SCHEMA,
        b0a_prompt_path=PROMPT,
        lifecycle_truth_table_path=TRUTH_TABLE,
        b0a_output_path=b0a_output,
        b0a_receipt_path=b0a_receipt,
        b0b_schema_path=B0B_SCHEMA,
        b0b_prompt_path=B0B_PROMPT,
        role_truth_table_path=B0B_TRUTH_TABLE,
        usage_attestation_path=_write_usage(tmp_path, "r6b-b0b"),
        stop_remaining_percent_lte=5,
        output_path=b0b_output,
        receipt_path=b0b_receipt,
        timeout_seconds=30,
        run_command=fake_for(b0b_response),
    )
    assert load_json(b0b_receipt)["runner_version"] == RUNNER_VERSION
    assert validate_existing_b0b(
        input_packet_path=packet_path,
        input_manifest_path=manifest_path,
        b0a_schema_path=SCHEMA,
        b0a_prompt_path=PROMPT,
        lifecycle_truth_table_path=TRUTH_TABLE,
        b0a_output_path=b0a_output,
        b0a_receipt_path=b0a_receipt,
        b0b_schema_path=B0B_SCHEMA,
        b0b_prompt_path=B0B_PROMPT,
        role_truth_table_path=B0B_TRUTH_TABLE,
        output_path=b0b_output,
        receipt_path=b0b_receipt,
    ) == b0b_validation
    assert r6.RUNNER_VERSION == "v2-core-legacy-lifecycle-runner-r6-candidate"


def test_r6b_manifest_is_bootstrap_only() -> None:
    manifest = build_manifest(ARTIFACT_DIR)
    r6_manifest = load_json(
        ARTIFACT_DIR / "legacy_lifecycle_execution_manifest_candidate_r6.json"
    )
    r6a_manifest = load_json(
        ARTIFACT_DIR / "legacy_lifecycle_execution_manifest_candidate_r6a.json"
    )
    assert manifest["bootstrap_only_revision_from_r6a"] is True
    assert manifest["transport_adapter_changed_from_r6a"] is False
    assert manifest["prior_r6a_api_call_count"] == 0
    assert manifest["formal_model"] == MODEL
    assert manifest["reasoning_effort"] == REASONING
    assert manifest["protocol_probe_review_id"] == r6a_manifest["protocol_probe_review_id"]
    assert manifest["run_directory"] != r6a_manifest["run_directory"]
    for key in (
        "recovery_design",
        "teacher_distillation_design",
        "teacher_pairwise_audit",
        "selection",
        "input_manifest",
        "b0a_prompt",
        "b0a_schema",
        "lifecycle_truth_table",
        "b0b_prompt",
        "b0b_schema",
        "role_truth_table",
        "b0a_validator",
        "b0b_validator",
        "operational_policy",
    ):
        assert manifest[f"{key}_sha256"] == r6_manifest[f"{key}_sha256"]
        assert manifest[f"{key}_sha256"] == r6a_manifest[f"{key}_sha256"]
    assert manifest["transport_runner_sha256"] == r6a_manifest["runner_sha256"]
    assert manifest["base_runner_sha256"] == r6_manifest["runner_sha256"]
