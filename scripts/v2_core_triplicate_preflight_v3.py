"""Freeze v8 after the final-normalized-receipt integration fix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import v2_core_triplicate_preflight_v2 as previous
from scripts.v2_core_source_audit_v1 import sha256


OUT = previous.OUT
VERSION = "v2-core-m2a-triplicate-execution-r8-candidate"
MANIFEST_FILENAME = "feasibility_probe_execution_manifest_v8.json"
RUN_DIRECTORY = "feasibility_probe_runs_r8"
STAGE_RUNNERS = {
    "STAGE_B": "scripts/v2_core_codex_staged_runner_v1.py",
    "STAGE_D": "scripts/v2_core_codex_stage_d_runner_v3.py",
}
REPO_COMPONENT_FILES = (
    "scripts/v2_core_codex_staged_runner_v1.py",
    "scripts/v2_core_codex_stage_d_runner_v2.py",
    "scripts/v2_core_codex_stage_d_runner_v3.py",
    "scripts/v2_core_staged_output_validator_v1.py",
    "scripts/v2_core_m2a_contracts_v1.py",
    "scripts/v2_core_source_audit_v1.py",
)


def build(artifact_dir: Path) -> dict[str, Any]:
    manifest = previous.build(artifact_dir)
    manifest["execution_manifest_version"] = VERSION
    manifest["run_directory"] = RUN_DIRECTORY
    manifest["stage_runners"] = STAGE_RUNNERS
    manifest["repo_components"] = {
        filename: sha256(previous.ROOT / filename) for filename in REPO_COMPONENT_FILES
    }
    common = {
        "model": manifest["formal_model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "probe_manifest_sha256": manifest["probe_manifest_sha256"],
        "components": manifest["components"],
        "repo_components": manifest["repo_components"],
        "stage_runners": STAGE_RUNNERS,
        "stage_order": ["STAGE_B", "PROGRAM_ROUTE", "STAGE_D", "PROGRAM_PERMISSION"],
        "selection_labels_visible": False,
        "future_performance_visible": False,
    }
    cases = []
    for row in manifest["cases"]:
        fingerprint = {
            **common,
            "review_id": row["review_id"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "as_of": row["as_of"],
            "packet_sha256": row["packet_sha256"],
        }
        cases.append({**row, "frozen_input_fingerprint": previous.canonical_sha256(fingerprint)})
    cases.sort(key=lambda item: str(item["review_id"]))
    manifest["cases"] = cases
    shards = []
    for shard_id, offset in enumerate(range(0, len(cases), previous.SHARD_SIZE), 1):
        rows = cases[offset : offset + previous.SHARD_SIZE]
        shards.append(
            {
                "shard_id": shard_id,
                "case_count": len(rows),
                "review_ids": [row["review_id"] for row in rows],
                "shard_input_sha256": previous.canonical_sha256(rows),
            }
        )
    manifest["shards"] = shards
    matrix_sha = previous.canonical_sha256(cases)
    manifest["cross_round_input_matrix_sha256"] = matrix_sha
    manifest["rounds"] = [
        {
            "round": number,
            "status": "NOT_STARTED（尚未開始）",
            "input_matrix_sha256": matrix_sha,
            "stage_b_output_dir": f"{RUN_DIRECTORY}/round_{number}/stage_b",
            "program_route_dir": f"{RUN_DIRECTORY}/round_{number}/program_route",
            "stage_d_output_dir": f"{RUN_DIRECTORY}/round_{number}/stage_d",
            "final_permission_dir": f"{RUN_DIRECTORY}/round_{number}/final_permission",
        }
        for number in previous.ROUNDS
    ]
    return manifest


def validate(manifest: dict[str, Any], artifact_dir: Path) -> list[str]:
    errors: list[str] = []
    if manifest.get("execution_manifest_version") != VERSION:
        errors.append("EXECUTION_VERSION_MISMATCH")
    if manifest.get("run_directory") != RUN_DIRECTORY:
        errors.append("RUN_DIRECTORY_MISMATCH")
    if manifest.get("stage_runners") != STAGE_RUNNERS:
        errors.append("STAGE_RUNNER_MAP_MISMATCH")
    if manifest.get("formal_model") != "gpt-5.6-sol" or manifest.get("reasoning_effort") != "xhigh":
        errors.append("MODEL_OR_EFFORT_MISMATCH")
    if manifest.get("budget_stop_remaining_percent_lte") != 5:
        errors.append("BUDGET_THRESHOLD_MISMATCH")
    cases = manifest.get("cases") or []
    if len(cases) != 48 or len({row["review_id"] for row in cases}) != 48:
        errors.append("CASE_MATRIX_NOT_48_UNIQUE_CASES")
    if len(manifest.get("shards") or []) != 12:
        errors.append("SHARD_COUNT_MISMATCH")
    matrix_hashes = {row["input_matrix_sha256"] for row in manifest.get("rounds") or []}
    if matrix_hashes != {manifest.get("cross_round_input_matrix_sha256")}:
        errors.append("ROUND_INPUT_MATRIX_HASHES_DIFFER")
    probe = json.loads((artifact_dir / "feasibility_probe_manifest.json").read_text(encoding="utf-8"))
    packet_dir = artifact_dir / probe["packet_directory"]
    for row in cases:
        path = packet_dir / row["packet_file"]
        if not path.is_file() or sha256(path) != row["packet_sha256"]:
            errors.append(f"PACKET_HASH_MISMATCH:{row['review_id']}")
    for filename, expected in (manifest.get("components") or {}).items():
        if sha256(artifact_dir / filename) != expected:
            errors.append(f"COMPONENT_HASH_MISMATCH:{filename}")
    for filename, expected in (manifest.get("repo_components") or {}).items():
        if sha256(previous.ROOT / filename) != expected:
            errors.append(f"REPO_COMPONENT_HASH_MISMATCH:{filename}")
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=OUT)
    args = parser.parse_args()
    artifact_dir = args.artifact_dir.resolve()
    manifest = build(artifact_dir)
    errors = validate(manifest, artifact_dir)
    if errors:
        print(json.dumps({"status": "FAIL", "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    path = artifact_dir / MANIFEST_FILENAME
    previous.write_new_or_identical(path, previous.canonical_bytes(manifest))
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "cases": manifest["case_count"],
                "rounds": manifest["round_count"],
                "shards_per_round": len(manifest["shards"]),
                "cross_round_input_matrix_sha256": manifest["cross_round_input_matrix_sha256"],
                "manifest_sha256": sha256(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
