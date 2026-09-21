"""Build calibration-only teacher traces for legacy pure-AI semantic distillation."""

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


ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)
TRACE_VERSION = "v2-core-legacy-teacher-decision-trace-r1-candidate"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _days(as_of: str, earlier: str | None) -> int | None:
    if earlier is None:
        return None
    return (date.fromisoformat(as_of) - date.fromisoformat(earlier)).days


def _candidate_view(candidate: dict[str, Any], as_of: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "evidence_option_id": candidate["evidence_option_id"],
        "basis": candidate["basis"],
        "scale": candidate["scale"],
        "direction": candidate["direction"],
        "status": candidate["status"],
        "start_date": candidate["start_date"],
        "confirmed_end_date": candidate["confirmed_end_date"],
        "start_recency_calendar_days": _days(as_of, candidate["start_date"]),
        "end_recency_calendar_days": _days(as_of, candidate["confirmed_end_date"]),
        "component_count": candidate["component_count"],
        "objective_metrics": candidate["objective_metrics"],
        "selection_reasons": candidate["selection_reasons"],
        "source_evidence_refs": candidate["source_evidence_refs"],
    }


def build_trace(artifact_dir: Path) -> dict[str, Any]:
    references = load_reference_cases(artifact_dir)
    input_manifest = load_json(
        artifact_dir / "legacy_anchor_alignment_input_manifest_candidate_r1.json"
    )
    input_rows = {str(row["review_id"]): row for row in input_manifest["rows"]}
    r4_report = load_json(artifact_dir / "legacy_role_alignment_report_candidate_r4.json")
    if not r4_report["legacy_answers_loaded"] or r4_report["completed_valid_case_count"] != 14:
        raise ValueError("R4 must be completely frozen and revealed before teacher trace creation")
    r4_cases = {str(row["review_id"]): row for row in r4_report["cases"]}

    cases: list[dict[str, Any]] = []
    scenario_pairs = Counter()
    teacher_basis = Counter()
    r4_basis = Counter()
    teacher_basis_by_scenario = Counter()
    trigger_paths = Counter()
    exact_selected = 0
    teacher_in_r4_alternatives = 0
    mismatched_teacher_in_r4_alternatives = 0

    structured = [
        reference
        for reference in references.values()
        if reference.positive and reference.structured_legacy_reference
    ]
    for reference in sorted(structured, key=lambda item: item.review_id):
        review_id = reference.review_id
        trigger = reference.legacy_trigger
        if trigger is None:
            raise ValueError(f"missing legacy trigger: {review_id}")
        manifest_row = input_rows[review_id]
        packet = load_json(
            artifact_dir
            / input_manifest["input_packet_directory"]
            / manifest_row["input_packet_file"]
        )
        if packet["blindness_contract"]["future_performance_included"] is not False:
            raise ValueError(f"future performance present in packet: {review_id}")
        anchor = trigger["macro_anchor"]
        teacher_candidates = [
            row
            for row in packet["candidate_pool"]
            if row["direction"] == anchor["direction"]
            and row["start_date"] == anchor["start"]
            and row["confirmed_end_date"] == anchor["end"]
        ]
        if not teacher_candidates:
            raise ValueError(f"teacher anchor absent from frozen candidate pool: {review_id}")
        r4_case = r4_cases[review_id]
        selected = r4_case["program_usable_scenario_working_anchor"]
        if selected is None:
            raise ValueError(f"R4 selected no working anchor: {review_id}")
        r4_output = load_json(
            artifact_dir
            / "legacy_role_alignment_runs_candidate_r4"
            / "outputs"
            / f"{review_id}.json"
        )
        alternatives = r4_output["roles"]["scenario_working"]["alternative_candidate_ids"]
        teacher_ids = {row["candidate_id"] for row in teacher_candidates}
        selected_exact = selected["candidate_id"] in teacher_ids
        teacher_alt = bool(teacher_ids.intersection(alternatives))
        exact_selected += int(selected_exact)
        teacher_in_r4_alternatives += int(teacher_alt)
        mismatched_teacher_in_r4_alternatives += int(teacher_alt and not selected_exact)
        scenario_pair = (trigger["scenario"], r4_case["program_derived_scenario"])
        scenario_pairs[scenario_pair] += 1
        r4_basis[selected["basis"]] += 1
        trigger_paths[str(trigger["trigger_path"])] += 1
        for candidate in teacher_candidates:
            teacher_basis[candidate["basis"]] += 1
            teacher_basis_by_scenario[(trigger["scenario"], candidate["basis"])] += 1

        cases.append(
            {
                "review_id": review_id,
                "as_of": packet["as_of"],
                "teacher": {
                    "scenario": trigger["scenario"],
                    "working_anchor": {
                        "direction": anchor["direction"],
                        "start_date": anchor["start"],
                        "end_date": anchor["end"],
                        "status": anchor["status"],
                        "ai_basis": anchor.get("ai_basis"),
                    },
                    "matching_fixed_candidates": [
                        _candidate_view(candidate, packet["as_of"])
                        for candidate in teacher_candidates
                    ],
                    "trigger_path": trigger["trigger_path"],
                    "signal_date": trigger["signal_date"],
                    "episode_or_add_candidate": trigger["episode_or_add_candidate"],
                    "taiji_generation": trigger["taiji_generation"],
                    "large_quadrant": trigger["large_quadrant"],
                    "small_quadrant": trigger["small_quadrant"],
                    "large_dow": trigger["large_dow"],
                    "small_dow": trigger["small_dow"],
                    "left_right": trigger["left_right"],
                    "stop_date": trigger["stop_date"],
                    "stop_price": trigger["stop_price"],
                    "invalidation": trigger["invalidation"],
                    "required_gates": trigger["required_gates"],
                    "evidence": trigger["evidence"],
                },
                "r4_misselection": {
                    "selected_working_candidate": _candidate_view(selected, packet["as_of"]),
                    "selected_exact_teacher_object": selected_exact,
                    "teacher_candidate_in_r4_alternatives": teacher_alt,
                    "alternative_candidate_ids": alternatives,
                    "program_scenario": r4_case["program_derived_scenario"],
                    "scenario_matches_teacher": r4_case["program_scenario_match"],
                },
                "pairwise_differences": {
                    "basis_changed": all(
                        candidate["basis"] != selected["basis"]
                        for candidate in teacher_candidates
                    ),
                    "start_date_delta_calendar_days": (
                        date.fromisoformat(selected["start_date"])
                        - date.fromisoformat(anchor["start"])
                    ).days,
                    "end_date_exact": selected["confirmed_end_date"] == anchor["end"],
                    "status_exact": selected["status"] in {anchor["status"], "CONFIRMED"}
                    if anchor["status"] == "COMPLETED"
                    else selected["status"] == anchor["status"],
                    "scenario_changed": trigger["scenario"]
                    != r4_case["program_derived_scenario"],
                },
            }
        )

    count = len(cases)
    return {
        "trace_version": TRACE_VERSION,
        "status": "CALIBRATION_TEACHER_TRACE_READY（校準教師軌跡可供語意萃取）",
        "scope": "CALIBRATION_STRUCTURED_POSITIVES_ONLY",
        "case_count": count,
        "formal_ai_calls": 0,
        "future_performance_used": False,
        "locked_reproduction_set_opened": False,
        "identity_used": False,
        "r4_outputs_modified": False,
        "teacher_answers_allowed_only_for_offline_specification_distillation": True,
        "teacher_answers_allowed_in_formal_ai_input": False,
        "summary": {
            "r4_exact_teacher_working_anchor_count": exact_selected,
            "r4_exact_teacher_working_anchor_percent": round(100 * exact_selected / count, 2),
            "teacher_candidate_in_r4_alternatives_count": teacher_in_r4_alternatives,
            "teacher_candidate_in_r4_alternatives_percent": round(
                100 * teacher_in_r4_alternatives / count, 2
            ),
            "mismatched_teacher_candidate_in_r4_alternatives_count": (
                mismatched_teacher_in_r4_alternatives
            ),
            "teacher_candidate_basis_counts_including_equivalent_scale_duplicates": dict(
                sorted(teacher_basis.items())
            ),
            "r4_selected_basis_counts": dict(sorted(r4_basis.items())),
            "teacher_basis_by_scenario": {
                f"{scenario}|{basis}": value
                for (scenario, basis), value in sorted(teacher_basis_by_scenario.items())
            },
            "teacher_to_r4_scenario_pairs": {
                f"{teacher}|{r4}": value
                for (teacher, r4), value in sorted(scenario_pairs.items())
            },
            "teacher_trigger_path_counts": dict(sorted(trigger_paths.items())),
        },
        "cases": cases,
    }


