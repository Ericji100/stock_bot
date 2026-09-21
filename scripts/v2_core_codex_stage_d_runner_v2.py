"""Run M2A STAGE_D with AI semantics separated from program provenance.

The model emits only subjective course-semantics fields.  Deterministic
identity, model, prompt, schema, packet, truth-table, and STAGE_B hashes are
bound by the program after the untouched raw semantic JSON passes its strict
transport schema.  This is enrichment, never repair of an AI field.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import v2_core_codex_staged_runner_v1 as base
from scripts.v2_core_staged_output_validator_v1 import validate_stage_b, validate_stage_d


OUT = base.OUT
RUNNER_VERSION = "v2-core-codex-stage-d-runner-r5"
STAGE_B_PROMPT_FILENAME = base.STAGE_B_PROMPT_FILENAME
STAGE_D_PROMPT_FILENAME = "v2_core_stage_d.prompt.candidate_r3.md"
PROGRAM_BOUND_FIELDS = (
    "schema_version",
    "review_id",
    "anonymous_stock_id",
    "as_of",
    "packet_sha256",
    "stage_b_output_sha256",
    "prompt_sha256",
    "schema_sha256",
    "truth_table_sha256",
    "model",
    "reasoning_effort",
    "independent_review",
)


def program_bound_metadata(
    *,
    packet: dict[str, Any],
    packet_path: Path,
    stage_b_path: Path,
    schema_path: Path,
    prompt_path: Path,
    truth_table_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "v2-core-stage-d-r1-candidate",
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "as_of": packet["as_of"],
        "packet_sha256": base.sha256(packet_path),
        "stage_b_output_sha256": base.sha256(stage_b_path),
        "prompt_sha256": base.sha256(prompt_path),
        "schema_sha256": base.sha256(schema_path),
        "truth_table_sha256": base.sha256(truth_table_path),
        "model": base.MODEL,
        "reasoning_effort": base.REASONING,
        "independent_review": False,
    }


def semantic_transport_schema(
    local_schema: dict[str, Any],
    *,
    evidence_ref_values: list[str],
    primary_scenario: str,
) -> dict[str, Any]:
    schema = base.strict_transport_schema(
        local_schema,
        evidence_ref_values=evidence_ref_values,
        primary_scenario=primary_scenario,
    )
    for field in PROGRAM_BOUND_FIELDS:
        schema["properties"].pop(field, None)
    schema["required"] = [
        field for field in schema.get("required", []) if field not in PROGRAM_BOUND_FIELDS
    ]
    Draft202012Validator.check_schema(schema)
    return schema


def bind_program_metadata(
    semantic_output: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    overlap = sorted(set(semantic_output).intersection(PROGRAM_BOUND_FIELDS))
    if overlap:
        raise base.StagedRunnerError(
            "AI semantic output contains program-bound fields: " + ",".join(overlap)
        )
    if set(metadata) != set(PROGRAM_BOUND_FIELDS):
        raise base.StagedRunnerError("program-bound metadata contract mismatch")
    return {**metadata, **semantic_output}


def rendered_stage_d_prompt(
    *,
    prompt_text: str,
    packet: dict[str, Any],
    stage_b: dict[str, Any],
    metadata: dict[str, Any],
    primary_scenario: str,
) -> str:
    payload = {
        "program_bound_metadata_not_for_ai_output": metadata,
        "program_primary_scenario": primary_scenario,
        "stage_b_output": stage_b,
        "packet": base.compact_packet(packet),
    }
    return (
        prompt_text.rstrip()
        + "\n\n以下是唯一允許使用的匿名AS-OF輸入。只輸出傳輸Schema要求的AI語意JSON；"
        + "不得輸出program_bound_metadata_not_for_ai_output。\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


def validate_raw_semantics(
    semantic_output: dict[str, Any], transport_schema: dict[str, Any]
) -> None:
    errors = sorted(
        error.message
        for error in Draft202012Validator(transport_schema).iter_errors(semantic_output)
    )
    if errors:
        raise base.StagedRunnerError("stored AI semantic output invalid: " + "; ".join(errors))


def run_stage_d_case(
    *,
    packet_path: Path,
    stage_b_path: Path,
    stage_b_schema_path: Path,
    stage_b_prompt_path: Path,
    schema_path: Path,
    prompt_path: Path,
    truth_table_path: Path,
    output_path: Path,
    route_path: Path,
    permission_path: Path,
    receipt_path: Path,
    timeout_seconds: int,
    run_command: base.RunCommand = base.subprocess.run,
) -> dict[str, Any]:
    packet = base.load_json(packet_path)
    stage_b = base.load_json(stage_b_path)
    stage_b_validation = validate_stage_b(
        stage_b,
        packet_path=packet_path,
        schema_path=stage_b_schema_path,
        prompt_path=stage_b_prompt_path,
        truth_table_path=truth_table_path,
    )
    if stage_b_validation["status"] != "PASS":
        raise base.StagedRunnerError("STAGE_D dependency STAGE_B is invalid")
    scenario = str(stage_b_validation["program_derived_scenario"])
    route_record = base.build_route_record(
        packet, stage_b_path, truth_table_path, stage_b_validation
    )
    base.write_new_or_identical(route_path, base.canonical_bytes(route_record))
    if scenario == "UNRESOLVED_NO_TRADE":
        permission = base.build_unresolved_permission(
            packet, truth_table_path, stage_b_validation
        )
        base.write_new_or_identical(permission_path, base.canonical_bytes(permission))
        return {
            "status": "PROGRAM_UNKNOWN_NO_STAGE_D_CALL",
            "program_derived_scenario": scenario,
            "program_derived_permission": "UNKNOWN",
        }

    local_schema = base.load_json(schema_path)
    refs = [str(item["ref"]) for item in packet["evidence_catalog"]]
    transport = semantic_transport_schema(
        local_schema,
        evidence_ref_values=refs,
        primary_scenario=scenario,
    )
    metadata = program_bound_metadata(
        packet=packet,
        packet_path=packet_path,
        stage_b_path=stage_b_path,
        schema_path=schema_path,
        prompt_path=prompt_path,
        truth_table_path=truth_table_path,
    )
    full_prompt = prompt_path.read_text(encoding="utf-8")
    selected_prompt = base.selected_stage_d_prompt(full_prompt, scenario)
    prompt = rendered_stage_d_prompt(
        prompt_text=selected_prompt,
        packet=packet,
        stage_b=stage_b,
        metadata=metadata,
        primary_scenario=scenario,
    )
    semantic_output, receipt, raw_bytes = base.call_codex(
        prompt=prompt,
        transport_schema=transport,
        timeout_seconds=timeout_seconds,
        invalid_raw_path=base.invalid_output_path(output_path),
        run_command=run_command,
    )
    output = bind_program_metadata(semantic_output, metadata)
    validation = validate_stage_d(
        output,
        packet_path=packet_path,
        stage_b_path=stage_b_path,
        stage_b_schema_path=stage_b_schema_path,
        stage_b_prompt_path=stage_b_prompt_path,
        schema_path=schema_path,
        prompt_path=prompt_path,
        truth_table_path=truth_table_path,
    )
    if validation["status"] != "PASS":
        base.write_new_or_identical(base.invalid_output_path(output_path), raw_bytes)
        raise base.StagedRunnerError(
            "STAGE_D local validation failed: "
            + json.dumps(validation, ensure_ascii=False)
        )
    base.write_new_or_identical(base.raw_output_path(output_path), raw_bytes)
    base.write_new_or_identical(output_path, base.canonical_bytes(output))
    receipt["runner_version"] = RUNNER_VERSION
    base.write_new_or_identical(
        receipt_path,
        base.canonical_bytes(
            {
                **receipt,
                "stage": "STAGE_D",
                "selected_primary_scenario": scenario,
                "source_prompt_sha256": base.sha256(prompt_path),
                "program_bound_metadata_sha256": base.canonical_sha256(metadata),
                "validation": validation,
            }
        ),
    )
    permission = base.build_resolved_permission(
        packet, scenario, output_path, truth_table_path, validation
    )
    base.write_new_or_identical(permission_path, base.canonical_bytes(permission))
    return {
        "status": "COMPLETED",
        "program_derived_scenario": scenario,
        "program_derived_permission": validation["program_derived_permission"],
    }


def validate_existing_stage_d_artifacts(
    *,
    packet_path: Path,
    stage_b_path: Path,
    stage_b_schema_path: Path,
    stage_b_prompt_path: Path,
    schema_path: Path,
    prompt_path: Path,
    truth_table_path: Path,
    output_path: Path,
    route_path: Path,
    permission_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    packet = base.load_json(packet_path)
    stage_b = base.load_json(stage_b_path)
    stage_b_validation = validate_stage_b(
        stage_b,
        packet_path=packet_path,
        schema_path=stage_b_schema_path,
        prompt_path=stage_b_prompt_path,
        truth_table_path=truth_table_path,
    )
    if stage_b_validation["status"] != "PASS":
        raise base.StagedRunnerError("existing STAGE_D has invalid STAGE_B dependency")
    scenario = str(stage_b_validation["program_derived_scenario"])
    expected_route = base.build_route_record(
        packet, stage_b_path, truth_table_path, stage_b_validation
    )
    if not route_path.is_file() or base.load_json(route_path) != expected_route:
        raise base.StagedRunnerError("existing STAGE_D program route is missing or inconsistent")
    if scenario == "UNRESOLVED_NO_TRADE":
        expected_permission = base.build_unresolved_permission(
            packet, truth_table_path, stage_b_validation
        )
        if base.load_json(permission_path) != expected_permission:
            raise base.StagedRunnerError("existing unresolved permission is inconsistent")
        if output_path.exists() or receipt_path.exists():
            raise base.StagedRunnerError("unresolved route must not have STAGE_D AI artifacts")
        return {
            "status": "RESUMED_VALID_EXISTING",
            "program_derived_scenario": scenario,
            "program_derived_permission": "UNKNOWN",
        }

    raw_path = base.raw_output_path(output_path)
    if not output_path.is_file() or not raw_path.is_file() or not receipt_path.is_file():
        raise base.StagedRunnerError("existing resolved permission lacks STAGE_D artifacts")
    semantic_output = base.load_json(raw_path)
    local_schema = base.load_json(schema_path)
    refs = [str(item["ref"]) for item in packet["evidence_catalog"]]
    transport = semantic_transport_schema(
        local_schema,
        evidence_ref_values=refs,
        primary_scenario=scenario,
    )
    validate_raw_semantics(semantic_output, transport)
    metadata = program_bound_metadata(
        packet=packet,
        packet_path=packet_path,
        stage_b_path=stage_b_path,
        schema_path=schema_path,
        prompt_path=prompt_path,
        truth_table_path=truth_table_path,
    )
    output = base.load_json(output_path)
    if output != bind_program_metadata(semantic_output, metadata):
        raise base.StagedRunnerError("normalized STAGE_D differs from raw semantics plus program metadata")
    validation = validate_stage_d(
        output,
        packet_path=packet_path,
        stage_b_path=stage_b_path,
        stage_b_schema_path=stage_b_schema_path,
        stage_b_prompt_path=stage_b_prompt_path,
        schema_path=schema_path,
        prompt_path=prompt_path,
        truth_table_path=truth_table_path,
    )
    if validation["status"] != "PASS":
        raise base.StagedRunnerError("existing STAGE_D output is invalid")
    expected_permission = base.build_resolved_permission(
        packet, scenario, output_path, truth_table_path, validation
    )
    if base.load_json(permission_path) != expected_permission:
        raise base.StagedRunnerError("existing STAGE_D permission is inconsistent")
    receipt = base.load_json(receipt_path)
    receipt_checks = {
        "runner_version": RUNNER_VERSION,
        "model": base.MODEL,
        "reasoning_effort": base.REASONING,
        "stage": "STAGE_D",
        "selected_primary_scenario": scenario,
        "source_prompt_sha256": base.sha256(prompt_path),
        "raw_output_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "normalized_output_sha256": base.canonical_sha256(output),
        "program_bound_metadata_sha256": base.canonical_sha256(metadata),
    }
    for key, expected in receipt_checks.items():
        if receipt.get(key) != expected:
            raise base.StagedRunnerError(f"existing STAGE_D receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise base.StagedRunnerError("existing STAGE_D receipt validation mismatch")
    return {
        "status": "RESUMED_VALID_EXISTING",
        "program_derived_scenario": scenario,
        "program_derived_permission": validation["program_derived_permission"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=OUT)
    parser.add_argument("--execution-manifest", type=Path, required=True)
    parser.add_argument("--usage-attestation", type=Path, required=True)
    parser.add_argument("--round", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--shard-id", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args(argv)
    artifact_dir = args.artifact_dir.resolve()
    manifest = base.verify_execution_manifest(args.execution_manifest.resolve(), artifact_dir)
    usage = base.parse_usage_attestation(args.usage_attestation.resolve())
    base.assert_budget_allows(usage, float(manifest["budget_stop_remaining_percent_lte"]))
    shards = [row for row in manifest["shards"] if int(row["shard_id"]) == args.shard_id]
    if len(shards) != 1:
        raise base.StagedRunnerError("shard missing or duplicated")
    case_map = {row["review_id"]: row for row in manifest["cases"]}
    probe = base.load_json(artifact_dir / "feasibility_probe_manifest.json")
    packet_dir = artifact_dir / str(probe["packet_directory"])
    output_root = artifact_dir / str(manifest["run_directory"]) / f"round_{args.round}"
    completed: list[dict[str, Any]] = []
    for review_id in shards[0]["review_ids"]:
        row = case_map[review_id]
        packet_path = packet_dir / row["packet_file"]
        if base.sha256(packet_path) != row["packet_sha256"]:
            raise base.StagedRunnerError(f"packet changed: {review_id}")
        stage_b_path = output_root / "stage_b" / f"{review_id}.json"
        if not stage_b_path.is_file():
            raise base.StagedRunnerError(f"STAGE_D requires completed STAGE_B: {review_id}")
        stage_d_path = output_root / "stage_d" / f"{review_id}.json"
        route_path = output_root / "program_route" / f"{review_id}.json"
        permission_path = output_root / "final_permission" / f"{review_id}.json"
        receipt_path = output_root / "receipts" / f"{review_id}.stage_d.json"
        kwargs = {
            "packet_path": packet_path,
            "stage_b_path": stage_b_path,
            "stage_b_schema_path": artifact_dir / "v2_core_stage_b.schema.candidate.json",
            "stage_b_prompt_path": artifact_dir / STAGE_B_PROMPT_FILENAME,
            "schema_path": artifact_dir / "v2_core_stage_d.schema.candidate.json",
            "prompt_path": artifact_dir / STAGE_D_PROMPT_FILENAME,
            "truth_table_path": artifact_dir / "permission_truth_table.json",
            "output_path": stage_d_path,
            "route_path": route_path,
            "permission_path": permission_path,
            "receipt_path": receipt_path,
        }
        if permission_path.exists():
            result = validate_existing_stage_d_artifacts(**kwargs)
        else:
            result = run_stage_d_case(
                **kwargs,
                timeout_seconds=args.timeout_seconds,
            )
        completed.append({"review_id": review_id, **result})
    print(
        json.dumps(
            {
                "status": "SHARD_COMPLETE",
                "round": args.round,
                "shard_id": args.shard_id,
                "cases": completed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
