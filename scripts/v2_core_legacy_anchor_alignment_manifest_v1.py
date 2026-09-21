"""Freeze the execution manifest for one-pass legacy anchor alignment."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)
MANIFEST_VERSION = "v2-core-legacy-anchor-alignment-execution-r1-candidate"
OUTPUT_FILE = "legacy_anchor_alignment_execution_manifest_candidate_r1.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def build_manifest(artifact_dir: Path) -> dict[str, Any]:
    files = {
        "design": artifact_dir / "LEGACY_ANCHOR_ALIGNMENT_SUBGATE_R1.md",
        "selection": artifact_dir / "legacy_anchor_alignment_selection_candidate_r1.json",
        "input_manifest": artifact_dir / "legacy_anchor_alignment_input_manifest_candidate_r1.json",
        "prompt": artifact_dir / "v2_core_legacy_anchor_alignment.prompt.candidate_r1.md",
        "schema": artifact_dir / "v2_core_legacy_anchor_alignment.schema.candidate_r1.json",
        "runner": ROOT / "scripts" / "v2_core_legacy_anchor_alignment_runner_v1.py",
        "validator": ROOT / "scripts" / "v2_core_legacy_anchor_alignment_validator_v1.py",
        "comparator": ROOT / "scripts" / "v2_core_legacy_anchor_alignment_compare_v1.py",
        "operational_policy": artifact_dir / "operational_budget_override_v13.json",
    }
    for name, path in files.items():
        if not path.is_file():
            raise FileNotFoundError(f"missing {name}: {path}")
    input_manifest = load_json(files["input_manifest"])
    if input_manifest["case_count"] != 14 or input_manifest["required_rounds"] != 1:
        raise ValueError("unexpected input matrix")
    policy = load_json(files["operational_policy"])
    stop = float(policy["stop_new_ai_calls_when_remaining_percent_lte"])
    if stop != 60.0:
        raise ValueError("operational policy is not the latest 60% stop rule")
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "READY_FOR_FORMAL_AI_ONE_PASS（可執行正式AI單輪對齊）",
        "milestone": "MILESTONE_2A／ONE_PASS_LEGACY_ALIGNMENT",
        "subgate": "CONTROLLING_ANCHOR_ALIGNMENT_B0",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "required_rounds": 1,
        "expected_case_rounds": input_manifest["case_count"],
        "stop_remaining_percent_lte": stop,
        "input_packet_directory": input_manifest["input_packet_directory"],
        "run_directory": "legacy_anchor_alignment_runs_candidate_r1",
        "legacy_answers_available_to_ai": False,
        "legacy_answers_revealed_only_after_all_outputs_frozen": True,
        "future_performance_used": False,
        "identity_used": False,
        "locked_reproduction_set_opened": False,
        "trade_permission_granted": False,
        "design_file": files["design"].name,
        "design_sha256": sha256_path(files["design"]),
        "selection_file": files["selection"].name,
        "selection_sha256": sha256_path(files["selection"]),
        "input_manifest_file": files["input_manifest"].name,
        "input_manifest_sha256": sha256_path(files["input_manifest"]),
        "prompt_file": files["prompt"].name,
        "prompt_sha256": sha256_path(files["prompt"]),
        "schema_file": files["schema"].name,
        "schema_sha256": sha256_path(files["schema"]),
        "runner_file": str(files["runner"].relative_to(ROOT)).replace("\\", "/"),
        "runner_sha256": sha256_path(files["runner"]),
        "validator_file": str(files["validator"].relative_to(ROOT)).replace("\\", "/"),
        "validator_sha256": sha256_path(files["validator"]),
        "comparator_file": str(files["comparator"].relative_to(ROOT)).replace("\\", "/"),
        "comparator_sha256": sha256_path(files["comparator"]),
        "operational_policy_file": files["operational_policy"].name,
        "operational_policy_sha256": sha256_path(files["operational_policy"]),
        "rows": input_manifest["rows"],
    }
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
                "stop_remaining_percent_lte": manifest["stop_remaining_percent_lte"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
