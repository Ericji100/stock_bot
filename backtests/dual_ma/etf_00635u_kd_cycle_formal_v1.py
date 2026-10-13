"""Formal, manifest-gated report pack for the isolated 00635U KD-cycle study."""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import product
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from . import etf_00635u_formal_v1 as prior
from . import etf_00635u_kd_cycle_v1 as cycle
from . import etf_00635u_prep_v1 as prep
from . import etf_00635u_research_v1 as research
from .etf_00635u_strategy_v1 import ETFResearchConfig, ExecutionTiming


REPORT_NAME = "README.md"
TRADE_REPORT_NAME = "trade-details.md"

LINE_LABELS = {
    **prior.LINE_LABELS,
    "KD_CYCLE_C_SIGNAL_CLOSE": "策略 C（訊號日收盤成交）",
    "KD_CYCLE_C_NEXT_SESSION_OPEN": "策略 C（次一交易日開盤成交）",
    "KD_CYCLE_D_SIGNAL_CLOSE": "策略 D（訊號日收盤成交）",
    "KD_CYCLE_D_NEXT_SESSION_OPEN": "策略 D（次一交易日開盤成交）",
}

GRID_VALUES = (45, 55, 65)
GRID_MA_VALUES = (18, 21, 24)
GRID_PARAMETERS = tuple(product(GRID_MA_VALUES, GRID_VALUES, GRID_VALUES))


def _line_id(policy: cycle.KDCyclePolicy, timing: ExecutionTiming) -> str:
    return f"KD_CYCLE_{policy.name}_{timing.value}"


def _label(line_id: str) -> str:
    return LINE_LABELS.get(line_id, line_id)


def _money(value: object, *, signed: bool = False) -> str:
    return prior._money(value, signed=signed)


def _percent(value: object) -> str:
    return prior._percent(value)


def _pp(value: object) -> str:
    return prior._percentage_points(value)


def _curve(result: cycle.KDCycleRunResult) -> tuple[tuple[date, Decimal], ...]:
    return tuple((row["date"], row["equity"]) for row in result.state.daily)


def _regime_daily(
    result: cycle.KDCycleRunResult,
    rows: Sequence[cycle.KDEntryRow],
) -> list[dict[str, Any]]:
    by_day = {row.day: row for row in rows}
    output = []
    for item in result.state.daily:
        source = by_day[item["date"]].base
        regime = (
            "WARMUP" if source.sma21 is None or source.sma144 is None else
            "HIGH" if source.sma21 > source.sma144 else
            "LOW" if source.sma21 < source.sma144 else "EQUAL"
        )
        output.append({**item, "regime": regime})
    return output


def _core_study(
    rows: Sequence[cycle.KDEntryRow],
    policy: cycle.KDCyclePolicy,
) -> dict[str, list[dict[str, Any]]]:
    summaries, daily, ledger = [], [], []
    base_rows = [row.base for row in rows]
    for weight in (0, 20, 40, 60):
        core_capital = Decimal("500000") * Decimal(weight) / Decimal("100")
        tactical_capital = Decimal("500000") - core_capital
        tactical = cycle.run_kd_cycle_strategy(
            rows, policy=policy, timing=ExecutionTiming.NEXT_SESSION_OPEN,
            initial_capital=tactical_capital,
        )
        tactical_by_day = {row["date"]: row for row in tactical.state.daily}
        if core_capital:
            core = research.run_buy_hold(
                base_rows, reinvest=True,
                config=ETFResearchConfig(initial_capital=core_capital),
            )
            core_by_day = {row["date"]: row for row in core.audit["account_daily"]}
            ledger.extend({
                "policy": policy.name, "core_pct": weight,
                "sleeve": "CORE", **row,
            } for row in core.audit["transactions"])
        else:
            core_by_day = {
                row.day: {
                    "date": row.day, "cash": Decimal("0"), "shares": 0,
                    "liquidation_value": Decimal("0"), "equity": Decimal("0"),
                }
                for row in base_rows
            }
        ledger.extend({
            "policy": policy.name, "core_pct": weight,
            "sleeve": "TACTICAL", **row,
        } for row in tactical.state.trades)
        allocation = []
        for day in sorted(set(core_by_day) & set(tactical_by_day)):
            core_row, tactical_row = core_by_day[day], tactical_by_day[day]
            combined = core_row["equity"] + tactical_row["equity"]
            record = {
                "policy": policy.name, "core_pct": weight,
                "rebalance": "NONE_MAIN_METHOD", "date": day,
                "core_cash": core_row["cash"], "core_shares": core_row["shares"],
                "core_liquidation_value": core_row["liquidation_value"],
                "core_equity": core_row["equity"],
                "tactical_cash": tactical_row["cash"],
                "tactical_shares": tactical_row["shares"],
                "tactical_liquidation_value": tactical_row["liquidation_value"],
                "tactical_equity": tactical_row["equity"],
                "combined_equity": combined,
                "reconstruction_error": (
                    combined - core_row["equity"] - tactical_row["equity"]
                ),
            }
            daily.append(record)
            allocation.append((day, combined))
        metrics = research.performance_metrics(allocation)
        entries = [
            row for row in ledger
            if row["policy"] == policy.name and row["core_pct"] == weight
        ]
        summaries.append({
            "policy": policy.name, "core_pct": weight,
            "rebalance": "NONE_MAIN_METHOD",
            "ending_equity": allocation[-1][1], "cagr": metrics["cagr"],
            "max_drawdown": metrics["max_drawdown"],
            "maximum_reconstruction_error": max(
                abs(row["reconstruction_error"])
                for row in daily
                if row["policy"] == policy.name and row["core_pct"] == weight
            ),
            "transaction_count": len(entries),
            "total_commission": sum(
                (research.decimal(row.get("commission", 0)) for row in entries),
                Decimal("0"),
            ),
            "total_tax": sum(
                (research.decimal(row.get("tax", 0)) for row in entries),
                Decimal("0"),
            ),
        })
    return {"summary": summaries, "daily": daily, "ledger": ledger}


