"""Publish the Traditional-Chinese v1.3 presentation of the accepted 0050 final strategy.

This module is report-only.  It reads the sealed v1.2 package, preserves every
inherited data artifact byte-for-byte, and adds independently recomputable
Chinese presentation tables.  It never replays or changes strategy logic.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_final_strategy_v1 as v12
from . import etf_0050_standard_report_v3 as standard
from .etf_0050_base_v1 import ETF0050Error, tree_hash
from .etf_0050_formal_v2 import _artifact_rows, _verify_artifacts, _write_csv, _write_json
from .paths import dev_run, report_path


VERSION = "dual-ma-0050-final-strategy-v1.3-r3"
SOURCE_RUN = dev_run("02-etf-0050-final-strategy-v1-2")
SOURCE_REPORT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-2")
SOURCE_RUN_MANIFEST = "1ACA38F71BABF747D135A1327C871EBFDDB028F7855A70AE8F218B4EDAFAA30D"
SOURCE_REPORT_MANIFEST = "335F9446B4093B9696B258CA1DD8B77CF6BB6053F6BBF19ADF0C5C650B720E3D"
SOURCE_READBACK = "366FDCC7BB4C554F467C0942E9B85C652F7C37B82A070E1C3878634586751C94"
RUN_ROOT_DEFAULT = dev_run("02-etf-0050-final-strategy-v1-3-r3")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-3-r3")

NAMES = {
    v12.SIGNAL_CLOSE_ID: "最終策略－訊號當日收盤成交",
    v12.NEXT_OPEN_ID: "最終策略－次一交易日開盤成交",
    v12.BUY_HOLD_CASH_ID: "0050 買進持有－股息保留現金",
    v12.BUY_HOLD_REINVEST_ID: "0050 買進持有－股息再投入",
    v12.BENCHMARK_ID: "臺灣加權股價報酬指數換算",
}
EXECUTION_NAMES = {
    v12.SIGNAL_CLOSE_ID: "訊號當日收盤成交",
    v12.NEXT_OPEN_ID: "次一交易日開盤成交",
    v12.BUY_HOLD_CASH_ID: "首日開盤買進後全程持有",
    v12.BUY_HOLD_REINVEST_ID: "首日開盤買進；股息於付款日收盤再投入",
    v12.BENCHMARK_ID: "指數收盤換算（不可直接交易）",
}
MAIN_HEADERS = (
    "策略／比較基準", "成交方式", "期末資產", "總報酬", "年化報酬率", "最大回落",
    "獲利年度", "虧損年度", "平盤年度", "進場／出場／未平倉", "最大投入",
)
ANNUAL_HEADERS = (
    "策略", "年度", "年初資產", "年末／截止資產", "策略報酬", "策略最大回落",
    "大盤報酬", "大盤最大回落", "進場", "出場", "年底／截止仍持有",
)


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _filter(rows: Sequence[Mapping[str, Any]], **expected: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if all(str(row.get(key)) == str(value) for key, value in expected.items())]


def _year_counts(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int, int]:
    values = [_d(row["return"]) for row in rows]
    return sum(x > 0 for x in values), sum(x < 0 for x in values), sum(x == 0 for x in values)


def build_performance_summary(
    summary: Sequence[Mapping[str, Any]], annual: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {row["comparison_id"]: row for row in summary}
    output = []
    for comparison_id in v12.MAIN_IDS:
        source = by_id[comparison_id]
        positive, negative, flat = _year_counts(_filter(annual, comparison_id=comparison_id))
        counts = "—" if comparison_id == v12.BENCHMARK_ID else "/".join(
            str(source.get(key) or 0) for key in ("rounds", "closed_rounds", "open_rounds")
        )
        output.append({
            "comparison_id": comparison_id,
            "策略／比較基準": NAMES[comparison_id], "成交方式": EXECUTION_NAMES[comparison_id],
            "期末資產": source["ending_equity"], "總報酬": source["total_return"],
            "年化報酬率": source["cagr"], "最大回落": source["max_drawdown"],
            "獲利年度": positive, "虧損年度": negative, "平盤年度": flat,
            "進場／出場／未平倉": counts,
            "最大投入": source.get("peak_concurrent_investment") or None,
        })
    return output


def build_relative_differences(summary: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_id = {row["comparison_id"]: row for row in summary}
    output = []
    for strategy_id in (v12.SIGNAL_CLOSE_ID, v12.NEXT_OPEN_ID):
        strategy = by_id[strategy_id]
        row: dict[str, Any] = {"comparison_id": strategy_id, "策略": NAMES[strategy_id]}
        for prefix, baseline_id in (("相較大盤", v12.BENCHMARK_ID), ("相較0050股息再投入", v12.BUY_HOLD_REINVEST_ID)):
            baseline = by_id[baseline_id]
            row[f"{prefix}期末資產差異"] = _d(strategy["ending_equity"]) - _d(baseline["ending_equity"])
            row[f"{prefix}年化報酬率差異"] = _d(strategy["cagr"]) - _d(baseline["cagr"])
            row[f"{prefix}最大回落改善"] = _d(strategy["max_drawdown"]) - _d(baseline["max_drawdown"])
        output.append(row)
    return output


def build_complete_annual(
    annual: Sequence[Mapping[str, Any]], daily: Sequence[Mapping[str, Any]],
    strategy_tx: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    market = {row["year"]: row for row in _filter(annual, comparison_id=v12.BENCHMARK_ID)}
    daily_by_id_year: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in daily:
        daily_by_id_year[(str(row["comparison_id"]), str(row["date"])[:4])].append(row)
    tx_map = {v12.SIGNAL_CLOSE_ID: "SIGNAL_CLOSE", v12.NEXT_OPEN_ID: "NEXT_SESSION_OPEN"}
    output = []
    for comparison_id in v12.MAIN_IDS:
        for row in _filter(annual, comparison_id=comparison_id):
            year = str(row["year"])
            market_row = market[year]
            if comparison_id in tx_map:
                transactions = [tx for tx in strategy_tx if tx["execution_timing"] == tx_map[comparison_id] and tx["date"].startswith(year)]
                entries = sum(tx["role"] == "MOTHER" for tx in transactions)
                exits = sum(tx["role"] == "EXIT" for tx in transactions)
                last = sorted(daily_by_id_year[(comparison_id, year)], key=lambda item: item["date"])[-1]
                open_count: Any = 1 if int(last.get("shares") or 0) > 0 else 0
            elif comparison_id in {v12.BUY_HOLD_CASH_ID, v12.BUY_HOLD_REINVEST_ID}:
                entries, exits, open_count = (1 if year == "2006" else 0), 0, 1
            else:
                entries = exits = open_count = None
            output.append({
                "comparison_id": comparison_id, "策略": NAMES[comparison_id], "年度": year,
                "年初資產": row["starting_equity"], "年末／截止資產": row["ending_equity"],
                "策略報酬": row["return"], "策略最大回落": row["max_drawdown"],
                "大盤報酬": market_row["return"], "大盤最大回落": market_row["max_drawdown"],
                "進場": entries, "出場": exits, "年底／截止仍持有": open_count,
            })
    return output


def _money(value: Any) -> str:
    return "—" if value in (None, "") else standard.money(value)


def _pct(value: Any) -> str:
    return "—" if value in (None, "") else standard.pct(value)


def _pp(value: Any) -> str:
    return f"{_d(value) * 100:+.2f} 個百分點"


def _parameter_value(metric: str, value: Any) -> str:
    if metric == "ending_equity":
        return _money(value)
    if metric == "calmar":
        return f"{_d(value):.3f}"
    return _pct(value)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    return standard.md_table(tuple(headers), rows)


def _study(title: str, purpose: str, method: str, result: str, plain: str, impact: str) -> str:
    return "\n".join([
        f"## {title}", "", "### 測試目的", "", purpose, "", "### 測試方法", "", method, "",
        "### 觀察結果", "", result, "", "### 白話結論", "", plain, "",
        "### 對最終決策的影響", "", impact, "",
    ])


def _render_report(
    root: Path, performance: Sequence[Mapping[str, Any]], differences: Sequence[Mapping[str, Any]],
    annual: Sequence[Mapping[str, Any]], source: Mapping[str, Sequence[Mapping[str, Any]]],
    produced: str, attachments: Sequence[str],
) -> None:
    perf_table = _table(MAIN_HEADERS, [[
        row["策略／比較基準"], row["成交方式"], _money(row["期末資產"]), _pct(row["總報酬"]),
        _pct(row["年化報酬率"]), _pct(row["最大回落"]), row["獲利年度"], row["虧損年度"],
        row["平盤年度"], row["進場／出場／未平倉"], _money(row["最大投入"]),
    ] for row in performance])
    diff_table = _table((
        "策略", "相較大盤期末資產差異", "相較大盤年化報酬率差異", "相較大盤最大回落改善",
        "相較0050股息再投入期末資產差異", "相較0050股息再投入年化報酬率差異",
        "相較0050股息再投入最大回落改善",
    ), [[
        row["策略"], _money(row["相較大盤期末資產差異"]), _pp(row["相較大盤年化報酬率差異"]),
        _pp(row["相較大盤最大回落改善"]), _money(row["相較0050股息再投入期末資產差異"]),
        _pp(row["相較0050股息再投入年化報酬率差異"]), _pp(row["相較0050股息再投入最大回落改善"]),
    ] for row in differences])
    annual_table = _table(ANNUAL_HEADERS, [[
        row["策略"], row["年度"], _money(row["年初資產"]), _money(row["年末／截止資產"]),
        _pct(row["策略報酬"]), _pct(row["策略最大回落"]), _pct(row["大盤報酬"]),
        _pct(row["大盤最大回落"]), row["進場"] if row["進場"] is not None else "—",
        row["出場"] if row["出場"] is not None else "—",
        row["年底／截止仍持有"] if row["年底／截止仍持有"] is not None else "—",
    ] for row in annual])
    capital_table = _table(("策略／比較基準", "累計買進", "最大投入", "已實現淨損益", "股息", "費用與稅", "滑價成本"), [[
        NAMES[row["comparison_id"]], _money(row.get("cumulative_buy")), _money(row.get("peak_concurrent_investment")),
        _money(row.get("realized_net_pnl")), _money(row.get("dividends_received")),
        _money(row.get("fees_and_tax")), _money(row.get("slippage_cost")),
    ] for row in source["summary"]])
    timing_table = _table(("成交方式", "單邊滑價", "期末資產", "年化報酬率", "最大回落", "完成回合"), [[
        "訊號當日收盤成交" if row["execution_timing"] == "SIGNAL_CLOSE" else "次一交易日開盤成交",
        f"{row['slippage_bps']} 基點", _money(row["ending_equity"]), _pct(row["cagr"]),
        _pct(row["max_asset_drawdown"]), row["closed_rounds"],
    ] for row in source["cost"]])
    period_names = {"2006_2012": "2006～2012", "2013_2019": "2013～2019", "2020_2026M09": "2020～2026年9月"}
    period_table = _table(("期間", "期初資產", "期末資產", "年化報酬率", "最大回落"), [[
        period_names[row["period"]], _money(row["starting_equity"]), _money(row["ending_equity"]),
        _pct(row["cagr"]), _pct(row["max_drawdown"]),
    ] for row in source["periods"]])
    rolling_rows = []
    for window in (3, 5, 10):
        bucket = [row for row in source["rolling"] if int(row["window_years"]) == window]
        rolling_rows.append([f"{window} 年", len(bucket), _pct(median([_d(row["cagr"]) for row in bucket])),
                             _pct(min(_d(row["cagr"]) for row in bucket)), _pct(min(_d(row["max_drawdown"]) for row in bucket))])
    rolling_table = _table(("滾動期間", "觀測數", "年化報酬率中位數", "最差年化報酬率", "最差最大回落"), rolling_rows)
    regime_names = {"BULL": "多頭", "BEAR": "空頭", "SIDEWAYS": "盤整"}
    regime_table = _table(("市場狀態", "交易日報酬數", "策略複合報酬", "平均每日報酬"), [[
        regime_names[row["regime"]], row["session_returns"], _pct(row["compounded_strategy_return"]),
        _pct(row["average_daily_return"]),
    ] for row in source["regime"]])
    stable = source["stability"][0]
    concentration_table = _table(("移除最佳回合數", "期末資產", "年化報酬率", "最大回落", "相較原始期末差異"), [[
        row["removed_best_trade_count"], _money(row["ending_equity"]), _pct(row["cagr"]),
        _pct(row["max_drawdown"]), _money(row["ending_equity_change_vs_baseline"]),
    ] for row in source["concentration"]])
    metric_names = {"cagr": "年化報酬率", "ending_equity": "期末資產", "max_drawdown": "最大回落",
                    "worst_year": "最差年度報酬", "calmar": "年化報酬率／最大回落",
                    "rolling_5y_worst_cagr": "五年滾動期間最差年化報酬率"}
    parameter_table = _table(("指標", "組數", "最小值", "第10百分位", "中位數", "第90百分位", "最大值"), [[
        metric_names.get(row["metric"], row["metric"]), row["count"],
        *[_parameter_value(row["metric"], row[field]) for field in ("min", "p10", "median", "p90", "max")],
    ] for row in source["parameter_distribution"]])
    center = source["parameter_center"][0]
    neighborhood = source["parameter_neighborhood"][0]
    parameter_evidence = (
        f"中心參數 21／144 與 MACD(21,55,55)：期末資產 {_money(center['ending_equity'])} 元、"
        f"年化報酬率 {_pct(center['cagr'])}、最大回落 {_pct(center['max_drawdown'])}。"
        f"局部鄰域共 {neighborhood['neighbor_count']} 組，年化報酬率中位數 {_pct(neighborhood['neighborhood_median_cagr'])}，"
        f"範圍 {_pct(neighborhood['neighborhood_min_cagr'])}～{_pct(neighborhood['neighborhood_max_cagr'])}。"
    )
    allocation_table = _table(("配置", "期末資產", "年化報酬率", "最大回落", "再平衡次數", "再平衡成本"), [[
        f"核心持有 {row['core_weight_pct']}%／策略交易 {row['tactical_weight_pct']}%（{'不再平衡' if row['rebalance']=='NONE' else '每年再平衡'}）",
        _money(row["ending_equity"]), _pct(row["cagr"]), _pct(row["max_drawdown"]),
        row.get("rebalance_events") or "—", _money(row.get("rebalance_cost")),
    ] for row in source["allocations"]])
    links = "\n".join(f"- [{name}]({name})" for name in attachments)
    sections = [
        _study("一頁白話結論", "判斷固定規則在完整歷史樣本及既定成本後是否仍為正報酬。",
               "以次一交易日開盤成交作實務主口徑，同時保留訊號當日收盤成交作理想化研究對照。",
               "實務主口徑期末資產為 6,067,613.25 元，年化報酬率 12.78%，最大回落 -23.17%。",
               "本歷史樣本扣除既定成本後為正報酬，但不證明未來一定具有正期望。",
               "策略規則維持不變，後續應以實務主口徑執行並持續監控。"),
        _study("完整績效總表", "用同一本金、期間與估值方式比較五條正式比較線。",
               "統計 2006-01-02 至 2026-09-30 的連續帳戶，年度不重設。",
               perf_table, "股息再投入持有線的期末資產較高，但兩個策略版本的最大回落明顯較淺。",
               "最終決策不能只看期末資產，必須同時衡量回落、交易成本與執行可行性。"),
        _study("策略相對大盤與0050持有的差異", "直接量化策略相對兩個重要比較基準的得失。",
               "期末資產差異＝策略減比較基準；年化報酬率差異以百分點表示；最大回落改善＝策略最大回落減比較基準最大回落，正值代表回落較淺。",
               "註：差異以未四捨五入原值計算，表格顯示可能有 0.01 元或 0.01 個百分點的四捨五入差。\n\n" + diff_table,
               "兩個策略版本都以較低回落交換部分長期報酬。",
               "若優先考慮風險控制，策略仍具決策價值；若只追求樣本期末值，股息再投入持有較高。"),
        _study("策略規則與兩種成交版本", "消除規則與成交時點的歧義。",
               "以下規格集中列示，可單獨閱讀：\n\n"
               "- 本報告嚴格區分兩種價格座標：『技術分析用收盤價／最低價』會在分割時等比例重訂，只用於均線、訊號與防守判斷；『未還原開盤價／收盤價』是市場實際價格，只用於成交、股息再投入與未平倉清算帳務。\n"
               "- MA21 大於或等於 MA144（等號歸高市場狀態）；MA21 小於 MA144 為低市場狀態。持倉期間每天重新判定並動態切換市場狀態。\n"
               "- 高市場狀態進場：空手時，前一日技術分析用收盤價不高於前一日 MA21，且當日技術分析用收盤價高於當日 MA21。低市場狀態進場：MACD 柱狀值前一日小於或等於 0、當日大於 0。\n"
               "- 固定初始防守價取最近一段『技術分析用收盤價不高於 MA21』期間累積的技術分析用最低價、前一日技術分析用最低價與訊號日技術分析用最低價的可用最小值；必須嚴格低於進場參考價，進場後不上移。分割時，技術分析用價格與防守價座標等比例調整。\n"
               "- 高市場狀態中，當日技術分析用收盤價嚴格低於固定初始防守價時優先出場；低市場狀態只維護防守價而不執行。\n"
               "- 低市場狀態由 MACD 柱狀值綠翻紅進場時，出場條件立即啟用。MA21 突破進場時，若柱狀值大於或等於 0（含 0）便立即啟用；否則等待持有後首次大於或等於 0 才啟用。啟用後，只有前一日柱狀值大於或等於 0、當日小於 0 才以 MACD 出場。\n"
               "- 已持有時不加碼；賣出當日不重新進場；截止日仍持有時不強制平倉。\n"
               "- 訊號當日收盤版本的進場與出場都使用訊號當日收盤；次一交易日開盤版本的進場與出場都使用訊號後下一官方交易日開盤，不存在收盤進場、隔日開盤出場的混合第三版。\n"
               "- 單一連續帳戶初始本金 500,000 元，只買整股；每次以當時可用現金為預算上限，初始風險不得超過可用現金 20%；不借款、不加碼、不逐年重設。\n"
               "- 使用 2005 年資料暖機，正式期間為 2006-01-02 至 2026-09-30。買進與賣出各計 20 基點滑價；手續費率 0.0399%，每筆四捨五入至整元，並假設沒有最低手續費；ETF 賣出稅 0.1%。未平倉部位以當日未還原收盤價，扣除預估賣出手續費與稅後的清算值列入資產。\n"
               "- 股息資格在除息日依持股鎖定，付款日入帳；策略閒置現金於下一次合規進場投入。0050 買進持有股息再投入線採付款日收盤再投入，實務能否同日動用股息取決於券商入帳時點。2025-06-18 分割只調整股數與價格座標，不是交易，也不視為暴跌或停損。",
               "訊號當日收盤成交只作研究對照；次一交易日開盤成交才是正式執行與歸因口徑。",
               "所有訊號都在收盤後才能完整確認，因此實務不應把同日收盤成交當成可直接複製的結果。",
               "正式監控器應在盤後建立下一官方交易日開盤待執行單。"),
        _study("完整逐年績效", "檢查結果是否只由少數年度造成，並把策略與大盤逐年並排。",
               "沿用單一連續帳戶，每年年初資產等於前一年年末資產；同時統計進場、出場與年底持有。",
               annual_table, "策略存在虧損年度，正報酬並不是逐年穩定發生。",
               "年度不順時不得臨時改規則，應依既定監控條件評估是否仍在歷史合理範圍。"),
        _study("交易紀錄與資金投入", "確認帳戶成果可由實際投入、股息與成本完整解釋。",
               "初始本金 500,000 元；每次以當時可用現金及初始風險上限決定整股數，不借款、不加碼。手續費率為 0.0399%，ETF 賣出稅為 0.1%。股息依除息資格鎖定並在付款日入帳；股息再投入比較線的 31 筆交易以付款日未還原收盤價成交，實務能否同日動用股息仍取決於券商入帳時點。分割只調整價格與股數座標，不視為交易。",
               capital_table, "50 萬元是帳戶起始本金，不是每筆固定投入；獲利累積後單筆最大投入可高於 50 萬元。",
               "實務系統必須逐日保存現金、股數、清算值、股息權利與成本。"),
        _study("成交時間與交易成本測試", "評估延後成交與更差價格對績效的影響。",
               "固定原訊號，分別測兩種成交方式及買賣各 20、40、60 基點；20 基點等於 0.20%。",
               timing_table, "成本愈高，期末資產與年化報酬率愈低；實務結果應以次一交易日開盤 20 基點為主。",
               "若實際滑價長期超過 40 或 60 基點壓力帶，應暫停擴大部位並檢查執行品質。"),
        _study("不同時期與市場狀態測試", "確認績效是否只集中在單一年代或多頭環境。",
               "比較固定歷史區間、3／5／10 年滾動期間，以及以已完成歷史資訊分類的多頭、空頭與盤整。",
               period_table + "\n\n" + rolling_table + "\n\n" + regime_table,
               "長期平均為正不代表任何起點都順利；滾動最差值與市場狀態分解更接近真實持有感受。",
               "未來固定追蹤五年滾動結果、空頭表現與最長水下期間。"),
        _study("交易集中度與單筆穩定性", "確認成果是否過度依賴少數大贏家，並量化連虧與水下期。",
               "依完整回合計算連續虧損、最佳／最差交易與水下期；另固定原訊號移除最佳 1、3、5 回合。",
               f"最長連虧 {stable['longest_losing_streak']} 回合；最佳單筆 {_money(stable['best_trade_pnl'])} 元，最差單筆 {_money(stable['worst_trade_pnl'])} 元；最長水下 {stable['longest_underwater_days']} 日。\n\n{concentration_table}",
               "策略需要承受連虧與長期未創新高，也確實依賴部分大贏家。",
               "監控應警示連虧、水下期與單筆貢獻集中，但不能用短期結果隨意改參數。"),
        _study("243組參數穩健性", "檢查現行參數是否只是孤立的樣本內尖峰。",
               "完整比較 243 組均線與 MACD 參數，並檢查現行參數附近只改一格的局部結果。",
               parameter_evidence + "\n\n" + parameter_table,
               "現行設定附近仍有相近表現，結果並非只靠一個精確參數點成立。",
               "正式參數維持 21／144 與 MACD(21,55,55)；任何變更都要另做樣本外研究與品質驗收。"),
        _study("核心持有比例研究", "評估把部分資金長期持有 0050 對報酬與回落的影響。",
               "比較不同核心持有／策略交易比例，並分成不再平衡與每年再平衡。每個比例使用獨立子帳戶重建。",
               allocation_table, "提高核心持有比例在本樣本可能增加期末資產，也可能加深最大回落。",
               "核心比例屬資產配置決策，不改寫本策略規則；正式策略本身仍以全策略交易規則呈現。"),
        _study("最終實務建議與監控項目", "把研究結論轉成可執行的日常流程與停看條件。",
               "盤後計算訊號，次一官方交易日開盤執行；逐日記錄價格、均線、MACD 柱狀值、市場狀態、出場條件是否啟用、固定防守價、現金、股數、股息與分割。",
               "主要監控項目為資料缺日、非官方交易日成交、負現金、重複進場、實際滑價、五年滾動績效、連虧與水下期。",
               "策略可以進入後續執行與監控，但應視為可被新資料否證的工作假說。",
               "規則、參數或成交模型若要改動，必須另開研究版本，不得直接覆寫本定版。"),
    ]
    glossary = "；".join((
        "CAGR＝年化報酬率", "MaxDD＝最大回落", "SIGNAL_CLOSE＝訊號當日收盤成交",
        "NEXT_SESSION_OPEN＝次一交易日開盤成交", "BUY_HOLD＝買進後全程持有",
        "regime＝市場狀態／多空環境", "benchmark＝比較基準", "rolling＝滾動期間",
        "histogram＝MACD柱狀值", "armed＝出場條件已啟用", "raw close＝未還原收盤價",
        "bps＝基點（20基點＝0.20%）", "core/tactical＝核心持有／策略交易",
        "sleeve＝子帳戶", "replay＝重新執行驗證", "QA＝品質驗收",
        "manifest＝產物清單", "SHA＝檔案指紋", "readback＝讀回驗證", "parity＝逐值一致性",
    ))
    tech = _study(
        "技術驗收與重現附件", "證明 v1.3 只改呈現，沒有改變 v1.2 的任何既有數值。",
        "逐檔比對沿用附件的 SHA 檔案指紋；重新計算三張新增表；驗證全部相對連結、章節錨點與產物清單。",
        f"候選狀態為 `STOP_WAIT_00_QA`，等待 00 獨立品質驗收。技術名詞：{glossary}。\n\n"
        "重現命令：\n\n```powershell\npython -m backtests.dual_ma.etf_0050_final_strategy_v1_3 run\n"
        "python -m backtests.dual_ma.etf_0050_final_strategy_v1_3 validate\n"
        "python -m pytest tests/backtests/dual_ma/test_etf_0050_final_strategy_v1_3.py -q\n```\n\n"
        "自訂輸出路徑時，run 與 report 資料夾名稱都必須以 `v1-3-r3` 結尾。",
        "主文只使用友善中文名稱；英文內部代碼與檔名只留在本技術章節及附件。",
        "只有 00 明確驗收後才能宣稱正式通過；候選產物不得覆寫 v1.2。",
    )
    toc_headings = [
        "一頁白話結論", "完整績效總表", "策略相對大盤與0050持有的差異", "策略規則與兩種成交版本",
        "完整逐年績效", "交易紀錄與資金投入", "成交時間與交易成本測試", "不同時期與市場狀態測試",
        "交易集中度與單筆穩定性", "243組參數穩健性", "核心持有比例研究", "最終實務建議與監控項目",
        "技術驗收與重現附件", "完整附件目錄",
    ]
    toc = "\n".join(f"- [{heading}](#{v12._markdown_anchor(heading)})" for heading in toc_headings)
    report = "\n".join([
        "# 0050 最終策略定版報告｜候選 v1.3 修正版3", "", f"產出時間：{produced}", "",
        "候選狀態：等待 00 獨立品質驗收。", "",
        "## 閱讀順序", "", toc, "",
        *sections, tech, "## 完整附件目錄", "", links, "",
    ])
    (root / "README.md").write_text(report, encoding="utf-8", newline="\n")
    (root / "report.md").write_text(report, encoding="utf-8", newline="\n")


def _source_artifacts() -> list[str]:
    manifest = json.loads((SOURCE_REPORT / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
    return [row["path"] for row in manifest["artifacts"] if row["path"] not in {"README.md", "report.md"}]


def _report_contract_errors(text: str) -> list[str]:
    errors = []
    headings = [
        "## 一頁白話結論", "## 完整績效總表", "## 策略相對大盤與0050持有的差異",
        "## 策略規則與兩種成交版本", "## 完整逐年績效", "## 交易紀錄與資金投入",
        "## 成交時間與交易成本測試", "## 不同時期與市場狀態測試",
        "## 交易集中度與單筆穩定性", "## 243組參數穩健性", "## 核心持有比例研究",
        "## 最終實務建議與監控項目", "## 技術驗收與重現附件", "## 完整附件目錄",
    ]
    positions = [text.find(heading) for heading in headings]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        errors.append("required section order mismatch")
    for heading in headings[:-2]:
        start = text.find(heading)
        end = min((value for value in positions if value > start), default=len(text))
        section = text[start:end]
        for label in ("### 測試目的", "### 測試方法", "### 觀察結果", "### 白話結論", "### 對最終決策的影響"):
            if label not in section:
                errors.append(f"{heading} missing {label}")
    for required in (*MAIN_HEADERS, *ANNUAL_HEADERS, "不證明未來一定具有正期望", "等待 00 獨立品質驗收"):
        if required not in text:
            errors.append(f"missing Chinese report term: {required}")
    main_text = text.split("## 技術驗收與重現附件", 1)[0]
    forbidden = (
        "FINAL_SIGNAL_CLOSE", "FINAL_NEXT_SESSION_OPEN", "BUY_HOLD", "TAIEX_TOTAL_RETURN",
        "SIGNAL_CLOSE", "NEXT_SESSION_OPEN", "CAGR", "MaxDD", "benchmark", "rolling",
        "histogram", "armed", "raw close", "bps", "core/tactical", "sleeve", "replay",
        "manifest", "SHA", "readback", "parity",
    )
    for token in forbidden:
        if token in main_text:
            errors.append(f"technical token escaped into main report: {token}")
    for required in (
        "等號歸高市場狀態", "必須嚴格低於進場參考價", "每天重新判定並動態切換市場狀態",
        "賣出當日不重新進場", "不存在收盤進場、隔日開盤出場的混合第三版",
        "初始風險不得超過可用現金 20%", "2025-06-18 分割只調整股數與價格座標",
        "差異以未四捨五入原值計算", "中心參數 21／144", "局部鄰域共 11 組",
        "技術分析用收盤價／最低價", "前一日技術分析用收盤價不高於前一日 MA21",
        "技術分析用收盤價不高於 MA21", "當日技術分析用收盤價嚴格低於固定初始防守價",
        "未還原開盤價／收盤價", "只用於成交、股息再投入與未平倉清算帳務",
    ):
        if required not in main_text:
            errors.append(f"missing self-contained evidence: {required}")
    for invalid in (
        "前一日未還原收盤價不高於前一日 MA21",
        "當日未還原收盤價高於當日 MA21",
        "未還原收盤價嚴格低於固定初始防守價",
    ):
        if invalid in main_text:
            errors.append(f"raw price incorrectly used for technical rule: {invalid}")
    return errors


def run_study(*, run_root: Path = RUN_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT) -> dict[str, Any]:
    if run_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite isolated v1.3 output")
    for path, expected in ((SOURCE_RUN / "manifest.json", SOURCE_RUN_MANIFEST),
                           (SOURCE_REPORT / "artifact-manifest.json", SOURCE_REPORT_MANIFEST),
                           (SOURCE_RUN / "readback.json", SOURCE_READBACK),
                           (SOURCE_REPORT / "readback.json", SOURCE_READBACK)):
        if _sha(path) != expected:
            raise ETF0050Error(f"sealed v1.2 pin changed: {path}")
    before = {str(SOURCE_RUN): tree_hash(SOURCE_RUN), str(SOURCE_REPORT): tree_hash(SOURCE_REPORT)}
    summary = _read_csv(SOURCE_REPORT / "main-comparison-summary.csv")
    base_annual = _read_csv(SOURCE_REPORT / "main-comparison-calendar-year.csv")
    daily = _read_csv(SOURCE_REPORT / "main-comparison-daily.csv")
    strategy_tx = _read_csv(SOURCE_REPORT / "strategy-transactions.csv")
    performance = build_performance_summary(summary, base_annual)
    differences = build_relative_differences(summary)
    annual = build_complete_annual(base_annual, daily, strategy_tx)
    source = {
        "summary": summary, "cost": _read_csv(SOURCE_REPORT / "execution-cost-sensitivity.csv"),
        "periods": _read_csv(SOURCE_REPORT / "fixed-periods.csv"),
        "rolling": _read_csv(SOURCE_REPORT / "rolling-3-5-10.csv"),
        "regime": _read_csv(SOURCE_REPORT / "market-regime-performance.csv"),
        "stability": _read_csv(SOURCE_REPORT / "trade-stability.csv"),
        "concentration": _read_csv(SOURCE_REPORT / "concentration.csv"),
        "parameter_distribution": _read_csv(SOURCE_REPORT / "parameter-distribution.csv"),
        "parameter_center": _filter(_read_csv(SOURCE_REPORT / "parameter-results-243.csv"), parameter_id="MA21_144_MACD21_55_55"),
        "parameter_neighborhood": _filter(_read_csv(SOURCE_REPORT / "parameter-neighborhoods.csv"), parameter_id="MA21_144_MACD21_55_55"),
        "allocations": _read_csv(SOURCE_REPORT / "allocation-summary.csv"),
    }
    git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], text=True, encoding="utf-8").strip()
    produced_iso = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
    produced = datetime.fromisoformat(produced_iso).strftime("%Y-%m-%d %H:%M:%S（台北）")
    inherited = _source_artifacts()
    with tempfile.TemporaryDirectory(prefix="0050-final-v1-3-") as tmp:
        stage = Path(tmp); run_stage = stage / "run"; report_stage = stage / "report"
        run_stage.mkdir(); report_stage.mkdir()
        parity_files = []
        for name in inherited:
            target_name = "source-v1-2-input-manifest.json" if name == "input-manifest.json" else name
            shutil.copy2(SOURCE_REPORT / name, run_stage / target_name)
            parity_files.append({"source": name, "target": target_name, "sha256": _sha(SOURCE_REPORT / name), "exact": _sha(SOURCE_REPORT / name) == _sha(run_stage / target_name)})
        _write_csv(run_stage / "complete-performance-summary.csv", performance)
        _write_csv(run_stage / "strategy-relative-differences.csv", differences)
        _write_csv(run_stage / "complete-calendar-year.csv", annual)
        _write_json(run_stage / "source-v1-2-parity.json", {"status": "PASS_EXACT", "files": parity_files})
        _write_json(run_stage / "v1-3-input-manifest.json", {
            "version": VERSION, "status": "STOP_WAIT_00_QA", "produced_at_taipei": produced_iso,
            "git_commit": git_commit, "source_run_manifest_sha256": SOURCE_RUN_MANIFEST,
            "source_report_manifest_sha256": SOURCE_REPORT_MANIFEST, "source_readback_sha256": SOURCE_READBACK,
            "scope": "Traditional-Chinese presentation and derived fields only; no strategy or value changes",
        })
        after = {str(SOURCE_RUN): tree_hash(SOURCE_RUN), str(SOURCE_REPORT): tree_hash(SOURCE_REPORT)}
        if before != after or not all(row["exact"] for row in parity_files):
            raise ETF0050Error("sealed v1.2 tree or inherited artifact changed")
        run_excluded = {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"}
        run_artifacts = _artifact_rows(run_stage, run_excluded)
        _write_json(run_stage / "manifest.json", {"status": "STOP_WAIT_00_QA", "version": VERSION, "produced_at_taipei": produced_iso, "git_commit": git_commit, "artifact_count": len(run_artifacts), "artifacts": run_artifacts})
        run_bad = _verify_artifacts(run_stage, run_artifacts)
        _write_json(run_stage / "readback.json", {"status": "PASS" if not run_bad else "FAIL", "mismatches": run_bad})
        _write_json(run_stage / "QA_READY.json", {"status": "STOP_WAIT_00_QA", "version": VERSION, "ready_for_claimed_acceptance": False})
        _write_json(run_stage / "checkpoint.json", {"status": "STOP_WAIT_00_QA", "version": VERSION, "manifest_sha256": _sha(run_stage / "manifest.json"), "readback_sha256": _sha(run_stage / "readback.json"), "safe_next_step": "00 independent v1.3 QA"})
        if run_bad:
            raise ETF0050Error(f"run readback failed: {run_bad}")
        for path in run_stage.iterdir():
            if path.is_file() and path.name not in run_excluded:
                shutil.copy2(path, report_stage / path.name)
        attachments = ["report.md", *sorted(path.name for path in report_stage.iterdir() if path.is_file()), "artifact-manifest.json", "readback.json", "checkpoint.json"]
        _render_report(report_stage, performance, differences, annual, source, produced, attachments)
        for name in ("artifact-manifest.json", "readback.json", "checkpoint.json"):
            _write_json(report_stage / name, {"status": "BUILDING"})
        contract_errors = _report_contract_errors((report_stage / "README.md").read_text(encoding="utf-8"))
        broken = v12._broken_markdown_links(report_stage)
        report_excluded = {"artifact-manifest.json", "readback.json", "checkpoint.json"}
        report_artifacts = _artifact_rows(report_stage, report_excluded)
        missing = v12.research._readme_missing_artifacts(report_stage, [str(row["path"]) for row in report_artifacts])
        if contract_errors or broken or missing:
            raise ETF0050Error(f"report contract failed: {contract_errors}; links={broken}; coverage={missing}")
        _write_json(report_stage / "artifact-manifest.json", {"status": "STOP_WAIT_00_QA", "version": VERSION + "-report", "produced_at_taipei": produced_iso, "source_run_manifest_sha256": _sha(run_stage / "manifest.json"), "artifact_count": len(report_artifacts), "artifacts": report_artifacts})
        report_bad = _verify_artifacts(report_stage, report_artifacts)
        _write_json(report_stage / "readback.json", {"status": "PASS" if not report_bad else "FAIL", "mismatches": report_bad})
        _write_json(report_stage / "checkpoint.json", {"status": "STOP_WAIT_00_QA", "version": VERSION, "artifact_manifest_sha256": _sha(report_stage / "artifact-manifest.json"), "readback_sha256": _sha(report_stage / "readback.json"), "safe_next_step": "00 independent v1.3 report QA"})
        if report_bad:
            raise ETF0050Error(f"report readback failed: {report_bad}")
        run_root.parent.mkdir(parents=True, exist_ok=True); report_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(run_stage, run_root); shutil.copytree(report_stage, report_root)
    return {"status": "STOP_WAIT_00_QA", "run_root": str(run_root), "report_root": str(report_root), "run_manifest_sha256": _sha(run_root / "manifest.json"), "report_manifest_sha256": _sha(report_root / "artifact-manifest.json"), "readback_sha256": _sha(run_root / "readback.json"), "source_parity": "PASS_EXACT", "source_trees_unchanged": True}


def validate(*, run_root: Path = RUN_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT) -> dict[str, Any]:
    run_manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8-sig"))
    report_manifest = json.loads((report_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
    run_bad = _verify_artifacts(run_root, run_manifest["artifacts"]); report_bad = _verify_artifacts(report_root, report_manifest["artifacts"])
    parity = json.loads((run_root / "source-v1-2-parity.json").read_text(encoding="utf-8-sig"))
    summary = _read_csv(SOURCE_REPORT / "main-comparison-summary.csv"); base_annual = _read_csv(SOURCE_REPORT / "main-comparison-calendar-year.csv")
    expected_performance = build_performance_summary(summary, base_annual); expected_diff = build_relative_differences(summary)
    expected_annual = build_complete_annual(base_annual, _read_csv(SOURCE_REPORT / "main-comparison-daily.csv"), _read_csv(SOURCE_REPORT / "strategy-transactions.csv"))
    actual_performance = _read_csv(run_root / "complete-performance-summary.csv"); actual_diff = _read_csv(run_root / "strategy-relative-differences.csv"); actual_annual = _read_csv(run_root / "complete-calendar-year.csv")
    def normalized(rows: Sequence[Mapping[str, Any]]) -> list[list[str]]:
        fields = list(rows[0]) if rows else []
        return [[v12.normalize_value(row.get(field)) for field in fields] for row in rows]
    derived_ok = normalized(expected_performance) == normalized(actual_performance) and normalized(expected_diff) == normalized(actual_diff) and normalized(expected_annual) == normalized(actual_annual)
    contract = {name: _report_contract_errors((report_root / name).read_text(encoding="utf-8")) for name in ("README.md", "report.md")}
    broken = v12._broken_markdown_links(report_root); missing = v12.research._readme_missing_artifacts(report_root, [str(row["path"]) for row in report_manifest["artifacts"]])
    version_ok = run_manifest.get("version") == VERSION and report_manifest.get("version") == VERSION + "-report" and run_root.name.endswith("v1-3-r3") and report_root.name.endswith("v1-3-r3")
    pins_ok = _sha(SOURCE_RUN / "manifest.json") == SOURCE_RUN_MANIFEST and _sha(SOURCE_REPORT / "artifact-manifest.json") == SOURCE_REPORT_MANIFEST
    status = "PASS" if not run_bad and not report_bad and parity["status"] == "PASS_EXACT" and all(row["exact"] for row in parity["files"]) and derived_ok and not any(contract.values()) and not broken and not missing and version_ok and pins_ok else "FAIL"
    return {"status": status, "run_artifacts": len(run_manifest["artifacts"]), "report_artifacts": len(report_manifest["artifacts"]), "run_mismatches": run_bad, "report_mismatches": report_bad, "inherited_files": len(parity["files"]), "source_parity": parity["status"], "derived_tables_recomputed": derived_ok, "report_contract_errors": contract, "broken_links_or_anchors": broken, "readme_missing_artifacts": missing, "version_consistent": version_ok, "source_pins": pins_ok, "network_used": False, "writes_performed": False}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("mode", choices=("run", "validate"), nargs="?", default="run")
    parser.add_argument("--run-root", type=Path, default=RUN_ROOT_DEFAULT); parser.add_argument("--report-root", type=Path, default=REPORT_ROOT_DEFAULT)
    args = parser.parse_args(argv)
    result = validate(run_root=args.run_root, report_root=args.report_root) if args.mode == "validate" else run_study(run_root=args.run_root, report_root=args.report_root)
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["status"] in {"PASS", "STOP_WAIT_00_QA"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
