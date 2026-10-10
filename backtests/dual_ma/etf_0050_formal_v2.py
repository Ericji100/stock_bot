"""Cash-safe 0050 reports and a continuous TWD 500,000 comparison.

This module is deliberately versioned instead of mutating ``base-v1``.  It
keeps the adopted signal/exit decisions, resizes every entry to cash actually
available, applies corporate actions in causal order, and publishes one
canonical manifest/readback pair.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import statistics
import subprocess
import tempfile
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Mapping, Sequence

from .costs import CATHAY_WEB_028_TW_V1, size_entry
from .etf_0050_base_v1 import (
    MAIN_SCENARIO_BPS,
    PACKAGE_ROOT_V3,
    SLIPPAGE_SCENARIOS_BPS,
    STRATEGY_SPECS,
    YEARS,
    ETF0050Error,
    StrategyRunSlice,
    run_five_strategy_interfaces,
    tree_hash,
)
from .etf_0050_formal_v1 import (
    END,
    START,
    PackageData,
    _canonical_bytes,
    _jsonable,
    _sha_bytes,
    build_manifest_factory,
    load_package,
)
from .fugle_source import sha256_file
from .h2_ma144_upward_entry_2022h2 import build_ma144_gate_map
from .models import InstrumentType
from .paths import report_path


ANNUAL_CAPITAL = Decimal("10000")
CONTINUOUS_CAPITAL = Decimal("500000")
OUTPUT_ROOT_DEFAULT = report_path("etf", "0050", "base-v2.2")
BASE_V1_ROOT = report_path("etf", "0050", "base-v1")
STOCK_ACCOUNT_ROOT = report_path("analysis", "shared-account-position-sizing-v2")


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _money(value: Any) -> str:
    return "—" if value is None else f"{_d(value):,.2f}"


def _pct(value: Any) -> str:
    return "—" if value is None else f"{_d(value) * 100:.2f}%"


def geometric_annual_return(annual_returns: Sequence[Decimal]) -> Decimal:
    """Geometric mean for independent annual cohorts."""

    if not annual_returns:
        return Decimal("0")
    product = Decimal("1")
    for value in annual_returns:
        product *= Decimal("1") + value
    if product <= 0:
        return Decimal("-1")
    return Decimal(str(float(product) ** (1 / len(annual_returns)) - 1))


def annualized_return(starting: Decimal, ending: Decimal, start: date, end: date) -> Decimal:
    if starting <= 0 or ending <= 0:
        return Decimal("-1")
    days = max((end - start).days + 1, 1)
    return Decimal(str(float(ending / starting) ** (365 / days) - 1))


def _entry_stop_in_raw_coordinate(row: Mapping[str, Any]) -> Decimal:
    if row.get("initial_stop_raw_override") not in (None, ""):
        value = _d(row["initial_stop_raw_override"])
        if value <= 0:
            raise ETF0050Error("entry raw stop override is not positive")
        return value
    signal_entry = _d(row["entry_reference_close"])
    if signal_entry <= 0:
        raise ETF0050Error("entry signal coordinate is not positive")
    return _d(row["reference_price"]) * _d(row["initial_stop"]) / signal_entry


def _execution_trace(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in ("signal_date", "fill_timing", "signal_reference_price")
        if row.get(key) not in (None, "")
    }


def _mfe_mae(
    *, package: PackageData, record: Mapping[str, Any], end: date, bps: int,
    research_slippage: bool = False,
) -> tuple[Decimal, Decimal]:
    model = (
        CATHAY_WEB_028_TW_V1.with_research_slippage(bps)
        if research_slippage
        else CATHAY_WEB_028_TW_V1.with_slippage(bps)
    )
    entry_day = record["entry_date"]
    finish = record.get("exit_date") or end
    payments = record["dividend_payments"]
    values: list[Decimal] = []
    for day in package.sessions:
        if day < entry_day or day > finish:
            continue
        shares = int(record["entry_shares"])
        if entry_day < package.split.effective_date <= day:
            shares = int(Decimal(shares) * package.split.ratio)
        paid = sum(
            (_d(item["cash"]) for item in payments if item["payment_date"] <= day),
            Decimal("0"),
        )
        sale = model.sell(package.raw_by_date[day].close, shares, InstrumentType.ETF)
        values.append((sale.cash_amount + paid - _d(record["buy_total"])) / _d(record["buy_total"]))
    return (max(values), min(values)) if values else (Decimal("0"), Decimal("0"))


def validate_cash_safe_metric_semantics(metrics: Mapping[str, Any]) -> None:
    """Fail closed on final holdings and actual-versus-estimated cost semantics."""

    open_rounds = int(metrics["open_rounds"])
    final_shares = int(metrics.get("final_shares") or 0)
    if (open_rounds > 0 and final_shares <= 0) or (open_rounds == 0 and final_shares != 0):
        raise ETF0050Error(
            "final_shares must be positive exactly when a cutoff position remains open"
        )
    pairs = (
        ("fees_and_tax", "actual_fees_and_tax"),
        ("slippage_cost", "actual_slippage_cost"),
    )
    for legacy, explicit in pairs:
        if _d(metrics[legacy]) != _d(metrics[explicit]):
            raise ETF0050Error(f"{legacy} must retain the actual-incurred-only basis")
    for field in (
        "actual_fees_and_tax",
        "actual_slippage_cost",
        "cutoff_estimated_sale_fees_and_tax",
        "cutoff_estimated_sale_slippage_cost",
    ):
        if _d(metrics[field]) < 0:
            raise ETF0050Error(f"negative cost component: {field}")


def replay_cash_safe_strategy(
    *,
    package: PackageData,
    run: StrategyRunSlice,
    start: date,
    end: date,
    slippage_bps: int,
    initial_capital: Decimal,
    budget_mode: str,
    research_slippage: bool = False,
) -> dict[str, Any]:
    """Replay frozen decisions with available-cash sizing and no borrowing."""

    model = (
        CATHAY_WEB_028_TW_V1.with_research_slippage(slippage_bps)
        if research_slippage
        else CATHAY_WEB_028_TW_V1.with_slippage(slippage_bps)
    )
    sessions = [day for day in package.sessions if start <= day <= end]
    if not sessions:
        raise ETF0050Error("cash-safe replay has no official sessions")
    start, end = sessions[0], sessions[-1]
    tx_by_day: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for row in run.transactions:
        if start <= row["date"] <= end:
            tx_by_day[row["date"]].append(row)
    ex_by_day = {event.ex_date: event for event in package.dividends if start <= event.ex_date <= end}
    pay_by_day: dict[date, list[Any]] = defaultdict(list)
    for event in package.dividends:
        if start <= event.payment_date <= end:
            pay_by_day[event.payment_date].append(event)

    cash = initial_capital
    min_cash = cash
    active_leg: str | None = None
    records: dict[str, dict[str, Any]] = {}
    rights: dict[date, list[dict[str, Any]]] = defaultdict(list)
    transactions: list[dict[str, Any]] = []
    dividends: list[dict[str, Any]] = []
    splits: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    cumulative_buy = Decimal("0")
    peak_invested = Decimal("0")
    peak_invested_date: date | None = None
    peak_equity = initial_capital
    max_drawdown = Decimal("0")

    for day in sessions:
        if day == package.split.effective_date:
            before = 0 if active_leg is None else int(records[active_leg]["shares"])
            if active_leg is not None:
                records[active_leg]["shares"] = int(
                    Decimal(records[active_leg]["shares"]) * package.split.ratio
                )
            after = 0 if active_leg is None else int(records[active_leg]["shares"])
            splits.append(
                {
                    "date": day,
                    "type": "SPLIT_REBASE_ONLY",
                    "ratio": package.split.ratio,
                    "shares_before": before,
                    "shares_after": after,
                    "leg_id": active_leg,
                    "transaction": False,
                    "pnl": Decimal("0"),
                }
            )

        event = ex_by_day.get(day)
        if event is not None:
            shares = 0 if active_leg is None else int(records[active_leg]["shares"])
            amount = Decimal(shares) * event.amount_per_share
            right = {
                "ex_date": event.ex_date,
                "payment_date": event.payment_date,
                "shares": shares,
                "leg_id": active_leg,
                "amount_per_share": event.amount_per_share,
                "cash": amount,
            }
            rights[event.payment_date].append(right)
            dividends.append({**right, "stage": "ENTITLEMENT_LOCKED"})

        for event in pay_by_day.get(day, []):
            candidates = [item for item in rights.get(day, []) if item["ex_date"] == event.ex_date]
            if candidates:
                right = candidates[0]
            else:
                right = {
                    "ex_date": event.ex_date,
                    "payment_date": event.payment_date,
                    "shares": 0,
                    "leg_id": None,
                    "amount_per_share": event.amount_per_share,
                    "cash": Decimal("0"),
                }
            cash += _d(right["cash"])
            if right["leg_id"] is not None:
                records[str(right["leg_id"])]["dividend_payments"].append(dict(right))
            dividends.append({**right, "stage": "CASH_PAID"})

        ordered = sorted(tx_by_day.get(day, []), key=lambda row: 0 if row["role"] == "EXIT" else 1)
        for frozen in ordered:
            leg_id = str(frozen["leg_id"])
            if frozen["role"] == "EXIT":
                if active_leg != leg_id:
                    skipped.append(
                        {
                            "date": day,
                            "leg_id": leg_id,
                            "reason": "對應買進因資金不足而略過，故不執行賣出",
                        }
                    )
                    continue
                record = records[leg_id]
                shares = int(record["shares"])
                order = model.sell(_d(frozen["reference_price"]), shares, InstrumentType.ETF)
                cash += order.cash_amount
                record.update(
                    {
                        "exit_date": day,
                        "exit_reason": frozen.get("exit_reason"),
                        "exit_reference_price": order.reference_price,
                        "exit_fill_price": order.fill_price,
                        "sell_gross": order.gross,
                        "sell_commission": order.commission,
                        "sell_tax": order.tax,
                        "sell_net": order.cash_amount,
                        "final_shares": shares,
                    }
                )
                transactions.append(
                    {
                        "date": day,
                        "role": "EXIT",
                        "status": "FILLED",
                        "leg_id": leg_id,
                        "reference_price": order.reference_price,
                        "fill_price": order.fill_price,
                        "shares": shares,
                        "gross": order.gross,
                        "commission": order.commission,
                        "tax": order.tax,
                        "cash_amount": order.cash_amount,
                        "exit_reason": frozen.get("exit_reason"),
                        **_execution_trace(frozen),
                    }
                )
                active_leg = None
            elif frozen.get("status") == "FILLED":
                if active_leg is not None:
                    raise ETF0050Error("frozen ETF decisions opened a second position")
                budget = min(ANNUAL_CAPITAL, cash) if budget_mode == "fixed_10000" else cash
                sized = size_entry(
                    _d(frozen["reference_price"]),
                    _entry_stop_in_raw_coordinate(frozen),
                    InstrumentType.ETF,
                    cost_model=model,
                    max_total_cost=budget,
                    max_initial_r=budget * Decimal("0.20"),
                    max_shares=1_000_000,
                )
                if sized is None:
                    skipped.append(
                        {
                            "date": day,
                            "leg_id": leg_id,
                            "reason": "可用現金不足以買進至少一股，或風險上限不允許",
                            "available_cash": cash,
                            "target_budget": budget,
                        }
                    )
                    continue
                cash -= sized.buy.cash_amount
                cumulative_buy += sized.buy.cash_amount
                if sized.buy.cash_amount > peak_invested:
                    peak_invested = sized.buy.cash_amount
                    peak_invested_date = day
                records[leg_id] = {
                    "leg_id": leg_id,
                    "entry_date": day,
                    "entry_reference_price": sized.buy.reference_price,
                    "entry_fill_price": sized.buy.fill_price,
                    "entry_shares": sized.shares,
                    "shares": sized.shares,
                    "buy_gross": sized.buy.gross,
                    "buy_commission": sized.buy.commission,
                    "buy_total": sized.buy.cash_amount,
                    "initial_stop_raw": sized.estimated_stop_sale.reference_price,
                    "initial_r": sized.initial_r,
                    "dividend_payments": [],
                    "exit_date": None,
                }
                transactions.append(
                    {
                        "date": day,
                        "role": "MOTHER",
                        "status": "FILLED",
                        "leg_id": leg_id,
                        "reference_price": sized.buy.reference_price,
                        "fill_price": sized.buy.fill_price,
                        "shares": sized.shares,
                        "gross": sized.buy.gross,
                        "commission": sized.buy.commission,
                        "tax": sized.buy.tax,
                        "cash_amount": sized.buy.cash_amount,
                        "initial_stop_raw": sized.estimated_stop_sale.reference_price,
                        "initial_r": sized.initial_r,
                        **_execution_trace(frozen),
                    }
                )
                active_leg = leg_id
        min_cash = min(min_cash, cash)
        if cash < 0:
            raise ETF0050Error(f"cash became negative on {day}: {cash}")
        shares = 0 if active_leg is None else int(records[active_leg]["shares"])
        liquidation = (
            model.sell(package.raw_by_date[day].close, shares, InstrumentType.ETF).cash_amount
            if shares
            else Decimal("0")
        )
        equity = cash + liquidation
        peak_equity = max(peak_equity, equity)
        drawdown = equity / peak_equity - 1 if peak_equity else Decimal("0")
        max_drawdown = min(max_drawdown, drawdown)
        curve.append(
            {
                "date": day,
                "cash": cash,
                "shares": shares,
                "raw_close": package.raw_by_date[day].close,
                "liquidation_value": liquidation,
                "equity": equity,
                "drawdown": drawdown,
                "active_leg_id": active_leg,
            }
        )

    details: list[dict[str, Any]] = []
    realized = Decimal("0")
    cutoff_estimate = Decimal("0")
    returns: list[Decimal] = []
    mfes: list[Decimal] = []
    maes: list[Decimal] = []
    closed_returns: list[Decimal] = []
    for record in records.values():
        dividends_received = sum(
            (_d(item["cash"]) for item in record["dividend_payments"]), Decimal("0")
        )
        mfe, mae = _mfe_mae(
            package=package, record=record, end=end, bps=slippage_bps,
            research_slippage=research_slippage,
        )
        if record.get("exit_date") is not None:
            pnl = _d(record["sell_net"]) + dividends_received - _d(record["buy_total"])
            realized += pnl
            status = "已賣出"
            exit_reference = record["exit_reference_price"]
            exit_fill = record["exit_fill_price"]
            final_shares = int(record["final_shares"])
            closed_returns.append(pnl / _d(record["buy_total"]))
        else:
            final_shares = int(record["shares"])
            mark = model.sell(package.raw_by_date[end].close, final_shares, InstrumentType.ETF)
            pnl = mark.cash_amount + dividends_received - _d(record["buy_total"])
            cutoff_estimate += pnl
            status = "截至截止日仍持有（按收盤價估算）"
            exit_reference = mark.reference_price
            exit_fill = mark.fill_price
            record["sell_commission"] = mark.commission
            record["sell_tax"] = mark.tax
        rate = pnl / _d(record["buy_total"])
        returns.append(rate)
        mfes.append(mfe)
        maes.append(mae)
        details.append(
            {
                "strategy_id": run.spec.strategy_id,
                "strategy_label": run.spec.label,
                "leg_id": record["leg_id"],
                "status_zh": status,
                "entry_date": record["entry_date"],
                "entry_raw_price": record["entry_reference_price"],
                "entry_fill_price": record["entry_fill_price"],
                "entry_shares": record["entry_shares"],
                "final_shares": final_shares,
                "buy_gross": record["buy_gross"],
                "buy_commission": record["buy_commission"],
                "buy_total": record["buy_total"],
                "exit_date": record.get("exit_date"),
                "exit_reason": record.get("exit_reason"),
                "exit_raw_or_cutoff_price": exit_reference,
                "exit_fill_or_mark_price": exit_fill,
                "sell_commission": record.get("sell_commission", Decimal("0")),
                "sell_tax": record.get("sell_tax", Decimal("0")),
                "cash_dividends": dividends_received,
                "net_pnl_or_estimate": pnl,
                "return_rate_or_estimate": rate,
                "mfe": mfe,
                "mae": mae,
            }
        )

    ending = curve[-1]["equity"]
    combined = ending - initial_capital
    details_total = sum((_d(item["net_pnl_or_estimate"]) for item in details), Decimal("0"))
    if abs(details_total - combined) > Decimal("0.000001"):
        raise ETF0050Error(
            f"detail/account reconciliation failed: {details_total} != {combined}"
        )
    fees_tax = sum(
        (_d(row.get("commission", 0)) + _d(row.get("tax", 0)) for row in transactions),
        Decimal("0"),
    )
    slippage = sum(
        (
            abs(_d(row["fill_price"]) - _d(row["reference_price"]))
            * int(row["shares"])
            for row in transactions
        ),
        Decimal("0"),
    )
    cutoff_estimated_fees_tax = sum(
        (
            _d(row["sell_commission"]) + _d(row["sell_tax"])
            for row in details
            if row["status_zh"] != "已賣出"
        ),
        Decimal("0"),
    )
    cutoff_estimated_slippage = sum(
        (
            abs(_d(row["exit_fill_or_mark_price"]) - _d(row["exit_raw_or_cutoff_price"]))
            * int(row["final_shares"])
            for row in details
            if row["status_zh"] != "已賣出"
        ),
        Decimal("0"),
    )
    idle = Decimal(
        str(
            statistics.fmean(
                float(max(_d(row["cash"]), Decimal("0")) / _d(row["equity"]))
                if _d(row["equity"]) > 0
                else 0
                for row in curve
            )
        )
    )
    metrics = {
        "strategy_id": run.spec.strategy_id,
        "strategy_label": run.spec.label,
        "slippage_bps": slippage_bps,
        "external_principal": initial_capital,
        "additional_top_up_or_borrowing": Decimal("0"),
        "rounds": len(details),
        "closed_rounds": len(closed_returns),
        "open_rounds": len(details) - len(closed_returns),
        "win_rate": Decimal(sum(value > 0 for value in closed_returns)) / len(closed_returns)
        if closed_returns
        else None,
        "cumulative_buy": cumulative_buy,
        "peak_concurrent_investment": peak_invested,
        "peak_investment_date": peak_invested_date,
        "realized_net_pnl": realized,
        "cutoff_estimated_pnl": cutoff_estimate,
        "combined_pnl": combined,
        "profit_per_100_invested": combined / cumulative_buy * 100 if cumulative_buy else None,
        "ending_equity": ending,
        "total_return": ending / initial_capital - 1,
        "cagr": annualized_return(initial_capital, ending, start, end),
        "max_asset_drawdown": max_drawdown,
        "idle_cash_ratio": idle,
        "average_return": sum(returns, Decimal("0")) / len(returns) if returns else None,
        "median_return": Decimal(str(statistics.median(returns))) if returns else None,
        "max_trade_win": max(returns) if returns else None,
        "max_trade_loss": min(returns) if returns else None,
        "average_mfe": sum(mfes, Decimal("0")) / len(mfes) if mfes else None,
        "average_mae": sum(maes, Decimal("0")) / len(maes) if maes else None,
        "fees_and_tax": fees_tax,
        "slippage_cost": slippage,
        "actual_fees_and_tax": fees_tax,
        "actual_slippage_cost": slippage,
        "cutoff_estimated_sale_fees_and_tax": cutoff_estimated_fees_tax,
        "cutoff_estimated_sale_slippage_cost": cutoff_estimated_slippage,
        "transaction_count": len(transactions),
        "skipped_for_cash": sum("現金不足" in row["reason"] for row in skipped),
        "minimum_cash": min_cash,
        "dividends_received": sum(
            (_d(row["cash"]) for row in dividends if row["stage"] == "CASH_PAID"),
            Decimal("0"),
        ),
        "start_date": start,
        "end_date": end,
        "final_shares": int(curve[-1]["shares"]),
    }
    validate_cash_safe_metric_semantics(metrics)
    return {
        "metrics": metrics,
        "details": details,
        "transactions": transactions,
        "dividends": dividends,
        "splits": splits,
        "curve": curve,
        "skipped": skipped,
    }


def simulate_buy_hold_cash_safe(
    *,
    package: PackageData,
    start: date,
    end: date,
    slippage_bps: int,
    initial_capital: Decimal,
    reinvest: bool,
) -> dict[str, Any]:
    model = CATHAY_WEB_028_TW_V1.with_slippage(slippage_bps)
    sessions = [day for day in package.sessions if start <= day <= end]
    if not sessions:
        raise ETF0050Error("buy-and-hold has no official sessions")
    start, end = sessions[0], sessions[-1]
    cash = initial_capital
    first_price = package.raw_by_date[start].close
    shares = int((cash / model._fill_price(first_price, "BUY")).to_integral_value(rounding=ROUND_FLOOR))
    while shares and model.buy(first_price, shares).cash_amount > cash:
        shares -= 1
    if not shares:
        raise ETF0050Error("initial capital cannot buy one ETF share")
    first = model.buy(first_price, shares)
    cash -= first.cash_amount
    cumulative_buy = first.cash_amount
    transactions = [
        {
            "date": start,
            "role": "首次買進",
            "reference_price": first.reference_price,
            "fill_price": first.fill_price,
            "shares": shares,
            "gross": first.gross,
            "commission": first.commission,
            "tax": first.tax,
            "cash_amount": first.cash_amount,
        }
    ]
    rights: dict[date, dict[str, Any]] = {}
    dividends: list[dict[str, Any]] = []
    splits: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    peak_equity = initial_capital
    max_drawdown = Decimal("0")
    total_dividends = Decimal("0")
    for day in sessions:
        if day == package.split.effective_date:
            before = shares
            shares = int(Decimal(shares) * package.split.ratio)
            splits.append(
                {
                    "date": day,
                    "type": "SPLIT_REBASE_ONLY",
                    "ratio": package.split.ratio,
                    "shares_before": before,
                    "shares_after": shares,
                    "transaction": False,
                    "pnl": Decimal("0"),
                }
            )
        for event in package.dividends:
            if event.ex_date == day:
                right = {
                    "ex_date": event.ex_date,
                    "payment_date": event.payment_date,
                    "shares": shares,
                    "amount_per_share": event.amount_per_share,
                    "cash": Decimal(shares) * event.amount_per_share,
                }
                rights[event.payment_date] = right
                dividends.append({**right, "stage": "ENTITLEMENT_LOCKED"})
            if event.payment_date == day:
                right = rights.get(
                    day,
                    {
                        "ex_date": event.ex_date,
                        "payment_date": day,
                        "shares": 0,
                        "amount_per_share": event.amount_per_share,
                        "cash": Decimal("0"),
                    },
                )
                cash += _d(right["cash"])
                total_dividends += _d(right["cash"])
                dividends.append({**right, "stage": "CASH_PAID"})
                if reinvest and _d(right["cash"]) > 0:
                    price = package.raw_by_date[day].close
                    extra = int((cash / model._fill_price(price, "BUY")).to_integral_value(rounding=ROUND_FLOOR))
                    while extra and model.buy(price, extra).cash_amount > cash:
                        extra -= 1
                    if extra:
                        order = model.buy(price, extra)
                        cash -= order.cash_amount
                        shares += extra
                        cumulative_buy += order.cash_amount
                        transactions.append(
                            {
                                "date": day,
                                "role": "股利發放日再投入",
                                "reference_price": order.reference_price,
                                "fill_price": order.fill_price,
                                "shares": extra,
                                "gross": order.gross,
                                "commission": order.commission,
                                "tax": order.tax,
                                "cash_amount": order.cash_amount,
                            }
                        )
        if cash < 0:
            raise ETF0050Error("buy-and-hold cash became negative")
        mark = model.sell(package.raw_by_date[day].close, shares, InstrumentType.ETF)
        equity = cash + mark.cash_amount
        peak_equity = max(peak_equity, equity)
        drawdown = equity / peak_equity - 1
        max_drawdown = min(max_drawdown, drawdown)
        curve.append(
            {
                "date": day,
                "cash": cash,
                "shares": shares,
                "raw_close": package.raw_by_date[day].close,
                "liquidation_value": mark.cash_amount,
                "equity": equity,
                "drawdown": drawdown,
            }
        )
    final_mark = model.sell(package.raw_by_date[end].close, shares, InstrumentType.ETF)
    ending = cash + final_mark.cash_amount
    combined = ending - initial_capital
    returns = [(_d(row["equity"]) / initial_capital - 1) for row in curve]
    label = "0050 全程持有（股利再投入）" if reinvest else "0050 全程持有（股利留現金）"
    identifier = "BUY_HOLD_REINVEST" if reinvest else "BUY_HOLD_CASH"
    actual_fees = sum(
        (_d(row["commission"]) + _d(row.get("tax", 0)) for row in transactions),
        Decimal("0"),
    )
    actual_slippage = sum(
        (abs(_d(row["fill_price"]) - _d(row["reference_price"])) * int(row["shares"]) for row in transactions),
        Decimal("0"),
    )
    cutoff_estimated_fees = final_mark.commission + final_mark.tax
    cutoff_estimated_slippage = abs(final_mark.fill_price - final_mark.reference_price) * shares
    trade_return = combined / cumulative_buy
    metrics = {
        "strategy_id": identifier,
        "strategy_label": label,
        "slippage_bps": slippage_bps,
        "external_principal": initial_capital,
        "additional_top_up_or_borrowing": Decimal("0"),
        "rounds": 1,
        "closed_rounds": 0,
        "open_rounds": 1,
        "win_rate": None,
        "cumulative_buy": cumulative_buy,
        "peak_concurrent_investment": cumulative_buy,
        "peak_investment_date": transactions[-1]["date"],
        "realized_net_pnl": Decimal("0"),
        "cutoff_estimated_pnl": combined,
        "combined_pnl": combined,
        "profit_per_100_invested": combined / cumulative_buy * 100,
        "ending_equity": ending,
        "total_return": ending / initial_capital - 1,
        "cagr": annualized_return(initial_capital, ending, start, end),
        "max_asset_drawdown": max_drawdown,
        "idle_cash_ratio": Decimal(
            str(statistics.fmean(float(_d(row["cash"]) / _d(row["equity"])) for row in curve))
        ),
        "average_return": trade_return,
        "median_return": trade_return,
        "max_trade_win": trade_return,
        "max_trade_loss": trade_return,
        "average_mfe": max(returns),
        "average_mae": min(returns),
        "fees_and_tax": actual_fees,
        "slippage_cost": actual_slippage,
        "actual_fees_and_tax": actual_fees,
        "actual_slippage_cost": actual_slippage,
        "cutoff_estimated_sale_fees_and_tax": cutoff_estimated_fees,
        "cutoff_estimated_sale_slippage_cost": cutoff_estimated_slippage,
        "transaction_count": len(transactions),
        "skipped_for_cash": 0,
        "minimum_cash": min(_d(row["cash"]) for row in curve),
        "dividends_received": total_dividends,
        "final_shares": shares,
        "start_date": start,
        "end_date": end,
    }
    detail = {
        "strategy_id": identifier,
        "strategy_label": label,
        "leg_id": f"{identifier}-{start.year}-{end.year}",
        "status_zh": "截至截止日仍持有（按收盤價估算）",
        "entry_date": start,
        "entry_raw_price": first.reference_price,
        "entry_fill_price": first.fill_price,
        "entry_shares": transactions[0]["shares"],
        "final_shares": shares,
        "buy_gross": first.gross,
        "buy_commission": first.commission,
        "buy_total": cumulative_buy,
        "exit_date": None,
        "exit_reason": None,
        "exit_raw_or_cutoff_price": final_mark.reference_price,
        "exit_fill_or_mark_price": final_mark.fill_price,
        "sell_commission": final_mark.commission,
        "sell_tax": final_mark.tax,
        "cash_dividends": total_dividends,
        "net_pnl_or_estimate": combined,
        "return_rate_or_estimate": trade_return,
        "mfe": max(returns),
        "mae": min(returns),
    }
    validate_cash_safe_metric_semantics(metrics)
    return {
        "metrics": metrics,
        "details": [detail],
        "transactions": transactions,
        "dividends": dividends,
        "splits": splits,
        "curve": curve,
        "skipped": [],
    }


def aggregate_independent_years(
    metrics: Sequence[Mapping[str, Any]], details: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    by_strategy: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in metrics:
        if int(row["slippage_bps"]) == MAIN_SCENARIO_BPS:
            by_strategy[str(row["strategy_id"])].append(row)
    details_by_strategy: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in details:
        if int(row["slippage_bps"]) == MAIN_SCENARIO_BPS:
            details_by_strategy[str(row["strategy_id"])].append(row)
    result: list[dict[str, Any]] = []
    for strategy_id, rows in by_strategy.items():
        trade_rows = details_by_strategy[strategy_id]
        trade_returns = [_d(row["return_rate_or_estimate"]) for row in trade_rows]
        closed = [row for row in trade_rows if row["status_zh"] == "已賣出"]
        external = sum((_d(row["external_principal"]) for row in rows), Decimal("0"))
        ending = sum((_d(row["ending_equity"]) for row in rows), Decimal("0"))
        combined = ending - external
        result.append(
            {
                "strategy_id": strategy_id,
                "strategy_label": rows[0]["strategy_label"],
                "external_principal": external,
                "additional_top_up_or_borrowing": Decimal("0"),
                "rounds": len(trade_rows),
                "closed_rounds": len(closed),
                "open_rounds": len(trade_rows) - len(closed),
                "win_rate": Decimal(sum(_d(row["return_rate_or_estimate"]) > 0 for row in closed)) / len(closed)
                if closed
                else None,
                "cumulative_buy": sum((_d(row["cumulative_buy"]) for row in rows), Decimal("0")),
                "peak_concurrent_investment": max(_d(row["peak_concurrent_investment"]) for row in rows),
                "peak_investment_date": max(rows, key=lambda row: _d(row["peak_concurrent_investment"]))["peak_investment_date"],
                "realized_net_pnl": sum((_d(row["realized_net_pnl"]) for row in rows), Decimal("0")),
                "cutoff_estimated_pnl": sum((_d(row["cutoff_estimated_pnl"]) for row in rows), Decimal("0")),
                "combined_pnl": combined,
                "profit_per_100_invested": combined / sum((_d(row["cumulative_buy"]) for row in rows), Decimal("0")) * 100
                if sum((_d(row["cumulative_buy"]) for row in rows), Decimal("0"))
                else None,
                "ending_equity": ending,
                "total_return": ending / external - 1,
                "cagr": geometric_annual_return([_d(row["total_return"]) for row in sorted(rows, key=lambda x: x["year"])]),
                "max_asset_drawdown": min(_d(row["max_asset_drawdown"]) for row in rows),
                "idle_cash_ratio": sum((_d(row["idle_cash_ratio"]) for row in rows), Decimal("0")) / len(rows),
                "average_return": sum(trade_returns, Decimal("0")) / len(trade_returns) if trade_returns else None,
                "median_return": Decimal(str(statistics.median(trade_returns))) if trade_returns else None,
                "max_trade_win": max(trade_returns) if trade_returns else None,
                "max_trade_loss": min(trade_returns) if trade_returns else None,
                "average_mfe": sum((_d(row["mfe"]) for row in trade_rows), Decimal("0")) / len(trade_rows)
                if trade_rows
                else None,
                "average_mae": sum((_d(row["mae"]) for row in trade_rows), Decimal("0")) / len(trade_rows)
                if trade_rows
                else None,
                "fees_and_tax": sum((_d(row["fees_and_tax"]) for row in rows), Decimal("0")),
                "slippage_cost": sum((_d(row["slippage_cost"]) for row in rows), Decimal("0")),
                "actual_fees_and_tax": sum(
                    (_d(row.get("actual_fees_and_tax", row["fees_and_tax"])) for row in rows),
                    Decimal("0"),
                ),
                "actual_slippage_cost": sum(
                    (_d(row.get("actual_slippage_cost", row["slippage_cost"])) for row in rows),
                    Decimal("0"),
                ),
                "cutoff_estimated_sale_fees_and_tax": sum(
                    (_d(row.get("cutoff_estimated_sale_fees_and_tax", 0)) for row in rows),
                    Decimal("0"),
                ),
                "cutoff_estimated_sale_slippage_cost": sum(
                    (_d(row.get("cutoff_estimated_sale_slippage_cost", 0)) for row in rows),
                    Decimal("0"),
                ),
                "transaction_count": sum(int(row["transaction_count"]) for row in rows),
                "skipped_for_cash": sum(int(row["skipped_for_cash"]) for row in rows),
                "minimum_cash": min(_d(row["minimum_cash"]) for row in rows),
                "dividends_received": sum((_d(row["dividends_received"]) for row in rows), Decimal("0")),
            }
        )
    return sorted(result, key=lambda row: row["strategy_id"])


def _capital_table(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| 方法 | 外部本金 | 額外補款／借款 | 買入周轉總額 | 單一時點最高持倉成本（含獲利再投入） | 截止總資產 | 合計損益 | 總報酬 | 年化報酬 | 最大回落 | 閒置現金 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['strategy_label']} | {_money(row['external_principal'])} | {_money(row['additional_top_up_or_borrowing'])} | {_money(row['cumulative_buy'])} | {_money(row['peak_concurrent_investment'])} | {_money(row['ending_equity'])} | {_money(row['combined_pnl'])} | {_pct(row['total_return'])} | {_pct(row['cagr'])} | {_pct(row['max_asset_drawdown'])} | {_pct(row['idle_cash_ratio'])} |"
        )
    return "\n".join(lines)


def _trading_table(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| 方法 | 總進場 | 完成買賣 | 截止仍持有 | 勝率 | 平均／中位單筆 | 每100元買入周轉的損益 | 股利收入 | 實際已發生費稅與價差 | 截止估值預留賣出費稅與價差 |",
        "|---|---:|---:|---:|---:|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        actual_cost = _d(row.get("actual_fees_and_tax", row["fees_and_tax"])) + _d(
            row.get("actual_slippage_cost", row["slippage_cost"])
        )
        cutoff_sale_reserve = _d(row.get("cutoff_estimated_sale_fees_and_tax", 0)) + _d(
            row.get("cutoff_estimated_sale_slippage_cost", 0)
        )
        lines.append(
            f"| {row['strategy_label']} | {int(row['closed_rounds']) + int(row['open_rounds'])} | {row['closed_rounds']} | {row['open_rounds']} | {_pct(row['win_rate'])} | {_pct(row['average_return'])}／{_pct(row['median_return'])} | {_money(row['profit_per_100_invested'])} | {_money(row['dividends_received'])} | {_money(actual_cost)} | {_money(cutoff_sale_reserve)} |"
        )
    return "\n".join(lines)


def _timing_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if str(row["strategy_id"]).startswith(("A_", "B_", "C_"))]


def _benchmark_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if str(row["strategy_id"]).startswith("BUY_HOLD_")]


def render_year_report(year: int, rows: Sequence[Mapping[str, Any]]) -> str:
    timing = _timing_rows(rows)
    benchmarks = _benchmark_rows(rows)
    active = [row for row in timing if int(row["transaction_count"]) > 0]
    best = max(active, key=lambda row: _d(row["combined_pnl"]))
    no_trade = [row["strategy_label"] for row in timing if int(row["transaction_count"]) == 0]
    return "\n".join(
        [
            f"# 0050 雙均線回測｜{year}（現金約束修正版）",
            "",
            "## 一、先講結論",
            f"在買進與賣出各預留 0.2% 價差、另扣手續費與交易稅後，有實際交易的方法中，以「{best['strategy_label']}」合計損益最高：{_money(best['combined_pnl'])} 元。未交易的方法不會因損益為零而被稱為最佳。",
            *( [f"本年度沒有交易訊號：{'、'.join(no_trade)}。"] if no_trade else [] ),
            "",
            "## 二、回測範圍",
            "每個方法年初各放入 1 萬元，不能借款、不能讓現金變成負數。每次進場會使用當時帳戶內幾乎全部可用現金，不是每筆固定買 1 萬元；賣出後的本金與獲利可供下一次進場繼續使用。0050 每個交易日都可被監控；訊號使用還原分割與股利影響後的技術序列，成交與估值使用當日原始價格。",
            "",
            "## 三、五個會進出的擇時策略",
            "這五組才是本研究要比較的進出規則。符合條件才買進，跌破防守價或均線趨勢失效才賣出；賣出後若出現新的完整訊號，可以再次進場。",
            _capital_table(timing),
            "",
            "## 四、兩個從頭抱到尾的對照基準",
            "這兩組不是進出策略。它們都在年度第一個交易日買進後一路持有到截止日，只差在股利留現金或拿去再買0050。用途是檢查擇時是否真的勝過直接持有。",
            _capital_table(benchmarks),
            "",
            "## 五、五個擇時策略的交易過程",
            _trading_table(timing),
            "",
            "## 六、截至截止日仍持有",
            "尚未賣出的部位按年度最後交易日（2026 為 9 月 30 日）收盤價，扣除假設賣出費稅與價差後估算；不假裝已經賣出。股利在除息日鎖定資格，發放日才增加現金。",
            "",
            "## 七、分割與股利",
            "2025 年 0050 一拆四只調整股數與技術座標，不算買賣，也不產生損益。股利留現金與股利再投入分開呈現；再投入是用發放日原價實際能買到的整股數計算。",
            "",
            "## 八、逐筆附件",
            "請見 details/trade_details_all_scenarios.csv；每筆都有已實現獲利率或截止日估算獲利率。每日資產、股利權利、分割與略過訊號也都有獨立附件。",
            "",
            "## 九、使用限制",
            "這是固定規則的歷史回測。年度報告每年重新投入 1 萬元，用於看不同市場年度；真正的長期資金比較請看 shared-account-500k.md。",
            "",
        ]
    )


def render_overview(rows: Sequence[Mapping[str, Any]]) -> str:
    timing = _timing_rows(rows)
    benchmarks = _benchmark_rows(rows)
    active = [row for row in timing if int(row["transaction_count"]) > 0]
    best = max(active, key=lambda row: _d(row["ending_equity"]))
    return "\n".join(
        [
            "# 0050 雙均線回測｜2022～2026 五年總覽（現金約束修正版）",
            "",
            "## 一、先講結論",
            f"只比較五個有進出規則的策略，五個年度各自投入 1 萬元後，截止總資產最高的是「{best['strategy_label']}」：{_money(best['ending_equity'])} 元。五年年化採各年度報酬的幾何平均。",
            "",
            "## 二、五個會進出的擇時策略",
            _capital_table(timing),
            "",
            "## 三、兩個全程持有對照基準",
            "全程持有不是進出策略，只用來回答：花力氣判斷買賣點，是否勝過從頭抱到尾。",
            _capital_table(benchmarks),
            "",
            "## 四、五個擇時策略的交易品質",
            _trading_table(timing),
            "",
            "## 五、怎麼解讀",
            "外部本金才是實際從口袋放入的錢。買入周轉總額是同一筆資金反覆進場所累加的金額，不是新增本金；單一時點最高持倉成本則包含先前已實現獲利的再投入，因此可能高於最初本金，但不代表追加資金或借款。",
            "",
            "## 六、比較界線",
            "這張表是五個獨立年度的壓力測試，不是單一帳戶連續複利。要和股票共享帳戶或大盤做公平比較，請看 50 萬元連續帳戶報告。",
            "",
        ]
    )


def render_continuous_report(rows: Sequence[Mapping[str, Any]]) -> str:
    timing = _timing_rows(rows)
    benchmarks = _benchmark_rows(rows)
    best_timing = max(timing, key=lambda row: _d(row["ending_equity"]))
    best_benchmark = max(benchmarks, key=lambda row: _d(row["ending_equity"]))
    return "\n".join(
        [
            "# 0050｜50 萬元連續帳戶（2022-01-03～2026-09-30）",
            "",
            "## 一、先講結論",
            f"五個擇時策略中，截止總資產最高的是「{best_timing['strategy_label']}」：{_money(best_timing['ending_equity'])} 元；兩個對照基準中，最高的是「{best_benchmark['strategy_label']}」：{_money(best_benchmark['ending_equity'])} 元。全程持有不是擇時策略，兩者分開排名。",
            "",
            "## 二、帳戶與部位怎麼計算",
            "帳戶只在開始時放入 50 萬元，不追加、不借款。0050 擇時策略每次進場會投入當時帳戶內幾乎全部可用現金，不是每筆固定 1 萬元；賣出後的本金與已實現獲利都留在帳戶，下一次再投入，所以後期單次持倉成本可能超過 50 萬元。所有方法使用同一份 0050 原價、同一成本模型與同一截止日估值。",
            "",
            "## 三、五個會進出的擇時策略",
            _capital_table(timing),
            "",
            "## 四、兩個從頭抱到尾的對照基準",
            _capital_table(benchmarks),
            "",
            "## 五、五個擇時策略的交易過程",
            _trading_table(timing),
            "",
            "## 六、股利再投入",
            "股利留現金：股利發放後留在帳戶。股利再投入：發放日以當日原價買進可負擔的整股。兩者的截止總資產都已包含持股市值與現金，因此可直接看到整體報酬。",
            "",
        ]
    )


def _read_verified_stock_summary(root: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    manifest_path = root / "artifact_manifest.json"
    readback_path = root / "readback.json"
    summary_path = root / "scenario-summary.csv"
    if not all(path.is_file() for path in (manifest_path, readback_path, summary_path)):
        raise ETF0050Error("stock shared-account comparison input is incomplete")
    readback = json.loads(readback_path.read_text(encoding="utf-8-sig"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if readback.get("status") != "PASS":
        raise ETF0050Error("stock shared-account readback did not PASS")
    artifact = next((row for row in manifest["artifacts"] if row["path"] == "scenario-summary.csv"), None)
    if artifact is None or sha256_file(summary_path).upper() != str(artifact["sha256"]).upper():
        raise ETF0050Error("stock shared-account scenario summary hash mismatch")
    with summary_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows, {
        "root": str(root),
        "artifact_manifest_sha256": sha256_file(manifest_path),
        "readback_sha256": sha256_file(readback_path),
        "scenario_summary_sha256": sha256_file(summary_path),
        "status": "PASS",
    }


def render_stock_comparison(
    *, etf_rows: Sequence[Mapping[str, Any]], stock_rows: Sequence[Mapping[str, str]]
) -> str:
    lines = [
        "# 50 萬元長期比較｜股票共享帳戶、0050 全程持有與 0050 擇時",
        "",
        "## 一、共同期間與資金",
        "共同期間為 2022-01-03 至 2026-09-30，起始本金均為 50 萬元，不允許借款；尚未賣出的持股都按截止日收盤價扣除假設賣出成本後估算。",
        "",
        "## 二、結果",
        "| 類型 | 方法 | 截止總資產 | 總報酬 | 年化報酬 | 最大回落 | 單一時點最高持倉成本（含獲利再投入） | 閒置現金 | 完成買賣 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in stock_rows:
        lines.append(
            f"| 股票五組共享帳戶 | {row['scenario']} | {_money(row['final_equity'])} | {_pct(row['cumulative_return'])} | {_pct(row['annualized_return'])} | {_pct(row['max_drawdown'])} | {_money(row['peak_invested_capital'])} | {_pct(row['average_cash_ratio'])} | {row['completed_trades']} |"
        )
    first = stock_rows[0]
    lines.append(
        f"| 大盤報酬指數 | 加權報酬指數全程持有 | {_money(first['benchmark_final_equity'])} | {_pct(first['benchmark_cumulative_return'])} | {_pct(first['benchmark_annualized_return'])} | {_pct(first['benchmark_max_drawdown'])} | {_money(500000)} | 0.00% | 0 |"
    )
    for row in etf_rows:
        row_type = "0050擇時策略" if str(row["strategy_id"]).startswith(("A_", "B_", "C_")) else "0050對照基準"
        lines.append(
            f"| {row_type} | {row['strategy_label']} | {_money(row['ending_equity'])} | {_pct(row['total_return'])} | {_pct(row['cagr'])} | {_pct(row['max_asset_drawdown'])} | {_money(row['peak_concurrent_investment'])} | {_pct(row['idle_cash_ratio'])} | {row['closed_rounds']} |"
        )
    lines.extend(
        [
            "",
            "## 三、閱讀提醒",
            "0050擇時策略會依規則買賣，而且每次投入當時幾乎全部可用現金；股票五組共享帳戶則依各情境，每檔固定約 1 萬元或按帳戶比例配置。兩邊起始本金與期間一致，但每筆部位大小不同，因此這是帳戶整體成果比較，不是『每筆都買 1 萬元』的比較。0050對照基準從頭抱到尾，不和擇時策略混在一起排名。買入周轉總額不是新增本金。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(value))


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    materialized = [_jsonable(row) for row in rows]
    fields: list[str] = []
    for row in materialized:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)


def _artifact_rows(root: Path, excluded: set[str]) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        rows.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    return rows


def _verify_artifacts(root: Path, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    mismatches = []
    for row in rows:
        path = root / str(row["path"])
        if not path.is_file():
            mismatches.append(f"missing:{row['path']}")
        elif path.stat().st_size != int(row["bytes"]):
            mismatches.append(f"bytes:{row['path']}")
        elif sha256_file(path).lower() != str(row["sha256"]).lower():
            mismatches.append(f"sha256:{row['path']}")
    return mismatches


def _git_commit(repo_root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True).strip()


def run_formal_v2(
    *,
    package_root: Path,
    output_root: Path,
    repo_root: Path,
    stock_account_root: Path = STOCK_ACCOUNT_ROOT,
) -> Mapping[str, Any]:
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite versioned output: {output_root}")
    package = load_package(package_root)
    git_commit = _git_commit(repo_root)
    gate_map = build_ma144_gate_map(
        dates=[bar.date for bar in package.signal_bars],
        closes=[bar.close for bar in package.signal_bars],
        official_sessions=package.sessions,
    )
    base_v1_hash = tree_hash(BASE_V1_ROOT)
    all_metrics: list[dict[str, Any]] = []
    all_details: list[dict[str, Any]] = []
    all_transactions: list[dict[str, Any]] = []
    all_dividends: list[dict[str, Any]] = []
    all_splits: list[dict[str, Any]] = []
    all_curves: list[dict[str, Any]] = []
    all_skipped: list[dict[str, Any]] = []

    for year in YEARS:
        year_end = min(date(year, 12, 31), END)
        signal = [bar for bar in package.signal_bars if bar.date <= year_end]
        raw = [bar for bar in package.raw_bars if bar.date <= year_end]
        for bps in SLIPPAGE_SCENARIOS_BPS:
            runs = run_five_strategy_interfaces(
                signal_bars=signal,
                raw_execution_bars=raw,
                split_events=(package.split,),
                gate_map=gate_map,
                official_sessions=package.sessions,
                manifest_factory=build_manifest_factory(package=package, year=year, git_commit=git_commit),
                slippage_bps=bps,
                backtest_start=date(year, 1, 1),
                backtest_end=year_end,
                split_signal_coordinate_already_adjusted=True,
            )
            for run in runs:
                result = replay_cash_safe_strategy(
                    package=package,
                    run=run,
                    start=date(year, 1, 1),
                    end=year_end,
                    slippage_bps=bps,
                    initial_capital=ANNUAL_CAPITAL,
                    budget_mode="fixed_10000",
                )
                result["metrics"]["year"] = year
                all_metrics.append(result["metrics"])
                for key, target in (
                    ("details", all_details),
                    ("transactions", all_transactions),
                    ("dividends", all_dividends),
                    ("splits", all_splits),
                    ("curve", all_curves),
                    ("skipped", all_skipped),
                ):
                    target.extend(
                        {
                            "year": year,
                            "slippage_bps": bps,
                            "strategy_id": run.spec.strategy_id,
                            "strategy_label": run.spec.label,
                            **row,
                        }
                        for row in result[key]
                    )
            for reinvest in (False, True):
                result = simulate_buy_hold_cash_safe(
                    package=package,
                    start=date(year, 1, 1),
                    end=year_end,
                    slippage_bps=bps,
                    initial_capital=ANNUAL_CAPITAL,
                    reinvest=reinvest,
                )
                result["metrics"]["year"] = year
                all_metrics.append(result["metrics"])
                sid = result["metrics"]["strategy_id"]
                label = result["metrics"]["strategy_label"]
                for key, target in (
                    ("details", all_details),
                    ("transactions", all_transactions),
                    ("dividends", all_dividends),
                    ("splits", all_splits),
                    ("curve", all_curves),
                ):
                    target.extend(
                        {"year": year, "slippage_bps": bps, "strategy_id": sid, "strategy_label": label, **row}
                        for row in result[key]
                    )

    annual_main = [row for row in all_metrics if int(row["slippage_bps"]) == MAIN_SCENARIO_BPS]
    aggregate = aggregate_independent_years(all_metrics, all_details)
    if len(all_metrics) != len(YEARS) * len(SLIPPAGE_SCENARIOS_BPS) * 7:
        raise ETF0050Error("annual scenario row count mismatch")
    if any(_d(row["minimum_cash"]) < 0 for row in all_metrics):
        raise ETF0050Error("negative cash survived annual replay")

    continuous_runs = run_five_strategy_interfaces(
        signal_bars=package.signal_bars,
        raw_execution_bars=package.raw_bars,
        split_events=(package.split,),
        gate_map=gate_map,
        official_sessions=package.sessions,
        manifest_factory=build_manifest_factory(package=package, year=2022, git_commit=git_commit),
        slippage_bps=MAIN_SCENARIO_BPS,
        backtest_start=START,
        backtest_end=END,
        split_signal_coordinate_already_adjusted=True,
    )
    continuous: list[dict[str, Any]] = []
    continuous_details: list[dict[str, Any]] = []
    continuous_transactions: list[dict[str, Any]] = []
    continuous_curves: list[dict[str, Any]] = []
    continuous_actions: list[dict[str, Any]] = []
    for run in continuous_runs:
        result = replay_cash_safe_strategy(
            package=package,
            run=run,
            start=START,
            end=END,
            slippage_bps=MAIN_SCENARIO_BPS,
            initial_capital=CONTINUOUS_CAPITAL,
            budget_mode="all_available",
        )
        continuous.append(result["metrics"])
        continuous_details.extend(result["details"])
        continuous_transactions.extend({"strategy_id": run.spec.strategy_id, **row} for row in result["transactions"])
        continuous_curves.extend({"strategy_id": run.spec.strategy_id, **row} for row in result["curve"])
        continuous_actions.extend({"strategy_id": run.spec.strategy_id, "action_kind": "DIVIDEND", **row} for row in result["dividends"])
        continuous_actions.extend({"strategy_id": run.spec.strategy_id, "action_kind": "SPLIT", **row} for row in result["splits"])
    for reinvest in (False, True):
        result = simulate_buy_hold_cash_safe(
            package=package,
            start=START,
            end=END,
            slippage_bps=MAIN_SCENARIO_BPS,
            initial_capital=CONTINUOUS_CAPITAL,
            reinvest=reinvest,
        )
        continuous.append(result["metrics"])
        continuous_details.extend(result["details"])
        sid = result["metrics"]["strategy_id"]
        continuous_transactions.extend({"strategy_id": sid, **row} for row in result["transactions"])
        continuous_curves.extend({"strategy_id": sid, **row} for row in result["curve"])
        continuous_actions.extend({"strategy_id": sid, "action_kind": "DIVIDEND", **row} for row in result["dividends"])
        continuous_actions.extend({"strategy_id": sid, "action_kind": "SPLIT", **row} for row in result["splits"])
    if any(_d(row["minimum_cash"]) < 0 for row in continuous):
        raise ETF0050Error("negative cash survived continuous replay")

    stock_rows, stock_verification = _read_verified_stock_summary(stock_account_root)
    with tempfile.TemporaryDirectory(prefix="0050-base-v2-") as temp_name:
        stage = Path(temp_name)
        for year in YEARS:
            rows = [row for row in annual_main if int(row["year"]) == year]
            (stage / f"{year}-report.md").write_text(render_year_report(year, rows), encoding="utf-8")
        (stage / "2022-2026-overview.md").write_text(render_overview(aggregate), encoding="utf-8")
        (stage / "shared-account-500k.md").write_text(render_continuous_report(continuous), encoding="utf-8")
        (stage / "comparison-with-stock.md").write_text(
            render_stock_comparison(etf_rows=continuous, stock_rows=stock_rows), encoding="utf-8"
        )
        (stage / "README.md").write_text(
            "# 0050 回測固定入口 base-v2.2\n\n"
            "先看 `comparison-with-stock.md`；再看 `shared-account-500k.md`；年度壓力測試看 `2022-report.md` 至 `2026-report.md`；五年獨立年度總覽看 `2022-2026-overview.md`。\n\n"
            "本版將五個擇時策略與兩個全程持有對照基準分開呈現，並明確標示0050每次投入幾乎全部可用現金、獲利會再投入；同時禁止負現金，修正五年年化與跨年度逐筆統計，並保留舊版本不覆寫。\n",
            encoding="utf-8",
        )
        details_root = stage / "details"
        _write_csv(details_root / "annual_scenario_metrics.csv", all_metrics)
        _write_csv(details_root / "annual_trade_details.csv", all_details)
        _write_csv(details_root / "annual_transactions.csv", all_transactions)
        _write_csv(details_root / "annual_daily_equity.csv", all_curves)
        _write_csv(details_root / "annual_dividend_ledger.csv", all_dividends)
        _write_csv(details_root / "annual_split_ledger.csv", all_splits)
        _write_csv(details_root / "annual_skipped_signals.csv", all_skipped)
        _write_json(details_root / "five_year_aggregate_20bps.json", aggregate)
        _write_csv(details_root / "continuous_500k_metrics.csv", continuous)
        _write_csv(details_root / "continuous_500k_trade_details.csv", continuous_details)
        _write_csv(details_root / "continuous_500k_transactions.csv", continuous_transactions)
        _write_csv(details_root / "continuous_500k_daily_equity.csv", continuous_curves)
        _write_csv(details_root / "continuous_500k_corporate_actions.csv", continuous_actions)
        _write_json(details_root / "stock_source_verification.json", stock_verification)
        qa = {
            "status": "PASS",
            "annual_metric_rows": len(all_metrics),
            "annual_detail_rows": len(all_details),
            "continuous_method_rows": len(continuous),
            "annual_negative_cash_rows": sum(_d(row["minimum_cash"]) < 0 for row in all_metrics),
            "continuous_negative_cash_rows": sum(_d(row["minimum_cash"]) < 0 for row in continuous),
            "geometric_cagr_recomputed": True,
            "pooled_trade_statistics_recomputed": True,
            "buy_hold_dividend_amount_per_share_present": all(
                row.get("amount_per_share") is not None for row in all_dividends
            ),
            "split_ratio_present": all(row.get("ratio") is not None for row in all_splits),
            "base_v1_tree_sha256_before": base_v1_hash,
            "base_v1_tree_sha256_after": tree_hash(BASE_V1_ROOT),
            "stock_source_status": stock_verification["status"],
        }
        if qa["base_v1_tree_sha256_before"] != qa["base_v1_tree_sha256_after"]:
            raise ETF0050Error("base-v1 changed during base-v2 publication")
        _write_json(details_root / "qa-recompute.json", qa)
        artifacts = _artifact_rows(stage, {"manifest.json", "readback.json", "checkpoint.json"})
        manifest = {
            "schema_version": "dual-ma-0050-base-v2-manifest",
            "status": "STOP_WAIT_INDEPENDENT_QA",
            "git_commit": git_commit,
            "package_root": str(package_root),
            "package_tree_sha256": package.package_hash,
            "base_v1_tree_sha256": base_v1_hash,
            "period": {"from": START, "through": END},
            "annual_initial_capital": ANNUAL_CAPITAL,
            "continuous_initial_capital": CONTINUOUS_CAPITAL,
            "main_slippage_bps": MAIN_SCENARIO_BPS,
            "artifacts": artifacts,
        }
        _write_json(stage / "manifest.json", manifest)
        mismatches = _verify_artifacts(stage, artifacts)
        readback = {
            "schema_version": "dual-ma-0050-base-v2-readback",
            "status": "PASS" if not mismatches else "FAIL",
            "mismatches": mismatches,
            "artifact_hashes_verified": not mismatches,
            "qa_status": qa["status"],
            "base_v1_unchanged": qa["base_v1_tree_sha256_before"] == qa["base_v1_tree_sha256_after"],
        }
        if mismatches:
            raise ETF0050Error(f"base-v2 artifact readback failed: {mismatches}")
        _write_json(stage / "readback.json", readback)
        checkpoint = {
            "schema_version": "dual-ma-0050-base-v2-checkpoint",
            "status": "STOP_WAIT_INDEPENDENT_QA",
            "manifest_sha256": sha256_file(stage / "manifest.json"),
            "readback_sha256": sha256_file(stage / "readback.json"),
            "safe_next_step": "independent QA of formulas, cash, split, dividends and comparison inputs",
        }
        _write_json(stage / "checkpoint.json", checkpoint)
        output_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output_root)

    return {
        "status": "STOP_WAIT_INDEPENDENT_QA",
        "output_root": str(output_root),
        "manifest_sha256": sha256_file(output_root / "manifest.json"),
        "readback_sha256": sha256_file(output_root / "readback.json"),
        "annual_rows": len(all_metrics),
        "continuous_rows": len(continuous),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, default=PACKAGE_ROOT_V3)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT_DEFAULT)
    parser.add_argument("--stock-account-root", type=Path, default=STOCK_ACCOUNT_ROOT)
    args = parser.parse_args(argv)
    result = run_formal_v2(
        package_root=args.package_root,
        output_root=args.output_root,
        repo_root=Path(__file__).resolve().parents[2],
        stock_account_root=args.stock_account_root,
    )
    print(json.dumps(_jsonable(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
