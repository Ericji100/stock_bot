"""Execution timing and robustness research for the accepted 0050 regime strategies.

Signals are frozen from causal close-of-day indicator state.  This module only
schedules those frozen orders; it never uses a future fill to revise a signal.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from bisect import bisect_left, bisect_right
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from itertools import product
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_2006_fixed_methods_v1 as accepted
from . import etf_0050_ma21_fixed_stop_v1 as fixed
from . import etf_0050_ma21_ma144_regime_v1 as regime
from . import etf_0050_ma21_macd_v2 as moving
from . import etf_0050_standard_report_v3 as standard
from .etf_0050_base_v1 import ETF0050Error, StrategyRunSlice
from .etf_0050_base_v1 import tree_hash
from .costs import CATHAY_WEB_028_TW_V1, size_entry
from .etf_0050_formal_v2 import (
    CONTINUOUS_CAPITAL, _artifact_rows, _verify_artifacts, _write_csv, _write_json,
    replay_cash_safe_strategy,
)
from .etf_0050_ten_year_same_method_v1 import _d, normalize_value
from .models import InstrumentType
from .paths import dev_run, report_path


TIMING_SIGNAL_CLOSE = "SIGNAL_CLOSE"
TIMING_NEXT_OPEN = "NEXT_SESSION_OPEN"
TIMING_NEXT_CLOSE = "NEXT_SESSION_CLOSE"
EXECUTION_TIMINGS = (TIMING_SIGNAL_CLOSE, TIMING_NEXT_OPEN, TIMING_NEXT_CLOSE)
SLIPPAGE_BPS = (20, 40, 60)
MA_FAST_VALUES = (18, 21, 24)
MA_SLOW_VALUES = (126, 144, 162)
MACD_FAST_VALUES = (18, 21, 24)
MACD_SLOW_VALUES = (50, 55, 60)
MACD_SIGNAL_VALUES = (45, 55, 65)
PARAMETER_GRID = tuple(product(
    MA_FAST_VALUES, MA_SLOW_VALUES,
    MACD_FAST_VALUES, MACD_SLOW_VALUES, MACD_SIGNAL_VALUES,
))
ALLOCATION_WEIGHTS = (0, 20, 40, 60)
RUNNER_VERSION = "dual-ma-0050-execution-time-parameter-allocation-v1.5"
RUN_ROOT_DEFAULT = dev_run("02-etf-0050-execution-stability-four-phase-v1-5")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "experiments", "2006-2026-execution-stability-four-phase-v1-5")
V14_RUN_ROOT = dev_run("02-etf-0050-execution-stability-four-phase-v1-4")
V14_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-execution-stability-four-phase-v1-4")
V14_RUN_MANIFEST = "BF068E29F947DE6F02D5534989B2C23F0EC17D1036408F539A192356C569F1A4"
V14_REPORT_MANIFEST = "42E2146F45DFB243BB262FA90A1D4938F81984B8B8F2CD1CE36F60092D3C8E90"
V13_RUN_ROOT = dev_run("02-etf-0050-execution-stability-four-phase-v1-3")
V13_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-execution-stability-four-phase-v1-3")
V13_RUN_MANIFEST = "22370F152A3EECA9167E9FA1C82EBF2F1A8629B0AB584EA0F8EBA76C1A01B1C5"
V13_REPORT_MANIFEST = "94C827F2C06EB7C4D8F427A7283FC7A17E847413720AC326C9A9F1A2A8834D0C"
V12_RUN_ROOT = dev_run("02-etf-0050-execution-stability-four-phase-v1-2")
V12_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-execution-stability-four-phase-v1-2")
V12_RUN_MANIFEST = "0FAA83E8D138ACEC09E6E26A3E1CDB8ED37641F4F114541B2DF96672E09EB25A"
V12_REPORT_MANIFEST = "BF279343468E1BA04A3DCC4FC8DBE59578519FDED7741F3614DC90BB4670A30A"
V11_RUN_ROOT = dev_run("02-etf-0050-execution-stability-four-phase-v1-1")
V11_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-execution-stability-four-phase-v1-1")
V11_RUN_MANIFEST = "E5A6E825ECC40532DCA8A33A730890A3A04619DA24C2EDF0F7D9DB46C03F978E"
V11_REPORT_MANIFEST = "C06E95BD88EAA497CFDA0345EB177DC5B368013210F39953646A2CFEDA806FCD"
PREVIOUS_CANDIDATE_RUN_ROOT = dev_run("02-etf-0050-execution-stability-four-phase-v1")
PREVIOUS_CANDIDATE_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-execution-stability-four-phase-v1")
PREVIOUS_CANDIDATE_RUN_MANIFEST = "AC7F87DD9C816107D8ECA42B432541D7EBE11AFF0759134FC88B3EB94731B424"
PREVIOUS_CANDIDATE_REPORT_MANIFEST = "8330723DDA7656DF20F39AD5BE30A2BBFFB061358CCC7CE7FF6D553D7D794E41"
V15_RUN_ROOT = dev_run("02-etf-0050-2006-2026-seven-methods-v1-5")
V16_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-seven-methods-complete-report-v1-6")
V15_RUN_MANIFEST = "92BD69796D8F20C31C76EBAEC3F26ECB872B4540C92A6AD59F9D6C1EFF1C28A9"
V16_REPORT_MANIFEST = "F9A6B678CB428EE5CF1C2A6069719FFE3D454D84A19BEB22046DCDA97C73F7DE"
FIXED_PERIODS = (
    ("2006_2012", date(2006, 1, 2), date(2012, 12, 31)),
    ("2013_2019", date(2013, 1, 1), date(2019, 12, 31)),
    ("2020_2026M09", date(2020, 1, 1), date(2026, 9, 30)),
)


def _max_affordable_shares(cash: Decimal, reference: Decimal, bps: int) -> int:
    if cash <= 0:
        return 0
    model = CATHAY_WEB_028_TW_V1.with_research_slippage(bps)
    estimate = int(cash / (reference * (Decimal("1") + Decimal(bps) / Decimal("10000"))))
    while estimate > 0 and model.buy(reference, estimate).cash_amount > cash:
        estimate -= 1
    return estimate


def replay_core_sleeve(
    *, package: Any, initial_capital: Decimal, slippage_bps: int = 20,
) -> dict[str, Any]:
    """Buy-and-hold sleeve with dividend reinvestment after the payment date."""

    sessions = tuple(day for day in package.sessions if accepted.FORMAL_START <= day <= accepted.CUTOFF)
    model = CATHAY_WEB_028_TW_V1.with_research_slippage(slippage_bps)
    cash, shares = initial_capital, 0
    transactions: list[dict[str, Any]] = []
    dividends: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    rights: dict[date, Decimal] = {}
    pay_by_day: dict[date, list[Any]] = {}
    reinvest_by_day: dict[date, list[Any]] = {}
    for event in package.dividends:
        pay_cursor = bisect_left(sessions, event.payment_date)
        if pay_cursor < len(sessions):
            pay_by_day.setdefault(sessions[pay_cursor], []).append(event)
        cursor = bisect_right(sessions, event.payment_date)
        if cursor < len(sessions):
            reinvest_by_day.setdefault(sessions[cursor], []).append(event)
    peak = initial_capital
    first = sessions[0]

    for day in sessions:
        if day == package.split.effective_date:
            before = shares
            shares = int(Decimal(shares) * package.split.ratio)
            transactions.append({
                "date": day, "role": "SPLIT_REBASE_ONLY", "shares_before": before,
                "shares_after": shares, "cash_amount": Decimal("0"),
            })
        for event in package.dividends:
            if event.ex_date == day:
                rights[event.ex_date] = Decimal(shares) * event.amount_per_share
                dividends.append({
                    "ex_date": day, "payment_date": event.payment_date,
                    "shares": shares, "amount_per_share": event.amount_per_share,
                    "cash": rights[event.ex_date], "stage": "ENTITLEMENT_LOCKED",
                })
        if day == first and initial_capital > 0:
            quantity = _max_affordable_shares(cash, package.raw_by_date[day].open, slippage_bps)
            if quantity:
                order = model.buy(package.raw_by_date[day].open, quantity)
                cash -= order.cash_amount
                shares += quantity
                transactions.append({
                    "date": day, "role": "CORE_INITIAL_BUY", "reference_price": order.reference_price,
                    "fill_price": order.fill_price, "shares": quantity, "gross": order.gross,
                    "commission": order.commission, "tax": order.tax, "cash_amount": order.cash_amount,
                })
        for event in pay_by_day.get(day, ()):
            amount = rights.get(event.ex_date, Decimal("0"))
            cash += amount
            dividends.append({
                "ex_date": event.ex_date, "payment_date": event.payment_date,
                "cash": amount, "stage": "CASH_PAID",
            })
        for event in reinvest_by_day.get(day, ()):
            quantity = _max_affordable_shares(cash, package.raw_by_date[day].open, slippage_bps)
            if quantity:
                order = model.buy(package.raw_by_date[day].open, quantity)
                cash -= order.cash_amount
                shares += quantity
                transactions.append({
                    "date": day, "role": "CORE_DIVIDEND_REINVEST", "reference_price": order.reference_price,
                    "fill_price": order.fill_price, "shares": quantity, "gross": order.gross,
                    "commission": order.commission, "tax": order.tax, "cash_amount": order.cash_amount,
                    "source_ex_date": event.ex_date, "source_payment_date": event.payment_date,
                    "reinvest_date": day,
                })
        liquidation = model.sell(package.raw_by_date[day].close, shares, InstrumentType.ETF).cash_amount if shares else Decimal("0")
        equity = cash + liquidation
        peak = max(peak, equity)
        curve.append({
            "date": day, "cash": cash, "shares": shares,
            "liquidation_value": liquidation, "equity": equity,
            "drawdown": equity / peak - 1 if peak else Decimal("0"),
        })
        if cash < 0:
            raise ETF0050Error("core sleeve cash became negative")
    return {"curve": curve, "transactions": transactions, "dividends": dividends}


def combine_isolated_sleeves(
    *, core: Mapping[str, Any], tactical: Mapping[str, Any], core_weight_pct: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    core_by_day = {row["date"]: row for row in core["curve"]}
    tactical_by_day = {row["date"]: row for row in tactical["curve"]}
    if set(core_by_day) != set(tactical_by_day):
        raise ETF0050Error("sleeve calendars differ")
    combined: list[dict[str, Any]] = []
    peak = CONTINUOUS_CAPITAL
    for day in sorted(core_by_day):
        c, t = core_by_day[day], tactical_by_day[day]
        equity = _d(c["equity"]) + _d(t["equity"])
        peak = max(peak, equity)
        combined.append({
            "date": day,
            "core_cash": c["cash"], "core_shares": c["shares"],
            "core_liquidation_value": c.get("liquidation_value", _d(c["equity"]) - _d(c["cash"])), "core_equity": c["equity"],
            "tactical_cash": t["cash"], "tactical_shares": t["shares"],
            "tactical_liquidation_value": t.get("liquidation_value", _d(t["equity"]) - _d(t["cash"])), "tactical_equity": t["equity"],
            "cash": _d(c["cash"]) + _d(t["cash"]),
            "equity": equity, "drawdown": equity / peak - 1,
        })
    start, end = combined[0], combined[-1]
    years = (end["date"] - start["date"]).days / 365
    summary = {
        "core_weight_pct": core_weight_pct,
        "tactical_weight_pct": 100 - core_weight_pct,
        "ending_equity": end["equity"],
        "total_return": _d(end["equity"]) / CONTINUOUS_CAPITAL - 1,
        "cagr": Decimal(str(float(_d(end["equity"]) / CONTINUOUS_CAPITAL) ** (1 / years) - 1)),
        "max_drawdown": min(_d(row["drawdown"]) for row in combined),
        "minimum_core_cash": min(_d(row["core_cash"]) for row in combined),
        "minimum_tactical_cash": min(_d(row["tactical_cash"]) for row in combined),
        **underwater_statistics(combined),
    }
    return combined, summary


def _zero_sleeve(package: Any) -> dict[str, Any]:
    return {
        "curve": [
            {"date": day, "cash": Decimal("0"), "shares": 0,
             "liquidation_value": Decimal("0"), "equity": Decimal("0"),
             "drawdown": Decimal("0")}
            for day in package.sessions if accepted.FORMAL_START <= day <= accepted.CUTOFF
        ],
        "transactions": [], "dividends": [],
    }


def _normalize_allocation_dividend_event(
    row: Mapping[str, Any], sessions: Sequence[date],
) -> dict[str, Any]:
    """Give every allocation dividend event a causal date and cash_amount."""

    stage = str(row.get("stage", "EVENT"))
    if stage == "ENTITLEMENT_LOCKED":
        event_day = row["ex_date"]
    elif stage == "CASH_PAID":
        cursor = bisect_left(sessions, row["payment_date"])
        if cursor >= len(sessions):
            raise ETF0050Error("dividend payment has no effective official session")
        event_day = sessions[cursor]
    else:
        raise ETF0050Error(f"unsupported dividend stage: {stage}")
    return {
        **row, "date": event_day, "role": "DIVIDEND_" + stage,
        "cash_amount": _d(row.get("cash", 0)),
    }


def run_no_rebalance_allocations(
    *, package: Any, raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    indicators = regime.build_indicator_rows(raw_rows, events)
    frozen, _daily, _events = regime.build_regime_run(
        indicator_rows=indicators, raw_by_date=package.raw_by_date,
        strategy_id=regime.FIXED_ID, moving_stop=False,
        start=accepted.FORMAL_START, end=accepted.CUTOFF, slippage_bps=20,
    )
    delayed, _audit = schedule_frozen_orders(
        run=frozen, sessions=package.sessions, raw_by_date=package.raw_by_date,
        indicator_by_date={row["date"]: row for row in indicators}, timing=TIMING_NEXT_OPEN,
    )
    summaries: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    for core_pct in ALLOCATION_WEIGHTS:
        core_capital = CONTINUOUS_CAPITAL * Decimal(core_pct) / Decimal("100")
        tactical_capital = CONTINUOUS_CAPITAL - core_capital
        core = replay_core_sleeve(package=package, initial_capital=core_capital) if core_capital else _zero_sleeve(package)
        tactical = replay_cash_safe_strategy(
            package=package, run=delayed, start=accepted.FORMAL_START, end=accepted.CUTOFF,
            slippage_bps=20, initial_capital=tactical_capital,
            budget_mode="all_available", research_slippage=True,
        ) if tactical_capital else _zero_sleeve(package)
        combined, summary = combine_isolated_sleeves(
            core=core, tactical=tactical, core_weight_pct=core_pct,
        )
        allocation_id = f"CORE_{core_pct}_TACTICAL_{100-core_pct}_NO_REBALANCE"
        summaries.append({"allocation_id": allocation_id, "rebalance": "NONE", **summary})
        daily.extend({"allocation_id": allocation_id, "rebalance": "NONE", **row} for row in combined)
        for sleeve, payload in (("CORE", core), ("TACTICAL", tactical)):
            ledger.extend(
                {"allocation_id": allocation_id, "rebalance": "NONE", "sleeve": sleeve, **row}
                for row in payload["transactions"]
            )
            ledger.extend(
                {"allocation_id": allocation_id, "rebalance": "NONE", "sleeve": sleeve,
                 **_normalize_allocation_dividend_event(row, package.sessions)}
                for row in payload.get("dividends", ())
            )
            ledger.extend(
                {"allocation_id": allocation_id, "rebalance": "NONE", "sleeve": sleeve,
                 "role": str(row.get("type", "SPLIT_REBASE_ONLY")), **row}
                for row in payload.get("splits", ())
            )
    return summaries, daily, ledger


def _entry_stop_raw(row: Mapping[str, Any]) -> Decimal:
    if row.get("initial_stop_raw_override") not in (None, ""):
        return _d(row["initial_stop_raw_override"])
    return _d(row["reference_price"]) * _d(row["initial_stop"]) / _d(row["entry_reference_close"])


def _minimum_sale_shares(*, model: Any, reference: Decimal, shares: int, required_cash: Decimal) -> int:
    """Smallest whole-share sale whose net proceeds cover required_cash."""

    if required_cash <= 0 or shares <= 0:
        return 0
    if model.sell(reference, shares, InstrumentType.ETF).cash_amount < required_cash:
        return shares
    low, high = 1, shares
    while low < high:
        middle = (low + high) // 2
        if model.sell(reference, middle, InstrumentType.ETF).cash_amount >= required_cash:
            high = middle
        else:
            low = middle + 1
    return low


def _transfer_at_close(
    *, day: date, reference: Decimal, desired: Decimal, donor_name: str,
    donor: dict[str, Any], recipient_name: str, recipient: dict[str, Any],
    model: Any, ledger: list[dict[str, Any]],
) -> Decimal:
    """Fund a sleeve transfer from cash first, selling ETF only if necessary."""

    before_total = _d(donor["cash"]) + _d(recipient["cash"])
    sale_cost = Decimal("0")
    sale_proceeds = Decimal("0")
    if _d(donor["cash"]) < desired:
        required = desired - _d(donor["cash"])
        quantity = _minimum_sale_shares(
            model=model, reference=reference, shares=int(donor["shares"]), required_cash=required,
        )
        if quantity:
            sale = model.sell(reference, quantity, InstrumentType.ETF)
            donor["cash"] = _d(donor["cash"]) + sale.cash_amount
            sale_proceeds = sale.cash_amount
            donor["shares"] = int(donor["shares"]) - quantity
            sale_cost = (
                (sale.reference_price - sale.fill_price) * quantity
                + sale.commission + sale.tax
            )
            ledger.append({
                "date": day, "role": "ANNUAL_REBALANCE_SELL", "sleeve": donor_name,
                "reference_price": sale.reference_price, "fill_price": sale.fill_price,
                "shares": quantity, "gross": sale.gross, "commission": sale.commission,
                "tax": sale.tax, "cash_amount": sale.cash_amount,
                "cash_before": _d(donor["cash"]) - sale.cash_amount,
                "cash_after": donor["cash"], "shares_after": donor["shares"],
            })
    transferred = min(desired, _d(donor["cash"]))
    donor["cash"] = _d(donor["cash"]) - transferred
    recipient["cash"] = _d(recipient["cash"]) + transferred
    ledger.append({
        "date": day, "role": "ANNUAL_CASH_TRANSFER", "from_sleeve": donor_name,
        "to_sleeve": recipient_name, "gross_transfer": transferred,
        "sell_commission": Decimal("0"), "sell_tax": Decimal("0"),
        "transfer_cost": Decimal("0"), "securities_sold": sale_cost > 0,
        "cash_conservation_difference": before_total - (_d(donor["cash"]) + _d(recipient["cash"]))
            + sale_proceeds,
    })
    return transferred


def annual_rebalanced_sleeves(
    *, package: Any, run: StrategyRunSlice, core_weight_pct: int, slippage_bps: int = 20,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Replay two real cash/share sleeves and rebalance after each first-session close.

    Daily causal order is: split, ex-date entitlement, cash payment, tactical
    t+1-open exit/entry, close mark, then annual close rebalance.  Transfers use
    donor cash first.  A tactical recipient keeps new cash idle until a later
    frozen compliant entry; an existing tactical position is never topped up.
    """

    sessions = tuple(day for day in package.sessions if accepted.FORMAL_START <= day <= accepted.CUTOFF)
    model = CATHAY_WEB_028_TW_V1.with_research_slippage(slippage_bps)
    weight = Decimal(core_weight_pct) / Decimal("100")
    core = {"cash": CONTINUOUS_CAPITAL * weight, "shares": 0}
    tactical = {"cash": CONTINUOUS_CAPITAL * (1 - weight), "shares": 0, "active_leg": None}
    tx_by_day: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for row in run.transactions:
        if sessions[0] <= row["date"] <= sessions[-1]:
            tx_by_day[row["date"]].append(row)
    ex_by_day = {event.ex_date: event for event in package.dividends}
    pay_by_day: dict[date, list[Any]] = defaultdict(list)
    core_reinvest_by_day: dict[date, list[Any]] = defaultdict(list)
    for event in package.dividends:
        cursor = bisect_left(sessions, event.payment_date)
        if cursor < len(sessions):
            pay_by_day[sessions[cursor]].append(event)
        reinvest_cursor = bisect_right(sessions, event.payment_date)
        if reinvest_cursor < len(sessions):
            core_reinvest_by_day[sessions[reinvest_cursor]].append(event)
    rights: dict[str, dict[date, Decimal]] = {"CORE": {}, "TACTICAL": {}}
    daily: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    peak = CONTINUOUS_CAPITAL
    total_rebalance_cost = Decimal("0")
    seen_year = sessions[0].year

    for index, day in enumerate(sessions):
        raw = package.raw_by_date[day]
        if day == package.split.effective_date:
            for name, sleeve in (("CORE", core), ("TACTICAL", tactical)):
                before = int(sleeve["shares"])
                sleeve["shares"] = int(Decimal(before) * package.split.ratio)
                ledger.append({"date": day, "role": "SPLIT_REBASE_ONLY", "sleeve": name,
                               "ratio": package.split.ratio, "shares_before": before,
                               "shares_after": sleeve["shares"], "cash_amount": Decimal("0")})
        event = ex_by_day.get(day)
        if event is not None:
            for name, sleeve in (("CORE", core), ("TACTICAL", tactical)):
                amount = Decimal(int(sleeve["shares"])) * event.amount_per_share
                rights[name][event.ex_date] = amount
                ledger.append({"date": day, "role": "DIVIDEND_ENTITLEMENT", "sleeve": name,
                               "ex_date": event.ex_date, "payment_date": event.payment_date,
                               "shares": sleeve["shares"], "amount_per_share": event.amount_per_share,
                               "cash_amount": amount})
        for event in pay_by_day.get(day, ()):
            for name, sleeve in (("CORE", core), ("TACTICAL", tactical)):
                amount = rights[name].get(event.ex_date, Decimal("0"))
                sleeve["cash"] = _d(sleeve["cash"]) + amount
                ledger.append({"date": day, "role": "DIVIDEND_CASH_PAID", "sleeve": name,
                               "ex_date": event.ex_date, "payment_date": event.payment_date,
                               "cash_amount": amount, "cash_after": sleeve["cash"]})
        for _event in core_reinvest_by_day.get(day, ()):
            quantity = _max_affordable_shares(_d(core["cash"]), raw.open, slippage_bps)
            if quantity:
                buy = model.buy(raw.open, quantity)
                core["cash"] = _d(core["cash"]) - buy.cash_amount
                core["shares"] = int(core["shares"]) + quantity
                ledger.append({"date": day, "role": "CORE_DIVIDEND_REINVEST", "sleeve": "CORE",
                               "reference_price": buy.reference_price, "fill_price": buy.fill_price,
                               "shares": quantity, "gross": buy.gross, "commission": buy.commission,
                               "tax": buy.tax, "cash_amount": buy.cash_amount,
                               "cash_after": core["cash"], "shares_after": core["shares"]})
        if index == 0 and _d(core["cash"]) > 0:
            quantity = _max_affordable_shares(_d(core["cash"]), raw.open, slippage_bps)
            if quantity:
                buy = model.buy(raw.open, quantity)
                core["cash"] = _d(core["cash"]) - buy.cash_amount
                core["shares"] = quantity
                ledger.append({"date": day, "role": "CORE_INITIAL_BUY", "sleeve": "CORE",
                               "reference_price": buy.reference_price, "fill_price": buy.fill_price,
                               "shares": quantity, "gross": buy.gross, "commission": buy.commission,
                               "tax": buy.tax, "cash_amount": buy.cash_amount,
                               "cash_after": core["cash"], "shares_after": core["shares"]})
        for frozen in sorted(tx_by_day.get(day, ()), key=lambda row: 0 if row["role"] == "EXIT" else 1):
            leg_id = str(frozen["leg_id"])
            if frozen["role"] == "EXIT":
                if tactical["active_leg"] != leg_id:
                    continue
                quantity = int(tactical["shares"])
                if quantity:
                    sale = model.sell(_d(frozen["reference_price"]), quantity, InstrumentType.ETF)
                    tactical["cash"] = _d(tactical["cash"]) + sale.cash_amount
                    tactical["shares"] = 0
                    ledger.append({"date": day, "role": "TACTICAL_EXIT", "sleeve": "TACTICAL",
                                   "leg_id": leg_id, "reference_price": sale.reference_price,
                                   "fill_price": sale.fill_price, "shares": quantity, "gross": sale.gross,
                                   "commission": sale.commission, "tax": sale.tax,
                                   "cash_amount": sale.cash_amount, "cash_after": tactical["cash"],
                                   "shares_after": 0})
                tactical["active_leg"] = None
            elif frozen.get("status") == "FILLED" and tactical["active_leg"] is None:
                budget = _d(tactical["cash"])
                sized = size_entry(
                    _d(frozen["reference_price"]), _entry_stop_raw(frozen), InstrumentType.ETF,
                    cost_model=model, max_total_cost=budget,
                    max_initial_r=budget * Decimal("0.20"), max_shares=1_000_000,
                )
                if sized is not None:
                    tactical["cash"] = budget - sized.buy.cash_amount
                    tactical["shares"] = sized.shares
                    tactical["active_leg"] = leg_id
                    ledger.append({"date": day, "role": "TACTICAL_ENTRY", "sleeve": "TACTICAL",
                                   "leg_id": leg_id, "reference_price": sized.buy.reference_price,
                                   "fill_price": sized.buy.fill_price, "shares": sized.shares,
                                   "gross": sized.buy.gross, "commission": sized.buy.commission,
                                   "tax": sized.buy.tax, "cash_amount": sized.buy.cash_amount,
                                   "cash_after": tactical["cash"], "shares_after": tactical["shares"]})

        core_liq_before = model.sell(raw.close, int(core["shares"]), InstrumentType.ETF).cash_amount if core["shares"] else Decimal("0")
        tactical_liq_before = model.sell(raw.close, int(tactical["shares"]), InstrumentType.ETF).cash_amount if tactical["shares"] else Decimal("0")
        before = _d(core["cash"]) + core_liq_before + _d(tactical["cash"]) + tactical_liq_before
        if day.year != seen_year:
            seen_year = day.year
            target_core = before * weight
            core_equity_before = _d(core["cash"]) + core_liq_before
            desired = abs(core_equity_before - target_core)
            if desired > 0:
                donor_name, donor, recipient_name, recipient = (
                    ("CORE", core, "TACTICAL", tactical)
                    if core_equity_before > target_core
                    else ("TACTICAL", tactical, "CORE", core)
                )
                transferred = _transfer_at_close(
                    day=day, reference=raw.close, desired=desired, donor_name=donor_name,
                    donor=donor, recipient_name=recipient_name, recipient=recipient,
                    model=model, ledger=ledger,
                )
                if recipient_name == "CORE" and transferred > 0:
                    quantity = _max_affordable_shares(_d(core["cash"]), raw.close, slippage_bps)
                    if quantity:
                        buy = model.buy(raw.close, quantity)
                        core["cash"] = _d(core["cash"]) - buy.cash_amount
                        core["shares"] = int(core["shares"]) + quantity
                        ledger.append({"date": day, "role": "ANNUAL_CORE_BUY", "sleeve": "CORE",
                                       "reference_price": buy.reference_price, "fill_price": buy.fill_price,
                                       "shares": quantity, "gross": buy.gross, "commission": buy.commission,
                                       "tax": buy.tax, "cash_amount": buy.cash_amount,
                                       "cash_after": core["cash"], "shares_after": core["shares"]})
                core_liq_after = model.sell(raw.close, int(core["shares"]), InstrumentType.ETF).cash_amount if core["shares"] else Decimal("0")
                tactical_liq_after = model.sell(raw.close, int(tactical["shares"]), InstrumentType.ETF).cash_amount if tactical["shares"] else Decimal("0")
                after = _d(core["cash"]) + core_liq_after + _d(tactical["cash"]) + tactical_liq_after
                event_cost = before - after
                total_rebalance_cost += event_cost
                ledger.append({"date": day, "role": "ANNUAL_REBALANCE_RECONCILIATION",
                               "from_sleeve": donor_name, "to_sleeve": recipient_name,
                               "target_transfer": desired, "gross_transfer": transferred,
                               "equity_before": before, "equity_after": after,
                               "total_cost": event_cost,
                               "conservation_difference": before - after - event_cost})
        core_liq = model.sell(raw.close, int(core["shares"]), InstrumentType.ETF).cash_amount if core["shares"] else Decimal("0")
        tactical_liq = model.sell(raw.close, int(tactical["shares"]), InstrumentType.ETF).cash_amount if tactical["shares"] else Decimal("0")
        core_equity = _d(core["cash"]) + core_liq
        tactical_equity = _d(tactical["cash"]) + tactical_liq
        equity = core_equity + tactical_equity
        peak = max(peak, equity)
        if _d(core["cash"]) < 0 or _d(tactical["cash"]) < 0:
            raise ETF0050Error(f"annual sleeve cash became negative on {day}")
        daily.append({"date": day, "raw_close": raw.close,
                      "core_cash": core["cash"], "core_shares": core["shares"],
                      "core_liquidation_value": core_liq, "core_equity": core_equity,
                      "tactical_cash": tactical["cash"], "tactical_shares": tactical["shares"],
                      "tactical_liquidation_value": tactical_liq,
                      "tactical_active_leg_id": tactical["active_leg"],
                      "tactical_equity": tactical_equity,
                      "cash": _d(core["cash"]) + _d(tactical["cash"]),
                      "equity": equity, "drawdown": equity / peak - 1})
    years = (sessions[-1] - sessions[0]).days / 365
    summary = {"core_weight_pct": core_weight_pct, "tactical_weight_pct": 100 - core_weight_pct,
               "rebalance": "ANNUAL_FIRST_SESSION_CLOSE_ACTUAL_LEDGER",
               "ending_equity": daily[-1]["equity"],
               "total_return": _d(daily[-1]["equity"]) / CONTINUOUS_CAPITAL - 1,
               "cagr": Decimal(str(float(_d(daily[-1]["equity"]) / CONTINUOUS_CAPITAL) ** (1 / years) - 1)),
               "max_drawdown": min(_d(row["drawdown"]) for row in daily),
               "rebalance_events": sum(row["role"] == "ANNUAL_REBALANCE_RECONCILIATION" for row in ledger),
               "rebalance_cost": total_rebalance_cost,
               "minimum_core_cash": min(_d(row["core_cash"]) for row in daily),
               "minimum_tactical_cash": min(_d(row["tactical_cash"]) for row in daily),
               **underwater_statistics(daily)}
    return daily, ledger, summary


