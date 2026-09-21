"""Offline integrity audit of 14 legacy V2 calibration positives.

This is not a formal AI call or performance backtest.  It reads frozen legacy
ledgers only after the R9D formal outputs were frozen, and produces no inputs
for the model.  It distinguishes factual contradictions from old label aliases.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, write_new_or_identical


VERSION = "v2-core-legacy-reference-integrity-audit-v1"
LEDGER_DIR_BY_BATCH = {
    "889": ROOT / "reports" / "course_backtest" / "2023-09-04" / "historical_scan_formal_ai_v1_v2",
    "1029": ROOT / "reports" / "course_backtest" / "2024-02-02" / "historical_scan_2023h2_formal_ai_v1_v2",
}
CANONICAL_PATHS = {
    "MATURE_TREND_PULLBACK": {
        "Q4_TO_Q1_RELAUNCH", "SMALL_DOW_REVERSAL", "MA_HABIT_RECLAIM_WITH_STRUCTURE", "FIRST_SHALLOW_PULLBACK",
    },
    "MACRO_COPY_RESONANCE": {"BREAK_THEN_RETEST", "SMALL_REANCHOR_RELAUNCH", "DIRECT_TO_RIGHT"},
    "BEAR_REVERSAL_LEFT_RIGHT": {"LL", "LR", "RL", "RR", "DIRECT_TO_RIGHT"},
    "FRESH_Q1_EXPANSION": {"INITIAL_DESTRUCTIVE_EXPANSION", "FIRST_SHALLOW_CORRECTION_RELAUNCH", "CONTINUATION_ADD"},
}
CANONICAL_TAIJI = {"ANCHOR_LEG_1", "CORRECTION", "COPY_LEG_3", "COPY_LEG_5", "LATER_GENERATION"}
INDEPENDENT_STOP_RE = re.compile(r"independent stop\s+([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


def load_ledger_by_code(batch: str) -> dict[str, dict[str, Any]]:
    ledger_dir = LEDGER_DIR_BY_BATCH[batch]
    results: dict[str, dict[str, Any]] = {}
    for path in sorted(ledger_dir.glob("agent_*_ai_decisions.jsonl")):
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            code = str(row["code"])
            if code in results:
                raise ValueError(f"duplicate code in legacy ledgers: {batch}/{code}")
            results[code] = {"row": row, "ledger_file": path.name}
    return results


def build_report(artifact_dir: Path) -> dict[str, Any]:
    trace = load_json(artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json")
    labels = load_json(artifact_dir / "sealed" / "feasibility_probe_calibration_labels.json")
    by_id = {str(row["review_id"]): row for row in labels["rows"]}
    ledgers = {batch: load_ledger_by_code(batch) for batch in LEDGER_DIR_BY_BATCH}
    rows = []
    for item in trace["cases"]:
        review_id = item["review_id"]
        teacher = item["teacher"]
        label = by_id[review_id]
        batch = str(label["source_batch"])
        original = ledgers[batch][str(label["code"])]
        source = original["row"]
        matches = [row for row in source["v2"]["triggers"] if row["signal_date"] == teacher["signal_date"]]
        if len(matches) != 1:
            raise ValueError(f"legacy signal not unique in original ledger: {review_id}")
        trigger = matches[0]
        packet = load_json(artifact_dir / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{review_id}.json")
        signal_day = packet["daily_context_to_as_of"][-1]
        if signal_day["date"] != teacher["signal_date"]:
            raise ValueError(f"signal day packet mismatch: {review_id}")
        all_gate_evidence = "\n".join(
            text
            for gate in teacher["required_gates"].values()
            for text in gate.get("evidence", [])
        )
        independent_stops = [float(value) for value in INDEPENDENT_STOP_RE.findall(all_gate_evidence)]
        conflicting_stop = any(abs(value - float(teacher["stop_price"])) > 0.0001 for value in independent_stops)
        signal_low_used = any(abs(value - float(signal_day["low"])) <= 0.0001 for value in independent_stops)
        path = teacher["trigger_path"]
        generation = teacher["taiji_generation"]
        rows.append({
            "review_id": review_id,
            "source_batch": batch,
            "source_ledger_file": original["ledger_file"],
            "source_index": source["index"],
            "scenario": teacher["scenario"],
            "signal_date": teacher["signal_date"],
            "teacher_matches_ledger_trigger": all((
                trigger["scenario"] == teacher["scenario"],
                trigger["trigger_path"] == path,
                trigger["stop_date"] == teacher["stop_date"],
                abs(float(trigger["stop_price"]) - float(teacher["stop_price"])) <= 0.0001,
            )),
            "trigger_path": path,
            "trigger_path_in_current_v2_text": path in CANONICAL_PATHS[teacher["scenario"]],
            "taiji_generation": generation,
            "taiji_label_in_current_v2_text": generation in CANONICAL_TAIJI,
            "audit_structural_stop_rework": source.get("audit", {}).get("structural_stop_rework"),
            "independent_stop_claims_in_gate_evidence": independent_stops,
            "gate_evidence_conflicts_with_selected_stop": conflicting_stop,
            "conflicting_gate_stop_equals_signal_day_low": conflicting_stop and signal_low_used,
            "selected_stop": teacher["stop_price"],
            "signal_day_low": signal_day["low"],
            "future_performance_consulted": False,
        })
    return {
        "report_version": VERSION,
        "scope": "CALIBRATION_POSITIVE_REFERENCE_INTEGRITY_ONLY",
        "case_count": len(rows),
        "source_trigger_matches": sum(row["teacher_matches_ledger_trigger"] for row in rows),
        "noncanonical_path_label_count": sum(not row["trigger_path_in_current_v2_text"] for row in rows),
        "noncanonical_taiji_label_count": sum(not row["taiji_label_in_current_v2_text"] for row in rows),
        "documented_second_pass_count": sum(bool(row["audit_structural_stop_rework"]) for row in rows),
        "conflicting_stop_evidence_count": sum(row["gate_evidence_conflicts_with_selected_stop"] for row in rows),
        "future_performance_used": False,
        "formal_ai_calls": 0,
        "locked_reproduction_set_opened": False,
        "label_mismatch_is_not_automatic_trade_error": True,
        "rows": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 舊V2 14筆校準正例：參考紀錄完整性稽核", "",
        "此稽核在新R9D正式輸出凍結後，才離線讀取舊最終ledger、校準標籤與訊號日封包。沒有AI呼叫、期後績效、locked集或對原規則／舊ledger的修改。**舊紀錄是比較參考，不是先驗無誤的標準答案。**", "",
        f"- 舊teacher軌跡與來源ledger的情境／路徑／停損一致：{report['source_trigger_matches']}/{report['case_count']}",
        f"- 路徑名稱未逐字出現在目前V2情境路徑清單：{report['noncanonical_path_label_count']}筆（可能是舊別名，非自動違規）",
        f"- 太極世代名稱未逐字出現在目前V2 §6.1：{report['noncanonical_taiji_label_count']}筆（可能是舊別名或加碼狀態）",
        f"- 來源audit明記結構停損二次覆核：{report['documented_second_pass_count']}筆；未註記者不能反推絕無覆核",
        f"- gate證據的`independent stop`價與同筆正式stop不一致：{report['conflicting_stop_evidence_count']}筆", "",
        "| 匿名案 | 舊情境 | 路徑標籤 | 世代標籤 | 覆核註記 | 停損證據衝突 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        path = "同名" if row["trigger_path_in_current_v2_text"] else f"別名待核：{row['trigger_path']}"
        taiji = "同名" if row["taiji_label_in_current_v2_text"] else f"映射待核：{row['taiji_generation']}"
        lines.append(
            f"| {row['review_id']} | {row['scenario']} | {path} | {taiji} | "
            f"{row['audit_structural_stop_rework'] or '未記載'} | {'是' if row['gate_evidence_conflicts_with_selected_stop'] else '否'} |"
        )
    lines.extend([
        "", "名稱不一致須先映射原批次語意，不能直接當作交易邏輯不同；反過來，停損證據價與正式失效價衝突不能靠別名映射解決。正例完整性稽核不替代反例、一致性、locked驗證或扣成本後績效。", "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    output = args.artifact_dir / "legacy_reference_integrity_audit_candidate_r1.json"
    write_new_or_identical(output, canonical_bytes(report))
    write_new_or_identical(output.with_suffix(".md"), render_markdown(report).encode("utf-8"))
    print(json.dumps({key: report[key] for key in (
        "case_count", "source_trigger_matches", "noncanonical_path_label_count",
        "noncanonical_taiji_label_count", "documented_second_pass_count", "conflicting_stop_evidence_count",
    )}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