def render_markdown(trace: dict[str, Any]) -> str:
    summary = trace["summary"]
    lines = [
        "# 舊純 AI 教師決策軌跡 R1",
        "",
        f"- 狀態：`{trace['status']}`",
        f"- 校準正例：{trace['case_count']}案",
        f"- R4精確工作錨重現：{summary['r4_exact_teacher_working_anchor_count']}/{trace['case_count']}（{summary['r4_exact_teacher_working_anchor_percent']:.2f}%）",
        f"- 舊工作錨進入R4替代候選：{summary['teacher_candidate_in_r4_alternatives_count']}/{trace['case_count']}（{summary['teacher_candidate_in_r4_alternatives_percent']:.2f}%）",
        "- 正式AI呼叫：0；未讀取鎖定重現集、股票身分或未來績效。",
        "",
        "## 情境漂移",
        "",
        "| 舊純AI → R4 | 案數 |",
        "|---|---:|",
    ]
    for pair, count in summary["teacher_to_r4_scenario_pairs"].items():
        lines.append(f"| `{pair.replace('|', ' → ')}` | {count} |")
    lines.extend(
        [
            "",
            "## 逐案蒸餾索引",
            "",
            "| review id | 舊情境 | 舊工作錨 | 舊候選基底 | R4工作錨 | R4情境 | 錨一致 |",
            "|---|---|---|---|---|---|---:|",
        ]
    )
    for case in trace["cases"]:
        teacher = case["teacher"]
        old_anchor = teacher["working_anchor"]
        wrong = case["r4_misselection"]
        selected = wrong["selected_working_candidate"]
        bases = ", ".join(
            sorted({row["basis"] for row in teacher["matching_fixed_candidates"]})
        )
        lines.append(
            f"| `{case['review_id']}` | `{teacher['scenario']}` | "
            f"{old_anchor['start_date']}～{old_anchor['end_date'] or 'FORMING'} | "
            f"{bases} | {selected['start_date']}～{selected['confirmed_end_date'] or 'FORMING'} | "
            f"`{wrong['program_scenario']}` | "
            f"{'是' if wrong['selected_exact_teacher_object'] else '否'} |"
        )
    lines.extend(
        [
            "",
            "## 用途限制",
            "",
            "本檔只供離線萃取跨案例共同語意。不得把review id、個案日期、候選ID或teacher答案放入正式AI prompt；不得據此開啟locked reproduction或進行績效調參。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    trace = build_trace(args.artifact_dir)
    json_path = args.artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json"
    md_path = args.artifact_dir / "legacy_teacher_decision_trace_candidate_r1.md"
    json_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(trace), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": trace["status"],
                "case_count": trace["case_count"],
                "summary": trace["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
