"""Prepare and account for the six-stock enlightenment AI replay.

This module never decides whether a setup is tradable.  It freezes public
daily bars, prepares causal evidence packets, and accounts for decisions that
were authored separately by the reviewing AI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OLD_RUN = ROOT / "reports/course_backtest/2026-09-05/enlightenment_ai_small_test_v1"
RUN = ROOT / "reports/course_backtest/2026-09-05/enlightenment_ai_small_test_v2"
AS_OF = "2026-09-04"
FETCH_FROM = "2019-01-01"
COHORT = [
    {"code": "6125", "name": "廣運", "symbol": "6125.TWO", "monitor_month": "2025-10"},
    {"code": "6182", "name": "合晶", "symbol": "6182.TWO", "monitor_month": "2025-10"},
    {"code": "6189", "name": "豐藝", "symbol": "6189.TW", "monitor_month": "2023-01"},
    {"code": "8358", "name": "金居", "symbol": "8358.TWO", "monitor_month": "2025-09"},
    {"code": "2630", "name": "亞航", "symbol": "2630.TW", "monitor_month": "2025-11"},
    {"code": "8234", "name": "新漢", "symbol": "8234.TWO", "monitor_month": "2025-04"},
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def yahoo_payload(item: dict[str, str]) -> dict[str, Any]:
    import httpx

    path = RUN / "sources/yahoo" / f"{item['code']}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["response"]
    params = {
        "period1": int(datetime.fromisoformat(FETCH_FROM).replace(tzinfo=timezone.utc).timestamp()),
        "period2": int(datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp()),
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    with httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{item['symbol']}",
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
    if not payload.get("chart", {}).get("result"):
        raise ValueError(f"Yahoo has no chart result for {item['symbol']}")
    save_json(
        path,
        {
            "retrieved_utc": datetime.now(timezone.utc).isoformat(),
            "url": str(response.url),
            "response": payload,
        },
    )
    return payload


def payload_to_frame(payload: dict[str, Any]) -> pd.DataFrame:
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
    frame = frame.dropna(subset=["date", "raw_open", "raw_high", "raw_low", "raw_close"])
    factor = frame["adj_close"] / frame["raw_close"]
    for target, raw in (("open", "raw_open"), ("high", "raw_high"), ("low", "raw_low"), ("close", "raw_close")):
        frame[target] = frame[raw] * factor
    return frame.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for window in (5, 13, 21, 55, 105, 144):
        frame[f"ma{window}"] = frame["close"].rolling(window).mean()
    ema21 = frame["close"].ewm(span=21, adjust=False).mean()
    ema55 = frame["close"].ewm(span=55, adjust=False).mean()
    frame["macd"] = ema21 - ema55
    frame["macd_signal"] = frame["macd"].ewm(span=55, adjust=False).mean()
    frame["macd_hist"] = frame["macd"] - frame["macd_signal"]
    previous = frame["close"].shift(1)
    true_range = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - previous).abs(), (frame["low"] - previous).abs()],
        axis=1,
    ).max(axis=1)
    frame["atr14"] = true_range.rolling(14).mean()
    frame["volume20"] = frame["volume"].rolling(20).mean()
    frame["prior_high5"] = frame["high"].shift(1).rolling(5).max()
    frame["prior_high10"] = frame["high"].shift(1).rolling(10).max()
    frame["prior_high20"] = frame["high"].shift(1).rolling(20).max()
    frame["prior_low10"] = frame["low"].shift(1).rolling(10).min()
    frame["prior_low20"] = frame["low"].shift(1).rolling(20).min()
    # A pivot is usable only three sessions later.  This keeps every packet causal.
    center_low = frame["low"].shift(3)
    center_high = frame["high"].shift(3)
    frame["confirmed_pivot_low"] = center_low.where(
        center_low.eq(frame["low"].rolling(7).min())
    )
    frame["confirmed_pivot_high"] = center_high.where(
        center_high.eq(frame["high"].rolling(7).max())
    )
    frame["latest_pivot_low"] = frame["confirmed_pivot_low"].ffill()
    frame["latest_pivot_high"] = frame["confirmed_pivot_high"].ffill()
    return frame


def first_session(frame: pd.DataFrame, month: str) -> str:
    rows = frame[frame["date"].astype(str) >= f"{month}-01"]
    if rows.empty:
        raise ValueError(f"no session on or after {month}")
    return str(rows.iloc[0]["date"])


def candidate_reason(frame: pd.DataFrame, index: int) -> list[str]:
    if index == 0:
        return []
    row, previous = frame.iloc[index], frame.iloc[index - 1]
    reasons: list[str] = []
    if row["close"] > row["prior_high10"]:
        reasons.append("BREAK_10D_HIGH")
    if row["close"] > row["prior_high20"]:
        reasons.append("BREAK_20D_HIGH")
    for window in (5, 13, 21, 55, 105, 144):
        ma = f"ma{window}"
        if pd.notna(row[ma]) and previous["close"] <= previous[ma] and row["close"] > row[ma]:
            reasons.append(f"RECLAIM_MA{window}")
    if previous["macd_hist"] <= 0 < row["macd_hist"]:
        reasons.append("MACD_21_55_55_HIST_TURN_POSITIVE")
    if pd.notna(row["latest_pivot_high"]) and previous["close"] <= previous["latest_pivot_high"] < row["close"]:
        reasons.append("BREAK_CONFIRMED_SMALL_PIVOT")
    bullish_body = row["close"] > row["open"] and (row["close"] - row["open"]) >= 0.2 * max(row["high"] - row["low"], 1e-9)
    if bullish_body and row["close"] > row["prior_high5"]:
        reasons.append("BULLISH_RELAUNCH")
    return reasons


def evidence_row(frame: pd.DataFrame, index: int, reasons: list[str]) -> dict[str, Any]:
    row = frame.iloc[index]
    previous = frame.iloc[max(0, index - 1)]
    out: dict[str, Any] = {
        "date": str(row["date"]),
        "reasons": reasons,
        "ohlc": [round(float(row[c]), 4) for c in ("open", "high", "low", "close")],
        "raw_ohlc": [round(float(row[c]), 4) for c in ("raw_open", "raw_high", "raw_low", "raw_close")],
        "ma": {str(w): round(float(row[f"ma{w}"]), 4) if pd.notna(row[f"ma{w}"]) else None for w in (5, 13, 21, 55, 105, 144)},
        "ma_slope_21": round(float(row["ma21"] - frame.iloc[max(0, index - 5)]["ma21"]), 4) if index >= 5 and pd.notna(row["ma21"]) else None,
        "macd_hist": round(float(row["macd_hist"]), 4),
        "macd_hist_change": round(float(row["macd_hist"] - previous["macd_hist"]), 4),
        "volume_ratio_20": round(float(row["volume"] / row["volume20"]), 3) if row["volume20"] else None,
        "prior_high_20": round(float(row["prior_high20"]), 4) if pd.notna(row["prior_high20"]) else None,
        "prior_low_20": round(float(row["prior_low20"]), 4) if pd.notna(row["prior_low20"]) else None,
        "latest_confirmed_pivot_low": round(float(row["latest_pivot_low"]), 4) if pd.notna(row["latest_pivot_low"]) else None,
        "latest_confirmed_pivot_high": round(float(row["latest_pivot_high"]), 4) if pd.notna(row["latest_pivot_high"]) else None,
        "atr14": round(float(row["atr14"]), 4) if pd.notna(row["atr14"]) else None,
    }
    return out


def prepare() -> None:
    manifest: dict[str, Any] = {
        "version": "enlightenment-ai-small-test-v1",
        "as_of": AS_OF,
        "fetch_from": FETCH_FROM,
        "data_source": "Yahoo Finance chart API, includeAdjustedClose=true",
        "technical_price_basis": "back-adjusted OHLC using adj_close/raw_close",
        "monitor_date_rule": "first valid trading session on or after supplied YYYY-MM-01",
        "ai_decision_rule": "candidate events are program-prepared; trade decisions must be authored outside this script",
        "items": [],
    }
    for item in COHORT:
        frame = add_indicators(payload_to_frame(yahoo_payload(item)))
        monitor_on = first_session(frame, item["monitor_month"])
        price_path = RUN / "sources/prices" / f"{item['code']}.csv"
        price_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(price_path, index=False, encoding="utf-8-sig")
        monitor_index = frame.index[frame["date"].astype(str) == monitor_on][0]
        events = []
        for index in range(monitor_index, len(frame)):
            reasons = candidate_reason(frame, index)
            if reasons:
                events.append(evidence_row(frame, index, reasons))
        packet = {
            **item,
            "monitor_on": monitor_on,
            "first_bar": str(frame.iloc[0]["date"]),
            "last_bar": str(frame.iloc[-1]["date"]),
            "pre_monitor_bars": int(monitor_index),
            "visible_bar_count": int(len(frame)),
            "candidate_events": events,
        }
        packet_path = RUN / "packets" / f"{item['code']}.json"
        save_json(packet_path, packet)
        manifest["items"].append(
            {
                **item,
                "monitor_on": monitor_on,
                "first_bar": packet["first_bar"],
                "last_bar": packet["last_bar"],
                "pre_monitor_bars": int(monitor_index),
                "candidate_event_count": len(events),
                "price_path": str(price_path.resolve()),
                "price_sha256": digest(price_path),
                "packet_path": str(packet_path.resolve()),
                "packet_sha256": digest(packet_path),
            }
        )
    save_json(RUN / "input_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def charts() -> None:
    from PIL import Image, ImageDraw, ImageFont

    manifest = json.loads((RUN / "input_manifest.json").read_text(encoding="utf-8"))
    for item in manifest["items"]:
        frame = pd.read_csv(item["price_path"], parse_dates=["date"])
        monitor = pd.Timestamp(item["monitor_on"])
        start = max(frame["date"].min(), monitor - pd.DateOffset(years=3))
        visible = frame[(frame["date"] >= start) & (frame["date"] <= pd.Timestamp(AS_OF))]
        width, height = 2100, 1150
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        left, right = 75, width - 35
        price_top, price_bottom = 65, 720
        volume_top, volume_bottom = 760, 880
        macd_top, macd_bottom = 920, 1090
        draw.text((left, 20), f"{item['code']} | monitor {item['monitor_on']} | adjusted OHLC through {AS_OF}", fill="black", font=font)

        def xpos(index: int) -> int:
            return int(left + index * (right - left) / max(len(visible) - 1, 1))

        price_columns = ["close", "ma21", "ma55", "ma105", "ma144"]
        values = pd.concat([visible[c] for c in price_columns]).dropna()
        low, high = float(values.min()), float(values.max())

        def py(value: float) -> int:
            return int(price_bottom - (value - low) * (price_bottom - price_top) / max(high - low, 1e-9))

        for y in range(price_top, price_bottom + 1, (price_bottom - price_top) // 5):
            draw.line((left, y, right, y), fill="#e2e8f0", width=1)
        line_specs = (("close", "#111827", 3), ("ma21", "#2563eb", 2), ("ma55", "#f59e0b", 2), ("ma105", "#16a34a", 2), ("ma144", "#dc2626", 2))
        for column, color, line_width in line_specs:
            points = [(xpos(n), py(float(value))) for n, value in enumerate(visible[column]) if pd.notna(value)]
            if len(points) > 1:
                draw.line(points, fill=color, width=line_width)
        legend = "close black | MA21 blue | MA55 orange | MA105 green | MA144 red | monitor purple"
        draw.text((left, price_top + 5), legend, fill="#334155", font=font)

        maximum_volume = max(float(visible["volume"].max()), 1.0)
        for n, value in enumerate(visible["volume"]):
            x = xpos(n)
            y = int(volume_bottom - float(value) / maximum_volume * (volume_bottom - volume_top))
            draw.line((x, volume_bottom, x, y), fill="#64748b", width=1)
        draw.text((left, volume_top), "volume", fill="#334155", font=font)

        hist = visible["macd_hist"].fillna(0.0)
        maximum_hist = max(float(hist.abs().max()), 1e-9)
        zero = (macd_top + macd_bottom) // 2
        draw.line((left, zero, right, zero), fill="black", width=1)
        for n, value in enumerate(hist):
            x = xpos(n)
            y = int(zero - float(value) / maximum_hist * (macd_bottom - macd_top) / 2)
            draw.line((x, zero, x, y), fill="#dc2626" if value >= 0 else "#16a34a", width=1)
        draw.text((left, macd_top), "MACD 21/55/55 histogram", fill="#334155", font=font)
        monitor_positions = visible.index[visible["date"] >= monitor]
        if len(monitor_positions):
            local_index = visible.index.get_loc(monitor_positions[0])
            mx = xpos(int(local_index))
            for top, bottom in ((price_top, price_bottom), (volume_top, volume_bottom), (macd_top, macd_bottom)):
                draw.line((mx, top, mx, bottom), fill="#7c3aed", width=3)
        draw.rectangle((left, price_top, right, price_bottom), outline="#94a3b8", width=1)
        draw.rectangle((left, volume_top, right, volume_bottom), outline="#94a3b8", width=1)
        draw.rectangle((left, macd_top, right, macd_bottom), outline="#94a3b8", width=1)
        path = RUN / "charts" / f"{item['code']}_overview.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        print(path)


def summaries() -> None:
    manifest = json.loads((RUN / "input_manifest.json").read_text(encoding="utf-8"))
    for item in manifest["items"]:
        frame = pd.read_csv(item["price_path"], parse_dates=["date"])
        monitor = pd.Timestamp(item["monitor_on"])
        visible = frame[(frame["date"] >= monitor - pd.DateOffset(months=12)) & (frame["date"] <= pd.Timestamp(AS_OF))].copy()
        visible["month"] = visible["date"].dt.to_period("M")
        rows = []
        for month, group in visible.groupby("month"):
            last = group.iloc[-1]
            rows.append(
                {
                    "month": str(month),
                    "open": round(float(group.iloc[0]["open"]), 2),
                    "high": round(float(group["high"].max()), 2),
                    "low": round(float(group["low"].min()), 2),
                    "close": round(float(last["close"]), 2),
                    "ma21": round(float(last["ma21"]), 2),
                    "ma55": round(float(last["ma55"]), 2),
                    "ma105": round(float(last["ma105"]), 2),
                    "ma144": round(float(last["ma144"]), 2),
                    "hist": round(float(last["macd_hist"]), 2),
                    "volume_m": round(float(group["volume"].sum() / 1_000_000), 1),
                }
            )
        print(f"\n=== {item['code']} {item['name']} monitor={item['monitor_on']} ===")
        print(pd.DataFrame(rows).to_string(index=False))


def events(code: str) -> None:
    packet = json.loads((RUN / "packets" / f"{code}.json").read_text(encoding="utf-8"))
    rows = []
    for event in packet["candidate_events"]:
        reasons = event["reasons"]
        if not ({"BREAK_20D_HIGH", "MACD_21_55_55_HIST_TURN_POSITIVE", "BREAK_CONFIRMED_SMALL_PIVOT", "RECLAIM_MA105", "RECLAIM_MA144"} & set(reasons)):
            continue
        rows.append(
            {
                "date": event["date"],
                "close": event["ohlc"][3],
                "ma21": event["ma"]["21"],
                "ma55": event["ma"]["55"],
                "ma105": event["ma"]["105"],
                "ma144": event["ma"]["144"],
                "pivot_low": event["latest_confirmed_pivot_low"],
                "pivot_high": event["latest_confirmed_pivot_high"],
                "vol20": event["volume_ratio_20"],
                "reasons": ",".join(reasons),
            }
        )
    print(pd.DataFrame(rows).to_string(index=False))


def probe(code: str, signal_date: str, stop: float) -> None:
    """Inspect objective execution/exit consequences for one authored plan."""
    from scripts.course_daily_screen_trial import _add_indicators
    from scripts.course_exit_variant_backtest import (
        _confirmed_pivot_defenses,
        simulate_hybrid_variant,
        simulate_simple_variant,
    )

    item = next(item for item in COHORT if item["code"] == code)
    raw = pd.read_csv(RUN / "sources/prices" / f"{code}.csv", parse_dates=["date"])
    frame = _add_indicators(raw[["date", "open", "high", "low", "close", "volume"]].copy())
    positions = frame.index[frame["date"].dt.strftime("%Y-%m-%d") == signal_date]
    if len(positions) != 1 or positions[0] + 1 >= len(frame):
        raise ValueError("signal must identify a session before the final bar")
    entry_position = int(positions[0]) + 1
    entry = float(frame.iloc[entry_position]["open"])
    observed = frame.iloc[entry_position:]
    maximum = float(observed["high"].max())
    trade = {
        "trade_id": f"PROBE-{code}-{signal_date}",
        "code": code,
        "name": item["name"],
        "entry_date": frame.iloc[entry_position]["date"].date().isoformat(),
        "entry_price": entry,
        "defense": stop,
        "initial_risk_pct": (entry - stop) / entry * 100,
        "mfe_pct": (maximum / entry - 1) * 100,
        "max_favorable_price": maximum,
    }
    pivot = _confirmed_pivot_defenses(frame, entry_position)
    results = {
        "plan": trade,
        "fixed": simulate_simple_variant(frame=frame, trade=trade, variant="FIXED_PIVOT"),
        "ma21_one": simulate_simple_variant(frame=frame, trade=trade, variant="MA21_ONE_CLOSE"),
        "ma21_two": simulate_simple_variant(frame=frame, trade=trade, variant="MA21_TWO_CLOSES"),
        "hybrid_no_volume_cut": simulate_hybrid_variant(
            frame=frame,
            trade=trade,
            pivot_defenses=pivot,
            partial_fraction=0.0,
        ),
    }
    print(json.dumps(results, ensure_ascii=False, indent=2))


def _buy_cost(shares: int, price: float) -> float:
    return shares * price * 1.001425


def _sell_proceeds(shares: int, price: float) -> float:
    return shares * price * (1.0 - 0.001425 - 0.003)


def _tw_stock_tick(price: float) -> float:
    """Return one Taiwan common-stock price tick for an executable raw price."""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1_000:
        return 1.0
    return 5.0


def _dividends(code: str) -> dict[str, float]:
    source = json.loads((RUN / "sources/yahoo" / f"{code}.json").read_text(encoding="utf-8"))
    result = source["response"]["chart"]["result"][0]
    rows: dict[str, float] = {}
    for event in (result.get("events", {}).get("dividends") or {}).values():
        day = datetime.fromtimestamp(int(event["date"]), timezone.utc).date().isoformat()
        rows[day] = rows.get(day, 0.0) + float(event["amount"])
    return rows


def _simulate_code(
    item: dict[str, Any],
    approved: list[dict[str, Any]],
    *,
    allow_adds: bool,
    lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from scripts.course_daily_screen_trial import _add_indicators
    from scripts.course_exit_variant_backtest import _confirmed_pivot_defenses

    source = pd.read_csv(RUN / "sources/prices" / f"{item['code']}.csv", parse_dates=["date"])
    indicator_input = source[["date", "open", "high", "low", "close", "volume"]].copy()
    technical = _add_indicators(indicator_input)
    for column in ("raw_open", "raw_high", "raw_low", "raw_close", "atr14"):
        technical[column] = source[column].to_numpy()
    start_rows = technical.index[technical["date"].dt.strftime("%Y-%m-%d") >= item["monitor_on"]]
    if len(start_rows) == 0:
        raise ValueError(f"no monitoring bars for {item['code']}")
    start_position = int(start_rows[0])
    pivot_defenses = _confirmed_pivot_defenses(technical, start_position)
    signals = {row["signal_date"]: row for row in approved}
    distributions = _dividends(item["code"])
    pending_buy: dict[str, Any] | None = None
    pending_exit: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    episodes: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    episode_number = 0
    watch_active = True
    invalidation_on = None if lifecycle is None else lifecycle.get("date")
    invalidation_reason = None if lifecycle is None else lifecycle.get("reason")

    for position, row in technical.iloc[start_position:].iterrows():
        day = row["date"].date().isoformat()

        # Yahoo Close is split-normalized.  Shares here are therefore expressed
        # in current-share-equivalent units and must not be multiplied again on
        # Yahoo split events.  Dividends are on the same normalized basis.
        if current and day in distributions:
            amount = distributions[day] * sum(t["shares"] for t in current["tranches"])
            current["dividend_cash"] += amount
            current["cash_events"].append({"date": day, "event": "DIVIDEND（現金股利）", "cash": round(amount, 4)})

        if current and pending_exit:
            raw_price = float(row["raw_open"])
            for tranche in current["tranches"]:
                tranche["sell_price"] = raw_price
                tranche["sell_date"] = day
                tranche["sell_proceeds"] = _sell_proceeds(tranche["shares"], raw_price)
            current["exit_date"] = day
            current["exit_price_raw"] = raw_price
            current["exit_price_adjusted"] = float(row["open"])
            current["exit_reason"] = pending_exit["reason"]
            current["status"] = "CLOSED（交易已結束）"
            current["realized_pnl"] = (
                sum(t["sell_proceeds"] - t["buy_cost"] for t in current["tranches"])
                + current["dividend_cash"]
            )
            current["net_pnl"] = current["realized_pnl"]
            current["net_return_on_deployed_pct"] = current["net_pnl"] / sum(t["buy_cost"] for t in current["tranches"]) * 100
            episodes.append(current)
            audit.append({"date": day, "event": "SELL（賣出）", "episode": current["episode_id"], "reason": current["exit_reason"]})
            current = None
            pending_exit = None

        if pending_buy:
            adjusted_open = float(row["open"])
            cap = float(pending_buy["signal_close"]) + 0.5 * float(pending_buy["signal_atr"])
            raw_open = float(row["raw_open"])
            adjustment_factor = adjusted_open / raw_open
            raw_cap = cap / adjustment_factor
            tick_tolerance = _tw_stock_tick(raw_cap) * adjustment_factor
            if adjusted_open <= float(pending_buy["stop"]):
                audit.append({"date": day, "event": "BUY_SKIPPED（取消進場）", "reason": "開盤已低於結構防線", "signal": pending_buy})
            elif adjusted_open > cap + tick_tolerance:
                audit.append(
                    {
                        "date": day,
                        "event": "BUY_SKIPPED（取消進場）",
                        "reason": "開盤超過追價上限及一個最小跳動單位容許值",
                        "signal": pending_buy,
                        "cap": round(cap, 4),
                        "tick_tolerance": round(tick_tolerance, 4),
                    }
                )
            else:
                raw_price = raw_open
                shares = math.floor(10_000 / (raw_price * 1.001425))
                if shares <= 0:
                    raise ValueError(f"{item['code']} cannot buy a single share with TWD 10,000")
                tranche = {
                    "role": pending_buy["role"],
                    "signal_date": pending_buy["signal_date"],
                    "entry_date": day,
                    "entry_price_raw": raw_price,
                    "entry_price_adjusted": adjusted_open,
                    "shares": shares,
                    "buy_cost": _buy_cost(shares, raw_price),
                    "stop_adjusted": float(pending_buy["stop"]),
                    "family": pending_buy["family"],
                    "scenario": pending_buy["scenario"],
                    "reason": pending_buy["reason"],
                }
                if current is None:
                    episode_number += 1
                    current = {
                        "code": item["code"],
                        "name": item["name"],
                        "episode_id": f"{item['code']}-E{episode_number}",
                        "attempt_number": episode_number,
                        "campaign_id": f"{item['code']}-C1",
                        "entry_date": day,
                        "mother_entry_adjusted": adjusted_open,
                        "initial_defense": float(pending_buy["stop"]),
                        "dynamic_defense": float(pending_buy["stop"]),
                        "small_pivot_defense": None,
                        "small_pivot_warnings": [],
                        "last_warned_small_pivot": None,
                        "initial_risk": adjusted_open - float(pending_buy["stop"]),
                        "profit_protect": False,
                        "profit_protect_activation_date": None,
                        "below_ma21_streak": 0,
                        "tranches": [],
                        "dividend_cash": 0.0,
                        "cash_events": [],
                        "mfe_high_adjusted": adjusted_open,
                        "mae_low_adjusted": adjusted_open,
                        "exit_signal_date": None,
                    }
                else:
                    current["dynamic_defense"] = max(current["dynamic_defense"], float(pending_buy["stop"]))
                current["tranches"].append(tranche)
                audit.append({"date": day, "event": f"BUY_{tranche['role']}（買進）", "episode": current["episode_id"], "shares": shares, "raw_price": round(raw_price, 4)})
            pending_buy = None

        if current:
            current["mfe_high_adjusted"] = max(current["mfe_high_adjusted"], float(row["high"]))
            current["mae_low_adjusted"] = min(current["mae_low_adjusted"], float(row["low"]))
            close = float(row["close"])
            current["below_ma21_streak"] = current["below_ma21_streak"] + 1 if close < float(row["MA21"]) else 0
            reason = None
            if close < current["dynamic_defense"]:
                reason = "DYNAMIC_OR_POSITION_DEFENSE（動態或部位防線失守）"
            elif current["profit_protect"] and current["below_ma21_streak"] >= 2:
                reason = "MA21_TWO_CLOSES（波段期連續兩日跌破21MA）"
            if reason:
                current["exit_signal_date"] = day
                pending_exit = {"reason": reason}
                audit.append({"date": day, "event": "EXIT_TRIGGERED（已觸發出場）", "episode": current["episode_id"], "reason": reason})
            else:
                small_defense = current.get("small_pivot_defense")
                if (
                    current["profit_protect"]
                    and small_defense is not None
                    and close < float(small_defense)
                    and current.get("last_warned_small_pivot") != float(small_defense)
                ):
                    warning = {
                        "date": day,
                        "defense": float(small_defense),
                        "close": close,
                        "event": "SMALL_PIVOT_BROKEN（小級樞紐失守警告）",
                    }
                    current["small_pivot_warnings"].append(warning)
                    current["last_warned_small_pivot"] = float(small_defense)
                    audit.append({**warning, "episode": current["episode_id"]})
                activation = current["mother_entry_adjusted"] + 2.0 * current["initial_risk"]
                if not current["profit_protect"] and float(row["high"]) >= activation:
                    current["profit_protect"] = True
                    current["profit_protect_activation_date"] = day
                    weighted_cost = sum(t["entry_price_adjusted"] * t["shares"] for t in current["tranches"]) / sum(t["shares"] for t in current["tranches"])
                    current["dynamic_defense"] = max(current["dynamic_defense"], weighted_cost)
                    current["below_ma21_streak"] = 1 if close < float(row["MA21"]) else 0
                if current["profit_protect"]:
                    defense = pivot_defenses.get(position)
                    if defense is not None and float(defense) < close:
                        previous = current.get("small_pivot_defense")
                        current["small_pivot_defense"] = (
                            float(defense) if previous is None else max(float(previous), float(defense))
                        )

        if invalidation_on == day and watch_active:
            watch_active = False
            pending_buy = None
            audit.append(
                {
                    "date": day,
                    "event": "CAMPAIGN_INVALIDATED（大結構交易週期失效）",
                    "reason": invalidation_reason,
                }
            )
            audit.append(
                {
                    "date": day,
                    "event": "REMOVED_FROM_WATCHLIST（已移出監控）",
                    "reason": "大結構防線失守；必須等待上游選股策略再次選入",
                }
            )
            if current and pending_exit is None:
                current["exit_signal_date"] = day
                pending_exit = {"reason": "CAMPAIGN_INVALIDATED（大結構交易週期失效）"}

        decision = signals.get(day)
        if decision and not watch_active:
            audit.append(
                {
                    "date": day,
                    "event": "SIGNAL_BLOCKED_REMOVED_FROM_WATCHLIST（訊號遭監控資格阻擋）",
                    "reason": "大結構失效後尚無新的上游選股入選事件",
                }
            )
        elif decision and pending_exit is None:
            if current is None and ({"MOTHER", "REENTRY"} & set(decision["eligibility"])):
                role = "MOTHER（母單）"
            elif current and allow_adds and "ADD" in decision["eligibility"] and len(current["tranches"]) < 3:
                weighted = sum(t["entry_price_adjusted"] * t["shares"] for t in current["tranches"]) / sum(t["shares"] for t in current["tranches"])
                if float(row["close"]) <= weighted:
                    audit.append({"date": day, "event": "ADD_SKIPPED（取消加碼）", "reason": "持倉尚未獲利", "episode": current["episode_id"]})
                    continue
                role = f"ADD_{len(current['tranches'])}（第{len(current['tranches'])}次加碼）"
            else:
                audit.append({"date": day, "event": "SIGNAL_EVIDENCE_ONLY（僅保存訊號證據）", "reason": "目前部位角色不允許重複成交"})
                continue
            pending_buy = {
                **decision,
                "role": role,
                "signal_close": float(row["close"]),
                "signal_atr": float(row["ATR14"]),
            }

    if current:
        final = technical.iloc[-1]
        current["status"] = "OPEN（持有中）"
        current["exit_date"] = None
        current["exit_price_raw"] = None
        current["exit_price_adjusted"] = None
        current["exit_reason"] = "AS_OF（截至回測日）"
        current["mark_date"] = final["date"].date().isoformat()
        current["mark_price_raw"] = float(final["raw_close"])
        current["mark_price_adjusted"] = float(final["close"])
        liquidation = sum(_sell_proceeds(t["shares"], float(final["raw_close"])) - t["buy_cost"] for t in current["tranches"])
        current["unrealized_pnl_after_exit_cost"] = liquidation + current["dividend_cash"]
        current["net_pnl"] = current["unrealized_pnl_after_exit_cost"]
        current["net_return_on_deployed_pct"] = current["net_pnl"] / sum(t["buy_cost"] for t in current["tranches"]) * 100
        episodes.append(current)

    for episode in episodes:
        episode["tranche_count"] = len(episode["tranches"])
        episode["deployed_cash"] = sum(t["buy_cost"] for t in episode["tranches"])
        episode["mfe_pct_from_mother"] = (episode["mfe_high_adjusted"] / episode["mother_entry_adjusted"] - 1.0) * 100
        episode["mae_pct_from_mother"] = (episode["mae_low_adjusted"] / episode["mother_entry_adjusted"] - 1.0) * 100
    return {
        "code": item["code"],
        "name": item["name"],
        "monitor_on": item["monitor_on"],
        "monitor_until": invalidation_on,
        "watch_status": "REMOVED_FROM_WATCHLIST（已移出監控）" if not watch_active else "WATCHING（監控中）",
        "episodes": episodes,
        "audit": audit,
    }


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    episodes = [episode for stock in result["stocks"] for episode in stock["episodes"]]
    deployed = sum(float(episode["deployed_cash"]) for episode in episodes)
    net = sum(float(episode["net_pnl"]) for episode in episodes)
    events: dict[str, dict[str, list[tuple[str, float]]]] = {}
    for episode in episodes:
        for tranche in episode["tranches"]:
            day = tranche["entry_date"]
            events.setdefault(day, {"open": [], "close": []})["open"].append((episode["episode_id"], float(tranche["buy_cost"])))
        if episode.get("exit_date"):
            day = episode["exit_date"]
            for tranche in episode["tranches"]:
                events.setdefault(day, {"open": [], "close": []})["close"].append((episode["episode_id"], float(tranche["buy_cost"])))
    active_tranches = 0
    active_cash = 0.0
    episode_counts: dict[str, int] = {}
    maximum_tranches = maximum_episodes = 0
    peak_cash = 0.0
    for day in sorted(events):
        # Exits execute at the open before any new entry queued for this session.
        for episode_id, cash in events[day]["close"]:
            active_tranches -= 1
            active_cash -= cash
            episode_counts[episode_id] -= 1
            if episode_counts[episode_id] == 0:
                del episode_counts[episode_id]
        for episode_id, cash in events[day]["open"]:
            active_tranches += 1
            active_cash += cash
            episode_counts[episode_id] = episode_counts.get(episode_id, 0) + 1
        maximum_tranches = max(maximum_tranches, active_tranches)
        maximum_episodes = max(maximum_episodes, len(episode_counts))
        peak_cash = max(peak_cash, active_cash)
    realized = sum(float(episode["net_pnl"]) for episode in episodes if episode["status"].startswith("CLOSED"))
    unrealized = sum(float(episode["net_pnl"]) for episode in episodes if episode["status"].startswith("OPEN"))
    return {
        "stocks": len(result["stocks"]),
        "trade_episodes": len(episodes),
        "closed": sum(episode["status"].startswith("CLOSED") for episode in episodes),
        "open": sum(episode["status"].startswith("OPEN") for episode in episodes),
        "tranches": sum(int(episode["tranche_count"]) for episode in episodes),
        "deployed_cash_sum": round(deployed, 2),
        "net_pnl": round(net, 2),
        "realized_net_pnl": round(realized, 2),
        "unrealized_net_pnl_after_estimated_exit_cost": round(unrealized, 2),
        "return_on_deployed_cash_pct": round(net / deployed * 100, 4) if deployed else 0.0,
        "win_rate_pct": round(sum(float(episode["net_pnl"]) > 0 for episode in episodes) / len(episodes) * 100, 2) if episodes else 0.0,
        "average_episode_mfe_pct": round(sum(float(episode["mfe_pct_from_mother"]) for episode in episodes) / len(episodes), 4) if episodes else 0.0,
        "maximum_concurrent_episodes": maximum_episodes,
        "maximum_concurrent_tranches": maximum_tranches,
        "peak_concurrent_deployed_cash": round(peak_cash, 2),
    }


def replay() -> None:
    manifest = json.loads((RUN / "input_manifest.json").read_text(encoding="utf-8"))
    authored_path = RUN / "ai_authored_trigger_review.json"
    authored = json.loads(authored_path.read_text(encoding="utf-8"))
    output: dict[str, Any] = {
        "version": "enlightenment-ai-small-test-backtest-v2",
        "as_of": AS_OF,
        "source_manifest_sha256": digest(RUN / "input_manifest.json"),
        "ai_decisions_sha256": digest(authored_path),
        "costs": {"buy_commission": 0.001425, "sell_commission": 0.001425, "sell_tax": 0.003},
        "exit_rule": "固定／加碼防線；母單達 +2R 後成本防線只升不降，因果確認小級樞紐僅警告，不再直接清掉母單；連續兩日收盤跌破21MA才觸發波段出場。",
        "variants": {},
        "rejected_key_opportunities": authored["rejected_key_opportunities"],
    }
    for key, allow_adds in (("MOTHER_ONLY_10K（母單一筆一萬元）", False), ("MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）", True)):
        result = {"name": key, "stocks": []}
        for item in manifest["items"]:
            result["stocks"].append(
                _simulate_code(
                    item,
                    authored["approved_events"].get(item["code"], []),
                    allow_adds=allow_adds,
                    lifecycle=authored.get("campaign_invalidations", {}).get(item["code"]),
                )
            )
        result["summary"] = _summary(result)
        output["variants"][key] = result
    save_json(RUN / "backtest.json", output)
    print(json.dumps({key: value["summary"] for key, value in output["variants"].items()}, ensure_ascii=False, indent=2))


def _money(value: float) -> str:
    return f"{value:+,.0f}"


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def report() -> None:
    result = json.loads((RUN / "backtest.json").read_text(encoding="utf-8"))
    old_result = json.loads((OLD_RUN / "backtest.json").read_text(encoding="utf-8"))
    authored = json.loads((RUN / "ai_authored_trigger_review.json").read_text(encoding="utf-8"))
    manifest = json.loads((RUN / "input_manifest.json").read_text(encoding="utf-8"))
    lines = [
        "# 六檔啟蒙層 AI 綜合判讀回測 v2",
        "",
        "> 這是 `RETROSPECTIVE_NOT_BLIND（事後研究、不是樣本外盲測）`。每個訊號的證據仍切在訊號日，但 AI 已處於知道完整歷史存在的研究環境；結果只能用來校準規則，不能當成未來績效保證。",
        "",
        "## 口徑",
        "",
        f"- 行情截至：{result['as_of']}。月份輸入一律換成該月第一個實際交易日。",
        "- 技術判讀：公司行動回溯調整 OHLC；成交帳務：Yahoo 分割標準化原始價格、現金股利、買賣手續費各 0.1425%、賣出交易稅 0.3%。",
        "- 訊號：當日日 K 收盤成立，下一交易日開盤成交；若開盤低於防線或超過訊號收盤 + 0.5ATR 加一個最小跳動單位容許值，取消該次進場。固定投入約一萬元，不啟用跳空縮量。",
        "- 出場：固定／加碼防線；母單達 +2R 後成本防線只升不降。因果確認的小級樞紐失守只進入警告，不直接清掉母單；連續兩日收盤跌破 21MA 才觸發波段出場。",
        "- 監控資格：大級防線失守即標記 `CAMPAIGN_INVALIDATED（大結構交易週期失效）` 並移出監控；沒有新的上游選股入選事件，不得再觸發。",
        "- `MOTHER_PLUS_2（母單＋最多兩次獲利後加碼）` 只接受不同日期、獨立結構，且訊號收盤時整體持倉已有浮盈。",
        "",
        "## 監控起點與資料量",
        "",
        "| 股票 | 使用者輸入 | 實際監控起日 | 監控前日 K | 資料首日 |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in manifest["items"]:
        lines.append(f"| {item['code']} {item['name']} | {item['monitor_month']} | {item['monitor_on']} | {item['pre_monitor_bars']:,} | {item['first_bar']} |")
    lines += ["", "## 兩種部位版本結果", "", "| 版本 | 回合 | 已結束／持有中 | 成交份數 | 已實現 | 未實現* | 合計淨損益 | 投入金額合計報酬 | 最大同時股票／份數 | 尖峰占用資金 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for variant in result["variants"].values():
        summary = variant["summary"]
        lines.append(
            f"| {variant['name']} | {summary['trade_episodes']} | {summary['closed']}／{summary['open']} | {summary['tranches']} | "
            f"{_money(summary['realized_net_pnl'])} | {_money(summary['unrealized_net_pnl_after_estimated_exit_cost'])} | {_money(summary['net_pnl'])} | "
            f"{_pct(summary['return_on_deployed_cash_pct'])} | {summary['maximum_concurrent_episodes']}／{summary['maximum_concurrent_tranches']} | {summary['peak_concurrent_deployed_cash']:,.0f} |"
        )
    lines += ["", "\\* 未實現損益已假設在回測截止收盤賣出並扣除賣出成本；不是實際已成交。", ""]
    lines += [
        "## v1／v2 差異",
        "",
        "| 版本 | v1合計淨損益 | v2合計淨損益 | 差額 | v1投入金額合計報酬 | v2投入金額合計報酬 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, variant in result["variants"].items():
        current_summary = variant["summary"]
        old_summary = old_result["variants"][key]["summary"]
        delta = float(current_summary["net_pnl"]) - float(old_summary["net_pnl"])
        lines.append(
            f"| {key} | {_money(old_summary['net_pnl'])} | {_money(current_summary['net_pnl'])} | {_money(delta)} | "
            f"{_pct(old_summary['return_on_deployed_cash_pct'])} | {_pct(current_summary['return_on_deployed_cash_pct'])} |"
        )
    lines += [
        "",
        "本次同時修正三項已確認語意：小級樞紐失守只警告、不直接清掉母單；豐藝於 2024/12/16 大結構失效後移出監控；0.5ATR 追價線增加一個最小跳動單位容許值。沒有啟用跳空縮量。",
        "",
        "### 關鍵案例",
        "",
        "- 合晶 2026/6/5 跌破 87.30 只記錄 `SMALL_PIVOT_BROKEN（小級樞紐失守警告）`；其後於 2026/7/21 連續兩日跌破21MA，7/22 開盤 134 元出場。",
        "- 豐藝 2024/12/16 轉為 `REMOVED_FROM_WATCHLIST（已移出監控）`；2025/7 與 2026/4～6 的訊號全部由監控資格閘門阻擋。",
        "- 豐藝 2024/2/2 的開盤只超過原0.5ATR線不到一個最小跳動單位，因此成交；金居 2026/4/8 的跳空仍遠超容許值，繼續取消。",
        "",
    ]

    for variant in result["variants"].values():
        lines += [f"## {variant['name']}逐筆回合", "", "| 股票／回合 | 訊號→進場 | 份數 | 初始防線 | 出場訊號→成交／原因 | MFE | MAE | 淨損益 | 報酬 | 狀態 |", "|---|---|---:|---:|---|---:|---:|---:|---:|---|"]
        for stock in variant["stocks"]:
            for episode in stock["episodes"]:
                mother = episode["tranches"][0]
                if episode["status"].startswith("OPEN"):
                    exit_text = f"截至 {episode['mark_date']}／{episode['mark_price_raw']:.2f}"
                else:
                    exit_text = f"{episode['exit_signal_date']}→{episode['exit_date']}／{episode['exit_price_raw']:.2f}；{episode['exit_reason']}"
                lines.append(
                    f"| {episode['code']} {episode['name']}／{episode['episode_id']} | {mother['signal_date']}→{mother['entry_date']}／{mother['entry_price_raw']:.2f} | "
                    f"{episode['tranche_count']} | {episode['initial_defense']:.2f} | {exit_text} | {_pct(episode['mfe_pct_from_mother'])} | {_pct(episode['mae_pct_from_mother'])} | "
                    f"{_money(episode['net_pnl'])} | {_pct(episode['net_return_on_deployed_pct'])} | {episode['status']} |"
                )
        lines.append("")
        lines += ["### 各份成交", "", "| 股票／回合 | 角色 | 觸發家族 | 訊號日 | 進場日／原始價／股數 | 該份防線 | 出場日／原始價 |", "|---|---|---|---:|---|---:|---|"]
        for stock in variant["stocks"]:
            for episode in stock["episodes"]:
                for tranche in episode["tranches"]:
                    sold = "持有中" if tranche.get("sell_date") is None else f"{tranche['sell_date']}／{tranche['sell_price']:.2f}"
                    lines.append(
                        f"| {episode['code']} {episode['name']}／{episode['episode_id']} | {tranche['role']} | {tranche['family']} | {tranche['signal_date']} | "
                        f"{tranche['entry_date']}／{tranche['entry_price_raw']:.2f}／{tranche['shares']}股 | {tranche['stop_adjusted']:.2f} | {sold} |"
                    )
        lines.append("")

    lines += ["## 金居判讀修正", "", "金居不是被永久排除。2025/7～9 應判為 `FRESH_Q1_EXPANSION（新生定錨直接擴張）` 的大級初升；只是 2025/9 加入監控時，小級正處急漲延伸。正確狀態流程是：", "", "1. 2025/09/09：`WATCHING（監控中）`，拒絕當日追價但保留名單。", "2. 2025/10/28：第一次較可控修正完成，收復 21MA 且突破 223.97 小道氏防線，觸發母單；10/29 開盤成交。", "3. 2026/01/09：達 +2R 後連兩日跌破 21MA，1/12 出場；本回合仍為正報酬。", "4. 2026/01/14：大結構未壞，重新觸發新母單；2/2 小級防線失守，小賠退出。", "5. 2026/04/07：有再進場計畫，但 4/8 跳空超過追價上限，依規則取消，不因後來大漲改判成交。", "6. 2026/08/25：大修正後形成 392 較高低點，以 LR（左右）再進母單；截至 9/4 仍為 `OPEN（持有中）`。", "", "這表示 `REJECTED（不合格）` 應該作用於某一次交易機會，不應自動等於把一檔具大級多頭潛力的股票移出監控。", ""]

    lines += ["## 關鍵未成交／拒絕事件", "", "| 股票 | 日期 | 狀態 | 原因 |", "|---|---:|---|---|"]
    for code, rows in authored["rejected_key_opportunities"].items():
        name = next(item["name"] for item in manifest["items"] if item["code"] == code)
        for row in rows:
            lines.append(f"| {code} {name} | {row['date']} | {row['status']} | {row['reason']} |")
    lines += ["", "## 解讀限制", "", "- 只有六檔，且股票與監控日期由使用者指定，不代表完整選股母體。", "- AI 判讀規則是在同一段對話中持續校準，尤其金居的級數分類已依本次檢討修正；因此這份是研究校準，不是凍結規則後的樣本外驗證。", "- 加碼版增加的是絕對獲利，不一定提高資金效率；應同時比較合計淨損益、投入金額合計報酬及尖峰資金占用。", "- 下一輪應先凍結這份判讀規格，再拿全新的股票／日期做真正前向或盲式回放。", ""]
    (RUN / "backtest.md").write_text("\n".join(lines), encoding="utf-8")
    print(RUN / "backtest.md")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "charts", "summaries", "events", "probe", "replay", "report"])
    parser.add_argument("--code", default="")
    parser.add_argument("--signal", default="")
    parser.add_argument("--stop", type=float, default=0.0)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "charts":
        charts()
    elif args.command == "summaries":
        summaries()
    elif args.command == "events":
        if args.code not in {item["code"] for item in COHORT}:
            raise ValueError("--code must name one cohort stock")
        events(args.code)
    elif args.command == "probe":
        if args.code not in {item["code"] for item in COHORT} or not args.signal or args.stop <= 0:
            raise ValueError("probe requires valid --code, --signal and --stop")
        probe(args.code, args.signal, args.stop)
    elif args.command == "replay":
        replay()
    elif args.command == "report":
        report()


if __name__ == "__main__":
    main()
