"""Freeze the R5 execution manifest for one-pass lifecycle-first alignment."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
MANIFEST_VERSION = "v2-core-legacy-role-alignment-execution-r5-candidate"
OUTPUT_FILE = "legacy_role_alignment_execution_manifest_candidate_r5.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def build_manifest(artifact_dir: Path) -> dict[str, Any]:
    files = {
        "distillation_design": artifact_dir / "LEGACY_TEACHER_DISTILLATION_R5.md",
        "role_contract_md": artifact_dir / "LEGACY_ROLE_SELECTION_CONTRACT_R5.md",
        "role_contract_json": artifact_dir / "legacy_role_selection_contract.candidate_r5.json",
        "pairwise_audit": artifact_dir / "legacy_teacher_pairwise_audit_candidate_r1.json",
        "contract_fit_audit": artifact_dir / "legacy_role_contract_fit_candidate_r1.json",
        "selection": artifact_dir / "legacy_anchor_alignment_selection_candidate_r1.json",
        "input_manifest": artifact_dir / "legacy_anchor_alignment_input_manifest_candidate_r1.json",
        "prompt": artifact_dir / "v2_core_legacy_role_alignment.prompt.candidate_r5.md",
        "schema": artifact_dir / "v2_core_legacy_role_alignment.schema.candidate_r5.json",
        "runner": ROOT / "scripts" / "v2_core_legacy_role_alignment_runner_v5.py",
        "validator": ROOT / "scripts" / "v2_core_legacy_role_alignment_validator_v5.py",
        "comparator": ROOT / "scripts" / "v2_core_legacy_role_alignment_compare_v5.py",
        "operational_policy": artifact_dir / "operational_budget_override_v14.json"
    }
    for name, path in files.items():
        if not path.is_file():
            raise FileNotFoundError(f"missing {name}: {path}")
    input_manifest = load_json(files["input_manifest"])
    if input_manifest["case_count"] != 14 or input_manifest["required_rounds"] != 1:
        raise ValueError("unexpected input matrix")
    fit = load_json(files["contract_fit_audit"])
    if fit["summary"]["teacher_profile_represented_count"] != 14:
        raise ValueError("R5 contract does not cover all calibration teachers")
    policy = load_json(files["operational_policy"])
    stop = float(policy["stop_new_ai_calls_when_remaining_percent_lte"])
    if stop != 5.0:
        raise ValueError("operational policy is not the current 5% stop rule")
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "READY_FOR_FORMAL_AI_ONE_PASS（可執行正式AI單輪對齊）",
        "milestone": "MILESTONE_2A／ONE_PASS_LEGACY_ALIGNMENT",
        "subgate": "LIFECYCLE_FIRST_FOUR_ROLE_ALIGNMENT_R5",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "required_rounds": 1,
        "expected_case_rounds": input_manifest["case_count"],
        "stop_remaining_percent_lte": stop,
        "input_packet_directory": input_manifest["input_packet_directory"],
        "run_directory": "legacy_role_alignment_runs_candidate_r5",
        "teacher_labels_used_for_offline_contract_distillation": True,
        "legacy_answers_available_to_ai": False,
        "legacy_answers_revealed_only_after_all_outputs_frozen": True,
        "future_performance_used": False,
        "identity_used": False,
        "locked_reproduction_set_opened": False,
        "trade_permission_granted": False,
        "prior_ai_outputs_reused": False,
        "comparison_role": "SCENARIO_WORKING_ANCHOR_ONLY",
        "rows": input_manifest["rows"]
    }
    script_keys = {"runner", "validator", "comparator"}
    for key, path in files.items():
        manifest[f"{key}_file"] = str(path.relative_to(ROOT)).replace("\\", "/") if key in script_keys else path.name
        manifest[f"{key}_sha256"] = sha256_path(path)
    output_path = artifact_dir / OUTPUT_FILE
    payload = canonical_bytes(manifest)
    if output_path.exists() and output_path.read_bytes() != payload:
        raise FileExistsError(f"refusing to overwrite non-identical manifest: {output_path}")
    output_path.write_bytes(payload)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    manifest = build_manifest(args.artifact_dir)
    print(json.dumps({"status": manifest["status"], "expected_case_rounds": manifest["expected_case_rounds"], "stop_remaining_percent_lte": manifest["stop_remaining_percent_lte"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
