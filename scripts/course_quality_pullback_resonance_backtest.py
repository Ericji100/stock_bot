"""Compare the requested high-quality-pullback plus dual-scale-resonance mix.

High-quality pullback is frozen before seeing outcomes as the existing
PULLBACK_RELAUNCH setup after the TOUCH_RISK actual-fill gate.  Plain RECLAIM
is deliberately excluded.  Dual-scale candidates are reused from the causal
v1 large-structure scan so this comparison does not redefine their signals.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.course_dual_scale_entry_backtest as dual  # noqa: E402
from scripts.course_add_vs_no_add_backtest import Costs  # noqa: E402
from scripts.course_corporate_action_backtest import load_inputs  # noqa: E402
from scripts.course_corporate_action_data import AS_OF, RUN, read  # noqa: E402


METHOD_VERSION = "course-quality-pullback-resonance-v1"
SOURCE_DUAL = RUN / "dual_scale_entry_v1" / "comparison.json"
SOURCE_OLD = ROOT / "reports/course_backtest/2026-09-04/radar_full_history_v2/backtest.json"
LABELS = {
    "SMALL_BASELINE": "SMALL_BASELINE（現行小結構基準）",
    "QUALITY_PULLBACK": "QUALITY_PULLBACK（高品質拉回）",
    "DUAL_RESONANCE": "DUAL_RESONANCE（大小級共振）",
    "HYBRID_RESONANCE": "HYBRID_RESONANCE（高品質拉回＋大小級共振）",
}


def is_quality_pullback(pattern: str | None) -> bool:
    """The definition is structural and fixed, never based on realized P&L."""
    return pattern == "PULLBACK_RELAUNCH"


def _old_pattern_map() -> dict[str, str]:
    source = read(SOURCE_OLD)
    return {
        trade["trade_id"]: str(trade.get("pattern") or "NONE")
        for candidate in source["candidates"]
        for trade in candidate["trades"]
    }


def _reconstruct_proposals(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    patterns = _old_pattern_map()
    baseline = []
    for trade in source["results"]["SMALL_BASELINE"]["trades"]:
        proposal = dict(trade["entry_proposal"])
        # The dual-scale report prefixes the simulated trade id with SMALL-,
        # while entry_proposal keeps the original v2 id used by the pattern
        # catalogue.
        legacy_id = str(proposal["trade_id"])
        proposal["small_pattern"] = patterns[legacy_id]
        baseline.append(proposal)

    resonance_by_id: dict[str, dict[str, Any]] = {}
    result = source["results"]["DUAL_RESONANCE"]
    for trade in result["trades"]:
        proposal = dict(trade["entry_proposal"])
        resonance_by_id[proposal["proposal_id"]] = proposal
    for skipped in result["skipped"]:
        proposal = {key: value for key, value in skipped.items() if key != "skip_reason"}
        resonance_by_id[proposal["proposal_id"]] = proposal
    return baseline, list(resonance_by_id.values())


def _mean(values: list[float]) -> float | None:
    return None if not values else statistics.fmean(values)


def _f(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _augment(result: dict[str, Any], variant_code: str) -> None:
    trades = result["trades"]
    summary = result["summary"]
    quality = (
        [row for row in trades if row["trigger_family"] == "SMALL_BASELINE"]
        if variant_code in {"QUALITY_PULLBACK", "HYBRID_RESONANCE"}
        else []
    )
    resonance = (
        [row for row in trades if row["trigger_family"] == "DUAL_RESONANCE"]
        if variant_code in {"DUAL_RESONANCE", "HYBRID_RESONANCE"}
        else []
    )
    summary.update({
        "quality_pullback_count": len(quality),
        "dual_resonance_count": len(resonance),
        "quality_pullback_net_pnl": sum(float(row["net_pnl"]) for row in quality),
        "dual_resonance_net_pnl": sum(float(row["net_pnl"]) for row in resonance),
        "average_mfe_pct": _mean([float(row["max_campaign_gross_return_pct"]) for row in trades]),
        "average_mae_proxy_pct": _mean([
            min(float(day["net_liquidation_pnl"]) for day in row["daily"]) / 10_000.0 * 100.0
            for row in trades if row["daily"]
        ]),
    })


def build() -> dict[str, Any]:
    source = read(SOURCE_DUAL)
    baseline, resonance = _reconstruct_proposals(source)
    quality_pullback = [row for row in baseline if is_quality_pullback(row.get("small_pattern"))]
    if len(baseline) != 72 or len(quality_pullback) != 17 or len(resonance) != 47:
        raise AssertionError(
            f"unexpected frozen proposal counts: baseline={len(baseline)}, "
            f"pullback={len(quality_pullback)}, resonance={len(resonance)}"
        )

    manifest = read(RUN / "input_manifest.json")
    items = {str(item["code"]): item for item in manifest["items"]}
    required = {row["code"] for row in baseline + resonance}
    inputs = {}
    for code in sorted(required):
        raw, actions, _ = load_inputs(items[code])
        inputs[code] = (raw, actions)

    dual.LABELS.update(LABELS)
    variants = {
        "SMALL_BASELINE": baseline,
        "QUALITY_PULLBACK": quality_pullback,
        "DUAL_RESONANCE": resonance,
        "HYBRID_RESONANCE": quality_pullback + resonance,
    }
    costs = Costs()
    results = {}
    for code, proposals in variants.items():
        print(f"Simulate {code} candidates={len(proposals)}", flush=True)
        result = dual._run_variant(code=code, proposals=proposals, inputs=inputs, costs=costs)
        _augment(result, code)
        results[code] = result

    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": AS_OF,
        "definition": {
            "quality_pullback": (
                "Existing TOUCH_RISK entry whose pre-outcome setup code is "
                "PULLBACK_RELAUNCH; RECLAIM is excluded"
            ),
            "dual_resonance": "unchanged 47 causal proposals from dual_scale_entry_v1",
            "hybrid_collision": (
                "same stock/date preserves quality pullback; an existing same-stock "
                "open campaign blocks the later signal"
            ),
            "position_exit_costs": source["parameters"],
        },
        "frozen_candidate_counts": {
            "small_baseline": len(baseline),
            "quality_pullback": len(quality_pullback),
            "dual_resonance_before_open-position_dedup": len(resonance),
            "requested_union_before_dedup": len(quality_pullback) + len(resonance),
        },
        "results": results,
    }


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    results = payload["results"]
    add("baseline_count", results["SMALL_BASELINE"]["summary"]["trade_count"] == 72,
        results["SMALL_BASELINE"]["summary"]["trade_count"])
    add("quality_pullback_frozen_count", payload["frozen_candidate_counts"]["quality_pullback"] == 17,
        payload["frozen_candidate_counts"]["quality_pullback"])
    add("quality_pullback_patterns", all(
        row["entry_proposal"]["small_pattern"] == "PULLBACK_RELAUNCH"
        for row in results["QUALITY_PULLBACK"]["trades"]
    ), [row["entry_proposal"].get("small_pattern") for row in results["QUALITY_PULLBACK"]["trades"]])
    add("hybrid_has_only_requested_families", set(
        results["HYBRID_RESONANCE"]["summary"]["family_counts"]
    ) <= {"SMALL_BASELINE", "DUAL_RESONANCE"},
        results["HYBRID_RESONANCE"]["summary"]["family_counts"])
    for code, result in results.items():
        trades = result["trades"]
        ids = [row["trade_id"] for row in trades]
        add(f"{code}.unique_ids", len(ids) == len(set(ids)), len(ids))
        add(f"{code}.funded", result["summary"]["funding_shortfall"] == 0,
            result["summary"]["funding_shortfall"])
        reconciled = 500_000.0 + sum(float(row["net_pnl"]) for row in trades)
        add(f"{code}.equity_reconciles",
            abs(result["summary"]["final_net_liquidation_equity"] - reconciled) < 1e-6,
            result["summary"]["final_net_liquidation_equity"] - reconciled)
        by_code: dict[str, list[dict[str, Any]]] = {}
        for trade in trades:
            by_code.setdefault(trade["code"], []).append(trade)
        overlaps = []
        for stock, rows in by_code.items():
            ordered = sorted(rows, key=lambda row: row["entry_date"])
            for left, right in zip(ordered, ordered[1:]):
                if not left["exit_date"] or left["exit_date"] >= right["entry_date"]:
                    overlaps.append((stock, left["trade_id"], right["trade_id"]))
        add(f"{code}.no_overlap", not overlaps, overlaps[:5])
    failures = [row for row in checks if not row["passed"]]
    return {
        "check_count": len(checks),
        "passed_count": len(checks) - len(failures),
        "failed_count": len(failures),
        "checks": checks,
    }


def render(payload: dict[str, Any]) -> str:
    lines = [
        f"# 高品質拉回＋大小級共振回測｜截至 {payload['as_of']}", "",
        "> 高品質拉回事前固定為既有 PULLBACK_RELAUNCH，不用績效挑股票；大小級共振沿用前次因果訊號。每筆1萬元、不加碼、相同除權息、成本與出場規則。", "",
        "## 結果", "",
        "| 版本 | 交易 | 已出場/持有中 | 總淨損益 | 50萬報酬 | 最大回撤 | 勝率 | 中位報酬 | PF | 已出場損益/PF | 最大同時持倉 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for code in LABELS:
        summary = payload["results"][code]["summary"]
        lines.append(
            f"| {LABELS[code]} | {summary['trade_count']} | {summary['closed_count']}/{summary['open_or_triggered_count']} | "
            f"{summary['net_pnl']:,.0f} | {summary['return_on_500k_pct']:+.2f}% | {summary['max_drawdown_pct']:.2f}% | "
            f"{_f(summary['positive_trade_rate_pct'])}% | {_f(summary['median_trade_return_on_campaign_budget_pct'])}% | "
            f"{_f(summary['profit_factor'])} | {summary['closed_net_pnl']:,.0f} / {_f(summary['closed_profit_factor'])} | "
            f"{summary['max_concurrent_positions']} |"
        )
    lines += [
        "", "## 要求版本的組成", "",
    ]
    requested = payload["results"]["HYBRID_RESONANCE"]["summary"]
    lines += [
        f"- 實際交易：{requested['trade_count']}筆。",
        f"- `QUALITY_PULLBACK（高品質拉回）`：{requested['quality_pullback_count']}筆，損益 {requested['quality_pullback_net_pnl']:,.0f}元。",
        f"- `DUAL_RESONANCE（大小級共振）`：{requested['dual_resonance_count']}筆，損益 {requested['dual_resonance_net_pnl']:,.0f}元。",
        f"- 已出場損益：{requested['closed_net_pnl']:,.0f}元；持有中估值損益：{requested['open_net_pnl']:,.0f}元。",
        f"- 持有中正／負：{requested['open_positive_count']}／{requested['open_negative_count']}。",
        f"- 資金最高占用：{requested['peak_open_book_cost']:,.0f}元；50萬元資金缺口 {requested['funding_shortfall']:,.0f}元。", "",
        "## 規則", "",
        "- 高品質拉回：原現行訊號的 `PULLBACK_RELAUNCH（多頭回檔重新發動）`，仍通過實際成交10%／2.5ATR風險；排除單純 `RECLAIM（均線收復）`。",
        "- 大小級共振：大結構邊界為當時已確認二級樞紐高或前55日平台上緣，大、小級道氏方向都必須為多；初始停損仍使用小級確認樞紐。",
        "- 同股同日兩者同時成立時保留既有拉回成交；同股前一筆仍持有時，不重複開倉。",
        "- 績效包含截至日持有部位按收盤估值及預估賣出成本；沒有使用未來MFE決定是否進場。", "",
        "## 限制", "",
        "- 樣本期短，大小級共振多數發生較晚，尚有未出場部位；不能把截至日正報酬直接視為長期穩定獲利。",
        "- 日K不知道盤中高低先後，也未加入真實零股價差、最低手續費及漲跌停排隊。",
        "- 本回測沒有修改正式機器人或自動下單規則。", "",
        f"方法版本：`{payload['method_version']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=RUN / "quality_pullback_resonance_v1")
    args = parser.parse_args()
    payload = build()
    validation = validate(payload)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / "comparison.md").write_text(render(payload), encoding="utf-8")
    (args.output_dir / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if validation["failed_count"]:
        raise AssertionError(f"validation failed: {validation['failed_count']}")
    print((args.output_dir / "comparison.md").resolve())
    for code in LABELS:
        summary = dict(payload["results"][code]["summary"])
        summary.pop("equity_curve", None)
        print(code, json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
