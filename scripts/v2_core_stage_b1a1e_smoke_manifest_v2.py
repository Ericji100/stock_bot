"""Build immutable execution manifest for Stage B1a1E focused smoke R2."""

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
from scripts.v2_core_stage_b1a1e_focus_input_v2 import generate_all


OUTPUT_FILE = "stage_b1a1e_smoke_execution_manifest_candidate_r2.json"
OUTPUT_MD = "stage_b1a1e_smoke_execution_manifest_candidate_r2.md"


def build_manifest(artifact_dir: Path = ARTIFACT_DIR) -> dict:
    input_manifest = generate_all(artifact_dir)
    bindings = {
        "input_manifest_file": "stage_b1a1e_input_manifest_candidate_r2.json",
        "prompt_file": "v2_core_stage_b1a1e.prompt.candidate_r2.md",
        "schema_file": "v2_core_stage_b1a1e.schema.candidate_r2.json",
        "runner_file": "scripts/v2_core_stage_b1a1e_codex_runner_v1.py",
        "validator_file": "scripts/v2_core_stage_b1a1e_candidate_validator_v1.py",
        "comparator_file": "scripts/v2_core_stage_b1a1e_smoke_compare_v1.py",
        "gate_file": "B1A1E_SMOKE_GATE_R2.md",
        "operational_policy_file": "operational_budget_override_v10.json",
    }
    manifest = {
        "manifest_version": "v2-core-stage-b1a1e-smoke-execution-r2-candidate",
        "status": "READY_FOR_FORMAL_AI_SMOKE（可執行正式AI Smoke）",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "case_count": input_manifest["case_count"],
        "required_rounds": 3,
        "expected_case_rounds": input_manifest["case_count"] * 3,
        "stop_remaining_percent_lte": 25,
        "run_directory": "stage_b1a1e_smoke_runs_candidate_r2",
        "input_packet_directory": input_manifest["input_packet_directory"],
        "selection_uses_prior_calibration_outputs": True,
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
                "selected_candidate_count": row["selected_candidate_count"],
                "evidence_option_count": row["evidence_option_count"],
            }
            for row in input_manifest["rows"]
        ],
    }
    write_new_or_identical(artifact_dir / OUTPUT_FILE, canonical_bytes(manifest))
    write_new_or_identical(
        artifact_dir / OUTPUT_MD,
        (
            "# Stage B1a1E Smoke執行清單 R2\n\n"
            f"- 狀態：`{manifest['status']}`\n"
            f"- 模型：`{manifest['formal_model']}／{manifest['reasoning_effort']}`\n"
            f"- 案例輪次：{manifest['expected_case_rounds']}\n"
            f"- 停止線：剩餘≤{manifest['stop_remaining_percent_lte']}%。\n"
            "- 未使用股票身分、sealed標籤、舊答案或未來績效。\n"
        ).encode("utf-8"),
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    return parser.parse_args()


def main() -> int:
    print(json.dumps(build_manifest(parse_args().artifact_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
