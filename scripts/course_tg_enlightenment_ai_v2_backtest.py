"""Replay the 2026-05-01 Telegram selection universe with enlightenment AI v2.

The batch policy is deliberately causal: every entry decision sees only bars
available at the signal close.  The policy is a frozen, auditable translation
of the six-stock AI calibration rather than a claim that 747 charts were
individually judged by a fresh LLM call.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.enlightenment_ai_small_test import (  # noqa: E402
    _buy_cost,
    _sell_proceeds,
    _tw_stock_tick,
    add_indicators,
    candidate_reason,
)


METHOD_VERSION = "tg-enlightenment-ai-v2-batch-policy-v1"
AS_OF = "2026-09-04"
FETCH_FROM = "2019-01-01"
CATALOG = ROOT / "reports/course_backtest/2026-09-05/tg_selection_catalog_v1/catalog.json"
SOURCE_MANIFEST = ROOT / "reports/course_backtest/2026-09-05/tg_strategy_entry_matrix_v1/input_manifest.json"
RUN = ROOT / "reports/course_backtest/2026-09-06/tg_enlightenment_ai_v2"
PRICE_DIR = RUN / "sources/prices"
EVENT_DIR = RUN / "sources/events"
DECISIONS_PATH = RUN / "batch_decisions.json"
RESULT_PATH = RUN / "backtest.json"
REPORT_PATH = RUN / "backtest.md"
RULE_DOC_PATH = ROOT / "docs/enlightenment-ai-judgement-v2.md"
RULE_CONFIG_PATH = ROOT / "config/enlightenment_ai_rules_v2.json"

BUY_COMMISSION = 0.001425
SELL_COMMISSION = 0.001425
SELL_TAX = 0.003
NOMINAL_PER_TRANCHE = 10_000.0
MIN_PRE_MONITOR_BARS = 750


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_frame(payload: dict[str, Any]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(result["timestamp"], unit="s", utc=True).date,
            "raw_open": quote["open"],
            "raw_high": quote["high"],
            "raw_low": quote["low"],
            "raw_close": quote["close"],
            "adj_close": adjusted,
            "volume": quote["volume"],
        }
    )
    numeric = ["raw_open", "raw_high", "raw_low", "raw_close", "adj_close", "volume"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(subset=["date", "raw_open", "raw_high", "raw_low", "raw_close", "adj_close"])
    factor = frame["adj_close"] / frame["raw_close"]
    for target, raw in (("open", "raw_open"), ("high", "raw_high"), ("low", "raw_low"), ("close", "raw_close")):
        frame[target] = frame[raw] * factor
    frame = frame.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    dividends = []
    for event in (result.get("events", {}).get("dividends") or {}).values():
        dividends.append(
            {
                "date": datetime.fromtimestamp(int(event["date"]), timezone.utc).date().isoformat(),
                "amount": float(event["amount"]),
            }
        )
    return frame, sorted(dividends, key=lambda row: row["date"])


def _fetch_one(item: dict[str, Any]) -> dict[str, Any]:
    code = str(item["code"])
    price_path = PRICE_DIR / f"{code}.csv"
    event_path = EVENT_DIR / f"{code}.json"
    if price_path.exists() and event_path.exists():
        frame = pd.read_csv(price_path, usecols=["date"])
        if len(frame) >= MIN_PRE_MONITOR_BARS and str(frame.iloc[-1]["date"]) >= AS_OF:
            return {
                "code": code,
                "name": item["name"],
                "symbol": item["symbol"],
                "price_path": str(price_path.resolve()),
                "event_path": str(event_path.resolve()),
                "bars": len(frame),
                "first_bar": str(frame.iloc[0]["date"]),
                "last_bar": str(frame.iloc[-1]["date"]),
                "price_sha256": _digest(price_path),
                "event_sha256": _digest(event_path),
                "cache": True,
            }
    params = {
        "period1": int(datetime.fromisoformat(FETCH_FROM).replace(tzinfo=timezone.utc).timestamp()),
        "period2": int(datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp()),
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{item['symbol']}"
    with httpx.Client(timeout=45, headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    if not payload.get("chart", {}).get("result"):
        raise ValueError(f"Yahoo has no result for {item['symbol']}")
    frame, dividends = _payload_frame(payload)
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    EVENT_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_csv(price_path, index=False, encoding="utf-8-sig")
    _save(event_path, {"dividends": dividends})
    return {
        "code": code,
        "name": item["name"],
        "symbol": item["symbol"],
        "price_path": str(price_path.resolve()),
        "event_path": str(event_path.resolve()),
        "bars": len(frame),
        "first_bar": str(frame.iloc[0]["date"]),
        "last_bar": str(frame.iloc[-1]["date"]),
        "price_sha256": _digest(price_path),
        "event_sha256": _digest(event_path),
        "cache": False,
    }


def prepare(workers: int) -> dict[str, Any]:
    catalog = _read(CATALOG)
    source = _read(SOURCE_MANIFEST)
    source_items = {str(row["code"]): row for row in source["items"]}
    items = []
    errors = []
    jobs = [source_items[str(code)] for code in catalog["monitor_codes"] if str(code) in source_items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, item): item for item in jobs}
        for number, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                items.append(future.result())
            except Exception as exc:  # pragma: no cover - network evidence
                errors.append({"code": str(item["code"]), "symbol": item["symbol"], "error": str(exc)})
            if number % 25 == 0 or number == len(futures):
                print(f"prices {number}/{len(futures)} errors={len(errors)}", flush=True)
    manifest = {
        "method_version": METHOD_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": AS_OF,
        "fetch_from": FETCH_FROM,
        "catalog_path": str(CATALOG.resolve()),
        "catalog_sha256": _digest(CATALOG),
        "requested_stock_count": len(catalog["monitor_codes"]),
        "prepared_stock_count": len(items),
        "errors": sorted(errors, key=lambda row: row["code"]),
        "items": sorted(items, key=lambda row: row["code"]),
    }
    _save(RUN / "input_manifest.json", manifest)
    return manifest


def _selection_map(catalog: dict[str, Any]) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, dict[str, Any]]]:
    by_code: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    stock_meta = {str(row["code"]): row for row in catalog["monitor_stocks"]}
    for row in catalog["events"]:
        if row.get("long_eligible"):
            by_code[str(row["code"])][str(row["date"])].append(row)
    return by_code, stock_meta


def _major_pivots(frame: pd.DataFrame, radius: int = 10) -> pd.Series:
    lows = frame["low"].astype(float)
    centered = lows.shift(radius)
    confirmed = centered.where(centered.eq(lows.rolling(radius * 2 + 1).min()))
    return confirmed.ffill()


def _slope(series: pd.Series, index: int, bars: int) -> float:
    if index < bars or pd.isna(series.iloc[index]) or pd.isna(series.iloc[index - bars]):
        return 0.0
    return float(series.iloc[index] - series.iloc[index - bars])


def _classify_scenario(frame: pd.DataFrame, index: int, reasons: list[str]) -> tuple[str, int, list[str]]:
    row = frame.iloc[index]
    close = float(row.close)
    atr = max(float(row.atr14), 1e-9)
    ma21 = float(row.ma21)
    ma55 = float(row.ma55)
    ma105 = float(row.ma105)
    ma144 = float(row.ma144)
    score = 0
    evidence: list[str] = []
    above_long = close > ma105 and close > ma144
    long_rising = _slope(frame.ma105, index, 20) > 0 or _slope(frame.ma144, index, 20) > 0
    ma21_rising = _slope(frame.ma21, index, 5) > 0
    if above_long:
        score += 2
        evidence.append("ABOVE_MA105_MA144")
    if long_rising:
        score += 2
        evidence.append("LONG_MA_RISING")
    if ma21_rising:
        score += 1
        evidence.append("MA21_RISING")
    if "BREAK_CONFIRMED_SMALL_PIVOT" in reasons:
        score += 2
        evidence.append("BREAK_SMALL_DOW_DEFENSE")
    if "BREAK_20D_HIGH" in reasons:
        score += 1
        evidence.append("BREAK_20D_HIGH")
    if "BULLISH_RELAUNCH" in reasons:
        score += 1
        evidence.append("BULLISH_RELAUNCH")
    prior_macd_hist = float(frame.iloc[index - 1].macd_hist) if index > 0 else float("nan")
    if float(row.macd_hist) > 0 and math.isfinite(prior_macd_hist) and float(row.macd_hist) > prior_macd_hist:
        score += 1
        evidence.append("MACD_21_55_55_POSITIVE_EXPANDING")
    reclaimed_long = "RECLAIM_MA105" in reasons or "RECLAIM_MA144" in reasons
    reclaimed_mid = "RECLAIM_MA21" in reasons or "RECLAIM_MA55" in reasons
    if reclaimed_long:
        score += 2
        evidence.append("RECLAIM_LONG_MA")
    elif reclaimed_mid:
        score += 1
        evidence.append("RECLAIM_MID_MA")
    extension_atr = (close - ma21) / atr
    if extension_atr > 3.0:
        score -= 2
        evidence.append("EXTENDED_ABOVE_MA21")
    recent_63_low = float(frame.low.iloc[max(0, index - 62): index + 1].min())
    advance_from_low = close / recent_63_low - 1.0
    if advance_from_low > 0.65:
        score -= 1
        evidence.append("LATE_AFTER_63D_ADVANCE")
    was_bearish = False
    if index >= 20:
        prior = frame.iloc[index - 20]
        was_bearish = float(prior.close) < float(prior.ma55) and float(prior.ma55) < float(prior.ma105)
    if was_bearish and (reclaimed_long or "BREAK_CONFIRMED_SMALL_PIVOT" in reasons):
        scenario = "BEAR_REVERSAL_LEFT_RIGHT（空頭末端左右反轉）"
    elif above_long and long_rising and ("BULLISH_RELAUNCH" in reasons or reclaimed_mid):
        scenario = "MATURE_TREND_PULLBACK（長多慣性拉回再發動）"
    elif above_long and "BREAK_CONFIRMED_SMALL_PIVOT" in reasons:
        scenario = "MACRO_COPY_RESONANCE（大定錨複製共振）"
    else:
        scenario = "FRESH_Q1_EXPANSION（新生定錨直接擴張）"
    return scenario, score, evidence


def _judge_candidate(frame: pd.DataFrame, index: int, reasons: list[str]) -> tuple[dict[str, Any] | None, str]:
    row = frame.iloc[index]
    required = {"BREAK_CONFIRMED_SMALL_PIVOT", "BREAK_20D_HIGH", "BULLISH_RELAUNCH"}
    if not required.intersection(reasons):
        return None, "NO_PRICE_STRUCTURE_TRIGGER（沒有價格結構觸發）"
    stop = row.latest_pivot_low
    if pd.isna(stop):
        return None, "NO_CAUSAL_PIVOT_DEFENSE（沒有當時可用樞紐防線）"
    close = float(row.close)
    stop = float(stop)
    atr = float(row.atr14)
    if stop <= 0 or stop >= close or not math.isfinite(atr) or atr <= 0:
        return None, "INVALID_DEFENSE（防線無效）"
    risk_pct = (close - stop) / close * 100.0
    risk_atr = (close - stop) / atr
    scenario, score, evidence = _classify_scenario(frame, index, reasons)
    if risk_pct <= 12:
        score += 1
        evidence.append("CONTROLLED_STRUCTURE_RISK")
    elif risk_pct > 20:
        score -= 2
        evidence.append("WIDE_STRUCTURE_RISK")
    if risk_pct > 30 or risk_atr > 6:
        return None, "STRUCTURE_TOO_FAR（結構防線距離失去可執行性）"
    if scenario == "BEAR_REVERSAL_LEFT_RIGHT（空頭末端左右反轉）" and score < 5:
        return None, "BEAR_REVERSAL_NOT_CONFIRMED（空頭反轉證據不足）"
    if score < 5:
        return None, "V2_SCORE_NOT_ENOUGH（大小級與位階證據不足）"
    family = (
        "PULLBACK_RELAUNCH（拉回守住再發動）"
        if "BULLISH_RELAUNCH" in reasons or any(reason.startswith("RECLAIM_MA") for reason in reasons)
        else "PIVOT_BREAK（樞紐確認突破）"
    )
    return {
        "signal_date": row.date.date().isoformat(),
        "family": family,
        "scenario": scenario,
        "stop": round(stop, 6),
        "signal_close": close,
        "signal_atr": atr,
        "risk_pct_at_signal": risk_pct,
        "risk_atr_at_signal": risk_atr,
        "score": score,
        "evidence": evidence,
        "reasons": reasons,
    }, "APPROVED（AI v2批次規則通過）"


def _large_invalidated(frame: pd.DataFrame, index: int) -> tuple[bool, float | None]:
    row = frame.iloc[index]
    defense = row.latest_major_low
    if pd.isna(defense) or index < 20:
        return False, None
    close = float(row.close)
    ma55 = float(row.ma55)
    ma105 = float(row.ma105)
    bearish_long = ma55 < ma105 and _slope(frame.ma105, index, 20) < 0
    return bool(close < float(defense) and close < float(row.ma144) and bearish_long), float(defense)


def decide() -> dict[str, Any]:
    manifest = _read(RUN / "input_manifest.json")
    catalog = _read(CATALOG)
    selection_by_code, stock_meta = _selection_map(catalog)
    items = {str(row["code"]): row for row in manifest["items"]}
    approved: dict[str, list[dict[str, Any]]] = {}
    lifecycle: dict[str, list[dict[str, Any]]] = {}
    diagnostics: dict[str, Any] = {}
    for number, code in enumerate(sorted(items), 1):
        item = items[code]
        frame = pd.read_csv(item["price_path"], parse_dates=["date"])
        frame = add_indicators(frame)
        frame["latest_major_low"] = _major_pivots(frame)
        selection_days = selection_by_code.get(code, {})
        if not selection_days:
            continue
        first_selection = min(selection_days)
        positions = frame.index[frame.date.dt.strftime("%Y-%m-%d") >= first_selection]
        if len(positions) == 0:
            continue
        first_position = int(positions[0])
        pre_monitor = first_position
        if pre_monitor < MIN_PRE_MONITOR_BARS:
            diagnostics[code] = {
                "status": "INSUFFICIENT_HISTORY（歷史資料不足）",
                "pre_monitor_bars": pre_monitor,
            }
            continue
        watch_active = False
        campaign = 0
        last_approved_index = -10_000
        decisions: list[dict[str, Any]] = []
        states: list[dict[str, Any]] = []
        rejections: Counter[str] = Counter()
        campaign_strategies: set[str] = set()
        for index in range(first_position, len(frame) - 1):
            row = frame.iloc[index]
            day = row.date.date().isoformat()
            selected_today = selection_days.get(day, [])
            if selected_today:
                if not watch_active:
                    watch_active = True
                    campaign += 1
                    states.append(
                        {
                            "date": day,
                            "event": "RESELECTED（上游選股重新選入）" if campaign > 1 else "WATCHING（加入監控）",
                            "campaign": campaign,
                        }
                    )
                    campaign_strategies = set()
                campaign_strategies.update(str(event["strategy"]) for event in selected_today)
            if not watch_active:
                continue
            invalid, large_defense = _large_invalidated(frame, index)
            if invalid:
                watch_active = False
                states.append(
                    {
                        "date": day,
                        "event": "CAMPAIGN_INVALIDATED（大結構交易週期失效）",
                        "campaign": campaign,
                        "large_defense": large_defense,
                    }
                )
                states.append(
                    {
                        "date": day,
                        "event": "REMOVED_FROM_WATCHLIST（已移出監控）",
                        "campaign": campaign,
                    }
                )
                continue
            reasons = candidate_reason(frame, index)
            if not reasons:
                continue
            decision, status = _judge_candidate(frame, index, reasons)
            if decision is None:
                rejections[status] += 1
                continue
            if index - last_approved_index < 5:
                rejections["DUPLICATE_SAME_SWING（同一小波段重複訊號）"] += 1
                continue
            active_sources = sorted(campaign_strategies)
            decision.update(
                {
                    "code": code,
                    "name": stock_meta[code]["name"],
                    "campaign": campaign,
                    "monitor_on": first_selection,
                    "active_selection_strategies": active_sources,
                    "selected_today_strategies": sorted({str(event["strategy"]) for event in selected_today}),
                }
            )
            decisions.append(decision)
            last_approved_index = index
        if decisions:
            approved[code] = decisions
        if states:
            lifecycle[code] = states
        diagnostics[code] = {
            "status": "ANALYZED（已分析）",
            "pre_monitor_bars": pre_monitor,
            "candidate_approved": len(decisions),
            "rejections": dict(rejections),
        }
        if number % 100 == 0 or number == len(items):
            print(f"decisions {number}/{len(items)} approved={sum(len(rows) for rows in approved.values())}", flush=True)
    payload = {
        "method_version": METHOD_VERSION,
        "authored_on": datetime.now(timezone.utc).isoformat(),
        "decision_mode": "FROZEN_BATCH_POLICY（依六檔AI校準凍結後的可稽核批次規則；不是逐檔重新呼叫外部LLM）",
        "outcome_blindness": "RETROSPECTIVE_NOT_BLIND（事後研究、不是樣本外盲測）；每個判斷只讀取訊號日以前資料。",
        "rules": {
            "minimum_pre_monitor_bars": MIN_PRE_MONITOR_BARS,
            "entry_score_min": 5,
            "duplicate_swing_cooldown_bars": 5,
            "execution": "次日開盤；0.5ATR追價上限加一個最小跳動單位；固定約一萬元；不縮量",
            "large_invalidation": "跌破因果確認大級樞紐、位於144MA下且55MA<105MA、105MA二十日斜率向下",
        },
        "approved_events": approved,
        "lifecycle_events": lifecycle,
        "diagnostics": diagnostics,
    }
    _save(DECISIONS_PATH, payload)
    return payload


def _load_frame(item: dict[str, Any]) -> pd.DataFrame:
    frame = pd.read_csv(item["price_path"], parse_dates=["date"])
    return add_indicators(frame)


def _dividends(item: dict[str, Any]) -> dict[str, float]:
    rows = _read(Path(item["event_path"])).get("dividends", [])
    result: dict[str, float] = defaultdict(float)
    for row in rows:
        result[str(row["date"])] += float(row["amount"])
    return dict(result)


def _simulate_stock(
    item: dict[str, Any],
    signals: list[dict[str, Any]],
    lifecycle: list[dict[str, Any]],
    *,
    allow_adds: bool,
) -> dict[str, Any]:
    frame = _load_frame(item)
    frame["latest_small_pivot"] = frame.latest_pivot_low
    signal_map = {str(row["signal_date"]): row for row in signals}
    invalidations = {
        str(row["date"]): row
        for row in lifecycle
        if str(row["event"]).startswith("CAMPAIGN_INVALIDATED")
        or str(row["event"]).startswith("REMOVED_FROM_WATCHLIST")
    }
    reselected = {
        str(row["date"])
        for row in lifecycle
        if str(row["event"]).startswith("WATCHING") or str(row["event"]).startswith("RESELECTED")
    }
    distributions = _dividends(item)
    start_day = min([str(row["monitor_on"]) for row in signals], default=None)
    if start_day is None:
        return {"code": item["code"], "name": item["name"], "episodes": [], "audit": []}
    start_rows = frame.index[frame.date.dt.strftime("%Y-%m-%d") >= start_day]
    if len(start_rows) == 0:
        return {"code": item["code"], "name": item["name"], "episodes": [], "audit": []}
    watch_active = False
    pending_buy: dict[str, Any] | None = None
    pending_exit: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    episodes: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    episode_number = 0
    for _, row in frame.iloc[int(start_rows[0]):].iterrows():
        day = row.date.date().isoformat()
        if current and day in distributions:
            amount = distributions[day] * sum(int(t["shares"]) for t in current["tranches"])
            current["dividend_cash"] += amount
        if current and pending_exit:
            raw_price = float(row.raw_open)
            for tranche in current["tranches"]:
                tranche["sell_price"] = raw_price
                tranche["sell_date"] = day
                tranche["sell_proceeds"] = _sell_proceeds(int(tranche["shares"]), raw_price)
            current["exit_date"] = day
            current["exit_price_raw"] = raw_price
            current["exit_price_adjusted"] = float(row.open)
            current["exit_reason"] = pending_exit["reason"]
            current["status"] = "CLOSED（交易已結束）"
            current["realized_pnl"] = (
                sum(float(t["sell_proceeds"]) - float(t["buy_cost"]) for t in current["tranches"])
                + float(current["dividend_cash"])
            )
            current["net_pnl"] = current["realized_pnl"]
            episodes.append(current)
            audit.append({"date": day, "event": "SELL（賣出）", "episode": current["episode_id"]})
            current = None
            pending_exit = None
        if pending_buy:
            adjusted_open = float(row.open)
            raw_open = float(row.raw_open)
            cap = float(pending_buy["signal_close"]) + 0.5 * float(pending_buy["signal_atr"])
            factor = adjusted_open / raw_open
            tick_tolerance = _tw_stock_tick(cap / factor) * factor
            if adjusted_open <= float(pending_buy["stop"]):
                audit.append({"date": day, "event": "BUY_SKIPPED（取消進場）", "reason": "開盤跌破防線"})
            elif adjusted_open > cap + tick_tolerance:
                audit.append({"date": day, "event": "BUY_SKIPPED（取消進場）", "reason": "開盤超過0.5ATR追價線及一個最小跳動單位"})
            else:
                shares = math.floor(NOMINAL_PER_TRANCHE / (raw_open * (1 + BUY_COMMISSION)))
                if shares > 0:
                    tranche = {
                        "role": pending_buy["role"],
                        "signal_date": pending_buy["signal_date"],
                        "entry_date": day,
                        "entry_price_raw": raw_open,
                        "entry_price_adjusted": adjusted_open,
                        "shares": shares,
                        "buy_cost": _buy_cost(shares, raw_open),
                        "stop_adjusted": float(pending_buy["stop"]),
                        "family": pending_buy["family"],
                        "scenario": pending_buy["scenario"],
                        "score": pending_buy["score"],
                        "active_selection_strategies": pending_buy["active_selection_strategies"],
                    }
                    if current is None:
                        episode_number += 1
                        current = {
                            "code": item["code"],
                            "name": item["name"],
                            "episode_id": f"{item['code']}-E{episode_number}",
                            "campaign": pending_buy["campaign"],
                            "entry_date": day,
                            "mother_entry_adjusted": adjusted_open,
                            "initial_defense": float(pending_buy["stop"]),
                            "dynamic_defense": float(pending_buy["stop"]),
                            "small_pivot_defense": None,
                            "last_warned_small_pivot": None,
                            "small_pivot_warning_count": 0,
                            "initial_risk": adjusted_open - float(pending_buy["stop"]),
                            "profit_protect": False,
                            "profit_protect_activation_date": None,
                            "below_ma21_streak": 0,
                            "tranches": [],
                            "dividend_cash": 0.0,
                            "mfe_high_adjusted": adjusted_open,
                            "mae_low_adjusted": adjusted_open,
                            "peak_net_liquidation_return_pct": -100.0,
                            "exit_signal_date": None,
                        }
                    else:
                        current["dynamic_defense"] = max(float(current["dynamic_defense"]), float(pending_buy["stop"]))
                    current["tranches"].append(tranche)
                    if current["profit_protect"]:
                        weighted = sum(
                            float(t["entry_price_adjusted"]) * int(t["shares"])
                            for t in current["tranches"]
                        ) / sum(int(t["shares"]) for t in current["tranches"])
                        current["dynamic_defense"] = max(
                            float(current["dynamic_defense"]),
                            weighted,
                            float(pending_buy["stop"]),
                        )
                    audit.append({"date": day, "event": f"BUY_{tranche['role']}（買進）", "episode": current["episode_id"]})
            pending_buy = None
        if current:
            current["mfe_high_adjusted"] = max(float(current["mfe_high_adjusted"]), float(row.high))
            current["mae_low_adjusted"] = min(float(current["mae_low_adjusted"]), float(row.low))
            close = float(row.close)
            current["below_ma21_streak"] = int(current["below_ma21_streak"]) + 1 if close < float(row.ma21) else 0
            activation = float(current["mother_entry_adjusted"]) + 2 * float(current["initial_risk"])
            if not current["profit_protect"] and float(row.high) >= activation:
                current["profit_protect"] = True
                current["profit_protect_activation_date"] = day
                weighted = sum(float(t["entry_price_adjusted"]) * int(t["shares"]) for t in current["tranches"]) / sum(int(t["shares"]) for t in current["tranches"])
                current["dynamic_defense"] = max(float(current["dynamic_defense"]), weighted)
            reason = None
            if close < float(current["dynamic_defense"]):
                reason = "DYNAMIC_OR_POSITION_DEFENSE（動態或部位防線失守）"
            elif current["profit_protect"] and int(current["below_ma21_streak"]) >= 2:
                reason = "MA21_TWO_CLOSES（波段期連續兩日跌破21MA）"
            if reason:
                current["exit_signal_date"] = day
                pending_exit = {"reason": reason}
                audit.append({"date": day, "event": "EXIT_TRIGGERED（已觸發出場）", "episode": current["episode_id"], "reason": reason})
            else:
                small = current.get("small_pivot_defense")
                if (
                    current["profit_protect"] and small is not None and close < float(small)
                    and current.get("last_warned_small_pivot") != float(small)
                ):
                    current["small_pivot_warning_count"] += 1
                    current["last_warned_small_pivot"] = float(small)
                    audit.append({"date": day, "event": "SMALL_PIVOT_BROKEN（小級樞紐失守警告）", "episode": current["episode_id"], "defense": small})
                if current["profit_protect"] and pd.notna(row.latest_small_pivot) and float(row.latest_small_pivot) < close:
                    previous = current.get("small_pivot_defense")
                    current["small_pivot_defense"] = float(row.latest_small_pivot) if previous is None else max(float(previous), float(row.latest_small_pivot))
            live_deployed = sum(float(t["buy_cost"]) for t in current["tranches"])
            hypothetical_pnl = (
                sum(
                    _sell_proceeds(int(t["shares"]), float(row.raw_close)) - float(t["buy_cost"])
                    for t in current["tranches"]
                )
                + float(current["dividend_cash"])
            )
            current["peak_net_liquidation_return_pct"] = max(
                float(current["peak_net_liquidation_return_pct"]),
                hypothetical_pnl / live_deployed * 100 if live_deployed else 0.0,
            )
        if day in invalidations:
            watch_active = False
            pending_buy = None
            audit.append({"date": day, "event": "CAMPAIGN_INVALIDATED（大結構交易週期失效）"})
            if current and pending_exit is None:
                current["exit_signal_date"] = day
                pending_exit = {"reason": str(invalidations[day]["event"])}
        # Fixed same-day precedence: invalidate/remove the old campaign first,
        # then permit an independently evidenced upstream re-selection.
        if day in reselected:
            watch_active = True
        decision = signal_map.get(day)
        if decision and watch_active and pending_exit is None:
            intent = str(decision.get("intent") or "").upper()
            if current is None:
                if intent and "ADD" in intent:
                    audit.append({"date": day, "event": "SIGNAL_EVIDENCE_ONLY（僅保存訊號證據）", "reason": "AI僅核准加碼，但當時無母單持倉"})
                    continue
                role = "MOTHER（母單）"
            elif allow_adds and len(current["tranches"]) < 3:
                if intent and "ADD" not in intent:
                    audit.append({"date": day, "event": "SIGNAL_EVIDENCE_ONLY（僅保存訊號證據）", "reason": "AI核准母單／再進場，但當時已有持倉"})
                    continue
                hypothetical_pnl = (
                    sum(
                        _sell_proceeds(int(t["shares"]), float(row.raw_close)) - float(t["buy_cost"])
                        for t in current["tranches"]
                    )
                    + float(current["dividend_cash"])
                )
                if hypothetical_pnl <= 0:
                    audit.append({"date": day, "event": "ADD_SKIPPED（取消加碼）", "reason": "扣除成本後持倉尚未獲利"})
                    continue
                role = f"ADD_{len(current['tranches'])}（第{len(current['tranches'])}次加碼）"
            else:
                audit.append({"date": day, "event": "SIGNAL_EVIDENCE_ONLY（僅保存訊號證據）"})
                continue
            pending_buy = {**decision, "role": role}
    if current:
        final = frame.iloc[-1]
        current["status"] = "OPEN（持有中）"
        current["exit_date"] = None
        current["exit_price_raw"] = None
        current["exit_price_adjusted"] = None
        current["exit_reason"] = "AS_OF（截至回測日）"
        current["mark_date"] = final.date.date().isoformat()
        current["mark_price_raw"] = float(final.raw_close)
        current["mark_price_adjusted"] = float(final.close)
        current["net_pnl"] = (
            sum(_sell_proceeds(int(t["shares"]), float(final.raw_close)) - float(t["buy_cost"]) for t in current["tranches"])
            + float(current["dividend_cash"])
        )
        episodes.append(current)
    for episode in episodes:
        episode["tranche_count"] = len(episode["tranches"])
        episode["deployed_cash"] = sum(float(t["buy_cost"]) for t in episode["tranches"])
        episode["net_return_on_deployed_pct"] = float(episode["net_pnl"]) / float(episode["deployed_cash"]) * 100
        episode["mfe_pct_from_mother"] = (float(episode["mfe_high_adjusted"]) / float(episode["mother_entry_adjusted"]) - 1) * 100
        episode["mae_pct_from_mother"] = (float(episode["mae_low_adjusted"]) / float(episode["mother_entry_adjusted"]) - 1) * 100
        if episode["status"].startswith("CLOSED"):
            episode["holding_sessions"] = int(
                ((frame.date >= pd.Timestamp(episode["entry_date"])) & (frame.date <= pd.Timestamp(episode["exit_date"]))).sum()
            )
        else:
            episode["holding_sessions"] = int((frame.date >= pd.Timestamp(episode["entry_date"])).sum())
    return {"code": item["code"], "name": item["name"], "episodes": episodes, "audit": audit}


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(max(0.0, value) for value in values)
    losses = -sum(min(0.0, value) for value in values)
    return None if losses == 0 else gains / losses


def _distribution(values: list[float]) -> dict[str, int]:
    bins = [
        ("≤-20%", lambda value: value <= -20),
        ("-20%～-10%", lambda value: -20 < value <= -10),
        ("-10%～0%", lambda value: -10 < value < 0),
        ("0%～10%", lambda value: 0 <= value < 10),
        ("10%～20%", lambda value: 10 <= value < 20),
        ("20%～50%", lambda value: 20 <= value < 50),
        ("50%～100%", lambda value: 50 <= value < 100),
        ("≥100%", lambda value: value >= 100),
    ]
    return {label: sum(test(value) for value in values) for label, test in bins}


def _mark_to_market_drawdown(result: dict[str, Any], items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a daily liquidation-value curve without imposing a capital limit.

    The denominator is the variant's peak concurrent deployed cash.  This is a
    research risk view, not an account-level CAGR calculation.
    """
    episodes = [episode for stock in result["stocks"] for episode in stock["episodes"]]
    if not episodes:
        return {
            "base_cash": 0.0,
            "max_drawdown_twd": 0.0,
            "max_drawdown_pct": 0.0,
            "peak_date": None,
            "trough_date": None,
        }
    start = min(str(episode["entry_date"]) for episode in episodes)
    price_by_code: dict[str, pd.Series] = {}
    dividend_by_code: dict[str, dict[str, float]] = {}
    all_dates: set[str] = set()
    for stock in result["stocks"]:
        code = str(stock["code"])
        frame = pd.read_csv(items[code]["price_path"], usecols=["date", "raw_close"])
        frame["date"] = frame["date"].astype(str)
        frame = frame[(frame["date"] >= start) & (frame["date"] <= AS_OF)]
        price_by_code[code] = frame.set_index("date")["raw_close"].astype(float)
        all_dates.update(frame["date"].tolist())
        dividend_by_code[code] = _dividends(items[code])

    base_cash = float(result["summary"]["peak_concurrent_deployed_cash"])
    peak_equity = base_cash
    peak_date = start
    maximum_amount = 0.0
    maximum_pct = 0.0
    trough_date = start
    for day in sorted(all_dates):
        portfolio_pnl = 0.0
        for episode in episodes:
            entry_date = str(episode["entry_date"])
            if day < entry_date:
                continue
            exit_date = episode.get("exit_date")
            if exit_date and day >= str(exit_date):
                portfolio_pnl += float(episode["net_pnl"])
                continue
            series = price_by_code[str(episode["code"])]
            eligible = series.loc[:day]
            if eligible.empty:
                continue
            mark = float(eligible.iloc[-1])
            live_tranches = [tranche for tranche in episode["tranches"] if str(tranche["entry_date"]) <= day]
            portfolio_pnl += sum(
                _sell_proceeds(int(tranche["shares"]), mark) - float(tranche["buy_cost"])
                for tranche in live_tranches
            )
            for ex_date, amount in dividend_by_code[str(episode["code"])].items():
                if entry_date <= ex_date <= day:
                    eligible_shares = sum(
                        int(tranche["shares"])
                        for tranche in live_tranches
                        if str(tranche["entry_date"]) <= ex_date
                    )
                    portfolio_pnl += float(amount) * eligible_shares
        equity = base_cash + portfolio_pnl
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
        "method": "以尖峰投入資金作基準本金，逐日按估計可變現淨值計算；未設資金上限。",
    }


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    episodes = [episode for stock in result["stocks"] for episode in stock["episodes"]]
    audit = [event for stock in result["stocks"] for event in stock.get("audit", [])]
    pnl = [float(episode["net_pnl"]) for episode in episodes]
    returns = [float(episode["net_return_on_deployed_pct"]) for episode in episodes]
    mfe = [float(episode["mfe_pct_from_mother"]) for episode in episodes]
    deployed = sum(float(episode["deployed_cash"]) for episode in episodes)
    closed_episodes = [episode for episode in episodes if episode["status"].startswith("CLOSED")]
    open_episodes = [episode for episode in episodes if episode["status"].startswith("OPEN")]
    closed_pnl = [float(episode["net_pnl"]) for episode in closed_episodes]
    open_pnl = [float(episode["net_pnl"]) for episode in open_episodes]
    closed_returns = [float(episode["net_return_on_deployed_pct"]) for episode in closed_episodes]
    open_returns = [float(episode["net_return_on_deployed_pct"]) for episode in open_episodes]
    events: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(lambda: {"open": [], "close": []})
    for episode in episodes:
        for tranche in episode["tranches"]:
            key = f"{episode['episode_id']}|{tranche['role']}"
            cash = float(tranche["buy_cost"])
            events[str(tranche["entry_date"])]["open"].append((key, cash))
            if tranche.get("sell_date"):
                events[str(tranche["sell_date"])]["close"].append((key, cash))
    active: dict[str, float] = {}
    active_stocks: dict[str, int] = defaultdict(int)
    key_stock = {
        f"{episode['episode_id']}|{tranche['role']}": episode["code"]
        for episode in episodes for tranche in episode["tranches"]
    }
    peak_cash = 0.0
    peak_tranches = 0
    peak_stocks = 0
    peak_date = None
    for day in sorted(events):
        for key, _ in events[day]["close"]:
            if key in active:
                active_stocks[key_stock[key]] -= 1
                del active[key]
        for key, cash in events[day]["open"]:
            active[key] = cash
            active_stocks[key_stock[key]] += 1
        cash = sum(active.values())
        stocks = sum(count > 0 for count in active_stocks.values())
        peak_tranches = max(peak_tranches, len(active))
        peak_stocks = max(peak_stocks, stocks)
        if cash > peak_cash:
            peak_cash = cash
            peak_date = day
    winners = sorted(episodes, key=lambda episode: float(episode["net_pnl"]), reverse=True)
    top1 = max(pnl, default=0.0)
    top3 = sum(sorted([value for value in pnl if value > 0], reverse=True)[:3])
    net = sum(pnl)
    open_deployed = sum(float(episode["deployed_cash"]) for episode in open_episodes)
    buy_skipped = sum(str(event.get("event", "")).startswith("BUY_SKIPPED") for event in audit)
    add_skipped = sum(str(event.get("event", "")).startswith("ADD_SKIPPED") for event in audit)
    evidence_only = sum(str(event.get("event", "")).startswith("SIGNAL_EVIDENCE_ONLY") for event in audit)
    initial_risk_pct = [
        max(
            0.0,
            (float(tranche["entry_price_adjusted"]) - float(tranche["stop_adjusted"]))
            / float(tranche["entry_price_adjusted"])
            * 100,
        )
        for episode in episodes
        for tranche in episode["tranches"]
    ]
    risk_events: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"open": [], "close": []})
    for episode in episodes:
        for tranche in episode["tranches"]:
            risk_cash = float(tranche["buy_cost"]) * max(
                0.0,
                (float(tranche["entry_price_adjusted"]) - float(tranche["stop_adjusted"]))
                / float(tranche["entry_price_adjusted"]),
            )
            risk_events[str(tranche["entry_date"])]["open"].append(risk_cash)
            if episode.get("exit_date"):
                risk_events[str(episode["exit_date"])]["close"].append(risk_cash)
    active_static_risk = 0.0
    peak_static_risk = 0.0
    peak_static_risk_date = None
    for day in sorted(risk_events):
        active_static_risk -= sum(risk_events[day]["close"])
        active_static_risk += sum(risk_events[day]["open"])
        if active_static_risk > peak_static_risk:
            peak_static_risk = active_static_risk
            peak_static_risk_date = day
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        by_scenario[str(episode["tranches"][0]["scenario"])].append(episode)
    scenario_breakdown = []
    for scenario, rows in sorted(by_scenario.items(), key=lambda item: (-len(item[1]), item[0])):
        values = [float(row["net_pnl"]) for row in rows]
        cash = sum(float(row["deployed_cash"]) for row in rows)
        scenario_breakdown.append(
            {
                "scenario": scenario,
                "episodes": len(rows),
                "open": sum(str(row["status"]).startswith("OPEN") for row in rows),
                "net_pnl": round(sum(values), 2),
                "return_on_deployed_cash_pct": round(sum(values) / cash * 100, 4) if cash else 0.0,
                "average_return_pct": round(statistics.fmean(float(row["net_return_on_deployed_pct"]) for row in rows), 4),
                "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 2),
                "profit_factor": None if _profit_factor(values) is None else round(float(_profit_factor(values)), 4),
            }
        )
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        for strategy in episode["tranches"][0].get("active_selection_strategies", []):
            by_strategy[str(strategy)].append(episode)
    strategy_breakdown = []
    for strategy, rows in sorted(by_strategy.items(), key=lambda item: (-len(item[1]), item[0])):
        values = [float(row["net_pnl"]) for row in rows]
        cash = sum(float(row["deployed_cash"]) for row in rows)
        strategy_breakdown.append(
            {
                "strategy": strategy,
                "episodes": len(rows),
                "open": sum(str(row["status"]).startswith("OPEN") for row in rows),
                "net_pnl_full_overlap_attribution": round(sum(values), 2),
                "return_on_deployed_cash_pct": round(sum(values) / cash * 100, 4) if cash else 0.0,
                "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 2),
                "profit_factor": None if _profit_factor(values) is None else round(float(_profit_factor(values)), 4),
                "average_mfe_pct": round(
                    statistics.fmean(float(row["mfe_pct_from_mother"]) for row in rows), 4
                ),
            }
        )
    exit_reason_breakdown = dict(Counter(str(episode["exit_reason"]) for episode in episodes))
    return {
        "stocks_traded": len({str(episode["code"]) for episode in episodes}),
        "trade_episodes": len(episodes),
        "closed": len(closed_episodes),
        "open": len(open_episodes),
        "tranches": sum(int(episode["tranche_count"]) for episode in episodes),
        "buy_fills": sum(int(episode["tranche_count"]) for episode in episodes),
        "sell_orders": len(closed_episodes),
        "buy_skipped": buy_skipped,
        "add_skipped_not_profitable": add_skipped,
        "signal_evidence_only": evidence_only,
        "gross_profit": round(sum(max(0.0, value) for value in pnl), 2),
        "gross_loss": round(sum(min(0.0, value) for value in pnl), 2),
        "realized_net_pnl": round(sum(closed_pnl), 2),
        "unrealized_net_pnl_after_estimated_exit_cost": round(sum(open_pnl), 2),
        "net_pnl": round(net, 2),
        "total_deployed_cash": round(deployed, 2),
        "return_on_deployed_cash_pct": round(net / deployed * 100, 4) if deployed else 0.0,
        "return_on_500k_pct": round(net / 500_000 * 100, 4),
        "win_rate_pct": round(sum(value > 0 for value in pnl) / len(pnl) * 100, 2) if pnl else 0.0,
        "closed_win_rate_pct": round(sum(value > 0 for value in closed_pnl) / len(closed_pnl) * 100, 2) if closed_pnl else 0.0,
        "open_win_rate_pct": round(sum(value > 0 for value in open_pnl) / len(open_pnl) * 100, 2) if open_pnl else 0.0,
        "profit_factor": None if _profit_factor(pnl) is None else round(float(_profit_factor(pnl)), 4),
        "closed_profit_factor": None if _profit_factor(closed_pnl) is None else round(float(_profit_factor(closed_pnl)), 4),
        "average_return_pct": round(statistics.fmean(returns), 4) if returns else 0.0,
        "median_return_pct": round(statistics.median(returns), 4) if returns else 0.0,
        "closed_average_return_pct": round(statistics.fmean(closed_returns), 4) if closed_returns else 0.0,
        "open_average_return_pct": round(statistics.fmean(open_returns), 4) if open_returns else 0.0,
        "average_mfe_pct": round(statistics.fmean(mfe), 4) if mfe else 0.0,
        "median_mfe_pct": round(statistics.median(mfe), 4) if mfe else 0.0,
        "mfe_20_plus_count": sum(value >= 20 for value in mfe),
        "mfe_20_plus_final_positive_count": sum(
            float(episode["mfe_pct_from_mother"]) >= 20 and float(episode["net_pnl"]) > 0 for episode in episodes
        ),
        "closed_average_giveback_points": round(
            statistics.fmean(float(episode["peak_net_liquidation_return_pct"]) - float(episode["net_return_on_deployed_pct"]) for episode in closed_episodes), 4
        ) if closed_episodes else 0.0,
        "average_holding_sessions": round(statistics.fmean(float(episode["holding_sessions"]) for episode in episodes), 2) if episodes else 0.0,
        "average_initial_risk_pct_per_fill": round(statistics.fmean(initial_risk_pct), 4) if initial_risk_pct else 0.0,
        "maximum_initial_risk_pct_per_fill": round(max(initial_risk_pct), 4) if initial_risk_pct else 0.0,
        "peak_static_initial_stop_risk_twd": round(peak_static_risk, 2),
        "peak_static_initial_stop_risk_date": peak_static_risk_date,
        "return_distribution": _distribution(returns),
        "closed_return_distribution": _distribution(closed_returns),
        "open_return_distribution": _distribution(open_returns),
        "mfe_distribution": _distribution(mfe),
        "scenario_breakdown": scenario_breakdown,
        "strategy_breakdown_overlap": strategy_breakdown,
        "exit_reason_breakdown": exit_reason_breakdown,
        "current_open_tranches": sum(int(episode["tranche_count"]) for episode in open_episodes),
        "current_open_deployed_cash": round(open_deployed, 2),
        "current_estimated_liquidation_value": round(open_deployed + sum(open_pnl), 2),
        "maximum_concurrent_stocks": peak_stocks,
        "maximum_concurrent_tranches": peak_tranches,
        "peak_concurrent_deployed_cash": round(peak_cash, 2),
        "return_on_peak_capital_pct": round(net / peak_cash * 100, 4) if peak_cash else 0.0,
        "peak_date": peak_date,
        "top1_profit_concentration_pct": round(top1 / net * 100, 2) if net > 0 else None,
        "top3_profit_concentration_pct": round(top3 / net * 100, 2) if net > 0 else None,
        "net_without_top1": round(net - top1, 2),
        "top_winners": [
            {"code": row["code"], "name": row["name"], "episode_id": row["episode_id"], "net_pnl": round(float(row["net_pnl"]), 2), "return_pct": round(float(row["net_return_on_deployed_pct"]), 2)}
            for row in winners[:10]
        ],
        "top_losers": [
            {"code": row["code"], "name": row["name"], "episode_id": row["episode_id"], "net_pnl": round(float(row["net_pnl"]), 2), "return_pct": round(float(row["net_return_on_deployed_pct"]), 2)}
            for row in list(reversed(winners[-10:]))
        ],
    }


