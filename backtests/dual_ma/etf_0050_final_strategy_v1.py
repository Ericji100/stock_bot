"""Final decision package for the accepted 0050 fixed-defense regime strategy.

This renderer consumes sealed, already-accepted research artifacts.  It keeps
the two execution conventions separate and publishes only the final strategy,
two buy-and-hold comparators, and a non-tradable market reference in the main
comparison.
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
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_execution_stability_v1 as research
from . import etf_0050_ma21_ma144_regime_v1 as regime
from . import etf_0050_standard_report_v3 as standard
from .costs import CATHAY_WEB_028_TW_V1
from .etf_0050_base_v1 import ETF0050Error, tree_hash
from .etf_0050_formal_v2 import _artifact_rows, _verify_artifacts, _write_csv, _write_json
from .etf_0050_ten_year_same_method_v1 import normalize_value
from .paths import dev_run, report_path


VERSION = "dual-ma-0050-final-strategy-v1.2"
RUN_ROOT_DEFAULT = dev_run("02-etf-0050-final-strategy-v1-2")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-2")

SOURCE_RUN = dev_run("02-etf-0050-2006-2026-seven-methods-v1-5")
SOURCE_RUN_MANIFEST = "92BD69796D8F20C31C76EBAEC3F26ECB872B4540C92A6AD59F9D6C1EFF1C28A9"
V16_REPORT = report_path("etf", "0050", "experiments", "2006-2026-seven-methods-complete-report-v1-6")
V16_REPORT_MANIFEST = "F9A6B678CB428EE5CF1C2A6069719FFE3D454D84A19BEB22046DCDA97C73F7DE"
FOUR_PHASE_RUN = dev_run("02-etf-0050-execution-stability-four-phase-v1-5")
FOUR_PHASE_RUN_MANIFEST = "C5040B56494E2D97805DA7375863A35E207268554CC53D314CABB1280B813ED5"
FOUR_PHASE_REPORT = report_path("etf", "0050", "experiments", "2006-2026-execution-stability-four-phase-v1-5")
FOUR_PHASE_REPORT_MANIFEST = "20EAD32C267D6C0A26E1C9E8FE60BBB67844D99A9CC271FF6149792C635C1301"

SIGNAL_CLOSE_ID = "FINAL_SIGNAL_CLOSE_20BPS"
NEXT_OPEN_ID = "FINAL_NEXT_SESSION_OPEN_20BPS"
BUY_HOLD_CASH_ID = "BUY_HOLD_CASH"
BUY_HOLD_REINVEST_ID = "BUY_HOLD_REINVEST"
BENCHMARK_ID = "TAIEX_TOTAL_RETURN_EQUIVALENT_500K"
MAIN_IDS = (SIGNAL_CLOSE_ID, NEXT_OPEN_ID, BUY_HOLD_CASH_ID, BUY_HOLD_REINVEST_ID, BENCHMARK_ID)
START = date(2006, 1, 2)
END = date(2026, 9, 30)
CAPITAL = Decimal("500000")


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_fields(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def _project(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> list[dict[str, Any]]:
    return [{field: row.get(field) for field in fields} for row in rows]


def _filter(rows: Sequence[Mapping[str, Any]], **expected: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if all(str(row.get(key)) == str(value) for key, value in expected.items())]


def _annual_rows(comparison_id: str, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: row["date"])
    output: list[dict[str, Any]] = []
    prior_end = CAPITAL
    for year in range(START.year, END.year + 1):
        bucket = [row for row in ordered if date.fromisoformat(str(row["date"])).year == year]
        if not bucket:
            continue
        peak = prior_end
        worst = Decimal("0")
        for row in bucket:
            equity = _d(row["equity"])
            peak = max(peak, equity)
            worst = min(worst, equity / peak - 1)
        ending = _d(bucket[-1]["equity"])
        output.append({
            "comparison_id": comparison_id, "year": year,
            "start_date": bucket[0]["date"], "end_date": bucket[-1]["date"],
            "starting_equity": prior_end, "ending_equity": ending,
            "return": ending / prior_end - 1, "max_drawdown": worst,
        })
        prior_end = ending
    return output


def _cagr(ending: Decimal) -> Decimal:
    years = (END - START).days / 365
    return Decimal(str(float(ending / CAPITAL) ** (1 / years) - 1))


def _benchmark_payload(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    peak = CAPITAL
    daily: list[dict[str, Any]] = []
    for row in rows:
        equity = _d(row["equivalent_500k"])
        peak = max(peak, equity)
        daily.append({
            "comparison_id": BENCHMARK_ID, "date": row["date"], "cash": "", "shares": "",
            "raw_close": row["index_value"], "liquidation_value": "", "equity": equity,
            "drawdown": equity / peak - 1,
        })
    ending = _d(daily[-1]["equity"])
    summary = {
        "comparison_id": BENCHMARK_ID, "label": "加權報酬指數等值資產",
        "category": "NON_TRADABLE_MARKET_REFERENCE", "execution_timing": "INDEX_CLOSE",
        "tradeability": "不可直接交易；未扣策略交易成本", "cost_basis": "NONE_REFERENCE_ONLY",
        "ending_equity": ending, "total_return": ending / CAPITAL - 1, "cagr": _cagr(ending),
        "max_drawdown": min(_d(row["drawdown"]) for row in daily),
        "rounds": None, "closed_rounds": None, "open_rounds": None, "win_rate": None,
        "cumulative_buy": None, "peak_concurrent_investment": None, "realized_net_pnl": None,
        "cutoff_estimated_pnl": None, "fees_and_tax": None, "slippage_cost": None,
        "dividends_received": None, "idle_cash_ratio": None, "final_shares": None,
    }
    return summary, daily


def _summary_row(row: Mapping[str, Any], *, comparison_id: str, label: str, timing: str) -> dict[str, Any]:
    return {
        "comparison_id": comparison_id, "label": label, "category": "FINAL_STRATEGY",
        "execution_timing": timing,
        "tradeability": "理想化研究對照" if timing == "SIGNAL_CLOSE" else "實務主口徑",
        "cost_basis": "BUY_AND_SELL_20BPS_PLUS_COMMISSION_AND_ETF_SELL_TAX",
        **{key: row.get(key) for key in (
            "ending_equity", "total_return", "cagr", "max_asset_drawdown", "rounds",
            "closed_rounds", "open_rounds", "win_rate", "cumulative_buy",
            "peak_concurrent_investment", "realized_net_pnl", "cutoff_estimated_pnl",
            "fees_and_tax", "slippage_cost", "dividends_received", "idle_cash_ratio", "final_shares",
        )},
        "max_drawdown": row.get("max_asset_drawdown"),
    }


def _buy_hold_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "comparison_id": row["strategy_id"], "label": row["strategy_label"],
        "category": "BUY_AND_HOLD", "execution_timing": "INITIAL_OPEN_THEN_HOLD",
        "tradeability": "可交易歷史模擬", "cost_basis": "20BPS_PLUS_COMMISSION_AND_ETF_SELL_TAX_MARK",
        **{key: row.get(key) for key in (
            "ending_equity", "total_return", "cagr", "max_asset_drawdown", "rounds",
            "closed_rounds", "open_rounds", "win_rate", "cumulative_buy",
            "peak_concurrent_investment", "realized_net_pnl", "cutoff_estimated_pnl",
            "fees_and_tax", "slippage_cost", "dividends_received", "idle_cash_ratio", "final_shares",
        )},
        "max_drawdown": row.get("max_asset_drawdown"),
    }


def _comparison_daily(
    rows: Sequence[Mapping[str, Any]], *, comparison_id: str,
) -> list[dict[str, Any]]:
    return [{
        "comparison_id": comparison_id, "date": row["date"], "cash": row.get("cash", ""),
        "shares": row.get("shares", ""), "raw_close": row.get("raw_close", ""),
        "liquidation_value": row.get("liquidation_value", ""), "equity": row["equity"],
        "drawdown": row["drawdown"],
    } for row in rows]


def _buy_hold_reinvest_audit(
    *, daily: Sequence[Mapping[str, Any]], transactions: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, Any]], expected_daily_rows: int = 5093,
    expected_reinvest_rows: int = 31,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Prove the sealed reinvest comparator uses payment-day close and conserves cash."""
    curve = sorted(
        _filter(daily, comparison_id=BUY_HOLD_REINVEST_ID),
        key=lambda row: row["date"],
    )
    tx = sorted(
        _filter(transactions, strategy_id=BUY_HOLD_REINVEST_ID),
        key=lambda row: row["date"],
    )
    paid = sorted(
        _filter(actions, strategy_id=BUY_HOLD_REINVEST_ID, stage="CASH_PAID"),
        key=lambda row: row["payment_date"],
    )
    errors: list[str] = []
    if len(curve) != expected_daily_rows:
        errors.append(f"reinvest daily rows={len(curve)}, expected {expected_daily_rows}")
    if len(paid) != expected_reinvest_rows:
        errors.append(f"cash-paid rows={len(paid)}, expected {expected_reinvest_rows}")
    initial = [row for row in tx if row["date"] == str(START)]
    reinvest = [row for row in tx if row["date"] != str(START)]
    if len(initial) != 1:
        errors.append(f"initial buy rows={len(initial)}, expected 1")
    if len(reinvest) != expected_reinvest_rows:
        errors.append(f"reinvest rows={len(reinvest)}, expected {expected_reinvest_rows}")

    curve_by_date = {row["date"]: row for row in curve}
    prior_cash_by_date = {
        row["date"]: CAPITAL if index == 0 else _d(curve[index - 1]["cash"])
        for index, row in enumerate(curve)
    }
    paid_by_date = {row["payment_date"]: row for row in paid}
    reinvest_by_date = {row["date"]: row for row in reinvest}
    if len(paid_by_date) != len(paid):
        errors.append("duplicate dividend payment dates")
    if len(reinvest_by_date) != len(reinvest):
        errors.append("duplicate reinvest dates")
    if set(paid_by_date) != set(reinvest_by_date):
        errors.append("reinvest dates do not exactly equal payment dates")

    model = CATHAY_WEB_028_TW_V1.with_research_slippage(20)
    audit: list[dict[str, Any]] = []
    for payment_date in sorted(set(paid_by_date) | set(reinvest_by_date)):
        payment = paid_by_date.get(payment_date)
        order = reinvest_by_date.get(payment_date)
        day = curve_by_date.get(payment_date)
        if payment is None or order is None or day is None:
            errors.append(f"incomplete payment-day evidence: {payment_date}")
            continue
        shares = int(order["shares"])
        expected = model.buy(_d(day["raw_close"]), shares)
        same_payment_date = order["date"] == payment["payment_date"]
        reference_is_close = _d(order["reference_price"]) == _d(day["raw_close"])
        fill_is_20bps = _d(order["fill_price"]) == expected.fill_price
        order_cost_exact = all(
            _d(order[field]) == getattr(expected, field)
            for field in ("gross", "commission", "tax", "cash_amount")
        )
        cash_before = prior_cash_by_date[payment_date]
        expected_cash_after = cash_before + _d(payment["cash"]) - _d(order["cash_amount"])
        cash_conserved = expected_cash_after == _d(day["cash"])
        all_checks = same_payment_date and reference_is_close and fill_is_20bps and order_cost_exact and cash_conserved
        if not all_checks:
            errors.append(f"reinvest assumption mismatch: {payment_date}")
        audit.append({
            "ex_date": payment["ex_date"], "payment_date": payment_date,
            "reinvest_date": order["date"], "timing_assumption": "PAYMENT_DAY_CLOSE",
            "reference_close": day["raw_close"], "transaction_reference": order["reference_price"],
            "fill_price": order["fill_price"], "expected_fill_price_20bps": expected.fill_price,
            "shares": shares, "gross": order["gross"], "commission": order["commission"],
            "dividend_cash": payment["cash"], "cash_before": cash_before,
            "buy_cash_amount": order["cash_amount"], "expected_cash_after": expected_cash_after,
            "actual_cash_after": day["cash"], "same_payment_date": same_payment_date,
            "reference_is_payment_day_close": reference_is_close, "fill_is_20bps": fill_is_20bps,
            "order_cost_exact": order_cost_exact, "cash_conserved": cash_conserved,
            "all_checks_pass": all_checks,
        })
    return audit, errors


