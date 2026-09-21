"""Immutable R9D transport-only rerun of the R9C route-specific trigger pilot."""

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
from scripts import v2_core_trigger_runner_r9 as r9c_runner
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path
from scripts.v2_core_trigger_judgement_r9d import parse_raw_without_duplicate_keys, transport_for, validate_and_gate


VERSION = "v2-core-trigger-runner-r9d-candidate-r1"
MANIFEST_DIRECTORY = "trigger_manifests_candidate_r9d"
OUTPUT_DIRECTORY = "trigger_runs_candidate_r9d"
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunnerError = base.StagedRunnerError


def manifest_path(artifact_dir: Path, review_id: str) -> Path:
    return artifact_dir / MANIFEST_DIRECTORY / f"{review_id}.json"


def output_paths(artifact_dir: Path, review_id: str) -> tuple[Path, Path]:
    output = artifact_dir / OUTPUT_DIRECTORY / f"{review_id}.json"
    return output, output.with_name(output.stem + ".receipt.json")


def build_manifest(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    source = r9c_runner.build_manifest(artifact_dir, review_id)
    packet, _defense_result, _hierarchy_result, _defense_row, defense_id, working_id, scenario = r9c_runner._inputs(artifact_dir, review_id)
    contract = ROOT / "scripts" / "v2_core_trigger_judgement_r9d.py"
    return {
        **source,
        "manifest_version": VERSION,
        "contract_file": contract.name,
        "contract_sha256": sha256_path(contract),
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(transport_for(packet, defense_id, working_id, scenario))).hexdigest(),
        "transport_only_change_from_r9c": "ALLOW_DUPLICATE_EVIDENCE_REFS_WITHOUT_EXTRA_WEIGHT",
        "r9c_invalid_attempt_preserved": True,
    }


def prepare(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    manifest = build_manifest(artifact_dir, review_id)
    base.write_new_or_identical(manifest_path(artifact_dir, review_id), canonical_bytes(manifest))
    return manifest


def _bound_manifest(artifact_dir: Path, review_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    stored = load_json(manifest_path(artifact_dir, review_id))
    if stored != build_manifest(artifact_dir, review_id):
        raise RunnerError("R9D manifest changed")
    packet, _defense_result, _hierarchy_result, defense_row, defense_id, working_id, scenario = r9c_runner._inputs(artifact_dir, review_id)
    working_row = next(row for row in packet["candidate_pool"] if row["candidate_id"] == working_id)
    schema = transport_for(packet, defense_id, working_id, scenario)
    return stored, packet, defense_row, working_row, schema


def run_trigger(
    *, artifact_dir: Path, review_id: str, usage_attestation_path: Path,
    timeout_seconds: int = 1200, run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    manifest, packet, defense_row, working_row, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw, invalid = base.raw_output_path(output), base.invalid_output_path(output)
    if any(path.exists() for path in (output, receipt_path, raw, invalid)):
        raise RunnerError("R9D output exists; validate instead of re-running")
    base.assert_budget_allows(base.parse_usage_attestation(usage_attestation_path), manifest["stop_remaining_percent_lte"])
    prompt = r9c_runner.rendered_prompt(
        (artifact_dir / manifest["prompt_file"]).read_text(encoding="utf-8"), packet, defense_row, working_row,
    )
    response, receipt, raw_bytes = base.call_codex(
        prompt=prompt, transport_schema=schema, timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid, run_command=run_command,
    )
    try:
        duplicate_checked = parse_raw_without_duplicate_keys(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R9D raw JSON duplicate/parse validation failed") from exc
    if duplicate_checked != response:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R9D raw/transport mismatch")
    validation = validate_and_gate(
        response, packet=packet, defense_id=manifest["bound_defense_candidate_id"],
        working_id=manifest["bound_working_anchor_id"], scenario=manifest["program_scenario"],
    )
    if validation["status"] != "VALID":
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R9D local validation failed: " + json.dumps(validation, ensure_ascii=False))
    receipt = {
        **receipt,
        "runner_version": VERSION,
        "stage": "R9D_V2_TRIGGER_SIGNAL_CANDIDATE",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "defense_output_sha256": manifest["defense_output_sha256"],
        "defense_receipt_sha256": manifest["defense_receipt_sha256"],
        "hierarchy_output_sha256": manifest["hierarchy_output_sha256"],
        "hierarchy_receipt_sha256": manifest["hierarchy_receipt_sha256"],
        "prompt_source_sha256": manifest["prompt_sha256"],
        "contract_sha256": manifest["contract_sha256"],
        "usage_attestation_sha256": sha256_path(usage_attestation_path),
        "stop_remaining_percent_lte": manifest["stop_remaining_percent_lte"],
        "validation": validation,
    }
    base.write_new_or_identical(raw, raw_bytes)
    base.write_new_or_identical(output, canonical_bytes(response))
    base.write_new_or_identical(receipt_path, canonical_bytes(receipt))
    return validation


def validate_existing(*, artifact_dir: Path, review_id: str) -> dict[str, Any]:
    manifest, packet, defense_row, working_row, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw = base.raw_output_path(output)
    if not all(path.is_file() for path in (output, receipt_path, raw)):
        raise RunnerError("R9D output/raw/receipt incomplete")
    response = load_json(output)
    if parse_raw_without_duplicate_keys(raw.read_bytes()) != response:
        raise RunnerError("R9D raw/normalized mismatch")
    validation = validate_and_gate(
        response, packet=packet, defense_id=manifest["bound_defense_candidate_id"],
        working_id=manifest["bound_working_anchor_id"], scenario=manifest["program_scenario"],
    )
    if validation["status"] != "VALID":
        raise RunnerError("R9D existing output invalid")
    prompt = r9c_runner.rendered_prompt(
        (artifact_dir / manifest["prompt_file"]).read_text(encoding="utf-8"), packet, defense_row, working_row,
    )
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": VERSION,
        "model": manifest["formal_model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "stage": "R9D_V2_TRIGGER_SIGNAL_CANDIDATE",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "defense_output_sha256": manifest["defense_output_sha256"],
        "defense_receipt_sha256": manifest["defense_receipt_sha256"],
        "hierarchy_output_sha256": manifest["hierarchy_output_sha256"],
        "hierarchy_receipt_sha256": manifest["hierarchy_receipt_sha256"],
        "prompt_source_sha256": manifest["prompt_sha256"],
        "contract_sha256": manifest["contract_sha256"],
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        "rendered_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "raw_output_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "normalized_output_sha256": hashlib.sha256(canonical_bytes(response)).hexdigest(),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"R9D receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("R9D receipt validation mismatch")
    return validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--validate-existing", action="store_true")
    parser.add_argument("--usage-attestation", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    if args.prepare:
        result = prepare(args.artifact_dir, args.review_id)
    elif args.validate_existing:
        result = validate_existing(artifact_dir=args.artifact_dir, review_id=args.review_id)
    else:
        if args.usage_attestation is None:
            parser.error("--usage-attestation required for formal call")
        result = run_trigger(
            artifact_dir=args.artifact_dir, review_id=args.review_id,
            usage_attestation_path=args.usage_attestation, timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
