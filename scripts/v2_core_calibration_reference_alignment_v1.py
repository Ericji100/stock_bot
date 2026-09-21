"""Audit frozen AI rounds against calibration-only legacy V2 references.

This is a post-hoc calibration audit.  It never feeds legacy answers back into an
AI prompt and it does not open the locked reproduction set.  The purpose is to
detect whether the current staged representation is even capable of expressing
the structural object used by the earlier formal AI judgement.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SELECTED_REVIEW_IDS = (
    "FP-b13c903946105e6b32eaf8d5",
    "FP-d79297cd1f01c1600d27cb75",
    "FP-725be50d02258221dd227b2f",
    "FP-46f54e49cda2a16d69f9c6d6",
    "FP-a13d10a6d495943b9196dfc6",
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(100.0 * numerator / denominator, 2)


def _status_code(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).split("（", 1)[0]


def _trade_action(permission: str | None) -> str:
    return "TRADE" if permission == "TRADE_APPROVED" else "NO_TRADE"


def _calendar_delta_days(left: str | None, right: str | None) -> int | None:
    if not left or not right:
        return None
    return (date.fromisoformat(left) - date.fromisoformat(right)).days


def _find_controlling_anchor(stage_b: dict[str, Any]) -> dict[str, Any] | None:
    anchor_id = stage_b.get("shared_structure", {}).get("controlling_anchor_id")
    if not anchor_id:
        return None
    for anchor in stage_b.get("anchors", []):
        if anchor.get("anchor_id") == anchor_id:
            return anchor
    return None


def _exact_anchor_match(candidate: dict[str, Any], reference: dict[str, Any]) -> bool:
    return (
        candidate.get("direction") == reference.get("direction")
        and candidate.get("start_date") == reference.get("start")
        and candidate.get("confirmed_end_date") == reference.get("end")
    )


def _load_stage_atom_rounds(
    artifact_dir: Path,
    run_directory: str,
    stage_directory: str,
    review_id: str,
    collection_key: str,
    atom_key: str,
    required_rounds: int = 3,
) -> dict[str, list[str | None]]:
    by_candidate: dict[str, list[str | None]] = {}
    for round_number in range(1, required_rounds + 1):
        path = (
            artifact_dir
            / run_directory
            / f"round_{round_number}"
            / stage_directory
            / f"{review_id}.json"
        )
        if not path.exists():
            continue
        payload = load_json(path)
        for item in payload.get(collection_key, []):
            candidate_id = str(item["candidate_id"])
            by_candidate.setdefault(candidate_id, [None] * required_rounds)
            by_candidate[candidate_id][round_number - 1] = item[atom_key].get("judgement")
    return by_candidate


@dataclass(frozen=True)
class ReferenceCase:
    review_id: str
    case_role: str
    intended_scenario: str
    source_classification: str
    expected_permission: str
    legacy_trigger: dict[str, Any] | None

    @property
    def positive(self) -> bool:
        return self.case_role == "POSITIVE_REFERENCE"

    @property
    def structured_legacy_reference(self) -> bool:
        return self.legacy_trigger is not None


def load_reference_cases(artifact_dir: Path) -> dict[str, ReferenceCase]:
    label_path = artifact_dir / "sealed" / "feasibility_probe_calibration_labels.json"
    daily_path = artifact_dir / "calibration_daily_cases_v2.jsonl"
    labels = load_json(label_path)["rows"]
    daily_rows = load_jsonl(daily_path)
    daily_by_key = {
        (str(row["packet_sha256"]), str(row["as_of"])): row for row in daily_rows
    }
    references: dict[str, ReferenceCase] = {}
    for label in labels:
        if label.get("future_performance_included") is not False:
            raise ValueError(f"future performance flag is not false: {label['review_id']}")
        key = (str(label["source_packet_sha256"]), str(label["as_of"]))
        source = daily_by_key.get(key)
        if source is None and not str(label["source_classification"]).startswith(
            "PARTIAL_REFERENCE_ONLY"
        ):
            raise ValueError(f"missing calibration daily source for {label['review_id']}")
        if source is not None and source.get("future_performance_included") is not False:
            raise ValueError(f"daily source contains future performance: {label['review_id']}")
        expected_permission = (
            _status_code(source.get("expected_permission"))
            if source is not None
            else ("TRADE_APPROVED" if label["case_role"] == "POSITIVE_REFERENCE" else "WAIT")
        )
        references[str(label["review_id"])] = ReferenceCase(
            review_id=str(label["review_id"]),
            case_role=str(label["case_role"]),
            intended_scenario=str(label["intended_scenario"]),
            source_classification=str(label["source_classification"]),
            expected_permission=expected_permission or "UNKNOWN",
            legacy_trigger=source.get("legacy_v2_trigger") if source is not None else None,
        )
    return references


def _r8_alignment(artifact_dir: Path, references: dict[str, ReferenceCase]) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = []
    case_rows: list[dict[str, Any]] = []
    for round_number in range(1, 4):
        exact_permission = action_matches = 0
        positive_trade = nonpositive_no_trade = 0
        positive_scenario = 0
        anchor_direction = anchor_start = anchor_end = anchor_interval = 0
        positive_count = nonpositive_count = structured_positive_count = 0
        for review_id, reference in sorted(references.items()):
            route = load_json(
                artifact_dir
                / "feasibility_probe_runs_r8"
                / f"round_{round_number}"
                / "program_route"
                / f"{review_id}.json"
            )
            permission = load_json(
                artifact_dir
                / "feasibility_probe_runs_r8"
                / f"round_{round_number}"
                / "final_permission"
                / f"{review_id}.json"
            )
            stage_b = load_json(
                artifact_dir
                / "feasibility_probe_runs_r8"
                / f"round_{round_number}"
                / "stage_b"
                / f"{review_id}.json"
            )
            actual_permission = _status_code(permission.get("program_derived_permission")) or "UNKNOWN"
            actual_scenario = route.get("program_derived_scenario")
            exact_permission += int(actual_permission == reference.expected_permission)
            action_matches += int(
                _trade_action(actual_permission) == _trade_action(reference.expected_permission)
            )
            row: dict[str, Any] = {
                "review_id": review_id,
                "round": round_number,
                "case_role": reference.case_role,
                "reference_permission": reference.expected_permission,
                "actual_permission": actual_permission,
                "trade_action_matches": (
                    _trade_action(actual_permission) == _trade_action(reference.expected_permission)
                ),
                "reference_scenario": (
                    reference.legacy_trigger.get("scenario") if reference.legacy_trigger else None
                ),
                "actual_scenario": actual_scenario,
                "scenario_comparable": reference.structured_legacy_reference,
            }
            if reference.positive:
                positive_count += 1
                positive_trade += int(actual_permission == "TRADE_APPROVED")
                if reference.structured_legacy_reference:
                    structured_positive_count += 1
                    legacy_scenario = str(reference.legacy_trigger["scenario"])
                    positive_scenario += int(actual_scenario == legacy_scenario)
                    legacy_anchor = reference.legacy_trigger["macro_anchor"]
                    current_anchor = _find_controlling_anchor(stage_b)
                    direction_match = bool(
                        current_anchor
                        and current_anchor.get("direction") == legacy_anchor.get("direction")
                    )
                    start_match = bool(
                        current_anchor
                        and current_anchor.get("start_date") == legacy_anchor.get("start")
                    )
                    end_match = bool(
                        current_anchor
                        and current_anchor.get("end_date") == legacy_anchor.get("end")
                    )
                    interval_match = direction_match and start_match and end_match
                    anchor_direction += int(direction_match)
                    anchor_start += int(start_match)
                    anchor_end += int(end_match)
                    anchor_interval += int(interval_match)
                    row["legacy_anchor"] = {
                        "direction": legacy_anchor.get("direction"),
                        "start_date": legacy_anchor.get("start"),
                        "end_date": legacy_anchor.get("end"),
                    }
                    row["r8_controlling_anchor"] = (
                        {
                            "anchor_id": current_anchor.get("anchor_id"),
                            "direction": current_anchor.get("direction"),
                            "start_date": current_anchor.get("start_date"),
                            "end_date": current_anchor.get("end_date"),
                            "role": current_anchor.get("role"),
                            "status": current_anchor.get("status"),
                            "start_delta_calendar_days": _calendar_delta_days(
                                current_anchor.get("start_date"), legacy_anchor.get("start")
                            ),
                            "end_delta_calendar_days": _calendar_delta_days(
                                current_anchor.get("end_date"), legacy_anchor.get("end")
                            ),
                        }
                        if current_anchor
                        else None
                    )
                    row["anchor_exact_interval_match"] = interval_match
                else:
                    row["legacy_anchor"] = None
                    row["r8_controlling_anchor"] = None
                    row["anchor_exact_interval_match"] = None
            else:
                nonpositive_count += 1
                nonpositive_no_trade += int(actual_permission != "TRADE_APPROVED")
                row["legacy_anchor"] = None
                row["r8_controlling_anchor"] = None
                row["anchor_exact_interval_match"] = None
            case_rows.append(row)
        count = len(references)
        rounds.append(
            {
                "round": round_number,
                "case_count": count,
                "positive_reference_count": positive_count,
                "positive_with_structured_legacy_trigger_count": structured_positive_count,
                "nonpositive_reference_count": nonpositive_count,
                "exact_five_state_permission_agreement_percent": _pct(exact_permission, count),
                "trade_vs_no_trade_agreement_percent": _pct(action_matches, count),
                "positive_trade_recall_percent": _pct(positive_trade, positive_count),
                "nonpositive_no_trade_rate_percent": _pct(
                    nonpositive_no_trade, nonpositive_count
                ),
                "positive_scenario_agreement_percent": _pct(
                    positive_scenario, structured_positive_count
                ),
                "positive_anchor_direction_agreement_percent": _pct(
                    anchor_direction, structured_positive_count
                ),
                "positive_anchor_start_exact_percent": _pct(
                    anchor_start, structured_positive_count
                ),
                "positive_anchor_end_exact_percent": _pct(
                    anchor_end, structured_positive_count
                ),
                "positive_anchor_interval_exact_percent": _pct(
                    anchor_interval, structured_positive_count
                ),
            }
        )
    return {"round_metrics": rounds, "case_rounds": case_rows}


def _b1b_alignment(
    artifact_dir: Path,
    references: dict[str, ReferenceCase],
    selected_review_ids: Iterable[str],
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    positive_count = positive_exact_catalog = positive_exact_parent_pool = 0
    for review_id in selected_review_ids:
        reference = references[review_id]
        packet = load_json(
            artifact_dir
            / "stage_b1b1_parent_source_input_packets_candidate_r1"
            / f"{review_id}.json"
        )
        catalog = load_json(
            artifact_dir / "objective_candidate_catalogs_r1" / f"{review_id}.json"
        )
        parent_candidates = packet.get("candidate_segments", [])
        all_candidates = catalog.get("objective_segment_candidates", [])
        legacy_anchor = (
            reference.legacy_trigger.get("macro_anchor") if reference.legacy_trigger else None
        )
        exact_catalog_ids: list[str] = []
        exact_parent_ids: list[str] = []
        if reference.positive:
            positive_count += 1
            exact_catalog_ids = [
                str(item["candidate_id"])
                for item in all_candidates
                if _exact_anchor_match(item, legacy_anchor)
            ]
            exact_parent_ids = [
                str(item["candidate_id"])
                for item in parent_candidates
                if _exact_anchor_match(item, legacy_anchor)
            ]
            positive_exact_catalog += int(bool(exact_catalog_ids))
            positive_exact_parent_pool += int(bool(exact_parent_ids))
            comparability = (
                "COMPARABLE_EXACT_REFERENCE_OBJECT"
                if exact_parent_ids
                else "NOT_COMPARABLE_CANDIDATE_OBJECT_MISMATCH"
            )
        else:
            comparability = "NOT_COMPARABLE_REFERENCE_GRANULARITY"
        b1b1 = _load_stage_atom_rounds(
            artifact_dir,
            "stage_b1b1_parent_source_runs_candidate_r2",
            "stage_b1b1_parent_source",
            review_id,
            "candidate_parent_perceptions",
            "parent_source_relation",
        )
        b1b2_r1 = _load_stage_atom_rounds(
            artifact_dir,
            "stage_b1b2_control_relevance_runs_candidate_r1",
            "stage_b1b2_control_relevance",
            review_id,
            "candidate_control_perceptions",
            "current_control_relevance",
        )
        b1b2_r2 = _load_stage_atom_rounds(
            artifact_dir,
            "stage_b1b2_control_relevance_runs_candidate_r2",
            "stage_b1b2_control_relevance",
            review_id,
            "candidate_control_perceptions",
            "current_control_relevance",
        )
        candidate_rows = []
        for item in parent_candidates:
            candidate_id = str(item["candidate_id"])
            candidate_rows.append(
                {
                    "candidate_id": candidate_id,
                    "scale": item.get("scale"),
                    "direction": item.get("direction"),
                    "start_date": item.get("start_date"),
                    "confirmed_end_date": item.get("confirmed_end_date"),
                    "status": item.get("status"),
                    "exact_legacy_anchor_match": candidate_id in exact_parent_ids,
                    "b1b1_parent_source_by_round": b1b1.get(candidate_id, [None, None, None]),
                    "b1b2_r1_control_relevance_by_round": b1b2_r1.get(
                        candidate_id, [None, None, None]
                    ),
                    "b1b2_r2_control_relevance_by_round": b1b2_r2.get(
                        candidate_id, [None, None, None]
                    ),
                }
            )
        cases.append(
            {
                "review_id": review_id,
                "case_role": reference.case_role,
                "source_classification": reference.source_classification,
                "reference_permission": reference.expected_permission,
                "reference_scenario": (
                    reference.legacy_trigger.get("scenario") if reference.legacy_trigger else None
                ),
                "legacy_anchor": (
                    {
                        "direction": legacy_anchor.get("direction"),
                        "start_date": legacy_anchor.get("start"),
                        "end_date": legacy_anchor.get("end"),
                    }
                    if legacy_anchor
                    else None
                ),
                "objective_catalog_candidate_count": len(all_candidates),
                "parent_candidate_count": len(parent_candidates),
                "exact_reference_object_ids_in_catalog": exact_catalog_ids,
                "exact_reference_object_ids_in_parent_pool": exact_parent_ids,
                "role_atom_comparability": comparability,
                "parent_candidates": candidate_rows,
            }
        )
    return {
        "selected_case_count": len(cases),
        "positive_reference_count": positive_count,
        "positive_exact_reference_object_coverage_in_full_catalog_percent": _pct(
            positive_exact_catalog, positive_count
        ),
        "positive_exact_reference_object_coverage_in_parent_pool_percent": _pct(
            positive_exact_parent_pool, positive_count
        ),
        "cases": cases,
    }


def build_report(
    artifact_dir: Path,
    selected_review_ids: Iterable[str] = DEFAULT_SELECTED_REVIEW_IDS,
) -> dict[str, Any]:
    selected_review_ids = tuple(selected_review_ids)
    references = load_reference_cases(artifact_dir)
    missing = sorted(set(selected_review_ids) - set(references))
    if missing:
        raise ValueError(f"missing selected calibration references: {missing}")
    r8 = _r8_alignment(artifact_dir, references)
    b1b = _b1b_alignment(artifact_dir, references, selected_review_ids)
    source_files = (
        artifact_dir / "sealed" / "feasibility_probe_calibration_labels.json",
        artifact_dir / "calibration_daily_cases_v2.jsonl",
        artifact_dir / "feasibility_probe_execution_manifest_v8.json",
        artifact_dir / "stage_b1b1_parent_source_execution_manifest_candidate_r2.json",
        artifact_dir / "stage_b1b2_control_relevance_execution_manifest_candidate_r1.json",
        artifact_dir / "stage_b1b2_control_relevance_execution_manifest_candidate_r2.json",
    )
    no_r8_round_fully_aligned = all(
        row["positive_trade_recall_percent"] < 100.0
        or row["positive_scenario_agreement_percent"] < 100.0
        or row["positive_anchor_interval_exact_percent"] < 100.0
        for row in r8["round_metrics"]
    )
    object_gap = (
        b1b["positive_exact_reference_object_coverage_in_parent_pool_percent"] < 100.0
    )
    return {
        "report_version": "v2-core-calibration-reference-alignment-r1",
        "status": (
            "REVISION_REQUIRED_REFERENCE_OBJECT_GAP（需要修訂：參考結構物件缺口）"
            if object_gap
            else "READY_FOR_ATOM_ALIGNMENT_REVIEW（可進入原子語意對齊審查）"
        ),
        "milestone": "MILESTONE_2A（四情境可行性探針）",
        "scope": "CALIBRATION_ONLY_POSTHOC_AUDIT（僅校準集事後稽核）",
        "formal_legacy_reproduction": False,
        "locked_reproduction_set_opened": False,
        "future_performance_used": False,
        "legacy_answers_exposed_to_ai": False,
        "identity_used_for_ai": False,
        "identity_emitted_in_report": False,
        "reference_case_count": len(references),
        "structured_legacy_trigger_reference_count": sum(
            int(reference.structured_legacy_reference) for reference in references.values()
        ),
        "selected_b1b_review_ids": list(selected_review_ids),
        "r8_alignment": r8,
        "b1b_alignment": b1b,
        "findings": {
            "no_r8_round_fully_aligned_to_all_positive_reference_dimensions": no_r8_round_fully_aligned,
            "b1b_positive_reference_object_gap": object_gap,
            "negative_and_boundary_cases_are_not_role_level_ground_truth": True,
            "single_lucky_round_must_not_be_promoted_to_reference": True,
            "b1b2_r2_should_continue_before_object_gap_is_fixed": False,
        },
        "required_next_action": (
            "Create a versioned composite/campaign anchor candidate layer from AS-OF-only objective "
            "boundaries, prove that it can express calibration legacy anchors without feeding those "
            "answers to AI, then repeat calibration atom alignment before new repeatability calls."
        ),
        "source_sha256": {path.name: sha256_file(path) for path in source_files},
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 舊純 AI 校準參考與現行分階段機制對齊稽核 R1",
        "",
        f"狀態：`{report['status']}`",
        "",
        "本報告只在既有 AI 輸出全部凍結後，使用校準集標籤做事後稽核；沒有把舊答案餵回 AI、沒有開啟鎖定重現集，也沒有使用未來績效。它不是正式舊 AI 重現報告。",
        "",
        "## R8 三輪相對舊純 AI 校準參考",
        "",
        "| 輪次 | 五分類權限一致 | 買／不買一致 | 舊正例交易重現 | 非正例維持不交易 | 舊正例情境一致 | 定錨方向一致 | 定錨起點完全一致 | 定錨終點完全一致 | 定錨區間完全一致 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["r8_alignment"]["round_metrics"]:
        values = [
            row["round"],
            row["exact_five_state_permission_agreement_percent"],
            row["trade_vs_no_trade_agreement_percent"],
            row["positive_trade_recall_percent"],
            row["nonpositive_no_trade_rate_percent"],
            row["positive_scenario_agreement_percent"],
            row["positive_anchor_direction_agreement_percent"],
            row["positive_anchor_start_exact_percent"],
            row["positive_anchor_end_exact_percent"],
            row["positive_anchor_interval_exact_percent"],
        ]
        lines.append("| " + " | ".join(str(v) if i == 0 else f"{v:.2f}%" for i, v in enumerate(values)) + " |")
    lines.extend(
        [
            "",
            "交易重現對 16 個舊 V2 正例比較；其中 14 個具完整舊 AI trigger，才可比較情境與定錨。另 2 個 747 部分參考正例只保留交易層粗標籤。負例與邊界例只有不交易參考，不能被假設成某個候選定錨的角色標準答案。",
            "",
            "## B1b 五案結構物件覆蓋",
            "",
            "| Review ID | 參考角色 | 舊純 AI 大定錨 | B1b parent 候選 | 完全相同物件 | 原子可比性 |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for case in report["b1b_alignment"]["cases"]:
        anchor = case["legacy_anchor"]
        anchor_text = (
            f"{anchor['direction']} {anchor['start_date']}～{anchor['end_date']}"
            if anchor
            else "無角色級參考"
        )
        exact = len(case["exact_reference_object_ids_in_parent_pool"])
        lines.append(
            f"| `{case['review_id']}` | `{case['case_role']}` | {anchor_text} | "
            f"{case['parent_candidate_count']} | {exact} | `{case['role_atom_comparability']}` |"
        )
    b1b = report["b1b_alignment"]
    lines.extend(
        [
            "",
            f"兩個舊 V2 正例在完整客觀 catalog 的完全相同定錨覆蓋率為 **{b1b['positive_exact_reference_object_coverage_in_full_catalog_percent']:.2f}%**；在 B1b parent 候選池亦為 **{b1b['positive_exact_reference_object_coverage_in_parent_pool_percent']:.2f}%**。",
            "",
            "## 判定",
            "",
            "目前不能從三輪中挑一輪當作純 AI 標準答案後繼續校正。這會同時造成挑答案偏誤，且掩蓋輸入結構物件不同的問題。現行 B1b 對單一確認腿做 PARENT／CONTROL 判讀，但舊純 AI 的控制定錨常是跨多腿的 campaign／複合定錨；兩者不是同一欄位語意。",
            "",
            "下一步必須先建立獨立版本的複合／campaign 定錨候選層：程式只從 AS-OF 客觀邊界列出有限候選，不替 AI 指派課程角色；先在校準正例證明舊定錨可被候選空間表達，再進行原子語意與三輪一致性。B1b2 R2 的既有 7 筆結果保留，但在物件缺口修正前不再追加呼叫。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    args.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
