"""Immutable R7B role adjudication against one frozen episode."""

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
from scripts.v2_core_legacy_role_adjudication_r7b import transport_for, validate_and_resolve
from scripts.v2_core_legacy_role_qualification_manifest_r7 import BUDGET_FILE, MODEL, REASONING
from scripts.v2_core_legacy_role_qualification_r7b import episode_object
from scripts.v2_core_legacy_role_qualification_runner_r7 import _bound_inputs
from scripts.v2_core_legacy_role_qualification_runner_r7b import (
    frozen_episode,
    manifest_path as qualification_manifest_path,
    output_paths as qualification_paths,
    validate_existing as validate_qualification,
)
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path


VERSION = "v2-core-legacy-role-adjudication-runner-r7b-candidate-r1"
PROMPT_FILE = "v2_core_legacy_role_adjudication.prompt.candidate_r7b.md"
MANIFEST_DIRECTORY = "legacy_role_adjudication_manifests_candidate_r7b"
OUTPUT_DIRECTORY = "legacy_role_adjudication_runs_candidate_r7b"
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunnerError = base.StagedRunnerError


def manifest_path(artifact_dir: Path, review_id: str, role: str) -> Path:
    return artifact_dir / MANIFEST_DIRECTORY / role / f"{review_id}.json"


def output_paths(artifact_dir: Path, review_id: str, role: str) -> tuple[Path, Path]:
    output = artifact_dir / OUTPUT_DIRECTORY / role / f"{review_id}.json"
    return output, output.with_name(output.stem + ".receipt.json")


