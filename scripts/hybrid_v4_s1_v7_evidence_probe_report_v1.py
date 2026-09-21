"""Audit three independent AI runs of the V4-S1/V7 evidence probe."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .hybrid_v3_sharding_v2 import _publish_immutable, canonical_json_bytes, canonical_sha256
from .hybrid_v4_s1_v6_causal_probe_report_v1 import evaluate as evaluate_base


VERSION = "hybrid-v4-s1-v7-evidence-probe-report-v1"


def evaluate(
    *,
    case_records: Path,
    run_roots: list[Path],
    protocol: Path,
    schema: Path,
) -> dict[str, Any]:
    report = evaluate_base(
        case_records=case_records,
        run_roots=run_roots,
        protocol=protocol,
        schema=schema,
    )
    report.pop("report_sha256", None)
    report["report_version"] = VERSION
    report["classification"] = (
        "OUTCOME_BLIND_CAUSAL_AND_RELATION_EVIDENCE_PROBE_NOT_FORMAL_ACCEPTANCE"
    )
    report["report_sha256"] = canonical_sha256(report)
    return report


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# V4-S1／V7 因果與關係比較證據三輪探針",
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
            "本探針只回答補齊精確因果與關係比較證據後，單一可達案例能否形成穩定判讀。它不能取代跨股票、跨月份的正式三輪驗收，也尚未開封未來績效。",
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
