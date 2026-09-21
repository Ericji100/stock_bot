"""Revalidate and compare three B1a2 active-link smoke rounds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_stage_b1a2_active_link_runner_v1 import validate_existing_artifacts
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json
from scripts.v2_core_stage_b1a_candidate_validator_v1 import sha256_file


PASS_THRESHOLD = 90.0
FAIL_THRESHOLD = 75.0


def pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def compare(*, artifact_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    input_manifest_path = artifact_dir / manifest["input_manifest_file"]
    schema_path = artifact_dir / manifest["schema_file"]
    prompt_path = artifact_dir / manifest["prompt_file"]
    run_dir = artifact_dir / manifest["run_directory"]
    rounds = int(manifest["required_rounds"])
    completed: dict[tuple[str, int], dict[str, Any]] = {}
    missing: list[str] = []
    errors: list[str] = []
    invalid_artifacts: list[str] = []
    for row in manifest["rows"]:
        review_id = str(row["review_id"])
        input_path = artifact_dir / manifest["input_packet_directory"] / row["input_packet_file"]
        if sha256_file(input_path) != row["input_packet_sha256"]:
            errors.append(f"INPUT:{review_id}:HASH_MISMATCH")
            continue
        for round_number in range(1, rounds + 1):
            output_path = run_dir / f"round_{round_number}" / "stage_b1a2_active_link" / f"{review_id}.json"
            receipt_path = run_dir / f"round_{round_number}" / "receipts" / f"{review_id}.stage_b1a2_active_link.json"
            invalid_path = output_path.with_suffix(".invalid.raw")
            if invalid_path.is_file():
                invalid_artifacts.append(str(invalid_path))
                continue
            if not output_path.is_file() and not receipt_path.is_file():
                missing.append(f"round_{round_number}:{review_id}")
                continue
            try:
                completed[(review_id, round_number)] = {
                    "output": load_json(output_path),
                    "validation": validate_existing_artifacts(
                        input_packet_path=input_path,
                        input_manifest_path=input_manifest_path,
                        schema_path=schema_path,
                        prompt_path=prompt_path,
                        output_path=output_path,
                        receipt_path=receipt_path,
                    ),
                }
            except Exception as exc:
                errors.append(f"ARTIFACT:round_{round_number}:{review_id}:{type(exc).__name__}:{exc}")
    expected = int(manifest["expected_case_rounds"])
    base = {
        "manifest_version": manifest["manifest_version"],
        "formal_model": manifest["formal_model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "case_count": manifest["case_count"],
        "required_rounds": rounds,
        "expected_case_rounds": expected,
        "completed_case_rounds": len(completed),
        "missing_case_rounds": missing,
        "error_count": len(errors),
        "errors": errors,
        "invalid_artifacts": invalid_artifacts,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "course_role_assigned": False,
    }
    if errors or invalid_artifacts:
        return {**base, "status": "SMOKE_PROTOCOL_FAILED（Smoke協定失敗）", "metrics_published": False, "metrics": None}
    if len(completed) != expected:
        return {**base, "status": "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）", "metrics_published": False, "metrics": None}

    total = judgement_same = reason_same = option_same = eligible_same = 0
    case_set_same = 0
    cases = []
    for row in manifest["rows"]:
        review_id = str(row["review_id"])
        outputs = [completed[(review_id, number)]["output"] for number in range(1, rounds + 1)]
        validations = [completed[(review_id, number)]["validation"] for number in range(1, rounds + 1)]
        output_indexes = [
            {str(value["candidate_id"]): value for value in output["candidate_link_perceptions"]}
            for output in outputs
        ]
        validation_indexes = [
            {str(value["candidate_id"]): value for value in validation["derived_candidate_results"]}
            for validation in validations
        ]
        candidate_ids = sorted(output_indexes[0])
        local_judgement = local_eligible = 0
        for candidate_id in candidate_ids:
            atoms = [index[candidate_id]["active_campaign_link"] for index in output_indexes]
            eligibles = [index[candidate_id]["b1a2_shortlisted"] for index in validation_indexes]
            total += 1
            if len({value["judgement"] for value in atoms}) == 1:
                judgement_same += 1
                local_judgement += 1
            if len({value["reason_code"] for value in atoms}) == 1:
                reason_same += 1
            if len({value["evidence_option_id"] for value in atoms}) == 1:
                option_same += 1
            if len(set(eligibles)) == 1:
                eligible_same += 1
                local_eligible += 1
        sets = [tuple(value["b1a2_shortlist_ids"]) for value in validations]
        set_same = len(set(sets)) == 1
        case_set_same += set_same
        cases.append(
            {
                "review_id": review_id,
                "candidate_count": len(candidate_ids),
                "active_link_consistency_percent": pct(local_judgement, len(candidate_ids)),
                "shortlist_eligibility_consistency_percent": pct(local_eligible, len(candidate_ids)),
                "shortlist_set_consistent": set_same,
                "shortlist_sizes_by_round": [len(value) for value in sets],
            }
        )
    metrics = {
        "schema_and_semantic_valid_percent": 100.0,
        "active_link_atom_consistency_percent": pct(judgement_same, total),
        "reason_code_consistency_percent": pct(reason_same, total),
        "primary_option_consistency_percent": pct(option_same, total),
        "shortlist_eligibility_consistency_percent": pct(eligible_same, total),
        "shortlist_set_consistency_percent": pct(case_set_same, len(cases)),
        "candidate_comparisons": total,
        "cases": cases,
    }
    core = [
        metrics["active_link_atom_consistency_percent"],
        metrics["shortlist_eligibility_consistency_percent"],
        metrics["shortlist_set_consistency_percent"],
    ]
    if all(value >= PASS_THRESHOLD for value in core):
        status = "SMOKE_PASSED（Smoke通過）"
    elif any(value < FAIL_THRESHOLD for value in core):
        status = "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）"
    else:
        status = "SMOKE_REVISION_REQUIRED（Smoke需要修訂）"
    return {**base, "status": status, "metrics_published": True, "metrics": metrics}


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Stage B1a2 主動Campaign連結 Smoke比較報告 R1",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 正式模型：`{report['formal_model']}／{report['reasoning_effort']}`",
        f"- 案例輪次：{report['completed_case_rounds']}／{report['expected_case_rounds']}",
        "- 未指派課程角色、唯一控制錨、情境或交易權限。",
    ]
    if not report["metrics_published"]:
        return "\n".join(lines + ["", "三輪尚未完整或協定失敗，因此不發布部分一致率。", ""])
    metrics = report["metrics"]
    lines.extend(
        [
            "", "## 門檻結果", "",
            f"- active link原子一致率：{metrics['active_link_atom_consistency_percent']:.2f}%",
            f"- shortlist候選資格一致率：{metrics['shortlist_eligibility_consistency_percent']:.2f}%",
            f"- shortlist逐案集合一致率：{metrics['shortlist_set_consistency_percent']:.2f}%",
            f"- reason code一致率：{metrics['reason_code_consistency_percent']:.2f}%（診斷項）",
            "", "| 案例 | 候選 | active link | 資格 | 集合一致 | 三輪shortlist數 |",
            "|---|---:|---:|---:|---|---|",
        ]
    )
    for row in metrics["cases"]:
        sizes = "／".join(str(value) for value in row["shortlist_sizes_by_round"])
        lines.append(
            f"| `{row['review_id']}` | {row['candidate_count']} | "
            f"{row['active_link_consistency_percent']:.2f}% | "
            f"{row['shortlist_eligibility_consistency_percent']:.2f}% | "
            f"{'是' if row['shortlist_set_consistent'] else '否'} | {sizes} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(artifact_dir=args.artifact_dir, manifest_path=args.manifest)
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