def _context(artifact_dir: Path, review_id: str, role: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    _source_manifest, _row, packet, shortlist = _bound_inputs(artifact_dir, review_id, role)
    episode, _episode_paths = frozen_episode(artifact_dir, review_id)
    qualification = validate_qualification(artifact_dir=artifact_dir, review_id=review_id, role=role)
    if qualification["resolution_reason"] != "MULTIPLE_NON_EQUIVALENT_ELIGIBLE_CANDIDATES":
        raise RunnerError("R7B adjudication only for non-equivalent eligible ambiguity")
    return packet, shortlist, qualification, episode


def build_manifest(artifact_dir: Path, review_id: str, role: str) -> dict[str, Any]:
    packet, shortlist, qualification, episode = _context(artifact_dir, review_id, role)
    output, receipt = qualification_paths(artifact_dir, review_id, role)
    raw = base.raw_output_path(output)
    schema = transport_for(packet, shortlist, role, qualification, episode)
    prompt_path = artifact_dir / PROMPT_FILE
    contract_path = ROOT / "scripts" / "v2_core_legacy_role_adjudication_r7b.py"
    return {
        "manifest_version": VERSION,
        "status": "CANDIDATE_NOT_FROZEN（候選、尚未凍結）",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "role": role,
        "formal_model": MODEL,
        "reasoning_effort": REASONING,
        "required_rounds": 1,
        "qualification_manifest_sha256": sha256_path(qualification_manifest_path(artifact_dir, review_id)),
        "input_packet_sha256": sha256_path(artifact_dir / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{review_id}.json"),
        "shortlist_sha256": sha256_path(artifact_dir / "legacy_role_shortlists_candidate_r7_v2" / f"{review_id}.json"),
        "qualification_output_sha256": sha256_path(output),
        "qualification_raw_sha256": sha256_path(raw),
        "qualification_receipt_sha256": sha256_path(receipt),
        "bound_episode_candidate_id": episode["selected_candidate_id"],
        "eligible_candidate_ids": qualification["eligible_candidate_ids"],
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


def prepare(artifact_dir: Path, review_id: str, role: str) -> dict[str, Any]:
    manifest = build_manifest(artifact_dir, review_id, role)
    base.write_new_or_identical(manifest_path(artifact_dir, review_id, role), canonical_bytes(manifest))
    return manifest


def _bound_manifest(artifact_dir: Path, review_id: str, role: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    stored = load_json(manifest_path(artifact_dir, review_id, role))
    if stored != build_manifest(artifact_dir, review_id, role):
        raise RunnerError("R7B adjudication manifest bindings changed")
    packet, shortlist, qualification, episode = _context(artifact_dir, review_id, role)
    schema = transport_for(packet, shortlist, role, qualification, episode)
    return stored, packet, shortlist, qualification, episode


def rendered_prompt(prompt_text: str, packet: dict[str, Any], shortlist: dict[str, Any], role: str, qualification: dict[str, Any], episode: dict[str, Any], qualification_output: dict[str, Any]) -> str:
    eligible = set(qualification["eligible_candidate_ids"])
    payload = {
        "packet": packet,
        "bound_current_episode": episode_object(shortlist, episode),
        "adjudication_role": role,
        "eligible_role_candidates": [row for row in shortlist["roles"][role]["candidate_rows"] if row["candidate_id"] in eligible],
        "frozen_eligible_assessments": [row for row in qualification_output["candidate_assessments"] if row["candidate_id"] in eligible],
    }
    return (
        prompt_text.rstrip()
        + "\n\n以下是匿名AS-OF封包、固定episode及已凍結eligible候選。只做相對角色裁決。\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


def run_adjudication(
    *, artifact_dir: Path, review_id: str, role: str, usage_attestation_path: Path,
    timeout_seconds: int = 1200, run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    manifest, packet, shortlist, qualification, episode = _bound_manifest(artifact_dir, review_id, role)
    output, receipt_path = output_paths(artifact_dir, review_id, role)
    raw, invalid = base.raw_output_path(output), base.invalid_output_path(output)
    if any(path.exists() for path in (output, receipt_path, raw, invalid)):
        raise RunnerError("R7B adjudication output exists; validate instead of re-running")
    attestation = base.parse_usage_attestation(usage_attestation_path)
    base.assert_budget_allows(attestation, manifest["stop_remaining_percent_lte"])
    qualified_output, _qualified_receipt = qualification_paths(artifact_dir, review_id, role)
    schema = transport_for(packet, shortlist, role, qualification, episode)
    prompt = rendered_prompt(
        (artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"),
        packet, shortlist, role, qualification, episode, load_json(qualified_output),
    )
    response, receipt, raw_bytes = base.call_codex(
        prompt=prompt, transport_schema=schema, timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid, run_command=run_command,
    )
    validation = validate_and_resolve(
        response, packet=packet, shortlist=shortlist, role=role,
        qualification=qualification, episode_result=episode,
    )
    if validation["status"] != "VALID":
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R7B adjudication local validation failed: " + json.dumps(validation, ensure_ascii=False))
    receipt = {
        **receipt,
        "runner_version": VERSION,
        "stage": "R7B_EPISODE_BOUND_ROLE_ADJUDICATION",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "role": role,
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id, role)),
        "qualification_output_sha256": manifest["qualification_output_sha256"],
        "bound_episode_candidate_id": manifest["bound_episode_candidate_id"],
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


def validate_existing(*, artifact_dir: Path, review_id: str, role: str) -> dict[str, Any]:
    manifest, packet, shortlist, qualification, episode = _bound_manifest(artifact_dir, review_id, role)
    output, receipt_path = output_paths(artifact_dir, review_id, role)
    raw = base.raw_output_path(output)
    if not all(path.is_file() for path in (output, receipt_path, raw)):
        raise RunnerError("R7B adjudication output/raw/receipt incomplete")
    response = load_json(output)
    validation = validate_and_resolve(
        response, packet=packet, shortlist=shortlist, role=role,
        qualification=qualification, episode_result=episode,
    )
    if validation["status"] != "VALID":
        raise RunnerError("R7B existing adjudication invalid")
    qualified_output, _qualified_receipt = qualification_paths(artifact_dir, review_id, role)
    schema = transport_for(packet, shortlist, role, qualification, episode)
    prompt = rendered_prompt(
        (artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"),
        packet, shortlist, role, qualification, episode, load_json(qualified_output),
    )
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "R7B_EPISODE_BOUND_ROLE_ADJUDICATION",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "role": role,
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id, role)),
        "qualification_output_sha256": manifest["qualification_output_sha256"],
        "bound_episode_candidate_id": manifest["bound_episode_candidate_id"],
        "prompt_source_sha256": manifest["prompt_sha256"],
        "contract_sha256": manifest["contract_sha256"],
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        "rendered_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "raw_output_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "normalized_output_sha256": hashlib.sha256(canonical_bytes(response)).hexdigest(),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"R7B adjudication receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("R7B adjudication receipt validation mismatch")
    return validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--validate-existing", action="store_true")
    parser.add_argument("--usage-attestation", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    if args.prepare:
        result = prepare(args.artifact_dir, args.review_id, args.role)
    elif args.validate_existing:
        result = validate_existing(artifact_dir=args.artifact_dir, review_id=args.review_id, role=args.role)
    else:
        if args.usage_attestation is None:
            parser.error("--usage-attestation required for formal call")
        result = run_adjudication(
            artifact_dir=args.artifact_dir, review_id=args.review_id, role=args.role,
            usage_attestation_path=args.usage_attestation, timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
