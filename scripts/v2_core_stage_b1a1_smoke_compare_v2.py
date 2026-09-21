"""Revalidate and compare three Stage B1a1 R2 smoke rounds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_stage_b1a1_candidate_validator_v2 import ATOMIC_FIELDS, load_json
from scripts.v2_core_stage_b1a1_codex_runner_v2 import (
    StageB1a1RunnerError,
    validate_existing_stage_b1a1_artifacts,
)
from scripts.v2_core_stage_b1a_candidate_validator_v1 import sha256_file


PASS_THRESHOLD = 90.0
FAIL_THRESHOLD = 75.0


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _artifact_paths(run_dir: Path, round_number: int, review_id: str) -> tuple[Path, Path]:
    round_dir = run_dir / f"round_{round_number}"
    return (
        round_dir / "stage_b1a1" / f"{review_id}.json",
        round_dir / "receipts" / f"{review_id}.stage_b1a1.json",
    )


def _verify_manifest(manifest: dict[str, Any], artifact_dir: Path) -> list[str]:
    errors: list[str] = []
    expected = {
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "required_rounds": 3,
        "expected_case_rounds": int(manifest.get("case_count", 0)) * 3,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            errors.append(f"MANIFEST:{key}:EXPECTED_{value}:ACTUAL_{manifest.get(key)}")
    for file_key, hash_key in (
        ("prompt_file", "prompt_sha256"),
        ("output_schema_file", "output_schema_sha256"),
        ("input_manifest_file", "input_manifest_sha256"),
        ("smoke_gate_file", "smoke_gate_sha256"),
        ("operational_policy_file", "operational_policy_sha256"),
    ):
        path = artifact_dir / str(manifest.get(file_key, ""))
        if not path.is_file():
            errors.append(f"MANIFEST:MISSING_COMPONENT:{file_key}")
        elif sha256_file(path) != manifest.get(hash_key):
            errors.append(f"MANIFEST:HASH_MISMATCH:{file_key}")
    for filename, expected_hash in manifest.get("repo_components", {}).items():
        path = ROOT / filename
        if not path.is_file() or sha256_file(path) != expected_hash:
            errors.append(f"MANIFEST:REPO_COMPONENT_MISMATCH:{filename}")
    input_dir = artifact_dir / "stage_b1a1_input_packets_candidate_r2"
    source_dir = artifact_dir / "stage_b1a_input_packets_candidate_r1"
    relation_dir = artifact_dir / "objective_relation_catalogs_r1"
    focus_dir = artifact_dir / "objective_focus_catalogs_r1"
    for row in manifest.get("rows", []):
        review_id = row["review_id"]
        for label, path, expected_hash in (
            ("INPUT", input_dir / row["input_packet_file"], row["input_packet_sha256"]),
            ("SOURCE", source_dir / f"{review_id}.json", row["source_b1a_input_sha256"]),
            ("RELATION", relation_dir / f"{review_id}.json", row["relation_catalog_sha256"]),
            ("FOCUS", focus_dir / f"{review_id}.json", row["focus_catalog_sha256"]),
        ):
            if not path.is_file() or sha256_file(path) != expected_hash:
                errors.append(f"MANIFEST:{review_id}:{label}_HASH_MISMATCH")
    return sorted(set(errors))


def _assessment_index(output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["candidate_id"]: row for row in output["candidate_assessments"]}


def _full_signature(value: dict[str, Any]) -> str:
    normalized = {
        "result": value["result"],
        "primary_evidence_refs": value["primary_evidence_refs"],
        "supporting_evidence_refs": sorted(value["supporting_evidence_refs"]),
        "contradicting_evidence_refs": sorted(value["contradicting_evidence_refs"]),
        "missing_evidence_codes": sorted(value["missing_evidence_codes"]),
        "reason_code": value["reason_code"],
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compare_smoke(
    *, manifest_path: Path, artifact_dir: Path, run_dir: Path | None = None
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    errors = _verify_manifest(manifest, artifact_dir)
    run_directory = run_dir or artifact_dir / manifest["run_directory"]
    input_dir = artifact_dir / "stage_b1a1_input_packets_candidate_r2"
    source_dir = artifact_dir / "stage_b1a_input_packets_candidate_r1"
    relation_dir = artifact_dir / "objective_relation_catalogs_r1"
    focus_dir = artifact_dir / "objective_focus_catalogs_r1"
    schema_path = artifact_dir / manifest["output_schema_file"]
    prompt_path = artifact_dir / manifest["prompt_file"]
    completed = 0
    missing: list[str] = []
    outputs: dict[str, dict[int, dict[str, Any]]] = {}
    validations: dict[str, dict[int, dict[str, Any]]] = {}
    for row in manifest["rows"]:
        review_id = row["review_id"]
        for round_number in range(1, manifest["required_rounds"] + 1):
            output_path, receipt_path = _artifact_paths(run_directory, round_number, review_id)
            key = f"round_{round_number}:{review_id}"
            if not output_path.exists() and not receipt_path.exists():
                missing.append(key)
                continue
            try:
                validation = validate_existing_stage_b1a1_artifacts(
                    input_packet_path=input_dir / row["input_packet_file"],
                    source_b1a_input_path=source_dir / f"{review_id}.json",
                    relation_catalog_path=relation_dir / f"{review_id}.json",
                    focus_catalog_path=focus_dir / f"{review_id}.json",
                    schema_path=schema_path,
                    prompt_path=prompt_path,
                    output_path=output_path,
                    receipt_path=receipt_path,
                )
            except (StageB1a1RunnerError, OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"ARTIFACT:{key}:{exc}")
                continue
            completed += 1
            outputs.setdefault(review_id, {})[round_number] = load_json(output_path)
            validations.setdefault(review_id, {})[round_number] = validation

    expected = manifest["expected_case_rounds"]
    base = {
        "manifest_version": manifest["manifest_version"],
        "status": "",
        "formal_model": manifest["formal_model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "case_count": manifest["case_count"],
        "required_rounds": manifest["required_rounds"],
        "expected_case_rounds": expected,
        "completed_case_rounds": completed,
        "missing_case_rounds": sorted(missing),
        "error_count": len(set(errors)),
        "errors": sorted(set(errors)),
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "metrics_published": False,
        "metrics": None,
    }
    if errors:
        base["status"] = "SMOKE_PROTOCOL_FAILED（Smoke協定失敗）"
        return base
    if completed != expected:
        base["status"] = "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）"
        return base

    atom_total = atom_result_same = atom_primary_same = atom_full_same = 0
    pool_total = pool_same = 0
    cases = []
    for row in manifest["rows"]:
        review_id = row["review_id"]
        indexes = {
            round_number: _assessment_index(outputs[review_id][round_number])
            for round_number in range(1, 4)
        }
        candidate_ids = sorted(indexes[1])
        case_total = case_same = case_primary_same = 0
        for candidate_id in candidate_ids:
            for field in ATOMIC_FIELDS:
                values = [indexes[round_number][candidate_id][field] for round_number in range(1, 4)]
                atom_total += 1
                case_total += 1
                if len({value["result"] for value in values}) == 1:
                    atom_result_same += 1
                    case_same += 1
                if len({tuple(value["primary_evidence_refs"]) for value in values}) == 1:
                    atom_primary_same += 1
                    case_primary_same += 1
                if len({_full_signature(value) for value in values}) == 1:
                    atom_full_same += 1
        pools = {
            tuple(validations[review_id][round_number]["eligible_ids"])
            for round_number in range(1, 4)
        }
        pool_sizes = [
            validations[review_id][round_number]["eligible_count"]
            for round_number in range(1, 4)
        ]
        pool_total += 1
        if len(pools) == 1:
            pool_same += 1
        cases.append(
            {
                "review_id": review_id,
                "candidate_count": len(candidate_ids),
                "atomic_result_consistency_percent": _pct(case_same, case_total),
                "primary_evidence_consistency_percent": _pct(
                    case_primary_same, case_total
                ),
                "eligible_set_consistent": len(pools) == 1,
                "eligible_sizes_by_round": pool_sizes,
            }
        )
    metrics = {
        "schema_and_semantic_valid_percent": 100.0,
        "atomic_result_consistency_percent": _pct(atom_result_same, atom_total),
        "primary_evidence_consistency_percent": _pct(atom_primary_same, atom_total),
        "full_evidence_consistency_percent": _pct(atom_full_same, atom_total),
        "eligible_set_consistency_percent": _pct(pool_same, pool_total),
        "atomic_comparisons": atom_total,
        "pool_comparisons": pool_total,
        "cases": cases,
    }
    gates = [
        metrics["atomic_result_consistency_percent"],
        metrics["primary_evidence_consistency_percent"],
        metrics["eligible_set_consistency_percent"],
    ]
    if any(value < FAIL_THRESHOLD for value in gates):
        status = "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）"
    elif any(value < PASS_THRESHOLD for value in gates):
        status = "SMOKE_REVISION_REQUIRED（Smoke需要修訂）"
    else:
        status = "SMOKE_PASSED（Smoke通過）"
    base.update({"status": status, "metrics_published": True, "metrics": metrics})
    return base


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Stage B1a1 R2 Smoke 比較報告",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 正式模型：`{report['formal_model']}／{report['reasoning_effort']}`",
        f"- 案例輪次：{report['completed_case_rounds']}／{report['expected_case_rounds']}",
        "- 未使用股票身分、sealed舊答案或未來績效。",
    ]
    if not report["metrics_published"]:
        lines.extend(["", "三輪尚未完整且合法完成，因此不發布部分一致率。"])
        return "\n".join(lines) + "\n"
    metrics = report["metrics"]
    lines.extend(
        [
            "",
            "## 門檻結果",
            "",
            f"- 四原子結果一致率：{metrics['atomic_result_consistency_percent']:.2f}%",
            f"- 主證據一致率：{metrics['primary_evidence_consistency_percent']:.2f}%",
            f"- 完整證據一致率：{metrics['full_evidence_consistency_percent']:.2f}%（診斷項）",
            f"- B1a1合格集合一致率：{metrics['eligible_set_consistency_percent']:.2f}%",
            "",
            "| 案例 | 候選數 | 原子結果 | 主證據 | 合格集合一致 | 各輪合格數 |",
            "|---|---:|---:|---:|---|---|",
        ]
    )
    for row in metrics["cases"]:
        sizes = "／".join(str(value) for value in row["eligible_sizes_by_round"])
        lines.append(
            f"| `{row['review_id']}` | {row['candidate_count']} | "
            f"{row['atomic_result_consistency_percent']:.2f}% | "
            f"{row['primary_evidence_consistency_percent']:.2f}% | "
            f"{'是' if row['eligible_set_consistent'] else '否'} | {sizes} |"
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
    return 0 if report["status"] != "SMOKE_PROTOCOL_FAILED（Smoke協定失敗）" else 1


if __name__ == "__main__":
    raise SystemExit(main())
