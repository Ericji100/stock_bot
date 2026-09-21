"""Produce a reproducible diagnostic report for the three frozen V1 runs.

This is deliberately separate from ``hybrid_v3_consistency.py``.  The latter
is part of the frozen execution surface and owns the pass/fail gate; this file
only explains a completed result and never changes or merges semantic rows.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from hybrid_v3_policy import reduce_v3


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan/hybrid_monitoring_v1"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _counter(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items(), key=lambda item: (-item[1], item[0])))


def _same(values: list[Any]) -> bool:
    return len({json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values}) == 1


FIELD_ACCESSORS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "primary_scenario": lambda row: row["primary_scenario"],
    "left_right_phase": lambda row: row["left_right_phase"],
    "stage_location": lambda row: row["stage_location"],
    "large.direction": lambda row: row["scales"]["large"]["direction"],
    "large.quadrant": lambda row: row["scales"]["large"]["quadrant"],
    "large.dow_state": lambda row: row["scales"]["large"]["dow_state"],
    "small.direction": lambda row: row["scales"]["small"]["direction"],
    "small.quadrant": lambda row: row["scales"]["small"]["quadrant"],
    "small.dow_state": lambda row: row["scales"]["small"]["dow_state"],
    "scales.relationship": lambda row: row["scales"]["relationship"],
    "taiji.phase": lambda row: row["taiji"]["phase"],
    "taiji.generation": lambda row: row["taiji"]["generation"],
    "flag.q3": lambda row: row["semantic_flags"]["q3"],
    "flag.exhausted": lambda row: row["semantic_flags"]["exhausted"],
    "flag.scale_conflict": lambda row: row["semantic_flags"]["scale_conflict"],
    "flag.single_indicator_only": lambda row: row["semantic_flags"]["single_indicator_only"],
}


def analyze(run_dir: Path) -> dict[str, Any]:
    consistency_dir = run_dir / "consistency"
    packets = {row["review_id"]: row for row in _jsonl(run_dir / "consistency_sample.jsonl")}
    runs = [
        {row["review_id"]: row for row in _jsonl(consistency_dir / f"run_{number}.jsonl")}
        for number in (1, 2, 3)
    ]
    expected_ids = set(packets)
    if len(expected_ids) != 60 or any(set(run) != expected_ids for run in runs):
        raise ValueError("diagnostics require three complete 60-row consistency runs")
    ids = sorted(expected_ids)
    policies = [
        {review_id: reduce_v3(packets[review_id], run[review_id]) for review_id in ids}
        for run in runs
    ]
    formal = json.loads((consistency_dir / "consistency_result.json").read_text(encoding="utf-8-sig"))

    run_summaries: list[dict[str, Any]] = []
    for index in range(3):
        run_summaries.append(
            {
                "run": index + 1,
                "permissions": _counter([policies[index][review_id]["permission"] for review_id in ids]),
                "routes": _counter([policies[index][review_id]["route"] for review_id in ids]),
                "scenarios": _counter([runs[index][review_id]["primary_scenario"] for review_id in ids]),
                "phases": _counter([runs[index][review_id]["left_right_phase"] for review_id in ids]),
            }
        )

    pairwise: list[dict[str, Any]] = []
    for left, right in ((0, 1), (0, 2), (1, 2)):
        pairwise.append(
            {
                "pair": f"Run {left + 1} vs Run {right + 1}",
                "permission": sum(
                    policies[left][review_id]["permission"] == policies[right][review_id]["permission"]
                    for review_id in ids
                )
                / len(ids),
                "route": sum(
                    policies[left][review_id]["route"] == policies[right][review_id]["route"]
                    for review_id in ids
                )
                / len(ids),
                "scenario": sum(
                    runs[left][review_id]["primary_scenario"] == runs[right][review_id]["primary_scenario"]
                    for review_id in ids
                )
                / len(ids),
                "phase": sum(
                    runs[left][review_id]["left_right_phase"] == runs[right][review_id]["left_right_phase"]
                    for review_id in ids
                )
                / len(ids),
                "scenario_and_phase": sum(
                    (
                        runs[left][review_id]["primary_scenario"],
                        runs[left][review_id]["left_right_phase"],
                    )
                    == (
                        runs[right][review_id]["primary_scenario"],
                        runs[right][review_id]["left_right_phase"],
                    )
                    for review_id in ids
                )
                / len(ids),
            }
        )

    field_agreement: dict[str, dict[str, Any]] = {}
    for name, accessor in FIELD_ACCESSORS.items():
        matching = sum(_same([accessor(run[review_id]) for run in runs]) for review_id in ids)
        field_agreement[name] = {"matching": matching, "total": len(ids), "rate": matching / len(ids)}

    permission_mismatches: list[dict[str, Any]] = []
    for review_id in ids:
        permission_values = [policy[review_id]["permission"] for policy in policies]
        if _same(permission_values):
            continue
        alternative_rows = [run[review_id].get("alternative_scenarios") or [] for run in runs]
        permission_mismatches.append(
            {
                "review_id": review_id,
                "as_of": packets[review_id]["as_of"],
                "event_types": packets[review_id]["event_types"],
                "permissions": permission_values,
                "routes": [policy[review_id]["route"] for policy in policies],
                "reason_codes": [policy[review_id].get("reason_codes") or [] for policy in policies],
                "scenarios": [run[review_id]["primary_scenario"] for run in runs],
                "phases": [run[review_id]["left_right_phase"] for run in runs],
                "stages": [run[review_id]["stage_location"] for run in runs],
                "alternative_counts": [len(rows) for rows in alternative_rows],
                "has_material_alternative": [
                    any(
                        bool(item.get("changes_direction"))
                        or bool(item.get("changes_invalidation"))
                        or bool(item.get("changes_position_role"))
                        for item in rows
                    )
                    for rows in alternative_rows
                ],
            }
        )

    scenario_only = phase_only = both = 0
    for review_id in ids:
        scenario_diff = not _same([run[review_id]["primary_scenario"] for run in runs])
        phase_diff = not _same([run[review_id]["left_right_phase"] for run in runs])
        if scenario_diff and phase_diff:
            both += 1
        elif scenario_diff:
            scenario_only += 1
        elif phase_diff:
            phase_only += 1

    operational: list[dict[str, Any]] = []
    for number in (1, 2, 3):
        audit = _jsonl(consistency_dir / f"run_{number}_audit.jsonl")
        successes = [row for row in audit if row.get("elapsed_seconds") is not None and not row.get("error")]
        errors = [row for row in audit if row.get("error")]
        elapsed = [float(row["elapsed_seconds"]) for row in successes]
        operational.append(
            {
                "run": number,
                "successful_batches": len(successes),
                "attempt_errors": len(errors),
                "timeouts": sum("TimeoutExpired" in str(row.get("error")) for row in errors),
                "validation_errors": sum("ValueError" in str(row.get("error")) for row in errors),
                "terminal_error_events": sum(row.get("event") == "TERMINAL_ERROR" for row in audit),
                "successful_elapsed_mean_seconds": statistics.mean(elapsed),
                "successful_elapsed_median_seconds": statistics.median(elapsed),
                "successful_elapsed_max_seconds": max(elapsed),
            }
        )

    return {
        "diagnostic_version": "hybrid-v3-consistency-diagnostics-v1",
        "scope": {"anonymous_cases": len(ids), "runs": 3, "performance_eligible": False},
        "formal_result": {
            "passed": formal["passed"],
            "thresholds": formal["thresholds"],
            "metrics": formal["metrics"],
            "conservative_permission_counts": formal["permission_counts_after_conservative_merge"],
        },
        "run_summaries": run_summaries,
        "pairwise": pairwise,
        "three_way_field_agreement": field_agreement,
        "scenario_phase_mismatch_types": {
            "scenario_only": scenario_only,
            "phase_only": phase_only,
            "both": both,
            "total": scenario_only + phase_only + both,
        },
        "permission_mismatches": permission_mismatches,
        "operational": operational,
        "findings": [
            "Schema, causal attestation, and evidence references were valid for all accepted rows.",
            "Primary scenario, stage, scale relationship, and Taiji generation were materially unstable.",
            "All four permission mismatches were WAIT versus V2_CORE TRADE.",
            "Three of four permission mismatches held scenario and phase constant; optional material alternatives changed the reducer outcome.",
            "The event-stratified sample produced no unanimous TRADE case, so it cannot validate positive-route repeatability or performance.",
        ],
    }


def _pct(value: float) -> str:
    return f"{value:.2%}"


def render_markdown(payload: dict[str, Any]) -> str:
    formal = payload["formal_result"]
    lines = [
        "# HYBRID_MONITORING_PROTOCOL_V1 三輪一致性診斷",
        "",
        "## 結論",
        "",
        "- 正式結果：`FAIL（未通過）`。",
        f"- Schema／因果／證據合法率：{_pct(formal['metrics']['schema_and_causality'])}。",
        f"- 交易／等待／移除權限一致率：{_pct(formal['metrics']['permission'])}（門檻 {_pct(formal['thresholds']['permission'])}）。",
        f"- 主要情境＋左右階段一致率：{_pct(formal['metrics']['scenario_and_phase'])}（門檻 {_pct(formal['thresholds']['scenario_and_phase'])}）。",
        "- 保守合併後 60 筆全為 `WAIT/NO_TRADE`；依凍結協定不得解封全量 V3 績效。",
        "- 本樣本是匿名且不連續的政策邊界切片，沒有完整持倉生命週期，不能計算有效報酬。",
        "",
        "## 三輪權限與路徑",
        "",
        "| 輪次 | WAIT | TRADE | NO_TRADE | V2_CORE |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in payload["run_summaries"]:
        lines.append(
            f"| Run {row['run']} | {row['permissions'].get('WAIT', 0)} | {row['permissions'].get('TRADE', 0)} | "
            f"{row['routes'].get('NO_TRADE', 0)} | {row['routes'].get('V2_CORE', 0)} |"
        )
    lines.extend(
        [
            "",
            "沒有任何案例在三輪都被判為 `TRADE`；共有 4 個案例只有單一輪判為 `TRADE`。",
            "",
            "## 兩兩一致率",
            "",
            "| 比較 | 權限 | 路徑 | 情境 | 左右階段 | 情境＋階段 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload["pairwise"]:
        lines.append(
            f"| {row['pair']} | {_pct(row['permission'])} | {_pct(row['route'])} | {_pct(row['scenario'])} | "
            f"{_pct(row['phase'])} | {_pct(row['scenario_and_phase'])} |"
        )
    lines.extend(["", "## 三輪逐欄位完全一致率", "", "| 欄位 | 一致案例 | 一致率 |", "|---|---:|---:|"])
    for name, row in payload["three_way_field_agreement"].items():
        lines.append(f"| `{name}` | {row['matching']}/{row['total']} | {_pct(row['rate'])} |")

    mismatch_types = payload["scenario_phase_mismatch_types"]
    lines.extend(
        [
            "",
            "情境或左右階段共 28/60 案例不一致：",
            "",
            f"- 只有情境不同：{mismatch_types['scenario_only']} 筆。",
            f"- 只有左右階段不同：{mismatch_types['phase_only']} 筆。",
            f"- 情境與左右階段都不同：{mismatch_types['both']} 筆。",
            "",
            "## 會改變交易權限的 4 個案例",
            "",
            "| 匿名案例 | 截至日 | 事件 | Run 1 | Run 2 | Run 3 | 重大替代解讀（R1/R2/R3） |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in payload["permission_mismatches"]:
        decisions = [f"{permission}/{route}" for permission, route in zip(row["permissions"], row["routes"])]
        material = "/".join("Y" if value else "N" for value in row["has_material_alternative"])
        lines.append(
            f"| `{row['review_id']}` | {row['as_of']} | {', '.join(row['event_types'])} | "
            f"{decisions[0]} | {decisions[1]} | {decisions[2]} | {material} |"
        )
    lines.extend(
        [
            "",
            "其中 3/4 案例的主要情境與左右階段三輪相同，必要 gates 也相同；差異來自 AI 是否選擇填入會改變結構角色的 `alternative_scenarios`。現行 reducer 將它視為 `MATERIAL_ALTERNATIVE` 並改判等待，因此可選欄位的生成漂移直接改變交易權限。",
            "",
            "## 作業可靠性",
            "",
            "| 輪次 | 成功批次 | 嘗試錯誤 | 逾時 | 驗證拒絕 | 終止事件 | 成功批次平均秒數 | 中位秒數 | 最長秒數 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload["operational"]:
        lines.append(
            f"| Run {row['run']} | {row['successful_batches']} | {row['attempt_errors']} | {row['timeouts']} | "
            f"{row['validation_errors']} | {row['terminal_error_events']} | "
            f"{row['successful_elapsed_mean_seconds']:.1f} | {row['successful_elapsed_median_seconds']:.1f} | "
            f"{row['successful_elapsed_max_seconds']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## 根因與下一版要求",
            "",
            "1. `primary_scenario`、`stage_location`、`scales.relationship`、`taiji.generation` 是自由語意分類，三輪一致率只有 35%～60%。",
            "2. 各情境只提交自己的 gate，情境一變，其他情境 gates 就消失，無法由程式在同一份固定事實上重算四條路徑。",
            "3. `alternative_scenarios` 是可選陣列，卻能直接阻擋交易；有填與沒填的生成差異造成 3/4 權限漂移。",
            "4. 樣本只按客觀事件種類分層，沒有保證包含可交易候選；三輪保守合併後零筆交易，無法驗證正向路徑穩定性。",
            "5. 三案一批使一個長尾或非法引用拖累另外兩案，Run 2 曾整體終止，Run 3 也有重試，尚不適合每日正式作業。",
            "",
            "下一版必須：固定原子語意問題、讓四情境由同一組固定欄位計算、把替代情境改為必填的明確歧義 gate、由程式計算世代／phase 中可客觀化的部分、分開抽取可交易候選與等待／移除樣本，並把單案例設為可獨立恢復單位。V3 原始規則不因這次失敗而修改。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    args = parser.parse_args()
    payload = analyze(args.run_dir)
    output_json = args.run_dir / "consistency" / "consistency_diagnostics.json"
    output_md = args.run_dir / "consistency" / "consistency_diagnostics.md"
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    output_md.write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps({"json": str(output_json), "markdown": str(output_md), "formal_passed": payload["formal_result"]["passed"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
