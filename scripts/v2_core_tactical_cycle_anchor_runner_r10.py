"""Immutable R10 tactical-cycle role pilot over frozen R8/R9A/R9B evidence."""

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
from scripts.v2_core_anchor_hierarchy_choice_runner_r9 import output_paths as hierarchy_paths, validate_existing as validate_hierarchy
from scripts.v2_core_episode_defense_runner_r8 import output_paths as defense_paths, validate_existing as validate_defense
from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY, OUTPUT_DIRECTORY as DEFENSE_SHORTLIST_DIRECTORY
from scripts.v2_core_legacy_role_qualification_manifest_r7 import BUDGET_FILE, MODEL, REASONING
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path
from scripts.v2_core_tactical_cycle_anchor_r10 import candidate_options, parse_raw_without_duplicate_keys, transport_for, validate_and_select


VERSION = "v2-core-tactical-cycle-anchor-runner-r10-candidate-r1"
PROMPT_FILE = "v2_core_tactical_cycle_anchor.prompt.candidate_r10.md"
DESIGN_FILE = "R10_SOURCE_BOUND_REPAIR_PLAN_CANDIDATE.md"
MANIFEST_DIRECTORY = "tactical_cycle_anchor_manifests_candidate_r10"
OUTPUT_DIRECTORY = "tactical_cycle_anchor_runs_candidate_r10"
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunnerError = base.StagedRunnerError


def manifest_path(artifact_dir: Path, review_id: str) -> Path:
    return artifact_dir / MANIFEST_DIRECTORY / f"{review_id}.json"


def output_paths(artifact_dir: Path, review_id: str) -> tuple[Path, Path]:
    output = artifact_dir / OUTPUT_DIRECTORY / f"{review_id}.json"
    return output, output.with_name(output.stem + ".receipt.json")


def _inputs(artifact_dir: Path, review_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, dict[str, Any]]:
    packet = load_json(artifact_dir / INPUT_DIRECTORY / f"{review_id}.json")
    defense = validate_defense(artifact_dir=artifact_dir, review_id=review_id)
    retrieval = validate_retrieval(artifact_dir=artifact_dir, review_id=review_id)
    hierarchy = validate_hierarchy(artifact_dir=artifact_dir, review_id=review_id)
    if packet["review_id"] != review_id:
        raise RunnerError("R10 packet ID mismatch")
    if any(stage["status"] != "VALID" for stage in (defense, retrieval, hierarchy)):
        raise RunnerError("R10 precursor invalid")
    if defense["resolution_status"] != "SELECTED" or hierarchy["resolution_status"] != "SELECTED":
        raise RunnerError("R10 requires selected defense/broad anchor")
    defense_id = defense["selected_candidate_id"]
    if hierarchy["bound_defense_candidate_id"] != defense_id or retrieval["bound_defense_candidate_id"] != defense_id:
        raise RunnerError("R10 precursor defense bindings differ")
    candidate_options(packet, hierarchy, retrieval)
    defense_shortlist = load_json(artifact_dir / DEFENSE_SHORTLIST_DIRECTORY / f"{review_id}.json")
    defense_row = next(row for row in defense_shortlist["candidate_rows"] if row["candidate_id"] == defense_id)
    return packet, retrieval, hierarchy, defense_id, defense_row


