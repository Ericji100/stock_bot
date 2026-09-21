"""Immutable R10B V2 trigger pilot with a separately selected tactical cycle."""

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
from scripts.v2_core_episode_defense_runner_r8 import output_paths as defense_paths
from scripts.v2_core_legacy_role_qualification_manifest_r7 import BUDGET_FILE, MODEL, REASONING
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path
from scripts.v2_core_tactical_cycle_anchor_runner_r10 import _inputs as role_inputs, output_paths as role_paths, validate_existing as validate_role
from scripts.v2_core_trigger_judgement_r10b import confirmed_high_options, objective_risk_context, obstacle_high_options, parse_raw_without_duplicate_keys, transport_for, validate_and_gate


VERSION = "v2-core-trigger-runner-r10b-candidate-r1"
MANIFEST_DIRECTORY = "trigger_manifests_candidate_r10b"
OUTPUT_DIRECTORY = "trigger_runs_candidate_r10b"
DESIGN_FILE = "R10_SOURCE_BOUND_REPAIR_PLAN_CANDIDATE.md"
PROMPT_BY_SCENARIO = {
    "FRESH_Q1_EXPANSION": "v2_core_trigger.prompt.candidate_r10b_fresh.md",
    "MACRO_COPY_RESONANCE": "v2_core_trigger.prompt.candidate_r10b_macro.md",
}
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunnerError = base.StagedRunnerError


def manifest_path(artifact_dir: Path, review_id: str) -> Path:
    return artifact_dir / MANIFEST_DIRECTORY / f"{review_id}.json"


def output_paths(artifact_dir: Path, review_id: str) -> tuple[Path, Path]:
    output = artifact_dir / OUTPUT_DIRECTORY / f"{review_id}.json"
    return output, output.with_name(output.stem + ".receipt.json")


def _inputs(artifact_dir: Path, review_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str, str, str]:
    packet, retrieval, hierarchy, defense_id, defense_row = role_inputs(artifact_dir, review_id)
    role = validate_role(artifact_dir=artifact_dir, review_id=review_id)
    if role["resolution_status"] != "SELECTED" or role["trade_permission_granted"]:
        raise RunnerError("R10B requires selected no-trade tactical cycle")
    if role["bound_broad_anchor_id"] != hierarchy["selected_working_anchor_id"] or role["bound_defense_candidate_id"] != defense_id:
        raise RunnerError("R10B broad/defense role binding changed")
    scenario = role["program_scenario"]
    if scenario not in PROMPT_BY_SCENARIO:
        raise RunnerError("R10B scenario unsupported")
    tactical_id = role["selected_tactical_cycle_anchor_id"]
    broad_id = hierarchy["selected_working_anchor_id"]
    return packet, hierarchy, defense_row, role, defense_id, broad_id, tactical_id


