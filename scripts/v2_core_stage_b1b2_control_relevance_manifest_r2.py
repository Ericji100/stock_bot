"""Freeze the evidence-complete B1b2 R2 execution manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR, canonical_bytes, sha256_path, write_new_or_identical
from scripts.v2_core_stage_b1b2_control_relevance_input_r2 import generate_all


OUTPUT = "stage_b1b2_control_relevance_execution_manifest_candidate_r2.json"


def generate(artifact_dir: Path = ARTIFACT_DIR) -> dict:
    source = generate_all(artifact_dir)
    files = {
        "input_manifest_file": "stage_b1b2_control_relevance_input_manifest_candidate_r2.json",
        "prompt_file": "v2_core_stage_b1b2_control_relevance.prompt.candidate_r2.md",
        "schema_file": "v2_core_stage_b1b2_control_relevance.schema.candidate_r1.json",
        "design_file": "B1B2_CURRENT_CONTROL_RELEVANCE_PROBE_R2.md",
        "operational_policy_file": "operational_budget_override_v13.json",
        "runner_file": "scripts/v2_core_stage_b1b2_control_relevance_runner_v1.py",
        "validator_file": "scripts/v2_core_stage_b1b2_control_relevance_validator_v1.py",
        "comparator_file": "scripts/v2_core_stage_b1b2_control_relevance_compare_v1.py",
    }
    manifest = {
        "manifest_version": "v2-core-stage-b1b2-control-relevance-calibration-r2-candidate",
        "status": "READY_FOR_FORMAL_AI_CALIBRATION（可執行正式AI校準）",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "case_count": source["case_count"],
        "candidate_count": source["candidate_count"],
        "required_rounds": 3,
        "expected_case_rounds": source["case_count"] * 3,
        "stop_remaining_percent_lte": 60,
        "run_directory": "stage_b1b2_control_relevance_runs_candidate_r2",
        "input_packet_directory": source["input_packet_directory"],
        "single_atom_only": "current_control_relevance",
        "all_fixed_path_segment_snapshots_present": True,
        "upstream_parent_source_consensus_used": True,
        "upstream_parent_source_answers_exposed": False,
        "unique_controlling_anchor_selected": False,
        "trade_permission_granted": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "prior_ai_control_answers_used": False,
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
