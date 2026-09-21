"""Replay the six-stock calibration cohort under judgement specification V2.

The AI-authored file owns structure/scenario/trigger decisions.  This module
validates that closed decision audit, reuses the frozen six-stock market data,
and performs only execution, accounting, aggregation, and Markdown rendering.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import enlightenment_ai_small_test as base


SOURCE_RUN = ROOT / "reports/course_backtest/2026-09-05/enlightenment_ai_small_test_v2"
PREVIOUS_RUN = SOURCE_RUN
RUN = ROOT / "reports/course_backtest/2026-09-06/enlightenment_ai_small_test_judgement_v2"
DECISIONS_PATH = RUN / "ai_v2_trigger_review.json"
RULES_PATH = ROOT / "config/enlightenment_ai_rules_v2.json"
RULE_DOC_PATH = ROOT / "docs/enlightenment-ai-judgement-v2.md"
RULE_SCHEMA_PATH = ROOT / "config/enlightenment_ai_judgement_v2.schema.json"
AS_OF = "2026-09-04"

SCENARIO_LABELS = {
    "MATURE_TREND_PULLBACK": "MATURE_TREND_PULLBACK（長多慣性拉回再發動）",
    "MACRO_COPY_RESONANCE": "MACRO_COPY_RESONANCE（大定錨複製共振）",
    "BEAR_REVERSAL_LEFT_RIGHT": "BEAR_REVERSAL_LEFT_RIGHT（空頭末段左右反轉）",
    "FRESH_Q1_EXPANSION": "FRESH_Q1_EXPANSION（新生定錨直接擴張）",
}
REQUIRED_GATE_COUNTS = {
    "MATURE_TREND_PULLBACK": 8,
    "MACRO_COPY_RESONANCE": 8,
    "BEAR_REVERSAL_LEFT_RIGHT": 6,
    "FRESH_Q1_EXPANSION": 7,
}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _scenario_key(label: str) -> str:
    return label.split("（", 1)[0]


def validate_decisions() -> dict[str, Any]:
    rules = _read(RULES_PATH)
    decisions = _read(DECISIONS_PATH)
    manifest = _read(SOURCE_RUN / "input_manifest.json")
    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, detail: Any) -> None:
        checks.append({"id": check_id, "passed": bool(passed), "detail": detail})

    add(
        "rule_version_is_v2",
        rules.get("schema_version") == "enlightenment-ai-rules-v2",
        rules.get("schema_version"),
    )
    add(
        "decision_references_official_v2",
        decisions.get("rule_reference") == "docs/enlightenment-ai-judgement-v2.md"
        and decisions.get("rule_config") == "config/enlightenment_ai_rules_v2.json",
        {"rule_reference": decisions.get("rule_reference"), "rule_config": decisions.get("rule_config")},
    )
    manifest_codes = {item["code"] for item in manifest["items"]}
    add(
        "six_stocks_complete",
        set(decisions["approved_events"]) == manifest_codes
        and set(decisions["stock_structure_maps"]) == manifest_codes,
        sorted(manifest_codes),
    )
    add(
        "preferred_history_available",
        all(int(item["pre_monitor_bars"]) >= 750 for item in manifest["items"]),
        {item["code"]: item["pre_monitor_bars"] for item in manifest["items"]},
    )

    source_hashes: dict[str, str] = {}
    event_errors: list[str] = []
    event_count = 0
    for item in manifest["items"]:
        code = item["code"]
        price_path = SOURCE_RUN / "sources/prices" / f"{code}.csv"
        source_hashes[code] = _sha256(price_path)
        frame = pd.read_csv(price_path)
        frame["date"] = frame["date"].astype(str)
        by_date = frame.set_index("date")
        seen: set[str] = set()
        invalidation = decisions.get("campaign_invalidations", {}).get(code, {}).get("date")
        for event in decisions["approved_events"][code]:
            event_count += 1
            day = event["signal_date"]
            if day in seen:
                event_errors.append(f"{code} {day}: duplicate event")
            seen.add(day)
            if day not in by_date.index:
                event_errors.append(f"{code} {day}: missing price bar")
                continue
            if day < item["monitor_on"]:
                event_errors.append(f"{code} {day}: before monitoring")
            if invalidation and day >= invalidation:
                event_errors.append(f"{code} {day}: on/after campaign invalidation")
            scenario = _scenario_key(event["scenario"])
            if scenario not in REQUIRED_GATE_COUNTS:
                event_errors.append(f"{code} {day}: unknown scenario {scenario}")
            elif event["scenario"] != SCENARIO_LABELS[scenario]:
                event_errors.append(f"{code} {day}: scenario label mismatch")
            elif len(event.get("gate_evidence", [])) != REQUIRED_GATE_COUNTS[scenario]:
                event_errors.append(
                    f"{code} {day}: {scenario} needs {REQUIRED_GATE_COUNTS[scenario]} gates"
                )
            close = float(by_date.loc[day, "close"])
            stop = float(event["stop"])
            if not 0 < stop < close:
                event_errors.append(f"{code} {day}: invalid stop {stop} for close {close}")
            if event.get("large_quadrant") == "Q3" or event.get("small_quadrant") == "Q3":
                event_errors.append(f"{code} {day}: Q3 cannot trigger")
            if not set(event.get("eligibility", [])).issubset({"MOTHER", "REENTRY", "ADD"}):
                event_errors.append(f"{code} {day}: invalid eligibility")
            if not event.get("reason") or not event.get("stop_source_date"):
                event_errors.append(f"{code} {day}: missing reason or stop source")

    add("source_files_frozen_and_readable", len(source_hashes) == 6, source_hashes)
    add("approved_events_closed_gate_audit", not event_errors, event_errors or event_count)
    add(
        "campaign_invalidations_have_reasons",
        all(row.get("date") and row.get("reason") for row in decisions["campaign_invalidations"].values()),
        decisions["campaign_invalidations"],
    )
    non_triggers = [row for rows in decisions["reviewed_non_triggers"].values() for row in rows]
    add(
        "non_triggers_explain_status_and_reason",
        all(row.get("date") and row.get("status") and row.get("reason") for row in non_triggers),
        len(non_triggers),
    )
    add(
        "execution_parameters_not_promoted_to_v2",
        "BACKTEST_PARAMETER" in decisions.get("execution_boundary", "")
        and "minimum_score" not in rules
        and "chase_cap" not in rules,
        decisions.get("execution_boundary"),
    )

    payload = {
        "version": "enlightenment-ai-six-stock-v2-decision-validation",
        "passed": all(check["passed"] for check in checks),
        "check_count": len(checks),
        "approved_event_count": event_count,
        "reviewed_non_trigger_count": len(non_triggers),
        "rule_sha256": _sha256(RULES_PATH),
        "rule_document_sha256": _sha256(RULE_DOC_PATH),
        "rule_schema_sha256": _sha256(RULE_SCHEMA_PATH),
        "decision_sha256": _sha256(DECISIONS_PATH),
        "checks": checks,
    }
    _save(RUN / "decision_validation.json", payload)
    if not payload["passed"]:
        failures = [check for check in checks if not check["passed"]]
        raise ValueError(json.dumps(failures, ensure_ascii=False, indent=2))
    return payload


def _episodes(variant: dict[str, Any]) -> list[dict[str, Any]]:
    return [episode for stock in variant["stocks"] for episode in stock["episodes"]]


def _all_tranches(variant: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (episode, tranche)
        for episode in _episodes(variant)
        for tranche in episode["tranches"]
    ]


def _profit_factor(episodes: list[dict[str, Any]]) -> float | None:
    gross_profit = sum(max(float(row["net_pnl"]), 0.0) for row in episodes)
    gross_loss = -sum(min(float(row["net_pnl"]), 0.0) for row in episodes)
    return gross_profit / gross_loss if gross_loss else None


def _distribution(values: list[float], bins: list[tuple[str, float, float]]) -> list[dict[str, Any]]:
    rows = []
    for label, lower, upper in bins:
        selected = [value for value in values if lower <= value < upper]
        rows.append({"bucket": label, "count": len(selected), "pct": len(selected) / len(values) * 100 if values else 0.0})
    return rows


def _mark_to_market_drawdown(variant: dict[str, Any]) -> dict[str, Any]:
    episodes = _episodes(variant)
    if not episodes:
        return {"base_cash": 0.0, "max_drawdown_twd": 0.0, "max_drawdown_pct": 0.0, "peak_date": None, "trough_date": None}

    price_by_code: dict[str, pd.Series] = {}
    all_dates: set[str] = set()
    for stock in variant["stocks"]:
        frame = pd.read_csv(SOURCE_RUN / "sources/prices" / f"{stock['code']}.csv")
        frame["date"] = frame["date"].astype(str)
        frame = frame[frame["date"] <= AS_OF]
        series = frame.set_index("date")["raw_close"].astype(float)
        price_by_code[stock["code"]] = series
        all_dates.update(series.index)
    dates = sorted(day for day in all_dates if day >= min(row["entry_date"] for row in episodes))
    base_cash = float(variant["summary"]["peak_concurrent_deployed_cash"])
    peak_equity = base_cash
    peak_date = dates[0]
    maximum_amount = 0.0
    maximum_pct = 0.0
    trough_date = dates[0]
    for day in dates:
        pnl = 0.0
        for episode in episodes:
            if day < episode["entry_date"]:
                continue
            exit_date = episode.get("exit_date")
            if exit_date and day >= exit_date:
                pnl += float(episode["net_pnl"])
                continue
            series = price_by_code[episode["code"]]
            eligible = series.loc[:day]
            if eligible.empty:
                continue
            mark = float(eligible.iloc[-1])
            live_tranches = [row for row in episode["tranches"] if row["entry_date"] <= day]
            pnl += sum(base._sell_proceeds(int(row["shares"]), mark) - float(row["buy_cost"]) for row in live_tranches)
            pnl += sum(float(row["cash"]) for row in episode.get("cash_events", []) if row["date"] <= day)
        equity = base_cash + pnl
        if equity > peak_equity:
            peak_equity = equity
            peak_date = day
        drawdown = peak_equity - equity
        drawdown_pct = drawdown / peak_equity * 100 if peak_equity else 0.0
        if drawdown > maximum_amount:
            maximum_amount = drawdown
            maximum_pct = drawdown_pct
            trough_date = day
    return {
        "base_cash": round(base_cash, 2),
        "max_drawdown_twd": round(maximum_amount, 2),
        "max_drawdown_pct": round(maximum_pct, 4),
        "peak_date": peak_date,
        "trough_date": trough_date,
        "method": "以尖峰投入資金作基準本金，逐日按可變現淨值計算；僅為六檔組合風險觀察。",
    }


def _enhance_summary(variant: dict[str, Any]) -> None:
    episodes = _episodes(variant)
    tranches = _all_tranches(variant)
    pnl = [float(row["net_pnl"]) for row in episodes]
    returns = [float(row["net_return_on_deployed_pct"]) for row in episodes]
    mfes = [float(row["mfe_pct_from_mother"]) for row in episodes]
    maes = [float(row["mae_pct_from_mother"]) for row in episodes]
    gross_profit = sum(max(value, 0.0) for value in pnl)
    gross_loss = -sum(min(value, 0.0) for value in pnl)
    initial_risk_pct = [
        max(0.0, (float(tranche["entry_price_adjusted"]) - float(tranche["stop_adjusted"])) / float(tranche["entry_price_adjusted"]) * 100)
        for _, tranche in tranches
    ]
    risk_events: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"open": [], "close": []})
    for episode, tranche in tranches:
        risk_cash = float(tranche["buy_cost"]) * max(
            0.0,
            (float(tranche["entry_price_adjusted"]) - float(tranche["stop_adjusted"])) / float(tranche["entry_price_adjusted"]),
        )
        risk_events[tranche["entry_date"]]["open"].append(risk_cash)
        if episode.get("exit_date"):
            risk_events[episode["exit_date"]]["close"].append(risk_cash)
    active_risk = peak_risk = 0.0
    for day in sorted(risk_events):
        active_risk -= sum(risk_events[day]["close"])
        active_risk += sum(risk_events[day]["open"])
        peak_risk = max(peak_risk, active_risk)

    summary = variant["summary"]
    summary.update(
        {
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(_profit_factor(episodes), 4) if _profit_factor(episodes) is not None else None,
            "average_episode_pnl": round(sum(pnl) / len(pnl), 2) if pnl else 0.0,
            "median_episode_pnl": round(float(pd.Series(pnl).median()), 2) if pnl else 0.0,
            "average_episode_return_pct": round(sum(returns) / len(returns), 4) if returns else 0.0,
            "median_episode_return_pct": round(float(pd.Series(returns).median()), 4) if returns else 0.0,
            "average_episode_mae_pct": round(sum(maes) / len(maes), 4) if maes else 0.0,
            "median_episode_mfe_pct": round(float(pd.Series(mfes).median()), 4) if mfes else 0.0,
            "mfe_ge_20_count": sum(value >= 20 for value in mfes),
            "mfe_ge_20_profitable_final_count": sum(mfe >= 20 and ret > 0 for mfe, ret in zip(mfes, returns)),
            "average_initial_risk_pct_per_fill": round(sum(initial_risk_pct) / len(initial_risk_pct), 4) if initial_risk_pct else 0.0,
            "maximum_initial_risk_pct_per_fill": round(max(initial_risk_pct), 4) if initial_risk_pct else 0.0,
            "peak_static_initial_stop_risk_twd": round(peak_risk, 2),
            "full_period_pnl_over_peak_cash_pct": round(float(summary["net_pnl"]) / float(summary["peak_concurrent_deployed_cash"]) * 100, 4)
            if summary["peak_concurrent_deployed_cash"]
            else 0.0,
            "return_distribution": _distribution(
                returns,
                [
                    ("≤-10%", -math.inf, -10.0),
                    ("-10%～0%", -10.0, 0.0),
                    ("0%～10%", 0.0, 10.0),
                    ("10%～20%", 10.0, 20.0),
                    ("20%～50%", 20.0, 50.0),
                    ("≥50%", 50.0, math.inf),
                ],
            ),
            "mfe_distribution": _distribution(
                mfes,
                [
                    ("<5%", -math.inf, 5.0),
                    ("5%～10%", 5.0, 10.0),
                    ("10%～20%", 10.0, 20.0),
                    ("20%～50%", 20.0, 50.0),
                    ("≥50%", 50.0, math.inf),
                ],
            ),
        }
    )
    summary["mark_to_market_drawdown"] = _mark_to_market_drawdown(variant)

    by_stock: dict[str, dict[str, Any]] = {}
    for stock in variant["stocks"]:
        rows = stock["episodes"]
        deployed = sum(float(row["deployed_cash"]) for row in rows)
        stock_net = sum(float(row["net_pnl"]) for row in rows)
        by_stock[f"{stock['code']} {stock['name']}"] = {
            "episodes": len(rows),
            "closed": sum(row["status"].startswith("CLOSED") for row in rows),
            "open": sum(row["status"].startswith("OPEN") for row in rows),
            "tranches": sum(int(row["tranche_count"]) for row in rows),
            "deployed_cash": round(deployed, 2),
            "net_pnl": round(stock_net, 2),
            "return_on_deployed_cash_pct": round(stock_net / deployed * 100, 4) if deployed else 0.0,
            "winning_episodes": sum(float(row["net_pnl"]) > 0 for row in rows),
        }
    variant["by_stock"] = by_stock

    by_scenario: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        grouped[episode["tranches"][0]["scenario"]].append(episode)
    for scenario, rows in grouped.items():
        deployed = sum(float(row["deployed_cash"]) for row in rows)
        scenario_net = sum(float(row["net_pnl"]) for row in rows)
        by_scenario[scenario] = {
            "episodes": len(rows),
            "wins": sum(float(row["net_pnl"]) > 0 for row in rows),
            "deployed_cash": round(deployed, 2),
            "net_pnl": round(scenario_net, 2),
            "return_on_deployed_cash_pct": round(scenario_net / deployed * 100, 4) if deployed else 0.0,
            "average_mfe_pct": round(sum(float(row["mfe_pct_from_mother"]) for row in rows) / len(rows), 4),
        }
    variant["by_scenario"] = by_scenario


def replay() -> dict[str, Any]:
    validation = validate_decisions()
    manifest = _read(SOURCE_RUN / "input_manifest.json")
    decisions = _read(DECISIONS_PATH)

    # The accounting engine reads its source through a module constant.  Point
    # it at the frozen source directory; outputs remain isolated in RUN.
    base.RUN = SOURCE_RUN
    output: dict[str, Any] = {
        "version": "enlightenment-ai-six-stock-backtest-under-judgement-v2",
        "as_of": AS_OF,
        "rule_version": "enlightenment-ai-rules-v2",
        "rule_sha256": validation["rule_sha256"],
        "rule_document_sha256": validation["rule_document_sha256"],
        "decision_sha256": validation["decision_sha256"],
        "source_manifest_sha256": _sha256(SOURCE_RUN / "input_manifest.json"),
        "market_data_directory": str((SOURCE_RUN / "sources").resolve()),
        "judgement_boundary": decisions["execution_boundary"],
        "backtest_parameters": {
            "nominal_twd_per_fill": 10000,
            "buy_commission": 0.001425,
            "sell_commission": 0.001425,
            "sell_tax": 0.003,
            "entry": "訊號日收盤成立，下一交易日開盤成交；低於防線或高於收盤+0.5ATR加一跳取消。",
            "exit": "固定／加碼防線；母單達+2R後成本防線只升不降；因果小樞紐失守先警告；連續兩日跌破21MA出場。",
            "corporate_actions": "技術使用還原 OHLC；帳務使用 Yahoo 分割標準化原始價、現金股利及交易成本。",
        },
        "variants": {},
        "reviewed_non_triggers": decisions["reviewed_non_triggers"],
        "stock_structure_maps": decisions["stock_structure_maps"],
        "decision_validation": validation,
    }
    for key, allow_adds in (
        ("MOTHER_ONLY_10K（固定母單一萬元）", False),
        ("MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）", True),
    ):
        result = {"name": key, "stocks": []}
        for source_item in manifest["items"]:
            item = dict(source_item)
            result["stocks"].append(
                base._simulate_code(
                    item,
                    decisions["approved_events"][item["code"]],
                    allow_adds=allow_adds,
                    lifecycle=decisions.get("campaign_invalidations", {}).get(item["code"]),
                )
            )
        result["summary"] = base._summary(result)
        _enhance_summary(result)
        output["variants"][key] = result
    _save(RUN / "backtest.json", output)
    return output


def _money(value: float) -> str:
    return f"{value:+,.0f}"


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def _pf(value: float | None) -> str:
    return "∞" if value is None else f"{value:.2f}"


def _stock_name(manifest: dict[str, Any], code: str) -> str:
    return next(item["name"] for item in manifest["items"] if item["code"] == code)


def _fill_skips(variant: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for stock in variant["stocks"]:
        for event in stock["audit"]:
            if event["event"].startswith("BUY_SKIPPED"):
                signal = event.get("signal", {})
                rows.append(
                    {
                        "stock": f"{stock['code']} {stock['name']}",
                        "signal_date": signal.get("signal_date"),
                        "session": event["date"],
                        "reason": event["reason"],
                        "cap": event.get("cap"),
                    }
                )
    return rows


def report() -> Path:
    result = _read(RUN / "backtest.json")
    decisions = _read(DECISIONS_PATH)
    manifest = _read(SOURCE_RUN / "input_manifest.json")
    previous = _read(PREVIOUS_RUN / "backtest.json")
    approved_counts = {code: len(rows) for code, rows in decisions["approved_events"].items()}
    non_trigger_counts = {code: len(rows) for code, rows in decisions["reviewed_non_triggers"].items()}

    lines = [
        "# 六檔啟蒙層 AI 判讀規格 V2 小回測",
        "",
        "> `RETROSPECTIVE_NOT_BLIND（事後研究、不是樣本外盲測）`：每個訊號的資料切在該日收盤，但 AI 知道完整歷史存在。六檔又是使用者指定的校準樣本，因此本報表可檢查規則與交易行為，不能證明未來穩定獲利。",
        "",
        "## 結論摘要",
        "",
    ]
    mother = result["variants"]["MOTHER_ONLY_10K（固定母單一萬元）"]["summary"]
    adding = result["variants"]["MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）"]["summary"]
    lines += [
        f"- 固定母單：{mother['trade_episodes']} 個交易回合、{mother['tranches']} 份成交，合計淨損益 `{_money(mother['net_pnl'])}`，投入金額合計報酬 `{_pct(mother['return_on_deployed_cash_pct'])}`，尖峰占用 `{mother['peak_concurrent_deployed_cash']:,.0f}` 元。",
        f"- 加碼版：{adding['trade_episodes']} 個交易回合、{adding['tranches']} 份成交，合計淨損益 `{_money(adding['net_pnl'])}`，投入金額合計報酬 `{_pct(adding['return_on_deployed_cash_pct'])}`，尖峰占用 `{adding['peak_concurrent_deployed_cash']:,.0f}` 元。",
        f"- 加碼版相對固定母單多投入 {adding['tranches'] - mother['tranches']} 份，合計淨損益差 `{_money(adding['net_pnl'] - mother['net_pnl'])}`；這是絕對損益比較，不等於已控制選股偏誤。",
        f"- 獲利集中：合晶貢獻固定版 `{_money(result['variants']['MOTHER_ONLY_10K（固定母單一萬元）']['by_stock']['6182 合晶']['net_pnl'])}`（占 {result['variants']['MOTHER_ONLY_10K（固定母單一萬元）']['by_stock']['6182 合晶']['net_pnl'] / mother['net_pnl'] * 100:.2f}%），加碼版 `{_money(result['variants']['MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）']['by_stock']['6182 合晶']['net_pnl'])}`（占 {result['variants']['MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）']['by_stock']['6182 合晶']['net_pnl'] / adding['net_pnl'] * 100:.2f}%）。排除合晶後仍為正，但只剩固定版 `{_money(mother['net_pnl'] - result['variants']['MOTHER_ONLY_10K（固定母單一萬元）']['by_stock']['6182 合晶']['net_pnl'])}`、加碼版 `{_money(adding['net_pnl'] - result['variants']['MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）']['by_stock']['6182 合晶']['net_pnl'])}`。",
        "- 截止日兩版都沒有持有中部位。金居 8/27 雖通過 V2 結構觸發，但 8/28 開盤 525 元高於追價上限 509.66 元，依沿用的回測參數取消成交。",
        "",
        "## 規則與會計邊界",
        "",
        f"- 判讀規格：`enlightenment-ai-rules-v2`，規則 SHA-256 `{result['rule_sha256']}`。",
        "- V2 決定：大／小定錨、太極世代、象限、道氏防線、左右位置、唯一主要情境、觸發與失效。四情境必要門檻逐項 PASS 才能進入核准訊號。",
        "- 回測參數：每份約 10,000 元；訊號收盤確認、下一交易日開盤；開盤低於防線或高於訊號收盤 + 0.5ATR 加一跳則取消。這個追價條件不是 V2 規則。",
        "- 出場參數：初始／加碼防線；母單達 +2R 後提高至成本，連續兩日收盤跌破 21MA 才出場；小級樞紐失守先警告。這個出場式也不是 V2 情境定義。",
        "- 技術價格採公司行動回溯還原；帳務採分割標準化原始價格、現金股利、買賣手續費各 0.1425% 與賣出交易稅 0.3%。",
        "- 固定版是固定名目金額，不是固定風險金額；不同停損距離會造成每份承擔的初始風險不同，報表另列平均與最大風險距離。",
        "",
        "## 監控起點與資料量",
        "",
        "| 股票 | 使用者輸入 | 實際監控起日 | 監控前日 K | 資料首日 | V2核准訊號 | 重要未觸發 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in manifest["items"]:
        lines.append(
            f"| {item['code']} {item['name']} | {item['monitor_month']} | {item['monitor_on']} | {item['pre_monitor_bars']:,} | {item['first_bar']} | {approved_counts[item['code']]} | {non_trigger_counts.get(item['code'], 0)} |"
        )

    lines += [
        "",
        "### 39 個 V2 核准訊號如何轉成交易",
        "",
        "| 版本 | 母單成交 | 加碼成交 | 成交參數取消 | 僅保存訊號證據 | 合計 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for variant in result["variants"].values():
        events = [event["event"] for stock in variant["stocks"] for event in stock["audit"]]
        mothers = sum(event.startswith("BUY_MOTHER") for event in events)
        adds_count = sum(event.startswith("BUY_ADD_") for event in events)
        skips = sum(event.startswith("BUY_SKIPPED") for event in events)
        evidence_only = sum(event.startswith("SIGNAL_EVIDENCE_ONLY") for event in events)
        lines.append(
            f"| {variant['name']} | {mothers} | {adds_count} | {skips} | {evidence_only} | {mothers + adds_count + skips + evidence_only} |"
        )
    lines += [
        "",
        "固定版遇到持有中的後續加碼訊號，只保存為證據；加碼版會執行最多兩次。加碼防線會上移整體動態防線，所以加碼版不是單純把同一交易放大：例如新漢在 2025/6 加碼後提前出場，之後 7 月另開新 episode，因而比固定版多一個回合。",
    ]

    lines += ["", "## V2 大結構地圖", "", "| 股票 | 主要定錨／朝代 | 監控判讀 | 大級防線／失效 |", "|---|---|---|---|"]
    for item in manifest["items"]:
        row = decisions["stock_structure_maps"][item["code"]]
        lines.append(
            f"| {item['code']} {item['name']} | {row['primary_anchor']} | {row['monitoring_view']} | {row['large_defense']} |"
        )

    lines += [
        "",
        "## 兩種部位版本統計",
        "",
        "| 版本 | 回合 | 已結束／持有中 | 成交份數 | 勝率 | 已實現 | 未實現估值 | 合計淨損益 | 投入合計報酬 | PF | 最大同時股票／份數 | 尖峰資金 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in result["variants"].values():
        s = variant["summary"]
        lines.append(
            f"| {variant['name']} | {s['trade_episodes']} | {s['closed']}／{s['open']} | {s['tranches']} | {s['win_rate_pct']:.2f}% | "
            f"{_money(s['realized_net_pnl'])} | {_money(s['unrealized_net_pnl_after_estimated_exit_cost'])} | {_money(s['net_pnl'])} | "
            f"{_pct(s['return_on_deployed_cash_pct'])} | {_pf(s['profit_factor'])} | {s['maximum_concurrent_episodes']}／{s['maximum_concurrent_tranches']} | {s['peak_concurrent_deployed_cash']:,.0f} |"
        )
    lines += [
        "",
        "註：未實現損益是假設在 2026/09/04 收盤變現並扣賣出成本，不是實際成交。PF 使用已結束與截至日估值後的全部回合。",
        "",
        "### 風險與資金效率",
        "",
        "| 版本 | 平均／中位回合報酬 | 平均 MAE | 平均／中位 MFE | MFE≥20%且最終獲利 | 平均／最大每份初始風險距離 | 尖峰靜態停損風險 | 全期損益÷尖峰資金 | 最大逐日回撤 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in result["variants"].values():
        s = variant["summary"]
        dd = s["mark_to_market_drawdown"]
        lines.append(
            f"| {variant['name']} | {_pct(s['average_episode_return_pct'])}／{_pct(s['median_episode_return_pct'])} | {_pct(s['average_episode_mae_pct'])} | "
            f"{_pct(s['average_episode_mfe_pct'])}／{_pct(s['median_episode_mfe_pct'])} | {s['mfe_ge_20_profitable_final_count']}／{s['mfe_ge_20_count']} | "
            f"{s['average_initial_risk_pct_per_fill']:.2f}%／{s['maximum_initial_risk_pct_per_fill']:.2f}% | {s['peak_static_initial_stop_risk_twd']:,.0f} | "
            f"{_pct(s['full_period_pnl_over_peak_cash_pct'])} | {_money(-dd['max_drawdown_twd'])}／-{dd['max_drawdown_pct']:.2f}%（{dd['peak_date']}→{dd['trough_date']}） |"
        )
    lines += [
        "",
        "補充：全期損益÷尖峰資金包含跨年重複使用資金，不是單次報酬、CAGR 或可直接複製的資金曲線。逐日回撤以尖峰投入資金作基準本金。",
        "",
        "## 最終損益漲跌幅分布",
        "",
        "| 版本 | ≤-10% | -10%～0% | 0%～10% | 10%～20% | 20%～50% | ≥50% |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in result["variants"].values():
        dist = {row["bucket"]: row["count"] for row in variant["summary"]["return_distribution"]}
        lines.append(
            f"| {variant['name']} | {dist['≤-10%']} | {dist['-10%～0%']} | {dist['0%～10%']} | {dist['10%～20%']} | {dist['20%～50%']} | {dist['≥50%']} |"
        )
    lines += [
        "",
        "### MFE（持有期間最大浮盈）分布",
        "",
        "| 版本 | <5% | 5%～10% | 10%～20% | 20%～50% | ≥50% |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for variant in result["variants"].values():
        dist = {row["bucket"]: row["count"] for row in variant["summary"]["mfe_distribution"]}
        lines.append(
            f"| {variant['name']} | {dist['<5%']} | {dist['5%～10%']} | {dist['10%～20%']} | {dist['20%～50%']} | {dist['≥50%']} |"
        )

    for variant in result["variants"].values():
        lines += [
            "",
            f"## {variant['name']}：各股損益",
            "",
            "| 股票 | 回合 | 已結束／持有中 | 成交份數 | 勝／負回合 | 投入合計 | 淨損益 | 投入合計報酬 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for stock, row in variant["by_stock"].items():
            losses = row["episodes"] - row["winning_episodes"]
            lines.append(
                f"| {stock} | {row['episodes']} | {row['closed']}／{row['open']} | {row['tranches']} | {row['winning_episodes']}／{losses} | "
                f"{row['deployed_cash']:,.0f} | {_money(row['net_pnl'])} | {_pct(row['return_on_deployed_cash_pct'])} |"
            )
        lines += [
            "",
            "### 依母單主要情境",
            "",
            "| 主要情境 | 回合 | 勝回合 | 平均 MFE | 投入合計 | 淨損益 | 投入合計報酬 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for scenario, row in variant["by_scenario"].items():
            lines.append(
                f"| {scenario} | {row['episodes']} | {row['wins']} | {_pct(row['average_mfe_pct'])} | {row['deployed_cash']:,.0f} | {_money(row['net_pnl'])} | {_pct(row['return_on_deployed_cash_pct'])} |"
            )

    lines += [
        "",
        "## 與判讀規格 V2 建立前的六檔回測比較",
        "",
        "> 前一份資料夾名稱也含 `v2`，但那是六檔回測修正版，不是正式的「啟蒙層 AI 判讀規格 V2」。本表只用來顯示重新判讀後交易結果如何改變。",
        "",
        "| 版本 | 舊回合→新回合 | 舊成交份→新成交份 | 舊淨損益 | 新淨損益 | 差額 | 舊尖峰資金→新尖峰資金 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    previous_key_map = {
        "MOTHER_ONLY_10K（固定母單一萬元）": "MOTHER_ONLY_10K（母單一筆一萬元）",
        "MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）": "MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）",
    }
    for key, variant in result["variants"].items():
        new = variant["summary"]
        old = previous["variants"][previous_key_map[key]]["summary"]
        lines.append(
            f"| {key} | {old['trade_episodes']}→{new['trade_episodes']} | {old['tranches']}→{new['tranches']} | {_money(old['net_pnl'])} | {_money(new['net_pnl'])} | "
            f"{_money(new['net_pnl'] - old['net_pnl'])} | {old['peak_concurrent_deployed_cash']:,.0f}→{new['peak_concurrent_deployed_cash']:,.0f} |"
        )

    for variant in result["variants"].values():
        lines += [
            "",
            f"## {variant['name']}：完整逐筆回合",
            "",
            "| 股票／回合 | 母單訊號→進場 | 份數 | 初始防線 | 出場訊號→成交／原因 | MFE | MAE | 淨損益 | 報酬 | 狀態 |",
            "|---|---|---:|---:|---|---:|---:|---:|---:|---|",
        ]
        for stock in variant["stocks"]:
            for episode in stock["episodes"]:
                mother_fill = episode["tranches"][0]
                if episode["status"].startswith("OPEN"):
                    exit_text = f"截至 {episode['mark_date']}／{episode['mark_price_raw']:.2f}"
                else:
                    exit_text = f"{episode['exit_signal_date']}→{episode['exit_date']}／{episode['exit_price_raw']:.2f}；{episode['exit_reason']}"
                lines.append(
                    f"| {episode['code']} {episode['name']}／{episode['episode_id']} | {mother_fill['signal_date']}→{mother_fill['entry_date']}／{mother_fill['entry_price_raw']:.2f} | "
                    f"{episode['tranche_count']} | {episode['initial_defense']:.2f} | {exit_text} | {_pct(episode['mfe_pct_from_mother'])} | {_pct(episode['mae_pct_from_mother'])} | "
                    f"{_money(episode['net_pnl'])} | {_pct(episode['net_return_on_deployed_pct'])} | {episode['status']} |"
                )
        lines += [
            "",
            "### 完整各份成交",
            "",
            "| 股票／回合 | 角色 | 主要情境／觸發 | 訊號日 | 進場日／原始價／股數 | 該份防線 | 出場日／原始價 |",
            "|---|---|---|---:|---|---:|---|",
        ]
        for stock in variant["stocks"]:
            for episode in stock["episodes"]:
                for tranche in episode["tranches"]:
                    sold = "持有中" if tranche.get("sell_date") is None else f"{tranche['sell_date']}／{tranche['sell_price']:.2f}"
                    lines.append(
                        f"| {episode['code']} {episode['name']}／{episode['episode_id']} | {tranche['role']} | {tranche['scenario']}／{tranche['family']} | {tranche['signal_date']} | "
                        f"{tranche['entry_date']}／{tranche['entry_price_raw']:.2f}／{tranche['shares']}股 | {tranche['stop_adjusted']:.2f} | {sold} |"
                    )

        skips = _fill_skips(variant)
        lines += [
            "",
            "### V2 已觸發但成交參數取消",
            "",
            "| 股票 | 訊號日 | 原定成交日 | 原因 | 追價上限 |",
            "|---|---:|---:|---|---:|",
        ]
        if skips:
            for row in skips:
                cap = "—" if row["cap"] is None else f"{row['cap']:.2f}"
                lines.append(f"| {row['stock']} | {row['signal_date']} | {row['session']} | {row['reason']} | {cap} |")
        else:
            lines.append("| — | — | — | 無 | — |")

    lines += [
        "",
        "## V2 重要未觸發／受阻事件",
        "",
        "| 股票 | 日期 | 狀態 | V2原因 |",
        "|---|---:|---|---|",
    ]
    for code, rows in decisions["reviewed_non_triggers"].items():
        for row in rows:
            lines.append(f"| {code} {_stock_name(manifest, code)} | {row['date']} | {row['status']} | {row['reason']} |")

    validation = result["decision_validation"]
    lines += [
        "",
        "## 自我檢查",
        "",
        f"- V2 決策稽核：`{'PASS（通過）' if validation['passed'] else 'FAIL（未通過）'}`，{validation['check_count']} 項；39 個核准訊號均有對應情境的完整必要門檻證據。",
        "- 六檔監控前資料均超過 750 根日 K；訊號日、監控起點、防線價格、Q3 排除、情境標籤及 campaign 失效順序均已程式檢查。",
        "- 同股同日只保留一個訊號；第一筆實際成交永遠是母單；加碼只在不同日期、持倉已有收盤浮盈且未達兩次上限時執行。",
        "- 豐藝 2024/12/16、廣運 2026/07/28 大級失效後移出監控；沒有上游重新入選就不允許後續訊號復活。",
        "- 金居 2026/4/7 與 8/25 僅 ARMED，不用均線或紅 K 提前觸發；真正 V2 結構觸發分別是 4/13 與 8/27，後續是否成交由既有追價參數決定。",
        "",
        "## 研究限制",
        "",
        "- 六檔是指定校準樣本，豐藝的監控期又遠長於其他股票；交易筆數不可視為六檔等權的統計證據。",
        "- 情境績效高度受個別大波段影響，不應用這六檔選擇『最佳情境』或修改 V2。",
        "- 這次比較的是同一批 V2 核准訊號在不同部位規則下的結果；沒有資金上限、持倉排序或真實零股成交滑價限制。",
        "- 下一個有效步驟是把 V2 凍結後套到 5/1 起完整選股母體，或做完全未看結果的逐日前向回放。",
        "",
    ]
    path = RUN / "backtest.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["validate", "replay", "report", "all"])
    args = parser.parse_args()
    if args.command == "validate":
        result = validate_decisions()
        print(json.dumps({"passed": result["passed"], "check_count": result["check_count"]}, ensure_ascii=False))
    elif args.command == "replay":
        result = replay()
        print(json.dumps({key: value["summary"] for key, value in result["variants"].items()}, ensure_ascii=False, indent=2))
    elif args.command == "report":
        print(report())
    else:
        result = replay()
        path = report()
        print(json.dumps({key: value["summary"] for key, value in result["variants"].items()}, ensure_ascii=False, indent=2))
        print(path)


if __name__ == "__main__":
    main()
