"""Diagnose the B1a2 R3 held-out failure without future-performance data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_stage_b1a2_active_link_runner_v1 import validate_existing_artifacts
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 100.0


def _validated_rounds(artifact_dir: Path, manifest: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    review_id = str(row["review_id"])
    packet_path = artifact_dir / manifest["input_packet_directory"] / row["input_packet_file"]
    run_dir = artifact_dir / manifest["run_directory"]
    return [
        validate_existing_artifacts(
            input_packet_path=packet_path,
            input_manifest_path=artifact_dir / manifest["input_manifest_file"],
            schema_path=artifact_dir / manifest["schema_file"],
            prompt_path=artifact_dir / manifest["prompt_file"],
            output_path=run_dir / f"round_{round_number}" / "stage_b1a2_active_link" / f"{review_id}.json",
            receipt_path=run_dir / f"round_{round_number}" / "receipts" / f"{review_id}.stage_b1a2_active_link.json",
        )
        for round_number in range(1, int(manifest["required_rounds"]) + 1)
    ]


def inspect_manifest(artifact_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    candidates = []
    for row in manifest["rows"]:
        review_id = str(row["review_id"])
        packet = load_json(artifact_dir / manifest["input_packet_directory"] / row["input_packet_file"])
        options = {str(item["candidate_id"]): item for item in packet["role_link_evidence_options"]}
        rounds = _validated_rounds(artifact_dir, manifest, row)
        indexes = [
            {str(item["candidate_id"]): item["active_campaign_link"]["result"] for item in result["derived_candidate_results"]}
            for result in rounds
        ]
        for candidate_id, option in sorted(options.items()):
            results = [index[candidate_id] for index in indexes]
            path_lengths = sorted(len(path["relation_ids"]) for path in option["fixed_relation_paths"])
            candidates.append(
                {
                    "review_id": review_id,
                    "candidate_id": candidate_id,
                    "result_pattern": results,
                    "stable": len(set(results)) == 1,
                    "fixed_path_edge_lengths": path_lengths,
                    "min_fixed_path_edges": min(path_lengths) if path_lengths else None,
                    "max_fixed_path_edges": max(path_lengths) if path_lengths else None,
                }
            )
    return {
        "manifest_file": manifest_path.name,
        "manifest_version": manifest["manifest_version"],
        "case_count": int(manifest["case_count"]),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def evaluate_threshold(candidates: list[dict[str, Any]], threshold: int) -> dict[str, Any]:
    escalated = [
        row for row in candidates
        if row["min_fixed_path_edges"] is not None and row["min_fixed_path_edges"] >= threshold
    ]
    drifts = [row for row in candidates if not row["stable"]]
    captured = [row for row in drifts if row in escalated]
    unflagged = [row for row in candidates if row not in escalated]
    unflagged_stable = [row for row in unflagged if row["stable"]]
    return {
        "min_fixed_path_edges": threshold,
        "candidate_count": len(candidates),
        "escalation_candidate_count": len(escalated),
        "escalation_rate_percent": _pct(len(escalated), len(candidates)),
        "raw_drift_candidate_count": len(drifts),
        "captured_drift_candidate_count": len(captured),
        "drift_capture_percent": _pct(len(captured), len(drifts)),
        "unflagged_active_link_consistency_percent": _pct(len(unflagged_stable), len(unflagged)),
        "final_shortlist_reproducibility_percent": (
            100.0 if len(captured) == len(drifts) and len(unflagged_stable) == len(unflagged)
            else _pct(len(unflagged_stable), len(unflagged))
        ),
    }


def analyze(*, artifact_dir: Path, source_manifest: Path, reference_manifests: list[Path]) -> dict[str, Any]:
    source = inspect_manifest(artifact_dir, source_manifest)
    references = [inspect_manifest(artifact_dir, path) for path in reference_manifests]
    all_candidates = [row for item in [*references, source] for row in item["candidates"]]
    source_drifts = [row for row in source["candidates"] if not row["stable"]]
    all_drifts = [row for row in all_candidates if not row["stable"]]
    shortest_drift_path = min(
        (row["min_fixed_path_edges"] for row in all_drifts if row["min_fixed_path_edges"] is not None),
        default=None,
    )
    threshold_rows = [evaluate_threshold(all_candidates, value) for value in (1, 2, 3)]
    matching_threshold = next(
        (row for row in threshold_rows if row["min_fixed_path_edges"] == shortest_drift_path),
        None,
    )
    return {
        "analysis_version": "v2-core-stage-b1a2-r3-heldout-failure-analysis-r1",
        "status": "FAILURE_DIAGNOSTIC_ONLY（失敗診斷、非新規則）",
        "source_manifest_file": source_manifest.name,
        "source_candidate_count": source["candidate_count"],
        "source_drift_candidate_count": len(source_drifts),
        "source_drift_candidates": source_drifts,
        "reference_manifest_files": [path.name for path in reference_manifests],
        "reference_and_source_candidate_count": len(all_candidates),
        "threshold_counterfactuals": threshold_rows,
        "minimal_revision_hypothesis": {
            "candidate_policy": (
                f"ESCALATE_WHEN_MIN_FIXED_PATH_EDGES_GTE_{shortest_drift_path}"
                if shortest_drift_path is not None else "NO_PATH_LENGTH_HYPOTHESIS_AVAILABLE"
            ),
            "observed_shortest_drift_path_edges": shortest_drift_path,
            "implied_escalation_rate_percent": (
                matching_threshold["escalation_rate_percent"] if matching_threshold else None
            ),
            "reason": (
                "A path-length-only policy must include the shortest observed drifting candidate; "
                "this is a diagnostic boundary, not proof that path length is the correct semantic feature."
            ),
            "recommendation": (
                "DO_NOT_FREEZE_PATH_LENGTH_ONLY_REVISION_WITHOUT_SEMANTIC_DIAGNOSTIC"
                if shortest_drift_path is not None and shortest_drift_path <= 1
                else "VALIDATE_MINIMAL_THRESHOLD_ON_ANOTHER_FRESH_HELDOUT"
            ),
            "is_formally_validated": False,
            "required_next_step": (
                "Separate objective causal reachability from subjective campaign-role judgement, then validate the revised layer boundary."
                if shortest_drift_path is not None and shortest_drift_path <= 1
                else "Treat all evidence used here as calibration and validate the new policy on another truly fresh held-out batch."
            ),
        },
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "ai_outputs_modified": False,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# B1a2 R3 全新留出失敗拆解 R1",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 來源候選：{report['source_candidate_count']}個；漂移：{report['source_drift_candidate_count']}個。",
        "- 未使用股票身分、sealed答案或未來績效；未修改AI輸出。",
        "",
        "## 漂移候選",
        "",
        "| 案例 | 候選 | 三輪結果 | 固定路徑邊數 | 最短邊數 |",
        "|---|---|---|---|---:|",
    ]
    for row in report["source_drift_candidates"]:
        lines.append(
            f"| `{row['review_id']}` | `{row['candidate_id']}` | "
            f"{'／'.join(row['result_pattern'])} | {row['fixed_path_edge_lengths']} | "
            f"{row['min_fixed_path_edges']} |"
        )
    lines.extend(
        [
            "",
            "## 只針對一致性的門檻反事實",
            "",
            "| 最短路徑門檻 | 升級候選 | 升級率 | 漂移捕捉 | 非升級一致率 | 最終可重現率 |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report["threshold_counterfactuals"]:
        lines.append(
            f"| ≥{row['min_fixed_path_edges']}邊 | {row['escalation_candidate_count']} | "
            f"{row['escalation_rate_percent']:.2f}% | {row['drift_capture_percent']:.2f}% | "
            f"{row['unflagged_active_link_consistency_percent']:.2f}% | "
            f"{row['final_shortlist_reproducibility_percent']:.2f}% |"
        )
    lines.extend(
        [
            "",
            "## 最小修訂假說",
            "",
            f"若只使用路徑長度，必須至少涵蓋最短{report['minimal_revision_hypothesis']['observed_shortest_drift_path_edges']}邊的漂移候選，"
            f"會升級約{report['minimal_revision_hypothesis']['implied_escalation_rate_percent']:.2f}%候選。"
            "這只是診斷邊界，不代表路徑長度就是正確語意特徵；若門檻已降至1邊，應先拆分客觀因果可達性與主觀campaign角色，不宜繼續機械降門檻。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--reference-manifest", type=Path, action="append", default=[])
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()
    report = analyze(
        artifact_dir=args.artifact_dir,
        source_manifest=args.source_manifest,
        reference_manifests=args.reference_manifest,
    )
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
