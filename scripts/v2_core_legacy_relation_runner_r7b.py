"""Immutable R7B fixed-role relation runner and deterministic route receipt."""

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
from scripts.v2_core_legacy_relation_r7b import role_fingerprint, transport_for, validate_and_route
from scripts.v2_core_legacy_role_cross_object_preflight_r7b import build_report
from scripts.v2_core_legacy_role_qualification_manifest_r7 import BUDGET_FILE, MODEL, REASONING
from scripts.v2_core_legacy_role_qualification_runner_r7 import _bound_inputs
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path


VERSION = "v2-core-legacy-relation-runner-r7b-candidate-r1"
PROMPT_FILE = "v2_core_legacy_relation.prompt.candidate_r7b.md"
PREFLIGHT_DIRECTORY = "legacy_role_cross_object_preflight_candidate_r7b"
MANIFEST_DIRECTORY = "legacy_relation_manifests_candidate_r7b"
OUTPUT_DIRECTORY = "legacy_relation_runs_candidate_r7b"
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunnerError = base.StagedRunnerError


def preflight_path(artifact_dir: Path, review_id: str) -> Path:
    return artifact_dir / PREFLIGHT_DIRECTORY / f"{review_id}.json"


def manifest_path(artifact_dir: Path, review_id: str) -> Path:
    return artifact_dir / MANIFEST_DIRECTORY / f"{review_id}.json"


def output_paths(artifact_dir: Path, review_id: str) -> tuple[Path, Path]:
    output = artifact_dir / OUTPUT_DIRECTORY / f"{review_id}.json"
    return output, output.with_name(output.stem + ".receipt.json")


def _ready_report(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    current = build_report(artifact_dir, review_id)
    if current["status"] != "CROSS_ROLE_PRELIMINARY_READY":
        raise RunnerError("R7B role preflight not ready for relation AI")
    path = preflight_path(artifact_dir, review_id)
    stored = load_json(path)
    if stored != current:
        raise RunnerError("R7B preflight changed after freeze")
    return stored


def build_manifest(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    report = _ready_report(artifact_dir, review_id)
    _source_manifest, row, packet, _shortlist = _bound_inputs(artifact_dir, review_id, "CURRENT_EPISODE_UP")
    schema = transport_for(packet, report)
    prompt_path = artifact_dir / PROMPT_FILE
    contract_path = ROOT / "scripts" / "v2_core_legacy_relation_r7b.py"
    return {
        "manifest_version": VERSION,
        "status": "CANDIDATE_NOT_FROZEN（候選、尚未凍結）",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "formal_model": MODEL,
        "reasoning_effort": REASONING,
        "required_rounds": 1,
        "input_packet_sha256": row["input_packet_sha256"],
        "role_preflight_sha256": sha256_path(preflight_path(artifact_dir, review_id)),
        "bound_roles_sha256": role_fingerprint(report),
        "prompt_file": PROMPT_FILE,
        "prompt_sha256": sha256_path(prompt_path),
        "contract_file": contract_path.name,
        "contract_sha256": sha256_path(contract_path),
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        "budget_file": BUDGET_FILE,
        "budget_sha256": sha256_path(artifact_dir / BUDGET_FILE),
        "stop_remaining_percent_lte": 70,
        "formal_ai_calls": 0,
        "teacher_answers_read": False,
        "locked_set_opened": False,
        "future_performance_used": False,
    }


def prepare(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    manifest = build_manifest(artifact_dir, review_id)
    base.write_new_or_identical(manifest_path(artifact_dir, review_id), canonical_bytes(manifest))
    return manifest


def _bound_manifest(artifact_dir: Path, review_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    stored = load_json(manifest_path(artifact_dir, review_id))
    if stored != build_manifest(artifact_dir, review_id):
        raise RunnerError("R7B relation manifest bindings changed")
    report = _ready_report(artifact_dir, review_id)
    _source_manifest, _row, packet, _shortlist = _bound_inputs(artifact_dir, review_id, "CURRENT_EPISODE_UP")
    schema = transport_for(packet, report)
    return stored, packet, report, schema


def rendered_prompt(prompt_text: str, packet: dict[str, Any], report: dict[str, Any]) -> str:
    payload = {
        "packet": packet,
        "frozen_role_objects": report["selected_role_objects"],
        "frozen_role_resolutions": report["role_results"],
        "bound_roles_sha256": role_fingerprint(report),
    }
    return (
        prompt_text.rstrip()
        + "\n\n以下是唯一允許使用的匿名AS-OF封包與凍結角色。只判關係原子，不改角色。\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


def run_relation(
    *,
    artifact_dir: Path,
    review_id: str,
    usage_attestation_path: Path,
    timeout_seconds: int = 1200,
    run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    manifest, packet, report, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw, invalid = base.raw_output_path(output), base.invalid_output_path(output)
    if any(path.exists() for path in (output, receipt_path, raw, invalid)):
        raise RunnerError("R7B relation output exists; validate instead of re-running")
    attestation = base.parse_usage_attestation(usage_attestation_path)
    base.assert_budget_allows(attestation, manifest["stop_remaining_percent_lte"])
    prompt = rendered_prompt((artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"), packet, report)
    response, receipt, raw_bytes = base.call_codex(
        prompt=prompt,
        transport_schema=schema,
        timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid,
        run_command=run_command,
    )
    validation = validate_and_route(response, packet=packet, report=report)
    if validation["status"] != "VALID":
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R7B relation local validation failed: " + json.dumps(validation, ensure_ascii=False))
    receipt = {
        **receipt,
        "runner_version": VERSION,
        "stage": "R7B_FIXED_ROLE_RELATIONS",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "role_preflight_sha256": manifest["role_preflight_sha256"],
        "bound_roles_sha256": manifest["bound_roles_sha256"],
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
    manifest, packet, report, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw = base.raw_output_path(output)
    if not all(path.is_file() for path in (output, receipt_path, raw)):
        raise RunnerError("R7B relation output/raw/receipt incomplete")
    response = load_json(output)
    validation = validate_and_route(response, packet=packet, report=report)
    if validation["status"] != "VALID":
        raise RunnerError("R7B existing relation output invalid")
    prompt = rendered_prompt((artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"), packet, report)
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "R7B_FIXED_ROLE_RELATIONS",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "role_preflight_sha256": manifest["role_preflight_sha256"],
        "bound_roles_sha256": manifest["bound_roles_sha256"],
        "prompt_source_sha256": manifest["prompt_sha256"],
        "contract_sha256": manifest["contract_sha256"],
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        "rendered_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "raw_output_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "normalized_output_sha256": hashlib.sha256(canonical_bytes(response)).hexdigest(),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"R7B relation receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("R7B relation receipt validation mismatch")
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
        result = run_relation(
            artifact_dir=args.artifact_dir,
            review_id=args.review_id,
            usage_attestation_path=args.usage_attestation,
            timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