def _trade_report(trades: Sequence[Mapping[str, Any]]) -> str:
    reason = {
        "HIGH_REGIME_MA21_BREAKOUT": "高檔 MA21 向上突破",
        "LOW_REGIME_MACD_GREEN_TO_RED": "低檔 MACD 新翻紅",
        "MACD_ONLY_GREEN_TO_RED": "純 MACD 新翻紅",
        "KD_9_55_55_HIGHER_LOW_MA21_BREAKOUT": "KD 循環低點墊高＋MA21 突破",
        "INITIAL_PROTECTIVE_STOP": "固定初始防守",
        "MACD_RED_TO_GREEN": "MACD 紅翻綠",
        "KD_2R_DEATH_CROSS_EXIT": "費後 2R 啟動後 KD 新死叉",
    }
    rows = []
    for row in trades:
        code = row.get("entry_type") if row["role"] == "ENTRY" else row.get("exit_reason")
        rows.append(
            f"| {_label(row['line_id'])} | {'進場' if row['role'] == 'ENTRY' else '出場'} | "
            f"{row['signal_date']} | {row['fill_date']} | {reason.get(code, code)} | "
            f"`{row['position_id']}` | {int(row['shares']):,} | {_money(row['fill_price'])} | "
            f"{_money(row.get('commission', 0))} | {_money(row.get('tax', 0))} | "
            f"{_money(row['initial_stop']) if row.get('initial_stop') is not None else '—'} | "
            f"{_money(row['initial_r']) if row.get('initial_r') is not None else '—'} | "
            f"{row.get('kd_2r_arm_date') or '—'} | "
            f"{_money(row['net_pnl'], signed=True) if row.get('net_pnl') is not None else '—'} | "
            f"{row.get('cycle_id') or '—'} |"
        )
    return (
        "# 00635U KD 循環進場隔離研究｜繁體中文逐筆明細\n\n"
        "[返回主報告](README.md)\n\n"
        "> 完整原始精度保留於 [逐筆交易 CSV](trade-details.csv)；本表只作閱讀。\n\n"
        "| 策略 | 角色 | 訊號日 | 成交日 | 進場來源／出場理由 | 部位 ID | 股數 | 成交價 | 手續費 | 稅 | 初始防守 | 初始 R | 2R 啟動日 | 淨損益 | 循環 ID |\n"
        "|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|---|\n"
        + "\n".join(rows) + "\n"
    )


