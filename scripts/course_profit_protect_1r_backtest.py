"""Backtest a causal +1R close-based giveback protector on the frozen hybrid entries."""
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

from scripts.course_add_vs_no_add_backtest import (  # noqa: E402
    Costs,
    MODELS,
    simulate_trade,
    summarize,
)
from scripts.course_corporate_action_backtest import load_inputs  # noqa: E402
from scripts.course_corporate_action_data import AS_OF, RUN, read  # noqa: E402


METHOD_VERSION = "course-profit-protect-1r-v1"
SOURCE = RUN / "quality_pullback_resonance_v1" / "comparison.json"
LABELS = {
    "CURRENT_EXIT": "CURRENT_EXIT（現行出場）",
    "PROFIT_PROTECT_1R": "PROFIT_PROTECT_1R（1R浮盈保護）",
}


def _mean(values: list[float]) -> float | None:
    return None if not values else statistics.fmean(values)


def _profit_factor(values: list[float]) -> float | None:
    profit = sum(max(0.0, value) for value in values)
    loss = -sum(min(0.0, value) for value in values)
    return None if loss == 0 else profit / loss


def _trade_input(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": row["trade_id"],
        "code": str(row["code"]),
        "name": str(row["name"]),
        "entry_date": row["entry_date"],
        "entry_price": float(row["entry_price"]),
        "initial_defense": float(row["initial_defense"]),
        "mfe_pct": float(row.get("reference_fixed_defense_mfe_pct", 0.0)),
    }


def _augment(result: dict[str, Any]) -> None:
    trades = result["trades"]
    summary = result["summary"]
    closed = [row for row in trades if row["status"] == "CLOSED"]
    open_rows = [row for row in trades if row["status"] != "CLOSED"]
    closed_pnls = [float(row["net_pnl"]) for row in closed]
    returns = [float(row["net_return_on_campaign_budget_pct"]) for row in trades]
    winners = [value for value in returns if value > 0]
    losers = [value for value in returns if value < 0]
    summary.update({
        "closed_net_pnl": sum(closed_pnls),
        "open_net_pnl": sum(float(row["net_pnl"]) for row in open_rows),
        "closed_positive_rate_pct": (
            None if not closed else sum(value > 0 for value in closed_pnls) / len(closed) * 100.0
        ),
        "closed_profit_factor": _profit_factor(closed_pnls),
        "average_winner_pct": _mean(winners),
        "average_loser_pct": _mean(losers),
        "payoff_ratio": (
            None if not winners or not losers else _mean(winners) / abs(_mean(losers))
        ),
        "profit_protect_activation_count": sum(
            row.get("profit_protect_activation_date") is not None for row in trades
        ),
        "profit_protect_exit_count": sum(
            row["exit_reason"] == "PROFIT_PROTECT_1R_GIVEBACK" for row in trades
        ),
        "family_counts": dict(Counter(row["trigger_family"] for row in trades)),
    })


