"""Publish the accepted 0050 continuous-account replay in the standard v2 report format."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .paths import report_path


SOURCE_ROOT = report_path("etf", "0050", "base-v2.2")
BENCHMARK_ROOT = report_path("analysis", "shared-account-position-sizing-v2")
OUTPUT_ROOT_DEFAULT = report_path("etf", "0050", "base-v3")
INITIAL_CAPITAL = Decimal("500000")
YEARS = tuple(range(2022, 2027))
TIMING_PREFIXES = ("A_", "B_", "C_")
METHOD_ORDER = (
    "A_MEDIUM",
    "A_LONG",
    "B_MEDIUM",
    "B_LONG",
    "C_LONG",
    "BUY_HOLD_CASH",
    "BUY_HOLD_REINVEST",
)
COLORS = {
    "A_MEDIUM": "#2563eb",
    "A_LONG": "#16a34a",
    "B_MEDIUM": "#7c3aed",
    "B_LONG": "#ea580c",
    "C_LONG": "#dc2626",
    "BUY_HOLD_CASH": "#64748b",
    "BUY_HOLD_REINVEST": "#111827",
    "MARKET": "#0891b2",
}
EXIT_REASON_ZH = {
    "STRUCTURAL_STOP": "防守價或均線趨勢失效",
    "REGIME_EXIT": "均線多頭區間結束",
    "PROFIT_EXIT": "獲利保護條件成立",
    "KD_PROFIT_EXIT": "KD死亡交叉停利",
    "M1_EXIT": "原策略出場",
    "KD_DEATH_CROSS": "KD死亡交叉停利",
    "": "截至截止日仍持有",
}


class StandardReportError(RuntimeError):
    pass


def dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def money(value: Any) -> str:
    return f"{dec(value):,.2f}"


def pct(value: Any) -> str:
    return "—" if value in (None, "") else f"{dec(value) * 100:.2f}%"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8", newline="\n")
        return
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(jsonable(list(rows)))


def md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = [
        "|" + "|".join(headers) + "|",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("|" + "|".join(str(value) for value in row) + "|" for row in rows)
    return "\n".join(lines)


def verify_source(source_root: Path, qa_filename: str = "QA_ACCEPTED.json") -> dict[str, Any]:
    manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    qa_path = source_root / qa_filename
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    if qa.get("status") not in {"QA_ACCEPTED", "STOP_WAIT_00_QA"}:
        raise StandardReportError("source replay is not accepted or ready for 00 QA")
    mismatches = []
    for item in manifest["artifacts"]:
        path = source_root / item["path"]
        if not path.is_file() or path.stat().st_size != item["bytes"] or sha256(path) != item["sha256"]:
            mismatches.append(item["path"])
    if mismatches:
        raise StandardReportError(f"source artifact mismatch: {mismatches}")
    return {
        "manifest_sha256": sha256(source_root / "manifest.json"),
        "qa_sha256": sha256(qa_path),
        "artifact_count": len(manifest["artifacts"]),
    }


def curve_metrics(values: Sequence[tuple[date, Decimal]], initial: Decimal | None = None) -> dict[str, Any]:
    if not values:
        raise StandardReportError("empty curve")
    points = list(values)
    peak = initial if initial is not None else points[0][1]
    max_drawdown = Decimal("0")
    peak_day = points[0][0]
    drawdown_peak = peak_day
    drawdown_trough = peak_day
    for day, value in points:
        if value > peak:
            peak = value
            peak_day = day
        drawdown = value / peak - 1 if peak else Decimal("0")
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            drawdown_peak = peak_day
            drawdown_trough = day
    start_value = initial if initial is not None else points[0][1]
    elapsed = Decimal(max((points[-1][0] - points[0][0]).days, 1)) / Decimal("365.2425")
    cumulative = points[-1][1] / start_value - 1
    annualized = Decimal(str(math.pow(float(points[-1][1] / start_value), 1 / float(elapsed)) - 1))
    return {
        "start_value": start_value,
        "end_value": points[-1][1],
        "cumulative_return": cumulative,
        "annualized_return": annualized,
        "max_drawdown": max_drawdown,
        "drawdown_peak_date": drawdown_peak,
        "drawdown_trough_date": drawdown_trough,
        "return_to_drawdown": annualized / abs(max_drawdown) if max_drawdown else None,
    }


def method_kind(strategy_id: str) -> str:
    return "擇時策略" if strategy_id.startswith(TIMING_PREFIXES) else "全程持有基準"


def benchmark_years(benchmark: Sequence[Mapping[str, str]]) -> dict[int, dict[str, Any]]:
    output: dict[int, dict[str, Any]] = {}
    prior = INITIAL_CAPITAL
    for year in YEARS:
        rows = [row for row in benchmark if date.fromisoformat(row["date"]).year == year]
        points = [(date.fromisoformat(row["date"]), dec(row["equivalent_500k"])) for row in rows]
        metrics = curve_metrics(points, prior)
        output[year] = {
            "benchmark_return": dec(rows[-1]["equivalent_500k"]) / prior - 1,
            "benchmark_max_drawdown": metrics["max_drawdown"],
            "benchmark_ending_equity": dec(rows[-1]["equivalent_500k"]),
        }
        prior = dec(rows[-1]["equivalent_500k"])
    return output


def annual_summaries(
    curves: Sequence[Mapping[str, str]],
    transactions: Sequence[Mapping[str, str]],
    details: Sequence[Mapping[str, str]],
    actions: Sequence[Mapping[str, str]],
    benchmark: Sequence[Mapping[str, str]],
    labels: Mapping[str, str],
) -> list[dict[str, Any]]:
    by_strategy: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in curves:
        by_strategy[row["strategy_id"]].append(row)
    transaction_by_strategy: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in transactions:
        transaction_by_strategy[row["strategy_id"]].append(row)
    detail_by_strategy: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in details:
        detail_by_strategy[row["strategy_id"]].append(row)
    action_by_strategy: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in actions:
        action_by_strategy[row["strategy_id"]].append(row)
    benchmark_by_year = benchmark_years(benchmark)
    output: list[dict[str, Any]] = []
    for strategy_id in METHOD_ORDER:
        strategy_curves = sorted(by_strategy[strategy_id], key=lambda row: row["date"])
        strategy_transactions = transaction_by_strategy[strategy_id]
        buy_by_leg = {
            row["leg_id"]: dec(row["cash_amount"])
            for row in strategy_transactions
            if row.get("leg_id") and row["role"] != "EXIT"
        }
        prior_end = INITIAL_CAPITAL
        cumulative_control_cost = Decimal("0")
        for year in YEARS:
            year_rows = [row for row in strategy_curves if date.fromisoformat(row["date"]).year == year]
            points = [(date.fromisoformat(row["date"]), dec(row["equity"])) for row in year_rows]
            metrics = curve_metrics(points, prior_end)
            year_tx = [row for row in strategy_transactions if date.fromisoformat(row["date"]).year == year]
            entries = [row for row in year_tx if row["role"] in {"MOTHER", "首次買進"}]
            exits = [row for row in year_tx if row["role"] == "EXIT"]
            buys = [row for row in year_tx if row["role"] != "EXIT"]
            buy_turnover = sum((dec(row["cash_amount"]) for row in buys), Decimal("0"))
            if strategy_id.startswith(TIMING_PREFIXES):
                peak_cost = max(
                    (buy_by_leg.get(row.get("active_leg_id", ""), Decimal("0")) for row in year_rows),
                    default=Decimal("0"),
                )
            else:
                cumulative_control_cost += buy_turnover
                peak_cost = cumulative_control_cost
            realized = sum(
                (
                    dec(row["net_pnl_or_estimate"])
                    for row in detail_by_strategy[strategy_id]
                    if row.get("exit_date")
                    and date.fromisoformat(row["exit_date"]).year == year
                    and row["status_zh"] == "已賣出"
                ),
                Decimal("0"),
            )
            dividends = sum(
                (
                    dec(row["cash"])
                    for row in action_by_strategy[strategy_id]
                    if row.get("action_kind") == "DIVIDEND"
                    and row.get("stage") == "CASH_PAID"
                    and row.get("payment_date")
                    and date.fromisoformat(row["payment_date"]).year == year
                ),
                Decimal("0"),
            )
            cost = sum(
                (
                    dec(row.get("commission"))
                    + dec(row.get("tax"))
                    + abs(dec(row.get("fill_price")) - dec(row.get("reference_price"))) * int(row["shares"])
                    for row in year_tx
                ),
                Decimal("0"),
            )
            last = year_rows[-1]
            row = {
                "strategy_id": strategy_id,
                "strategy_label": labels[strategy_id],
                "method_kind": method_kind(strategy_id),
                "year": year,
                "period_end": last["date"],
                "starting_equity": prior_end,
                "ending_equity": dec(last["equity"]),
                "year_return": dec(last["equity"]) / prior_end - 1,
                "year_max_drawdown": metrics["max_drawdown"],
                "average_cash_ratio": sum(
                    (dec(item["cash"]) / dec(item["equity"]) for item in year_rows if dec(item["equity"]) > 0),
                    Decimal("0"),
                )
                / len(year_rows),
                "entries": len(entries),
                "exits": len(exits),
                "ending_open_positions": 1 if int(last["shares"]) > 0 else 0,
                "buy_turnover": buy_turnover,
                "peak_position_cost": peak_cost,
                "realized_pnl_from_exits": realized,
                "dividends_received": dividends,
                "fees_tax_and_slippage": cost,
                **benchmark_by_year[year],
            }
            output.append(row)
            prior_end = dec(last["equity"])
    return output


def overall_summaries(
    metrics: Sequence[Mapping[str, str]], annual: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    annual_by_strategy: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in annual:
        annual_by_strategy[str(row["strategy_id"])].append(row)
    output = []
    for raw in sorted(metrics, key=lambda row: METHOD_ORDER.index(row["strategy_id"])):
        strategy_id = raw["strategy_id"]
        years = sorted(annual_by_strategy[strategy_id], key=lambda row: int(row["year"]))
        profitable = [int(row["year"]) for row in years if dec(row["year_return"]) > 0]
        losing = [int(row["year"]) for row in years if dec(row["year_return"]) < 0]
        flat = [int(row["year"]) for row in years if dec(row["year_return"]) == 0]
        through_2024 = next(dec(row["ending_equity"]) for row in years if int(row["year"]) == 2024)
        defense_2022 = next(row for row in years if int(row["year"]) == 2022)
        total_profit = dec(raw["ending_equity"]) - INITIAL_CAPITAL
        later_profit = dec(raw["ending_equity"]) - through_2024
        output.append(
            {
                **raw,
                "method_kind": method_kind(strategy_id),
                "profitable_year_count": len(profitable),
                "profitable_years": "、".join(map(str, profitable)) or "無",
                "losing_year_count": len(losing),
                "losing_years": "、".join(map(str, losing)) or "無",
                "flat_years": "、".join(map(str, flat)) or "無",
                "return_to_drawdown": dec(raw["cagr"]) / abs(dec(raw["max_asset_drawdown"]))
                if dec(raw["max_asset_drawdown"])
                else None,
                "profit_through_2024": through_2024 - INITIAL_CAPITAL,
                "profit_during_2025_2026": later_profit,
                "share_of_profit_from_2025_2026": later_profit / total_profit if total_profit > 0 else None,
                "year_2022_return_advantage_vs_benchmark": dec(defense_2022["year_return"])
                - dec(defense_2022["benchmark_return"]),
                "year_2022_drawdown_advantage_vs_benchmark": abs(dec(defense_2022["benchmark_max_drawdown"]))
                - abs(dec(defense_2022["year_max_drawdown"])),
            }
        )
    return output


def comparison_daily(
    curves: Sequence[Mapping[str, str]], benchmark: Sequence[Mapping[str, str]], labels: Mapping[str, str]
) -> list[dict[str, Any]]:
    output = [
        {
            "date": row["date"],
            "strategy_id": row["strategy_id"],
            "strategy_label": labels[row["strategy_id"]],
            "method_kind": method_kind(row["strategy_id"]),
            "equity": dec(row["equity"]),
            "drawdown": dec(row["drawdown"]),
            "cash": dec(row["cash"]),
            "shares": int(row["shares"]),
        }
        for row in curves
    ]
    peak = INITIAL_CAPITAL
    for row in benchmark:
        equity = dec(row["equivalent_500k"])
        peak = max(peak, equity)
        output.append(
            {
                "date": row["date"],
                "strategy_id": "MARKET",
                "strategy_label": "台股加權報酬指數",
                "method_kind": "大盤基準",
                "equity": equity,
                "drawdown": equity / peak - 1,
                "cash": Decimal("0"),
                "shares": "",
            }
        )
    return sorted(output, key=lambda row: (row["date"], row["strategy_id"]))


def svg_chart(path: Path, daily: Sequence[Mapping[str, Any]], *, value_field: str, title: str) -> None:
    width, height = 1380, 760
    left, right, top, bottom = 85, 40, 90, 65
    series: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in daily:
        series[str(row["strategy_id"])].append(row)
    ordered = [*METHOD_ORDER, "MARKET"]
    all_values = [dec(row[value_field]) for key in ordered for row in series[key]]
    low, high = min(all_values), max(all_values)
    if value_field == "drawdown":
        high = Decimal("0")
    span = high - low or Decimal("1")
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{left}" y="38" font-size="24" font-family="sans-serif">{title}</text>',
    ]
    for grid in range(6):
        y = top + grid * (height - top - bottom) / 5
        value = high - span * Decimal(grid) / Decimal(5)
        label = f"{float(value) * 100:.0f}%" if value_field == "drawdown" else f"{float(value):,.0f}"
        lines.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        lines.append(f'<text x="5" y="{y+5:.1f}" font-size="12" font-family="sans-serif">{label}</text>')
    legend_x = 770
    for index, key in enumerate(ordered):
        values = sorted(series[key], key=lambda row: row["date"])
        points = []
        for position, row in enumerate(values):
            x = left + position * (width - left - right) / max(1, len(values) - 1)
            y = top + float((high - dec(row[value_field])) / span) * (height - top - bottom)
            points.append(f"{x:.1f},{y:.1f}")
        dash = ' stroke-dasharray="7 5"' if key in {"BUY_HOLD_CASH", "BUY_HOLD_REINVEST", "MARKET"} else ""
        lines.append(
            f'<polyline points="{" ".join(points)}" fill="none" stroke="{COLORS[key]}" stroke-width="2"{dash}/>'
        )
        legend_y = 54 + (index % 4) * 20
        x = legend_x + (index // 4) * 280
        label = str(values[0]["strategy_label"])
        lines.append(f'<line x1="{x}" y1="{legend_y}" x2="{x+28}" y2="{legend_y}" stroke="{COLORS[key]}" stroke-width="3"{dash}/>')
        lines.append(f'<text x="{x+35}" y="{legend_y+5}" font-size="12" font-family="sans-serif">{label}</text>')
    dates = sorted({str(row["date"]) for row in daily})
    lines.extend(
        [
            f'<text x="{left}" y="{height-20}" font-size="12" font-family="sans-serif">{dates[0]}</text>',
            f'<text x="{width-right-75}" y="{height-20}" font-size="12" font-family="sans-serif">{dates[-1]}</text>',
            "</svg>",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def overall_table(rows: Sequence[Mapping[str, Any]]) -> str:
    return md_table(
        (
            "方法",
            "獲利年度",
            "最後總資產",
            "累積報酬",
            "換算年報酬",
            "最大資產回落",
            "年報酬／回落",
            "平均現金閒置",
            "單一時點最高持倉成本",
            "總進場",
            "完成買賣",
            "截止仍持有",
        ),
        (
            (
                row["strategy_label"],
                f"{row['profitable_year_count']}／{len(YEARS)}（{row['profitable_years']}）",
                money(row["ending_equity"]),
                pct(row["total_return"]),
                pct(row["cagr"]),
                pct(row["max_asset_drawdown"]),
                f"{dec(row['return_to_drawdown']):.2f}" if row["return_to_drawdown"] is not None else "—",
                pct(row["idle_cash_ratio"]),
                money(row["peak_concurrent_investment"]),
                int(row["closed_rounds"]) + int(row["open_rounds"]),
                row["closed_rounds"],
                row["open_rounds"],
            )
            for row in rows
        ),
    )


def opportunity_table(signal_audit: Sequence[Mapping[str, str]]) -> str:
    period = f"{YEARS[0]}-{YEARS[-1]}"
    rows = [row for row in signal_audit if row["period"] == period]
    rows.sort(key=lambda row: METHOD_ORDER.index(row["strategy_id"]))
    return md_table(
        (
            "方法",
            "完整訊號候選",
            "實際買進",
            "已有持股，不重複買",
            "144日線沒有向上",
            "風險寬度不足7%",
            "高於21日線不足5%",
            "資料不足",
            "其他未進場",
        ),
        (
            (
                row["strategy_label"],
                row["candidate_decisions"],
                row["filled"],
                row["already_holding"],
                row["ma144_not_up"],
                row["risk_width_below_7pct"],
                row["extension_below_5pct"],
                row["input_undetermined"],
                row["other_rejected"],
            )
            for row in rows
        ),
    )


def no_trade_explanations(
    summaries: Sequence[Mapping[str, Any]],
    signal_audit: Sequence[Mapping[str, str]],
) -> list[str]:
    totals = {
        row["strategy_id"]: row
        for row in signal_audit
        if row["period"] == f"{YEARS[0]}-{YEARS[-1]}"
    }
    lines = []
    for summary in summaries:
        if summary["method_kind"] != "擇時策略" or int(summary["rounds"]) != 0:
            continue
        audit = totals.get(summary["strategy_id"])
        if audit is None or int(audit["candidate_decisions"]) == 0:
            lines.append(f"- {summary['strategy_label']}：研究期間沒有完整進場訊號，因此沒有交易機會。")
            continue
        reasons = []
        for field, label in (
            ("ma144_not_up", "144日線沒有向上"),
            ("risk_width_below_7pct", "風險寬度不足7%"),
            ("extension_below_5pct", "高於21日線不足5%"),
            ("input_undetermined", "當時資料不足"),
            ("other_rejected", "其他條件不通過"),
        ):
            if int(audit[field]):
                reasons.append(f"{label} {audit[field]} 次")
        lines.append(
            f"- {summary['strategy_label']}：有 {audit['candidate_decisions']} 次完整訊號候選，"
            f"但沒有一次同時通過全部條件（{'、'.join(reasons)}），因此沒有交易機會。"
        )
    return lines


def render_reports(
    summaries: Sequence[Mapping[str, Any]],
    annual: Sequence[Mapping[str, Any]],
    details: Sequence[Mapping[str, str]],
    market: Mapping[str, Any],
    signal_audit: Sequence[Mapping[str, str]],
    period_start: date = date(2022, 1, 3),
    period_end: date = date(2026, 9, 30),
) -> tuple[str, str, str, str]:
    timing = [row for row in summaries if row["method_kind"] == "擇時策略"]
    controls = [row for row in summaries if row["method_kind"] == "全程持有基準"]
    best = max(timing, key=lambda row: dec(row["ending_equity"]))
    annual_timing = [row for row in annual if row["method_kind"] == "擇時策略"]
    annual_controls = [row for row in annual if row["method_kind"] == "全程持有基準"]
    annual_headers = (
        "方法",
        "年度",
        "年初資產",
        "年底／截止資產",
        "帳戶報酬",
        "帳戶最大回落",
        "大盤報酬",
        "大盤最大回落",
        "進場",
        "賣出",
        "年底仍持有",
        "平均現金閒置",
    )
    annual_rows = lambda rows: (
        (
            row["strategy_label"],
            f"{row['year']}{'（至9/30）' if int(row['year']) == 2026 else ''}",
            money(row["starting_equity"]),
            money(row["ending_equity"]),
            pct(row["year_return"]),
            pct(row["year_max_drawdown"]),
            pct(row["benchmark_return"]),
            pct(row["benchmark_max_drawdown"]),
            row["entries"],
            row["exits"],
            row["ending_open_positions"],
            pct(row["average_cash_ratio"]),
        )
        for row in rows
    )
    regime = md_table(
        (
            "方法",
            "截至2024累積獲利",
            "2025～2026增加獲利",
            "後兩年占總獲利",
            "2022比大盤多賺／少賠",
            "2022最大回落少於大盤",
        ),
        (
            (
                row["strategy_label"],
                money(row["profit_through_2024"]),
                money(row["profit_during_2025_2026"]),
                pct(row["share_of_profit_from_2025_2026"]),
                pct(row["year_2022_return_advantage_vs_benchmark"]),
                pct(row["year_2022_drawdown_advantage_vs_benchmark"]),
            )
            for row in timing
        ),
    )
    capital = md_table(
        (
            "方法",
            "買入周轉總額",
            "單一時點最高持倉成本",
            "已賣出損益",
            "截止日持股估算損益",
            "股利收入",
            "實際已發生費稅與價差",
            "截止估值預留賣出費稅與價差",
        ),
        (
            (
                row["strategy_label"],
                money(row["cumulative_buy"]),
                money(row["peak_concurrent_investment"]),
                money(row["realized_net_pnl"]),
                money(row["cutoff_estimated_pnl"]),
                money(row["dividends_received"]),
                money(dec(row["actual_fees_and_tax"]) + dec(row["actual_slippage_cost"])),
                money(
                    dec(row["cutoff_estimated_sale_fees_and_tax"])
                    + dec(row["cutoff_estimated_sale_slippage_cost"])
                ),
            )
            for row in timing
        ),
    )
    no_trade_notes = no_trade_explanations(summaries, signal_audit)
    main = "\n".join(
        [
            "# 0050｜50萬元連續帳戶正式報告",
            "",
            "![0050策略、全程持有與大盤的每日總資產](equity-curves.svg)",
            "",
            "## 先看結論",
            "",
            f"五個擇時策略都只在 {period_start} 投入一次 50 萬元，之後連續操作到 {period_end}，不在每年一月重設。五個擇時策略中，最後總資產最高的是「{best['strategy_label']}」：{money(best['ending_equity'])} 元；獲利年度 {best['profitable_year_count']} 個（{best['profitable_years']}）。",
            "",
            "## 五個會進出的擇時策略",
            "",
            overall_table(timing),
            "",
            "## 兩個從頭持有到截止日的對照基準",
            "",
            "這兩組不參與擇時策略排名；用途是檢查判斷買賣點是否勝過直接持有。",
            "",
            overall_table(controls),
            "",
            "## 大盤基準",
            "",
            md_table(
                ("方法", "最後總資產", "累積報酬", "換算年報酬", "最大資產回落", "獲利年度"),
                (("台股加權報酬指數", money(market["end_value"]), pct(market["cumulative_return"]), pct(market["annualized_return"]), pct(market["max_drawdown"]), market["profitable_years"]),),
            ),
            "",
            "## 五個擇時策略的每年表現",
            "",
            md_table(annual_headers, annual_rows(annual_timing)),
            "",
            "## 全程持有基準的每年表現",
            "",
            md_table(annual_headers, annual_rows(annual_controls)),
            "",
            "## 空頭防守與後段多頭依賴",
            "",
            regime,
            "",
            "後兩年占比高不代表策略無效，但表示最終獲利較依賴 2025～2026 行情。2026 只統計到 9 月 30 日。",
            "",
            "## 資金與交易過程",
            "",
            capital,
            "",
            "## 進場機會稽核",
            "",
            "表中的『完整訊號候選』是均線與型態已走到可判斷買進的那一天；實際買進前，仍須逐項通過該方法要求的條件。已有持股時不會再重複買進。",
            "",
            opportunity_table(signal_audit),
            "",
            *no_trade_notes,
            *( [""] if no_trade_notes else [] ),
            "嚴格長線版會依序檢查三項條件：144日線向上、風險寬度至少7%、收盤價至少高於21日線5%；它不是只檢查最後一項。",
            "",
            "## 這份重播怎麼做",
            "",
            "- 每個方法都是獨立的 50 萬元帳戶；不是把五個方法同時塞入同一帳戶。",
            "- 擇時策略每次進場使用當時帳戶內幾乎全部可用現金，並將已實現獲利繼續投入；不是每筆固定 1 萬元。",
            "- 持倉、現金與策略狀態可以跨年；年度表只是從同一條連續資產曲線切片。",
            "- 0050 是長期持續監控標的；一段均線多頭區間結束後，下一個新的多頭區間可重新開始監控並等待完整進場訊號。",
            "- 買進與賣出各預留 0.2% 價差，另扣手續費與賣出交易稅。",
            "- 『實際已發生費稅與價差』只計已執行交易；『截止估值預留賣出費稅與價差』是尚未執行、只用於截止日清算估值的賣出成本預留。",
            "- 0050 一拆四只調整股數與技術座標，不當成買賣或損益。",
            f"- 截止日仍持有的部位，以 {period_end} 原始收盤價扣除假設賣出成本估算。",
            "",
            "## 附件",
            "",
            "- [風險與大盤比較](benchmark-risk-report.md)",
            "- [完整逐筆交易](trade-details.md)",
            "- [每日總資產與回落](daily-equity.csv)",
            "- [逐年總表](calendar-year-summary.csv)",
            "- [全期間總表](scenario-summary.csv)",
            "- [全部買賣動作](transactions.csv)",
            "- [每年及五年的進場機會稽核](signal-opportunity-audit.csv)",
            "- [股利與分割](corporate-actions.csv)",
            "- [大盤報酬指數](benchmark.csv)",
            "",
        ]
    )
    risk_rows = [row for row in annual_timing if int(row["year"]) in {2022, 2025, 2026}]
    risk = "\n".join(
        [
            "# 0050｜風險與大盤比較",
            "",
            "![0050策略、全程持有與大盤的每日資產回落](drawdown-curves.svg)",
            "",
            "## 大盤基準",
            "",
            f"大盤使用臺灣證券交易所發行量加權股價報酬指數，包含股利。50萬元從 {period_start} 到 {period_end} 等值成長為 {money(market['end_value'])} 元；累積報酬 {pct(market['cumulative_return'])}，換算年報酬 {pct(market['annualized_return'])}，最大資產回落 {pct(market['max_drawdown'])}。",
            "",
            "## 判讀方式",
            "",
            "- 最大資產回落：資產曾經從高點跌到低點多少，絕對值越小越好。",
            "- 年報酬／回落：每承受一單位最大回落換到多少年報酬，越高越好。",
            "- 平均現金閒置：每天現金占總資產的平均比例，太高代表資金利用不足。",
            "",
            "## 2022防守與2025～2026多頭依賴",
            "",
            md_table(
                ("方法", "年度", "帳戶報酬", "帳戶最大回落", "大盤報酬", "大盤最大回落", "平均現金閒置"),
                (
                    (
                        row["strategy_label"],
                        f"{row['year']}{'（至9/30）' if int(row['year']) == 2026 else ''}",
                        pct(row["year_return"]),
                        pct(row["year_max_drawdown"]),
                        pct(row["benchmark_return"]),
                        pct(row["benchmark_max_drawdown"]),
                        pct(row["average_cash_ratio"]),
                    )
                    for row in risk_rows
                ),
            ),
            "",
            "2026不是完整年度；判斷策略是否只受惠於多頭，應同時查看2022是否少跌、2023～2024是否仍有累積，以及後兩年獲利占比。",
            "",
        ]
    )
    trade_lines = [
        "# 0050｜完整逐筆交易",
        "",
        "已賣出交易使用費稅後淨利與已實現獲利率；截至截止日仍持有的交易使用扣除假設賣出成本後的估算損益。逐筆獲利率一律以該列實際買入總額為分母（逐筆損益 ÷ 實際買入總額），不以帳戶初始本金為分母。",
        "",
    ]
    for summary in summaries:
        strategy_id = str(summary["strategy_id"])
        rows = [row for row in details if row["strategy_id"] == strategy_id]
        trade_lines.extend([f"## {summary['strategy_label']}", ""])
        if not rows:
            explanation = no_trade_explanations([summary], signal_audit)
            trade_lines.extend(
                [
                    explanation[0][2:] if explanation else "研究期間沒有進場。",
                    "",
                ]
            )
            continue
        trade_lines.extend(
            [
                md_table(
                    ("狀態", "買進日期", "投入金額", "賣出／估值日期", "賣出原因", "費稅後損益", "獲利率"),
                    (
                        (
                            row["status_zh"],
                            row["entry_date"],
                            money(row["buy_total"]),
                            row.get("exit_date") or str(period_end),
                            EXIT_REASON_ZH.get(row.get("exit_reason", ""), row.get("exit_reason", "") or "截至截止日仍持有"),
                            money(row["net_pnl_or_estimate"]),
                            pct(row["return_rate_or_estimate"]),
                        )
                        for row in rows
                    ),
                ),
                "",
            ]
        )
    index = "\n".join(
        [
            "# 0050正式回測報告",
            "",
            "本版依《雙均線回測固定報告格式 v2》產出；50萬元只在起始日投入一次，年度統計不重設資金或策略狀態。",
            "",
            "1. [50萬元連續帳戶正式報告](shared-account-report.md)",
            "2. [風險與大盤比較](benchmark-risk-report.md)",
            "3. [完整逐筆交易](trade-details.md)",
            "",
        ]
    )
    return index, main, risk, "\n".join(trade_lines)


def artifact_rows(root: Path, excluded: set[str]) -> list[dict[str, Any]]:
    return [
        {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in excluded
    ]


def build(
    *,
    source_root: Path = SOURCE_ROOT,
    benchmark_root: Path = BENCHMARK_ROOT,
    output_root: Path = OUTPUT_ROOT_DEFAULT,
    years: Sequence[int] | None = None,
    period_start: date = date(2022, 1, 3),
    period_end: date = date(2026, 9, 30),
    source_qa_filename: str = "QA_ACCEPTED.json",
    final_status: str = "STOP_WAIT_INDEPENDENT_QA",
) -> dict[str, Any]:
    global YEARS
    if years is not None:
        YEARS = tuple(int(year) for year in years)
    if output_root.exists():
        raise StandardReportError(f"output already exists: {output_root}")
    source_check = verify_source(source_root, source_qa_filename)
    details_root = source_root / "details"
    metrics = read_csv(details_root / "continuous_500k_metrics.csv")
    details = read_csv(details_root / "continuous_500k_trade_details.csv")
    transactions = read_csv(details_root / "continuous_500k_transactions.csv")
    curves = read_csv(details_root / "continuous_500k_daily_equity.csv")
    actions = read_csv(details_root / "continuous_500k_corporate_actions.csv")
    signal_audit_path = details_root / "continuous_500k_signal_audit.csv"
    signal_audit = read_csv(signal_audit_path) if signal_audit_path.is_file() else []
    benchmark = read_csv(benchmark_root / "benchmark.csv")
    labels = {row["strategy_id"]: row["strategy_label"] for row in metrics}
    if set(labels) != set(METHOD_ORDER):
        raise StandardReportError(f"unexpected method set: {sorted(labels)}")
    annual = annual_summaries(curves, transactions, details, actions, benchmark, labels)
    summaries = overall_summaries(metrics, annual)
    daily = comparison_daily(curves, benchmark, labels)
    market_curve = [(date.fromisoformat(row["date"]), dec(row["equivalent_500k"])) for row in benchmark]
    market = curve_metrics(market_curve, INITIAL_CAPITAL)
    benchmark_annual = benchmark_years(benchmark)
    market_profitable = [year for year in YEARS if dec(benchmark_annual[year]["benchmark_return"]) > 0]
    market["profitable_years"] = f"{len(market_profitable)}／{len(YEARS)}（{'、'.join(map(str, market_profitable))}）"
    index, main, risk, trade_report = render_reports(
        summaries, annual, details, market, signal_audit, period_start, period_end
    )
    repo = Path(__file__).resolve().parents[2]
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, encoding="utf-8"
    ).strip()
    with tempfile.TemporaryDirectory(prefix="0050-standard-v3-") as temp_name:
        stage = Path(temp_name)
        (stage / "report.md").write_text(index, encoding="utf-8", newline="\n")
        (stage / "shared-account-report.md").write_text(main, encoding="utf-8", newline="\n")
        (stage / "benchmark-risk-report.md").write_text(risk, encoding="utf-8", newline="\n")
        (stage / "trade-details.md").write_text(trade_report, encoding="utf-8", newline="\n")
        write_csv(stage / "scenario-summary.csv", summaries)
        write_csv(stage / "calendar-year-summary.csv", annual)
        write_csv(stage / "daily-equity.csv", daily)
        write_csv(stage / "trade-details.csv", details)
        write_csv(stage / "transactions.csv", transactions)
        write_csv(stage / "corporate-actions.csv", actions)
        write_csv(stage / "signal-opportunity-audit.csv", signal_audit)
        write_csv(stage / "benchmark.csv", benchmark)
        svg_chart(stage / "equity-curves.svg", daily, value_field="equity", title="0050策略、全程持有與大盤：每日總資產")
        svg_chart(stage / "drawdown-curves.svg", daily, value_field="drawdown", title="0050策略、全程持有與大盤：每日資產回落")
        write_json(
            stage / "input-manifest.json",
            {
                "status": "PASS",
                "report_template": "docs/strategies/dual_ma/standard-backtest-report-v2.md",
                "runner_git_commit": git_commit,
                "source_root": source_root,
                "source_check": source_check,
                "benchmark_path": benchmark_root / "benchmark.csv",
                "benchmark_sha256": sha256(benchmark_root / "benchmark.csv"),
                "period": {"start": period_start, "cutoff": period_end},
                "initial_capital": INITIAL_CAPITAL,
                "annual_reset": False,
            },
        )
        excluded = {"artifact-manifest.json", "readback.json", "checkpoint.json"}
        artifacts = artifact_rows(stage, excluded)
        write_json(
            stage / "artifact-manifest.json",
            {
                "status": final_status,
                "artifact_count": len(artifacts),
                "annual_rows": len(annual),
                "daily_rows": len(daily),
                "trade_rows": len(details),
                "artifacts": artifacts,
            },
        )
        mismatches = [item["path"] for item in artifacts if sha256(stage / item["path"]) != item["sha256"]]
        write_json(
            stage / "readback.json",
            {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches},
        )
        if mismatches:
            raise StandardReportError(f"artifact readback failed: {mismatches}")
        write_json(
            stage / "checkpoint.json",
            {
                "status": final_status,
                "artifact_manifest_sha256": sha256(stage / "artifact-manifest.json"),
                "readback_sha256": sha256(stage / "readback.json"),
                "safe_next_step": "independent annual, trade, benchmark and chart QA",
            },
        )
        output_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output_root)
    return {
        "status": final_status,
        "output_root": output_root,
        "annual_rows": len(annual),
        "daily_rows": len(daily),
        "trade_rows": len(details),
        "artifact_manifest_sha256": sha256(output_root / "artifact-manifest.json"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--benchmark-root", type=Path, default=BENCHMARK_ROOT)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT_DEFAULT)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            jsonable(
                build(
                    source_root=args.source_root,
                    benchmark_root=args.benchmark_root,
                    output_root=args.output_root,
                )
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