def run_formal_pack(
    manifest_path: Path,
    output_root: Path,
    *,
    accepted_manifest_sha256: str,
) -> dict[str, Any]:
    if cycle.sha256_file(cycle.CONTRACT) != cycle.CONTRACT_SHA256:
        raise RuntimeError("contract SHA mismatch")
    input_manifest = prep.validate_formal_snapshot_gate(
        manifest_path, accepted_manifest_sha256=accepted_manifest_sha256,
        accepted_by="00",
    )
    if input_manifest.get("snapshot_id") != cycle.SNAPSHOT_ID:
        raise RuntimeError("snapshot ID mismatch")
    if accepted_manifest_sha256 != cycle.INPUT_MANIFEST_SHA256:
        raise RuntimeError("input manifest pin mismatch")
    data = prior.load_formal_snapshot(manifest_path)
    entry_rows = cycle.build_kd_entry_rows(data.raw_rows)
    base = prior.run_formal_analysis(data, include_macd_only_variants=True)
    output_root.mkdir(parents=True, exist_ok=False)

    new_results: dict[str, cycle.KDCycleRunResult] = {}
    for policy in cycle.KDCyclePolicy:
        for timing in ExecutionTiming:
            new_results[_line_id(policy, timing)] = cycle.run_kd_cycle_strategy(
                entry_rows, policy=policy, timing=timing
            )

    rewritten_ma144 = [
        replace(
            row,
            base=replace(row.base, sma144=Decimal(index % 97 + 1)),
        )
        for index, row in enumerate(entry_rows)
    ]
    invariance_checks = []
    for timing in ExecutionTiming:
        baseline = new_results[_line_id(cycle.KDCyclePolicy.D, timing)]
        replay = cycle.run_kd_cycle_strategy(
            rewritten_ma144, policy=cycle.KDCyclePolicy.D, timing=timing
        )
        clean = lambda records: [
            {
                key: value for key, value in row.items()
                if key != "entry_sma144_diagnostic"
            }
            for row in records
        ]
        orders_equal = clean(baseline.state.trades) == clean(replay.state.trades)
        events_equal = baseline.state.events == replay.state.events
        accounts_equal = baseline.state.daily == replay.state.daily
        if not (orders_equal and events_equal and accounts_equal):
            raise AssertionError("Strategy D MA144 diagnostic rewrite changed execution")
        invariance_checks.append({
            "timing": timing.value,
            "orders_equal_ignoring_diagnostic_field": orders_equal,
            "events_equal": events_equal,
            "accounts_equal": accounts_equal,
        })

    lines = list(base["lines"])
    for line_id, result in new_results.items():
        lines.append(research.ComparisonLine(
            line_id, "ACTIVE_STRATEGY", _curve(result),
            {"trades": tuple(result.state.trades)},
        ))
    line_by_id = {line.line_id: line for line in lines}

    active: dict[str, dict[str, Any]] = {}
    for line_id, payload in base["active"].items():
        active[line_id] = {
            "trades": list(payload["result"].state.trades),
            "events": list(payload["result"].state.events),
            "daily": list(payload["result"].state.daily),
        }
    for line_id, result in new_results.items():
        active[line_id] = {
            "trades": list(result.state.trades),
            "events": list(result.state.events),
            "daily": list(result.state.daily),
        }

    metrics = {
        line.line_id: research.performance_metrics(
            line.curve, active.get(line.line_id, {}).get("trades", ())
        )
        for line in lines
    }
    summary = [{"line_id": key, **value} for key, value in metrics.items()]
    prep._write_csv(output_root / "formal-summary.csv", summary)
    prep._write_csv(output_root / "daily-equity.csv", [
        {"line_id": line.line_id, "date": day, "equity": equity}
        for line in lines for day, equity in line.curve
    ])
    account_daily = [
        {"line_id": line_id, **row}
        for line_id, payload in active.items() for row in payload["daily"]
    ]
    if any(
        row["cash"] < 0
        or row["equity"] != row["cash"] + row["liquidation_value"]
        for row in account_daily
    ):
        raise AssertionError("strategy account daily invariant failed")
    prep._write_csv(output_root / "strategy-account-daily.csv", account_daily)

    trades = [
        {"line_id": line_id, **row}
        for line_id, payload in active.items() for row in payload["trades"]
    ]
    events = [
        {"line_id": line_id, **row}
        for line_id, payload in active.items() for row in payload["events"]
    ]
    prep._write_csv(output_root / "trade-details.csv", trades)
    prep._write_csv(output_root / "event-ledger.csv", events)
    (output_root / TRADE_REPORT_NAME).write_text(
        _trade_report(trades), encoding="utf-8"
    )

    cycle_audit = [
        {"line_id": line_id, **row}
        for line_id, result in new_results.items() for row in result.cycle_audit
    ]
    prep._write_csv(output_root / "kd-cycle-audit.csv", cycle_audit)
    company_actions = [
        {
            "date": row.day, "price_rebase_factor": row.price_rebase_factor,
            "share_factor": row.share_factor,
            "cash_dividend_per_share": row.cash_dividend_per_share,
            "dividend_event_id": row.dividend_event_id,
            "dividend_stage": row.dividend_stage,
        }
        for row in data.raw_rows
        if row.price_rebase_factor != 1 or row.share_factor != 1
        or row.cash_dividend_per_share or row.dividend_stage
    ]
    prep._write_csv(
        output_root / "corporate-action-ledger.csv",
        company_actions or [{"status": "NO_CORPORATE_ACTIONS_IN_ACCEPTED_SNAPSHOT"}],
    )

    annual, rolling, fixed, regimes = [], [], [], []
    fixed_periods = (
        ("2015_2019", date(2015, 4, 15), date(2019, 12, 31)),
        ("2020_2022", date(2020, 1, 1), date(2022, 12, 31)),
        ("2023_LATEST", date(2023, 1, 1), data.indicator_rows[-1].day),
    )
    for line in lines:
        annual.extend({"line_id": line.line_id, **row} for row in research.annual_slices(line.curve))
        for years in (3, 5, 10):
            rolling.extend({"line_id": line.line_id, **row} for row in research.rolling_windows(line.curve, years))
        fixed.extend({
            "line_id": line.line_id, "period_id": period_id,
            **research.slice_curve(line.curve, start, end),
        } for period_id, start, end in fixed_periods)
    for line_id, result in new_results.items():
        regimes.extend({"line_id": line_id, **row} for row in research.regime_summary(_regime_daily(result, entry_rows)))
    prep._write_csv(output_root / "annual-performance.csv", annual)
    prep._write_csv(output_root / "rolling-windows.csv", rolling)
    prep._write_csv(output_root / "fixed-periods.csv", fixed)
    prep._write_csv(output_root / "regime-performance.csv", regimes)

    benchmark_ids = ("00635U_BUY_HOLD_CASH", "00635U_BUY_HOLD_REINVEST")
    relative = [
        {"line_id": line_id, "benchmark_id": benchmark_id,
         **research.relative_to_benchmark(metrics[line_id], metrics[benchmark_id])}
        for line_id in active for benchmark_id in benchmark_ids
    ]
    prep._write_csv(output_root / "relative-benchmark.csv", relative)
    taiex_rows = [{"status": base["taiex_status"]}]
    if base["taiex_supplement"] is not None:
        taiex_rows = [
            {"status": base["taiex_status"], "date": day, "equity": equity}
            for day, equity in base["taiex_supplement"].curve
        ]
    prep._write_csv(output_root / "taiex-supplement.csv", taiex_rows)
    prep._write_csv(output_root / "index-alignment-ledger.csv", [{
        "status": "OMITTED_NO_SP_GSCI_GOLD_ER_OFFICIAL_HISTORY"
    }])

    diagnostics = []
    for line_id, payload in active.items():
        line = line_by_id[line_id]
        stability = research.trade_stability(payload["trades"])
        path = research.path_diagnostics(line.curve)
        removals = {
            f"remove_best_{count}_{key}": value
            for count in (1, 3, 5)
            for key, value in research.trade_removal_cash_counterfactual(
                line.curve, payload["trades"], count
            ).items()
        }
        diagnostics.append({"line_id": line_id, **stability, **path, **removals})
    prep._write_csv(output_root / "path-and-concentration.csv", diagnostics)

    costs = []
    for bps in (20, 40, 60):
        for policy in cycle.KDCyclePolicy:
            for timing in ExecutionTiming:
                result = cycle.run_kd_cycle_strategy(
                    entry_rows, policy=policy, timing=timing,
                    slippage_bps=bps,
                )
                result_metrics = research.performance_metrics(
                    _curve(result), result.state.trades
                )
                costs.append({
                    "policy": policy.name, "timing": timing.value,
                    "slippage_bps": bps,
                    "ending_equity": result.final_equity,
                    "cagr": result_metrics["cagr"],
                    "max_drawdown": result_metrics["max_drawdown"],
                    "completed_rounds": result_metrics["closed_positions"],
                })
    prep._write_csv(output_root / "cost-sensitivity.csv", costs)

    grid = []
    for ma_fast, entry_k_period, entry_d_period in GRID_PARAMETERS:
        variant = cycle.build_kd_entry_rows(
            data.raw_rows, ma_fast=ma_fast,
            entry_k_period=entry_k_period, entry_d_period=entry_d_period,
        )
        for policy in cycle.KDCyclePolicy:
            result = cycle.run_kd_cycle_strategy(
                variant, policy=policy, timing=ExecutionTiming.NEXT_SESSION_OPEN
            )
            result_metrics = research.performance_metrics(
                _curve(result), result.state.trades
            )
            rolling5 = [
                row["cagr"] for row in research.rolling_windows(_curve(result), 5)
                if row.get("status") == "OK"
            ]
            grid.append({
                "policy": policy.name, "ma_fast": ma_fast,
                "entry_kd": f"9/{entry_k_period}/{entry_d_period}",
                "exit_kd": "9/9/55", "two_r": "2R",
                "ending_equity": result.final_equity,
                "total_return": result_metrics["total_return"],
                "cagr": result_metrics["cagr"],
                "max_drawdown": result_metrics["max_drawdown"],
                "rolling_5y_worst_cagr": min(rolling5) if rolling5 else None,
            })
    grid_summary = []
    for policy in cycle.KDCyclePolicy:
        selected = [row for row in grid if row["policy"] == policy.name]
        baseline = next(
            row for row in selected
            if row["ma_fast"] == 21 and row["entry_kd"] == "9/55/55"
        )
        grid_summary.append({
            "policy": policy.name, "cells": len(selected),
            "median_ending_equity": median(row["ending_equity"] for row in selected),
            "positive_ratio": Decimal(sum(row["total_return"] > 0 for row in selected)) / len(selected),
            "baseline_ending_equity": baseline["ending_equity"],
            "worst_5y_cagr": min(
                row["rolling_5y_worst_cagr"] for row in selected
                if row["rolling_5y_worst_cagr"] is not None
            ),
        })
    prep._write_csv(output_root / "parameter-grid.csv", grid)
    prep._write_csv(output_root / "parameter-grid-summary.csv", grid_summary)

    core_summary, core_daily, core_ledger = [], [], []
    for policy in cycle.KDCyclePolicy:
        study = _core_study(entry_rows, policy)
        core_summary.extend(study["summary"])
        core_daily.extend(study["daily"])
        core_ledger.extend(study["ledger"])
    if any(row["reconstruction_error"] != 0 for row in core_daily):
        raise AssertionError("core/tactical reconstruction invariant failed")
    prep._write_csv(output_root / "core-allocation-summary.csv", core_summary)
    prep._write_csv(output_root / "core-allocation-daily.csv", core_daily)
    prep._write_csv(output_root / "core-allocation-ledger.csv", core_ledger or [{"status": "NO_TRANSACTIONS"}])

    shutil.copyfile(cycle.CONTRACT, output_root / "strategy-rule-contract.md")
    (output_root / "contract-verification.json").write_bytes(
        prep._canonical_bytes({
            "status": "PASS", "contract_sha256": cycle.CONTRACT_SHA256,
            "copied_contract_sha256": prep.sha256_file(output_root / "strategy-rule-contract.md"),
            "semantic_rewrite": False,
        }) + b"\n"
    )
    (output_root / "ma144-invariance-readback.json").write_bytes(
        prep._canonical_bytes({
            "status": "PASS",
            "policy": cycle.KDCyclePolicy.D.value,
            "ma144_role": "DIAGNOSTIC_ONLY",
            "checks": invariance_checks,
        }) + b"\n"
    )

    chart_lines = [
        "STRATEGY_B_NEXT_SESSION_OPEN",
        "MACD_ONLY_STRATEGY_B_NEXT_SESSION_OPEN",
        "KD_CYCLE_C_NEXT_SESSION_OPEN",
        "KD_CYCLE_D_NEXT_SESSION_OPEN",
        "00635U_BUY_HOLD_CASH",
    ]
    equity_series = {
        _label(line_id): [value for _day, value in line_by_id[line_id].curve]
        for line_id in chart_lines
    }
    drawdown_series = {}
    for line_id in chart_lines:
        peak, values = Decimal("0"), []
        for _day, value in line_by_id[line_id].curve:
            peak = max(peak, value)
            values.append(value / peak - 1 if peak else Decimal("0"))
        drawdown_series[_label(line_id)] = values
    annual_series = {
        _label(line_id): [row["return"] for row in annual if row["line_id"] == line_id]
        for line_id in chart_lines
    }
    rolling_series = {
        _label(line_id): [
            row["cagr"] for row in rolling
            if row["line_id"] == line_id and row.get("years") == 5 and row.get("status") == "OK"
        ] for line_id in chart_lines
    }
    regime_series = {
        _label(line_id): [row["compounded_return"] for row in regimes if row["line_id"] == line_id]
        for line_id in new_results
    }
    pnl_series = {
        _label(line_id): [row["net_pnl"] for row in active[line_id]["trades"] if row["role"] == "EXIT"]
        for line_id in new_results
    }
    cost_series = {
        f"策略 {policy.name}": [
            row["ending_equity"] for row in costs
            if row["policy"] == policy.name and row["timing"] == ExecutionTiming.NEXT_SESSION_OPEN.value
        ] for policy in cycle.KDCyclePolicy
    }
    core_series = {
        f"策略 {policy.name}": [row["ending_equity"] for row in core_summary if row["policy"] == policy.name]
        for policy in cycle.KDCyclePolicy
    }
    grid_series = {
        f"策略 {policy.name}": [row["ending_equity"] for row in grid if row["policy"] == policy.name]
        for policy in cycle.KDCyclePolicy
    }
    chart_specs = {
        "equity-chart.svg": ("equity_curve", "正式資產曲線", equity_series, "交易日期", "資產（元）", [row.day.isoformat() for row in data.indicator_rows]),
        "drawdown-chart.svg": ("drawdown", "正式回撤路徑", drawdown_series, "交易日期", "回撤率", [row.day.isoformat() for row in data.indicator_rows]),
        "annual-chart.svg": ("annual_return", "逐年報酬", annual_series, "年度", "報酬率", [str(row["end"].year) for row in research.annual_slices(lines[0].curve)]),
        "rolling-window-chart.svg": ("rolling_window", "五年滾動年化報酬", rolling_series, "窗口序號", "年化報酬率", None),
        "regime-chart.svg": ("regime", "高低均線區間績效", regime_series, "均線區間", "複合報酬", ["高", "低"]),
        "trade-pnl-chart.svg": ("trade_pnl", "新策略逐筆交易淨損益", pnl_series, "交易序號", "淨損益（元）", None),
        "cost-pressure-chart.svg": ("cost_pressure", "成本壓力測試", cost_series, "單邊滑價（基點）", "期末資產（元）", ["20", "40", "60"]),
        "core-allocation-chart.svg": ("core_allocation", "核心持有比例", core_series, "核心比例", "期末資產（元）", ["0%", "20%", "40%", "60%"]),
        "parameter-grid-chart.svg": ("parameter_grid", "27 組預先凍結鄰近參數", grid_series, "參數序號", "期末資產（元）", [str(i) for i in range(1, 28)]),
    }
    for name, (kind, title, source, x_label, y_label, ticks) in chart_specs.items():
        (output_root / name).write_text(
            prep._chart_svg(source, title=title, kind=kind, x_label=x_label, y_label=y_label, x_tick_labels=ticks),
            encoding="utf-8",
        )

    practical_ids = (
        "KD_CYCLE_C_NEXT_SESSION_OPEN", "KD_CYCLE_D_NEXT_SESSION_OPEN"
    )
    insufficient = [
        line_id for line_id in practical_ids if metrics[line_id]["closed_positions"] < 10
    ]
    insufficiency_text = (
        "；".join(
            f"{_label(line_id)}完成交易 {int(metrics[line_id]['closed_positions'])} 筆"
            for line_id in insufficient
        ) + "，樣本不足，只能作探索性研究，不得宣稱採納。"
        if insufficient else
        "兩個實務主口徑完成交易均不少於 10 筆；仍只屬歷史隔離研究，不代表採納。"
    )
    perf_table = "\n".join(
        f"| {_label(row['line_id'])} | "
        f"{'次一交易日開盤' if 'NEXT_SESSION_OPEN' in row['line_id'] else '訊號日收盤' if 'SIGNAL_CLOSE' in row['line_id'] else '比較基準'} | "
        f"{_money(row['ending_equity'])} | {_percent(row['total_return'])} | {_percent(row['cagr'])} | {_percent(row['max_drawdown'])} | "
        f"{int(row['profitable_years'])}／{int(row['losing_years'])}／{int(row['flat_years'])} | "
        f"{int(row['closed_positions'] + row['open_positions'])}／{int(row['closed_positions'])}／{int(row['open_positions'])} | {_money(row['maximum_single_entry_cash'])} |"
        for row in summary
    )
    relative_table = "\n".join(
        f"| {_label(row['line_id'])} | {_label(row['benchmark_id'])} | "
        f"{_money(row['ending_equity_difference'], signed=True)} | {_pp(row['cagr_difference'])} | {_pp(row['max_drawdown_difference'])} |"
        for row in relative if row["line_id"] in practical_ids
    )
    cycle_signals = [row for row in cycle_audit if row.get("signal_generated")]
    cancellations = [row for row in cycle_audit if row.get("execution_status") == "CANCELLED_OPEN_AT_OR_BELOW_STOP"]
    grid_table = "\n".join(
        f"| 策略 {row['policy']} | {int(row['cells'])} | {_money(row['median_ending_equity'])} | {_percent(row['positive_ratio'])} | {_money(row['baseline_ending_equity'])} | {_percent(row['worst_5y_cagr'])} |"
        for row in grid_summary
    )
    core_table = "\n".join(
        f"| 策略 {row['policy']} | {int(row['core_pct'])}% | {_money(row['ending_equity'])} | {_percent(row['cagr'])} | {_percent(row['max_drawdown'])} |"
        for row in core_summary
    )
    annual_table = "\n".join(
        f"| {_label(row['line_id'])} | {row['end'].year} | "
        f"{_money(row['starting_equity'])} | {_money(row['ending_equity'])} | "
        f"{_percent(row['return'])} | {_percent(row['max_drawdown'])} |"
        for row in annual
    )
    fixed_table = "\n".join(
        f"| {_label(row['line_id'])} | {row['period_id']} | "
        f"{row.get('start', '—')}～{row.get('end', '—')} | "
        f"{_money(row['starting_equity']) if row.get('status') == 'OK' else '資料不足'} | "
        f"{_money(row['ending_equity']) if row.get('status') == 'OK' else '資料不足'} | "
        f"{_percent(row['return']) if row.get('status') == 'OK' else '資料不足'} | "
        f"{_percent(row['cagr']) if row.get('status') == 'OK' else '資料不足'} | "
        f"{_percent(row['max_drawdown']) if row.get('status') == 'OK' else '資料不足'} |"
        for row in fixed
    )
    rolling_summary_rows = []
    for line in lines:
        for years in (3, 5, 10):
            selected = [
                row for row in rolling
                if row["line_id"] == line.line_id
                and row.get("years") == years and row.get("status") == "OK"
            ]
            rolling_summary_rows.append({
                "line_id": line.line_id, "years": years, "count": len(selected),
                "worst": min((row["cagr"] for row in selected), default=None),
                "median": median(row["cagr"] for row in selected) if selected else None,
                "best": max((row["cagr"] for row in selected), default=None),
                "worst_drawdown": min((row["max_drawdown"] for row in selected), default=None),
            })
    rolling_table = "\n".join(
        f"| {_label(row['line_id'])} | {row['years']} 年 | {row['count']} | "
        f"{_percent(row['worst']) if row['worst'] is not None else '資料不足'} | "
        f"{_percent(row['median']) if row['median'] is not None else '資料不足'} | "
        f"{_percent(row['best']) if row['best'] is not None else '資料不足'} | "
        f"{_percent(row['worst_drawdown']) if row['worst_drawdown'] is not None else '資料不足'} |"
        for row in rolling_summary_rows
    )
    regime_table = "\n".join(
        f"| {_label(row['line_id'])} | {'高均線區間' if row['regime'] == 'HIGH' else '低均線區間'} | "
        f"{int(row['sessions']):,} | {_percent(row['compounded_return'])} |"
        for row in regimes
    )
    cost_table = "\n".join(
        f"| 策略 {row['policy']} | {'次一交易日開盤' if row['timing'] == 'NEXT_SESSION_OPEN' else '訊號日收盤'} | "
        f"{int(row['slippage_bps'])} | {_money(row['ending_equity'])} | "
        f"{_percent(row['cagr'])} | {_percent(row['max_drawdown'])} | {int(row['completed_rounds'])} |"
        for row in costs
    )
    concentration_table = "\n".join(
        f"| {_label(row['line_id'])} | {int(row['closed_trades'])} | {_percent(row['win_rate'])} | "
        f"{_money(row['best_trade'], signed=True) if row['best_trade'] is not None else '—'} | "
        f"{_money(row['worst_trade'], signed=True) if row['worst_trade'] is not None else '—'} | "
        f"{_percent(row['top1_gain_concentration'])} | {_percent(row['top3_gain_concentration'])} | "
        f"{int(row['longest_consecutive_losses'])} | {int(row['longest_underwater_calendar_days'])} 日 |"
        for row in diagnostics
    )
    new_trade_summary = "\n".join(
        f"| {_label(line_id)} | {int(metrics[line_id]['closed_positions'] + metrics[line_id]['open_positions'])} | "
        f"{int(metrics[line_id]['closed_positions'])} | {int(metrics[line_id]['open_positions'])} | "
        f"{_money(metrics[line_id]['maximum_single_entry_cash'])} |"
        for line_id in new_results
    )

    attachments = (
        "formal-summary.csv", "relative-benchmark.csv", "daily-equity.csv",
        "strategy-account-daily.csv", "trade-details.md", "trade-details.csv",
        "event-ledger.csv", "kd-cycle-audit.csv", "corporate-action-ledger.csv",
        "annual-performance.csv", "fixed-periods.csv", "rolling-windows.csv",
        "regime-performance.csv", "path-and-concentration.csv",
        "cost-sensitivity.csv", "parameter-grid.csv", "parameter-grid-summary.csv",
        "core-allocation-summary.csv", "core-allocation-daily.csv",
        "core-allocation-ledger.csv", "index-alignment-ledger.csv",
        "taiex-supplement.csv", "strategy-rule-contract.md",
        "contract-verification.json", "ma144-invariance-readback.json",
        "equity-chart.svg", "drawdown-chart.svg",
        "annual-chart.svg", "rolling-window-chart.svg", "regime-chart.svg",
        "trade-pnl-chart.svg", "cost-pressure-chart.svg",
        "core-allocation-chart.svg", "parameter-grid-chart.svg",
        "manifest.json", "readback.json",
    )
    section_titles = (
        ("conclusion", "01｜一頁白話結論"),
        ("performance", "02｜完整績效總表"),
        ("relative", "03｜相對基準"),
        ("rules", "04｜策略規則與成交版本"),
        ("annual", "05｜完整逐年績效"),
        ("trades", "06｜交易紀錄、循環稽核與資金投入"),
        ("execution", "07｜成交時間與成本"),
        ("periods", "08｜時期、滾動與狀態"),
        ("concentration", "09｜交易集中度與路徑風險"),
        ("grid", "10｜預先凍結參數鄰近穩健性"),
        ("core", "11｜核心持有比例研究"),
        ("practice", "12｜實務建議與黃金 ETF 限制"),
        ("qa", "13｜技術驗收與隔離重播"),
        ("attachments", "14｜完整附件目錄"),
    )
    observations = {
        "conclusion": f"策略 C 次開期末資產 {_money(metrics[practical_ids[0]]['ending_equity'])}；策略 D 次開期末資產 {_money(metrics[practical_ids[1]]['ending_equity'])}。{insufficiency_text}",
        "performance": f"共同期間保留原策略、純 MACD、新策略 C／D 與兩條買進持有，共 {len(lines)} 條比較線。",
        "relative": "策略 C／D 均逐一比較 00635U 現金保留與分配再投入；官方 Gold ER 未取得，因此不以代理替代。",
        "rules": f"契約原文 SHA-256 為 `{cycle.CONTRACT_SHA256}`，複製檔與來源位元組一致；本輪只改進場。",
        "annual": f"完整逐年附件含 {len(annual):,} 列，年度帳戶連續、不重設本金。",
        "trades": f"逐循環稽核共 {len(cycle_audit):,} 列，合法訊號 {len(cycle_signals):,} 列，次開低於或等於防守價取消 {len(cancellations):,} 列。",
        "execution": f"成本附件含 {len(costs):,} 列，涵蓋 C／D、收盤／次開及 20／40／60 基點。",
        "periods": f"固定期間 {len(fixed):,} 列、滾動窗口 {len(rolling):,} 列；C／D 高低均線區間另列 {len(regimes):,} 列。",
        "concentration": f"{len(active)} 條主動線均列勝率、最佳／最差交易、獲利集中度、連虧與水下期。",
        "grid": "策略 C、D 各 27 組預先凍結鄰近參數；主規則固定 MA21 與進場 KD(9,55,55)，出場 KD(9,9,55) 與 2R 完全不搜尋。",
        "core": f"C／D 各 0／20／40／60% 共 {len(core_summary)} 組，core 與 tactical 每日分帳且重建誤差為零。",
        "practice": insufficiency_text,
        "qa": f"輸入 manifest `{accepted_manifest_sha256}` 已由 00 gate 接受；輸出需兩次全新隔離重播 exact parity。",
        "attachments": f"本包提供 {len(attachments)} 個可點入口，含原始 CSV、中文 SVG、契約、manifest 與 readback。",
    }
    prose = {
        "conclusion": ("先回答新進場是否值得進一步研究。", "以次一交易日開盤為實務主口徑，同時比較原策略、純 MACD、買進持有及風險。", observations["conclusion"], "單一歷史樣本不能證明未來正期望；交易筆數不足時只可稱探索性。", "報告只交 00 嚴格 QA，不自動採納策略。"),
        "performance": ("在同期間比較所有正式線。", "所有線以 500,000 元、相同資料與費稅重播。", observations["performance"], "期末資產、年化報酬率與最大回落必須一起看。", "主表是其餘分析的唯一母表。"),
        "relative": ("衡量相對可交易被動基準的差距。", "計算期末資產、年化與最大回落差；TAIEX 只作補充。", observations["relative"], "缺少 Gold ER 時不能宣稱完成黃金指數超額報酬比較。", "主要決策只依可驗證的 00635U 基準。"),
        "rules": ("鎖定唯一進場契約與未改動範圍。", "逐字保存契約；稽核 K1／L1／H1、K2_so_far、突破、分流、防守與 2R。", observations["rules"], "突破日不必 K<D，也不是比較突破日單一 K；只有 K2_so_far>K1。", "任何語意變更必須另開版本。"),
        "annual": ("檢查報酬是否集中少數年度。", "以前一年末資產為次年起點，首年使用初始本金。", observations["annual"], "獲利年度數不能取代最差年度幅度。", "年度集中過高會降低採納信心。"),
        "trades": ("讓 00 能逐筆重建型態、訂單與帳務。", "保存循環所有區段、候選日、成交／排除、R／2R、armed 與出場。", observations["trades"], "每循環最多一次訊號，次開成交不具正風險距離即取消。", "任何摘要交易數都須由附件一對一重建。"),
        "execution": ("量化成交時間與摩擦。", "兩種成交版本各自進出同口徑，並施加三種滑價。", observations["execution"], "成本可能改變 2R 與出場路徑，不能只線性扣減。", "實務判讀固定使用次一交易日開盤。"),
        "periods": ("檢查不同時期、滾動窗口與均線區間。", "輸出固定期、月末滾動 3／5／10 年及 HIGH／LOW 診斷。", observations["periods"], "全期正報酬不代表每個長窗口穩健。", "最差窗口限制最終結論。"),
        "concentration": ("檢查少數交易與路徑依賴。", "列勝率、最佳／最差、前 1／3 筆獲利集中、連虧與水下期。", observations["concentration"], "少數交易撐起結果時，正期望證據偏弱。", "集中度過高只能保留研究候選。"),
        "grid": ("檢查主參數是否只在單點有效。", "事前固定 MA 18／21／24、進場 K/D 平滑 45／55／65，共 27 組；RSV9、出場 KD 與 2R 固定。", observations["grid"], "鄰近格只作敏感度，不用來事後改選漂亮參數。", "主設定若顯著偏離分布，結論降級。"),
        "core": ("觀察核心持有比例的報酬與回落取捨。", "core 與 tactical 分帳，主方法不再平衡。", observations["core"], "混合結果不是事後線性乘權重。", "年度再平衡若研究必須另開版本。"),
        "practice": ("把歷史研究轉成限制條件。", "檢查交易筆數、成本、轉倉、匯率、時區與追蹤誤差。", observations["practice"], "00635U 是期貨 ETF 載具，不等於現貨黃金。", "只有 00 QA 通過才可決定下一步；本報告不代表採納。"),
        "qa": ("證明資料、規則、帳務與輸出可重現。", "驗契約 SHA、輸入 gate、帳務守恆、連結、圖表 metadata、manifest 與雙重播。", observations["qa"], "任一 hash、因果或帳務差異都必須停止。", "狀態只可停在指定 QA stop code。"),
        "attachments": ("提供完整稽核材料。", "逐檔列出報告用資料、帳本、圖表與驗證檔。", observations["attachments"], "主文不能取代原始附件。", "00 以 SHA 與獨立重算為最終依據。"),
    }
    extras = {
        "performance": "\n| 主線 | 成交方式 | 期末資產 | 總報酬 | 年化報酬率 | 最大回落 | 獲利／虧損／平盤年度 | 進場／出場／未平倉 | 最大投入 |\n|---|---|---:|---:|---:|---:|---:|---:|---:|\n" + perf_table + "\n\n![正式資產曲線](equity-chart.svg)\n\n![正式回撤路徑](drawdown-chart.svg)\n",
        "relative": "\n| 策略 | 比較基準 | 期末資產差 | 年化報酬率差 | 最大回落差 |\n|---|---|---:|---:|---:|\n" + relative_table + "\n",
        "rules": f"\n- **新進場：** 使用進場 KD(9,55,55) 的 K<D／K>D 循環、嚴格 K2_so_far > K1 與 MA21 收盤向上突破；策略 C 以 MA21／MA144 嚴格分流，等號不進，策略 D 的 MA144 只作診斷。\n- **未改動規則：** 原策略仍保留 MACD(21,55,55) 比較線；新策略沿用固定初始防守、淨成本 2R 永久啟動、出場 KD(9,9,55) 新死叉，固定防守優先。訊號日收盤成交與次一個可成交交易日開盤成交各自進出同口徑。\n- [唯一規則契約原文](strategy-rule-contract.md)\n- [契約 SHA 驗證](contract-verification.json)\n- [策略 D MA144 隔離回讀](ma144-invariance-readback.json)\n- [逐循環因果稽核](kd-cycle-audit.csv)\n",
        "annual": "\n#### 完整逐年績效表\n\n| 策略／基準 | 年度 | 年初資產 | 年末資產 | 年度報酬 | 年度最大回落 |\n|---|---:|---:|---:|---:|---:|\n" + annual_table + "\n\n- [完整逐年績效 CSV](annual-performance.csv)\n\n![逐年報酬](annual-chart.svg)\n",
        "trades": f"\n| 新策略 | 進場 | 已完成 | 未平倉 | 最大投入 |\n|---|---:|---:|---:|---:|\n{new_trade_summary}\n\n- [繁體中文逐筆交易明細]({TRADE_REPORT_NAME})\n- [逐筆交易明細 CSV](trade-details.csv)\n- [每日主帳](strategy-account-daily.csv)\n- [事件帳](event-ledger.csv)\n- [公司行動帳](corporate-action-ledger.csv)\n- [逐循環稽核](kd-cycle-audit.csv)\n\n![新策略逐筆損益](trade-pnl-chart.svg)\n",
        "execution": "\n#### 完整成交與成本壓力表\n\n| 策略 | 成交方式 | 單邊滑價（基點） | 期末資產 | 年化報酬率 | 最大回落 | 完成交易 |\n|---|---|---:|---:|---:|---:|---:|\n" + cost_table + "\n\n- [完整成本壓力 CSV](cost-sensitivity.csv)\n\n![成本壓力](cost-pressure-chart.svg)\n",
        "periods": "\n#### 固定期間完整表\n\n| 策略／基準 | 期間 | 實際起訖 | 期初資產 | 期末資產 | 總報酬 | 年化報酬率 | 最大回落 |\n|---|---|---|---:|---:|---:|---:|---:|\n" + fixed_table + "\n\n#### 滾動 3／5／10 年摘要\n\n| 策略／基準 | 窗口 | 有效數 | 最差年化 | 中位年化 | 最佳年化 | 窗口最差回落 |\n|---|---:|---:|---:|---:|---:|---:|\n" + rolling_table + "\n\n#### 高低均線區間\n\n| 策略 | 區間 | 交易日數 | 區間複合報酬 |\n|---|---|---:|---:|\n" + regime_table + "\n\n- [固定期間 CSV](fixed-periods.csv)\n- [完整滾動窗口 CSV](rolling-windows.csv)\n- [高低均線區間 CSV](regime-performance.csv)\n\n![五年滾動年化](rolling-window-chart.svg)\n\n![高低均線區間](regime-chart.svg)\n",
        "concentration": "\n| 策略 | 已完成 | 勝率 | 最佳交易 | 最差交易 | 最佳 1 筆集中度 | 最佳 3 筆集中度 | 最長連虧 | 最長水下期 |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|\n" + concentration_table + "\n\n- [集中度、移除最佳交易與路徑診斷](path-and-concentration.csv)\n",
        "grid": "\n| 策略 | 格數 | 中位期末資產 | 正報酬比例 | 主設定期末資產 | 五年最差年化 |\n|---|---:|---:|---:|---:|---:|\n" + grid_table + "\n\n![27 組鄰近參數](parameter-grid-chart.svg)\n",
        "core": "\n| 策略 | 核心比例 | 期末資產 | 年化報酬率 | 最大回落 |\n|---|---:|---:|---:|---:|\n" + core_table + "\n\n![核心持有比例](core-allocation-chart.svg)\n",
        "qa": "\n- [輸出 manifest](manifest.json)\n- [回讀驗證](readback.json)\n",
        "attachments": "\n" + "\n".join(f"- [{name}]({name})" for name in attachments) + "\n",
    }
    sections = [
        prep._section(anchor, title, *prose[anchor]) + extras.get(anchor, "")
        for anchor, title in section_titles
    ]
    toc = "\n".join(
        f"- [{title}](#{prep._chapter_anchor(title)})"
        for _anchor, title in section_titles
    )
    report = (
        "# 00635U 黃金期貨 ETF｜KD 循環低點墊高＋MA21 突破隔離研究\n\n"
        "> **狀態：隔離研究候選，尚未採納。** 凍結資料與唯一契約均已通過 SHA gate；仍須 00 嚴格 QA。\n"
        f"> 資料快照：`{cycle.SNAPSHOT_ID}`；輸入 manifest SHA-256：`{accepted_manifest_sha256}`。\n"
        f"> 唯一規則契約 SHA-256：`{cycle.CONTRACT_SHA256}`；契約原文已原樣收入附件。\n\n"
        "## 閱讀順序\n\n" + toc + "\n\n" + "\n\n".join(sections)
        + f"\n\n`{cycle.STOP_CODE}`\n"
    )
    (output_root / REPORT_NAME).write_text(report, encoding="utf-8")

    artifact_names = tuple(
        name for name in attachments if name not in {"manifest.json", "readback.json"}
    ) + (REPORT_NAME,)
    manifest = {
        "schema": "00635U_KD_CYCLE_ENTRY_STUDY_V1",
        "artifact_classification": "ISOLATED_RESEARCH_CANDIDATE",
        "publication_eligible": False,
        "intended_use": "STRICT_00_QA_BEFORE_ANY_PUBLICATION",
        "snapshot_id": input_manifest.get("snapshot_id"),
        "input_manifest_sha256": accepted_manifest_sha256,
        "contract_sha256": cycle.CONTRACT_SHA256,
        "source_package_manifest_sha256": input_manifest.get("source_package_manifest_sha256"),
        "source_package_readback_sha256": input_manifest.get("source_package_readback_sha256"),
        "files": [
            {"path": name, "sha256": prep.sha256_file(output_root / name)}
            for name in artifact_names
        ],
    }
    (output_root / "manifest.json").write_bytes(prep._canonical_bytes(manifest) + b"\n")
    readback = {
        "status": "PASS", "formal": True,
        "artifact_classification": "ISOLATED_RESEARCH_CANDIDATE",
        "publication_eligible": False, "stop_code": cycle.STOP_CODE,
        "manifest_sha256": prep.sha256_file(output_root / "manifest.json"),
        "files_verified": len(artifact_names),
        "account_invariants": "PASS",
        "contract_sha256": cycle.CONTRACT_SHA256,
    }
    (output_root / "readback.json").write_bytes(prep._canonical_bytes(readback) + b"\n")
    errors = (
        prep.validate_report_tree(output_root, report_name=REPORT_NAME)
        + prep.validate_report_schema(output_root, report_name=REPORT_NAME)
    )
    if errors:
        raise RuntimeError("; ".join(errors))
    return {
        "status": cycle.STOP_CODE,
        "report": str(output_root / REPORT_NAME),
        "manifest_sha256": readback["manifest_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--accepted-manifest-sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(run_formal_pack(
        args.snapshot_manifest,
        args.output_root,
        accepted_manifest_sha256=args.accepted_manifest_sha256,
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

