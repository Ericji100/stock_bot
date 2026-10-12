"""Manifest-bound formal adapter/orchestrator for 00635U.

The module performs no network access.  Its caller must first pass the 00 SHA
gate in ``etf_00635u_prep_v1``.  Synthetic tests exercise the same surfaces.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import etf_00635u_research_v1 as research
from .etf_00635u_strategy_v1 import (
    ETFResearchConfig,
    ExecutionTiming,
    IndicatorRow,
    RawETFRow,
    StrategyVersion,
    build_causal_indicator_rows,
    run_strategy,
)


REPORT_NAME = "README.md"
TRADE_REPORT_NAME = "trade-details.md"
STOP_CODE = "STOP_WAIT_00_00635U_PREP_QA_R4"

LINE_LABELS = {
    "STRATEGY_A_SIGNAL_CLOSE": "策略 A（訊號日收盤成交）",
    "STRATEGY_A_NEXT_SESSION_OPEN": "策略 A（次一交易日開盤成交）",
    "STRATEGY_B_SIGNAL_CLOSE": "策略 B（訊號日收盤成交）",
    "STRATEGY_B_NEXT_SESSION_OPEN": "策略 B（次一交易日開盤成交）",
    "00635U_BUY_HOLD_CASH": "00635U 買進持有（現金保留）",
    "00635U_BUY_HOLD_REINVEST": "00635U 買進持有（分配再投入）",
    "GOLD_ER_OFFICIAL_TRACKED_INDEX": "官方追蹤 Gold ER 指數",
    "TAIEX_TOTAL_RETURN_SUPPLEMENT": "臺灣加權股價報酬指數（補充）",
}


def _line_label(line_id: str, *, include_id: bool = False) -> str:
    label = LINE_LABELS.get(line_id, line_id)
    return f"{label}（`{line_id}`）" if include_id else label


def _strategy_label(value: object) -> str:
    raw = getattr(value, "name", value)
    if raw in {"A", StrategyVersion.A.value}:
        return "策略 A"
    if raw in {"B", StrategyVersion.B.value}:
        return "策略 B"
    return f"策略 {raw}"


def _money(value: object, *, signed: bool = False) -> str:
    spec = "+,.2f" if signed else ",.2f"
    return f"{format(research.decimal(value), spec)} 元"


def _percent(value: object, *, signed: bool = False) -> str:
    spec = "+.2f" if signed else ".2f"
    return f"{format(research.decimal(value) * 100, spec)}%"


def _percentage_points(value: object) -> str:
    return f"{format(research.decimal(value) * 100, '+.2f')} 百分點"


def _trade_report(trades: Sequence[Mapping[str, Any]], *, synthetic: bool) -> str:
    banner = (
        "> **SYNTHETIC TEST ONLY／合成測試資料，不是正式實績。**"
        if synthetic else
        "> **FORMAL／正式資料閘門輸出。** 請搭配主報告與 manifest 驗證。"
    )
    rows = []
    for row in trades:
        entry_or_exit = "進場" if row.get("role") == "ENTRY" else "出場"
        source_or_reason = row.get("entry_type") or row.get("exit_reason") or "—"
        rows.append(
            f"| {_line_label(str(row['line_id']))} | {entry_or_exit} | "
            f"{row.get('signal_date', '—')} | {row.get('fill_date', '—')} | "
            f"{'訊號日收盤' if row.get('timing') == ExecutionTiming.SIGNAL_CLOSE.value else '次一交易日開盤'} | "
            f"{source_or_reason} | `{row.get('position_id', '—')}` | {int(row.get('shares', 0)):,} | "
            f"{_money(row.get('reference_price', 0))} | {_money(row.get('fill_price', 0))} | "
            f"{_money(row.get('commission', 0))} | {_money(row.get('tax', 0))} | "
            f"{_money(row['initial_stop']) if row.get('initial_stop') is not None else '—'} | "
            f"{_money(row['initial_r']) if row.get('initial_r') is not None else '—'} | "
            f"{row.get('kd_2r_arm_date') or '—'} | "
            f"{_money(row['net_pnl'], signed=True) if row.get('net_pnl') is not None else '—'} |"
        )
    body = "\n".join(rows) if rows else "| — | — | — | — | — | — | — | 0 | — | — | — | — | — | — | — | — |"
    return (
        "# 00635U 因果回測｜逐筆交易明細\n\n" + banner + "\n\n"
        "金額均為新臺幣；原始精確值保留於 [trade-details.csv](trade-details.csv)。\n\n"
        "| 策略 | 角色 | 訊號日 | 成交日 | 成交時點 | 進場來源／出場理由 | 部位 ID | 股數 | 參考價 | 成交價 | 手續費 | 稅 | 初始防守 | 初始 R | 2R 啟動日 | 淨損益 |\n"
        "|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|\n"
        + body + "\n"
    )


@dataclass(frozen=True)
class FormalSnapshotData:
    raw_rows: tuple[RawETFRow, ...]
    indicator_rows: tuple[IndicatorRow, ...]
    gold_er: tuple[research.IndexObservation, ...]
    taiex_total_return: tuple[research.IndexObservation, ...] | None = None


def _bool(value: Any, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_formal_snapshot(manifest_path: Path) -> FormalSnapshotData:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_role = {item.get("role"): item for item in manifest.get("files", [])}
    required = {"etf_daily_causal", "gold_er_official"}
    missing = sorted(required - set(by_role))
    if missing:
        raise ValueError(f"formal manifest missing roles: {missing}")
    root = manifest_path.parent
    raw_rows = []
    for item in _read_csv(root / by_role["etf_daily_causal"]["path"]):
        if Decimal(item.get("cash_dividend_per_share") or "0") != 0 and not item.get("dividend_stage"):
            raise ValueError("FORMAL rejects legacy unstaged cash dividend rows")
        raw_rows.append(RawETFRow(
            day=date.fromisoformat(item["date"]),
            open=None if item.get("open") in (None, "") else Decimal(item["open"]),
            high=Decimal(item["high"]), low=Decimal(item["low"]), close=Decimal(item["close"]),
            volume=Decimal(item.get("volume") or "0"),
            official_session=_bool(item.get("official_session"), True),
            tradable=_bool(item.get("tradable"), True),
            tradability_reason=item.get("tradability_reason", ""),
            price_rebase_factor=Decimal(item.get("price_rebase_factor") or "1"),
            share_factor=Decimal(item.get("share_factor") or "1"),
            cash_dividend_per_share=Decimal(item.get("cash_dividend_per_share") or "0"),
            dividend_event_id=item.get("dividend_event_id", ""),
            dividend_stage=item.get("dividend_stage", ""),
        ))
    gold = tuple(
        research.IndexObservation(
            source_date=date.fromisoformat(item["source_date"]),
            available_date=date.fromisoformat(item["available_date"]),
            value=Decimal(item["value"]),
            source_timezone=item["source_timezone"],
        )
        for item in _read_csv(root / by_role["gold_er_official"]["path"])
    )
    taiex = None
    if "taiex_total_return" in by_role:
        taiex = tuple(
            research.IndexObservation(
                source_date=date.fromisoformat(item["source_date"]),
                available_date=date.fromisoformat(item["available_date"]),
                value=Decimal(item["value"]),
                source_timezone=item["source_timezone"],
            )
            for item in _read_csv(root / by_role["taiex_total_return"]["path"])
        )
    return FormalSnapshotData(
        raw_rows=tuple(raw_rows),
        indicator_rows=tuple(build_causal_indicator_rows(raw_rows)),
        gold_er=gold,
        taiex_total_return=taiex,
    )


def run_formal_analysis(data: FormalSnapshotData) -> dict[str, Any]:
    rows = data.indicator_rows
    lines = research.run_seven_lines(
        rows,
        data.gold_er,
        gold_alignment_policy="LAST_KNOWN_PUBLISHED",
    )
    line_metrics = {}
    for line in lines:
        metrics = research.performance_metrics(line.curve, line.audit.get("trades", ()))
        if line.role == "TRADABLE_PASSIVE_COMPARATOR":
            transactions = line.audit["transactions"]
            metrics.update({
                "closed_positions": 0,
                "open_positions": 1,
                "maximum_single_entry_cash": max(
                    (research.decimal(item["cash_amount"]) for item in transactions),
                    default=Decimal("0"),
                ),
            })
        line_metrics[line.line_id] = metrics
    active: dict[str, Any] = {}
    line_by_id = {line.line_id: line for line in lines}
    for strategy in StrategyVersion:
        for timing in ExecutionTiming:
            result = run_strategy(rows, ETFResearchConfig(strategy=strategy, timing=timing))
            line_id = f"STRATEGY_{strategy.name}_{timing.value}"
            curve = tuple((item["date"], item["equity"]) for item in result.state.daily)
            metrics = research.performance_metrics(curve, result.state.trades)
            active[line_id] = {
                "result": result,
                "metrics": research.performance_metrics(curve, result.state.trades),
                "annual": research.annual_slices(curve),
                "rolling": {years: research.rolling_windows(curve, years) for years in (3, 5, 10)},
                "regime": research.regime_summary(result.state.daily),
                "stability": research.trade_stability(result.state.trades),
                "path": research.path_diagnostics(curve),
            }
            comparison_line = line_by_id[line_id]
            start = comparison_line.audit["comparison_start"]
            end = comparison_line.audit["comparison_end"]
            comparison_trades = [
                item for item in result.state.trades
                if start <= item["fill_date"] <= end
            ]
            active[line_id]["metrics"] = research.performance_metrics(
                comparison_line.curve, comparison_trades
            )
            active[line_id]["comparison_trades"] = comparison_trades
            line_metrics[line_id] = active[line_id]["metrics"]
    benchmark_ids = (
        "00635U_BUY_HOLD_CASH",
        "00635U_BUY_HOLD_REINVEST",
        "GOLD_ER_OFFICIAL_TRACKED_INDEX",
    )
    relative = {
        line_id: {
            benchmark_id: research.relative_to_benchmark(
                payload["metrics"], line_metrics[benchmark_id]
            )
            for benchmark_id in benchmark_ids
        }
        for line_id, payload in active.items()
    }
    taiex_line = None
    taiex_status = "HOLD_MISSING_TAIEX_TOTAL_RETURN_ROLE"
    if data.taiex_total_return is not None:
        taiex_line = research.run_index_line(
            data.taiex_total_return,
            [row.day for row in rows],
            line_id="TAIEX_TOTAL_RETURN_SUPPLEMENT",
            alignment_policy="LAST_KNOWN_PUBLISHED",
        )
        taiex_status = "AVAILABLE_SUPPLEMENT_ONLY"
    return {
        "lines": lines,
        "line_metrics": line_metrics,
        "active": active,
        "relative_benchmarks": relative,
        "cost_sensitivity": research.cost_sensitivity(rows),
        "taiex_supplement": taiex_line,
        "taiex_status": taiex_status,
    }


def run_formal_pack(
    manifest_path: Path,
    output_root: Path,
    *,
    accepted_manifest_sha256: str,
) -> dict[str, Any]:
    from . import etf_00635u_prep_v1 as prep

    input_manifest = prep.validate_formal_snapshot_gate(
        manifest_path,
        accepted_manifest_sha256=accepted_manifest_sha256,
        accepted_by="00",
    )
    input_classification = input_manifest.get(
        "data_classification", "FORMAL_ACCEPTED_SNAPSHOT"
    )
    synthetic_test = input_classification == "SYNTHETIC_TEST_ONLY"
    data = load_formal_snapshot(manifest_path)
    analysis = run_formal_analysis(data)
    output_root.mkdir(parents=True, exist_ok=True)
    summary = [
        {"line_id": line_id, **metrics}
        for line_id, metrics in analysis["line_metrics"].items()
    ]
    prep._write_csv(output_root / "formal-summary.csv", summary)
    daily = [
        {"line_id": line.line_id, "date": day, "equity": equity}
        for line in analysis["lines"] for day, equity in line.curve
    ]
    prep._write_csv(output_root / "daily-equity.csv", daily)
    trades, events, annual, rolling = [], [], [], []
    for line_id, payload in analysis["active"].items():
        trades.extend({"line_id": line_id, **row} for row in payload["result"].state.trades)
        events.extend({"line_id": line_id, **row} for row in payload["result"].state.events)
        annual.extend({"line_id": line_id, **row} for row in payload["annual"])
        for years, records in payload["rolling"].items():
            rolling.extend({"line_id": line_id, **row} for row in records)
    prep._write_csv(output_root / "trade-details.csv", trades or [{"status": "NO_TRADES"}])
    (output_root / TRADE_REPORT_NAME).write_text(
        _trade_report(trades, synthetic=synthetic_test), encoding="utf-8"
    )
    prep._write_csv(output_root / "event-ledger.csv", events or [{"status": "NO_EVENTS"}])
    prep._write_csv(output_root / "annual-performance.csv", annual)
    prep._write_csv(output_root / "rolling-windows.csv", rolling)
    prep._write_csv(output_root / "cost-sensitivity.csv", analysis["cost_sensitivity"])
    index_line = next(line for line in analysis["lines"] if line.line_id == "GOLD_ER_OFFICIAL_TRACKED_INDEX")
    prep._write_csv(output_root / "index-alignment-ledger.csv", list(index_line.audit["alignment_ledger"]))
    relative_rows = [
        {"line_id": line_id, "benchmark_id": benchmark_id, **values}
        for line_id, benchmarks in analysis["relative_benchmarks"].items()
        for benchmark_id, values in benchmarks.items()
    ]
    prep._write_csv(output_root / "relative-benchmark.csv", relative_rows)
    taiex_rows = [{"status": analysis["taiex_status"]}]
    if analysis["taiex_supplement"] is not None:
        taiex_rows = [
            {"status": analysis["taiex_status"], "date": day, "equity": value}
            for day, value in analysis["taiex_supplement"].curve
        ]
    prep._write_csv(output_root / "taiex-supplement.csv", taiex_rows)
    diagnostic_rows = []
    for line_id, payload in analysis["active"].items():
        line = next(item for item in analysis["lines"] if item.line_id == line_id)
        counterfactuals = {
            count: research.trade_removal_cash_counterfactual(
                line.curve, payload["comparison_trades"], count
            )
            for count in (1, 3, 5)
        }
        diagnostic_rows.append({
            "line_id": line_id, **payload["stability"], **payload["path"],
            **{
                f"remove_best_{count}_{key}": value
                for count, result in counterfactuals.items()
                for key, value in result.items()
            },
        })
    prep._write_csv(output_root / "path-and-concentration.csv", diagnostic_rows)
    fixed_periods = (
        ("2015_2019", date(2015, 4, 15), date(2019, 12, 31)),
        ("2020_2022", date(2020, 1, 1), date(2022, 12, 31)),
        ("2023_LATEST", date(2023, 1, 1), data.indicator_rows[-1].day),
    )
    fixed_rows = []
    for line in analysis["lines"]:
        for period_id, start, end in fixed_periods:
            fixed_rows.append({"line_id": line.line_id, "period_id": period_id, **research.slice_curve(line.curve, start, end)})
    prep._write_csv(output_root / "fixed-periods.csv", fixed_rows)
    grid_rows = []
    for strategy in StrategyVersion:
        for parameters in research.parameter_grid():
            variant = build_causal_indicator_rows(
                data.raw_rows,
                ma_fast=parameters[0], ma_slow=parameters[1],
                macd_fast=parameters[2], macd_slow=parameters[3], macd_signal=parameters[4],
            )
            result = run_strategy(
                variant,
                ETFResearchConfig(strategy=strategy, timing=ExecutionTiming.NEXT_SESSION_OPEN),
            )
            curve = tuple((item["date"], item["equity"]) for item in result.state.daily)
            metrics = research.performance_metrics(curve, result.state.trades)
            rolling_5 = research.rolling_windows(curve, 5)
            valid_5 = [item["cagr"] for item in rolling_5 if item.get("status") == "OK"]
            grid_rows.append({
                "strategy": strategy.name,
                "ma_fast": parameters[0], "ma_slow": parameters[1],
                "macd_fast": parameters[2], "macd_slow": parameters[3], "macd_signal": parameters[4],
                "kd": "9/9/55", "two_r": "2R",
                "ending_equity": result.final_equity,
                "total_return": metrics["total_return"],
                "cagr": metrics["cagr"],
                "max_drawdown": metrics["max_drawdown"],
                "return_drawdown_ratio": (
                    None
                    if metrics["max_drawdown"] == 0
                    else metrics["cagr"] / abs(metrics["max_drawdown"])
                ),
                "rolling_5y_worst_cagr": min(valid_5) if valid_5 else None,
            })
    prep._write_csv(output_root / "parameter-grid.csv", grid_rows)
    grid_summary_rows = [
        {"strategy": strategy.name, **research.parameter_grid_summary([
            row for row in grid_rows if row["strategy"] == strategy.name
        ])}
        for strategy in StrategyVersion
    ]
    prep._write_csv(output_root / "parameter-grid-summary.csv", grid_summary_rows)
    core_rows, core_summary, core_ledger = [], [], []
    for strategy in StrategyVersion:
        allocation = research.run_core_allocation_study(
            data.indicator_rows,
            strategy=strategy,
            timing=ExecutionTiming.NEXT_SESSION_OPEN,
        )
        core_rows.extend({"strategy": strategy.name, **row} for row in allocation["daily"])
        core_summary.extend({"strategy": strategy.name, **row} for row in allocation["summary"])
        core_ledger.extend({"strategy": strategy.name, **row} for row in allocation["ledger"])
    prep._write_csv(output_root / "core-allocation-daily.csv", core_rows)
    prep._write_csv(output_root / "core-allocation-summary.csv", core_summary)
    prep._write_csv(output_root / "core-allocation-ledger.csv", core_ledger or [{"status": "NO_TRANSACTIONS"}])
    equity_series = {
        _line_label(line.line_id): [value for _day, value in line.curve]
        for line in analysis["lines"][:4]
    }
    drawdown_series = {}
    for line in analysis["lines"][:4]:
        peak, values = Decimal("0"), []
        for _day, value in line.curve:
            peak = max(peak, value)
            values.append(value / peak - 1 if peak else Decimal("0"))
        drawdown_series[_line_label(line.line_id)] = values
    annual_series = {
        _line_label(line_id): [item["return"] for item in payload["annual"]]
        for line_id, payload in analysis["active"].items()
    }
    rolling_series = {
        _line_label(line_id): [item["cagr"] for item in payload["rolling"][5] if item.get("status") == "OK"]
        for line_id, payload in analysis["active"].items()
    }
    regime_series = {
        _line_label(line_id): [item["compounded_return"] for item in payload["regime"]]
        for line_id, payload in analysis["active"].items()
    }
    trade_series = {
        _line_label(line_id): [item["net_pnl"] for item in payload["comparison_trades"] if item["role"] == "EXIT"]
        for line_id, payload in analysis["active"].items()
    }
    cost_series = {
        _strategy_label(strategy): [
            item["ending_equity"] for item in analysis["cost_sensitivity"]
            if item["strategy"] == strategy.value and item["timing"] == ExecutionTiming.NEXT_SESSION_OPEN.value
        ] for strategy in StrategyVersion
    }
    grid_series = {
        _strategy_label(strategy): [item["ending_equity"] for item in grid_rows if item["strategy"] == strategy.name]
        for strategy in StrategyVersion
    }
    core_series = {
        _strategy_label(strategy): [item["ending_equity"] for item in core_summary if item["strategy"] == strategy.name]
        for strategy in StrategyVersion
    }
    chart_specs = {
        "equity-chart.svg": ("equity_curve", "正式資產曲線", equity_series, "交易序列", "資產（元）"),
        "drawdown-chart.svg": ("drawdown", "正式回撤路徑", drawdown_series, "交易序列", "回撤率"),
        "annual-chart.svg": ("annual_return", "正式逐年報酬", annual_series, "年度", "報酬率"),
        "rolling-window-chart.svg": ("rolling_window", "五年滾動年化報酬", rolling_series, "月末窗口", "年化報酬率"),
        "regime-chart.svg": ("regime", "市場狀態績效", regime_series, "高／低價格區間", "複合報酬"),
        "trade-pnl-chart.svg": ("trade_pnl", "逐筆交易淨損益", trade_series, "交易序號", "淨損益（元）"),
        "cost-pressure-chart.svg": ("cost_pressure", "成本壓力測試", cost_series, "滑價（20／40／60 個基點）", "期末資產（元）"),
        "core-allocation-chart.svg": ("core_allocation", "核心持有比例", core_series, "核心比例", "期末資產（元）"),
        "parameter-grid-chart.svg": ("parameter_grid", "243 格參數分布", grid_series, "參數格", "期末資產（元）"),
    }
    for name, (kind, title, source, x_label, y_label) in chart_specs.items():
        (output_root / name).write_text(
            prep._chart_svg(source, title=title, kind=kind, x_label=x_label, y_label=y_label),
            encoding="utf-8",
        )
    line_lookup = {line.line_id: line for line in analysis["lines"]}
    table = "\n".join(
        f"| {_line_label(row['line_id'], include_id=True)} | "
        f"{'次一交易日開盤' if 'NEXT_SESSION_OPEN' in row['line_id'] else '訊號日收盤' if 'SIGNAL_CLOSE' in row['line_id'] else '比較基準'} | "
        f"{line_lookup[row['line_id']].audit['comparison_start']}～{line_lookup[row['line_id']].audit['comparison_end']}（{int(line_lookup[row['line_id']].audit['comparison_observations']):,} 筆） | "
        f"{_money(row['ending_equity'])} | {_percent(row['total_return'])} | {_percent(row['cagr'])} | {_percent(row['max_drawdown'])} | "
        f"{int(row['profitable_years'])}／{int(row['losing_years'])}／{int(row['flat_years'])} 年 | "
        f"{int(row['closed_positions'] + row['open_positions'])}／{int(row['closed_positions'])}／{int(row['open_positions'])} 次 | {_money(row['maximum_single_entry_cash'])} |"
        for row in summary
    )
    section_titles = (
        ("conclusion", "01｜一頁白話結論"), ("performance", "02｜完整績效總表"),
        ("relative", "03｜相對基準"), ("rules", "04｜策略規則與成交版本"),
        ("annual", "05｜完整逐年績效"), ("trades", "06｜交易紀錄與資金投入"),
        ("execution", "07｜成交時間與交易成本"), ("periods", "08｜時期、滾動與狀態"),
        ("concentration", "09｜交易集中度與單筆穩定性"), ("grid", "10｜243 組參數穩健性"),
        ("core", "11｜核心持有比例研究"), ("practice", "12｜實務建議與監控"),
        ("qa", "13｜技術驗收與隔離重播"), ("attachments", "14｜完整附件目錄"),
    )
    attachments = (
        "formal-summary.csv", "relative-benchmark.csv", "daily-equity.csv",
        TRADE_REPORT_NAME, "trade-details.csv",
        "event-ledger.csv", "annual-performance.csv", "fixed-periods.csv", "rolling-windows.csv",
        "path-and-concentration.csv", "cost-sensitivity.csv", "parameter-grid.csv",
        "parameter-grid-summary.csv", "core-allocation-summary.csv", "core-allocation-daily.csv",
        "core-allocation-ledger.csv", "index-alignment-ledger.csv", "taiex-supplement.csv",
        "equity-chart.svg", "drawdown-chart.svg", "annual-chart.svg", "rolling-window-chart.svg",
        "regime-chart.svg", "trade-pnl-chart.svg", "cost-pressure-chart.svg",
        "core-allocation-chart.svg", "parameter-grid-chart.svg",
        "manifest.json", "readback.json",
    )
    next_open_ids = [key for key in analysis["active"] if "NEXT_SESSION_OPEN" in key]
    best_next = max(next_open_ids, key=lambda key: analysis["active"][key]["metrics"]["ending_equity"])
    worst_annual_line, worst_annual = min(
        ((key, item) for key in next_open_ids for item in analysis["active"][key]["annual"]),
        key=lambda pair: pair[1]["return"],
    )
    worst_rolling_pair = min(
        ((key, item) for key in next_open_ids for item in analysis["active"][key]["rolling"][5] if item.get("status") == "OK"),
        key=lambda pair: pair[1]["cagr"],
        default=None,
    )
    cost20 = {item["strategy"]: item for item in analysis["cost_sensitivity"] if item["slippage_bps"] == 20 and item["timing"] == ExecutionTiming.NEXT_SESSION_OPEN.value}
    cost60 = {item["strategy"]: item for item in analysis["cost_sensitivity"] if item["slippage_bps"] == 60 and item["timing"] == ExecutionTiming.NEXT_SESSION_OPEN.value}
    practical_summary = "；".join(
        f"{_line_label(line_id)}：期末資產 {_money(analysis['line_metrics'][line_id]['ending_equity'])}、"
        f"總報酬 {_percent(analysis['line_metrics'][line_id]['total_return'])}、"
        f"年化報酬率 {_percent(analysis['line_metrics'][line_id]['cagr'])}、"
        f"最大回落 {_percent(analysis['line_metrics'][line_id]['max_drawdown'])}"
        for line_id in next_open_ids
    )
    cost_pressure = "；".join(
        f"{_strategy_label(key)}的滑價由 20 提高到 60 個基點後，期末資產變動 "
        f"{_money(cost60[key]['ending_equity'] - cost20[key]['ending_equity'], signed=True)}"
        for key in sorted(cost20)
    )
    grid_table = "\n".join(
        f"| {_strategy_label(row['strategy'])} | {_money(row['median_ending_equity'])} | "
        f"{_percent(row['positive_expectation_cell_ratio'])} | {_money(row['baseline_ending_equity'])} | "
        f"{_money(row['local_neighborhood_median'])} | "
        f"{_percent(row['rolling_5y_worst_cagr']) if row['rolling_5y_worst_cagr'] is not None else '資料不足'} |"
        for row in grid_summary_rows
    )
    core_table = "\n".join(
        f"| {_strategy_label(row['strategy'])} | {int(row['core_pct'])}% | "
        f"{_money(row['ending_equity'])} | {_percent(row['cagr'])} | {_percent(row['max_drawdown'])} |"
        for row in core_summary
    )
    relative_table = "\n".join(
        f"| {_line_label(row['line_id'])} | {_line_label(row['benchmark_id'])} | "
        f"{_money(row['ending_equity_difference'], signed=True)} | "
        f"{_percentage_points(row['cagr_difference'])} | {_percentage_points(row['max_drawdown_difference'])} |"
        for row in relative_rows
    )
    common_line = analysis["lines"][0]
    common_period = (
        f"{common_line.audit['comparison_start']} 至 {common_line.audit['comparison_end']}，"
        f"共 {int(common_line.audit['comparison_observations']):,} 筆共同觀測"
    )
    worst_rolling_text = (
        "資料不足"
        if worst_rolling_pair is None else
        f"{_line_label(worst_rolling_pair[0])}為 {_percent(worst_rolling_pair[1]['cagr'])}"
    )
    taiex_text = (
        "資料角色缺少，補充比較維持暫緩"
        if analysis["taiex_status"] == "HOLD_MISSING_TAIEX_TOTAL_RETURN_ROLE"
        else "資料可用，但只列補充附件，不納入主七線"
    )
    core_tradeoff = "；".join(
        f"{_strategy_label(strategy)}期末資產最高為核心 {int(max((row for row in core_summary if row['strategy'] == strategy.name), key=lambda row: row['ending_equity'])['core_pct'])}%；"
        f"回落最淺為核心 {int(max((row for row in core_summary if row['strategy'] == strategy.name), key=lambda row: row['max_drawdown'])['core_pct'])}%"
        for strategy in StrategyVersion
    )
    prose = {
        "conclusion": ("先用實務成交版本回答候選策略在完整成本後的表現。", "比較策略 A、策略 B 的次一交易日開盤版本，再檢查最差年度、最差五年窗口與成本壓力。", practical_summary + f"；期末資產較高者為 {_line_label(best_next)}。最差年度為 {_line_label(worst_annual_line)} {_percent(worst_annual['return'])}；最差五年窗口：{worst_rolling_text}。{cost_pressure}。", "不能只因期末資產較高就稱正期望；最差窗口、集中度與參數分布必須同時支持。", "若任何壓力測試或可比期間失敗，結論降為暫緩。"),
        "performance": ("用同一共同期間比較主七線。", "每線重訂基準至 500,000.00 元並列實際起訖日與觀測數；Gold ER 只作不可交易基準。", f"主七線的共同期間為 {common_period}；四條主動策略的交易數與最大投入直接來自逐筆帳本。", "年化報酬率與最大回落必須一起看，不能只看期末資產。", "主表是後續相對、期間與壓力分析的唯一母表。"),
        "relative": ("逐策略比較三個指定基準。", "分別計算相對現金持有、分配再投入與官方 Gold ER 的期末資產差、年化報酬率差與最大回落差。", f"四條策略各比較三個基準，共 {len(relative_rows):,} 組；臺灣加權股價報酬指數目前為：{taiex_text}。", "Gold ER 差異不是可交易超額報酬，因指數沒有 ETF 費稅、轉倉追蹤與匯率摩擦。", "只有三個基準方向一致時，才提高正期望信心。"),
        "rules": ("確認策略 A、策略 B 與兩種成交版本沒有混用。", "逐項核對共同進場、regime、固定防守、A/B 出場差異、停損優先順序與成交時點。", "共同規則與唯一差異完整列於本章；所有進場、2R 啟動、現金分配、公司行動與出場都有事件 ID 與部位 ID。", "達到 2R 只啟動策略 B 的後續判斷，不會立即賣出，也不會回溯舊死叉。", "規則改動必須另開版本並重跑全部附件。"),
        "annual": ("檢查獲利是否集中少數年份。", "連續帳戶以前一年度末為起點，首年用 500,000.00 元，完整納入首交易日。", f"實務成交線的最差年度出現在 {_line_label(worst_annual_line)}，年度報酬為 {_percent(worst_annual['return'])}。", "年度不重設本金；獲利年度次數不能取代最差年度幅度。", "若少數年度貢獻過大，實務採用需降權。"),
        "trades": ("讓讀者直接核對每筆投入與出場。", "交易表含訊號日、成交日、成交方式、部位／事件 ID、股數、價格、費稅、初始 R、2R 啟動日與淨損益。", f"本次輸出 {len(trades):,} 筆進出場紀錄；可先閱讀中文明細，再用 CSV 與事件帳本重建。", "每次投入不是固定 500,000.00 元，而受可用現金、整股與 20.00% 初始風險上限約束。", "任何主表交易數都必須能由附件一對一重建。"),
        "execution": ("量化成交時間與成本敏感度。", "同時比較訊號日收盤／次一交易日開盤及 20／40／60 個基點，輸出期末資產、年化報酬率、最大回落與完成回合。", cost_pressure + "。", "成本升高若快速侵蝕成果，策略優勢可能只是理想成交假象。", "實務主口徑固定為次一個可成交交易日的開盤。"),
        "periods": ("檢查固定時期、月末滾動窗口與市場狀態。", "固定期輸出期初／期末／總報酬／年化報酬率／最大回落；滾動 3／5／10 年採月末窗口。", f"實務成交線的五年最差年化報酬率為：{worst_rolling_text}；歷史不足的窗口會明確標示資料不足。", "全期正報酬若伴隨長期負窗口，仍不能稱穩健。", "以最差窗口與低／高價格區間的差異限制部位。"),
        "concentration": ("檢查少數交易、連虧與水下期。", "輸出最佳/最差、top1/top3 集中度、連虧、水下期；移除最佳 1/3/5 採已實現現金 PnL 扣除，不重跑 sizing。", "反事實附件明標 `REALIZED_CASH_PNL_SUBTRACTION_NO_RESIZING`，不冒充完整訊號重播。", "若扣除少數最佳交易後崩解，期望值證據偏弱。", "集中度過高時只列研究候選。"),
        "grid": ("檢查中心參數是否位於穩健區域。", "策略 A、策略 B 各 243 格輸出總報酬、年化報酬率、最大回落、報酬／回落比與五年最差，再彙總分位與鄰域。", "下表同時呈現中位數期末資產、正報酬格比例、中心格與鄰域中位數；所有金額與比例均使用人類可讀格式。", "看分布與中心鄰域，不挑單一最高格；KD 9/9/55 與 2R 不參與搜尋。", "中心若顯著弱於鄰域或正報酬格不足，結論降級。"),
        "core": ("評估核心持有 0／20／40／60%。", "核心與戰術各自保存現金、股數、清算值與資產，主方法不再平衡，並列成本與重建誤差。", core_tradeoff + "。完整風險與報酬數字列於下表。", "核心比例提高不必然同時改善報酬與回落；需用期末資產、年化報酬率和最大回落一起取捨。", "年度再平衡只能另列選項，不混入主結果。"),
        "practice": ("把研究轉成可執行限制。", "同時監控可成交 session、Gold ER stale、期貨轉倉、正逆價差、USD/TWD、時區與追蹤缺口。", "ETF 與 Gold ER 可因載具摩擦與發布時間在同日不同方向。", "這是 00635U 的結果，不是現貨黃金的無摩擦結果。", "上線前需持續監控資料與交易成本漂移。"),
        "qa": ("證明資料、報告與附件可重現。", "正式執行入口會再次核對資料清單本身的 SHA-256、資料格式與每個輸入檔案雜湊；輸出也附自己的 manifest 與 readback。", f"輸入資料清單 SHA-256 為 `{accepted_manifest_sha256}`；主七線共同期間已一致，兩個隔離目錄必須產生完全相同的位元組。", "不論從命令列或程式入口執行，只要資料、雜湊、連結或圖表語意有一項不符，就會停止而不產生可採用結果。", "00 只有在全部驗證通過後才可接受此研究包。"),
        "attachments": ("提供完整重建材料。", "逐檔列出主表、交易、事件、期間、參數、核心、對時、圖表與 receipts。", f"本次共有 {len(attachments)} 個可點入口。", "主文摘要不能取代 CSV 與 manifest。", "00 以附件雜湊與隔離 replay 為最終依據。"),
    }
    extras = {
        "performance": "\n| 主線 | 成交方式 | 實際起訖日（觀測數） | 期末資產 | 總報酬 | 年化報酬率 | 最大回落 | 獲利／虧損／平盤年度 | 進場／出場／未平倉 | 最大投入 |\n|---|---|---|---:|---:|---:|---:|---:|---:|---:|\n" + table + "\n\n![正式資產曲線](equity-chart.svg)\n",
        "relative": "\n| 策略 | 比較基準 | 期末資產差 | 年化報酬率差 | 最大回落差 |\n|---|---|---:|---:|---:|\n" + relative_table + "\n\n> 年化報酬率差與最大回落差均以百分點表示。\n",
        "rules": "\n- **共同進場：** 只用 00635U 自有歷史計算 MA21、MA144 與 MACD(21,55,55)。`MA21 >= MA144`（等號歸高）為高價格區間：前一日技術收盤不高於前一日 MA21、當日技術收盤高於當日 MA21，才是新突破進場。`MA21 < MA144` 為低價格區間：MACD 柱狀值前一日不大於零、當日大於零，才是新綠翻紅進場。\n- **固定初始防守：** 取本次 MA21 拉回階段低點、前一交易日低點與訊號日低點三者最小值，而且必須嚴格低於技術進場價。\n- **策略 A：** 高價格區間先檢查固定初始防守，再檢查已啟動的 MACD 柱狀值新紅翻綠；固定防守優先。低價格區間只維護、不執行固定防守，沿用已啟動的 MACD 新紅翻綠出場。\n- **策略 B：** 低價格區間進場部位完全沿用策略 A。只有高價格區間進場部位會從進場到離場持續執行固定防守；在完整買賣成本後的標記淨損益先達到初始風險的 2 倍（淨成本 2R）時，才永久啟動 KD(9,9,55)。啟動後只接受新發生的 K 向下穿越 D；達到 2R 本身不賣出，也不回溯啟動前的死叉。固定防守與有效死叉同日成立時，固定防守優先。\n- **成交版本：** 同時計算訊號日收盤成交與次一個可成交交易日開盤成交；後者為實務主口徑。停牌、零量或缺開盤價時，待成交指令保留至下一個可成交官方交易日。\n",
        "annual": "\n![逐年報酬](annual-chart.svg)\n\n![回撤路徑](drawdown-chart.svg)\n",
        "trades": f"\n- [繁體中文逐筆交易明細]({TRADE_REPORT_NAME})\n- [逐筆交易明細 CSV](trade-details.csv)\n- [事件帳本 CSV](event-ledger.csv)\n",
        "execution": "\n![成本壓力](cost-pressure-chart.svg)\n",
        "periods": "\n![滾動窗口](rolling-window-chart.svg)\n\n![市場狀態](regime-chart.svg)\n",
        "concentration": "\n![逐筆交易淨損益](trade-pnl-chart.svg)\n",
        "grid": "\n| 策略 | 中位數期末資產 | 正報酬格比例 | 中心格期末資產 | 鄰域中位數 | 五年最差年化 |\n|---|---:|---:|---:|---:|---:|\n" + grid_table + "\n\n![243 格參數分布](parameter-grid-chart.svg)\n",
        "core": "\n| 策略 | 核心比例 | 期末資產 | 年化報酬率 | 最大回落 |\n|---|---:|---:|---:|---:|\n" + core_table + "\n\n![核心持有比例](core-allocation-chart.svg)\n",
        "attachments": "\n" + "\n".join(f"- [{name}]({name})" for name in attachments) + "\n",
    }
    sections = [
        prep._section(anchor, title, *prose[anchor]) + extras.get(anchor, "")
        for anchor, title in section_titles
    ]
    toc = "\n".join(
        f"- [{title}](#{prep._chapter_anchor(title)})" for _anchor, title in section_titles
    )
    status_banner = (
        "**狀態：FORMAL PIPELINE TEST／SYNTHETIC TEST ONLY。** 本報告只驗證正式流程，所有數字均為合成測試值，不得視為 00635U 正式實績。"
        if synthetic_test else
        "**狀態：FORMAL。** 輸入資料已通過 00 指定的 manifest 閘門；仍須完成本報告與附件驗收。"
    )
    report = (
        "# 00635U 黃金期貨 ETF 因果回測｜正式報告\n\n"
        f"> {status_banner}\n> 輸入 manifest SHA-256：`{accepted_manifest_sha256}`。\n\n"
        "## 閱讀順序\n\n" + toc + "\n\n" + "\n\n".join(sections)
        + f"\n\n`{STOP_CODE}`\n"
    )
    (output_root / REPORT_NAME).write_text(report, encoding="utf-8")
    artifact_names = tuple(name for name in attachments if name not in {"manifest.json", "readback.json"}) + (REPORT_NAME,)
    manifest = {
        "schema": "00635U_FORMAL_RUN_V1",
        "input_manifest_sha256": accepted_manifest_sha256,
        "input_data_classification": input_classification,
        "files": [
            {"path": name, "sha256": prep.sha256_file(output_root / name)}
            for name in artifact_names
        ],
    }
    (output_root / "manifest.json").write_bytes(prep._canonical_bytes(manifest) + b"\n")
    readback = {
        "status": "PASS", "formal": True,
        "stop_code": STOP_CODE,
        "manifest_sha256": prep.sha256_file(output_root / "manifest.json"),
        "files_verified": len(artifact_names),
    }
    (output_root / "readback.json").write_bytes(prep._canonical_bytes(readback) + b"\n")
    errors = (
        prep.validate_report_tree(output_root, report_name=REPORT_NAME)
        + prep.validate_report_schema(output_root, report_name=REPORT_NAME)
    )
    if errors:
        raise RuntimeError("; ".join(errors))
    return {
        "status": STOP_CODE,
        "report": str(output_root / REPORT_NAME),
        "manifest_sha256": readback["manifest_sha256"],
    }
