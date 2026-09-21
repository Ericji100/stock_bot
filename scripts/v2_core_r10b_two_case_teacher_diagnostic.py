"""Offline two-case role/trigger comparison after R10/R10B formal freeze."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_episode_defense_runner_r8 import validate_existing as validate_defense
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, write_new_or_identical
from scripts.v2_core_tactical_cycle_anchor_runner_r10 import validate_existing as validate_role
from scripts.v2_core_trigger_runner_r10b import validate_existing as validate_trigger


VERSION = "v2-core-r10b-two-case-teacher-diagnostic-r1"
REVIEW_IDS = ("FP-18b86f08f05563ff5886097b", "FP-38178c2820dd71aa800fd8d7")


def build_report(artifact_dir: Path) -> dict[str, Any]:
    frozen = []
    for review_id in REVIEW_IDS:
        defense = validate_defense(artifact_dir=artifact_dir, review_id=review_id)
        role = validate_role(artifact_dir=artifact_dir, review_id=review_id)
        trigger = validate_trigger(artifact_dir=artifact_dir, review_id=review_id)
        if any(stage["status"] != "VALID" for stage in (defense, role, trigger)):
            raise ValueError("R8/R10/R10B formal artifact invalid")
        frozen.append((review_id, defense, role, trigger))
    teacher_doc = load_json(artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json")
    teachers = {row["review_id"]: row["teacher"] for row in teacher_doc["cases"]}
    integrity_doc = load_json(artifact_dir / "legacy_reference_integrity_audit_candidate_r1.json")
    integrity = {row["review_id"]: row for row in integrity_doc["rows"]}
    rows = []
    for review_id, defense, role, trigger in frozen:
        teacher = teachers[review_id]
        legacy = integrity[review_id]
        packet = load_json(artifact_dir / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{review_id}.json")
        defense_list = load_json(artifact_dir / "episode_defense_shortlists_candidate_r8" / f"{review_id}.json")
        defense_row = next(row for row in defense_list["candidate_rows"] if row["candidate_id"] == defense["selected_candidate_id"])
        teacher_candidate_ids = {row["candidate_id"] for row in teacher["matching_fixed_candidates"]}
        output = load_json(artifact_dir / "trigger_runs_candidate_r10b" / f"{review_id}.json")
        rows.append({
            "review_id": review_id,
            "as_of": packet["as_of"],
            "scenario": trigger["program_scenario"],
            "scenario_match": trigger["program_scenario"] == teacher["scenario"],
            "tactical_anchor_id": role["selected_tactical_cycle_anchor_id"],
            "teacher_anchor_candidate_ids": sorted(teacher_candidate_ids),
            "tactical_anchor_exact_match": role["selected_tactical_cycle_anchor_id"] in teacher_candidate_ids,
            "defense_exact_match": defense_row["source_date"] == teacher["stop_date"] and defense_row["price"] == teacher["stop_price"],
            "legacy_signal_date": teacher["signal_date"],
            "new_signal_disposition": trigger["signal_disposition"],
            "signal_permission_match": (trigger["signal_disposition"] == "TRIGGERED_SIGNAL_CANDIDATE") == (teacher["signal_date"] == packet["as_of"]),
            "legacy_trigger_path": teacher["trigger_path"],
            "new_trigger_path": trigger["proposed_trigger_path"],
            "trigger_path_exact_match": teacher["trigger_path"] == trigger["proposed_trigger_path"],
            "legacy_taiji_generation": teacher["taiji_generation"],
            "new_taiji_generation": trigger["taiji_generation"],
            "taiji_label_exact_match": teacher["taiji_generation"] == trigger["taiji_generation"],
            "legacy_quadrants": [teacher["large_quadrant"], teacher["small_quadrant"]],
            "new_quadrants": [trigger["large_quadrant"], trigger["small_quadrant"]],
            "quadrants_exact_match": [teacher["large_quadrant"], teacher["small_quadrant"]] == [trigger["large_quadrant"], trigger["small_quadrant"]],
            "new_nonpass_gates": {key: item["judgement"] for key, item in output["gate_assessments"].items() if item["judgement"] != "PASS"},
            "objective_risk_context": trigger["objective_risk_context"],
            "legacy_gate_stop_conflict": legacy["gate_evidence_conflicts_with_selected_stop"],
            "legacy_documented_second_pass": legacy["audit_structural_stop_rework"],
            "actual_next_open_fill_computed": False,
        })
    return {
        "report_version": VERSION,
        "scope": "TWO_CASE_TOUCHED_CALIBRATION_DIAGNOSTIC_ONLY",
        "case_count": len(rows),
        "scenario_matches": sum(row["scenario_match"] for row in rows),
        "tactical_anchor_exact_matches": sum(row["tactical_anchor_exact_match"] for row in rows),
        "defense_exact_matches": sum(row["defense_exact_match"] for row in rows),
        "signal_permission_matches": sum(row["signal_permission_match"] for row in rows),
        "trigger_path_exact_matches": sum(row["trigger_path_exact_match"] for row in rows),
        "taiji_label_exact_matches": sum(row["taiji_label_exact_match"] for row in rows),
        "quadrant_exact_matches": sum(row["quadrants_exact_match"] for row in rows),
        "teacher_used_in_formal_ai_input": False,
        "future_performance_used": False,
        "locked_reproduction_set_opened": False,
        "not_formal_reproduction_or_performance": True,
        "rows": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# R10/R10B 兩案舊AI最終紀錄對照（已觸及的校準案）", "",
        "正式AI輸出、原始檔及收據均先重驗，之後才離線對照舊最終紀錄。這兩案參與語意修訂，**不得當盲測重現率**；沒有隔日成交、持倉或績效。", "",
        f"- 情境：{report['scenario_matches']}/2；戰術週期錨：{report['tactical_anchor_exact_matches']}/2；小級防線：{report['defense_exact_matches']}/2",
        f"- 訊號日是否核准進場：{report['signal_permission_matches']}/2；觸發路徑逐字相同：{report['trigger_path_exact_matches']}/2",
        f"- 太極標籤逐字相同：{report['taiji_label_exact_matches']}/2；大小象限標籤逐字相同：{report['quadrant_exact_matches']}/2", "",
        "| 匿名案／訊號日 | 舊最終AI | R10B | 實際防線風險 | 主要差異 |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for row in report["rows"]:
        old = f"{row['legacy_trigger_path']}／{row['legacy_taiji_generation']}／{'-'.join(row['legacy_quadrants'])}"
        new = f"{row['new_signal_disposition']}／{row['new_trigger_path']}／{row['new_taiji_generation']}／{'-'.join(row['new_quadrants'])}"
        difference = ", ".join(f"{key}={value}" for key, value in row["new_nonpass_gates"].items()) or "必要gate全PASS；路徑／象限仍與舊紀錄不同"
        lines.append(f"| {row['review_id']}／{row['as_of']} | {old} | {new} | {row['objective_risk_context']['close_to_stop_percent']:.2f}% | {difference} |")
    lines.extend([
        "", "第一案雖已選到舊AI的4/14內層錨，R10B仍認為4/17→4/24→5/2→5/8的價格序列可能不是第一段，且以正式小級防線21.5573計風險，至已確認大級障礙的空間約1.28倍風險；舊來源在空間gate另有把訊號日低點誤稱防線的內部矛盾。R10B把尚未確認的5/3～5/4回檔計入第五段，仍需嚴格稽核代數是否過度確定，但即便改為UNKNOWN，V2也不會因此自動核准。", "",
        "第二案5/4訊號與舊紀錄在『可進場』及小級防線相同；R10B選`SMALL_REANCHOR_RELAUNCH`而非舊`DIRECT_TO_RIGHT`，並判大／小Q2而非舊Q4／Q1。這可能對後續加碼、出場與部位風險有影響，所以只能稱**部分交易行為重現**，不能稱整套邏輯相同。", "",
        "下一輪應先稽核第一案太極代數與第二案象限／路徑的課程證據，再在未參與修訂的正例及反例上凍結後盲測。不能為了把2案都變PASS而放寬V2，亦不能用期後績效決定當日正確答案。", "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    output = args.artifact_dir / "r10b_two_case_teacher_diagnostic_candidate_r1.json"
    write_new_or_identical(output, canonical_bytes(report))
    write_new_or_identical(output.with_suffix(".md"), render_markdown(report).encode("utf-8"))
    print(json.dumps({key: report[key] for key in (
        "case_count", "tactical_anchor_exact_matches", "defense_exact_matches",
        "signal_permission_matches", "trigger_path_exact_matches", "taiji_label_exact_matches", "quadrant_exact_matches",
    )}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
