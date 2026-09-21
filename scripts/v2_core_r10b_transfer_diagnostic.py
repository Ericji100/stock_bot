"""Offline diagnostic of the pre-registered R10B transfer pair.

The teacher record is loaded only after every available formal artifact has
been revalidated. Invalid formal output remains invalid; it is not repaired.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_anchor_family_retrieval_runner_r9 import validate_existing as validate_retrieval
from scripts.v2_core_anchor_hierarchy_choice_r9 import parse_raw_without_duplicate_keys, validate_and_choose
from scripts.v2_core_anchor_hierarchy_choice_runner_r9 import _inputs as hierarchy_inputs, validate_existing as validate_hierarchy
from scripts.v2_core_episode_defense_runner_r8 import validate_existing as validate_defense
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, write_new_or_identical
from scripts.v2_core_tactical_cycle_anchor_runner_r10 import validate_existing as validate_tactical
from scripts.v2_core_trigger_runner_r10b import validate_existing as validate_trigger


VERSION = "v2-core-r10b-pre-registered-transfer-diagnostic-r1"
SELECTION_FILE = "r10b_transfer_selection_candidate_r1.json"


def _ref_duplicates(response: dict[str, Any]) -> int:
    return sum(
        len(refs) - len(set(refs))
        for assessment in response.get("representative_assessments", {}).values()
        for refs in [assessment.get("supporting_evidence_refs", [])]
    )


def build_report(artifact_dir: Path) -> dict[str, Any]:
    selection = load_json(artifact_dir / SELECTION_FILE)
    if selection["review_ids"] != ["FP-a0a473840247b7fe32be8227", "FP-a46bf89ae6077ab9e326306c"]:
        raise ValueError("transfer selection changed")
    if not selection["not_sample_outcome_blind"] or selection["locked_reproduction_set_opened"]:
        raise ValueError("transfer selection scope changed")

    frozen: list[dict[str, Any]] = []
    for review_id in selection["review_ids"]:
        defense = validate_defense(artifact_dir=artifact_dir, review_id=review_id)
        retrieval = validate_retrieval(artifact_dir=artifact_dir, review_id=review_id)
        if defense["status"] != "VALID" or retrieval["status"] != "VALID":
            raise ValueError("R8/R9A precursor invalid")
        if review_id == selection["review_ids"][0]:
            hierarchy = validate_hierarchy(artifact_dir=artifact_dir, review_id=review_id)
            tactical = validate_tactical(artifact_dir=artifact_dir, review_id=review_id)
            trigger = validate_trigger(artifact_dir=artifact_dir, review_id=review_id)
            if any(item["status"] != "VALID" for item in (hierarchy, tactical, trigger)):
                raise ValueError("fresh transfer artifact invalid")
            frozen.append({
                "review_id": review_id,
                "formal_status": "VALID_FULL_SIGNAL_CHAIN",
                "defense": defense,
                "hierarchy": hierarchy,
                "tactical": tactical,
                "trigger": trigger,
            })
        else:
            invalid = artifact_dir / "anchor_hierarchy_choice_runs_candidate_r9" / f"{review_id}.invalid.raw"
            if not invalid.is_file():
                raise ValueError("macro invalid raw missing")
            raw_bytes = invalid.read_bytes()
            response = parse_raw_without_duplicate_keys(raw_bytes)
            packet, shortlist, defense_id, _defense_row, retrieval_result = hierarchy_inputs(artifact_dir, review_id)
            validation = validate_and_choose(
                response, packet=packet, defense=shortlist,
                defense_id=defense_id, retrieval=retrieval_result,
            )
            if validation["status"] != "INVALID" or _ref_duplicates(response) < 1:
                raise ValueError("macro hierarchy invalid cause changed")
            if not any("non-unique elements" in error for error in validation["errors"]):
                raise ValueError("macro hierarchy has another invalid cause")
            frozen.append({
                "review_id": review_id,
                "formal_status": "INVALID_R9B_DUPLICATE_EVIDENCE_REF",
                "defense": defense,
                "invalid_raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "duplicate_evidence_reference_count": _ref_duplicates(response),
                "model_proposed_relationship_unaccepted": response["relationship_class"],
                "model_proposed_working_anchor_unaccepted": response["selected_working_anchor_id"],
                "validation_errors": validation["errors"],
            })

    teachers = {
        item["review_id"]: item["teacher"]
        for item in load_json(artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json")["cases"]
    }
    rows = []
    for item in frozen:
        review_id = item["review_id"]
        teacher = teachers[review_id]
        stop_row = next(
            row for row in load_json(artifact_dir / "episode_defense_shortlists_candidate_r8" / f"{review_id}.json")["candidate_rows"]
            if row["candidate_id"] == item["defense"]["selected_candidate_id"]
        )
        row = {
            "review_id": review_id,
            "as_of": teacher["signal_date"],
            "formal_status": item["formal_status"],
            "legacy_scenario": teacher["scenario"],
            "legacy_signal_permission": True,
            "legacy_trigger_path": teacher["trigger_path"],
            "legacy_taiji_generation": teacher["taiji_generation"],
            "defense_exact_match": stop_row["source_date"] == teacher["stop_date"] and stop_row["price"] == teacher["stop_price"],
            "defense_source_date": stop_row["source_date"],
            "defense_price": stop_row["price"],
            "actual_next_open_fill_computed": False,
        }
        if item["formal_status"] == "VALID_FULL_SIGNAL_CHAIN":
            trigger = item["trigger"]
            row.update({
                "new_scenario": trigger["program_scenario"],
                "tactical_anchor_exact_match": item["tactical"]["selected_tactical_cycle_anchor_id"] in {
                    candidate["candidate_id"] for candidate in teacher["matching_fixed_candidates"]
                },
                "new_signal_disposition": trigger["signal_disposition"],
                "signal_permission_match": trigger["signal_disposition"] == "TRIGGERED_SIGNAL_CANDIDATE",
                "new_trigger_path": trigger["proposed_trigger_path"],
                "new_taiji_generation": trigger["taiji_generation"],
                "new_nonpass_gates": {name: judgement for name, judgement in trigger["gate_judgements"].items() if judgement != "PASS"},
                "close_to_stop_percent": trigger["objective_risk_context"]["close_to_stop_percent"],
            })
        else:
            row.update({
                "new_scenario": None,
                "new_signal_disposition": None,
                "signal_permission_match": None,
                "model_proposed_relationship_unaccepted": item["model_proposed_relationship_unaccepted"],
                "duplicate_evidence_reference_count": item["duplicate_evidence_reference_count"],
                "invalid_raw_sha256": item["invalid_raw_sha256"],
                "validation_errors": item["validation_errors"],
            })
        rows.append(row)
    return {
        "report_version": VERSION,
        "scope": "TWO_PRE_REGISTERED_UNTOUCHED_CALIBRATION_POSITIVES_NOT_LOCKED_OR_OUTCOME_BLIND",
        "case_count": len(rows),
        "full_chain_valid_count": sum(row["formal_status"] == "VALID_FULL_SIGNAL_CHAIN" for row in rows),
        "invalid_formal_count": sum(row["formal_status"] != "VALID_FULL_SIGNAL_CHAIN" for row in rows),
        "defense_exact_matches": sum(row["defense_exact_match"] for row in rows),
        "signal_permission_matches_among_full_chain_valid": sum(row.get("signal_permission_match") is True for row in rows),
        "signal_permission_comparable_count": sum(row["formal_status"] == "VALID_FULL_SIGNAL_CHAIN" for row in rows),
        "teacher_used_in_formal_ai_input": False,
        "future_performance_used": False,
        "locked_reproduction_set_opened": False,
        "not_formal_reproduction_or_performance": True,
        "rows": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    fresh, macro = report["rows"]
    return "\n".join([
        "# R10B 兩筆移轉測試：舊最終 AI 紀錄對照", "",
        "兩筆在本輪 R10/R10B 語意修訂前預先登記，正式提示詞只含匿名、當日以前資料。舊紀錄只在輸出凍結後用於離線比對；但兩筆仍是**校準正例**，選取曾按舊情境分層，並非真正盲測。", "",
        f"- 完整有效判讀鏈：{report['full_chain_valid_count']}/2；R9B 格式失效：{report['invalid_formal_count']}/2。",
        f"- 小級防線日期與價格：{report['defense_exact_matches']}/2 對上舊紀錄。",
        f"- 在可比較的 {report['signal_permission_comparable_count']} 筆中，進場權限 {report['signal_permission_matches_among_full_chain_valid']}/{report['signal_permission_comparable_count']} 對上；**不能把另一筆失效當成不進場**。", "",
        "| 案例／訊號日 | 舊最終 AI | 本輪有效結果 | 主要差異 |",
        "| --- | --- | --- | --- |",
        f"| {fresh['review_id']}／{fresh['as_of']} | {fresh['legacy_scenario']}；{fresh['legacy_trigger_path']}；可進場 | {fresh['new_scenario']}；{fresh['new_signal_disposition']} | 太極為 {fresh['new_taiji_generation']}，早期世代與位置／空間兩項 FAIL；固定防線風險 {fresh['close_to_stop_percent']:.4f}% |",
        f"| {macro['review_id']}／{macro['as_of']} | {macro['legacy_scenario']}；{macro['legacy_trigger_path']}；可進場 | R9B 未通過格式驗證，**無進場判讀** | 證據 ref 重複 {macro['duplicate_evidence_reference_count']} 筆，原始輸出保留；其中提出的 {macro['model_proposed_relationship_unaccepted']} 僅供診斷，不能視為合法情境 |",
        "", "第一筆已選到舊最終紀錄相同的 2023-02-14 形成中戰術錨、防線 2023-04-27@88.3777 與大小級 Q1/Q1；5/4 收盤也確實突破已確認小級高點。分歧不在資料缺漏或防線，而在 V2 對『第一段／第一次修正後早期』與追價位置的主觀判定。新 AI 指出 2/21→3/6→3/24→3/28→4/12→4/18→4/25→4/27 已有多輪確認轉折，故不認同舊紀錄的 `NEW_ANCHOR_GEN_1`。這項差異必須以課程證據稽核，不能因舊交易後來賺錢就判舊答案正確。", "",
        "第二筆 R9B 原始 JSON 有重複證據引用，被凍結的 `uniqueItems` 格式契約拒絕。原始檔沒有被刪重、覆寫或重跑；R10／R10B 未啟動。其未核准的 R9B 草案甚至選了 `FRESH_FORMING_UP_ANCHOR`，而舊紀錄是 `MACRO_COPY_RESONANCE`，顯示即便修復格式，也仍須獨立檢查結構語意，不能把格式修復當成交易重現。", "",
        "此測試沒有隔日開盤成交、持倉、加碼、出場、損益或正期望結果；也不能由一筆有效正例推估整體重現率。下一步應先釐清新生定錨的太極世代與位置語意，再以**獨立新版本**處理證據 ref 重複的傳輸問題，加入客觀反例並完成凍結樣本檢驗。", "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    output = args.artifact_dir / "r10b_transfer_diagnostic_candidate_r1.json"
    write_new_or_identical(output, canonical_bytes(report))
    write_new_or_identical(output.with_suffix(".md"), render_markdown(report).encode("utf-8"))
    print(json.dumps({key: report[key] for key in (
        "case_count", "full_chain_valid_count", "invalid_formal_count",
        "defense_exact_matches", "signal_permission_matches_among_full_chain_valid",
        "signal_permission_comparable_count",
    )}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
