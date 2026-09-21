"""Immutable Codex runner for Stage B1a1 fixed-segment eligibility candidate R2."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable
import subprocess


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import v2_core_stage_b1a_codex_runner_v1 as base
from scripts.v2_core_stage_b1a1_candidate_validator_v2 import (
    load_json,
    validate_stage_b1a1_response,
)
from scripts.v2_core_stage_b1a_candidate_validator_v1 import sha256_file


RUNNER_VERSION = "v2-core-stage-b1a1-codex-runner-r2-candidate"
MODEL = base.MODEL
REASONING = base.REASONING
StageB1a1RunnerError = base.StageB1aRunnerError
RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def compact_stage_b1a1_packet(packet: dict[str, Any]) -> dict[str, Any]:
    compact = base.compact_stage_b1a_packet(packet)
    compact["focus_relations"] = packet["focus_relations"]
    compact["review_constraints"] = packet["review_constraints"]
    return compact


def rendered_prompt(prompt_text: str, packet: dict[str, Any]) -> str:
    return (
        prompt_text.rstrip()
        + "\n\n以下是唯一允許使用的匿名AS-OF輸入。只輸出Schema JSON。\n"
        + json.dumps(
            {"packet": compact_stage_b1a1_packet(packet)},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _assert_input_bindings(
    packet: dict[str, Any],
    *,
    source_b1a_input_path: Path,
    relation_catalog_path: Path,
    focus_catalog_path: Path,
) -> None:
    bindings = packet.get("source_bindings", {})
    checks = {
        "source_b1a_input_sha256": sha256_file(source_b1a_input_path),
        "relation_catalog_sha256": sha256_file(relation_catalog_path),
        "focus_catalog_sha256": sha256_file(focus_catalog_path),
    }
    for key, expected in checks.items():
        if bindings.get(key) != expected:
            raise StageB1a1RunnerError(f"input binding mismatch: {key}")
    source = load_json(source_b1a_input_path)
    relation = load_json(relation_catalog_path)
    focus = load_json(focus_catalog_path)
    if not (
        packet.get("review_id")
        == source.get("review_id")
        == relation.get("review_id")
        == focus.get("review_id")
    ):
        raise StageB1a1RunnerError("input review_id binding mismatch")
    if not (
        packet.get("as_of")
        == source.get("as_of")
        == relation.get("as_of")
        == focus.get("as_of")
    ):
        raise StageB1a1RunnerError("input as_of binding mismatch")


def run_stage_b1a1_case(
    *,
    input_packet_path: Path,
    source_b1a_input_path: Path,
    relation_catalog_path: Path,
    focus_catalog_path: Path,
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
    _assert_input_bindings(
        packet,
        source_b1a_input_path=source_b1a_input_path,
        relation_catalog_path=relation_catalog_path,
        focus_catalog_path=focus_catalog_path,
    )
    refs = [str(item["ref"]) for item in packet["evidence_catalog"]]
    transport = base.strict_transport_schema(schema, evidence_ref_values=refs)
    prompt = rendered_prompt(prompt_path.read_text(encoding="utf-8"), packet)
    output, receipt, raw_bytes = base.call_codex(
        prompt=prompt,
        transport_schema=transport,
        timeout_seconds=timeout_seconds,
        invalid_raw_path=base.invalid_output_path(output_path),
        run_command=run_command,
    )
    receipt["runner_version"] = RUNNER_VERSION
    validation = validate_stage_b1a1_response(
        response=output,
        schema=schema,
        packet=packet,
    )
    if validation["status"] != "VALID":
        base.write_new_or_identical(base.invalid_output_path(output_path), raw_bytes)
        raise StageB1a1RunnerError(
            "STAGE_B1A1 local validation failed: "
            + json.dumps(validation, ensure_ascii=False)
        )
    base.write_new_or_identical(base.raw_output_path(output_path), raw_bytes)
    base.write_new_or_identical(output_path, base.canonical_bytes(output))
    bound_receipt = {
        **receipt,
        "stage": "STAGE_B1A1",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": sha256_file(input_packet_path),
        "source_b1a_input_sha256": sha256_file(source_b1a_input_path),
        "relation_catalog_sha256": sha256_file(relation_catalog_path),
        "focus_catalog_sha256": sha256_file(focus_catalog_path),
        "prompt_source_sha256": sha256_file(prompt_path),
        "local_schema_sha256": sha256_file(schema_path),
        "usage_attestation_sha256": sha256_file(usage_attestation_path),
        "stop_remaining_percent_lte": stop_remaining_percent_lte,
        "validation": validation,
    }
    base.write_new_or_identical(receipt_path, base.canonical_bytes(bound_receipt))
    return validation


def validate_existing_stage_b1a1_artifacts(
    *,
    input_packet_path: Path,
    source_b1a_input_path: Path,
    relation_catalog_path: Path,
    focus_catalog_path: Path,
    schema_path: Path,
    prompt_path: Path,
    output_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    raw_path = base.raw_output_path(output_path)
    if not output_path.is_file() or not raw_path.is_file() or not receipt_path.is_file():
        raise StageB1a1RunnerError("existing B1a1 output/raw/receipt set is incomplete")
    packet = load_json(input_packet_path)
    schema = load_json(schema_path)
    _assert_input_bindings(
        packet,
        source_b1a_input_path=source_b1a_input_path,
        relation_catalog_path=relation_catalog_path,
        focus_catalog_path=focus_catalog_path,
    )
    output = load_json(output_path)
    validation = validate_stage_b1a1_response(
        response=output,
        schema=schema,
        packet=packet,
    )
    if validation["status"] != "VALID":
        raise StageB1a1RunnerError("existing B1a1 output is invalid")
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": RUNNER_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "STAGE_B1A1",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": sha256_file(input_packet_path),
        "source_b1a_input_sha256": sha256_file(source_b1a_input_path),
        "relation_catalog_sha256": sha256_file(relation_catalog_path),
        "focus_catalog_sha256": sha256_file(focus_catalog_path),
        "prompt_source_sha256": sha256_file(prompt_path),
        "local_schema_sha256": sha256_file(schema_path),
        "raw_output_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "normalized_output_sha256": base.canonical_sha256(output),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise StageB1a1RunnerError(f"existing B1a1 receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise StageB1a1RunnerError("existing B1a1 receipt validation mismatch")
    return validation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-packet", type=Path, required=True)
    parser.add_argument("--source-b1a-input", type=Path, required=True)
    parser.add_argument("--relation-catalog", type=Path, required=True)
    parser.add_argument("--focus-catalog", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--usage-attestation", type=Path, required=True)
    parser.add_argument("--stop-remaining-percent-lte", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validation = run_stage_b1a1_case(
        input_packet_path=args.input_packet,
        source_b1a_input_path=args.source_b1a_input,
        relation_catalog_path=args.relation_catalog,
        focus_catalog_path=args.focus_catalog,
        schema_path=args.schema,
        prompt_path=args.prompt,
        usage_attestation_path=args.usage_attestation,
        stop_remaining_percent_lte=args.stop_remaining_percent_lte,
        output_path=args.output,
        receipt_path=args.receipt,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
