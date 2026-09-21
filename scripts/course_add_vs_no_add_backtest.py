"""Corporate-action-aware comparison of one-shot and pyramiding position plans.

The comparison freezes the corrected original entry cohort.  Both plans have
the same NT$10,000 maximum gross campaign budget and the same causal exit
state machine.  Only the timing of the campaign budget differs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_corporate_action_backtest import (  # noqa: E402
    adjusted_history,
    event_dates,
    load_inputs,
)
from scripts.course_corporate_action_data import AS_OF, RUN, digest, read  # noqa: E402
from scripts.course_daily_screen_trial import THRESHOLDS, _add_indicators, _pivot_structure  # noqa: E402


METHOD_VERSION = "course-add-vs-no-add-v1"
MODEL_LABELS = {
    "ONE_SHOT": "ONE_SHOT（不加碼、一次建立）",
    "PYRAMID": "PYRAMID（金字塔加碼）",
}
STATE_LABELS = {
    "INITIAL_RISK": "INITIAL_RISK（初始風險期）",
    "PROFIT_PROTECT_1R": "PROFIT_PROTECT_1R（1R浮盈保護）",
    "TREND_RUNNER": "TREND_RUNNER（波段延伸期）",
    "EXIT_TRIGGERED": "EXIT_TRIGGERED（已觸發出場）",
    "CLOSED": "CLOSED（交易已結束）",
    "OPEN": "OPEN（持有中）",
}
EXIT_LABELS = {
    "FIXED_DEFENSE": "FIXED_DEFENSE（跌破初始結構防線）",
    "CAMPAIGN_COST": "CAMPAIGN_COST（延伸期整體跌回成本）",
    "DYNAMIC_PIVOT": "DYNAMIC_PIVOT（跌破只升不降確認樞紐）",
    "MA21_TWO_CLOSES": "MA21_TWO_CLOSES（延伸期連兩日跌破21MA）",
    "PROFIT_PROTECT_1R_GIVEBACK": "PROFIT_PROTECT_1R_GIVEBACK（1R後回吐最高收盤浮盈50%）",
    "AS_OF": "AS_OF（截至日估值）",
}


@dataclass(frozen=True)
class Costs:
    commission_rate: float = 0.001425
    sell_tax_rate: float = 0.003
    minimum_commission: float = 0.0
    slippage: float = 0.0

    def fee(self, gross: float) -> float:
        return 0.0 if gross <= 0 else max(self.minimum_commission, gross * self.commission_rate)

    def buy_terms(self, quantity: float, reference: float) -> tuple[float, float, float]:
        price = reference * (1.0 + self.slippage)
        gross = quantity * price
        return price, gross, gross + self.fee(gross)

    def sell_terms(self, quantity: float, reference: float) -> tuple[float, float, float, float]:
        price = reference * (1.0 - self.slippage)
        gross = quantity * price
        fee = self.fee(gross)
        tax = gross * self.sell_tax_rate
        return price, gross, gross - fee - tax, fee + tax


@dataclass(frozen=True)
class Model:
    code: str
    mother_budget: float
    add_budgets: tuple[float, ...]
    add_thresholds_r: tuple[float, ...]
    add_chase_caps_r: tuple[float, ...]

    @property
    def campaign_budget(self) -> float:
        return self.mother_budget + sum(self.add_budgets)


MODELS = {
    "ONE_SHOT": Model("ONE_SHOT", 10_000.0, (), (), ()),
    "PYRAMID": Model("PYRAMID", 5_000.0, (2_500.0, 2_500.0), (1.0, 2.0), (1.5, 2.5)),
}


def _sha(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _mean(values: list[float]) -> float | None:
    return None if not values else statistics.fmean(values)


def _median(values: list[float]) -> float | None:
    return None if not values else statistics.median(values)


def _profit_factor(values: list[float]) -> float | None:
    profits = sum(max(0.0, value) for value in values)
    losses = -sum(min(0.0, value) for value in values)
    return None if losses == 0 else profits / losses


def _quantity_for_budget(budget: float, reference: float, costs: Costs) -> int:
    """Largest integer-share order whose buy outflow does not exceed budget."""
    if budget <= 0 or reference <= 0:
        return 0
    estimated_price = reference * (1.0 + costs.slippage)
    quantity = max(0, math.floor(budget / estimated_price))
    while quantity and costs.buy_terms(quantity, reference)[2] > budget + 1e-9:
        quantity -= 1
    return quantity


def _make_economic_frame(
    raw: pd.DataFrame,
    actions: list[dict[str, Any]],
    entry_day: str,
) -> tuple[pd.DataFrame, dict[str, list[dict[str, Any]]], dict[str, dict[str, float]]]:
    """Return a causal total-return chart in entry-date price units.

    History through entry is affine-adjusted only with actions known by entry.
    Later dividends/share changes are then booked forward, avoiding artificial
    ex-date gaps in MA and pivot calculations.
    """
    prefix = adjusted_history(raw, actions, entry_day)
    keep = ["date", "open", "high", "low", "close", "volume"]
    pieces = [prefix[keep].copy()]
    mapped = event_dates(actions, raw)
    unit_factor = 1.0
    cash_per_entry_share = 0.0
    future_rows: list[dict[str, Any]] = []
    raw_prices: dict[str, dict[str, float]] = {}
    for row in raw.itertuples(index=False):
        day = row.date.date().isoformat()
        raw_prices[day] = {
            "open": float(row.open), "high": float(row.high), "low": float(row.low),
            "close": float(row.close), "volume": float(row.volume),
        }
        if day <= entry_day:
            continue
        for event in mapped.get(day, []):
            cash_per_entry_share += unit_factor * float(event.get("cash", 0.0))
            unit_factor *= float(event.get("ratio", 1.0))
        future_rows.append({
            "date": row.date,
            "open": unit_factor * float(row.open) + cash_per_entry_share,
            "high": unit_factor * float(row.high) + cash_per_entry_share,
            "low": unit_factor * float(row.low) + cash_per_entry_share,
            "close": unit_factor * float(row.close) + cash_per_entry_share,
            "volume": float(row.volume),
        })
    if future_rows:
        pieces.append(pd.DataFrame(future_rows, columns=keep))
    frame = pd.concat(pieces, ignore_index=True).drop_duplicates("date", keep="last")
    frame = frame.sort_values("date").reset_index(drop=True)
    frame = _add_indicators(frame)
    return frame, mapped, raw_prices


def _pivot_snapshots(frame: pd.DataFrame, start: int) -> dict[int, dict[str, Any] | None]:
    return {
        index: _pivot_structure(frame.iloc[: index + 1].copy(), THRESHOLDS)["defense"]
        for index in range(max(0, start - 1), len(frame))
    }


def _new_leg(day: str, label: str, target: float, reference: float, costs: Costs) -> dict[str, Any] | None:
    quantity = _quantity_for_budget(target, reference, costs)
    if quantity <= 0:
        return None
    price, gross, outflow = costs.buy_terms(quantity, reference)
    return {
        "label": label,
        "entry_date": day,
        "reference_price": reference,
        "entry_price": price,
        "original_quantity": quantity,
        "quantity": float(quantity),
        "gross_buy": gross,
        "buy_fee": outflow - gross,
        "buy_outflow": outflow,
        "dividends": 0.0,
        "sale_gross": 0.0,
        "sale_net": 0.0,
        "sale_cost": 0.0,
    }


def _position_values(legs: list[dict[str, Any]], reference: float, costs: Costs) -> dict[str, float]:
    quantity = sum(float(leg["quantity"]) for leg in legs)
    dividends = sum(float(leg["dividends"]) for leg in legs)
    gross_buys = sum(float(leg["gross_buy"]) for leg in legs)
    buy_outflow = sum(float(leg["buy_outflow"]) for leg in legs)
    _, gross, net, sell_cost = costs.sell_terms(quantity, reference)
    return {
        "quantity": quantity,
        "dividends": dividends,
        "gross_buys": gross_buys,
        "buy_outflow": buy_outflow,
        "gross_value": gross + dividends,
        "net_value": net + dividends,
        "gross_pnl": gross + dividends - gross_buys,
        "net_pnl": net + dividends - buy_outflow,
        "estimated_sell_cost": sell_cost,
    }


def _apply_actions(legs: list[dict[str, Any]], actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for action in actions:
        ratio = float(action.get("ratio", 1.0))
        cash = float(action.get("cash", 0.0))
        before = sum(float(leg["quantity"]) for leg in legs)
        entitlement = 0.0
        for leg in legs:
            old_quantity = float(leg["quantity"])
            leg_cash = old_quantity * cash
            leg["dividends"] += leg_cash
            leg["quantity"] = old_quantity * ratio
            entitlement += leg_cash
        records.append({
            "date": action["date"], "ratio": ratio, "cash_per_share": cash,
            "quantity_before": before,
            "quantity_after": sum(float(leg["quantity"]) for leg in legs),
            "cash_entitlement": entitlement,
            "source": action.get("source"),
        })
    return records


def simulate_trade(
    *,
    trade: dict[str, Any],
    raw: pd.DataFrame,
    actions: list[dict[str, Any]],
    model: Model,
    costs: Costs,
    as_of: str = AS_OF,
    profit_protect_1r: bool = False,
    profit_giveback_fraction: float = 0.50,
) -> dict[str, Any]:
    if not 0.0 < profit_giveback_fraction < 1.0:
        raise ValueError("profit_giveback_fraction must be between 0 and 1")
    frame, mapped_actions, raw_prices = _make_economic_frame(raw, actions, trade["entry_date"])
    frame = frame[frame.date.dt.strftime("%Y-%m-%d") <= as_of].reset_index(drop=True)
    day_to_index = {value.date().isoformat(): index for index, value in enumerate(frame.date)}
    start = day_to_index[trade["entry_date"]]
    pivots = _pivot_snapshots(frame, start)
    entry = float(trade["entry_price"])
    initial_defense = float(trade["initial_defense"])
    initial_r = entry - initial_defense
    if initial_r <= 0:
        raise ValueError(f"{trade['trade_id']}: non-positive initial R")

    mother = _new_leg(trade["entry_date"], "MOTHER（母單）", model.mother_budget, entry, costs)
    if mother is None:
        raise ValueError(f"{trade['trade_id']}: campaign budget cannot buy one share")
    legs = [mother]
    transactions = [{
        "date": trade["entry_date"], "type": "BUY", "leg": mother["label"],
        "quantity": mother["original_quantity"], "price": mother["entry_price"],
        "gross": mother["gross_buy"], "fee": mother["buy_fee"], "outflow": mother["buy_outflow"],
    }]
    action_records: list[dict[str, Any]] = []
    state_events = [{"date": trade["entry_date"], "state": STATE_LABELS["INITIAL_RISK"]}]
    add_events: list[dict[str, Any]] = []
    phase = "INITIAL_RISK"
    runner = False
    active_pivot = initial_defense
    prior = pivots.get(start - 1)
    if prior and float(prior["price"]) < entry:
        active_pivot = max(active_pivot, float(prior["price"]))
    add_count = 0
    pending_add: dict[str, Any] | None = None
    pending_exit: dict[str, Any] | None = None
    attempted_pivots: set[str] = set()
    last_add_pivot_price = initial_defense
    last_add_pivot_bar_date = trade["entry_date"]
    below_ma21_streak = 0
    daily: list[dict[str, Any]] = []
    exit_date = exit_price = exit_trigger_date = None
    exit_reason = "AS_OF"
    gross_sale = net_sale = sell_cost = 0.0
    max_campaign_gross_return = -math.inf
    max_campaign_date = trade["entry_date"]
    peak_close_r = -math.inf
    profit_protect_activation_date: str | None = None

    for index in range(start, len(frame)):
        row = frame.iloc[index]
        day = row.date.date().isoformat()
        raw_row = raw_prices[day]

        if day > trade["entry_date"] and mapped_actions.get(day):
            action_records.extend(_apply_actions(legs, mapped_actions[day]))

        if pending_exit is not None:
            quantity = sum(float(leg["quantity"]) for leg in legs)
            actual_price, gross_sale, net_sale, sell_cost = costs.sell_terms(quantity, raw_row["open"])
            total_quantity = quantity
            for leg in legs:
                share = 0.0 if total_quantity == 0 else float(leg["quantity"]) / total_quantity
                leg["sale_gross"] = gross_sale * share
                leg["sale_net"] = net_sale * share
                leg["sale_cost"] = sell_cost * share
            exit_date, exit_price = day, actual_price
            transactions.append({
                "date": day, "type": "SELL", "reason": pending_exit["reason"],
                "quantity": quantity, "price": actual_price, "gross": gross_sale,
                "cost": sell_cost, "net": net_sale,
            })
            state_events.append({"date": day, "state": STATE_LABELS["CLOSED"], "price": actual_price})
            break

        if pending_add is not None:
            threshold = float(pending_add["threshold_r"])
            cap = float(pending_add["chase_cap_r"])
            economic_open = float(row.open)
            reason = None
            if economic_open <= entry:
                reason = "ADD_NOT_ABOVE_MOTHER（加碼價未高於母單）"
            elif economic_open > entry + cap * initial_r:
                reason = "ADD_NO_CHASE（加碼開盤超過追價上限）"
            else:
                target = model.add_budgets[add_count]
                leg = _new_leg(day, f"ADD_{add_count + 1}（第{add_count + 1}次加碼）", target, raw_row["open"], costs)
                if leg is None:
                    reason = "ADD_TOO_SMALL（加碼預算不足一股）"
                else:
                    legs.append(leg)
                    add_count += 1
                    last_add_pivot_price = float(pending_add["pivot_price"])
                    last_add_pivot_bar_date = str(pending_add["pivot_bar_date"])
                    transactions.append({
                        "date": day, "type": "BUY", "leg": leg["label"],
                        "quantity": leg["original_quantity"], "price": leg["entry_price"],
                        "gross": leg["gross_buy"], "fee": leg["buy_fee"], "outflow": leg["buy_outflow"],
                    })
                    add_events.append({
                        "signal_date": pending_add["signal_date"], "date": day,
                        "status": "FILLED（已加碼）", "threshold_r": threshold,
                        "pivot_bar_date": pending_add["pivot_bar_date"],
                        "pivot_confirmed_date": pending_add["pivot_confirmed_date"],
                        "pivot_price": pending_add["pivot_price"], "price": leg["entry_price"],
                        "quantity": leg["original_quantity"], "gross": leg["gross_buy"],
                        "signal_close_r": pending_add["signal_close_r"],
                    })
            if reason:
                add_events.append({
                    "signal_date": pending_add["signal_date"], "date": day,
                    "status": reason, "threshold_r": threshold,
                    "pivot_bar_date": pending_add["pivot_bar_date"],
                    "pivot_confirmed_date": pending_add["pivot_confirmed_date"],
                    "pivot_price": pending_add["pivot_price"],
                    "signal_close_r": pending_add["signal_close_r"],
                })
            pending_add = None

        high_r = (float(row.high) - entry) / initial_r
        close_r = (float(row.close) - entry) / initial_r
        if not runner and ((index == start and close_r >= 2.0) or (index > start and high_r >= 2.0)):
            runner = True
            phase = "TREND_RUNNER"
            below_ma21_streak = 0
            state_events.append({
                "date": day, "state": STATE_LABELS[phase],
                "note": "+2R後進入獲利保護；進場當日須收盤達標以消除OHLC先後不明",
            })

        if not runner:
            peak_close_r = max(peak_close_r, close_r)
            if profit_protect_1r and profit_protect_activation_date is None and peak_close_r >= 1.0:
                profit_protect_activation_date = day
                phase = "PROFIT_PROTECT_1R"
                state_events.append({
                    "date": day,
                    "state": STATE_LABELS[phase],
                    "note": "收盤首次達+1R；後續若收盤浮盈回吐最高收盤浮盈50%，次交易日開盤出場",
                })

        below_ma21 = float(row.close) < float(row.MA21)
        if runner:
            below_ma21_streak = below_ma21_streak + 1 if below_ma21 else 0

        values = _position_values(legs, raw_row["close"], costs)
        campaign_gross_return = values["gross_pnl"] / values["gross_buys"] * 100.0
        if campaign_gross_return > max_campaign_gross_return:
            max_campaign_gross_return = campaign_gross_return
            max_campaign_date = day

        reason = None
        if float(row.close) < initial_defense:
            reason = "FIXED_DEFENSE"
        elif runner:
            if values["gross_value"] < values["gross_buys"]:
                reason = "CAMPAIGN_COST"
            elif float(row.close) < max(entry, active_pivot):
                reason = "DYNAMIC_PIVOT"
            elif below_ma21_streak >= 2:
                reason = "MA21_TWO_CLOSES"
        elif (
            profit_protect_1r
            and profit_protect_activation_date is not None
            and peak_close_r >= 1.0
            and close_r <= peak_close_r * (1.0 - profit_giveback_fraction)
        ):
            reason = "PROFIT_PROTECT_1R_GIVEBACK"

        current_pivot = pivots.get(index)
        daily.append({
            "date": day, "phase": STATE_LABELS[phase], "economic_close": float(row.close),
            "raw_close": raw_row["close"], "ma21": float(row.MA21),
            "active_pivot": active_pivot, "below_ma21_streak": below_ma21_streak,
            "gross_buys": values["gross_buys"], "gross_value": values["gross_value"],
            "net_liquidation_pnl": values["net_pnl"], "open_book_cost": values["buy_outflow"],
            "leg_count": len(legs), "exit_reason": reason,
        })
        if reason:
            exit_trigger_date = day
            exit_reason = reason
            pending_exit = {"reason": reason, "signal_date": day}
            state_events.append({
                "date": day, "state": STATE_LABELS["EXIT_TRIGGERED"],
                "reason": EXIT_LABELS[reason],
            })
            continue

        if add_count < len(model.add_budgets) and pending_add is None and current_pivot:
            pivot_price = float(current_pivot["price"])
            pivot_bar_date = str(current_pivot["bar_date"])
            pivot_key = f"{pivot_bar_date}:{pivot_price:.8f}"
            threshold = model.add_thresholds_r[add_count]
            fresh = (
                pivot_bar_date > last_add_pivot_bar_date
                and pivot_price > last_add_pivot_price
                and pivot_key not in attempted_pivots
            )
            if fresh and close_r >= threshold and float(row.close) > float(row.MA21):
                attempted_pivots.add(pivot_key)
                pending_add = {
                    "signal_date": day, "threshold_r": threshold,
                    "chase_cap_r": model.add_chase_caps_r[add_count],
                    "pivot_bar_date": pivot_bar_date,
                    "pivot_confirmed_date": str(current_pivot["confirmed_date"]),
                    "pivot_price": pivot_price, "signal_close_r": close_r,
                }

        if current_pivot:
            active_pivot = max(active_pivot, float(current_pivot["price"]))

    last = frame.iloc[-1]
    performance_day = exit_date or last.date.date().isoformat()
    status = "CLOSED" if exit_date else ("EXIT_TRIGGERED" if pending_exit else "OPEN")
    if exit_date:
        dividends = sum(float(leg["dividends"]) for leg in legs)
        gross_buys = sum(float(leg["gross_buy"]) for leg in legs)
        buy_outflow = sum(float(leg["buy_outflow"]) for leg in legs)
        gross_pnl = gross_sale + dividends - gross_buys
        net_pnl = net_sale + dividends - buy_outflow
        estimated_sell_cost = sell_cost
        final_book_cost = 0.0
    else:
        last_raw = raw_prices[performance_day]
        values = _position_values(legs, last_raw["close"], costs)
        gross_pnl = values["gross_pnl"]
        net_pnl = values["net_pnl"]
        estimated_sell_cost = values["estimated_sell_cost"]
        final_book_cost = values["buy_outflow"]

    leg_results = []
    final_reference = None if exit_date else raw_prices[performance_day]["close"]
    open_total_quantity = sum(float(leg["quantity"]) for leg in legs)
    if not exit_date:
        _, _, open_total_net, open_total_sell_cost = costs.sell_terms(open_total_quantity, float(final_reference))
    for leg in legs:
        if exit_date:
            leg_net_value = float(leg["sale_net"]) + float(leg["dividends"])
            leg_sell_cost = float(leg["sale_cost"])
        else:
            share = 0.0 if open_total_quantity == 0 else float(leg["quantity"]) / open_total_quantity
            leg_net_value = open_total_net * share + float(leg["dividends"])
            leg_sell_cost = open_total_sell_cost * share
        leg_results.append({
            **{key: value for key, value in leg.items() if key not in {"sale_gross", "sale_net", "sale_cost"}},
            "net_pnl": leg_net_value - float(leg["buy_outflow"]),
            "estimated_or_actual_sell_cost": leg_sell_cost,
        })

    return {
        "trade_id": trade["trade_id"], "code": trade["code"], "name": trade["name"],
        "entry_date": trade["entry_date"], "entry_price": entry,
        "initial_defense": initial_defense, "initial_risk_pct": initial_r / entry * 100.0,
        "model": model.code, "model_label": MODEL_LABELS[model.code],
        "status": status, "status_label": STATE_LABELS[status],
        "exit_trigger_date": exit_trigger_date, "exit_date": exit_date,
        "exit_price": exit_price, "exit_reason": exit_reason,
        "exit_reason_label": EXIT_LABELS[exit_reason], "performance_date": performance_day,
        "gross_buy_value": sum(float(leg["gross_buy"]) for leg in legs),
        "buy_outflow": sum(float(leg["buy_outflow"]) for leg in legs),
        "gross_pnl": gross_pnl, "net_pnl": net_pnl,
        "net_return_on_campaign_budget_pct": net_pnl / model.campaign_budget * 100.0,
        "net_return_on_deployed_pct": net_pnl / sum(float(leg["buy_outflow"]) for leg in legs) * 100.0,
        "max_campaign_gross_return_pct": max_campaign_gross_return,
        "max_campaign_date": max_campaign_date,
        "profit_protect_1r_enabled": profit_protect_1r,
        "profit_giveback_fraction": profit_giveback_fraction,
        "profit_protect_activation_date": profit_protect_activation_date,
        "reference_fixed_defense_mfe_pct": float(trade["mfe_pct"]),
        "add_count": add_count, "add_events": add_events,
        "legs": leg_results, "state_events": state_events, "action_events": action_records,
        "transactions": transactions, "daily": daily,
        "total_cost_including_open_liquidation": sum(float(leg["buy_fee"]) for leg in legs) + estimated_sell_cost,
        "final_open_book_cost": final_book_cost,
    }


def summarize(trades: list[dict[str, Any]], *, account: float = 500_000.0) -> dict[str, Any]:
    pnls = [float(trade["net_pnl"]) for trade in trades]
    returns = [float(trade["net_return_on_campaign_budget_pct"]) for trade in trades]
    deployed_returns = [float(trade["net_return_on_deployed_pct"]) for trade in trades]
    total_deployed = sum(float(trade["gross_buy_value"]) for trade in trades)

    calendar = sorted({row["date"] for trade in trades for row in trade["daily"]} |
                      {tx["date"] for trade in trades for tx in trade["transactions"]})
    equity_curve = []
    peak_equity = account
    drawdown = 0.0
    latest: dict[str, float] = {}
    book: dict[str, float] = {}
    for day in calendar:
        for trade in trades:
            rows = [row for row in trade["daily"] if row["date"] == day]
            if rows:
                latest[trade["trade_id"]] = float(rows[-1]["net_liquidation_pnl"])
                book[trade["trade_id"]] = float(rows[-1]["open_book_cost"])
            if trade["exit_date"] == day:
                latest[trade["trade_id"]] = float(trade["net_pnl"])
                book[trade["trade_id"]] = 0.0
        equity = account + sum(latest.values())
        peak_equity = max(peak_equity, equity)
        day_drawdown = (equity / peak_equity - 1.0) * 100.0
        drawdown = min(drawdown, day_drawdown)
        equity_curve.append({
            "date": day, "net_liquidation_equity": equity,
            "open_book_cost": sum(book.values()),
            "open_positions": sum(value > 0 for value in book.values()),
            "drawdown_pct": day_drawdown,
        })

    cash = account
    minimum_cash = account
    funding_day = None
    transaction_events = defaultdict(list)
    for trade in trades:
        for tx in trade["transactions"]:
            transaction_events[tx["date"]].append(tx)
    for day in sorted(transaction_events):
        # Existing positions sell before new positions/additions buy at the open.
        for tx in sorted(transaction_events[day], key=lambda item: 0 if item["type"] == "SELL" else 1):
            cash += float(tx.get("net", 0.0)) if tx["type"] == "SELL" else -float(tx["outflow"])
            if cash < minimum_cash:
                minimum_cash, funding_day = cash, day

    add_legs = [leg for trade in trades for leg in trade["legs"] if str(leg["label"]).startswith("ADD_")]
    big = [trade for trade in trades if float(trade["reference_fixed_defense_mfe_pct"]) >= 20.0]
    exit_counts = Counter(trade["exit_reason_label"] for trade in trades)
    final_equity = account + sum(pnls)
    return {
        "trade_count": len(trades),
        "closed_count": sum(trade["status"] == "CLOSED" for trade in trades),
        "open_or_triggered_count": sum(trade["status"] != "CLOSED" for trade in trades),
        "final_net_liquidation_equity": final_equity,
        "net_pnl": sum(pnls),
        "return_on_500k_pct": sum(pnls) / account * 100.0,
        "max_drawdown_pct": drawdown,
        "return_to_drawdown": None if drawdown == 0 else sum(pnls) / account * 100.0 / abs(drawdown),
        "positive_trade_rate_pct": None if not pnls else sum(value > 0 for value in pnls) / len(pnls) * 100.0,
        "mean_trade_return_on_campaign_budget_pct": _mean(returns),
        "median_trade_return_on_campaign_budget_pct": _median(returns),
        "mean_return_on_deployed_pct": _mean(deployed_returns),
        "profit_factor": _profit_factor(pnls),
        "gross_buy_value": total_deployed,
        "net_pnl_per_gross_buy_pct": None if not total_deployed else sum(pnls) / total_deployed * 100.0,
        "total_cost_including_open_liquidation": sum(float(trade["total_cost_including_open_liquidation"]) for trade in trades),
        "peak_open_book_cost": max((row["open_book_cost"] for row in equity_curve), default=0.0),
        "max_concurrent_positions": max((row["open_positions"] for row in equity_curve), default=0),
        "minimum_cash_without_dividend_reinvestment": minimum_cash,
        "funding_peak_date": funding_day,
        "funding_shortfall": max(0.0, -minimum_cash),
        "trades_with_add": sum(trade["add_count"] > 0 for trade in trades),
        "trades_with_two_adds": sum(trade["add_count"] > 1 for trade in trades),
        "add_leg_count": len(add_legs),
        "add_legs_net_pnl": sum(float(leg["net_pnl"]) for leg in add_legs),
        "add_legs_positive_rate_pct": None if not add_legs else sum(float(leg["net_pnl"]) > 0 for leg in add_legs) / len(add_legs) * 100.0,
        "mfe20_reference_count": len(big),
        "mfe20_positive_count": sum(float(trade["net_pnl"]) > 0 for trade in big),
        "mfe20_mean_campaign_return_pct": _mean([float(trade["net_return_on_campaign_budget_pct"]) for trade in big]),
        "exit_reason_counts": dict(exit_counts),
        "equity_curve": equity_curve,
    }


def build(input_path: Path = RUN / "backtest.json") -> dict[str, Any]:
    source = read(input_path)
    manifest_path = RUN / "input_manifest.json"
    manifest = read(manifest_path)
    fixed = list(source["fixed_original_entries"])
    if len(fixed) != 89:
        raise AssertionError(f"expected corrected 89 fixed entries, got {len(fixed)}")
    items = {str(item["code"]): item for item in manifest["items"]}
    loaded: dict[str, tuple[pd.DataFrame, list[dict[str, Any]]]] = {}
    for code in sorted({str(trade["code"]) for trade in fixed}):
        raw, actions, _ = load_inputs(items[code])
        loaded[code] = (raw, actions)

    scenarios = {
        "BASE": Costs(),
        "MIN20": Costs(minimum_commission=20.0),
        "MIN20_SLIP10BP": Costs(minimum_commission=20.0, slippage=0.001),
    }
    scenario_results: dict[str, Any] = {}
    for scenario, costs in scenarios.items():
        models = {}
        for model_code, model in MODELS.items():
            trades = []
            for number, trade in enumerate(fixed, 1):
                raw, actions = loaded[str(trade["code"])]
                trades.append(simulate_trade(
                    trade=trade, raw=raw, actions=actions, model=model, costs=costs,
                    as_of=source["as_of"],
                ))
                if scenario == "BASE" and number % 20 == 0:
                    print(f"{scenario} {model_code} {number}/{len(fixed)}", flush=True)
            models[model_code] = {"summary": summarize(trades), "trades": trades}
        scenario_results[scenario] = models

    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": source["as_of"],
        "cohort": {
            "name": "corrected_original_fixed_entry_cohort",
            "source_trade_count_before_closed_market_removal": 90,
            "valid_trade_count": len(fixed),
            "invalid_removed": "6113-2026-07-10（颱風休市日無可執行進場）",
        },
        "parameters": {
            "initial_account": 500_000.0,
            "campaign_budget": 10_000.0,
            "models": {key: asdict(value) for key, value in MODELS.items()},
            "add_confirmation": "close reaches +1R/+2R, above MA21, and a newly confirmed higher pivot exists; order next symbol session",
            "add_chase_caps": "+1.5R/+2.5R at next open",
            "loss_add": "forbidden",
            "runner_activation": "intraday high reaches +2R; entry session requires close >= +2R because OHLC order is unknown",
            "runner_exit": "campaign below gross cost, close below max(mother entry, causal rising pivot), or two runner closes below MA21",
            "initial_exit": "close below initial defense; sell next symbol session open",
            "new_pivot_effective_for_exit": "following symbol session",
            "corporate_actions": "same frozen corrected action inputs; economic chart includes non-reinvested entitlements",
            "integer_odd_lot_shares": True,
            "no_top_n_no_future_mfe_decisions": True,
            "cost_scenarios": {key: asdict(value) for key, value in scenarios.items()},
        },
        "inputs": [_sha(input_path), _sha(manifest_path)],
        "results": scenario_results,
    }


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    for scenario, models in payload["results"].items():
        for model_code, result in models.items():
            trades = result["trades"]
            summary = result["summary"]
            prefix = f"{scenario}.{model_code}"
            check(f"{prefix}.trade_count", len(trades) == 89, len(trades))
            max_outflow = max(sum(float(leg["buy_outflow"]) for leg in trade["legs"]) for trade in trades)
            check(f"{prefix}.campaign_cap", max_outflow <= 10_000.0 + 1e-6, max_outflow)
            worst_reconciliation = max(abs(
                float(trade["net_pnl"]) - sum(float(leg["net_pnl"]) for leg in trade["legs"])
            ) for trade in trades)
            check(f"{prefix}.leg_pnl_reconciliation", worst_reconciliation < 1e-6, worst_reconciliation)
            expected_equity = 500_000.0 + sum(float(trade["net_pnl"]) for trade in trades)
            equity_error = abs(float(summary["final_net_liquidation_equity"]) - expected_equity)
            check(f"{prefix}.equity_reconciliation", equity_error < 1e-6, equity_error)
            check(f"{prefix}.funding", float(summary["funding_shortfall"]) == 0.0,
                  summary["minimum_cash_without_dividend_reinvestment"])
            if model_code == "ONE_SHOT":
                add_count = sum(int(trade["add_count"]) for trade in trades)
                check(f"{prefix}.no_adds", add_count == 0, add_count)
            else:
                filled = [event for trade in trades for event in trade["add_events"]
                          if event["status"] == "FILLED（已加碼）"]
                threshold_errors = [event for event in filled
                                    if float(event["signal_close_r"]) + 1e-9 < float(event["threshold_r"])]
                future_confirmations = [event for event in filled
                                        if event["pivot_confirmed_date"] > event["signal_date"]]
                late_fills = [event for event in filled if event["date"] <= event["signal_date"]]
                check(f"{prefix}.add_thresholds", not threshold_errors, len(threshold_errors))
                check(f"{prefix}.causal_pivots", not future_confirmations, len(future_confirmations))
                check(f"{prefix}.next_session_add", not late_fills, len(late_fills))
                check(f"{prefix}.max_two_adds",
                      max(int(trade["add_count"]) for trade in trades) <= 2,
                      max(int(trade["add_count"]) for trade in trades))
            exit_order_errors = [trade["trade_id"] for trade in trades
                                 if trade["exit_date"] and trade["exit_date"] <= trade["exit_trigger_date"]]
            check(f"{prefix}.next_session_exit", not exit_order_errors, len(exit_order_errors))

    base = payload["results"]["BASE"]
    check("base.same_trade_ids",
          {trade["trade_id"] for trade in base["ONE_SHOT"]["trades"]}
          == {trade["trade_id"] for trade in base["PYRAMID"]["trades"]}, 89)
    check("base.same_max_campaign_budget",
          MODELS["ONE_SHOT"].campaign_budget == MODELS["PYRAMID"].campaign_budget,
          MODELS["ONE_SHOT"].campaign_budget)
    failures = [row for row in checks if not row["passed"]]
    return {
        "method_version": payload["method_version"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "check_count": len(checks), "passed_count": len(checks) - len(failures),
        "failed_count": len(failures), "checks": checks,
    }


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def render(payload: dict[str, Any]) -> str:
    base = payload["results"]["BASE"]
    lines = [
        f"# 加碼版與不加碼版公平回測｜截至 {payload['as_of']}",
        "",
        "> 研究用途，不是投資建議。使用除權息修正後的原90筆固定進場，其中1筆休市日無效，實際比較89筆。沒有用事後MFE選股。",
        "",
        "## 公平比較規格",
        "",
        "- 兩版每檔最高核定金額都為10,000元，50萬元帳戶，整數股零股，全部有效訊號進場，不做Top N。",
        "- ONE_SHOT（不加碼、一次建立）：進場時一次投入10,000元上限。",
        "- PYRAMID（金字塔加碼）：母單5,000元；+1R且出現新確認較高樞紐後加2,500元；+2R後再出現更新且更高樞紐再加2,500元。加碼次日開盤不得超過+1.5R／+2.5R。",
        "- 兩版使用相同狀態出場：初始期跌破固定結構防線；+2R後，整筆跌回成本、跌破只升不降的確認樞紐，或連兩日跌破21MA，次一交易日開盤出場。",
        "- 當日才確認的樞紐只能從下一交易日成為出場防線；虧損不加碼。",
        "",
        "## 主要結果（比例費用、無最低手續費、無滑價）",
        "",
        "| 指標 | 不加碼版 | 加碼版 |",
        "|---|---:|---:|",
    ]
    metrics = [
        ("期末淨值", "final_net_liquidation_equity", "money"),
        ("帳戶淨損益", "net_pnl", "money"),
        ("50萬元報酬", "return_on_500k_pct", "pct"),
        ("最大回撤", "max_drawdown_pct", "pct"),
        ("報酬／回撤", "return_to_drawdown", "num"),
        ("正報酬交易率", "positive_trade_rate_pct", "pct"),
        ("淨Profit factor", "profit_factor", "num"),
        ("總買進金額", "gross_buy_value", "money"),
        ("每投入金額淨報酬", "net_pnl_per_gross_buy_pct", "pct"),
        ("最高同時帳面投入", "peak_open_book_cost", "money"),
        ("最大同時持股", "max_concurrent_positions", "int"),
        ("MFE≥20%最後保持正報酬", "mfe20_positive_count", "ratio"),
        ("MFE≥20%平均策略報酬", "mfe20_mean_campaign_return_pct", "pct"),
    ]
    for label, key, kind in metrics:
        values = []
        for model_code in ("ONE_SHOT", "PYRAMID"):
            summary = base[model_code]["summary"]
            value = summary[key]
            if kind == "money":
                values.append(f"{value:,.0f}元")
            elif kind == "pct":
                values.append(f"{value:+.2f}%")
            elif kind == "int":
                values.append(str(value))
            elif kind == "ratio":
                values.append(f"{value}/{summary['mfe20_reference_count']}")
            else:
                values.append(_fmt(value))
        lines.append(f"| {label} | {values[0]} | {values[1]} |")

    pyramid = base["PYRAMID"]["summary"]
    lines += [
        "",
        "## 加碼本身的結果",
        "",
        f"- 89筆中，有 {pyramid['trades_with_add']} 筆至少加碼一次；{pyramid['trades_with_two_adds']} 筆完成兩次加碼，共 {pyramid['add_leg_count']} 個加碼批次。",
        f"- 單獨拆出加碼批次，合計淨損益 {_fmt(pyramid['add_legs_net_pnl'], 0)} 元，正報酬率 {_fmt(pyramid['add_legs_positive_rate_pct'])}%。",
        "- 這是各批實際買價到共同出場／截至日估值的損益，不把母單原有獲利算到加碼單。",
        "",
        "## 成本敏感度（帳戶淨損益）",
        "",
        "| 情境 | 不加碼版 | 加碼版 |",
        "|---|---:|---:|",
    ]
    for scenario, label in (
        ("BASE", "比例費用、無滑價"),
        ("MIN20", "每筆最低手續費20元"),
        ("MIN20_SLIP10BP", "最低20元＋單邊0.1%不利滑價"),
    ):
        one = payload["results"][scenario]["ONE_SHOT"]["summary"]["net_pnl"]
        add = payload["results"][scenario]["PYRAMID"]["summary"]["net_pnl"]
        lines.append(f"| {label} | {one:,.0f}元 | {add:,.0f}元 |")

    winner = max(("ONE_SHOT", "PYRAMID"), key=lambda key: base[key]["summary"]["net_pnl"])
    difference = base["PYRAMID"]["summary"]["net_pnl"] - base["ONE_SHOT"]["summary"]["net_pnl"]
    lines += [
        "",
        "## 判讀",
        "",
        f"- 本樣本主要比較結果：{MODEL_LABELS[winner]}淨損益較高；加碼版相對不加碼版差額為 {difference:+,.0f} 元。",
        "- 加碼版從母單只投入一半開始，因此總投入和早期風險較低；若最後績效較低，不能直接解讀為加碼無效，也可能是許多股票沒有出現合格的第二次進場，因而長期只持有半倉。",
        "- 若加碼版績效較高，仍需同時確認加碼批次本身為正，以及最大回撤沒有惡化；不能只看帳戶期末值。",
        "",
        "## 限制",
        "",
        "- +1R、+2R、5,000/2,500/2,500與追價上限是本輪事前固定的研究代理，不是課程宣稱的唯一最佳參數。",
        "- 新進場型態以『新的已確認較高樞紐＋收盤在21MA之上』代理，不是AI逐張圖主觀判讀。",
        "- 以日K OHLC回放；進場當日無法知道最高價與觸發的盤中先後，因此進場日只有收盤達+2R才切換延伸期。",
        "- 未模擬委託簿、漲跌停排隊、個別零股價差；配股可交易日與現增權益仍沿用前版資料品質限制。",
        "- 樣本短且參數尚未做樣本外或前向驗證，不能據此宣稱能穩定獲利。",
        "",
        f"方法版本：`{payload['method_version']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=RUN / "backtest.json")
    parser.add_argument("--output-dir", type=Path, default=RUN / "add_vs_no_add_v1")
    args = parser.parse_args()
    payload = build(args.input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "comparison.json"
    markdown_path = args.output_dir / "comparison.md"
    validation_path = args.output_dir / "validation.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render(payload), encoding="utf-8")
    validation = validate(payload)
    validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    if validation["failed_count"]:
        raise AssertionError(f"validation failed: {validation['failed_count']} checks")
    print(markdown_path.resolve())
    for model_code in ("ONE_SHOT", "PYRAMID"):
        summary = payload["results"]["BASE"][model_code]["summary"].copy()
        summary.pop("equity_curve", None)
        print(model_code, json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
