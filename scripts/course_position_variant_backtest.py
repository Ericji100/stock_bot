"""Backtest capital-constrained portfolio variants on course-triggered entries.

The entry universe comes from ``course_radar_trigger_backtest.py`` and the
exit paths come from ``course_exit_variant_backtest.py``.  Ranking is rebuilt
at each armed close with only data available at that time.  This script is a
research simulator; it does not place orders.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_daily_screen_trial import (  # noqa: E402
    THRESHOLDS,
    _decision,
    _elson_context,
    _laoxiao_context,
)
from scripts.course_radar_trigger_backtest import discover_radar_union  # noqa: E402
from scripts.course_watchlist_backtest import enlightenment_snapshot, load_full_frame  # noqa: E402


METHOD_VERSION = "course-position-variant-backtest-v1"
EXIT_VARIANTS = ("HYBRID_RUNNER", "MA21_ONE_CLOSE")
EXIT_LABELS = {
    "HYBRID_RUNNER": "HYBRID_RUNNER（混合波段出場）",
    "MA21_ONE_CLOSE": "MA21_ONE_CLOSE（首次收盤跌破21MA）",
}
MODEL_LABELS = {
    "ALL_QUALIFIED": "ALL_QUALIFIED（全部合格、受總風險上限）",
    "TOP_3": "TOP_3（最多同持3檔）",
    "TOP_5": "TOP_5（最多同持5檔）",
    "TOP_8": "TOP_8（最多同持8檔）",
    "CORE_AND_POOL": "CORE_AND_POOL（核心3檔＋探索池）",
}
MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "ALL_QUALIFIED": {"max_positions": None, "risk_rate": 0.005, "mode": "equal"},
    "TOP_3": {"max_positions": 3, "risk_rate": 0.005, "mode": "equal"},
    "TOP_5": {"max_positions": 5, "risk_rate": 0.005, "mode": "equal"},
    "TOP_8": {"max_positions": 8, "risk_rate": 0.005, "mode": "equal"},
    "CORE_AND_POOL": {
        "max_positions": 9,
        "core_positions": 3,
        "core_risk_rate": 0.006,
        "explore_risk_rate": 0.002,
        "mode": "core_pool",
    },
}


def _mean(values: list[float]) -> float | None:
    return None if not values else round(statistics.fmean(values), 4)


def _median(values: list[float]) -> float | None:
    return None if not values else round(statistics.median(values), 4)


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    return None if losses == 0 else round(gains / losses, 4)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _f(value: Any, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def _find_radar_scores(source: dict[str, Any]) -> dict[tuple[str, str], int]:
    start = date.fromisoformat(source["selection_window"]["start"])
    end = date.fromisoformat(source["as_of"])
    by_date, _ = discover_radar_union(start, end)
    return {
        (bar_date.isoformat(), row["code"]): int(row["score"])
        for bar_date, rows in by_date.items()
        for row in rows
    }


def _causal_priority(
    *,
    trade: dict[str, Any],
    frame: pd.DataFrame,
    radar_score: int,
) -> tuple[int, dict[str, Any]]:
    """Rebuild relative daily priority without a future-data market input.

    The market adjustment in the daily screen is identical for every stock on
    the same date and therefore cannot alter that day's relative order.  A
    neutral market value keeps the rank causal while avoiding a costly and
    survivorship-prone historical breadth rebuild.
    """

    armed_date = date.fromisoformat(trade["armed_date"])
    cut = frame[frame["date"].dt.date <= armed_date].copy()
    if cut.empty or cut.iloc[-1]["date"].date() != armed_date:
        raise ValueError(f"{trade['trade_id']} lacks an armed-date bar")
    snapshot = enlightenment_snapshot(cut, THRESHOLDS)
    elson = _elson_context(cut, snapshot["direction"], snapshot["setup"], snapshot["sstv"])
    laoxiao = _laoxiao_context(cut, snapshot["direction"], snapshot["setup"], THRESHOLDS)
    decision = _decision(
        frame=cut,
        selection_score=radar_score,
        radar_member=True,
        structure=snapshot["structure"],
        direction=snapshot["direction"],
        quadrant=snapshot["quadrant"],
        setup=snapshot["setup"],
        sstv=snapshot["sstv"],
        taiji=snapshot["taiji"],
        elson=elson,
        laoxiao=laoxiao,
        market_context={"level": "NEUTRAL", "new_entry_policy": "REDUCED"},
        thresholds=THRESHOLDS,
    )
    evidence = {
        "armed_date": trade["armed_date"],
        "radar_score": radar_score,
        "direction_relation": snapshot["direction"]["relation"],
        "quadrant": snapshot["quadrant"]["working_quadrant"],
        "pattern": snapshot["setup"]["pattern_code"],
        "sstv_quality": snapshot["sstv"]["quality"],
        "taiji_sequence": snapshot["taiji"]["sequence"],
        "taiji_state": snapshot["taiji"]["state"],
        "elson_alignment": elson["alignment"],
        "laoxiao_character": laoxiao["stock_character"],
        "laoxiao_zone": laoxiao["stage_zone"],
        "liquidity_pass": laoxiao["liquidity_pass"],
        "risk_distance_pct": snapshot["risk"]["distance_pct"],
        "market_component": "neutral_constant_does_not_change_same_day_order",
    }
    return int(decision["priority_score"]), evidence


def build_universe(
    radar_path: Path,
    exits_path: Path,
    exit_variant: str,
) -> tuple[list[dict[str, Any]], dict[str, pd.DataFrame], dict[str, Any]]:
    radar = json.loads(radar_path.read_text(encoding="utf-8"))
    exits = json.loads(exits_path.read_text(encoding="utf-8"))
    candidates = {row["code"]: row for row in radar["candidates"]}
    entries = {
        trade["trade_id"]: trade
        for candidate in radar["candidates"]
        for trade in candidate["trades"]
    }
    exit_rows = {row["trade_id"]: row for row in exits["results"][exit_variant]}
    if set(entries) != set(exit_rows):
        raise ValueError("entry and exit trade ids differ")

    frames: dict[str, pd.DataFrame] = {}
    as_of = date.fromisoformat(radar["as_of"])
    radar_scores = _find_radar_scores(radar)
    universe: list[dict[str, Any]] = []
    missing_scores: list[str] = []
    for trade_id, entry in entries.items():
        candidate = candidates[entry["code"]]
        symbol = str(candidate["symbol"])
        if symbol not in frames:
            frames[symbol], _ = load_full_frame(symbol, as_of)
        score_key = (entry["last_selected_date_before_entry"], entry["code"])
        radar_score = radar_scores.get(score_key)
        if radar_score is None:
            radar_score = 50
            missing_scores.append(trade_id)
        priority, evidence = _causal_priority(
            trade=entry,
            frame=frames[symbol],
            radar_score=radar_score,
        )
        exit_row = exit_rows[trade_id]
        item = dict(entry)
        item.update(
            {
                "symbol": symbol,
                "priority_score": priority,
                "priority_evidence": evidence,
                "exit_plan": exit_row,
                "reference_mfe_pct": float(exit_row["reference_mfe_pct"]),
            }
        )
        universe.append(item)

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in universe:
        by_day[item["entry_date"]].append(item)
    for rows in by_day.values():
        rows.sort(key=lambda row: (-row["priority_score"], -row["priority_evidence"]["radar_score"], row["code"]))
        for rank, row in enumerate(rows, 1):
            row["same_day_rank"] = rank
            row["same_day_signal_count"] = len(rows)
    universe.sort(key=lambda row: (row["entry_date"], row["same_day_rank"], row["code"]))
    audit = {
        "trade_count": len(universe),
        "missing_radar_score_count": len(missing_scores),
        "missing_radar_score_trade_ids": missing_scores,
        "priority_definition": "course_daily_screen priority at armed close, neutral market constant",
        "priority_uses_future_data": False,
    }
    return universe, frames, audit


def _net_return_for_ranking(
    row: dict[str, Any],
    commission_rate: float,
    sell_tax_rate: float,
) -> float:
    entry = float(row["entry_price"])
    exit_plan = row["exit_plan"]
    gross_proceeds = 0.0
    sell_cost = 0.0
    remaining = 1.0
    for partial in exit_plan.get("partial_exits") or []:
        fraction = min(float(partial["fraction"]), remaining)
        price = float(partial["price"])
        gross_proceeds += fraction * price
        sell_cost += fraction * price * (commission_rate + sell_tax_rate)
        remaining -= fraction
    terminal_price = float(exit_plan["performance_price"])
    gross_proceeds += remaining * terminal_price
    sell_cost += remaining * terminal_price * (commission_rate + sell_tax_rate)
    buy_cost = entry * (1.0 + commission_rate)
    return round((gross_proceeds - sell_cost - buy_cost) / entry * 100.0, 4)


def ranking_diagnostics(
    universe: list[dict[str, Any]],
    commission_rate: float,
    sell_tax_rate: float,
) -> dict[str, Any]:
    tiers = {"RANK_1_3": [], "RANK_4_5": [], "RANK_6_8": [], "RANK_9_PLUS": []}
    for row in universe:
        rank = int(row["same_day_rank"])
        key = "RANK_1_3" if rank <= 3 else "RANK_4_5" if rank <= 5 else "RANK_6_8" if rank <= 8 else "RANK_9_PLUS"
        tiers[key].append(row)
    output: dict[str, Any] = {}
    for key, rows in tiers.items():
        returns = [_net_return_for_ranking(row, commission_rate, sell_tax_rate) for row in rows]
        output[key] = {
            "trade_count": len(rows),
            "mean_net_return_pct": _mean(returns),
            "median_net_return_pct": _median(returns),
            "positive_rate_pct": None if not returns else round(sum(value > 0 for value in returns) / len(returns) * 100.0, 2),
            "profit_factor": _profit_factor(returns),
            "mfe_30_count": sum(float(row["reference_mfe_pct"]) >= 30.0 for row in rows),
        }
    scores = [int(row["priority_score"]) for row in universe]
    returns = [_net_return_for_ranking(row, commission_rate, sell_tax_rate) for row in universe]
    correlation = None
    if len(set(scores)) > 1 and len(set(returns)) > 1:
        score_ranks = pd.Series(scores).rank(method="average")
        return_ranks = pd.Series(returns).rank(method="average")
        correlation = round(float(score_ranks.corr(return_ranks)), 4)
    return {
        "same_day_rank_tiers": output,
        "priority_score_spearman_vs_net_return": correlation,
        "interpretation": "positive correlation and monotonic rank-tier results would support ranking; this sample is not out-of-sample",
    }


def _sell(
    *,
    holding: dict[str, Any],
    quantity: int,
    price: float,
    trade_date: str,
    reason: str,
    commission_rate: float,
    sell_tax_rate: float,
) -> tuple[float, float]:
    quantity = min(quantity, int(holding["remaining_shares"]))
    if quantity <= 0:
        return 0.0, 0.0
    gross = quantity * price
    commission = gross * commission_rate
    tax = gross * sell_tax_rate
    net = gross - commission - tax
    holding["remaining_shares"] -= quantity
    holding["remaining_risk_dollars"] = holding["initial_risk_dollars"] * (
        holding["remaining_shares"] / holding["initial_shares"]
    )
    holding["net_sale_proceeds"] += net
    holding["costs_paid"] += commission + tax
    holding["sales"].append(
        {
            "date": trade_date,
            "reason": reason,
            "quantity": quantity,
            "price": round(price, 4),
            "commission": round(commission, 4),
            "tax": round(tax, 4),
            "net_proceeds": round(net, 4),
        }
    )
    return net, commission + tax


def _mark_price(holding: dict[str, Any], trade_date: str) -> float:
    value = holding["close_by_date"].get(trade_date)
    if value is not None:
        holding["last_mark"] = value
    return float(holding["last_mark"])


def simulate_portfolio(
    *,
    universe: list[dict[str, Any]],
    frames: dict[str, pd.DataFrame],
    model: str,
    as_of: str,
    initial_cash: float,
    per_trade_risk_rate: float,
    portfolio_heat_rate: float,
    single_stock_cap_rate: float,
    cash_reserve_rate: float,
    commission_rate: float,
    sell_tax_rate: float,
) -> dict[str, Any]:
    config = dict(MODEL_CONFIGS[model])
    if config["mode"] == "equal":
        config["risk_rate"] = per_trade_risk_rate
    entries_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in universe:
        entries_by_date[row["entry_date"]].append(row)
    for rows in entries_by_date.values():
        rows.sort(key=lambda row: (-row["priority_score"], -row["priority_evidence"]["radar_score"], row["code"]))

    calendar = sorted(
        {
            value.date().isoformat()
            for frame in frames.values()
            for value in frame["date"]
            if universe[0]["entry_date"] <= value.date().isoformat() <= as_of
        }
    )
    close_maps = {
        symbol: {
            row.date.date().isoformat(): float(row.close)
            for row in frame[["date", "close"]].itertuples(index=False)
        }
        for symbol, frame in frames.items()
    }
    cash = float(initial_cash)
    costs_paid = 0.0
    holdings: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    max_concurrent = 0

    def current_equity(day: str, *, net_liquidation: bool = True) -> float:
        value = cash
        for holding in holdings.values():
            mark = _mark_price(holding, day)
            gross = holding["remaining_shares"] * mark
            if net_liquidation:
                gross *= 1.0 - commission_rate - sell_tax_rate
            value += gross
        return value

    for day in calendar:
        # Exits are executed first because their rules were known before any
        # new intraday trigger on this session.
        to_close: list[str] = []
        for trade_id, holding in list(holdings.items()):
            plan = holding["row"]["exit_plan"]
            for partial_index, partial in enumerate(plan.get("partial_exits") or []):
                if partial["date"] != day or partial_index in holding["processed_partials"]:
                    continue
                quantity = int(math.floor(holding["initial_shares"] * float(partial["fraction"])))
                net, cost = _sell(
                    holding=holding,
                    quantity=quantity,
                    price=float(partial["price"]),
                    trade_date=day,
                    reason=str(partial["reason"]),
                    commission_rate=commission_rate,
                    sell_tax_rate=sell_tax_rate,
                )
                cash += net
                costs_paid += cost
                holding["processed_partials"].add(partial_index)
                transactions.append({"trade_id": trade_id, "type": "PARTIAL_SELL", **holding["sales"][-1]})
            if plan["status"] == "CLOSED" and plan["exit_date"] == day:
                net, cost = _sell(
                    holding=holding,
                    quantity=int(holding["remaining_shares"]),
                    price=float(plan["exit_price"]),
                    trade_date=day,
                    reason=str(plan["exit_reason"]),
                    commission_rate=commission_rate,
                    sell_tax_rate=sell_tax_rate,
                )
                cash += net
                costs_paid += cost
                transactions.append({"trade_id": trade_id, "type": "FINAL_SELL", **holding["sales"][-1]})
                to_close.append(trade_id)
        for trade_id in to_close:
            holding = holdings.pop(trade_id)
            net_pnl = holding["net_sale_proceeds"] - holding["buy_outflow"]
            completed.append(
                {
                    **{key: holding["row"][key] for key in ("trade_id", "code", "name", "entry_date", "priority_score", "same_day_rank")},
                    "bucket": holding["bucket"],
                    "status": "CLOSED",
                    "status_label": "CLOSED（交易已結束）",
                    "initial_shares": holding["initial_shares"],
                    "entry_price": holding["entry_price"],
                    "exit_date": day,
                    "net_pnl": round(net_pnl, 4),
                    "net_return_pct": round(net_pnl / (holding["initial_shares"] * holding["entry_price"]) * 100.0, 4),
                    "costs": round(holding["costs_paid"], 4),
                    "reference_mfe_pct": holding["row"]["reference_mfe_pct"],
                }
            )

        signals = [row for row in entries_by_date.get(day, []) if row["trade_id"] not in holdings]
        signals.sort(key=lambda row: (-row["priority_score"], -row["priority_evidence"]["radar_score"], row["code"]))
        filtered: list[dict[str, Any]] = []
        open_codes = {holding["row"]["code"] for holding in holdings.values()}
        for row in signals:
            if row["code"] in open_codes:
                skipped.append({"trade_id": row["trade_id"], "code": row["code"], "entry_date": day, "reason": "DUPLICATE_OPEN（同股仍持有）", "priority_score": row["priority_score"], "same_day_rank": row["same_day_rank"], "reference_mfe_pct": row["reference_mfe_pct"]})
            else:
                filtered.append(row)

        max_positions = config.get("max_positions")
        slots = len(filtered) if max_positions is None else max(0, int(max_positions) - len(holdings))
        selected = filtered[:slots]
        for row in filtered[slots:]:
            skipped.append({"trade_id": row["trade_id"], "code": row["code"], "entry_date": day, "reason": "SKIPPED_POSITION_CAP（持股上限）", "priority_score": row["priority_score"], "same_day_rank": row["same_day_rank"], "reference_mfe_pct": row["reference_mfe_pct"]})

        sizing_equity = equity_curve[-1]["equity"] if equity_curve else current_equity(day)
        heat_used = sum(float(holding["remaining_risk_dollars"]) for holding in holdings.values())
        heat_available = max(0.0, sizing_equity * portfolio_heat_rate - heat_used)
        core_open = sum(holding["bucket"] == "CORE（核心）" for holding in holdings.values())
        desired: list[tuple[dict[str, Any], str, float]] = []
        for row in selected:
            if config["mode"] == "core_pool" and core_open < int(config["core_positions"]):
                bucket = "CORE（核心）"
                rate = float(config["core_risk_rate"])
                core_open += 1
            elif config["mode"] == "core_pool":
                bucket = "EXPLORE（探索）"
                rate = float(config["explore_risk_rate"])
            else:
                bucket = "STANDARD（標準）"
                rate = float(config["risk_rate"])
            desired.append((row, bucket, sizing_equity * rate))
        total_desired_risk = sum(item[2] for item in desired)
        heat_scale = 0.0 if total_desired_risk <= 0 else min(1.0, heat_available / total_desired_risk)

        proposals: list[dict[str, Any]] = []
        for row, bucket, desired_risk in desired:
            entry = float(row["entry_price"])
            defense = float(row["defense"])
            per_share_risk = entry - defense + entry * commission_rate + defense * (commission_rate + sell_tax_rate)
            risk_budget = desired_risk * heat_scale
            max_notional = sizing_equity * single_stock_cap_rate
            qty_by_risk = 0 if per_share_risk <= 0 else math.floor(risk_budget / per_share_risk)
            qty_by_notional = math.floor(max_notional / (entry * (1.0 + commission_rate)))
            quantity = max(0, min(qty_by_risk, qty_by_notional))
            proposals.append(
                {
                    "row": row,
                    "bucket": bucket,
                    "quantity": quantity,
                    "entry": entry,
                    "defense": defense,
                    "per_share_risk": per_share_risk,
                    "risk_budget": risk_budget,
                }
            )

        cash_available = max(0.0, cash - sizing_equity * cash_reserve_rate)
        proposed_cost = sum(item["quantity"] * item["entry"] * (1.0 + commission_rate) for item in proposals)
        cash_scale = 1.0 if proposed_cost <= cash_available or proposed_cost == 0 else cash_available / proposed_cost
        for proposal in proposals:
            row = proposal["row"]
            quantity = math.floor(proposal["quantity"] * cash_scale)
            if quantity <= 0:
                reason = "SKIPPED_HEAT（組合風險額度不足）" if heat_scale == 0 else "SKIPPED_CASH_OR_SIZE（現金或部位不足）"
                skipped.append({"trade_id": row["trade_id"], "code": row["code"], "entry_date": day, "reason": reason, "priority_score": row["priority_score"], "same_day_rank": row["same_day_rank"], "reference_mfe_pct": row["reference_mfe_pct"]})
                continue
            gross = quantity * proposal["entry"]
            commission = gross * commission_rate
            outflow = gross + commission
            if outflow > cash + 1e-6:
                raise AssertionError("buy exceeds cash")
            cash -= outflow
            costs_paid += commission
            initial_risk = quantity * proposal["per_share_risk"]
            holding = {
                "row": row,
                "bucket": proposal["bucket"],
                "initial_shares": quantity,
                "remaining_shares": quantity,
                "entry_price": proposal["entry"],
                "defense": proposal["defense"],
                "buy_outflow": outflow,
                "net_sale_proceeds": 0.0,
                "costs_paid": commission,
                "initial_risk_dollars": initial_risk,
                "remaining_risk_dollars": initial_risk,
                "sales": [],
                "processed_partials": set(),
                "close_by_date": close_maps[row["symbol"]],
                "last_mark": proposal["entry"],
            }
            holdings[row["trade_id"]] = holding
            transactions.append(
                {
                    "trade_id": row["trade_id"],
                    "type": "BUY",
                    "date": day,
                    "quantity": quantity,
                    "price": proposal["entry"],
                    "commission": round(commission, 4),
                    "risk_dollars": round(initial_risk, 4),
                    "bucket": proposal["bucket"],
                }
            )

        max_concurrent = max(max_concurrent, len(holdings))
        equity = current_equity(day)
        gross_equity = current_equity(day, net_liquidation=False)
        invested = sum(holding["remaining_shares"] * _mark_price(holding, day) for holding in holdings.values())
        heat = sum(float(holding["remaining_risk_dollars"]) for holding in holdings.values())
        equity_curve.append(
            {
                "date": day,
                "equity": round(equity, 4),
                "gross_equity": round(gross_equity, 4),
                "cash": round(cash, 4),
                "invested_market_value": round(invested, 4),
                "invested_pct": round(invested / gross_equity * 100.0, 4) if gross_equity else None,
                "open_positions": len(holdings),
                "committed_heat_pct": round(heat / gross_equity * 100.0, 4) if gross_equity else None,
            }
        )

    for holding in holdings.values():
        mark = _mark_price(holding, as_of)
        remaining_value = holding["remaining_shares"] * mark * (1.0 - commission_rate - sell_tax_rate)
        net_pnl = holding["net_sale_proceeds"] + remaining_value - holding["buy_outflow"]
        estimated_exit_cost = holding["remaining_shares"] * mark * (commission_rate + sell_tax_rate)
        completed.append(
            {
                **{key: holding["row"][key] for key in ("trade_id", "code", "name", "entry_date", "priority_score", "same_day_rank")},
                "bucket": holding["bucket"],
                "status": "OPEN",
                "status_label": "OPEN（持有中）",
                "initial_shares": holding["initial_shares"],
                "entry_price": holding["entry_price"],
                "mark_date": as_of,
                "mark_price": round(mark, 4),
                "net_pnl": round(net_pnl, 4),
                "net_return_pct": round(net_pnl / (holding["initial_shares"] * holding["entry_price"]) * 100.0, 4),
                "costs": round(holding["costs_paid"] + estimated_exit_cost, 4),
                "reference_mfe_pct": holding["row"]["reference_mfe_pct"],
            }
        )

    equities = [float(row["equity"]) for row in equity_curve]
    peak = initial_cash
    drawdowns: list[float] = []
    for equity in equities:
        peak = max(peak, equity)
        drawdowns.append((equity / peak - 1.0) * 100.0)
    pnls = [float(row["net_pnl"]) for row in completed]
    accepted_ids = {row["trade_id"] for row in completed}
    big = [row for row in universe if float(row["reference_mfe_pct"]) >= 30.0]
    final_equity = equities[-1] if equities else initial_cash
    skip_counts = dict(sorted(Counter(row["reason"] for row in skipped).items()))
    return {
        "model": model,
        "model_label": MODEL_LABELS[model],
        "summary": {
            "initial_cash": round(initial_cash, 2),
            "final_net_liquidation_equity": round(final_equity, 2),
            "total_return_pct": round((final_equity / initial_cash - 1.0) * 100.0, 4),
            "max_drawdown_pct": _round(min(drawdowns) if drawdowns else 0.0),
            "executed_trade_count": len(completed),
            "closed_count": sum(row["status"] == "CLOSED" for row in completed),
            "open_count": sum(row["status"] == "OPEN" for row in completed),
            "positive_trade_rate_pct": None if not completed else round(sum(value > 0 for value in pnls) / len(pnls) * 100.0, 2),
            "profit_factor_net_dollars": _profit_factor(pnls),
            "total_costs_including_open_liquidation_estimate": round(sum(float(row["costs"]) for row in completed), 2),
            "max_concurrent_positions": max_concurrent,
            "average_invested_pct": _mean([float(row["invested_pct"]) for row in equity_curve if row["invested_pct"] is not None]),
            "max_committed_heat_pct": _round(max((float(row["committed_heat_pct"]) for row in equity_curve if row["committed_heat_pct"] is not None), default=0.0)),
            "skipped_signal_count": len(skipped),
            "skip_reason_counts": skip_counts,
            "mfe_30_total": len(big),
            "mfe_30_captured": sum(row["trade_id"] in accepted_ids for row in big),
            "mfe_30_missed": [f"{row['code']} {row['name']}" for row in big if row["trade_id"] not in accepted_ids],
        },
        "trades": sorted(completed, key=lambda row: (row["entry_date"], row["code"])),
        "skipped": skipped,
        "transactions": transactions,
        "equity_curve": equity_curve,
    }


def build_payload(
    *,
    radar_path: Path,
    exits_path: Path,
    initial_cash: float = 500_000.0,
    per_trade_risk_rate: float = 0.005,
    portfolio_heat_rate: float = 0.03,
    single_stock_cap_rate: float = 0.20,
    cash_reserve_rate: float = 0.10,
    commission_rate: float = 0.001425,
    sell_tax_rate: float = 0.003,
) -> dict[str, Any]:
    source = json.loads(radar_path.read_text(encoding="utf-8"))
    results: dict[str, dict[str, Any]] = {}
    audits: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    for exit_variant in EXIT_VARIANTS:
        universe, frames, audit = build_universe(radar_path, exits_path, exit_variant)
        audits[exit_variant] = audit
        diagnostics[exit_variant] = ranking_diagnostics(universe, commission_rate, sell_tax_rate)
        results[exit_variant] = {
            model: simulate_portfolio(
                universe=universe,
                frames=frames,
                model=model,
                as_of=source["as_of"],
                initial_cash=initial_cash,
                per_trade_risk_rate=per_trade_risk_rate,
                portfolio_heat_rate=portfolio_heat_rate,
                single_stock_cap_rate=single_stock_cap_rate,
                cash_reserve_rate=cash_reserve_rate,
                commission_rate=commission_rate,
                sell_tax_rate=sell_tax_rate,
            )
            for model in MODEL_CONFIGS
        }
    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": source["as_of"],
        "selection_window": source["selection_window"],
        "input_paths": {"radar": str(radar_path.resolve()), "exits": str(exits_path.resolve())},
        "parameters": {
            "initial_cash": initial_cash,
            "base_per_trade_risk_rate": per_trade_risk_rate,
            "portfolio_heat_rate": portfolio_heat_rate,
            "single_stock_notional_cap_rate": single_stock_cap_rate,
            "cash_reserve_rate": cash_reserve_rate,
            "buy_and_sell_commission_rate": commission_rate,
            "sell_transaction_tax_rate": sell_tax_rate,
            "minimum_commission": "not_modeled_broker_specific",
            "slippage": "not_modeled",
            "share_unit": "one_share_integer_odd_lots_allowed",
            "entry": "original_trigger_fill",
            "sizing_equity": "previous_session_net_liquidation_equity",
            "open_position_valuation": "as_of_close_less_estimated_sell_cost",
            "same_stock_overlap": "rejected",
            "delayed_waitlist_entry": "not_allowed_without_a_new_trigger",
            "model_configs": MODEL_CONFIGS,
        },
        "audit": audits,
        "ranking_diagnostics": diagnostics,
        "results": results,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# 啟蒙層50萬元部位／容量回測｜截至 {payload['as_of']}",
        "",
        "> 研究用途，不是投資建議。使用同一組90筆雷達觸發事件；排名只用 ARMED（已建立進場計畫）當時已知資料。",
        "",
        "## 條件",
        "",
        "- 初始資金 500,000 元；單筆基準風險 0.5%；Portfolio Heat（組合初始風險）上限 3%；單檔市值上限 20%；保留現金 10%。",
        "- 買賣手續費各 0.1425%，賣出股票交易稅 0.3%；未計券商最低手續費、折扣與滑價。",
        "- 同一股票尚未出場時，不接受重疊的新交易；被容量排除的訊號不延後偷補，除非資料本身另有新觸發。",
        "- 主表採混合波段出場；另用21MA一日出場做敏感度檢查，因為持有時間會改變後續資金容量。",
        "",
    ]
    for exit_variant in EXIT_VARIANTS:
        lines += [f"## {EXIT_LABELS[exit_variant]}", "", "| 模型 | 期末淨值 | 總報酬 | 最大回撤 | 進場筆數 | 正報酬率 | PF | 最大同持 | 平均投入 | 最大Heat | 30%大波段捕捉 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for model in MODEL_CONFIGS:
            row = payload["results"][exit_variant][model]["summary"]
            lines.append(
                f"| {MODEL_LABELS[model]} | {row['final_net_liquidation_equity']:,.0f} | {_f(row['total_return_pct'])}% | {_f(row['max_drawdown_pct'])}% | "
                f"{row['executed_trade_count']} | {_f(row['positive_trade_rate_pct'])}% | {_f(row['profit_factor_net_dollars'])} | {row['max_concurrent_positions']} | "
                f"{_f(row['average_invested_pct'])}% | {_f(row['max_committed_heat_pct'])}% | {row['mfe_30_captured']}/{row['mfe_30_total']} |"
            )
        lines.append("")

    lines += [
        "## 排名本身有沒有用",
        "",
        "同日排名只比較當天一起觸發的股票，避免拿不同市場日期硬比。以下為扣標準手續費與賣出稅後的單筆結果；尚未做樣本外驗證。",
        "",
    ]
    for exit_variant in EXIT_VARIANTS:
        diag = payload["ranking_diagnostics"][exit_variant]
        lines += [f"### {EXIT_LABELS[exit_variant]}", "", "| 同日名次 | 筆數 | 平均淨報酬 | 中位淨報酬 | 正報酬率 | PF | MFE≥30% |", "|---|---:|---:|---:|---:|---:|---:|"]
        for key, label in (("RANK_1_3", "1～3"), ("RANK_4_5", "4～5"), ("RANK_6_8", "6～8"), ("RANK_9_PLUS", "9以後")):
            row = diag["same_day_rank_tiers"][key]
            lines.append(f"| {label} | {row['trade_count']} | {_f(row['mean_net_return_pct'])}% | {_f(row['median_net_return_pct'])}% | {_f(row['positive_rate_pct'])}% | {_f(row['profit_factor'])} | {row['mfe_30_count']} |")
        lines += ["", f"優先分與事後淨報酬的 Spearman 相關係數：{_f(diag['priority_score_spearman_vs_net_return'], 4)}。", ""]

    primary = payload["results"]["HYBRID_RUNNER"]
    lines += ["## 各模型漏掉的大波段", ""]
    for model in MODEL_CONFIGS:
        summary = primary[model]["summary"]
        missed = "、".join(summary["mfe_30_missed"]) or "無"
        lines.append(f"- {MODEL_LABELS[model]}：捕捉 {summary['mfe_30_captured']}/{summary['mfe_30_total']}；漏掉：{missed}。")
    lines += [
        "",
        "## 限制",
        "",
        "- 歷史只涵蓋本機實際保留的雷達資料，期間短，且只有90筆觸發，不足以證明排序具穩定預測力。",
        "- 排名沿用課程整合優先分，但大盤分數在同一天對所有股票相同，因此以中性常數移除；不影響同日先後順序。",
        "- 此輪只比較候選覆蓋率與部位容量，所有模型仍為一次建立目標部位；尚未加入順勢加碼，避免同時改變篩選、部位與進場三件事。",
        "- 未模擬漲跌停成交失敗、委託簿衝擊、除權息現金流與券商最低手續費。",
        "",
        "## 稽核",
        "",
        f"- 方法版本：`{payload['method_version']}`",
        f"- 參數：`{json.dumps(payload['parameters'], ensure_ascii=False, sort_keys=True)}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--radar-input",
        type=Path,
        default=ROOT / "reports" / "course_backtest" / "2026-09-04" / "radar_full_history_v2" / "backtest.json",
    )
    parser.add_argument(
        "--exit-input",
        type=Path,
        default=ROOT / "reports" / "course_backtest" / "2026-09-04" / "exit_variants_v1" / "comparison.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "reports" / "course_backtest" / "2026-09-04" / "position_variants_v1",
    )
    args = parser.parse_args()
    payload = build_payload(radar_path=args.radar_input, exits_path=args.exit_input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "comparison.json"
    markdown_path = args.output_dir / "comparison.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    print(markdown_path.resolve())
    for exit_variant in EXIT_VARIANTS:
        print(exit_variant)
        for model in MODEL_CONFIGS:
            print(model, json.dumps(payload["results"][exit_variant][model]["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
