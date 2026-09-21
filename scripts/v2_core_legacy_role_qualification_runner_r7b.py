"""R7B immutable role runner: reuse frozen R7 episode, bind four later roles."""

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
from scripts.v2_core_legacy_role_adjudication_runner_r7 import (
    output_paths as adjudication_paths,
    validate_existing as validate_episode_adjudication,
)
from scripts.v2_core_legacy_role_qualification_manifest_r7 import BUDGET_FILE, MODEL, OUTPUT_MANIFEST as R7_EXECUTION_MANIFEST, REASONING
from scripts.v2_core_legacy_role_qualification_r7b import episode_object, transport_for, validate_and_resolve
from scripts.v2_core_legacy_role_qualification_runner_r7 import (
    _bound_inputs,
    output_paths as qualification_paths,
    validate_existing as validate_episode_qualification,
)
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path


VERSION = "v2-core-legacy-role-qualification-runner-r7b-candidate-r1"
PROMPT_FILE = "v2_core_legacy_role_qualification.prompt.candidate_r7b.md"
MANIFEST_DIRECTORY = "legacy_role_qualification_manifests_candidate_r7b"
OUTPUT_DIRECTORY = "legacy_role_qualification_runs_candidate_r7b"
EPISODE_ROLE = "CURRENT_EPISODE_UP"
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunnerError = base.StagedRunnerError


def manifest_path(artifact_dir: Path, review_id: str) -> Path:
    return artifact_dir / MANIFEST_DIRECTORY / f"{review_id}.json"


def output_paths(artifact_dir: Path, review_id: str, role: str) -> tuple[Path, Path]:
    output = artifact_dir / OUTPUT_DIRECTORY / role / f"{review_id}.json"
    return output, output.with_name(output.stem + ".receipt.json")


def frozen_episode(artifact_dir: Path, review_id: str) -> tuple[dict[str, Any], dict[str, Path]]:
    first = validate_episode_qualification(artifact_dir=artifact_dir, review_id=review_id, role=EPISODE_ROLE)
    first_output, first_receipt = qualification_paths(artifact_dir, review_id, EPISODE_ROLE)
    paths = {"qualification_output": first_output, "qualification_receipt": first_receipt}
    if first["resolution_reason"] == "MULTIPLE_NON_EQUIVALENT_ELIGIBLE_CANDIDATES":
        result = validate_episode_adjudication(artifact_dir=artifact_dir, review_id=review_id, role=EPISODE_ROLE)
        adj_output, adj_receipt = adjudication_paths(artifact_dir, review_id, EPISODE_ROLE)
        paths.update({"adjudication_output": adj_output, "adjudication_receipt": adj_receipt})
    else:
        result = first
    if result["role_status"] != "SELECTED":
        raise RunnerError("R7B requires a uniquely resolved frozen episode")
    return result, paths


