"""Revalidate and compare three focused Stage B1a1E smoke rounds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_stage_b1a1e_candidate_validator_v1 import load_json
from scripts.v2_core_stage_b1a1e_codex_runner_v1 import (
    validate_existing_stage_b1a1e_artifacts,
)
from scripts.v2_core_stage_b1a_candidate_validator_v1 import sha256_file


PASS_THRESHOLD = 90.0
FAIL_THRESHOLD = 75.0


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _paths(run_dir: Path, round_number: int, review_id: str) -> tuple[Path, Path]:
    return (
        run_dir / f"round_{round_number}" / "stage_b1a1e" / f"{review_id}.json",
        run_dir / f"round_{round_number}" / "receipts" / f"{review_id}.stage_b1a1e.json",
    )


def _perception_signature(output: dict[str, Any]) -> dict[str, tuple[str, str]]:
    result = {}
    for row in output["candidate_perceptions"]:
        candidate_id = row["candidate_id"]
        directional = row["directional_path"]
        result[f"{candidate_id}:directional"] = (
            directional["judgement"],
            directional["reason_code"],
        )
        for target in row["structural_targets"]:
            option_id = target["evidence_option_id"]
            for field in (
                "same_level_meaningful_target",
                "challenge_or_break_realized",
            ):
                value = target[field]
                result[f"{candidate_id}:{option_id}:{field}"] = (
                    value["judgement"],
                    value["reason_code"],
                )
    return result


def _derived_index(validation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["candidate_id"]: row for row in validation["derived_candidate_results"]}


def compare_smoke(
    *, manifest_path: Path, artifact_dir: Path, run_dir: Path | None = None
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    run_directory = run_dir or artifact_dir / manifest["run_directory"]
    input_manifest_path = artifact_dir / manifest["input_manifest_file"]
    schema_path = artifact_dir / manifest["schema_file"]
    prompt_path = artifact_dir / manifest["prompt_file"]
    completed: dict[tuple[str, int], dict[str, Any]] = {}
    missing = []
    invalid_artifacts = []
    errors = []

    for row in manifest["rows"]:
        review_id = row["review_id"]
        input_path = artifact_dir / manifest["input_packet_directory"] / row[
            "input_packet_file"
        ]
        if sha256_file(input_path) != row["input_packet_sha256"]:
            errors.append(f"INPUT:{review_id}:HASH_MISMATCH")
            continue
        for round_number in range(1, int(manifest["required_rounds"]) + 1):
            output_path, receipt_path = _paths(run_directory, round_number, review_id)
            invalid_path = output_path.with_suffix(".invalid.raw")
            if invalid_path.is_file():
                invalid_artifacts.append(
                    {
                        "case_round": f"round_{round_number}:{review_id}",
                        "path": invalid_path.as_posix(),
                        "sha256": sha256_file(invalid_path),
                    }
                )
                continue
            if not output_path.is_file() and not receipt_path.is_file():
                missing.append(f"round_{round_number}:{review_id}")
                continue
            try:
                validation = validate_existing_stage_b1a1e_artifacts(
                    input_packet_path=input_path,
                    input_manifest_path=input_manifest_path,
                    schema_path=schema_path,
                    prompt_path=prompt_path,
                    output_path=output_path,
                    receipt_path=receipt_path,
                )
                completed[(review_id, round_number)] = {
                    "output": load_json(output_path),
                    "validation": validation,
                }
            except Exception as exc:
                errors.append(
                    f"ARTIFACT:round_{round_number}:{review_id}:{type(exc).__name__}:{exc}"
                )

    expected = int(manifest["case_count"]) * int(manifest["required_rounds"])
    base_report = {
        "manifest_version": manifest["manifest_version"],
        "formal_model": manifest["formal_model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "case_count": manifest["case_count"],
        "required_rounds": manifest["required_rounds"],
        "expected_case_rounds": expected,
        "completed_case_rounds": len(completed),
        "missing_case_rounds": sorted(missing),
        "error_count": len(errors) + len(invalid_artifacts),
        "errors": sorted(errors),
        "invalid_artifacts": invalid_artifacts,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
    }
    if invalid_artifacts or errors:
        return {
            **base_report,
            "status": "SMOKE_PROTOCOL_FAILED（Smoke協定失敗）",
            "metrics_published": False,
            "metrics": None,
        }
    if len(completed) != expected:
        return {
            **base_report,
            "status": "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）",
            "metrics_published": False,
            "metrics": None,
        }

    perception_total = perception_same = 0
    reason_total = reason_same = 0
    derived_total = derived_same = 0
    primary_total = primary_same = 0
    pool_total = pool_same = 0
    cases = []
    for row in manifest["rows"]:
        review_id = row["review_id"]
        rounds = [completed[(review_id, number)] for number in (1, 2, 3)]
        perception_maps = [_perception_signature(value["output"]) for value in rounds]
        if len({frozenset(value) for value in perception_maps}) != 1:
            raise ValueError(f"perception key mismatch for {review_id}")
        case_perception_total = case_perception_same = 0
        for key in sorted(perception_maps[0]):
            values = [mapping[key] for mapping in perception_maps]
            perception_total += 1
            case_perception_total += 1
            reason_total += 1
            if len({value[0] for value in values}) == 1:
                perception_same += 1
                case_perception_same += 1
            if len(set(values)) == 1:
                reason_same += 1

        derived_maps = [_derived_index(value["validation"]) for value in rounds]
        candidate_ids = sorted(derived_maps[0])
        if any(set(value) != set(candidate_ids) for value in derived_maps):
            raise ValueError(f"derived candidate mismatch for {review_id}")
        case_derived_total = case_derived_same = 0
        case_primary_total = case_primary_same = 0
        for candidate_id in candidate_ids:
            for atom in ("directional_coherence", "structural_challenge_or_break"):
                values = [mapping[candidate_id][atom] for mapping in derived_maps]
                derived_total += 1
                case_derived_total += 1
                primary_total += 1
                case_primary_total += 1
                if len({value["result"] for value in values}) == 1:
                    derived_same += 1
                    case_derived_same += 1
                if len({value["primary_evidence_option_id"] for value in values}) == 1:
                    primary_same += 1
                    case_primary_same += 1
        pools = [frozenset(value["validation"]["partial_eligible_ids"]) for value in rounds]
        pool_total += 1
        if len(set(pools)) == 1:
            pool_same += 1
        cases.append(
            {
                "review_id": review_id,
                "candidate_count": len(candidate_ids),
                "perception_consistency_percent": _pct(
                    case_perception_same, case_perception_total
                ),
                "derived_atom_consistency_percent": _pct(
                    case_derived_same, case_derived_total
                ),
                "primary_option_consistency_percent": _pct(
                    case_primary_same, case_primary_total
                ),
                "partial_eligible_set_consistent": len(set(pools)) == 1,
                "partial_eligible_sizes_by_round": [len(value) for value in pools],
            }
        )

    metrics = {
        "schema_and_semantic_valid_percent": 100.0,
        "ai_perception_consistency_percent": _pct(perception_same, perception_total),
        "reason_code_consistency_percent": _pct(reason_same, reason_total),
        "derived_atom_consistency_percent": _pct(derived_same, derived_total),
        "primary_option_consistency_percent": _pct(primary_same, primary_total),
        "partial_eligible_set_consistency_percent": _pct(pool_same, pool_total),
        "perception_comparisons": perception_total,
        "derived_atom_comparisons": derived_total,
        "cases": cases,
    }
    gates = [
        metrics["ai_perception_consistency_percent"],
        metrics["derived_atom_consistency_percent"],
        metrics["primary_option_consistency_percent"],
        metrics["partial_eligible_set_consistency_percent"],
    ]
    if any(value < FAIL_THRESHOLD for value in gates):
        status = "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）"
    elif any(value < PASS_THRESHOLD for value in gates):
        status = "SMOKE_REVISION_REQUIRED（Smoke需要修訂）"
    else:
        status = "SMOKE_PASSED（Smoke通過）"
    return {
        **base_report,
        "status": status,
        "metrics_published": True,
        "metrics": metrics,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Stage B1a1E 聚焦Smoke比較報告",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 正式模型：`{report['formal_model']}／{report['reasoning_effort']}`",
        f"- 案例輪次：{report['completed_case_rounds']}／{report['expected_case_rounds']}",
        "- 未使用股票身分、sealed舊答案或未來績效。",
    ]
    if not report["metrics_published"]:
        lines.extend(["", "三輪未完整或協定失敗，因此不發布部分正式一致率。"])
        return "\n".join(lines) + "\n"
    metrics = report["metrics"]
    lines.extend(
        [
            "",
            "## 門檻結果",
            "",
            f"- AI證據感知一致率：{metrics['ai_perception_consistency_percent']:.2f}%",
            f"- 程式合成兩原子一致率：{metrics['derived_atom_consistency_percent']:.2f}%",
            f"- 唯一主證據選項一致率：{metrics['primary_option_consistency_percent']:.2f}%",
            f"- 部分合格集合一致率：{metrics['partial_eligible_set_consistency_percent']:.2f}%",
            f"- reason code一致率：{metrics['reason_code_consistency_percent']:.2f}%（診斷項）",
            "",
            "| 案例 | 候選 | AI感知 | 合成原子 | 主證據 | 集合一致 | 三輪合格數 |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for row in metrics["cases"]:
        sizes = "／".join(str(value) for value in row["partial_eligible_sizes_by_round"])
        lines.append(
            f"| `{row['review_id']}` | {row['candidate_count']} | "
            f"{row['perception_consistency_percent']:.2f}% | "
            f"{row['derived_atom_consistency_percent']:.2f}% | "
            f"{row['primary_option_consistency_percent']:.2f}% | "
            f"{'是' if row['partial_eligible_set_consistent'] else '否'} | {sizes} |"
        )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = compare_smoke(
        manifest_path=args.manifest,
        artifact_dir=args.artifact_dir,
        run_dir=args.run_dir,
    )
    if args.json_output:
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.markdown_output:
        args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "SMOKE_PROTOCOL_FAILED（Smoke協定失敗）" else 0


if __name__ == "__main__":
    raise SystemExit(main())
