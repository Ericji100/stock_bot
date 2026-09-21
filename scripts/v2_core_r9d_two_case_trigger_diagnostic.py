"""Post-freeze, offline teacher comparison of two R9D V2 signal decisions.

This script cannot initiate AI calls.  It validates immutable formal outputs
before opening the older pure-AI decision trace, and never feeds teacher or
subsequent price data back into a formal prompt.
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

from scripts.v2_core_anchor_hierarchy_choice_runner_r9 import validate_existing as validate_hierarchy
from scripts.v2_core_episode_defense_runner_r8 import validate_existing as validate_defense
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, write_new_or_identical
from scripts.v2_core_trigger_runner_r9d import validate_existing as validate_trigger


VERSION = "v2-core-r9d-two-case-trigger-diagnostic-r1"
REVIEW_IDS = ("FP-18b86f08f05563ff5886097b", "FP-38178c2820dd71aa800fd8d7")


def build_report(artifact_dir: Path) -> dict[str, Any]:
    frozen = []
    for review_id in REVIEW_IDS:
        defense = validate_defense(artifact_dir=artifact_dir, review_id=review_id)
        hierarchy = validate_hierarchy(artifact_dir=artifact_dir, review_id=review_id)
        trigger = validate_trigger(artifact_dir=artifact_dir, review_id=review_id)
        if any(stage["status"] != "VALID" for stage in (defense, hierarchy, trigger)):
            raise ValueError("R8/R9/R9D formal artifact failed validation")
        frozen.append((review_id, defense, hierarchy, trigger))
    teacher_doc = load_json(artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json")
    teachers = {row["review_id"]: row["teacher"] for row in teacher_doc["cases"]}
    rows = []
    for review_id, defense, hierarchy, trigger in frozen:
        teacher = teachers[review_id]
        packet = load_json(artifact_dir / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{review_id}.json")
        response = load_json(artifact_dir / "trigger_runs_candidate_r9d" / f"{review_id}.json")
        defense_list = load_json(artifact_dir / "episode_defense_shortlists_candidate_r8" / f"{review_id}.json")
        defense_row = next(row for row in defense_list["candidate_rows"] if row["candidate_id"] == defense["selected_candidate_id"])
        working_row = next(row for row in packet["candidate_pool"] if row["candidate_id"] == hierarchy["selected_working_anchor_id"])
        teacher_gate = teacher["required_gates"].get("EARLY_LOCATION_WITH_SPACE", {}).get("evidence", [])
        rows.append({
            "review_id": review_id,
            "as_of": packet["as_of"],
            "teacher_scenario": teacher["scenario"],
            "r9d_scenario": trigger["program_scenario"],
            "scenario_match": teacher["scenario"] == trigger["program_scenario"],
            "teacher_signal_date": teacher["signal_date"],
            "teacher_trigger_path": teacher["trigger_path"],
            "r9d_signal_disposition": trigger["signal_disposition"],
            "r9d_trigger_path": trigger["proposed_trigger_path"],
            "signal_decision_match": (trigger["signal_disposition"] == "TRIGGERED_SIGNAL_CANDIDATE") == (teacher["signal_date"] == packet["as_of"]),
            "teacher_working_anchor": teacher["working_anchor"],
            "r9d_working_anchor": {key: working_row[key] for key in ("candidate_id", "basis", "start_date", "confirmed_end_date", "status")},
            "teacher_episode_defense": {"date": teacher["stop_date"], "price": teacher["stop_price"]},
            "r9d_episode_defense": {"date": defense_row["source_date"], "price": defense_row["price"]},
            "defense_exact_match": defense_row["source_date"] == teacher["stop_date"] and defense_row["price"] == teacher["stop_price"],
            "r9d_nonpass_gates": {
                gate: {
                    "judgement": item["judgement"],
                    "explanation": item["explanation"],
                    "supporting_evidence_refs": item["supporting_evidence_refs"],
                }
                for gate, item in response["gate_assessments"].items() if item["judgement"] != "PASS"
            },
            "legacy_early_space_evidence": teacher_gate,
            "signal_day_low": packet["daily_context_to_as_of"][-1]["low"],
            "actual_entry_or_fill_computed": False,
        })
    return {
        "report_version": VERSION,
        "scope": "TWO_CASE_CALIBRATION_DIAGNOSTIC_ONLY",
        "case_count": len(rows),
        "scenario_matches": sum(row["scenario_match"] for row in rows),
        "defense_exact_matches": sum(row["defense_exact_match"] for row in rows),
        "signal_decision_matches": sum(row["signal_decision_match"] for row in rows),
        "formal_ai_used_teacher_answer": False,
        "future_performance_used": False,
        "locked_holdout_opened": False,
        "not_formal_reproduction_or_performance": True,
        "rows": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# R9D 兩案舊純 AI 觸發對照（校準診斷）", "",
        "正式 AI 輸出、原始檔與收據重驗後，才離線開啟舊 AI 答案。這兩案曾用於語意校準，不能作為盲測、正式重現率或正期望績效。沒有計算隔日成交、資金與損益。", "",
        f"- 情境類型相同：{report['scenario_matches']}/{report['case_count']}",
        f"- 小級交易防線日期與價格相同：{report['defense_exact_matches']}/{report['case_count']}",
        f"- 訊號日是否核准進場相同：{report['signal_decision_matches']}/{report['case_count']}", "",
        "| 案例 | 舊純AI | R9D正式判讀 | R9D未過條件 |",
        "| --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        gates = ", ".join(f"{gate}={item['judgement']}" for gate, item in row["r9d_nonpass_gates"].items())
        lines.append(f"| {row['review_id']} ({row['as_of']}) | {row['teacher_scenario']}／{row['teacher_trigger_path']} | {row['r9d_scenario']}／{row['r9d_signal_disposition']} | {gates} |")
    lines.extend([
        "", "## 因果差異與需查證事項", "",
        "1. 新生定錨案：舊AI工作錨從 2023-04-14 MACD 翻紅起算；R9D繼承的 R9B 工作錨從 2023-03-20 大級價格低點起算。R9D因而質疑是否仍屬第一段／初期，也以實際小級防線 21.5573 與 2022-04-11 已確認大級高點 22.8479 評估空間。這是級數與可用空間的實質分歧，不可只改標籤硬湊一致。",
        "2. 舊AI此案的 `EARLY_LOCATION_WITH_SPACE` 證據寫『independent stop 21.9206』，但它在同一筆的正式小級失效／停損價為 21.5573；21.9206 是訊號日低點。舊紀錄內部不一致，應將該證據標為待人工/課程稽核，不能直接把舊答案視為無誤真值。",
        "3. 大定錨複製案：舊AI以 2023-04-17 已確認高點 71.8489 當修正空方壓力，判大Q4／小Q1、准許直接右側；R9D要求更明確的修正空頭防線與共同向上策略，並將後續攻擊判得偏晚。V2 §6.1 明示第五段／後代不自動禁止，故 R9D 的 `NOT_Q3_OR_EXHAUSTED=FAIL` 理由應優先複核，不能僅因攻擊序號就排除。",
        "4. 舊AI此案使用 `COPY_LEG_2` 字樣，但現行 V2 §6.1 用 `COPY_LEG_3` 表示第一代複製；須先對齊世代命名，不能把名稱差異當作行情結構差異。",
        "", "## 下一步邊界", "",
        "先把『大級campaign、可交易工作錨、小級episode防線』三者及觸發用道氏線分開，要求每個非 PASS 條件指出 V2 原文依據和同級價格證據。保留原V2文本與所有舊判讀，不用未來損益倒推 PASS。完成這項語意校準後，再用未參與修訂的案例做盲化驗證；目前不擴成全量回測。", "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    output = args.artifact_dir / "r9d_two_case_trigger_teacher_diagnostic_candidate_r1.json"
    write_new_or_identical(output, canonical_bytes(report))
    write_new_or_identical(output.with_suffix(".md"), render_markdown(report).encode("utf-8"))
    print(json.dumps({key: report[key] for key in ("case_count", "scenario_matches", "defense_exact_matches", "signal_decision_matches")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
