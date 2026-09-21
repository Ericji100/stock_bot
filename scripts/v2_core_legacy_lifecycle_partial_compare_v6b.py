"""Post-termination calibration diagnostic for the partial R6B lifecycle run."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)
JSON_OUTPUT = "legacy_lifecycle_partial_alignment_report_candidate_r6b.json"
MD_OUTPUT = "legacy_lifecycle_partial_alignment_report_candidate_r6b.md"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def write_new_or_identical(path: Path, payload: bytes) -> None:
    if path.exists() and path.read_bytes() != payload:
        raise FileExistsError(f"refusing to overwrite non-identical report: {path}")
    path.write_bytes(payload)


def build_report(artifact_dir: Path) -> dict[str, Any]:
    from scripts.v2_core_legacy_lifecycle_runner_v6b import (
        validate_existing_b0a,
        validate_existing_b0b,
    )
    from scripts.v2_core_legacy_roles_b0b_validator_v6 import validate_response

    manifest_path = artifact_dir / "legacy_lifecycle_execution_manifest_candidate_r6b.json"
    teacher_path = artifact_dir / "legacy_teacher_pairwise_audit_candidate_r1.json"
    input_manifest_path = artifact_dir / "legacy_anchor_alignment_input_manifest_candidate_r1.json"
    b0a_schema_path = artifact_dir / "v2_core_legacy_lifecycle_b0a.schema.candidate_r6.json"
    b0a_prompt_path = artifact_dir / "v2_core_legacy_lifecycle_b0a.prompt.candidate_r6.md"
    lifecycle_truth_path = artifact_dir / "legacy_lifecycle_route_truth_table.candidate_r6.json"
    b0b_schema_path = artifact_dir / "v2_core_legacy_roles_b0b.schema.candidate_r6.json"
    b0b_prompt_path = artifact_dir / "v2_core_legacy_roles_b0b.prompt.candidate_r6.md"
    role_truth_path = artifact_dir / "legacy_scenario_role_truth_table.candidate_r6.json"
    input_dir = artifact_dir / "legacy_anchor_alignment_input_packets_candidate_r1"
    run_dir = artifact_dir / "legacy_lifecycle_runs_candidate_r6b"

    manifest = load_json(manifest_path)
    teacher = load_json(teacher_path)
    teacher_by_id = {row["review_id"]: row for row in teacher["cases"]}
    if set(teacher_by_id) != {row["review_id"] for row in manifest["rows"]}:
        raise ValueError("teacher and R6B manifest case sets differ")

    rows: list[dict[str, Any]] = []
    scenario_confusion: Counter[str] = Counter()
    scenario_by_teacher: Counter[str] = Counter()
    b0a_valid = b0b_valid = b0b_invalid = 0
    scenario_matches = working_matches = 0
    invalid_errors: dict[str, list[str]] = {}

    for manifest_row in manifest["rows"]:
        review_id = manifest_row["review_id"]
        packet_path = input_dir / manifest_row["input_packet_file"]
        b0a_output = run_dir / "b0a" / f"{review_id}.json"
        b0a_receipt = run_dir / "b0a-receipts" / f"{review_id}.json"
        b0b_output = run_dir / "b0b" / f"{review_id}.json"
        b0b_receipt = run_dir / "b0b-receipts" / f"{review_id}.json"
        b0b_invalid_raw = run_dir / "b0b" / f"{review_id}.invalid.raw"
        teacher_row = teacher_by_id[review_id]
        teacher_scenario = teacher_row["teacher_scenario"]
        teacher_candidates = {
            row["candidate_id"] for row in teacher_row["teacher_equivalent_candidates"]
        }

        row: dict[str, Any] = {
            "review_id": review_id,
            "as_of": manifest_row["as_of"],
            "teacher_scenario": teacher_scenario,
            "teacher_equivalent_working_candidate_ids": sorted(teacher_candidates),
            "b0a_status": "NOT_RUN",
            "program_scenario": None,
            "scenario_matches_teacher": None,
            "b0b_status": "NOT_RUN",
            "scenario_working_candidate_id": None,
            "working_anchor_matches_teacher_equivalent": None,
        }
        if b0a_output.is_file():
            validation = validate_existing_b0a(
                input_packet_path=packet_path,
                input_manifest_path=input_manifest_path,
                schema_path=b0a_schema_path,
                prompt_path=b0a_prompt_path,
                lifecycle_truth_table_path=lifecycle_truth_path,
                output_path=b0a_output,
                receipt_path=b0a_receipt,
            )
            b0a_valid += 1
            row["b0a_status"] = "VALID"
            row["program_scenario"] = validation["program_derived_scenario_family"]
            row["scenario_matches_teacher"] = row["program_scenario"] == teacher_scenario
            scenario_matches += int(row["scenario_matches_teacher"])
            scenario_confusion[f"{teacher_scenario}|{row['program_scenario']}"] += 1
            scenario_by_teacher[teacher_scenario] += 1

        if b0b_output.is_file():
            validation = validate_existing_b0b(
                input_packet_path=packet_path,
                input_manifest_path=input_manifest_path,
                b0a_schema_path=b0a_schema_path,
                b0a_prompt_path=b0a_prompt_path,
                lifecycle_truth_table_path=lifecycle_truth_path,
                b0a_output_path=b0a_output,
                b0a_receipt_path=b0a_receipt,
                b0b_schema_path=b0b_schema_path,
                b0b_prompt_path=b0b_prompt_path,
                role_truth_table_path=role_truth_path,
                output_path=b0b_output,
                receipt_path=b0b_receipt,
            )
            if validation["status"] != "VALID":
                raise ValueError(f"saved B0B output failed revalidation: {review_id}")
            response = load_json(b0b_output)
            working = response["roles"]["scenario_working"]["candidate_id"]
            b0b_valid += 1
            row["b0b_status"] = "VALID"
            row["scenario_working_candidate_id"] = working
            row["working_anchor_matches_teacher_equivalent"] = working in teacher_candidates
            working_matches += int(row["working_anchor_matches_teacher_equivalent"])
        elif b0b_invalid_raw.is_file():
            if not b0a_output.is_file():
                raise ValueError("B0B invalid raw exists without B0A output")
            response = load_json(b0b_invalid_raw)
            validation = validate_response(
                response=response,
                schema=load_json(b0b_schema_path),
                packet=load_json(packet_path),
                b0a_response=load_json(b0a_output),
                b0a_schema=load_json(b0a_schema_path),
                lifecycle_truth_table=load_json(lifecycle_truth_path),
                role_truth_table=load_json(role_truth_path),
            )
            if validation["status"] != "INVALID":
                raise ValueError("saved invalid raw no longer validates as INVALID")
            b0b_invalid += 1
            row["b0b_status"] = "INVALID"
            row["b0b_validation_errors"] = validation["errors"]
            invalid_errors[review_id] = validation["errors"]
        rows.append(row)

    expected = len(manifest["rows"])
    report = {
        "report_version": "v2-core-legacy-lifecycle-partial-alignment-r6b",
        "status": "INVALIDATED_PARTIAL_CALIBRATION_DIAGNOSTIC（失效版本部分校準診斷）",
        "milestone": "MILESTONE_2A／ONE_PASS_LEGACY_ALIGNMENT",
        "formal_reproduction_rate_published": False,
        "reason_formal_rate_not_published": "R6B terminated on the first invalid B0B output before all 14 cases completed",
        "teacher_revealed_only_after_r6b_terminated": True,
        "teacher_answers_available_to_ai": False,
        "future_performance_used": False,
        "identity_used": False,
        "locked_reproduction_set_opened": False,
        "expected_case_count": expected,
        "b0a_valid_count": b0a_valid,
        "b0b_valid_count": b0b_valid,
        "b0b_invalid_count": b0b_invalid,
        "not_started_b0a_count": expected - b0a_valid,
        "partial_scenario_match_count": scenario_matches,
        "partial_scenario_comparable_count": b0a_valid,
        "partial_scenario_match_percent": round(100.0 * scenario_matches / b0a_valid, 2)
        if b0a_valid
        else None,
        "partial_working_anchor_match_count": working_matches,
        "partial_working_anchor_comparable_count": b0b_valid,
        "partial_working_anchor_match_percent": round(100.0 * working_matches / b0b_valid, 2)
        if b0b_valid
        else None,
        "partial_scenario_confusion": dict(sorted(scenario_confusion.items())),
        "partial_teacher_scenario_counts": dict(sorted(scenario_by_teacher.items())),
        "invalid_errors": invalid_errors,
        "diagnostic_conclusions": [
            "MATURE_COLLAPSE_PERSISTS_IN_PARTIAL_RUN",
            "SCENARIO_WORKING_ANCHOR_ALIGNMENT_REMAINS_ZERO_IN_VALID_B0B_SUBSET",
            "B0A_CONTROL_CHALLENGER_AND_CURRENT_EPISODE_ARE_NOT_A_SINGLE_GUARANTEED_OBJECT",
            "R6B_MUST_NOT_BE_RETRIED_OR_EXTENDED",
        ],
        "rows": rows,
        "inputs": {
            "execution_manifest_file": manifest_path.name,
            "execution_manifest_sha256": sha256_path(manifest_path),
            "teacher_pairwise_audit_file": teacher_path.name,
            "teacher_pairwise_audit_sha256": sha256_path(teacher_path),
            "input_manifest_file": input_manifest_path.name,
            "input_manifest_sha256": sha256_path(input_manifest_path),
        },
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# R6B舊純AI生命週期部分對齊診斷",
        "",
        f"狀態：`{report['status']}`",
        "",
        "R6B在第8案B0B出現不可修補的角色綁定錯位後已終止。本報告只在版本終止、既有輸出不可變後揭盲校準teacher；不是正式14案重現率。",
        "",
        "## 結果",
        "",
        f"- B0A合法：{report['b0a_valid_count']}／{report['expected_case_count']}。",
        f"- B0B合法：{report['b0b_valid_count']}；invalid：{report['b0b_invalid_count']}。",
        f"- 已完成B0A子集的情境命中：{report['partial_scenario_match_count']}／{report['partial_scenario_comparable_count']}（{report['partial_scenario_match_percent']:.2f}%）。",
        f"- 合法B0B子集的情境工作錨命中：{report['partial_working_anchor_match_count']}／{report['partial_working_anchor_comparable_count']}（{report['partial_working_anchor_match_percent']:.2f}%）。",
        "- 因版本未完成14案，以上只能稱部分失敗診斷，不能冒充正式重現率。",
        "",
        "## 逐案",
        "",
        "| review id | teacher情境 | R6B情境 | 情境一致 | B0B | 工作錨一致 |",
        "| --- | --- | --- | ---: | --- | ---: |",
    ]
    for row in report["rows"]:
        lines.append(
            "| {review_id} | {teacher_scenario} | {program_scenario} | {scenario} | {b0b_status} | {working} |".format(
                review_id=row["review_id"],
                teacher_scenario=row["teacher_scenario"],
                program_scenario=row["program_scenario"] or "NOT_RUN",
                scenario="是" if row["scenario_matches_teacher"] else "否" if row["scenario_matches_teacher"] is False else "—",
                b0b_status=row["b0b_status"],
                working="是" if row["working_anchor_matches_teacher_equivalent"] else "否" if row["working_anchor_matches_teacher_equivalent"] is False else "—",
            )
        )
    lines.extend(
        [
            "",
            "## 結論",
            "",
            "R6B不是只有第8案validator太嚴。部分揭盲已證明MATURE塌縮仍存在，而且合法角色輸出的工作錨仍未命中teacher等價物件。下一版必須先固定當前episode、直接父代、仍控制的DOWN與成熟campaign等角色物件，再用同一物件判斷控制關係；不得只放寬第8案綁定後重跑。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    report = build_report(args.artifact_dir)
    write_new_or_identical(args.artifact_dir / JSON_OUTPUT, canonical_bytes(report))
    write_new_or_identical(
        args.artifact_dir / MD_OUTPUT, render_markdown(report).encode("utf-8")
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "b0a_valid_count": report["b0a_valid_count"],
                "b0b_valid_count": report["b0b_valid_count"],
                "partial_scenario_match_percent": report["partial_scenario_match_percent"],
                "partial_working_anchor_match_percent": report[
                    "partial_working_anchor_match_percent"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