def _report_contract_violations(text: str) -> list[str]:
    violations = []
    for phrase in ("理想化上限", "支持『策略具有正期望』", "付款後下一官方交易日開盤再投入"):
        if phrase in text:
            violations.append(f"forbidden phrase: {phrase}")
    if not text.startswith("# 0050 最終策略定版報告｜候選 v1.2\n"):
        violations.append("report title is not candidate v1.2")
    required = (
        "付款日收盤再投入", "實務是否能在同日使用該筆股息，須視券商實際入帳時點",
        "理想化研究對照", "不證明未來正期望",
    )
    for phrase in required:
        if phrase not in text:
            violations.append(f"missing required phrase: {phrase}")
    return violations


def _regime_performance(
    daily: Sequence[Mapping[str, Any]], regimes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_day = {str(row["date"]): row["regime"] for row in regimes}
    ordered = sorted(daily, key=lambda row: row["date"])
    buckets: dict[str, list[Decimal]] = defaultdict(list)
    for prior, current in zip(ordered, ordered[1:]):
        label = by_day.get(str(current["date"]), "WARMUP")
        if label in {"BULL", "BEAR", "SIDEWAYS"}:
            buckets[label].append(_d(current["equity"]) / _d(prior["equity"]) - 1)
    output = []
    for label in ("BULL", "BEAR", "SIDEWAYS"):
        product = Decimal("1")
        for value in buckets[label]:
            product *= 1 + value
        output.append({
            "regime": label, "session_returns": len(buckets[label]),
            "compounded_strategy_return": product - 1,
            "average_daily_return": sum(buckets[label], Decimal("0")) / len(buckets[label]) if buckets[label] else None,
        })
    return output


def _parity_projection(
    *, source_path: Path, actual_rows: Sequence[Mapping[str, Any]], source_selector: Mapping[str, Any],
) -> dict[str, Any]:
    fields, source_rows = _read_fields(source_path)
    expected = _filter(source_rows, **source_selector)
    projected = _project(actual_rows, fields)
    normalized_expected = [[normalize_value(row.get(field)) for field in fields] for row in expected]
    normalized_actual = [[normalize_value(row.get(field)) for field in fields] for row in projected]
    return {
        "source": str(source_path), "fields": fields, "expected_rows": len(expected),
        "actual_rows": len(actual_rows), "exact": normalized_expected == normalized_actual,
    }


def _write_line_svg(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    width, height, left, top = 1200, 620, 75, 45
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["comparison_id"])].append(row)
    maximum = max(_d(row["equity"]) for row in rows)
    colors = ["#244a7c", "#d97706", "#15966f", "#7c3aed", "#64748b"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             '<text x="75" y="28" font-size="20">0050 final strategy: unified TWD 500,000 comparison</text>']
    for index, comparison_id in enumerate(MAIN_IDS):
        series = grouped[comparison_id]
        sampled = series[::max(1, len(series)//500)] + ([series[-1]] if series[-1] not in series[::max(1, len(series)//500)] else [])
        points = []
        for row in sampled:
            day_index = series.index(row)
            x = left + day_index / (len(series)-1) * (width-left-30)
            y = top + (1-float(_d(row["equity"])/maximum)) * (height-top-80)
            points.append(f"{x:.1f},{y:.1f}")
        color = colors[index]
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(points)}"/>')
        parts.append(f'<text x="{left+index*220}" y="{height-25}" fill="{color}" font-size="12">{comparison_id}</text>')
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def _table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    return standard.md_table(tuple(headers), rows)


def _money_or_dash(value: Any) -> str:
    return "—" if value in (None, "") else standard.money(value)


def _markdown_anchor(heading: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", heading.strip().lower(), flags=re.UNICODE)
    return re.sub(r"-+", "-", re.sub(r"\s+", "-", cleaned)).strip("-")


def _broken_markdown_links(root: Path) -> list[str]:
    """Validate local links, including same-document ``#anchor`` links."""
    broken: list[str] = []
    for md in root.glob("*.md"):
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", md.read_text(encoding="utf-8")):
            target_name, marker, fragment = target.partition("#")
            if target_name.startswith(("http://", "https://")):
                continue
            target_path = md if not target_name else root / target_name
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


def _render_report(
    *, root: Path, summary: Sequence[Mapping[str, Any]], annual: Sequence[Mapping[str, Any]],
    cost_rows: Sequence[Mapping[str, Any]], rolling: Sequence[Mapping[str, Any]],
    fixed_periods: Sequence[Mapping[str, Any]], regime_rows: Sequence[Mapping[str, Any]],
    trade_stability: Sequence[Mapping[str, Any]], concentration: Sequence[Mapping[str, Any]],
    parameter_center: Mapping[str, Any], parameter_neighborhood: Mapping[str, Any],
    parameter_distribution: Sequence[Mapping[str, Any]], allocations: Sequence[Mapping[str, Any]],
    parity: Mapping[str, Any], produced: str, attachments: Sequence[str],
) -> None:
    primary = next(row for row in summary if row["comparison_id"] == NEXT_OPEN_ID)
    primary_positive = (
        _d(primary["ending_equity"]) > CAPITAL
        and _d(primary["cagr"]) > 0
        and _d(primary["realized_net_pnl"]) > 0
    )
    closed_rounds = _d(primary["closed_rounds"])
    realized_per_closed = _d(primary["realized_net_pnl"]) / closed_rounds
    sample_verdict = (
        "本歷史樣本扣除既定成本後為正報酬，支持作為後續執行與監控的工作假說；"
        "不證明未來正期望。"
        if primary_positive else
        "扣除主成本後，實務主口徑未同時通過期末資產、CAGR 與已實現淨損益為正；"
        "本歷史樣本不支持把策略定義為正期望。"
    )
    summary_table = _table((
        (row["label"], row["execution_timing"], standard.money(row["ending_equity"]),
         standard.pct(row["total_return"]), standard.pct(row["cagr"]), standard.pct(row["max_drawdown"]),
         row["tradeability"])
        for row in summary
    ), ("比較線", "成交／估值", "期末資產", "總報酬", "CAGR", "MaxDD", "定位"))
    capital_table = _table((
        (row["label"], row.get("rounds") or "—", row.get("closed_rounds") or "—", row.get("open_rounds") or "—",
         _money_or_dash(row.get("peak_concurrent_investment")), _money_or_dash(row.get("dividends_received")),
         _money_or_dash(row.get("fees_and_tax")), _money_or_dash(row.get("slippage_cost")),
         standard.pct(row.get("idle_cash_ratio")))
        for row in summary
    ), ("比較線", "進場", "出場", "未平倉", "最大投入", "股息", "費稅", "滑價", "平均閒置現金"))
    annual_table = _table((
        (row["comparison_id"], row["year"], standard.money(row["starting_equity"]),
         standard.money(row["ending_equity"]), standard.pct(row["return"]), standard.pct(row["max_drawdown"]))
        for row in annual
    ), ("比較線", "年度", "年初資產", "年末／截止資產", "報酬", "MaxDD"))
    cost_table = _table((
        (row["execution_timing"], row["slippage_bps"], standard.money(row["ending_equity"]),
         standard.pct(row["cagr"]), standard.pct(row["max_asset_drawdown"]), row["closed_rounds"])
        for row in cost_rows
    ), ("成交版本", "bps", "期末資產", "CAGR", "MaxDD", "已出場"))
    rolling_summary = []
    for window in (3, 5, 10):
        bucket = [row for row in rolling if int(row["window_years"]) == window]
        rolling_summary.append((window, len(bucket), standard.pct(median([_d(row["cagr"]) for row in bucket])),
                                standard.pct(min(_d(row["cagr"]) for row in bucket)),
                                standard.pct(max(_d(row["cagr"]) for row in bucket)),
                                standard.pct(min(_d(row["max_drawdown"]) for row in bucket))))
    rolling_table = _table(rolling_summary, ("視窗年", "觀測數", "CAGR 中位", "最差 CAGR", "最佳 CAGR", "最差 MaxDD"))
    period_table = _table((
        (row["period"], standard.money(row["starting_equity"]), standard.money(row["ending_equity"]),
         standard.pct(row["cagr"]), standard.pct(row["max_drawdown"])) for row in fixed_periods
    ), ("固定期間", "期初", "期末", "CAGR", "MaxDD"))
    regime_table = _table((
        (row["regime"], row["session_returns"], standard.pct(row["compounded_strategy_return"]),
         standard.pct(row["average_daily_return"])) for row in regime_rows
    ), ("行情", "日報酬數", "複合策略報酬", "平均日報酬"))
    stability_table = _table((
        (row["longest_losing_streak"], row["best_leg_id"], standard.money(row["best_trade_pnl"]),
         row["worst_leg_id"], standard.money(row["worst_trade_pnl"]), row["longest_underwater_days"],
         f"{row['underwater_start']}～{row['recovery_date']}") for row in trade_stability
    ), ("最長連虧", "最佳回合", "最佳損益", "最差回合", "最差損益", "最長水下日", "水下期間"))
    concentration_table = _table((
        (row["removed_best_trade_count"], row.get("removed_leg_ids") or "無",
         standard.money(row["ending_equity"]), standard.pct(row["cagr"]),
         standard.money(row["ending_equity_change_vs_baseline"])) for row in concentration
    ), ("移除最佳回合數", "固定原訊號移除回合", "期末資產", "CAGR", "期末變化"))
    distribution_table = _table((
        (row["metric"], row["count"], row["min"], row["p10"], row["median"], row["p90"], row["max"])
        for row in parameter_distribution
    ), ("指標", "n", "min", "p10", "median", "p90", "max"))
    allocation_table = _table((
        (row["allocation_id"], standard.money(row["ending_equity"]), standard.pct(row["cagr"]),
         standard.pct(row["max_drawdown"]), row.get("rebalance"), row.get("rebalance_events") or "—",
         standard.money(row.get("rebalance_cost"))) for row in allocations
    ), ("配置研究", "期末資產", "CAGR", "MaxDD", "再平衡", "次數", "成本"))
    links = "\n".join(f"- [{name}]({name})" for name in attachments)
    report = "\n".join([
        "# 0050 最終策略定版報告｜候選 v1.2", "", f"產出時間：{produced}", "",
        "狀態：`STOP_WAIT_00_QA`。未經 00 ACCEPT 不得取代正式總目錄。", "",
        "## 目錄", "",
        "1. [一頁式總結](#一頁式總結)", "2. [策略與帳戶完整規格](#策略與帳戶完整規格)",
        "3. [全期、逐年、交易與資金](#全期逐年交易與資金)", "4. [成交時間與成本可實現性](#成交時間與成本可實現性)",
        "5. [時間與行情穩定性](#時間與行情穩定性)", "6. [交易穩定性與獲利集中度](#交易穩定性與獲利集中度)",
        "7. [參數穩健性](#參數穩健性)", "8. [資產配置研究](#資產配置研究)",
        "9. [限制、執行與監控規格](#限制執行與監控規格)", "10. [QA 與重播](#qa-與重播)", "",
        "## 一頁式總結", "", "### 測試目的", "",
        "在完全一致的 50 萬元、期間、原始價格、公司行動、費稅與截止估值下，確認最終固定防守策略的理想化收盤版與可執行隔日開盤版，並和兩種 0050 全程持有及市場參考線公平比較。", "",
        "### 方法", "", "主策略買賣均採同一成交版本；20bps 是主成本。加權報酬指數只作不可直接交易、未扣策略交易成本的市場參考。", "",
        "### 結果", "", summary_table, "", "![五線統一比較](main-comparison-equity.svg)", "",
        f"實務主口徑每個已完成回合的已實現淨損益平均為 {standard.money(realized_per_closed)} 元；這是描述歷史回合的算術值，不是未來保證。", "",
        "### 白話結論", "", sample_verdict + " 實務判斷以 NEXT_SESSION_OPEN 為主；SIGNAL_CLOSE 只是同日收盤的理想化研究對照，不代表績效上限。兩條全程持有線承擔更深回落；指數線不能視為可直接成交策略。", "",
        "### 對定版決策的影響", "", "唯一正式策略維持『MA21/MA144 分流進場＋固定初始防守＋MACD(21,55,55)』；資產配置與其他舊策略不改變這項定版。", "",
        "## 策略與帳戶完整規格", "",
        "- 每日 SMA21≥SMA144（等號歸高 regime）：空手時只在前日 technical close≤前日 SMA21、當日 technical close>當日 SMA21 的新突破進場。",
        "- 每日 SMA21<SMA144：空手時只在 MACD histogram 前日≤0、當日>0 的綠翻紅進場。",
        "- 固定初始防守價：每次候選進場時，取『最近一段 technical close≤SMA21 期間累積的 technical low、前一日 technical low、訊號日 technical low』中所有可用值的最小值；必須嚴格低於進場參考價，進場後不上移。technical 價格遇分割會等比例重訂座標。",
        "- 高 regime 持倉：當日 technical close 嚴格小於固定初始防守價即出場；若同日也發生 MACD 前日 histogram≥0、當日<0，防守出場優先。低 regime 的防守只維護不執行，唯一出場是 armed 後同一 MACD 紅翻綠。",
        "- 持倉每日動態切換 regime；綠翻紅進場立即 armed。MA21 進場若 histogram≥0（含 0）即 armed，否則等持有後首次 histogram≥0。已有持股不加碼、賣出日不重進、同日停損優先、截止不強平。",
        "- SIGNAL_CLOSE：進出都在訊號當日收盤成交，因訊號到收盤才完整成立，僅是理想化研究對照。NEXT_SESSION_OPEN：進出都在訊號後下一官方交易日開盤成交，是實務主口徑。不存在收盤進、隔日開盤出的混合第三版。",
        "- 單一連續帳戶初始 500,000 元；這不是每筆固定投入。每次以當時可用現金為預算上限、只買整股，且初始風險（買進總成本減去依初始防守價估算的賣出淨收）不得超過可用現金 20%；不借款、不加碼、不逐年重設。",
        "- 2005 暖機，正式期 2006-01-02～2026-09-30。主成本為買、賣各 20bps 滑價；手續費率 0.0399%，每筆四捨五入至整元、無最低手續費假設；ETF 賣出稅 0.1%。未平倉每日資產用當日 raw close 扣估計賣出手續費與稅後的清算值。",
        "- 股息依除息資格鎖定並於付款日入現金；策略閒置現金隨下一次合規進場投入。封存的全程持有再投入比較線採『付款日收盤再投入』：31 筆均以付款日 raw close 加買方 20bps 與手續費買入。這是歷史模擬假設；實務是否能在同日使用該筆股息，須視券商實際入帳時點。2025-06-18 split 只調整股份／技術座標，不是暴跌、停損或交易。", "",
        "## 全期、逐年、交易與資金", "", "### 測試目的", "", "看長期結果是否由合理的交易、投入、股息與成本構成。", "",
        "### 方法", "", "五線共用同一每日日曆與截止估值；四條可交易線保留現金、股數、清算值與公司行動流水。", "",
        "### 結果", "", capital_table, "", annual_table, "",
        "### 白話結論", "", "期末資產必須和投入規模、實際費稅、股息及未平倉一起解讀；市場指數的空白成本欄是口徑差異，不是零成本可交易承諾。", "",
        "### 對定版決策的影響", "", "主策略仍採單一帳戶、無借款、無年度重置；監控程式需逐日保存 cash、shares、liquidation value 與 active leg。", "",
        "## 成交時間與成本可實現性", "", "### 測試目的", "", "估計訊號落地延遲和更差成交成本會削弱多少績效。", "",
        "### 方法", "", "固定同一訊號序列，分別令進出都採 SIGNAL_CLOSE 或 NEXT_SESSION_OPEN，並測買賣各 20/40/60bps；費稅照扣。", "",
        "### 結果", "", cost_table, "",
        "### 白話結論", "", "隔日開盤 20bps 才是決策主數字；40/60bps 顯示策略對執行品質敏感，但不是改動策略訊號。", "",
        "### 對定版決策的影響", "", "正式監控與事後績效歸因必須以 NEXT_SESSION_OPEN 為預設，SIGNAL_CLOSE 僅保留研究對照。", "",
        "## 時間與行情穩定性", "", "### 測試目的", "", "確認績效是否只出現在單一年代或單一市場方向。", "",
        "### 方法", "", "實務主口徑採固定三段、月末 rolling 3/5/10 年；行情用前一交易日 trailing 252-session 報酬分類 bull≥10%、bear≤-10%、其餘 sideways。", "",
        "### 結果", "", period_table, "", rolling_table, "", regime_table, "",
        "### 白話結論", "", "rolling 最差值與空頭／盤整表現比單一全期 CAGR 更能描述持有體驗；行情分類完全因果，不使用當日未完成資訊。", "",
        "### 對定版決策的影響", "", "策略定版不等於任何時段都獲利；未來監控應固定追蹤 rolling 5 年、最長水下期與 regime participation。", "",
        "## 交易穩定性與獲利集中度", "", "### 測試目的", "", "辨認連虧、水下期及少數大贏家的依賴程度。", "",
        "### 方法", "", "依完整成交回合計算連虧與極值；集中度測試固定原訊號序列，移除最佳 1/3/5 回合後重播現金帳戶，不重生新訊號。", "",
        "### 結果", "", stability_table, "", concentration_table, "",
        "### 白話結論", "", "策略需要容忍連續虧損與長期水下；移除大贏家後仍只是壓力測試，不能冒充完整反事實。", "",
        "### 對定版決策的影響", "", "監控應警示連虧、水下期與單筆貢獻集中，但不得因短期不順臨時改規則。", "",
        "## 參數穩健性", "", "### 測試目的", "", "檢查現行參數是否只是孤立樣本內尖峰。", "",
        "### 方法", "", "固定版本完成 243 組 factorial，另看中心一次只移動一個參數一格的 11 組局部鄰域。", "",
        "### 結果", "",
        f"中心 MA21/144 + MACD(21,55,55)：期末 {standard.money(parameter_center['ending_equity'])}、CAGR {standard.pct(parameter_center['cagr'])}、MaxDD {standard.pct(parameter_center['max_drawdown'])}；局部鄰域中位 CAGR {standard.pct(parameter_neighborhood['neighborhood_median_cagr'])}，範圍 {standard.pct(parameter_neighborhood['neighborhood_min_cagr'])}～{standard.pct(parameter_neighborhood['neighborhood_max_cagr'])}。", "", distribution_table, "",
        "### 白話結論", "", "中心不是靠單一精確數字才成立，但樣本內最高參數也不能直接取代現行設定。", "",
        "### 對定版決策的影響", "", "定版參數鎖定 21/144 與 MACD(21,55,55)；未來變更必須另走樣本外研究與 QA。", "",
        "## 資產配置研究", "", "### 測試目的", "", "評估部分資金長期持有 0050 對報酬與回落的影響。", "",
        "### 方法", "", "core 權重 0/20/40/60%，同時保留不再平衡主分析與每年首交易日 close 的年度敏感度；兩袖套以真實 cash/shares、股息、split 和必要交易成本逐日重建。", "",
        "### 結果", "", allocation_table, "",
        "### 白話結論", "", "更高 core 在本樣本可能提高期末值，也加深回落；這是資產配置研究，不是改寫正式策略。", "",
        "### 對定版決策的影響", "", "正式策略仍是 100% tactical 規則本身；任何 core 比例應由使用者另行做資產配置決策。", "",
        "## 限制、執行與監控規格", "",
        "- 單一歷史樣本不能證明未來正期望；指數參考線不可直接交易，亦未扣策略費稅。",
        "- 盤後產生訊號後，監控器應建立下一官方交易日開盤待執行單；退出同理。若末日無下一交易日，維持未成交／未平倉。",
        "- 每日需保存 technical/raw 價格、SMA21/144、histogram、regime、armed、固定防守、signal date、scheduled fill date、cash、shares、股息權利、split 與實際成本。",
        "- 監控告警：資料缺日、非官方交易日成交、cash<0、同日重進、已有持股加碼、價格／股份因子不守恆、實際滑價超過 40/60bps 壓力帶。", "",
        "## QA 與重播", "",
        f"兩成交版本核心資料 parity：`{parity['status']}`。SIGNAL_CLOSE 對 v1.6 source、NEXT_SESSION_OPEN 對四階段 v1.5，逐列檢查摘要、逐筆、流水、每日資產與公司行動。", "",
        "```powershell", "python -m backtests.dual_ma.etf_0050_final_strategy_v1 run",
        "python -m backtests.dual_ma.etf_0050_final_strategy_v1 validate",
        "python -m pytest tests/backtests/dual_ma/test_etf_0050_final_strategy_v1.py -q", "```", "",
        "## 完整附件", "", links, "",
    ])
    (root / "README.md").write_text(report, encoding="utf-8", newline="\n")
    (root / "report.md").write_text(report, encoding="utf-8", newline="\n")


def run_study(*, run_root: Path = RUN_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT) -> dict[str, Any]:
    if run_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite isolated final-strategy output")
    pins = (
        (SOURCE_RUN / "manifest.json", SOURCE_RUN_MANIFEST),
        (V16_REPORT / "artifact-manifest.json", V16_REPORT_MANIFEST),
        (FOUR_PHASE_RUN / "manifest.json", FOUR_PHASE_RUN_MANIFEST),
        (FOUR_PHASE_REPORT / "artifact-manifest.json", FOUR_PHASE_REPORT_MANIFEST),
    )
    for path, expected in pins:
        if _sha(path) != expected:
            raise ETF0050Error(f"protected manifest changed: {path}")
    protected = (SOURCE_RUN, V16_REPORT, FOUR_PHASE_RUN, FOUR_PHASE_REPORT)
    before = {str(path): tree_hash(path) for path in protected}

    phase_summary = standard.read_csv(FOUR_PHASE_RUN / "phase1-scenario-summary.csv")
    phase_details = standard.read_csv(FOUR_PHASE_RUN / "trade-details.csv")
    phase_tx = standard.read_csv(FOUR_PHASE_RUN / "transactions.csv")
    phase_daily = standard.read_csv(FOUR_PHASE_RUN / "daily-equity.csv")
    phase_actions = standard.read_csv(FOUR_PHASE_RUN / "corporate-actions.csv")
    source_summary = standard.read_csv(SOURCE_RUN / "scenario-summary.csv")
    source_details = standard.read_csv(SOURCE_RUN / "trade-details.csv")
    source_tx = standard.read_csv(SOURCE_RUN / "transactions.csv")
    source_daily = standard.read_csv(SOURCE_RUN / "daily-equity.csv")
    source_actions = standard.read_csv(SOURCE_RUN / "corporate-actions.csv")
    benchmark = standard.read_csv(SOURCE_RUN / "benchmark.csv")

    def phase(rows: Sequence[Mapping[str, Any]], timing: str, bps: int = 20) -> list[dict[str, Any]]:
        return _filter(rows, strategy_id=regime.FIXED_ID, execution_timing=timing, slippage_bps=bps)

    signal_parts = {
        "scenario-summary": phase(phase_summary, research.TIMING_SIGNAL_CLOSE),
        "trade-details": phase(phase_details, research.TIMING_SIGNAL_CLOSE),
        "transactions": phase(phase_tx, research.TIMING_SIGNAL_CLOSE),
        "daily-equity": phase(phase_daily, research.TIMING_SIGNAL_CLOSE),
        "corporate-actions": phase(phase_actions, research.TIMING_SIGNAL_CLOSE),
    }
    open_parts = {
        "scenario-summary": phase(phase_summary, research.TIMING_NEXT_OPEN),
        "trade-details": phase(phase_details, research.TIMING_NEXT_OPEN),
        "transactions": phase(phase_tx, research.TIMING_NEXT_OPEN),
        "daily-equity": phase(phase_daily, research.TIMING_NEXT_OPEN),
        "corporate-actions": phase(phase_actions, research.TIMING_NEXT_OPEN),
    }
    buy_hold_ids = {BUY_HOLD_CASH_ID, BUY_HOLD_REINVEST_ID}
    buy_summary_by_id = {row["strategy_id"]: dict(row) for row in source_summary if row["strategy_id"] in buy_hold_ids}
    buy_summary = [buy_summary_by_id[comparison_id] for comparison_id in (BUY_HOLD_CASH_ID, BUY_HOLD_REINVEST_ID)]
    buy_details = [dict(row) for row in source_details if row["strategy_id"] in buy_hold_ids]
    buy_tx = [dict(row) for row in source_tx if row["strategy_id"] in buy_hold_ids]
    buy_daily = [dict(row) for row in source_daily if row["strategy_id"] in buy_hold_ids]
    buy_actions = [dict(row) for row in source_actions if row["strategy_id"] in buy_hold_ids]

    comparison_calendar = {row["date"] for row in signal_parts["daily-equity"]}
    aligned_benchmark = [row for row in benchmark if row["date"] in comparison_calendar]
    if {row["date"] for row in aligned_benchmark} != comparison_calendar:
        raise ETF0050Error("TAIEX total-return reference does not cover the complete 0050 calendar")
    benchmark_summary, benchmark_daily = _benchmark_payload(aligned_benchmark)
    summaries = [
        _summary_row(signal_parts["scenario-summary"][0], comparison_id=SIGNAL_CLOSE_ID,
                     label="最終策略－訊號當日收盤成交版", timing="SIGNAL_CLOSE"),
        _summary_row(open_parts["scenario-summary"][0], comparison_id=NEXT_OPEN_ID,
                     label="最終策略－次一交易日開盤成交版", timing="NEXT_SESSION_OPEN"),
        *[_buy_hold_summary(row) for row in buy_summary], benchmark_summary,
    ]
    daily = [
        *_comparison_daily(signal_parts["daily-equity"], comparison_id=SIGNAL_CLOSE_ID),
        *_comparison_daily(open_parts["daily-equity"], comparison_id=NEXT_OPEN_ID),
    ]
    for comparison_id in (BUY_HOLD_CASH_ID, BUY_HOLD_REINVEST_ID):
        daily.extend(_comparison_daily(_filter(buy_daily, strategy_id=comparison_id), comparison_id=comparison_id))
    daily.extend(benchmark_daily)
    annual = []
    for comparison_id in MAIN_IDS:
        annual.extend(_annual_rows(comparison_id, _filter(daily, comparison_id=comparison_id)))
    reinvest_audit, reinvest_errors = _buy_hold_reinvest_audit(
        daily=daily, transactions=buy_tx, actions=buy_actions,
    )
    if reinvest_errors:
        raise ETF0050Error(f"buy-hold reinvest assumption failed: {reinvest_errors}")

    cost_rows = [dict(row) for row in phase_summary if row["strategy_id"] == regime.FIXED_ID
                 and row["execution_timing"] in {research.TIMING_SIGNAL_CLOSE, research.TIMING_NEXT_OPEN}
                 and int(row["slippage_bps"]) in {20, 40, 60}]
    rolling = _filter(standard.read_csv(FOUR_PHASE_RUN / "rolling-3-5-10.csv"), strategy_role="FIXED_PRIMARY")
    fixed_periods = _filter(standard.read_csv(FOUR_PHASE_RUN / "fixed-periods.csv"), strategy_role="FIXED_PRIMARY")
    market_regimes = standard.read_csv(FOUR_PHASE_RUN / "market-regime.csv")
    regime_performance = _regime_performance(_filter(daily, comparison_id=NEXT_OPEN_ID), market_regimes)
    trade_stability = _filter(standard.read_csv(FOUR_PHASE_RUN / "trade-stability.csv"), strategy_role="FIXED_PRIMARY")
    concentration = standard.read_csv(FOUR_PHASE_RUN / "concentration.csv")
    parameter_rows = standard.read_csv(FOUR_PHASE_RUN / "parameter-results-243.csv")
    parameter_center = next(row for row in parameter_rows if row["parameter_id"] == "MA21_144_MACD21_55_55")
    parameter_neighborhoods = standard.read_csv(FOUR_PHASE_RUN / "parameter-neighborhoods.csv")
    parameter_neighborhood = next(row for row in parameter_neighborhoods if row["parameter_id"] == "MA21_144_MACD21_55_55")
    parameter_distribution = standard.read_csv(FOUR_PHASE_RUN / "parameter-distribution.csv")
    allocations = standard.read_csv(FOUR_PHASE_RUN / "allocation-summary.csv")

    signal_parity: dict[str, Any] = {}
    open_parity: dict[str, Any] = {}
    mapping = {
        "scenario-summary": ("scenario-summary.csv", "phase1-scenario-summary.csv"),
        "trade-details": ("trade-details.csv", "trade-details.csv"),
        "transactions": ("transactions.csv", "transactions.csv"),
        "daily-equity": ("daily-equity.csv", "daily-equity.csv"),
        "corporate-actions": ("corporate-actions.csv", "corporate-actions.csv"),
    }
    for key, (signal_filename, open_filename) in mapping.items():
        signal_parity[signal_filename] = _parity_projection(
            source_path=SOURCE_RUN / signal_filename, actual_rows=signal_parts[key],
            source_selector={"strategy_id": regime.FIXED_ID},
        )
        open_parity[open_filename] = _parity_projection(
            source_path=FOUR_PHASE_RUN / open_filename, actual_rows=open_parts[key],
            source_selector={
                "strategy_id": regime.FIXED_ID,
                "execution_timing": research.TIMING_NEXT_OPEN,
                "slippage_bps": 20,
            },
        )
    parity = {
        "status": "PASS_EXACT" if all(row["exact"] for row in (*signal_parity.values(), *open_parity.values())) else "FAIL",
        "signal_close_vs_v1_6_source": signal_parity,
        "next_session_open_vs_four_phase_v1_5": open_parity,
    }
    if parity["status"] != "PASS_EXACT":
        raise ETF0050Error("final-strategy core parity failed")

    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2],
        text=True, encoding="utf-8",
    ).strip()
    produced_iso = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
    produced = datetime.fromisoformat(produced_iso).strftime("%Y-%m-%d %H:%M:%S（台北）")
    with tempfile.TemporaryDirectory(prefix="0050-final-strategy-v1-") as tmp:
        stage = Path(tmp)
        run_stage, report_stage = stage / "run", stage / "report"
        run_stage.mkdir(); report_stage.mkdir()
        datasets: dict[str, Sequence[Mapping[str, Any]]] = {
            "main-comparison-summary.csv": summaries, "main-comparison-daily.csv": daily,
            "main-comparison-calendar-year.csv": annual, "execution-cost-sensitivity.csv": cost_rows,
            "strategy-trade-details.csv": [*signal_parts["trade-details"], *open_parts["trade-details"]],
            "strategy-transactions.csv": [*signal_parts["transactions"], *open_parts["transactions"]],
            "strategy-corporate-actions.csv": [*signal_parts["corporate-actions"], *open_parts["corporate-actions"]],
            "buy-hold-trade-details.csv": buy_details, "buy-hold-transactions.csv": buy_tx,
            "buy-hold-corporate-actions.csv": buy_actions, "rolling-3-5-10.csv": rolling,
            "buy-hold-reinvest-audit.csv": reinvest_audit,
            "fixed-periods.csv": fixed_periods, "market-regime.csv": market_regimes,
            "market-regime-performance.csv": regime_performance, "trade-stability.csv": trade_stability,
            "concentration.csv": concentration, "parameter-results-243.csv": parameter_rows,
            "parameter-rolling-5y.csv": standard.read_csv(FOUR_PHASE_RUN / "parameter-rolling-5y.csv"),
            "parameter-neighborhoods.csv": parameter_neighborhoods,
            "parameter-distribution.csv": parameter_distribution,
            "parameter-heatmap.csv": standard.read_csv(FOUR_PHASE_RUN / "parameter-heatmap.csv"),
            "allocation-summary.csv": allocations,
            "allocation-daily-equity.csv": standard.read_csv(FOUR_PHASE_RUN / "allocation-daily-equity.csv"),
            "allocation-sleeve-ledger.csv": standard.read_csv(FOUR_PHASE_RUN / "allocation-sleeve-ledger.csv"),
            "benchmark.csv": aligned_benchmark,
        }
        for prefix, parts in (("signal-close", signal_parts), ("next-session-open", open_parts)):
            for key, rows in parts.items():
                datasets[f"{prefix}-{key}.csv"] = rows
        for name, rows in datasets.items():
            _write_csv(run_stage / name, rows)
        _write_json(run_stage / "core-parity.json", parity)
        protected_after = {str(path): tree_hash(path) for path in protected}
        if before != protected_after:
            raise ETF0050Error("protected source trees changed")
        _write_json(run_stage / "input-manifest.json", {
            "status": "PASS", "version": VERSION, "produced_at_taipei": produced_iso,
            "git_commit": git_commit, "period": {"warmup": "2005", "start": str(START), "end": str(END)},
            "initial_capital": str(CAPITAL), "annual_reset": False,
            "final_strategy_id": regime.FIXED_ID,
            "execution_versions": {
                "SIGNAL_CLOSE": "entry and exit at same signal-day close; idealized research control",
                "NEXT_SESSION_OPEN": "entry and exit at next official-session open; practical primary",
            },
            "main_cost": "buy and sell 20bps plus commission and ETF sell tax",
            "buy_hold_reinvest_assumption": {
                "timing": "payment-day close", "audited_transactions": len(reinvest_audit),
                "practical_caveat": "same-day dividend availability depends on broker posting time",
            },
            "stress_cost_bps": [40, 60], "protected_before": before, "protected_after": protected_after,
        })
        run_excluded = {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"}
        run_artifacts = _artifact_rows(run_stage, run_excluded)
        _write_json(run_stage / "manifest.json", {
            "status": "STOP_WAIT_00_QA", "version": VERSION, "produced_at_taipei": produced_iso,
            "git_commit": git_commit, "artifact_count": len(run_artifacts), "artifacts": run_artifacts,
        })
        run_bad = _verify_artifacts(run_stage, run_artifacts)
        _write_json(run_stage / "readback.json", {"status": "PASS" if not run_bad else "FAIL", "mismatches": run_bad})
        _write_json(run_stage / "QA_READY.json", {
            "status": "STOP_WAIT_00_QA", "version": VERSION, "ready_for_claimed_acceptance": False,
        })
        _write_json(run_stage / "checkpoint.json", {
            "status": "STOP_WAIT_00_QA", "version": VERSION,
            "manifest_sha256": _sha(run_stage / "manifest.json"),
            "readback_sha256": _sha(run_stage / "readback.json"), "safe_next_step": "00 independent final-strategy QA",
        })
        if run_bad:
            raise ETF0050Error(f"run readback failed: {run_bad}")

        for path in run_stage.iterdir():
            if path.is_file() and path.name not in {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"}:
                shutil.copy2(path, report_stage / path.name)
        _write_line_svg(report_stage / "main-comparison-equity.svg", daily)
        attachments = [
            "report.md", "main-comparison-equity.svg", *datasets.keys(), "core-parity.json",
            "input-manifest.json", "artifact-manifest.json", "readback.json", "checkpoint.json",
        ]
        _render_report(
            root=report_stage, summary=summaries, annual=annual, cost_rows=cost_rows,
            rolling=rolling, fixed_periods=fixed_periods, regime_rows=regime_performance,
            trade_stability=trade_stability, concentration=concentration,
            parameter_center=parameter_center, parameter_neighborhood=parameter_neighborhood,
            parameter_distribution=parameter_distribution, allocations=allocations,
            parity=parity, produced=produced, attachments=attachments,
        )
        for report_name in ("README.md", "report.md"):
            violations = _report_contract_violations((report_stage / report_name).read_text(encoding="utf-8"))
            if violations:
                raise ETF0050Error(f"{report_name} contract failed: {violations}")
        shutil.copy2(run_stage / "input-manifest.json", report_stage / "input-manifest.json")
        for name in ("artifact-manifest.json", "readback.json", "checkpoint.json"):
            _write_json(report_stage / name, {"status": "BUILDING"})
        broken = _broken_markdown_links(report_stage)
        if broken:
            raise ETF0050Error(f"broken report links/anchors: {broken}")
        report_excluded = {"artifact-manifest.json", "readback.json", "checkpoint.json"}
        report_artifacts = _artifact_rows(report_stage, report_excluded)
        missing = research._readme_missing_artifacts(report_stage, [str(row["path"]) for row in report_artifacts])
        if missing:
            raise ETF0050Error(f"README artifact coverage incomplete: {missing}")
        _write_json(report_stage / "artifact-manifest.json", {
            "status": "STOP_WAIT_00_QA", "version": VERSION + "-report",
            "produced_at_taipei": produced_iso, "source_run_manifest_sha256": _sha(run_stage / "manifest.json"),
            "artifact_count": len(report_artifacts), "artifacts": report_artifacts,
        })
        report_bad = _verify_artifacts(report_stage, report_artifacts)
        _write_json(report_stage / "readback.json", {"status": "PASS" if not report_bad else "FAIL", "mismatches": report_bad})
        _write_json(report_stage / "checkpoint.json", {
            "status": "STOP_WAIT_00_QA", "version": VERSION,
            "artifact_manifest_sha256": _sha(report_stage / "artifact-manifest.json"),
            "readback_sha256": _sha(report_stage / "readback.json"), "safe_next_step": "00 independent final report QA",
        })
        if report_bad:
            raise ETF0050Error(f"report readback failed: {report_bad}")
        run_root.parent.mkdir(parents=True, exist_ok=True)
        report_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(run_stage, run_root)
        shutil.copytree(report_stage, report_root)
    return {
        "status": "STOP_WAIT_00_QA", "run_root": str(run_root), "report_root": str(report_root),
        "run_manifest_sha256": _sha(run_root / "manifest.json"),
        "run_readback_sha256": _sha(run_root / "readback.json"),
        "report_manifest_sha256": _sha(report_root / "artifact-manifest.json"),
        "report_readback_sha256": _sha(report_root / "readback.json"),
        "core_parity": parity["status"], "main_comparison_lines": len(summaries),
        "protected_trees_unchanged": True,
    }


def validate(*, run_root: Path = RUN_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT) -> dict[str, Any]:
    run_manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8-sig"))
    report_manifest = json.loads((report_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
    run_checkpoint = json.loads((run_root / "checkpoint.json").read_text(encoding="utf-8-sig"))
    report_checkpoint = json.loads((report_root / "checkpoint.json").read_text(encoding="utf-8-sig"))
    run_bad = _verify_artifacts(run_root, run_manifest["artifacts"])
    report_bad = _verify_artifacts(report_root, report_manifest["artifacts"])
    broken = _broken_markdown_links(report_root)
    missing = research._readme_missing_artifacts(report_root, [str(row["path"]) for row in report_manifest["artifacts"]])
    parity = json.loads((run_root / "core-parity.json").read_text(encoding="utf-8-sig"))
    summary = standard.read_csv(run_root / "main-comparison-summary.csv")
    daily = standard.read_csv(run_root / "main-comparison-daily.csv")
    annual = standard.read_csv(run_root / "main-comparison-calendar-year.csv")
    buy_tx = standard.read_csv(run_root / "buy-hold-transactions.csv")
    buy_actions = standard.read_csv(run_root / "buy-hold-corporate-actions.csv")
    stored_reinvest_audit = standard.read_csv(run_root / "buy-hold-reinvest-audit.csv")
    recomputed_reinvest_audit, reinvest_errors = _buy_hold_reinvest_audit(
        daily=daily, transactions=buy_tx, actions=buy_actions,
    )
    reinvest_audit_ok = (
        not reinvest_errors and len(stored_reinvest_audit) == len(recomputed_reinvest_audit) == 31
        and all(row.get("all_checks_pass") == "True" for row in stored_reinvest_audit)
    )
    report_contract_errors = {
        name: _report_contract_violations((report_root / name).read_text(encoding="utf-8"))
        for name in ("README.md", "report.md")
    }
    version_consistent = (
        run_manifest.get("version") == VERSION
        and report_manifest.get("version") == VERSION + "-report"
        and run_checkpoint.get("version") == VERSION
        and report_checkpoint.get("version") == VERSION
        and run_root.name.endswith("v1-2") and report_root.name.endswith("v1-2")
    )
    ids = {row["comparison_id"] for row in summary}
    row_count_errors = sum(len(_filter(daily, comparison_id=comparison_id)) != 5093 for comparison_id in MAIN_IDS)
    ending_errors = 0
    account_errors = 0
    for item in summary:
        rows = _filter(daily, comparison_id=item["comparison_id"])
        if not rows or _d(rows[-1]["equity"]) != _d(item["ending_equity"]):
            ending_errors += 1
        if item["comparison_id"] != BENCHMARK_ID:
            account_errors += sum(_d(row["equity"]) != _d(row["cash"]) + _d(row["liquidation_value"]) for row in rows)
    annual_errors = 0
    for comparison_id in MAIN_IDS:
        years = _filter(annual, comparison_id=comparison_id)
        if len(years) != 21 or _d(years[-1]["ending_equity"]) != _d(_filter(summary, comparison_id=comparison_id)[0]["ending_equity"]):
            annual_errors += 1
    protected_ok = all(_sha(path) == expected for path, expected in (
        (SOURCE_RUN / "manifest.json", SOURCE_RUN_MANIFEST),
        (V16_REPORT / "artifact-manifest.json", V16_REPORT_MANIFEST),
        (FOUR_PHASE_RUN / "manifest.json", FOUR_PHASE_RUN_MANIFEST),
        (FOUR_PHASE_REPORT / "artifact-manifest.json", FOUR_PHASE_REPORT_MANIFEST),
    ))
    checks = {
        "main_ids_exact": ids == set(MAIN_IDS), "main_summary_rows": len(summary),
        "daily_row_count_errors": row_count_errors, "daily_account_equation_errors": account_errors,
        "summary_ending_errors": ending_errors, "annual_errors": annual_errors,
        "parity": parity["status"], "protected_pins": protected_ok,
        "reinvest_audit_rows": len(stored_reinvest_audit),
        "reinvest_audit": "PASS" if reinvest_audit_ok else "FAIL",
        "reinvest_errors": reinvest_errors, "report_contract_errors": report_contract_errors,
        "version_consistent": version_consistent,
    }
    status = "PASS" if (
        not run_bad and not report_bad and not broken and not missing and ids == set(MAIN_IDS)
        and len(summary) == 5 and not row_count_errors and not account_errors and not ending_errors
        and not annual_errors and parity["status"] == "PASS_EXACT" and protected_ok
        and reinvest_audit_ok and not any(report_contract_errors.values()) and version_consistent
    ) else "FAIL"
    return {
        "status": status, "run_artifacts": len(run_manifest["artifacts"]), "run_mismatches": run_bad,
        "report_artifacts": len(report_manifest["artifacts"]), "report_mismatches": report_bad,
        "broken_links_or_anchors": broken, "readme_missing_artifacts": missing,
        "checks": checks, "network_used": False, "writes_performed": False,
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


if __name__ == "__main__":
    raise SystemExit(main())
