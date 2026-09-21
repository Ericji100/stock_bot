"""Deterministic three-round drift analysis for a completed Stage B1a1 smoke.

The analyzer revalidates immutable smoke artifacts before decomposing result,
evidence, and eligibility drift.  It is diagnostic only: it does not repair AI
outputs, choose a preferred round, or read identity, sealed labels, or future
performance.
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
from scripts.v2_core_stage_b1a1_smoke_compare_v3 import compare_smoke


ROUNDS = (1, 2, 3)


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _output_path(run_dir: Path, round_number: int, review_id: str) -> Path:
    return run_dir / f"round_{round_number}" / "stage_b1a1" / f"{review_id}.json"


def _assessment_index(output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["candidate_id"]: row for row in output["candidate_assessments"]}


def _eligible(assessment: dict[str, Any]) -> bool:
    return all(assessment[field]["result"] == "PASS" for field in ATOMIC_FIELDS)


def _evidence_type(ref: str) -> str:
    return ref.split(":", 1)[0].split("-", 1)[0]


def _all_evidence_refs(verdict: dict[str, Any]) -> set[str]:
    return {
        *verdict["primary_evidence_refs"],
        *verdict["supporting_evidence_refs"],
        *verdict["contradicting_evidence_refs"],
    }


def _evidence_sort_key(ref: str) -> tuple[int, str]:
    priority = {
        "RELC": 0,
        "PIVOT": 1,
        "SEG": 2,
        "PRICE": 3,
        "MACD": 4,
    }
    return (priority.get(_evidence_type(ref), 99), ref)


def _canonical_primary(verdict: dict[str, Any]) -> tuple[str, ...]:
    if verdict["result"] == "UNKNOWN":
        return ()
    refs = sorted(_all_evidence_refs(verdict), key=_evidence_sort_key)
    return (refs[0],) if refs else ()


def analyze_final_drift(
    *, manifest_path: Path, artifact_dir: Path, run_dir: Path | None = None
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    run_directory = run_dir or artifact_dir / manifest["run_directory"]
    smoke = compare_smoke(
        manifest_path=manifest_path,
        artifact_dir=artifact_dir,
        run_dir=run_directory,
    )
    if smoke["error_count"]:
        raise ValueError("Stage B1a1 smoke has protocol errors")
    if smoke["completed_case_rounds"] != smoke["expected_case_rounds"]:
        raise ValueError("completed three-round smoke required")
    if not smoke["metrics_published"]:
        raise ValueError("formal three-round smoke metrics required")

    atom_counts = {
        field: {
            "total": 0,
            "result_stable": 0,
            "primary_stable": 0,
            "canonical_primary_stable": 0,
            "primary_type_stable": 0,
            "reason_stable": 0,
            "result_stable_primary_drift": 0,
            "common_cited_evidence": 0,
            "result_patterns": Counter(),
        }
        for field in ATOMIC_FIELDS
    }
    unstable_results: list[dict[str, Any]] = []
    stable_result_primary_drifts: list[dict[str, Any]] = []
    eligibility_drifts: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []

    for manifest_row in manifest["rows"]:
        review_id = manifest_row["review_id"]
        indexed = [
            _assessment_index(load_json(_output_path(run_directory, round_number, review_id)))
            for round_number in ROUNDS
        ]
        candidate_sets = [set(rows) for rows in indexed]
        if len({frozenset(values) for values in candidate_sets}) != 1:
            raise ValueError(f"candidate set mismatch between rounds: {review_id}")

        eligible_sets = [
            {candidate_id for candidate_id, row in rows.items() if _eligible(row)}
            for rows in indexed
        ]
        stable_eligibility_count = 0
        case_drift_count = 0

        for candidate_id in sorted(candidate_sets[0]):
            eligibility = [candidate_id in values for values in eligible_sets]
            if len(set(eligibility)) == 1:
                stable_eligibility_count += 1
            else:
                case_drift_count += 1
                eligibility_drifts.append(
                    {
                        "review_id": review_id,
                        "candidate_id": candidate_id,
                        "eligible_by_round": eligibility,
                        "changed_atoms": [
                            field
                            for field in ATOMIC_FIELDS
                            if len(
                                {
                                    rows[candidate_id][field]["result"]
                                    for rows in indexed
                                }
                            )
                            > 1
                        ],
                    }
                )

            for field in ATOMIC_FIELDS:
                values = [rows[candidate_id][field] for rows in indexed]
                results = [value["result"] for value in values]
                primaries = [tuple(value["primary_evidence_refs"]) for value in values]
                primary_types = [
                    tuple(_evidence_type(ref) for ref in primary)
                    for primary in primaries
                ]
                reasons = [value["reason_code"] for value in values]
                canonical_primaries = [_canonical_primary(value) for value in values]
                evidence_sets = [_all_evidence_refs(value) for value in values]
                common_evidence = set.intersection(*evidence_sets) if evidence_sets else set()
                counts = atom_counts[field]
                counts["total"] += 1
                counts["result_patterns"]["/".join(results)] += 1

                result_stable = len(set(results)) == 1
                primary_stable = len(set(primaries)) == 1
                if result_stable:
                    counts["result_stable"] += 1
                else:
                    unstable_results.append(
                        {
                            "review_id": review_id,
                            "candidate_id": candidate_id,
                            "atom": field,
                            "results_by_round": results,
                            "primary_evidence_by_round": [list(value) for value in primaries],
                        }
                    )
                if primary_stable:
                    counts["primary_stable"] += 1
                if len(set(canonical_primaries)) == 1:
                    counts["canonical_primary_stable"] += 1
                if len(set(primary_types)) == 1:
                    counts["primary_type_stable"] += 1
                if len(set(reasons)) == 1:
                    counts["reason_stable"] += 1
                if common_evidence:
                    counts["common_cited_evidence"] += 1
                if result_stable and not primary_stable:
                    counts["result_stable_primary_drift"] += 1
                    stable_result_primary_drifts.append(
                        {
                            "review_id": review_id,
                            "candidate_id": candidate_id,
                            "atom": field,
                            "result": results[0],
                            "primary_evidence_by_round": [list(value) for value in primaries],
                            "primary_types_by_round": [list(value) for value in primary_types],
                            "common_cited_evidence": sorted(common_evidence),
                        }
                    )

        cases.append(
            {
                "review_id": review_id,
                "candidate_count": len(candidate_sets[0]),
                "eligible_sizes_by_round": [len(values) for values in eligible_sets],
                "eligible_sets_exact_same": len(
                    {frozenset(values) for values in eligible_sets}
                )
                == 1,
                "stable_eligibility_count": stable_eligibility_count,
                "eligibility_drift_count": case_drift_count,
            }
        )

    atom_metrics: dict[str, Any] = {}
    for field, counts in atom_counts.items():
        total = int(counts["total"])
        stable_results = int(counts["result_stable"])
        atom_metrics[field] = {
            "candidates": total,
            "result_consistency_percent": _pct(stable_results, total),
            "primary_evidence_consistency_percent": _pct(
                int(counts["primary_stable"]), total
            ),
            "primary_evidence_type_consistency_percent": _pct(
                int(counts["primary_type_stable"]), total
            ),
            "counterfactual_canonical_primary_consistency_percent": _pct(
                int(counts["canonical_primary_stable"]), total
            ),
            "reason_code_consistency_percent": _pct(
                int(counts["reason_stable"]), total
            ),
            "stable_result_with_primary_drift_count": int(
                counts["result_stable_primary_drift"]
            ),
            "stable_result_primary_drift_percent": _pct(
                int(counts["result_stable_primary_drift"]), stable_results
            ),
            "common_cited_evidence_percent": _pct(
                int(counts["common_cited_evidence"]), total
            ),
            "result_patterns": dict(sorted(counts["result_patterns"].items())),
        }

    total_candidates = sum(row["candidate_count"] for row in cases)
    return {
        "analysis_version": "v2-core-stage-b1a1-final-drift-r1",
        "status": "FINAL_FAILURE_DIAGNOSTIC_ONLY（最終失敗診斷、非新規則）",
        "source_smoke_status": smoke["status"],
        "formal_model": smoke["formal_model"],
        "reasoning_effort": smoke["reasoning_effort"],
        "completed_case_rounds": smoke["completed_case_rounds"],
        "expected_case_rounds": smoke["expected_case_rounds"],
        "source_metrics": smoke["metrics"],
        "summary": {
            "candidate_count": total_candidates,
            "unstable_atomic_result_count": len(unstable_results),
            "stable_result_primary_drift_count": len(stable_result_primary_drifts),
            "eligibility_drift_candidate_count": len(eligibility_drifts),
        },
        "atom_metrics": atom_metrics,
        "cases": cases,
        "unstable_atomic_results": unstable_results,
        "stable_result_primary_drifts": stable_result_primary_drifts,
        "eligibility_drifts": eligibility_drifts,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "ai_outputs_modified": False,
    }


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["source_metrics"]
    summary = report["summary"]
    lines = [
        "# Stage B1a1 R4 三輪失敗拆解",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 來源狀態：`{report['source_smoke_status']}`",
        f"- 正式模型：`{report['formal_model']}／{report['reasoning_effort']}`",
        f"- 案例輪次：{report['completed_case_rounds']}／{report['expected_case_rounds']}",
        "- 本報告只重驗與拆解既有輸出；未讀取身分、sealed答案或未來績效，未修改AI答案。",
        "",
        "## 正式門檻",
        "",
        f"- 原子結果一致率：{metrics['atomic_result_consistency_percent']:.2f}%",
        f"- 主證據一致率：{metrics['primary_evidence_consistency_percent']:.2f}%",
        f"- 合格集合一致率：{metrics['eligible_set_consistency_percent']:.2f}%",
        f"- 不穩定原子結果：{summary['unstable_atomic_result_count']}個",
        f"- 結果相同但主證據漂移：{summary['stable_result_primary_drift_count']}個",
        f"- 合格資格漂移候選：{summary['eligibility_drift_candidate_count']}個",
        "",
        "## 原子拆解",
        "",
        "| 原子 | 結果一致 | 主證據一致 | 固定排序反事實 | 主證據類型一致 | reason一致 | 穩定結果中主證據漂移 | 三輪共同引用證據 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for field, row in report["atom_metrics"].items():
        lines.append(
            f"| `{field}` | {row['result_consistency_percent']:.2f}% | "
            f"{row['primary_evidence_consistency_percent']:.2f}% | "
            f"{row['counterfactual_canonical_primary_consistency_percent']:.2f}% | "
            f"{row['primary_evidence_type_consistency_percent']:.2f}% | "
            f"{row['reason_code_consistency_percent']:.2f}% | "
            f"{row['stable_result_primary_drift_percent']:.2f}% | "
            f"{row['common_cited_evidence_percent']:.2f}% |"
        )
    lines.extend(
        [
            "",
            "## 案例與合格集合",
            "",
            "| 案例 | 候選 | 三輪合格數 | 集合完全相同 | 資格漂移候選 |",
            "|---|---:|---|---|---:|",
        ]
    )
    for row in report["cases"]:
        sizes = "／".join(str(value) for value in row["eligible_sizes_by_round"])
        lines.append(
            f"| `{row['review_id']}` | {row['candidate_count']} | {sizes} | "
            f"{'是' if row['eligible_sets_exact_same'] else '否'} | "
            f"{row['eligibility_drift_count']} |"
        )
    lines.extend(
        [
            "",
            "## 合格資格漂移",
            "",
            "| 案例 | 候選 | R1／R2／R3 | 改變原子 |",
            "|---|---|---|---|",
        ]
    )
    for row in report["eligibility_drifts"]:
        eligibility = "／".join("合格" if value else "不合格" for value in row["eligible_by_round"])
        atoms = "、".join(row["changed_atoms"])
        lines.append(
            f"| `{row['review_id']}` | `{row['candidate_id']}` | {eligibility} | {atoms} |"
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
    report = analyze_final_drift(
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
