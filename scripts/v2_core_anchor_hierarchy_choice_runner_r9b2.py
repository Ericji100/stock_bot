"""Immutable, separately versioned R9B2 evidence-ref transport runner."""

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
from scripts import v2_core_anchor_hierarchy_choice_runner_r9 as r9b_runner
from scripts.v2_core_anchor_hierarchy_choice_r9b2 import (
    parse_raw_without_duplicate_keys, transport_for, validate_and_choose,
)
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path


VERSION = "v2-core-anchor-hierarchy-choice-runner-r9b2-candidate-r1"
MANIFEST_DIRECTORY = "anchor_hierarchy_choice_manifests_candidate_r9b2"
OUTPUT_DIRECTORY = "anchor_hierarchy_choice_runs_candidate_r9b2"
STAGE = "R9B2_ANCHOR_HIERARCHY_CHOICE_CANDIDATE"
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunnerError = base.StagedRunnerError


def manifest_path(artifact_dir: Path, review_id: str) -> Path:
    return artifact_dir / MANIFEST_DIRECTORY / f"{review_id}.json"


def output_paths(artifact_dir: Path, review_id: str) -> tuple[Path, Path]:
    output = artifact_dir / OUTPUT_DIRECTORY / f"{review_id}.json"
    return output, output.with_name(output.stem + ".receipt.json")


def build_manifest(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    source = r9b_runner.build_manifest(artifact_dir, review_id)
    packet, defense, defense_id, _defense_row, retrieval = r9b_runner._inputs(artifact_dir, review_id)
    contract = ROOT / "scripts" / "v2_core_anchor_hierarchy_choice_r9b2.py"
    schema = transport_for(packet, defense, defense_id, retrieval)
    old_output, _old_receipt = r9b_runner.output_paths(artifact_dir, review_id)
    old_invalid = base.invalid_output_path(old_output)
    return {
        **source,
        "manifest_version": VERSION,
        "contract_file": contract.name,
        "contract_sha256": sha256_path(contract),
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        "transport_only_change_from_r9b": "ALLOW_REPEATED_EVIDENCE_REFS_WITH_ONE_EVIDENCE_WEIGHT",
        "predecessor_invalid_raw_sha256": sha256_path(old_invalid) if old_invalid.is_file() else None,
        "output_directory": OUTPUT_DIRECTORY,
    }


def prepare(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    manifest = build_manifest(artifact_dir, review_id)
    base.write_new_or_identical(manifest_path(artifact_dir, review_id), canonical_bytes(manifest))
    return manifest


def _bound_manifest(
    artifact_dir: Path, review_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    stored = load_json(manifest_path(artifact_dir, review_id))
    if stored != build_manifest(artifact_dir, review_id):
        raise RunnerError("R9B2 manifest changed")
    packet, defense, defense_id, defense_row, retrieval = r9b_runner._inputs(artifact_dir, review_id)
    schema = transport_for(packet, defense, defense_id, retrieval)
    return stored, packet, defense, defense_row, retrieval, schema


def run_choice(
    *, artifact_dir: Path, review_id: str, usage_attestation_path: Path,
    timeout_seconds: int = 1200, run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    manifest, packet, defense, defense_row, retrieval, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw, invalid = base.raw_output_path(output), base.invalid_output_path(output)
    if any(path.exists() for path in (output, receipt_path, raw, invalid)):
        raise RunnerError("R9B2 output exists; validate instead of re-running")
    base.assert_budget_allows(
        base.parse_usage_attestation(usage_attestation_path), manifest["stop_remaining_percent_lte"],
    )
    prompt = r9b_runner.rendered_prompt(
        (artifact_dir / manifest["prompt_file"]).read_text(encoding="utf-8"),
        packet, defense_row, retrieval,
    )
    response, receipt, raw_bytes = base.call_codex(
        prompt=prompt, transport_schema=schema, timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid, run_command=run_command,
    )
    try:
        parsed = parse_raw_without_duplicate_keys(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R9B2 raw JSON duplicate-key/parse validation failed") from exc
    if parsed != response:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R9B2 raw/transport mismatch")
    validation = validate_and_choose(
        response, packet=packet, defense=defense,
        defense_id=manifest["bound_defense_candidate_id"], retrieval=retrieval,
    )
    if validation["status"] != "VALID":
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R9B2 local validation failed: " + json.dumps(validation, ensure_ascii=False))
    receipt = {
        **receipt,
        "runner_version": VERSION,
        "stage": STAGE,
        "review_id": review_id,
        "as_of": packet["as_of"],
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "bound_defense_candidate_id": manifest["bound_defense_candidate_id"],
        "defense_output_sha256": manifest["defense_output_sha256"],
        "defense_receipt_sha256": manifest["defense_receipt_sha256"],
        "retrieval_output_sha256": manifest["retrieval_output_sha256"],
        "retrieval_receipt_sha256": manifest["retrieval_receipt_sha256"],
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
    manifest, packet, defense, defense_row, retrieval, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw = base.raw_output_path(output)
    if not all(path.is_file() for path in (output, receipt_path, raw)):
        raise RunnerError("R9B2 output/raw/receipt incomplete")
    response = load_json(output)
    if parse_raw_without_duplicate_keys(raw.read_bytes()) != response:
        raise RunnerError("R9B2 raw/normalized mismatch")
    validation = validate_and_choose(
        response, packet=packet, defense=defense,
        defense_id=manifest["bound_defense_candidate_id"], retrieval=retrieval,
    )
    if validation["status"] != "VALID":
        raise RunnerError("R9B2 existing output invalid")
    prompt = r9b_runner.rendered_prompt(
        (artifact_dir / manifest["prompt_file"]).read_text(encoding="utf-8"),
        packet, defense_row, retrieval,
    )
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": VERSION,
        "model": manifest["formal_model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "stage": STAGE,
        "review_id": review_id,
        "as_of": packet["as_of"],
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "bound_defense_candidate_id": manifest["bound_defense_candidate_id"],
        "defense_output_sha256": manifest["defense_output_sha256"],
        "defense_receipt_sha256": manifest["defense_receipt_sha256"],
        "retrieval_output_sha256": manifest["retrieval_output_sha256"],
        "retrieval_receipt_sha256": manifest["retrieval_receipt_sha256"],
        "prompt_source_sha256": manifest["prompt_sha256"],
        "contract_sha256": manifest["contract_sha256"],
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        "rendered_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "raw_output_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "normalized_output_sha256": hashlib.sha256(canonical_bytes(response)).hexdigest(),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"R9B2 receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("R9B2 receipt validation mismatch")
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
        result = run_choice(
            artifact_dir=args.artifact_dir, review_id=args.review_id,
            usage_attestation_path=args.usage_attestation, timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