def build_manifest(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    _r7_manifest, _row, packet, shortlist = _bound_inputs(artifact_dir, review_id, EPISODE_ROLE)
    episode_result, episode_paths = frozen_episode(artifact_dir, review_id)
    episode = episode_object(shortlist, episode_result)
    role_rows = []
    for role in sorted(shortlist["roles"]):
        if role == EPISODE_ROLE:
            continue
        schema = transport_for(packet, shortlist, role, episode_result)
        role_rows.append({
            "role": role,
            "candidate_count": shortlist["roles"][role]["shortlist_count"],
            "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        })
    prompt_path = artifact_dir / PROMPT_FILE
    contract_path = ROOT / "scripts" / "v2_core_legacy_role_qualification_r7b.py"
    return {
        "manifest_version": VERSION,
        "status": "CANDIDATE_NOT_FROZEN（候選、尚未凍結）",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "formal_model": MODEL,
        "reasoning_effort": REASONING,
        "required_rounds": 1,
        "r7_execution_manifest_sha256": sha256_path(artifact_dir / R7_EXECUTION_MANIFEST),
        "r7_input_packet_sha256": _row["input_packet_sha256"],
        "r7_shortlist_sha256": _row["shortlist_sha256"],
        "frozen_episode_candidate_id": episode["candidate_id"],
        "frozen_episode_evidence_option_id": episode["evidence_option_id"],
        "frozen_episode_source_hashes": {key: sha256_path(path) for key, path in episode_paths.items()},
        "prompt_file": PROMPT_FILE,
        "prompt_sha256": sha256_path(prompt_path),
        "contract_file": contract_path.name,
        "contract_sha256": sha256_path(contract_path),
        "budget_file": BUDGET_FILE,
        "budget_sha256": sha256_path(artifact_dir / BUDGET_FILE),
        "stop_remaining_percent_lte": 70,
        "formal_ai_calls": 0,
        "teacher_answers_read": False,
        "locked_set_opened": False,
        "future_performance_used": False,
        "role_rows": role_rows,
    }


def prepare(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    manifest = build_manifest(artifact_dir, review_id)
    base.write_new_or_identical(manifest_path(artifact_dir, review_id), canonical_bytes(manifest))
    return manifest


def _bound_manifest(artifact_dir: Path, review_id: str, role: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    stored = load_json(manifest_path(artifact_dir, review_id))
    if stored != build_manifest(artifact_dir, review_id):
        raise RunnerError("R7B execution manifest bindings changed")
    matches = [row for row in stored["role_rows"] if row["role"] == role]
    if len(matches) != 1:
        raise RunnerError("R7B role not uniquely listed")
    _r7_manifest, _row, packet, shortlist = _bound_inputs(artifact_dir, review_id, role)
    episode_result, _paths = frozen_episode(artifact_dir, review_id)
    schema = transport_for(packet, shortlist, role, episode_result)
    if hashlib.sha256(canonical_bytes(schema)).hexdigest() != matches[0]["transport_schema_sha256"]:
        raise RunnerError("R7B transport schema hash mismatch")
    return stored, packet, shortlist, episode_result, schema


def rendered_prompt(prompt_text: str, packet: dict[str, Any], shortlist: dict[str, Any], role: str, episode_result: dict[str, Any]) -> str:
    payload = {
        "packet": packet,
        "bound_current_episode": episode_object(shortlist, episode_result),
        "qualification_role": role,
        "qualification_role_shortlist": shortlist["roles"][role]["candidate_rows"],
    }
    return (
        prompt_text.rstrip()
        + "\n\n以下是匿名AS-OF封包、已固定episode物件與本次角色候選。"
        + "不得重新選episode；只輸出Schema JSON。\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


def run_role(
    *,
    artifact_dir: Path,
    review_id: str,
    role: str,
    usage_attestation_path: Path,
    timeout_seconds: int = 1200,
    run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    manifest, packet, shortlist, episode_result, schema = _bound_manifest(artifact_dir, review_id, role)
    output, receipt_path = output_paths(artifact_dir, review_id, role)
    raw, invalid = base.raw_output_path(output), base.invalid_output_path(output)
    if any(path.exists() for path in (output, receipt_path, raw, invalid)):
        raise RunnerError("R7B output exists; validate instead of re-running")
    attestation = base.parse_usage_attestation(usage_attestation_path)
    base.assert_budget_allows(attestation, manifest["stop_remaining_percent_lte"])
    prompt = rendered_prompt((artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"), packet, shortlist, role, episode_result)
    response, receipt, raw_bytes = base.call_codex(
        prompt=prompt,
        transport_schema=schema,
        timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid,
        run_command=run_command,
    )
    validation = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role, episode_result=episode_result)
    if validation["status"] != "VALID":
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R7B local validation failed: " + json.dumps(validation, ensure_ascii=False))
    receipt = {
        **receipt,
        "runner_version": VERSION,
        "stage": "R7B_EPISODE_BOUND_ROLE_QUALIFICATION",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "role": role,
        "execution_manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "frozen_episode_candidate_id": manifest["frozen_episode_candidate_id"],
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
    manifest, packet, shortlist, episode_result, schema = _bound_manifest(artifact_dir, review_id, role)
    output, receipt_path = output_paths(artifact_dir, review_id, role)
    raw = base.raw_output_path(output)
    if not all(path.is_file() for path in (output, receipt_path, raw)):
        raise RunnerError("R7B output/raw/receipt incomplete")
    response = load_json(output)
    validation = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role, episode_result=episode_result)
    if validation["status"] != "VALID":
        raise RunnerError("R7B existing output invalid")
    prompt = rendered_prompt((artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"), packet, shortlist, role, episode_result)
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "R7B_EPISODE_BOUND_ROLE_QUALIFICATION",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "role": role,
        "execution_manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "frozen_episode_candidate_id": manifest["frozen_episode_candidate_id"],
        "prompt_source_sha256": manifest["prompt_sha256"],
        "contract_sha256": manifest["contract_sha256"],
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        "rendered_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "raw_output_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "normalized_output_sha256": hashlib.sha256(canonical_bytes(response)).hexdigest(),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"R7B receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("R7B receipt validation mismatch")
    return validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--role")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--validate-existing", action="store_true")
    parser.add_argument("--usage-attestation", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    if args.prepare:
        result = prepare(args.artifact_dir, args.review_id)
    else:
        if not args.role:
            parser.error("--role required unless preparing")
        if args.validate_existing:
            result = validate_existing(artifact_dir=args.artifact_dir, review_id=args.review_id, role=args.role)
        else:
            if args.usage_attestation is None:
                parser.error("--usage-attestation required for formal call")
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
