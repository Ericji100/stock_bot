"""Build the fixed Traditional-Chinese report for the 0050 MA21/MACD experiment."""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import etf_0050_standard_report_v3 as standard
from .etf_0050_ma21_macd_v2 import (
    ACCEPTED_REPORT_ROOT,
    ACCEPTED_REPORT_ARTIFACT_SHA256,
    CUTOFF,
    FORMAL_START,
    METHOD_ORDER,
    REPORT_ROOT_DEFAULT,
    STRATEGY_ID,
    STRATEGY_LABEL,
    YEARS,
)
from .etf_0050_ten_year_same_method_v1 import normalize_value


REPORT_VERSION = "dual-ma-0050-ma21-macd-standard-report-v2.1"
COLORS = {
    "A_LONG": "#16a34a",
    STRATEGY_ID: "#dc2626",
    "BUY_HOLD_CASH": "#64748b",
    "BUY_HOLD_REINVEST": "#111827",
    "MARKET": "#0891b2",
}
EXIT_REASON_ZH = {
    "STRUCTURAL_STOP": "收盤跌破既有結構防守價",
    "REGIME_EXIT": "均線多頭區間結束",
    "MACD_RED_TO_GREEN": "MACD 柱狀值由非負轉為負值停利",
    "": "截至資料截止日仍持有",
}


def _configure_standard_helpers() -> dict[str, Any]:
    previous = {
        "years": standard.YEARS,
        "method_order": standard.METHOD_ORDER,
        "timing_prefixes": standard.TIMING_PREFIXES,
        "colors": standard.COLORS,
        "exit_reason_zh": dict(standard.EXIT_REASON_ZH),
    }
    standard.YEARS = YEARS
    standard.METHOD_ORDER = METHOD_ORDER
    standard.TIMING_PREFIXES = ("A_", "MA21_")
    standard.COLORS = dict(COLORS)
    standard.EXIT_REASON_ZH.update(EXIT_REASON_ZH)
    return previous


def _restore_standard_helpers(previous: Mapping[str, Any]) -> None:
    standard.YEARS = previous["years"]
    standard.METHOD_ORDER = previous["method_order"]
    standard.TIMING_PREFIXES = previous["timing_prefixes"]
    standard.COLORS = previous["colors"]
    standard.EXIT_REASON_ZH.clear()
    standard.EXIT_REASON_ZH.update(previous["exit_reason_zh"])


def _read(path: Path) -> list[dict[str, str]]:
    return standard.read_csv(path)


