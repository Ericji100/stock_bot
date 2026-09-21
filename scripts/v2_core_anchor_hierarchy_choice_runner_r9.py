"""Immutable R9B hierarchy runner over frozen R9A representatives and defense."""

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
from scripts.v2_core_anchor_family_retrieval_runner_r9 import output_paths as retrieval_paths, validate_existing as validate_retrieval
from scripts.v2_core_anchor_hierarchy_choice_r9 import parse_raw_without_duplicate_keys, transport_for, validate_and_choose
from scripts.v2_core_episode_defense_runner_r8 import output_paths as defense_paths, validate_existing as validate_defense
from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY, OUTPUT_DIRECTORY as DEFENSE_SHORTLIST_DIRECTORY
from scripts.v2_core_legacy_role_qualification_manifest_r7 import BUDGET_FILE, MODEL, REASONING
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path


VERSION = "v2-core-anchor-hierarchy-choice-runner-r9-candidate-r1"
PROMPT_FILE = "v2_core_anchor_hierarchy_choice.prompt.candidate_r9.md"
MANIFEST_DIRECTORY = "anchor_hierarchy_choice_manifests_candidate_r9"
OUTPUT_DIRECTORY = "anchor_hierarchy_choice_runs_candidate_r9"
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunnerError = base.StagedRunnerError


def manifest_path(artifact_dir: Path, review_id: str) -> Path:
    return artifact_dir / MANIFEST_DIRECTORY / f"{review_id}.json"


def output_paths(artifact_dir: Path, review_id: str) -> tuple[Path, Path]:
    output = artifact_dir / OUTPUT_DIRECTORY / f"{review_id}.json"
    return output, output.with_name(output.stem + ".receipt.json")


def _inputs(artifact_dir: Path, review_id: str) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any], dict[str, Any]]:
    packet = load_json(artifact_dir / INPUT_DIRECTORY / f"{review_id}.json")
    defense = load_json(artifact_dir / DEFENSE_SHORTLIST_DIRECTORY / f"{review_id}.json")
    defense_result = validate_defense(artifact_dir=artifact_dir, review_id=review_id)
    if defense_result["resolution_status"] != "SELECTED":
        raise RunnerError("R9B requires selected defense")
    defense_id = defense_result["selected_candidate_id"]
    defense_row = next(row for row in defense["candidate_rows"] if row["candidate_id"] == defense_id)
    retrieval = validate_retrieval(artifact_dir=artifact_dir, review_id=review_id)
    if retrieval["bound_defense_candidate_id"] != defense_id:
        raise RunnerError("R9B retrieval/defense mismatch")
    return packet, defense, defense_id, defense_row, retrieval


def build_manifest(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    packet, defense, defense_id, _defense_row, retrieval = _inputs(artifact_dir, review_id)
    schema = transport_for(packet, defense, defense_id, retrieval)
    defense_output, defense_receipt = defense_paths(artifact_dir, review_id)
    retrieval_output, retrieval_receipt = retrieval_paths(artifact_dir, review_id)
    contract = ROOT / "scripts" / "v2_core_anchor_hierarchy_choice_r9.py"
    return {
        "manifest_version": VERSION,
        "status": "CANDIDATE_NOT_FROZEN（候選、尚未凍結）",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "formal_model": MODEL,
        "reasoning_effort": REASONING,
        "required_rounds": 1,
        "input_packet_sha256": sha256_path(artifact_dir / INPUT_DIRECTORY / f"{review_id}.json"),
        "defense_shortlist_sha256": sha256_path(artifact_dir / DEFENSE_SHORTLIST_DIRECTORY / f"{review_id}.json"),
        "defense_output_sha256": sha256_path(defense_output),
        "defense_receipt_sha256": sha256_path(defense_receipt),
        "retrieval_output_sha256": sha256_path(retrieval_output),
        "retrieval_receipt_sha256": sha256_path(retrieval_receipt),
        "bound_defense_candidate_id": defense_id,
        "representative_ids_sha256": hashlib.sha256(canonical_bytes(retrieval["group_representatives"])).hexdigest(),
        "prompt_file": PROMPT_FILE,
        "prompt_sha256": sha256_path(artifact_dir / PROMPT_FILE),
        "contract_file": contract.name,
        "contract_sha256": sha256_path(contract),
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


def _bound_manifest(artifact_dir: Path, review_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    stored = load_json(manifest_path(artifact_dir, review_id))
    if stored != build_manifest(artifact_dir, review_id):
        raise RunnerError("R9B manifest changed")
    packet, defense, defense_id, defense_row, retrieval = _inputs(artifact_dir, review_id)
    schema = transport_for(packet, defense, defense_id, retrieval)
    return stored, packet, defense, defense_row, retrieval, schema


def rendered_prompt(prompt_text: str, packet: dict[str, Any], defense_row: dict[str, Any], retrieval: dict[str, Any]) -> str:
    pool = {row["candidate_id"]: row for row in packet["candidate_pool"]}
    representatives = [
        {**group, "candidate": pool[group["candidate_id"]]}
        for group in retrieval["group_representatives"].values()
    ]
    payload = {"packet": packet, "frozen_trade_episode_defense": defense_row, "frozen_family_representatives": representatives}
    return (
        prompt_text.rstrip()
        + "\n\n以下是匿名AS-OF封包、固定防線與逐組固定代表。只判背景／工作錨層級。\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


def run_choice(
    *, artifact_dir: Path, review_id: str, usage_attestation_path: Path,
    timeout_seconds: int = 1200, run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    manifest, packet, defense, defense_row, retrieval, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw, invalid = base.raw_output_path(output), base.invalid_output_path(output)
    if any(path.exists() for path in (output, receipt_path, raw, invalid)):
        raise RunnerError("R9B output exists; validate instead of re-running")
    base.assert_budget_allows(base.parse_usage_attestation(usage_attestation_path), manifest["stop_remaining_percent_lte"])
    prompt = rendered_prompt((artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"), packet, defense_row, retrieval)
    response, receipt, raw_bytes = base.call_codex(
        prompt=prompt, transport_schema=schema, timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid, run_command=run_command,
    )
    try:
        duplicate_checked = parse_raw_without_duplicate_keys(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R9B raw JSON duplicate/parse validation failed") from exc
    if duplicate_checked != response:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R9B raw/transport mismatch")
    validation = validate_and_choose(
        response, packet=packet, defense=defense,
        defense_id=manifest["bound_defense_candidate_id"], retrieval=retrieval,
    )
    if validation["status"] != "VALID":
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R9B local validation failed: " + json.dumps(validation, ensure_ascii=False))
    receipt = {
        **receipt,
        "runner_version": VERSION,
        "stage": "R9B_ANCHOR_HIERARCHY_CHOICE_CANDIDATE",
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
        raise RunnerError("R9B output/raw/receipt incomplete")
    response = load_json(output)
    if parse_raw_without_duplicate_keys(raw.read_bytes()) != response:
        raise RunnerError("R9B raw/normalized mismatch")
    validation = validate_and_choose(
        response, packet=packet, defense=defense,
        defense_id=manifest["bound_defense_candidate_id"], retrieval=retrieval,
    )
    if validation["status"] != "VALID":
        raise RunnerError("R9B existing output invalid")
    prompt = rendered_prompt((artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"), packet, defense_row, retrieval)
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "R9B_ANCHOR_HIERARCHY_CHOICE_CANDIDATE",
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
            raise RunnerError(f"R9B receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("R9B receipt validation mismatch")
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