def replay() -> dict[str, Any]:
    manifest = _read(RUN / "input_manifest.json")
    decisions = _read(DECISIONS_PATH)
    items = {str(row["code"]): row for row in manifest["items"]}
    output: dict[str, Any] = {
        "method_version": METHOD_VERSION,
        "rule_reference": str(RULE_DOC_PATH.resolve()),
        "rule_sha256": _digest(RULE_DOC_PATH),
        "rule_config": str(RULE_CONFIG_PATH.resolve()),
        "rule_config_sha256": _digest(RULE_CONFIG_PATH),
        "judgement_scope": "V2_ALIGNED_BATCH_PROXY（V2概念校準後的批次代理；不是逐檔正式AI V2判讀）",
        "as_of": AS_OF,
        "catalog_sha256": _digest(CATALOG),
        "input_manifest_sha256": _digest(RUN / "input_manifest.json"),
        "decisions_sha256": _digest(DECISIONS_PATH),
        "costs": {"buy_commission": BUY_COMMISSION, "sell_commission": SELL_COMMISSION, "sell_tax": SELL_TAX},
        "variants": {},
    }
    for key, allow_adds in (("MOTHER_ONLY_10K（母單固定一萬元）", False), ("MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）", True)):
        result = {"name": key, "stocks": []}
        for number, code in enumerate(sorted(decisions["approved_events"]), 1):
            if code not in items:
                continue
            result["stocks"].append(
                _simulate_stock(
                    items[code],
                    decisions["approved_events"][code],
                    decisions.get("lifecycle_events", {}).get(code, []),
                    allow_adds=allow_adds,
                )
            )
            if number % 50 == 0:
                print(f"{key}: {number}/{len(decisions['approved_events'])}", flush=True)
        result["summary"] = _summary(result)
        result["summary"]["mark_to_market_drawdown"] = _mark_to_market_drawdown(result, items)
        output["variants"][key] = result
    _save(RESULT_PATH, output)
    return output


