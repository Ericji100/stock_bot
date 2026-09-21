"""Build the immutable execution manifest for the R4 held-out validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import (
    ARTIFACT_DIR,
    canonical_bytes,
    sha256_path,
    write_new_or_identical,
)
from scripts.v2_core_stage_b1a1e_primary_gate_validation_input_v1 import generate_all


OUTPUT_FILE = "stage_b1a1e_primary_gate_validation_execution_manifest_r1.json"
OUTPUT_MD = "stage_b1a1e_primary_gate_validation_execution_manifest_r1.md"


def build_manifest(artifact_dir: Path = ARTIFACT_DIR) -> dict:
    input_manifest = generate_all(artifact_dir)
    bindings = {
        "input_manifest_file": "stage_b1a1e_primary_gate_validation_input_manifest_r1.json",
        "prompt_file": "v2_core_stage_b1a1e_primary_gate.prompt.candidate_r4.md",
        "schema_file": "v2_core_stage_b1a1e_primary_gate.schema.candidate_r4.json",
        "runner_file": "scripts/v2_core_stage_b1a1e_primary_gate_runner_v1.py",
        "validator_file": "scripts/v2_core_stage_b1a1e_primary_gate_validator_v1.py",
        "comparator_file": "scripts/v2_core_stage_b1a1e_primary_gate_compare_v1.py",
        "gate_file": "B1A1E_PRIMARY_GATE_HELDOUT_VALIDATION_R1.md",
        "operational_policy_file": "operational_budget_override_v12.json",
    }
    manifest = {
        "manifest_version": "v2-core-stage-b1a1e-primary-gate-heldout-validation-r1",
        "status": "READY_FOR_FORMAL_AI_HELDOUT_VALIDATION（可執行正式AI留出驗證）",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "case_count": input_manifest["case_count"],
        "required_rounds": 3,
        "expected_case_rounds": input_manifest["case_count"] * 3,
        "stop_remaining_percent_lte": 3,
        "run_directory": "stage_b1a1e_primary_gate_validation_runs_r1",
        "input_packet_directory": input_manifest["input_packet_directory"],
        "program_gate_directory": input_manifest["program_gate_directory"],
        "program_gate_hidden_from_ai": True,
        "candidate_scales": ["LARGE", "SMALL"],
        "auxiliary_standalone_candidate_forbidden": True,
        "prior_review_ids_excluded": True,
        "prior_answers_present_in_ai_packets": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        **bindings,
        **{
            key.replace("_file", "_sha256"): sha256_path(
                ROOT / value if value.startswith("scripts/") else artifact_dir / value
            )
            for key, value in bindings.items()
        },
        "rows": [
            {
                "review_id": row["review_id"],
                "as_of": row["as_of"],
                "input_packet_file": row["input_packet_file"],
                "input_packet_sha256": row["input_packet_sha256"],
                "program_gate_file": row["program_gate_file"],
                "program_gate_sha256": row["program_gate_sha256"],
                "selected_candidate_count": row["selected_candidate_count"],
                "directional_evidence_option_count": row["directional_evidence_option_count"],
                "objective_contact_candidate_count": row["objective_contact_candidate_count"],
            }
            for row in input_manifest["rows"]
        ],
    }
    write_new_or_identical(artifact_dir / OUTPUT_FILE, canonical_bytes(manifest))
    write_new_or_identical(
        artifact_dir / OUTPUT_MD,
        (
            "# Stage B1a1E主候選客觀Gate R4留出驗證執行清單\n\n"
            f"- 狀態：`{manifest['status']}`\n"
            f"- 模型：`{manifest['formal_model']}／{manifest['reasoning_effort']}`\n"
            f"- 全新留出案例輪次：{manifest['expected_case_rounds']}\n"
            "- R4 prompt／schema／runner／validator不變。\n"
            "- AI只看方向封包；程式Gate sidecar不送入prompt。\n"
            f"- 停止線：官方主要額度剩餘≤{manifest['stop_remaining_percent_lte']}%。\n"
            "- 未使用舊答案、未來績效、股票身分或sealed標籤。\n"
        ).encode("utf-8"),
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    print(json.dumps(build_manifest(args.artifact_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
