"""Build a complete report view from a pinned seven-method source run."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_2006_fixed_methods_v1 as accepted
from . import etf_0050_2006_seven_methods_report_v1 as compact
from . import etf_0050_2006_seven_methods_v1 as runner
from . import etf_0050_ma21_fixed_stop_v1 as fixed
from . import etf_0050_ma21_ma144_regime_v1 as regime
from . import etf_0050_standard_report_v3 as standard
from .etf_0050_base_v1 import tree_hash
from .paths import dev_run, report_path


REPORT_VERSION = "dual-ma-0050-2006-2026-seven-methods-complete-report-v1.4"
SOURCE_ROOT = dev_run("02-etf-0050-2006-2026-seven-methods-v1-3")
SEALED_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-seven-methods-v1-3")
OUTPUT_ROOT_DEFAULT = report_path("etf", "0050", "experiments", "2006-2026-seven-methods-complete-report-v1-4")
EXPECTED_RUN_MANIFEST = "8A4B933BF866E62C5CC43CCC2438A5CD20DE9D73545A464C3A83A28E09E29BCB"
EXPECTED_REPORT_MANIFEST = "71473FE01DA2BF47F6A0DE914C06049159166C09EBBE276ABC960A4276D78D22"


def _read(path: Path) -> list[dict[str, str]]:
    return standard.read_csv(path)


def _stamp() -> tuple[str, str]:
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    return now.isoformat(), now.strftime("%Y-%m-%d %H:%M:%S（台北）")


def _years_text(row: Mapping[str, Any], key: str) -> str:
    value = str(row[key])
    return value if value and value != "無" else "無"


def _overall_full(rows: Sequence[Mapping[str, Any]]) -> str:
    return standard.md_table(
        (
            "方法", "期末資產", "總報酬", "CAGR", "最大回落",
            "獲利年度", "虧損年度", "平盤年度", "進場／出場／未平倉", "最大投入",
        ),
        ((
            row["strategy_label"], standard.money(row["ending_equity"]),
            standard.pct(row["total_return"]), standard.pct(row["cagr"]),
            standard.pct(row["max_asset_drawdown"]),
            f"{row['profitable_year_count']}/21（{_years_text(row, 'profitable_years')}）",
            f"{row['losing_year_count']}/21（{_years_text(row, 'losing_years')}）",
            f"{21-int(row['profitable_year_count'])-int(row['losing_year_count'])}/21（{_years_text(row, 'flat_years')}）",
            f"{int(row['closed_rounds']) + int(row['open_rounds'])}／{row['closed_rounds']}／{row['open_rounds']}",
            standard.money(row["peak_concurrent_investment"]),
        ) for row in rows),
    )


def _capital_full(rows: Sequence[Mapping[str, Any]]) -> str:
    return standard.md_table(
        (
            "方法", "累計買進", "最大投入", "已實現淨損益", "截止未實現／估計損益",
            "股息收入", "手續費＋證交稅", "滑價成本",
        ),
        ((
            row["strategy_label"], standard.money(row["cumulative_buy"]),
            standard.money(row["peak_concurrent_investment"]), standard.money(row["realized_net_pnl"]),
            standard.money(row["cutoff_estimated_pnl"]), standard.money(row["dividends_received"]),
            standard.money(row["actual_fees_and_tax"]), standard.money(row["actual_slippage_cost"]),
        ) for row in rows),
    )


def _annual_full(rows: Sequence[Mapping[str, Any]]) -> str:
    return standard.md_table(
        (
            "方法", "年度", "年初資產", "年底／截止資產", "策略報酬", "策略最大回落",
            "大盤報酬", "大盤最大回落", "進場", "出場", "年底／截止仍持有",
        ),
        ((
            row["strategy_label"], f"{row['year']}{'（至 9/30）' if int(row['year']) == 2026 else ''}",
            standard.money(row["starting_equity"]), standard.money(row["ending_equity"]),
            standard.pct(row["year_return"]), standard.pct(row["year_max_drawdown"]),
            standard.pct(row["benchmark_return"]), standard.pct(row["benchmark_max_drawdown"]),
            row["entries"], row["exits"], row["ending_open_positions"],
        ) for row in rows),
    )


def _market_period(rows: Sequence[Mapping[str, Any]]) -> str:
    chosen = [row for row in rows if int(row["year"]) in {2022, 2025, 2026}]
    return standard.md_table(
        ("方法", "年度", "策略報酬", "策略最大回落", "大盤報酬", "大盤最大回落", "年底／截止仍持有"),
        ((
            row["strategy_label"], f"{row['year']}{'（至 9/30）' if int(row['year']) == 2026 else ''}",
            standard.pct(row["year_return"]), standard.pct(row["year_max_drawdown"]),
            standard.pct(row["benchmark_return"]), standard.pct(row["benchmark_max_drawdown"]),
            row["ending_open_positions"],
        ) for row in chosen),
    )


def _concentration(rows: Sequence[Mapping[str, Any]]) -> str:
    def share(row: Mapping[str, Any]) -> str:
        total = standard.dec(row["ending_equity"]) - standard.INITIAL_CAPITAL
        if total <= 0 or abs(total) < Decimal("1"):
            return "不適用（總獲利非正或接近零）"
        return standard.pct(row["share_of_profit_from_2025_2026"])
    return standard.md_table(
        ("方法", "截至 2024 累積獲利", "2025～2026/09 增加獲利", "後段占總獲利"),
        ((
            row["strategy_label"], standard.money(row["profit_through_2024"]),
            standard.money(row["profit_during_2025_2026"]), share(row),
        ) for row in rows),
    )


def _signal_summary(
    regime_rows: Sequence[Mapping[str, str]], event_rows: Sequence[Mapping[str, str]],
) -> str:
    by_strategy: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in regime_rows:
        by_strategy[row["strategy_id"]].append(row)
    event_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in event_rows:
        event_counts[row["strategy_id"]][row["event"]] += 1
    labels = {regime.MOVING_ID: regime.MOVING_LABEL, regime.FIXED_ID: regime.FIXED_LABEL}
    return standard.md_table(
        (
            "新策略", "SMA21≥SMA144 日", "SMA21<SMA144 日", "MA21 突破候選",
            "MACD 綠翻紅候選", "實際進場", "已有持股略過", "同日賣出略過",
            "無效防守略過", "MACD armed", "MACD 出場", "停損出場／同日優先",
            "低 regime stop inactive 日", "低 regime stop 命中但抑制",
        ),
        ((
            labels[sid],
            sum(row["regime"] == "SMA21_GE_SMA144" for row in rows),
            sum(row["regime"] == "SMA21_LT_SMA144" for row in rows),
            sum(row["selected_entry_type"] == "MA21_BREAKOUT" for row in rows),
            sum(row["selected_entry_type"] == "MACD_GREEN_TO_RED" for row in rows),
            sum(row["entry_status"] == "FILLED" for row in rows),
            sum(row["entry_status"] == "SKIPPED_ALREADY_HOLDING" for row in rows),
            sum(row["entry_status"] == "SKIPPED_SAME_DAY_EXIT" for row in rows),
            sum(row["entry_status"] == "SKIPPED_INVALID_STOP" for row in rows),
            event_counts[sid]["MACD_ARMED"], event_counts[sid]["VALID_RED_TO_GREEN_EXIT"],
            event_counts[sid]["STRUCTURAL_STOP_EXIT"] + event_counts[sid]["INITIAL_STOP_EXIT"] + event_counts[sid]["STOP_PRIORITY_OVER_MACD"],
            event_counts[sid]["STOP_ENFORCEMENT_INACTIVE_LOW_REGIME"],
            event_counts[sid]["STOP_EXIT_SUPPRESSED_LOW_REGIME"],
        ) for sid, rows in by_strategy.items()),
    )


def _reconcile(
    *, metrics: Sequence[Mapping[str, str]], annual: Sequence[Mapping[str, Any]],
    details: Sequence[Mapping[str, str]], transactions: Sequence[Mapping[str, str]],
    curves: Sequence[Mapping[str, str]], actions: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    by_annual = defaultdict(list)
    by_curve = defaultdict(list)
    by_tx = defaultdict(list)
    by_detail = defaultdict(list)
    by_action = defaultdict(list)
    for row in annual: by_annual[row["strategy_id"]].append(row)
    for row in curves: by_curve[row["strategy_id"]].append(row)
    for row in transactions: by_tx[row["strategy_id"]].append(row)
    for row in details: by_detail[row["strategy_id"]].append(row)
    for row in actions: by_action[row["strategy_id"]].append(row)
    metric_map = {row["strategy_id"]: row for row in metrics}
    for sid in runner.METHOD_ORDER:
        metric = metric_map[sid]
        years = sorted(by_annual[sid], key=lambda row: int(row["year"]))
        curves_sid = sorted(by_curve[sid], key=lambda row: row["date"])
        tx = by_tx[sid]
        det = by_detail[sid]
        acts = by_action[sid]
        entries = sum(row["role"] in {"MOTHER", "首次買進"} for row in tx)
        exits = sum(row["role"] == "EXIT" for row in tx)
        buy_total = sum((standard.dec(row["cash_amount"]) for row in tx if row["role"] != "EXIT"), Decimal("0"))
        fees_tax = sum((standard.dec(row.get("commission")) + standard.dec(row.get("tax")) for row in tx), Decimal("0"))
        slippage = sum((abs(standard.dec(row["fill_price"]) - standard.dec(row["reference_price"])) * int(row["shares"]) for row in tx), Decimal("0"))
        realized = sum((standard.dec(row["net_pnl_or_estimate"]) for row in det if row["status_zh"] == "已賣出"), Decimal("0"))
        estimated = sum((standard.dec(row["net_pnl_or_estimate"]) for row in det if row["status_zh"] != "已賣出"), Decimal("0"))
        dividends = sum((standard.dec(row["cash"]) for row in acts if row.get("action_kind") == "DIVIDEND" and row.get("stage") == "CASH_PAID"), Decimal("0"))
        peak = max(standard.dec(row["peak_position_cost"]) for row in years)
        scalar = {
            "ending_equity": standard.dec(curves_sid[-1]["equity"]) == standard.dec(metric["ending_equity"]),
            "cumulative_buy": buy_total == standard.dec(metric["cumulative_buy"]),
            "peak_investment": peak == standard.dec(metric["peak_concurrent_investment"]),
            "realized_pnl": realized == standard.dec(metric["realized_net_pnl"]),
            "cutoff_estimated_pnl": estimated == standard.dec(metric["cutoff_estimated_pnl"]),
            "dividends": dividends == standard.dec(metric["dividends_received"]),
            "fees_tax": fees_tax == standard.dec(metric["actual_fees_and_tax"]),
            "slippage": slippage == standard.dec(metric["actual_slippage_cost"]),
            "entries": entries == int(metric["closed_rounds"]) + int(metric["open_rounds"]),
            "exits": exits == int(metric["closed_rounds"]),
        }
        if not all(scalar.values()):
            raise standard.StandardReportError(f"summary reconciliation failed: {sid}: {scalar}")
        continuity = True
        carry = True
        prior = standard.INITIAL_CAPITAL
        for year_row in years:
            year = int(year_row["year"])
            last = [row for row in curves_sid if date.fromisoformat(row["date"]).year == year][-1]
            continuity &= standard.dec(year_row["starting_equity"]) == prior
            continuity &= standard.dec(year_row["ending_equity"]) == standard.dec(last["equity"])
            carry &= int(year_row["ending_open_positions"]) == (1 if int(last["shares"]) > 0 else 0)
            prior = standard.dec(year_row["ending_equity"])
        if not continuity or not carry:
            raise standard.StandardReportError(f"annual/carry reconciliation failed: {sid}")
        checks.append({
            "strategy_id": sid, "summary_checks": scalar,
            "annual_rows": len(years), "annual_continuity": continuity,
            "carry_position_matches_last_daily_shares": carry,
        })
    return {
        "status": "PASS", "strategy_checks": checks,
        "carry_definition": "On each calendar year's final official session (2026: cutoff 09-30), 1 iff daily-equity shares > 0; otherwise 0. It is not a count of entries during the year.",
    }


def _definitions(*, dynamic_exit: bool = False) -> list[str]:
    new_moving = (
        "6. **分流移動版**：互斥 regime/entry cross；持倉時逐日依 regime 切換出場。SMA21<SMA144 只允許 armed 後 MACD 紅翻綠，移動道氏 stop 僅維護不執行；SMA21≥SMA144 恢復移動道氏防線優先、否則 MACD。"
        if dynamic_exit else
        "6. **分流移動版**：當日 SMA21≥SMA144 只採 MA21 新突破；SMA21<SMA144 只採前日 histogram≤0、當日>0 的 MACD 綠翻紅；使用移動道氏防線。"
    )
    new_fixed = (
        "7. **分流固定版**：相同互斥進場與逐日 regime 切換。SMA21<SMA144 只允許 MACD，固定初始防守僅維護不執行；SMA21≥SMA144 恢復固定防守優先、否則 MACD。"
        if dynamic_exit else
        "7. **分流固定版**：相同互斥 regime/entry cross；使用固定初始防守，不啟動道氏追蹤。"
    )
    return [
        "1. **基本版－長線**：沿用已驗收的長線雙均線策略。",
        "2. **原移動版**：前日 technical close≤SMA21、當日 technical close>SMA21 才進場；移動道氏防線優先，否則 MACD(21,55,55) 柱狀值非負轉負出場。",
        "3. **原固定版**：同一 MA21 新突破進場；進場時固定初始防守，持有中不上移；防守優先，否則 MACD 紅翻綠出場。",
        "4. **0050 全程持有（股利留現金）**。",
        "5. **0050 全程持有（股利再投入）**。",
        new_moving,
        new_fixed,
        "共同規則：MACD 綠翻紅進場當日即 armed；其後等下一次 histogram≥0 轉 <0；停損與 MACD 同日成立時停損優先；已有持股不加碼；賣出當日不重進；截止仍持有不強平。",
        "成交與資金：50 萬元單一連續帳戶，不逐年重設；主文採買賣各 20bps 滑價，另扣手續費與賣出證交稅；0/10/20bps 僅為敏感度分析，0bps 不可實現。",
        "股息與分割：股息依除息資格與發放日入帳；股份／價格因子在公司行動日等比例換算，分割不當成暴跌、停損或交易。",
    ]


def _named_dynamic_exit_audit(event_rows: Sequence[Mapping[str, str]]) -> list[str]:
    labels = {regime.MOVING_ID: "分流移動版", regime.FIXED_ID: "分流固定版"}
    lines: list[str] = []
    for entry_day in ("2011-12-26", "2012-07-03"):
        parts: list[str] = []
        for sid, label in labels.items():
            entry = next((row for row in event_rows if row["strategy_id"] == sid and row["date"] == entry_day and row["event"] == "ENTRY"), None)
            if entry is None:
                parts.append(f"{label}：未找到 ENTRY")
                continue
            leg = entry["leg_id"]
            leg_rows = [row for row in event_rows if row["strategy_id"] == sid and row.get("leg_id") == leg]
            suppressed = [row for row in leg_rows if row["event"] == "STOP_EXIT_SUPPRESSED_LOW_REGIME"]
            exit_row = next((row for row in leg_rows if row["event"] in {"VALID_RED_TO_GREEN_EXIT", "STOP_PRIORITY_OVER_MACD", "STRUCTURAL_STOP_EXIT", "INITIAL_STOP_EXIT"}), None)
            exit_text = "截止仍持有" if exit_row is None else f"{exit_row['date']} {exit_row['event']}"
            parts.append(f"{label} {leg}：低 regime 價格 stop 抑制 {len(suppressed)} 日，最終 {exit_text}")
        lines.append(f"- **{entry_day} 進場回合**：" + "；".join(parts) + "。")
    return lines


def _lineage_text(*, dynamic_exit: bool, source_run_name: str) -> str:
    if not dynamic_exit:
        return (
            "狀態：`STOP_WAIT_00_QA`。本版只重製報告；唯一數據來源是已通過 00 QA "
            "的 sealed v1-3 run，沒有重跑策略或修改數據。"
        )
    return (
        "狀態：`STOP_WAIT_00_QA`。本研究版本已修改兩個 MA21/MA144 分流策略的每日 regime "
        f"出場規則，並在來源 run `{source_run_name}` 正式重播；本報告建置只讀取該已完成 "
        "run，渲染階段沒有再次重播策略。原五組由已驗收 sealed v1-3 run 承接，10 份核心 "
        "CSV 已逐位元 `PASS_EXACT`；sealed v1-3 並不是兩個新策略數值的來源。"
    )


def _render(
    *, summaries: Sequence[Mapping[str, Any]], annual: Sequence[Mapping[str, Any]],
    details: Sequence[Mapping[str, str]], market: Mapping[str, Any],
    regime_rows: Sequence[Mapping[str, str]], event_rows: Sequence[Mapping[str, str]],
    produced: str, version_label: str = "v1.4", dynamic_exit: bool = False,
    source_run_name: str = "02-etf-0050-2006-2026-seven-methods-v1-3",
) -> tuple[str, str, str, str]:
    timing = [row for row in summaries if row["method_kind"] == "擇時策略"]
    controls = [row for row in summaries if row["method_kind"] == "全程持有基準"]
    best = max(timing, key=lambda row: standard.dec(row["ending_equity"]))
    index = "\n".join([
        f"# 0050 2006～2026/09 七組比較｜完整報告版 {version_label}", "", f"產出時間：{produced}", "",
        _lineage_text(dynamic_exit=dynamic_exit, source_run_name=source_run_name), "",
        "## 七組策略完整定義", "", *_definitions(dynamic_exit=dynamic_exit), "",
        "## 完整附件", "",
        "- [完整主報告](report.md)", "- [完整共享帳戶報告](shared-account-report.md)",
        "- [基準、風險與行情分段](benchmark-risk-report.md)",
        "- [逐筆交易易讀版](trade-details.md)", "- [逐筆交易 CSV](trade-details.csv)",
        "- [全部交易流水](transactions.csv)", "- [每日資產](daily-equity.csv)",
        "- [逐年摘要](calendar-year-summary.csv)", "- [20bps 全期摘要](scenario-summary.csv)",
        "- [0/10/20bps 全情境摘要](all-scenarios-summary.csv)",
        "- [全情境逐筆](all-scenarios-trade-details.csv)", "- [全情境交易流水](all-scenarios-transactions.csv)",
        "- [全情境每日資產](all-scenarios-daily-equity.csv)", "- [全情境公司行動](all-scenarios-corporate-actions.csv)",
        "- [MA21 機會稽核](signal-opportunity-audit.csv)", "- [新策略 regime/entry 稽核](new-strategy-regime-entry-audit.csv)",
        "- [MACD armed/cross 稽核](new-strategy-macd-armed-cross-audit.csv)",
        "- [stop/exit priority 稽核](new-strategy-stop-exit-priority-audit.csv)",
        *( ["- [動態 regime 出場稽核](dynamic-regime-exit-audit.csv)"] if dynamic_exit else [] ),
        "- [完整 MACD 事件](macd-event-audit.csv)", "- [公司行動](corporate-actions.csv)",
        "- [benchmark](benchmark.csv)", "- [資產曲線](equity-curves.svg)", "- [回落曲線](drawdown-curves.svg)",
        "- [展示值對帳](report-value-reconciliation.json)", "- [輸入與方法](input-manifest.json)",
        "- [產物 SHA manifest](artifact-manifest.json)", "- [readback](readback.json)",
        "- [原五組 parity](report-parity.json)", "- [QA checkpoint](checkpoint.json)", "",
    ])
    main = "\n".join([
        "# 0050 七組比較｜完整主報告", "", f"產出時間：{produced}", "",
        "## 來源譜系", "", _lineage_text(dynamic_exit=dynamic_exit, source_run_name=source_run_name), "",
        "![七組方法與大盤每日總資產](equity-curves.svg)", "", "## 七組策略完整定義", "", *_definitions(dynamic_exit=dynamic_exit), "",
        "## 主摘要（20bps）", "",
        f"五個擇時策略中期末資產最高為「{best['strategy_label']}」：{standard.money(best['ending_equity'])} 元。這是歷史樣本內結果，不等於樣本外已證明正期望。", "",
        _overall_full([*timing, *controls]), "", "表中『進場／出場／未平倉』是完整帳戶事件數；最大投入是單一時點最高持倉成本，不是新增本金。", "",
        "## 資金、交易與損益（20bps）", "", _capital_full(summaries), "",
        "以上欄位由 sealed run 的 scenario-summary、transactions、trade-details 與 corporate-actions 重建；逐項對帳結果見 [展示值對帳](report-value-reconciliation.json)。", "",
        "## 完整逐年表", "", _annual_full(annual), "",
        "『年底仍持有』定義：該曆年最後一個官方交易日的 daily-equity `shares>0` 記為 1，否則 0；它不是該年度進場次數。2026 使用 9 月 30 日截止狀態。147 列均已和每日曲線最後一列對帳。", "",
        "## 2022 空頭與 2025～2026/09 多頭", "", _market_period(annual), "",
        "## 獲利集中度", "", _concentration(summaries), "",
        "後段占比只在全期總獲利為正且不接近零時呈現；若分母為負或接近零則標示不適用，避免過度解讀。", "",
        "## 訊號統計與異常摘要", "", _signal_summary(regime_rows, event_rows), "",
        "- 兩組新策略各有 5,093 個正式交易日，regime 互斥且 SMA21=SMA144 歸 `SMA21_GE_SMA144`。",
        "- 2011-10-14 兩組各有一次 MACD 綠翻紅候選，但候選防守價等於進場價，因此依既有 sizing 約束留痕、不進場。",
        *( _named_dynamic_exit_audit(event_rows) if dynamic_exit else [] ),
        "- 詳細逐日值不在主報告展開，請使用 regime/entry、MACD armed/cross、stop/exit priority 三份附件。", "",
        "## 資料、因果與方法限制", "",
        "- 2005 年只作暖機，正式績效自 2006-01-02 起算；2026 只到 9 月 30 日。",
        "- 2005～2009 每日價採 FinMind 公開資料；TWSE 第一方僅逐月高低／量／額驗證，不能宣稱逐日雙源。",
        "- 技術指標使用逐日因果、公司行動連續的技術座標；成交、現金與估值使用當日原始價格。",
        "- 這是單一歷史樣本；需另做樣本外、替代期間與執行偏差檢查，才可加強正期望判斷。", "",
    ])
    risk = "\n".join([
        "# 0050 七組比較｜基準、風險與行情分段", "", f"產出時間：{produced}", "",
        "![七組方法與大盤每日資產回落](drawdown-curves.svg)", "",
        f"加權報酬指數等值 50 萬元期末 {standard.money(market['end_value'])} 元；累積 {standard.pct(market['cumulative_return'])}；年化 {standard.pct(market['annualized_return'])}；最大回落 {standard.pct(market['max_drawdown'])}。", "",
        "## 全期風險", "", _overall_full(summaries), "",
        "## 2022、2025、2026/09 與大盤", "", _market_period(annual), "",
        "## 後段獲利集中度", "", _concentration(summaries), "",
        "不能只以期末資產判斷；最大回落、資金閒置、成本敏感度、後段獲利占比與樣本外穩定性必須一起看。", "",
    ])
    trades = compact._trade_report(summaries, details, produced)
    return index, main, risk, trades


def build(
    *, source_root: Path = SOURCE_ROOT, output_root: Path = OUTPUT_ROOT_DEFAULT,
    expected_run_manifest: str = EXPECTED_RUN_MANIFEST,
    sealed_report_root: Path = SEALED_REPORT_ROOT,
    expected_sealed_report_manifest: str = EXPECTED_REPORT_MANIFEST,
    report_version: str = REPORT_VERSION, version_label: str = "v1.4",
    source_run_strategy_replay_performed: bool = False,
    source_run_data_or_strategy_changed: bool = False,
) -> dict[str, Any]:
    if output_root.exists():
        raise standard.StandardReportError(f"refusing to overwrite versioned report: {output_root}")
    if standard.sha256(source_root / "manifest.json").upper() != expected_run_manifest:
        raise standard.StandardReportError("source run manifest pin changed")
    if standard.sha256(sealed_report_root / "artifact-manifest.json").upper() != expected_sealed_report_manifest:
        raise standard.StandardReportError("sealed predecessor report manifest pin changed")
    protected_before = {"run": tree_hash(source_root), "report": tree_hash(sealed_report_root)}
    source_check = standard.verify_source(source_root, "QA_READY.json")
    metrics, details = _read(source_root / "scenario-summary.csv"), _read(source_root / "trade-details.csv")
    transactions, curves = _read(source_root / "transactions.csv"), _read(source_root / "daily-equity.csv")
    actions, benchmark = _read(source_root / "corporate-actions.csv"), _read(source_root / "benchmark.csv")
    regime_rows = _read(source_root / "new-strategy-regime-entry-audit.csv")
    event_rows = _read(source_root / "macd-event-audit.csv")
    labels = {row["strategy_id"]: row["strategy_label"] for row in metrics}
    previous = (standard.YEARS, standard.METHOD_ORDER, standard.TIMING_PREFIXES, dict(standard.COLORS), dict(standard.EXIT_REASON_ZH))
    standard.YEARS, standard.METHOD_ORDER = accepted.YEARS, runner.METHOD_ORDER
    standard.TIMING_PREFIXES, standard.COLORS = ("A_", "MA21_"), dict(compact.COLORS)
    standard.EXIT_REASON_ZH.update(compact.EXIT_ZH)
    try:
        annual = standard.annual_summaries(curves, transactions, details, actions, benchmark, labels)
        summaries = standard.overall_summaries(metrics, annual)
        daily = standard.comparison_daily(curves, benchmark, labels)
        market = standard.curve_metrics(
            [(date.fromisoformat(row["date"]), standard.dec(row["equivalent_500k"])) for row in benchmark],
            standard.INITIAL_CAPITAL,
        )
        reconciliation = _reconcile(
            metrics=metrics, annual=annual, details=details,
            transactions=transactions, curves=curves, actions=actions,
        )
        iso_time, produced = _stamp()
        dynamic_exit = version_label != "v1.4"
        index, main, risk, trades = _render(
            summaries=summaries, annual=annual, details=details, market=market,
            regime_rows=regime_rows, event_rows=event_rows, produced=produced,
            version_label=version_label, dynamic_exit=dynamic_exit,
            source_run_name=source_root.name,
        )
        parity = json.loads((source_root / "details" / "accepted-five-method-parity.json").read_text(encoding="utf-8-sig"))
        git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], text=True, encoding="utf-8").strip()
        with tempfile.TemporaryDirectory(prefix="0050-seven-complete-report-v1-4-") as temp_name:
            stage = Path(temp_name)
            for name, text in (("README.md", index), ("report.md", main), ("shared-account-report.md", main), ("benchmark-risk-report.md", risk), ("trade-details.md", trades)):
                (stage / name).write_text(text, encoding="utf-8", newline="\n")
            standard.write_csv(stage / "scenario-summary.csv", summaries)
            standard.write_csv(stage / "calendar-year-summary.csv", annual)
            standard.write_csv(stage / "daily-equity.csv", daily)
            for name in (
                "trade-details.csv", "transactions.csv", "corporate-actions.csv", "benchmark.csv",
                "signal-opportunity-audit.csv", "new-strategy-regime-entry-audit.csv",
                "new-strategy-macd-armed-cross-audit.csv", "new-strategy-stop-exit-priority-audit.csv",
                "macd-event-audit.csv",
            ):
                shutil.copy2(source_root / name, stage / name)
            if (source_root / "dynamic-regime-exit-audit.csv").is_file():
                shutil.copy2(source_root / "dynamic-regime-exit-audit.csv", stage / "dynamic-regime-exit-audit.csv")
            for source, target in {
                "all_scenarios_metrics.csv": "all-scenarios-summary.csv",
                "all_scenarios_trade_details.csv": "all-scenarios-trade-details.csv",
                "all_scenarios_transactions.csv": "all-scenarios-transactions.csv",
                "all_scenarios_daily_equity.csv": "all-scenarios-daily-equity.csv",
                "all_scenarios_corporate_actions.csv": "all-scenarios-corporate-actions.csv",
            }.items():
                shutil.copy2(source_root / "details" / source, stage / target)
            standard.svg_chart(stage / "equity-curves.svg", daily, value_field="equity", title="0050：七組方法與大盤每日總資產")
            standard.svg_chart(stage / "drawdown-curves.svg", daily, value_field="drawdown", title="0050：七組方法與大盤每日資產回落")
            standard.write_json(stage / "report-parity.json", parity)
            standard.write_json(stage / "report-value-reconciliation.json", reconciliation)
            protected_after = {"run": tree_hash(source_root), "report": tree_hash(sealed_report_root)}
            if protected_before != protected_after:
                raise standard.StandardReportError("pinned source run or predecessor report changed during report build")
            standard.write_json(stage / "input-manifest.json", {
                "status": "PASS", "report_version": report_version,
                "produced_at_taipei": iso_time, "builder_git_commit": git_commit,
                "source_root": source_root, "source_check": source_check,
                "sealed_run_manifest_sha256": expected_run_manifest,
                "sealed_report_manifest_sha256": expected_sealed_report_manifest,
                "sealed_tree_hashes_before": protected_before,
                "sealed_tree_hashes_after": protected_after,
                "source_run_strategy_replay_performed": source_run_strategy_replay_performed,
                "source_run_data_or_strategy_changed": source_run_data_or_strategy_changed,
                "report_render_strategy_replay_performed": False,
                "report_render_data_or_strategy_changed": False,
                "source_run_lineage": (
                    "Two MA21/MA144 regime strategies changed and were replayed in this source run; "
                    "the original five methods are inherited from sealed v1-3 and verified by ten exact CSV parity checks."
                    if dynamic_exit else
                    "No strategy or data change; report-only rendering from the accepted sealed v1-3 run."
                ),
                "main_slippage_bps": 20, "sensitivity_bps": [0, 10, 20],
                "source_limitation_2005_2009": "FinMind public daily; TWSE monthly high/low/volume/amount validation, not daily dual-source",
            })
            for planned in ("artifact-manifest.json", "readback.json", "checkpoint.json"):
                standard.write_json(stage / planned, {"status": "BUILDING"})
            for svg in (stage / "equity-curves.svg", stage / "drawdown-curves.svg"):
                ET.parse(svg)
            broken: list[str] = []
            for md in stage.glob("*.md"):
                for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", md.read_text(encoding="utf-8")):
                    if not (stage / target).is_file():
                        broken.append(f"{md.name}->{target}")
            if broken:
                raise standard.StandardReportError(f"broken Markdown links: {broken}")
            excluded = {"artifact-manifest.json", "readback.json", "checkpoint.json"}
            artifacts = standard.artifact_rows(stage, excluded)
            standard.write_json(stage / "artifact-manifest.json", {
                "status": "STOP_WAIT_00_QA", "report_version": report_version,
                "produced_at_taipei": iso_time, "artifact_count": len(artifacts),
                "annual_rows": len(annual), "daily_rows": len(daily), "trade_rows": len(details),
                "source_run_manifest_sha256": expected_run_manifest,
                "source_run_strategy_replay_performed": source_run_strategy_replay_performed,
                "source_run_data_or_strategy_changed": source_run_data_or_strategy_changed,
                "report_render_strategy_replay_performed": False,
                "artifacts": artifacts,
            })
            mismatches = [item["path"] for item in artifacts if standard.sha256(stage / item["path"]) != item["sha256"]]
            standard.write_json(stage / "readback.json", {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches})
            if mismatches:
                raise standard.StandardReportError(f"report readback failed: {mismatches}")
            standard.write_json(stage / "checkpoint.json", {
                "status": "STOP_WAIT_00_QA",
                "artifact_manifest_sha256": standard.sha256(stage / "artifact-manifest.json").upper(),
                "readback_sha256": standard.sha256(stage / "readback.json").upper(),
                "safe_next_step": "00 independent complete-report QA only; source strategy run is already complete and pinned",
            })
            output_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(stage, output_root)
        return {
            "status": "STOP_WAIT_00_QA", "output_root": str(output_root),
            "artifact_manifest_sha256": standard.sha256(output_root / "artifact-manifest.json").upper(),
            "readback_sha256": standard.sha256(output_root / "readback.json").upper(),
            "report_artifacts": len(artifacts), "annual_rows": len(annual),
            "pinned_source_run_and_predecessor_report_unchanged": True,
        }
    finally:
        standard.YEARS, standard.METHOD_ORDER, standard.TIMING_PREFIXES, standard.COLORS, exit_map = previous
        standard.EXIT_REASON_ZH.clear()
        standard.EXIT_REASON_ZH.update(exit_map)


def validate(
    *, output_root: Path = OUTPUT_ROOT_DEFAULT, source_root: Path = SOURCE_ROOT,
    expected_run_manifest: str = EXPECTED_RUN_MANIFEST,
    sealed_report_root: Path = SEALED_REPORT_ROOT,
    expected_sealed_report_manifest: str = EXPECTED_REPORT_MANIFEST,
) -> dict[str, Any]:
    manifest = json.loads((output_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
    mismatches = [
        item["path"] for item in manifest["artifacts"]
        if not (output_root / item["path"]).is_file()
        or (output_root / item["path"]).stat().st_size != item["bytes"]
        or standard.sha256(output_root / item["path"]) != item["sha256"]
    ]
    broken: list[str] = []
    for md in output_root.glob("*.md"):
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", md.read_text(encoding="utf-8")):
            if not (output_root / target).is_file():
                broken.append(f"{md.name}->{target}")
    inputs = json.loads((output_root / "input-manifest.json").read_text(encoding="utf-8-sig"))
    sealed_hashes_ok = (
        standard.sha256(source_root / "manifest.json").upper() == expected_run_manifest
        and standard.sha256(sealed_report_root / "artifact-manifest.json").upper() == expected_sealed_report_manifest
    )
    sealed_trees_ok = {
        "run": tree_hash(source_root), "report": tree_hash(sealed_report_root),
    } == inputs["sealed_tree_hashes_after"]
    reconciliation = json.loads((output_root / "report-value-reconciliation.json").read_text(encoding="utf-8-sig"))
    status = "PASS" if not mismatches and not broken and sealed_hashes_ok and sealed_trees_ok and reconciliation["status"] == "PASS" else "FAIL"
    return {
        "status": status, "artifact_count": len(manifest["artifacts"]),
        "artifact_mismatches": mismatches, "broken_links": broken,
        "sealed_manifest_hashes_unchanged": sealed_hashes_ok,
        "sealed_tree_hashes_unchanged": sealed_trees_ok,
        "value_reconciliation": reconciliation["status"],
        "annual_rows": manifest["annual_rows"], "daily_rows": manifest["daily_rows"],
        "network_used": False, "writes_performed": False,
    }