def run_allocation_study(
    *, package: Any, raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    summaries, daily, ledger = run_no_rebalance_allocations(
        package=package, raw_rows=raw_rows, events=events,
    )
    indicators = regime.build_indicator_rows(raw_rows, events)
    frozen, _daily, _events = regime.build_regime_run(
        indicator_rows=indicators, raw_by_date=package.raw_by_date,
        strategy_id=regime.FIXED_ID, moving_stop=False,
        start=accepted.FORMAL_START, end=accepted.CUTOFF, slippage_bps=20,
    )
    delayed, _audit = schedule_frozen_orders(
        run=frozen, sessions=package.sessions, raw_by_date=package.raw_by_date,
        indicator_by_date={row["date"]: row for row in indicators}, timing=TIMING_NEXT_OPEN,
    )
    for core_pct in ALLOCATION_WEIGHTS:
        allocation_id = f"CORE_{core_pct}_TACTICAL_{100-core_pct}_ANNUAL"
        combined, events_rows, summary = annual_rebalanced_sleeves(
            package=package, run=delayed, core_weight_pct=core_pct,
        )
        summaries.append({"allocation_id": allocation_id, **summary})
        daily.extend({"allocation_id": allocation_id, **row} for row in combined)
        ledger.extend({"allocation_id": allocation_id, "sleeve": "TRANSFER", **row} for row in events_rows)
    return summaries, daily, ledger


def enrich_allocation_summaries(
    summaries: Sequence[Mapping[str, Any]], daily: Sequence[Mapping[str, Any]],
    benchmark_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    benchmark = {
        date.fromisoformat(str(row["date"])): _d(row.get("equivalent_500k", row.get("close")))
        for row in benchmark_rows
    }
    regime_map = {
        date.fromisoformat(str(row["date"])): row["regime"]
        for row in causal_market_regimes(benchmark_rows)
    }
    output: list[dict[str, Any]] = []
    for summary in summaries:
        rows = sorted(
            (row for row in daily if row["allocation_id"] == summary["allocation_id"]),
            key=lambda row: row["date"],
        )
        annual = annual_performance(rows)
        bull_strategy = bull_benchmark = empty_benchmark = Decimal("1")
        for prior, current in zip(rows, rows[1:]):
            strategy_return = _d(current["equity"]) / _d(prior["equity"])
            benchmark_return = benchmark[current["date"]] / benchmark[prior["date"]]
            if regime_map[current["date"]] == "BULL":
                bull_strategy *= strategy_return
                bull_benchmark *= benchmark_return
            if int(prior.get("tactical_shares", 0)) == 0:
                empty_benchmark *= benchmark_return
        output.append({
            **summary,
            "worst_year": min(_d(row["return"]) for row in annual),
            "bull_strategy_return": bull_strategy - 1,
            "bull_benchmark_return": bull_benchmark - 1,
            "bull_participation": (bull_strategy - 1) / (bull_benchmark - 1) if bull_benchmark != 1 else None,
            "tactical_empty_period_benchmark_return": empty_benchmark - 1,
            "average_idle_cash_ratio": sum((_d(row.get("cash", 0)) / _d(row["equity"]) for row in rows), Decimal("0")) / Decimal(len(rows)),
        })
    return output


def _shift_year(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def rolling_performance(
    rows: Sequence[Mapping[str, Any]], windows: Sequence[int] = (3, 5, 10),
) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: row["date"])
    days = [row["date"] for row in ordered]
    month_ends = [
        index for index, day in enumerate(days)
        if index == len(days) - 1 or (days[index + 1].year, days[index + 1].month) != (day.year, day.month)
    ]
    output: list[dict[str, Any]] = []
    for end_index in month_ends:
        end_row = ordered[end_index]
        for years in windows:
            target = _shift_year(end_row["date"], years)
            if target < days[0]:
                continue
            start_index = bisect_left(days, target)
            if start_index >= end_index:
                continue
            start_equity = _d(ordered[start_index]["equity"])
            end_equity = _d(end_row["equity"])
            if start_equity <= 0:
                continue
            cagr = Decimal(str(float(end_equity / start_equity) ** (365 / max((end_row["date"] - days[start_index]).days, 1)) - 1))
            peak, max_dd = start_equity, Decimal("0")
            for row in ordered[start_index : end_index + 1]:
                equity = _d(row["equity"])
                peak = max(peak, equity)
                max_dd = min(max_dd, equity / peak - 1)
            output.append({
                "window_years": years, "start_date": days[start_index],
                "end_date": end_row["date"], "start_equity": start_equity,
                "end_equity": end_equity, "total_return": end_equity / start_equity - 1,
                "cagr": cagr, "max_drawdown": max_dd,
            })
    return output


def rolling_cagr_series(
    rows: Sequence[Mapping[str, Any]], years: int,
) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: row["date"])
    days = [row["date"] for row in ordered]
    output: list[dict[str, Any]] = []
    for end_index, end_row in enumerate(ordered):
        if end_index != len(ordered) - 1 and (
            days[end_index + 1].year, days[end_index + 1].month
        ) == (end_row["date"].year, end_row["date"].month):
            continue
        target = _shift_year(end_row["date"], years)
        if target < days[0]:
            continue
        start_index = bisect_left(days, target)
        if start_index >= end_index:
            continue
        start_equity, end_equity = _d(ordered[start_index]["equity"]), _d(end_row["equity"])
        elapsed = max((end_row["date"] - days[start_index]).days, 1)
        output.append({
            "window_years": years, "start_date": days[start_index], "end_date": end_row["date"],
            "start_equity": start_equity, "end_equity": end_equity,
            "total_return": end_equity / start_equity - 1,
            "cagr": Decimal(str(float(end_equity / start_equity) ** (365 / elapsed) - 1)),
        })
    return output


def annual_performance(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for row in sorted(rows, key=lambda item: item["date"]):
        grouped.setdefault(row["date"].year, []).append(row)
    output: list[dict[str, Any]] = []
    prior = _d(rows[0]["equity"])
    for year, bucket in grouped.items():
        start_equity, end_equity = prior, _d(bucket[-1]["equity"])
        peak, max_dd = start_equity, Decimal("0")
        for row in bucket:
            equity = _d(row["equity"])
            peak = max(peak, equity)
            max_dd = min(max_dd, equity / peak - 1)
        output.append({
            "year": year, "start_date": bucket[0]["date"], "end_date": bucket[-1]["date"],
            "starting_equity": start_equity, "ending_equity": end_equity,
            "return": end_equity / start_equity - 1, "max_drawdown": max_dd,
        })
        prior = end_equity
    return output


def underwater_statistics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row["date"])
    peak = _d(ordered[0]["equity"])
    peak_day = ordered[0]["date"]
    start: date | None = None
    longest_days = 0
    longest_start: date | None = None
    longest_end: date | None = None
    for row in ordered[1:]:
        equity, day = _d(row["equity"]), row["date"]
        if equity >= peak:
            if start is not None:
                duration = (day - start).days
                if duration > longest_days:
                    longest_days, longest_start, longest_end = duration, start, day
                start = None
            peak, peak_day = equity, day
        elif start is None:
            start = peak_day
    if start is not None:
        duration = (ordered[-1]["date"] - start).days
        if duration > longest_days:
            longest_days, longest_start, longest_end = duration, start, None
    return {
        "longest_underwater_days": longest_days,
        "underwater_start": longest_start,
        "recovery_date": longest_end or "UNRECOVERED_AT_CUTOFF",
    }


def period_performance(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: row["date"])
    output: list[dict[str, Any]] = []
    for label, start, end in FIXED_PERIODS:
        bucket = [row for row in ordered if start <= row["date"] <= end]
        if not bucket:
            continue
        first, last = _d(bucket[0]["equity"]), _d(bucket[-1]["equity"])
        peak, max_dd = first, Decimal("0")
        for row in bucket:
            equity = _d(row["equity"])
            peak = max(peak, equity)
            max_dd = min(max_dd, equity / peak - 1)
        elapsed = max((bucket[-1]["date"] - bucket[0]["date"]).days, 1)
        output.append({
            "period": label, "start_date": bucket[0]["date"], "end_date": bucket[-1]["date"],
            "starting_equity": first, "ending_equity": last,
            "total_return": last / first - 1,
            "cagr": Decimal(str(float(last / first) ** (365 / elapsed) - 1)),
            "max_drawdown": max_dd,
        })
    return output


def trade_stability(details: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    closed = [row for row in details if row.get("exit_date") not in (None, "")]
    ordered = sorted(closed, key=lambda row: (row["exit_date"], row["leg_id"]))
    streak = longest = 0
    for row in ordered:
        if _d(row["net_pnl_or_estimate"]) < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    best = max(ordered, key=lambda row: _d(row["net_pnl_or_estimate"]))
    worst = min(ordered, key=lambda row: _d(row["net_pnl_or_estimate"]))
    return {
        "longest_losing_streak": longest,
        "best_leg_id": best["leg_id"], "best_trade_pnl": best["net_pnl_or_estimate"],
        "worst_leg_id": worst["leg_id"], "worst_trade_pnl": worst["net_pnl_or_estimate"],
    }


def causal_market_regimes(benchmark_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Classify today using only the prior session's trailing-252 benchmark return."""

    ordered = sorted(benchmark_rows, key=lambda row: row["date"])
    values = [_d(row.get("equivalent_500k", row.get("close"))) for row in ordered]
    output: list[dict[str, Any]] = []
    for index, row in enumerate(ordered):
        trailing = None
        if index >= 253:
            trailing = values[index - 1] / values[index - 253] - 1
        label = (
            "WARMUP" if trailing is None else
            "BULL" if trailing >= Decimal("0.10") else
            "BEAR" if trailing <= Decimal("-0.10") else "SIDEWAYS"
        )
        output.append({
            "date": row["date"], "regime": label,
            "prior_session_trailing_252_return": trailing,
            "rule": "BULL>=10%; BEAR<=-10%; otherwise SIDEWAYS; prior-session trailing 252-session return",
        })
    return output


def phase2_analysis(
    phase1: Mapping[str, Sequence[Mapping[str, Any]]],
    benchmark_rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    selected = {
        regime.MOVING_ID: "MOVING_CONTROL",
        regime.FIXED_ID: "FIXED_PRIMARY",
    }
    annual_rows: list[dict[str, Any]] = []
    rolling_rows: list[dict[str, Any]] = []
    period_rows: list[dict[str, Any]] = []
    stability_rows: list[dict[str, Any]] = []
    for strategy_id, role in selected.items():
        curves = [
            row for row in phase1["curves"]
            if row["strategy_id"] == strategy_id
            and row["execution_timing"] == TIMING_NEXT_OPEN
            and int(row["slippage_bps"]) == 20
        ]
        details = [
            row for row in phase1["details"]
            if row["strategy_id"] == strategy_id
            and row["execution_timing"] == TIMING_NEXT_OPEN
            and int(row["slippage_bps"]) == 20
        ]
        prefix = {"strategy_id": strategy_id, "strategy_role": role}
        annual_rows.extend({**prefix, **row} for row in annual_performance(curves))
        rolling_rows.extend({**prefix, **row} for row in rolling_performance(curves))
        period_rows.extend({**prefix, **row} for row in period_performance(curves))
        stability_rows.append({**prefix, **trade_stability(details), **underwater_statistics(curves)})
    return {
        "calendar_year": annual_rows,
        "rolling": rolling_rows,
        "fixed_periods": period_rows,
        "trade_stability": stability_rows,
        "market_regime": causal_market_regimes(benchmark_rows),
    }


def run_concentration_counterfactuals(
    *, package: Any, raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any],
    phase1: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    primary_details = [
        row for row in phase1["details"]
        if row["strategy_id"] == regime.FIXED_ID
        and row["execution_timing"] == TIMING_NEXT_OPEN
        and int(row["slippage_bps"]) == 20
        and row.get("exit_date") not in (None, "")
    ]
    ranked = sorted(primary_details, key=lambda row: _d(row["net_pnl_or_estimate"]), reverse=True)
    indicators = regime.build_indicator_rows(raw_rows, events)
    frozen, _daily, _events = regime.build_regime_run(
        indicator_rows=indicators, raw_by_date=package.raw_by_date,
        strategy_id=regime.FIXED_ID, moving_stop=False,
        start=accepted.FORMAL_START, end=accepted.CUTOFF, slippage_bps=20,
    )
    delayed, _audit = schedule_frozen_orders(
        run=frozen, sessions=package.sessions, raw_by_date=package.raw_by_date,
        indicator_by_date={row["date"]: row for row in indicators}, timing=TIMING_NEXT_OPEN,
    )
    output: list[dict[str, Any]] = []
    baseline = next(
        row for row in phase1["metrics"]
        if row["strategy_id"] == regime.FIXED_ID
        and row["execution_timing"] == TIMING_NEXT_OPEN
        and int(row["slippage_bps"]) == 20
    )
    for count in (0, 1, 3, 5):
        excluded = {row["leg_id"] for row in ranked[:count]}
        if count == 0:
            metric = baseline
        else:
            counterfactual = StrategyRunSlice(
                delayed.spec, delayed.result, delayed.decisions,
                tuple(row for row in delayed.transactions if row.get("leg_id") not in excluded),
                delayed.legs,
            )
            replay = replay_cash_safe_strategy(
                package=package, run=counterfactual, start=accepted.FORMAL_START,
                end=accepted.CUTOFF, slippage_bps=20, initial_capital=CONTINUOUS_CAPITAL,
                budget_mode="all_available", research_slippage=True,
            )
            metric = replay["metrics"]
        output.append({
            "removed_best_trade_count": count,
            "removed_leg_ids": ";".join(sorted(excluded)),
            "ending_equity": metric["ending_equity"], "cagr": metric["cagr"],
            "max_drawdown": metric["max_asset_drawdown"],
            "closed_rounds": metric["closed_rounds"],
            "ending_equity_change_vs_baseline": _d(metric["ending_equity"]) - _d(baseline["ending_equity"]),
        })
    return output


def build_parameter_indicator_rows(
    raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any],
    *, ma_fast: int, ma_slow: int, macd_fast: int, macd_slow: int, macd_signal: int,
) -> list[dict[str, Any]]:
    if not (ma_fast < ma_slow and macd_fast < macd_slow):
        raise ValueError("fast periods must be below slow periods")
    factors: dict[date, Decimal] = {}
    for event in events:
        factor = Decimal("1")
        for value in (event.cash_price_factor, event.share_price_factor, event.rights_price_factor):
            if value is not None:
                factor *= _d(value)
        factors[event.event_date] = factors.get(event.event_date, Decimal("1")) * factor
    closes: list[Decimal] = []
    ema_fast = ema_slow = signal = None
    output: list[dict[str, Any]] = []
    for raw in raw_rows:
        day = date.fromisoformat(str(raw["date"]))
        factor = factors.get(day, Decimal("1"))
        if factor != 1:
            closes = [value * factor for value in closes]
            ema_fast = None if ema_fast is None else ema_fast * factor
            ema_slow = None if ema_slow is None else ema_slow * factor
            signal = None if signal is None else signal * factor
        close, low = _d(raw["close"]), _d(raw["low"])
        closes.append(close)
        ema_fast = moving._ema(ema_fast, close, macd_fast)
        ema_slow = moving._ema(ema_slow, close, macd_slow)
        dif = ema_fast - ema_slow
        signal = moving._ema(signal, dif, macd_signal)
        output.append({
            "date": day, "technical_close": close, "technical_low": low,
            "raw_close": close, "price_rebase_factor": factor,
            "sma21": sum(closes[-ma_fast:], Decimal("0")) / Decimal(ma_fast) if len(closes) >= ma_fast else None,
            "sma144": sum(closes[-ma_slow:], Decimal("0")) / Decimal(ma_slow) if len(closes) >= ma_slow else None,
            "histogram": dif - signal, "dow_events": (),
        })
    return output


def run_parameter_grid(
    *, package: Any, raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    rolling_five: list[dict[str, Any]] = []
    for ma_fast, ma_slow, macd_fast, macd_slow, macd_signal in PARAMETER_GRID:
        params = {
            "ma_fast": ma_fast, "ma_slow": ma_slow,
            "macd_fast": macd_fast, "macd_slow": macd_slow,
            "macd_signal": macd_signal,
        }
        indicators = build_parameter_indicator_rows(raw_rows, events, **params)
        indicator_by_date = {row["date"]: row for row in indicators}
        frozen, _daily, _events = regime.build_regime_run(
            indicator_rows=indicators, raw_by_date=package.raw_by_date,
            strategy_id=regime.FIXED_ID, moving_stop=False,
            start=accepted.FORMAL_START, end=accepted.CUTOFF, slippage_bps=20,
        )
        executable, _audit = schedule_frozen_orders(
            run=frozen, sessions=package.sessions, raw_by_date=package.raw_by_date,
            indicator_by_date=indicator_by_date, timing=TIMING_NEXT_OPEN,
        )
        replay = replay_cash_safe_strategy(
            package=package, run=executable, start=accepted.FORMAL_START,
            end=accepted.CUTOFF, slippage_bps=20, initial_capital=CONTINUOUS_CAPITAL,
            budget_mode="all_available", research_slippage=True,
        )
        annual = annual_performance(replay["curve"])
        rolling = rolling_cagr_series(replay["curve"], 5)
        underwater = underwater_statistics(replay["curve"])
        metric = dict(replay["metrics"])
        max_dd = abs(_d(metric["max_asset_drawdown"]))
        row = {
            **params, "parameter_id": f"MA{ma_fast}_{ma_slow}_MACD{macd_fast}_{macd_slow}_{macd_signal}",
            "ending_equity": metric["ending_equity"], "cagr": metric["cagr"],
            "max_drawdown": metric["max_asset_drawdown"],
            "calmar": _d(metric["cagr"]) / max_dd if max_dd else None,
            "worst_year": min((_d(item["return"]) for item in annual), default=None),
            "profitable_years": sum(_d(item["return"]) > 0 for item in annual),
            "trades": metric["closed_rounds"],
            "actual_cost": _d(metric["actual_fees_and_tax"]) + _d(metric["actual_slippage_cost"]),
            **underwater,
            "rolling_5y_median_cagr": median([_d(item["cagr"]) for item in rolling]),
            "rolling_5y_worst_cagr": min(_d(item["cagr"]) for item in rolling),
        }
        results.append(row)
        rolling_five.extend({"parameter_id": row["parameter_id"], **item} for item in rolling)
    return results, rolling_five


def _quantile(values: Sequence[Decimal], fraction: Decimal) -> Decimal:
    ordered = sorted(values)
    index = int((Decimal(len(ordered) - 1) * fraction).to_integral_value(rounding=ROUND_HALF_UP))
    return ordered[index]


def parameter_robustness(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    axes = (MA_FAST_VALUES, MA_SLOW_VALUES, MACD_FAST_VALUES, MACD_SLOW_VALUES, MACD_SIGNAL_VALUES)
    keyed = {
        tuple(int(row[key]) for key in ("ma_fast", "ma_slow", "macd_fast", "macd_slow", "macd_signal")): row
        for row in rows
    }
    neighborhoods: list[dict[str, Any]] = []
    for params, row in keyed.items():
        positions = tuple(axis.index(value) for axis, value in zip(axes, params))
        neighbors = [
            candidate for candidate_params, candidate in keyed.items()
            if sum(abs(axis.index(value) - position) for axis, value, position in zip(axes, candidate_params, positions)) <= 1
        ]
        cagr_values = [_d(item["cagr"]) for item in neighbors]
        equity_values = [_d(item["ending_equity"]) for item in neighbors]
        neighborhoods.append({
            "parameter_id": row["parameter_id"], "neighbor_count": len(neighbors),
            "neighborhood_median_cagr": median(cagr_values),
            "neighborhood_min_cagr": min(cagr_values), "neighborhood_max_cagr": max(cagr_values),
            "neighborhood_median_ending_equity": median(equity_values),
            "isolated_peak_ratio": _d(row["cagr"]) / median(cagr_values) if median(cagr_values) else None,
        })
    distributions: list[dict[str, Any]] = []
    for metric in ("cagr", "ending_equity", "max_drawdown", "calmar", "rolling_5y_worst_cagr"):
        values = [_d(row[metric]) for row in rows if row.get(metric) not in (None, "")]
        distributions.append({
            "metric": metric, "count": len(values), "min": min(values),
            "p10": _quantile(values, Decimal("0.10")),
            "median": median(values), "p90": _quantile(values, Decimal("0.90")),
            "max": max(values),
        })
    heatmap: list[dict[str, Any]] = []
    for ma_fast in MA_FAST_VALUES:
        for ma_slow in MA_SLOW_VALUES:
            bucket = [row for row in rows if int(row["ma_fast"]) == ma_fast and int(row["ma_slow"]) == ma_slow]
            heatmap.append({
                "ma_fast": ma_fast, "ma_slow": ma_slow, "parameter_count": len(bucket),
                "median_cagr": median([_d(row["cagr"]) for row in bucket]),
                "min_cagr": min(_d(row["cagr"]) for row in bucket),
                "max_cagr": max(_d(row["cagr"]) for row in bucket),
                "median_max_drawdown": median([_d(row["max_drawdown"]) for row in bucket]),
            })
    return neighborhoods, distributions, heatmap


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _markdown_anchor(heading: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", heading.strip().lower(), flags=re.UNICODE)
    return re.sub(r"-+", "-", re.sub(r"\s+", "-", cleaned)).strip("-")


def _broken_markdown_links(root: Path) -> list[str]:
    broken: list[str] = []
    for md in root.glob("*.md"):
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", md.read_text(encoding="utf-8")):
            target_name, marker, fragment = target.partition("#")
            target_path = root / target_name
            if not target_path.is_file():
                broken.append(f"{md.name}->{target}")
                continue
            if marker and target_path.suffix.lower() == ".md":
                headings = {
                    _markdown_anchor(line.lstrip("#").strip())
                    for line in target_path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("#")
                }
                if fragment not in headings:
                    broken.append(f"{md.name}->{target} (missing anchor)")
    return broken


def _readme_missing_artifacts(root: Path, artifact_paths: Sequence[str]) -> list[str]:
    readme = (root / "README.md").read_text(encoding="utf-8")
    targets = {
        target.partition("#")[0]
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme)
    }
    required = {path for path in artifact_paths if path != "README.md"}
    return sorted(required - targets)


def _baseline_parity(phase1: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    files = {
        "metrics": "scenario-summary.csv", "details": "trade-details.csv",
        "transactions": "transactions.csv", "curves": "daily-equity.csv",
        "actions": "corporate-actions.csv",
    }
    strategy_ids = {regime.MOVING_ID, regime.FIXED_ID}
    checks: dict[str, Any] = {}
    for key, name in files.items():
        with (V15_RUN_ROOT / name).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or ())
            expected = [row for row in reader if row["strategy_id"] in strategy_ids]
        actual = [
            row for row in phase1[key]
            if row["strategy_id"] in strategy_ids
            and row["execution_timing"] == TIMING_SIGNAL_CLOSE
            and int(row["slippage_bps"]) == 20
        ]
        normalized_expected = [[normalize_value(row.get(field)) for field in fields] for row in expected]
        normalized_actual = [[normalize_value(row.get(field)) for field in fields] for row in actual]
        exact = normalized_expected == normalized_actual
        checks[name] = {"expected_rows": len(expected), "actual_rows": len(actual), "exact": exact}
        if not exact:
            raise ETF0050Error(f"v1.6 baseline parity failed: {name}")
    return {"status": "PASS_EXACT", "checks": checks, "source_manifest_sha256": V15_RUN_MANIFEST}


def _write_bar_svg(path: Path, rows: Sequence[Mapping[str, Any]], *, label: str, value: str, title: str) -> None:
    width, height, margin = 1100, 520, 80
    values = [_d(row[value]) for row in rows]
    maximum = max(values) if values else Decimal("1")
    bar_width = (width - 2 * margin) / max(len(rows), 1)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="{margin}" y="35" font-size="22">{title}</text>']
    for index, row in enumerate(rows):
        x = margin + index * bar_width + 4
        bar_height = float(_d(row[value]) / maximum) * (height - 150)
        y = height - 90 - bar_height
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(bar_width-8,2):.1f}" height="{bar_height:.1f}" fill="#35618f"/>')
        parts.append(f'<text transform="translate({x+8:.1f},{height-75}) rotate(45)" font-size="9">{row[label]}</text>')
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def _write_heatmap_svg(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    width, height, cell = 700, 520, 120
    values = [_d(row["median_cagr"]) for row in rows]
    low, high = min(values), max(values)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             '<text x="90" y="35" font-size="22">Parameter plateau: median CAGR by MA pair</text>']
    for row in rows:
        x = 130 + MA_SLOW_VALUES.index(int(row["ma_slow"])) * cell
        y = 80 + MA_FAST_VALUES.index(int(row["ma_fast"])) * cell
        ratio = float((_d(row["median_cagr"]) - low) / (high - low)) if high != low else .5
        color = f"rgb({int(230-120*ratio)},{int(240-80*ratio)},{int(250-20*ratio)})"
        parts.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" fill="{color}" stroke="#333"/>')
        parts.append(f'<text x="{x+12}" y="{y+55}" font-size="16">{float(_d(row["median_cagr"]))*100:.2f}%</text>')
        parts.append(f'<text x="{x+12}" y="{y+82}" font-size="12">n={row["parameter_count"]}</text>')
    for index, value_ in enumerate(MA_SLOW_VALUES):
        parts.append(f'<text x="{155+index*cell}" y="465" font-size="14">slow {value_}</text>')
    for index, value_ in enumerate(MA_FAST_VALUES):
        parts.append(f'<text x="35" y="145" font-size="14">fast {value_}</text>')
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def _render_reports(
    *, report_root: Path, phase1_metrics: Sequence[Mapping[str, Any]],
    phase2: Mapping[str, Sequence[Mapping[str, Any]]], parameter_rows: Sequence[Mapping[str, Any]],
    allocation_rows: Sequence[Mapping[str, Any]], allocation_daily: Sequence[Mapping[str, Any]],
    allocation_ledger: Sequence[Mapping[str, Any]], concentration: Sequence[Mapping[str, Any]],
    parity: Mapping[str, Any], produced: str,
) -> None:
    primary = next(row for row in phase1_metrics if row["strategy_id"] == regime.FIXED_ID and row["execution_timing"] == TIMING_NEXT_OPEN and int(row["slippage_bps"]) == 20)
    moving_primary = next(row for row in phase1_metrics if row["strategy_id"] == regime.MOVING_ID and row["execution_timing"] == TIMING_NEXT_OPEN and int(row["slippage_bps"]) == 20)
    baseline = next(row for row in parameter_rows if row["parameter_id"] == "MA21_144_MACD21_55_55")
    role_by_strategy = {regime.FIXED_ID: "FIXED_PRIMARY", regime.MOVING_ID: "MOVING_CONTROL"}
    def year_outcome(metric: Mapping[str, Any], sign: str) -> str:
        rows = [row for row in phase2["calendar_year"] if row["strategy_role"] == role_by_strategy[metric["strategy_id"]]]
        if sign == "positive":
            selected = [str(row["year"]) for row in rows if _d(row["return"]) > 0]
        elif sign == "negative":
            selected = [str(row["year"]) for row in rows if _d(row["return"]) < 0]
        else:
            selected = [str(row["year"]) for row in rows if _d(row["return"]) == 0]
        return f"{len(selected)}/{len(rows)}（{'、'.join(selected) if selected else '無'}）"
    main_summary_table = standard.md_table(
        ("策略", "期末資產", "總報酬", "CAGR", "最大回落", "獲利年度", "虧損年度", "平盤年度", "進場／出場／未平倉", "最大投入"),
        ((row["strategy_label"], standard.money(row["ending_equity"]), standard.pct(row["total_return"]),
          standard.pct(row["cagr"]), standard.pct(row["max_asset_drawdown"]),
          year_outcome(row, "positive"), year_outcome(row, "negative"), year_outcome(row, "flat"),
          f"{row['rounds']}／{row['closed_rounds']}／{row['open_rounds']}",
          standard.money(row["peak_concurrent_investment"]))
         for row in (moving_primary, primary)),
    )
    phase1_table = standard.md_table(
        ("策略", "成交", "bps", "期末資產", "CAGR", "MaxDD", "交易"),
        ((row["strategy_label"], row["execution_timing"], row["slippage_bps"], standard.money(row["ending_equity"]), standard.pct(row["cagr"]), standard.pct(row["max_asset_drawdown"]), row["closed_rounds"]) for row in phase1_metrics),
    )
    capital_table = standard.md_table(
        ("策略", "成交", "bps", "累計買進", "已實現損益", "截止估計損益", "股息", "費稅", "滑價"),
        ((row["strategy_label"], row["execution_timing"], row["slippage_bps"],
          standard.money(row["cumulative_buy"]), standard.money(row["realized_net_pnl"]),
          standard.money(row["cutoff_estimated_pnl"]), standard.money(row["dividends_received"]),
          standard.money(row["actual_fees_and_tax"]), standard.money(row["actual_slippage_cost"]))
         for row in phase1_metrics),
    )
    year_table = standard.md_table(
        ("策略", "年度", "年初資產", "年末資產", "報酬", "MaxDD"),
        ((row["strategy_role"], row["year"], standard.money(row["starting_equity"]),
          standard.money(row["ending_equity"]), standard.pct(row["return"]),
          standard.pct(row["max_drawdown"])) for row in phase2["calendar_year"]),
    )
    fixed_table = standard.md_table(
        ("策略", "期間", "期初", "期末", "CAGR", "MaxDD"),
        ((row["strategy_role"], row["period"], standard.money(row["starting_equity"]),
          standard.money(row["ending_equity"]), standard.pct(row["cagr"]),
          standard.pct(row["max_drawdown"])) for row in phase2["fixed_periods"]),
    )
    rolling_summary = []
    for role in ("FIXED_PRIMARY", "MOVING_CONTROL"):
        for window in (3, 5, 10):
            bucket = [row for row in phase2["rolling"]
                      if row["strategy_role"] == role and int(row["window_years"]) == window]
            rolling_summary.append({"role": role, "window": window, "count": len(bucket),
                                    "median": median([_d(row["cagr"]) for row in bucket]),
                                    "worst": min(_d(row["cagr"]) for row in bucket),
                                    "best": max(_d(row["cagr"]) for row in bucket),
                                    "worst_dd": min(_d(row["max_drawdown"]) for row in bucket)})
    rolling_table = standard.md_table(
        ("策略", "視窗", "觀測數", "CAGR 中位", "最差", "最佳", "最差 MaxDD"),
        ((row["role"], f"{row['window']} 年", row["count"], standard.pct(row["median"]),
          standard.pct(row["worst"]), standard.pct(row["best"]), standard.pct(row["worst_dd"]))
         for row in rolling_summary),
    )
    stability_table = standard.md_table(
        ("策略", "最長連虧", "最佳回合／損益", "最差回合／損益", "最長水下天數", "水下期間"),
        ((row["strategy_role"], row["longest_losing_streak"],
          f"{row['best_leg_id']}／{standard.money(row['best_trade_pnl'])}",
          f"{row['worst_leg_id']}／{standard.money(row['worst_trade_pnl'])}",
          row["longest_underwater_days"], f"{row['underwater_start']}～{row['recovery_date']}")
         for row in phase2["trade_stability"]),
    )
    concentration_table = standard.md_table(
        ("移除最佳回合數", "固定原訊號移除回合", "期末資產", "CAGR", "變化"),
        ((row["removed_best_trade_count"], row.get("removed_leg_ids") or "無",
          standard.money(row["ending_equity"]), standard.pct(row["cagr"]),
          standard.money(row["ending_equity_change_vs_baseline"])) for row in concentration),
    )
    neighborhoods, distributions, _heatmap = parameter_robustness(parameter_rows)
    center_neighborhood = next(row for row in neighborhoods if row["parameter_id"] == "MA21_144_MACD21_55_55")
    center_tuple = (21, 144, 21, 55, 55)
    axes = (MA_FAST_VALUES, MA_SLOW_VALUES, MACD_FAST_VALUES, MACD_SLOW_VALUES, MACD_SIGNAL_VALUES)
    def local_distance(row: Mapping[str, Any]) -> int:
        values = tuple(int(row[key]) for key in ("ma_fast", "ma_slow", "macd_fast", "macd_slow", "macd_signal"))
        return sum(abs(axis.index(value) - axis.index(center)) for axis, value, center in zip(axes, values, center_tuple))
    local_rows = sorted((row for row in parameter_rows if local_distance(row) <= 1), key=lambda row: row["parameter_id"])
    local_table = standard.md_table(
        ("中心局部組", "期末資產", "CAGR", "MaxDD", "rolling 5Y 最差"),
        ((row["parameter_id"], standard.money(row["ending_equity"]), standard.pct(row["cagr"]),
          standard.pct(row["max_drawdown"]), standard.pct(row["rolling_5y_worst_cagr"])) for row in local_rows),
    )
    distribution_table = standard.md_table(
        ("指標", "n", "min", "p10", "median", "p90", "max"),
        ((row["metric"], row["count"], row["min"], row["p10"], row["median"], row["p90"], row["max"])
         for row in distributions),
    )
    allocation_table = standard.md_table(
        ("配置", "期末資產", "CAGR", "MaxDD", "core 最低現金", "tactical 最低現金", "調整次數", "調整成本"),
        ((row["allocation_id"], standard.money(row["ending_equity"]), standard.pct(row["cagr"]),
          standard.pct(row["max_drawdown"]), standard.money(row.get("minimum_core_cash")),
          standard.money(row.get("minimum_tactical_cash")), row.get("rebalance_events") or "—",
          standard.money(row.get("rebalance_cost"))) for row in allocation_rows),
    )
    annual_ledger = [row for row in allocation_ledger if str(row.get("allocation_id", "")).endswith("_ANNUAL")]
    ledger_counts = {role: sum(row.get("role") == role for row in annual_ledger) for role in (
        "ANNUAL_REBALANCE_SELL", "ANNUAL_CASH_TRANSFER", "ANNUAL_CORE_BUY",
        "TACTICAL_ENTRY", "TACTICAL_EXIT", "DIVIDEND_CASH_PAID", "SPLIT_REBASE_ONLY",
    )}
    annual_final = []
    for row in allocation_rows:
        if not str(row["allocation_id"]).endswith("_ANNUAL"):
            continue
        last = next(item for item in reversed(allocation_daily) if item["allocation_id"] == row["allocation_id"])
        annual_final.append((row["allocation_id"], last["core_cash"], last["core_shares"],
                             last["tactical_cash"], last["tactical_shares"], last["equity"]))
    final_holdings_table = standard.md_table(
        ("配置", "core 現金", "core 股數", "tactical 現金", "tactical 股數", "總資產"),
        ((item[0], standard.money(item[1]), item[2], standard.money(item[3]), item[4], standard.money(item[5]))
         for item in annual_final),
    )
    attachments = [
        "phase1-scenario-summary.csv", "trade-details.md", "trade-details.csv", "transactions.csv", "daily-equity.csv",
        "phase1-gap-cost-summary.csv",
        "calendar-year.csv", "rolling-3-5-10.csv", "fixed-periods.csv", "market-regime.csv",
        "trade-stability.csv", "concentration.csv", "parameter-results-243.csv",
        "parameter-rolling-5y.csv", "parameter-neighborhoods.csv", "parameter-distribution.csv",
        "parameter-heatmap.csv", "allocation-summary.csv", "allocation-daily-equity.csv",
        "allocation-sleeve-ledger.csv", "corporate-actions.csv", "benchmark.csv",
        "baseline-v1-6-parity.json",
        "original-seven-context.csv", "execution-audit.csv", "input-manifest.json",
        "artifact-manifest.json", "readback.json", "checkpoint.json", "phase1-ending-equity.svg",
        "parameter-heatmap.svg", "allocation-frontier.svg",
    ]
    links = "\n".join(f"- [{name}]({name})" for name in attachments)
    readme = "\n".join([
        "# 0050 四階段研究 v1.5｜閱讀目錄", "", f"產出時間：{produced}", "",
        "狀態：`STOP_WAIT_00_QA`。v1.5 是隔離候選；正式總目錄仍以 v1.6 ACCEPT 為準。", "",
        "## 主報告", "",
        "1. [來源譜系](report.md#來源譜系)",
        "2. [策略完整定義](report.md#策略完整定義)",
        "3. [主摘要](report.md#主摘要)",
        "4. [方法、來源與因果順序](report.md#方法來源與因果順序)",
        "5. [Phase 1｜18 種成交情境](report.md#phase-118-種成交情境)",
        "6. [Phase 2｜逐年、rolling 與集中度](report.md#phase-2逐年rolling-與集中度)",
        "7. [Phase 3｜243 組與中心局部鄰域](report.md#phase-3243-組與中心局部鄰域)",
        "8. [Phase 4｜混合配置真實雙袖套帳本](report.md#phase-4混合配置真實雙袖套帳本)",
        "9. [限制與 QA 附錄](report.md#限制與-qa-附錄)", "",
        "## 附件", "", links, "",
    ])
    report = "\n".join([
        "# 0050 成交、時間、參數與混合持有研究 v1.5｜完整主報告", "", f"產出時間：{produced}", "",
        "狀態：`STOP_WAIT_00_QA`。本候選未被採納；既有七組 v1.5 source run、v1.6 正式報告、v1.1／v1.2／v1.3／v1.4 退件候選均保持唯讀。", "",
        "## 來源譜系", "",
        "Phase 1～3 的兩個研究策略由唯讀的既有七組 v1.5 source run 與同一封閉資料包重播；訊號日收盤 20bps 五份核心資料對 v1.6 維持逐列 exact parity。v1.6 是目前唯一 ACCEPT 的正式報告；本四階段 v1.5 僅是修正後候選。", "",
        "## 策略完整定義", "",
        "1. **分流移動版**：當日 SMA21≥SMA144（等號歸高 regime）時，只在前一日 technical close≤前一日 SMA21、且當日 technical close>當日 SMA21 的 MA21 新突破進場；當日 SMA21<SMA144 時，只在 MACD histogram 前一日≤0、當日>0 的綠翻紅進場。高 regime 出場使用移動道氏防線優先、否則 armed 後 MACD 紅翻綠；低 regime 價格防線只維護不執行，只允許 armed 後 MACD 出場。",
        "2. **分流固定版**：使用完全相同、互斥的 regime 與進場交叉：SMA21≥SMA144 採上述 MA21 新突破，SMA21<SMA144 採上述 MACD 綠翻紅。高 regime 出場使用進場時固定初始防守優先、否則 armed 後 MACD；低 regime 固定防守只維護不執行，只允許 armed 後 MACD 出場。",
        "共同規則：MACD 綠翻紅進場因當日 histogram>0，入場當日立即 armed；MA21 新突破進場若當日 histogram≥0（含 0）即 armed，否則持有後第一次觀察到 histogram≥0 才 armed。armed 後的 MACD 出場精確定義為前一日 histogram≥0、當日<0。持倉期間每天依當日 SMA21／SMA144 重新切換高／低 regime 的 stop 執行權；已有持股不加碼、賣出當日不重進、停損與 MACD 同日成立時停損優先、截止仍持有不強平。主摘要採可實現的 t+1 open、買賣各 20bps，另扣手續費與 ETF 賣出稅。", "",
        "## 主摘要", "", main_summary_table, "",
        "表中最大投入是單一時點最高持倉成本，不是新增本金；進場／出場／未平倉是完整帳戶事件數。", "",
        "## 方法、來源與因果順序", "",
        "期間 2006-01-02～2026-09-30，2005 僅暖機；初始本金 500,000 元，不逐年重設。所有訊號只使用當時可得資料。",
        "Phase 1 的 A 是訊號日收盤 exact-parity 對照，B 是 t+1 開盤主口徑，C 是 t+1 收盤；買賣各測 20/40/60bps 並另扣費稅。",
        "Phase 4 年度敏感度不是 NAV 指數縮放，而是逐日真實雙袖套帳本。每日順序固定為：split → 除息資格 → payment day 先入現金 → 下一官方交易日 open 才由 core 使用可用現金再投入；tactical t+1 開盤退出／進場依各自訊號執行。每年首個官方交易日的年度 rebalance 另在該日 close 完成。",
        "年度調整先用 donor 現金；不足才按當日原始收盤價賣出最少整股並課實際滑價、手續費、ETF 稅。現金轉帳本身不課稅。core 收款後可於同收盤價買入；tactical 收款只留現金，既有持股不加碼，等下一個 frozen 合規進場才使用。", "",
        f"v1.6 baseline parity：`{parity['status']}`。", "",
        "## Phase 1｜18 種成交情境", "", phase1_table, "",
        f"固定分流版 t+1 開盤 20bps 主口徑：期末 {standard.money(primary['ending_equity'])}，CAGR {standard.pct(primary['cagr'])}，MaxDD {standard.pct(primary['max_asset_drawdown'])}。時間與成本惡化並非線性，應以整張表而非單一最佳格判讀。", "",
        "![18 種情境期末資產](phase1-ending-equity.svg)", "",
        "### 資金、交易與損益", "", capital_table, "",
        "## Phase 2｜逐年、rolling 與集中度", "",
        f"共有 {len(phase2['calendar_year'])} 列完整年度結果與 {len(phase2['rolling'])} 個月末 rolling 觀測。", "",
        "### 完整逐年表", "", year_table, "",
        "### 固定三段", "", fixed_table, "",
        "### Rolling 3／5／10 年摘要", "", rolling_table, "",
        "### 交易與水下穩定性", "", stability_table, "",
        "### 獲利集中度壓力測試", "", concentration_table, "",
        "這不是重新生成訊號的完整反事實。它固定原始訊號與成交順序，只移除原序列中最佳 1／3／5 個成交回合，再以同一現金安全規則重播；跳過交易可能創造的新進場機會不會被重新發現，因此只能解讀為『固定原訊號序列、移除最佳成交回合的獲利集中度壓力測試』。", "",
        "Market regime 使用前一交易日 trailing 252-session benchmark return：>=10% bull、<=-10% bear、其餘 sideways；原始逐日分類見附件。", "",
        "## Phase 3｜243 組與中心局部鄰域", "",
        f"243/243 組完成。現行 21/144 + MACD(21,55,55)：期末 {standard.money(baseline['ending_equity'])}、CAGR {standard.pct(baseline['cagr'])}、MaxDD {standard.pct(baseline['max_drawdown'])}、rolling 5 年最差 {standard.pct(baseline['rolling_5y_worst_cagr'])}。",
        f"中心的一軸一步局部鄰域共 {center_neighborhood['neighbor_count']} 組；中位 CAGR {standard.pct(center_neighborhood['neighborhood_median_cagr'])}，範圍 {standard.pct(center_neighborhood['neighborhood_min_cagr'])}～{standard.pct(center_neighborhood['neighborhood_max_cagr'])}，isolated-peak ratio {center_neighborhood['isolated_peak_ratio']}。", "",
        local_table, "", "### 全 243 組分布", "", distribution_table, "",
        "樣本內最高參數不能直接取代現行參數；本節目的是看平台寬度與中心是否孤峰。", "",
        "![參數平台](parameter-heatmap.svg)", "",
        "## Phase 4｜混合配置真實雙袖套帳本", "", allocation_table, "",
        "不再平衡是主分析；年度調整只作敏感度。較高 core 比例在此樣本提高期末值，也明顯加深回落，不能稱為單一最佳配置。", "",
        "### 年度配置截止持股與現金", "", final_holdings_table, "",
        f"年度帳本事件數：現金轉帳 {ledger_counts['ANNUAL_CASH_TRANSFER']}、必要賣出 {ledger_counts['ANNUAL_REBALANCE_SELL']}、core 買入 {ledger_counts['ANNUAL_CORE_BUY']}、tactical 進／出 {ledger_counts['TACTICAL_ENTRY']}／{ledger_counts['TACTICAL_EXIT']}、股息發放 {ledger_counts['DIVIDEND_CASH_PAID']}、split {ledger_counts['SPLIT_REBASE_ONLY']}。",
        "`allocation-sleeve-ledger.csv` 可由實際交易、股息與 split 重建每個袖套；`allocation-daily-equity.csv` 每日同時列出 core/tactical 的 shares、cash、liquidation value 與 equity。", "",
        "![配置 frontier](allocation-frontier.svg)", "",
        "## 限制與 QA 附錄", "",
        "- 單一歷史樣本、參數網格與配置 frontier 不能證明未來正期望；需要樣本外與替代市場驗證。",
        "- concentration 是固定原訊號序列壓力測試，不會重生被移除交易後的新訊號。",
        "- 年度調整在首個官方交易日收盤執行；tactical 訊號成交在該日開盤，故年度調整不會回頭改變當日策略成交。",
        "- 全樹 `tests/backtests/dual_ma -m \"not end_to_end\"` 的既有 5 個 failure 均由外部 SPEC_REPO commit pin 觸發；traceback 顯示目前外部 HEAD 為 `3bd43797...`。這些 failure 與本模組無關，但未隱藏、未改成通過。",
        "- v1.5 驗證 core 在 payment day 先認列現金、下一交易日才再投入；8 組配置的兩個袖套都能由正規化交易／股息／split 流水逐日重建 cash、shares、liquidation value 與 equity。",
        "- 全現金 donor 測試要求不產生 ETF 賣出與 sell tax；有持股 donor 測試要求實際整股賣出並遞減股數。", "",
        "## 完整附件", "", links, "",
        "## 精確重播與測試", "", "```powershell",
        "python -m backtests.dual_ma.etf_0050_execution_stability_v1 run",
        "python -m backtests.dual_ma.etf_0050_execution_stability_v1 validate",
        "python -m pytest tests/backtests/dual_ma/test_etf_0050_execution_stability_v1.py tests/backtests/dual_ma/test_etf_0050_formal_v2.py tests/backtests/dual_ma/test_costs.py -q",
        "```", "",
    ])
    (report_root / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    (report_root / "report.md").write_text(report, encoding="utf-8", newline="\n")
    primary_details = standard.read_csv(report_root / "trade-details.csv")
    detail_table = standard.md_table(
        ("leg", "進場", "出場", "淨損益", "報酬", "原因"),
        ((row["leg_id"], row["entry_date"], row["exit_date"] or "未平倉", standard.money(row["net_pnl_or_estimate"]), standard.pct(row["return_rate_or_estimate"]), row["exit_reason"] or "截止估值") for row in primary_details if row["strategy_id"] == regime.FIXED_ID and row["execution_timing"] == TIMING_NEXT_OPEN and int(row["slippage_bps"]) == 20),
    )
    (report_root / "trade-details.md").write_text("# 主口徑逐筆交易\n\n" + detail_table + "\n", encoding="utf-8", newline="\n")


def run_study(*, run_root: Path = RUN_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT) -> dict[str, Any]:
    if run_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite isolated research output")
    if _file_sha256(V15_RUN_ROOT / "manifest.json") != V15_RUN_MANIFEST:
        raise ETF0050Error("v1.5 source run manifest changed")
    if _file_sha256(V16_REPORT_ROOT / "artifact-manifest.json") != V16_REPORT_MANIFEST:
        raise ETF0050Error("v1.6 accepted report manifest changed")
    for path, expected in (
        (V14_RUN_ROOT / "manifest.json", V14_RUN_MANIFEST),
        (V14_REPORT_ROOT / "artifact-manifest.json", V14_REPORT_MANIFEST),
        (V13_RUN_ROOT / "manifest.json", V13_RUN_MANIFEST),
        (V13_REPORT_ROOT / "artifact-manifest.json", V13_REPORT_MANIFEST),
        (V12_RUN_ROOT / "manifest.json", V12_RUN_MANIFEST),
        (V12_REPORT_ROOT / "artifact-manifest.json", V12_REPORT_MANIFEST),
        (V11_RUN_ROOT / "manifest.json", V11_RUN_MANIFEST),
        (V11_REPORT_ROOT / "artifact-manifest.json", V11_REPORT_MANIFEST),
        (PREVIOUS_CANDIDATE_RUN_ROOT / "manifest.json", PREVIOUS_CANDIDATE_RUN_MANIFEST),
        (PREVIOUS_CANDIDATE_REPORT_ROOT / "artifact-manifest.json", PREVIOUS_CANDIDATE_REPORT_MANIFEST),
    ):
        if _file_sha256(path) != expected:
            raise ETF0050Error(f"previous candidate pin changed: {path}")
    protected_paths = (
        V15_RUN_ROOT, V16_REPORT_ROOT, V14_RUN_ROOT, V14_REPORT_ROOT,
        V13_RUN_ROOT, V13_REPORT_ROOT,
        V12_RUN_ROOT, V12_REPORT_ROOT, V11_RUN_ROOT, V11_REPORT_ROOT,
        PREVIOUS_CANDIDATE_RUN_ROOT, PREVIOUS_CANDIDATE_REPORT_ROOT,
    )
    protected_before = {str(path): tree_hash(path) for path in protected_paths}
    package, raw_rows, events = accepted.load_package()
    phase1, execution_audit = run_phase1(package=package, raw_rows=raw_rows, events=events)
    gap_cost_summary = phase1_gap_cost_summary(phase1, execution_audit)
    parity = _baseline_parity(phase1)
    benchmark = standard.read_csv(V15_RUN_ROOT / "benchmark.csv")
    phase2 = phase2_analysis(phase1, benchmark)
    concentration = run_concentration_counterfactuals(
        package=package, raw_rows=raw_rows, events=events, phase1=phase1,
    )
    parameter_rows, parameter_rolling = run_parameter_grid(
        package=package, raw_rows=raw_rows, events=events,
    )
    neighborhoods, distributions, heatmap = parameter_robustness(parameter_rows)
    allocation_rows, allocation_daily, allocation_ledger = run_allocation_study(
        package=package, raw_rows=raw_rows, events=events,
    )
    allocation_rows = enrich_allocation_summaries(allocation_rows, allocation_daily, benchmark)
    protected_after_compute = {str(path): tree_hash(path) for path in protected_paths}
    if protected_before != protected_after_compute:
        raise ETF0050Error("accepted/source trees changed during computation")
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2],
        text=True, encoding="utf-8",
    ).strip()
    produced_iso = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
    produced_text = datetime.fromisoformat(produced_iso).strftime("%Y-%m-%d %H:%M:%S（台北）")
    with tempfile.TemporaryDirectory(prefix="0050-four-phase-v1-") as temp_name:
        root = Path(temp_name)
        run_stage, report_stage = root / "run", root / "report"
        run_stage.mkdir(); report_stage.mkdir()
        datasets = {
            "phase1-scenario-summary.csv": phase1["metrics"],
            "trade-details.csv": phase1["details"], "transactions.csv": phase1["transactions"],
            "daily-equity.csv": phase1["curves"], "corporate-actions.csv": phase1["actions"],
            "execution-audit.csv": execution_audit,
            "phase1-gap-cost-summary.csv": gap_cost_summary,
            "calendar-year.csv": phase2["calendar_year"], "rolling-3-5-10.csv": phase2["rolling"],
            "fixed-periods.csv": phase2["fixed_periods"], "trade-stability.csv": phase2["trade_stability"],
            "market-regime.csv": phase2["market_regime"], "concentration.csv": concentration,
            "parameter-results-243.csv": parameter_rows, "parameter-rolling-5y.csv": parameter_rolling,
            "parameter-neighborhoods.csv": neighborhoods, "parameter-distribution.csv": distributions,
            "parameter-heatmap.csv": heatmap, "allocation-summary.csv": allocation_rows,
            "allocation-daily-equity.csv": allocation_daily, "allocation-sleeve-ledger.csv": allocation_ledger,
        }
        for name, rows in datasets.items():
            _write_csv(run_stage / name, rows)
        shutil.copy2(V15_RUN_ROOT / "benchmark.csv", run_stage / "benchmark.csv")
        shutil.copy2(V15_RUN_ROOT / "scenario-summary.csv", run_stage / "original-seven-context.csv")
        _write_json(run_stage / "baseline-v1-6-parity.json", parity)
        protected_after = {str(path): tree_hash(path) for path in protected_paths}
        if protected_before != protected_after:
            raise ETF0050Error("accepted/source trees changed while staging")
        input_manifest = {
            "status": "PASS", "runner_version": RUNNER_VERSION, "produced_at_taipei": produced_iso,
            "git_commit": git_commit, "source_v15_run_root": str(V15_RUN_ROOT),
            "source_v15_run_manifest_sha256": V15_RUN_MANIFEST,
            "accepted_v16_report_root": str(V16_REPORT_ROOT),
            "accepted_v16_report_manifest_sha256": V16_REPORT_MANIFEST,
            "period": {"warmup_from": "2005-01-03", "formal_from": "2006-01-02", "through": "2026-09-30"},
            "initial_capital": "500000", "annual_reset": False,
            "phase1": {"timings": EXECUTION_TIMINGS, "slippage_bps": SLIPPAGE_BPS, "primary": "NEXT_SESSION_OPEN_20BPS"},
            "phase2": {"rolling_windows_years": [3, 5, 10], "rolling_observation": "calendar month final official session", "periods": FIXED_PERIODS, "market_regime_rule": "prior-session trailing 252 return: bull>=10%, bear<=-10%, otherwise sideways"},
            "phase3": {"factorial_count": 243, "ma_fast": MA_FAST_VALUES, "ma_slow": MA_SLOW_VALUES, "macd_fast": MACD_FAST_VALUES, "macd_slow": MACD_SLOW_VALUES, "macd_signal": MACD_SIGNAL_VALUES},
            "phase4": {"core_weights_pct": ALLOCATION_WEIGHTS, "no_rebalance_primary": True, "annual_rebalance_sensitivity": "first official session close"},
            "protected_tree_hashes_before": protected_before, "protected_tree_hashes_after": protected_after,
        }
        _write_json(run_stage / "input-manifest.json", input_manifest)
        run_excluded = {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"}
        run_artifacts = _artifact_rows(run_stage, run_excluded)
        _write_json(run_stage / "manifest.json", {
            "status": "STOP_WAIT_00_QA", "runner_version": RUNNER_VERSION,
            "produced_at_taipei": produced_iso, "git_commit": git_commit,
            "artifact_count": len(run_artifacts), "artifacts": run_artifacts,
        })
        run_bad = _verify_artifacts(run_stage, run_artifacts)
        _write_json(run_stage / "readback.json", {"status": "PASS" if not run_bad else "FAIL", "mismatches": run_bad})
        _write_json(run_stage / "QA_READY.json", {"status": "STOP_WAIT_00_QA", "ready_for_claimed_acceptance": False})
        _write_json(run_stage / "checkpoint.json", {
            "status": "STOP_WAIT_00_QA", "manifest_sha256": _file_sha256(run_stage / "manifest.json"),
            "readback_sha256": _file_sha256(run_stage / "readback.json"),
            "safe_next_step": "00 independent four-phase research QA only",
        })
        if run_bad:
            raise ETF0050Error(f"run readback failed: {run_bad}")
        for path in run_stage.iterdir():
            if path.is_file() and path.name not in {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"}:
                shutil.copy2(path, report_stage / path.name)
        _write_bar_svg(report_stage / "phase1-ending-equity.svg", phase1["metrics"], label="scenario_id", value="ending_equity", title="Phase 1 ending equity")
        _write_heatmap_svg(report_stage / "parameter-heatmap.svg", heatmap)
        _write_bar_svg(report_stage / "allocation-frontier.svg", allocation_rows, label="allocation_id", value="ending_equity", title="Allocation ending equity frontier")
        _render_reports(
            report_root=report_stage, phase1_metrics=phase1["metrics"], phase2=phase2,
            parameter_rows=parameter_rows, allocation_rows=allocation_rows,
            allocation_daily=allocation_daily, allocation_ledger=allocation_ledger,
            concentration=concentration,
            parity=parity, produced=produced_text,
        )
        shutil.copy2(run_stage / "input-manifest.json", report_stage / "input-manifest.json")
        for planned in ("artifact-manifest.json", "readback.json", "checkpoint.json"):
            _write_json(report_stage / planned, {"status": "BUILDING"})
        broken = _broken_markdown_links(report_stage)
        if broken:
            raise ETF0050Error(f"broken report links: {broken}")
        report_excluded = {"artifact-manifest.json", "readback.json", "checkpoint.json"}
        report_artifacts = _artifact_rows(report_stage, report_excluded)
        missing_from_readme = _readme_missing_artifacts(
            report_stage, [str(row["path"]) for row in report_artifacts],
        )
        if missing_from_readme:
            raise ETF0050Error(f"README artifact coverage incomplete: {missing_from_readme}")
        _write_json(report_stage / "artifact-manifest.json", {
            "status": "STOP_WAIT_00_QA", "report_version": RUNNER_VERSION + "-report",
            "produced_at_taipei": produced_iso, "source_run_manifest_sha256": _file_sha256(run_stage / "manifest.json"),
            "artifact_count": len(report_artifacts), "artifacts": report_artifacts,
        })
        report_bad = _verify_artifacts(report_stage, report_artifacts)
        _write_json(report_stage / "readback.json", {"status": "PASS" if not report_bad else "FAIL", "mismatches": report_bad})
        _write_json(report_stage / "checkpoint.json", {
            "status": "STOP_WAIT_00_QA", "artifact_manifest_sha256": _file_sha256(report_stage / "artifact-manifest.json"),
            "readback_sha256": _file_sha256(report_stage / "readback.json"),
            "safe_next_step": "00 independent report and data QA only",
        })
        if report_bad:
            raise ETF0050Error(f"report readback failed: {report_bad}")
        run_root.parent.mkdir(parents=True, exist_ok=True)
        report_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(run_stage, run_root)
        shutil.copytree(report_stage, report_root)
    return {
        "status": "STOP_WAIT_00_QA", "run_root": str(run_root), "report_root": str(report_root),
        "run_manifest_sha256": _file_sha256(run_root / "manifest.json"),
        "run_readback_sha256": _file_sha256(run_root / "readback.json"),
        "report_manifest_sha256": _file_sha256(report_root / "artifact-manifest.json"),
        "report_readback_sha256": _file_sha256(report_root / "readback.json"),
        "baseline_parity": parity["status"], "parameter_count": len(parameter_rows),
        "broken_links": [], "protected_trees_unchanged": True,
    }


def deep_validate(run_root: Path = RUN_ROOT_DEFAULT) -> dict[str, Any]:
    package, _raw_rows, _events = accepted.load_package()
    metrics = standard.read_csv(run_root / "phase1-scenario-summary.csv")
    transactions = standard.read_csv(run_root / "transactions.csv")
    curves = standard.read_csv(run_root / "daily-equity.csv")
    actions = standard.read_csv(run_root / "corporate-actions.csv")
    audit = standard.read_csv(run_root / "execution-audit.csv")
    next_session = {package.sessions[index]: package.sessions[index + 1] for index in range(len(package.sessions) - 1)}
    timing_errors = 0
    for row in audit:
        if row["status"] != "SCHEDULED":
            continue
        signal_day, fill_day = date.fromisoformat(row["signal_date"]), date.fromisoformat(row["fill_date"])
        expected_day = signal_day if row["timing"] == TIMING_SIGNAL_CLOSE else next_session[signal_day]
        expected_price = (
            package.raw_by_date[fill_day].open
            if row["timing"] == TIMING_NEXT_OPEN
            else package.raw_by_date[fill_day].close
        )
        if fill_day != expected_day or _d(row["fill_reference_price"]) != expected_price:
            timing_errors += 1
    equation_errors = 0
    liquidation_errors = 0
    transaction_math_errors = 0
    reconstructed_errors = 0
    for metric in metrics:
        key = (metric["strategy_id"], metric["execution_timing"], int(metric["slippage_bps"]))
        model = CATHAY_WEB_028_TW_V1.with_research_slippage(key[2])
        tx_rows = [row for row in transactions if (row["strategy_id"], row["execution_timing"], int(row["slippage_bps"])) == key]
        curve_rows = [row for row in curves if (row["strategy_id"], row["execution_timing"], int(row["slippage_bps"])) == key]
        action_rows = [row for row in actions if (row["strategy_id"], row["execution_timing"], int(row["slippage_bps"])) == key]
        tx_by_day: dict[date, list[Mapping[str, Any]]] = {}
        for row in tx_rows:
            tx_by_day.setdefault(date.fromisoformat(row["date"]), []).append(row)
            expected = (
                model.buy(_d(row["reference_price"]), int(row["shares"]))
                if row["role"] == "MOTHER"
                else model.sell(_d(row["reference_price"]), int(row["shares"]), InstrumentType.ETF)
            )
            if any(_d(row[field]) != getattr(expected, field) for field in ("fill_price", "gross", "commission", "tax", "cash_amount")):
                transaction_math_errors += 1
        action_by_day: dict[date, list[Mapping[str, Any]]] = {}
        for row in action_rows:
            day_text = row.get("date") if row.get("action_kind") == "SPLIT" else row.get("payment_date") if row.get("stage") == "CASH_PAID" else ""
            if day_text:
                action_by_day.setdefault(date.fromisoformat(day_text), []).append(row)
        cash, shares = CONTINUOUS_CAPITAL, 0
        for row in curve_rows:
            day = date.fromisoformat(row["date"])
            for action in action_by_day.get(day, ()):
                if action["action_kind"] == "SPLIT":
                    shares = int(action["shares_after"])
                elif action.get("stage") == "CASH_PAID":
                    cash += _d(action["cash"])
            for tx in sorted(tx_by_day.get(day, ()), key=lambda item: 0 if item["role"] == "EXIT" else 1):
                if tx["role"] == "EXIT":
                    cash += _d(tx["cash_amount"]); shares -= int(tx["shares"])
                else:
                    cash -= _d(tx["cash_amount"]); shares += int(tx["shares"])
            expected_liquidation = model.sell(_d(row["raw_close"]), shares, InstrumentType.ETF).cash_amount if shares else Decimal("0")
            if _d(row["equity"]) != _d(row["cash"]) + _d(row["liquidation_value"]):
                equation_errors += 1
            if _d(row["liquidation_value"]) != expected_liquidation:
                liquidation_errors += 1
            if cash != _d(row["cash"]) or shares != int(row["shares"]):
                reconstructed_errors += 1
    allocation = standard.read_csv(run_root / "allocation-daily-equity.csv")
    allocation_errors = sum(
        _d(row["equity"]) != _d(row["core_equity"]) + _d(row["tactical_equity"])
        or _d(row["cash"]) != _d(row["core_cash"]) + _d(row["tactical_cash"])
        for row in allocation
    )
    pre_rebalance_mismatches = 0
    parity_fields = (
        "core_cash", "core_shares", "core_equity",
        "tactical_cash", "tactical_shares", "tactical_equity", "equity",
    )
    for core_pct in ALLOCATION_WEIGHTS:
        plain_id = f"CORE_{core_pct}_TACTICAL_{100-core_pct}_NO_REBALANCE"
        annual_id = f"CORE_{core_pct}_TACTICAL_{100-core_pct}_ANNUAL"
        plain = {row["date"]: row for row in allocation if row["allocation_id"] == plain_id and row["date"].startswith("2006-")}
        annual = {row["date"]: row for row in allocation if row["allocation_id"] == annual_id and row["date"].startswith("2006-")}
        for day in set(plain) | set(annual):
            if day not in plain or day not in annual or any(
                _d(plain[day][key]) != _d(annual[day][key]) for key in parity_fields
            ):
                pre_rebalance_mismatches += 1
    ledger = standard.read_csv(run_root / "allocation-sleeve-ledger.csv")
    rebalance_conservation_errors = sum(
        abs(_d(row["conservation_difference"])) > Decimal("0.0000001")
        for row in ledger if row.get("role") == "ANNUAL_REBALANCE_RECONCILIATION"
    )
    allocation_reconstruction_errors = 0
    allocation_liquidation_errors = 0
    allocation_ledger_schema_errors = 0
    cash_donor_sell_tax_errors = 0
    model20 = CATHAY_WEB_028_TW_V1.with_research_slippage(20)
    for core_pct in ALLOCATION_WEIGHTS:
        for suffix in ("NO_REBALANCE", "ANNUAL"):
            allocation_id = f"CORE_{core_pct}_TACTICAL_{100-core_pct}_{suffix}"
            state = {
                "CORE": {"cash": CONTINUOUS_CAPITAL * Decimal(core_pct) / Decimal("100"), "shares": 0},
                "TACTICAL": {"cash": CONTINUOUS_CAPITAL * Decimal(100-core_pct) / Decimal("100"), "shares": 0},
            }
            rows_by_day: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
            for event in ledger:
                if event.get("allocation_id") != allocation_id:
                    continue
                if not event.get("date"):
                    allocation_ledger_schema_errors += 1
                    continue
                if str(event.get("role", "")).startswith("DIVIDEND_") and event.get("cash_amount") in (None, ""):
                    allocation_ledger_schema_errors += 1
                rows_by_day[date.fromisoformat(event["date"])].append(event)
            curve_rows = [row for row in allocation if row["allocation_id"] == allocation_id]
            for row in curve_rows:
                day = date.fromisoformat(row["date"])
                role_priority = {
                    "SPLIT_REBASE_ONLY": 0, "DIVIDEND_ENTITLEMENT_LOCKED": 1,
                    "DIVIDEND_ENTITLEMENT": 1, "DIVIDEND_CASH_PAID": 2,
                    "EXIT": 3, "TACTICAL_EXIT": 3, "MOTHER": 4, "TACTICAL_ENTRY": 4,
                    "ANNUAL_REBALANCE_SELL": 5, "ANNUAL_CASH_TRANSFER": 6,
                    "ANNUAL_CORE_BUY": 7, "CORE_INITIAL_BUY": 7,
                    "CORE_DIVIDEND_REINVEST": 7,
                }
                for event in sorted(rows_by_day.get(day, ()), key=lambda item: role_priority.get(item["role"], 99)):
                    role = event["role"]
                    sleeve_name = event.get("sleeve")
                    if role == "SPLIT_REBASE_ONLY":
                        state[sleeve_name]["shares"] = int(event["shares_after"])
                    elif role == "DIVIDEND_CASH_PAID":
                        state[sleeve_name]["cash"] += _d(event["cash_amount"])
                    elif role in {"CORE_INITIAL_BUY", "CORE_DIVIDEND_REINVEST", "ANNUAL_CORE_BUY", "TACTICAL_ENTRY", "MOTHER"}:
                        state[sleeve_name]["cash"] -= _d(event["cash_amount"])
                        state[sleeve_name]["shares"] += int(event["shares"])
                    elif role in {"TACTICAL_EXIT", "ANNUAL_REBALANCE_SELL", "EXIT"}:
                        state[sleeve_name]["cash"] += _d(event["cash_amount"])
                        state[sleeve_name]["shares"] -= int(event["shares"])
                    elif role == "ANNUAL_CASH_TRANSFER":
                        donor, recipient = event["from_sleeve"], event["to_sleeve"]
                        amount = _d(event["gross_transfer"])
                        state[donor]["cash"] -= amount
                        state[recipient]["cash"] += amount
                        if str(event.get("securities_sold", "")).lower() in {"false", "0"} and (
                            _d(event.get("sell_tax") or 0) != 0 or _d(event.get("sell_commission") or 0) != 0
                        ):
                            cash_donor_sell_tax_errors += 1
                raw_close = package.raw_by_date[day].close
                for sleeve_name, prefix in (("CORE", "core"), ("TACTICAL", "tactical")):
                    sleeve = state[sleeve_name]
                    expected_liq = model20.sell(raw_close, sleeve["shares"], InstrumentType.ETF).cash_amount if sleeve["shares"] else Decimal("0")
                    if expected_liq != _d(row[f"{prefix}_liquidation_value"]):
                        allocation_liquidation_errors += 1
                    if (
                        sleeve["cash"] != _d(row[f"{prefix}_cash"])
                        or sleeve["shares"] != int(row[f"{prefix}_shares"])
                        or sleeve["cash"] + expected_liq != _d(row[f"{prefix}_equity"])
                    ):
                        allocation_reconstruction_errors += 1
    parameters = standard.read_csv(run_root / "parameter-results-243.csv")
    parameter_keys = {
        tuple(int(row[key]) for key in ("ma_fast", "ma_slow", "macd_fast", "macd_slow", "macd_signal"))
        for row in parameters
    }
    inputs = json.loads((run_root / "input-manifest.json").read_text(encoding="utf-8-sig"))
    protected_trees_ok = inputs["protected_tree_hashes_before"] == inputs["protected_tree_hashes_after"] == {
        str(path): tree_hash(path) for path in (
            V15_RUN_ROOT, V16_REPORT_ROOT, V14_RUN_ROOT, V14_REPORT_ROOT,
            V13_RUN_ROOT, V13_REPORT_ROOT,
            V12_RUN_ROOT, V12_REPORT_ROOT, V11_RUN_ROOT, V11_REPORT_ROOT,
            PREVIOUS_CANDIDATE_RUN_ROOT, PREVIOUS_CANDIDATE_REPORT_ROOT,
        )
    }
    checks = {
        "phase1_scenarios": len(metrics) == 18,
        "phase1_daily_rows": len(curves) == 18 * 5093,
        "timing_and_fill_price_errors": timing_errors,
        "transaction_math_errors": transaction_math_errors,
        "daily_equity_equation_errors": equation_errors,
        "daily_liquidation_errors": liquidation_errors,
        "daily_cash_share_reconstruction_errors": reconstructed_errors,
        "allocation_daily_rows": len(allocation) == 8 * 5093,
        "allocation_conservation_errors": allocation_errors,
        "allocation_pre_first_rebalance_mismatches": pre_rebalance_mismatches,
        "rebalance_conservation_errors": rebalance_conservation_errors,
        "allocation_ledger_schema_errors": allocation_ledger_schema_errors,
        "all_8_allocation_cash_share_equity_reconstruction_errors": allocation_reconstruction_errors,
        "all_8_allocation_liquidation_errors": allocation_liquidation_errors,
        "cash_donor_sell_tax_errors": cash_donor_sell_tax_errors,
        "parameter_rows": len(parameters), "parameter_unique_keys": len(parameter_keys),
        "parameter_grid_exact": parameter_keys == set(PARAMETER_GRID),
        "protected_trees_unchanged": protected_trees_ok,
    }
    status = "PASS" if all(
        value is True or (isinstance(value, int) and not isinstance(value, bool) and (
            value == 0 if key.endswith(("errors", "mismatches")) else value in {243}
        ))
        for key, value in checks.items()
    ) else "FAIL"
    return {"status": status, "checks": checks, "transaction_rows_checked": len(transactions), "daily_rows_checked": len(curves)}


def validate(*, run_root: Path = RUN_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT) -> dict[str, Any]:
    run_manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8-sig"))
    report_manifest = json.loads((report_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
    run_bad = _verify_artifacts(run_root, run_manifest["artifacts"])
    report_bad = _verify_artifacts(report_root, report_manifest["artifacts"])
    broken = _broken_markdown_links(report_root)
    missing_from_readme = _readme_missing_artifacts(
        report_root, [str(row["path"]) for row in report_manifest["artifacts"]],
    )
    protected_ok = (
        _file_sha256(V15_RUN_ROOT / "manifest.json") == V15_RUN_MANIFEST
        and _file_sha256(V16_REPORT_ROOT / "artifact-manifest.json") == V16_REPORT_MANIFEST
        and _file_sha256(V14_RUN_ROOT / "manifest.json") == V14_RUN_MANIFEST
        and _file_sha256(V14_REPORT_ROOT / "artifact-manifest.json") == V14_REPORT_MANIFEST
        and _file_sha256(V13_RUN_ROOT / "manifest.json") == V13_RUN_MANIFEST
        and _file_sha256(V13_REPORT_ROOT / "artifact-manifest.json") == V13_REPORT_MANIFEST
        and _file_sha256(V12_RUN_ROOT / "manifest.json") == V12_RUN_MANIFEST
        and _file_sha256(V12_REPORT_ROOT / "artifact-manifest.json") == V12_REPORT_MANIFEST
        and _file_sha256(V11_RUN_ROOT / "manifest.json") == V11_RUN_MANIFEST
        and _file_sha256(V11_REPORT_ROOT / "artifact-manifest.json") == V11_REPORT_MANIFEST
        and _file_sha256(PREVIOUS_CANDIDATE_RUN_ROOT / "manifest.json") == PREVIOUS_CANDIDATE_RUN_MANIFEST
        and _file_sha256(PREVIOUS_CANDIDATE_REPORT_ROOT / "artifact-manifest.json") == PREVIOUS_CANDIDATE_REPORT_MANIFEST
    )
    parameter_rows = standard.read_csv(run_root / "parameter-results-243.csv")
    parity = json.loads((run_root / "baseline-v1-6-parity.json").read_text(encoding="utf-8-sig"))
    deep = deep_validate(run_root)
    status = "PASS" if not run_bad and not report_bad and not broken and not missing_from_readme and protected_ok and len(parameter_rows) == 243 and parity["status"] == "PASS_EXACT" and deep["status"] == "PASS" else "FAIL"
    return {
        "status": status, "run_artifacts": len(run_manifest["artifacts"]), "run_mismatches": run_bad,
        "report_artifacts": len(report_manifest["artifacts"]), "report_mismatches": report_bad,
        "broken_links": broken, "readme_missing_artifacts": missing_from_readme,
        "protected_manifest_pins_unchanged": protected_ok,
        "parameter_count": len(parameter_rows), "baseline_parity": parity["status"],
        "deep_validation": deep,
        "network_used": False, "writes_performed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("run", "validate"), nargs="?", default="run")
    parser.add_argument("--run-root", type=Path, default=RUN_ROOT_DEFAULT)
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT_DEFAULT)
    args = parser.parse_args(argv)
    result = validate(run_root=args.run_root, report_root=args.report_root) if args.mode == "validate" else run_study(run_root=args.run_root, report_root=args.report_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"PASS", "STOP_WAIT_00_QA"} else 1


def _tag_replay(
    output: dict[str, list[dict[str, Any]]],
    replay: Mapping[str, Any],
    *,
    timing: str,
    bps: int,
) -> None:
    bucket = {key: [] for key in ("metrics", "details", "transactions", "curves", "actions")}
    fixed._append_replay(bucket, replay, bps=bps)
    scenario_id = f"{timing}_{bps}BPS"
    for key, rows in bucket.items():
        output[key].extend(
            {"execution_timing": timing, "scenario_id": scenario_id, **row}
            for row in rows
        )


def run_phase1(
    *,
    package: Any,
    raw_rows: Sequence[Mapping[str, Any]],
    events: Sequence[Any],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Run fixed-regime and moving-control timing/cost scenarios."""

    indicators = regime.build_indicator_rows(raw_rows, events)
    indicator_by_date = {row["date"]: row for row in indicators}
    output = {key: [] for key in ("metrics", "details", "transactions", "curves", "actions")}
    execution_audit: list[dict[str, Any]] = []
    modes = ((regime.MOVING_ID, True), (regime.FIXED_ID, False))

    for strategy_id, moving_stop in modes:
        for bps in SLIPPAGE_BPS:
            same_day, _daily, _events = regime.build_regime_run(
                indicator_rows=indicators,
                raw_by_date=package.raw_by_date,
                strategy_id=strategy_id,
                moving_stop=moving_stop,
                start=accepted.FORMAL_START,
                end=accepted.CUTOFF,
                slippage_bps=bps,
            )
            executable, audit = schedule_frozen_orders(
                run=same_day,
                sessions=package.sessions,
                raw_by_date=package.raw_by_date,
                indicator_by_date=indicator_by_date,
                timing=TIMING_SIGNAL_CLOSE,
            )
            replay = replay_cash_safe_strategy(
                package=package,
                run=executable,
                start=accepted.FORMAL_START,
                end=accepted.CUTOFF,
                slippage_bps=bps,
                initial_capital=CONTINUOUS_CAPITAL,
                budget_mode="all_available",
                research_slippage=True,
            )
            _tag_replay(output, replay, timing=TIMING_SIGNAL_CLOSE, bps=bps)
            execution_audit.extend({"slippage_bps": bps, **row} for row in audit)

        causal_run, _daily, _events = regime.build_regime_run(
            indicator_rows=indicators,
            raw_by_date=package.raw_by_date,
            strategy_id=strategy_id,
            moving_stop=moving_stop,
            start=accepted.FORMAL_START,
            end=accepted.CUTOFF,
            slippage_bps=20,
        )
        for timing in (TIMING_NEXT_OPEN, TIMING_NEXT_CLOSE):
            executable, audit = schedule_frozen_orders(
                run=causal_run,
                sessions=package.sessions,
                raw_by_date=package.raw_by_date,
                indicator_by_date=indicator_by_date,
                timing=timing,
            )
            for bps in SLIPPAGE_BPS:
                replay = replay_cash_safe_strategy(
                    package=package,
                    run=executable,
                    start=accepted.FORMAL_START,
                    end=accepted.CUTOFF,
                    slippage_bps=bps,
                    initial_capital=CONTINUOUS_CAPITAL,
                    budget_mode="all_available",
                    research_slippage=True,
                )
                _tag_replay(output, replay, timing=timing, bps=bps)
                execution_audit.extend({"slippage_bps": bps, **row} for row in audit)
    stops = {
        (row["execution_timing"], int(row["slippage_bps"]), row["strategy_id"], row.get("leg_id", "")): row.get("initial_stop_raw")
        for row in output["transactions"] if row.get("role") == "MOTHER"
    }
    for row in output["details"]:
        row["initial_stop"] = stops.get((
            row["execution_timing"], int(row["slippage_bps"]),
            row["strategy_id"], row.get("leg_id", ""),
        ))
    return output, execution_audit


def phase1_gap_cost_summary(
    phase1: Mapping[str, Sequence[Mapping[str, Any]]],
    audit: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    metrics = phase1["metrics"]
    for metric in metrics:
        strategy_id = metric["strategy_id"]
        timing = metric["execution_timing"]
        bps = int(metric["slippage_bps"])
        rows = [
            row for row in audit
            if row["strategy_id"] == strategy_id and row["timing"] == timing
            and int(row["slippage_bps"]) == bps and row.get("status") == "SCHEDULED"
        ]
        gaps = [_d(row["reference_gap_pct"]) for row in rows]
        reference = next(
            row for row in metrics
            if row["strategy_id"] == strategy_id and row["execution_timing"] == timing
            and int(row["slippage_bps"]) == 20
        )
        output.append({
            "strategy_id": strategy_id, "execution_timing": timing, "slippage_bps": bps,
            "orders": len(rows), "average_reference_gap_pct": sum(gaps, Decimal("0")) / Decimal(len(gaps)),
            "median_reference_gap_pct": median(gaps),
            "worst_adverse_buy_gap_pct": max((_d(row["reference_gap_pct"]) for row in rows if row["role"] == "MOTHER"), default=None),
            "worst_adverse_sell_gap_pct": min((_d(row["reference_gap_pct"]) for row in rows if row["role"] == "EXIT"), default=None),
            "ending_equity": metric["ending_equity"],
            "ending_equity_erosion_vs_20bps": _d(metric["ending_equity"]) - _d(reference["ending_equity"]),
            "actual_fees_tax": metric["actual_fees_and_tax"],
            "actual_slippage_cost": metric["actual_slippage_cost"],
        })
    return output


def schedule_frozen_orders(
    *,
    run: StrategyRunSlice,
    sessions: Sequence[date],
    raw_by_date: Mapping[date, Any],
    indicator_by_date: Mapping[date, Mapping[str, Any]],
    timing: str,
) -> tuple[StrategyRunSlice, list[dict[str, Any]]]:
    """Map signal-day orders to an executable session without changing signals.

    For delayed entries, ``initial_stop_raw_override`` maps the signal-day
    technical stop through intervening corporate-action rebases and the fill
    day's technical/raw coordinate.  Market gaps never change the signal or
    the technical stop.
    """

    if timing not in EXECUTION_TIMINGS:
        raise ValueError(f"unsupported execution timing: {timing}")
    ordered = tuple(sorted(set(sessions)))
    next_session = {ordered[index]: ordered[index + 1] for index in range(len(ordered) - 1)}
    scheduled: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []

    for frozen in run.transactions:
        signal_day = frozen["date"]
        fill_day = signal_day if timing == TIMING_SIGNAL_CLOSE else next_session.get(signal_day)
        if fill_day is None:
            audit.append({
                "strategy_id": run.spec.strategy_id,
                "leg_id": frozen.get("leg_id", ""),
                "role": frozen.get("role", ""),
                "signal_date": signal_day,
                "fill_date": "",
                "timing": timing,
                "status": "NO_NEXT_SESSION_BEFORE_CUTOFF",
            })
            continue
        bar = raw_by_date[fill_day]
        fill_reference = bar.close if timing in {TIMING_SIGNAL_CLOSE, TIMING_NEXT_CLOSE} else bar.open
        row = dict(frozen)
        row.update({
            "date": fill_day,
            "reference_price": fill_reference,
            "signal_date": signal_day,
            "fill_timing": timing,
            "signal_reference_price": frozen["reference_price"],
        })
        rebase = Decimal("1")
        if fill_day != signal_day:
            for day in ordered:
                if signal_day < day <= fill_day:
                    rebase *= _d(indicator_by_date[day].get("price_rebase_factor", 1))
        if frozen.get("role") == "MOTHER":
            indicator = indicator_by_date[fill_day]
            technical_close = _d(indicator["technical_close"])
            if technical_close <= 0:
                raise ETF0050Error("fill-day technical close must be positive")
            if technical_close != _d(raw_by_date[fill_day].close):
                raise ETF0050Error("current-session technical close must equal raw tradable close")
            row["initial_stop_raw_override"] = _d(frozen["initial_stop"]) * rebase
        scheduled.append(row)
        signal_reference = _d(frozen["reference_price"])
        audit.append({
            "strategy_id": run.spec.strategy_id,
            "leg_id": frozen.get("leg_id", ""),
            "role": frozen.get("role", ""),
            "signal_date": signal_day,
            "fill_date": fill_day,
            "timing": timing,
            "status": "SCHEDULED",
            "signal_reference_price": signal_reference,
            "fill_reference_price": fill_reference,
            "reference_gap": fill_reference - signal_reference,
            "reference_gap_pct": fill_reference / signal_reference - 1,
            "intervening_rebase_factor": rebase,
        })

    delayed = StrategyRunSlice(
        run.spec,
        run.result,
        run.decisions,
        tuple(scheduled),
        run.legs,
    )
    return delayed, audit


if __name__ == "__main__":
    raise SystemExit(main())
