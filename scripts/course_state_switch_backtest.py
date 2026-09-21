"""Explicit INITIAL_RISK / TREND_RUNNER exits, fixed-notional portfolio replay.

Research only. Reuses the frozen 90 entry events, never ranks or resizes by
future returns. Old reports are preserved; all output is written separately.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_daily_screen_trial import THRESHOLDS, _pivot_structure
from scripts.course_exit_variant_backtest import (
    _finish_result, simulate_hybrid_variant, simulate_simple_variant,
)
from scripts.course_watchlist_backtest import load_full_frame

VERSION = "course-state-switch-v1"
LABELS = {
    "HYBRID_RUNNER": "HYBRID_RUNNER（舊混合版）",
    "MA21_ONE_CLOSE": "MA21_ONE_CLOSE（跌破21MA全出）",
    "STATE_HALF": "STATE_HALF（早期轉弱減半）",
    "STATE_WAIT": "STATE_WAIT（早期轉弱先等待）",
}
STATES = {
    "INITIAL_RISK": "INITIAL_RISK（初始風險期）",
    "EARLY_WEAKNESS": "EARLY_WEAKNESS（早期轉弱）",
    "TREND_RUNNER": "TREND_RUNNER（波段延伸期）",
    "EXIT_WARNING": "EXIT_WARNING（出場警戒）",
    "EXIT_TRIGGERED": "EXIT_TRIGGERED（已觸發出場）",
    "CLOSED": "CLOSED（交易已結束）",
}
REASONS = {
    "FIXED_DEFENSE": "FIXED_DEFENSE（跌破初始防線）",
    "EARLY_STRUCTURE": "EARLY_STRUCTURE（21MA與型態或樞紐同步失守）",
    "EARLY_HALF": "EARLY_HALF（首次21MA轉弱減半）",
    "DYNAMIC_DEFENSE": "DYNAMIC_DEFENSE（跌破成本或動態樞紐）",
    "MA21_TWO_CLOSES": "MA21_TWO_CLOSES（延伸期連兩日跌破21MA）",
    "AS_OF": "AS_OF（截至日估值）",
}


def volume_warning(frame: pd.DataFrame, position: int) -> bool:
    if position == 0:
        return False
    row, prev = frame.iloc[position], frame.iloc[position - 1]
    spread = float(row.high - row.low)
    return bool(
        row.VOL_MA20 > 0 and row.ATR14 > 0 and spread > 0
        and row.volume / row.VOL_MA20 >= 2.5
        and (row.close - row.MA21) / row.ATR14 >= 2
        and (row.close - row.low) / spread <= 0.35
        and (row.close / prev.close - 1) * 100 <= 1
    )


def simulate_state(frame: pd.DataFrame, trade: dict, mode: str,
                   pivots: dict[int, dict | None], *, use_setup_low: bool = True) -> dict:
    """Close signals, next-symbol-session fills, irreversible +2R phase.

    Levels confirmed on today's close become active tomorrow. Previously
    known levels are retained even when the underlying pivot helper filters
    them away after a break. Setup-low is the ARMED candle's low (proxy).
    """
    if mode not in {"STATE_HALF", "STATE_WAIT"}:
        raise ValueError(mode)
    index = {d.date().isoformat(): i for i, d in enumerate(frame.date)}
    start, armed = index[trade["entry_date"]], index[trade["armed_date"]]
    entry, original = float(trade["entry_price"]), float(trade["defense"])
    if not original < entry:
        raise ValueError("non-positive initial R")
    setup_low = float(frame.iloc[armed].low)
    if setup_low >= entry:
        raise ValueError("setup low not below entry")
    r = {
        "trade_id": trade["trade_id"], "code": trade["code"], "name": trade["name"],
        "entry_date": trade["entry_date"], "entry_price": entry,
        "initial_defense": original, "initial_risk_pct": trade["initial_risk_pct"],
        "variant": mode, "variant_label": LABELS[mode],
        "reference_mfe_pct": trade["mfe_pct"],
        "reference_max_price": trade["max_favorable_price"],
        "setup_low": setup_low, "setup_low_date": trade["armed_date"],
        "early_structure_uses_setup_low": use_setup_low,
        "exit_trigger_date": None, "exit_date": None, "exit_price": None,
        "exit_reason": "AS_OF", "exit_reason_label": REASONS["AS_OF"],
        "status": "OPEN", "status_label": "OPEN（持有中）",
        "partial_exits": [], "state_events": [], "daily_audit": [],
        "activation_date": None, "activation_price": entry + 2 * (entry - original),
    }
    phase = "INITIAL_RISK"
    runner = False
    half_used = False
    fraction = 1.0
    pending_half = None
    known_pivot = original
    before = pivots.get(start - 1)
    if before and before["price"] < entry:
        known_pivot = max(known_pivot, float(before["price"]))
    dynamic = original
    streak = 0
    signal_pos = None

    def event(day, state, **extra):
        r["state_events"].append({"date": day, "state": state,
                                  "label": STATES[state], **extra})

    event(trade["entry_date"], phase)
    for i in range(start, len(frame)):
        row = frame.iloc[i]
        day = row.date.date().isoformat()
        if pending_half is not None:
            price = float(row.open)
            r["partial_exits"].append({
                "signal_date": pending_half, "date": day, "price": price,
                "fraction": 0.5, "return_pct": (price / entry - 1) * 100,
                "reason": "EARLY_HALF", "reason_label": REASONS["EARLY_HALF"],
            })
            fraction = 0.5
            pending_half = None
        close = float(row.close)
        below = close < float(row.MA21)
        if not runner and row.high >= r["activation_price"]:
            runner = True
            phase = "TREND_RUNNER"
            r["activation_date"] = day
            dynamic = max(dynamic, entry, known_pivot)
            streak = 0  # Only runner-phase closes count for the two-close rule.
            event(day, phase, defense=dynamic)
        streak = streak + 1 if below else 0
        early_defense = max(original, setup_low if use_setup_low else original, known_pivot)
        reason = None
        if close < original:
            reason = "FIXED_DEFENSE"
        elif runner:
            if close < dynamic:
                reason = "DYNAMIC_DEFENSE"
            elif streak >= 2:
                reason = "MA21_TWO_CLOSES"
        elif below and close < early_defense:
            reason = "EARLY_STRUCTURE"
        elif below:
            if phase != "EARLY_WEAKNESS":
                phase = "EARLY_WEAKNESS"
                event(day, phase, structure_defense=early_defense)
            if mode == "STATE_HALF" and not half_used:
                half_used = True
                pending_half = day
        elif phase == "EARLY_WEAKNESS":
            phase = "INITIAL_RISK"
            event(day, phase, note="21MA收復；不買回已減碼部位")

        warning = runner and volume_warning(frame, i)
        if warning:
            event(day, "EXIT_WARNING", note="爆量不漲僅警戒，不產生減碼委託")
        r["daily_audit"].append({
            "date": day, "phase": phase, "phase_label": STATES[phase],
            "close": close, "ma21": float(row.MA21), "below_ma21_streak": streak,
            "initial_defense": original, "early_defense": early_defense,
            "dynamic_defense": dynamic, "remaining_fraction": fraction,
            "warning": warning, "exit_reason": reason,
        })
        if reason:
            signal_pos = i
            r.update(exit_trigger_date=day, exit_trigger_close=close,
                     exit_reason=reason, exit_reason_label=REASONS[reason])
            event(day, "EXIT_TRIGGERED", reason=REASONS[reason])
            break
        # Raise tomorrow's level only after evaluating today's active defense.
        record = pivots.get(i)
        if record:
            if record["confirmed_date"] > day:
                raise ValueError("future-confirmed pivot supplied")
            known_pivot = max(known_pivot, float(record["price"]))
        if runner:
            dynamic = max(dynamic, entry, known_pivot)

    r["final_dynamic_defense"] = dynamic
    r["pending_half_signal_date"] = pending_half
    end = frame.iloc[-1]
    price, day = float(end.close), end.date.date().isoformat()
    if signal_pos is not None:
        if signal_pos + 1 < len(frame):
            fill = frame.iloc[signal_pos + 1]
            price, day = float(fill.open), fill.date.date().isoformat()
            r.update(status="CLOSED", status_label=STATES["CLOSED"], exit_date=day, exit_price=price)
            event(day, "CLOSED", price=price)
        else:
            r.update(status="EXIT_TRIGGERED", status_label=STATES["EXIT_TRIGGERED"])
    return _finish_result(r, frame=frame, entry_position=start, signal_position=signal_pos,
                          performance_price=price, performance_date=day,
                          remaining_fraction=fraction)


@dataclass(frozen=True)
class Costs:
    commission: float = 0.001425
    tax: float = 0.003
    minimum_fee: float = 0.0
    slippage: float = 0.0

    def fee(self, gross):
        return max(self.minimum_fee, gross * self.commission) if gross else 0.0

    def buy(self, quantity, reference):
        gross = quantity * reference * (1 + self.slippage)
        return gross + self.fee(gross)

    def sell(self, quantity, reference):
        gross = quantity * reference * (1 - self.slippage)
        return gross - self.fee(gross) - gross * self.tax


def fixed_notional(rows: list[dict], frames: dict[str, pd.DataFrame],
                   start: str, as_of: str, costs: Costs, budget=10_000.0,
                   account=500_000.0) -> dict:
    """All entries, integer shares, no ranking/no capacity skips.

    Cash shortfall is reported, never silently funded or cured by dropping a
    trade. Minimum funding assumes open sales reusable before intraday buys.
    Equity denominator always remains the specified account, not minimum cash.
    """
    calendar = sorted({d.date().isoformat() for f in frames.values() for d in f.date
                       if start <= d.date().isoformat() <= as_of})
    marks = {code: {row.date.date().isoformat(): float(row.close)
                    for row in f[["date", "close"]].itertuples(index=False)}
             for code, f in frames.items()}
    events = defaultdict(list)
    trades = {}
    for r in rows:
        qty = int(budget / (r["entry_price"] * (1 + costs.slippage)))
        while qty > 0 and costs.buy(qty, r["entry_price"]) > budget:
            qty -= 1
        if qty <= 0:
            raise ValueError("budget cannot buy one share")
        t = {"trade_id": r["trade_id"], "code": r["code"], "name": r["name"],
             "entry_date": r["entry_date"], "entry_price": r["entry_price"],
             "quantity": qty, "remaining": qty, "buy_outflow": costs.buy(qty, r["entry_price"]),
             "sale_proceeds": 0.0, "gross_pnl": -qty * r["entry_price"],
             "mark": r["entry_price"], "status_label": r["status_label"],
             "reference_mfe_pct": r["reference_mfe_pct"], "fills": []}
        trades[r["trade_id"]] = t
        events[r["entry_date"]].append((1, r["trade_id"], "BUY", None, r["entry_price"]))
        for partial in r["partial_exits"]:
            events[partial["date"]].append((0, r["trade_id"], "PARTIAL", partial["fraction"], partial["price"]))
        if r["status"] == "CLOSED":
            events[r["exit_date"]].append((0, r["trade_id"], "SELL", None, r["exit_price"]))
    flow = 0.0
    min_flow = 0.0
    funding_day = None
    active = set()
    curve = []
    peak_equity = account
    drawdown = 0.0
    max_holdings = 0
    for day in calendar:
        for _, tid, kind, fraction, reference in sorted(events[day], key=lambda x: x[0]):
            t = trades[tid]
            if kind == "BUY":
                if tid in active:
                    raise ValueError("duplicate entry")
                active.add(tid)
                flow -= t["buy_outflow"]
                q = t["quantity"]
                cash_change = -t["buy_outflow"]
            else:
                if tid not in active:
                    raise ValueError("sale precedes entry")
                q = t["remaining"] if kind == "SELL" else min(t["remaining"], math.floor(t["quantity"] * fraction))
                cash_change = costs.sell(q, reference)
                flow += cash_change
                t["sale_proceeds"] += cash_change
                t["gross_pnl"] += q * reference
                t["remaining"] -= q
                if t["remaining"] == 0:
                    active.remove(tid)
            t["fills"].append({"date": day, "kind": kind, "shares": q,
                                "reference_price": reference, "cash_change": cash_change})
            if flow < min_flow:
                min_flow, funding_day = flow, day
        gross_mv = 0.0
        net_mv = 0.0
        for tid in active:
            t = trades[tid]
            t["mark"] = marks[t["code"]].get(day, t["mark"])
            gross_mv += t["remaining"] * t["mark"]
            net_mv += costs.sell(t["remaining"], t["mark"])
        equity = account + flow + net_mv
        peak_equity = max(peak_equity, equity)
        drawdown = min(drawdown, (equity / peak_equity - 1) * 100)
        max_holdings = max(max_holdings, len(active))
        curve.append({"date": day, "cash": account + flow, "net_liquidation_equity": equity,
                      "market_value": gross_mv, "holdings": len(active)})
    for t in trades.values():
        t["gross_pnl"] += t["remaining"] * t["mark"]
        t["unrealized_net_value"] = costs.sell(t["remaining"], t["mark"])
        t["net_pnl"] = t["sale_proceeds"] + t["unrealized_net_value"] - t["buy_outflow"]
        t["net_return_pct"] = t["net_pnl"] / t["buy_outflow"] * 100
        t["cost_including_liquidation"] = t["gross_pnl"] - t["net_pnl"]
    ts = list(trades.values())
    pnl = sum(t["net_pnl"] for t in ts)
    if abs(curve[-1]["net_liquidation_equity"] - account - pnl) > 1e-6:
        raise AssertionError("equity does not reconcile with trade P&L")
    profits = sum(max(0, t["net_pnl"]) for t in ts)
    losses = -sum(min(0, t["net_pnl"]) for t in ts)
    big = [t for t in ts if t["reference_mfe_pct"] >= 30]
    return {"summary": {
        "initial_cash": account, "budget_per_entry_including_fee": budget,
        "trade_count": len(ts), "net_pnl": pnl, "final_equity": account + pnl,
        "return_on_500k_pct": pnl / account * 100,
        "max_drawdown_on_500k_pct": drawdown,
        "minimum_recycled_funding": -min_flow, "funding_peak_date": funding_day,
        "funding_shortfall_vs_500k": max(0, -min_flow - account),
        "max_concurrent_positions": max_holdings,
        "net_profit_factor": None if not losses else profits / losses,
        "total_cost_including_open_liquidation": sum(t["cost_including_liquidation"] for t in ts),
        "open_count": len(active), "closed_count": len(ts) - len(active),
        "big_positive": sum(t["net_pnl"] > 0 for t in big), "big_total": len(big),
        "gross_equal_weight_mean_pct": statistics.fmean(r["performance_return_pct"] for r in rows),
    }, "trades": ts, "daily_equity": curve}


def fingerprint(path):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def build_payload(source_path: Path, legacy_path: Path) -> dict:
    source = json.loads(source_path.read_text(encoding="utf8"))
    legacy = json.loads(legacy_path.read_text(encoding="utf8"))
    as_of = date.fromisoformat(source["as_of"])
    frames, paths, pivots = {}, [], {}
    candidates = {c["code"]: c for c in source["candidates"] if c["trades"]}
    originals = [t for c in candidates.values() for t in c["trades"]]
    results = {key: [] for key in LABELS}
    pivot_only = {key: [] for key in ("STATE_HALF", "STATE_WAIT")}
    differences = []
    for code, c in candidates.items():
        frame, path = load_full_frame(c["symbol"], as_of)
        frames[code] = frame
        paths.append(fingerprint(path))
        start = min(t["entry_date"] for t in c["trades"])
        begin = next(i for i, d in enumerate(frame.date) if d.date().isoformat() == start)
        # Recompute every snapshot on a truncated history: never future pivots.
        pivots[code] = {i: _pivot_structure(frame.iloc[:i + 1].copy(), THRESHOLDS)["defense"]
                        for i in range(begin - 1, len(frame))}
    prior = {key: {r["trade_id"]: r for r in legacy["results"][key]}
             for key in ("HYBRID_RUNNER", "MA21_ONE_CLOSE")}
    for t in originals:
        f, p = frames[t["code"]], pivots[t["code"]]
        results["HYBRID_RUNNER"].append(simulate_hybrid_variant(
            frame=f, trade=t, pivot_defenses={i: None if v is None else v["price"] for i, v in p.items()}))
        results["MA21_ONE_CLOSE"].append(simulate_simple_variant(frame=f, trade=t, variant="MA21_ONE_CLOSE"))
        for key in ("HYBRID_RUNNER", "MA21_ONE_CLOSE"):
            r, old = results[key][-1], prior[key][t["trade_id"]]
            if any(r.get(k) != old.get(k) for k in ("exit_date", "exit_trigger_date", "status")) or abs(r["performance_return_pct"] - old["performance_return_pct"]) > 0.001:
                differences.append({"trade_id": t["trade_id"], "variant": key})
        for key in ("STATE_HALF", "STATE_WAIT"):
            results[key].append(simulate_state(f, t, key, p))
            pivot_only[key].append(simulate_state(f, t, key, p, use_setup_low=False))
    if differences:
        raise ValueError(f"legacy replay changed, investigate before comparing: {differences}")
    scenarios = {"BASE": Costs(), "MIN20": Costs(minimum_fee=20),
                 "MIN20_SLIP10BP": Costs(minimum_fee=20, slippage=0.001)}
    portfolios = {scenario: {key: fixed_notional(rs, frames, source["selection_window"]["start"], source["as_of"], costs)
                             for key, rs in results.items()}
                  for scenario, costs in scenarios.items()}
    definition_sensitivity = {
        "definition": "confirmed_pivot_only_no_ARMED_candle_low; diagnostic_not_selected_after_optimization",
        "exit_results": pivot_only,
        "portfolios": {scenario: {key: fixed_notional(rs, frames, source["selection_window"]["start"], source["as_of"], costs)
                                   for key, rs in pivot_only.items()}
                       for scenario, costs in scenarios.items()},
    }
    return {"version": VERSION, "generated_at": datetime.now().astimezone().isoformat(),
            "as_of": source["as_of"], "selection_window": source["selection_window"],
            "input_audit": [fingerprint(source_path), fingerprint(legacy_path), *paths],
            "legacy_mismatches": differences,
            "parameters": {"activation_r": 2, "early_half_fraction": 0.5,
                           "pattern_low": "ARMED_candle_low_proxy",
                           "early_structure": "close_below_MA21_and_max(setup_low,initial_defense,retained_confirmed_pivot)",
                           "new_pivot_effective": "following_session",
                           "runner_touch": "high_reaches_2R; cost_defense_applies_at_that_close",
                           "runner_ma21_streak": "count_from_activation_session",
                           "no_rebuy_no_add": True, "volume_warning_only": True,
                           "entry_ids": "same_frozen_90_no_new_reentries_no_ranking",
                           "cost_scenarios": {s: asdict(c) for s, c in scenarios.items()},
                           "same_day_cash_reuse": "sell_at_open_before_buy_trigger; not_settlement_simulation"},
            "exit_results": results, "portfolios": portfolios,
            "early_structure_definition_sensitivity": definition_sensitivity}


def render(payload):
    base = payload["portfolios"]["BASE"]
    lines = [f"# 狀態切換出場回測｜截至 {payload['as_of']}", "",
             "固定同一組90筆雷達啟蒙觸發。每筆固定1萬元（含買進手續費）、整數股零股、全部買入；初始帳戶50萬元。沒有Top N、固定風險縮放或加碼。", "",
             "## 同口徑結果", "",
             "| 出場版本 | 未扣成本單筆等權平均 | 帳戶淨損益 | 50萬元帳戶報酬 | 最大回撤 | 最低周轉資金 | 最大同持 | 淨PF | 大波段正獲利 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for key, p in base.items():
        s = p["summary"]
        lines.append(f"| {LABELS[key]} | {s['gross_equal_weight_mean_pct']:.2f}% | {s['net_pnl']:,.0f} | {s['return_on_500k_pct']:.2f}% | {s['max_drawdown_on_500k_pct']:.2f}% | {s['minimum_recycled_funding']:,.0f} | {s['max_concurrent_positions']} | {s['net_profit_factor']:.2f} | {s['big_positive']}/{s['big_total']} |")
    lines += ["", "帳戶報酬分母一律50萬元。最低周轉資金只是這段歷史的最大現金缺口，不拿它當報酬分母；尚未出場部位按9/4收盤扣估計賣出成本估值，淨損益不是全數已實現。", "",
              "## 新版規則與明確代理", "",
              "- INITIAL_RISK（初始風險期）：收盤跌破原始防線，次日開盤全出。首次收盤低於21MA進入 EARLY_WEAKNESS（早期轉弱）；STATE_HALF（早期減半版）次日賣原始股數一半，向下取整且整筆交易只減一次；STATE_WAIT（等待版）不減。收復21MA回到初始風險期，不買回。",
              "- 未到+2R，若收盤低於21MA，且同時低於型態低點或已知樞紐，次日全出。型態低點暫用ARMED日K低點，不宣稱等於課程完整型態辨識；已知防線保留不因失守而消失。",
              "- TREND_RUNNER（波段延伸期）：盤中最高到原始進場價+2倍初始每股風險後，狀態不可逆；該日收盤已適用成本防線。之後收盤跌破只升不降的動態防線，或延伸期內連兩日收盤低於21MA，次日開盤全出。",
              "- 當日收盤才確認的新樞紐，自下一日生效；不回填。成本防線是名目買價，未保證扣成本或跳空後不虧。",
              "- EXIT_WARNING（出場警戒）：沿用舊爆量不漲偵測，只記錄警戒，不減碼。完整每日狀態與委託事件保存於JSON。", "",
              "## 零股成本敏感度", "",
              "| 版本 | 比例費用、無滑價 | 最低20元/單、無滑價 | 最低20元/單、單邊0.1%不利滑價 |",
              "|---|---:|---:|---:|"]
    for key in LABELS:
        values = [payload["portfolios"][s][key]["summary"]["net_pnl"] for s in ("BASE", "MIN20", "MIN20_SLIP10BP")]
        lines.append(f"| {LABELS[key]} | " + " | ".join(f"{v:,.0f}元" for v in values) + " |")
    lines += ["", "基礎：買賣手續費各0.1425%、賣出稅0.3%。20元最低費與0.1%滑價是壓力情境，不代表使用者券商實際條件。零股不保證在整股OHLC價成交。費用依[證交所交易機制](https://www.twse.com.tw/en/products/system/trading.html)，最低費情境參考[零股交易說明](https://www.twse.com.tw/market_insights/zh/detail/ff8080818bf08529018bf6c8a5690016)。", "",
              "## 五筆參考大波段逐筆（每筆原始投入約1萬元）", "",
              "| 股票 | 舊混合版淨損益 | 新減半版淨損益 | 新等待版淨損益 | 新減半版出場原因 |",
              "|---|---:|---:|---:|---|"]
    indexes = {k: {t["trade_id"]: t for t in v["trades"]} for k, v in base.items()}
    for r in payload["exit_results"]["STATE_HALF"]:
        if r["reference_mfe_pct"] < 30:
            continue
        values = [indexes[k][r["trade_id"]]["net_pnl"] for k in ("HYBRID_RUNNER", "STATE_HALF", "STATE_WAIT")]
        lines.append(f"| {r['code']} {r['name']} | " + " | ".join(f"{v:,.0f}" for v in values) + f" | {r['exit_reason_label']} |")
    lines += ["", "## 型態低點定義敏感度（不是已驗證的新策略）", "",
              "主表把進場型態低點轉譯成ARMED那根訊號K的低點。為分辨『狀態切換』與『單根訊號K低點過緊』的影響，另只採已確認樞紐、不採訊號K低點，其他規則不變。此為定義稽核，不能事後選好看的版本宣称有樣本外獲利能力。", "",
              "| 僅確認樞紐版本 | 淨損益 | 50萬元報酬 | 最大回撤 | 大波段正獲利 |", "|---|---:|---:|---:|---:|"]
    for key, p in payload["early_structure_definition_sensitivity"]["portfolios"]["BASE"].items():
        s = p["summary"]
        lines.append(f"| {LABELS[key]}／僅確認樞紐 | {s['net_pnl']:,.0f} | {s['return_on_500k_pct']:.2f}% | {s['max_drawdown_on_500k_pct']:.2f}% | {s['big_positive']}/{s['big_total']} |")
    lines += ["", "## 限制與稽核", "",
              "- 只保留了雷達歷史，並非所有策略當時完整名單。沿用原90筆固定進場事件；提前出場後不新增重進，不重新訓練或挑選股票。",
              "- 此為課程規則的程式代理，不是AI逐張圖判讀；初始型態低點定義與+2R當日防線生效時序均須前向驗證。",
              "- 使用本地OHLC，未模擬漲跌停排隊、停牌成交、零股價差及除權息現金流；滑價壓力情境不能取代逐筆零股資料。",
              "- 本次含原進場成本未必符合歷史最小跳動單位的情形，固定沿用以隔離出場影響；因此不能宣稱為可逐筆成交的實盤回測。",
              "- 所有版本皆重播；舊版日期、狀態、報酬與保存報告差異為0筆。程式與價格來源SHA256記於JSON。",
              f"- 方法版本：{VERSION}"]
    return "\n".join(lines) + "\n"


def main():
    folder = ROOT / "reports/course_backtest/2026-09-04"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=folder / "radar_full_history_v2/backtest.json")
    parser.add_argument("--legacy", type=Path, default=folder / "exit_variants_v1/comparison.json")
    parser.add_argument("--output-dir", type=Path, default=folder / "state_switch_v1")
    args = parser.parse_args()
    p = build_payload(args.source, args.legacy)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "comparison.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf8")
    (args.output_dir / "comparison.md").write_text(render(p), encoding="utf8")
    print(json.dumps({k: v["summary"] for k, v in p["portfolios"]["BASE"].items()}, ensure_ascii=False, indent=2))
    print(args.output_dir / "comparison.md")


if __name__ == "__main__":
    main()
