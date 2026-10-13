"""End-to-end formal 0050 ETF replay released by 00 for package v3."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import statistics
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import COST_MODEL, EXECUTION_MODEL, SPEC_SHA256, STRATEGY_VERSION, RunManifest
from .costs import CATHAY_WEB_028_TW_V1
from .etf_0050_base_v1 import (
    MAIN_SCENARIO_BPS,
    SLIPPAGE_SCENARIOS_BPS,
    STRATEGY_SPECS,
    SYMBOL,
    YEARS,
    CashDividendEvent,
    ETF0050Error,
    SplitEvent,
    StrategyRunSlice,
    fixed_detail_sample,
    run_five_strategy_interfaces,
    tree_hash,
    validate_primary_split_evidence,
    validate_ready_package,
)
from .fugle_source import sha256_file
from .h2_ma144_upward_entry_2022h2 import build_ma144_gate_map
from .models import Bar, InstrumentType, decimal


INITIAL_CAPITAL = Decimal("10000")
START = date(2022, 1, 1)
END = date(2026, 9, 30)
SPLIT_DATE = date(2025, 6, 18)
FORMAL_MANIFEST = "formal-manifest-v3.json"
FORMAL_READBACK = "formal-readback-v3.json"


@dataclass(frozen=True)
class PackageData:
    marker: Mapping[str, Any]
    raw_bars: tuple[Bar, ...]
    signal_bars: tuple[Bar, ...]
    sessions: tuple[date, ...]
    raw_by_date: Mapping[date, Bar]
    factors_by_date: Mapping[date, Mapping[str, Any]]
    split: SplitEvent
    dividends: tuple[CashDividendEvent, ...]
    package_hash: str
    source_master_hash: str


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _bar_from_row(row: Mapping[str, Any]) -> Bar:
    return Bar.from_values(
        date.fromisoformat(str(row["date"])),
        _d(row["open"]),
        _d(row["high"]),
        _d(row["low"]),
        _d(row["close"]),
    )


def load_package(package_root: Path) -> PackageData:
    marker = validate_ready_package(package_root)
    consumer = marker["consumer_entry"]
    raw_doc = _load_json(package_root / consumer["execution_prices"])
    factor_doc = _load_json(package_root / consumer["technical_factors"])
    split_doc = _load_json(package_root / consumer["split_event"])
    dividend_doc = _load_json(package_root / consumer["dividend_events"])
    if raw_doc.get("schema_version") != "dual-ma-etf-research-package-v3-daily-raw":
        raise ETF0050Error("raw price schema mismatch")
    if factor_doc.get("schema_version") != "dual-ma-etf-research-package-v3-causal-factors":
        raise ETF0050Error("causal factor schema mismatch")
    raw_rows = raw_doc["rows"]
    factor_rows = factor_doc["rows"]
    if len(raw_rows) != 1388 or len(factor_rows) != len(raw_rows):
        raise ETF0050Error("v3 price/factor row count mismatch")
    raw_bars = tuple(_bar_from_row(row) for row in raw_rows)
    factor_by_date = {date.fromisoformat(row["date"]): row for row in factor_rows}
    if set(factor_by_date) != {bar.date for bar in raw_bars}:
        raise ETF0050Error("factor dates do not exactly match raw price dates")
    signal_bars: list[Bar] = []
    for raw in raw_bars:
        factor = factor_by_date[raw.date]
        split_factor = _d(factor["split_factor"])
        offset = _d(factor["dividend_offset"])
        signal = Bar.from_values(
            raw.date,
            raw.open * split_factor - offset,
            raw.high * split_factor - offset,
            raw.low * split_factor - offset,
            raw.close * split_factor - offset,
        )
        if signal.close != _d(factor["technical_close"]):
            raise ETF0050Error(f"technical close mismatch on {raw.date}")
        signal_bars.append(signal)
    lineage = split_doc["lineage"]["result"]
    split = SplitEvent(
        effective_date=date.fromisoformat(split_doc["milestones"]["resumption_base_date"]),
        ratio=_d(split_doc["ratio"]),
        official_pre_split_reference=Decimal("188.65"),
        official_post_split_reference=_d(split_doc["official_reference_price"]),
        exact_post_split_reference=_d(split_doc["exact_reference_price"]),
        tick_size=_d(split_doc["tick_policy"]["tick_size"]),
        primary_source_url=lineage["url"],
        primary_source_sha256=lineage["sha256"],
    )
    validate_primary_split_evidence(split)
    dividends = tuple(
        CashDividendEvent(
            date.fromisoformat(row["ex_date"]),
            date.fromisoformat(row["payment_date"]),
            _d(row["amount_per_unit_original"]),
        )
        for row in dividend_doc["events"]
    )
    if len(dividends) != 12 or dividend_doc.get("complete") is not True:
        raise ETF0050Error("v3 dividend ledger is incomplete")
    return PackageData(
        marker=marker,
        raw_bars=raw_bars,
        signal_bars=tuple(signal_bars),
        sessions=tuple(bar.date for bar in raw_bars),
        raw_by_date={bar.date: bar for bar in raw_bars},
        factors_by_date=factor_by_date,
        split=split,
        dividends=dividends,
        package_hash=tree_hash(package_root),
        source_master_hash=sha256_file(package_root / consumer["source_master_table"]),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_manifest_factory(
    *, package: PackageData, year: int, git_commit: str
):
    raw_hash = str(package.marker["consumer_entry"]["execution_prices"])

    def factory(matrix_id: str, slippage_bps: int) -> RunManifest:
        return RunManifest(
            run_id=f"0050-base-v1-{year}-{slippage_bps}bps",
            strategy_version=STRATEGY_VERSION,
            spec_sha256=SPEC_SHA256,
            git_commit=git_commit,
            universe_id="ETF-0050-EVERY-TRADING-DAY",
            data_snapshot_id="etf-research-package-v3",
            data_start=package.raw_bars[0].date,
            data_end=package.raw_bars[-1].date,
            data_hash=f"{package.package_hash}:{raw_hash}",
            execution_model=EXECUTION_MODEL,
            cost_model=COST_MODEL,
            matrix_id=matrix_id,
            slippage_bps=slippage_bps,
            strategy_parameters={"kd_exit": False, "single_position": True},
            metadata={"released_by_00": True, "package_version": "v3"},
        )

    return factory


@dataclass(frozen=True)
class CorporateLedgers:
    dividends: tuple[Mapping[str, Any], ...]
    splits: tuple[Mapping[str, Any], ...]
    dividend_cash_by_leg: Mapping[str, Decimal]


def build_corporate_ledgers(
    *,
    transactions: Sequence[Mapping[str, Any]],
    dividends: Sequence[CashDividendEvent],
    split: SplitEvent,
    start: date,
    end: date,
) -> CorporateLedgers:
    tx_by_day: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for row in transactions:
        tx_by_day[row["date"]].append(row)
    ex_by_day = {event.ex_date: event for event in dividends if start <= event.ex_date <= end}
    pay_by_day: dict[date, list[CashDividendEvent]] = defaultdict(list)
    for event in dividends:
        if start <= event.payment_date <= end:
            pay_by_day[event.payment_date].append(event)
    days = sorted(set(tx_by_day) | set(ex_by_day) | set(pay_by_day) | ({split.effective_date} if start <= split.effective_date <= end else set()))
    shares = 0
    leg_id: str | None = None
    rights: dict[date, tuple[int, str | None, Decimal]] = {}
    dividend_rows: list[Mapping[str, Any]] = []
    split_rows: list[Mapping[str, Any]] = []
    by_leg: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for day in days:
        if day == split.effective_date:
            before = shares
            adjusted = Decimal(shares) * split.ratio
            if adjusted != adjusted.to_integral_value():
                raise ETF0050Error("split produced fractional strategy shares")
            shares = int(adjusted)
            split_rows.append(
                {
                    "date": day,
                    "type": "SPLIT_REBASE_ONLY",
                    "ratio": split.ratio,
                    "shares_before": before,
                    "shares_after": shares,
                    "leg_id": leg_id,
                    "transaction": False,
                    "pnl": Decimal("0"),
                }
            )
        event = ex_by_day.get(day)
        if event is not None:
            amount = Decimal(shares) * event.amount_per_share
            rights[event.payment_date] = (shares, leg_id, amount)
            dividend_rows.append(
                {
                    "ex_date": event.ex_date,
                    "payment_date": event.payment_date,
                    "stage": "ENTITLEMENT_LOCKED",
                    "shares": shares,
                    "leg_id": leg_id,
                    "amount_per_share": event.amount_per_share,
                    "cash": amount,
                }
            )
        for event in pay_by_day.get(day, []):
            entitled_shares, entitled_leg, amount = rights.get(
                event.payment_date, (0, None, Decimal("0"))
            )
            if entitled_leg is not None:
                by_leg[entitled_leg] += amount
            dividend_rows.append(
                {
                    "ex_date": event.ex_date,
                    "payment_date": event.payment_date,
                    "stage": "CASH_PAID",
                    "shares": entitled_shares,
                    "leg_id": entitled_leg,
                    "amount_per_share": event.amount_per_share,
                    "cash": amount,
                }
            )
        for row in tx_by_day.get(day, []):
            if row["role"] == "EXIT":
                if int(row["shares"]) != shares:
                    raise ETF0050Error(f"exit shares mismatch on {day}")
                shares = 0
                leg_id = None
            elif row.get("status") == "FILLED":
                if shares:
                    raise ETF0050Error("single-position strategy opened while already held")
                shares = int(row["shares"])
                leg_id = str(row["leg_id"])
    return CorporateLedgers(tuple(dividend_rows), tuple(split_rows), dict(by_leg))


def _daily_curve(
    *,
    package: PackageData,
    transactions: Sequence[Mapping[str, Any]],
    ledgers: CorporateLedgers,
    start: date,
    end: date,
    slippage_bps: int,
) -> tuple[list[dict[str, Any]], Decimal]:
    model = CATHAY_WEB_028_TW_V1.with_slippage(slippage_bps)
    tx_by_day: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for row in transactions:
        tx_by_day[row["date"]].append(row)
    paid_by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in ledgers.dividends:
        if row["stage"] == "CASH_PAID":
            paid_by_day[row["payment_date"]] += row["cash"]
    sessions = [day for day in package.sessions if start <= day <= end]
    cash = INITIAL_CAPITAL
    shares = 0
    peak = INITIAL_CAPITAL
    max_drawdown = Decimal("0")
    rows: list[dict[str, Any]] = []
    for day in sessions:
        if day == package.split.effective_date:
            shares = int(Decimal(shares) * package.split.ratio)
        cash += paid_by_day[day]
        for row in tx_by_day.get(day, []):
            if row["role"] == "EXIT":
                cash += _d(row["cash_amount"])
                shares = 0
            elif row.get("status") == "FILLED":
                cash -= _d(row["cash_amount"])
                shares = int(row["shares"])
        raw_close = package.raw_by_date[day].close
        liquidation = (
            model.sell(raw_close, shares, InstrumentType.ETF).cash_amount
            if shares
            else Decimal("0")
        )
        equity = cash + liquidation
        peak = max(peak, equity)
        drawdown = (equity / peak - Decimal("1")) if peak else Decimal("0")
        max_drawdown = min(max_drawdown, drawdown)
        rows.append(
            {
                "date": day,
                "raw_close": raw_close,
                "cash": cash,
                "shares": shares,
                "liquidation_value": liquidation,
                "equity": equity,
                "drawdown": drawdown,
            }
        )
    return rows, max_drawdown


def _leg_mfe_mae(
    *,
    package: PackageData,
    leg: Any,
    entry_tx: Mapping[str, Any],
    dividend_rows: Sequence[Mapping[str, Any]],
    end: date,
    slippage_bps: int,
) -> tuple[Decimal, Decimal]:
    model = CATHAY_WEB_028_TW_V1.with_slippage(slippage_bps)
    finish = leg.exit_date or end
    original_shares = int(entry_tx["shares"])
    values: list[Decimal] = []
    for day in package.sessions:
        if not leg.entry_date <= day <= finish:
            continue
        shares = original_shares
        if leg.entry_date < package.split.effective_date <= day:
            shares = int(Decimal(shares) * package.split.ratio)
        sale = model.sell(package.raw_by_date[day].close, shares, InstrumentType.ETF)
        paid = sum(
            (
                _d(row["cash"])
                for row in dividend_rows
                if row["stage"] == "CASH_PAID"
                and row["leg_id"] == leg.leg_id
                and row["payment_date"] <= day
            ),
            Decimal("0"),
        )
        values.append((sale.cash_amount + paid - leg.buy_total) / leg.buy_total)
    return (max(values), min(values)) if values else (Decimal("0"), Decimal("0"))


def compute_strategy_metrics(
    *,
    package: PackageData,
    run: StrategyRunSlice,
    year: int,
    slippage_bps: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], CorporateLedgers, list[dict[str, Any]]]:
    start = date(year, 1, 1)
    end = min(date(year, 12, 31), END)
    transactions = list(run.transactions)
    ledgers = build_corporate_ledgers(
        transactions=transactions,
        dividends=package.dividends,
        split=package.split,
        start=start,
        end=end,
    )
    entries = {str(row["leg_id"]): row for row in transactions if row["role"] != "EXIT" and row.get("status") == "FILLED"}
    exits = {str(row["leg_id"]): row for row in transactions if row["role"] == "EXIT"}
    details: list[dict[str, Any]] = []
    round_returns: list[Decimal] = []
    mfes: list[Decimal] = []
    maes: list[Decimal] = []
    realized = Decimal("0")
    open_estimate = Decimal("0")
    for leg in run.legs:
        entry = entries[leg.leg_id]
        dividend_cash = ledgers.dividend_cash_by_leg.get(leg.leg_id, Decimal("0"))
        mfe, mae = _leg_mfe_mae(
            package=package,
            leg=leg,
            entry_tx=entry,
            dividend_rows=ledgers.dividends,
            end=end,
            slippage_bps=slippage_bps,
        )
        mfes.append(mfe)
        maes.append(mae)
        exit_row = exits.get(leg.leg_id)
        if exit_row is not None:
            pnl = _d(exit_row["cash_amount"]) + dividend_cash - leg.buy_total
            realized += pnl
            state = "已賣出"
            exit_date = exit_row["date"]
            exit_raw = _d(exit_row["reference_price"])
            exit_fill = _d(exit_row["fill_price"])
            final_shares = int(exit_row["shares"])
        else:
            final_shares = leg.shares
            sale = CATHAY_WEB_028_TW_V1.with_slippage(slippage_bps).sell(
                package.raw_by_date[end].close, final_shares, InstrumentType.ETF
            )
            pnl = sale.cash_amount - leg.buy_total
            open_estimate += pnl
            realized += dividend_cash
            state = "尚未賣出／截止日估算"
            exit_date = None
            exit_raw = package.raw_by_date[end].close
            exit_fill = sale.fill_price
        return_rate = pnl / leg.buy_total
        round_returns.append(return_rate)
        details.append(
            {
                "year": year,
                "slippage_bps": slippage_bps,
                "strategy_id": run.spec.strategy_id,
                "strategy_label": run.spec.label,
                "leg_id": leg.leg_id,
                "status_zh": state,
                "entry_date": leg.entry_date,
                "entry_raw_price": _d(entry["reference_price"]),
                "entry_fill_price": _d(entry["fill_price"]),
                "entry_shares": int(entry["shares"]),
                "final_shares": final_shares,
                "buy_gross": leg.buy_gross,
                "buy_commission": leg.buy_commission,
                "buy_total": leg.buy_total,
                "exit_date": exit_date,
                "exit_raw_or_cutoff_price": exit_raw,
                "exit_fill_or_mark_price": exit_fill,
                "sell_commission": leg.sell_commission if exit_row else CATHAY_WEB_028_TW_V1.with_slippage(slippage_bps).sell(exit_raw, final_shares, InstrumentType.ETF).commission,
                "sell_tax": leg.sell_tax if exit_row else CATHAY_WEB_028_TW_V1.with_slippage(slippage_bps).sell(exit_raw, final_shares, InstrumentType.ETF).tax,
                "cash_dividends": dividend_cash,
                "net_pnl_or_estimate": pnl,
                "return_rate_or_estimate": return_rate,
                "mfe": mfe,
                "mae": mae,
            }
        )
    curve, max_drawdown = _daily_curve(
        package=package,
        transactions=transactions,
        ledgers=ledgers,
        start=start,
        end=end,
        slippage_bps=slippage_bps,
    )
    cumulative_buy = sum((_d(row["cash_amount"]) for row in entries.values()), Decimal("0"))
    fees_tax = sum((_d(row.get("commission", 0)) + _d(row.get("tax", 0)) for row in transactions), Decimal("0"))
    slippage_cost = sum((abs(_d(row["fill_price"]) - _d(row["reference_price"])) * int(row["shares"]) for row in transactions), Decimal("0"))
    combined = realized + open_estimate
    closed_returns = [row["return_rate_or_estimate"] for row in details if row["status_zh"] == "已賣出"]
    winning = sum(1 for value in closed_returns if value > 0)
    peak_entry = max(
        entries.values(), key=lambda row: _d(row["cash_amount"]), default=None
    )
    ending_equity = curve[-1]["equity"] if curve else INITIAL_CAPITAL
    days = max((end - start).days + 1, 1)
    cagr = Decimal(str(float(ending_equity / INITIAL_CAPITAL) ** (365 / days) - 1)) if ending_equity > 0 else Decimal("-1")
    idle = statistics.fmean(float(max(row["cash"], 0) / row["equity"]) if row["equity"] > 0 else 0 for row in curve) if curve else 1.0
    metrics = {
        "year": year,
        "slippage_bps": slippage_bps,
        "strategy_id": run.spec.strategy_id,
        "strategy_label": run.spec.label,
        "rounds": len(details),
        "closed_rounds": len(closed_returns),
        "open_rounds": len(details) - len(closed_returns),
        "win_rate": Decimal(winning) / len(closed_returns) if closed_returns else None,
        "cumulative_buy": cumulative_buy,
        "realized_net_pnl": realized,
        "cutoff_estimated_pnl": open_estimate,
        "combined_pnl": combined,
        "profit_per_100_invested": combined / cumulative_buy * 100 if cumulative_buy else None,
        "peak_concurrent_investment": _d(peak_entry["cash_amount"]) if peak_entry else Decimal("0"),
        "peak_investment_date": peak_entry["date"] if peak_entry else None,
        "average_return": sum(round_returns, Decimal("0")) / len(round_returns) if round_returns else None,
        "median_return": Decimal(str(statistics.median(round_returns))) if round_returns else None,
        "max_trade_win": max(round_returns) if round_returns else None,
        "max_trade_loss": min(round_returns) if round_returns else None,
        "average_mfe": sum(mfes, Decimal("0")) / len(mfes) if mfes else None,
        "average_mae": sum(maes, Decimal("0")) / len(maes) if maes else None,
        "max_asset_drawdown": max_drawdown,
        "cagr": cagr,
        "idle_cash_ratio": Decimal(str(idle)),
        "fees_and_tax": fees_tax,
        "slippage_cost": slippage_cost,
        "transaction_count": len(transactions),
        "ending_equity": ending_equity,
        "total_return": ending_equity / INITIAL_CAPITAL - 1,
    }
    return metrics, details, ledgers, curve


def compute_buy_hold(
    *,
    package: PackageData,
    year: int,
    slippage_bps: int,
    reinvest: bool,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    model = CATHAY_WEB_028_TW_V1.with_slippage(slippage_bps)
    sessions = [day for day in package.sessions if day.year == year and day <= END]
    if not sessions:
        raise ETF0050Error(f"no sessions for {year}")
    start, end = sessions[0], sessions[-1]
    cash = INITIAL_CAPITAL
    first_price = package.raw_by_date[start].close
    shares = int((cash / first_price).to_integral_value(rounding=ROUND_FLOOR))
    while shares and model.buy(first_price, shares).cash_amount > cash:
        shares -= 1
    buy = model.buy(first_price, shares)
    cash -= buy.cash_amount
    transactions: list[dict[str, Any]] = [
        {
            "date": start,
            "role": "BUY_AND_HOLD_INITIAL",
            "reference_price": buy.reference_price,
            "fill_price": buy.fill_price,
            "shares": shares,
            "gross": buy.gross,
            "commission": buy.commission,
            "tax": buy.tax,
            "cash_amount": buy.cash_amount,
        }
    ]
    rights: dict[date, tuple[int, Decimal]] = {}
    dividend_rows: list[dict[str, Any]] = []
    split_rows: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    peak = INITIAL_CAPITAL
    max_drawdown = Decimal("0")
    total_dividends = Decimal("0")
    cumulative_buy = buy.cash_amount
    for day in sessions:
        if day == package.split.effective_date:
            before = shares
            shares = int(Decimal(shares) * package.split.ratio)
            split_rows.append(
                {
                    "date": day,
                    "type": "SPLIT_REBASE_ONLY",
                    "shares_before": before,
                    "shares_after": shares,
                    "transaction": False,
                    "pnl": Decimal("0"),
                }
            )
        for event in package.dividends:
            if event.ex_date == day:
                amount = Decimal(shares) * event.amount_per_share
                rights[event.payment_date] = (shares, amount)
                dividend_rows.append(
                    {
                        "ex_date": event.ex_date,
                        "payment_date": event.payment_date,
                        "stage": "ENTITLEMENT_LOCKED",
                        "shares": shares,
                        "cash": amount,
                    }
                )
            if event.payment_date == day:
                entitled, amount = rights.get(day, (0, Decimal("0")))
                cash += amount
                total_dividends += amount
                dividend_rows.append(
                    {
                        "ex_date": event.ex_date,
                        "payment_date": day,
                        "stage": "CASH_PAID",
                        "shares": entitled,
                        "cash": amount,
                    }
                )
                if reinvest and amount > 0:
                    price = package.raw_by_date[day].close
                    extra = int((cash / price).to_integral_value(rounding=ROUND_FLOOR))
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
                                "role": "股利於發放日按原價再投入",
                                "reference_price": order.reference_price,
                                "fill_price": order.fill_price,
                                "shares": extra,
                                "gross": order.gross,
                                "commission": order.commission,
                                "tax": order.tax,
                                "cash_amount": order.cash_amount,
                            }
                        )
        mark = model.sell(package.raw_by_date[day].close, shares, InstrumentType.ETF)
        equity = cash + mark.cash_amount
        peak = max(peak, equity)
        drawdown = equity / peak - 1
        max_drawdown = min(max_drawdown, drawdown)
        curve.append(
            {
                "date": day,
                "raw_close": package.raw_by_date[day].close,
                "cash": cash,
                "shares": shares,
                "liquidation_value": mark.cash_amount,
                "equity": equity,
                "drawdown": drawdown,
            }
        )
    final_sale = model.sell(package.raw_by_date[end].close, shares, InstrumentType.ETF)
    open_estimate = final_sale.cash_amount - cumulative_buy
    combined = cash + final_sale.cash_amount - INITIAL_CAPITAL
    days = max((end - start).days + 1, 1)
    ending = cash + final_sale.cash_amount
    cagr = Decimal(str(float(ending / INITIAL_CAPITAL) ** (365 / days) - 1))
    label = "買進持有－股利再投入" if reinvest else "買進持有－股利留現金"
    fees = sum((_d(row["commission"]) for row in transactions), Decimal("0")) + final_sale.commission + final_sale.tax
    slip = sum((abs(_d(row["fill_price"]) - _d(row["reference_price"])) * int(row["shares"]) for row in transactions), Decimal("0")) + abs(final_sale.fill_price - final_sale.reference_price) * shares
    metrics = {
        "year": year,
        "slippage_bps": slippage_bps,
        "strategy_id": "BUY_HOLD_REINVEST" if reinvest else "BUY_HOLD_CASH",
        "strategy_label": label,
        "rounds": 1,
        "closed_rounds": 0,
        "open_rounds": 1,
        "win_rate": None,
        "cumulative_buy": cumulative_buy,
        "realized_net_pnl": total_dividends,
        "cutoff_estimated_pnl": combined - total_dividends,
        "combined_pnl": combined,
        "profit_per_100_invested": combined / cumulative_buy * 100,
        "peak_concurrent_investment": cumulative_buy,
        "peak_investment_date": transactions[-1]["date"],
        "average_return": combined / INITIAL_CAPITAL,
        "median_return": combined / INITIAL_CAPITAL,
        "max_trade_win": combined / INITIAL_CAPITAL,
        "max_trade_loss": combined / INITIAL_CAPITAL,
        "average_mfe": max(row["equity"] / INITIAL_CAPITAL - 1 for row in curve),
        "average_mae": min(row["equity"] / INITIAL_CAPITAL - 1 for row in curve),
        "max_asset_drawdown": max_drawdown,
        "cagr": cagr,
        "idle_cash_ratio": Decimal(str(statistics.fmean(float(max(row["cash"], 0) / row["equity"]) for row in curve))),
        "fees_and_tax": fees,
        "slippage_cost": slip,
        "transaction_count": len(transactions),
        "ending_equity": ending,
        "total_return": ending / INITIAL_CAPITAL - 1,
    }
    detail = [
        {
            "year": year,
            "slippage_bps": slippage_bps,
            "strategy_id": metrics["strategy_id"],
            "strategy_label": label,
            "status_zh": "尚未賣出／截止日估算",
            "entry_date": start,
            "entry_raw_price": buy.reference_price,
            "entry_fill_price": buy.fill_price,
            "entry_shares": transactions[0]["shares"],
            "final_shares": shares,
            "buy_total": cumulative_buy,
            "exit_date": None,
            "exit_raw_or_cutoff_price": final_sale.reference_price,
            "exit_fill_or_mark_price": final_sale.fill_price,
            "sell_commission": final_sale.commission,
            "sell_tax": final_sale.tax,
            "cash_dividends": total_dividends,
            "net_pnl_or_estimate": combined,
            "return_rate_or_estimate": combined / INITIAL_CAPITAL,
            "mfe": metrics["average_mfe"],
            "mae": metrics["average_mae"],
        }
    ]
    return metrics, detail, dividend_rows + split_rows, curve, transactions


def _money(value: Any) -> str:
    if value is None:
        return "—"
    return f"{_d(value):,.2f}"


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{_d(value) * 100:.2f}%"


def _metrics_table(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| 方法 | 回合 | 勝率 | 累計買入 | 已實現淨利 | 截止日估算 | 合計 | 每投入100元獲利 | 最大回落 | 年化報酬 | 閒置現金 | 費稅 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {label} | {rounds} | {win} | {buy} | {realized} | {open_} | {combined} | {per100} | {dd} | {cagr} | {idle} | {cost} |".format(
                label=row["strategy_label"],
                rounds=row["rounds"],
                win=_pct(row["win_rate"]),
                buy=_money(row["cumulative_buy"]),
                realized=_money(row["realized_net_pnl"]),
                open_=_money(row["cutoff_estimated_pnl"]),
                combined=_money(row["combined_pnl"]),
                per100=_money(row["profit_per_100_invested"]),
                dd=_pct(row["max_asset_drawdown"]),
                cagr=_pct(row["cagr"]),
                idle=_pct(row["idle_cash_ratio"]),
                cost=_money(row["fees_and_tax"] + row["slippage_cost"]),
            )
        )
    return "\n".join(lines)


def _diagnostic_table(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| 方法 | 最高同時投入（日） | 平均／中位報酬 | 最大單筆贏／虧 | 平均 MFE／MAE | 交易次數 |",
        "|---|---|---|---|---|---:|",
    ]
    for row in rows:
        lines.append(
            "| {label} | {peak}（{day}） | {avg}／{median} | {win}／{loss} | {mfe}／{mae} | {count} |".format(
                label=row["strategy_label"],
                peak=_money(row["peak_concurrent_investment"]),
                day=row["peak_investment_date"] or "—",
                avg=_pct(row["average_return"]),
                median=_pct(row["median_return"]),
                win=_pct(row["max_trade_win"]),
                loss=_pct(row["max_trade_loss"]),
                mfe=_pct(row["average_mfe"]),
                mae=_pct(row["average_mae"]),
                count=row["transaction_count"],
            )
        )
    return "\n".join(lines)


def render_year_report_formal(year: int, rows: Sequence[Mapping[str, Any]]) -> str:
    ranked = sorted(rows, key=lambda row: _d(row["combined_pnl"]), reverse=True)
    best = ranked[0]
    hold = next(row for row in rows if row["strategy_id"] == "BUY_HOLD_CASH")
    strategies = [row for row in rows if row["strategy_id"] not in {"BUY_HOLD_CASH", "BUY_HOLD_REINVEST"}]
    relative = "\n".join(
        f"- {row['strategy_label']}：相對全程持有 {'領先' if row['total_return'] >= hold['total_return'] else '落後'} {_pct(abs(row['total_return'] - hold['total_return']))}。"
        for row in strategies
    )
    return "\n".join(
        [
            f"# 0050 雙均線基準回測｜{year}",
            "",
            "## 一、先講結論",
            f"主情境為單邊滑價 20 bps。合計損益最高的是「{best['strategy_label']}」，合計 {_money(best['combined_pnl'])} 元。這是固定規則的事後回測，不代表未來一定重現。",
            "",
            "## 二、買進持有、股利再投入與五個策略",
            _metrics_table(rows),
            "",
            "## 三、交易品質與風險",
            _diagnostic_table(rows),
            "",
            "## 四、策略擇時相對 0050 全程持有",
            "0050 同時是本研究標的與基準；策略結果代表何時持有 0050，不能與股票選股報告直接相加。",
            relative,
            "",
            "## 五、已實現與尚未賣出",
            "已賣出回合列入已實現淨利；年底或 2026-09-30 尚未賣出的部位，僅按當日 TWSE 原價估算，沒有假裝強制賣出。現金股利在除息日鎖定權利、發放日入帳。",
            "",
            "## 六、成本與資料口徑",
            "0、10、20 bps 三種滑價均有機器明細；本報告展示 20 bps。買賣手續費沿用既定模型，ETF 賣出證交稅為 0.1%。訊號使用 v3 因果技術序列，成交、費稅與截止估值使用原價。",
            "",
            "## 七、來源分組",
            "本任務只有「0050 ETF 單一來源」，沒有七種股票選股來源。第一方來源與 SHA-256 見 formal-manifest-v3.json。",
            "",
            "## 八、逐筆附件",
            "完整逐筆、分割、股利權利、每日資產曲線與固定種子抽樣均在 details/。OPEN/HOLD 在附件中一律另附白話「尚未賣出／截止日估算」。",
            "",
        ]
    )


def aggregate_rows(metrics: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_strategy: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in metrics:
        if int(row["slippage_bps"]) == MAIN_SCENARIO_BPS:
            by_strategy[str(row["strategy_id"])].append(row)
    output: list[dict[str, Any]] = []
    for strategy_id, rows in by_strategy.items():
        combined = sum((_d(row["combined_pnl"]) for row in rows), Decimal("0"))
        total_capital = INITIAL_CAPITAL * len(rows)
        closed = sum(int(row["closed_rounds"]) for row in rows)
        weighted_wins = sum((_d(row["win_rate"]) * int(row["closed_rounds"]) for row in rows if row["win_rate"] is not None), Decimal("0"))
        cumulative_buy = sum((_d(row["cumulative_buy"]) for row in rows), Decimal("0"))
        returns = [_d(row["average_return"]) for row in rows if row["average_return"] is not None]
        output.append(
            {
                "strategy_id": strategy_id,
                "strategy_label": rows[0]["strategy_label"],
                "rounds": sum(int(row["rounds"]) for row in rows),
                "closed_rounds": closed,
                "open_rounds": sum(int(row["open_rounds"]) for row in rows),
                "win_rate": weighted_wins / closed if closed else None,
                "cumulative_buy": cumulative_buy,
                "realized_net_pnl": sum((_d(row["realized_net_pnl"]) for row in rows), Decimal("0")),
                "cutoff_estimated_pnl": sum((_d(row["cutoff_estimated_pnl"]) for row in rows), Decimal("0")),
                "combined_pnl": combined,
                "profit_per_100_invested": combined / cumulative_buy * 100 if cumulative_buy else None,
                "peak_concurrent_investment": max(_d(row["peak_concurrent_investment"]) for row in rows),
                "peak_investment_date": max(rows, key=lambda row: _d(row["peak_concurrent_investment"]))["peak_investment_date"],
                "average_return": sum(returns, Decimal("0")) / len(returns) if returns else None,
                "median_return": Decimal(str(statistics.median(returns))) if returns else None,
                "max_trade_win": max((_d(row["max_trade_win"]) for row in rows if row["max_trade_win"] is not None), default=None),
                "max_trade_loss": min((_d(row["max_trade_loss"]) for row in rows if row["max_trade_loss"] is not None), default=None),
                "average_mfe": sum((_d(row["average_mfe"]) for row in rows if row["average_mfe"] is not None), Decimal("0")) / len(rows),
                "average_mae": sum((_d(row["average_mae"]) for row in rows if row["average_mae"] is not None), Decimal("0")) / len(rows),
                "max_asset_drawdown": min(_d(row["max_asset_drawdown"]) for row in rows),
                "cagr": Decimal(str(float(Decimal("1") + combined / total_capital) ** (1 / len(rows)) - 1)),
                "idle_cash_ratio": sum((_d(row["idle_cash_ratio"]) for row in rows), Decimal("0")) / len(rows),
                "fees_and_tax": sum((_d(row["fees_and_tax"]) for row in rows), Decimal("0")),
                "slippage_cost": sum((_d(row["slippage_cost"]) for row in rows), Decimal("0")),
                "transaction_count": sum(int(row["transaction_count"]) for row in rows),
                "ending_equity": total_capital + combined,
                "total_return": combined / total_capital,
            }
        )
    return sorted(output, key=lambda row: row["strategy_id"])


def render_overview_formal(
    *, annual: Sequence[Mapping[str, Any]], aggregate: Sequence[Mapping[str, Any]]
) -> str:
    best = max(aggregate, key=lambda row: _d(row["combined_pnl"]))
    hold_2022 = next(row for row in annual if row["year"] == 2022 and row["slippage_bps"] == 20 and row["strategy_id"] == "BUY_HOLD_CASH")
    defense = []
    participation = []
    for strategy in (row for row in aggregate if row["strategy_id"].startswith(("A_", "B_", "C_"))):
        row_2022 = next(row for row in annual if row["year"] == 2022 and row["slippage_bps"] == 20 and row["strategy_id"] == strategy["strategy_id"])
        defense.append(f"- {strategy['strategy_label']}：2022 最大回落 {_pct(row_2022['max_asset_drawdown'])}，全程持有為 {_pct(hold_2022['max_asset_drawdown'])}。")
        ratios = []
        for year in (2025, 2026):
            row = next(item for item in annual if item["year"] == year and item["slippage_bps"] == 20 and item["strategy_id"] == strategy["strategy_id"])
            hold = next(item for item in annual if item["year"] == year and item["slippage_bps"] == 20 and item["strategy_id"] == "BUY_HOLD_CASH")
            if _d(hold["total_return"]) > 0:
                ratios.append(_d(row["total_return"]) / _d(hold["total_return"]))
        ratio = sum(ratios, Decimal("0")) / len(ratios) if ratios else Decimal("0")
        participation.append(f"- {strategy['strategy_label']}：2025～2026 多頭參與率 {_pct(ratio)}。")
    return "\n".join(
        [
            "# 0050 雙均線基準回測｜2022～2026 五年總覽",
            "",
            "## 一、五年總結",
            f"五個獨立年度 cohort 合併後，20 bps 主情境合計損益最高的是「{best['strategy_label']}」，合計 {_money(best['combined_pnl'])} 元。",
            "",
            "## 二、五策略與兩種買進持有",
            _metrics_table(aggregate),
            "",
            "## 三、完整診斷",
            _diagnostic_table(aggregate),
            "",
            "## 四、2022 防守效果",
            *defense,
            "",
            "## 五、2025～2026 多頭參與率",
            "參與率是策略年度報酬除以 0050 全程持有年度報酬；0050 本身就是標的，因此衡量的是擇時代價或效益。",
            *participation,
            "",
            "## 六、不可直接相加的界線",
            "本報告只有 0050 ETF 單一來源；它不是七種股票選股來源之一，也不能與股票選股策略績效直接相加。",
            "",
            "## 七、重算與附件",
            "0/10/20 bps、逐筆、公司行動、每日資產曲線、抽樣核對、manifest 與 readback 全部保留於同一輸出根。",
            "",
        ]
    )


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


def _audit_samples(
    *, package: PackageData, details: Sequence[Mapping[str, Any]], year: int
) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in details
        if int(row["year"]) == year
        and int(row["slippage_bps"]) == MAIN_SCENARIO_BPS
        and str(row["strategy_id"]).startswith(("A_", "B_", "C_"))
    ]
    sampled = fixed_detail_sample(candidates, seed=year)
    output: list[dict[str, Any]] = []
    model = CATHAY_WEB_028_TW_V1.with_slippage(MAIN_SCENARIO_BPS)
    for row in sampled:
        entry_day = row["entry_date"]
        raw = package.raw_by_date[entry_day].close
        buy = model.buy(raw, int(row["entry_shares"]))
        checks = {
            "entry_date_in_v3_sessions": entry_day in package.raw_by_date,
            "raw_execution_reference_matches": raw == _d(row["entry_raw_price"]),
            "buy_fill_matches_cost_model": buy.fill_price == _d(row["entry_fill_price"]),
            "buy_commission_matches_cost_model": buy.commission == _d(row["buy_commission"]),
            "causal_signal_row_exists": entry_day in package.factors_by_date,
            "etf_sell_tax_nonnegative": _d(row["sell_tax"]) >= 0,
            "split_coordinate_consistent": not (
                entry_day < SPLIT_DATE
                and row["exit_date"] is not None
                and row["exit_date"] >= SPLIT_DATE
                and int(row["final_shares"]) != int(row["entry_shares"]) * 4
            ),
        }
        output.append(
            {
                "year": year,
                "strategy_id": row["strategy_id"],
                "leg_id": row["leg_id"],
                "checks": checks,
                "status": "PASS" if all(checks.values()) else "FAIL",
            }
        )
    return output


def _artifact_rows(root: Path, *, excluded: set[str]) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        rows.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return rows


def _verify_rows(root: Path, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    mismatches: list[str] = []
    for row in rows:
        path = root / str(row["path"])
        if not path.is_file():
            mismatches.append(f"missing:{row['path']}")
            continue
        if path.stat().st_size != int(row["bytes"]):
            mismatches.append(f"bytes:{row['path']}")
        if sha256_file(path).lower() != str(row["sha256"]).lower():
            mismatches.append(f"sha256:{row['path']}")
    return mismatches


def _git_commit(repo_root: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True
    ).strip()


def run_formal_batch(
    *,
    package_root: Path,
    output_root: Path,
    repo_root: Path,
    released_by_00: bool,
) -> Mapping[str, Any]:
    if not released_by_00:
        raise ETF0050Error("formal replay is fail-closed without --released-by-00")
    formal_targets = [
        *(output_root / f"{year}-report.md" for year in YEARS),
        output_root / "2022-2026-overview.md",
        output_root / FORMAL_MANIFEST,
        output_root / FORMAL_READBACK,
    ]
    existing = [str(path) for path in formal_targets if path.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite formal artifacts: {existing}")

    old_reports_root = output_root.parents[2]
    v1 = package_root.parent / "03-etf-research-package-v1"
    v2 = package_root.parent / "03-etf-research-package-v2"
    immutable_before = {
        "old_stock_reports": tree_hash(old_reports_root, exclude=output_root),
        "etf_package_v1": tree_hash(v1),
        "etf_package_v2": tree_hash(v2),
        "etf_package_v3": tree_hash(package_root),
    }
    package = load_package(package_root)
    git_commit = _git_commit(repo_root)
    gate_map = build_ma144_gate_map(
        dates=[bar.date for bar in package.signal_bars],
        closes=[bar.close for bar in package.signal_bars],
        official_sessions=package.sessions,
    )

    all_metrics: list[dict[str, Any]] = []
    all_details: list[dict[str, Any]] = []
    all_transactions: list[dict[str, Any]] = []
    all_dividends: list[dict[str, Any]] = []
    all_splits: list[dict[str, Any]] = []
    all_curves: list[dict[str, Any]] = []
    all_decisions: list[dict[str, Any]] = []
    formal_runs: list[dict[str, Any]] = []

    for year in YEARS:
        year_end = min(date(year, 12, 31), END)
        signal_bars = [bar for bar in package.signal_bars if bar.date <= year_end]
        raw_bars = [bar for bar in package.raw_bars if bar.date <= year_end]
        for bps in SLIPPAGE_SCENARIOS_BPS:
            runs = run_five_strategy_interfaces(
                signal_bars=signal_bars,
                raw_execution_bars=raw_bars,
                split_events=(package.split,),
                gate_map=gate_map,
                official_sessions=package.sessions,
                manifest_factory=build_manifest_factory(
                    package=package, year=year, git_commit=git_commit
                ),
                slippage_bps=bps,
                backtest_start=date(year, 1, 1),
                backtest_end=year_end,
                split_signal_coordinate_already_adjusted=True,
            )
            for run in runs:
                metrics, details, ledgers, curve = compute_strategy_metrics(
                    package=package, run=run, year=year, slippage_bps=bps
                )
                all_metrics.append(metrics)
                all_details.extend(details)
                all_transactions.extend(
                    {
                        "year": year,
                        "slippage_bps": bps,
                        "strategy_id": run.spec.strategy_id,
                        "strategy_label": run.spec.label,
                        **dict(row),
                    }
                    for row in run.transactions
                )
                all_decisions.extend(
                    {
                        "year": year,
                        "slippage_bps": bps,
                        "strategy_id": run.spec.strategy_id,
                        "strategy_label": run.spec.label,
                        **dict(row),
                    }
                    for row in run.decisions
                )
                all_dividends.extend(
                    {
                        "year": year,
                        "slippage_bps": bps,
                        "strategy_id": run.spec.strategy_id,
                        **dict(row),
                    }
                    for row in ledgers.dividends
                )
                all_splits.extend(
                    {
                        "year": year,
                        "slippage_bps": bps,
                        "strategy_id": run.spec.strategy_id,
                        **dict(row),
                    }
                    for row in ledgers.splits
                )
                all_curves.extend(
                    {
                        "year": year,
                        "slippage_bps": bps,
                        "strategy_id": run.spec.strategy_id,
                        **dict(row),
                    }
                    for row in curve
                )
                formal_runs.append(
                    {
                        "year": year,
                        "slippage_bps": bps,
                        "strategy_id": run.spec.strategy_id,
                        "semantic_result_fingerprint": run.result.semantic_result_fingerprint,
                    }
                )
            for reinvest in (False, True):
                metrics, details, actions, curve, transactions = compute_buy_hold(
                    package=package,
                    year=year,
                    slippage_bps=bps,
                    reinvest=reinvest,
                )
                all_metrics.append(metrics)
                all_details.extend(details)
                all_transactions.extend(
                    {
                        "year": year,
                        "slippage_bps": bps,
                        "strategy_id": metrics["strategy_id"],
                        "strategy_label": metrics["strategy_label"],
                        **row,
                    }
                    for row in transactions
                )
                all_dividends.extend(
                    {
                        "year": year,
                        "slippage_bps": bps,
                        "strategy_id": metrics["strategy_id"],
                        **row,
                    }
                    for row in actions
                    if "ex_date" in row
                )
                all_splits.extend(
                    {
                        "year": year,
                        "slippage_bps": bps,
                        "strategy_id": metrics["strategy_id"],
                        **row,
                    }
                    for row in actions
                    if row.get("type") == "SPLIT_REBASE_ONLY"
                )
                all_curves.extend(
                    {
                        "year": year,
                        "slippage_bps": bps,
                        "strategy_id": metrics["strategy_id"],
                        **row,
                    }
                    for row in curve
                )

    annual_main = [row for row in all_metrics if row["slippage_bps"] == MAIN_SCENARIO_BPS]
    aggregate = aggregate_rows(all_metrics)
    audit_rows = [row for year in YEARS for row in _audit_samples(package=package, details=all_details, year=year)]
    if any(row["status"] != "PASS" for row in audit_rows):
        raise ETF0050Error("fixed-seed trade audit failed")
    if any(sum(1 for row in audit_rows if row["year"] == year) < min(12, sum(1 for item in all_details if item["year"] == year and item["slippage_bps"] == 20 and str(item["strategy_id"]).startswith(("A_", "B_", "C_")))) for year in YEARS):
        raise ETF0050Error("fixed-seed annual audit sample is incomplete")

    false_split_transactions = [
        row for row in all_transactions if row.get("role") in {"SPLIT", "DIVIDEND", "CORPORATE_ACTION"}
    ]
    if false_split_transactions:
        raise ETF0050Error("a corporate action was recorded as a trade")
    event_signal = package.factors_by_date[SPLIT_DATE]
    prior_signal = package.factors_by_date[date(2025, 6, 10)]
    split_exit_candidates = [
        row
        for row in all_details
        if row.get("exit_date") == SPLIT_DATE
        and row.get("entry_date") < SPLIT_DATE
        and str(row.get("strategy_id", "")).startswith(("A_", "B_", "C_"))
    ]
    split_acceptance = {
        "status": "PASS",
        "effective_date": SPLIT_DATE,
        "prior_session": date(2025, 6, 10),
        "next_session": date(2025, 6, 19),
        "exact_reference": package.split.exact_post_split_reference,
        "official_tick_reference": package.split.official_post_split_reference,
        "resumption_market_close_excluded_from_conservation": package.raw_by_date[SPLIT_DATE].close,
        "technical_close_before": _d(prior_signal["technical_close"]),
        "technical_close_event": _d(event_signal["technical_close"]),
        "false_stop_count": len(split_exit_candidates),
        "corporate_action_transaction_count": 0,
    }
    if split_acceptance["false_stop_count"] != 0:
        raise ETF0050Error("formal replay contains a split-day exit candidate")

    recompute_mismatches: list[dict[str, Any]] = []
    details_by_key: dict[tuple[int, int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in all_details:
        details_by_key[(int(row["year"]), int(row["slippage_bps"]), str(row["strategy_id"]))].append(row)
    for metric in all_metrics:
        key = (int(metric["year"]), int(metric["slippage_bps"]), str(metric["strategy_id"]))
        rows = details_by_key.get(key, [])
        if key[2].startswith(("A_", "B_", "C_")):
            independent = sum(
                (
                    _d(row["net_pnl_or_estimate"])
                    + (
                        _d(row["cash_dividends"])
                        if row["status_zh"] != "已賣出"
                        else Decimal("0")
                    )
                    for row in rows
                ),
                Decimal("0"),
            )
        else:
            independent = sum(
                (_d(row["net_pnl_or_estimate"]) for row in rows), Decimal("0")
            )
        if independent != _d(metric["combined_pnl"]):
            recompute_mismatches.append(
                {
                    "key": key,
                    "metric": metric["combined_pnl"],
                    "independent": independent,
                }
            )
    if recompute_mismatches:
        raise ETF0050Error(
            f"independent detail recompute mismatches: {recompute_mismatches[:3]}"
        )

    with tempfile.TemporaryDirectory(prefix="0050-formal-v3-") as temp_name:
        stage = Path(temp_name)
        details_root = stage / "details"
        for year in YEARS:
            rows = [row for row in annual_main if row["year"] == year]
            (stage / f"{year}-report.md").write_text(
                render_year_report_formal(year, rows), encoding="utf-8"
            )
        (stage / "2022-2026-overview.md").write_text(
            render_overview_formal(annual=all_metrics, aggregate=aggregate),
            encoding="utf-8",
        )
        (stage / "README-formal-v3.md").write_text(
            "# 0050 ETF 正式回測 base-v1\n\n狀態：STOP_WAIT_00_QA。v3 已由 00 採納並放行；主文 20 bps，機器明細含 0/10/20 bps。舊 checkpoint 保留不覆寫。\n",
            encoding="utf-8",
        )
        _write_csv(details_root / "scenario_metrics.csv", all_metrics)
        _write_csv(details_root / "trade_details_all_scenarios.csv", all_details)
        _write_csv(details_root / "transactions_all_scenarios.csv", all_transactions)
        _write_csv(details_root / "dividend_entitlement_and_payment_ledger.csv", all_dividends)
        _write_csv(details_root / "split_event_ledger.csv", all_splits)
        _write_csv(details_root / "daily_equity_all_scenarios.csv", all_curves)
        _write_json(details_root / "decisions_all_scenarios.json", all_decisions)
        _write_json(details_root / "fixed_seed_trade_audit.json", audit_rows)
        _write_json(details_root / "split_acceptance.json", split_acceptance)
        _write_json(details_root / "formal_runs.json", formal_runs)
        _write_json(details_root / "five_year_aggregate_20bps.json", aggregate)
        recompute = {
            "status": "PASS",
            "annual_metrics_sha256": _sha_bytes(_canonical_bytes(all_metrics)),
            "aggregate_sha256": _sha_bytes(_canonical_bytes(aggregate)),
            "five_strategy_ids": [spec.strategy_id for spec in STRATEGY_SPECS],
            "benchmark_ids": ["BUY_HOLD_CASH", "BUY_HOLD_REINVEST"],
            "annual_row_count": len(all_metrics),
            "expected_annual_row_count": len(YEARS) * len(SLIPPAGE_SCENARIOS_BPS) * 7,
            "detail_recompute_mismatch_count": len(recompute_mismatches),
        }
        if recompute["annual_row_count"] != recompute["expected_annual_row_count"]:
            raise ETF0050Error("formal scenario row count mismatch")
        _write_json(details_root / "independent_recompute.json", recompute)

        source_after_stage = {
            "old_stock_reports": tree_hash(old_reports_root, exclude=output_root),
            "etf_package_v1": tree_hash(v1),
            "etf_package_v2": tree_hash(v2),
            "etf_package_v3": tree_hash(package_root),
        }
        if source_after_stage != immutable_before:
            raise ETF0050Error("an immutable source/report tree changed during replay")
        artifacts = _artifact_rows(
            stage, excluded={FORMAL_MANIFEST, FORMAL_READBACK}
        )
        manifest = {
            "schema_version": "dual-ma-0050-formal-manifest-v3",
            "status": "STOP_WAIT_00_QA",
            "released_by_00": True,
            "release_gate_argument": "--released-by-00",
            "package_root": str(package_root),
            "package_tree_sha256": package.package_hash,
            "source_master_sha256": package.source_master_hash,
            "git_commit": git_commit,
            "period": {"from": START, "through": END, "warmup_from": package.raw_bars[0].date},
            "slippage_scenarios_bps": list(SLIPPAGE_SCENARIOS_BPS),
            "main_scenario_bps": MAIN_SCENARIO_BPS,
            "strategy_ids": [spec.strategy_id for spec in STRATEGY_SPECS],
            "benchmark_ids": ["BUY_HOLD_CASH", "BUY_HOLD_REINVEST"],
            "source_group": "0050 ETF 單一來源；不可與股票七來源報告直接相加",
            "split_acceptance": split_acceptance,
            "immutable_tree_sha256_before": immutable_before,
            "immutable_tree_sha256_after": source_after_stage,
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        }
        _write_json(stage / FORMAL_MANIFEST, manifest)
        mismatches = _verify_rows(stage, artifacts)
        readback = {
            "schema_version": "dual-ma-0050-formal-readback-v3",
            "status": "PASS" if not mismatches else "FAIL",
            "mismatches": mismatches,
            "artifact_hashes_verified": not mismatches,
            "source_hashes_unchanged": source_after_stage == immutable_before,
            "annual_report_count": sum((stage / f"{year}-report.md").is_file() for year in YEARS),
            "overview_present": (stage / "2022-2026-overview.md").is_file(),
            "fixed_seed_audit_status": "PASS",
            "split_acceptance_status": split_acceptance["status"],
            "recompute_status": recompute["status"],
        }
        if readback["status"] != "PASS":
            raise ETF0050Error(f"formal readback failed: {mismatches}")
        _write_json(stage / FORMAL_READBACK, readback)

        output_root.mkdir(parents=True, exist_ok=True)
        for source in sorted(stage.rglob("*")):
            if source.is_dir():
                continue
            relative = source.relative_to(stage)
            target = output_root / relative
            if target.exists():
                raise FileExistsError(f"refusing to overwrite output: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    final_immutable = {
        "old_stock_reports": tree_hash(old_reports_root, exclude=output_root),
        "etf_package_v1": tree_hash(v1),
        "etf_package_v2": tree_hash(v2),
        "etf_package_v3": tree_hash(package_root),
    }
    if final_immutable != immutable_before:
        raise ETF0050Error("immutable trees changed after artifact publication")
    return {
        "status": "STOP_WAIT_00_QA",
        "formal_manifest": str(output_root / FORMAL_MANIFEST),
        "formal_manifest_sha256": sha256_file(output_root / FORMAL_MANIFEST),
        "formal_readback": str(output_root / FORMAL_READBACK),
        "formal_readback_sha256": sha256_file(output_root / FORMAL_READBACK),
        "tests_required": True,
        "annual_reports": [str(output_root / f"{year}-report.md") for year in YEARS],
        "overview": str(output_root / "2022-2026-overview.md"),
        "metrics_rows": len(all_metrics),
        "trade_detail_rows": len(all_details),
    }
