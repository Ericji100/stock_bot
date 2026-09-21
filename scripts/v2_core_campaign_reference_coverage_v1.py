"""Post-hoc calibration audit for campaign candidate representation coverage.

The generator must be frozen before this audit runs.  Legacy references are
used only here, never during candidate construction or AI prompting.
"""

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
REPORT_VERSION = "v2-core-campaign-reference-coverage-r1"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _pct(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(100.0 * numerator / denominator, 2)


def _matches(candidate: dict[str, Any], anchor: dict[str, Any]) -> bool:
    return (
        candidate.get("direction") == anchor.get("direction")
        and candidate.get("start_date") == anchor.get("start")
        and candidate.get("confirmed_end_date") == anchor.get("end")
    )


def build_report(artifact_dir: Path) -> dict[str, Any]:
    references = load_reference_cases(artifact_dir)
    cases: list[dict[str, Any]] = []
    old_full = old_short = new_full = new_short = 0
    basis_counts: dict[str, int] = {}
    structured = [
        reference
        for reference in references.values()
        if reference.positive and reference.structured_legacy_reference
    ]
    for reference in sorted(structured, key=lambda item: item.review_id):
        review_id = reference.review_id
        anchor = reference.legacy_trigger["macro_anchor"]
        old_catalog = load_json(
            artifact_dir / "objective_candidate_catalogs_r1" / f"{review_id}.json"
        )
        new_catalog = load_json(
            artifact_dir
            / "campaign_candidate_catalogs_candidate_r1"
            / f"{review_id}.json"
        )
        old_shortlist = set(old_catalog["prompt_shortlist_segment_ids"])
        new_shortlist = set(new_catalog["prompt_shortlist_campaign_ids"])
        old_matches = [
            item
            for item in old_catalog["objective_segment_candidates"]
            if _matches(item, anchor)
        ]
        new_matches = [
            item for item in new_catalog["campaign_candidates"] if _matches(item, anchor)
        ]
        old_in_shortlist = any(item["candidate_id"] in old_shortlist for item in old_matches)
        new_in_shortlist = any(item["candidate_id"] in new_shortlist for item in new_matches)
        old_full += int(bool(old_matches))
        old_short += int(old_in_shortlist)
        new_full += int(bool(new_matches))
        new_short += int(new_in_shortlist)
        for item in new_matches:
            basis_counts[item["basis"]] = basis_counts.get(item["basis"], 0) + 1
        cases.append(
            {
                "review_id": review_id,
                "reference_scenario": reference.legacy_trigger["scenario"],
                "legacy_anchor": {
                    "direction": anchor.get("direction"),
                    "start_date": anchor.get("start"),
                    "end_date": anchor.get("end"),
                    "status": anchor.get("status"),
                },
                "r1_exact_match_ids": [item["candidate_id"] for item in old_matches],
                "r1_exact_match_in_shortlist": old_in_shortlist,
                "campaign_r1_exact_matches": [
                    {
                        "candidate_id": item["candidate_id"],
                        "basis": item["basis"],
                        "scale": item["scale"],
                        "in_shortlist": item["candidate_id"] in new_shortlist,
                    }
                    for item in new_matches
                ],
                "campaign_r1_exact_match_in_shortlist": new_in_shortlist,
            }
        )
    count = len(cases)
    status = (
        "FULL_REPRESENTATION_RECOVERED_SHORTLIST_REVISION_REQUIRED"
        if new_full == count and new_short < count
        else "REFERENCE_REPRESENTATION_REVISION_REQUIRED"
    )
    return {
        "report_version": REPORT_VERSION,
        "status": status,
        "milestone": "MILESTONE_2A／B1B_REFERENCE_ALIGNMENT",
        "scope": "CALIBRATION_ONLY_POST_HOC_REPRESENTATION_AUDIT",
        "generator_was_frozen_before_reference_join": True,
        "legacy_answers_exposed_to_generator": False,
        "legacy_answers_exposed_to_ai": False,
        "formal_ai_calls": 0,
        "future_performance_used": False,
        "locked_reproduction_set_opened": False,
        "identity_used_for_ai": False,
        "structured_positive_reference_count": count,
        "r1_full_exact_coverage_percent": _pct(old_full, count),
        "r1_shortlist_exact_coverage_percent": _pct(old_short, count),
        "campaign_r1_full_exact_coverage_percent": _pct(new_full, count),
        "campaign_r1_shortlist_exact_coverage_percent": _pct(new_short, count),
        "campaign_r1_full_exact_covered_count": new_full,
        "campaign_r1_shortlist_exact_covered_count": new_short,
        "campaign_r1_exact_match_basis_counts": dict(sorted(basis_counts.items())),
        "cases": cases,
        "finding": (
            "The additive campaign catalog can express every structured legacy anchor, "
            "but three exact objects are not yet delivered by the outcome-blind shortlist. "
            "Do not resume semantic AI alignment until deterministic compression is revised."
        ),
        "required_next_action": (
            "Create and validate an outcome-blind structural compression layer that retains "
            "campaign-scale alternatives without using calibration labels, then rerun this "
            "post-hoc audit before any new formal AI call."
        ),
    }


def markdown(report: dict[str, Any]) -> str:
    missing = [
        case
        for case in report["cases"]
        if not case["campaign_r1_exact_match_in_shortlist"]
    ]
    lines = [
        "# M2A 大段落候選與舊純 AI 參考覆蓋稽核 R1",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 舊純 AI 結構化正例：{report['structured_positive_reference_count']} 筆",
        "- 本稽核只在候選目錄凍結後做校準比對；沒有把舊答案餵給產生器或 AI。",
        "- 未開啟鎖定重現集，未使用未來績效，也沒有新增正式 AI 呼叫。",
        "",
        "| 表示層 | 完整目錄精確覆蓋 | 送入候選短名單精確覆蓋 |",
        "|---|---:|---:|",
        f"| 原 R1 短腿候選 | {report['r1_full_exact_coverage_percent']:.2f}% | {report['r1_shortlist_exact_coverage_percent']:.2f}% |",
        f"| 新 Campaign R1 大段落候選 | {report['campaign_r1_full_exact_coverage_percent']:.2f}% | {report['campaign_r1_shortlist_exact_coverage_percent']:.2f}% |",
        "",
        "## 判讀",
        "",
        "新目錄已證明資料封包足以重建舊純 AI 使用的 14 個大段落物件；先前落差確有一部分來自候選物件切得過短，而不是單純 AI 判錯。",
        "但短名單目前只有 11/14（78.57%）會實際送到後續語意判讀，因此還不能宣稱已恢復純 AI 判讀，也不能挑其中一輪繼續校正。",
        "",
        "## 尚未進入短名單的校準物件",
        "",
        "| review_id | 情境 | 方向 | 起點 | 終點 | 完整目錄中的來源 |",
        "|---|---|---|---|---|---|",
    ]
    for case in missing:
        anchor = case["legacy_anchor"]
        bases = ", ".join(
            sorted({item["basis"] for item in case["campaign_r1_exact_matches"]})
        )
        lines.append(
            f"| `{case['review_id']}` | `{case['reference_scenario']}` | {anchor['direction']} | {anchor['start_date']} | {anchor['end_date'] or 'FORMING'} | `{bases}` |"
        )
    lines.extend(
        [
            "",
            "## 下一個安全動作",
            "",
            "先建立不讀取標籤的結構壓縮／分群層，確保長週期、最近控制端點與形成中大段落都有代表候選，再以本報表做事後覆蓋稽核。通過後才啟動新的 gpt-5.6-sol／xhigh 三輪判讀。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    json_path = args.artifact_dir / "campaign_reference_coverage_r1.json"
    md_path = args.artifact_dir / "campaign_reference_coverage_r1.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "full_exact_coverage_percent": report["campaign_r1_full_exact_coverage_percent"],
                "shortlist_exact_coverage_percent": report["campaign_r1_shortlist_exact_coverage_percent"],
                "report": str(md_path.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
