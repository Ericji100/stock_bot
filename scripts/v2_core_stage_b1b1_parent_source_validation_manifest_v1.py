"""Freeze the fresh B1b1 parent-source heldout execution manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR, ROOT, canonical_bytes, sha256_path, write_new_or_identical
from scripts.v2_core_stage_b1b1_parent_source_validation_input_v1 import generate_all


OUTPUT = "stage_b1b1_parent_source_validation_execution_manifest_r1.json"


def generate(artifact_dir: Path = ARTIFACT_DIR) -> dict:
    source = generate_all(artifact_dir)
    files = {
        "input_manifest_file": "stage_b1b1_parent_source_validation_input_manifest_r1.json",
        "prompt_file": "v2_core_stage_b1b1_parent_source.prompt.candidate_r1.md",
        "schema_file": "v2_core_stage_b1b1_parent_source.schema.candidate_r1.json",
        "design_file": "B1B1_PARENT_SOURCE_HELDOUT_R1.md",
        "operational_policy_file": "operational_budget_override_v13.json",
        "runner_file": "scripts/v2_core_stage_b1b1_parent_source_runner_v1.py",
        "validator_file": "scripts/v2_core_stage_b1b1_parent_source_validator_v1.py",
        "comparator_file": "scripts/v2_core_stage_b1b1_parent_source_compare_v1.py",
    }
    manifest = {
        "manifest_version": "v2-core-stage-b1b1-parent-source-heldout-r1",
        "status": "READY_FOR_FORMAL_AI_HELDOUT_VALIDATION（可執行正式AI留出驗證）",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "case_count": source["case_count"],
        "candidate_count": source["candidate_count"],
        "required_rounds": 3,
        "expected_case_rounds": source["case_count"] * 3,
        "stop_remaining_percent_lte": 60,
        "run_directory": "stage_b1b1_parent_source_validation_runs_r1",
        "input_packet_directory": source["input_packet_directory"],
        "calibration_cases_excluded": True,
        "single_atom_only": "parent_source_relation",
        "program_derives_eligibility": True,
        "final_course_role_assigned": False,
        "controlling_anchor_decided": False,
        "trade_permission_granted": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "prior_ai_role_answers_used": False,
        **files,
        **{
            key.replace("_file", "_sha256"): sha256_path(
                ROOT / value if value.startswith("scripts/") else artifact_dir / value
            )
            for key, value in files.items()
        },
        "rows": source["rows"],
    }
    write_new_or_identical(artifact_dir / OUTPUT, canonical_bytes(manifest))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    print(json.dumps(generate(args.artifact_dir), ensure_ascii=False, indent=2))
