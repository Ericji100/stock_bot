"""Audit calibration teacher anchors against fixed AS-OF candidate alternatives.

This is an offline specification-distillation artifact.  It never opens the
locked reproduction set and never uses identity or post-AS-OF performance.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import json
import math
from pathlib import Path
import statistics
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)
AUDIT_VERSION = "v2-core-legacy-teacher-pairwise-audit-r1-candidate"
TOP_K = 3


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _basis_family(basis: str) -> str:
    if basis.startswith("MACD_"):
        return "MACD_REGIME"
    if basis.startswith("PIVOT_"):
        return "PIVOT_CAMPAIGN"
    return basis.split("_", 1)[0]


def _day_delta(left: str, right: str) -> int:
    return abs((date.fromisoformat(left) - date.fromisoformat(right)).days)


def _relative_delta(left: float | int | None, right: float | int | None) -> float:
    if left is None or right is None:
        return 0.0 if left is right else 1.0
    denominator = max(abs(float(left)), 1.0)
    return abs(float(left) - float(right)) / denominator


def _distance_components(
    teacher: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, float]:
    """Return a fixed, outcome-neutral descriptor distance.

    The weights are declared here before inspecting results.  They prefer the
    same direction/lifecycle, then compare basis family, scale, temporal span,
    and AS-OF-only path descriptors.  This score is diagnostic, not a trading
    rule and not a learned objective.
    """

    direction = 100.0 if teacher["direction"] != candidate["direction"] else 0.0
    lifecycle = 10.0 if teacher["status"] != candidate["status"] else 0.0
    basis_family = (
        4.0 if _basis_family(teacher["basis"]) != _basis_family(candidate["basis"]) else 0.0
    )
    scale = 2.0 if teacher["scale"] != candidate["scale"] else 0.0
    start = min(_day_delta(teacher["start_date"], candidate["start_date"]) / 21.0, 10.0)

    teacher_end = teacher["confirmed_end_date"]
    candidate_end = candidate["confirmed_end_date"]
    if teacher_end is None and candidate_end is None:
        end = 0.0
    elif teacher_end is None or candidate_end is None:
        end = 6.0
    else:
        end = min(_day_delta(teacher_end, candidate_end) / 21.0, 10.0)

    teacher_metrics = teacher["objective_metrics"]
    candidate_metrics = candidate["objective_metrics"]
    bars = min(
        _relative_delta(
            teacher_metrics.get("trading_bars"), candidate_metrics.get("trading_bars")
        )
        * 2.0,
        4.0,
    )
    path_metrics = 0.0
    for key in (
        "directional_move_atr_at_start",
        "directional_move_pct",
        "max_countermove_pct",
        "path_efficiency",
    ):
        path_metrics += min(
            _relative_delta(teacher_metrics.get(key), candidate_metrics.get(key)) * 0.5,
            1.0,
        )
    components = min(
        abs(int(teacher["component_count"]) - int(candidate["component_count"])) * 0.25,
        2.0,
    )
    return {
        "direction": round(direction, 6),
        "lifecycle": round(lifecycle, 6),
        "basis_family": round(basis_family, 6),
        "scale": round(scale, 6),
        "start_date": round(start, 6),
        "end_date": round(end, 6),
        "trading_bars": round(bars, 6),
        "as_of_path_descriptors": round(path_metrics, 6),
        "component_count": round(components, 6),
    }


def _candidate_distance(
    teacher_candidates: list[dict[str, Any]], candidate: dict[str, Any]
) -> tuple[float, dict[str, float], str]:
    scored = []
    for teacher in teacher_candidates:
        components = _distance_components(teacher, candidate)
        scored.append((sum(components.values()), components, teacher["candidate_id"]))
    score, components, teacher_id = min(scored, key=lambda row: (row[0], row[2]))
    return round(score, 6), components, teacher_id


def _candidate_view(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "basis": candidate["basis"],
        "basis_family": _basis_family(candidate["basis"]),
        "scale": candidate["scale"],
        "direction": candidate["direction"],
        "status": candidate["status"],
        "start_date": candidate["start_date"],
        "confirmed_end_date": candidate["confirmed_end_date"],
        "component_count": candidate["component_count"],
        "objective_metrics": candidate["objective_metrics"],
    }


def _comparison(
    teacher: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    teacher_metrics = teacher["objective_metrics"]
    candidate_metrics = candidate["objective_metrics"]
    return {
        "basis_family_changed": _basis_family(teacher["basis"])
        != _basis_family(candidate["basis"]),
        "scale_changed": teacher["scale"] != candidate["scale"],
        "direction_changed": teacher["direction"] != candidate["direction"],
        "lifecycle_changed": teacher["status"] != candidate["status"],
        "start_date_signed_delta_days": (
            date.fromisoformat(candidate["start_date"])
            - date.fromisoformat(teacher["start_date"])
        ).days,
        "end_presence_changed": (teacher["confirmed_end_date"] is None)
        != (candidate["confirmed_end_date"] is None),
        "trading_bars_signed_delta": int(candidate_metrics["trading_bars"])
        - int(teacher_metrics["trading_bars"]),
        "directional_move_pct_signed_delta": round(
            float(candidate_metrics["directional_move_pct"])
            - float(teacher_metrics["directional_move_pct"]),
            6,
        ),
        "max_countermove_pct_signed_delta": round(
            float(candidate_metrics["max_countermove_pct"])
            - float(teacher_metrics["max_countermove_pct"]),
            6,
        ),
        "path_efficiency_signed_delta": round(
            float(candidate_metrics["path_efficiency"])
            - float(teacher_metrics["path_efficiency"]),
            6,
        ),
    }


def _mean(values: list[float | int]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def build_audit(artifact_dir: Path) -> dict[str, Any]:
    trace = load_json(artifact_dir / "legacy_teacher_decision_trace_candidate_r1.json")
    manifest = load_json(
        artifact_dir / "legacy_anchor_alignment_input_manifest_candidate_r1.json"
    )
    if trace["case_count"] != 14 or trace["locked_reproduction_set_opened"]:
        raise ValueError("expected the sealed 14-case calibration teacher trace")
    manifest_rows = {str(row["review_id"]): row for row in manifest["rows"]}

    cases: list[dict[str, Any]] = []
    r4_nearest_count = 0
    teacher_macd_r4_pivot = 0
    completed_teacher_r4_forming = 0
    r4_later_start = 0
    r4_earlier_start = 0
    scenario_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "case_count": 0,
            "r4_exact_count": 0,
            "r4_scenario_match_count": 0,
            "r4_basis_family_change_count": 0,
            "r4_lifecycle_change_count": 0,
            "r4_start_date_signed_delta_days": [],
            "r4_trading_bars_signed_delta": [],
        }
    )
    transition_counts = Counter()

    for trace_case in trace["cases"]:
        review_id = str(trace_case["review_id"])
        row = manifest_rows[review_id]
        packet = load_json(
            artifact_dir / manifest["input_packet_directory"] / row["input_packet_file"]
        )
        if packet["blindness_contract"]["future_performance_included"] is not False:
            raise ValueError(f"future performance present in {review_id}")
        teacher_ids = {
            candidate["candidate_id"]
            for candidate in trace_case["teacher"]["matching_fixed_candidates"]
        }
        teacher_candidates = [
            candidate for candidate in packet["candidate_pool"] if candidate["candidate_id"] in teacher_ids
        ]
        if len(teacher_candidates) != len(teacher_ids):
            raise ValueError(f"teacher candidate lookup failed: {review_id}")

        alternatives = []
        for candidate in packet["candidate_pool"]:
            if candidate["candidate_id"] in teacher_ids:
                continue
            score, components, matched_teacher_id = _candidate_distance(
                teacher_candidates, candidate
            )
            teacher_match = next(
                item for item in teacher_candidates if item["candidate_id"] == matched_teacher_id
            )
            alternatives.append(
                {
                    "distance": score,
                    "distance_components": components,
                    "compared_teacher_candidate_id": matched_teacher_id,
                    "candidate": _candidate_view(candidate),
                    "differences": _comparison(teacher_match, candidate),
                }
            )
        alternatives.sort(
            key=lambda item: (item["distance"], item["candidate"]["candidate_id"])
        )
        for rank, alternative in enumerate(alternatives, start=1):
            alternative["rank"] = rank

        r4_id = trace_case["r4_misselection"]["selected_working_candidate"]["candidate_id"]
        if r4_id in teacher_ids:
            r4_audit = {
                "is_teacher_equivalent": True,
                "rank_among_non_teacher_candidates": None,
                "distance": 0.0,
                "distance_components": {},
                "candidate": trace_case["r4_misselection"]["selected_working_candidate"],
                "differences": {},
            }
        else:
            r4_audit = next(
                alternative for alternative in alternatives if alternative["candidate"]["candidate_id"] == r4_id
            )
            r4_audit = {"is_teacher_equivalent": False, **r4_audit}
            r4_nearest_count += int(r4_audit["rank"] == 1)

        scenario = trace_case["teacher"]["scenario"]
        stats = scenario_stats[scenario]
        stats["case_count"] += 1
        stats["r4_exact_count"] += int(r4_id in teacher_ids)
        stats["r4_scenario_match_count"] += int(
            trace_case["r4_misselection"]["scenario_matches_teacher"]
        )
        if r4_id not in teacher_ids:
            differences = r4_audit["differences"]
            stats["r4_basis_family_change_count"] += int(
                differences["basis_family_changed"]
            )
            stats["r4_lifecycle_change_count"] += int(differences["lifecycle_changed"])
            stats["r4_start_date_signed_delta_days"].append(
                differences["start_date_signed_delta_days"]
            )
            stats["r4_trading_bars_signed_delta"].append(
                differences["trading_bars_signed_delta"]
            )

            matched_teacher = next(
                item
                for item in teacher_candidates
                if item["candidate_id"] == r4_audit["compared_teacher_candidate_id"]
            )
            r4_candidate = next(
                item for item in packet["candidate_pool"] if item["candidate_id"] == r4_id
            )
            teacher_family = _basis_family(matched_teacher["basis"])
            r4_family = _basis_family(r4_candidate["basis"])
            transition_counts[(teacher_family, r4_family)] += 1
            teacher_macd_r4_pivot += int(
                teacher_family == "MACD_REGIME" and r4_family == "PIVOT_CAMPAIGN"
            )
            completed_teacher_r4_forming += int(
                matched_teacher["status"] == "CONFIRMED" and r4_candidate["status"] == "FORMING"
            )
            delta = differences["start_date_signed_delta_days"]
            r4_later_start += int(delta > 0)
            r4_earlier_start += int(delta < 0)

        cases.append(
            {
                "review_id": review_id,
                "as_of": trace_case["as_of"],
                "teacher_scenario": scenario,
                "r4_program_scenario": trace_case["r4_misselection"]["program_scenario"],
                "r4_scenario_matches_teacher": trace_case["r4_misselection"][
                    "scenario_matches_teacher"
                ],
                "teacher_trigger_path": trace_case["teacher"]["trigger_path"],
                "teacher_ai_basis": trace_case["teacher"]["working_anchor"]["ai_basis"],
                "teacher_required_gates": trace_case["teacher"]["required_gates"],
                "teacher_equivalent_candidates": [
                    _candidate_view(candidate) for candidate in teacher_candidates
                ],
                "r4_selected_audit": r4_audit,
                "nearest_non_teacher_candidates": alternatives[:TOP_K],
            }
        )

    scenario_summary = {}
    for scenario, stats in sorted(scenario_stats.items()):
        scenario_summary[scenario] = {
            "case_count": stats["case_count"],
            "r4_exact_count": stats["r4_exact_count"],
            "r4_scenario_match_count": stats["r4_scenario_match_count"],
            "r4_basis_family_change_count": stats["r4_basis_family_change_count"],
            "r4_lifecycle_change_count": stats["r4_lifecycle_change_count"],
            "mean_r4_start_date_signed_delta_days": _mean(
                stats["r4_start_date_signed_delta_days"]
            ),
            "mean_r4_trading_bars_signed_delta": _mean(
                stats["r4_trading_bars_signed_delta"]
            ),
        }

    mismatch_count = sum(
        not case["r4_selected_audit"]["is_teacher_equivalent"] for case in cases
    )
    observed_patterns = [
        {
            "pattern": "MACD teacher → pivot R4",
            "count": teacher_macd_r4_pivot,
            "denominator": mismatch_count,
            "interpretation": "R4 frequently replaced the teacher's broader MACD-regime span with a pivot campaign.",
        },
        {
            "pattern": "confirmed teacher → forming R4",
            "count": completed_teacher_r4_forming,
            "denominator": mismatch_count,
            "interpretation": "R4 sometimes replaced a completed parent/copy anchor with the active child leg.",
        },
        {
            "pattern": "R4 start later than teacher",
            "count": r4_later_start,
            "denominator": mismatch_count,
            "interpretation": "A later start indicates recency bias toward a smaller/recent structure.",
        },
        {
            "pattern": "R4 start earlier than teacher",
            "count": r4_earlier_start,
            "denominator": mismatch_count,
            "interpretation": "An earlier start indicates campaign over-expansion that can absorb a fresh anchor.",
        },
    ]

    return {
        "audit_version": AUDIT_VERSION,
        "status": "CALIBRATION_PAIRWISE_AUDIT_READY（校準成對語意稽核完成）",
        "scope": "CALIBRATION_STRUCTURED_POSITIVES_ONLY",
        "purpose": "OFFLINE_TEACHER_SEMANTIC_DISTILLATION_NOT_A_TRADING_RULE",
        "case_count": len(cases),
        "formal_ai_calls": 0,
        "future_performance_used": False,
        "locked_reproduction_set_opened": False,
        "identity_used": False,
        "teacher_answers_allowed_in_formal_ai_input": False,
        "distance_contract": {
            "version": "OUTCOME_NEUTRAL_DESCRIPTOR_DISTANCE_V1",
            "top_k": TOP_K,
            "uses_only_as_of_fixed_candidate_fields": True,
            "uses_future_return_or_trade_outcome": False,
            "is_a_trading_rule": False,
            "weights": {
                "direction_mismatch": 100.0,
                "lifecycle_mismatch": 10.0,
                "basis_family_mismatch": 4.0,
                "scale_mismatch": 2.0,
                "start_date_distance": "min(abs(days)/21,10)",
                "end_presence_mismatch": 6.0,
                "end_date_distance": "min(abs(days)/21,10)",
                "trading_bars_relative_distance": "min(relative*2,4)",
                "four_path_descriptors": "sum(min(relative*0.5,1))",
                "component_count_distance": "min(abs(delta)*0.25,2)",
            },
        },
        "summary": {
            "r4_teacher_equivalent_count": len(cases) - mismatch_count,
            "r4_mismatch_count": mismatch_count,
            "r4_is_nearest_non_teacher_count": r4_nearest_count,
            "teacher_macd_r4_pivot_count": teacher_macd_r4_pivot,
            "completed_teacher_r4_forming_count": completed_teacher_r4_forming,
            "r4_later_start_than_teacher_count": r4_later_start,
            "r4_earlier_start_than_teacher_count": r4_earlier_start,
            "basis_family_transition_counts": {
                f"{teacher}->{r4}": count
                for (teacher, r4), count in sorted(transition_counts.items())
            },
            "by_teacher_scenario": scenario_summary,
            "observed_patterns_not_yet_rules": observed_patterns,
        },
        "cases": cases,
    }


def render_markdown(audit: dict[str, Any]) -> str:
    summary = audit["summary"]
    lines = [
        "# 舊純 AI 教師定錨成對語意稽核 R1",
        "",
        f"- 狀態：`{audit['status']}`",
        f"- 校準正例：{audit['case_count']}案",
        f"- R4精確重現：{summary['r4_teacher_equivalent_count']}/{audit['case_count']}",
        f"- R4錯選：{summary['r4_mismatch_count']}/{audit['case_count']}",
        "- 正式AI呼叫：0；沒有使用股票身分、未來績效或鎖定重現集。",
        "- 本報告只萃取教師語意，不是交易規則，也不能據此宣稱正期望。",
        "",
        "## 系統性偏移",
        "",
        f"- 舊教師為MACD週期、R4改選樞紐：{summary['teacher_macd_r4_pivot_count']}案。",
        f"- 舊教師為已完成錨、R4改選作用中錨：{summary['completed_teacher_r4_forming_count']}案。",
        f"- R4起點晚於教師（偏近期小結構）：{summary['r4_later_start_than_teacher_count']}案。",
        f"- R4起點早於教師（過度擴張大結構）：{summary['r4_earlier_start_than_teacher_count']}案。",
        "",
        "## 依舊情境統計",
        "",
        "| 舊純AI情境 | 案數 | 錨精確 | 情境一致 | 基底族群改變 | 生命週期改變 | R4起點差（日） | K棒數差 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario, row in summary["by_teacher_scenario"].items():
        lines.append(
            f"| `{scenario}` | {row['case_count']} | {row['r4_exact_count']} | "
            f"{row['r4_scenario_match_count']} | {row['r4_basis_family_change_count']} | "
            f"{row['r4_lifecycle_change_count']} | {row['mean_r4_start_date_signed_delta_days']} | "
            f"{row['mean_r4_trading_bars_signed_delta']} |"
        )
    lines.extend(
        [
            "",
            "正的起點差代表R4選得更晚、更靠近當下；負值代表R4把起點往更早處擴張。",
            "",
            "## 逐案比較",
            "",
            "| review id | 舊情境 | R4情境 | 教師錨 | R4錨 | R4非教師排名 | 起點差 | 情境一致 |",
            "|---|---|---|---|---|---:|---:|---:|",
        ]
    )
    for case in audit["cases"]:
        teacher = case["teacher_equivalent_candidates"][0]
        r4 = case["r4_selected_audit"]
        r4_candidate = r4["candidate"]
        rank = r4.get("rank", "教師等價")
        delta = r4.get("differences", {}).get("start_date_signed_delta_days", 0)
        teacher_label = (
            f"{teacher['basis']}/{teacher['status']} "
            f"{teacher['start_date']}→{teacher['confirmed_end_date'] or '作用中'}"
        )
        r4_label = (
            f"{r4_candidate['basis']}/{r4_candidate['status']} "
            f"{r4_candidate['start_date']}→{r4_candidate['confirmed_end_date'] or '作用中'}"
        )
        lines.append(
            f"| `{case['review_id']}` | `{case['teacher_scenario']}` | "
            f"`{case['r4_program_scenario']}` | {teacher_label} | {r4_label} | "
            f"{rank} | {delta} | {'是' if case['r4_scenario_matches_teacher'] else '否'} |"
        )
    lines.extend(
        [
            "",
            "## 對下一版的限制",
            "",
            "1. 下一版必須先重現教師的角色層次：父代／複製錨、當前修正與進場小結構不得互相替代。",
            "2. MACD週期與樞紐候選是不同證據基底；不能固定偏好最近、最有效率或最長的樞紐片段。",
            "3. 這14案只能用於規格蒸餾；凍結下一版後，才可一次開啟鎖定重現集做真正盲測。",
            "4. 在單輪舊純AI重現未達標前，不進入三輪一致性，也不做績效背書。",
            "",
            "詳細的前三個客觀近鄰、固定距離分解、教師必要條件與逐案差異保存在同名JSON。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument(
        "--output-json",
        type=Path,
        default=ARTIFACT_DIR / "legacy_teacher_pairwise_audit_candidate_r1.json",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=ARTIFACT_DIR / "legacy_teacher_pairwise_audit_candidate_r1.md",
    )
    args = parser.parse_args()
    audit = build_audit(args.artifact_dir)
    args.output_json.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output_md.write_text(render_markdown(audit), encoding="utf-8")
    print(json.dumps(audit["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
