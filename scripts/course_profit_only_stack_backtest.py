"""Backtest a maximum-two-position, profit-only trigger stack.

The input is the frozen TG strategy/entry matrix.  Atomic entry families may
confirm the same stock, but same-day confirmations create only one position.
A later atomic trigger may add one more position only when the existing
position was profitable using information available at the new signal close.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_add_vs_no_add_backtest import summarize  # noqa: E402
from scripts.course_corporate_action_data import read, save  # noqa: E402
from scripts.course_tg_strategy_entry_matrix import FAMILY_LABELS  # noqa: E402


METHOD_VERSION = "course-profit-only-trigger-stack-v2"
INPUT_PATH = ROOT / "reports/course_backtest/2026-09-05/tg_strategy_entry_matrix_v1/comparison.json"
OUTPUT_DIR = ROOT / "reports/course_backtest/2026-09-05/profit_only_trigger_stack_v2"
ATOMIC_FAMILIES = [
    "OLD_MA_RECLAIM",
    "OLD_PULLBACK_RELAUNCH",
    "OLD_LARGE_BREAKOUT",
    "OLD_DUAL_RESONANCE",
    "V2_EARLY_TAIJI_PULLBACK",
    "V2_LONG_MA_PULLBACK",
    "V2_Q1_BREAKOUT",
]
# More structurally specific confirmations win when several would enter on the
# same day.  This is fixed before measuring the stacked result.
FAMILY_PRIORITY = [
    "V2_Q1_BREAKOUT",
    "V2_LONG_MA_PULLBACK",
    "V2_EARLY_TAIJI_PULLBACK",
    "OLD_DUAL_RESONANCE",
    "OLD_PULLBACK_RELAUNCH",
    "OLD_LARGE_BREAKOUT",
    "OLD_MA_RECLAIM",
]


def _known_pnl(trade: dict[str, Any], signal_day: str) -> float | None:
    rows = [row for row in trade.get("daily", []) if row["date"] <= signal_day]
    return None if not rows else float(rows[-1]["net_liquidation_pnl"])


def _active_on_entry_day(trade: dict[str, Any], entry_day: str) -> bool:
    return bool(
        trade["entry_date"] <= entry_day
        and (not trade.get("exit_date") or trade["exit_date"] > entry_day)
    )


def _buy_outflow(trade: dict[str, Any]) -> float:
    return sum(
        float(row["outflow"])
        for row in trade.get("transactions", [])
        if row["type"] == "BUY"
    )


def _sell_net(trade: dict[str, Any]) -> float:
    return sum(
        float(row["net"])
        for row in trade.get("transactions", [])
        if row["type"] == "SELL"
    )


def select_profit_only_stack(
    candidates: list[dict[str, Any]], *, capital_limit: float | None,
    max_positions_per_stock: int = 2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float | None]:
    """Select one mother plus profitable follow-on positions up to a cap."""
    if max_positions_per_stock < 1:
        raise ValueError("max_positions_per_stock must be at least one")
    priority = {family: index for index, family in enumerate(FAMILY_PRIORITY)}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for trade in candidates:
        grouped[(str(trade["entry_date"]), str(trade["code"]))].append(trade)

    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    cash = capital_limit
    processed_exits: set[str] = set()
    days = sorted({entry_day for entry_day, _ in grouped})
    for day in days:
        if cash is not None:
            for trade in accepted:
                if (
                    trade["trade_id"] not in processed_exits
                    and trade.get("exit_date")
                    and trade["exit_date"] <= day
                ):
                    cash += _sell_net(trade)
                    processed_exits.add(trade["trade_id"])

        day_groups = []
        for (entry_day, code), rows in grouped.items():
            if entry_day != day:
                continue
            options = sorted(rows, key=lambda row: (
                priority[row["entry_variant"]], row["trade_id"],
            ))
            chosen = options[0]
            day_groups.append((
                -len(options), priority[chosen["entry_variant"]],
                float(chosen["initial_risk_pct"]), code, options,
            ))

        for _, _, _, code, options in sorted(day_groups):
            candidate = options[0]
            open_positions = [
                row for row in accepted
                if row["code"] == code and _active_on_entry_day(row, day)
            ]
            known_pnl = None
            existing_trade_ids: list[str] = []
            if not open_positions:
                role = "MOTHER（母部位）"
                reason = None
            elif len(open_positions) < max_positions_per_stock:
                existing_trade_ids = [row["trade_id"] for row in open_positions]
                known_values = [
                    _known_pnl(row, candidate["entry_proposal"]["signal_date"])
                    for row in open_positions
                ]
                known_pnl = (
                    None if any(value is None for value in known_values)
                    else sum(float(value) for value in known_values if value is not None)
                )
                if known_pnl is not None and known_pnl > 0:
                    add_number = len(open_positions)
                    role = f"ADD_{add_number}（第{add_number}次獲利後加碼）"
                    reason = None
                else:
                    role = None
                    reason = "EXISTING_CAMPAIGN_NOT_PROFITABLE（現有整體部位尚未獲利）"
            else:
                role = None
                reason = f"MAX_POSITIONS（單股已有{max_positions_per_stock}個部位）"

            outflow = _buy_outflow(candidate)
            if role and cash is not None and outflow > cash:
                role = None
                reason = "CAPITAL_LIMIT（50萬資金不足）"

            confirmations = [row["entry_variant"] for row in options]
            if role:
                accepted_trade = {
                    **candidate,
                    "stack_role": role,
                    "same_day_confirmations": confirmations,
                    "existing_trade_ids": existing_trade_ids,
                    "existing_campaign_known_pnl_at_add_signal": known_pnl,
                }
                accepted.append(accepted_trade)
                if cash is not None:
                    cash -= outflow
            else:
                skipped.append({
                    "entry_date": day,
                    "code": code,
                    "name": candidate["name"],
                    "selected_family": candidate["entry_variant"],
                    "same_day_confirmations": confirmations,
                    "reason": reason,
                    "available_cash": cash,
                    "required_outflow": outflow,
                    "existing_campaign_known_pnl_at_add_signal": known_pnl,
                    "candidate_net_pnl_if_taken": float(candidate["net_pnl"]),
                })
    return accepted, skipped, cash


def _result(trades: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> dict[str, Any]:
    summary = summarize(trades)
    closed = [row for row in trades if row["status"] == "CLOSED"]
    open_rows = [row for row in trades if row["status"] != "CLOSED"]
    summary.update({
        "realized_pnl": sum(float(row["net_pnl"]) for row in closed),
        "unrealized_pnl": sum(float(row["net_pnl"]) for row in open_rows),
        "mother_count": sum(row.get("stack_role", "").startswith("MOTHER") for row in trades),
        "add_count": sum(row.get("stack_role", "").startswith("ADD") for row in trades),
        "closed_positive_count": sum(float(row["net_pnl"]) > 0 for row in closed),
        "closed_negative_count": sum(float(row["net_pnl"]) < 0 for row in closed),
        "open_positive_count": sum(float(row["net_pnl"]) > 0 for row in open_rows),
        "open_negative_count": sum(float(row["net_pnl"]) < 0 for row in open_rows),
        "skip_reason_counts": dict(Counter(row["reason"] for row in skipped)),
    })
    def make_role_stats(role_rows: list[dict[str, Any]]) -> dict[str, Any]:
        role_closed = [row for row in role_rows if row["status"] == "CLOSED"]
        role_open = [row for row in role_rows if row["status"] != "CLOSED"]
        return {
            "trade_count": len(role_rows),
            "closed_count": len(role_closed),
            "open_count": len(role_open),
            "positive_count": sum(float(row["net_pnl"]) > 0 for row in role_rows),
            "negative_count": sum(float(row["net_pnl"]) < 0 for row in role_rows),
            "realized_pnl": sum(float(row["net_pnl"]) for row in role_closed),
            "unrealized_pnl": sum(float(row["net_pnl"]) for row in role_open),
            "net_pnl": sum(float(row["net_pnl"]) for row in role_rows),
            "average_return_pct": (
                None if not role_rows else
                sum(float(row["net_return_on_campaign_budget_pct"]) for row in role_rows) / len(role_rows)
            ),
            "average_mfe_pct": (
                None if not role_rows else
                sum(float(row["max_campaign_gross_return_pct"]) for row in role_rows) / len(role_rows)
            ),
        }
    summary["role_stats"] = {
        "MOTHER（母部位）": make_role_stats([
            row for row in trades if row.get("stack_role", "").startswith("MOTHER")
        ]),
        "ADD_ALL（全部加碼）": make_role_stats([
            row for row in trades if row.get("stack_role", "").startswith("ADD_")
        ]),
    }
    for role in sorted({
        row["stack_role"] for row in trades if row.get("stack_role", "").startswith("ADD_")
    }):
        summary["role_stats"][role] = make_role_stats([
            row for row in trades if row.get("stack_role") == role
        ])
    return {"summary": summary, "trades": trades, "skipped": skipped}


def build(input_path: Path = INPUT_PATH) -> dict[str, Any]:
    source = read(input_path)
    atomic = [
        trade
        for family in ATOMIC_FAMILIES
        for trade in source["results"][family]["trades"]
    ]
    max2_trades, max2_skips, _ = select_profit_only_stack(
        atomic, capital_limit=None, max_positions_per_stock=2,
    )
    max3_trades, max3_skips, _ = select_profit_only_stack(
        atomic, capital_limit=None, max_positions_per_stock=3,
    )
    baseline = source["results"]["OLD_ALL"]
    blind_summary = summarize(atomic)
    return {
        "method_version": METHOD_VERSION,
        "as_of": source["as_of"],
        "source": str(input_path.resolve()),
        "rules": {
            "atomic_families": ATOMIC_FAMILIES,
            "same_day": "同股同日多規則只買一份；結構較具體者優先",
            "add": "後續新觸發時，原部位在訊號日收盤估值淨損益必須大於0",
            "per_stock_cap": 2,
            "per_position_budget": 10_000.0,
            "funded_priority": "同日共振數多優先、結構具體者優先、初始風險較小優先、股票代號",
        },
        "baseline_old_all": baseline,
        "blind_atomic_stack": {"summary": blind_summary},
        "profit_only_max2": _result(max2_trades, max2_skips),
        "profit_only_max3": _result(max3_trades, max3_skips),
    }


def _f(value: float | None) -> str:
    if value is None:
        return "—"
    if math.isinf(value):
        return "∞"
    return f"{value:,.2f}"


def render(payload: dict[str, Any]) -> str:
    baseline = payload["baseline_old_all"]["summary"]
    blind = payload["blind_atomic_stack"]["summary"]
    max2 = payload["profit_only_max2"]["summary"]
    max3 = payload["profit_only_max3"]["summary"]
    lines = [
        "# 單股最多加碼一／兩次比較",
        "",
        f"> 績效截至 {payload['as_of']}；每個部位上限1萬元、含成本與除權息，沿用各觸發原本的狀態切換出場。",
        "",
        "## 規則",
        "",
        "- 七種原子進場訊號納入候選；聯集版與候選路由不再重複加入。",
        "- 同一股票同一天有多個規則觸發，只建立一個母部位。",
        "- 後續出現新觸發時，現有整體部位在訊號日收盤估值淨損益必須大於0，才可再加一份。",
        "- 比較單股最多兩份（加碼一次）與最多三份（加碼兩次）；每份各自沿用其進場價、防線與出場狀態。",
        "- 本輪不設資金上限；資金需求只作觀察，不因現金不足刪除交易。",
        "",
        "## 結果",
        "",
        "| 版本 | 交易 | 母單/加碼 | 已出場/持有 | 已實現 | 未實現 | 總損益 | 最大回撤 | PF | 最大同持 | 最高成本/資金缺口 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| 舊版四類聯集（同股一份） | {baseline['trade_count']} | — | {baseline['closed_count']}/{baseline['open_or_triggered_count']} | — | — | {baseline['net_pnl']:,.0f} | {baseline['max_drawdown_pct']:.2f}% | {_f(baseline['profit_factor'])} | {baseline['max_concurrent_positions']} | {baseline['peak_open_book_cost']:,.0f}/0 |",
        f"| 七原子訊號盲目各買一份 | {blind['trade_count']} | — | {blind['closed_count']}/{blind['open_or_triggered_count']} | — | — | {blind['net_pnl']:,.0f} | {blind['max_drawdown_pct']:.2f}% | {_f(blind['profit_factor'])} | {blind['max_concurrent_positions']} | {blind['peak_open_book_cost']:,.0f}/{blind['funding_shortfall']:,.0f} |",
        f"| 最多加碼一次（單股最多兩份） | {max2['trade_count']} | {max2['mother_count']}/{max2['add_count']} | {max2['closed_count']}/{max2['open_or_triggered_count']} | {max2['realized_pnl']:,.0f} | {max2['unrealized_pnl']:,.0f} | {max2['net_pnl']:,.0f} | {max2['max_drawdown_pct']:.2f}% | {_f(max2['profit_factor'])} | {max2['max_concurrent_positions']} | {max2['peak_open_book_cost']:,.0f}/{max2['funding_shortfall']:,.0f} |",
        f"| 最多加碼兩次（單股最多三份） | {max3['trade_count']} | {max3['mother_count']}/{max3['add_count']} | {max3['closed_count']}/{max3['open_or_triggered_count']} | {max3['realized_pnl']:,.0f} | {max3['unrealized_pnl']:,.0f} | {max3['net_pnl']:,.0f} | {max3['max_drawdown_pct']:.2f}% | {_f(max3['profit_factor'])} | {max3['max_concurrent_positions']} | {max3['peak_open_book_cost']:,.0f}/{max3['funding_shortfall']:,.0f} |",
        "",
        f"最多加碼兩次相較加碼一次，總損益變化 **{max3['net_pnl'] - max2['net_pnl']:+,.0f}元**；最高持倉成本增加 {max3['peak_open_book_cost'] - max2['peak_open_book_cost']:,.0f}元。",
        "",
        "## 最多加碼兩次：母部位與加碼拆解",
        "",
        "| 角色 | 筆數 | 已出場/持有 | 正/負 | 已實現 | 未實現 | 合計損益 | 平均報酬 | 平均MFE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for role in (
        "MOTHER（母部位）", "ADD_1（第1次獲利後加碼）",
        "ADD_2（第2次獲利後加碼）", "ADD_ALL（全部加碼）",
    ):
        stats = max3["role_stats"].get(role)
        if stats is None:
            continue
        lines.append(
            f"| {role} | {stats['trade_count']} | {stats['closed_count']}/{stats['open_count']} | "
            f"{stats['positive_count']}/{stats['negative_count']} | {stats['realized_pnl']:,.0f} | "
            f"{stats['unrealized_pnl']:,.0f} | {stats['net_pnl']:,.0f} | "
            f"{_f(stats['average_return_pct'])}% | {_f(stats['average_mfe_pct'])}% |"
        )
    lines.extend([
        "",
        "## 最多加碼兩次版本逐筆",
        "",
        "| 股票 | 角色 | 同日確認 | 訊號 | 進場日/價 | 防線 | 出場或估值 | MFE | 淨損益 | 報酬 | 狀態 |",
        "|---|---|---|---|---:|---:|---|---:|---:|---:|---|",
    ])
    for trade in payload["profit_only_max3"]["trades"]:
        proposal = trade["entry_proposal"]
        confirmations = "、".join(trade["same_day_confirmations"])
        if trade["status"] == "CLOSED":
            performance = f"{trade['exit_date']}／{float(trade['exit_price']):,.2f}"
        else:
            performance = f"{trade['performance_date']}／持有中估值"
        lines.append(
            f"| {trade['code']} {trade['name']} | {trade['stack_role']} | {confirmations} | {proposal['signal_date']} | "
            f"{trade['entry_date']}／{float(trade['entry_price']):,.2f} | {float(trade['initial_defense']):,.2f} | {performance} | "
            f"{float(trade['max_campaign_gross_return_pct']):+.2f}% | {float(trade['net_pnl']):+,.2f} | "
            f"{float(trade['net_return_on_campaign_budget_pct']):+.2f}% | {trade['status_label']} |"
        )
    lines.extend([
        "",
        "## 被擋下的後續觸發",
        "",
        "| 原因 | 次數 |",
        "|---|---:|",
    ])
    for reason, count in sorted(max3["skip_reason_counts"].items()):
        lines.append(f"| {reason} | {count} |")
    lines.extend([
        "",
        "## 限制",
        "",
        "- 原部位『已獲利』只看新訊號日收盤前已知估值，不使用後續MFE。",
        "- 不同觸發各自出場，尚未測試兩份合併成單一共同防線的做法。",
        "- 本輪允許資金需求超過50萬元，50萬報酬欄只是共同比較分母，不代表50萬元足以成交全部部位。",
        "- 樣本截至日仍有大量持倉，未實現損益可能繼續變動。",
        "- 本結果不代表實盤獲利保證。",
        "",
        f"方法版本：`{payload['method_version']}`",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=INPUT_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    payload = build(args.input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    save(args.output_dir / "backtest.json", payload)
    (args.output_dir / "backtest.md").write_text(render(payload), encoding="utf-8")
    print(render(payload))


if __name__ == "__main__":
    main()
