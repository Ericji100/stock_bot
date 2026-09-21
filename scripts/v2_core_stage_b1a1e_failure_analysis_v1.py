"""Deterministic failure decomposition for completed Stage B1a1E smoke R1."""

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
from scripts.v2_core_stage_b1a1e_codex_runner_v1 import (
    validate_existing_stage_b1a1e_artifacts,
)
from scripts.v2_core_stage_b1a1e_smoke_compare_v1 import compare_smoke


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _paths(run_dir: Path, round_number: int, review_id: str) -> tuple[Path, Path]:
    return (
        run_dir / f"round_{round_number}" / "stage_b1a1e" / f"{review_id}.json",
        run_dir / f"round_{round_number}" / "receipts" / f"{review_id}.stage_b1a1e.json",
    )


def _output_index(output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["candidate_id"]: row for row in output["candidate_perceptions"]}


def _derived_index(validation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["candidate_id"]: row for row in validation["derived_candidate_results"]}


def analyze_failure(*, manifest_path: Path, artifact_dir: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    run_dir = artifact_dir / manifest["run_directory"]
    smoke = compare_smoke(
        manifest_path=manifest_path, artifact_dir=artifact_dir, run_dir=run_dir
    )
    if smoke["status"] != "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）":
        raise ValueError("completed B1a1E feasibility failure required")
    selection = load_json(artifact_dir / "stage_b1a1e_calibration_selection_candidate_r1.json")
    role_map = {
        (row["review_id"], selected["candidate_id"]): selected["selection_role"]
        for row in selection["rows"]
        for selected in row["selected"]
    }
    input_manifest_path = artifact_dir / manifest["input_manifest_file"]
    schema_path = artifact_dir / manifest["schema_file"]
    prompt_path = artifact_dir / manifest["prompt_file"]

    perception_counts = {
        name: {"total": 0, "same": 0, "patterns": Counter()}
        for name in (
            "directional_path",
            "same_level_meaningful_target",
            "challenge_or_break_realized",
        )
    }
    derived_counts = {
        name: {"total": 0, "same": 0, "patterns": Counter()}
        for name in ("directional_coherence", "structural_challenge_or_break")
    }
    role_counts: dict[str, dict[str, int]] = {}
    candidate_drifts = []
    cases = []

    for row in manifest["rows"]:
        review_id = row["review_id"]
        input_path = artifact_dir / manifest["input_packet_directory"] / row[
            "input_packet_file"
        ]
        round_outputs = []
        round_validations = []
        for round_number in (1, 2, 3):
            output_path, receipt_path = _paths(run_dir, round_number, review_id)
            round_outputs.append(_output_index(load_json(output_path)))
            validation = validate_existing_stage_b1a1e_artifacts(
                input_packet_path=input_path,
                input_manifest_path=input_manifest_path,
                schema_path=schema_path,
                prompt_path=prompt_path,
                output_path=output_path,
                receipt_path=receipt_path,
            )
            round_validations.append(_derived_index(validation))

        case_drift = 0
        for candidate_id in sorted(round_outputs[0]):
            role = role_map[(review_id, candidate_id)]
            role_counts.setdefault(role, {"total": 0, "derived_stable": 0, "eligible_stable": 0})
            role_counts[role]["total"] += 1
            perception_changes = []

            directional = [
                output[candidate_id]["directional_path"]["judgement"]
                for output in round_outputs
            ]
            counts = perception_counts["directional_path"]
            counts["total"] += 1
            counts["patterns"]["/".join(directional)] += 1
            if len(set(directional)) == 1:
                counts["same"] += 1
            else:
                perception_changes.append(
                    {"perception": "directional_path", "pattern": directional}
                )

            target_maps = []
            for output in round_outputs:
                target_maps.append(
                    {
                        value["evidence_option_id"]: value
                        for value in output[candidate_id]["structural_targets"]
                    }
                )
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
                        perception_changes.append(
                            {
                                "perception": field,
                                "evidence_option_id": option_id,
                                "pattern": values,
                            }
                        )

            derived_patterns = {}
            primary_patterns = {}
            for atom in ("directional_coherence", "structural_challenge_or_break"):
                values = [validation[candidate_id][atom] for validation in round_validations]
                results = [value["result"] for value in values]
                primaries = [value["primary_evidence_option_id"] for value in values]
                derived_patterns[atom] = results
                primary_patterns[atom] = primaries
                counts = derived_counts[atom]
                counts["total"] += 1
                counts["patterns"]["/".join(results)] += 1
                if len(set(results)) == 1:
                    counts["same"] += 1

            eligible = [
                validation[candidate_id]["partial_eligible"]
                for validation in round_validations
            ]
            derived_stable = all(len(set(values)) == 1 for values in derived_patterns.values())
            eligible_stable = len(set(eligible)) == 1
            if derived_stable:
                role_counts[role]["derived_stable"] += 1
            if eligible_stable:
                role_counts[role]["eligible_stable"] += 1
            if perception_changes or not derived_stable or not eligible_stable:
                case_drift += 1
                candidate_drifts.append(
                    {
                        "review_id": review_id,
                        "candidate_id": candidate_id,
                        "selection_role": role,
                        "perception_changes": perception_changes,
                        "derived_result_patterns": derived_patterns,
                        "primary_option_patterns": primary_patterns,
                        "partial_eligible_pattern": eligible,
                    }
                )
        cases.append(
            {
                "review_id": review_id,
                "candidate_count": len(round_outputs[0]),
                "candidate_drift_count": case_drift,
            }
        )

    perception_metrics = {
        name: {
            "comparisons": values["total"],
            "consistency_percent": _pct(values["same"], values["total"]),
            "patterns": dict(sorted(values["patterns"].items())),
        }
        for name, values in perception_counts.items()
    }
    derived_metrics = {
        name: {
            "comparisons": values["total"],
            "consistency_percent": _pct(values["same"], values["total"]),
            "patterns": dict(sorted(values["patterns"].items())),
        }
        for name, values in derived_counts.items()
    }
    role_metrics = {
        role: {
            **values,
            "derived_stability_percent": _pct(values["derived_stable"], values["total"]),
            "eligibility_stability_percent": _pct(
                values["eligible_stable"], values["total"]
            ),
        }
        for role, values in sorted(role_counts.items())
    }
    return {
        "analysis_version": "v2-core-stage-b1a1e-failure-analysis-r1",
        "status": "FAILURE_DIAGNOSTIC_ONLY（失敗診斷、非新規則）",
        "source_smoke_status": smoke["status"],
        "source_metrics": smoke["metrics"],
        "perception_metrics": perception_metrics,
        "derived_metrics": derived_metrics,
        "role_metrics": role_metrics,
        "cases": cases,
        "candidate_drift_count": len(candidate_drifts),
        "candidate_drifts": candidate_drifts,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "ai_outputs_modified": False,
    }


def render_markdown(report: dict[str, Any]) -> str:
    source = report["source_metrics"]
    lines = [
        "# Stage B1a1E R1 失敗拆解",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 來源：`{report['source_smoke_status']}`",
        f"- AI感知一致率：{source['ai_perception_consistency_percent']:.2f}%",
        f"- 合成原子一致率：{source['derived_atom_consistency_percent']:.2f}%",
        f"- 主證據選項一致率：{source['primary_option_consistency_percent']:.2f}%",
        f"- 部分合格集合一致率：{source['partial_eligible_set_consistency_percent']:.2f}%",
        f"- 發生漂移的候選：{report['candidate_drift_count']}個",
        "- 未使用股票身分、sealed答案或未來績效；未修改AI輸出。",
        "",
        "## AI感知層",
        "",
        "| 子命題 | 比較數 | 一致率 |",
        "|---|---:|---:|",
    ]
    for name, row in report["perception_metrics"].items():
        lines.append(f"| `{name}` | {row['comparisons']} | {row['consistency_percent']:.2f}% |")
    lines.extend(
        [
            "",
            "## 程式合成層",
            "",
            "| 原子 | 候選數 | 一致率 |",
            "|---|---:|---:|",
        ]
    )
    for name, row in report["derived_metrics"].items():
        lines.append(f"| `{name}` | {row['comparisons']} | {row['consistency_percent']:.2f}% |")
    lines.extend(
        [
            "",
            "## 選取角色",
            "",
            "| 角色 | 候選 | 兩原子穩定 | 部分資格穩定 |",
            "|---|---:|---:|---:|",
        ]
    )
    for role, row in report["role_metrics"].items():
        lines.append(
            f"| `{role}` | {row['total']} | {row['derived_stability_percent']:.2f}% | "
            f"{row['eligibility_stability_percent']:.2f}% |"
        )
    lines.extend(
        [
            "",
            "## 漂移候選",
            "",
            "| 案例 | 候選 | 角色 | 方向原子 | 結構原子 | 部分資格 | 感知變更數 |",
            "|---|---|---|---|---|---|---:|",
        ]
    )
    for row in report["candidate_drifts"]:
        direction = "／".join(row["derived_result_patterns"]["directional_coherence"])
        structural = "／".join(
            row["derived_result_patterns"]["structural_challenge_or_break"]
        )
        eligible = "／".join("是" if value else "否" for value in row["partial_eligible_pattern"])
        lines.append(
            f"| `{row['review_id']}` | `{row['candidate_id']}` | `{row['selection_role']}` | "
            f"{direction} | {structural} | {eligible} | {len(row['perception_changes'])} |"
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
    report = analyze_failure(manifest_path=args.manifest, artifact_dir=args.artifact_dir)
    args.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
