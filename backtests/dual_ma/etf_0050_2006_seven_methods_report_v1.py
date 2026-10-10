"""Traditional-Chinese report for the isolated seven-method 0050 study."""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_standard_report_v3 as standard
from . import etf_0050_2006_fixed_methods_v1 as accepted
from . import etf_0050_2006_seven_methods_v1 as runner
from . import etf_0050_ma21_fixed_stop_v1 as fixed
from . import etf_0050_ma21_ma144_regime_v1 as regime


REPORT_VERSION = "dual-ma-0050-2006-2026-seven-methods-report-v1.3"
COLORS = {
    "A_LONG": "#16a34a", fixed.CURRENT_STRATEGY_ID: "#f97316",
    fixed.STRATEGY_ID: "#dc2626", "BUY_HOLD_CASH": "#64748b",
    "BUY_HOLD_REINVEST": "#111827", regime.MOVING_ID: "#2563eb",
    regime.FIXED_ID: "#9333ea", "MARKET": "#0891b2",
}
EXIT_ZH = {
    "STRUCTURAL_STOP": "收盤跌破移動道氏防線",
    "INITIAL_PROTECTIVE_STOP": "收盤跌破固定初始防守價",
    "MACD_RED_TO_GREEN": "MACD 柱狀值由非負轉負",
    "": "截止日仍持有（未強平）",
}


def _read(path: Path) -> list[dict[str, str]]:
    return standard.read_csv(path)


def _stamp() -> tuple[str, str]:
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    return now.isoformat(), now.strftime("%Y-%m-%d %H:%M:%S（台北）")


def _overall(rows: Sequence[Mapping[str, Any]]) -> str:
    return standard.md_table(
        ("方法", "期末資產", "總報酬", "年化", "最大回落", "報酬／回落", "平均閒置", "完成／持有"),
        ((
            row["strategy_label"], standard.money(row["ending_equity"]),
            standard.pct(row["total_return"]), standard.pct(row["cagr"]),
            standard.pct(row["max_asset_drawdown"]),
            f"{standard.dec(row['return_to_drawdown']):.2f}" if row.get("return_to_drawdown") is not None else "—",
            standard.pct(row["idle_cash_ratio"]), f"{row['closed_rounds']}／{row['open_rounds']}",
        ) for row in rows),
    )


def _annual(rows: Sequence[Mapping[str, Any]]) -> str:
    return standard.md_table(
        ("方法", "年度", "年初資產", "年末／截止資產", "年度報酬", "年度最大回落", "進場", "賣出"),
        ((
            row["strategy_label"], row["year"], standard.money(row["starting_equity"]),
            standard.money(row["ending_equity"]), standard.pct(row["year_return"]),
            standard.pct(row["year_max_drawdown"]), row["entries"], row["exits"],
        ) for row in rows),
    )


def _trade_report(summaries: Sequence[Mapping[str, Any]], details: Sequence[Mapping[str, str]], produced: str) -> str:
    lines = ["# 0050 七組比較｜逐筆交易", "", f"產出時間：{produced}", "", "主文採 20bps；截止仍持有不強制賣出，損益為截止日估算。", ""]
    for summary in summaries:
        sid = str(summary["strategy_id"])
        lines += [f"## {summary['strategy_label']}", ""]
        rows = [row for row in details if row["strategy_id"] == sid]
        lines += [standard.md_table(
            ("狀態", "進場", "買進價", "股數", "初始防守", "賣出／估值日", "原因", "淨損益", "報酬"),
            ((
                row["status_zh"], row["entry_date"], standard.money(row["entry_fill_price"]),
                row["entry_shares"], standard.money(row["initial_stop"]) if row.get("initial_stop") else "—",
                row.get("exit_date") or str(accepted.CUTOFF), EXIT_ZH.get(row.get("exit_reason", ""), row.get("exit_reason", "")),
                standard.money(row["net_pnl_or_estimate"]), standard.pct(row["return_rate_or_estimate"]),
            ) for row in rows),
        ), ""]
    return "\n".join(lines)


