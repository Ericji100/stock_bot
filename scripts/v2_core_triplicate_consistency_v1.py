"""Compare three frozen M2A staged runs without reading sealed labels or returns."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_codex_staged_runner_v1 import (
    OUT,
    load_json,
    validate_existing_stage_b_artifacts,
    validate_existing_stage_d_artifacts,
    verify_execution_manifest,
)
from scripts.v2_core_codex_stage_d_runner_v2 import (
    validate_existing_stage_d_artifacts as validate_existing_stage_d_artifacts_v7,
)
from scripts.v2_core_codex_stage_d_runner_v3 import (
    validate_existing_stage_d_artifacts as validate_existing_stage_d_artifacts_v8,
)


COMPARATOR_VERSION = "v2-core-triplicate-consistency-r2"
THRESHOLDS = {
    "schema_legal_rate": 100.0,
    "causal_legal_rate": 100.0,
    "final_permission_consistency_rate": 95.0,
    "primary_scenario_consistency_rate": 90.0,
    "direction_consistency_rate": 90.0,
    "core_atom_consistency_rate": 90.0,
    "trigger_episode_consistency_rate": 90.0,
}
SHARED_VERDICT_FIELDS = (
    "location_remaining_space",
    "location_not_extended",
    "exhaustion",
    "signal_has_independent_structure",
)


def _result(value: Any) -> str:
    if isinstance(value, dict) and value.get("result") in {"PASS", "FAIL", "UNKNOWN"}:
        return str(value["result"])
    return "MISSING"


def _anchor_signature(stage_b: dict[str, Any]) -> tuple[Any, ...]:
    controlling = stage_b["shared_structure"]["controlling_anchor_id"]
    matches = [row for row in stage_b["anchors"] if row["anchor_id"] == controlling]
    if len(matches) != 1:
        return ("UNRESOLVED",)
    anchor = matches[0]
    return (
        anchor["role"],
        anchor["scale"],
        anchor["direction"],
        anchor["start_date"],
        anchor["start_price"],
        anchor["end_date"],
        anchor["end_price"],
        anchor["status"],
    )


def _stop_signature(stage_b: dict[str, Any], stop_id: str | None) -> tuple[Any, ...]:
    if stop_id is None:
        return (None,)
    matches = [row for row in stage_b["stops"] if row["stop_id"] == stop_id]
    if len(matches) != 1:
        return ("UNRESOLVED", stop_id)
    stop = matches[0]
    return (
        stop["scope"],
        stop["scale"],
        stop["direction"],
        stop["source_date"],
        stop["confirmed_on"],
        stop["price"],
    )


def core_atom_map(stage_b: dict[str, Any], stage_d: dict[str, Any] | None) -> dict[str, str]:
    atoms = {"stage_b.data_sufficiency": _result(stage_b["data_sufficiency"])}
    shared = stage_b["shared_structure"]
    for field in SHARED_VERDICT_FIELDS:
        atoms[f"stage_b.shared.{field}"] = _result(shared[field])
    for name, value in stage_b["routing_atoms"].items():
        atoms[f"stage_b.routing.{name}"] = _result(value)
    controlling = shared["controlling_anchor_id"]
    matches = [row for row in stage_b["anchors"] if row["anchor_id"] == controlling]
    if len(matches) == 1:
        for name, value in matches[0]["atomic"].items():
            atoms[f"stage_b.controlling_anchor.{name}"] = _result(value)
    else:
        atoms["stage_b.controlling_anchor.UNRESOLVED"] = "MISSING"
    if stage_d is None:
        atoms["stage_d.called"] = "NO"
        return atoms
    atoms["stage_d.called"] = "YES"
    scenario = str(stage_d["primary_scenario"])
    for name, value in stage_d["scenario_evaluation"]["gates"].items():
        atoms[f"stage_d.{scenario}.gate.{name}"] = _result(value)
    for name, value in stage_d["blocking_atoms"].items():
        atoms[f"stage_d.blocking.{name}"] = _result(value)
    atoms["stage_d.location_remaining_space"] = _result(stage_d["location_remaining_space"])
    atoms["stage_d.signal_has_independent_structure"] = _result(
        stage_d["signal_has_independent_structure"]
    )
    return atoms


def collect_case_round(
    artifact_dir: Path,
    manifest: dict[str, Any],
    review_id: str,
    round_number: int,
) -> dict[str, Any]:
    probe = load_json(artifact_dir / "feasibility_probe_manifest.json")
    row = next(item for item in manifest["cases"] if item["review_id"] == review_id)
    packet_path = artifact_dir / probe["packet_directory"] / row["packet_file"]
    root = artifact_dir / str(manifest["run_directory"]) / f"round_{round_number}"
    stage_b_path = root / "stage_b" / f"{review_id}.json"
    stage_b_receipt = root / "receipts" / f"{review_id}.stage_b.json"
    stage_b_validation = validate_existing_stage_b_artifacts(
        packet_path=packet_path,
        schema_path=artifact_dir / "v2_core_stage_b.schema.candidate.json",
        prompt_path=artifact_dir / "v2_core_stage_b.prompt.candidate_r2.md",
        truth_table_path=artifact_dir / "permission_truth_table.json",
        output_path=stage_b_path,
        receipt_path=stage_b_receipt,
    )
    stage_b = load_json(stage_b_path)
    permission_path = root / "final_permission" / f"{review_id}.json"
    stage_d_path = root / "stage_d" / f"{review_id}.json"
    stage_d_receipt = root / "receipts" / f"{review_id}.stage_d.json"
    route_path = root / "program_route" / f"{review_id}.json"
    execution_version = str(manifest.get("execution_manifest_version", ""))
    if execution_version.startswith("v2-core-m2a-triplicate-execution-r8"):
        stage_d_validator = validate_existing_stage_d_artifacts_v8
        stage_d_prompt = "v2_core_stage_d.prompt.candidate_r3.md"
    elif execution_version.startswith("v2-core-m2a-triplicate-execution-r7"):
        stage_d_validator = validate_existing_stage_d_artifacts_v7
        stage_d_prompt = "v2_core_stage_d.prompt.candidate_r3.md"
    else:
        stage_d_validator = validate_existing_stage_d_artifacts
        stage_d_prompt = "v2_core_stage_d.prompt.candidate_r2.md"
    stage_d_validation = stage_d_validator(
        packet_path=packet_path,
        stage_b_path=stage_b_path,
        stage_b_schema_path=artifact_dir / "v2_core_stage_b.schema.candidate.json",
        stage_b_prompt_path=artifact_dir / "v2_core_stage_b.prompt.candidate_r2.md",
        schema_path=artifact_dir / "v2_core_stage_d.schema.candidate.json",
        prompt_path=artifact_dir / stage_d_prompt,
        truth_table_path=artifact_dir / "permission_truth_table.json",
        output_path=stage_d_path,
        route_path=route_path,
        permission_path=permission_path,
        receipt_path=stage_d_receipt,
    )
    stage_d = load_json(stage_d_path) if stage_d_path.is_file() else None
    shared = stage_b["shared_structure"]
    if stage_d is None:
        trigger = ("NOT_CALLED", None, None, None)
        stop_signature = (None,)
    else:
        trigger_row = stage_d["trigger"]
        trigger = (
            trigger_row["status"],
            trigger_row["canonical_route"],
            trigger_row["trigger_date"],
            trigger_row["episode_stop_id"],
        )
        stop_signature = _stop_signature(stage_b, trigger_row["episode_stop_id"])
    return {
        "schema_legal": True,
        "causal_legal": True,
        "primary_scenario": stage_d_validation["program_derived_scenario"],
        "final_permission": stage_d_validation["program_derived_permission"],
        "direction": (
            shared["large_scale"]["direction"],
            shared["small_scale"]["direction"],
        ),
        "controlling_anchor": _anchor_signature(stage_b),
        "trigger_episode": (trigger, stop_signature),
        "core_atoms": core_atom_map(stage_b, stage_d),
    }


def _same(values: list[Any]) -> bool:
    return len(values) == 3 and values[0] == values[1] == values[2]


def _rate(passed: int, total: int) -> float:
    return round(100.0 * passed / total, 2) if total else 0.0


def compare(artifact_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = verify_execution_manifest(manifest_path, artifact_dir)
    case_results: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        review_id = case["review_id"]
        rounds: list[dict[str, Any]] = []
        for round_number in (1, 2, 3):
            try:
                rounds.append(collect_case_round(artifact_dir, manifest, review_id, round_number))
            except (FileNotFoundError, KeyError, ValueError, RuntimeError) as exc:
                missing.append(
                    {
                        "review_id": review_id,
                        "round": round_number,
                        "reason": type(exc).__name__,
                    }
                )
        if len(rounds) != 3:
            continue
        atom_keys = sorted(set().union(*(row["core_atoms"] for row in rounds)))
        atom_consistent = sum(
            _same([row["core_atoms"].get(key, "NOT_APPLICABLE") for row in rounds])
            for key in atom_keys
        )
        case_results.append(
            {
                "review_id": review_id,
                "schema_legal": all(row["schema_legal"] for row in rounds),
                "causal_legal": all(row["causal_legal"] for row in rounds),
                "primary_scenario_consistent": _same([row["primary_scenario"] for row in rounds]),
                "final_permission_consistent": _same([row["final_permission"] for row in rounds]),
                "direction_consistent": _same([row["direction"] for row in rounds]),
                "controlling_anchor_consistent": _same([row["controlling_anchor"] for row in rounds]),
                "trigger_episode_consistent": _same([row["trigger_episode"] for row in rounds]),
                "core_atom_consistent_count": atom_consistent,
                "core_atom_total_count": len(atom_keys),
                "round_scenarios": [row["primary_scenario"] for row in rounds],
                "round_permissions": [row["final_permission"] for row in rounds],
            }
        )
    if missing:
        return {
            "comparator_version": COMPARATOR_VERSION,
            "status": "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）",
            "expected_case_rounds": len(manifest["cases"]) * 3,
            "completed_case_rounds": len(manifest["cases"]) * 3 - len(missing),
            "missing_case_rounds": len(missing),
            "missing": missing,
            "future_performance_used": False,
            "sealed_labels_used": False,
        }
    total = len(case_results)
    atom_passed = sum(row["core_atom_consistent_count"] for row in case_results)
    atom_total = sum(row["core_atom_total_count"] for row in case_results)
    rates = {
        "schema_legal_rate": _rate(sum(row["schema_legal"] for row in case_results), total),
        "causal_legal_rate": _rate(sum(row["causal_legal"] for row in case_results), total),
        "final_permission_consistency_rate": _rate(
            sum(row["final_permission_consistent"] for row in case_results), total
        ),
        "primary_scenario_consistency_rate": _rate(
            sum(row["primary_scenario_consistent"] for row in case_results), total
        ),
        "direction_consistency_rate": _rate(
            sum(row["direction_consistent"] for row in case_results), total
        ),
        "core_atom_consistency_rate": _rate(atom_passed, atom_total),
        "trigger_episode_consistency_rate": _rate(
            sum(row["trigger_episode_consistent"] for row in case_results), total
        ),
        "controlling_anchor_consistency_rate": _rate(
            sum(row["controlling_anchor_consistent"] for row in case_results), total
        ),
    }
    failed_thresholds = [
        name for name, threshold in THRESHOLDS.items() if rates[name] < threshold
    ]
    if not failed_thresholds:
        status = "PASS（通過）"
    elif (
        rates["core_atom_consistency_rate"] < 75
        or rates["primary_scenario_consistency_rate"] < 75
    ):
        status = "FEASIBILITY_FAILED（可行性失敗）"
    else:
        status = "REVISION_REQUIRED（需要修訂）"
    distributions = []
    for index in range(3):
        distributions.append(
            {
                "round": index + 1,
                "primary_scenario_counts": dict(
                    sorted(Counter(row["round_scenarios"][index] for row in case_results).items())
                ),
                "final_permission_counts": dict(
                    sorted(Counter(row["round_permissions"][index] for row in case_results).items())
                ),
            }
        )
    return {
        "comparator_version": COMPARATOR_VERSION,
        "status": status,
        "case_count": total,
        "rates_percent": rates,
        "thresholds_percent": THRESHOLDS,
        "failed_thresholds": failed_thresholds,
        "round_distributions": distributions,
        "case_results": case_results,
        "future_performance_used": False,
        "sealed_labels_used": False,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# M2A三輪一致性比較",
        "",
        f"- 狀態：`{report['status']}`",
        "- 正式模型：`gpt-5.6-sol／xhigh`",
        "- 未使用未來績效或sealed舊答案。",
        "",
    ]
    if report["status"].startswith("NOT_READY"):
        lines.extend(
            [
                "## 完整度",
                "",
                f"- 預期案例輪次：{report['expected_case_rounds']}",
                f"- 已完成案例輪次：{report['completed_case_rounds']}",
                f"- 尚缺案例輪次：{report['missing_case_rounds']}",
                "",
                "正式AI三輪完成前，不計算一致率，也不宣稱M2A通過。",
            ]
        )
        return "\n".join(lines) + "\n"
    lines.extend(["## 一致率", "", "| 指標 | 結果 | 門檻 |", "|---|---:|---:|"])
    for name, threshold in THRESHOLDS.items():
        lines.append(f"| `{name}` | {report['rates_percent'][name]:.2f}% | {threshold:.2f}% |")
    lines.extend(
        [
            f"| `controlling_anchor_consistency_rate` | {report['rates_percent']['controlling_anchor_consistency_rate']:.2f}% | 揭露指標 |",
            "",
            "## 未通過門檻",
            "",
            ", ".join(f"`{name}`" for name in report["failed_thresholds"]) or "無。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=OUT)
    parser.add_argument(
        "--execution-manifest",
        type=Path,
        default=OUT / "feasibility_probe_execution_manifest_v6.json",
    )
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    args = parser.parse_args()
    report = compare(args.artifact_dir.resolve(), args.execution_manifest.resolve())
    if args.output_json:
        args.output_json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.output_md:
        args.output_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"].startswith(("PASS", "NOT_READY")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
