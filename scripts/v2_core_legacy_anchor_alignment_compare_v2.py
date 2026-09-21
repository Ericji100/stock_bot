"""Compare the fully frozen R2 anchor run with legacy calibration references.

Legacy answers are loaded only after all expected output/raw/receipt sets pass
immutable replay validation.  Both AI preferred-anchor alignment and the more
conservative program-usable alignment are reported; only the latter gates the
next stage.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_calibration_reference_alignment_v1 import load_reference_cases
from scripts.v2_core_legacy_anchor_alignment_runner_v2 import (
    RunnerError,
    validate_existing_artifacts,
)
from scripts.v2_core_legacy_anchor_alignment_validator_v2 import load_json


ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)
REPORT_VERSION = "v2-core-legacy-anchor-alignment-report-r2-candidate"
ANCHOR_THRESHOLD_PERCENT = 85.0
SCENARIO_THRESHOLD_PERCENT = 85.0


def _pct(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(100.0 * numerator / denominator, 2)


def _delta(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    return (date.fromisoformat(left) - date.fromisoformat(right)).days


def _normalized_status(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return "CONFIRMED" if text in {"COMPLETED", "CONFIRMED"} else text


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
    execution_path = artifact_dir / "legacy_anchor_alignment_execution_manifest_candidate_r2.json"
    execution = load_json(execution_path)
    input_manifest_path = artifact_dir / execution["input_manifest_file"]
    schema_path = artifact_dir / execution["schema_file"]
    prompt_path = artifact_dir / execution["prompt_file"]
    run_dir = artifact_dir / execution["run_directory"]
    missing: list[str] = []
    invalid: list[dict[str, str]] = []
    validations: dict[str, dict[str, Any]] = {}
    for row in execution["rows"]:
        review_id = str(row["review_id"])
        input_path = artifact_dir / execution["input_packet_directory"] / row["input_packet_file"]
        output_path = run_dir / "outputs" / f"{review_id}.json"
        receipt_path = run_dir / "receipts" / f"{review_id}.legacy_anchor_alignment_r2.json"
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
        "subgate": "CONTROLLING_ANCHOR_ALIGNMENT_B0_R2",
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
            "Complete all immutable R2 outputs; calibration references remain unopened."
        )
        return report

    # Deliberate reveal boundary: no calibration reference is read above this line.
    references = load_reference_cases(artifact_dir)
    report["legacy_answers_loaded"] = True
    counts = {
        "ai_interval": 0,
        "ai_scenario": 0,
        "program_interval": 0,
        "program_scenario": 0,
        "conflict": 0,
    }
    cases: list[dict[str, Any]] = []
    for row in execution["rows"]:
        review_id = str(row["review_id"])
        reference = references[review_id]
        if not reference.structured_legacy_reference:
            raise ValueError(f"alignment case lacks structured legacy anchor: {review_id}")
        output = load_json(run_dir / "outputs" / f"{review_id}.json")
        validation = validations[review_id]
        legacy_trigger = reference.legacy_trigger
        legacy_anchor = legacy_trigger["macro_anchor"]
        ai_candidate = validation["ai_selected_candidate"]
        program_candidate = validation["program_usable_candidate"]
        ai_match = _match(ai_candidate, legacy_anchor)
        program_match = _match(program_candidate, legacy_anchor)
        ai_scenario = output["selection"]["recommended_scenario_family"]
        program_scenario = validation["program_usable_scenario_family"]
        reference_scenario = legacy_trigger["scenario"]
        ai_scenario_ok = ai_scenario == reference_scenario
        program_scenario_ok = program_scenario == reference_scenario
        counts["ai_interval"] += int(ai_match["full_interval_match"])
        counts["ai_scenario"] += int(ai_scenario_ok)
        counts["program_interval"] += int(program_match["full_interval_match"])
        counts["program_scenario"] += int(program_scenario_ok)
        counts["conflict"] += int(validation["alternative_changes_control_conclusion"] is True)
        cases.append(
            {
                "review_id": review_id,
                "ai_selection_status": validation["ai_selection_status"],
                "program_usable_selection_status": validation["program_usable_selection_status"],
                "alternative_changes_control_conclusion": validation[
                    "alternative_changes_control_conclusion"
                ],
                "ai_preferred_anchor": ai_candidate,
                "program_usable_anchor": program_candidate,
                "reference_anchor": {
                    "direction": legacy_anchor["direction"],
                    "start_date": legacy_anchor["start"],
                    "end_date": legacy_anchor["end"],
                    "status": legacy_anchor.get("status"),
                },
                "ai_preferred_match": ai_match,
                "program_usable_match": program_match,
                "ai_preferred_scenario": ai_scenario,
                "program_usable_scenario": program_scenario,
                "reference_scenario": reference_scenario,
                "ai_preferred_scenario_match": ai_scenario_ok,
                "program_usable_scenario_match": program_scenario_ok,
            }
        )
    metrics = {
        "schema_and_local_causal_valid_percent": 100.0,
        "ai_preferred_anchor_full_interval_exact_percent": _pct(counts["ai_interval"], expected),
        "ai_preferred_scenario_family_match_percent": _pct(counts["ai_scenario"], expected),
        "program_usable_anchor_full_interval_exact_percent": _pct(
            counts["program_interval"], expected
        ),
        "program_usable_scenario_family_match_percent": _pct(
            counts["program_scenario"], expected
        ),
        "permission_changing_conflict_percent": _pct(counts["conflict"], expected),
        "anchor_threshold_percent": ANCHOR_THRESHOLD_PERCENT,
        "scenario_threshold_percent": SCENARIO_THRESHOLD_PERCENT,
    }
    passed = bool(
        metrics["program_usable_anchor_full_interval_exact_percent"] >= ANCHOR_THRESHOLD_PERCENT
        and metrics["program_usable_scenario_family_match_percent"] >= SCENARIO_THRESHOLD_PERCENT
    )
    report.update(
        {
            "status": (
                "ANCHOR_ALIGNMENT_SUBGATE_PASSED（控制定錨對齊子關卡通過）"
                if passed
                else "ANCHOR_ALIGNMENT_REVISION_REQUIRED（控制定錨對齊需要修訂）"
            ),
            "metrics": metrics,
            "cases": cases,
            "required_next_action": (
                "Build the next single-pass scenario/trigger/permission alignment stage; do not start triplicate consistency yet."
                if passed
                else "Analyze aggregate semantic differences without future-performance tuning; do not start triplicate consistency."
            ),
        }
    )
    return report


def _anchor_text(anchor: dict[str, Any] | None) -> str:
    if not anchor:
        return "UNRESOLVED"
    return f"{anchor['direction']} {anchor['start_date']}～{anchor.get('confirmed_end_date') or anchor.get('end_date') or 'FORMING'}"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# M2A 單輪舊純 AI 控制定錨對齊 R2",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 完整合法輸出：{report['completed_valid_case_count']}/{report['expected_case_count']}",
        f"- 舊答案是否已揭盲：{'是' if report['legacy_answers_loaded'] else '否'}",
        "- 這只驗證共同結構層的控制定錨，不代表完整交易或績效已重現。",
        "",
    ]
    if report["metrics"] is None:
        lines.extend(
            [
                f"- 缺少案例：{', '.join(report['missing_review_ids']) or '無'}",
                f"- 不合法輸出：{len(report['invalid_outputs'])}",
                "",
                "校準舊答案尚未讀取；必須先完成並凍結全部 R2 輸出。",
                "",
            ]
        )
        return "\n".join(lines)
    metrics = report["metrics"]
    lines.extend(
        [
            "## 對齊率",
            "",
            f"- AI偏好錨完整區間：**{metrics['ai_preferred_anchor_full_interval_exact_percent']:.2f}%**",
            f"- AI偏好情境族：**{metrics['ai_preferred_scenario_family_match_percent']:.2f}%**",
            f"- 程式可用錨完整區間：**{metrics['program_usable_anchor_full_interval_exact_percent']:.2f}%**",
            f"- 程式可用情境族：**{metrics['program_usable_scenario_family_match_percent']:.2f}%**",
            f"- 會改變結論的替代衝突：{metrics['permission_changing_conflict_percent']:.2f}%",
            "",
            "正式門檻依程式可用結果計算，不以 AI 有偏好但仍衝突的結果灌高分數。",
            "",
            "## 逐案",
            "",
            "| review id | AI偏好錨 | 程式可用錨 | 舊控制錨 | AI一致 | 程式一致 |",
            "|---|---|---|---|---:|---:|",
        ]
    )
    for case in report["cases"]:
        lines.append(
            f"| `{case['review_id']}` | {_anchor_text(case['ai_preferred_anchor'])} | "
            f"{_anchor_text(case['program_usable_anchor'])} | {_anchor_text(case['reference_anchor'])} | "
            f"{'是' if case['ai_preferred_match']['full_interval_match'] else '否'} | "
            f"{'是' if case['program_usable_match']['full_interval_match'] else '否'} |"
        )
    lines.extend(
        [
            "",
            "## 尚未評估",
            "",
            "觸發日、進場、episode 防線、加碼、出場、最終交易權限及歷史績效仍未評估。達成定錨對齊後，還要依序重現這些交易欄位，最後才能比較純 AI 的正期望結果。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    json_path = args.artifact_dir / "legacy_anchor_alignment_report_candidate_r2.json"
    md_path = args.artifact_dir / "legacy_anchor_alignment_report_candidate_r2.md"
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
