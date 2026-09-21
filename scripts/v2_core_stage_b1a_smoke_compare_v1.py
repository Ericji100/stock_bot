"""Revalidate and compare three Stage B1a smoke rounds.

Incomplete runs never publish consistency percentages.  Complete runs compare
AI atomic results, evidence signatures, role sets, and the program-derived deep
review pool.  The comparator does not open sealed labels, identities, or future
performance.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_stage_b1a_candidate_validator_v1 import (  # noqa: E402
    ATOMIC_FIELDS,
    load_json,
    sha256_file,
)
from scripts.v2_core_stage_b1a_codex_runner_v1 import (  # noqa: E402
    StageB1aRunnerError,
    validate_existing_stage_b1a_artifacts,
)


PASS_THRESHOLD = 90.0
FAIL_THRESHOLD = 75.0


def _artifact_paths(run_dir: Path, round_number: int, review_id: str) -> tuple[Path, Path]:
    round_dir = run_dir / f"round_{round_number}"
    return (
        round_dir / "stage_b1a" / f"{review_id}.json",
        round_dir / "receipts" / f"{review_id}.stage_b1a.json",
    )


def _verify_manifest(manifest: dict[str, Any], artifact_dir: Path) -> list[str]:
    errors: list[str] = []
    expected_top = {
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "required_rounds": 3,
        "expected_case_rounds": int(manifest.get("case_count", 0)) * 3,
    }
    for key, expected in expected_top.items():
        if manifest.get(key) != expected:
            errors.append(f"MANIFEST:{key}:EXPECTED_{expected}:ACTUAL_{manifest.get(key)}")

    for file_key, hash_key in (
        ("prompt_file", "prompt_sha256"),
        ("output_schema_file", "output_schema_sha256"),
        ("input_manifest_file", "input_manifest_sha256"),
        ("smoke_gate_file", "smoke_gate_sha256"),
    ):
        path = artifact_dir / str(manifest.get(file_key, ""))
        if not path.is_file():
            errors.append(f"MANIFEST:MISSING_COMPONENT:{file_key}")
        elif sha256_file(path) != manifest.get(hash_key):
            errors.append(f"MANIFEST:HASH_MISMATCH:{file_key}")

    for filename, expected_hash in (manifest.get("repo_components") or {}).items():
        path = ROOT / filename
        if not path.is_file():
            errors.append(f"MANIFEST:MISSING_REPO_COMPONENT:{filename}")
        elif sha256_file(path) != expected_hash:
            errors.append(f"MANIFEST:REPO_HASH_MISMATCH:{filename}")

    input_dir = artifact_dir / "stage_b1a_input_packets_candidate_r1"
    candidate_dir = artifact_dir / "objective_candidate_catalogs_r1"
    focus_dir = artifact_dir / "objective_focus_catalogs_r1"
    for row in manifest.get("rows", []):
        review_id = str(row.get("review_id", ""))
        input_path = input_dir / str(row.get("input_packet_file", ""))
        candidate_path = candidate_dir / f"{review_id}.json"
        focus_path = focus_dir / f"{review_id}.json"
        for label, path, expected_hash in (
            ("INPUT", input_path, row.get("input_packet_sha256")),
            ("CANDIDATE", candidate_path, row.get("segment_catalog_sha256")),
            ("FOCUS", focus_path, row.get("focus_catalog_sha256")),
        ):
            if not path.is_file():
                errors.append(f"MANIFEST:{review_id}:MISSING_{label}")
            elif sha256_file(path) != expected_hash:
                errors.append(f"MANIFEST:{review_id}:{label}_HASH_MISMATCH")
    return sorted(set(errors))


def _assessment_index(output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row["candidate_id"]): row
        for row in output["segment_assessments"]
    }


def _atomic_result_signature(value: dict[str, Any]) -> str:
    return str(value["result"])


def _atomic_full_signature(value: dict[str, Any]) -> str:
    normalized = {
        "result": value["result"],
        "supporting_evidence_refs": sorted(value["supporting_evidence_refs"]),
        "contradicting_evidence_refs": sorted(value["contradicting_evidence_refs"]),
        "missing_evidence_codes": sorted(value["missing_evidence_codes"]),
        "reason_code": value["reason_code"],
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def compare_smoke(
    *,
    manifest_path: Path,
    artifact_dir: Path,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    errors = _verify_manifest(manifest, artifact_dir)
    run_directory = run_dir or artifact_dir / str(manifest["run_directory"])
    prompt_path = artifact_dir / str(manifest["prompt_file"])
    schema_path = artifact_dir / str(manifest["output_schema_file"])
    input_dir = artifact_dir / "stage_b1a_input_packets_candidate_r1"
    candidate_dir = artifact_dir / "objective_candidate_catalogs_r1"
    focus_dir = artifact_dir / "objective_focus_catalogs_r1"

    expected = int(manifest["expected_case_rounds"])
    completed = 0
    outputs: dict[str, dict[int, dict[str, Any]]] = {}
    validations: dict[str, dict[int, dict[str, Any]]] = {}
    missing: list[str] = []

    for row in manifest["rows"]:
        review_id = str(row["review_id"])
        input_path = input_dir / str(row["input_packet_file"])
        candidate_path = candidate_dir / f"{review_id}.json"
        focus_path = focus_dir / f"{review_id}.json"
        for round_number in range(1, int(manifest["required_rounds"]) + 1):
            output_path, receipt_path = _artifact_paths(
                run_directory, round_number, review_id
            )
            key = f"round_{round_number}:{review_id}"
            if not output_path.exists() and not receipt_path.exists():
                missing.append(key)
                continue
            try:
                validation = validate_existing_stage_b1a_artifacts(
                    input_packet_path=input_path,
                    candidate_catalog_path=candidate_path,
                    focus_catalog_path=focus_path,
                    schema_path=schema_path,
                    prompt_path=prompt_path,
                    output_path=output_path,
                    receipt_path=receipt_path,
                )
            except (StageB1aRunnerError, OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"ARTIFACT:{key}:{exc}")
                continue
            completed += 1
            outputs.setdefault(review_id, {})[round_number] = load_json(output_path)
            validations.setdefault(review_id, {})[round_number] = validation

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
        "error_count": len(errors),
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

    atomic_total = 0
    atomic_result_same = 0
    atomic_full_same = 0
    role_total = 0
    role_same = 0
    pool_total = 0
    pool_same = 0
    case_rows: list[dict[str, Any]] = []
    for row in manifest["rows"]:
        review_id = str(row["review_id"])
        round_indexes = {
            round_number: _assessment_index(outputs[review_id][round_number])
            for round_number in range(1, int(manifest["required_rounds"]) + 1)
        }
        candidate_ids = sorted(round_indexes[1])
        case_atomic_total = 0
        case_atomic_same = 0
        case_role_same = 0
        for candidate_id in candidate_ids:
            for field in ATOMIC_FIELDS:
                values = [
                    round_indexes[round_number][candidate_id][field]
                    for round_number in range(1, int(manifest["required_rounds"]) + 1)
                ]
                atomic_total += 1
                case_atomic_total += 1
                if len({_atomic_result_signature(value) for value in values}) == 1:
                    atomic_result_same += 1
                    case_atomic_same += 1
                if len({_atomic_full_signature(value) for value in values}) == 1:
                    atomic_full_same += 1
            roles = {
                tuple(sorted(round_indexes[round_number][candidate_id]["role_candidates"]))
                for round_number in range(1, int(manifest["required_rounds"]) + 1)
            }
            role_total += 1
            if len(roles) == 1:
                role_same += 1
                case_role_same += 1
        pools = {
            tuple(validations[review_id][round_number]["deep_review_pool_ids"])
            for round_number in range(1, int(manifest["required_rounds"]) + 1)
        }
        pool_total += 1
        if len(pools) == 1:
            pool_same += 1
        pool_sizes = [
            validations[review_id][round_number]["deep_review_pool_count"]
            for round_number in range(1, int(manifest["required_rounds"]) + 1)
        ]
        case_rows.append(
            {
                "review_id": review_id,
                "candidate_count": len(candidate_ids),
                "atomic_result_consistency_percent": _pct(
                    case_atomic_same, case_atomic_total
                ),
                "role_set_consistency_percent": _pct(case_role_same, len(candidate_ids)),
                "deep_review_pool_consistent": len(pools) == 1,
                "deep_review_pool_sizes_by_round": pool_sizes,
                "overflow_rounds": [
                    index + 1 for index, size in enumerate(pool_sizes) if size > 4
                ],
            }
        )

    metrics = {
        "schema_and_semantic_valid_percent": 100.0,
        "atomic_result_consistency_percent": _pct(atomic_result_same, atomic_total),
        "atomic_full_evidence_consistency_percent": _pct(atomic_full_same, atomic_total),
        "role_set_consistency_percent": _pct(role_same, role_total),
        "deep_review_pool_consistency_percent": _pct(pool_same, pool_total),
        "atomic_comparisons": atomic_total,
        "role_comparisons": role_total,
        "pool_comparisons": pool_total,
        "cases": case_rows,
    }
    gate_values = [
        metrics["atomic_result_consistency_percent"],
        metrics["role_set_consistency_percent"],
        metrics["deep_review_pool_consistency_percent"],
    ]
    if any(value < FAIL_THRESHOLD for value in gate_values):
        status = "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）"
    elif any(value < PASS_THRESHOLD for value in gate_values):
        status = "SMOKE_REVISION_REQUIRED（Smoke需要修訂）"
    else:
        status = "SMOKE_PASSED（Smoke通過）"
    base.update({"status": status, "metrics_published": True, "metrics": metrics})
    return base


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Stage B1a Smoke 比較報告",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 正式模型：`{report['formal_model']}／{report['reasoning_effort']}`",
        f"- 案例輪次：{report['completed_case_rounds']}／{report['expected_case_rounds']}",
        "- 未使用股票身分、sealed舊答案或未來績效。",
    ]
    if not report["metrics_published"]:
        lines.extend(
            [
                "",
                "三輪尚未完整且合法完成，因此不發布部分一致率。",
            ]
        )
        if report["errors"]:
            lines.extend(["", "## 錯誤", ""])
            lines.extend(f"- `{error}`" for error in report["errors"])
        return "\n".join(lines) + "\n"

    metrics = report["metrics"]
    lines.extend(
        [
            "",
            "## 門檻結果",
            "",
            f"- 粗篩原子結果一致率：{metrics['atomic_result_consistency_percent']:.2f}%",
            f"- 原子完整證據簽章一致率：{metrics['atomic_full_evidence_consistency_percent']:.2f}%（診斷項）",
            f"- 角色集合一致率：{metrics['role_set_consistency_percent']:.2f}%",
            f"- deep review pool一致率：{metrics['deep_review_pool_consistency_percent']:.2f}%",
            "",
            "| 案例 | 候選數 | 原子結果一致率 | 角色一致率 | Pool一致 | 各輪Pool數 | Overflow輪次 |",
            "|---|---:|---:|---:|---|---|---|",
        ]
    )
    for row in metrics["cases"]:
        lines.append(
            "| `{review_id}` | {candidate_count} | {atomic_result_consistency_percent:.2f}% | "
            "{role_set_consistency_percent:.2f}% | {pool} | {sizes} | {overflow} |".format(
                **row,
                pool="是" if row["deep_review_pool_consistent"] else "否",
                sizes="／".join(str(value) for value in row["deep_review_pool_sizes_by_round"]),
                overflow="、".join(str(value) for value in row["overflow_rounds"]) or "無",
            )
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
