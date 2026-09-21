"""Freeze the R6 two-stage single-pass legacy alignment execution manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)
MANIFEST_VERSION = "v2-core-legacy-lifecycle-execution-r6-candidate"
OUTPUT_FILE = "legacy_lifecycle_execution_manifest_candidate_r6.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def build_manifest(artifact_dir: Path) -> dict[str, Any]:
    files = {
        "recovery_design": artifact_dir / "LEGACY_ROLE_ALIGNMENT_RECOVERY_R6.md",
        "teacher_distillation_design": artifact_dir / "LEGACY_TEACHER_DISTILLATION_R5.md",
        "teacher_pairwise_audit": artifact_dir / "legacy_teacher_pairwise_audit_candidate_r1.json",
        "selection": artifact_dir / "legacy_anchor_alignment_selection_candidate_r1.json",
        "input_manifest": artifact_dir / "legacy_anchor_alignment_input_manifest_candidate_r1.json",
        "b0a_prompt": artifact_dir / "v2_core_legacy_lifecycle_b0a.prompt.candidate_r6.md",
        "b0a_schema": artifact_dir / "v2_core_legacy_lifecycle_b0a.schema.candidate_r6.json",
        "lifecycle_truth_table": artifact_dir / "legacy_lifecycle_route_truth_table.candidate_r6.json",
        "b0b_prompt": artifact_dir / "v2_core_legacy_roles_b0b.prompt.candidate_r6.md",
        "b0b_schema": artifact_dir / "v2_core_legacy_roles_b0b.schema.candidate_r6.json",
        "role_truth_table": artifact_dir / "legacy_scenario_role_truth_table.candidate_r6.json",
        "runner": ROOT / "scripts" / "v2_core_legacy_lifecycle_runner_v6.py",
        "b0a_validator": ROOT / "scripts" / "v2_core_legacy_lifecycle_b0a_validator_v6.py",
        "b0b_validator": ROOT / "scripts" / "v2_core_legacy_roles_b0b_validator_v6.py",
        "operational_policy": artifact_dir / "operational_budget_override_v14.json",
    }
    for name, path in files.items():
        if not path.is_file():
            raise FileNotFoundError(f"missing {name}: {path}")

    input_manifest = load_json(files["input_manifest"])
    if input_manifest.get("case_count") != 14 or input_manifest.get("required_rounds") != 1:
        raise ValueError("unexpected one-pass input matrix")
    if input_manifest.get("formal_model") != "gpt-5.6-sol" or input_manifest.get(
        "reasoning_effort"
    ) != "xhigh":
        raise ValueError("input manifest model/effort mismatch")
    lifecycle_truth = load_json(files["lifecycle_truth_table"])
    role_truth = load_json(files["role_truth_table"])
    if lifecycle_truth.get("no_trade_permission_at_this_stage") is not True:
        raise ValueError("B0A truth table may grant trade permission")
    if role_truth.get("no_trade_permission_at_this_stage") is not True:
        raise ValueError("B0B truth table may grant trade permission")
    policy = load_json(files["operational_policy"])
    stop = float(policy["stop_new_ai_calls_when_remaining_percent_lte"])
    if stop != 5.0:
        raise ValueError("operational policy is not the current 5% stop rule")

    rows = sorted(input_manifest["rows"], key=lambda row: str(row["review_id"]))
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "status": "READY_FOR_SINGLE_CASE_PROTOCOL_PROBE（可執行單案協定探針）",
        "milestone": "MILESTONE_2A／ONE_PASS_LEGACY_ALIGNMENT",
        "subgate": "DECOUPLED_LIFECYCLE_AND_SCENARIO_BOUND_ROLES_R6",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "required_rounds": 1,
        "expected_case_rounds": len(rows),
        "protocol_probe_review_id": rows[0]["review_id"],
        "stop_remaining_percent_lte": stop,
        "input_packet_directory": input_manifest["input_packet_directory"],
        "run_directory": "legacy_lifecycle_runs_candidate_r6",
        "teacher_labels_used_for_offline_contract_distillation": True,
        "legacy_answers_available_to_ai": False,
        "legacy_answers_revealed_only_after_all_outputs_frozen": True,
        "future_performance_used": False,
        "identity_used": False,
        "locked_reproduction_set_opened": False,
        "trade_permission_granted": False,
        "prior_r5_invalid_output_reused": False,
        "b0a_ai_outputs_scenario": False,
        "b0b_ai_revotes_scenario": False,
        "separate_usage_attestation_required_before_each_stage": True,
        "rows": rows,
    }
    script_keys = {"runner", "b0a_validator", "b0b_validator"}
    for key, path in files.items():
        manifest[f"{key}_file"] = (
            str(path.relative_to(ROOT)).replace("\\", "/") if key in script_keys else path.name
        )
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
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "expected_case_rounds": manifest["expected_case_rounds"],
                "protocol_probe_review_id": manifest["protocol_probe_review_id"],
                "stop_remaining_percent_lte": manifest["stop_remaining_percent_lte"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
