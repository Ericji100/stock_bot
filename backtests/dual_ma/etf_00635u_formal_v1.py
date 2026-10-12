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


@dataclass(frozen=True)
class FormalSnapshotData:
    raw_rows: tuple[RawETFRow, ...]
    indicator_rows: tuple[IndicatorRow, ...]
    gold_er: tuple[research.IndexObservation, ...]


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
    return FormalSnapshotData(
        raw_rows=tuple(raw_rows),
        indicator_rows=tuple(build_causal_indicator_rows(raw_rows)),
        gold_er=gold,
    )


def run_formal_analysis(data: FormalSnapshotData) -> dict[str, Any]:
    rows = data.indicator_rows
    lines = research.run_seven_lines(
        rows,
        data.gold_er,
        gold_alignment_policy="LAST_KNOWN_PUBLISHED",
    )
    line_metrics = {
        line.line_id: research.performance_metrics(line.curve) for line in lines
    }
    active: dict[str, Any] = {}
    for strategy in StrategyVersion:
        for timing in ExecutionTiming:
            result = run_strategy(rows, ETFResearchConfig(strategy=strategy, timing=timing))
            line_id = f"STRATEGY_{strategy.name}_{timing.value}"
            curve = tuple((item["date"], item["equity"]) for item in result.state.daily)
            active[line_id] = {
                "result": result,
                "metrics": research.performance_metrics(curve, result.state.trades),
                "annual": research.annual_slices(curve),
                "rolling": {years: research.rolling_windows(curve, years) for years in (3, 5, 10)},
                "regime": research.regime_summary(result.state.daily),
                "stability": research.trade_stability(result.state.trades),
                "path": research.path_diagnostics(curve),
            }
    benchmark = line_metrics["00635U_BUY_HOLD_REINVEST"]
    relative = {
        line_id: research.relative_to_benchmark(payload["metrics"], benchmark)
        for line_id, payload in active.items()
    }
    return {
        "lines": lines,
        "line_metrics": line_metrics,
        "active": active,
        "relative_to_buy_hold_reinvest": relative,
        "cost_sensitivity": research.cost_sensitivity(rows),
    }


