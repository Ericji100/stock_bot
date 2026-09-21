"""Offline, post-freeze teacher comparison for two R7 role/route diagnostics.

The teacher file is read only here, after immutable AI outputs/receipts exist.
No identity, future price, performance or teacher answer enters a formal AI call.
This is not a 14-case reproduction score or a backtest.
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

from scripts.v2_core_legacy_relation_runner_r7b import validate_existing as validate_relation_r7b
from scripts.v2_core_legacy_relation_runner_r7c import validate_existing as validate_relation_r7c
from scripts.v2_core_legacy_role_cross_object_preflight_r7b import build_report as report_r7b
from scripts.v2_core_legacy_role_cross_object_preflight_r7c import build_report as report_r7c
from scripts.v2_core_legacy_role_qualification_runner_r7b import validate_existing as validate_role_r7b
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, write_new_or_identical


VERSION = "v2-core-legacy-r7-two-case-teacher-diagnostic-r1"
CASES = (
    ("FP-18b86f08f05563ff5886097b", "R7B", "UP_CONTROL_CHALLENGER"),
    ("FP-38178c2820dd71aa800fd8d7", "R7C_MIXED", "IMMEDIATE_COMPLETED_UP_PARENT"),
)


def _candidate_assessment(artifact_dir: Path, review_id: str, role: str, candidate_id: str) -> dict[str, Any] | None:
    validate_role_r7b(artifact_dir=artifact_dir, review_id=review_id, role=role)
    path = artifact_dir / "legacy_role_qualification_runs_candidate_r7b" / role / f"{review_id}.json"
    response = load_json(path)
    rows = [item for item in response["candidate_assessments"] if item["candidate_id"] == candidate_id]
    return rows[0] if len(rows) == 1 else None


def build_report(artifact_dir: Path) -> dict[str, Any]:
    # No teacher read until all existing formal stage outputs have been revalidated.
    frozen = []
    for review_id, protocol, teacher_role in CASES:
        if protocol == "R7B":
            route = validate_relation_r7b(artifact_dir=artifact_dir, review_id=review_id)
            preflight = report_r7b(artifact_dir, review_id)
        else:
            route = validate_relation_r7c(artifact_dir=artifact_dir, review_id=review_id)
            preflight = report_r7c(artifact_dir, review_id)
        if route["status"] != "VALID" or preflight["status"] != "CROSS_ROLE_PRELIMINARY_READY":
            raise ValueError("formal frozen stages not valid/ready")
        frozen.append((review_id, protocol, teacher_role, route, preflight))
    teacher_doc = load_json(artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json")
    teachers = {item["review_id"]: item["teacher"] for item in teacher_doc["cases"]}
    rows = []
    for review_id, protocol, teacher_role, route, preflight in frozen:
        teacher = teachers[review_id]
        fixed = teacher["matching_fixed_candidates"]
        program_working = preflight["selected_role_objects"][route["program_working_role"]]
        if program_working is None:
            raise ValueError("resolved route has no working object")
        teacher_ids = {item["candidate_id"] for item in fixed}
        assessment = _candidate_assessment(artifact_dir, review_id, teacher_role, next(iter(teacher_ids))) if len(teacher_ids) == 1 else None
        rows.append({
            "review_id": review_id,
            "as_of": preflight["as_of"],
            "protocol": protocol,
            "program_scenario": route["program_derived_scenario"],
            "teacher_scenario": teacher["scenario"],
            "scenario_match": route["program_derived_scenario"] == teacher["scenario"],
            "program_working_role": route["program_working_role"],
            "program_working": {key: program_working[key] for key in ("candidate_id", "basis", "scale", "direction", "status", "start_date", "confirmed_end_date")},
            "teacher_working": {
                "direction": teacher["working_anchor"]["direction"],
                "status": teacher["working_anchor"]["status"],
                "start_date": teacher["working_anchor"]["start_date"],
                "confirmed_end_date": teacher["working_anchor"]["end_date"],
                "matching_fixed_candidate_ids": sorted(teacher_ids),
            },
            "working_fixed_candidate_match": program_working["candidate_id"] in teacher_ids,
            "working_boundary_match": (
                program_working["direction"] == teacher["working_anchor"]["direction"]
                and program_working["status"] == teacher["working_anchor"]["status"]
                and program_working["start_date"] == teacher["working_anchor"]["start_date"]
                and program_working["confirmed_end_date"] == teacher["working_anchor"]["end_date"]
            ),
            "teacher_candidate_role_examined": teacher_role,
            "teacher_candidate_role_assessment": {
                "atoms": assessment["atoms"],
                "candidate_id": assessment["candidate_id"],
                "evidence_option_id": assessment["evidence_option_id"],
            } if assessment else None,
            "teacher_stop_date": teacher["stop_date"],
            "teacher_trigger_path": teacher["trigger_path"],
            "trade_permission_granted": False,
        })
    return {
        "report_version": VERSION,
        "scope": "TWO_CASE_POST_FREEZE_CALIBRATION_DIAGNOSTIC_ONLY",
        "case_count": len(rows),
        "scenario_matches": sum(row["scenario_match"] for row in rows),
        "working_fixed_candidate_matches": sum(row["working_fixed_candidate_match"] for row in rows),
        "working_boundary_matches": sum(row["working_boundary_match"] for row in rows),
        "not_formal_14_case_reproduction": True,
        "not_performance_backtest": True,
        "teacher_used_in_formal_ai_input": False,
        "identity_used_in_formal_ai_input": False,
        "future_performance_used": False,
        "rows": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# R7兩案舊純AI語意對照（離線診斷，非正式重現率）", "",
        "先重驗既有AI輸出／收據，之後才離線讀取teacher；不使用股票身分、未來K棒或損益判斷訊號。",
        "本報告只有2案，且第二案混合R7B與R7C合法輸出；不能外推成14案正式重現率或績效。", "",
        f"- 情境相同：{report['scenario_matches']}/{report['case_count']}",
        f"- 工作錨固定候選相同：{report['working_fixed_candidate_matches']}/{report['case_count']}",
        f"- 工作錨起訖／生命週期相同：{report['working_boundary_matches']}/{report['case_count']}", "",
        "| 案例 | 舊純AI情境 | 目前情境 | 舊純AI工作錨 | 目前工作錨 | 候選相同 |", "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        teacher, program = row["teacher_working"], row["program_working"]
        t = f"{teacher['start_date']}～{teacher['confirmed_end_date'] or '形成中'}"
        p = f"{program['start_date']}～{program['confirmed_end_date'] or '形成中'} ({program['basis']})"
        lines.append(f"| {row['review_id']} | {row['teacher_scenario']} | {row['program_scenario']} | {t} | {p} | {'是' if row['working_fixed_candidate_match'] else '否'} |")
    lines.extend(["", "## 角色排除位置", ""])
    for row in report["rows"]:
        assessment = row["teacher_candidate_role_assessment"]
        atoms = ", ".join(f"{name}={value}" for name, value in assessment["atoms"].items()) if assessment else "候選未被角色輸出評估"
        lines.append(f"- `{row['review_id']}`：teacher候選在`{row['teacher_candidate_role_examined']}`的原子為：{atoms}。")
    lines.extend(["", "這些差異表示目前角色語意仍在校準，不能把合法輸出當作舊純AI交易已重現。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    output = args.artifact_dir / "legacy_r7_two_case_teacher_diagnostic_candidate_r1.json"
    markdown = output.with_suffix(".md")
    write_new_or_identical(output, canonical_bytes(report))
    write_new_or_identical(markdown, render_markdown(report).encode("utf-8"))
    print(json.dumps({key: report[key] for key in ("case_count", "scenario_matches", "working_fixed_candidate_matches", "working_boundary_matches")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