def build() -> dict[str, Any]:
    source = read(SOURCE)
    frozen = source["results"]["HYBRID_RESONANCE"]["trades"]
    if len(frozen) != 40:
        raise AssertionError(f"expected 40 frozen entries, got {len(frozen)}")

    manifest = read(RUN / "input_manifest.json")
    items = {str(item["code"]): item for item in manifest["items"]}
    loaded = {}
    for code in sorted({str(row["code"]) for row in frozen}):
        raw, actions, _ = load_inputs(items[code])
        loaded[code] = (raw, actions)

    costs = Costs()
    results: dict[str, dict[str, Any]] = {}
    for variant, enabled in (("CURRENT_EXIT", False), ("PROFIT_PROTECT_1R", True)):
        trades = []
        for row in frozen:
            raw, actions = loaded[str(row["code"])]
            result = simulate_trade(
                trade=_trade_input(row),
                raw=raw,
                actions=actions,
                model=MODELS["ONE_SHOT"],
                costs=costs,
                as_of=source["as_of"],
                profit_protect_1r=enabled,
                profit_giveback_fraction=0.50,
            )
            result["trigger_family"] = row["trigger_family"]
            result["entry_proposal"] = row["entry_proposal"]
            trades.append(result)
        variant_result = {"summary": summarize(trades), "trades": trades}
        _augment(variant_result)
        results[variant] = variant_result

    current_by_id = {row["trade_id"]: row for row in results["CURRENT_EXIT"]["trades"]}
    comparison = []
    for protected in results["PROFIT_PROTECT_1R"]["trades"]:
        current = current_by_id[protected["trade_id"]]
        comparison.append({
            "trade_id": protected["trade_id"],
            "code": protected["code"],
            "name": protected["name"],
            "trigger_family": protected["trigger_family"],
            "entry_date": protected["entry_date"],
            "entry_price": protected["entry_price"],
            "initial_defense": protected["initial_defense"],
            "protect_activation_date": protected["profit_protect_activation_date"],
            "current_exit_date": current["exit_date"],
            "current_exit_reason": current["exit_reason_label"],
            "current_net_pnl": current["net_pnl"],
            "current_return_pct": current["net_return_on_campaign_budget_pct"],
            "protected_exit_date": protected["exit_date"],
            "protected_exit_reason": protected["exit_reason_label"],
            "protected_net_pnl": protected["net_pnl"],
            "protected_return_pct": protected["net_return_on_campaign_budget_pct"],
            "net_pnl_change": protected["net_pnl"] - current["net_pnl"],
            "exit_changed": (
                protected["exit_date"] != current["exit_date"]
                or protected["exit_reason"] != current["exit_reason"]
            ),
        })

    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": AS_OF,
        "source": str(SOURCE.resolve()),
        "cohort": {
            "definition": "Frozen 40 executed HYBRID_RESONANCE entries",
            "trade_count": len(frozen),
            "entry_changes_allowed": False,
        },
        "rule": {
            "activation": "while below +2R runner state, first close at or above +1R",
            "peak": "highest causal closing return in R while below +2R runner state",
            "exit_trigger": "close return in R <= 50% of peak close return in R",
            "execution": "next symbol trading session open",
            "runner": "once +2R is reached, retain the existing dynamic-pivot/cost/MA21 exits",
            "giveback_fraction": 0.50,
            "costs": {
                "commission_rate": costs.commission_rate,
                "sell_tax_rate": costs.sell_tax_rate,
                "minimum_commission": costs.minimum_commission,
                "slippage": costs.slippage,
            },
        },
        "results": results,
        "trade_comparison": comparison,
    }


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    current = payload["results"]["CURRENT_EXIT"]
    protected = payload["results"]["PROFIT_PROTECT_1R"]
    add("trade_counts", current["summary"]["trade_count"] == protected["summary"]["trade_count"] == 40,
        [current["summary"]["trade_count"], protected["summary"]["trade_count"]])
    entry_mismatches = []
    for left, right in zip(current["trades"], protected["trades"]):
        if (left["trade_id"], left["entry_date"], left["entry_price"], left["initial_defense"]) != (
            right["trade_id"], right["entry_date"], right["entry_price"], right["initial_defense"]
        ):
            entry_mismatches.append(left["trade_id"])
    add("entries_frozen", not entry_mismatches, entry_mismatches[:5])
    source = read(SOURCE)["results"]["HYBRID_RESONANCE"]
    source_by_id = {row["trade_id"]: row for row in source["trades"]}
    default_diffs = [
        (row["trade_id"], row["net_pnl"] - source_by_id[row["trade_id"]]["net_pnl"])
        for row in current["trades"]
        if abs(row["net_pnl"] - source_by_id[row["trade_id"]]["net_pnl"]) > 1e-6
    ]
    add("current_replays_source", not default_diffs, default_diffs[:5])
    for variant, result in payload["results"].items():
        total = sum(float(row["net_pnl"]) for row in result["trades"])
        add(f"{variant}.equity_reconciles",
            abs(result["summary"]["final_net_liquidation_equity"] - (500_000.0 + total)) < 1e-6,
            result["summary"]["final_net_liquidation_equity"] - (500_000.0 + total))
        add(f"{variant}.funded", result["summary"]["funding_shortfall"] == 0,
            result["summary"]["funding_shortfall"])
    bad_protect_exits = [
        row["trade_id"] for row in protected["trades"]
        if row["exit_reason"] == "PROFIT_PROTECT_1R_GIVEBACK"
        and row["profit_protect_activation_date"] is None
    ]
    add("protect_exit_requires_activation", not bad_protect_exits, bad_protect_exits)
    bad_runner_exits = [
        row["trade_id"] for row in protected["trades"]
        if row["exit_reason"] == "PROFIT_PROTECT_1R_GIVEBACK"
        and any("TREND_RUNNER" in event["state"] for event in row["state_events"])
    ]
    add("runner_keeps_original_exit_family", not bad_runner_exits, bad_runner_exits)
    failures = [row for row in checks if not row["passed"]]
    return {
        "check_count": len(checks),
        "passed_count": len(checks) - len(failures),
        "failed_count": len(failures),
        "checks": checks,
    }