def _render(
    *, summaries: Sequence[Mapping[str, Any]], annual: Sequence[Mapping[str, Any]],
    details: Sequence[Mapping[str, str]], market: Mapping[str, Any], produced: str,
) -> tuple[str, str, str, str]:
    timing = [row for row in summaries if row["method_kind"] == "擇時策略"]
    controls = [row for row in summaries if row["method_kind"] == "全程持有基準"]
    best = max(timing, key=lambda row: standard.dec(row["ending_equity"]))
    definitions = [
        "1. **基本版－長線**：沿用既有驗收規則。",
        "2. **原移動版**：MA21 新突破進場，移動道氏防線優先，否則 MACD(21,55,55) 紅翻綠出場。",
        "3. **原固定版**：MA21 新突破進場，固定初始防守優先，否則 MACD(21,55,55) 紅翻綠出場。",
        "4. **全程持有（股利留現金）**。",
        "5. **全程持有（股利再投入）**。",
        "6. **新增分流移動版**：SMA21≥SMA144 時只接 MA21 新突破；SMA21<SMA144 時只接 MACD 綠翻紅；移動道氏防線。",
        "7. **新增分流固定版**：相同互斥分流進場；固定初始防守、不啟動道氏防線。",
    ]
    attachments = [
        ("主報告", "report.md"), ("共享帳戶完整報告", "shared-account-report.md"),
        ("逐筆交易 MD", "trade-details.md"), ("逐筆交易 CSV", "trade-details.csv"),
        ("交易流水", "transactions.csv"), ("每日資產", "daily-equity.csv"),
        ("逐年摘要", "calendar-year-summary.csv"), ("主情境摘要", "scenario-summary.csv"),
        ("全情境摘要", "all-scenarios-summary.csv"), ("全情境逐筆", "all-scenarios-trade-details.csv"),
        ("全情境交易流水", "all-scenarios-transactions.csv"), ("全情境每日資產", "all-scenarios-daily-equity.csv"),
        ("全情境公司行動", "all-scenarios-corporate-actions.csv"),
        ("新策略逐日 regime／entry 稽核", "new-strategy-regime-entry-audit.csv"),
        ("新策略 MACD armed／cross 稽核", "new-strategy-macd-armed-cross-audit.csv"),
        ("新策略 stop／exit priority 稽核", "new-strategy-stop-exit-priority-audit.csv"),
        ("完整 MACD 事件", "macd-event-audit.csv"), ("公司行動", "corporate-actions.csv"),
        ("基準與風險", "benchmark-risk-report.md"), ("基準資料", "benchmark.csv"),
        ("資產曲線", "equity-curves.svg"), ("回落曲線", "drawdown-curves.svg"),
        ("輸入口徑", "input-manifest.json"), ("產物 SHA", "artifact-manifest.json"),
        ("讀回驗證", "readback.json"), ("原五組 parity", "report-parity.json"),
        ("QA checkpoint", "checkpoint.json"),
    ]
    index = "\n".join([
        "# 0050 2006～2026/09 七組比較回測", "", f"產出時間：{produced}", "",
        "目前狀態：`STOP_WAIT_00_QA`；尚未經 00 採納。", "",
        "## 七組策略與新舊差異", "", *definitions, "",
        "共同口徑：2005 暖機、2006-01-02～2026-09-30、50 萬元單一連續帳戶、不逐年重設。主文採 20bps；0/10/20bps 僅為敏感度分析，0bps 是不可實現的理想化情境。", "",
        "新增兩組只改進場分流與防守型態；MACD、因果技術座標、費稅、股息、分割、現金與估值均沿用已驗收基準，未依結果調參。", "",
        "## 完整附件", "",
        *[f"- [{label}]({target})" for label, target in attachments], "",
    ])
    shared = "\n".join([
        "# 0050 七組比較｜共享帳戶完整報告", "", f"產出時間：{produced}", "",
        "![七組每日總資產](equity-curves.svg)", "", "## 結論摘要", "",
        f"五個擇時策略中，20bps 期末資產最高為「{best['strategy_label']}」：{standard.money(best['ending_equity'])} 元。這是歷史回測，不等於未來保證，也未據此修改規則。", "",
        "## 全期比較（20bps）", "", _overall([*timing, *controls]), "",
        "## 逐年完整結果", "", _annual(annual), "",
        "## 因果與執行規則", "",
        "- 每日收盤只使用當日及以前資訊；訊號與成交同為當日收盤口徑。",
        "- SMA21=SMA144 歸 SMA21≥SMA144；兩種進場來源每天互斥。",
        "- MACD 綠翻紅進場當日立即 armed，之後只等待下一次非負轉負。",
        "- 候選初始防守價必須低於進場參考價；若兩者相等則留痕但不進場。",
        "- 停損與 MACD 同日成立時停損優先；賣出當日不重新進場；截止仍持有不強平。",
        "- 2025 分割只作股數與技術座標等比例換算，不視為交易。", "",
    ])
    risk = "\n".join([
        "# 0050 七組比較｜基準與風險", "", f"產出時間：{produced}", "",
        "![七組每日資產回落](drawdown-curves.svg)", "",
        f"加權報酬指數等值 50 萬元期末為 {standard.money(market['end_value'])} 元；年化 {standard.pct(market['annualized_return'])}；最大回落 {standard.pct(market['max_drawdown'])}。", "",
        "## 五個擇時策略與兩個持有基準", "", _overall(summaries), "",
        "期末資產不能單獨證明策略具正期望；應同時檢查最大回落、報酬／回落、閒置現金、交易次數、成本敏感度與樣本外穩定性。0bps 只顯示理想化上限，正式判讀採 20bps。", "",
    ])
    return index, shared, risk, _trade_report(summaries, details, produced)


