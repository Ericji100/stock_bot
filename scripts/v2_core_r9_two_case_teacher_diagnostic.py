"""Post-freeze offline teacher diagnostic for R8 defense and R9 hierarchy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_anchor_family_retrieval_runner_r9 import validate_existing as validate_retrieval
from scripts.v2_core_anchor_hierarchy_choice_runner_r9 import validate_existing as validate_choice
from scripts.v2_core_episode_defense_runner_r8 import validate_existing as validate_defense
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, write_new_or_identical


VERSION = "v2-core-r9-two-case-teacher-diagnostic-r1"
REVIEW_IDS = ("FP-18b86f08f05563ff5886097b", "FP-38178c2820dd71aa800fd8d7")
RELATION_BY_TEACHER_SCENARIO = {
    "FRESH_Q1_EXPANSION": "FRESH_FORMING_UP_ANCHOR",
    "MACRO_COPY_RESONANCE": "COMPLETED_MACRO_UP_PARENT",
    "MATURE_TREND_PULLBACK": "MATURE_UP_CAMPAIGN",
    "BEAR_REVERSAL_LEFT_RIGHT": "ACTIVE_BEAR_CONTROL",
}


def build_report(artifact_dir: Path) -> dict[str, Any]:
    frozen = []
    for review_id in REVIEW_IDS:
        defense = validate_defense(artifact_dir=artifact_dir, review_id=review_id)
        retrieval = validate_retrieval(artifact_dir=artifact_dir, review_id=review_id)
        choice = validate_choice(artifact_dir=artifact_dir, review_id=review_id)
        if any(stage["status"] != "VALID" for stage in (defense, retrieval, choice)):
            raise ValueError("frozen R8/R9 stage not valid")
        frozen.append((review_id, defense, retrieval, choice))
    teacher_doc = load_json(artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json")
    teachers = {row["review_id"]: row["teacher"] for row in teacher_doc["cases"]}
    rows = []
    for review_id, defense, retrieval, choice in frozen:
        teacher = teachers[review_id]
        packet = load_json(artifact_dir / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{review_id}.json")
        defense_shortlist = load_json(artifact_dir / "episode_defense_shortlists_candidate_r8" / f"{review_id}.json")
        defense_row = next(row for row in defense_shortlist["candidate_rows"] if row["candidate_id"] == defense["selected_candidate_id"])
        anchor_row = next(row for row in packet["candidate_pool"] if row["candidate_id"] == choice["selected_working_anchor_id"])
        teacher_ids = {row["candidate_id"] for row in teacher["matching_fixed_candidates"]}
        representatives = {row["candidate_id"] for row in retrieval["group_representatives"].values()}
        rows.append({
            "review_id": review_id,
            "as_of": packet["as_of"],
            "teacher_scenario": teacher["scenario"],
            "r9_relationship_class": choice["relationship_class"],
            "relationship_class_family_match": choice["relationship_class"] == RELATION_BY_TEACHER_SCENARIO[teacher["scenario"]],
            "teacher_defense": {"date": teacher["stop_date"], "price": teacher["stop_price"]},
            "r8_defense": {"date": defense_row["source_date"], "price": defense_row["price"]},
            "defense_exact_match": defense_row["source_date"] == teacher["stop_date"] and defense_row["price"] == teacher["stop_price"],
            "teacher_working": {"candidate_ids": sorted(teacher_ids), "start_date": teacher["working_anchor"]["start_date"], "confirmed_end_date": teacher["working_anchor"]["end_date"]},
            "r9_working": {"candidate_id": anchor_row["candidate_id"], "basis": anchor_row["basis"], "start_date": anchor_row["start_date"], "confirmed_end_date": anchor_row["confirmed_end_date"]},
            "teacher_working_retrieved": bool(teacher_ids.intersection(representatives)),
            "working_exact_candidate_match": anchor_row["candidate_id"] in teacher_ids,
            "working_boundary_match": anchor_row["start_date"] == teacher["working_anchor"]["start_date"] and anchor_row["confirmed_end_date"] == teacher["working_anchor"]["end_date"],
            "trade_permission_granted": False,
        })
    return {
        "report_version": VERSION,
        "scope": "TWO_CASE_CALIBRATION_DIAGNOSTIC_ONLY",
        "case_count": len(rows),
        "defense_exact_matches": sum(row["defense_exact_match"] for row in rows),
        "teacher_working_retrieved_count": sum(row["teacher_working_retrieved"] for row in rows),
        "working_exact_candidate_matches": sum(row["working_exact_candidate_match"] for row in rows),
        "working_boundary_matches": sum(row["working_boundary_match"] for row in rows),
        "relationship_class_family_matches": sum(row["relationship_class_family_match"] for row in rows),
        "not_formal_reproduction_or_performance": True,
        "teacher_used_in_formal_ai_input": False,
        "future_performance_used": False,
        "rows": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# R8＋R9兩案舊純AI對照（校準診斷，非正式重現率）", "",
        "所有AI輸出／原始資料／收據先重驗，之後才離線讀舊AI答案。正式AI輸入沒有teacher、股票身分或未來績效。只有2案，且prompt已根據較早校準失敗蒸餾語意，不能當locked盲測或正期望證據。", "",
        f"- 交易防線日期＋價格：{report['defense_exact_matches']}/{report['case_count']}",
        f"- 舊AI工作錨進入R9族群代表：{report['teacher_working_retrieved_count']}/{report['case_count']}",
        f"- 最終工作錨固定候選相同：{report['working_exact_candidate_matches']}/{report['case_count']}",
        f"- 工作錨起訖相同：{report['working_boundary_matches']}/{report['case_count']}",
        f"- 粗層級類型同族：{report['relationship_class_family_matches']}/{report['case_count']}（不是正式情境或交易重現）", "",
        "| 案例 | 防線 | 舊AI工作錨 | R9工作錨 | 類型 |", "| --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        teacher, work = row["teacher_working"], row["r9_working"]
        t = f"{teacher['start_date']}～{teacher['confirmed_end_date'] or '形成中'}"
        w = f"{work['start_date']}～{work['confirmed_end_date'] or '形成中'} ({work['basis']})"
        lines.append(f"| {row['review_id']} | {'同日同價' if row['defense_exact_match'] else '不同'} | {t} | {w} | {row['r9_relationship_class']} |")
    lines.extend(["", "R9已消除『teacher候選沒被檢索』的問題，但尚未消除最後的級數／角色選擇分歧；本階段沒有交易權限、進場日期或績效結果。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    output = args.artifact_dir / "r9_two_case_teacher_diagnostic_candidate_r1.json"
    write_new_or_identical(output, canonical_bytes(report))
    write_new_or_identical(output.with_suffix(".md"), render_markdown(report).encode("utf-8"))
    print(json.dumps({key: report[key] for key in ("case_count", "defense_exact_matches", "teacher_working_retrieved_count", "working_exact_candidate_matches", "relationship_class_family_matches")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
