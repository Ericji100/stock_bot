"""Immutable two-stage runner for R6 B0A lifecycle and B0B role alignment."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import v2_core_stage_b1a_codex_runner_v1 as base
from scripts.v2_core_stage_b1a_candidate_validator_v1 import sha256_file
from scripts.v2_core_legacy_lifecycle_b0a_validator_v6 import (
    load_json,
    validate_response as validate_lifecycle_response,
)
from scripts.v2_core_legacy_roles_b0b_validator_v6 import (
    validate_response as validate_roles_response,
)


RUNNER_VERSION = "v2-core-legacy-lifecycle-runner-r6-candidate"
MODEL = "gpt-5.6-sol"
REASONING = "xhigh"
B0A_STAGE = "LEGACY_LIFECYCLE_B0A_R6"
B0B_STAGE = "LEGACY_ROLES_B0B_R6"
RunnerError = base.StageB1aRunnerError
RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def _manifest_row(
    *, packet: dict[str, Any], input_packet_path: Path, input_manifest_path: Path
) -> dict[str, Any]:
    manifest = load_json(input_manifest_path)
    if manifest.get("formal_model") != MODEL or manifest.get("reasoning_effort") != REASONING:
        raise RunnerError("input manifest model/effort mismatch")
    matches = [row for row in manifest.get("rows", []) if row.get("review_id") == packet.get("review_id")]
    if len(matches) != 1:
        raise RunnerError("input manifest review_id mismatch")
    row = matches[0]
    if row.get("as_of") != packet.get("as_of") or row.get("input_packet_file") != input_packet_path.name:
        raise RunnerError("input manifest packet identity mismatch")
    if row.get("input_packet_sha256") != sha256_file(input_packet_path):
        raise RunnerError("input manifest packet hash mismatch")
    return manifest


def _fixed_refs(packet: dict[str, Any]) -> list[str]:
    refs: set[str] = set()
    for row in packet.get("candidate_evidence_options", []):
        if isinstance(row, dict):
            refs.update(str(ref) for ref in row.get("source_evidence_refs", []))
    for row in packet.get("proxy_evidence", []):
        if isinstance(row, dict) and isinstance(row.get("ref"), str):
            refs.add(row["ref"])
    return sorted(refs)


def _transport_schema(schema: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    transport = base.strict_transport_schema(schema, evidence_ref_values=_fixed_refs(packet))
    candidate_ids = sorted(str(row["candidate_id"]) for row in packet["candidate_pool"])
    option_ids = sorted(
        str(row["evidence_option_id"]) for row in packet["candidate_evidence_options"]
    )
    evidence_refs = _fixed_refs(packet)

    def constrain(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                constrain(child)
            return
        if not isinstance(value, dict):
            return
        properties = value.get("properties")
        if isinstance(properties, dict):
            for name in list(properties):
                if name == "alternative_candidate_ids":
                    properties[name]["items"] = {"type": "string", "enum": candidate_ids}
                elif name.endswith("candidate_id"):
                    properties[name] = {"type": "string", "enum": [*candidate_ids, "NONE"]}
                elif name.endswith("evidence_option_id"):
                    properties[name] = {"type": "string", "enum": [*option_ids, "NONE"]}
                elif name == "supporting_packet_evidence_refs":
                    properties[name]["items"] = {"type": "string", "enum": evidence_refs}
        for child in value.values():
            constrain(child)

    constrain(transport)
    return transport


def _rendered_b0a_prompt(prompt_text: str, packet: dict[str, Any]) -> str:
    return (
        prompt_text.rstrip()
        + "\n\n以下是唯一允許使用的匿名AS-OF封包。只輸出Schema JSON。\n"
        + json.dumps({"packet": packet}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


def _rendered_b0b_prompt(
    prompt_text: str,
    packet: dict[str, Any],
    b0a_response: dict[str, Any],
    program_scenario: str,
) -> str:
    payload = {
        "packet": packet,
        "validated_b0a_output": b0a_response,
        "program_routed_scenario_family": program_scenario,
    }
    return (
        prompt_text.rstrip()
        + "\n\n以下是唯一允許使用的匿名AS-OF封包、已驗證B0A輸出與程式route。"
        + "不得改投情境；只輸出Schema JSON。\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


def run_b0a_case(
    *,
    input_packet_path: Path,
    input_manifest_path: Path,
    schema_path: Path,
    prompt_path: Path,
    lifecycle_truth_table_path: Path,
    usage_attestation_path: Path,
    stop_remaining_percent_lte: float,
    output_path: Path,
    receipt_path: Path,
    timeout_seconds: int,
    run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    attestation = base.parse_usage_attestation(usage_attestation_path)
    base.assert_budget_allows(attestation, stop_remaining_percent_lte)
    packet = load_json(input_packet_path)
    schema = load_json(schema_path)
    truth_table = load_json(lifecycle_truth_table_path)
    _manifest_row(
        packet=packet,
        input_packet_path=input_packet_path,
        input_manifest_path=input_manifest_path,
    )
    output, receipt, raw_bytes = base.call_codex(
        prompt=_rendered_b0a_prompt(prompt_path.read_text(encoding="utf-8"), packet),
        transport_schema=_transport_schema(schema, packet),
        timeout_seconds=timeout_seconds,
        invalid_raw_path=base.invalid_output_path(output_path),
        run_command=run_command,
    )
    receipt["runner_version"] = RUNNER_VERSION
    validation = validate_lifecycle_response(
        response=output,
        schema=schema,
        packet=packet,
        truth_table=truth_table,
    )
    if validation["status"] != "VALID":
        base.write_new_or_identical(base.invalid_output_path(output_path), raw_bytes)
        raise RunnerError(
            "LEGACY_LIFECYCLE_B0A_R6 local validation failed: "
            + json.dumps(validation, ensure_ascii=False)
        )
    base.write_new_or_identical(base.raw_output_path(output_path), raw_bytes)
    base.write_new_or_identical(output_path, base.canonical_bytes(output))
    bound = {
        **receipt,
        "stage": B0A_STAGE,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": sha256_file(input_packet_path),
        "input_manifest_sha256": sha256_file(input_manifest_path),
        "prompt_source_sha256": sha256_file(prompt_path),
        "local_schema_sha256": sha256_file(schema_path),
        "lifecycle_truth_table_sha256": sha256_file(lifecycle_truth_table_path),
        "usage_attestation_sha256": sha256_file(usage_attestation_path),
        "stop_remaining_percent_lte": stop_remaining_percent_lte,
        "validation": validation,
    }
    base.write_new_or_identical(receipt_path, base.canonical_bytes(bound))
    return validation


def validate_existing_b0a(
    *,
    input_packet_path: Path,
    input_manifest_path: Path,
    schema_path: Path,
    prompt_path: Path,
    lifecycle_truth_table_path: Path,
    output_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    raw_path = base.raw_output_path(output_path)
    if not output_path.is_file() or not raw_path.is_file() or not receipt_path.is_file():
        raise RunnerError("existing R6 B0A output/raw/receipt set is incomplete")
    packet = load_json(input_packet_path)
    schema = load_json(schema_path)
    truth_table = load_json(lifecycle_truth_table_path)
    _manifest_row(
        packet=packet,
        input_packet_path=input_packet_path,
        input_manifest_path=input_manifest_path,
    )
    output = load_json(output_path)
    validation = validate_lifecycle_response(
        response=output,
        schema=schema,
        packet=packet,
        truth_table=truth_table,
    )
    if validation["status"] != "VALID":
        raise RunnerError("existing R6 B0A output is invalid")
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": RUNNER_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": B0A_STAGE,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": sha256_file(input_packet_path),
        "input_manifest_sha256": sha256_file(input_manifest_path),
        "prompt_source_sha256": sha256_file(prompt_path),
        "local_schema_sha256": sha256_file(schema_path),
        "lifecycle_truth_table_sha256": sha256_file(lifecycle_truth_table_path),
        "raw_output_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "normalized_output_sha256": base.canonical_sha256(output),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"existing R6 B0A receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("existing R6 B0A receipt validation mismatch")
    return validation


def run_b0b_case(
    *,
    input_packet_path: Path,
    input_manifest_path: Path,
    b0a_schema_path: Path,
    b0a_prompt_path: Path,
    lifecycle_truth_table_path: Path,
    b0a_output_path: Path,
    b0a_receipt_path: Path,
    b0b_schema_path: Path,
    b0b_prompt_path: Path,
    role_truth_table_path: Path,
    usage_attestation_path: Path,
    stop_remaining_percent_lte: float,
    output_path: Path,
    receipt_path: Path,
    timeout_seconds: int,
    run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    attestation = base.parse_usage_attestation(usage_attestation_path)
    base.assert_budget_allows(attestation, stop_remaining_percent_lte)
    b0a_validation = validate_existing_b0a(
        input_packet_path=input_packet_path,
        input_manifest_path=input_manifest_path,
        schema_path=b0a_schema_path,
        prompt_path=b0a_prompt_path,
        lifecycle_truth_table_path=lifecycle_truth_table_path,
        output_path=b0a_output_path,
        receipt_path=b0a_receipt_path,
    )
    if b0a_validation["route_status"] != "ROUTED":
        raise RunnerError("R6 B0B cannot run when B0A route is unresolved")

    packet = load_json(input_packet_path)
    b0a_output = load_json(b0a_output_path)
    b0a_schema = load_json(b0a_schema_path)
    lifecycle_truth_table = load_json(lifecycle_truth_table_path)
    b0b_schema = load_json(b0b_schema_path)
    role_truth_table = load_json(role_truth_table_path)
    output, receipt, raw_bytes = base.call_codex(
        prompt=_rendered_b0b_prompt(
            b0b_prompt_path.read_text(encoding="utf-8"),
            packet,
            b0a_output,
            b0a_validation["program_derived_scenario_family"],
        ),
        transport_schema=_transport_schema(b0b_schema, packet),
        timeout_seconds=timeout_seconds,
        invalid_raw_path=base.invalid_output_path(output_path),
        run_command=run_command,
    )
    receipt["runner_version"] = RUNNER_VERSION
    validation = validate_roles_response(
        response=output,
        schema=b0b_schema,
        packet=packet,
        b0a_response=b0a_output,
        b0a_schema=b0a_schema,
        lifecycle_truth_table=lifecycle_truth_table,
        role_truth_table=role_truth_table,
    )
    if validation["status"] != "VALID":
        base.write_new_or_identical(base.invalid_output_path(output_path), raw_bytes)
        raise RunnerError(
            "LEGACY_ROLES_B0B_R6 local validation failed: "
            + json.dumps(validation, ensure_ascii=False)
        )
    base.write_new_or_identical(base.raw_output_path(output_path), raw_bytes)
    base.write_new_or_identical(output_path, base.canonical_bytes(output))
    bound = {
        **receipt,
        "stage": B0B_STAGE,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "program_derived_scenario_family": b0a_validation[
            "program_derived_scenario_family"
        ],
        "input_packet_sha256": sha256_file(input_packet_path),
        "input_manifest_sha256": sha256_file(input_manifest_path),
        "b0a_prompt_source_sha256": sha256_file(b0a_prompt_path),
        "b0a_local_schema_sha256": sha256_file(b0a_schema_path),
        "lifecycle_truth_table_sha256": sha256_file(lifecycle_truth_table_path),
        "b0a_output_sha256": sha256_file(b0a_output_path),
        "b0a_receipt_sha256": sha256_file(b0a_receipt_path),
        "prompt_source_sha256": sha256_file(b0b_prompt_path),
        "local_schema_sha256": sha256_file(b0b_schema_path),
        "role_truth_table_sha256": sha256_file(role_truth_table_path),
        "usage_attestation_sha256": sha256_file(usage_attestation_path),
        "stop_remaining_percent_lte": stop_remaining_percent_lte,
        "validation": validation,
    }
    base.write_new_or_identical(receipt_path, base.canonical_bytes(bound))
    return validation


def validate_existing_b0b(
    *,
    input_packet_path: Path,
    input_manifest_path: Path,
    b0a_schema_path: Path,
    b0a_prompt_path: Path,
    lifecycle_truth_table_path: Path,
    b0a_output_path: Path,
    b0a_receipt_path: Path,
    b0b_schema_path: Path,
    b0b_prompt_path: Path,
    role_truth_table_path: Path,
    output_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    raw_path = base.raw_output_path(output_path)
    if not output_path.is_file() or not raw_path.is_file() or not receipt_path.is_file():
        raise RunnerError("existing R6 B0B output/raw/receipt set is incomplete")
    b0a_validation = validate_existing_b0a(
        input_packet_path=input_packet_path,
        input_manifest_path=input_manifest_path,
        schema_path=b0a_schema_path,
        prompt_path=b0a_prompt_path,
        lifecycle_truth_table_path=lifecycle_truth_table_path,
        output_path=b0a_output_path,
        receipt_path=b0a_receipt_path,
    )
    packet = load_json(input_packet_path)
    output = load_json(output_path)
    validation = validate_roles_response(
        response=output,
        schema=load_json(b0b_schema_path),
        packet=packet,
        b0a_response=load_json(b0a_output_path),
        b0a_schema=load_json(b0a_schema_path),
        lifecycle_truth_table=load_json(lifecycle_truth_table_path),
        role_truth_table=load_json(role_truth_table_path),
    )
    if validation["status"] != "VALID":
        raise RunnerError("existing R6 B0B output is invalid")
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": RUNNER_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": B0B_STAGE,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "program_derived_scenario_family": b0a_validation[
            "program_derived_scenario_family"
        ],
        "input_packet_sha256": sha256_file(input_packet_path),
        "input_manifest_sha256": sha256_file(input_manifest_path),
        "b0a_prompt_source_sha256": sha256_file(b0a_prompt_path),
        "b0a_local_schema_sha256": sha256_file(b0a_schema_path),
        "lifecycle_truth_table_sha256": sha256_file(lifecycle_truth_table_path),
        "b0a_output_sha256": sha256_file(b0a_output_path),
        "b0a_receipt_sha256": sha256_file(b0a_receipt_path),
        "prompt_source_sha256": sha256_file(b0b_prompt_path),
        "local_schema_sha256": sha256_file(b0b_schema_path),
        "role_truth_table_sha256": sha256_file(role_truth_table_path),
        "raw_output_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "normalized_output_sha256": base.canonical_sha256(output),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"existing R6 B0B receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("existing R6 B0B receipt validation mismatch")
    return validation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="stage", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--input-packet", type=Path, required=True)
    common.add_argument("--input-manifest", type=Path, required=True)
    common.add_argument("--usage-attestation", type=Path, required=True)
    common.add_argument("--stop-remaining-percent-lte", type=float, required=True)
    common.add_argument("--output", type=Path, required=True)
    common.add_argument("--receipt", type=Path, required=True)
    common.add_argument("--timeout-seconds", type=int, default=1800)

    b0a = subparsers.add_parser("b0a", parents=[common])
    b0a.add_argument("--schema", type=Path, required=True)
    b0a.add_argument("--prompt", type=Path, required=True)
    b0a.add_argument("--lifecycle-truth-table", type=Path, required=True)

    b0b = subparsers.add_parser("b0b", parents=[common])
    b0b.add_argument("--b0a-schema", type=Path, required=True)
    b0b.add_argument("--b0a-prompt", type=Path, required=True)
    b0b.add_argument("--lifecycle-truth-table", type=Path, required=True)
    b0b.add_argument("--b0a-output", type=Path, required=True)
    b0b.add_argument("--b0a-receipt", type=Path, required=True)
    b0b.add_argument("--b0b-schema", type=Path, required=True)
    b0b.add_argument("--b0b-prompt", type=Path, required=True)
    b0b.add_argument("--role-truth-table", type=Path, required=True)
    args = parser.parse_args()

    common_values = {
        "input_packet_path": args.input_packet,
        "input_manifest_path": args.input_manifest,
        "usage_attestation_path": args.usage_attestation,
        "stop_remaining_percent_lte": args.stop_remaining_percent_lte,
        "output_path": args.output,
        "receipt_path": args.receipt,
        "timeout_seconds": args.timeout_seconds,
    }
    if args.stage == "b0a":
        result = run_b0a_case(
            **common_values,
            schema_path=args.schema,
            prompt_path=args.prompt,
            lifecycle_truth_table_path=args.lifecycle_truth_table,
        )
    else:
        result = run_b0b_case(
            **common_values,
            b0a_schema_path=args.b0a_schema,
            b0a_prompt_path=args.b0a_prompt,
            lifecycle_truth_table_path=args.lifecycle_truth_table,
            b0a_output_path=args.b0a_output,
            b0a_receipt_path=args.b0a_receipt,
            b0b_schema_path=args.b0b_schema,
            b0b_prompt_path=args.b0b_prompt,
            role_truth_table_path=args.role_truth_table,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
