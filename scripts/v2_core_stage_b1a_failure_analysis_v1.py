"""Deterministic failure analysis for a completed Stage B1a smoke run."""

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

from scripts.v2_core_stage_b1a_candidate_validator_v1 import ATOMIC_FIELDS, load_json
from scripts.v2_core_stage_b1a_smoke_compare_v1 import compare_smoke


ROLES = ("PARENT", "CONTROLLING", "CURRENT", "ALTERNATIVE")


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _output_path(run_dir: Path, round_number: int, review_id: str) -> Path:
    return run_dir / f"round_{round_number}" / "stage_b1a" / f"{review_id}.json"


def _receipt_path(run_dir: Path, round_number: int, review_id: str) -> Path:
    return (
        run_dir
        / f"round_{round_number}"
        / "receipts"
        / f"{review_id}.stage_b1a.json"
    )


def _assessment_index(output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["candidate_id"]: row for row in output["segment_assessments"]}


def _full_signature(verdict: dict[str, Any]) -> str:
    normalized = {
        "result": verdict["result"],
        "supporting_evidence_refs": sorted(verdict["supporting_evidence_refs"]),
        "contradicting_evidence_refs": sorted(verdict["contradicting_evidence_refs"]),
        "missing_evidence_codes": sorted(verdict["missing_evidence_codes"]),
        "reason_code": verdict["reason_code"],
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def analyze_failure(
    *, manifest_path: Path, artifact_dir: Path, run_dir: Path | None = None
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    run_directory = run_dir or artifact_dir / manifest["run_directory"]
    smoke = compare_smoke(
        manifest_path=manifest_path,
        artifact_dir=artifact_dir,
        run_dir=run_directory,
    )
    if smoke["completed_case_rounds"] != smoke["expected_case_rounds"]:
        raise ValueError("Stage B1a smoke is incomplete")
    if smoke["error_count"]:
        raise ValueError("Stage B1a smoke has protocol errors")
    if not smoke["metrics_published"]:
        raise ValueError("Stage B1a smoke metrics are unavailable")

    outputs: dict[str, dict[int, dict[str, dict[str, Any]]]] = {}
    pools: dict[str, dict[int, set[str]]] = {}
    for row in manifest["rows"]:
        review_id = row["review_id"]
        outputs[review_id] = {}
        pools[review_id] = {}
        for round_number in range(1, manifest["required_rounds"] + 1):
            outputs[review_id][round_number] = _assessment_index(
                load_json(_output_path(run_directory, round_number, review_id))
            )
            receipt = load_json(_receipt_path(run_directory, round_number, review_id))
            pools[review_id][round_number] = set(
                receipt["validation"]["deep_review_pool_ids"]
            )

    atom_metrics: dict[str, Any] = {}
    unstable_candidates: list[dict[str, Any]] = []
    for field in ATOMIC_FIELDS:
        total = 0
        result_same = 0
        evidence_same = 0
        pairwise_same = {"round_1_vs_2": 0, "round_1_vs_3": 0, "round_2_vs_3": 0}
        round_distribution = {
            str(round_number): Counter() for round_number in range(1, 4)
        }
        patterns: Counter[str] = Counter()
        for review_id, rounds in outputs.items():
            for candidate_id in sorted(rounds[1]):
                values = [rounds[index][candidate_id][field] for index in range(1, 4)]
                results = [value["result"] for value in values]
                total += 1
                patterns["/".join(results)] += 1
                for index, result in enumerate(results, start=1):
                    round_distribution[str(index)][result] += 1
                if len(set(results)) == 1:
                    result_same += 1
                if len({_full_signature(value) for value in values}) == 1:
                    evidence_same += 1
                for label, left, right in (
                    ("round_1_vs_2", 0, 1),
                    ("round_1_vs_3", 0, 2),
                    ("round_2_vs_3", 1, 2),
                ):
                    if results[left] == results[right]:
                        pairwise_same[label] += 1
                if len(set(results)) > 1:
                    unstable_candidates.append(
                        {
                            "review_id": review_id,
                            "candidate_id": candidate_id,
                            "family": field,
                            "round_values": results,
                        }
                    )
        atom_metrics[field] = {
            "candidate_comparisons": total,
            "result_consistency_percent": _pct(result_same, total),
            "full_evidence_consistency_percent": _pct(evidence_same, total),
            "pairwise_result_agreement_percent": {
                label: _pct(count, total) for label, count in pairwise_same.items()
            },
            "round_result_distribution": {
                round_number: dict(sorted(counts.items()))
                for round_number, counts in round_distribution.items()
            },
            "result_patterns": dict(sorted(patterns.items())),
        }

    role_exact_same = 0
    role_membership_same = {role: 0 for role in ROLES}
    role_round_counts = {
        str(round_number): {role: 0 for role in ROLES} for round_number in range(1, 4)
    }
    pool_membership_same = 0
    pool_membership_frequency: Counter[str] = Counter()
    pool_case_rows: list[dict[str, Any]] = []
    total_candidates = 0
    for review_id, rounds in outputs.items():
        case_ids = sorted(rounds[1])
        total_candidates += len(case_ids)
        case_pool_sets = [pools[review_id][index] for index in range(1, 4)]
        pool_case_rows.append(
            {
                "review_id": review_id,
                "candidate_count": len(case_ids),
                "pool_sizes_by_round": [len(value) for value in case_pool_sets],
                "pool_intersection_count": len(set.intersection(*case_pool_sets)),
                "pool_union_count": len(set.union(*case_pool_sets)),
                "pool_jaccard_all_rounds_percent": _pct(
                    len(set.intersection(*case_pool_sets)),
                    len(set.union(*case_pool_sets)),
                ),
            }
        )
        for candidate_id in case_ids:
            role_sets = [
                set(rounds[index][candidate_id]["role_candidates"])
                for index in range(1, 4)
            ]
            if role_sets[0] == role_sets[1] == role_sets[2]:
                role_exact_same += 1
            for index, role_set in enumerate(role_sets, start=1):
                for role in role_set:
                    role_round_counts[str(index)][role] += 1
            for role in ROLES:
                values = [role in role_set for role_set in role_sets]
                if len(set(values)) == 1:
                    role_membership_same[role] += 1

            pool_values = [candidate_id in pool for pool in case_pool_sets]
            if len(set(pool_values)) == 1:
                pool_membership_same += 1
            pool_membership_frequency[str(sum(pool_values))] += 1

    smoke_metrics = smoke["metrics"]
    failed_gates = []
    for label, key in (
        ("ATOMIC_RESULT", "atomic_result_consistency_percent"),
        ("ROLE_SET", "role_set_consistency_percent"),
        ("DEEP_REVIEW_POOL", "deep_review_pool_consistency_percent"),
    ):
        value = smoke_metrics[key]
        if value < 90:
            failed_gates.append(
                {
                    "gate": label,
                    "value_percent": value,
                    "classification": "FAIL_BELOW_75" if value < 75 else "REVISION_75_TO_90",
                }
            )

    return {
        "analysis_version": "v2-core-stage-b1a-failure-analysis-r1",
        "status": "FAILURE_DIAGNOSIS_COMPLETE（失敗診斷完成）",
        "source_smoke_status": smoke["status"],
        "formal_model": smoke["formal_model"],
        "reasoning_effort": smoke["reasoning_effort"],
        "completed_case_rounds": smoke["completed_case_rounds"],
        "protocol_error_count": smoke["error_count"],
        "failed_gates": failed_gates,
        "atom_metrics": atom_metrics,
        "role_metrics": {
            "candidate_comparisons": total_candidates,
            "exact_role_set_consistency_percent": _pct(role_exact_same, total_candidates),
            "per_role_membership_consistency_percent": {
                role: _pct(count, total_candidates)
                for role, count in role_membership_same.items()
            },
            "role_membership_count_by_round": role_round_counts,
        },
        "pool_metrics": {
            "candidate_membership_consistency_percent": _pct(
                pool_membership_same, total_candidates
            ),
            "membership_frequency_round_count": dict(
                sorted(pool_membership_frequency.items())
            ),
            "cases": pool_case_rows,
        },
        "unstable_atomic_candidate_count": len(
            {(row["review_id"], row["candidate_id"]) for row in unstable_candidates}
        ),
        "unstable_atomic_rows": sorted(
            unstable_candidates,
            key=lambda row: (row["review_id"], row["candidate_id"], row["family"]),
        ),
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "ai_outputs_modified": False,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Stage B1a Smoke 失敗拆解 R1",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 來源結果：`{report['source_smoke_status']}`",
        f"- 正式模型：`{report['formal_model']}／{report['reasoning_effort']}`",
        f"- 合法案例輪次：{report['completed_case_rounds']}／12",
        f"- 協定錯誤：{report['protocol_error_count']}",
        "- 未使用股票身分、sealed舊答案或未來績效；未修改AI輸出。",
        "",
        "## 門檻",
        "",
        "| 門檻 | 結果 | 分類 |",
        "|---|---:|---|",
    ]
    for row in report["failed_gates"]:
        lines.append(
            f"| `{row['gate']}` | {row['value_percent']:.2f}% | `{row['classification']}` |"
        )
    lines.extend(
        [
            "",
            "## 三個粗篩原子",
            "",
            "| 原子 | 結果一致率 | 完整證據一致率 | R1↔R2 | R1↔R3 | R2↔R3 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for field, value in report["atom_metrics"].items():
        pairwise = value["pairwise_result_agreement_percent"]
        lines.append(
            f"| `{field}` | {value['result_consistency_percent']:.2f}% | "
            f"{value['full_evidence_consistency_percent']:.2f}% | "
            f"{pairwise['round_1_vs_2']:.2f}% | {pairwise['round_1_vs_3']:.2f}% | "
            f"{pairwise['round_2_vs_3']:.2f}% |"
        )
    role = report["role_metrics"]
    lines.extend(
        [
            "",
            "## 角色",
            "",
            f"- 完整角色集合一致率：{role['exact_role_set_consistency_percent']:.2f}%",
            "- 單一角色成員資格一致率："
            + "、".join(
                f"{name} {value:.2f}%"
                for name, value in role["per_role_membership_consistency_percent"].items()
            ),
            "",
            "## Deep review pool",
            "",
            f"- 候選成員資格一致率：{report['pool_metrics']['candidate_membership_consistency_percent']:.2f}%",
            "",
            "| 案例 | 各輪Pool數 | 三輪交集 | 三輪聯集 | 交集／聯集 |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for row in report["pool_metrics"]["cases"]:
        sizes = "／".join(str(value) for value in row["pool_sizes_by_round"])
        lines.append(
            f"| `{row['review_id']}` | {sizes} | {row['pool_intersection_count']} | "
            f"{row['pool_union_count']} | {row['pool_jaccard_all_rounds_percent']:.2f}% |"
        )
    lines.extend(
        [
            "",
            f"共有 {report['unstable_atomic_candidate_count']} 個候選至少一個粗篩原子跨輪改變。",
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
    report = analyze_failure(
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
