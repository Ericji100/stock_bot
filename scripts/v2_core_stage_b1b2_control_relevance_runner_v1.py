"""Immutable Codex runner for the B1b2 control-relevance probe."""

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
from scripts.v2_core_stage_b1a1e_primary_gate_runner_v1 import _transport_schema
from scripts.v2_core_stage_b1a_candidate_validator_v1 import sha256_file
from scripts.v2_core_stage_b1b2_control_relevance_validator_v1 import load_json, validate_response


RUNNER_VERSION = "v2-core-stage-b1b2-control-relevance-runner-r1-candidate"
MODEL = base.MODEL
REASONING = base.REASONING
RunnerError = base.StageB1aRunnerError
RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def _manifest_row(packet: dict[str, Any], input_packet_path: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if manifest.get("formal_model") != MODEL or manifest.get("reasoning_effort") != REASONING:
        raise RunnerError("input manifest model/effort mismatch")
    matches = [row for row in manifest["rows"] if row["review_id"] == packet["review_id"]]
    if len(matches) != 1:
        raise RunnerError("input manifest review_id mismatch")
    row = matches[0]
    if row["as_of"] != packet["as_of"] or row["input_packet_file"] != input_packet_path.name:
        raise RunnerError("input manifest packet identity mismatch")
    if row["input_packet_sha256"] != sha256_file(input_packet_path):
        raise RunnerError("input manifest packet hash mismatch")
    return manifest


def rendered_prompt(prompt_text: str, packet: dict[str, Any]) -> str:
    return prompt_text.rstrip() + "\n\n以下是唯一允許使用的匿名AS-OF封包。只輸出Schema JSON。\n" + json.dumps(
        {"packet": packet}, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def run_case(
    *,
    input_packet_path: Path,
    input_manifest_path: Path,
    schema_path: Path,
    prompt_path: Path,
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
    _manifest_row(packet, input_packet_path, input_manifest_path)
    option_ids = [str(row["evidence_option_id"]) for row in packet["control_relevance_evidence_options"]]
    transport = _transport_schema(schema, option_ids)
    output, receipt, raw_bytes = base.call_codex(
        prompt=rendered_prompt(prompt_path.read_text(encoding="utf-8"), packet),
        transport_schema=transport,
        timeout_seconds=timeout_seconds,
        invalid_raw_path=base.invalid_output_path(output_path),
        run_command=run_command,
    )
    receipt["runner_version"] = RUNNER_VERSION
    validation = validate_response(response=output, schema=schema, packet=packet)
    if validation["status"] != "VALID":
        base.write_new_or_identical(base.invalid_output_path(output_path), raw_bytes)
        raise RunnerError("STAGE_B1B2_CONTROL_RELEVANCE local validation failed: " + json.dumps(validation, ensure_ascii=False))
    base.write_new_or_identical(base.raw_output_path(output_path), raw_bytes)
    base.write_new_or_identical(output_path, base.canonical_bytes(output))
    bound = {
        **receipt,
        "stage": "STAGE_B1B2_CONTROL_RELEVANCE",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": sha256_file(input_packet_path),
        "input_manifest_sha256": sha256_file(input_manifest_path),
        "prompt_source_sha256": sha256_file(prompt_path),
        "local_schema_sha256": sha256_file(schema_path),
        "usage_attestation_sha256": sha256_file(usage_attestation_path),
        "stop_remaining_percent_lte": stop_remaining_percent_lte,
        "validation": validation,
    }
    base.write_new_or_identical(receipt_path, base.canonical_bytes(bound))
    return validation


def validate_existing_artifacts(
    *,
    input_packet_path: Path,
    input_manifest_path: Path,
    schema_path: Path,
    prompt_path: Path,
    output_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    raw_path = base.raw_output_path(output_path)
    if not output_path.is_file() or not raw_path.is_file() or not receipt_path.is_file():
        raise RunnerError("existing B1b2 output/raw/receipt set is incomplete")
    packet = load_json(input_packet_path)
    schema = load_json(schema_path)
    _manifest_row(packet, input_packet_path, input_manifest_path)
    output = load_json(output_path)
    validation = validate_response(response=output, schema=schema, packet=packet)
    if validation["status"] != "VALID":
        raise RunnerError("existing B1b2 output is invalid")
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": RUNNER_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "STAGE_B1B2_CONTROL_RELEVANCE",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": sha256_file(input_packet_path),
        "input_manifest_sha256": sha256_file(input_manifest_path),
        "prompt_source_sha256": sha256_file(prompt_path),
        "local_schema_sha256": sha256_file(schema_path),
        "raw_output_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "normalized_output_sha256": base.canonical_sha256(output),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"existing B1b2 receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("existing B1b2 receipt validation mismatch")
    return validation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-packet", type=Path, required=True)
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--usage-attestation", type=Path, required=True)
    parser.add_argument("--stop-remaining-percent-lte", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    result = run_case(
        input_packet_path=args.input_packet,
        input_manifest_path=args.input_manifest,
        schema_path=args.schema,
        prompt_path=args.prompt,
        usage_attestation_path=args.usage_attestation,
        stop_remaining_percent_lte=args.stop_remaining_percent_lte,
        output_path=args.output,
        receipt_path=args.receipt,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