def build_manifest(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    packet, _hierarchy, defense_row, role, defense_id, broad_id, tactical_id = _inputs(artifact_dir, review_id)
    scenario = role["program_scenario"]
    schema = transport_for(packet, defense_id, broad_id, tactical_id, scenario)
    defense_output, defense_receipt = defense_paths(artifact_dir, review_id)
    role_output, role_receipt = role_paths(artifact_dir, review_id)
    contract = ROOT / "scripts" / "v2_core_trigger_judgement_r10b.py"
    prompt_file = PROMPT_BY_SCENARIO[scenario]
    return {
        "manifest_version": VERSION,
        "status": "CANDIDATE_NOT_FROZEN（候選、尚未凍結）",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "formal_model": MODEL,
        "reasoning_effort": REASONING,
        "required_rounds": 1,
        "input_packet_sha256": sha256_path(artifact_dir / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{review_id}.json"),
        "defense_output_sha256": sha256_path(defense_output),
        "defense_receipt_sha256": sha256_path(defense_receipt),
        "tactical_role_output_sha256": sha256_path(role_output),
        "tactical_role_receipt_sha256": sha256_path(role_receipt),
        "bound_defense_candidate_id": defense_id,
        "bound_broad_anchor_id": broad_id,
        "bound_tactical_cycle_anchor_id": tactical_id,
        "program_scenario": scenario,
        "objective_risk_context": objective_risk_context(packet, defense_row),
        "break_high_option_count": len(confirmed_high_options(packet)),
        "obstacle_high_option_count": len(obstacle_high_options(packet)),
        "prompt_file": prompt_file,
        "prompt_sha256": sha256_path(artifact_dir / prompt_file),
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
        "fixed_rr_threshold": False,
        "break_high_window_is_pilot_only": True,
    }


def prepare(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    manifest = build_manifest(artifact_dir, review_id)
    base.write_new_or_identical(manifest_path(artifact_dir, review_id), canonical_bytes(manifest))
    return manifest


def _bound_manifest(artifact_dir: Path, review_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = load_json(manifest_path(artifact_dir, review_id))
    if manifest != build_manifest(artifact_dir, review_id):
        raise RunnerError("R10B manifest changed")
    packet, hierarchy, defense_row, role, defense_id, broad_id, tactical_id = _inputs(artifact_dir, review_id)
    schema = transport_for(packet, defense_id, broad_id, tactical_id, role["program_scenario"])
    return manifest, packet, hierarchy, defense_row, schema


def rendered_prompt(prompt_text: str, packet: dict[str, Any], hierarchy: dict[str, Any], defense_row: dict[str, Any], manifest: dict[str, Any]) -> str:
    pool = {row["candidate_id"]: row for row in packet["candidate_pool"]}
    payload = {
        "packet": packet,
        "fixed_broad_price_anchor": pool[manifest["bound_broad_anchor_id"]],
        "fixed_tactical_cycle_anchor": pool[manifest["bound_tactical_cycle_anchor_id"]],
        "fixed_trade_episode_defense": defense_row,
        "objective_risk_context": manifest["objective_risk_context"],
        "confirmed_break_high_options": confirmed_high_options(packet),
        "obstacle_high_options": obstacle_high_options(packet),
    }
    return prompt_text.rstrip() + "\n\n以下為匿名AS-OF封包、已固定的三級物件、實際防線風險與突破／障礙選項。只判訊號日收盤。\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def run_trigger(
    *, artifact_dir: Path, review_id: str, usage_attestation_path: Path,
    timeout_seconds: int = 1200, run_command: RunCommand = subprocess.run,
) -> dict[str, Any]:
    manifest, packet, hierarchy, defense_row, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw, invalid = base.raw_output_path(output), base.invalid_output_path(output)
    if any(path.exists() for path in (output, receipt_path, raw, invalid)):
        raise RunnerError("R10B output exists; validate instead of re-running")
    base.assert_budget_allows(base.parse_usage_attestation(usage_attestation_path), manifest["stop_remaining_percent_lte"])
    prompt = rendered_prompt((artifact_dir / manifest["prompt_file"]).read_text(encoding="utf-8"), packet, hierarchy, defense_row, manifest)
    response, receipt, raw_bytes = base.call_codex(
        prompt=prompt, transport_schema=schema, timeout_seconds=timeout_seconds,
        invalid_raw_path=invalid, run_command=run_command,
    )
    try:
        duplicate_checked = parse_raw_without_duplicate_keys(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R10B raw JSON duplicate/parse validation failed") from exc
    if duplicate_checked != response:
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R10B raw/transport mismatch")
    validation = validate_and_gate(
        response, packet=packet, defense_id=manifest["bound_defense_candidate_id"],
        defense_row=defense_row, broad_id=manifest["bound_broad_anchor_id"],
        tactical_id=manifest["bound_tactical_cycle_anchor_id"], scenario=manifest["program_scenario"],
    )
    if validation["status"] != "VALID":
        base.write_new_or_identical(invalid, raw_bytes)
        raise RunnerError("R10B local validation failed: " + json.dumps(validation, ensure_ascii=False))
    receipt = {
        **receipt,
        "runner_version": VERSION,
        "stage": "R10B_V2_TRIGGER_SIGNAL_CANDIDATE",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "defense_output_sha256": manifest["defense_output_sha256"],
        "defense_receipt_sha256": manifest["defense_receipt_sha256"],
        "tactical_role_output_sha256": manifest["tactical_role_output_sha256"],
        "tactical_role_receipt_sha256": manifest["tactical_role_receipt_sha256"],
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
    manifest, packet, hierarchy, defense_row, schema = _bound_manifest(artifact_dir, review_id)
    output, receipt_path = output_paths(artifact_dir, review_id)
    raw = base.raw_output_path(output)
    if not all(path.is_file() for path in (output, receipt_path, raw)):
        raise RunnerError("R10B output/raw/receipt incomplete")
    response = load_json(output)
    if parse_raw_without_duplicate_keys(raw.read_bytes()) != response:
        raise RunnerError("R10B raw/normalized mismatch")
    validation = validate_and_gate(
        response, packet=packet, defense_id=manifest["bound_defense_candidate_id"],
        defense_row=defense_row, broad_id=manifest["bound_broad_anchor_id"],
        tactical_id=manifest["bound_tactical_cycle_anchor_id"], scenario=manifest["program_scenario"],
    )
    if validation["status"] != "VALID":
        raise RunnerError("R10B existing output invalid")
    prompt = rendered_prompt((artifact_dir / manifest["prompt_file"]).read_text(encoding="utf-8"), packet, hierarchy, defense_row, manifest)
    receipt = load_json(receipt_path)
    checks = {
        "runner_version": VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "stage": "R10B_V2_TRIGGER_SIGNAL_CANDIDATE",
        "review_id": review_id,
        "as_of": packet["as_of"],
        "manifest_sha256": sha256_path(manifest_path(artifact_dir, review_id)),
        "defense_output_sha256": manifest["defense_output_sha256"],
        "defense_receipt_sha256": manifest["defense_receipt_sha256"],
        "tactical_role_output_sha256": manifest["tactical_role_output_sha256"],
        "tactical_role_receipt_sha256": manifest["tactical_role_receipt_sha256"],
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
            raise RunnerError(f"R10B receipt mismatch: {key}")
    if receipt.get("validation") != validation:
        raise RunnerError("R10B receipt validation mismatch")
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