def build(*, source_root: Path, output_root: Path) -> dict[str, Any]:
    if output_root.exists():
        raise standard.StandardReportError(f"output already exists: {output_root}")
    source_check = standard.verify_source(source_root, "QA_READY.json")
    metrics, details = _read(source_root / "scenario-summary.csv"), _read(source_root / "trade-details.csv")
    transactions, curves = _read(source_root / "transactions.csv"), _read(source_root / "daily-equity.csv")
    actions, benchmark = _read(source_root / "corporate-actions.csv"), _read(source_root / "benchmark.csv")
    labels = {row["strategy_id"]: row["strategy_label"] for row in metrics}
    if tuple(labels) != runner.METHOD_ORDER:
        raise standard.StandardReportError("unexpected seven-method order")
    previous = (standard.YEARS, standard.METHOD_ORDER, standard.TIMING_PREFIXES, dict(standard.COLORS), dict(standard.EXIT_REASON_ZH))
    standard.YEARS, standard.METHOD_ORDER = accepted.YEARS, runner.METHOD_ORDER
    standard.TIMING_PREFIXES, standard.COLORS = ("A_", "MA21_"), dict(COLORS)
    standard.EXIT_REASON_ZH.update(EXIT_ZH)
    try:
        annual = standard.annual_summaries(curves, transactions, details, actions, benchmark, labels)
        summaries = standard.overall_summaries(metrics, annual)
        daily = standard.comparison_daily(curves, benchmark, labels)
        market_curve = [(date.fromisoformat(row["date"]), standard.dec(row["equivalent_500k"])) for row in benchmark]
        market = standard.curve_metrics(market_curve, standard.INITIAL_CAPITAL)
        iso_time, produced = _stamp()
        index, shared, risk, trades = _render(summaries=summaries, annual=annual, details=details, market=market, produced=produced)
        parity = json.loads((source_root / "details" / "accepted-five-method-parity.json").read_text(encoding="utf-8-sig"))
        git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], text=True, encoding="utf-8").strip()
        with tempfile.TemporaryDirectory(prefix="0050-seven-methods-report-v1-") as temp_name:
            stage = Path(temp_name)
            for name, text in (("README.md", index), ("report.md", index), ("shared-account-report.md", shared), ("benchmark-risk-report.md", risk), ("trade-details.md", trades)):
                (stage / name).write_text(text, encoding="utf-8", newline="\n")
            standard.write_csv(stage / "scenario-summary.csv", summaries)
            standard.write_csv(stage / "calendar-year-summary.csv", annual)
            standard.write_csv(stage / "daily-equity.csv", daily)
            copy_names = (
                "trade-details.csv", "transactions.csv", "corporate-actions.csv", "benchmark.csv",
                "signal-opportunity-audit.csv", "new-strategy-regime-entry-audit.csv",
                "new-strategy-macd-armed-cross-audit.csv", "new-strategy-stop-exit-priority-audit.csv",
                "macd-event-audit.csv",
            )
            for name in copy_names:
                shutil.copy2(source_root / name, stage / name)
            all_names = {
                "all_scenarios_metrics.csv": "all-scenarios-summary.csv",
                "all_scenarios_trade_details.csv": "all-scenarios-trade-details.csv",
                "all_scenarios_transactions.csv": "all-scenarios-transactions.csv",
                "all_scenarios_daily_equity.csv": "all-scenarios-daily-equity.csv",
                "all_scenarios_corporate_actions.csv": "all-scenarios-corporate-actions.csv",
            }
            for source, target in all_names.items():
                shutil.copy2(source_root / "details" / source, stage / target)
            standard.svg_chart(stage / "equity-curves.svg", daily, value_field="equity", title="0050：七組方法與大盤每日總資產")
            standard.svg_chart(stage / "drawdown-curves.svg", daily, value_field="drawdown", title="0050：七組方法與大盤每日資產回落")
            standard.write_json(stage / "report-parity.json", parity)
            standard.write_json(stage / "input-manifest.json", {
                "status": "PASS", "report_version": REPORT_VERSION,
                "produced_at_taipei": iso_time, "runner_git_commit": git_commit,
                "source_root": source_root, "source_check": source_check,
                "period": {"start": accepted.FORMAL_START, "cutoff": accepted.CUTOFF},
                "initial_capital": standard.INITIAL_CAPITAL, "annual_reset": False,
                "main_slippage_bps": 20, "sensitivity_slippage_bps": [0, 10, 20],
                "accepted_five_method_parity": parity,
            })
            for planned in ("artifact-manifest.json", "readback.json", "checkpoint.json"):
                standard.write_json(stage / planned, {"status": "BUILDING"})
            for svg in (stage / "equity-curves.svg", stage / "drawdown-curves.svg"):
                ET.parse(svg)
            for md in stage.glob("*.md"):
                for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", md.read_text(encoding="utf-8")):
                    if not (stage / target).is_file():
                        raise standard.StandardReportError(f"broken Markdown link: {md.name} -> {target}")
            excluded = {"artifact-manifest.json", "readback.json", "checkpoint.json"}
            artifacts = standard.artifact_rows(stage, excluded)
            standard.write_json(stage / "artifact-manifest.json", {
                "status": "STOP_WAIT_00_QA", "report_version": REPORT_VERSION,
                "produced_at_taipei": iso_time, "artifact_count": len(artifacts),
                "annual_rows": len(annual), "daily_rows": len(daily),
                "trade_rows": len(details), "artifacts": artifacts,
            })
            mismatches = [item["path"] for item in artifacts if standard.sha256(stage / item["path"]) != item["sha256"]]
            standard.write_json(stage / "readback.json", {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches})
            if mismatches:
                raise standard.StandardReportError(f"report readback failed: {mismatches}")
            standard.write_json(stage / "checkpoint.json", {
                "status": "STOP_WAIT_00_QA",
                "artifact_manifest_sha256": standard.sha256(stage / "artifact-manifest.json").upper(),
                "readback_sha256": standard.sha256(stage / "readback.json").upper(),
                "safe_next_step": "00 independent seven-method report QA only",
            })
            output_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(stage, output_root)
        return {
            "status": "STOP_WAIT_00_QA", "output_root": str(output_root),
            "artifact_manifest_sha256": standard.sha256(output_root / "artifact-manifest.json").upper(),
            "readback_sha256": standard.sha256(output_root / "readback.json").upper(),
        }
    finally:
        standard.YEARS, standard.METHOD_ORDER, standard.TIMING_PREFIXES, standard.COLORS, exit_map = previous
        standard.EXIT_REASON_ZH.clear()
        standard.EXIT_REASON_ZH.update(exit_map)
