"""Reveal-bound comparator for the fully frozen R3 structural-role run."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_calibration_reference_alignment_v1 import load_reference_cases
from scripts.v2_core_legacy_role_alignment_runner_v3 import (
    RunnerError,
    validate_existing_artifacts,
)
from scripts.v2_core_legacy_role_alignment_validator_v3 import load_json


ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)
REPORT_VERSION = "v2-core-legacy-role-alignment-report-r3-candidate"
ANCHOR_THRESHOLD_PERCENT = 85.0
SCENARIO_THRESHOLD_PERCENT = 85.0


def _pct(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(100.0 * numerator / denominator, 2)


def _normalized_status(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return "CONFIRMED" if text in {"COMPLETED", "CONFIRMED"} else text


def _delta(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    return (date.fromisoformat(left) - date.fromisoformat(right)).days


def _match(candidate: dict[str, Any] | None, anchor: dict[str, Any]) -> dict[str, Any]:
    direction = bool(candidate and candidate["direction"] == anchor["direction"])
    start = bool(candidate and candidate["start_date"] == anchor["start"])
    end = bool(candidate and candidate["confirmed_end_date"] == anchor["end"])
    status = bool(
        candidate
        and _normalized_status(candidate["status"]) == _normalized_status(anchor.get("status"))
    )
    return {
        "direction_match": direction,
        "start_exact_match": start,
        "end_exact_match": end,
        "full_interval_match": direction and start and end,
        "status_match": status,
        "start_delta_calendar_days": (
            _delta(candidate["start_date"], anchor["start"]) if candidate else None
        ),
        "end_delta_calendar_days": (
            _delta(candidate["confirmed_end_date"], anchor["end"]) if candidate else None
        ),
    }


def build_report(artifact_dir: Path) -> dict[str, Any]:
    execution = load_json(
        artifact_dir / "legacy_role_alignment_execution_manifest_candidate_r3.json"
    )
    input_manifest_path = artifact_dir / execution["input_manifest_file"]
    schema_path = artifact_dir / execution["schema_file"]
    prompt_path = artifact_dir / execution["prompt_file"]
    run_dir = artifact_dir / execution["run_directory"]
    validations: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    invalid: list[dict[str, str]] = []
    for row in execution["rows"]:
        review_id = str(row["review_id"])
        input_path = artifact_dir / execution["input_packet_directory"] / row["input_packet_file"]
        output_path = run_dir / "outputs" / f"{review_id}.json"
        receipt_path = run_dir / "receipts" / f"{review_id}.legacy_role_alignment_r3.json"
        raw_path = output_path.with_name(output_path.stem + ".raw.json")
        if not (output_path.is_file() and raw_path.is_file() and receipt_path.is_file()):
            missing.append(review_id)
            continue
        try:
            validations[review_id] = validate_existing_artifacts(
                input_packet_path=input_path,
                input_manifest_path=input_manifest_path,
                schema_path=schema_path,
                prompt_path=prompt_path,
                output_path=output_path,
                receipt_path=receipt_path,
            )
        except (RunnerError, ValueError, KeyError) as exc:
            invalid.append({"review_id": review_id, "error": str(exc)})

    expected = len(execution["rows"])
    completed = len(validations)
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "milestone": "MILESTONE_2A／ONE_PASS_LEGACY_ALIGNMENT",
        "subgate": "STRUCTURAL_ROLE_AND_WORKING_ANCHOR_ALIGNMENT_R3",
        "expected_case_count": expected,
        "completed_valid_case_count": completed,
        "missing_review_ids": sorted(missing),
        "invalid_outputs": invalid,
        "formal_model": execution["formal_model"],
        "reasoning_effort": execution["reasoning_effort"],
        "required_rounds": 1,
        "legacy_answers_loaded": False,
        "future_performance_used": False,
        "locked_reproduction_set_opened": False,
        "trade_path_fields": {
            "trigger_date": "NOT_EVALUATED",
            "entry": "NOT_EVALUATED",
            "episode_stop": "NOT_EVALUATED",
            "adds": "NOT_EVALUATED",
            "exit": "NOT_EVALUATED",
            "final_permission": "NOT_EVALUATED",
        },
        "metrics": None,
        "cases": [],
    }
    if missing or invalid or completed != expected:
        report["status"] = "NOT_READY_OUTPUTS_INCOMPLETE（AI輸出尚未完整）"
        report["required_next_action"] = (
            "Complete immutable R3 outputs; calibration references remain unopened."
        )
        return report

    # Deliberate reveal boundary: calibration references are first read here.
    references = load_reference_cases(artifact_dir)
    report["legacy_answers_loaded"] = True
    counts = Counter()
    basis_counts = Counter()
    cases: list[dict[str, Any]] = []
    for row in execution["rows"]:
        review_id = str(row["review_id"])
        reference = references[review_id]
        if not reference.structured_legacy_reference:
            raise ValueError(f"alignment case lacks structured legacy anchor: {review_id}")
        validation = validations[review_id]
        output = load_json(run_dir / "outputs" / f"{review_id}.json")
        legacy_trigger = reference.legacy_trigger
        legacy_anchor = legacy_trigger["macro_anchor"]
        ai_working = validation["role_candidates"]["SCENARIO_WORKING"]
        program_working = validation["program_usable_scenario_working_anchor"]
        campaign = validation["role_candidates"]["CAMPAIGN_CONTEXT"]
        parent = validation["role_candidates"]["PARENT"]
        ai_match = _match(ai_working, legacy_anchor)
        program_match = _match(program_working, legacy_anchor)
        ai_scenario = validation["ai_recommended_scenario_family"]
        program_scenario = validation["program_derived_scenario_family"]
        reference_scenario = legacy_trigger["scenario"]
        ai_scenario_match = ai_scenario == reference_scenario
        program_scenario_match = program_scenario == reference_scenario
        counts["ai_anchor"] += int(ai_match["full_interval_match"])
        counts["program_anchor"] += int(program_match["full_interval_match"])
        counts["ai_scenario"] += int(ai_scenario_match)
        counts["program_scenario"] += int(program_scenario_match)
        counts["routed"] += int(validation["route_status"] == "ROUTED")
        counts["ai_program_scenario"] += int(validation["recommendation_matches_program"])
        if ai_working:
            basis_counts[str(ai_working["basis"])] += 1
        cases.append(
            {
                "review_id": review_id,
                "route_status": validation["route_status"],
                "campaign_context_anchor": campaign,
                "ai_scenario_working_anchor": ai_working,
                "program_usable_scenario_working_anchor": program_working,
                "parent_anchor": parent,
                "reference_working_anchor": {
                    "direction": legacy_anchor["direction"],
                    "start_date": legacy_anchor["start"],
                    "end_date": legacy_anchor["end"],
                    "status": legacy_anchor.get("status"),
                },
                "ai_working_match": ai_match,
                "program_working_match": program_match,
                "ai_recommended_scenario": ai_scenario,
                "program_derived_scenario": program_scenario,
                "reference_scenario": reference_scenario,
                "ai_scenario_match": ai_scenario_match,
                "program_scenario_match": program_scenario_match,
                "recommendation_matches_program": validation["recommendation_matches_program"],
            }
        )
    metrics = {
        "schema_and_local_causal_valid_percent": 100.0,
        "ai_working_anchor_full_interval_exact_percent": _pct(counts["ai_anchor"], expected),
        "program_usable_working_anchor_full_interval_exact_percent": _pct(
            counts["program_anchor"], expected
        ),
        "ai_recommended_scenario_match_percent": _pct(counts["ai_scenario"], expected),
        "program_derived_scenario_match_percent": _pct(counts["program_scenario"], expected),
        "ai_program_scenario_agreement_percent": _pct(counts["ai_program_scenario"], expected),
        "routed_percent": _pct(counts["routed"], expected),
        "ai_working_anchor_basis_counts": dict(sorted(basis_counts.items())),
        "anchor_threshold_percent": ANCHOR_THRESHOLD_PERCENT,
        "scenario_threshold_percent": SCENARIO_THRESHOLD_PERCENT,
    }
    passed = bool(
        metrics["program_usable_working_anchor_full_interval_exact_percent"]
        >= ANCHOR_THRESHOLD_PERCENT
        and metrics["program_derived_scenario_match_percent"] >= SCENARIO_THRESHOLD_PERCENT
        and metrics["schema_and_local_causal_valid_percent"] == 100.0
    )
    report.update(
        {
            "status": (
                "ROLE_ALIGNMENT_SUBGATE_PASSED（結構角色對齊子關卡通過）"
                if passed
                else "ROLE_ALIGNMENT_REVISION_REQUIRED（結構角色對齊需要修訂）"
            ),
            "metrics": metrics,
            "cases": cases,
            "required_next_action": (
                "Build the next single-pass trigger/episode-stop/permission alignment stage; do not start triplicate consistency yet."
                if passed
                else "Analyze aggregate role/route differences without future-performance tuning; do not start triplicate consistency."
            ),
        }
    )
    return report


def _anchor_text(anchor: dict[str, Any] | None) -> str:
    if not anchor:
        return "UNRESOLVED"
    end = anchor.get("confirmed_end_date", anchor.get("end_date"))
    return f"{anchor['direction']} {anchor['start_date']}～{end or 'FORMING'}"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# M2A 單輪舊純 AI 結構角色與工作錨對齊 R3",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 完整合法輸出：{report['completed_valid_case_count']}/{report['expected_case_count']}",
        f"- 舊答案是否已揭盲：{'是' if report['legacy_answers_loaded'] else '否'}",
        "- 本關卡分開campaign背景錨、情境工作錨與父代；舊macro_anchor只和情境工作錨比較。",
        "- 本關卡不代表觸發、進場、防線、交易權限或績效已重現。",
        "",
    ]
    if report["metrics"] is None:
        lines.extend(
            [
                f"- 缺少案例：{', '.join(report['missing_review_ids']) or '無'}",
                f"- 不合法輸出：{len(report['invalid_outputs'])}",
                "",
                "校準舊答案尚未讀取；必須先完成並凍結全部R3輸出。",
                "",
            ]
        )
        return "\n".join(lines)
    metrics = report["metrics"]
    lines.extend(
        [
            "## 對齊率",
            "",
            f"- AI情境工作錨完整區間：**{metrics['ai_working_anchor_full_interval_exact_percent']:.2f}%**",
            f"- 程式可用工作錨完整區間：**{metrics['program_usable_working_anchor_full_interval_exact_percent']:.2f}%**",
            f"- AI建議情境：**{metrics['ai_recommended_scenario_match_percent']:.2f}%**",
            f"- 程式衍生情境：**{metrics['program_derived_scenario_match_percent']:.2f}%**",
            f"- AI建議與程式情境一致：{metrics['ai_program_scenario_agreement_percent']:.2f}%",
            f"- 可唯一路由：{metrics['routed_percent']:.2f}%",
            "",
            "## 逐案",
            "",
            "| review id | campaign背景 | 新工作錨 | 舊工作錨 | 工作錨一致 | 程式情境 | 舊情境 | 情境一致 |",
            "|---|---|---|---|---:|---|---|---:|",
        ]
    )
    for case in report["cases"]:
        lines.append(
            f"| `{case['review_id']}` | {_anchor_text(case['campaign_context_anchor'])} | "
            f"{_anchor_text(case['program_usable_scenario_working_anchor'])} | "
            f"{_anchor_text(case['reference_working_anchor'])} | "
            f"{'是' if case['program_working_match']['full_interval_match'] else '否'} | "
            f"`{case['program_derived_scenario']}` | `{case['reference_scenario']}` | "
            f"{'是' if case['program_scenario_match'] else '否'} |"
        )
    lines.extend(
        [
            "",
            "## 尚未評估",
            "",
            "觸發日、episode防線、下一開盤、加碼、出場、最終交易權限與歷史績效仍未評估。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    json_path = args.artifact_dir / "legacy_role_alignment_report_candidate_r3.json"
    md_path = args.artifact_dir / "legacy_role_alignment_report_candidate_r3.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "completed": report["completed_valid_case_count"],
                "expected": report["expected_case_count"],
                "legacy_answers_loaded": report["legacy_answers_loaded"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
