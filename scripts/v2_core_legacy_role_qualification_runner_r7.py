"""Immutable, one-role-at-a-time R7 teacher-blind qualification runner."""

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

from scripts import v2_core_codex_staged_runner_v1 as base
from scripts.v2_core_legacy_role_qualification_manifest_r7 import (
    BUDGET_FILE,
    INPUT_MANIFEST,
    MODEL,
    OUTPUT_MANIFEST,
    PROMPT_FILE,
    REASONING,
    SHORTLIST_MANIFEST,
)
from scripts.v2_core_legacy_role_qualification_r7 import build_schema, transport_schema, validate_and_resolve
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path


RUNNER_VERSION = "v2-core-legacy-role-qualification-runner-r7-candidate-r1"
OUTPUT_DIRECTORY = "legacy_role_qualification_runs_candidate_r7"
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunnerError = base.StagedRunnerError


def _bound_inputs(artifact_dir: Path, review_id: str, role: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest_path = artifact_dir / OUTPUT_MANIFEST
    manifest = load_json(manifest_path)
    if manifest["formal_model"] != MODEL or manifest["reasoning_effort"] != REASONING:
        raise RunnerError("R7 execution manifest model/effort mismatch")
    if base.MODEL != MODEL or base.REASONING != REASONING:
        raise RunnerError("R7 transport model/effort mismatch")
    for name, expected_key in (
        (INPUT_MANIFEST, "input_manifest_sha256"),
        (SHORTLIST_MANIFEST, "shortlist_manifest_sha256"),
        (PROMPT_FILE, "prompt_sha256"),
        (BUDGET_FILE, "budget_sha256"),
    ):
        if sha256_path(artifact_dir / name) != manifest[expected_key]:
            raise RunnerError(f"R7 source changed after manifest: {name}")
    contract_path = ROOT / "scripts" / "v2_core_legacy_role_qualification_r7.py"
    if sha256_path(contract_path) != manifest["qualification_contract_sha256"]:
        raise RunnerError("R7 qualification contract changed after manifest")
    matches = [row for row in manifest["rows"] if row["review_id"] == review_id and row["role"] == role]
    if len(matches) != 1:
        raise RunnerError("R7 review/role not uniquely present in execution manifest")
    row = matches[0]
    packet_path = artifact_dir / manifest["input_packet_directory"] / row["input_packet_file"]
    shortlist_path = artifact_dir / manifest["shortlist_directory"] / row["shortlist_file"]
    if sha256_path(packet_path) != row["input_packet_sha256"] or sha256_path(shortlist_path) != row["shortlist_sha256"]:
        raise RunnerError("R7 packet/shortlist hash mismatch")
    packet = load_json(packet_path)
    shortlist = load_json(shortlist_path)
    if packet["review_id"] != review_id or shortlist["review_id"] != review_id:
        raise RunnerError("R7 packet/shortlist identity mismatch")
    if packet["as_of"] != row["as_of"] or shortlist["as_of"] != row["as_of"]:
        raise RunnerError("R7 packet/shortlist as-of mismatch")
    schema = transport_schema(build_schema(packet, shortlist, role))
    if hashlib.sha256(canonical_bytes(schema)).hexdigest() != row["transport_schema_sha256"]:
        raise RunnerError("R7 transport schema changed after manifest")
    if shortlist["roles"][role]["shortlist_count"] != row["candidate_count"]:
        raise RunnerError("R7 candidate count mismatch")
    return manifest, row, packet, shortlist


def rendered_prompt(prompt_text: str, packet: dict[str, Any], shortlist: dict[str, Any], role: str) -> str:
    payload = {
        "packet": packet,
        "qualification_role": role,
        "qualification_role_shortlist": shortlist["roles"][role]["candidate_rows"],
    }
    return (
        prompt_text.rstrip()
        + "\n\n以下是唯一允許使用的匿名AS-OF資料與本次角色候選。"
        + "只評估qualification_role_shortlist中的候選；只輸出Schema JSON。\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


def output_paths(artifact_dir: Path, review_id: str, role: str) -> tuple[Path, Path]:
    output = artifact_dir / OUTPUT_DIRECTORY / role / f"{review_id}.json"
    receipt = output.with_name(output.stem + ".receipt.json")
    return output, receipt


def run_role(
    *,
    artifact_dir: Path,
    review_id: str,
    role: str,
    usage_attestation_path: Path,
    timeout_seconds: int = 1200,
    run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    manifest, row, packet, shortlist = _bound_inputs(artifact_dir, review_id, role)
    output_path, receipt_path = output_paths(artifact_dir, review_id, role)
    raw_path = base.raw_output_path(output_path)
    invalid_path = base.invalid_output_path(output_path)
    if any(path.exists() for path in (output_path, receipt_path, raw_path, invalid_path)):
        raise RunnerError("R7 output already exists; validate it instead of re-running")
    attestation = base.parse_usage_attestation(usage_attestation_path)
    base.assert_budget_allows(attestation, manifest["stop_remaining_percent_lte"])
    schema = transport_schema(build_schema(packet, shortlist, role))
    prompt = rendered_prompt((artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"), packet, shortlist, role)
    response, receipt, raw_bytes = base.call_codex(
        prompt=prompt,
        transport_schema=schema,
        timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid_path,
        run_command=run_command,
    )
    validation = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role)
    if validation["status"] != "VALID":
        base.write_new_or_identical(invalid_path, raw_bytes)
        raise RunnerError("R7 local validation failed: " + json.dumps(validation, ensure_ascii=False))
    receipt = {
        **receipt,
        "runner_version": RUNNER_VERSION,
        "stage": "R7_ROLE_QUALIFICATION",
        "review_id": review_id,
        "as_of": row["as_of"],
        "role": role,
        "execution_manifest_sha256": sha256_path(artifact_dir / OUTPUT_MANIFEST),
        "input_packet_sha256": row["input_packet_sha256"],
        "shortlist_sha256": row["shortlist_sha256"],
        "prompt_source_sha256": manifest["prompt_sha256"],
        "qualification_contract_sha256": manifest["qualification_contract_sha256"],
        "usage_attestation_sha256": sha256_path(usage_attestation_path),
        "stop_remaining_percent_lte": manifest["stop_remaining_percent_lte"],
        "validation": validation,
    }
    base.write_new_or_identical(raw_path, raw_bytes)
    base.write_new_or_identical(output_path, canonical_bytes(response))
    base.write_new_or_identical(receipt_path, canonical_bytes(receipt))
    return validation


def validate_existing(*, artifact_dir: Path, review_id: str, role: str) -> dict[str, Any]:
    manifest, row, packet, shortlist = _bound_inputs(artifact_dir, review_id, role)
    output_path, receipt_path = output_paths(artifact_dir, review_id, role)
    raw_path = base.raw_output_path(output_path)
    if not all(path.is_file() for path in (output_path, receipt_path, raw_path)):
        raise RunnerError("R7 output/raw/receipt set incomplete")
    response = load_json(output_path)
    validation = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role)
    if validation["status"] != "VALID":
        raise RunnerError("R7 existing output is locally invalid")
    receipt = load_json(receipt_path)
    schema = transport_schema(build_schema(packet, shortlist, role))
    prompt = rendered_prompt((artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"), packet, shortlist, role)
    checks = {
        "runner_version": RUNNER_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "R7_ROLE_QUALIFICATION",
        "review_id": review_id,
        "as_of": row["as_of"],
        "role": role,
        "execution_manifest_sha256": sha256_path(artifact_dir / OUTPUT_MANIFEST),
        "input_packet_sha256": row["input_packet_sha256"],
        "shortlist_sha256": row["shortlist_sha256"],
        "prompt_source_sha256": manifest["prompt_sha256"],
        "qualification_contract_sha256": manifest["qualification_contract_sha256"],
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        "rendered_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "raw_output_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "normalized_output_sha256": hashlib.sha256(canonical_bytes(response)).hexdigest(),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"R7 existing receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("R7 existing receipt validation mismatch")
    return validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--usage-attestation", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--validate-existing", action="store_true")
    args = parser.parse_args()
    if args.validate_existing:
        result = validate_existing(artifact_dir=args.artifact_dir, review_id=args.review_id, role=args.role)
    else:
        if args.usage_attestation is None:
            parser.error("--usage-attestation is required for a new formal AI call")
        result = run_role(
            artifact_dir=args.artifact_dir,
            review_id=args.review_id,
            role=args.role,
            usage_attestation_path=args.usage_attestation,
            timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
