"""Explain where a completed V2-core M2A triplicate probe is unstable."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_codex_staged_runner_v1 import OUT, verify_execution_manifest
from scripts.v2_core_triplicate_consistency_v1 import collect_case_round


ANALYZER_VERSION = "v2-core-feasibility-failure-analysis-r1"


def atom_family(atom_key: str) -> str:
    if atom_key == "stage_b.data_sufficiency":
        return "data_sufficiency"
    if atom_key.startswith("stage_b.shared."):
        return "shared_structure"
    if atom_key.startswith("stage_b.routing."):
        return "scenario_routing"
    if atom_key.startswith("stage_b.controlling_anchor."):
        return "controlling_anchor"
    if atom_key == "stage_d.called":
        return "stage_d_call"
    if ".gate." in atom_key:
        return "scenario_gate"
    if atom_key.startswith("stage_d.blocking."):
        return "blocking_gate"
    if atom_key.startswith("stage_d."):
        return "trigger_context"
    return "other"


def _rate(passed: int, total: int) -> float:
    return round(100.0 * passed / total, 2) if total else 0.0


def analyze(artifact_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = verify_execution_manifest(manifest_path, artifact_dir)
    atom_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    family_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    scenario_patterns: Counter[tuple[str, ...]] = Counter()
    permission_patterns: Counter[tuple[str, ...]] = Counter()
    approval_frequency: Counter[int] = Counter()
    anchor_signature_unique_counts: Counter[int] = Counter()
    trigger_signature_unique_counts: Counter[int] = Counter()

    for case in manifest["cases"]:
        review_id = str(case["review_id"])
        rounds = [collect_case_round(artifact_dir, manifest, review_id, number) for number in (1, 2, 3)]
        scenarios = tuple(str(row["primary_scenario"]) for row in rounds)
        permissions = tuple(str(row["final_permission"]) for row in rounds)
        scenario_patterns[scenarios] += 1
        permission_patterns[permissions] += 1
        approval_frequency[sum(value == "TRADE_APPROVED" for value in permissions)] += 1
        anchor_signature_unique_counts[len({row["controlling_anchor"] for row in rounds})] += 1
        trigger_signature_unique_counts[len({row["trigger_episode"] for row in rounds})] += 1

        atom_keys = sorted(set().union(*(row["core_atoms"] for row in rounds)))
        for atom_key in atom_keys:
            values = tuple(row["core_atoms"].get(atom_key, "NOT_APPLICABLE") for row in rounds)
            consistent = values[0] == values[1] == values[2]
            atom_totals[atom_key][1] += 1
            family = atom_family(atom_key)
            family_totals[family][1] += 1
            if consistent:
                atom_totals[atom_key][0] += 1
                family_totals[family][0] += 1

    atom_rows = [
        {
            "atom": key,
            "family": atom_family(key),
            "consistent_cases": counts[0],
            "evaluated_cases": counts[1],
            "consistency_rate": _rate(counts[0], counts[1]),
        }
        for key, counts in atom_totals.items()
    ]
    atom_rows.sort(key=lambda row: (row["consistency_rate"], -row["evaluated_cases"], row["atom"]))
    family_rows = [
        {
            "family": key,
            "consistent_case_atoms": counts[0],
            "evaluated_case_atoms": counts[1],
            "consistency_rate": _rate(counts[0], counts[1]),
        }
        for key, counts in family_totals.items()
    ]
    family_rows.sort(key=lambda row: (row["consistency_rate"], row["family"]))

    return {
        "analyzer_version": ANALYZER_VERSION,
        "status": "DIAGNOSTIC_COMPLETE（失敗診斷完成）",
        "case_count": len(manifest["cases"]),
        "family_consistency": family_rows,
        "least_consistent_atoms": atom_rows[:20],
        "scenario_patterns": [
            {"round_values": list(pattern), "case_count": count}
            for pattern, count in scenario_patterns.most_common()
        ],
        "permission_patterns": [
            {"round_values": list(pattern), "case_count": count}
            for pattern, count in permission_patterns.most_common()
        ],
        "approval_frequency": {
            str(times): approval_frequency[times]
            for times in range(4)
        },
        "anchor_signature_unique_counts": {
            str(count): anchor_signature_unique_counts[count]
            for count in range(1, 4)
        },
        "trigger_signature_unique_counts": {
            str(count): trigger_signature_unique_counts[count]
            for count in range(1, 4)
        },
        "buy_no_buy_consistency_rate": _rate(
            approval_frequency[0] + approval_frequency[3], len(manifest["cases"])
        ),
        "future_performance_used": False,
        "sealed_labels_used": False,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# M2A V8 可行性失敗分析",
        "",
        f"- 狀態：`{report['status']}`",
        f"- 案例：{report['case_count']} 個匿名 AS-OF 案例，各獨立執行三輪。",
        "- 未使用未來績效、股票身分、sealed 舊答案或人工修正答案。",
        f"- 僅看買／不買的一致率：{report['buy_no_buy_consistency_rate']:.2f}%（正式權限仍須區分 WAIT 與 UNKNOWN）。",
        "",
        "## 原子家族一致率",
        "",
        "| 家族 | 一致 case-atoms | 總 case-atoms | 一致率 |",
        "|---|---:|---:|---:|",
    ]
    for row in report["family_consistency"]:
        lines.append(
            f"| `{row['family']}` | {row['consistent_case_atoms']} | "
            f"{row['evaluated_case_atoms']} | {row['consistency_rate']:.2f}% |"
        )
    lines.extend(
        [
            "",
            "## 最不穩定的原子欄位",
            "",
            "| 原子 | 家族 | 一致案例 | 總案例 | 一致率 |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for row in report["least_consistent_atoms"]:
        lines.append(
            f"| `{row['atom']}` | `{row['family']}` | {row['consistent_cases']} | "
            f"{row['evaluated_cases']} | {row['consistency_rate']:.2f}% |"
        )
    lines.extend(
        [
            "",
            "## 交易核准穩定性",
            "",
            "| 三輪中核准次數 | 案例數 |",
            "|---:|---:|",
        ]
    )
    for times in range(4):
        lines.append(f"| {times} | {report['approval_frequency'][str(times)]} |")
    lines.extend(
        [
            "",
            "## 精確結構漂移",
            "",
            "| 三輪中不同簽章數 | 控制定錨案例數 | 觸發／防線案例數 |",
            "|---:|---:|---:|",
        ]
    )
    for count in range(1, 4):
        lines.append(
            f"| {count} | {report['anchor_signature_unique_counts'][str(count)]} | "
            f"{report['trigger_signature_unique_counts'][str(count)]} |"
        )
    lines.extend(
        [
            "",
            "## 結論",
            "",
            "本候選版的工程合法性已成立，但主觀感知尚未穩定；不得進入完整語意凍結、績效回測或正式監控。下一候選版應優先收斂控制定錨、大小級方向與觸發／episode 防線，再以新的校準探針重跑。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=OUT)
    parser.add_argument("--execution-manifest", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    report = analyze(args.artifact_dir.resolve(), args.execution_manifest.resolve())
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "case_count": report["case_count"],
        "buy_no_buy_consistency_rate": report["buy_no_buy_consistency_rate"],
        "output_json": str(args.output_json.resolve()),
        "output_md": str(args.output_md.resolve()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
