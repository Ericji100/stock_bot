"""Audit three independent AI runs of the V4-S1/V6 causal probe."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any

from .hybrid_v3_atomic_runner_v2 import _validate_merge_envelope
from .hybrid_v3_consistency_v2 import critical_question_ids
from .hybrid_v3_sharding_v2 import _publish_immutable, canonical_json_bytes, canonical_sha256
from .hybrid_v3_triplicate_smoke_audit import result_map
from .hybrid_v4_s1_atomic_policy_v1 import (
    reduce_atomic_v4_s1,
    stage_permission_v4_s1,
    validate_atomic,
)
from .hybrid_v4_s1_consistency_v1 import decision_signature


VERSION = "hybrid-v4-s1-v6-causal-probe-report-v1"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"expected one or more JSON objects: {path}")
    return rows


def _run_map(
    root: Path, records: list[dict[str, Any]], run_number: int
) -> dict[str, dict[str, Any]]:
    expected = {str(row["review_id"]): row for row in records}
    found: dict[str, dict[str, Any]] = {}
    for path in sorted((Path(root) / "cases").glob("*.json")):
        envelope = _read_json(path)
        review_id = str(envelope.get("review_id") or "")
        if review_id not in expected or review_id in found:
            raise ValueError(f"unexpected or duplicate review_id in {root}")
        _validate_merge_envelope(expected[review_id], envelope, run_number)
        errors = validate_atomic(expected[review_id]["packet"], envelope["output"])
        if errors:
            raise ValueError(f"atomic validation failed for {review_id}: {errors}")
        found[review_id] = envelope
    if set(found) != set(expected):
        raise ValueError(f"run {run_number} coverage differs")
    return found


def evaluate(
    *,
    case_records: Path,
    run_roots: list[Path],
    protocol: Path,
    schema: Path,
) -> dict[str, Any]:
    records = _read_jsonl(case_records)
    if len(run_roots) != 3:
        raise ValueError("causal probe requires exactly three runs")
    runs = [
        _run_map(root, records, run_number)
        for run_number, root in enumerate(run_roots, 1)
    ]
    critical_ids = critical_question_ids(_read_json(protocol), _read_json(schema))
    per_case: list[dict[str, Any]] = []
    pooled_exact = pooled_total = critical_exact = critical_total = 0
    permission_counts = [Counter() for _ in runs]
    unanimous_trades = exact_permissions = exact_scenario_stage = 0
    for record in records:
        review_id = str(record["review_id"])
        outputs = [run[review_id]["output"] for run in runs]
        decisions = [reduce_atomic_v4_s1(record["packet"], output) for output in outputs]
        permissions = [stage_permission_v4_s1(decision) for decision in decisions]
        signatures = [decision_signature(decision) for decision in decisions]
        for index, permission in enumerate(permissions):
            permission_counts[index][permission] += 1
        permission_exact = len(set(permissions)) == 1
        scenario_stage_exact = len(
            {
                (row["scenario"], row["stage"], row["left_right_phase"])
                for row in signatures
            }
        ) == 1
        exact_permissions += int(permission_exact)
        exact_scenario_stage += int(scenario_stage_exact)
        unanimous_trades += int(permission_exact and permissions[0] == "TRADE")

        maps = [result_map(output) for output in outputs]
        paths = sorted(set().union(*(set(mapping) for mapping in maps)))
        if any(set(mapping) != set(paths) for mapping in maps):
            raise ValueError(f"atomic manifest differs for {review_id}")
        critical_paths = [path for path in paths if path.rsplit(".", 1)[-1] in critical_ids]
        atom_exact = sum(len({mapping[path] for mapping in maps}) == 1 for path in paths)
        critical_case_exact = sum(
            len({mapping[path] for mapping in maps}) == 1 for path in critical_paths
        )
        pooled_exact += atom_exact
        pooled_total += len(paths)
        critical_exact += critical_case_exact
        critical_total += len(critical_paths)
        per_case.append(
            {
                "review_id": review_id,
                "anonymous_stock_id": record["anonymous_stock_id"],
                "as_of": record["packet"]["as_of"],
                "run_stage_permissions": permissions,
                "run_decisions": signatures,
                "material_permission_exact": permission_exact,
                "scenario_stage_phase_exact": scenario_stage_exact,
                "atomic_agreement": {
                    "exact": atom_exact,
                    "total": len(paths),
                    "rate": atom_exact / len(paths),
                },
                "critical_atomic_agreement": {
                    "exact": critical_case_exact,
                    "total": len(critical_paths),
                    "rate": critical_case_exact / len(critical_paths)
                    if critical_paths
                    else 0.0,
                },
            }
        )
    report_core = {
        "report_version": VERSION,
        "status": "DIAGNOSTIC_HAS_UNANIMOUS_TRADE"
        if unanimous_trades
        else "DIAGNOSTIC_NO_UNANIMOUS_TRADE",
        "classification": "OUTCOME_BLIND_CAUSAL_PACKET_PROBE_NOT_FORMAL_ACCEPTANCE",
        "cases": len(records),
        "runs": 3,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "formal_acceptance_claim_allowed": False,
        "metrics": {
            "schema_and_causal_validity": 1.0,
            "material_permission_exact": exact_permissions / len(records),
            "scenario_stage_phase_exact": exact_scenario_stage / len(records),
            "all_atomic_answers": {
                "exact": pooled_exact,
                "total": pooled_total,
                "rate": pooled_exact / pooled_total,
            },
            "critical_atomic_answers": {
                "exact": critical_exact,
                "total": critical_total,
                "rate": critical_exact / critical_total if critical_total else 0.0,
            },
            "unanimous_trade_cases": unanimous_trades,
            "run_permission_counts": [dict(row) for row in permission_counts],
        },
        "per_case": per_case,
    }
    return {**report_core, "report_sha256": canonical_sha256(report_core)}


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# V4-S1／V6 精確因果封包三輪探針",
        "",
        f"- 結果：`{report['status']}`",
        "- 性質：匿名、結果盲化的小樣本診斷；不是正式一致性驗收或績效報告。",
        f"- 案例／輪數：{report['cases']} 案 × {report['runs']} 輪",
        f"- 權限一致率：{metrics['material_permission_exact']:.2%}",
        f"- 情境＋位階一致率：{metrics['scenario_stage_phase_exact']:.2%}",
        f"- 全部原子一致率：{metrics['all_atomic_answers']['rate']:.2%}",
        f"- 關鍵原子一致率：{metrics['critical_atomic_answers']['rate']:.2%}",
        f"- 三輪一致進場案例：{metrics['unanimous_trade_cases']}",
        "",
        "| 匿名案例 | 截止日 | 三輪權限 | 三輪情境／位階 | 原子一致率 |",
        "|---|---|---|---|---:|",
    ]
    for row in report["per_case"]:
        decisions = "；".join(
            f"{item['scenario']}/{item['stage']}" for item in row["run_decisions"]
        )
        lines.append(
            f"| {row['review_id']} | {row['as_of']} | "
            f"{' / '.join(row['run_stage_permissions'])} | {decisions} | "
            f"{row['atomic_agreement']['rate']:.2%} |"
        )
    lines.extend(
        [
            "",
            "## 限制",
            "",
            "本探針只回答修正因果封包後，單一可達案例是否能形成穩定判讀。它不能取代跨股票、跨月份的正式三輪驗收，也尚未開封未來績效。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-records", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, action="append", required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(
        case_records=args.case_records,
        run_roots=args.run_root,
        protocol=args.protocol,
        schema=args.schema,
    )
    _publish_immutable(args.output_json, canonical_json_bytes(report) + b"\n")
    _publish_immutable(args.output_md, render_markdown(report).encode("utf-8"))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
