from __future__ import annotations

import json
from pathlib import Path
import subprocess

from jsonschema import Draft202012Validator
import pytest

from scripts import v2_core_legacy_lifecycle_runner_v6 as r6
from scripts.v2_core_legacy_lifecycle_manifest_v6a import build_manifest
from scripts.v2_core_legacy_lifecycle_runner_v6a import (
    MODEL,
    REASONING,
    RUNNER_VERSION,
    RunnerError,
    assert_no_ref_siblings,
    run_b0a_case,
    run_b0b_case,
    transport_schema,
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
    packet,
)


def _walk(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


@pytest.mark.parametrize("schema_path", [SCHEMA, B0B_SCHEMA])
def test_r6a_transport_schema_has_no_ref_siblings_and_is_draft_valid(
    schema_path: Path,
) -> None:
    packet_value = packet()
    rendered = transport_schema(load_json(schema_path), packet_value)
    assert_no_ref_siblings(rendered)
    Draft202012Validator.check_schema(rendered)
    for node in _walk(rendered):
        if isinstance(node, dict) and "$ref" in node:
            assert set(node) == {"$ref"}


@pytest.mark.parametrize("schema_path", [SCHEMA, B0B_SCHEMA])
def test_r6a_supporting_evidence_arrays_are_fully_inlined_and_bounded(
    schema_path: Path,
) -> None:
    packet_value = packet()
    rendered = transport_schema(load_json(schema_path), packet_value)
    expected_refs = {
        ref
        for row in packet_value["candidate_evidence_options"]
        for ref in row["source_evidence_refs"]
    } | {row["ref"] for row in packet_value["proxy_evidence"]}
    evidence_nodes = []
    for node in _walk(rendered):
        if not isinstance(node, dict):
            continue
        properties = node.get("properties")
        if isinstance(properties, dict) and "supporting_packet_evidence_refs" in properties:
            evidence_nodes.append(properties["supporting_packet_evidence_refs"])
    if schema_path == B0B_SCHEMA:
        assert evidence_nodes == []
        return
    assert evidence_nodes
    for node in evidence_nodes:
        assert "$ref" not in node
        assert node["type"] == "array"
        assert node["minItems"] == 0
        assert node["maxItems"] == 8
        assert set(node["items"]["enum"]) == expected_refs


def test_r6a_ref_sibling_guard_rejects_the_r6_failure_shape() -> None:
    with pytest.raises(RunnerError, match="forbidden \\$ref siblings"):
        assert_no_ref_siblings(
            {
                "type": "array",
                "items": {
                    "$ref": "#/$defs/evidenceRef",
                    "items": {"type": "string"},
                },
            }
        )


def test_r6a_two_stage_fake_transport_is_immutable_and_revalidates(
    tmp_path: Path,
) -> None:
    b0a_response, packet_value = fresh_response()
    b0b_response = fresh_roles_response(packet_value)
    packet_path, manifest_path = _write_runner_inputs(tmp_path, packet_value)
    b0a_output = tmp_path / "b0a" / "FP-synthetic-r6a.json"
    b0a_receipt = tmp_path / "b0a-receipts" / "FP-synthetic-r6a.json"
    b0b_output = tmp_path / "b0b" / "FP-synthetic-r6a.json"
    b0b_receipt = tmp_path / "b0b-receipts" / "FP-synthetic-r6a.json"
    calls: list[list[str]] = []

    def fake_for(payload: dict):
        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
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
        usage_attestation_path=_write_usage(tmp_path, "r6a-b0a"),
        stop_remaining_percent_lte=5,
        output_path=b0a_output,
        receipt_path=b0a_receipt,
        timeout_seconds=30,
        run_command=fake_for(b0a_response),
    )
    assert b0a_validation["program_derived_scenario_family"] == "FRESH_Q1_EXPANSION"
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
        usage_attestation_path=_write_usage(tmp_path, "r6a-b0b"),
        stop_remaining_percent_lte=5,
        output_path=b0b_output,
        receipt_path=b0b_receipt,
        timeout_seconds=30,
        run_command=fake_for(b0b_response),
    )
    assert b0b_validation["role_resolution_status"] == "RESOLVED"
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
    assert len(calls) == 2
    assert all(command[command.index("--model") + 1] == MODEL for command in calls)
    assert all(f'model_reasoning_effort="{REASONING}"' in command for command in calls)
    assert r6.RUNNER_VERSION == "v2-core-legacy-lifecycle-runner-r6-candidate"


def test_r6a_budget_gate_blocks_before_transport(tmp_path: Path) -> None:
    _, packet_value = fresh_response()
    packet_path, manifest_path = _write_runner_inputs(tmp_path, packet_value)
    called = False

    def should_not_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("transport must not run at the budget stop")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_b0a_case(
            input_packet_path=packet_path,
            input_manifest_path=manifest_path,
            schema_path=SCHEMA,
            prompt_path=PROMPT,
            lifecycle_truth_table_path=TRUTH_TABLE,
            usage_attestation_path=_write_usage(tmp_path, "r6a-stop", remaining=5.0),
            stop_remaining_percent_lte=5,
            output_path=tmp_path / "blocked.json",
            receipt_path=tmp_path / "blocked.receipt.json",
            timeout_seconds=30,
            run_command=should_not_run,
        )
    assert called is False


def test_r6a_manifest_changes_only_transport_runner() -> None:
    manifest = build_manifest(ARTIFACT_DIR)
    prior = load_json(
        ARTIFACT_DIR / "legacy_lifecycle_execution_manifest_candidate_r6.json"
    )
    assert manifest["transport_only_revision"] is True
    assert manifest["prior_r6_status"] == "INVALIDATED_PRE_INFERENCE_TRANSPORT"
    assert manifest["prior_r6_semantic_output_count"] == 0
    assert manifest["formal_model"] == prior["formal_model"] == MODEL
    assert manifest["reasoning_effort"] == prior["reasoning_effort"] == REASONING
    assert manifest["protocol_probe_review_id"] == prior["protocol_probe_review_id"]
    assert manifest["run_directory"] != prior["run_directory"]
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
        assert manifest[f"{key}_sha256"] == prior[f"{key}_sha256"]
    assert manifest["runner_sha256"] != prior["runner_sha256"]
    assert manifest["base_runner_sha256"] == prior["runner_sha256"]