def build_manifest(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    packet, retrieval, hierarchy, defense_id, _defense_row = _inputs(artifact_dir, review_id)
    schema = transport_for(packet, hierarchy, retrieval, defense_id)
    defense_output, defense_receipt = defense_paths(artifact_dir, review_id)
    retrieval_output, retrieval_receipt = retrieval_paths(artifact_dir, review_id)
    hierarchy_output, hierarchy_receipt = hierarchy_paths(artifact_dir, review_id)
    contract = ROOT / "scripts" / "v2_core_tactical_cycle_anchor_r10.py"
    return {
        "manifest_version": VERSION,
        "status": "CANDIDATE_NOT_FROZEN（候選、尚未凍結）",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "formal_model": MODEL,
        "reasoning_effort": REASONING,
        "required_rounds": 1,
        "input_packet_sha256": sha256_path(artifact_dir / INPUT_DIRECTORY / f"{review_id}.json"),
        "defense_output_sha256": sha256_path(defense_output),
        "defense_receipt_sha256": sha256_path(defense_receipt),
        "retrieval_output_sha256": sha256_path(retrieval_output),
        "retrieval_receipt_sha256": sha256_path(retrieval_receipt),
        "hierarchy_output_sha256": sha256_path(hierarchy_output),
        "hierarchy_receipt_sha256": sha256_path(hierarchy_receipt),
        "bound_defense_candidate_id": defense_id,
        "bound_broad_anchor_id": hierarchy["selected_working_anchor_id"],
        "program_scenario": "FRESH_Q1_EXPANSION" if hierarchy["relationship_class"] == "FRESH_FORMING_UP_ANCHOR" else "MACRO_COPY_RESONANCE",
        "option_candidate_ids": list(candidate_options(packet, hierarchy, retrieval)),
        "prompt_file": PROMPT_FILE,
        "prompt_sha256": sha256_path(artifact_dir / PROMPT_FILE),
        "design_file": DESIGN_FILE,
        "design_sha256": sha256_path(artifact_dir / DESIGN_FILE),
        "v2_rule_sha256": sha256_path(ROOT / "docs" / "enlightenment-ai-judgement-v2.md"),
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
        "trade_permission_granted": False,
    }


def prepare(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    manifest = build_manifest(artifact_dir, review_id)
    base.write_new_or_identical(manifest_path(artifact_dir, review_id), canonical_bytes(manifest))
    return manifest


def _bound_manifest(artifact_dir: Path, review_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = load_json(manifest_path(artifact_dir, review_id))
    if manifest != build_manifest(artifact_dir, review_id):
        raise RunnerError("R10 manifest changed")
    packet, retrieval, hierarchy, defense_id, defense_row = _inputs(artifact_dir, review_id)
    schema = transport_for(packet, hierarchy, retrieval, defense_id)
    return manifest, packet, retrieval, hierarchy, defense_row, schema


def rendered_prompt(prompt_text: str, packet: dict[str, Any], hierarchy: dict[str, Any], retrieval: dict[str, Any], defense_row: dict[str, Any]) -> str:
    pool = {row["candidate_id"]: row for row in packet["candidate_pool"]}
    options = candidate_options(packet, hierarchy, retrieval)
    payload = {
        "packet": packet,
        "fixed_broad_price_anchor": pool[hierarchy["selected_working_anchor_id"]],
        "fixed_trade_episode_defense": defense_row,
        "tactical_cycle_options": options,
    }
    return prompt_text.rstrip() + "\n\n以下是匿名AS-OF封包、已固定大級價格錨與小級防線、完整戰術候選。只判內層世代時鐘角色。\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def run_selection(
    *, artifact_dir: Path, review_id: str, usage_attestation_path: Path,
    timeout_seconds: int = 1200, run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    manifest, packet, retrieval, hierarchy, defense_row, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw, invalid = base.raw_output_path(output), base.invalid_output_path(output)
    if any(path.exists() for path in (output, receipt_path, raw, invalid)):
        raise RunnerError("R10 output exists; validate instead of re-running")
    base.assert_budget_allows(base.parse_usage_attestation(usage_attestation_path), manifest["stop_remaining_percent_lte"])
    prompt = rendered_prompt((artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"), packet, hierarchy, retrieval, defense_row)
    response, receipt, raw_bytes = base.call_codex(
        prompt=prompt, transport_schema=schema, timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid, run_command=run_command,
    )
    try:
        duplicate_checked = parse_raw_without_duplicate_keys(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R10 raw JSON duplicate/parse validation failed") from exc
    if duplicate_checked != response:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R10 raw/transport mismatch")
    validation = validate_and_select(response, packet=packet, hierarchy=hierarchy, retrieval=retrieval, defense_id=manifest["bound_defense_candidate_id"])
    if validation["status"] != "VALID":
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R10 local validation failed: " + json.dumps(validation, ensure_ascii=False))
    receipt = {
        **receipt,
        "runner_version": VERSION,
        "stage": "R10_TACTICAL_CYCLE_ROLE_CANDIDATE",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "defense_output_sha256": manifest["defense_output_sha256"],
        "defense_receipt_sha256": manifest["defense_receipt_sha256"],
        "retrieval_output_sha256": manifest["retrieval_output_sha256"],
        "retrieval_receipt_sha256": manifest["retrieval_receipt_sha256"],
        "hierarchy_output_sha256": manifest["hierarchy_output_sha256"],
        "hierarchy_receipt_sha256": manifest["hierarchy_receipt_sha256"],
        "prompt_source_sha256": manifest["prompt_sha256"],
        "design_sha256": manifest["design_sha256"],
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
    manifest, packet, retrieval, hierarchy, defense_row, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw = base.raw_output_path(output)
    if not all(path.is_file() for path in (output, receipt_path, raw)):
        raise RunnerError("R10 output/raw/receipt incomplete")
    response = load_json(output)
    if parse_raw_without_duplicate_keys(raw.read_bytes()) != response:
        raise RunnerError("R10 raw/normalized mismatch")
    validation = validate_and_select(response, packet=packet, hierarchy=hierarchy, retrieval=retrieval, defense_id=manifest["bound_defense_candidate_id"])
    if validation["status"] != "VALID":
        raise RunnerError("R10 existing output invalid")
    prompt = rendered_prompt((artifact_dir / PROMPT_FILE).read_text(encoding="utf-8"), packet, hierarchy, retrieval, defense_row)
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "R10_TACTICAL_CYCLE_ROLE_CANDIDATE",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "defense_output_sha256": manifest["defense_output_sha256"],
        "defense_receipt_sha256": manifest["defense_receipt_sha256"],
        "retrieval_output_sha256": manifest["retrieval_output_sha256"],
        "retrieval_receipt_sha256": manifest["retrieval_receipt_sha256"],
        "hierarchy_output_sha256": manifest["hierarchy_output_sha256"],
        "hierarchy_receipt_sha256": manifest["hierarchy_receipt_sha256"],
        "prompt_source_sha256": manifest["prompt_sha256"],
        "design_sha256": manifest["design_sha256"],
        "contract_sha256": manifest["contract_sha256"],
        "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
        "rendered_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "raw_output_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "normalized_output_sha256": hashlib.sha256(canonical_bytes(response)).hexdigest(),
    }
    for key, expected in checks.items():
        if receipt.get(key) != expected:
            raise RunnerError(f"R10 receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("R10 receipt validation mismatch")
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
        result = run_selection(
            artifact_dir=args.artifact_dir, review_id=args.review_id,
            usage_attestation_path=args.usage_attestation, timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