def run_formal_pack(
    manifest_path: Path,
    output_root: Path,
    *,
    accepted_manifest_sha256: str,
) -> dict[str, Any]:
    from . import etf_00635u_prep_v1 as prep

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
    prep._write_csv(output_root / "event-ledger.csv", events or [{"status": "NO_EVENTS"}])
    prep._write_csv(output_root / "annual-performance.csv", annual)
    prep._write_csv(output_root / "rolling-windows.csv", rolling)
    prep._write_csv(output_root / "cost-sensitivity.csv", analysis["cost_sensitivity"])
    index_line = next(line for line in analysis["lines"] if line.line_id == "GOLD_ER_OFFICIAL_TRACKED_INDEX")
    prep._write_csv(output_root / "index-alignment-ledger.csv", list(index_line.audit["alignment_ledger"]))
    relative_rows = [
        {"line_id": line_id, **values}
        for line_id, values in analysis["relative_to_buy_hold_reinvest"].items()
    ]
    prep._write_csv(output_root / "relative-benchmark.csv", relative_rows)
    diagnostic_rows = []
    for line_id, payload in analysis["active"].items():
        diagnostic_rows.append({"line_id": line_id, **payload["stability"], **payload["path"]})
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
            rolling_5 = research.rolling_windows(curve, 5)
            valid_5 = [item["cagr"] for item in rolling_5 if item.get("status") == "OK"]
            grid_rows.append({
                "strategy": strategy.name,
                "ma_fast": parameters[0], "ma_slow": parameters[1],
                "macd_fast": parameters[2], "macd_slow": parameters[3], "macd_signal": parameters[4],
                "kd": "9/9/55", "two_r": "2R",
                "ending_equity": result.final_equity,
                "rolling_5y_worst_cagr": min(valid_5) if valid_5 else None,
            })
    prep._write_csv(output_root / "parameter-grid.csv", grid_rows)
    prep._write_csv(output_root / "parameter-grid-summary.csv", [
        {"strategy": strategy.name, **research.parameter_grid_summary([
            row for row in grid_rows if row["strategy"] == strategy.name
        ])}
        for strategy in StrategyVersion
    ])
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
    series = {
        line.line_id: [value for _day, value in line.curve]
        for line in analysis["lines"][:4]
    }
    chart_titles = {
        "equity-chart.svg": "正式資產曲線",
        "drawdown-chart.svg": "正式回撤路徑",
        "annual-chart.svg": "正式逐年績效",
        "parameter-grid-chart.svg": "243 格參數穩健性",
    }
    for name, title in chart_titles.items():
        (output_root / name).write_text(prep._chart_svg(series, title=title), encoding="utf-8")
    table = "\n".join(
        f"| `{row['line_id']}` | {row['ending_equity']} | {row['total_return']} | {row['cagr']} | {row['max_drawdown']} |"
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
        "formal-summary.csv", "relative-benchmark.csv", "daily-equity.csv", "trade-details.csv",
        "event-ledger.csv", "annual-performance.csv", "fixed-periods.csv", "rolling-windows.csv",
        "path-and-concentration.csv", "cost-sensitivity.csv", "parameter-grid.csv",
        "parameter-grid-summary.csv", "core-allocation-summary.csv", "core-allocation-daily.csv",
        "core-allocation-ledger.csv", "index-alignment-ledger.csv",
        "equity-chart.svg", "drawdown-chart.svg", "annual-chart.svg", "parameter-grid-chart.svg",
        "manifest.json", "readback.json",
    )
    sections = []
    for anchor, title in section_titles:
        extra = ""
        if anchor == "performance":
            extra = "\n| 主線 | 期末資產 | 總報酬 | CAGR | MaxDD |\n|---|---:|---:|---:|---:|\n" + table + "\n\n![正式資產曲線](equity-chart.svg)\n"
        if anchor == "attachments":
            extra = "\n" + "\n".join(f"- [{name}]({name})" for name in attachments) + "\n"
        sections.append(prep._section(
            anchor, title,
            "以已驗收快照完成本節固定檢查。",
            "使用連續 50 萬帳戶、完整費稅、因果訊號與可成交 session；詳細方法見附件。",
            "本節數值及 ledger 已輸出，Gold ER 對時含來源時區、可用日與 stale provenance。",
            "結論只適用 00635U 可交易載具；需同時考慮期貨轉倉、匯率、時區與追蹤缺口。",
            "依最差滾動窗口、成本壓力、集中度與參數分布決定是否支持正期望。",
        ) + extra)
    toc = "\n".join(f"- [{title}](#{anchor})" for anchor, title in section_titles)
    report = (
        "# 00635U 黃金期貨 ETF 因果回測｜正式報告\n\n"
        f"> FORMAL；輸入 manifest SHA-256：`{accepted_manifest_sha256}`。\n\n"
        "## 閱讀順序\n\n" + toc + "\n\n" + "\n\n".join(sections)
    )
    (output_root / prep.REPORT_NAME).write_text(report, encoding="utf-8")
    artifact_names = tuple(name for name in attachments if name not in {"manifest.json", "readback.json"}) + (prep.REPORT_NAME,)
    manifest = {
        "schema": "00635U_FORMAL_RUN_V1",
        "input_manifest_sha256": accepted_manifest_sha256,
        "files": [
            {"path": name, "sha256": prep.sha256_file(output_root / name)}
            for name in artifact_names
        ],
    }
    (output_root / "manifest.json").write_bytes(prep._canonical_bytes(manifest) + b"\n")
    readback = {
        "status": "PASS", "formal": True,
        "manifest_sha256": prep.sha256_file(output_root / "manifest.json"),
        "files_verified": len(artifact_names),
    }
    (output_root / "readback.json").write_bytes(prep._canonical_bytes(readback) + b"\n")
    errors = prep.validate_report_tree(output_root)
    if errors:
        raise RuntimeError("; ".join(errors))
    return {
        "status": "FORMAL_PACK_COMPLETE",
        "report": str(output_root / prep.REPORT_NAME),
        "manifest_sha256": readback["manifest_sha256"],
    }
