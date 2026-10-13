"""0050 signal-regime sizing x isolated buy-and-hold core research.

This is a candidate-only study.  It freezes the accepted v1.7 signals and
exits, changes only the fraction of tactical cash used by HIGH-regime entries,
and optionally combines that tactical book with a separately accounted 20%
dividend-reinvesting core sleeve.  The accepted packages are read-only pins.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import statistics
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_2006_fixed_methods_v1 as accepted
from . import etf_0050_execution_stability_v1 as analysis
from . import etf_0050_ma21_ma144_regime_v1 as regime
from . import etf_0050_regime_entry_exit_comparison_v1 as prior_research
from .costs import CATHAY_WEB_028_TW_V1, size_entry
from .etf_0050_base_v1 import ETF0050Error, StrategyRunSlice
from .etf_0050_formal_v2 import (
    CONTINUOUS_CAPITAL,
    _artifact_rows,
    _entry_stop_in_raw_coordinate,
    _execution_trace,
    _mfe_mae,
    _verify_artifacts,
    _write_csv,
    _write_json,
    annualized_return,
    validate_cash_safe_metric_semantics,
)
from .models import InstrumentType
from .paths import dev_run, report_path


VERSION = "dual-ma-0050-regime-sizing-core-v1.0"
START = accepted.FORMAL_START
END = accepted.CUTOFF
CAPITAL = CONTINUOUS_CAPITAL
HIGH_WEIGHTS = (50, 75, 100)
CORE_WEIGHTS = (0, 20)
SLIPPAGE_BPS = (20, 40, 60)
TIMING_NEXT_OPEN = analysis.TIMING_NEXT_OPEN
TIMING_SIGNAL_CLOSE = analysis.TIMING_SIGNAL_CLOSE
STOP_CODE = "STOP_WAIT_00_0050_REGIME_SIZING_CORE_QA"

RUN_ROOT_DEFAULT = dev_run("02-etf-0050-regime-sizing-core-v1")
REPORT_ROOT_DEFAULT = report_path(
    "etf", "0050", "experiments", "2006-2026-regime-sizing-core-v1"
)
SEALED_RUN = dev_run("02-etf-0050-final-strategy-v1-7")
SEALED_REPORT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-7")
SEALED_RUN_MANIFEST = "7a560d7f23f7f6e222573abca6b3d66da7caac515727acc15200f04ab479b174"
SEALED_REPORT_MANIFEST = "fc7181a77496827996650708df97ee3ba9bb8d58132b035f6455c5c67d1e8b7f"
SEALED_READBACK = "366fdcc7bb4c554f467c0942e9b85c652f7c37b82a070e1c3878634586751c94"
PRIOR_RUN = dev_run("02-etf-0050-regime-entry-exit-comparison-v1-3-qa3")
PRIOR_REPORT = report_path(
    "etf", "0050", "experiments", "2006-2026-regime-entry-exit-comparison-v1-3-qa3"
)
PRIOR_RUN_MANIFEST = "35ef1ece6307733b0c31e36450b5a97feaf16e79f576faf15aaa676e5667223c"
PRIOR_RUN_READBACK = "bad39956584592b4824a9bd6a17a12183090b9476ace06c93b2c501fb0e89500"
PRIOR_REPORT_MANIFEST = "1696023369d84ebf5b38f008afcec8137fe2587c7a484617942f685883d017b5"
PRIOR_REPORT_READBACK = "ab4a9b0a5222d080198e9dd57d73d368d9b151894982545e5a0714bd41b7aa30"


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _canonical_digest(value: Any) -> str:
    def clean(item: Any) -> Any:
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Mapping):
            return {str(key): clean(val) for key, val in sorted(item.items())}
        if isinstance(item, (list, tuple)):
            return [clean(val) for val in item]
        return item
    payload = json.dumps(clean(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_source_pins() -> dict[str, Any]:
    pins = {
        "v1_7_run_manifest": (SEALED_RUN / "manifest.json", SEALED_RUN_MANIFEST),
        "v1_7_report_manifest": (SEALED_REPORT / "artifact-manifest.json", SEALED_REPORT_MANIFEST),
        "v1_7_run_readback": (SEALED_RUN / "readback.json", SEALED_READBACK),
        "v1_7_report_readback": (SEALED_REPORT / "readback.json", SEALED_READBACK),
        "prior_run_manifest": (PRIOR_RUN / "artifact-manifest.json", PRIOR_RUN_MANIFEST),
        "prior_run_readback": (PRIOR_RUN / "readback.json", PRIOR_RUN_READBACK),
        "prior_report_manifest": (PRIOR_REPORT / "artifact-manifest.json", PRIOR_REPORT_MANIFEST),
        "prior_report_readback": (PRIOR_REPORT / "readback.json", PRIOR_REPORT_READBACK),
    }
    checks = {
        name: {"path": str(path), "expected": expected, "actual": _sha(path)}
        for name, (path, expected) in pins.items()
    }
    if any(row["actual"].lower() != row["expected"].lower() for row in checks.values()):
        raise ETF0050Error(f"source pin mismatch: {checks}")
    manifests = (
        (SEALED_RUN, "manifest.json"),
        (SEALED_REPORT, "artifact-manifest.json"),
        (PRIOR_RUN, "artifact-manifest.json"),
        (PRIOR_REPORT, "artifact-manifest.json"),
    )
    artifact_checks = []
    for root, name in manifests:
        manifest = json.loads((root / name).read_text(encoding="utf-8-sig"))
        mismatches = _verify_artifacts(root, manifest["artifacts"])
        artifact_checks.append({"root": str(root), "mismatches": mismatches})
        if mismatches:
            raise ETF0050Error(f"source artifact mismatch: {root}: {mismatches}")
    return {"status": "PASS_EXACT", "pins": checks, "artifact_checks": artifact_checks}


def annotate_signal_regime(
    run: StrategyRunSlice, indicator_by_date: Mapping[date, Mapping[str, Any]],
) -> StrategyRunSlice:
    """Attach entry-time MA regime; equality is HIGH and remains fixed for the leg."""
    leg_regime: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for source in run.transactions:
        row = dict(source)
        leg_id = str(row["leg_id"])
        if row["role"] == "MOTHER":
            indicator = indicator_by_date[row["signal_date"]]
            if indicator.get("sma21") is None or indicator.get("sma144") is None:
                raise ETF0050Error(f"entry regime unavailable: {row['signal_date']}")
            label = "HIGH" if _d(indicator["sma21"]) >= _d(indicator["sma144"]) else "LOW"
            row.update({
                "signal_regime": label,
                "signal_sma21": indicator["sma21"],
                "signal_sma144": indicator["sma144"],
                "regime_equality_is_high": _d(indicator["sma21"]) == _d(indicator["sma144"]),
            })
            leg_regime[leg_id] = label
        else:
            if leg_id not in leg_regime:
                raise ETF0050Error(f"exit has no annotated entry: {leg_id}")
            row["signal_regime"] = leg_regime[leg_id]
        rows.append(row)
    return StrategyRunSlice(run.spec, run.result, run.decisions, tuple(rows), run.legs)


def replay_regime_sizing(
    *, package: Any, run: StrategyRunSlice, start: date, end: date,
    slippage_bps: int, initial_capital: Decimal, high_weight_pct: int,
) -> dict[str, Any]:
    """Replay frozen orders, using LOW=100% and HIGH={50,75,100}% cash."""
    if high_weight_pct not in HIGH_WEIGHTS:
        raise ValueError(f"unsupported HIGH weight: {high_weight_pct}")
    model = CATHAY_WEB_028_TW_V1.with_research_slippage(slippage_bps)
    sessions = [day for day in package.sessions if start <= day <= end]
    if not sessions:
        raise ETF0050Error("regime sizing replay has no sessions")
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
            splits.append({"date": day, "type": "SPLIT_REBASE_ONLY",
                           "ratio": package.split.ratio, "shares_before": before,
                           "shares_after": after, "leg_id": active_leg,
                           "transaction": False, "pnl": Decimal("0")})
        event = ex_by_day.get(day)
        if event is not None:
            shares = 0 if active_leg is None else int(records[active_leg]["shares"])
            right = {"ex_date": event.ex_date, "payment_date": event.payment_date,
                     "shares": shares, "leg_id": active_leg,
                     "amount_per_share": event.amount_per_share,
                     "cash": Decimal(shares) * event.amount_per_share}
            rights[event.payment_date].append(right)
            dividends.append({**right, "stage": "ENTITLEMENT_LOCKED"})
        for event in pay_by_day.get(day, []):
            matches = [item for item in rights.get(day, []) if item["ex_date"] == event.ex_date]
            right = matches[0] if matches else {
                "ex_date": event.ex_date, "payment_date": event.payment_date,
                "shares": 0, "leg_id": None, "amount_per_share": event.amount_per_share,
                "cash": Decimal("0"),
            }
            cash += _d(right["cash"])
            if right["leg_id"] is not None:
                records[str(right["leg_id"])]["dividend_payments"].append(dict(right))
            dividends.append({**right, "stage": "CASH_PAID"})

        ordered = sorted(tx_by_day.get(day, []), key=lambda item: 0 if item["role"] == "EXIT" else 1)
        for frozen in ordered:
            leg_id = str(frozen["leg_id"])
            if frozen["role"] == "EXIT":
                if active_leg != leg_id:
                    skipped.append({"date": day, "leg_id": leg_id,
                                    "reason": "對應買進未成交，故不執行賣出"})
                    continue
                record = records[leg_id]
                shares = int(record["shares"])
                order = model.sell(_d(frozen["reference_price"]), shares, InstrumentType.ETF)
                cash += order.cash_amount
                record.update({"exit_date": day, "exit_reason": frozen.get("exit_reason"),
                               "exit_reference_price": order.reference_price,
                               "exit_fill_price": order.fill_price,
                               "sell_gross": order.gross, "sell_commission": order.commission,
                               "sell_tax": order.tax, "sell_net": order.cash_amount,
                               "final_shares": shares})
                transactions.append({
                    "date": day, "role": "EXIT", "status": "FILLED", "leg_id": leg_id,
                    "reference_price": order.reference_price, "fill_price": order.fill_price,
                    "shares": shares, "gross": order.gross, "commission": order.commission,
                    "tax": order.tax, "cash_amount": order.cash_amount,
                    "exit_reason": frozen.get("exit_reason"),
                    "signal_regime": record["signal_regime"],
                    "entry_budget_fraction": record["entry_budget_fraction"],
                    **_execution_trace(frozen),
                })
                active_leg = None
            elif frozen.get("status") == "FILLED":
                if active_leg is not None:
                    raise ETF0050Error("frozen decisions opened a second position")
                label = str(frozen["signal_regime"])
                fraction = Decimal("1") if label == "LOW" else Decimal(high_weight_pct) / 100
                available_before = cash
                budget = available_before * fraction
                sized = size_entry(
                    _d(frozen["reference_price"]), _entry_stop_in_raw_coordinate(frozen),
                    InstrumentType.ETF, cost_model=model, max_total_cost=budget,
                    max_initial_r=budget * Decimal("0.20"), max_shares=1_000_000,
                )
                if sized is None:
                    skipped.append({"date": day, "leg_id": leg_id,
                                    "reason": "可用預算不足以買進至少一股，或風險上限不允許",
                                    "available_cash": cash, "target_budget": budget,
                                    "signal_regime": label})
                    continue
                cash -= sized.buy.cash_amount
                cumulative_buy += sized.buy.cash_amount
                if sized.buy.cash_amount > peak_invested:
                    peak_invested, peak_invested_date = sized.buy.cash_amount, day
                records[leg_id] = {
                    "leg_id": leg_id, "entry_date": day,
                    "entry_reference_price": sized.buy.reference_price,
                    "entry_fill_price": sized.buy.fill_price,
                    "entry_shares": sized.shares, "shares": sized.shares,
                    "buy_gross": sized.buy.gross, "buy_commission": sized.buy.commission,
                    "buy_total": sized.buy.cash_amount,
                    "initial_stop_raw": sized.estimated_stop_sale.reference_price,
                    "initial_r": sized.initial_r, "dividend_payments": [],
                    "exit_date": None, "signal_regime": label,
                    "entry_budget_fraction": fraction,
                    "available_cash_before_entry": available_before,
                    "target_budget": budget,
                }
                transactions.append({
                    "date": day, "role": "MOTHER", "status": "FILLED", "leg_id": leg_id,
                    "reference_price": sized.buy.reference_price, "fill_price": sized.buy.fill_price,
                    "shares": sized.shares, "gross": sized.buy.gross,
                    "commission": sized.buy.commission, "tax": sized.buy.tax,
                    "cash_amount": sized.buy.cash_amount,
                    "initial_stop_raw": sized.estimated_stop_sale.reference_price,
                    "initial_r": sized.initial_r, "signal_regime": label,
                    "entry_budget_fraction": fraction,
                    "available_cash_before_entry": available_before,
                    "target_budget": budget, "unused_cash_after_entry": cash,
                    **_execution_trace(frozen),
                })
                active_leg = leg_id

        min_cash = min(min_cash, cash)
        if cash < 0:
            raise ETF0050Error(f"cash became negative on {day}: {cash}")
        shares = 0 if active_leg is None else int(records[active_leg]["shares"])
        liquidation = (model.sell(package.raw_by_date[day].close, shares,
                                  InstrumentType.ETF).cash_amount if shares else Decimal("0"))
        equity = cash + liquidation
        peak_equity = max(peak_equity, equity)
        drawdown = equity / peak_equity - 1 if peak_equity else Decimal("0")
        max_drawdown = min(max_drawdown, drawdown)
        curve.append({"date": day, "cash": cash, "shares": shares,
                      "raw_close": package.raw_by_date[day].close,
                      "liquidation_value": liquidation, "equity": equity,
                      "drawdown": drawdown, "active_leg_id": active_leg,
                      "active_signal_regime": (None if active_leg is None
                                               else records[active_leg]["signal_regime"])})

    details: list[dict[str, Any]] = []
    realized = Decimal("0")
    cutoff_estimate = Decimal("0")
    returns: list[Decimal] = []
    mfes: list[Decimal] = []
    maes: list[Decimal] = []
    closed_returns: list[Decimal] = []
    for record in records.values():
        dividends_received = sum((_d(item["cash"]) for item in record["dividend_payments"]), Decimal("0"))
        mfe, mae = _mfe_mae(package=package, record=record, end=end,
                            bps=slippage_bps, research_slippage=True)
        if record.get("exit_date") is not None:
            pnl = _d(record["sell_net"]) + dividends_received - _d(record["buy_total"])
            realized += pnl
            status = "已賣出"
            exit_reference, exit_fill = record["exit_reference_price"], record["exit_fill_price"]
            final_shares = int(record["final_shares"])
            closed_returns.append(pnl / _d(record["buy_total"]))
        else:
            final_shares = int(record["shares"])
            mark = model.sell(package.raw_by_date[end].close, final_shares, InstrumentType.ETF)
            pnl = mark.cash_amount + dividends_received - _d(record["buy_total"])
            cutoff_estimate += pnl
            status = "截至截止日仍持有（按收盤價估算）"
            exit_reference, exit_fill = mark.reference_price, mark.fill_price
            record["sell_commission"], record["sell_tax"] = mark.commission, mark.tax
        rate = pnl / _d(record["buy_total"])
        returns.append(rate); mfes.append(mfe); maes.append(mae)
        details.append({
            "strategy_id": run.spec.strategy_id, "strategy_label": run.spec.label,
            "leg_id": record["leg_id"], "status_zh": status,
            "entry_date": record["entry_date"],
            "entry_raw_price": record["entry_reference_price"],
            "entry_fill_price": record["entry_fill_price"],
            "initial_stop": record["initial_stop_raw"],
            "entry_shares": record["entry_shares"], "final_shares": final_shares,
            "buy_gross": record["buy_gross"], "buy_commission": record["buy_commission"],
            "buy_total": record["buy_total"], "exit_date": record.get("exit_date"),
            "exit_reason": record.get("exit_reason"),
            "exit_raw_or_cutoff_price": exit_reference,
            "exit_fill_or_mark_price": exit_fill,
            "sell_commission": record.get("sell_commission", Decimal("0")),
            "sell_tax": record.get("sell_tax", Decimal("0")),
            "cash_dividends": dividends_received, "net_pnl_or_estimate": pnl,
            "return_rate_or_estimate": rate, "mfe": mfe, "mae": mae,
            "signal_regime": record["signal_regime"],
            "entry_budget_fraction": record["entry_budget_fraction"],
            "available_cash_before_entry": record["available_cash_before_entry"],
            "target_budget": record["target_budget"],
        })

    ending = _d(curve[-1]["equity"])
    combined = ending - initial_capital
    detail_total = sum((_d(item["net_pnl_or_estimate"]) for item in details), Decimal("0"))
    if abs(detail_total - combined) > Decimal("0.000001"):
        raise ETF0050Error(f"detail/account reconciliation failed: {detail_total} != {combined}")
    fees_tax = sum((_d(row.get("commission", 0)) + _d(row.get("tax", 0))
                    for row in transactions), Decimal("0"))
    slippage = sum((abs(_d(row["fill_price"]) - _d(row["reference_price"])) * int(row["shares"])
                    for row in transactions), Decimal("0"))
    cutoff_fees = sum((_d(row["sell_commission"]) + _d(row["sell_tax"])
                       for row in details if row["status_zh"] != "已賣出"), Decimal("0"))
    cutoff_slippage = sum((abs(_d(row["exit_fill_or_mark_price"]) - _d(row["exit_raw_or_cutoff_price"]))
                           * int(row["final_shares"]) for row in details
                           if row["status_zh"] != "已賣出"), Decimal("0"))
    idle = Decimal(str(statistics.fmean(
        float(max(_d(row["cash"]), Decimal("0")) / _d(row["equity"]))
        if _d(row["equity"]) > 0 else 0 for row in curve
    )))
    metrics = {
        "strategy_id": run.spec.strategy_id, "strategy_label": run.spec.label,
        "slippage_bps": slippage_bps, "external_principal": initial_capital,
        "additional_top_up_or_borrowing": Decimal("0"), "rounds": len(details),
        "closed_rounds": len(closed_returns), "open_rounds": len(details) - len(closed_returns),
        "win_rate": (Decimal(sum(value > 0 for value in closed_returns)) / len(closed_returns)
                     if closed_returns else None), "cumulative_buy": cumulative_buy,
        "peak_concurrent_investment": peak_invested,
        "peak_investment_date": peak_invested_date, "realized_net_pnl": realized,
        "cutoff_estimated_pnl": cutoff_estimate, "combined_pnl": combined,
        "profit_per_100_invested": combined / cumulative_buy * 100 if cumulative_buy else None,
        "ending_equity": ending, "total_return": ending / initial_capital - 1,
        "cagr": annualized_return(initial_capital, ending, start, end),
        "max_asset_drawdown": max_drawdown, "idle_cash_ratio": idle,
        "average_return": sum(returns, Decimal("0")) / len(returns) if returns else None,
        "median_return": Decimal(str(statistics.median(returns))) if returns else None,
        "max_trade_win": max(returns) if returns else None,
        "max_trade_loss": min(returns) if returns else None,
        "average_mfe": sum(mfes, Decimal("0")) / len(mfes) if mfes else None,
        "average_mae": sum(maes, Decimal("0")) / len(maes) if maes else None,
        "fees_and_tax": fees_tax, "slippage_cost": slippage,
        "actual_fees_and_tax": fees_tax, "actual_slippage_cost": slippage,
        "cutoff_estimated_sale_fees_and_tax": cutoff_fees,
        "cutoff_estimated_sale_slippage_cost": cutoff_slippage,
        "transaction_count": len(transactions),
        "skipped_for_cash": sum("不足" in row["reason"] for row in skipped),
        "minimum_cash": min_cash,
        "dividends_received": sum((_d(row["cash"]) for row in dividends
                                   if row["stage"] == "CASH_PAID"), Decimal("0")),
        "start_date": start, "end_date": end, "final_shares": int(curve[-1]["shares"]),
        "high_weight_pct": high_weight_pct,
    }
    validate_cash_safe_metric_semantics(metrics)
    return {"metrics": metrics, "details": details, "transactions": transactions,
            "dividends": dividends, "splits": splits, "curve": curve, "skipped": skipped}


def scenario_id(core_pct: int, high_pct: int, timing: str, bps: int) -> str:
    return f"CORE{core_pct}_HIGH{high_pct}_LOW100_{timing}_{bps}BPS"


def scenario_name(core_pct: int, high_pct: int) -> str:
    return f"核心{core_pct}%／高檔投入{high_pct}%／低檔投入100%"


def _top5_concentration(details: Sequence[Mapping[str, Any]]) -> Decimal | None:
    profits = sorted((_d(row["net_pnl_or_estimate"]) for row in details
                      if row.get("exit_date") not in (None, "")
                      and _d(row["net_pnl_or_estimate"]) > 0), reverse=True)
    return sum(profits[:5], Decimal("0")) / sum(profits, Decimal("0")) if profits else None


def _worst_rolling(rows: Sequence[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    rolling = analysis.rolling_performance(rows)
    return {years: min((row for row in rolling if int(row["window_years"]) == years),
                       key=lambda row: _d(row["cagr"])) for years in (3, 5, 10)}


def build_scenario(
    *, package: Any, run: StrategyRunSlice, core_pct: int, high_pct: int,
    timing: str, bps: int,
) -> dict[str, Any]:
    tactical_capital = CAPITAL * Decimal(100 - core_pct) / 100
    tactical = replay_regime_sizing(
        package=package, run=run, start=START, end=END, slippage_bps=bps,
        initial_capital=tactical_capital, high_weight_pct=high_pct,
    )
    core = (analysis._zero_sleeve(package) if core_pct == 0 else
            analysis.replay_core_sleeve(package=package,
                                        initial_capital=CAPITAL * Decimal(core_pct) / 100,
                                        slippage_bps=bps))
    combined, combined_metrics = analysis.combine_isolated_sleeves(
        core=core, tactical=tactical, core_weight_pct=core_pct,
    )
    # The shared sleeve helper predates the accepted v1.7 inclusive-day CAGR
    # convention.  Keep all six candidates on the sealed metric definition.
    combined_metrics["cagr"] = annualized_return(
        CAPITAL, _d(combined_metrics["ending_equity"]), START, END,
    )
    worst = _worst_rolling(combined)
    stability = analysis.trade_stability(tactical["details"])
    sid = scenario_id(core_pct, high_pct, timing, bps)
    name = scenario_name(core_pct, high_pct)
    calmar = (_d(combined_metrics["cagr"]) / abs(_d(combined_metrics["max_drawdown"]))
              if _d(combined_metrics["max_drawdown"]) != 0 else None)
    summary = {
        "scenario_id": sid, "scenario_name_zh": name, "core_weight_pct": core_pct,
        "tactical_weight_pct": 100 - core_pct, "high_weight_pct": high_pct,
        "low_weight_pct": 100, "execution_timing": timing, "slippage_bps": bps,
        **combined_metrics, "calmar": calmar,
        "worst_complete_3y_cagr": worst[3]["cagr"],
        "worst_complete_3y_start": worst[3]["start_date"],
        "worst_complete_3y_end": worst[3]["end_date"],
        "worst_complete_5y_cagr": worst[5]["cagr"],
        "worst_complete_5y_start": worst[5]["start_date"],
        "worst_complete_5y_end": worst[5]["end_date"],
        "worst_complete_10y_cagr": worst[10]["cagr"],
        "worst_complete_10y_start": worst[10]["start_date"],
        "worst_complete_10y_end": worst[10]["end_date"],
        **stability, "top5_profit_concentration": _top5_concentration(tactical["details"]),
        "tactical_ending_equity": tactical["metrics"]["ending_equity"],
        "tactical_idle_cash_ratio": tactical["metrics"]["idle_cash_ratio"],
        "tactical_actual_fees_tax": tactical["metrics"]["actual_fees_and_tax"],
        "tactical_actual_slippage": tactical["metrics"]["actual_slippage_cost"],
        "tactical_rounds": tactical["metrics"]["rounds"],
        "tactical_closed_rounds": tactical["metrics"]["closed_rounds"],
        "tactical_open_rounds": tactical["metrics"]["open_rounds"],
    }
    daily = [{"scenario_id": sid, "scenario_name_zh": name,
              "execution_timing": timing, "slippage_bps": bps,
              "core_weight_pct": core_pct, "high_weight_pct": high_pct,
              **row} for row in combined]
    details = [{"scenario_id": sid, "scenario_name_zh": name,
                "execution_timing": timing, "slippage_bps": bps,
                "core_weight_pct": core_pct, "high_weight_pct": high_pct,
                **row} for row in tactical["details"]]
    transactions = [{"scenario_id": sid, "scenario_name_zh": name,
                     "sleeve": "TACTICAL", "execution_timing": timing,
                     "slippage_bps": bps, "core_weight_pct": core_pct,
                     "high_weight_pct": high_pct, **row}
                    for row in tactical["transactions"]]
    transactions += [{"scenario_id": sid, "scenario_name_zh": name,
                      "sleeve": "CORE", "execution_timing": timing,
                      "slippage_bps": bps, "core_weight_pct": core_pct,
                      "high_weight_pct": high_pct, **row}
                     for row in core["transactions"]]
    dividends = [{"scenario_id": sid, "scenario_name_zh": name,
                  "sleeve": sleeve, "execution_timing": timing,
                  "slippage_bps": bps, "core_weight_pct": core_pct,
                  "high_weight_pct": high_pct, **row}
                 for sleeve, source in (("TACTICAL", tactical), ("CORE", core))
                 for row in source["dividends"]]
    return {"summary": summary, "daily": daily, "details": details,
            "transactions": transactions, "dividends": dividends,
            "tactical": tactical, "core": core}


def run_matrix(*, package: Any, raw_rows: Sequence[Mapping[str, Any]],
               events: Sequence[Any]) -> dict[str, Any]:
    indicators = regime.build_indicator_rows(raw_rows, events)
    indicator_by_date = {row["date"]: row for row in indicators}
    frozen, _daily, _events = regime.build_regime_run(
        indicator_rows=indicators, raw_by_date=package.raw_by_date,
        strategy_id=regime.FIXED_ID, moving_stop=False,
        start=START, end=END, slippage_bps=20,
    )
    scheduled: dict[str, StrategyRunSlice] = {}
    audits: list[dict[str, Any]] = []
    for timing in (TIMING_NEXT_OPEN, TIMING_SIGNAL_CLOSE):
        delayed, audit = analysis.schedule_frozen_orders(
            run=frozen, sessions=package.sessions, raw_by_date=package.raw_by_date,
            indicator_by_date=indicator_by_date, timing=timing,
        )
        scheduled[timing] = annotate_signal_regime(delayed, indicator_by_date)
        audits.extend({"execution_timing": timing, **row} for row in audit)
    scenarios: list[dict[str, Any]] = []
    for core_pct in CORE_WEIGHTS:
        for high_pct in HIGH_WEIGHTS:
            for bps in SLIPPAGE_BPS:
                scenarios.append(build_scenario(package=package,
                                                run=scheduled[TIMING_NEXT_OPEN],
                                                core_pct=core_pct, high_pct=high_pct,
                                                timing=TIMING_NEXT_OPEN, bps=bps))
            scenarios.append(build_scenario(package=package,
                                            run=scheduled[TIMING_SIGNAL_CLOSE],
                                            core_pct=core_pct, high_pct=high_pct,
                                            timing=TIMING_SIGNAL_CLOSE, bps=20))
    return {"scenarios": scenarios, "execution_audit": audits,
            "scheduled": scheduled, "indicators": indicators}


def exact_v17_parity(scenario: Mapping[str, Any]) -> dict[str, Any]:
    sealed_summary = _read_csv(SEALED_RUN / "next-session-open-scenario-summary.csv")[0]
    sealed_daily = _read_csv(SEALED_RUN / "next-session-open-daily-equity.csv")
    sealed_tx = _read_csv(SEALED_RUN / "next-session-open-transactions.csv")
    sealed_details = _read_csv(SEALED_RUN / "next-session-open-trade-details.csv")
    metric_fields = [key for key in sealed_summary if key not in
                     {"execution_timing", "scenario_id"}]
    actual_metrics = scenario["tactical"]["metrics"]
    metric_mismatches = []
    for field in metric_fields:
        actual = actual_metrics.get(field)
        expected = sealed_summary[field]
        actual_text = "" if actual is None else str(actual)
        if actual_text != expected:
            metric_mismatches.append({"field": field, "expected": expected,
                                      "actual": actual_text})
    for combined_field, sealed_field in (
        ("ending_equity", "ending_equity"), ("total_return", "total_return"),
        ("cagr", "cagr"), ("max_drawdown", "max_asset_drawdown"),
    ):
        actual_text = str(scenario["summary"][combined_field])
        expected = sealed_summary[sealed_field]
        if actual_text != expected:
            metric_mismatches.append({"field": "combined_" + combined_field,
                                      "expected": expected, "actual": actual_text})
    daily_fields = ["date", "cash", "shares", "raw_close", "liquidation_value",
                    "equity", "drawdown", "active_leg_id"]
    actual_daily = scenario["tactical"]["curve"]
    daily_mismatches = []
    if len(actual_daily) != len(sealed_daily):
        daily_mismatches.append({"field": "row_count", "expected": len(sealed_daily),
                                 "actual": len(actual_daily)})
    for index, (actual, expected) in enumerate(zip(actual_daily, sealed_daily)):
        for field in daily_fields:
            value = actual.get(field)
            shown = "" if value is None else str(value)
            if shown != expected[field]:
                daily_mismatches.append({"row": index, "field": field,
                                         "expected": expected[field], "actual": shown})
                if len(daily_mismatches) >= 20:
                    break
        if len(daily_mismatches) >= 20:
            break
    def compare_rows(actual_rows: Sequence[Mapping[str, Any]], expected_rows: Sequence[Mapping[str, str]],
                     omitted: set[str]) -> list[dict[str, Any]]:
        errors = []
        if len(actual_rows) != len(expected_rows):
            errors.append({"field": "row_count", "expected": len(expected_rows),
                           "actual": len(actual_rows)})
        for index, (actual, expected) in enumerate(zip(actual_rows, expected_rows)):
            for field in expected:
                if field in omitted:
                    continue
                value = actual.get(field)
                shown = "" if value is None else str(value)
                if shown != expected[field]:
                    errors.append({"row": index, "field": field,
                                   "expected": expected[field], "actual": shown})
                    if len(errors) >= 20:
                        return errors
        return errors
    tx_mismatches = compare_rows(
        scenario["tactical"]["transactions"], sealed_tx,
        {"execution_timing", "scenario_id", "slippage_bps", "strategy_id"},
    )
    detail_mismatches = compare_rows(
        scenario["tactical"]["details"], sealed_details,
        {"execution_timing", "scenario_id", "slippage_bps"},
    )
    status = "PASS_EXACT" if not (metric_mismatches or daily_mismatches or
                                  tx_mismatches or detail_mismatches) else "FAIL"
    return {"status": status, "metric_mismatches": metric_mismatches,
            "daily_mismatches": daily_mismatches, "transaction_mismatches": tx_mismatches,
            "detail_mismatches": detail_mismatches,
            "rows": {"daily": len(actual_daily),
                     "transactions": len(scenario["tactical"]["transactions"]),
                     "details": len(scenario["tactical"]["details"])}}


def _filter_run(run: StrategyRunSlice, removed: set[str]) -> StrategyRunSlice:
    return StrategyRunSlice(run.spec, run.result, run.decisions,
                            tuple(row for row in run.transactions
                                  if str(row.get("leg_id")) not in removed), run.legs)


def concentration_rows(*, package: Any, matrix: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    main = [item for item in matrix["scenarios"]
            if item["summary"]["execution_timing"] == TIMING_NEXT_OPEN
            and int(item["summary"]["slippage_bps"]) == 20]
    scheduled = matrix["scheduled"][TIMING_NEXT_OPEN]
    for source in main:
        summary = source["summary"]
        closed = sorted((row for row in source["tactical"]["details"]
                         if row.get("exit_date") not in (None, "")),
                        key=lambda row: _d(row["net_pnl_or_estimate"]), reverse=True)
        for count in (0, 1, 3, 5):
            removed = {str(row["leg_id"]) for row in closed[:count]}
            replay = build_scenario(
                package=package, run=_filter_run(scheduled, removed),
                core_pct=int(summary["core_weight_pct"]),
                high_pct=int(summary["high_weight_pct"]),
                timing=TIMING_NEXT_OPEN, bps=20,
            )
            rows.append({"scenario_id": summary["scenario_id"],
                         "scenario_name_zh": summary["scenario_name_zh"],
                         "removed_top_profit_trades": count,
                         "removed_leg_ids": ";".join(sorted(removed)),
                         "ending_equity": replay["summary"]["ending_equity"],
                         "cagr": replay["summary"]["cagr"],
                         "max_drawdown": replay["summary"]["max_drawdown"],
                         "ending_equity_change": (_d(replay["summary"]["ending_equity"])
                                                   - _d(summary["ending_equity"]))})
    return rows


def _scenario_tables(matrix: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    scenarios = matrix["scenarios"]
    main = [item for item in scenarios if item["summary"]["execution_timing"] == TIMING_NEXT_OPEN
            and int(item["summary"]["slippage_bps"]) == 20]
    annual: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    rolling: list[dict[str, Any]] = []
    regime_attr: list[dict[str, Any]] = []
    for item in main:
        sid, name = item["summary"]["scenario_id"], item["summary"]["scenario_name_zh"]
        annual.extend({"scenario_id": sid, "scenario_name_zh": name, **row}
                      for row in analysis.annual_performance(item["daily"]))
        periods.extend({"scenario_id": sid, "scenario_name_zh": name, **row}
                       for row in analysis.period_performance(item["daily"]))
        rolling.extend({"scenario_id": sid, "scenario_name_zh": name, **row}
                       for row in analysis.rolling_performance(item["daily"]))
        for label in ("HIGH", "LOW"):
            bucket = [row for row in item["tactical"]["details"]
                      if row["signal_regime"] == label and row.get("exit_date") not in (None, "")]
            profits = [_d(row["net_pnl_or_estimate"]) for row in bucket]
            regime_attr.append({"scenario_id": sid, "scenario_name_zh": name,
                                "signal_regime": label, "closed_trades": len(bucket),
                                "wins": sum(value > 0 for value in profits),
                                "net_pnl": sum(profits, Decimal("0")),
                                "average_trade_pnl": (sum(profits, Decimal("0")) / len(profits)
                                                      if profits else None)})
    cost = [item["summary"] for item in scenarios
            if item["summary"]["execution_timing"] == TIMING_NEXT_OPEN]
    timing = [item["summary"] for item in scenarios
              if int(item["summary"]["slippage_bps"]) == 20]
    return {"main": [item["summary"] for item in main], "annual": annual,
            "periods": periods, "rolling": rolling, "regime": regime_attr,
            "cost": cost, "timing": timing}


def _fmt_money(value: Any) -> str:
    return f"{_d(value):,.0f}"


def _fmt_pct(value: Any) -> str:
    return f"{_d(value) * 100:.2f}%"


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    return ["| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(str(value) for value in row) + " |" for row in rows)]


def report_text(*, tables: Mapping[str, Sequence[Mapping[str, Any]]],
                parity: Mapping[str, Any], sources: Mapping[str, Any],
                concentration: Sequence[Mapping[str, Any]],
                prior_row: Mapping[str, Any], benchmark_rows: Sequence[Mapping[str, Any]],
                replay_digest: str) -> str:
    main = sorted(tables["main"], key=lambda row: (-_d(row["cagr"]), _d(row["max_drawdown"])))
    best = main[0]
    baseline = next(row for row in main if int(row["core_weight_pct"]) == 0
                    and int(row["high_weight_pct"]) == 100)
    lines = ["# 0050 高低市場狀態差別投入 × 核心持有｜因果回測研究報告", "",
             f"版本：{VERSION}｜樣本：{START}～{END}｜初始本金：500,000 元｜正式口徑：次一交易日開盤、20 基點。", ""]
    sections: list[tuple[str, list[str]]] = []
    sections.append(("01｜先講結論", [
        f"六組中樣本內 CAGR 最高的是「{best['scenario_name_zh']}」，期末資產 {_fmt_money(best['ending_equity'])} 元、CAGR {_fmt_pct(best['cagr'])}、MDD {_fmt_pct(best['max_drawdown'])}、Calmar {_d(best['calmar']):.3f}。",
        f"原 v1.7 等價組合為期末 {_fmt_money(baseline['ending_equity'])} 元、CAGR {_fmt_pct(baseline['cagr'])}、MDD {_fmt_pct(baseline['max_drawdown'])}。",
        "這是同一歷史樣本內的候選比較，不足以證明未來正期望；在 00 審查前不採用、不覆寫 v1.7。",
    ]))
    sections.append(("02｜研究問題與唯一改動", [
        "只改兩件事：進場訊號日若 MA21＜MA144（低檔）投入戰術帳全部可用現金；若 MA21≥MA144（高檔，等號歸高檔）分別投入 50%、75%、100%。",
        "核心權重為 0% 或 20%；20% 核心在共同起始日以開盤價買進 0050，股利付款後次一正式交易日再投入，之後永不再平衡。",
    ]))
    sections.append(("03｜六組主結果", _md_table(
        ["組合", "期末資產", "CAGR", "MDD", "Calmar", "最長水下天數"],
        [[row["scenario_name_zh"], _fmt_money(row["ending_equity"]), _fmt_pct(row["cagr"]),
          _fmt_pct(row["max_drawdown"]), f"{_d(row['calmar']):.3f}", row["longest_underwater_days"]]
         for row in sorted(tables["main"], key=lambda item: (int(item["core_weight_pct"]),
                                                              int(item["high_weight_pct"])))],
    )))
    sections.append(("04｜每次到底投入多少", [
        "投入比例的分母是『該次進場前戰術帳可用現金』，不是固定 50 萬，也不是總合併資產。低檔固定 100%；高檔依方案為 50%、75% 或 100%。股數仍受整股、手續費與每筆初始風險不超過該次預算 20% 約束，因此實際買進金額會略低於目標預算。",
        "核心 20% 方案一開始把 100,000 元交給核心帳、400,000 元交給戰術帳；兩帳不互借、不補款、不再平衡。未投入的高檔戰術現金保留為現金。",
    ]))
    sections.append(("05｜因果與成交時序", [
        "訊號只使用訊號日收盤後已知的 MA21、MA144；市場狀態鎖在進場訊號日。主口徑到下一正式交易日開盤才成交，訊號日收盤僅是成對敏感度。",
        "持倉期間即使 MA21／MA144 跨越，也不加碼、不減碼、不強制出場；退出完全沿用封存 v1.7 決策。",
        f"v1.7 精確同值閘門：{parity['status']}。",
    ]))
    sections.append(("06｜核心帳、股利與公司行動", [
        "核心帳與戰術帳分別記現金、股數、手續費、稅、滑價、除息權利、付款與分割；每日最後才相加成總權益。",
        "除息日先鎖定權利，付款日才增加現金；核心再投入必須等付款後下一個正式交易日。分割只重算股數，不製造損益。",
        "完整紀錄：[逐日帳](daily-equity.csv)、[交易流水](transactions.csv)、[股利流水](dividend-ledger.csv)、[分割流水](corporate-actions.csv)。",
    ]))
    sections.append(("07｜高低檔歸因", _md_table(
        ["組合", "狀態", "已平倉", "勝筆", "淨損益", "平均每筆"],
        [[row["scenario_name_zh"], "高檔" if row["signal_regime"] == "HIGH" else "低檔",
          row["closed_trades"], row["wins"], _fmt_money(row["net_pnl"]),
          _fmt_money(row["average_trade_pnl"])] for row in tables["regime"]],
    )))
    sections.append(("08｜最差完整 3／5／10 年", _md_table(
        ["組合", "3 年 CAGR", "5 年 CAGR", "10 年 CAGR"],
        [[row["scenario_name_zh"], _fmt_pct(row["worst_complete_3y_cagr"]),
          _fmt_pct(row["worst_complete_5y_cagr"]), _fmt_pct(row["worst_complete_10y_cagr"])]
         for row in tables["main"]],
    ) + ["完整月末滾動窗口明細見 [rolling-3-5-10.csv](rolling-3-5-10.csv)。"]))
    sections.append(("09｜逐年與三段期間", [
        "逐年報酬與年內回撤見 [annual.csv](annual.csv)；固定三段（2006–2012、2013–2019、2020–2026/09）見 [fixed-periods.csv](fixed-periods.csv)。",
        "圖：[六組主權益曲線](six-main-equity.svg)／[逐年報酬](annual-returns.svg)。",
    ]))
    sections.append(("10｜成本與成交時點敏感度", [
        "主口徑另以 40／60 基點重跑；不是把 20 基點結果事後線性扣除。結果見 [cost-sensitivity.csv](cost-sensitivity.csv)。",
        "六組另以訊號日收盤、20 基點成對重跑，僅用於觀察成交落差，不作實務主結論。結果見 [timing-sensitivity.csv](timing-sensitivity.csv)。",
        "圖：[成本壓力](cost-sensitivity.svg)。",
    ]))
    sections.append(("11｜獲利集中與移除大贏家", [
        "每組均以已平倉淨利排序，移除前 1／3／5 筆後，用相同凍結訊號重新記帳；不是直接從期末資產減掉原損益。",
        "完整結果見 [concentration-counterfactual.csv](concentration-counterfactual.csv)。六組主結果也列出前五大獲利占全部正獲利比重與最長連敗。",
    ]))
    sections.append(("12｜與 v1.7、上一組合候選及基準比較", _md_table(
        ["對照", "期末資產", "CAGR", "MDD"],
        [["封存 v1.7 次日開盤", _fmt_money(baseline["ending_equity"]),
          _fmt_pct(baseline["cagr"]), _fmt_pct(baseline["max_drawdown"])],
         ["上一組合候選（凍結）", _fmt_money(prior_row["ending_equity"]),
          _fmt_pct(prior_row["cagr"]), _fmt_pct(prior_row["max_drawdown"])]] +
        [[row["label"], _fmt_money(row["ending_equity"]), _fmt_pct(row["cagr"]),
          _fmt_pct(row.get("max_asset_drawdown") or row["max_drawdown"])]
         for row in benchmark_rows],
    )))
    sections.append(("13｜可重現性與完整性", [
        f"兩次獨立記憶體重播的正規化 SHA-256 完全相同：`{replay_digest}`。",
        f"來源封存釘選：{sources['status']}；資料套件 SHA-256 另記於 [research-contract.json](research-contract.json)。",
        "所有輸出列入 [artifact-manifest.json](artifact-manifest.json)，再由 readback 逐檔重算；報告內部連結也另行驗證。",
        "重跑：`python -m backtests.dual_ma.etf_0050_regime_sizing_core_v1 run --run-root <run> --report-root <report>`；驗證：將 `run` 改為 `validate`。",
    ]))
    sections.append(("14｜限制、判定與待 00 決策", [
        "本研究仍是單一 ETF、單一歷史區間、同樣本內三個高檔投入比例與兩個核心比例的比較；未做樣本外鎖定、跨商品驗證或多重檢定校正。",
        "判定標準不是只看期末資產：需同時檢查 CAGR、MDD、Calmar、最差滾動期、長水下期、連敗、獲利集中、成本與成交敏感度。",
        f"目前狀態：`{STOP_CODE}`。只提交研究候選，等待 00 嚴格檢查；不採用、不合併、不改正式索引。",
    ]))
    for heading, body in sections:
        lines.extend([f"## {heading}", "", *body, ""])
    lines.extend(["## 附件索引", "",
                  "- [六組主摘要](main-six-summary.csv)",
                  "- [全部 24 情境摘要](scenario-summary.csv)",
                  "- [逐筆交易](trade-details.csv)",
                  "- [執行排程稽核](execution-audit.csv)",
                  "- [v1.7 精確同值](v1-7-exact-parity.json)",
                  "- [來源釘選](source-pins.json)",
                  "- [限制](limitations.json)", ""])
    return "\n".join(lines)


def _replay_payload(matrix: Mapping[str, Any]) -> dict[str, Any]:
    return {"summaries": [item["summary"] for item in matrix["scenarios"]],
            "daily": [row for item in matrix["scenarios"] for row in item["daily"]],
            "details": [row for item in matrix["scenarios"] for row in item["details"]],
            "transactions": [row for item in matrix["scenarios"] for row in item["transactions"]]}


def run_study(*, run_root: Path = RUN_ROOT_DEFAULT,
              report_root: Path = REPORT_ROOT_DEFAULT) -> dict[str, Any]:
    if run_root.resolve() in {SEALED_RUN.resolve(), PRIOR_RUN.resolve()} or report_root.resolve() in {
        SEALED_REPORT.resolve(), PRIOR_REPORT.resolve()
    }:
        raise ETF0050Error("output root overlaps a protected source")
    sources_before = validate_source_pins()
    package, raw_rows, events = accepted.load_package()
    first = run_matrix(package=package, raw_rows=raw_rows, events=events)
    second = run_matrix(package=package, raw_rows=raw_rows, events=events)
    digest_first, digest_second = _canonical_digest(_replay_payload(first)), _canonical_digest(_replay_payload(second))
    if digest_first != digest_second:
        raise ETF0050Error("double replay digest mismatch")
    tables = _scenario_tables(first)
    parity_source = next(item for item in first["scenarios"]
                         if item["summary"]["scenario_id"] ==
                         scenario_id(0, 100, TIMING_NEXT_OPEN, 20))
    parity = exact_v17_parity(parity_source)
    if parity["status"] != "PASS_EXACT":
        raise ETF0050Error(f"v1.7 exact parity failed: {parity}")
    concentration = concentration_rows(package=package, matrix=first)
    prior_rows = _read_csv(PRIOR_RUN / "replacement-attribution.csv")
    prior_row = next(row for row in prior_rows
                     if row["case"] == "BOTH_RULES" and row["timing"] == TIMING_NEXT_OPEN)
    sealed_main = _read_csv(SEALED_RUN / "main-comparison-summary.csv")
    benchmark_rows = [row for row in sealed_main if row["comparison_id"] in {
        "BUY_HOLD_CASH", "BUY_HOLD_REINVEST", "TAIEX_TOTAL_RETURN_EQUIVALENT_500K"
    }]
    all_summaries = [item["summary"] for item in first["scenarios"]]
    all_daily = [row for item in first["scenarios"] for row in item["daily"]]
    all_details = [row for item in first["scenarios"] for row in item["details"]]
    all_transactions = [row for item in first["scenarios"] for row in item["transactions"]]
    all_dividends = [row for item in first["scenarios"] for row in item["dividends"]]
    corporate_actions = []
    for item in first["scenarios"]:
        meta = {key: item["summary"][key] for key in (
            "scenario_id", "scenario_name_zh", "execution_timing",
            "slippage_bps", "core_weight_pct", "high_weight_pct",
        )}
        corporate_actions.extend({**meta, "sleeve": "TACTICAL", **row}
                                 for row in item["tactical"]["splits"])
        corporate_actions.extend({**meta, "sleeve": "CORE", **row}
                                 for row in item["core"]["transactions"]
                                 if row.get("role") == "SPLIT_REBASE_ONLY")
    run_root.mkdir(parents=True, exist_ok=False)
    report_root.mkdir(parents=True, exist_ok=False)
    _write_csv(run_root / "scenario-summary.csv", all_summaries)
    _write_csv(run_root / "main-six-summary.csv", tables["main"])
    _write_csv(run_root / "daily-equity.csv", all_daily)
    _write_csv(run_root / "trade-details.csv", all_details)
    _write_csv(run_root / "transactions.csv", all_transactions)
    _write_csv(run_root / "dividend-ledger.csv", all_dividends)
    _write_csv(run_root / "corporate-actions.csv", corporate_actions)
    _write_csv(run_root / "execution-audit.csv", first["execution_audit"])
    _write_csv(run_root / "annual.csv", tables["annual"])
    _write_csv(run_root / "fixed-periods.csv", tables["periods"])
    _write_csv(run_root / "rolling-3-5-10.csv", tables["rolling"])
    _write_csv(run_root / "regime-attribution.csv", tables["regime"])
    _write_csv(run_root / "cost-sensitivity.csv", tables["cost"])
    _write_csv(run_root / "timing-sensitivity.csv", tables["timing"])
    _write_csv(run_root / "concentration-counterfactual.csv", concentration)
    _write_csv(run_root / "frozen-comparisons.csv", [
        {"comparison": "上一組合候選", **prior_row},
        *({"comparison": row["label"], **row} for row in benchmark_rows),
    ])
    _write_json(run_root / "v1-7-exact-parity.json", parity)
    _write_json(run_root / "source-pins.json", sources_before)
    contract = {
        "version": VERSION, "formal_start": START, "formal_end": END,
        "initial_capital": CAPITAL, "data_pin": package.package_hash,
        "signal_regime": "HIGH iff signal-day MA21 >= MA144; equality is HIGH",
        "low_entry_fraction": "100% of tactical available cash",
        "high_entry_fractions": HIGH_WEIGHTS, "core_weights": CORE_WEIGHTS,
        "core": "initial-open 0050 buy-and-hold; dividend reinvest after payment; no rebalance",
        "position_policy": "no stacking; no cross-regime resize/add/forced exit",
        "main": "next official session open, 20bps", "stress_bps": SLIPPAGE_BPS,
        "paired_sensitivity": "signal close, 20bps", "double_replay_sha256": digest_first,
    }
    _write_json(run_root / "research-contract.json", contract)
    limitations = {"candidate_only": True, "adopted": False, "replaces_v1_7": False,
                   "limitations": [
                       "單一 ETF、單一歷史區間的樣本內比較。",
                       "三個高檔投入比例與兩個核心比例未做多重檢定校正。",
                       "尚未做封存樣本外、跨商品與即時前瞻驗證。",
                       "TAIEX 是不可直接交易且未扣成本的市場參考。",
                   ]}
    _write_json(run_root / "limitations.json", limitations)
    report = report_text(tables=tables, parity=parity, sources=sources_before,
                         concentration=concentration, prior_row=prior_row,
                         benchmark_rows=benchmark_rows, replay_digest=digest_first)
    (run_root / "README.md").write_text(report, encoding="utf-8")
    (run_root / "report.md").write_text(report, encoding="utf-8")
    main_results = [item for item in first["scenarios"]
                    if item["summary"]["execution_timing"] == TIMING_NEXT_OPEN
                    and int(item["summary"]["slippage_bps"]) == 20]
    (run_root / "six-main-equity.svg").write_text(prior_research._svg_lines(
        [(item["summary"]["scenario_name_zh"], item["daily"]) for item in main_results],
        "六組差別投入 × 核心持有權益曲線", source="main-six-summary.csv + daily-equity.csv"),
        encoding="utf-8")
    (run_root / "cost-sensitivity.svg").write_text(prior_research._svg_bars(
        rows=[(f"核{row['core_weight_pct']}-高{row['high_weight_pct']}-{row['slippage_bps']}bp",
               _d(row["ending_equity"])) for row in tables["cost"]],
        title="六組成本壓力：期末資產", source="cost-sensitivity.csv"), encoding="utf-8")
    best = max(tables["main"], key=lambda row: _d(row["cagr"]))
    annual_best = [row for row in tables["annual"] if row["scenario_id"] == best["scenario_id"]]
    (run_root / "annual-returns.svg").write_text(prior_research._svg_bars(
        rows=[(str(row["year"]), _d(row["return"])) for row in annual_best],
        title=f"{best['scenario_name_zh']}：逐年報酬", source="annual.csv", percent=True),
        encoding="utf-8")
    excluded = {"artifact-manifest.json", "readback.json", STOP_CODE + ".json"}
    run_artifacts = _artifact_rows(run_root, excluded)
    _write_json(run_root / "artifact-manifest.json", {
        "version": VERSION, "created_at": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
        "artifacts": run_artifacts, "source_pins": sources_before["pins"],
        "double_replay_sha256": digest_first,
    })
    run_readback = {"status": "PASS", "artifact_count": len(run_artifacts),
                    "mismatches": _verify_artifacts(run_root, run_artifacts),
                    "manifest_sha256": _sha(run_root / "artifact-manifest.json"),
                    "v1_7_parity": parity["status"], "scenario_count": len(all_summaries),
                    "main_scenario_count": len(tables["main"]),
                    "double_replay_match": digest_first == digest_second,
                    "double_replay_sha256": digest_first}
    _write_json(run_root / "readback.json", run_readback)
    if run_readback["mismatches"]:
        raise ETF0050Error(f"run readback mismatch: {run_readback['mismatches']}")
    for path in sorted(run_root.iterdir()):
        if path.is_file() and path.name not in {"artifact-manifest.json", "readback.json",
                                                STOP_CODE + ".json"}:
            shutil.copy2(path, report_root / path.name)
    _write_json(report_root / "artifact-manifest.json", {})
    _write_json(report_root / "readback.json", {})
    links = prior_research.validate_report_links(report_root)
    _write_json(report_root / "link-validation.json", links)
    if links["status"] != "PASS":
        raise ETF0050Error(f"report links failed: {links}")
    report_artifacts = _artifact_rows(report_root, {"artifact-manifest.json", "readback.json"})
    _write_json(report_root / "artifact-manifest.json", {
        "version": VERSION, "source_run_manifest": run_readback["manifest_sha256"],
        "artifacts": report_artifacts,
    })
    report_readback = {"status": "PASS", "artifact_count": len(report_artifacts),
                       "mismatches": _verify_artifacts(report_root, report_artifacts),
                       "manifest_sha256": _sha(report_root / "artifact-manifest.json")}
    _write_json(report_root / "readback.json", report_readback)
    if report_readback["mismatches"]:
        raise ETF0050Error(f"report readback mismatch: {report_readback['mismatches']}")
    sources_after = validate_source_pins()
    gate = {"status": STOP_CODE, "candidate_only": True, "adopted": False,
            "branch": "codex/0050-regime-sizing-core", "run_root": str(run_root),
            "report_root": str(report_root),
            "run_manifest_sha256": run_readback["manifest_sha256"],
            "run_readback_sha256": _sha(run_root / "readback.json"),
            "report_manifest_sha256": report_readback["manifest_sha256"],
            "report_readback_sha256": _sha(report_root / "readback.json"),
            "source_pins_unchanged": sources_before == sources_after,
            "v1_7_exact_parity": parity["status"],
            "double_replay_sha256": digest_first, "data_pin": package.package_hash,
            "scenario_count": len(all_summaries), "main_scenario_count": len(tables["main"]),
            "limitations": limitations["limitations"]}
    _write_json(run_root / (STOP_CODE + ".json"), gate)
    return gate


def validate(*, run_root: Path = RUN_ROOT_DEFAULT,
             report_root: Path = REPORT_ROOT_DEFAULT) -> dict[str, Any]:
    run_manifest = json.loads((run_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
    report_manifest = json.loads((report_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
    run_bad = _verify_artifacts(run_root, run_manifest["artifacts"])
    report_bad = _verify_artifacts(report_root, report_manifest["artifacts"])
    parity = json.loads((run_root / "v1-7-exact-parity.json").read_text(encoding="utf-8-sig"))
    links = prior_research.validate_report_links(report_root)
    summaries = _read_csv(run_root / "scenario-summary.csv")
    main = _read_csv(run_root / "main-six-summary.csv")
    concentration = _read_csv(run_root / "concentration-counterfactual.csv")
    tx = _read_csv(run_root / "transactions.csv")
    daily = _read_csv(run_root / "daily-equity.csv")
    entries = [row for row in tx if row.get("sleeve") == "TACTICAL" and row.get("role") == "MOTHER"]
    sizing_ok = all(
        (row["signal_regime"] == "LOW" and _d(row["entry_budget_fraction"]) == 1)
        or (row["signal_regime"] == "HIGH" and
            _d(row["entry_budget_fraction"]) == Decimal(row["high_weight_pct"]) / 100)
        for row in entries
    )
    no_negative_cash = all(_d(row["core_cash"]) >= 0 and _d(row["tactical_cash"]) >= 0
                           for row in daily)
    result = {"status": "PASS" if not run_bad and not report_bad
              and parity["status"] == "PASS_EXACT" and links["status"] == "PASS"
              and len(summaries) == 24 and len(main) == 6 and len(concentration) == 24
              and sizing_ok and no_negative_cash and validate_source_pins()["status"] == "PASS_EXACT"
              else "FAIL",
              "run_mismatches": run_bad, "report_mismatches": report_bad,
              "scenario_count": len(summaries), "main_scenario_count": len(main),
              "concentration_rows": len(concentration), "v1_7_parity": parity["status"],
              "links": links["status"], "entry_sizing": sizing_ok,
              "cash_conservation": no_negative_cash, "network_used": False,
              "writes_performed": False}
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("run", "validate"), nargs="?", default="run")
    parser.add_argument("--run-root", type=Path, default=RUN_ROOT_DEFAULT)
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT_DEFAULT)
    args = parser.parse_args(argv)
    result = (validate(run_root=args.run_root, report_root=args.report_root)
              if args.mode == "validate" else
              run_study(run_root=args.run_root, report_root=args.report_root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"PASS", STOP_CODE} else 1


if __name__ == "__main__":
    raise SystemExit(main())
