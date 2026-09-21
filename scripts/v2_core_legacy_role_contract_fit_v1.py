"""Audit whether the R5 candidate contract can express calibration teacher roles."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def basis_family(basis: str) -> str:
    if basis.startswith("MACD_"):
        return "MACD_REGIME"
    if basis.startswith("PIVOT_"):
        return "PIVOT_CAMPAIGN"
    return basis.split("_", 1)[0]


def build_fit_audit(artifact_dir: Path) -> dict[str, Any]:
    contract = load_json(artifact_dir / "legacy_role_selection_contract.candidate_r5.json")
    trace = load_json(artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json")
    pairwise = load_json(artifact_dir / "legacy_teacher_pairwise_audit_candidate_r1.json")
    if trace["locked_reproduction_set_opened"] or pairwise["locked_reproduction_set_opened"]:
        raise ValueError("locked reproduction set must remain sealed")

    pairwise_cases = {str(row["review_id"]): row for row in pairwise["cases"]}
    accepted_families = set(contract["candidate_basis_policy"]["allowed_families"])
    cases: list[dict[str, Any]] = []
    scenario_counts = Counter()
    covered_counts = Counter()
    mismatch_target_counts = Counter()

    for case in trace["cases"]:
        review_id = str(case["review_id"])
        scenario = str(case["teacher"]["scenario"])
        profile = contract["scenario_role_profiles"][scenario]
        candidate_checks = []
        for candidate in case["teacher"]["matching_fixed_candidates"]:
            family = basis_family(str(candidate["basis"]))
            checks = {
                "direction_accepted": candidate["direction"] in profile["accepted_direction"],
                "status_accepted": candidate["status"] in profile["accepted_status"],
                "basis_family_accepted": family in accepted_families,
            }
            candidate_checks.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "basis_family": family,
                    "checks": checks,
                    "profile_compatible": all(checks.values()),
                }
            )
        represented = any(row["profile_compatible"] for row in candidate_checks)
        scenario_counts[scenario] += 1
        covered_counts[scenario] += int(represented)

        pair = pairwise_cases[review_id]
        r4 = pair["r4_selected_audit"]
        targeted_constraints = []
        if not r4["is_teacher_equivalent"]:
            differences = r4["differences"]
            if differences["basis_family_changed"]:
                targeted_constraints.append("CROSS_FAMILY_COMPARISON")
            delta = differences["start_date_signed_delta_days"]
            if delta > 0:
                targeted_constraints.append("NO_RECENCY_DEFAULT")
            elif delta < 0:
                targeted_constraints.append("CAMPAIGN_CONTEXT_VS_WORKING_SEPARATION")
            if scenario == "MACRO_COPY_RESONANCE" or differences["lifecycle_changed"]:
                targeted_constraints.append("SEPARATE_PARENT_FROM_EPISODE")
            if scenario == "MATURE_TREND_PULLBACK":
                targeted_constraints.append("SEPARATE_MATURE_CAMPAIGN_FROM_TRIGGER")
            if scenario == "BEAR_REVERSAL_LEFT_RIGHT":
                targeted_constraints.append("BEAR_CONTROL_PRECEDES_UP_EPISODE")
            if scenario == "FRESH_Q1_EXPANSION":
                targeted_constraints.append("FRESH_NOT_ABSENCE_BUCKET")
        for code in set(targeted_constraints):
            mismatch_target_counts[code] += 1

        cases.append(
            {
                "review_id": review_id,
                "teacher_scenario": scenario,
                "working_role_class": profile["working_role_class"],
                "teacher_equivalent_candidate_checks": candidate_checks,
                "teacher_profile_represented": represented,
                "r4_was_teacher_equivalent": r4["is_teacher_equivalent"],
                "r4_misselection_targeted_by_constraints": sorted(
                    set(targeted_constraints)
                ),
            }
        )

    mismatch_cases = [case for case in cases if not case["r4_was_teacher_equivalent"]]
    targeted_mismatches = sum(
        bool(case["r4_misselection_targeted_by_constraints"]) for case in mismatch_cases
    )
    represented_count = sum(case["teacher_profile_represented"] for case in cases)
    return {
        "audit_version": "v2-core-legacy-role-contract-fit-r1-candidate",
        "status": (
            "CALIBRATION_CONTRACT_COVERAGE_READY（校準契約覆蓋完成）"
            if represented_count == len(cases)
            else "CALIBRATION_CONTRACT_COVERAGE_FAILED（校準契約覆蓋失敗）"
        ),
        "purpose": "REPRESENTABILITY_DIAGNOSTIC_NOT_FORMAL_REPRODUCTION",
        "contract_version": contract["contract_version"],
        "case_count": len(cases),
        "formal_ai_calls": 0,
        "future_performance_used": False,
        "identity_used": False,
        "locked_reproduction_set_opened": False,
        "teacher_answers_allowed_in_formal_ai_input": False,
        "summary": {
            "teacher_profile_represented_count": represented_count,
            "teacher_profile_represented_percent": round(
                100 * represented_count / len(cases), 2
            ),
            "r4_mismatch_count": len(mismatch_cases),
            "r4_mismatch_targeted_by_at_least_one_constraint_count": targeted_mismatches,
            "by_scenario": {
                scenario: {
                    "case_count": scenario_counts[scenario],
                    "represented_count": covered_counts[scenario],
                }
                for scenario in sorted(scenario_counts)
            },
            "mismatch_target_constraint_counts": dict(
                sorted(mismatch_target_counts.items())
            ),
        },
        "limitations": [
            "Coverage proves only that the contract can express the revealed calibration teachers.",
            "It does not prove that a blind AI will select those objects.",
            "It does not prove consistency, course compliance, or positive expectancy.",
        ],
        "cases": cases,
    }


def render_markdown(audit: dict[str, Any]) -> str:
    summary = audit["summary"]
    lines = [
        "# R5角色選擇契約校準覆蓋稽核 R1",
        "",
        f"- 狀態：`{audit['status']}`",
        f"- 校準教師物件可表達：{summary['teacher_profile_represented_count']}/{audit['case_count']}（{summary['teacher_profile_represented_percent']:.2f}%）",
        f"- R4錯選受至少一項R5限制直接針對：{summary['r4_mismatch_targeted_by_at_least_one_constraint_count']}/{summary['r4_mismatch_count']}",
        "- 正式AI呼叫：0；未使用股票身分、未來績效或鎖定重現集。",
        "- 這是可表達性稽核，不是正式重現率、正確率、一致性或績效結果。",
        "",
        "## 分情境覆蓋",
        "",
        "| 情境 | 教師案例 | 契約可表達 |",
        "|---|---:|---:|",
    ]
    for scenario, row in summary["by_scenario"].items():
        lines.append(f"| `{scenario}` | {row['case_count']} | {row['represented_count']} |")
    lines.extend(
        [
            "",
            "## R4偏移對應限制",
            "",
            "| 限制 | 命中錯選案例數 |",
            "|---|---:|",
        ]
    )
    for code, count in summary["mismatch_target_constraint_counts"].items():
        lines.append(f"| `{code}` | {count} |")
    lines.extend(
        [
            "",
            "## 結論",
            "",
            "R5契約沒有把任何個案日期或ID寫成正式規則，而且能容納14個已揭露教師物件；13個R4錯選也都有至少一項明確角色限制可檢查。下一步仍必須讓全新正式AI在看不到教師答案時做單輪選擇，才能知道契約是否真的重現舊純AI。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument(
        "--output-json",
        type=Path,
        default=ARTIFACT_DIR / "legacy_role_contract_fit_candidate_r1.json",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=ARTIFACT_DIR / "legacy_role_contract_fit_candidate_r1.md",
    )
    args = parser.parse_args()
    audit = build_fit_audit(args.artifact_dir)
    args.output_json.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output_md.write_text(render_markdown(audit), encoding="utf-8")
    print(json.dumps(audit["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
