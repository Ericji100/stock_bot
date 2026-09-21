"""Compare one-shot and causal mother-position/pyramiding portfolio rules."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_position_variant_backtest import (  # noqa: E402
    _f,
    _profit_factor,
    build_universe,
)


METHOD_VERSION = "course-dynamic-position-backtest-v1"
MODEL_LABELS = {
    "ONE_SHOT_12": "ONE_SHOT_12（一次進場、最多12檔）",
    "PYRAMID_12": "PYRAMID_12（母單0.2%＋確認加碼0.4%）",
    "PYRAMID_12_TIME5": "PYRAMID_12_TIME5（5日無延續釋放）",
    "PYRAMID_12_TIME10": "PYRAMID_12_TIME10（10日未確認釋放）",
    "PYRAMID_16": "PYRAMID_16（最多16檔、不強制時間出場）",
    "PYRAMID_16_TIME5": "PYRAMID_16_TIME5（最多16檔、5日釋放）",
}
MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "ONE_SHOT_12": {
        "max_positions": 12,
        "mother_risk_rate": 0.005,
        "add_risk_rate": 0.0,
        "time_rule": None,
    },
    "PYRAMID_12": {
        "max_positions": 12,
        "mother_risk_rate": 0.002,
        "add_risk_rate": 0.004,
        "time_rule": None,
    },
    "PYRAMID_12_TIME5": {
        "max_positions": 12,
        "mother_risk_rate": 0.002,
        "add_risk_rate": 0.004,
        "time_rule": "NO_0_5R_CLOSE_BY_BAR_5",
    },
    "PYRAMID_12_TIME10": {
        "max_positions": 12,
        "mother_risk_rate": 0.002,
        "add_risk_rate": 0.004,
        "time_rule": "NO_1R_CLOSE_BY_BAR_10",
    },
    "PYRAMID_16": {
        "max_positions": 16,
        "mother_risk_rate": 0.002,
        "add_risk_rate": 0.004,
        "time_rule": None,
    },
    "PYRAMID_16_TIME5": {
        "max_positions": 16,
        "mother_risk_rate": 0.002,
        "add_risk_rate": 0.004,
        "time_rule": "NO_0_5R_CLOSE_BY_BAR_5",
    },
}


def _mean(values: list[float]) -> float | None:
    return None if not values else round(statistics.fmean(values), 4)


def promotion_signal(close: float, entry: float, defense: float, threshold_r: float = 1.0) -> bool:
    risk = entry - defense
    return risk > 0 and close >= entry + threshold_r * risk


def time_release_signal(
    *,
    rule: str | None,
    bars_held: int,
    max_close_r: float,
    promotion_attempted: bool,
) -> bool:
    if rule == "NO_0_5R_CLOSE_BY_BAR_5":
        return bars_held >= 5 and max_close_r < 0.5 and not promotion_attempted
    if rule == "NO_1R_CLOSE_BY_BAR_10":
        return bars_held >= 10 and max_close_r < 1.0 and not promotion_attempted
    return False


def _maps(frames: dict[str, pd.DataFrame], first_day: str, as_of: str) -> tuple[list[str], dict[str, dict[str, dict[str, float]]]]:
    calendar = sorted(
        {
            value.date().isoformat()
            for frame in frames.values()
            for value in frame["date"]
            if first_day <= value.date().isoformat() <= as_of
        }
    )
    prices: dict[str, dict[str, dict[str, float]]] = {}
    for symbol, frame in frames.items():
        prices[symbol] = {
            row.date.date().isoformat(): {"open": float(row.open), "close": float(row.close)}
            for row in frame[["date", "open", "close"]].itertuples(index=False)
        }
    return calendar, prices


def _trade_record(holding: dict[str, Any], *, status: str, day: str, mark_price: float | None = None, commission_rate: float, sell_tax_rate: float) -> dict[str, Any]:
    liquidation = 0.0
    estimated_cost = 0.0
    if mark_price is not None and holding["remaining_shares"]:
        gross = holding["remaining_shares"] * mark_price
        estimated_cost = gross * (commission_rate + sell_tax_rate)
        liquidation = gross - estimated_cost
    pnl = holding["net_sale_proceeds"] + liquidation - holding["buy_outflow"]
    gross_buys = holding["gross_buy_value"]
    return {
        "trade_id": holding["row"]["trade_id"],
        "code": holding["row"]["code"],
        "name": holding["row"]["name"],
        "entry_date": holding["row"]["entry_date"],
        "priority_score": holding["row"]["priority_score"],
        "same_day_rank": holding["row"]["same_day_rank"],
        "status": status,
        "status_label": "OPEN（持有中）" if status == "OPEN" else "CLOSED（交易已結束）",
        "performance_date": day,
        "mark_price": None if mark_price is None else round(mark_price, 4),
        "buy_legs": holding["buy_legs"],
        "sales": holding["sales"],
        "promoted": holding["promoted"],
        "promotion_signal_date": holding["promotion_signal_date"],
        "promotion_skip_reason": holding["promotion_skip_reason"],
        "time_release_reason": holding["time_release_reason"],
        "net_pnl": round(pnl, 4),
        "net_return_on_gross_buys_pct": round(pnl / gross_buys * 100.0, 4),
        "costs_including_open_liquidation_estimate": round(holding["costs_paid"] + estimated_cost, 4),
        "reference_mfe_pct": holding["row"]["reference_mfe_pct"],
    }


def simulate_dynamic(
    *,
    universe: list[dict[str, Any]],
    frames: dict[str, pd.DataFrame],
    model: str,
    as_of: str,
    initial_cash: float = 500_000.0,
    portfolio_heat_rate: float = 0.03,
    single_stock_cap_rate: float = 0.20,
    cash_reserve_rate: float = 0.10,
    commission_rate: float = 0.001425,
    sell_tax_rate: float = 0.003,
    promotion_r: float = 1.0,
    add_chase_cap_r: float = 1.5,
) -> dict[str, Any]:
    config = MODEL_CONFIGS[model]
    entries_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in universe:
        entries_by_day[row["entry_date"]].append(row)
    for rows in entries_by_day.values():
        rows.sort(key=lambda row: (-row["priority_score"], -row["priority_evidence"]["radar_score"], row["code"]))

    calendar, prices = _maps(frames, universe[0]["entry_date"], as_of)
    next_symbol_day: dict[str, dict[str, str]] = {}
    for symbol, values in prices.items():
        symbol_days = sorted(day for day in values if calendar[0] <= day <= as_of)
        next_symbol_day[symbol] = {
            symbol_days[index]: symbol_days[index + 1]
            for index in range(len(symbol_days) - 1)
        }
    cash = float(initial_cash)
    holdings: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    max_concurrent = 0
    promotion_attempt_count = 0
    promotion_count = 0
    time_release_count = 0

    def price(holding: dict[str, Any], day: str, field: str) -> float:
        value = prices[holding["row"]["symbol"]].get(day, {}).get(field)
        if value is not None:
            holding["last_mark"] = float(value)
        return float(holding["last_mark"])

    def equity(day: str, net: bool = True) -> float:
        total = cash
        for holding in holdings.values():
            gross = holding["remaining_shares"] * price(holding, day, "close")
            if net:
                gross *= 1.0 - commission_rate - sell_tax_rate
            total += gross
        return total

    def sell_all(holding: dict[str, Any], day: str, sale_price: float, reason: str) -> None:
        nonlocal cash
        qty = int(holding["remaining_shares"])
        gross = qty * sale_price
        commission = gross * commission_rate
        tax = gross * sell_tax_rate
        net_proceeds = gross - commission - tax
        cash += net_proceeds
        holding["remaining_shares"] = 0
        holding["remaining_risk_dollars"] = 0.0
        holding["net_sale_proceeds"] += net_proceeds
        holding["costs_paid"] += commission + tax
        sale = {"date": day, "reason": reason, "quantity": qty, "price": round(sale_price, 4), "commission": round(commission, 4), "tax": round(tax, 4), "net_proceeds": round(net_proceeds, 4)}
        holding["sales"].append(sale)
        transactions.append({"trade_id": holding["row"]["trade_id"], "type": "FINAL_SELL", **sale})

    def sell_partial(holding: dict[str, Any], day: str, sale_price: float, fraction: float, reason: str) -> None:
        nonlocal cash
        qty = min(int(holding["remaining_shares"]), math.floor(holding["total_bought_shares"] * fraction))
        if qty <= 0:
            return
        gross = qty * sale_price
        commission = gross * commission_rate
        tax = gross * sell_tax_rate
        net_proceeds = gross - commission - tax
        cash += net_proceeds
        holding["remaining_shares"] -= qty
        holding["remaining_risk_dollars"] = holding["total_risk_dollars"] * holding["remaining_shares"] / holding["total_bought_shares"]
        holding["net_sale_proceeds"] += net_proceeds
        holding["costs_paid"] += commission + tax
        sale = {"date": day, "reason": reason, "quantity": qty, "price": round(sale_price, 4), "commission": round(commission, 4), "tax": round(tax, 4), "net_proceeds": round(net_proceeds, 4)}
        holding["sales"].append(sale)
        transactions.append({"trade_id": holding["row"]["trade_id"], "type": "PARTIAL_SELL", **sale})

    def buy_leg(holding: dict[str, Any], day: str, buy_price: float, quantity: int, leg: str, per_share_risk: float) -> None:
        nonlocal cash
        gross = quantity * buy_price
        commission = gross * commission_rate
        outflow = gross + commission
        cash -= outflow
        risk_dollars = quantity * per_share_risk
        holding["remaining_shares"] += quantity
        holding["total_bought_shares"] += quantity
        holding["gross_buy_value"] += gross
        holding["buy_outflow"] += outflow
        holding["costs_paid"] += commission
        holding["total_risk_dollars"] += risk_dollars
        holding["remaining_risk_dollars"] += risk_dollars
        record = {"date": day, "leg": leg, "quantity": quantity, "price": round(buy_price, 4), "commission": round(commission, 4), "risk_dollars": round(risk_dollars, 4)}
        holding["buy_legs"].append(record)
        transactions.append({"trade_id": holding["row"]["trade_id"], "type": "BUY", **record})

    for day in calendar:
        # Full exits have precedence over scheduled adds and partial sales.
        closing: list[str] = []
        for trade_id, holding in list(holdings.items()):
            plan = holding["row"]["exit_plan"]
            base_exit_today = plan["status"] == "CLOSED" and plan["exit_date"] == day
            time_exit_today = holding["pending_time_exit"] == day
            if base_exit_today or time_exit_today:
                reason = holding["time_release_reason"] if time_exit_today else str(plan["exit_reason"])
                sale_price = price(holding, day, "open") if time_exit_today else float(plan["exit_price"])
                sell_all(holding, day, sale_price, str(reason))
                trades.append(_trade_record(holding, status="CLOSED", day=day, commission_rate=commission_rate, sell_tax_rate=sell_tax_rate))
                closing.append(trade_id)
        for trade_id in closing:
            holdings.pop(trade_id)

        for holding in holdings.values():
            plan = holding["row"]["exit_plan"]
            for partial_index, partial in enumerate(plan.get("partial_exits") or []):
                if partial["date"] == day and partial_index not in holding["processed_partials"]:
                    sell_partial(holding, day, float(partial["price"]), float(partial["fraction"]), str(partial["reason"]))
                    holding["processed_partials"].add(partial_index)

        sizing_equity = curve[-1]["equity"] if curve else equity(day)

        # Adds are reserved for positions already proven by a +1R close and
        # execute at the next open.  They take priority over new mother orders.
        pending_adds = sorted(
            [holding for holding in holdings.values() if holding["pending_add"] == day],
            key=lambda holding: (-holding["row"]["priority_score"], holding["row"]["code"]),
        )
        for holding in pending_adds:
            promotion_attempt_count += 1
            holding["promotion_attempted"] = True
            add_price = price(holding, day, "open")
            entry = float(holding["entry_price"])
            risk = entry - float(holding["defense"])
            if add_price <= entry:
                holding["promotion_skip_reason"] = "ADD_NOT_ABOVE_COST（加碼價未高於母單成本）"
                continue
            if add_price > entry + add_chase_cap_r * risk:
                holding["promotion_skip_reason"] = "ADD_NO_CHASE（加碼開盤超過1.5R）"
                continue
            current_heat = sum(float(item["remaining_risk_dollars"]) for item in holdings.values())
            available_heat = max(0.0, sizing_equity * portfolio_heat_rate - current_heat)
            desired_risk = min(sizing_equity * float(config["add_risk_rate"]), available_heat)
            defense = float(holding["defense"])
            per_share_risk = add_price - defense + add_price * commission_rate + defense * (commission_rate + sell_tax_rate)
            stock_room = max(0.0, sizing_equity * single_stock_cap_rate - holding["remaining_shares"] * add_price)
            cash_room = max(0.0, cash - sizing_equity * cash_reserve_rate)
            quantity = max(
                0,
                min(
                    math.floor(desired_risk / per_share_risk) if per_share_risk > 0 else 0,
                    math.floor(stock_room / (add_price * (1.0 + commission_rate))),
                    math.floor(cash_room / (add_price * (1.0 + commission_rate))),
                ),
            )
            if quantity <= 0:
                holding["promotion_skip_reason"] = "ADD_CAPACITY_BLOCKED（加碼受風險或資金限制）"
                continue
            buy_leg(holding, day, add_price, quantity, "ADD_AFTER_1R（+1R確認加碼）", per_share_risk)
            holding["promoted"] = True
            promotion_count += 1

        # New mother positions.
        signals = entries_by_day.get(day, [])
        open_codes = {holding["row"]["code"] for holding in holdings.values()}
        eligible: list[dict[str, Any]] = []
        for row in signals:
            if row["code"] in open_codes:
                skipped.append({"trade_id": row["trade_id"], "code": row["code"], "entry_date": day, "reason": "DUPLICATE_OPEN（同股仍持有）", "reference_mfe_pct": row["reference_mfe_pct"]})
            else:
                eligible.append(row)
        slots = max(0, int(config["max_positions"]) - len(holdings))
        selected = eligible[:slots]
        for row in eligible[slots:]:
            skipped.append({"trade_id": row["trade_id"], "code": row["code"], "entry_date": day, "reason": "SKIPPED_POSITION_CAP（持股上限）", "reference_mfe_pct": row["reference_mfe_pct"]})

        current_heat = sum(float(holding["remaining_risk_dollars"]) for holding in holdings.values())
        available_heat = max(0.0, sizing_equity * portfolio_heat_rate - current_heat)
        desired_each = sizing_equity * float(config["mother_risk_rate"])
        heat_scale = 0.0 if not selected or desired_each <= 0 else min(1.0, available_heat / (desired_each * len(selected)))
        proposals: list[dict[str, Any]] = []
        for row in selected:
            entry = float(row["entry_price"])
            defense = float(row["defense"])
            per_share_risk = entry - defense + entry * commission_rate + defense * (commission_rate + sell_tax_rate)
            risk_budget = desired_each * heat_scale
            quantity = min(
                math.floor(risk_budget / per_share_risk) if per_share_risk > 0 else 0,
                math.floor(sizing_equity * single_stock_cap_rate / (entry * (1.0 + commission_rate))),
            )
            proposals.append({"row": row, "entry": entry, "defense": defense, "per_share_risk": per_share_risk, "quantity": max(0, quantity)})
        cash_room = max(0.0, cash - sizing_equity * cash_reserve_rate)
        total_cost = sum(item["quantity"] * item["entry"] * (1.0 + commission_rate) for item in proposals)
        cash_scale = 1.0 if total_cost <= cash_room or total_cost == 0 else cash_room / total_cost
        for proposal in proposals:
            row = proposal["row"]
            quantity = math.floor(proposal["quantity"] * cash_scale)
            if quantity <= 0:
                skipped.append({"trade_id": row["trade_id"], "code": row["code"], "entry_date": day, "reason": "SKIPPED_HEAT_CASH_SIZE（風險、現金或部位不足）", "reference_mfe_pct": row["reference_mfe_pct"]})
                continue
            holding = {
                "row": row,
                "entry_price": proposal["entry"],
                "defense": proposal["defense"],
                "remaining_shares": 0,
                "total_bought_shares": 0,
                "gross_buy_value": 0.0,
                "buy_outflow": 0.0,
                "net_sale_proceeds": 0.0,
                "costs_paid": 0.0,
                "total_risk_dollars": 0.0,
                "remaining_risk_dollars": 0.0,
                "buy_legs": [],
                "sales": [],
                "processed_partials": set(),
                "last_mark": proposal["entry"],
                "bars_held": 0,
                "max_close_r": float("-inf"),
                "promotion_attempted": False,
                "promoted": False,
                "promotion_signal_date": None,
                "promotion_skip_reason": None,
                "pending_add": None,
                "pending_time_exit": None,
                "time_release_reason": None,
            }
            holdings[row["trade_id"]] = holding
            buy_leg(holding, day, proposal["entry"], quantity, "MOTHER（母單）", proposal["per_share_risk"])

        # Close decisions are scheduled only after this day's completed bar.
        for holding in holdings.values():
            if day not in prices[holding["row"]["symbol"]]:
                continue
            holding["bars_held"] += 1
            close = price(holding, day, "close")
            raw_risk = float(holding["entry_price"]) - float(holding["defense"])
            close_r = (close - float(holding["entry_price"])) / raw_risk if raw_risk > 0 else float("-inf")
            holding["max_close_r"] = max(float(holding["max_close_r"]), close_r)
            if (
                float(config["add_risk_rate"]) > 0
                and not holding["promotion_attempted"]
                and holding["pending_add"] is None
                and promotion_signal(close, float(holding["entry_price"]), float(holding["defense"]), promotion_r)
            ):
                holding["promotion_signal_date"] = day
                holding["pending_add"] = next_symbol_day[holding["row"]["symbol"]].get(day)
            if (
                holding["pending_time_exit"] is None
                and time_release_signal(
                    rule=config["time_rule"],
                    bars_held=int(holding["bars_held"]),
                    max_close_r=float(holding["max_close_r"]),
                    promotion_attempted=bool(holding["promotion_attempted"] or holding["pending_add"]),
                )
            ):
                holding["pending_time_exit"] = next_symbol_day[holding["row"]["symbol"]].get(day)
                holding["time_release_reason"] = f"TIME_RELEASE（{config['time_rule']}）"

        max_concurrent = max(max_concurrent, len(holdings))
        net_equity = equity(day)
        gross_equity = equity(day, net=False)
        invested = sum(holding["remaining_shares"] * price(holding, day, "close") for holding in holdings.values())
        heat = sum(float(holding["remaining_risk_dollars"]) for holding in holdings.values())
        curve.append({"date": day, "equity": round(net_equity, 4), "cash": round(cash, 4), "invested_pct": round(invested / gross_equity * 100.0, 4) if gross_equity else None, "open_positions": len(holdings), "committed_heat_pct": round(heat / gross_equity * 100.0, 4) if gross_equity else None})

    for holding in holdings.values():
        mark = price(holding, as_of, "close")
        trades.append(_trade_record(holding, status="OPEN", day=as_of, mark_price=mark, commission_rate=commission_rate, sell_tax_rate=sell_tax_rate))

    equities = [float(row["equity"]) for row in curve]
    peak = initial_cash
    drawdowns: list[float] = []
    for value in equities:
        peak = max(peak, value)
        drawdowns.append((value / peak - 1.0) * 100.0)
    final_equity = equities[-1] if equities else initial_cash
    pnls = [float(row["net_pnl"]) for row in trades]
    accepted_ids = {row["trade_id"] for row in trades}
    big = [row for row in universe if float(row["reference_mfe_pct"]) >= 30.0]
    completed_by_id = {row["trade_id"]: row for row in trades}
    big_admitted = [row for row in big if row["trade_id"] in accepted_ids]
    big_positive = [row for row in big_admitted if float(completed_by_id[row["trade_id"]]["net_pnl"]) > 0]
    big_cut_nonpositive = [row for row in big_admitted if float(completed_by_id[row["trade_id"]]["net_pnl"]) <= 0]
    time_release_count = sum(bool(row["time_release_reason"]) for row in trades)
    max_drawdown = min(drawdowns) if drawdowns else 0.0
    total_return = (final_equity / initial_cash - 1.0) * 100.0
    average_invested = _mean([float(row["invested_pct"]) for row in curve if row["invested_pct"] is not None])
    return {
        "model": model,
        "model_label": MODEL_LABELS[model],
        "summary": {
            "initial_cash": initial_cash,
            "final_net_liquidation_equity": round(final_equity, 2),
            "total_return_pct": round(total_return, 4),
            "max_drawdown_pct": round(max_drawdown, 4),
            "return_to_max_drawdown": None if max_drawdown == 0 else round(total_return / abs(max_drawdown), 4),
            "executed_trade_count": len(trades),
            "positive_trade_rate_pct": None if not trades else round(sum(value > 0 for value in pnls) / len(pnls) * 100.0, 2),
            "profit_factor_net_dollars": _profit_factor(pnls),
            "max_concurrent_positions": max_concurrent,
            "average_invested_pct": average_invested,
            "return_per_average_invested_point": None if not average_invested else round(total_return / average_invested, 4),
            "max_committed_heat_pct": round(max((float(row["committed_heat_pct"]) for row in curve if row["committed_heat_pct"] is not None), default=0.0), 4),
            "total_costs_including_open_liquidation_estimate": round(sum(float(row["costs_including_open_liquidation_estimate"]) for row in trades), 2),
            "promotion_attempt_count": promotion_attempt_count,
            "promotion_count": promotion_count,
            "promotion_success_rate_pct": None if promotion_attempt_count == 0 else round(promotion_count / promotion_attempt_count * 100.0, 2),
            "time_release_count": time_release_count,
            "skipped_signal_count": len(skipped),
            "skip_reason_counts": dict(sorted(Counter(row["reason"] for row in skipped).items())),
            "mfe_30_total": len(big),
            "mfe_30_admitted": len(big_admitted),
            "mfe_30_positive_after_actual_management": len(big_positive),
            "mfe_30_mean_net_return_pct": _mean([float(completed_by_id[row["trade_id"]]["net_return_on_gross_buys_pct"]) for row in big_admitted]),
            "mfe_30_missed": [f"{row['code']} {row['name']}" for row in big if row["trade_id"] not in accepted_ids],
            "mfe_30_cut_to_nonpositive": [f"{row['code']} {row['name']}" for row in big_cut_nonpositive],
        },
        "trades": sorted(trades, key=lambda row: (row["entry_date"], row["code"])),
        "skipped": skipped,
        "transactions": transactions,
        "equity_curve": curve,
    }


def build_payload(radar_path: Path, exits_path: Path, baseline_path: Path) -> dict[str, Any]:
    source = json.loads(radar_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    universe, frames, audit = build_universe(radar_path, exits_path, "HYBRID_RUNNER")
    results = {
        model: simulate_dynamic(universe=universe, frames=frames, model=model, as_of=source["as_of"])
        for model in MODEL_CONFIGS
    }
    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": source["as_of"],
        "selection_window": source["selection_window"],
        "parameters": {
            "initial_cash": 500_000.0,
            "base_exit": "HYBRID_RUNNER",
            "portfolio_heat_rate": 0.03,
            "single_stock_cap_rate": 0.20,
            "cash_reserve_rate": 0.10,
            "commission_rate": 0.001425,
            "sell_tax_rate": 0.003,
            "promotion": "first close_at_or_above_1R_then_next_session_open",
            "add_guard": "add_open_above_mother_cost_and_not_above_1.5R",
            "loss_add": "forbidden",
            "time5": "after fifth held close, exit next open if best close below +0.5R",
            "time10": "after tenth held close, exit next open if best close below +1R",
            "slippage": "not_modeled",
            "minimum_commission": "not_modeled_broker_specific",
            "model_configs": MODEL_CONFIGS,
        },
        "audit": audit,
        "baseline_static": {
            "ALL_QUALIFIED": baseline["results"]["HYBRID_RUNNER"]["ALL_QUALIFIED"]["summary"],
            "TOP_5": baseline["results"]["HYBRID_RUNNER"]["TOP_5"]["summary"],
        },
        "results": results,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# 啟蒙層動態母單／順勢加碼回測｜截至 {payload['as_of']}",
        "",
        "> 研究用途，不是投資建議。同一組90筆啟蒙觸發，固定使用 HYBRID_RUNNER（混合波段出場），只改變進場部位與資金釋放規則。",
        "",
        "## 規則",
        "",
        "- 一次進場版：每筆風險0.5%，最多同持12檔。",
        "- 金字塔版：母單風險0.2%；日K收盤首次達+1R後，次日開盤追加最多0.4%風險。加碼價必須高於母單成本且不得高於+1.5R，虧損部位不加碼。",
        "- 5日釋放：持有第5根日K收盤後，若最好收盤仍未達+0.5R，次日開盤退出。",
        "- 10日釋放：持有第10根日K收盤後，若最好收盤仍未達+1R，次日開盤退出。",
        "- 全部版本：50萬元、組合風險上限3%、單檔市值上限20%、保留現金10%，並計入標準手續費與賣出稅。",
        "",
        "## 結果",
        "",
        "| 模型 | 期末淨值 | 總報酬 | 最大回撤 | 報酬/回撤 | 進場筆數 | PF | 最大同持 | 平均投入 | 加碼成功 | 時間釋放 | 大波段正獲利 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model in MODEL_CONFIGS:
        row = payload["results"][model]["summary"]
        promoted = "—" if row["promotion_attempt_count"] == 0 else f"{row['promotion_count']}/{row['promotion_attempt_count']}"
        lines.append(
            f"| {MODEL_LABELS[model]} | {row['final_net_liquidation_equity']:,.0f} | {_f(row['total_return_pct'])}% | {_f(row['max_drawdown_pct'])}% | {_f(row['return_to_max_drawdown'])} | "
            f"{row['executed_trade_count']} | {_f(row['profit_factor_net_dollars'])} | {row['max_concurrent_positions']} | "
            f"{_f(row['average_invested_pct'])}% | {promoted} | {row['time_release_count']} | {row['mfe_30_positive_after_actual_management']}/{row['mfe_30_total']} |"
        )
    base_all = payload["baseline_static"]["ALL_QUALIFIED"]
    base_top5 = payload["baseline_static"]["TOP_5"]
    lines += [
        "",
        "## 前一輪基準",
        "",
        f"- ALL_QUALIFIED（全部合格、受總風險上限）：{_f(base_all['total_return_pct'])}%，最大回撤 {_f(base_all['max_drawdown_pct'])}%，最大同持 {base_all['max_concurrent_positions']} 檔。",
        f"- TOP_5（最多同持5檔）：{_f(base_top5['total_return_pct'])}%，最大回撤 {_f(base_top5['max_drawdown_pct'])}%，只有 {base_top5['mfe_30_captured']}/{base_top5['mfe_30_total']} 筆大波段取得進場。",
        "",
        "## 大波段漏接",
        "",
    ]
    for model in MODEL_CONFIGS:
        row = payload["results"][model]["summary"]
        missed = "、".join(row["mfe_30_missed"]) or "無"
        cut = "、".join(row["mfe_30_cut_to_nonpositive"]) or "無"
        lines.append(f"- {MODEL_LABELS[model]}：未進場 {missed}；進場後未保住正報酬 {cut}。")
    lines += [
        "",
        "## 限制",
        "",
        "- +1R、+1.5R、5日、10日與0.2%／0.4%是本輪待驗證參數，不宣稱為課程固定答案。",
        "- 排名只用 ARMED（已建立進場計畫）當下資料；本輪不使用未來報酬挑股票。",
        "- 母單被持股上限排除後不延後補買；必須等待資料本身出現下一筆新觸發。",
        "- 未計滑價、券商最低手續費、委託簿衝擊、漲跌停成交失敗與除權息現金流。",
        "- 樣本期間短且只有90筆，最佳版本仍需更多歷史與前向模擬。",
        "",
        "## 稽核",
        "",
        f"- 方法版本：`{payload['method_version']}`",
        f"- 參數：`{json.dumps(payload['parameters'], ensure_ascii=False, sort_keys=True)}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radar-input", type=Path, default=ROOT / "reports" / "course_backtest" / "2026-09-04" / "radar_full_history_v2" / "backtest.json")
    parser.add_argument("--exit-input", type=Path, default=ROOT / "reports" / "course_backtest" / "2026-09-04" / "exit_variants_v1" / "comparison.json")
    parser.add_argument("--baseline-input", type=Path, default=ROOT / "reports" / "course_backtest" / "2026-09-04" / "position_variants_v1" / "comparison.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "course_backtest" / "2026-09-04" / "dynamic_positions_v1")
    args = parser.parse_args()
    payload = build_payload(args.radar_input, args.exit_input, args.baseline_input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "comparison.json"
    markdown_path = args.output_dir / "comparison.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    print(markdown_path.resolve())
    for model in MODEL_CONFIGS:
        print(model, json.dumps(payload["results"][model]["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