def _money(value: float) -> str:
    return f"{value:+,.0f}"


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def _pf(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def report() -> Path:
    catalog = _read(CATALOG)
    manifest = _read(RUN / "input_manifest.json")
    decisions = _read(DECISIONS_PATH)
    result = _read(RESULT_PATH)
    analyzed = [row for row in decisions["diagnostics"].values() if str(row["status"]).startswith("ANALYZED")]
    insufficient_codes = [code for code, row in decisions["diagnostics"].items() if str(row["status"]).startswith("INSUFFICIENT_HISTORY")]
    lines = [
        "# 2026/05/01起TG全策略選股 × 啟蒙判讀規格V2批次代理回測",
        "",
        "> `V2_ALIGNED_BATCH_PROXY（V2概念校準後的批次代理）`：這不是747檔逐檔正式AI V2判讀。正式V2要求AI逐檔提交定錨、太極、象限、道氏與每項必要條件證據；本次為了全量可重播，使用六檔校準後凍結的五分批次政策。因此結果只能回答「批次代理若全量執行會怎樣」，不能冒充正式V2績效。",
        "",
        "> `RETROSPECTIVE_NOT_BLIND（事後研究、不是樣本外盲測）`：所有特徵只讀取訊號日以前資料，但政策形成於已知歷史的研究環境。",
        "",
        "## 範圍與口徑",
        "",
        f"- 選股期間：{catalog['window']['start']}～{catalog['window']['end']}。",
        f"- TG原始訊息：{catalog['message_count']:,}則；去重選股命中：{catalog['event_count']:,}筆；監控母體：{catalog['monitor_stock_count']:,}檔。",
        f"- 長歷史行情成功：{manifest['prepared_stock_count']:,}/{manifest['requested_stock_count']:,}檔；最低要求為監控日前{MIN_PRE_MONITOR_BARS}根日K。",
        f"- 實際完成V2判讀：{len(analyzed):,}檔；歷史不足未判讀：{len(insufficient_codes):,}檔" + (f"（{', '.join(insufficient_codes)}）" if insufficient_codes else "。"),
        f"- V2概念批次代理核准事件：{sum(len(rows) for rows in decisions['approved_events'].values()):,}筆，涉及{len(decisions['approved_events']):,}檔。",
        "- 進場：訊號日收盤成立，次日開盤；超過收盤＋0.5ATR及一個最小跳動單位則取消。每份固定約一萬元，不縮量。",
        "- 出場：未達+2R以前使用部位防線；達+2R後成本防線只升不降，小級樞紐跌破只警告，連續兩日收盤跌破21MA才觸發波段出場。",
        "- 監控：大級結構失效即移出；只有後續上游選股事件可重新加入。",
        "- 帳務：原始可成交價格、現金股利、買賣手續費各0.1425%、賣出交易稅0.3%。",
        "",
        "## 整體結果",
        "",
        "| 版本 | 交易股票 | 回合 | 已出場／持有 | 買進份數 | 已實現 | 未實現* | 淨損益 | 投入合計報酬 | 尖峰資金報酬 | 50萬報酬** | 勝率 | PF | 最大持股／份數 | 尖峰資金 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in result["variants"].values():
        summary = variant["summary"]
        lines.append(
            f"| {variant['name']} | {summary['stocks_traded']} | {summary['trade_episodes']} | {summary['closed']}／{summary['open']} | {summary['buy_fills']} | "
            f"{_money(summary['realized_net_pnl'])} | {_money(summary['unrealized_net_pnl_after_estimated_exit_cost'])} | {_money(summary['net_pnl'])} | "
            f"{_pct(summary['return_on_deployed_cash_pct'])} | {_pct(summary['return_on_peak_capital_pct'])} | {_pct(summary['return_on_500k_pct'])} | {summary['win_rate_pct']:.2f}% | {_pf(summary['profit_factor'])} | "
            f"{summary['maximum_concurrent_stocks']}／{summary['maximum_concurrent_tranches']} | {summary['peak_concurrent_deployed_cash']:,.0f} |"
        )
    lines += [
        "",
        "\\* 未實現損益以截至日收盤假設賣出並扣除估計賣出成本。",
        "\\** 50萬報酬只是淨損益除以50萬元的參考值；本回測未設資金上限，尖峰需求遠高於50萬元，不能視為50萬元帳戶可實現績效。",
        "",
    ]
    for variant in result["variants"].values():
        summary = variant["summary"]
        lines += [
            f"## {variant['name']}統計",
            "",
            f"- 平均／中位報酬：{_pct(summary['average_return_pct'])}／{_pct(summary['median_return_pct'])}。",
            f"- 已出場平均報酬／勝率／PF：{_pct(summary['closed_average_return_pct'])}／{summary['closed_win_rate_pct']:.2f}%／{_pf(summary['closed_profit_factor'])}。",
            f"- 持有中平均報酬／勝率：{_pct(summary['open_average_return_pct'])}／{summary['open_win_rate_pct']:.2f}%。",
            f"- 平均／中位MFE：{_pct(summary['average_mfe_pct'])}／{_pct(summary['median_mfe_pct'])}。",
            f"- MFE≥20%共有{summary['mfe_20_plus_count']}筆，其中截至出場／截至日仍為正報酬{summary['mfe_20_plus_final_positive_count']}筆；已出場交易平均回吐{summary['closed_average_giveback_points']:.2f}個百分點。",
            f"- 平均持有：{summary['average_holding_sessions']:.2f}個交易日。",
            f"- 尖峰日期：{summary['peak_date']}；同時{summary['maximum_concurrent_stocks']}檔、{summary['maximum_concurrent_tranches']}份、投入{summary['peak_concurrent_deployed_cash']:,.0f}元。",
            f"- 截至日仍持有：{summary['open']}檔次、{summary['current_open_tranches']}份；原始投入{summary['current_open_deployed_cash']:,.0f}元，估計立即清算價值{summary['current_estimated_liquidation_value']:,.0f}元。",
            f"- 全期累計投入{summary['total_deployed_cash']:,.0f}元；買進成交{summary['buy_fills']}份、全回合賣出{summary['sell_orders']}次、跳空取消買進{summary['buy_skipped']}次。",
            f"- 獲利交易合計{_money(summary['gross_profit'])}；虧損交易合計{_money(summary['gross_loss'])}。",
            f"- 每份初始防線距離平均／最大：{summary['average_initial_risk_pct_per_fill']:.2f}%／{summary['maximum_initial_risk_pct_per_fill']:.2f}%；尖峰靜態初始停損風險{summary['peak_static_initial_stop_risk_twd']:,.0f}元（{summary['peak_static_initial_stop_risk_date']}）。",
            f"- 逐日估計最大回撤：-{summary['mark_to_market_drawdown']['max_drawdown_twd']:,.0f}元／-{summary['mark_to_market_drawdown']['max_drawdown_pct']:.2f}%（{summary['mark_to_market_drawdown']['peak_date']}→{summary['mark_to_market_drawdown']['trough_date']}；以尖峰投入作基準本金）。",
            "- 最大單筆／前三大獲利集中度："
            + (
                f"{summary['top1_profit_concentration_pct']}%／{summary['top3_profit_concentration_pct']}%"
                if summary["top1_profit_concentration_pct"] is not None
                else "不適用（整體淨損益為負）"
            )
            + f"；扣除最大單筆後淨損益{_money(summary['net_without_top1'])}。",
            "",
            "### V2情境分解",
            "",
            "| 情境 | 回合／持有中 | 淨損益 | 投入合計報酬 | 平均回合報酬 | 勝率 | PF |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in summary["scenario_breakdown"]:
            lines.append(
                f"| {row['scenario']} | {row['episodes']}／{row['open']} | {_money(row['net_pnl'])} | "
                f"{_pct(row['return_on_deployed_cash_pct'])} | {_pct(row['average_return_pct'])} | {row['win_rate_pct']:.2f}% | {_pf(row['profit_factor'])} |"
            )
        lines += [
            "",
            "### 上游選股來源交叉歸因",
            "",
            "> 同一股票campaign常同時出現在多個策略，以下每個來源都歸入該回合完整損益，故欄位不能相加；只適合比較條件關聯，不是獨立策略組合績效。",
            "",
            "| 選股來源 | 回合／持有中 | 交叉歸因淨損益 | 投入合計報酬 | 勝率 | PF | 平均MFE |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in summary["strategy_breakdown_overlap"]:
            lines.append(
                f"| {row['strategy']} | {row['episodes']}／{row['open']} | {_money(row['net_pnl_full_overlap_attribution'])} | "
                f"{_pct(row['return_on_deployed_cash_pct'])} | {row['win_rate_pct']:.2f}% | {_pf(row['profit_factor'])} | {_pct(row['average_mfe_pct'])} |"
            )
        lines += [
            "",
            "### 出場原因",
            "",
            "| 原因 | 回合 |",
            "|---|---:|",
        ]
        for reason, count in sorted(summary["exit_reason_breakdown"].items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"| {reason} | {count} |")
        lines += [
            "",
            "### 最終報酬分佈",
            "",
            "| 區間 | 筆數 |",
            "|---|---:|",
        ]
        for label, count in summary["return_distribution"].items():
            lines.append(f"| {label} | {count} |")
        lines += ["", "### 已出場／持有中報酬分佈", "", "| 區間 | 已出場 | 持有中 |", "|---|---:|---:|"]
        for label in summary["return_distribution"]:
            lines.append(f"| {label} | {summary['closed_return_distribution'][label]} | {summary['open_return_distribution'][label]} |")
        lines += ["", "### MFE分佈", "", "| 區間 | 筆數 |", "|---|---:|"]
        for label, count in summary["mfe_distribution"].items():
            lines.append(f"| {label} | {count} |")
        lines += ["", "### 前十大獲利", "", "| 股票／回合 | 淨損益 | 報酬 |", "|---|---:|---:|"]
        for row in summary["top_winners"]:
            lines.append(f"| {row['code']} {row['name']}／{row['episode_id']} | {_money(row['net_pnl'])} | {_pct(row['return_pct'])} |")
        lines += ["", "### 前十大虧損", "", "| 股票／回合 | 淨損益 | 報酬 |", "|---|---:|---:|"]
        for row in summary["top_losers"]:
            lines.append(f"| {row['code']} {row['name']}／{row['episode_id']} | {_money(row['net_pnl'])} | {_pct(row['return_pct'])} |")
        lines += ["", "### 完整逐筆交易", "", "| 股票／回合 | 情境 | 訊號→進場 | 份數 | 防線 | 出場／截至 | MFE | MAE | 淨損益 | 報酬 | 狀態 |", "|---|---|---|---:|---:|---|---:|---:|---:|---:|---|"]
        for stock in variant["stocks"]:
            for episode in stock["episodes"]:
                mother = episode["tranches"][0]
                if episode["status"].startswith("OPEN"):
                    exit_text = f"截至{episode['mark_date']}／{episode['mark_price_raw']:.2f}"
                else:
                    exit_text = f"{episode['exit_signal_date']}→{episode['exit_date']}／{episode['exit_price_raw']:.2f}；{episode['exit_reason']}"
                lines.append(
                    f"| {episode['code']} {episode['name']}／{episode['episode_id']} | {mother['scenario']} | {mother['signal_date']}→{mother['entry_date']}／{mother['entry_price_raw']:.2f} | "
                    f"{episode['tranche_count']} | {episode['initial_defense']:.2f} | {exit_text} | {_pct(episode['mfe_pct_from_mother'])} | {_pct(episode['mae_pct_from_mother'])} | "
                    f"{_money(episode['net_pnl'])} | {_pct(episode['net_return_on_deployed_pct'])} | {episode['status']} |"
                )
        lines.append("")
    lines += [
        "## 判讀限制",
        "",
        "- 這次用的是六檔AI校準後凍結的批次代理，不是逐檔提交正式V2的AI結構地圖；目的是先讓747檔以同一口徑重現與稽核。",
        "- 正式V2禁止把五分門檻、0.5ATR追價線或特定出場式視為判讀規則；本報表將它們保留為批次／成交／出場參數，且不得拿本結果宣稱正式V2已通過全量驗證。",
        "- 規則仍源自先前已看過結果的研究過程，因此不是樣本外證明。",
        "- 一檔股票可由多個選股策略重複選到；完整交易表只計一次部位，來源策略不能直接相加。",
        "- 固定一萬元不等於固定金額風險；報表保留每筆結構防線與MAE供後續壓力測試。",
        "",
        f"方法版本：`{METHOD_VERSION}`",
    ]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return REPORT_PATH


def validate() -> dict[str, Any]:
    manifest = _read(RUN / "input_manifest.json")
    decisions = _read(DECISIONS_PATH)
    result = _read(RESULT_PATH)
    checks = []
    def add(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
    add("catalog_universe", manifest["requested_stock_count"] == 747, manifest["requested_stock_count"])
    add("official_v2_references_readable", RULE_DOC_PATH.exists() and RULE_CONFIG_PATH.exists(), [str(RULE_DOC_PATH), str(RULE_CONFIG_PATH)])
    add("all_price_files_hash", all(_digest(Path(row["price_path"])) == row["price_sha256"] for row in manifest["items"]), len(manifest["items"]))
    analyzed = [row for row in decisions["diagnostics"].values() if row["status"].startswith("ANALYZED")]
    add("history_standard", all(int(row["pre_monitor_bars"]) >= MIN_PRE_MONITOR_BARS for row in analyzed), len(analyzed))
    for key, variant in result["variants"].items():
        episodes = [episode for stock in variant["stocks"] for episode in stock["episodes"]]
        add(f"{key}:pnl_reconcile", round(sum(float(row["net_pnl"]) for row in episodes), 2) == variant["summary"]["net_pnl"], len(episodes))
        add(f"{key}:position_limit", all(1 <= len(row["tranches"]) <= (3 if "PLUS_2" in key else 1) for row in episodes), len(episodes))
        summary = variant["summary"]
        add(
            f"{key}:distribution_complete",
            sum(summary["return_distribution"].values()) == len(episodes)
            and sum(summary["mfe_distribution"].values()) == len(episodes),
            len(episodes),
        )
        add(
            f"{key}:drawdown_nonnegative",
            float(summary["mark_to_market_drawdown"]["max_drawdown_twd"]) >= 0
            and float(summary["mark_to_market_drawdown"]["max_drawdown_pct"]) >= 0,
            summary["mark_to_market_drawdown"],
        )
    payload = {"method_version": METHOD_VERSION, "passed": all(row["passed"] for row in checks), "checks": checks}
    _save(RUN / "validation.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "decide", "replay", "report", "validate", "all"])
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if args.command in {"prepare", "all"}:
        prepare(args.workers)
    if args.command in {"decide", "all"}:
        decide()
    if args.command in {"replay", "all"}:
        replay()
    if args.command in {"report", "all"}:
        print(report())
    if args.command in {"validate", "all"}:
        print(json.dumps(validate(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
