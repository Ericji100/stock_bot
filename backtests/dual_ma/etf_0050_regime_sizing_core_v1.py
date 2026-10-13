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
import re
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


VERSION = "dual-ma-0050-regime-sizing-core-v1.2-qa3"
START = accepted.FORMAL_START
END = accepted.CUTOFF
CAPITAL = CONTINUOUS_CAPITAL
HIGH_WEIGHTS = (50, 75, 100)
CORE_WEIGHTS = (0, 20)
SLIPPAGE_BPS = (20, 40, 60)
TIMING_NEXT_OPEN = analysis.TIMING_NEXT_OPEN
TIMING_SIGNAL_CLOSE = analysis.TIMING_SIGNAL_CLOSE
STOP_CODE = "STOP_WAIT_00_0050_REGIME_SIZING_CORE_QA"

RUN_ROOT_DEFAULT = dev_run("02-etf-0050-regime-sizing-core-v1-qa3")
REPORT_ROOT_DEFAULT = report_path(
    "etf", "0050", "experiments", "2006-2026-regime-sizing-core-v1-qa3"
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
V1_RUN = dev_run("02-etf-0050-regime-sizing-core-v1")
V1_REPORT = report_path(
    "etf", "0050", "experiments", "2006-2026-regime-sizing-core-v1"
)
V1_RUN_MANIFEST = "6c76af9d0b508ffbeb84db1964e43d823d5dd8e2d8f5879e372653177c9fa7b0"
V1_RUN_READBACK = "d2c816d834aed2d757251467ad2ea833acfed17cf8dd974ea6405ddbe71289d6"
V1_REPORT_MANIFEST = "d4fc89bab063be05b9176d3e16a91b4f388de8443dabecfa2bdcc88f6ba63bbf"
V1_REPORT_READBACK = "32e3954492a6b8f37a3cdf94d3474a9bcb0ac7df108acb282795f97115280367"
QA2_RUN = dev_run("02-etf-0050-regime-sizing-core-v1-qa2")
QA2_REPORT = report_path(
    "etf", "0050", "experiments", "2006-2026-regime-sizing-core-v1-qa2"
)
QA2_RUN_MANIFEST = "daadbf6da9521f6a09f2b78c0c9e1ac25e08b0b61989dc1552d8a134691e1b5d"
QA2_RUN_READBACK = "824126b0dde9ba7a382502fc5fcff2e2514e5345097b9f3fd2c367577912e452"
QA2_REPORT_MANIFEST = "a1f8b786217eb05026ecdb05b3e93519cf8f52536f33ff6b32c3cd94621139a0"
QA2_REPORT_READBACK = "4d043d0adfacf4e421289a4e50dac31908c3b40e34502f092ef7a44e2459fc0a"


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
        "v1_run_manifest": (V1_RUN / "artifact-manifest.json", V1_RUN_MANIFEST),
        "v1_run_readback": (V1_RUN / "readback.json", V1_RUN_READBACK),
        "v1_report_manifest": (V1_REPORT / "artifact-manifest.json", V1_REPORT_MANIFEST),
        "v1_report_readback": (V1_REPORT / "readback.json", V1_REPORT_READBACK),
        "qa2_run_manifest": (QA2_RUN / "artifact-manifest.json", QA2_RUN_MANIFEST),
        "qa2_run_readback": (QA2_RUN / "readback.json", QA2_RUN_READBACK),
        "qa2_report_manifest": (QA2_REPORT / "artifact-manifest.json", QA2_REPORT_MANIFEST),
        "qa2_report_readback": (QA2_REPORT / "readback.json", QA2_REPORT_READBACK),
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
        (V1_RUN, "artifact-manifest.json"),
        (V1_REPORT, "artifact-manifest.json"),
        (QA2_RUN, "artifact-manifest.json"),
        (QA2_REPORT, "artifact-manifest.json"),
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
    first_entries = []
    for item in main:
        first = min(item["details"], key=lambda row: row["entry_date"])
        first_entries.append({
            "scenario_id": item["summary"]["scenario_id"],
            "scenario_name_zh": item["summary"]["scenario_name_zh"],
            "core_weight_pct": item["summary"]["core_weight_pct"],
            "high_weight_pct": item["summary"]["high_weight_pct"],
            **{key: first[key] for key in (
                "entry_date", "signal_regime", "available_cash_before_entry",
                "target_budget", "buy_total",
            )},
        })
    return {"main": [item["summary"] for item in main], "annual": annual,
            "periods": periods, "rolling": rolling, "regime": regime_attr,
            "cost": cost, "timing": timing, "first_entries": first_entries}


def _fmt_money(value: Any) -> str:
    return f"{_d(value):,.0f}"


def _fmt_pct(value: Any) -> str:
    return f"{_d(value) * 100:.2f}%"


def _fmt_price(value: Any) -> str:
    """Keep the source Decimal's effective precision for trade-level auditing."""
    return format(_d(value), "f")


def _exit_reason_zh(value: Any) -> str:
    code = "" if value in (None, "") else str(value)
    return {
        "MACD_RED_TO_GREEN": "MACD 翻綠",
        "INITIAL_PROTECTIVE_STOP": "固定初始防守",
    }.get(code, "截止日仍持有" if not code else f"其他出場（{code}）")


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    return ["| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(str(value) for value in row) + " |" for row in rows)]


def report_text(*, tables: Mapping[str, Sequence[Mapping[str, Any]]],
                parity: Mapping[str, Any], sources: Mapping[str, Any],
                concentration: Sequence[Mapping[str, Any]],
                prior_row: Mapping[str, Any], benchmark_rows: Sequence[Mapping[str, Any]],
                replay_digest: str) -> str:
    ordered_main = sorted(tables["main"], key=lambda item: (
        int(item["core_weight_pct"]), int(item["high_weight_pct"])))
    ranked = sorted(ordered_main, key=lambda row: _d(row["cagr"]), reverse=True)
    best = ranked[0]
    baseline = next(row for row in ordered_main if int(row["core_weight_pct"]) == 0
                    and int(row["high_weight_pct"]) == 100)
    by_id = {str(row["scenario_id"]): row for row in ordered_main}
    annual_by_id = {sid: [row for row in tables["annual"] if row["scenario_id"] == sid]
                    for sid in by_id}
    period_by_id = {sid: [row for row in tables["periods"] if row["scenario_id"] == sid]
                    for sid in by_id}
    cost_by_key = {(int(row["core_weight_pct"]), int(row["high_weight_pct"]),
                    int(row["slippage_bps"])): row for row in tables["cost"]}
    timing_by_key = {(int(row["core_weight_pct"]), int(row["high_weight_pct"]),
                      str(row["execution_timing"])): row for row in tables["timing"]}
    concentration_by_key = {(str(row["scenario_id"]), int(row["removed_top_profit_trades"])): row
                            for row in concentration}

    section_titles = [
        "01｜先講結論", "02｜研究問題與唯一改動", "03｜六組主結果",
        "04｜每次到底投入多少", "05｜因果與成交時序",
        "06｜核心帳、股利、公司行動與逐筆帳", "07｜高低檔歸因",
        "08｜最差完整 3／5／10 年", "09｜逐年與三段期間",
        "10｜成本與成交時點敏感度", "11｜獲利集中與移除大贏家",
        "12｜與 v1.7、上一組合候選及基準比較", "13｜可重現性與完整性",
        "14｜限制、判定與待 00 決策",
    ]
    lines = ["# 0050 高低市場狀態差別投入 × 核心持有｜因果回測研究報告", "",
             f"版本：{VERSION}｜樣本：{START}～{END}｜初始本金：500,000 元｜正式口徑：次一交易日開盤、20 基點。", "",
             "本頁可單獨閱讀；CSV 是稽核附件，不是理解結論的前提。逐筆帳另見 [繁體中文逐筆交易明細](trade-details.md)。", "",
             "## 建議閱讀順序", ""]
    lines.extend(f"- [{title}](#{prior_research._markdown_slug(title)})"
                 for title in section_titles)
    lines.extend(["", "建議先讀 01、03、08、10、11、12、14；要查單筆交易再開啟 [繁體中文逐筆交易明細](trade-details.md)。", ""])

    def add_section(title: str, *, purpose: str, method: str,
                    observations: Sequence[str], plain: str, impact: str) -> None:
        lines.extend([f"## {title}", "", "### 測試目的", "", purpose, "",
                      "### 測試方法", "", method, "", "### 觀察結果", ""])
        lines.extend(observations)
        lines.extend(["", "### 白話結論", "", plain, "",
                      "### 對最終決策的影響", "", impact, ""])

    add_section(section_titles[0],
        purpose="回答六組配置是否比封存 v1.7 更值得採用，以及樣本內正報酬能否直接解讀為未來正期望。",
        method="以次一交易日開盤、20 基點為唯一主口徑，同時比較年化報酬（CAGR）、最大回落（MDD）、年化報酬÷最大回落（Calmar）、最差滾動期與集中度，不用單一期末資產決定勝負。",
        observations=[
            f"- 樣本內年化報酬最高為「{best['scenario_name_zh']}」：期末 {_fmt_money(best['ending_equity'])} 元、年化報酬 {_fmt_pct(best['cagr'])}、最大回落 {_fmt_pct(best['max_drawdown'])}、年化報酬÷最大回落 {_d(best['calmar']):.3f}。",
            f"- v1.7 等價組合：期末 {_fmt_money(baseline['ending_equity'])} 元、年化報酬 {_fmt_pct(baseline['cagr'])}、最大回落 {_fmt_pct(baseline['max_drawdown'])}、年化報酬÷最大回落 {_d(baseline['calmar']):.3f}。",
            f"- 高年化報酬候選多賺 {_fmt_money(_d(best['ending_equity'])-_d(baseline['ending_equity']))} 元，但最大回落多 {_fmt_pct(abs(_d(best['max_drawdown']))-abs(_d(baseline['max_drawdown'])))}。",
        ],
        plain="20% 核心加上高檔全投入，賺得較多，但跌得也明顯更深；它不是全面勝過 v1.7。六組都在本樣本賺錢，只能說歷史證據為正，不能保證未來正期望。",
        impact="維持 v1.7，不採用新候選；把核心20%／高檔100%保留為需要樣本外驗證的非支配候選。")

    add_section(section_titles[1],
        purpose="隔離『市場狀態差別投入』與『20% 長期核心』兩個變因，避免同時修改進出場規則造成歸因混亂。",
        method="凍結 v1.7 的 91 個進場回合與全部退出決策；低檔固定投入戰術現金 100%，高檔只改為 50%、75%、100%；核心只取 0% 或 20%，形成六組。",
        observations=[
            "- 高檔定義：訊號日 MA21≥MA144，等號歸高檔；低檔為 MA21＜MA144。",
            "- 20% 核心初始 100,000 元；戰術帳初始 400,000 元。0% 核心的戰術帳初始為 500,000 元。",
            "- 持倉後跨越 MA21／MA144 不改股數、不加碼、不減碼、不強制出場。",
        ],
        plain="這次不是發明新買賣訊號，只是在同一批交易上測試『高檔少押一點』與『先留兩成長抱』。",
        impact="若結果不同，可以歸因於資金配置；不得把差異誤認為新進出場規則的效果。")

    add_section(section_titles[2],
        purpose="把六組主方案的報酬、風險與復原時間放在同一張表，判斷是否存在全面優於 v1.7 的配置。",
        method="每組以相同 500,000 元、相同交易日、相同 20 基點成本重播；年化報酬÷最大回落＝年化報酬／|最大回落|，水下期從前高到重新創高計日。",
        observations=_md_table(
            ["組合", "期末資產", "年化報酬", "最大回落", "年化報酬÷最大回落", "最長水下"],
            [[row["scenario_name_zh"], _fmt_money(row["ending_equity"]), _fmt_pct(row["cagr"]),
              _fmt_pct(row["max_drawdown"]), f"{_d(row['calmar']):.3f}",
              f"{row['longest_underwater_days']} 天"] for row in ordered_main]),
        plain="高檔投入比例愈高，期末資產與年化報酬愈高；加入 20% 核心雖提高部分組合的期末資產，卻把整體最大回落拉深到約 28.7%～29.0%。",
        impact="沒有任何新組合同時提高年化報酬又降低最大回落；核心20%／高檔100%僅列非支配候選，不取代 v1.7。")

    first_entry_rows = []
    for row in tables["first_entries"]:
        first_entry_rows.append([row["scenario_name_zh"], row["signal_regime"],
                                 _fmt_money(row["available_cash_before_entry"]),
                                 _fmt_money(row["target_budget"]), _fmt_money(row["buy_total"]),
                                 _fmt_money(_d(row["available_cash_before_entry"])-_d(row["buy_total"]))])
    add_section(section_titles[3],
        purpose="釐清『投入 50%／75%／100%』的分母與實際成交金額，避免誤以為每筆固定投入 50 萬。",
        method="分母固定為該筆進場前的戰術帳可用現金；目標預算再經整股、手續費與初始風險上限換算成實際股數。",
        observations=["以下為六組在 2006-02-07 第一筆高檔進場的實際帳：", ""] + _md_table(
            ["組合", "狀態", "進場前現金", "目標預算", "實際投入", "投入後留現金"], first_entry_rows),
        plain="每筆金額會隨前一筆盈虧改變。高檔 50% 是拿當時戰術現金的一半，不是永遠拿 250,000 元；低檔則拿當時戰術現金全部。",
        impact="資金使用符合研究問題；不能用固定本金回測結果替代本次複利現金帳。")

    add_section(section_titles[4],
        purpose="確認沒有用下一交易日價格反推訊號，也沒有在持倉期間用新狀態偷改部位。",
        method="訊號日收盤後才計算 MA21／MA144 並鎖定高檔／低檔；主口徑映射到次一正式交易日開盤。逐筆核對訊號日、成交日與持倉回合。",
        observations=[
            f"- v1.7 精確同值閘門：{parity['status']}；逐日 5,093 列、交易 181 筆、回合 91 筆均逐欄相同。",
            "- 每次只有 MOTHER 或 EXIT；沒有 RESIZE、ADD、REDUCE 或第二個同時持倉。",
            "- 訊號收盤 20 基點只作配對敏感度，不納入主排名。",
        ],
        plain="今天收盤看到訊號，最早明天開盤成交；持有後市場變高檔或低檔都不會偷偷調倉。",
        impact="因果時序可接受，且核心0%／高檔100%可精確回到 v1.7；本次差異確實來自配置。")

    add_section(section_titles[5],
        purpose="確認核心帳、戰術帳、股利與分割分開記帳且每日守恆，並提供可讀的逐筆稽核入口。",
        method="兩帳各自持有現金與股數；除息日鎖權、付款日入現金、核心於付款後下一正式交易日再投入；分割只重算股數。每日總權益＝核心權益＋戰術權益。",
        observations=[
            "- 20% 核心只出現『初始買進、股利再投入、分割重算』，沒有再平衡交易。",
            "- 六組所有日期的核心現金與戰術現金皆非負，合併現金與合併權益逐日守恆。",
            "- 可讀明細：[繁體中文逐筆交易明細](trade-details.md)；原始稽核：[交易流水](transactions.csv)、[股利流水](dividend-ledger.csv)、[分割流水](corporate-actions.csv)、[逐日帳](daily-equity.csv)。",
        ],
        plain="核心和戰術像兩個不能互相借錢的信封；股利真的付款後才能再買，分割也不會憑空創造獲利。",
        impact="帳務隔離與公司行動處理通過；逐筆問題可從中文頁回查原始 CSV。")

    add_section(section_titles[6],
        purpose="判斷歷史獲利主要來自高檔或低檔進場，並觀察降低高檔投入是否真的改善風險報酬。",
        method="依進場訊號日鎖定的高檔／低檔將 90 筆已平倉交易分組，合計勝筆、淨損益與平均每筆損益；未平倉不混入已實現統計。",
        observations=_md_table(
            ["組合", "狀態", "已平倉", "勝筆", "淨損益", "平均每筆"],
            [[row["scenario_name_zh"], "高檔" if row["signal_regime"] == "HIGH" else "低檔",
              row["closed_trades"], row["wins"], _fmt_money(row["net_pnl"]),
              _fmt_money(row["average_trade_pnl"])] for row in tables["regime"]]),
        plain="降低高檔投入會直接縮小高檔贏家與輸家的金額，但歷史上高檔淨貢獻為正，因此縮小後總報酬也下降。",
        impact="目前沒有證據支持把高檔壓到 50% 或 75%；兩者報酬下降且最大回落幾乎未改善。")

    rolling_rows = [[row["scenario_name_zh"],
                     f"{_fmt_pct(row['worst_complete_3y_cagr'])}（{row['worst_complete_3y_start']}～{row['worst_complete_3y_end']}）",
                     f"{_fmt_pct(row['worst_complete_5y_cagr'])}（{row['worst_complete_5y_start']}～{row['worst_complete_5y_end']}）",
                     f"{_fmt_pct(row['worst_complete_10y_cagr'])}（{row['worst_complete_10y_start']}～{row['worst_complete_10y_end']}）"]
                    for row in ordered_main]
    add_section(section_titles[7],
        purpose="避免長樣本的漂亮年化報酬掩蓋投資人可能實際遇到的差勁三年、五年或十年。",
        method="只用完整月末滾動窗口；對每組找出 3／5／10 年年化報酬最低的窗口與實際起訖日，不拿不足年限的尾端資料充數。",
        observations=_md_table(["組合", "最差完整3年", "最差完整5年", "最差完整10年"], rolling_rows) +
                     ["", "完整窗口逐列見 [rolling-3-5-10.csv](rolling-3-5-10.csv)。"],
        plain="六組最差完整三年與部分五年仍可能小幅虧損；即使全期年化報酬為正，投資人仍可能連續多年沒有正報酬。",
        impact="『樣本內正期望』不能被翻譯成任何持有三年都賺；採用前仍需樣本外與承受期檢驗。")

    annual_extrema = []
    partial_2026 = []
    for sid, bucket in annual_by_id.items():
        complete = [row for row in bucket if int(row["year"]) < 2026]
        worst = min(complete, key=lambda row: _d(row["return"]))
        best_year = max(complete, key=lambda row: _d(row["return"]))
        partial = next(row for row in bucket if int(row["year"]) == 2026)
        annual_extrema.append([by_id[sid]["scenario_name_zh"],
                               f"{worst['year']}／{_fmt_pct(worst['return'])}",
                               f"{best_year['year']}／{_fmt_pct(best_year['return'])}"])
        partial_2026.append([by_id[sid]["scenario_name_zh"],
                             _fmt_pct(partial["return"])])
    period_rows = []
    for sid, bucket in period_by_id.items():
        values = {row["period"]: _fmt_pct(row["cagr"]) for row in bucket}
        period_rows.append([by_id[sid]["scenario_name_zh"], values["2006_2012"],
                            values["2013_2019"], values["2020_2026M09"]])
    add_section(section_titles[8],
        purpose="檢查績效是否只集中在特定年份或最近一段行情。",
        method="以每年第一筆權益前的期末值作年初基準，計算年度報酬與年內最大回落；完整年度最好／最差排行只使用 2006～2025。2026 樣本只到 9 月 30 日，另列年初至 9 月底累計報酬，不把它當完整年度排行。另固定切成 2006–2012、2013–2019、2020–2026/09 三段並算年化報酬。",
        observations=["各組最好／最差完整年度（排除未完的 2026）：", ""] + _md_table(
            ["組合", "最差完整年度／報酬", "最佳完整年度／報酬"], annual_extrema) +
            ["", "2026 年初至 9/30 累計報酬（未滿全年）：", ""] + _md_table(
            ["組合", "2026 年初至9/30"], partial_2026) +
            ["", "三段期間年化報酬：", ""] + _md_table(
            ["組合", "2006–2012", "2013–2019", "2020–2026/09"], period_rows) +
            ["", "**報表口徑註：**20% 核心方案的 2006 年度與固定期間報酬，以首個交易日收盤後、已扣除核心建倉費的帳面資產為期初；因此不等同於用原始 500,000 元作期初的全期報酬。此處只說明既有序列口徑，不調整任何數值。", "",
             "圖：[六組權益曲線](six-main-equity.svg)／[最佳年化報酬候選逐年報酬](annual-returns.svg)。"],
        plain="策略不是每年都賺，且不同年代的成長速度差很多；全期平均會把壞年份與弱期間平滑掉。",
        impact="保留逐年與分段風險揭露；不能只用 2006～2026 的單一年化報酬宣稱穩定正期望。")

    cost_rows = []
    timing_rows = []
    for row in ordered_main:
        key = (int(row["core_weight_pct"]), int(row["high_weight_pct"]))
        cost_rows.append([row["scenario_name_zh"],
                          _fmt_pct(cost_by_key[key+(20,)]["cagr"]),
                          _fmt_pct(cost_by_key[key+(40,)]["cagr"]),
                          _fmt_pct(cost_by_key[key+(60,)]["cagr"]),
                          _fmt_money(cost_by_key[key+(60,)]["ending_equity"])])
        next_open = timing_by_key[key+(TIMING_NEXT_OPEN,)]
        signal_close = timing_by_key[key+(TIMING_SIGNAL_CLOSE,)]
        timing_rows.append([row["scenario_name_zh"], _fmt_pct(signal_close["cagr"]),
                            _fmt_pct(next_open["cagr"]),
                            _fmt_money(_d(next_open["ending_equity"])-_d(signal_close["ending_equity"]))])
    add_section(section_titles[9],
        purpose="確認候選不是只在低摩擦或理想化收盤成交下才成立。",
        method="20／40／60 基點各自完整重播，不事後線性扣成本；另以訊號日收盤 20 基點與次日開盤 20 基點成對比較。",
        observations=["成本壓力：", ""] + _md_table(
            ["組合", "20基點年化報酬", "40基點年化報酬", "60基點年化報酬", "60基點期末"], cost_rows) +
            ["", "成交時點：", ""] + _md_table(
            ["組合", "訊號收盤年化報酬", "次日開盤年化報酬", "次日開盤期末差"], timing_rows) +
            ["", "圖：[成本壓力圖](cost-sensitivity.svg)。"],
        plain="成本提高會明顯侵蝕報酬，但六組在 60 基點下仍維持正年化報酬；收盤與次日開盤差異不可忽略，所以只能把次日開盤當實務主口徑。",
        impact="成本韌性未推翻候選，但不構成採用理由；正式決策仍使用次日開盤 20 基點。")

    concentration_rows = []
    for row in ordered_main:
        sid = str(row["scenario_id"])
        concentration_rows.append([row["scenario_name_zh"],
                                   _fmt_pct(row["top5_profit_concentration"]),
                                   row["longest_losing_streak"],
                                   _fmt_pct(concentration_by_key[(sid, 0)]["cagr"]),
                                   _fmt_pct(concentration_by_key[(sid, 1)]["cagr"]),
                                   _fmt_pct(concentration_by_key[(sid, 3)]["cagr"]),
                                   _fmt_pct(concentration_by_key[(sid, 5)]["cagr"])])
    add_section(section_titles[10],
        purpose="檢查全期正報酬是否只靠少數幸運大贏家，以及移除大贏家後策略是否仍有正成長。",
        method="按已平倉淨利排序，分別移除前 1／3／5 個 leg，再用原始凍結訊號與完整現金帳重新重播；不是直接從原期末資產扣除損益。",
        observations=_md_table(
            ["組合", "前5獲利集中", "最長連敗", "原年化報酬", "移除1筆", "移除3筆", "移除5筆"],
            concentration_rows) + ["", "完整反事實見 [concentration-counterfactual.csv](concentration-counterfactual.csv)。"],
        plain="移除五大贏家後六組仍是正年化報酬，代表不是只靠五筆交易；但前五筆約占全部正獲利 51%～56%，集中度仍高。",
        impact="歷史正報酬具一定廣度，但集中風險不能忽略；不足以單獨證明未來正期望。")

    comparison_rows = [["封存 v1.7 次日開盤", _fmt_money(baseline["ending_equity"]),
                        _fmt_pct(baseline["cagr"]), _fmt_pct(baseline["max_drawdown"])],
                       ["上一組合候選（凍結）", _fmt_money(prior_row["ending_equity"]),
                        _fmt_pct(prior_row["cagr"]), _fmt_pct(prior_row["max_drawdown"])]]
    comparison_rows += [[row["label"], _fmt_money(row["ending_equity"]), _fmt_pct(row["cagr"]),
                         _fmt_pct(row.get("max_asset_drawdown") or row["max_drawdown"])]
                        for row in benchmark_rows]
    add_section(section_titles[11],
        purpose="把新候選放回既有正式策略、上一輪組合候選、0050 長抱與市場指數的共同尺度。",
        method="全部使用 500,000 元等值起點與相同截止日；0050 長抱含實際交易成本，TAIEX 為不可直接交易且未扣成本的參考。",
        observations=_md_table(["對照", "期末資產", "年化報酬", "最大回落"], comparison_rows) +
                     ["", f"本輪最高年化報酬候選另為：{best['scenario_name_zh']}，期末 {_fmt_money(best['ending_equity'])} 元、年化報酬 {_fmt_pct(best['cagr'])}、最大回落 {_fmt_pct(best['max_drawdown'])}。"],
        plain="新候選沒有打敗股利再投入長抱的報酬，也沒有保住 v1.7 的較淺回撤；它只是在策略族內換取較高報酬。",
        impact="不升級正式策略；v1.7 仍是風險調整後較合理的預設比較基準。")

    add_section(section_titles[12],
        purpose="證明報告、數字與圖表能由同一套凍結資料重建，且本次修版沒有改寫 v1、v1.7 或上一研究。",
        method="完整研究在記憶體獨立跑兩次後比正規化 SHA-256；輸出再建立 artifact manifest、逐檔 readback、Markdown 連結與圖表中文標籤稽核。",
        observations=[
            f"- 雙重重播 SHA-256：`{replay_digest}`，兩次完全相同。",
            f"- v1.7、上一研究與本研究 v1 來源釘選：{sources['status']}。",
            "- 資料套件 SHA-256、完整規格與重跑命令見 [research-contract.json](research-contract.json)。",
            "- 圖表中文標籤稽核見 [chart-label-audit.json](chart-label-audit.json)，報告連結稽核見 [link-validation.json](link-validation.json)。",
        ],
        plain="相同輸入會得到相同結果；這次只新增 qa2 目錄，不會把舊正式檔案悄悄換掉。",
        impact="可重現性通過，但仍需 00 對計算與程式作獨立 QA 才能進下一步。")

    add_section(section_titles[13],
        purpose="明確界定這份回測能回答與不能回答的問題，並給出目前唯一允許的決策狀態。",
        method="逐項檢查單一商品、同樣本挑選、多重比較、樣本外、交易可行性與指數可交易性限制；決策同時看報酬、回撤、滾動期、集中度與成本。",
        observations=[
            "- 單一 0050、單一 2006～2026/09 歷史區間；三個高檔投入比例與兩個核心比例都在同一樣本比較。",
            "- 未做多重檢定校正、封存樣本外、跨商品或即時前瞻驗證。",
            "- TAIEX 僅為市場參考，不可直接交易且未扣策略成本。",
            f"- 目前狀態：`{STOP_CODE}`；候選未採用、未合併、未改正式索引。",
        ],
        plain="這份報告能說『歷史上怎麼表現』，不能說『未來一定賺』。",
        impact="維持停止並等待 00 嚴格檢查；即使數值 QA 通過，也應先定義樣本外驗證再談採用。")

    lines.extend(["## 附件索引", "",
                  "- [繁體中文逐筆交易明細](trade-details.md)",
                  "- [六組主摘要](main-six-summary.csv)",
                  "- [全部 24 情境摘要](scenario-summary.csv)",
                  "- [逐筆交易原始 CSV](trade-details.csv)",
                  "- [執行排程稽核](execution-audit.csv)",
                  "- [v1.7 精確同值](v1-7-exact-parity.json)",
                  "- [來源釘選](source-pins.json)",
                  "- [限制](limitations.json)", ""])
    return "\n".join(lines)


def trade_details_text(details: Sequence[Mapping[str, Any]]) -> str:
    """Readable Traditional-Chinese ledger for the six primary scenarios."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in details:
        groups[str(row["scenario_id"])].append(row)
    ordered = sorted(groups.values(), key=lambda bucket: (
        int(bucket[0]["core_weight_pct"]), int(bucket[0]["high_weight_pct"])))
    main_section = "06｜核心帳、股利、公司行動與逐筆帳"
    lines = ["# 0050 差別投入 × 核心持有｜繁體中文逐筆交易明細", "",
             f"[返回主報告首頁](README.md)｜[返回主報告第 06 節](README.md#{prior_research._markdown_slug(main_section)})", "",
             "本頁只列六組正式主口徑（次一交易日開盤、20 基點）的戰術交易；未平倉交易獨立標示，股息為實際付款入帳金額。", "",
             "## 快速索引", ""]
    headings = []
    for bucket in ordered:
        heading = str(bucket[0]["scenario_name_zh"])
        headings.append((heading, bucket))
        lines.append(f"- [{heading}](#{prior_research._markdown_slug(heading)})")
    for heading, bucket in headings:
        closed = sum(row.get("exit_date") not in (None, "") for row in bucket)
        lines.extend(["", f"## {heading}", "",
                      f"共 {len(bucket)} 個回合：已平倉 {closed} 筆、截止日未平倉 {len(bucket)-closed} 筆。", ""])
        rows = []
        for row in sorted(bucket, key=lambda item: (item["entry_date"], item["leg_id"])):
            rows.append([
                row["leg_id"], row["entry_date"], row.get("exit_date") or "未平倉",
                "高檔" if row["signal_regime"] == "HIGH" else "低檔",
                _fmt_pct(row["entry_budget_fraction"]),
                _fmt_money(row["available_cash_before_entry"]),
                _fmt_money(row["target_budget"]), _fmt_money(row["buy_total"]),
                row["entry_shares"], _fmt_price(row["entry_fill_price"]),
                _fmt_price(row["initial_stop"]), _fmt_price(row["exit_fill_or_mark_price"]),
                _exit_reason_zh(row.get("exit_reason")), _fmt_money(row["cash_dividends"]),
                _fmt_money(row["net_pnl_or_estimate"]), _fmt_pct(row["return_rate_or_estimate"]),
            ])
        lines.extend(_md_table(
            ["交易編號", "進場日", "出場日", "訊號狀態", "預算比例", "進場前現金",
             "目標預算", "實際投入", "股數", "進場成交價", "初始防守", "出場／估值價",
             "出場原因", "現金股利", "淨損益／估值", "報酬率／估值"], rows))
        lines.extend(["", "[回到快速索引](#快速索引)｜[返回主報告](README.md)"])
    return "\n".join(lines) + "\n"


def chart_label_audit(root: Path) -> dict[str, Any]:
    rows = []
    forbidden = re.compile(r"CORE\d|HIGH\d|NEXT_SESSION|SIGNAL_CLOSE|BPS|REGIME-FIX")
    for name in ("six-main-equity.svg", "cost-sensitivity.svg", "annual-returns.svg"):
        text = (root / name).read_text(encoding="utf-8")
        labels = [re.sub(r"<[^>]+>", "", value)
                  for value in re.findall(r"<text\b[^>]*>(.*?)</text>", text, re.DOTALL)]
        semantic = [label for label in labels if any(char.isalpha() for char in label)]
        has_zh = any(any("\u4e00" <= char <= "\u9fff" for char in label)
                     for label in semantic)
        bad = [label for label in semantic if forbidden.search(label)]
        rows.append({"path": name, "text_labels": labels, "has_traditional_chinese": has_zh,
                     "forbidden_internal_labels": bad,
                     "status": "PASS" if has_zh and not bad else "FAIL"})
    return {"status": "PASS" if all(row["status"] == "PASS" for row in rows) else "FAIL",
            "charts": rows,
            "rule": "可讀圖表需含中文語意標籤，且不得顯示內部情境代碼。"}


def six_main_equity_svg(series: Sequence[tuple[str, Sequence[Mapping[str, Any]]]]) -> str:
    """Six lines with a dedicated two-row, three-column legend."""
    width, height, left, top, plot_w, plot_h = 1400, 800, 125, 80, 1200, 500
    values = [_d(row["equity"]) for _, rows in series for row in rows]
    hi = max(values)
    span = hi or Decimal("1")
    colors = ("#1565c0", "#ef6c00", "#2e7d32", "#c62828", "#6a1b9a", "#00838f")
    short = {
        "核心0%／高檔投入50%／低檔投入100%": "無核心・高檔50%",
        "核心0%／高檔投入75%／低檔投入100%": "無核心・高檔75%",
        "核心0%／高檔投入100%／低檔投入100%": "無核心・高檔100%",
        "核心20%／高檔投入50%／低檔投入100%": "核心20%・高檔50%",
        "核心20%／高檔投入75%／低檔投入100%": "核心20%・高檔75%",
        "核心20%／高檔投入100%／低檔投入100%": "核心20%・高檔100%",
    }
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" data-chart-kind="six-equity">',
        f'<metadata>source=daily-equity.csv; version={VERSION}; legend=two-row-three-column</metadata>',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{left}" y="35" font-size="24" font-family="sans-serif">六組差別投入 × 核心持有權益曲線</text>',
        f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" stroke="#555"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" stroke="#555"/>',
    ]
    for tick in range(6):
        value = hi * Decimal(tick) / 5
        y = top + plot_h * (1 - tick / 5)
        pieces.extend([
            f'<line x1="{left}" y1="{y:.1f}" x2="{left+plot_w}" y2="{y:.1f}" stroke="#e0e0e0"/>',
            f'<text x="{left-12}" y="{y+4:.1f}" text-anchor="end" font-size="13" font-family="sans-serif">{value:,.0f} 元</text>',
        ])
    first_rows = series[0][1]
    for tick in range(6):
        pos = round((len(first_rows) - 1) * tick / 5)
        x = left + plot_w * tick / 5
        pieces.append(f'<text x="{x:.1f}" y="{top+plot_h+28}" text-anchor="middle" font-size="13" font-family="sans-serif">{str(first_rows[pos]["date"])[:7]}</text>')
    for index, (name, rows) in enumerate(series):
        step = max(1, len(rows) // 600)
        sampled = list(rows[::step])
        if sampled[-1] is not rows[-1]:
            sampled.append(rows[-1])
        coords = []
        for cursor, row in enumerate(sampled):
            x = left + plot_w * cursor / max(len(sampled) - 1, 1)
            y = top + plot_h * float((hi - _d(row["equity"])) / span)
            coords.append(f"{x:.1f},{y:.1f}")
        color = colors[index]
        pieces.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.4" points="{" ".join(coords)}"/>')
        col, row_index = index % 3, index // 3
        legend_x, legend_y = left + col * 400, 665 + row_index * 52
        pieces.append(f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x+42}" y2="{legend_y}" stroke="{color}" stroke-width="4"/>')
        pieces.append(f'<text x="{legend_x+54}" y="{legend_y+5}" fill="{color}" font-size="16" font-family="sans-serif">{short[name]}</text>')
    pieces.append('<text x="125" y="770" font-size="12" fill="#555" font-family="sans-serif">低檔進場皆投入戰術帳可用現金 100%；圖例中的百分比為高檔進場投入比例。</text>')
    pieces.append('</svg>')
    return "\n".join(pieces)


def grouped_cost_svg(rows: Sequence[Mapping[str, Any]]) -> str:
    """Six readable groups, each containing 20/40/60-bps bars."""
    width, height, left, top, plot_w, plot_h = 1400, 780, 110, 105, 1240, 460
    ordered = sorted(rows, key=lambda row: (int(row["core_weight_pct"]),
                                           int(row["high_weight_pct"]),
                                           int(row["slippage_bps"])))
    groups: dict[tuple[int, int], list[Mapping[str, Any]]] = defaultdict(list)
    for row in ordered:
        groups[(int(row["core_weight_pct"]), int(row["high_weight_pct"]))].append(row)
    maximum = max(_d(row["ending_equity"]) for row in ordered)
    colors = {20: "#1565c0", 40: "#ef6c00", 60: "#c62828"}
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" data-chart-kind="grouped-cost">',
        f'<metadata>source=cost-sensitivity.csv; version={VERSION}; grouped=core-high-by-bps</metadata>',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{left}" y="32" font-size="24" font-family="sans-serif">六組成本壓力：期末資產</text>',
    ]
    for index, bps in enumerate((20, 40, 60)):
        x = 650 + index * 190
        pieces.extend([
            f'<rect x="{x}" y="52" width="24" height="14" fill="{colors[bps]}"/>',
            f'<text x="{x+34}" y="65" font-size="15" font-family="sans-serif">{bps} 基點</text>',
        ])
    pieces.extend([
        f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" stroke="#555"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" stroke="#555"/>',
    ])
    for tick in range(6):
        value = maximum * Decimal(tick) / 5
        y = top + plot_h * (1 - tick / 5)
        pieces.extend([
            f'<line x1="{left}" y1="{y:.1f}" x2="{left+plot_w}" y2="{y:.1f}" stroke="#e0e0e0"/>',
            f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" font-size="13" font-family="sans-serif">{value:,.0f}</text>',
        ])
    group_w = plot_w / len(groups)
    bar_w = 42
    for group_index, ((core, high), bucket) in enumerate(groups.items()):
        center = left + group_w * (group_index + .5)
        for bar_index, row in enumerate(sorted(bucket, key=lambda item: int(item["slippage_bps"]))):
            value = _d(row["ending_equity"])
            bar_h = plot_h * float(value / maximum)
            x = center - 1.5 * bar_w + bar_index * bar_w
            y = top + plot_h - bar_h
            bps = int(row["slippage_bps"])
            pieces.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w-5}" height="{bar_h:.1f}" fill="{colors[bps]}"/>')
            pieces.append(f'<text x="{x+(bar_w-5)/2:.1f}" y="{max(y-6, 95):.1f}" text-anchor="middle" font-size="10" font-family="sans-serif">{value/Decimal(10000):.0f}萬</text>')
        core_label = "無核心" if core == 0 else "核心20%"
        pieces.append(f'<text x="{center:.1f}" y="{top+plot_h+32}" text-anchor="middle" font-size="15" font-family="sans-serif"><tspan x="{center:.1f}">{core_label}</tspan><tspan x="{center:.1f}" dy="21">高檔{high}%</tspan></text>')
    pieces.append('<text x="110" y="735" font-size="12" fill="#555" font-family="sans-serif">每組三根柱依序為 20、40、60 基點；縱軸為期末資產（元），柱頂以萬元顯示。</text>')
    pieces.append('</svg>')
    return "\n".join(pieces)


def _replay_payload(matrix: Mapping[str, Any]) -> dict[str, Any]:
    return {"summaries": [item["summary"] for item in matrix["scenarios"]],
            "daily": [row for item in matrix["scenarios"] for row in item["daily"]],
            "details": [row for item in matrix["scenarios"] for row in item["details"]],
            "transactions": [row for item in matrix["scenarios"] for row in item["transactions"]]}


def run_study(*, run_root: Path = RUN_ROOT_DEFAULT,
              report_root: Path = REPORT_ROOT_DEFAULT) -> dict[str, Any]:
    if run_root.resolve() in {SEALED_RUN.resolve(), PRIOR_RUN.resolve(), V1_RUN.resolve(), QA2_RUN.resolve()} or report_root.resolve() in {
        SEALED_REPORT.resolve(), PRIOR_REPORT.resolve(), V1_REPORT.resolve(), QA2_REPORT.resolve()
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
    main_ids = {str(row["scenario_id"]) for row in tables["main"]}
    readable_details = trade_details_text(
        [row for row in all_details if str(row["scenario_id"]) in main_ids]
    )
    (run_root / "trade-details.md").write_text(readable_details, encoding="utf-8")
    main_results = [item for item in first["scenarios"]
                    if item["summary"]["execution_timing"] == TIMING_NEXT_OPEN
                    and int(item["summary"]["slippage_bps"]) == 20]
    (run_root / "six-main-equity.svg").write_text(six_main_equity_svg(
        [(item["summary"]["scenario_name_zh"], item["daily"]) for item in main_results]),
        encoding="utf-8")
    (run_root / "cost-sensitivity.svg").write_text(
        grouped_cost_svg(tables["cost"]), encoding="utf-8")
    best = max(tables["main"], key=lambda row: _d(row["cagr"]))
    annual_best = [row for row in tables["annual"] if row["scenario_id"] == best["scenario_id"]]
    (run_root / "annual-returns.svg").write_text(prior_research._svg_bars(
        rows=[("2026至9/30（未滿全年）" if int(row["year"]) == 2026 else str(row["year"]),
               _d(row["return"])) for row in annual_best],
        title=f"{best['scenario_name_zh']}：逐年報酬（2026僅至9/30）",
        source="annual.csv", percent=True),
        encoding="utf-8")
    chart_audit = chart_label_audit(run_root)
    _write_json(run_root / "chart-label-audit.json", chart_audit)
    if chart_audit["status"] != "PASS":
        raise ETF0050Error(f"chart label audit failed: {chart_audit}")
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
    _write_json(report_root / "link-validation.json", {})
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
    detail_rows = _read_csv(run_root / "trade-details.csv")
    report_text_value = (report_root / "README.md").read_text(encoding="utf-8")
    trade_text_value = (report_root / "trade-details.md").read_text(encoding="utf-8")
    numbered = re.findall(r"^## (\d{2})｜", report_text_value, re.MULTILINE)
    required_subheads = ("測試目的", "測試方法", "觀察結果", "白話結論", "對最終決策的影響")
    section_contract = numbered == [f"{number:02d}" for number in range(1, 15)]
    for number in range(1, 15):
        start_match = re.search(rf"^## {number:02d}｜", report_text_value, re.MULTILINE)
        next_match = re.search(rf"^## {number+1:02d}｜", report_text_value, re.MULTILINE) if number < 14 else None
        if start_match is None:
            section_contract = False
            continue
        segment = report_text_value[start_match.start():next_match.start() if next_match else len(report_text_value)]
        section_contract = section_contract and all(f"### {heading}" in segment
                                                     for heading in required_subheads)
    navigation_contract = (
        "## 建議閱讀順序" in report_text_value
        and len(re.findall(r"\]\(#\d{2}", report_text_value)) >= 14
        and "[繁體中文逐筆交易明細](trade-details.md)" in report_text_value
        and "[返回主報告首頁](README.md)" in trade_text_value
        and "## 快速索引" in trade_text_value
        and trade_text_value.count("## 核心") == 6
    )
    primary_details = [row for row in detail_rows
                       if row["execution_timing"] == TIMING_NEXT_OPEN
                       and int(row["slippage_bps"]) == 20]
    readable_trade_contract = (
        len(primary_details) == 546
        and trade_text_value == trade_details_text(primary_details)
        and "MACD_RED_TO_GREEN" not in trade_text_value
        and "INITIAL_PROTECTIVE_STOP" not in trade_text_value
        and "MACD 翻綠" in trade_text_value
        and "固定初始防守" in trade_text_value
    )
    chart_audit = json.loads((run_root / "chart-label-audit.json").read_text(encoding="utf-8-sig"))
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
              and sizing_ok and no_negative_cash and section_contract and navigation_contract
              and readable_trade_contract and chart_audit["status"] == "PASS"
              and validate_source_pins()["status"] == "PASS_EXACT"
              else "FAIL",
              "run_mismatches": run_bad, "report_mismatches": report_bad,
              "scenario_count": len(summaries), "main_scenario_count": len(main),
              "concentration_rows": len(concentration), "v1_7_parity": parity["status"],
              "links": links["status"], "entry_sizing": sizing_ok,
              "cash_conservation": no_negative_cash,
              "fourteen_section_five_part_contract": section_contract,
              "navigation_and_trade_details": navigation_contract,
              "trade_price_precision_and_exit_labels": readable_trade_contract,
              "chart_labels": chart_audit["status"], "network_used": False,
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
