"""Compare a fully frozen one-pass anchor run with calibration legacy references.

The calibration answer file is deliberately loaded only after every expected
output/raw/receipt set has passed immutable local replay validation.
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
from scripts.v2_core_legacy_anchor_alignment_runner_v1 import (
    RunnerError,
    validate_existing_artifacts,
)
from scripts.v2_core_legacy_anchor_alignment_validator_v1 import load_json


ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)
REPORT_VERSION = "v2-core-legacy-anchor-alignment-report-r1-candidate"
ANCHOR_THRESHOLD_PERCENT = 85.0
SCENARIO_THRESHOLD_PERCENT = 85.0


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(100.0 * numerator / denominator, 2)


def _delta(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    return (date.fromisoformat(left) - date.fromisoformat(right)).days


def _normalized_status(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return "CONFIRMED" if text in {"COMPLETED", "CONFIRMED"} else text


def build_report(artifact_dir: Path) -> dict[str, Any]:
    execution_manifest_path = (
        artifact_dir / "legacy_anchor_alignment_execution_manifest_candidate_r1.json"
    )
    execution = load_json(execution_manifest_path)
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
        receipt_path = run_dir / "receipts" / f"{review_id}.legacy_anchor_alignment.json"
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
    base_report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "milestone": "MILESTONE_2A／ONE_PASS_LEGACY_ALIGNMENT",
        "subgate": "CONTROLLING_ANCHOR_ALIGNMENT_B0",
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
        base_report["status"] = "NOT_READY_OUTPUTS_INCOMPLETE（AI輸出尚未完整）"
        base_report["required_next_action"] = (
            "Complete the immutable one-pass outputs. Calibration legacy answers remain unopened."
        )
        return base_report

    # Deliberate reveal boundary: no calibration answer is read above this line.
    references = load_reference_cases(artifact_dir)
    base_report["legacy_answers_loaded"] = True
    selected_count = direction_match = start_match = end_match = interval_match = 0
    status_match = scenario_match = 0
    cases: list[dict[str, Any]] = []
    for row in execution["rows"]:
        review_id = str(row["review_id"])
        reference = references[review_id]
        if not reference.structured_legacy_reference:
            raise ValueError(f"selected alignment case lacks structured legacy anchor: {review_id}")
        output = load_json(run_dir / "outputs" / f"{review_id}.json")
        validation = validations[review_id]
        selected = validation["selected_candidate"]
        legacy_trigger = reference.legacy_trigger
        legacy_anchor = legacy_trigger["macro_anchor"]
        is_selected = selected is not None
        selected_count += int(is_selected)
        direction_ok = bool(is_selected and selected["direction"] == legacy_anchor["direction"])
        start_ok = bool(is_selected and selected["start_date"] == legacy_anchor["start"])
        end_ok = bool(
            is_selected and selected["confirmed_end_date"] == legacy_anchor["end"]
        )
        interval_ok = direction_ok and start_ok and end_ok
        status_ok = bool(
            is_selected
            and _normalized_status(selected["status"])
            == _normalized_status(legacy_anchor.get("status"))
        )
        actual_scenario = output["selection"]["recommended_scenario_family"]
        scenario_ok = actual_scenario == legacy_trigger["scenario"]
        direction_match += int(direction_ok)
        start_match += int(start_ok)
        end_match += int(end_ok)
        interval_match += int(interval_ok)
        status_match += int(status_ok)
        scenario_match += int(scenario_ok)
        cases.append(
            {
                "review_id": review_id,
                "selection_status": output["selection"]["selection_status"],
                "selected_candidate_id": (
                    selected["candidate_id"] if selected is not None else None
                ),
                "actual_anchor": (
                    {
                        "direction": selected["direction"],
                        "start_date": selected["start_date"],
                        "end_date": selected["confirmed_end_date"],
                        "status": selected["status"],
                    }
                    if selected is not None
                    else None
                ),
                "reference_anchor": {
                    "direction": legacy_anchor["direction"],
                    "start_date": legacy_anchor["start"],
                    "end_date": legacy_anchor["end"],
                    "status": legacy_anchor.get("status"),
                },
                "direction_match": direction_ok,
                "start_exact_match": start_ok,
                "end_exact_match": end_ok,
                "full_interval_match": interval_ok,
                "status_match": status_ok,
                "start_delta_calendar_days": (
                    _delta(selected["start_date"], legacy_anchor["start"])
                    if selected is not None
                    else None
                ),
                "end_delta_calendar_days": (
                    _delta(selected["confirmed_end_date"], legacy_anchor["end"])
                    if selected is not None
                    else None
                ),
                "actual_scenario": actual_scenario,
                "reference_scenario": legacy_trigger["scenario"],
                "scenario_match": scenario_ok,
            }
        )
    metrics = {
        "schema_and_local_causal_valid_percent": 100.0,
        "selected_unique_control_percent": _pct(selected_count, expected),
        "anchor_direction_match_percent": _pct(direction_match, expected),
        "anchor_start_exact_percent": _pct(start_match, expected),
        "anchor_end_exact_percent": _pct(end_match, expected),
        "anchor_full_interval_exact_percent": _pct(interval_match, expected),
        "anchor_status_match_percent": _pct(status_match, expected),
        "scenario_family_match_percent": _pct(scenario_match, expected),
        "anchor_threshold_percent": ANCHOR_THRESHOLD_PERCENT,
        "scenario_threshold_percent": SCENARIO_THRESHOLD_PERCENT,
    }
    passed = bool(
        metrics["anchor_full_interval_exact_percent"] >= ANCHOR_THRESHOLD_PERCENT
        and metrics["scenario_family_match_percent"] >= SCENARIO_THRESHOLD_PERCENT
    )
    base_report.update(
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
                else "Analyze only representation/semantic differences, create a new version, and do not start triplicate consistency."
            ),
        }
    )
    return base_report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# M2A 單輪舊純 AI 控制定錨對齊 R1",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 完整合法輸出：{report['completed_valid_case_count']}/{report['expected_case_count']}",
        f"- 舊答案是否已揭盲：{'是' if report['legacy_answers_loaded'] else '否'}",
        "- 這是控制定錨子關卡，不是完整交易重現、三輪一致性或績效回測。",
        "",
    ]
    if report["metrics"] is None:
        lines.extend(
            [
                f"- 缺少案例：{', '.join(report['missing_review_ids']) or '無'}",
                f"- 不合法輸出：{len(report['invalid_outputs'])}",
                "",
                "校準舊答案尚未讀取；必須先完成並凍結全部輸出。",
                "",
            ]
        )
        return "\n".join(lines)
    metrics = report["metrics"]
    lines.extend(
        [
            "## 對齊率",
            "",
            f"- 完整方向＋起點＋終點：**{metrics['anchor_full_interval_exact_percent']:.2f}%**",
            f"- 方向：{metrics['anchor_direction_match_percent']:.2f}%",
            f"- 起點：{metrics['anchor_start_exact_percent']:.2f}%",
            f"- 終點：{metrics['anchor_end_exact_percent']:.2f}%",
            f"- 情境族：**{metrics['scenario_family_match_percent']:.2f}%**",
            "",
            "## 逐案",
            "",
            "| review id | 新控制錨 | 舊控制錨 | 完整一致 | 新情境 | 舊情境 | 情境一致 |",
            "|---|---|---|---:|---|---|---:|",
        ]
    )
    for case in report["cases"]:
        actual = case["actual_anchor"]
        reference = case["reference_anchor"]
        actual_text = (
            f"{actual['direction']} {actual['start_date']}～{actual['end_date'] or 'FORMING'}"
            if actual
            else "UNRESOLVED"
        )
        reference_text = (
            f"{reference['direction']} {reference['start_date']}～{reference['end_date'] or 'FORMING'}"
        )
        lines.append(
            f"| `{case['review_id']}` | {actual_text} | {reference_text} | "
            f"{'是' if case['full_interval_match'] else '否'} | `{case['actual_scenario']}` | "
            f"`{case['reference_scenario']}` | {'是' if case['scenario_match'] else '否'} |"
        )
    lines.extend(
        [
            "",
            "## 尚未評估",
            "",
            "觸發日、進場、episode 防線、加碼、出場與最終交易權限仍須由後續單輪分階段流程重現；本報表不得用來宣稱完整舊 AI 策略已重現。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    json_path = args.artifact_dir / "legacy_anchor_alignment_report_candidate_r1.json"
    md_path = args.artifact_dir / "legacy_anchor_alignment_report_candidate_r1.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
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
