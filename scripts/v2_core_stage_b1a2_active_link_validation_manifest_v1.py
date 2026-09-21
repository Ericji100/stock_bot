"""Freeze the B1a2 active-link held-out execution manifest."""

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
from scripts.v2_core_stage_b1a2_active_link_validation_input_v1 import generate_all


OUTPUT_FILE = "stage_b1a2_active_link_validation_execution_manifest_r1.json"
OUTPUT_MD = "stage_b1a2_active_link_validation_execution_manifest_r1.md"
INPUT_MANIFEST = "stage_b1a2_active_link_validation_input_manifest_r1.json"
PROMPT_FILE = "v2_core_stage_b1a2_active_link.prompt.candidate_r1.md"
SCHEMA_FILE = "v2_core_stage_b1a2_active_link.schema.candidate_r1.json"
DESIGN_FILE = "B1A2_ACTIVE_CAMPAIGN_LINK_HELDOUT_VALIDATION_R1.md"
POLICY_FILE = "operational_budget_override_v12.json"
RUNNER_FILE = "scripts/v2_core_stage_b1a2_active_link_runner_v1.py"
VALIDATOR_FILE = "scripts/v2_core_stage_b1a2_active_link_validator_v1.py"
COMPARATOR_FILE = "scripts/v2_core_stage_b1a2_active_link_compare_v1.py"
RUN_DIRECTORY = "stage_b1a2_active_link_validation_runs_r1"


def generate(artifact_dir: Path = ARTIFACT_DIR) -> dict:
    source = generate_all(artifact_dir)
    if source["case_count"] != 4 or source["candidate_count"] < 1:
        raise ValueError("unexpected B1a2 held-out input manifest shape")
    bindings = {
        "input_manifest_sha256": sha256_path(artifact_dir / INPUT_MANIFEST),
        "prompt_sha256": sha256_path(artifact_dir / PROMPT_FILE),
        "schema_sha256": sha256_path(artifact_dir / SCHEMA_FILE),
        "design_sha256": sha256_path(artifact_dir / DESIGN_FILE),
        "policy_sha256": sha256_path(artifact_dir / POLICY_FILE),
        "runner_sha256": sha256_path(ROOT / RUNNER_FILE),
        "validator_sha256": sha256_path(ROOT / VALIDATOR_FILE),
        "comparator_sha256": sha256_path(ROOT / COMPARATOR_FILE),
    }
    manifest = {
        "manifest_version": "v2-core-stage-b1a2-active-link-heldout-execution-r1",
        "status": "READY_FOR_FORMAL_AI_HELDOUT_VALIDATION（可執行正式AI留出驗證）",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "case_count": source["case_count"],
        "required_rounds": 3,
        "expected_case_rounds": source["case_count"] * 3,
        "stop_remaining_percent_lte": 3.0,
        "input_manifest_file": INPUT_MANIFEST,
        "input_packet_directory": source["input_packet_directory"],
        "prompt_file": PROMPT_FILE,
        "schema_file": SCHEMA_FILE,
        "design_file": DESIGN_FILE,
        "policy_file": POLICY_FILE,
        "runner_file": RUNNER_FILE,
        "validator_file": VALIDATOR_FILE,
        "comparator_file": COMPARATOR_FILE,
        "run_directory": RUN_DIRECTORY,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "prior_role_answers_used": False,
        "calibration_b1a2_answers_used": False,
        "course_role_generated_by_program": False,
        "bindings": bindings,
        "rows": source["rows"],
    }
    write_new_or_identical(artifact_dir / OUTPUT_FILE, canonical_bytes(manifest))
    lines = [
        "# Stage B1a2 主動Campaign連結留出驗證執行清單 R1",
        "",
        f"- 狀態：`{manifest['status']}`",
        "- 正式模型：`gpt-5.6-sol／xhigh`",
        f"- 案例：{manifest['case_count']}；三輪共{manifest['expected_case_rounds']}次。",
        "- 案例來自R4第二批留出集；未使用B1a2校準答案。",
        "- prompt、schema、runner、validator與門檻沿用校準版，不做調整。",
        "- 每次呼叫前必須有新鮮官方額度證明；剩餘≤3%不得啟動新呼叫。",
        "",
        "## 凍結雜湊",
        "",
    ]
    lines.extend(f"- `{key}`：`{value}`" for key, value in bindings.items())
    write_new_or_identical(
        artifact_dir / OUTPUT_MD, ("\n".join(lines) + "\n").encode("utf-8")
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    print(json.dumps(generate(args.artifact_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
