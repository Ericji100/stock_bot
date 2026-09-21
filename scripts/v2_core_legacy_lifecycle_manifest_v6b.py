"""Freeze the R6B bootstrap-only legacy lifecycle execution manifest."""

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
MANIFEST_VERSION = "v2-core-legacy-lifecycle-execution-r6b-candidate"
OUTPUT_FILE = "legacy_lifecycle_execution_manifest_candidate_r6b.json"
EXPECTED_R6_SHA256 = "1303c73717a82e844af20dc341a4e4e3d15640fdf639feed2215c751d96a3210"
EXPECTED_R6A_SHA256 = "b1452efd30c6c782fb5e9fdd53e699f2a63290c122cb33abb2df00e1bb634d79"


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
        "prior_r6_execution_manifest": artifact_dir
        / "legacy_lifecycle_execution_manifest_candidate_r6.json",
        "prior_r6_failure_analysis": artifact_dir
        / "FAILURE_ANALYSIS_legacy_lifecycle_r6_b0a_transport.md",
        "prior_r6a_execution_manifest": artifact_dir
        / "legacy_lifecycle_execution_manifest_candidate_r6a.json",
        "prior_r6a_failure_analysis": artifact_dir
        / "FAILURE_ANALYSIS_legacy_lifecycle_r6a_bootstrap.md",
        "recovery_design": artifact_dir / "LEGACY_ROLE_ALIGNMENT_RECOVERY_R6.md",
        "teacher_distillation_design": artifact_dir / "LEGACY_TEACHER_DISTILLATION_R5.md",
        "teacher_pairwise_audit": artifact_dir
        / "legacy_teacher_pairwise_audit_candidate_r1.json",
        "selection": artifact_dir / "legacy_anchor_alignment_selection_candidate_r1.json",
        "input_manifest": artifact_dir
        / "legacy_anchor_alignment_input_manifest_candidate_r1.json",
        "b0a_prompt": artifact_dir
        / "v2_core_legacy_lifecycle_b0a.prompt.candidate_r6.md",
        "b0a_schema": artifact_dir
        / "v2_core_legacy_lifecycle_b0a.schema.candidate_r6.json",
        "lifecycle_truth_table": artifact_dir
        / "legacy_lifecycle_route_truth_table.candidate_r6.json",
        "b0b_prompt": artifact_dir / "v2_core_legacy_roles_b0b.prompt.candidate_r6.md",
        "b0b_schema": artifact_dir / "v2_core_legacy_roles_b0b.schema.candidate_r6.json",
        "role_truth_table": artifact_dir
        / "legacy_scenario_role_truth_table.candidate_r6.json",
        "runner": ROOT / "scripts" / "v2_core_legacy_lifecycle_runner_v6b.py",
        "transport_runner": ROOT / "scripts" / "v2_core_legacy_lifecycle_runner_v6a.py",
        "base_runner": ROOT / "scripts" / "v2_core_legacy_lifecycle_runner_v6.py",
        "b0a_validator": ROOT
        / "scripts"
        / "v2_core_legacy_lifecycle_b0a_validator_v6.py",
        "b0b_validator": ROOT / "scripts" / "v2_core_legacy_roles_b0b_validator_v6.py",
        "operational_policy": artifact_dir / "operational_budget_override_v14.json",
    }
    for name, path in files.items():
        if not path.is_file():
            raise FileNotFoundError(f"missing {name}: {path}")

    r6 = load_json(files["prior_r6_execution_manifest"])
    r6a = load_json(files["prior_r6a_execution_manifest"])
    if sha256_path(files["prior_r6_execution_manifest"]) != EXPECTED_R6_SHA256:
        raise ValueError("prior R6 execution manifest hash changed")
    if sha256_path(files["prior_r6a_execution_manifest"]) != EXPECTED_R6A_SHA256:
        raise ValueError("prior R6A execution manifest hash changed")
    if r6a.get("transport_only_revision") is not True:
        raise ValueError("R6A transport revision contract missing")
    if sha256_path(files["transport_runner"]) != r6a.get("runner_sha256"):
        raise ValueError("R6A transport runner changed")
    if sha256_path(files["base_runner"]) != r6.get("runner_sha256"):
        raise ValueError("R6 base runner changed")

    unchanged_keys = (
        "recovery_design",
        "teacher_distillation_design",
        "teacher_pairwise_audit",
        "selection",
        "input_manifest",
        "b0a_prompt",
        "b0a_schema",
        "lifecycle_truth_table",
        "b0b_prompt",
        "b0b_schema",
        "role_truth_table",
        "b0a_validator",
        "b0b_validator",
        "operational_policy",
    )
    for key in unchanged_keys:
        actual = sha256_path(files[key])
        if actual != r6.get(f"{key}_sha256") or actual != r6a.get(f"{key}_sha256"):
            raise ValueError(f"R6 semantic or policy artifact changed: {key}")

    input_manifest = load_json(files["input_manifest"])
    if input_manifest.get("case_count") != 14 or input_manifest.get("required_rounds") != 1:
        raise ValueError("unexpected one-pass input matrix")
    if input_manifest.get("formal_model") != "gpt-5.6-sol" or input_manifest.get(
        "reasoning_effort"
    ) != "xhigh":
        raise ValueError("input manifest model/effort mismatch")
    policy = load_json(files["operational_policy"])
    stop = float(policy["stop_new_ai_calls_when_remaining_percent_lte"])
    if stop != 5.0:
        raise ValueError("operational policy is not the current 5% stop rule")

    rows = sorted(input_manifest["rows"], key=lambda row: str(row["review_id"]))
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "status": "READY_FOR_SINGLE_CASE_PROTOCOL_PROBE（可執行單案協定探針）",
        "milestone": "MILESTONE_2A／ONE_PASS_LEGACY_ALIGNMENT",
        "subgate": "R6B_BOOTSTRAP_ONLY_REVISION",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "required_rounds": 1,
        "expected_case_rounds": len(rows),
        "protocol_probe_review_id": rows[0]["review_id"],
        "stop_remaining_percent_lte": stop,
        "input_packet_directory": input_manifest["input_packet_directory"],
        "run_directory": "legacy_lifecycle_runs_candidate_r6b",
        "prior_r6_status": "INVALIDATED_PRE_INFERENCE_TRANSPORT",
        "prior_r6_semantic_output_count": 0,
        "prior_r6a_status": "INVALIDATED_PRE_EXECUTION_BOOTSTRAP",
        "prior_r6a_api_call_count": 0,
        "prior_r6a_semantic_output_count": 0,
        "bootstrap_only_revision_from_r6a": True,
        "transport_adapter_changed_from_r6a": False,
        "semantic_prompt_changed_from_r6": False,
        "local_schema_changed_from_r6": False,
        "truth_tables_changed_from_r6": False,
        "input_matrix_changed_from_r6": False,
        "teacher_labels_used_for_offline_contract_distillation": True,
        "legacy_answers_available_to_ai": False,
        "legacy_answers_revealed_only_after_all_outputs_frozen": True,
        "future_performance_used": False,
        "identity_used": False,
        "locked_reproduction_set_opened": False,
        "trade_permission_granted": False,
        "prior_r5_invalid_output_reused": False,
        "prior_r6_failed_call_reused": False,
        "prior_r6a_failed_call_reused": False,
        "b0a_ai_outputs_scenario": False,
        "b0b_ai_revotes_scenario": False,
        "separate_usage_attestation_required_before_each_stage": True,
        "rows": rows,
    }
    script_keys = {
        "runner",
        "transport_runner",
        "base_runner",
        "b0a_validator",
        "b0b_validator",
    }
    for key, path in files.items():
        manifest[f"{key}_file"] = (
            str(path.relative_to(ROOT)).replace("\\", "/")
            if key in script_keys
            else path.name
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
                "bootstrap_only_revision_from_r6a": manifest[
                    "bootstrap_only_revision_from_r6a"
                ],
                "stop_remaining_percent_lte": manifest["stop_remaining_percent_lte"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
