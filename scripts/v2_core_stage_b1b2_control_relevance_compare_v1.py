"""Revalidate and compare three B1b2 control-relevance rounds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_stage_b1b2_control_relevance_runner_v1 import RunnerError, validate_existing_artifacts
from scripts.v2_core_stage_b1b2_control_relevance_validator_v1 import load_json


def _pct(a: int, b: int) -> float:
    return round(100.0 * a / b, 2) if b else 100.0


def compare(*, artifact_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    run_dir = artifact_dir / manifest["run_directory"]
    missing, errors = [], []
    values: dict[str, list[dict[str, Any]]] = {}
    for row in manifest["rows"]:
        review_id = str(row["review_id"])
        values[review_id] = []
        for round_number in range(1, int(manifest["required_rounds"]) + 1):
            output = run_dir / f"round_{round_number}" / "stage_b1b2_control_relevance" / f"{review_id}.json"
            receipt = run_dir / f"round_{round_number}" / "receipts" / f"{review_id}.stage_b1b2_control_relevance.json"
            if not output.exists() or not receipt.exists():
                missing.append(f"round_{round_number}:{review_id}")
                continue
            try:
                values[review_id].append(
                    validate_existing_artifacts(
                        input_packet_path=artifact_dir / manifest["input_packet_directory"] / row["input_packet_file"],
                        input_manifest_path=artifact_dir / manifest["input_manifest_file"],
                        schema_path=artifact_dir / manifest["schema_file"],
                        prompt_path=artifact_dir / manifest["prompt_file"],
                        output_path=output,
                        receipt_path=receipt,
                    )
                )
            except RunnerError as exc:
                errors.append(f"round_{round_number}:{review_id}:{exc}")
    base = {
        "manifest_version": manifest["manifest_version"],
        "formal_model": manifest["formal_model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "case_count": manifest["case_count"],
        "required_rounds": manifest["required_rounds"],
        "expected_case_rounds": manifest["expected_case_rounds"],
        "completed_case_rounds": sum(len(rows) for rows in values.values()),
        "missing_case_rounds": missing,
        "error_count": len(errors),
        "errors": errors,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "unique_controlling_anchor_selected": False,
    }
    if missing or errors:
        return {**base, "status": "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）", "metrics_published": False, "metrics": None}
    total = atom_same = reason_same = option_same = relevant_same = 0
    set_same_count = 0
    result_counts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
    cases = []
    for row in manifest["rows"]:
        review_id = str(row["review_id"])
        rounds = values[review_id]
        indexes = [{str(item["candidate_id"]): item for item in result["derived_candidate_results"]} for result in rounds]
        candidate_ids = sorted(indexes[0])
        local_same = 0
        for candidate_id in candidate_ids:
            atoms = [index[candidate_id]["current_control_relevance"] for index in indexes]
            results = [atom["result"] for atom in atoms]
            reasons = [atom["reason_code"] for atom in atoms]
            options = [atom["primary_evidence_option_id"] for atom in atoms]
            relevant = [index[candidate_id]["control_relevant"] for index in indexes]
            total += 1
            atom_same += int(len(set(results)) == 1)
            reason_same += int(len(set(reasons)) == 1)
            option_same += int(len(set(options)) == 1)
            relevant_same += int(len(set(relevant)) == 1)
            local_same += int(len(set(relevant)) == 1)
            if len(set(results)) == 1:
                result_counts[results[0]] += 1
        sets = [tuple(result["control_relevant_ids"]) for result in rounds]
        consistent = len(set(sets)) == 1
        set_same_count += int(consistent)
        cases.append(
            {
                "review_id": review_id,
                "candidate_count": len(candidate_ids),
                "atom_consistency_percent": _pct(local_same, len(candidate_ids)),
                "relevant_set_consistent": consistent,
                "relevant_sizes_by_round": [len(value) for value in sets],
            }
        )
    metrics = {
        "schema_and_semantic_valid_percent": 100.0,
        "control_relevance_atom_consistency_percent": _pct(atom_same, total),
        "reason_code_consistency_percent": _pct(reason_same, total),
        "primary_option_consistency_percent": _pct(option_same, total),
        "relevance_boolean_consistency_percent": _pct(relevant_same, total),
        "relevant_set_consistency_percent": _pct(set_same_count, len(cases)),
        "unanimous_result_counts": result_counts,
        "candidate_comparisons": total,
        "cases": cases,
    }
    passed = (
        metrics["control_relevance_atom_consistency_percent"] >= 90
        and metrics["relevance_boolean_consistency_percent"] >= 90
        and metrics["relevant_set_consistency_percent"] >= 95
    )
    return {
        **base,
        "status": "SMOKE_PASSED（Smoke通過）" if passed else "SMOKE_REVISION_REQUIRED（Smoke需要修訂）",
        "metrics_published": True,
        "metrics": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(artifact_dir=args.artifact_dir, manifest_path=args.manifest)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
