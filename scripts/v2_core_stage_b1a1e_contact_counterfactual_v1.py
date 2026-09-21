"""Evaluate a deterministic target-contact counterfactual for Stage B1a1E.

The analysis never changes saved AI outputs.  AI remains responsible for the
semantic perception that a fixed target is meaningful at the candidate's
level.  The program supplies only the already-recorded AS-OF fact that price
reached or crossed that target, then derives the partial candidate set.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def analyze(*, artifact_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    run_dir = artifact_dir / manifest["run_directory"]
    rounds = int(manifest["required_rounds"])
    case_rows = []
    directional_same = structural_same = eligible_same = 0
    candidate_count = 0

    for manifest_row in manifest["rows"]:
        review_id = str(manifest_row["review_id"])
        packet = load_json(
            artifact_dir
            / manifest["input_packet_directory"]
            / manifest_row["input_packet_file"]
        )
        option_index = {
            str(row["evidence_option_id"]): row for row in packet["evidence_options"]
        }
        round_results = []
        for round_number in range(1, rounds + 1):
            output = load_json(
                run_dir
                / f"round_{round_number}"
                / "stage_b1a1e"
                / f"{review_id}.json"
            )
            candidates = {}
            for candidate in output["candidate_perceptions"]:
                candidate_id = str(candidate["candidate_id"])
                directional = candidate["directional_path"]["judgement"] == "SUPPORTS"
                contact_supports = []
                for target in candidate["structural_targets"]:
                    option_id = str(target["evidence_option_id"])
                    option = option_index[option_id]
                    reached = bool(
                        option["target"]["price_reached_or_crossed_objective"]
                    )
                    meaningful = (
                        target["same_level_meaningful_target"]["judgement"]
                        == "SUPPORTS"
                    )
                    if reached and meaningful:
                        contact_supports.append(option_id)
                structural = bool(contact_supports)
                candidates[candidate_id] = {
                    "directional": directional,
                    "structural_contact": structural,
                    "partial_eligible": directional and structural,
                    "contact_supporting_option_ids": sorted(contact_supports),
                }
            round_results.append(candidates)

        eligible_sets = []
        candidate_patterns = []
        ids = sorted(round_results[0])
        candidate_count += len(ids)
        for candidate_id in ids:
            directional_pattern = [
                row[candidate_id]["directional"] for row in round_results
            ]
            structural_pattern = [
                row[candidate_id]["structural_contact"] for row in round_results
            ]
            eligible_pattern = [
                row[candidate_id]["partial_eligible"] for row in round_results
            ]
            directional_same += len(set(directional_pattern)) == 1
            structural_same += len(set(structural_pattern)) == 1
            eligible_same += len(set(eligible_pattern)) == 1
            if (
                len(set(directional_pattern)) > 1
                or len(set(structural_pattern)) > 1
                or len(set(eligible_pattern)) > 1
            ):
                candidate_patterns.append(
                    {
                        "candidate_id": candidate_id,
                        "directional_pattern": directional_pattern,
                        "structural_contact_pattern": structural_pattern,
                        "partial_eligible_pattern": eligible_pattern,
                    }
                )
        for result in round_results:
            eligible_sets.append(
                sorted(
                    candidate_id
                    for candidate_id, row in result.items()
                    if row["partial_eligible"]
                )
            )
        case_rows.append(
            {
                "review_id": review_id,
                "candidate_count": len(ids),
                "partial_eligible_sizes_by_round": [
                    len(values) for values in eligible_sets
                ],
                "partial_eligible_set_consistent": len(
                    {tuple(values) for values in eligible_sets}
                )
                == 1,
                "drift_candidates": candidate_patterns,
            }
        )

    case_set_same = sum(row["partial_eligible_set_consistent"] for row in case_rows)
    return {
        "analysis_version": "v2-core-stage-b1a1e-contact-counterfactual-r1",
        "status": "DIAGNOSTIC_ONLY_NOT_A_RULE_CHANGE（僅診斷、不是規則變更）",
        "source_manifest": manifest_path.name,
        "case_count": len(case_rows),
        "candidate_count": candidate_count,
        "required_rounds": rounds,
        "directional_atom_consistency_percent": _pct(
            directional_same, candidate_count
        ),
        "structural_contact_atom_consistency_percent": _pct(
            structural_same, candidate_count
        ),
        "candidate_eligibility_consistency_percent": _pct(
            eligible_same, candidate_count
        ),
        "partial_eligible_set_consistency_percent": _pct(
            case_set_same, len(case_rows)
        ),
        "cases": case_rows,
        "counterfactual_contract": {
            "ai_decides_meaningful_same_level_target": True,
            "program_decides_objective_target_contact": True,
            "saved_ai_outputs_modified": False,
            "future_performance_used": False,
            "identity_used": False,
            "sealed_labels_used": False,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Stage B1a1E 客觀觸及反事實診斷 R1",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 來源：`{report['source_manifest']}`",
        f"- 方向原子一致率：{report['directional_atom_consistency_percent']:.2f}%",
        f"- 結構觸及原子一致率：{report['structural_contact_atom_consistency_percent']:.2f}%",
        f"- 候選資格一致率：{report['candidate_eligibility_consistency_percent']:.2f}%",
        f"- 部分合格集合一致率：{report['partial_eligible_set_consistency_percent']:.2f}%",
        "- AI只判斷固定目標是否同級且有結構意義；程式只使用AS-OF已記錄的客觀觸及事實。",
        "- 未修改既有AI輸出，未使用身分、sealed答案或未來績效。",
        "",
        "| 案例 | 候選 | 集合一致 | 三輪合格數 | 漂移候選 |",
        "|---|---:|---|---|---:|",
    ]
    for row in report["cases"]:
        sizes = "／".join(str(value) for value in row["partial_eligible_sizes_by_round"])
        lines.append(
            f"| `{row['review_id']}` | {row['candidate_count']} | "
            f"{'是' if row['partial_eligible_set_consistent'] else '否'} | "
            f"{sizes} | {len(row['drift_candidates'])} |"
        )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = analyze(artifact_dir=args.artifact_dir, manifest_path=args.manifest)
    args.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
