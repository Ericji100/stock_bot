"""Compare causal touch, close-confirmation, and retest entry semantics.

The opportunity set is the corrected 89 executable entries from the original
90-entry radar cohort.  The original ARMED plans are frozen; variants may skip
an opportunity but never introduce a new stock from future returns.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_add_vs_no_add_backtest import (  # noqa: E402
    Costs,
    MODELS,
    _make_economic_frame,
    simulate_trade,
    summarize,
)
from scripts.course_corporate_action_backtest import event_dates, load_inputs  # noqa: E402
from scripts.course_corporate_action_data import AS_OF, RUN, read  # noqa: E402
from scripts.course_radar_trigger_backtest import load_full_frame  # noqa: E402
from scripts.course_watchlist_backtest import enlightenment_snapshot  # noqa: E402


METHOD_VERSION = "course-entry-variant-v1"
SOURCE_V2 = ROOT / "reports/course_backtest/2026-09-04/radar_full_history_v2/backtest.json"
LABELS = {
    "TOUCH_BASE": "TOUCH_BASE（原盤中觸價）",
    "TOUCH_RISK": "TOUCH_RISK（觸價＋實際風險複核）",
    "TOUCH_RR2": "TOUCH_RR2（觸價＋風險＋2R空間）",
    "CLOSE_RISK": "CLOSE_RISK（收盤確認＋實際風險）",
    "CLOSE_RR2": "CLOSE_RR2（收盤確認＋風險＋2R空間）",
    "RETEST_RISK": "RETEST_RISK（回測守穩＋實際風險）",
    "RETEST_RR2": "RETEST_RR2（回測守穩＋風險＋2R空間）",
}
CONFIGS = {
    "TOUCH_BASE": {"method": "TOUCH", "risk_check": False, "rr_min": None},
    "TOUCH_RISK": {"method": "TOUCH", "risk_check": True, "rr_min": None},
    "TOUCH_RR2": {"method": "TOUCH", "risk_check": True, "rr_min": 2.0},
    "CLOSE_RISK": {"method": "CLOSE", "risk_check": True, "rr_min": None},
    "CLOSE_RR2": {"method": "CLOSE", "risk_check": True, "rr_min": 2.0},
    "RETEST_RISK": {"method": "RETEST", "risk_check": True, "rr_min": None},
    "RETEST_RR2": {"method": "RETEST", "risk_check": True, "rr_min": 2.0},
}


def _basis_by_day(raw: pd.DataFrame, actions: list[dict[str, Any]], anchor: str) -> dict[str, tuple[float, float]]:
    """Economic value = unit_factor * raw price + cash, anchored at ARMED."""
    mapped = event_dates(actions, raw)
    unit_factor, cash = 1.0, 0.0
    result: dict[str, tuple[float, float]] = {}
    for row in raw.itertuples(index=False):
        day = row.date.date().isoformat()
        if day > anchor:
            for event in mapped.get(day, []):
                cash += unit_factor * float(event.get("cash", 0.0))
                unit_factor *= float(event.get("ratio", 1.0))
        result[day] = (unit_factor, cash)
    return result


def _raw_level(economic_level: float, basis: tuple[float, float]) -> float:
    unit_factor, cash = basis
    return (economic_level - cash) / unit_factor


def _entry_quality(
    *,
    economic_entry: float,
    defense: float,
    atr: float,
    measured_target: float,
    risk_check: bool,
    rr_min: float | None,
) -> tuple[bool, str | None, dict[str, float]]:
    risk = economic_entry - defense
    if risk <= 0 or economic_entry <= 0 or atr <= 0:
        return False, "INVALID_RISK（實際風險非正值）", {}
    risk_pct = risk / economic_entry * 100.0
    risk_atr = risk / atr
    reward = measured_target - economic_entry
    reward_risk = reward / risk
    metrics = {
        "economic_entry": economic_entry,
        "risk_pct": risk_pct,
        "risk_atr": risk_atr,
        "measured_target": measured_target,
        "reward_risk_proxy": reward_risk,
    }
    if risk_check and (risk_pct > 10.0 or risk_atr > 2.5):
        return False, "ACTUAL_RISK_TOO_WIDE（實際成交風險過大）", metrics
    if rr_min is not None and reward_risk < rr_min:
        return False, "HEADROOM_BELOW_2R（預估空間不足2R）", metrics
    return True, None, metrics


def _safe_snapshot(frame: pd.DataFrame, index: int) -> dict[str, Any] | None:
    try:
        return enlightenment_snapshot(frame.iloc[: index + 1].copy())
    except ValueError:
        return None


def _next_index(index: int, frame: pd.DataFrame) -> int | None:
    return index + 1 if index + 1 < len(frame) else None


def _decision_target(frame: pd.DataFrame, signal_index: int, trigger: float) -> float:
    """Causal equal-range target recomputed at the actual entry decision."""
    recent = frame.iloc[max(0, signal_index - 9): signal_index + 1]
    height = float(recent.high.max() - recent.low.min())
    base = max(trigger, float(frame.iloc[signal_index].close))
    return base + height


def propose_entry(opportunity: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    frame = opportunity["economic_frame"]
    raw_prices = opportunity["raw_prices"]
    basis = opportunity["basis"]
    armed = opportunity["armed_date"]
    index_by_day = {value.date().isoformat(): index for index, value in enumerate(frame.date)}
    armed_index = index_by_day[armed]
    trigger = float(opportunity["trigger"])
    defense = float(opportunity["defense"])
    chase_cap = float(opportunity["chase_cap"])
    armed_target = _decision_target(frame, armed_index, trigger)
    method = config["method"]

    def make(fill_index: int, raw_reference: float, measured_target: float,
             reason_context: dict[str, Any]) -> dict[str, Any]:
        row = frame.iloc[fill_index]
        day = row.date.date().isoformat()
        unit_factor, cash = basis[day]
        actual_reference = float(raw_reference)
        economic_entry = unit_factor * actual_reference + cash
        accepted, reason, quality = _entry_quality(
            economic_entry=economic_entry,
            defense=defense,
            atr=float(row.ATR14),
            measured_target=measured_target,
            risk_check=bool(config["risk_check"]),
            rr_min=config["rr_min"],
        )
        if not accepted:
            return {"status": "SKIPPED", "reason": reason, "quality": quality, **reason_context}
        raw_defense = _raw_level(defense, basis[day])
        if actual_reference <= raw_defense:
            return {"status": "SKIPPED", "reason": "ENTRY_BELOW_DEFENSE（成交價不在防線上方）",
                    "quality": quality, **reason_context}
        return {
            "status": "ENTRY", "entry_date": day, "entry_price": actual_reference,
            "initial_defense": raw_defense, "quality": quality,
            "measured_target_definition": "實際決日已知突破基準＋當時最近10根日K高低區間",
            **reason_context,
        }

    first = _next_index(armed_index, frame)
    if first is None:
        return {"status": "SKIPPED", "reason": "NO_FOLLOWING_SESSION（沒有後續交易日）"}
    first_row = frame.iloc[first]
    first_day = first_row.date.date().isoformat()
    first_basis = basis[first_day]

    if method == "TOUCH":
        if float(first_row.open) > chase_cap:
            return {"status": "SKIPPED", "reason": "OPEN_ABOVE_CHASE_CAP（開盤超過追價上限）"}
        if float(first_row.open) <= defense:
            return {"status": "SKIPPED", "reason": "OPEN_BELOW_DEFENSE（開盤跌破防線）"}
        if float(first_row.high) < trigger:
            return {"status": "SKIPPED", "reason": "TRIGGER_NOT_TOUCHED（盤中未觸發）"}
        raw_trigger = _raw_level(trigger, first_basis)
        raw_fill = max(float(raw_prices[first_day]["open"]), raw_trigger)
        return make(first, raw_fill, armed_target,
                    {"trigger_type": "INTRADAY_TOUCH（盤中觸價）"})

    if method == "CLOSE":
        snapshot = _safe_snapshot(frame, first)
        if snapshot and snapshot["structural_invalid"]:
            return {"status": "SKIPPED", "reason": "STRUCTURE_INVALID（確認日結構失效）"}
        if float(first_row.close) < trigger:
            return {"status": "SKIPPED", "reason": "CLOSE_BELOW_TRIGGER（收盤未站上觸發價）"}
        if float(first_row.close) > chase_cap:
            return {"status": "SKIPPED", "reason": "CLOSE_ABOVE_CHASE_CAP（確認收盤超過追價上限）"}
        if float(first_row.close) <= defense:
            return {"status": "SKIPPED", "reason": "CLOSE_BELOW_DEFENSE（確認收盤跌破防線）"}
        fill = _next_index(first, frame)
        if fill is None:
            return {"status": "SKIPPED", "reason": "NO_ENTRY_SESSION（確認後沒有交易日）"}
        row = frame.iloc[fill]
        day = row.date.date().isoformat()
        entry_cap = float(first_row.close) + 0.5 * float(first_row.ATR14)
        if float(row.open) > entry_cap:
            return {"status": "SKIPPED", "reason": "CONFIRMED_OPEN_TOO_HIGH（確認後開盤過高）"}
        if float(row.open) <= defense:
            return {"status": "SKIPPED", "reason": "CONFIRMED_OPEN_BELOW_DEFENSE（確認後開盤跌破防線）"}
        return make(fill, float(raw_prices[day]["open"]), _decision_target(frame, first, trigger), {
            "trigger_type": "CLOSE_CONFIRM（收盤確認）", "confirmation_date": first_day,
        })

    if method != "RETEST":
        raise ValueError(method)
    breakout_index = None
    breakout_deadline = min(len(frame) - 1, armed_index + 5)
    for index in range(first, breakout_deadline + 1):
        row = frame.iloc[index]
        snapshot = _safe_snapshot(frame, index)
        if snapshot and snapshot["structural_invalid"]:
            return {"status": "SKIPPED", "reason": "STRUCTURE_INVALID（等待突破時結構失效）"}
        if float(row.close) >= trigger:
            breakout_index = index
            break
        if float(row.close) <= defense:
            return {"status": "SKIPPED", "reason": "DEFENSE_FAILED_BEFORE_BREAKOUT（突破前防線失效）"}
    if breakout_index is None:
        return {"status": "SKIPPED", "reason": "NO_CLOSE_BREAKOUT_IN_5_BARS（5日內未收盤突破）"}

    retest_deadline = min(len(frame) - 2, breakout_index + 10)
    for index in range(breakout_index + 1, retest_deadline + 1):
        row = frame.iloc[index]
        snapshot = _safe_snapshot(frame, index)
        if snapshot and snapshot["structural_invalid"]:
            return {"status": "SKIPPED", "reason": "STRUCTURE_INVALID（等待回測時結構失效）"}
        if float(row.close) <= defense:
            return {"status": "SKIPPED", "reason": "DEFENSE_FAILED_DURING_RETEST（等待回測時防線失效）"}
        touched_zone = float(row.low) <= trigger + 0.5 * float(row.ATR14)
        held = float(row.close) >= trigger and float(row.close) > float(row.MA21)
        relaunched = float(row.close) > float(row.open)
        if not (touched_zone and held and relaunched):
            continue
        fill = index + 1
        fill_row = frame.iloc[fill]
        fill_day = fill_row.date.date().isoformat()
        entry_cap = float(row.close) + 0.5 * float(row.ATR14)
        if float(fill_row.open) > entry_cap or float(fill_row.open) <= defense:
            continue
        return make(fill, float(raw_prices[fill_day]["open"]), _decision_target(frame, index, trigger), {
            "trigger_type": "RETEST_ENTRY（突破後回測守穩）",
            "breakout_date": frame.iloc[breakout_index].date.date().isoformat(),
            "retest_date": row.date.date().isoformat(),
        })
    return {"status": "SKIPPED", "reason": "NO_VALID_RETEST_IN_10_BARS（10日內沒有合格回測）"}


def _prepare_opportunities() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    corrected = read(RUN / "backtest.json")
    old = read(SOURCE_V2)
    manifest = read(RUN / "input_manifest.json")
    old_trades = {trade["trade_id"]: trade for candidate in old["candidates"] for trade in candidate["trades"]}
    old_candidates = {candidate["code"]: candidate for candidate in old["candidates"]}
    items = {str(item["code"]): item for item in manifest["items"]}
    loaded: dict[str, tuple[pd.DataFrame, list[dict[str, Any]]]] = {}
    old_frames: dict[str, pd.DataFrame] = {}
    audits = []
    opportunities = []
    for fixed in corrected["fixed_original_entries"]:
        code = str(fixed["code"])
        legacy = old_trades[fixed["trade_id"]]
        if code not in loaded:
            raw, actions, _ = load_inputs(items[code])
            loaded[code] = (raw, actions)
            old_frames[code], _ = load_full_frame(old_candidates[code]["symbol"], pd.Timestamp(AS_OF).date())
        raw, actions = loaded[code]
        armed = legacy["armed_date"]
        economic, _, raw_prices = _make_economic_frame(raw, actions, armed)
        economic = economic[economic.date.dt.strftime("%Y-%m-%d") <= corrected["as_of"]].reset_index(drop=True)
        basis = _basis_by_day(raw, actions, armed)
        old_frame = old_frames[code]
        old_close = float(old_frame.loc[old_frame.date.dt.strftime("%Y-%m-%d") == armed, "close"].iloc[0])
        corrected_armed_close = float(raw.loc[raw.date.dt.strftime("%Y-%m-%d") == armed, "close"].iloc[0])
        scale = corrected_armed_close / old_close
        entry_basis = basis[fixed["entry_date"]]
        defense_economic = entry_basis[0] * float(fixed["initial_defense"]) + entry_basis[1]
        trigger = float(legacy["trigger_price"]) * scale
        chase_cap = float(legacy["chase_cap"]) * scale
        scaled_defense = float(legacy["defense"]) * scale
        audits.append({
            "trade_id": fixed["trade_id"], "armed_date": armed, "scale": scale,
            "defense_economic_from_corrected_entry": defense_economic,
            "defense_scaled_from_frozen_plan": scaled_defense,
            "defense_difference": defense_economic - scaled_defense,
        })
        opportunities.append({
            "fixed": fixed, "legacy": legacy, "raw": raw, "actions": actions,
            "economic_frame": economic, "raw_prices": raw_prices, "basis": basis,
            "armed_date": armed, "trigger": trigger, "chase_cap": chase_cap,
            "defense": defense_economic,
        })
    return opportunities, audits


def _model_result(opportunities: list[dict[str, Any]], model_code: str, costs: Costs) -> dict[str, Any]:
    config = CONFIGS[model_code]
    proposals = []
    for opportunity in opportunities:
        if model_code.startswith("TOUCH"):
            fixed = opportunity["fixed"]
            if model_code == "TOUCH_BASE":
                proposal = {
                    "status": "ENTRY", "entry_date": fixed["entry_date"],
                    "entry_price": float(fixed["entry_price"]),
                    "initial_defense": float(fixed["initial_defense"]),
                    "trigger_type": "INTRADAY_TOUCH（原固定進場事件）",
                    "quality": {},
                }
            else:
                frame = opportunity["economic_frame"]
                index_by_day = {value.date().isoformat(): index for index, value in enumerate(frame.date)}
                entry_index = index_by_day[fixed["entry_date"]]
                armed_index = index_by_day[opportunity["armed_date"]]
                unit_factor, cash = opportunity["basis"][fixed["entry_date"]]
                economic_entry = unit_factor * float(fixed["entry_price"]) + cash
                target = _decision_target(frame, armed_index, float(opportunity["trigger"]))
                accepted_quality, reason, quality = _entry_quality(
                    economic_entry=economic_entry, defense=float(opportunity["defense"]),
                    atr=float(frame.iloc[entry_index].ATR14), measured_target=target,
                    risk_check=True, rr_min=config["rr_min"],
                )
                proposal = ({
                    "status": "ENTRY", "entry_date": fixed["entry_date"],
                    "entry_price": float(fixed["entry_price"]),
                    "initial_defense": float(fixed["initial_defense"]),
                    "trigger_type": "INTRADAY_TOUCH（原固定進場事件）",
                    "quality": quality,
                    "measured_target_definition": "ARMED決策日已知突破基準＋最近10根日K高低區間",
                } if accepted_quality else {
                    "status": "SKIPPED", "reason": reason, "quality": quality,
                })
        else:
            proposal = propose_entry(opportunity, config)
        proposals.append((opportunity, proposal))

    accepted: list[dict[str, Any]] = []
    skipped = []
    active_by_code: dict[str, dict[str, Any]] = {}
    for opportunity, proposal in sorted(proposals, key=lambda item: (
        item[1].get("entry_date", "9999-12-31"), item[0]["fixed"]["trade_id"]
    )):
        fixed = opportunity["fixed"]
        if proposal["status"] != "ENTRY":
            skipped.append({"trade_id": fixed["trade_id"], "code": fixed["code"], **proposal})
            continue
        prior = active_by_code.get(str(fixed["code"]))
        if prior and (not prior["exit_date"] or prior["exit_date"] >= proposal["entry_date"]):
            skipped.append({
                "trade_id": fixed["trade_id"], "code": fixed["code"], "status": "SKIPPED",
                "reason": "DUPLICATE_OPEN（同股前一交易仍持有）", "proposal": proposal,
            })
            continue
        simulated_input = {
            **fixed,
            "entry_date": proposal["entry_date"], "entry_price": proposal["entry_price"],
            "initial_defense": proposal["initial_defense"], "defense": proposal["initial_defense"],
        }
        result = simulate_trade(
            trade=simulated_input, raw=opportunity["raw"], actions=opportunity["actions"],
            model=MODELS["ONE_SHOT"], costs=costs, as_of=AS_OF,
        )
        result["entry_variant"] = model_code
        result["entry_variant_label"] = LABELS[model_code]
        result["entry_proposal"] = proposal
        accepted.append(result)
        active_by_code[str(fixed["code"])] = result

    summary = summarize(accepted)
    returns = [float(trade["net_return_on_campaign_budget_pct"]) for trade in accepted]
    win = [value for value in returns if value > 0]
    loss = [value for value in returns if value < 0]
    summary.update({
        "opportunity_count": len(opportunities), "executed_trade_count": len(accepted),
        "coverage_pct": len(accepted) / len(opportunities) * 100.0,
        "skipped_count": len(skipped),
        "skip_reason_counts": dict(Counter(item["reason"] for item in skipped)),
        "average_winner_pct": None if not win else statistics.fmean(win),
        "average_loser_pct": None if not loss else statistics.fmean(loss),
        "payoff_ratio": None if not win or not loss else statistics.fmean(win) / abs(statistics.fmean(loss)),
    })
    return {"summary": summary, "trades": accepted, "skipped": skipped}


def build() -> dict[str, Any]:
    opportunities, audits = _prepare_opportunities()
    costs = Costs()
    results = {}
    for model_code in CONFIGS:
        print(f"Replay {model_code}", flush=True)
        results[model_code] = _model_result(opportunities, model_code, costs)
    return {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": AS_OF,
        "cohort": "原90筆固定機會，排除6113颱風休市日後89筆",
        "parameters": {
            "account": 500_000.0, "budget_per_trade": 10_000.0,
            "position": "ONE_SHOT（不加碼、一次建立）",
            "exit": "與add_vs_no_add_v1相同狀態切換",
            "actual_risk_limits": {"pct": 10.0, "atr": 2.5},
            "headroom_proxy": "各版本實際決策日的已知突破基準＋最近10根日K高低區間；要求距實際成交至少2R",
            "close_confirm": "ARMED下一交易日收盤站上觸發且不超過原追價上限；再下一交易日開盤",
            "retest": "5日內收盤突破；其後10日內低點回到觸發價+0.5ATR內、收盤守觸發及MA21且收紅；次日開盤",
            "costs": costs.__dict__, "configs": CONFIGS,
        },
        "plan_basis_audit": audits,
        "results": results,
    }


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    checks = []
    def add(name: str, passed: bool, detail: Any):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
    add("opportunity_count", all(result["summary"]["opportunity_count"] == 89
                                 for result in payload["results"].values()), 89)
    add("touch_base_all_entries", payload["results"]["TOUCH_BASE"]["summary"]["executed_trade_count"] == 89,
        payload["results"]["TOUCH_BASE"]["summary"]["executed_trade_count"])
    add("defense_basis_consistency", max(abs(float(row["defense_difference"]))
                                         for row in payload["plan_basis_audit"]) < 0.02,
        max(abs(float(row["defense_difference"])) for row in payload["plan_basis_audit"]))
    for model_code, result in payload["results"].items():
        trades = result["trades"]
        ids = [trade["trade_id"] for trade in trades]
        add(f"{model_code}.unique_ids", len(ids) == len(set(ids)), len(ids))
        add(f"{model_code}.capital_funded", result["summary"]["funding_shortfall"] == 0,
            result["summary"]["minimum_cash_without_dividend_reinvestment"])
        add(f"{model_code}.equity_reconciles",
            abs(result["summary"]["final_net_liquidation_equity"]
                - 500_000.0 - sum(float(trade["net_pnl"]) for trade in trades)) < 1e-6,
            result["summary"]["final_net_liquidation_equity"])
        if model_code.endswith("RR2"):
            bad = [trade["trade_id"] for trade in trades
                   if float(trade["entry_proposal"]["quality"]["reward_risk_proxy"]) < 2.0 - 1e-9]
            add(f"{model_code}.rr2", not bad, len(bad))
        if model_code != "TOUCH_BASE":
            bad = [trade["trade_id"] for trade in trades
                   if float(trade["entry_proposal"]["quality"]["risk_pct"]) > 10.0 + 1e-9
                   or float(trade["entry_proposal"]["quality"]["risk_atr"]) > 2.5 + 1e-9]
            add(f"{model_code}.actual_risk", not bad, len(bad))
    failures = [row for row in checks if not row["passed"]]
    return {"check_count": len(checks), "passed_count": len(checks)-len(failures),
            "failed_count": len(failures), "checks": checks}


def _f(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def render(payload: dict[str, Any]) -> str:
    lines = [
        f"# 三種進場方式回測｜截至 {payload['as_of']}", "",
        "> 除權息修正後原90筆機會，排除休市日無效進場後89筆。固定不加碼、每筆1萬元上限、同一套狀態出場；沒有使用未來MFE選擇交易。", "",
        "## 規則", "",
        "- TOUCH（盤中觸價）：ARMED後下一交易日盤中碰觸訊號K高點即成交。TOUCH_BASE保留原規則；TOUCH_RISK另用實際成交價重算10%／2.5ATR風險。",
        "- CLOSE（收盤確認）：下一交易日收盤站上觸發價且未超過原追價上限，再下一交易日開盤進場。",
        "- RETEST（回測守穩）：5日內先收盤突破；10日內回測觸發區，收盤守住觸發價與21MA且收紅，再下一交易日開盤。",
        "- RR2版本另要求：在各版本實際決策日，用當時已知突破基準加最近10日區間計算等幅滿足點，距實際成交價至少2R。這是透明代理，不宣稱是課程唯一目標算法。", "",
        "## 結果", "",
        "| 版本 | 進場/89 | 帳戶淨損益 | 50萬報酬 | 最大回撤 | 正報酬率 | 平均贏 | 平均輸 | 賺賠比 | PF |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for code in CONFIGS:
        s = payload["results"][code]["summary"]
        lines.append(
            f"| {LABELS[code]} | {s['executed_trade_count']}/89 | {s['net_pnl']:,.0f} | {s['return_on_500k_pct']:+.2f}% | "
            f"{s['max_drawdown_pct']:.2f}% | {_f(s['positive_trade_rate_pct'])}% | {_f(s['average_winner_pct'])}% | "
            f"{_f(s['average_loser_pct'])}% | {_f(s['payoff_ratio'])} | {_f(s['profit_factor'])} |"
        )
    lines += ["", "## 進場淘汰原因", ""]
    for code in CONFIGS:
        reasons = payload["results"][code]["summary"]["skip_reason_counts"]
        text = "、".join(f"{key} {value}筆" for key, value in sorted(reasons.items())) or "無"
        lines.append(f"- {LABELS[code]}：{text}。")
    lines += [
        "", "## 限制", "",
        "- 不同版本進場筆數不同；低回撤或較高帳戶損益可能只是少做交易，必須同時看覆蓋率、每筆賺賠比與漏掉的大波段。",
        "- 日K無法辨認盤中高低先後；TOUCH沿用原本『碰價視為成交』假設。",
        "- 10日等幅滿足點只是可回測報酬空間代理；會在各自決策日因新資料更新，但不使用決策日之後資料。若課程另有更明確滿足點公式，應替換後重跑。",
        "- 未納入大盤狀態硬門檻、委託簿、漲跌停排隊、實際零股價差與券商最低手續費。",
        "- 樣本短且同時用於發現問題及比較，結果仍需樣本外與前向驗證。", "",
        f"方法版本：`{payload['method_version']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=RUN / "entry_variants_v1")
    args = parser.parse_args()
    payload = build()
    validation = validate(payload)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "comparison.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "comparison.md").write_text(render(payload), encoding="utf-8")
    (args.output_dir / "validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    if validation["failed_count"]:
        raise AssertionError(f"validation failed: {validation['failed_count']}")
    print((args.output_dir / "comparison.md").resolve())
    for code in CONFIGS:
        summary = payload["results"][code]["summary"].copy()
        summary.pop("equity_curve", None)
        print(code, json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
