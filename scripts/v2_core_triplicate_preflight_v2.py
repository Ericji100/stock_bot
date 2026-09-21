"""Freeze the v7 M2A matrix with program-bound STAGE_D provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_source_audit_v1 import sha256


OUT = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
VERSION = "v2-core-m2a-triplicate-execution-r7-candidate"
MANIFEST_FILENAME = "feasibility_probe_execution_manifest_v7.json"
RUN_DIRECTORY = "feasibility_probe_runs_r7"
ROUNDS = (1, 2, 3)
SHARD_SIZE = 4
ARTIFACT_COMPONENT_FILES = (
    "model_execution_policy.json",
    "subjective_term_evidence_dictionary.json",
    "permission_truth_table.json",
    "staged_prompt_contract.md",
    "v2_core_stage_b.prompt.candidate_r2.md",
    "v2_core_stage_d.prompt.candidate_r3.md",
    "v2_core_stage_b.schema.candidate.json",
    "v2_core_stage_d.schema.candidate.json",
)
REPO_COMPONENT_FILES = (
    "scripts/v2_core_codex_staged_runner_v1.py",
    "scripts/v2_core_codex_stage_d_runner_v2.py",
    "scripts/v2_core_staged_output_validator_v1.py",
    "scripts/v2_core_m2a_contracts_v1.py",
    "scripts/v2_core_source_audit_v1.py",
)


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def write_new_or_identical(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != payload:
        raise RuntimeError(f"refusing to overwrite changed artifact: {path}")
    path.write_bytes(payload)


def build(artifact_dir: Path) -> dict[str, Any]:
    probe_path = artifact_dir / "feasibility_probe_manifest.json"
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    if probe.get("case_count") != 48:
        raise RuntimeError("triplicate requires exactly 48 probe cases")
    if probe.get("formal_model") != "gpt-5.6-sol" or probe.get("reasoning_effort") != "xhigh":
        raise RuntimeError("probe model/effort differs from formal policy")
    packet_dir = artifact_dir / str(probe["packet_directory"])
    components = {
        filename: sha256(artifact_dir / filename) for filename in ARTIFACT_COMPONENT_FILES
    }
    repo_components = {
        filename: sha256(ROOT / filename) for filename in REPO_COMPONENT_FILES
    }
    common = {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "probe_manifest_sha256": sha256(probe_path),
        "components": components,
        "repo_components": repo_components,
        "stage_runners": {
            "STAGE_B": "scripts/v2_core_codex_staged_runner_v1.py",
            "STAGE_D": "scripts/v2_core_codex_stage_d_runner_v2.py",
        },
        "stage_order": ["STAGE_B", "PROGRAM_ROUTE", "STAGE_D", "PROGRAM_PERMISSION"],
        "selection_labels_visible": False,
        "future_performance_visible": False,
    }
    case_rows: list[dict[str, Any]] = []
    for row in probe["rows"]:
        packet_path = packet_dir / str(row["packet_file"])
        actual = sha256(packet_path)
        if actual != row["packet_sha256"]:
            raise RuntimeError(f"probe packet changed: {row['review_id']}")
        fingerprint_payload = {
            **common,
            "review_id": row["review_id"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "as_of": row["as_of"],
            "packet_sha256": actual,
        }
        case_rows.append(
            {
                "review_id": row["review_id"],
                "anonymous_stock_id": row["anonymous_stock_id"],
                "as_of": row["as_of"],
                "packet_file": row["packet_file"],
                "packet_sha256": actual,
                "frozen_input_fingerprint": canonical_sha256(fingerprint_payload),
            }
        )
    case_rows.sort(key=lambda item: str(item["review_id"]))
    shards = []
    for shard_id, offset in enumerate(range(0, len(case_rows), SHARD_SIZE), 1):
        rows = case_rows[offset : offset + SHARD_SIZE]
        shards.append(
            {
                "shard_id": shard_id,
                "case_count": len(rows),
                "review_ids": [row["review_id"] for row in rows],
                "shard_input_sha256": canonical_sha256(rows),
            }
        )
    matrix_sha = canonical_sha256(case_rows)
    rounds = [
        {
            "round": number,
            "status": "NOT_STARTED（尚未開始）",
            "input_matrix_sha256": matrix_sha,
            "stage_b_output_dir": f"{RUN_DIRECTORY}/round_{number}/stage_b",
            "program_route_dir": f"{RUN_DIRECTORY}/round_{number}/program_route",
            "stage_d_output_dir": f"{RUN_DIRECTORY}/round_{number}/stage_d",
            "final_permission_dir": f"{RUN_DIRECTORY}/round_{number}/final_permission",
        }
        for number in ROUNDS
    ]
    return {
        "execution_manifest_version": VERSION,
        "status": "PREFLIGHT_READY_AI_NOT_STARTED（前置就緒、AI尚未開始）",
        "formal_model": common["model"],
        "reasoning_effort": common["reasoning_effort"],
        "budget_stop_remaining_percent_lte": 5,
        "usage_check_required_before_each_shard": True,
        "shard_size": SHARD_SIZE,
        "case_count": len(case_rows),
        "round_count": len(rounds),
        "selection_labels_visible": False,
        "future_performance_visible": False,
        "probe_manifest_sha256": common["probe_manifest_sha256"],
        "components": components,
        "repo_components": repo_components,
        "stage_runners": common["stage_runners"],
        "program_bound_stage_d_fields": [
            "schema_version",
            "review_id",
            "anonymous_stock_id",
            "as_of",
            "packet_sha256",
            "stage_b_output_sha256",
            "prompt_sha256",
            "schema_sha256",
            "truth_table_sha256",
            "model",
            "reasoning_effort",
            "independent_review",
        ],
        "cases": case_rows,
        "shards": shards,
        "rounds": rounds,
        "cross_round_input_matrix_sha256": matrix_sha,
        "run_directory": RUN_DIRECTORY,
        "resume_rule": (
            "Never rerun an existing fully validated raw-semantics+program-metadata+receipt+program artifact set. "
            "Preserve invalid raw output without repair; any retry requires a separately versioned attempt policy."
        ),
    }


def validate(manifest: dict[str, Any], artifact_dir: Path) -> list[str]:
    errors: list[str] = []
    if manifest.get("formal_model") != "gpt-5.6-sol":
        errors.append("FORMAL_MODEL_MISMATCH")
    if manifest.get("reasoning_effort") != "xhigh":
        errors.append("REASONING_EFFORT_MISMATCH")
    if manifest.get("budget_stop_remaining_percent_lte") != 5:
        errors.append("BUDGET_THRESHOLD_MISMATCH")
    if manifest.get("stage_runners") != {
        "STAGE_B": "scripts/v2_core_codex_staged_runner_v1.py",
        "STAGE_D": "scripts/v2_core_codex_stage_d_runner_v2.py",
    }:
        errors.append("STAGE_RUNNER_MAP_MISMATCH")
    cases = manifest.get("cases") or []
    if len(cases) != 48 or len({row["review_id"] for row in cases}) != 48:
        errors.append("CASE_MATRIX_NOT_48_UNIQUE_CASES")
    matrix_hashes = {row["input_matrix_sha256"] for row in manifest.get("rounds") or []}
    if len(matrix_hashes) != 1 or next(iter(matrix_hashes), None) != manifest.get(
        "cross_round_input_matrix_sha256"
    ):
        errors.append("ROUND_INPUT_MATRIX_HASHES_DIFFER")
    probe = json.loads((artifact_dir / "feasibility_probe_manifest.json").read_text(encoding="utf-8"))
    packet_dir = artifact_dir / probe["packet_directory"]
    for row in cases:
        path = packet_dir / row["packet_file"]
        if not path.is_file() or sha256(path) != row["packet_sha256"]:
            errors.append(f"PACKET_HASH_MISMATCH:{row['review_id']}")
    for filename, expected in (manifest.get("components") or {}).items():
        path = artifact_dir / filename
        if not path.is_file() or sha256(path) != expected:
            errors.append(f"COMPONENT_HASH_MISMATCH:{filename}")
    for filename, expected in (manifest.get("repo_components") or {}).items():
        path = ROOT / filename
        if not path.is_file() or sha256(path) != expected:
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
    write_new_or_identical(path, canonical_bytes(manifest))
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
