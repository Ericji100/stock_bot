"""Offline teacher coverage audit after teacher-blind R8 defense shortlists freeze."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY, OUTPUT_DIRECTORY, build_for_file
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, write_new_or_identical


VERSION = "v2-core-episode-defense-coverage-r8-candidate-r1"


def build_audit(artifact_dir: Path) -> dict[str, Any]:
    # Fully verify AS-OF candidate artifacts before reading offline teacher.
    frozen = {}
    for path in sorted((artifact_dir / OUTPUT_DIRECTORY).glob("FP-*.json")):
        source = artifact_dir / INPUT_DIRECTORY / path.name
        current = build_for_file(source)
        stored = load_json(path)
        if stored != current:
            raise ValueError(f"shortlist changed after freeze: {path.name}")
        frozen[stored["review_id"]] = stored
    trace = load_json(artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json")
    teacher_rows = {item["review_id"]: item["teacher"] for item in trace["cases"]}
    if set(frozen) != set(teacher_rows):
        raise ValueError("frozen/teacher case universes differ")
    rows = []
    for review_id in sorted(frozen):
        shortlist = frozen[review_id]
        teacher = teacher_rows[review_id]
        matching = [row for row in shortlist["candidate_rows"] if row["source_date"] == teacher["stop_date"] and row["price"] == teacher["stop_price"]]
        rows.append({
            "review_id": review_id,
            "as_of": shortlist["as_of"],
            "scenario": teacher["scenario"],
            "candidate_count": shortlist["candidate_count"],
            "teacher_stop_date": teacher["stop_date"],
            "teacher_stop_price": teacher["stop_price"],
            "teacher_stop_candidate_ids": [row["candidate_id"] for row in matching],
            "teacher_stop_visible": len(matching) == 1,
            "teacher_stop_is_most_recent": bool(matching and shortlist["candidate_rows"] and matching[0]["candidate_id"] == shortlist["candidate_rows"][0]["candidate_id"]),
            "teacher_stop_confirmed_at_signal_close": bool(matching and matching[0]["availability"] == "CONFIRMED_AT_SIGNAL_CLOSE"),
        })
    return {
        "audit_version": VERSION,
        "scope": "OFFLINE_CALIBRATION_REPRESENTABILITY_ONLY",
        "case_count": len(rows),
        "teacher_stop_visible_count": sum(row["teacher_stop_visible"] for row in rows),
        "teacher_stop_most_recent_count": sum(row["teacher_stop_is_most_recent"] for row in rows),
        "teacher_stop_signal_close_confirmation_count": sum(row["teacher_stop_confirmed_at_signal_close"] for row in rows),
        "candidate_count_min": min(row["candidate_count"] for row in rows),
        "candidate_count_max": max(row["candidate_count"] for row in rows),
        "teacher_answers_used_in_candidate_generation": False,
        "future_performance_used": False,
        "selected_defense_or_trade_permission": False,
        "interpretation": "Coverage means the correct stop is among candidates, not that AI/program selected it; most-recent-low shortcut fails at least one case.",
        "rows": rows,
    }


def render_markdown(audit: dict[str, Any]) -> str:
    exception_rows = [row for row in audit["rows"] if not row["teacher_stop_is_most_recent"]]
    lines = [
        "# R8交易episode防線候選覆蓋（離線校準，非判讀正確率）", "",
        "候選先由匿名AS-OF已確認小級低點獨立產生；所有封包與候選重驗後，才讀舊純AI防線作離線覆蓋稽核。180日是本輪候選視窗，不是正式停損規則；找不到合格候選時須擴大檢查，不能直接判不合格。", "",
        f"- teacher防線日期與價格可見：{audit['teacher_stop_visible_count']}/{audit['case_count']}",
        f"- teacher防線恰為最近一個已確認小級低點：{audit['teacher_stop_most_recent_count']}/{audit['case_count']}",
        f"- 防線小級低點在訊號日收盤才確認：{audit['teacher_stop_signal_close_confirmation_count']}/{audit['case_count']}",
        f"- 每案候選數：{audit['candidate_count_min']}～{audit['candidate_count_max']}", "",
        "最近低點不能自動當防線；反例：", "",
    ]
    for row in exception_rows:
        lines.append(f"- `{row['review_id']}`：teacher防線為{row['teacher_stop_date']}，候選清單另有更近小級低點，仍須比較級數與控制權。")
    lines.extend(["", "覆蓋不等於正確選擇、情境重現、交易權限或正期望；不得將teacher日期餵給正式AI。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    audit = build_audit(args.artifact_dir)
    output = args.artifact_dir / "episode_defense_coverage_candidate_r8.json"
    write_new_or_identical(output, canonical_bytes(audit))
    write_new_or_identical(output.with_suffix(".md"), render_markdown(audit).encode("utf-8"))
    print(json.dumps({key: audit[key] for key in ("case_count", "teacher_stop_visible_count", "teacher_stop_most_recent_count", "teacher_stop_signal_close_confirmation_count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
