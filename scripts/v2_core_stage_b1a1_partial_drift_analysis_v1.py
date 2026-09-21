"""Deterministic two-round drift analysis for an incomplete Stage B1a1 smoke.

This is diagnostic only.  It revalidates existing immutable artifacts, never
publishes the formal three-round gate, and never reads identities, sealed
answers, or future performance.
"""

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

from scripts.v2_core_stage_b1a1_candidate_validator_v2 import ATOMIC_FIELDS, load_json
from scripts.v2_core_stage_b1a1_smoke_compare_v2 import compare_smoke


LEFT_ROUND = 1
RIGHT_ROUND = 2


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _output_path(run_dir: Path, round_number: int, review_id: str) -> Path:
    return (
        run_dir
        / f"round_{round_number}"
        / "stage_b1a1"
        / f"{review_id}.json"
    )


def _assessment_index(output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["candidate_id"]: row for row in output["candidate_assessments"]}


def _full_signature(verdict: dict[str, Any]) -> str:
    normalized = {
        "result": verdict["result"],
        "primary_evidence_refs": verdict["primary_evidence_refs"],
        "supporting_evidence_refs": sorted(verdict["supporting_evidence_refs"]),
        "contradicting_evidence_refs": sorted(verdict["contradicting_evidence_refs"]),
        "missing_evidence_codes": sorted(verdict["missing_evidence_codes"]),
        "reason_code": verdict["reason_code"],
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _eligible(assessment: dict[str, Any]) -> bool:
    return all(assessment[field]["result"] == "PASS" for field in ATOMIC_FIELDS)


def analyze_partial_drift(
    *, manifest_path: Path, artifact_dir: Path, run_dir: Path | None = None
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    run_directory = run_dir or artifact_dir / manifest["run_directory"]
    smoke = compare_smoke(
        manifest_path=manifest_path,
        artifact_dir=artifact_dir,
        run_dir=run_directory,
    )
    expected_partial = int(manifest["case_count"]) * 2
    if smoke["error_count"]:
        raise ValueError("Stage B1a1 smoke has protocol errors")
    if smoke["completed_case_rounds"] < expected_partial:
        raise ValueError(
            "at least two complete rounds required: "
            f"expected at least {expected_partial}, got {smoke['completed_case_rounds']}"
        )
    if smoke["metrics_published"]:
        raise ValueError("formal three-round metrics must not be published by this analyzer")

    atom_counts = {
        field: {
            "total": 0,
            "result_same": 0,
            "primary_same": 0,
            "full_same": 0,
            "transitions": Counter(),
        }
        for field in ATOMIC_FIELDS
    }
    overall = {"total": 0, "result_same": 0, "primary_same": 0, "full_same": 0}
    unstable_candidate_keys: set[tuple[str, str]] = set()
    eligibility_flips: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    exact_eligible_sets = 0

    for manifest_row in manifest["rows"]:
        review_id = manifest_row["review_id"]
        left = _assessment_index(
            load_json(_output_path(run_directory, LEFT_ROUND, review_id))
        )
        right = _assessment_index(
            load_json(_output_path(run_directory, RIGHT_ROUND, review_id))
        )
        if set(left) != set(right):
            raise ValueError(f"candidate set mismatch between rounds: {review_id}")

        left_eligible = {candidate_id for candidate_id, row in left.items() if _eligible(row)}
        right_eligible = {
            candidate_id for candidate_id, row in right.items() if _eligible(row)
        }
        if left_eligible == right_eligible:
            exact_eligible_sets += 1
        union = left_eligible | right_eligible
        intersection = left_eligible & right_eligible
        case_flip_count = 0

        for candidate_id in sorted(left):
            changed_atoms: list[dict[str, Any]] = []
            for field in ATOMIC_FIELDS:
                left_value = left[candidate_id][field]
                right_value = right[candidate_id][field]
                counts = atom_counts[field]
                counts["total"] += 1
                overall["total"] += 1
                transition = f"{left_value['result']}->{right_value['result']}"
                counts["transitions"][transition] += 1
                if left_value["result"] == right_value["result"]:
                    counts["result_same"] += 1
                    overall["result_same"] += 1
                else:
                    unstable_candidate_keys.add((review_id, candidate_id))
                    changed_atoms.append(
                        {
                            "atom": field,
                            "round_1_result": left_value["result"],
                            "round_2_result": right_value["result"],
                            "round_1_primary_evidence_refs": left_value[
                                "primary_evidence_refs"
                            ],
                            "round_2_primary_evidence_refs": right_value[
                                "primary_evidence_refs"
                            ],
                            "round_1_reason_code": left_value["reason_code"],
                            "round_2_reason_code": right_value["reason_code"],
                        }
                    )
                if (
                    left_value["primary_evidence_refs"]
                    == right_value["primary_evidence_refs"]
                ):
                    counts["primary_same"] += 1
                    overall["primary_same"] += 1
                if _full_signature(left_value) == _full_signature(right_value):
                    counts["full_same"] += 1
                    overall["full_same"] += 1

            left_is_eligible = candidate_id in left_eligible
            right_is_eligible = candidate_id in right_eligible
            if left_is_eligible != right_is_eligible:
                case_flip_count += 1
                eligibility_flips.append(
                    {
                        "review_id": review_id,
                        "candidate_id": candidate_id,
                        "transition": (
                            "ELIGIBLE->INELIGIBLE"
                            if left_is_eligible
                            else "INELIGIBLE->ELIGIBLE"
                        ),
                        "changed_atoms": changed_atoms,
                    }
                )

        cases.append(
            {
                "review_id": review_id,
                "candidate_count": len(left),
                "eligible_round_1": len(left_eligible),
                "eligible_round_2": len(right_eligible),
                "eligible_set_exact_same": left_eligible == right_eligible,
                "eligible_intersection_count": len(intersection),
                "eligible_union_count": len(union),
                "eligible_jaccard_percent": _pct(len(intersection), len(union)),
                "eligibility_flip_count": case_flip_count,
            }
        )

    atom_metrics: dict[str, Any] = {}
    for field, counts in atom_counts.items():
        total = int(counts["total"])
        atom_metrics[field] = {
            "comparisons": total,
            "result_agreement_percent": _pct(int(counts["result_same"]), total),
            "primary_evidence_agreement_percent": _pct(
                int(counts["primary_same"]), total
            ),
            "full_signature_agreement_percent": _pct(int(counts["full_same"]), total),
            "result_transitions": dict(sorted(counts["transitions"].items())),
        }

    flip_direction_counts = Counter(row["transition"] for row in eligibility_flips)
    return {
        "analysis_version": "v2-core-stage-b1a1-partial-drift-r1",
        "status": "PARTIAL_DIAGNOSTIC_ONLY（部分診斷、非正式三輪門檻）",
        "source_smoke_status": smoke["status"],
        "formal_model": smoke["formal_model"],
        "reasoning_effort": smoke["reasoning_effort"],
        "completed_case_rounds": expected_partial,
        "source_completed_case_rounds": smoke["completed_case_rounds"],
        "expected_case_rounds": smoke["expected_case_rounds"],
        "rounds_compared": [LEFT_ROUND, RIGHT_ROUND],
        "formal_gate_published": False,
        "overall": {
            "atomic_comparisons": overall["total"],
            "atomic_result_agreement_percent": _pct(
                overall["result_same"], overall["total"]
            ),
            "primary_evidence_agreement_percent": _pct(
                overall["primary_same"], overall["total"]
            ),
            "full_signature_agreement_percent": _pct(
                overall["full_same"], overall["total"]
            ),
            "eligible_set_exact_agreement_percent": _pct(
                exact_eligible_sets, len(cases)
            ),
            "unstable_candidate_count": len(unstable_candidate_keys),
            "eligibility_flip_count": len(eligibility_flips),
            "eligibility_flip_direction_counts": dict(
                sorted(flip_direction_counts.items())
            ),
        },
        "atom_metrics": atom_metrics,
        "cases": cases,
        "eligibility_flips": eligibility_flips,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "ai_outputs_modified": False,
    }


def render_markdown(report: dict[str, Any]) -> str:
    overall = report["overall"]
    lines = [
        "# Stage B1a1 R3 兩輪漂移拆解",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 來源狀態：`{report['source_smoke_status']}`",
        f"- 正式模型：`{report['formal_model']}／{report['reasoning_effort']}`",
        f"- 已完成：{report['completed_case_rounds']}／{report['expected_case_rounds']}案例輪次",
        "- 本報告不發布正式三輪門檻，不讀取股票身分、sealed答案或未來績效，也不修改AI輸出。",
        "",
        "## 兩輪總覽",
        "",
        f"- 原子結果一致率：{overall['atomic_result_agreement_percent']:.2f}%",
        f"- 主證據一致率：{overall['primary_evidence_agreement_percent']:.2f}%",
        f"- 完整簽章一致率：{overall['full_signature_agreement_percent']:.2f}%",
        f"- 合格集合逐案完全一致率：{overall['eligible_set_exact_agreement_percent']:.2f}%",
        f"- 至少一個原子改變的候選：{overall['unstable_candidate_count']}個",
        f"- 合格資格翻轉：{overall['eligibility_flip_count']}個",
        "",
        "## 原子漂移",
        "",
        "| 原子 | 結果一致 | 主證據一致 | 完整簽章一致 |",
        "|---|---:|---:|---:|",
    ]
    for field, row in report["atom_metrics"].items():
        lines.append(
            f"| `{field}` | {row['result_agreement_percent']:.2f}% | "
            f"{row['primary_evidence_agreement_percent']:.2f}% | "
            f"{row['full_signature_agreement_percent']:.2f}% |"
        )
    lines.extend(
        [
            "",
            "## 案例合格集合",
            "",
            "| 案例 | R1 | R2 | 完全相同 | 交集／聯集 | 資格翻轉 |",
            "|---|---:|---:|---|---:|---:|",
        ]
    )
    for row in report["cases"]:
        lines.append(
            f"| `{row['review_id']}` | {row['eligible_round_1']} | "
            f"{row['eligible_round_2']} | "
            f"{'是' if row['eligible_set_exact_same'] else '否'} | "
            f"{row['eligible_intersection_count']}／{row['eligible_union_count']} | "
            f"{row['eligibility_flip_count']} |"
        )
    lines.extend(
        [
            "",
            "## 資格翻轉驅動原子",
            "",
            "| 案例 | 候選 | 方向 | 改變原子 |",
            "|---|---|---|---|",
        ]
    )
    for row in report["eligibility_flips"]:
        changed = "、".join(
            f"{item['atom']} {item['round_1_result']}→{item['round_2_result']}"
            for item in row["changed_atoms"]
        )
        lines.append(
            f"| `{row['review_id']}` | `{row['candidate_id']}` | "
            f"`{row['transition']}` | {changed} |"
        )
    lines.extend(
        [
            "",
            "第三輪完成前，以上只能定位漂移來源，不能判定R3通過或失敗。",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = analyze_partial_drift(
        manifest_path=args.manifest,
        artifact_dir=args.artifact_dir,
        run_dir=args.run_dir,
    )
    args.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
