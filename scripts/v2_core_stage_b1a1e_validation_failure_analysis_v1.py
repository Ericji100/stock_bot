"""Deterministically decompose the held-out Stage B1a1E validation failure."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_stage_b1a1e_candidate_validator_v1 import load_json
from scripts.v2_core_stage_b1a1e_codex_runner_v1 import validate_existing_stage_b1a1e_artifacts
from scripts.v2_core_stage_b1a1e_smoke_compare_v1 import compare_smoke


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _paths(run_dir: Path, round_number: int, review_id: str) -> tuple[Path, Path]:
    return (
        run_dir / f"round_{round_number}" / "stage_b1a1e" / f"{review_id}.json",
        run_dir / f"round_{round_number}" / "receipts" / f"{review_id}.stage_b1a1e.json",
    )


def analyze(*, manifest_path: Path, artifact_dir: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    run_dir = artifact_dir / manifest["run_directory"]
    comparison = compare_smoke(
        manifest_path=manifest_path, artifact_dir=artifact_dir, run_dir=run_dir
    )
    if comparison["completed_case_rounds"] != comparison["expected_case_rounds"]:
        raise ValueError("completed three-round validation required")
    if comparison["status"] != "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）":
        raise ValueError("held-out feasibility failure required")

    input_manifest_path = artifact_dir / manifest["input_manifest_file"]
    schema_path = artifact_dir / manifest["schema_file"]
    prompt_path = artifact_dir / manifest["prompt_file"]
    perception_names = (
        "directional_path",
        "same_level_meaningful_target",
        "challenge_or_break_realized",
    )
    perception_counts = {
        name: {"total": 0, "same": 0, "patterns": Counter()} for name in perception_names
    }
    derived_counts = {
        name: {"total": 0, "same": 0, "patterns": Counter()}
        for name in ("directional_coherence", "structural_challenge_or_break")
    }
    drift_candidates = []

    for manifest_row in manifest["rows"]:
        review_id = str(manifest_row["review_id"])
        input_path = (
            artifact_dir
            / manifest["input_packet_directory"]
            / manifest_row["input_packet_file"]
        )
        packet = load_json(input_path)
        segment_index = {
            str(row["candidate_id"]): row for row in packet["selected_focus_segments"]
        }
        option_index = {
            str(row["evidence_option_id"]): row for row in packet["evidence_options"]
        }
        outputs = []
        validations = []
        for round_number in (1, 2, 3):
            output_path, receipt_path = _paths(run_dir, round_number, review_id)
            output = load_json(output_path)
            outputs.append(
                {str(row["candidate_id"]): row for row in output["candidate_perceptions"]}
            )
            validation = validate_existing_stage_b1a1e_artifacts(
                input_packet_path=input_path,
                input_manifest_path=input_manifest_path,
                schema_path=schema_path,
                prompt_path=prompt_path,
                output_path=output_path,
                receipt_path=receipt_path,
            )
            validations.append(
                {
                    str(row["candidate_id"]): row
                    for row in validation["derived_candidate_results"]
                }
            )

        for candidate_id in sorted(outputs[0]):
            perception_drifts = []
            directional = [
                output[candidate_id]["directional_path"]["judgement"] for output in outputs
            ]
            counts = perception_counts["directional_path"]
            counts["total"] += 1
            counts["patterns"]["/".join(directional)] += 1
            if len(set(directional)) == 1:
                counts["same"] += 1
            else:
                perception_drifts.append(
                    {"field": "directional_path", "pattern": directional}
                )

            target_maps = [
                {
                    str(row["evidence_option_id"]): row
                    for row in output[candidate_id]["structural_targets"]
                }
                for output in outputs
            ]
            for option_id in sorted(target_maps[0]):
                for field in (
                    "same_level_meaningful_target",
                    "challenge_or_break_realized",
                ):
                    values = [mapping[option_id][field]["judgement"] for mapping in target_maps]
                    counts = perception_counts[field]
                    counts["total"] += 1
                    counts["patterns"]["/".join(values)] += 1
                    if len(set(values)) == 1:
                        counts["same"] += 1
                    else:
                        option = option_index[option_id]
                        perception_drifts.append(
                            {
                                "field": field,
                                "evidence_option_id": option_id,
                                "pattern": values,
                                "target_scale": option["target"]["target_scale"],
                                "same_scale_objective": option["target"]["same_scale_objective"],
                                "price_reached_or_crossed_objective": option["target"][
                                    "price_reached_or_crossed_objective"
                                ],
                            }
                        )

            derived_patterns = {}
            primary_patterns = {}
            for atom in ("directional_coherence", "structural_challenge_or_break"):
                atom_rows = [validation[candidate_id][atom] for validation in validations]
                results = [row["result"] for row in atom_rows]
                primaries = [row["primary_evidence_option_id"] for row in atom_rows]
                derived_patterns[atom] = results
                primary_patterns[atom] = primaries
                counts = derived_counts[atom]
                counts["total"] += 1
                counts["patterns"]["/".join(results)] += 1
                if len(set(results)) == 1:
                    counts["same"] += 1

            eligible = [validation[candidate_id]["partial_eligible"] for validation in validations]
            if (
                perception_drifts
                or any(len(set(values)) > 1 for values in derived_patterns.values())
                or any(len(set(values)) > 1 for values in primary_patterns.values())
                or len(set(eligible)) > 1
            ):
                segment = segment_index[candidate_id]
                drift_candidates.append(
                    {
                        "review_id": review_id,
                        "candidate_id": candidate_id,
                        "scale": segment["scale"],
                        "direction": segment["direction"],
                        "status": segment["status"],
                        "basis": segment["basis"],
                        "objective_metrics": segment["objective_metrics"],
                        "perception_drifts": perception_drifts,
                        "derived_result_patterns": derived_patterns,
                        "primary_option_patterns": primary_patterns,
                        "partial_eligible_pattern": eligible,
                    }
                )

    perception_metrics = {
        name: {
            "comparisons": row["total"],
            "consistency_percent": _pct(row["same"], row["total"]),
            "patterns": dict(sorted(row["patterns"].items())),
        }
        for name, row in perception_counts.items()
    }
    derived_metrics = {
        name: {
            "comparisons": row["total"],
            "consistency_percent": _pct(row["same"], row["total"]),
            "patterns": dict(sorted(row["patterns"].items())),
        }
        for name, row in derived_counts.items()
    }
    eligibility_drifts = [
        row for row in drift_candidates if len(set(row["partial_eligible_pattern"])) > 1
    ]
    return {
        "analysis_version": "v2-core-stage-b1a1e-heldout-validation-failure-analysis-r1",
        "status": "FAILURE_DIAGNOSTIC_ONLY（失敗診斷、非新規則）",
        "source_status": comparison["status"],
        "source_metrics": comparison["metrics"],
        "perception_metrics": perception_metrics,
        "derived_metrics": derived_metrics,
        "drift_candidate_count": len(drift_candidates),
        "eligibility_drift_candidate_count": len(eligibility_drifts),
        "eligibility_drifts": eligibility_drifts,
        "all_drift_candidates": drift_candidates,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "ai_outputs_modified": False,
    }


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["source_metrics"]
    lines = [
        "# Stage B1a1E R2 留出驗證失敗拆解 R1",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 來源：`{report['source_status']}`",
        f"- AI感知一致率：{metrics['ai_perception_consistency_percent']:.2f}%",
        f"- 程式衍生原子一致率：{metrics['derived_atom_consistency_percent']:.2f}%",
        f"- 主證據一致率：{metrics['primary_option_consistency_percent']:.2f}%",
        f"- 部分合格集合一致率：{metrics['partial_eligible_set_consistency_percent']:.2f}%",
        f"- 有任一漂移的候選：{report['drift_candidate_count']}個",
        f"- 實際改變部分資格的候選：{report['eligibility_drift_candidate_count']}個",
        "- 未使用股票身分、sealed答案或未來績效；未修改AI輸出。",
        "",
        "## 感知與衍生原子",
        "",
        "| 層級 | 欄位 | 比較數 | 一致率 |",
        "|---|---|---:|---:|",
    ]
    for name, row in report["perception_metrics"].items():
        lines.append(f"| AI感知 | `{name}` | {row['comparisons']} | {row['consistency_percent']:.2f}% |")
    for name, row in report["derived_metrics"].items():
        lines.append(f"| 程式衍生 | `{name}` | {row['comparisons']} | {row['consistency_percent']:.2f}% |")
    lines.extend(
        [
            "",
            "## 改變部分資格的候選",
            "",
            "| 案例 | 候選 | 尺度／方向 | 方向原子 | 結構原子 | 部分資格 | 漂移感知數 |",
            "|---|---|---|---|---|---|---:|",
        ]
    )
    for row in report["eligibility_drifts"]:
        direction = "／".join(row["derived_result_patterns"]["directional_coherence"])
        structural = "／".join(row["derived_result_patterns"]["structural_challenge_or_break"])
        eligible = "／".join("是" if value else "否" for value in row["partial_eligible_pattern"])
        lines.append(
            f"| `{row['review_id']}` | `{row['candidate_id']}` | "
            f"{row['scale']}／{row['direction']} | {direction} | {structural} | "
            f"{eligible} | {len(row['perception_drifts'])} |"
        )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = analyze(manifest_path=args.manifest, artifact_dir=args.artifact_dir)
    args.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
