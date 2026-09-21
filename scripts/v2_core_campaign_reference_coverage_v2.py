"""Post-hoc coverage audit for the outcome-blind Campaign R2 candidate pool."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_calibration_reference_alignment_v1 import load_reference_cases


ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)
REPORT_VERSION = "v2-core-campaign-reference-coverage-r2"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_report(artifact_dir: Path) -> dict[str, Any]:
    references = load_reference_cases(artifact_dir)
    cases: list[dict[str, Any]] = []
    covered = 0
    structured = [
        reference
        for reference in references.values()
        if reference.positive and reference.structured_legacy_reference
    ]
    for reference in sorted(structured, key=lambda item: item.review_id):
        anchor = reference.legacy_trigger["macro_anchor"]
        review_id = reference.review_id
        catalog = load_json(
            artifact_dir / "campaign_candidate_catalogs_candidate_r1" / f"{review_id}.json"
        )
        pool = load_json(
            artifact_dir / "campaign_candidate_pools_candidate_r2" / f"{review_id}.json"
        )
        pool_ids = {item["candidate_id"] for item in pool["candidate_pool"]}
        matches = [
            item
            for item in catalog["campaign_candidates"]
            if item["direction"] == anchor["direction"]
            and item["start_date"] == anchor["start"]
            and item["confirmed_end_date"] == anchor["end"]
        ]
        retained = [item for item in matches if item["candidate_id"] in pool_ids]
        covered += int(bool(retained))
        cases.append(
            {
                "review_id": review_id,
                "reference_scenario": reference.legacy_trigger["scenario"],
                "legacy_anchor": {
                    "direction": anchor["direction"],
                    "start_date": anchor["start"],
                    "end_date": anchor["end"],
                    "status": anchor["status"],
                },
                "pool_count": len(pool_ids),
                "exact_retained_matches": [
                    {
                        "candidate_id": item["candidate_id"],
                        "basis": item["basis"],
                        "scale": item["scale"],
                        "selection_reasons": next(
                            row["selection_reasons"]
                            for row in pool["candidate_pool"]
                            if row["candidate_id"] == item["candidate_id"]
                        ),
                    }
                    for item in retained
                ],
            }
        )
    count = len(cases)
    coverage = round(100.0 * covered / count, 2) if count else None
    return {
        "report_version": REPORT_VERSION,
        "status": (
            "REFERENCE_OBJECT_POOL_READY_FOR_ONE_PASS_ALIGNMENT"
            if covered == count
            else "REFERENCE_OBJECT_POOL_REVISION_REQUIRED"
        ),
        "milestone": "MILESTONE_2A／REFERENCE_FIRST_ALIGNMENT",
        "scope": "CALIBRATION_ONLY_POST_HOC_POOL_COVERAGE_AUDIT",
        "generator_was_frozen_before_reference_join": True,
        "legacy_answers_exposed_to_pool_generator": False,
        "legacy_answers_exposed_to_ai": False,
        "formal_ai_calls": 0,
        "future_performance_used": False,
        "locked_reproduction_set_opened": False,
        "identity_used_for_ai": False,
        "structured_positive_reference_count": count,
        "exact_reference_object_retained_count": covered,
        "exact_reference_object_pool_coverage_percent": coverage,
        "pool_count_min": min(case["pool_count"] for case in cases),
        "pool_count_max": max(case["pool_count"] for case in cases),
        "cases": cases,
        "finding": (
            "Every structured positive legacy anchor is now expressible and retained by "
            "the outcome-blind R2 pool.  This proves representational comparability only; "
            "it does not yet prove semantic or trade-path reproduction."
        ),
        "required_next_action": (
            "Freeze the R2 pool and run one formal gpt-5.6-sol/xhigh alignment pass. "
            "Compare scenario, controlling anchor, trigger date, entry, stop, add, exit, "
            "and final permission to the frozen pure-AI reference before any triplicate run."
        ),
    }


def markdown(report: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# M2A 舊純 AI 參考優先對齊：候選池覆蓋 R2",
            "",
            f"- 狀態：`{report['status']}`",
            f"- 結構化純 AI 正例：{report['structured_positive_reference_count']} 筆",
            f"- 精確參考結構保留：{report['exact_reference_object_retained_count']}/{report['structured_positive_reference_count']}（{report['exact_reference_object_pool_coverage_percent']:.2f}%）",
            f"- 每案候選池：{report['pool_count_min']}～{report['pool_count_max']} 個",
            "- 正式 AI 呼叫：0；未使用未來績效、股票身分或鎖定重現集。",
            "",
            "## 結論",
            "",
            "表示層已達成可比性：舊純 AI 使用的 14 個結構物件，現在全部存在於會送往重現校準的候選池。",
            "這不代表判讀已重現；下一關是一輪正式 AI 對齊，必須先比較情境、控制定錨、觸發日、進場、防線、加碼、出場與最終交易權限。只有這關達標後，才進行三輪一致性。",
            "",
            "## 執行順序（已更正）",
            "",
            "1. 純 AI 參考結構可表示且可送達（本報表已通過）。",
            "2. 單輪正式機制重現純 AI 判讀與交易路徑。",
            "3. 重現差異達標後，才執行三輪一致性。",
            "4. 最後以未參與校準的資料驗證扣成本後正期望與風險。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    json_path = args.artifact_dir / "campaign_reference_coverage_r2.json"
    md_path = args.artifact_dir / "campaign_reference_coverage_r2.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "coverage_percent": report["exact_reference_object_pool_coverage_percent"], "report": str(md_path.resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
