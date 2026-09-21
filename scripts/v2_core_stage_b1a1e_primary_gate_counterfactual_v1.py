"""Evaluate the deterministic LARGE/SMALL primary-candidate gate.

This is a diagnostic only.  It reuses saved AI directional-path perceptions,
forbids AUXILIARY segments from independently entering the primary anchor
pool, and lets the program verify only two objective facts for a fixed prior
pivot: equal scale and price contact.  It does not choose the controlling
anchor, assign a course role, or grant trade permission.
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
    candidate_total = directional_same = eligibility_same = 0
    auxiliary_candidate_count = 0
    cases = []

    for manifest_row in manifest["rows"]:
        review_id = str(manifest_row["review_id"])
        packet = load_json(
            artifact_dir
            / manifest["input_packet_directory"]
            / manifest_row["input_packet_file"]
        )
        segment_index = {
            str(row["candidate_id"]): row for row in packet["selected_focus_segments"]
        }
        objective_structural = {candidate_id: False for candidate_id in segment_index}
        for option in packet["evidence_options"]:
            if option["atom_name"] != "structural_challenge_or_break":
                continue
            candidate_id = str(option["candidate_id"])
            segment = segment_index[candidate_id]
            target = option["target"]
            if (
                segment["scale"] in {"LARGE", "SMALL"}
                and bool(target["same_scale_objective"])
                and bool(target["price_reached_or_crossed_objective"])
            ):
                objective_structural[candidate_id] = True

        round_eligible_sets = []
        directional_by_candidate: dict[str, list[bool]] = {
            candidate_id: [] for candidate_id in segment_index
        }
        for round_number in range(1, rounds + 1):
            output = load_json(
                run_dir
                / f"round_{round_number}"
                / "stage_b1a1e"
                / f"{review_id}.json"
            )
            eligible = []
            for candidate in output["candidate_perceptions"]:
                candidate_id = str(candidate["candidate_id"])
                directional = (
                    candidate["directional_path"]["judgement"] == "SUPPORTS"
                )
                directional_by_candidate[candidate_id].append(directional)
                if directional and objective_structural[candidate_id]:
                    eligible.append(candidate_id)
            round_eligible_sets.append(sorted(eligible))

        drift_candidates = []
        for candidate_id in sorted(segment_index):
            candidate_total += 1
            if segment_index[candidate_id]["scale"] == "AUXILIARY":
                auxiliary_candidate_count += 1
            directional_pattern = directional_by_candidate[candidate_id]
            same = len(set(directional_pattern)) == 1
            directional_same += same
            eligibility_pattern = [
                candidate_id in eligible for eligible in round_eligible_sets
            ]
            eligible_stable = len(set(eligibility_pattern)) == 1
            eligibility_same += eligible_stable
            if not same or not eligible_stable:
                drift_candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "scale": segment_index[candidate_id]["scale"],
                        "directional_pattern": directional_pattern,
                        "objective_structural_contact": objective_structural[candidate_id],
                        "partial_eligible_pattern": eligibility_pattern,
                    }
                )
        cases.append(
            {
                "review_id": review_id,
                "candidate_count": len(segment_index),
                "auxiliary_candidate_count": sum(
                    row["scale"] == "AUXILIARY" for row in segment_index.values()
                ),
                "partial_eligible_sizes_by_round": [
                    len(values) for values in round_eligible_sets
                ],
                "partial_eligible_set_consistent": len(
                    {tuple(values) for values in round_eligible_sets}
                )
                == 1,
                "drift_candidates": drift_candidates,
            }
        )

    case_set_same = sum(row["partial_eligible_set_consistent"] for row in cases)
    return {
        "analysis_version": "v2-core-stage-b1a1e-primary-gate-counterfactual-r1",
        "status": "DIAGNOSTIC_ONLY_NOT_A_RULE_CHANGE（僅診斷、不是規則變更）",
        "source_manifest": manifest_path.name,
        "case_count": len(cases),
        "candidate_count": candidate_total,
        "auxiliary_candidate_count": auxiliary_candidate_count,
        "required_rounds": rounds,
        "directional_atom_consistency_percent": _pct(
            directional_same, candidate_total
        ),
        "candidate_eligibility_consistency_percent": _pct(
            eligibility_same, candidate_total
        ),
        "partial_eligible_set_consistency_percent": _pct(
            case_set_same, len(cases)
        ),
        "cases": cases,
        "counterfactual_contract": {
            "ai_decides_complete_directional_path": True,
            "program_checks_large_small_scale_match": True,
            "program_checks_objective_target_contact": True,
            "auxiliary_standalone_candidate_forbidden": True,
            "controlling_anchor_or_course_role_decided": False,
            "trade_permission_granted": False,
            "saved_ai_outputs_modified": False,
            "future_performance_used": False,
            "identity_used": False,
            "sealed_labels_used": False,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Stage B1a1E 主候選客觀Gate反事實診斷 R1",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 來源：`{report['source_manifest']}`",
        f"- 方向原子一致率：{report['directional_atom_consistency_percent']:.2f}%",
        f"- 候選資格一致率：{report['candidate_eligibility_consistency_percent']:.2f}%",
        f"- 部分合格集合一致率：{report['partial_eligible_set_consistency_percent']:.2f}%",
        f"- AUXILIARY候選：{report['auxiliary_candidate_count']}個（禁止獨立進入主錨池）。",
        "- 本診斷不決定唯一控制錨、課程角色、情境、觸發、防線或交易權限。",
        "- 未修改AI輸出，未使用身分、sealed答案或未來績效。",
        "",
        "| 案例 | 候選 | AUX | 集合一致 | 三輪合格數 | 漂移候選 |",
        "|---|---:|---:|---|---|---:|",
    ]
    for row in report["cases"]:
        sizes = "／".join(str(value) for value in row["partial_eligible_sizes_by_round"])
        lines.append(
            f"| `{row['review_id']}` | {row['candidate_count']} | "
            f"{row['auxiliary_candidate_count']} | "
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