def _baseline_report_parity(
    summaries: Sequence[Mapping[str, Any]],
    annual: Sequence[Mapping[str, Any]],
    daily: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if standard.sha256(ACCEPTED_REPORT_ROOT / "artifact-manifest.json").upper() != ACCEPTED_REPORT_ARTIFACT_SHA256:
        raise standard.StandardReportError("accepted report artifact pin changed")
    methods = {"A_LONG", "BUY_HOLD_CASH", "BUY_HOLD_REINVEST"}
    comparisons = {
        "scenario-summary.csv": (
            [row for row in summaries if row["strategy_id"] in methods],
            [row for row in _read(ACCEPTED_REPORT_ROOT / "scenario-summary.csv") if row["strategy_id"] in methods],
        ),
        "calendar-year-summary.csv": (
            [row for row in annual if row["strategy_id"] in methods],
            [row for row in _read(ACCEPTED_REPORT_ROOT / "calendar-year-summary.csv") if row["strategy_id"] in methods],
        ),
        "daily-equity.csv": (
            [row for row in daily if row["strategy_id"] in methods | {"MARKET"}],
            [row for row in _read(ACCEPTED_REPORT_ROOT / "daily-equity.csv") if row["strategy_id"] in methods | {"MARKET"}],
        ),
    }
    results: dict[str, Any] = {}
    for name, (new_rows, accepted_rows) in comparisons.items():
        fields = list(accepted_rows[0])
        new_normalized = [
            [normalize_value(row.get(field)) for field in fields] for row in new_rows
        ]
        accepted_normalized = [
            [normalize_value(row.get(field)) for field in fields]
            for row in accepted_rows
        ]
        exact = new_normalized == accepted_normalized
        results[name] = {
            "accepted_rows": len(accepted_rows),
            "replay_rows": len(new_rows),
            "numeric_and_text_exact": exact,
        }
        if not exact:
            raise standard.StandardReportError(f"accepted report parity failed: {name}")
    return {"status": "PASS", "files": results}


def _overall_table(rows: Sequence[Mapping[str, Any]]) -> str:
    return standard.md_table(
        (
            "方法", "獲利年度", "期末資產", "總報酬", "年化報酬",
            "最大資產回落", "報酬相對風險", "平均資金閒置",
            "最大投入", "進出次數", "尚未賣出部位",
        ),
        (
            (
                row["strategy_label"],
                f"{row['profitable_year_count']}／{len(YEARS)}（{row['profitable_years']}）",
                standard.money(row["ending_equity"]),
                standard.pct(row["total_return"]),
                standard.pct(row["cagr"]),
                standard.pct(row["max_asset_drawdown"]),
                f"{standard.dec(row['return_to_drawdown']):.2f}" if row["return_to_drawdown"] is not None else "—",
                standard.pct(row["idle_cash_ratio"]),
                standard.money(row["peak_concurrent_investment"]),
                f"{row['closed_rounds']} 買賣完成／{int(row['closed_rounds']) + int(row['open_rounds'])} 次進場",
                row["open_rounds"],
            )
            for row in rows
        ),
    )


def _annual_table(rows: Sequence[Mapping[str, Any]]) -> str:
    return standard.md_table(
        (
            "方法", "年度", "年初資產", "年底／截止資產", "年度報酬",
            "年度最大回落", "大盤報酬", "大盤最大回落", "進場", "賣出", "年底仍持有",
        ),
        (
            (
                row["strategy_label"],
                f"{row['year']}{'（至9/30）' if int(row['year']) == 2026 else ''}",
                standard.money(row["starting_equity"]),
                standard.money(row["ending_equity"]),
                standard.pct(row["year_return"]),
                standard.pct(row["year_max_drawdown"]),
                standard.pct(row["benchmark_return"]),
                standard.pct(row["benchmark_max_drawdown"]),
                row["entries"], row["exits"], row["ending_open_positions"],
            )
            for row in rows
        ),
    )


def _trade_report(
    summaries: Sequence[Mapping[str, Any]], details: Sequence[Mapping[str, str]]
) -> str:
    lines = [
        "# 0050｜逐筆交易",
        "",
        "已賣出與截至資料截止日仍持有分開標示；獲利率以該筆實際投入總額為分母，並已納入既有費稅、價差與持有期間股息。",
        "",
    ]
    for summary in summaries:
        sid = str(summary["strategy_id"])
        rows = [row for row in details if row["strategy_id"] == sid]
        lines.extend([f"## {summary['strategy_label']}", ""])
        lines.append(
            standard.md_table(
                (
                    "狀態", "訊號／買進日", "買進價格", "投入金額", "股數",
                    "賣出／估值日", "原因", "費稅", "股息", "淨損益", "獲利率",
                ),
                (
                    (
                        row["status_zh"], row["entry_date"],
                        standard.money(row["entry_fill_price"]),
                        standard.money(row["buy_total"]), row["entry_shares"],
                        row.get("exit_date") or str(CUTOFF),
                        EXIT_REASON_ZH.get(row.get("exit_reason", ""), row.get("exit_reason", "") or "截至資料截止日仍持有"),
                        standard.money(standard.dec(row["buy_commission"]) + standard.dec(row["sell_commission"]) + standard.dec(row["sell_tax"])),
                        standard.money(row["cash_dividends"]),
                        standard.money(row["net_pnl_or_estimate"]),
                        standard.pct(row["return_rate_or_estimate"]),
                    )
                    for row in rows
                ),
            )
        )
        lines.append("")
    return "\n".join(lines)


def _render(
    *, summaries: Sequence[Mapping[str, Any]], annual: Sequence[Mapping[str, Any]],
    details: Sequence[Mapping[str, str]], opportunities: Sequence[Mapping[str, str]],
    macd: Sequence[Mapping[str, str]], market: Mapping[str, Any],
) -> tuple[str, str, str, str, str]:
    timing = [row for row in summaries if row["method_kind"] == "擇時策略"]
    controls = [row for row in summaries if row["method_kind"] == "全程持有基準"]
    annual_timing = [row for row in annual if row["method_kind"] == "擇時策略"]
    best = max(timing, key=lambda row: standard.dec(row["ending_equity"]))
    signal_table = standard.md_table(
        ("日期", "收盤", "21日均線", "MACD柱狀值", "是否成交", "未成交原因", "防守價"),
        (
            (
                row["date"], standard.money(row["technical_close"]),
                standard.money(row["sma21"]), row["histogram"],
                row["execution_status"], row["not_filled_reason"] or "—",
                standard.money(row["structural_stop"]),
            )
            for row in opportunities
        ),
    )
    macd_counts: dict[str, int] = {}
    for row in macd:
        macd_counts[row["event"]] = macd_counts.get(row["event"], 0) + 1
    shared = "\n".join(
        [
            "# 0050｜MA21 突破與 MACD 停利十年比較",
            "",
            "![每日總資產](equity-curves.svg)",
            "",
            "## 先講結論",
            "",
            f"本研究使用同一筆 500,000 元本金，從 {FORMAL_START} 連續重播到 {CUTOFF}，不中途追加、不逐年重設。兩個會進出的策略中，期末資產較高的是「{best['strategy_label']}」：{standard.money(best['ending_equity'])} 元。這是固定規則回測結果，不據此修改參數。",
            "",
            "## 三組方法總覽",
            "",
            _overall_table([*timing, *controls]),
            "",
            "0050 全程持有是同一組基準的兩種股利處理列，不參與擇時策略排名。",
            "",
            "## 逐年結果",
            "",
            _annual_table(annual),
            "",
            "## 資金、交易與損益",
            "",
            standard.md_table(
                ("方法", "買入周轉總額", "最大投入", "完成交易", "截止仍持有", "已實現損益", "截止估算損益", "股息", "實際費稅與價差"),
                (
                    (
                        row["strategy_label"], standard.money(row["cumulative_buy"]),
                        standard.money(row["peak_concurrent_investment"]), row["closed_rounds"], row["open_rounds"],
                        standard.money(row["realized_net_pnl"]), standard.money(row["cutoff_estimated_pnl"]),
                        standard.money(row["dividends_received"]),
                        standard.money(standard.dec(row["actual_fees_and_tax"]) + standard.dec(row["actual_slippage_cost"])),
                    )
                    for row in summaries
                ),
            ),
            "",
            "## 新策略規則",
            "",
            "- 收盤由前一交易日位於 21 日均線下方或相等，轉為當日收盤高於 21 日均線，才是一次新突破；連續站上不重複買進。",
            "- 不要求 21 日均線高於 144 日均線，也不因之後跌破均線而賣出。",
            "- 停損採基本版－長線相同的因果 n=2 道氏動態結構核心：可繼承既有防線，持有中只上移，收盤跌破優先出場，出場後結束該防線生命週期。",
            "- 為遵守本策略不使用 21 日線高於 144 日線的規則，道氏防線每日更新不套用該快慢線門檻，也不因該 regime 結束而賣出或清除；因此停損日不必與基本版－長線逐筆相同。",
            "- MACD 使用 21、55、55；買進後須先處於或走到柱狀值大於或等於零，再等下一次由非負轉負才停利。",
            "- 同日同時符合防守價與 MACD 停利時，防守價優先；賣出當日不得重新進場。",
            f"- 截至 {CUTOFF} 仍持有時不強制賣出，只按當日收盤扣除假設賣出成本估算。",
            "",
            "## MA21 突破機會稽核",
            "",
            signal_table,
            "",
            f"事件稽核共 {len(macd)} 列：買進 {macd_counts.get('ENTRY', 0)}、買進後完成待命 {macd_counts.get('MACD_ARMED', 0)}、有效紅翻綠出場 {macd_counts.get('VALID_RED_TO_GREEN_EXIT', 0)}、停損優先 {macd_counts.get('STOP_PRIORITY_OVER_MACD', 0)}、一般結構停損 {macd_counts.get('STRUCTURAL_STOP_EXIT', 0)}、截止仍持有 {macd_counts.get('OPEN_AT_CUTOFF', 0)}、道氏防線建立／上移／套用／失效 {sum(macd_counts.get(name, 0) for name in ('DOW_DEFENSE_ESTABLISHED', 'DOW_DEFENSE_RAISED', 'DOW_DEFENSE_APPLIED_TO_LEG', 'DOW_DEFENSE_INVALIDATED'))}。完整逐日事件請見 [事件稽核](macd-event-audit.csv)。",
            "",
            "## 方法與限制",
            "",
            "- 技術指標使用逐日因果、公司行動連續的技術座標；實際成交與損益使用同日原始價格。",
            "- 0050 一拆四會同步調整歷史技術座標、既有防守價與股數，不當成暴跌、停損或額外交易。",
            "- 策略持有期間股息、除息、股份變動及現金流與既有已驗收基本版完全同口徑。",
            "- 2026 只統計到 9 月 30 日。",
            "",
        ]
    )
    risk_rows = [row for row in annual_timing if int(row["year"]) in {2022, 2025, 2026}]
    risk = "\n".join(
        [
            "# 0050｜大盤與風險比較",
            "",
            "![每日資產回落](drawdown-curves.svg)",
            "",
            f"台股加權報酬指數由等值 500,000 元成長為 {standard.money(market['end_value'])} 元；總報酬 {standard.pct(market['cumulative_return'])}，年化報酬 {standard.pct(market['annualized_return'])}，最大資產回落 {standard.pct(market['max_drawdown'])}。",
            "",
            "## 2022 空頭與 2025～2026 多頭",
            "",
            _annual_table(risk_rows),
            "",
            "判斷策略不能只看期末資產；需同時查看 2022 是否降低回落、2025～2026 獲利依賴、報酬相對風險與平均資金閒置。2026 為截至 9 月 30 日的非完整年度。",
            "",
        ]
    )
    index = "\n".join(
        [
            "# 0050 十年回測｜閱讀順序",
            "",
            "本報告只比較基本版－長線、新增的 MA21＋MACD 策略，以及 0050 全程持有的兩種股利口徑。",
            "",
            "1. [共享帳戶總覽](shared-account-report.md)",
            "2. [大盤與風險比較](benchmark-risk-report.md)",
            "3. [逐筆交易](trade-details.md)",
            "4. [MA21 突破機會稽核](signal-opportunity-audit.csv)",
            "5. [MACD 事件稽核](macd-event-audit.csv)",
            "",
        ]
    )
    return index, index, shared, risk, _trade_report(summaries, details)


def _build_configured(
    *, source_root: Path, output_root: Path = REPORT_ROOT_DEFAULT
) -> dict[str, Any]:
    if output_root.exists():
        raise standard.StandardReportError(f"output already exists: {output_root}")
    source_check = standard.verify_source(source_root, "QA_READY.json")
    metrics = _read(source_root / "scenario-summary.csv")
    details = _read(source_root / "trade-details.csv")
    transactions = _read(source_root / "transactions.csv")
    curves = _read(source_root / "daily-equity.csv")
    actions = _read(source_root / "corporate-actions.csv")
    opportunities = _read(source_root / "signal-opportunity-audit.csv")
    macd = _read(source_root / "macd-event-audit.csv")
    benchmark = _read(source_root / "benchmark.csv")
    labels = {row["strategy_id"]: row["strategy_label"] for row in metrics}
    if tuple(labels) != METHOD_ORDER:
        raise standard.StandardReportError(f"unexpected method order: {tuple(labels)}")
    annual = standard.annual_summaries(
        curves, transactions, details, actions, benchmark, labels
    )
    summaries = standard.overall_summaries(metrics, annual)
    daily = standard.comparison_daily(curves, benchmark, labels)
    market_curve = [
        (date.fromisoformat(row["date"]), standard.dec(row["equivalent_500k"]))
        for row in benchmark
    ]
    market = standard.curve_metrics(market_curve, standard.INITIAL_CAPITAL)
    parity = _baseline_report_parity(summaries, annual, daily)
    readme, index, shared, risk, trade_report = _render(
        summaries=summaries, annual=annual, details=details,
        opportunities=opportunities, macd=macd, market=market,
    )
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2],
        text=True, encoding="utf-8",
    ).strip()
    with tempfile.TemporaryDirectory(prefix="0050-ma21-macd-report-v2-1-") as temp_name:
        stage = Path(temp_name)
        for name, text in (
            ("README.md", readme), ("report.md", index),
            ("shared-account-report.md", shared),
            ("benchmark-risk-report.md", risk),
            ("trade-details.md", trade_report),
        ):
            (stage / name).write_text(text, encoding="utf-8", newline="\n")
        standard.write_csv(stage / "scenario-summary.csv", summaries)
        standard.write_csv(stage / "calendar-year-summary.csv", annual)
        standard.write_csv(stage / "daily-equity.csv", daily)
        for name in (
            "trade-details.csv", "transactions.csv", "corporate-actions.csv",
            "signal-opportunity-audit.csv", "macd-event-audit.csv", "benchmark.csv",
        ):
            shutil.copy2(source_root / name, stage / name)
        standard.svg_chart(stage / "equity-curves.svg", daily, value_field="equity", title="0050：基本長線、MA21＋MACD、全程持有與大盤")
        standard.svg_chart(stage / "drawdown-curves.svg", daily, value_field="drawdown", title="0050：每日資產回落比較")
        standard.write_json(stage / "report-parity.json", parity)
        standard.write_json(
            stage / "input-manifest.json",
            {
                "status": "PASS", "report_version": REPORT_VERSION,
                "runner_git_commit": git_commit, "source_root": source_root,
                "source_check": source_check,
                "period": {"start": FORMAL_START, "cutoff": CUTOFF},
                "initial_capital": standard.INITIAL_CAPITAL, "annual_reset": False,
                "fixed_template": "docs/strategies/dual_ma/standard-backtest-report-v2.md",
                "accepted_baseline_report_parity": parity,
            },
        )
        for svg in (stage / "equity-curves.svg", stage / "drawdown-curves.svg"):
            ET.parse(svg)
        for md in stage.glob("*.md"):
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", md.read_text(encoding="utf-8")):
                if not (stage / target).is_file():
                    raise standard.StandardReportError(f"broken Markdown link: {md.name} -> {target}")
        forbidden = re.compile(r"\b(?:HOLD|OPEN_AT_CUTOFF|OPEN)\b")
        if any(forbidden.search(path.read_text(encoding="utf-8")) for path in stage.glob("*.md")):
            raise standard.StandardReportError("report leaked an internal status token")
        excluded = {"artifact-manifest.json", "readback.json", "checkpoint.json"}
        artifacts = standard.artifact_rows(stage, excluded)
        standard.write_json(
            stage / "artifact-manifest.json",
            {
                "status": "STOP_WAIT_00_QA", "report_version": REPORT_VERSION,
                "artifact_count": len(artifacts), "annual_rows": len(annual),
                "daily_rows": len(daily), "trade_rows": len(details),
                "artifacts": artifacts,
            },
        )
        mismatches = [
            item["path"] for item in artifacts
            if standard.sha256(stage / item["path"]) != item["sha256"]
        ]
        standard.write_json(stage / "readback.json", {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches})
        if mismatches:
            raise standard.StandardReportError(f"report artifact readback failed: {mismatches}")
        standard.write_json(
            stage / "checkpoint.json",
            {
                "status": "STOP_WAIT_00_QA",
                "artifact_manifest_sha256": standard.sha256(stage / "artifact-manifest.json").upper(),
                "readback_sha256": standard.sha256(stage / "readback.json").upper(),
                "safe_next_step": "00 independent strategy, parity, report and chart QA only",
            },
        )
        output_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output_root)
    return {
        "status": "STOP_WAIT_00_QA", "output_root": output_root,
        "artifact_manifest_sha256": standard.sha256(output_root / "artifact-manifest.json").upper(),
        "readback_sha256": standard.sha256(output_root / "readback.json").upper(),
        "baseline_report_parity": "PASS",
    }


def build(*, source_root: Path, output_root: Path = REPORT_ROOT_DEFAULT) -> dict[str, Any]:
    previous = _configure_standard_helpers()
    try:
        return _build_configured(source_root=source_root, output_root=output_root)
    finally:
        _restore_standard_helpers(previous)