def _f(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def render(payload: dict[str, Any]) -> str:
    lines = [
        f"# 1R浮盈保護回測｜截至 {payload['as_of']}", "",
        "> 固定高品質拉回＋大小級共振的40筆實際進場，只更換出場；每筆1萬元、不加碼、含除權息及成本。", "",
        "## 結果", "",
        "| 版本 | 交易 | 已出場/持有中 | 總淨損益 | 50萬報酬 | 最大回撤 | 勝率 | 中位報酬 | PF | 已出場損益/PF |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for code in LABELS:
        summary = payload["results"][code]["summary"]
        lines.append(
            f"| {LABELS[code]} | {summary['trade_count']} | {summary['closed_count']}/{summary['open_or_triggered_count']} | "
            f"{summary['net_pnl']:,.0f} | {summary['return_on_500k_pct']:+.2f}% | {summary['max_drawdown_pct']:.2f}% | "
            f"{_f(summary['positive_trade_rate_pct'])}% | {_f(summary['median_trade_return_on_campaign_budget_pct'])}% | "
            f"{_f(summary['profit_factor'])} | {summary['closed_net_pnl']:,.0f} / {_f(summary['closed_profit_factor'])} |"
        )
    protected = payload["results"]["PROFIT_PROTECT_1R"]["summary"]
    changed = [row for row in payload["trade_comparison"] if row["exit_changed"]]
    improved = sorted(changed, key=lambda row: row["net_pnl_change"], reverse=True)
    lines += [
        "", "## 規則影響", "",
        f"- 啟動1R保護：{protected['profit_protect_activation_count']}筆。",
        f"- 因50%回吐實際出場：{protected['profit_protect_exit_count']}筆。",
        f"- 出場結果改變：{len(changed)}筆。", "",
        "## 出場變更幅度最大的交易", "",
        "| 股票 | 現行損益 | 保護版損益 | 差額 | 現行出場 | 保護版出場 |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in improved[:10]:
        lines.append(
            f"| {row['code']} {row['name']} | {row['current_net_pnl']:,.0f} | "
            f"{row['protected_net_pnl']:,.0f} | {row['net_pnl_change']:+,.0f} | "
            f"{row['current_exit_date'] or '持有中'} | {row['protected_exit_date'] or '持有中'} |"
        )
    lines += [
        "", "## 口徑", "",
        "- +1R只用收盤確認，不用盤中高點啟動。",
        "- 回吐基準是截至當日可知的最高收盤R，不使用未來資料。",
        "- 觸發後下一交易日開盤成交；若先達+2R，改回現行波段延伸規則。",
        "- 此版固定原40筆進場，不因提早出場加入原先被持倉去重擋掉的新交易。",
        "- 尚未修改正式機器人或自動下單規則。", "",
        f"方法版本：`{payload['method_version']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=RUN / "profit_protect_1r_v1")
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
