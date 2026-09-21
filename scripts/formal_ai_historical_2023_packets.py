"""Build facts-only causal packets for the 2023 historical-scan universe.

The module is intentionally limited to provenance, market-data freezing, and
dated fact extraction.  It does not classify an enlightenment scenario, score
a setup, approve a trigger, or infer a stop.  Those decisions belong to the
separate formal AI review under judgement specifications V1 and V2.

The historical scan ended on 2023-05-03.  Every selected stock is monitored
from ``max(2023-05-02, first selected day)`` through 2023-09-04.  An ``asof``
view always rebuilds indicators and pivots after truncating the price frame,
so a reviewer cannot accidentally see a future-confirmed structure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.formal_ai_full_review_packets import (
    add_causal_pivots,
    add_indicators,
    confirmed_pivot_timeline,
    daily_fact_row,
    macd_cycles,
)


def _configured_path(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else fallback


SOURCE_EVENTS = _configured_path(
    "FORMAL_AI_SOURCE_EVENTS",
    ROOT
    / "reports/historical_scan/2023-01-01_2023-05-03"
    / "all_scan_2023-01-01_2023-05-03_events.json",
)
STOCK_LIST = ROOT / "stock_list.json"
REUSABLE_2026_MANIFEST = _configured_path(
    "FORMAL_AI_REUSABLE_MANIFEST",
    ROOT / "reports/course_backtest/2026-09-06/tg_enlightenment_ai_v2/input_manifest.json",
)
RUN = _configured_path(
    "FORMAL_AI_RUN",
    ROOT / "reports/course_backtest/2023-09-04/historical_scan_formal_ai_v1_v2",
)
CATALOG = RUN / "catalog.json"
SOURCE_MANIFEST = RUN / "input_manifest.json"
PACKET_DIR = RUN / "review_packets"
PRICE_DIR = RUN / "sources/prices"
EVENT_DIR = RUN / "sources/events"

FETCH_FROM = os.environ.get("FORMAL_AI_FETCH_FROM", "2018-01-01")
AS_OF = os.environ.get("FORMAL_AI_AS_OF", "2023-09-04")
MONITOR_FLOOR = os.environ.get("FORMAL_AI_MONITOR_FLOOR", "2023-05-02")
SELECTION_START = os.environ.get("FORMAL_AI_SELECTION_START", "2023-01-01")
SELECTION_END = os.environ.get("FORMAL_AI_SELECTION_END", "2023-05-03")
EXPECTED_STOCKS = int(os.environ.get("FORMAL_AI_EXPECTED_STOCKS", "889"))
PREFERRED_PRE_MONITOR_BARS = 750
METHOD_VERSION = "formal-ai-historical-2023-facts-v1"

V1_DOC = ROOT / "docs/enlightenment-ai-judgement-v1.md"
V2_DOC = ROOT / "docs/enlightenment-ai-judgement-v2.md"
V1_SCHEMA = ROOT / "config/enlightenment_ai_judgement_v1.schema.json"
V2_SCHEMA = ROOT / "config/enlightenment_ai_judgement_v2.schema.json"
V2_RULES = ROOT / "config/enlightenment_ai_rules_v2.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _technical_key(signal: str) -> str:
    compact = re.sub(r"\s+", "", signal)
    match = re.fullmatch(r"突破(5|13|21|55|105|144)MA", compact)
    if match:
        return f"TECH_MA{match.group(1)}_BREAKOUT"
    match = re.fullmatch(r"跌破後收復(5|13|21|55|105|144)MA", compact)
    if match:
        return f"TECH_MA{match.group(1)}_RECLAIM"
    if compact == "KD黃金交叉":
        return "TECH_KD_GOLDEN"
    if compact == "MACD黃金交叉":
        return "TECH_MACD_GOLDEN"
    # Preserve an unknown technical category verbatim instead of silently
    # merging it into a generic technical bucket.
    return f"TECHNICAL::{signal.strip()}"


def normalize_strategy(event: dict[str, Any]) -> str:
    """Map upstream events to comparison keys without classifying the chart."""
    strategy = str(event.get("strategy") or "").strip().lower()
    if strategy == "technical":
        return _technical_key(str(event.get("signal") or ""))
    if strategy == "financial":
        return "FINANCIAL_G1_G2"
    if strategy.startswith("chip_"):
        return "CHIP"
    if strategy == "curated":
        return "CURATED"
    if strategy == "laoxiao":
        return "LAOXIAO"
    return f"UPSTREAM::{strategy or 'UNKNOWN'}"


def _stock_universe() -> dict[str, dict[str, Any]]:
    payload = read_json(STOCK_LIST)
    rows = payload.get("stocks", payload) if isinstance(payload, dict) else payload
    return {str(row["code"]): dict(row) for row in rows}


def build_catalog_payload(
    raw_events: list[dict[str, Any]],
    stock_universe: dict[str, dict[str, Any]],
    *,
    expected_stocks: int | None = EXPECTED_STOCKS,
) -> dict[str, Any]:
    """Deduplicate historical selections and build the formal watch universe."""
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    raw_by_code: Counter[str] = Counter()
    first_raw: dict[str, dict[str, Any]] = {}
    for raw in raw_events:
        code = str(raw.get("code") or "").strip()
        day = str(raw.get("date") or "").strip()
        if not code or not day:
            continue
        source = normalize_strategy(raw)
        raw_by_code[code] += 1
        first_raw.setdefault(code, raw)
        key = (code, day, source)
        if key not in grouped:
            grouped[key] = {
                "date": day,
                "strategy": source,
                "code": code,
                "name": str(raw.get("name") or ""),
                "long_eligible": True,
                "raw_strategies": set(),
                "raw_signals": set(),
                "raw_observation_count": 0,
            }
        record = grouped[key]
        record["raw_strategies"].add(str(raw.get("strategy") or ""))
        record["raw_signals"].add(str(raw.get("signal") or ""))
        record["raw_observation_count"] += 1

    codes = sorted(first_raw, key=lambda value: (int(value) if value.isdigit() else math.inf, value))
    if expected_stocks is not None and len(codes) != expected_stocks:
        raise ValueError(f"historical event universe is {len(codes)} stocks, expected {expected_stocks}")
    missing = [code for code in codes if code not in stock_universe]
    if missing:
        raise ValueError(f"stock_list.json is missing {len(missing)} historical codes: {missing[:20]}")

    events = []
    for key in sorted(grouped, key=lambda value: (value[1], value[0], value[2])):
        row = grouped[key]
        events.append(
            {
                **{name: value for name, value in row.items() if name not in {"raw_strategies", "raw_signals"}},
                "raw_strategies": sorted(row["raw_strategies"]),
                "raw_signals": sorted(row["raw_signals"]),
            }
        )

    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        by_code[str(row["code"])].append(row)
    monitor_stocks = []
    for code in codes:
        rows = by_code[code]
        dates = sorted({str(row["date"]) for row in rows})
        strategies = sorted({str(row["strategy"]) for row in rows})
        universe_row = stock_universe[code]
        first_selected = dates[0]
        monitor_on = max(MONITOR_FLOOR, first_selected)
        monitor_stocks.append(
            {
                "code": code,
                "name": str(universe_row.get("name") or first_raw[code].get("name") or ""),
                "industry": str(universe_row.get("industry") or first_raw[code].get("industry") or ""),
                "symbol": str(universe_row["symbol"]),
                "market": str(universe_row.get("market") or ""),
                "first_selected_date": first_selected,
                "monitor_on": monitor_on,
                "watchlist_entry_type": (
                    "LEFT_CENSORED_CARRY_IN"
                    if first_selected < MONITOR_FLOOR
                    else "NEW_UPSTREAM_SELECTION"
                ),
                "last_selected_date": dates[-1],
                "selection_event_count": len(rows),
                "raw_selection_event_count": int(raw_by_code[code]),
                "selection_date_count": len(dates),
                "strategy_count": len(strategies),
                "strategies": strategies,
            }
        )

    strategy_counts = Counter(str(row["strategy"]) for row in events)
    return {
        "method_version": METHOD_VERSION,
        "judgement_boundary": "UPSTREAM_SELECTION_PROVENANCE_ONLY（只整理上游選股，不判讀圖形或核准交易）",
        "source_path": str(SOURCE_EVENTS.resolve()),
        "source_sha256": digest(SOURCE_EVENTS) if SOURCE_EVENTS.exists() else None,
        "selection_window": {"start": SELECTION_START, "end": SELECTION_END},
        "monitor_rule": f"max({MONITOR_FLOOR}, first_selected_date)",
        "as_of": AS_OF,
        "raw_event_count": len(raw_events),
        "event_count": len(events),
        "duplicate_or_collapsed_event_count": len(raw_events) - len(events),
        "strategy_counts": dict(sorted(strategy_counts.items())),
        "monitor_stock_count": len(monitor_stocks),
        "left_censored_carry_in_count": sum(
            row["watchlist_entry_type"] == "LEFT_CENSORED_CARRY_IN"
            for row in monitor_stocks
        ),
        "monitor_codes": codes,
        "monitor_stocks": monitor_stocks,
        "events": events,
    }


def build_catalog() -> dict[str, Any]:
    payload = read_json(SOURCE_EVENTS)
    catalog = build_catalog_payload(list(payload["events"]), _stock_universe())
    write_json(CATALOG, catalog)
    return catalog


def selection_maps(
    catalog: dict[str, Any],
) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, Any]]]:
    by_code: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    meta = {str(row["code"]): row for row in catalog["monitor_stocks"]}
    for event in catalog["events"]:
        code = str(event["code"])
        day = str(event["date"])
        by_code[code][day].append(str(event["strategy"]))
    selections = {
        code: {day: sorted(set(values)) for day, values in sorted(days.items())}
        for code, days in by_code.items()
    }
    return selections, meta


def payload_to_frame(payload: dict[str, Any]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
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
    frame = frame.dropna(
        subset=["date", "raw_open", "raw_high", "raw_low", "raw_close", "adj_close"]
    )
    factor = frame["adj_close"] / frame["raw_close"]
    for target, raw in (
        ("open", "raw_open"),
        ("high", "raw_high"),
        ("low", "raw_low"),
        ("close", "raw_close"),
    ):
        frame[target] = frame[raw] * factor
    frame = frame.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    dividends = []
    for event in (result.get("events", {}).get("dividends") or {}).values():
        day = datetime.fromtimestamp(int(event["date"]), timezone.utc).date().isoformat()
        if FETCH_FROM <= day <= AS_OF:
            dividends.append({"date": day, "amount": float(event["amount"])})
    return frame, sorted(dividends, key=lambda row: row["date"])


def _event_date(event: dict[str, Any], fallback: str) -> str | None:
    value = event.get("date", fallback)
    try:
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
        return datetime.fromtimestamp(int(value), timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def payload_events(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Preserve every Yahoo chart event type with normalized event dates."""
    result = payload["chart"]["result"][0]
    normalized: dict[str, list[dict[str, Any]]] = {}
    for event_type, values in (result.get("events") or {}).items():
        rows = []
        iterable = values.items() if isinstance(values, dict) else enumerate(values or [])
        for key, raw in iterable:
            if not isinstance(raw, dict):
                continue
            day = _event_date(raw, str(key))
            if day is None or not (FETCH_FROM <= day <= AS_OF):
                continue
            timestamp = raw.get("date")
            row = {name: value for name, value in raw.items() if name != "date"}
            row.update({"date": day, "timestamp": timestamp})
            rows.append(row)
        normalized[str(event_type)] = sorted(rows, key=lambda row: row["date"])
    return dict(sorted(normalized.items()))


def _split_ratio(event: dict[str, Any]) -> float | None:
    numerator = event.get("numerator")
    denominator = event.get("denominator")
    try:
        if numerator is not None and denominator is not None and float(denominator):
            return float(numerator) / float(denominator)
    except (TypeError, ValueError):
        pass
    direct = event.get("ratio")
    if direct is not None:
        try:
            return float(direct)
        except (TypeError, ValueError):
            pass
    text = str(event.get("splitRatio") or "")
    match = re.fullmatch(r"\s*([0-9.]+)\s*[:/]\s*([0-9.]+)\s*", text)
    if match and float(match.group(2)):
        return float(match.group(1)) / float(match.group(2))
    return None


def corporate_action_audit(
    frame: pd.DataFrame,
    all_events: dict[str, list[dict[str, Any]]],
    monitor_on: str,
) -> dict[str, Any]:
    """Reconcile adjusted/raw factor jumps with Yahoo corporate-action events."""
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"])
    factor = pd.to_numeric(data["adj_close"], errors="coerce") / pd.to_numeric(
        data["raw_close"], errors="coerce"
    )
    relative = factor / factor.shift(1)
    jumps = []
    event_dates: list[tuple[pd.Timestamp, str]] = []
    for event_type, rows in all_events.items():
        for event in rows:
            if monitor_on <= str(event["date"]) <= AS_OF:
                event_dates.append((pd.Timestamp(event["date"]), event_type))
    for index in data.index:
        day = data.loc[index, "date"].date().isoformat()
        change = relative.loc[index]
        if day < monitor_on or day > AS_OF or pd.isna(change) or abs(float(change) - 1.0) <= 1e-5:
            continue
        nearby = sorted(
            {
                event_type
                for event_day, event_type in event_dates
                if abs((event_day - data.loc[index, "date"]).days) <= 4
            }
        )
        jumps.append(
            {
                "date": day,
                "previous_date": data.loc[index - 1, "date"].date().isoformat() if index else None,
                "factor_before": _number(factor.loc[index - 1], 10) if index else None,
                "factor_after": _number(factor.loc[index], 10),
                "factor_change_pct": _number((float(change) - 1.0) * 100.0, 6),
                "matched_event_types_within_4_days": nearby,
                "resolved": bool(nearby),
            }
        )
    splits = []
    unresolved_reasons = []
    for event in all_events.get("splits", []):
        day = str(event["date"])
        if not (monitor_on <= day <= AS_OF):
            continue
        ratio = _split_ratio(event)
        splits.append(
            {
                "date": day,
                "ratio": ratio,
                "splitRatio": event.get("splitRatio"),
                "numerator": event.get("numerator"),
                "denominator": event.get("denominator"),
            }
        )
        if ratio is None or ratio <= 0:
            unresolved_reasons.append(f"split ratio unavailable on {day}")
    for jump in jumps:
        if not jump["resolved"]:
            unresolved_reasons.append(f"unmatched adjustment-factor jump on {jump['date']}")
    return {
        "window": {"start": monitor_on, "end": AS_OF},
        "factor_jump_threshold": "absolute relative change > 1e-5",
        "factor_jumps": jumps,
        "events_in_window": [
            {"date": day.date().isoformat(), "type": event_type}
            for day, event_type in sorted(event_dates)
        ],
        "share_count_adjustments": splits,
        "requires_share_count_replay": bool(splits),
        "corporate_action_unresolved": bool(unresolved_reasons),
        "unresolved_reasons": unresolved_reasons,
    }


def _frozen_item(
    stock: dict[str, Any],
    price_path: Path,
    event_path: Path,
    *,
    source_origin: str,
    requested_symbol: str,
) -> dict[str, Any]:
    frame = pd.read_csv(price_path)
    event_payload = read_json(event_path)
    action_audit = corporate_action_audit(
        frame,
        dict(event_payload.get("all_events") or {}),
        str(stock["monitor_on"]),
    )
    return {
        "code": str(stock["code"]),
        "name": str(stock["name"]),
        "symbol": requested_symbol,
        "market": str(stock.get("market") or ""),
        "monitor_on": str(stock["monitor_on"]),
        "price_path": str(price_path.resolve()),
        "event_path": str(event_path.resolve()),
        "bars": int(len(frame)),
        "first_bar": str(frame.iloc[0]["date"]),
        "last_bar": str(frame.iloc[-1]["date"]),
        "price_sha256": digest(price_path),
        "event_sha256": digest(event_path),
        "source_origin": source_origin,
        "corporate_action_audit": action_audit,
    }


def _existing_local(stock: dict[str, Any]) -> dict[str, Any] | None:
    code = str(stock["code"])
    price_path = PRICE_DIR / f"{code}.csv"
    event_path = EVENT_DIR / f"{code}.json"
    if not price_path.exists() or not event_path.exists():
        return None
    try:
        dates = pd.read_csv(price_path, usecols=["date"])
        event_payload = read_json(event_path)
    except Exception:
        return None
    if (
        dates.empty
        or str(dates.iloc[-1]["date"]) < AS_OF
        or int(event_payload.get("schema_version") or 0) < 2
        or "all_events" not in event_payload
    ):
        return None
    return _frozen_item(
        stock,
        price_path,
        event_path,
        source_origin="LOCAL_2023_CACHE",
        requested_symbol=str(stock["symbol"]),
    )


def _freeze_reusable_source(
    stock: dict[str, Any], reusable: dict[str, Any]
) -> dict[str, Any]:
    code = str(stock["code"])
    price_path = PRICE_DIR / f"{code}.csv"
    event_path = EVENT_DIR / f"{code}.json"
    frame = pd.read_csv(reusable["price_path"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[
        (frame["date"] >= pd.Timestamp(FETCH_FROM))
        & (frame["date"] <= pd.Timestamp(AS_OF))
    ].copy()
    if frame.empty or frame["date"].iloc[-1].date().isoformat() < AS_OF:
        raise ValueError(f"reusable source for {code} does not cover {AS_OF}")
    frame["date"] = frame["date"].dt.date.astype(str)
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_csv(price_path, index=False, encoding="utf-8-sig")
    payload, yahoo_symbol = _request_yahoo_for_stock(stock)
    _, dividends = payload_to_frame(payload)
    all_events = payload_events(payload)
    write_json(
        event_path,
        {
            "schema_version": 2,
            "dividends": dividends,
            "splits": all_events.get("splits", []),
            "all_events": all_events,
            "yahoo_symbol": yahoo_symbol,
            "retrieved_utc": datetime.now(timezone.utc).isoformat(),
            "price_source_origin": str(Path(reusable["price_path"]).resolve()),
            "prior_event_source_origin": str(Path(reusable["event_path"]).resolve()),
            "future_events_removed": True,
        },
    )
    return _frozen_item(
        stock,
        price_path,
        event_path,
        source_origin="REUSED_2026_SOURCE_TRUNCATED_TO_2023_ASOF",
        requested_symbol=yahoo_symbol,
    )


def _candidate_symbols(symbol: str) -> list[str]:
    values = [symbol]
    if symbol.endswith(".TW"):
        values.append(symbol[:-3] + ".TWO")
    elif symbol.endswith(".TWO"):
        values.append(symbol[:-4] + ".TW")
    return list(dict.fromkeys(values))


def _request_yahoo(symbol: str) -> dict[str, Any]:
    fetch_start = datetime.fromisoformat(FETCH_FROM).replace(tzinfo=timezone.utc)
    exclusive_end = (
        datetime.fromisoformat(AS_OF).replace(tzinfo=timezone.utc)
        + timedelta(days=1)
    )
    params = {
        "period1": int(fetch_start.timestamp()),
        # Yahoo period2 is exclusive.
        "period2": int(exclusive_end.timestamp()),
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=45, headers={"User-Agent": "Mozilla/5.0"}) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
            if not payload.get("chart", {}).get("result"):
                raise ValueError(str(payload.get("chart", {}).get("error") or "no chart result"))
            return payload
        except Exception as exc:  # pragma: no cover - network timing
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"Yahoo request failed for {symbol}: {last_error}")


def _request_yahoo_for_stock(stock: dict[str, Any]) -> tuple[dict[str, Any], str]:
    errors = []
    for symbol in _candidate_symbols(str(stock["symbol"])):
        try:
            payload = _request_yahoo(symbol)
            frame, _ = payload_to_frame(payload)
            frame["date"] = pd.to_datetime(frame["date"])
            if frame.empty or frame["date"].iloc[-1].date().isoformat() < AS_OF:
                raise ValueError(f"chart does not cover {AS_OF}")
            return payload, symbol
        except Exception as exc:  # pragma: no cover - network evidence
            errors.append(f"{symbol}: {exc}")
    raise RuntimeError("; ".join(errors))


def _fetch_yahoo_source(stock: dict[str, Any]) -> dict[str, Any]:
    code = str(stock["code"])
    payload, symbol = _request_yahoo_for_stock(stock)
    frame, dividends = payload_to_frame(payload)
    all_events = payload_events(payload)
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[
        (frame["date"] >= pd.Timestamp(FETCH_FROM))
        & (frame["date"] <= pd.Timestamp(AS_OF))
    ].copy()
    frame["date"] = frame["date"].astype(str)
    price_path = PRICE_DIR / f"{code}.csv"
    event_path = EVENT_DIR / f"{code}.json"
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_csv(price_path, index=False, encoding="utf-8-sig")
    write_json(
        event_path,
        {
            "schema_version": 2,
            "dividends": dividends,
            "splits": all_events.get("splits", []),
            "all_events": all_events,
            "retrieved_utc": datetime.now(timezone.utc).isoformat(),
            "yahoo_symbol": symbol,
            "requested_window": {"from": FETCH_FROM, "through": AS_OF},
        },
    )
    return _frozen_item(
        stock,
        price_path,
        event_path,
        source_origin="YAHOO_CHART_FETCH_2023",
        requested_symbol=symbol,
    )


def _prepare_one(stock: dict[str, Any], reusable: dict[str, Any] | None) -> dict[str, Any]:
    cached = _existing_local(stock)
    if cached is not None:
        return cached
    if reusable is not None:
        return _freeze_reusable_source(stock, reusable)
    return _fetch_yahoo_source(stock)


def prepare_sources(workers: int = 12) -> dict[str, Any]:
    catalog = read_json(CATALOG) if CATALOG.exists() else build_catalog()
    reusable_items: dict[str, dict[str, Any]] = {}
    if REUSABLE_2026_MANIFEST.exists():
        reusable_manifest = read_json(REUSABLE_2026_MANIFEST)
        reusable_items = {str(row["code"]): row for row in reusable_manifest.get("items", [])}
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_prepare_one, stock, reusable_items.get(str(stock["code"]))): stock
            for stock in catalog["monitor_stocks"]
        }
        for number, future in enumerate(as_completed(futures), 1):
            stock = futures[future]
            try:
                items.append(future.result())
            except Exception as exc:  # pragma: no cover - network evidence
                errors.append(
                    {"code": str(stock["code"]), "symbol": str(stock["symbol"]), "error": str(exc)}
                )
            if number % 25 == 0 or number == len(futures):
                print(f"sources {number}/{len(futures)} errors={len(errors)}", flush=True)
    items.sort(key=lambda row: (int(row["code"]) if str(row["code"]).isdigit() else math.inf, str(row["code"])))
    manifest = {
        "method_version": METHOD_VERSION,
        "judgement_boundary": "FROZEN_MARKET_DATA_ONLY（無分類、分數或交易核准）",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fetch_from": FETCH_FROM,
        "as_of": AS_OF,
        "monitor_floor": MONITOR_FLOOR,
        "catalog_path": str(CATALOG.resolve()),
        "catalog_sha256": digest(CATALOG),
        "source_events_path": str(SOURCE_EVENTS.resolve()),
        "source_events_sha256": digest(SOURCE_EVENTS),
        "reusable_2026_manifest": str(REUSABLE_2026_MANIFEST.resolve()),
        "requested_stock_count": int(catalog["monitor_stock_count"]),
        "prepared_stock_count": len(items),
        "reused_2026_count": sum(str(row["source_origin"]).startswith("REUSED_2026") for row in items),
        "yahoo_fetch_count": sum(row["source_origin"] == "YAHOO_CHART_FETCH_2023" for row in items),
        "local_cache_count": sum(row["source_origin"] == "LOCAL_2023_CACHE" for row in items),
        "factor_jump_audit": {
            "stocks_with_factor_jumps_in_monitor_window": sum(
                bool(row["corporate_action_audit"]["factor_jumps"]) for row in items
            ),
            "stocks_requiring_share_count_replay": sum(
                bool(row["corporate_action_audit"]["requires_share_count_replay"])
                for row in items
            ),
            "corporate_action_unresolved_count": sum(
                bool(row["corporate_action_audit"]["corporate_action_unresolved"])
                for row in items
            ),
            "unresolved_codes": [
                str(row["code"])
                for row in items
                if row["corporate_action_audit"]["corporate_action_unresolved"]
            ],
        },
        "errors": sorted(errors, key=lambda row: str(row["code"])),
        "items": items,
    }
    write_json(SOURCE_MANIFEST, manifest)
    return manifest


def _number(value: Any, digits: int = 4) -> float | None:
    if value is None or pd.isna(value) or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _daily_fact_with_raw(
    frame: pd.DataFrame, index: int, selections: dict[str, list[str]]
) -> dict[str, Any]:
    result = daily_fact_row(frame, index, selections)
    row = frame.iloc[index]
    result.update(
        {
            "raw_open": _number(row.get("raw_open")),
            "raw_high": _number(row.get("raw_high")),
            "raw_low": _number(row.get("raw_low")),
            "raw_close": _number(row.get("raw_close")),
            "adj_close": _number(row.get("adj_close")),
            "adjustment_factor": _number(
                float(row["adj_close"]) / float(row["raw_close"])
                if pd.notna(row.get("adj_close")) and float(row.get("raw_close") or 0)
                else None,
                8,
            ),
        }
    )
    return result


def _read_dividends(path: Path, as_of: str) -> list[dict[str, Any]]:
    return [
        {"date": str(row["date"]), "amount": float(row["amount"])}
        for row in read_json(path).get("dividends", [])
        if str(row.get("date") or "") <= as_of
    ]


def _read_all_events(path: Path, as_of: str) -> dict[str, list[dict[str, Any]]]:
    return {
        event_type: [row for row in rows if str(row.get("date") or "") <= as_of]
        for event_type, rows in read_json(path).get("all_events", {}).items()
    }


def _causal_frame(item: dict[str, Any], as_of: str) -> pd.DataFrame:
    frame = pd.read_csv(item["price_path"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[frame["date"] <= pd.Timestamp(as_of)].copy().reset_index(drop=True)
    if frame.empty:
        raise ValueError(f"{item['code']} has no bars through {as_of}")
    frame = add_indicators(frame)
    frame = add_causal_pivots(frame, 3, "small")
    frame = add_causal_pivots(frame, 10, "large")
    return frame


def build_packet(
    item: dict[str, Any],
    selections: dict[str, list[str]],
    stock_meta: dict[str, Any],
) -> dict[str, Any]:
    frame = _causal_frame(item, AS_OF)
    monitor_on = str(stock_meta["monitor_on"])
    pre_roll = int((frame["date"] < pd.Timestamp(monitor_on)).sum())
    monitored = frame[
        (frame["date"] >= pd.Timestamp(monitor_on))
        & (frame["date"] <= pd.Timestamp(AS_OF))
    ]
    daily = [_daily_fact_with_raw(frame, int(index), selections) for index in monitored.index]
    return {
        "packet_version": "formal-ai-historical-2023-causal-evidence-v1",
        "stock": {
            "code": str(item["code"]),
            "name": str(item["name"]),
            "symbol": str(item["symbol"]),
            "market": str(item.get("market") or ""),
        },
        "as_of": AS_OF,
        "first_selected_on": str(stock_meta["first_selected_date"]),
        "monitor_on": monitor_on,
        "watchlist_entry": {
            "date": monitor_on,
            "event": "WATCHING（納入監控）",
            "type": str(stock_meta["watchlist_entry_type"]),
            "left_censored": stock_meta["watchlist_entry_type"] == "LEFT_CENSORED_CARRY_IN",
            "review_policy": (
                f"AI must independently reject or retain from facts visible on {MONITOR_FLOOR}; "
                "earlier selections are provenance, not proof of continuous prior monitoring."
                if stock_meta["watchlist_entry_type"] == "LEFT_CENSORED_CARRY_IN"
                else "New upstream selection starts monitoring on its actual selection date."
            ),
        },
        "last_selected_on": str(stock_meta["last_selected_date"]),
        "selection_event_count": int(stock_meta["selection_event_count"]),
        "raw_selection_event_count": int(stock_meta["raw_selection_event_count"]),
        "selection_date_count": int(stock_meta["selection_date_count"]),
        "all_selection_sources": list(stock_meta["strategies"]),
        "selection_timeline": selections,
        "dividends_to_asof": _read_dividends(Path(item["event_path"]), AS_OF),
        "corporate_actions_to_asof": _read_all_events(Path(item["event_path"]), AS_OF),
        "corporate_action_audit": item["corporate_action_audit"],
        "data_quality": {
            "requested_fetch_from": FETCH_FROM,
            "first_bar": frame["date"].iloc[0].date().isoformat(),
            "last_bar": frame["date"].iloc[-1].date().isoformat(),
            "bars": int(len(frame)),
            "pre_monitor_bars": pre_roll,
            "preferred_750_met": pre_roll >= PREFERRED_PRE_MONITOR_BARS,
            "raw_and_adjusted_ohlc_present": all(
                column in frame.columns
                for column in ("raw_open", "raw_high", "raw_low", "raw_close", "open", "high", "low", "close")
            ),
            "price_sha256": str(item["price_sha256"]),
            "event_sha256": str(item["event_sha256"]),
            "source_origin": str(item["source_origin"]),
        },
        "macd_21_55_55_cycles": macd_cycles(frame, start=FETCH_FROM),
        "confirmed_pivots": confirmed_pivot_timeline(frame, start="2020-01-01"),
        "daily_visible_facts_from_monitoring": daily,
        "boundary": {
            "ai_must_decide": [
                "anchors", "scale", "taiji", "quadrants", "dow", "left_right",
                "stage", "scenario", "trigger", "stop", "invalidation",
            ],
            "packet_does_not_decide": True,
            "contains_classification_score_or_approval": False,
            "pivot_causality": "radius-3/radius-10 pivots appear only on confirmation date; source date is preserved",
            "latest_visible_date": AS_OF,
            "future_bars_loaded_into_indicators": False,
            "future_outcomes_included": False,
        },
    }


def prepare_packets() -> dict[str, Any]:
    catalog = read_json(CATALOG)
    source_manifest = read_json(SOURCE_MANIFEST)
    selections, stock_meta = selection_maps(catalog)
    items = {str(row["code"]): row for row in source_manifest["items"]}
    PACKET_DIR.mkdir(parents=True, exist_ok=True)
    manifest_items = []
    for number, code in enumerate(catalog["monitor_codes"], 1):
        if code not in items:
            continue
        packet = build_packet(items[code], selections[code], stock_meta[code])
        path = PACKET_DIR / f"{code}.json"
        write_json(path, packet)
        manifest_items.append(
            {
                "code": code,
                "name": packet["stock"]["name"],
                "packet": str(path.resolve()),
                "packet_sha256": digest(path),
                "first_selected_on": packet["first_selected_on"],
                "monitor_on": packet["monitor_on"],
                "pre_monitor_bars": packet["data_quality"]["pre_monitor_bars"],
                "preferred_750_met": packet["data_quality"]["preferred_750_met"],
                "monitored_sessions": len(packet["daily_visible_facts_from_monitoring"]),
            }
        )
        if number % 50 == 0 or number == len(catalog["monitor_codes"]):
            print(f"evidence packets {number}/{len(catalog['monitor_codes'])}", flush=True)
    packet_manifest = {
        **source_manifest,
        "method_version": METHOD_VERSION,
        "judgement_boundary": "FACTS_ONLY（只準備因果事實；無情境分類、分數或交易核准）",
        "rule_files": {
            str(path.relative_to(ROOT)): digest(path)
            for path in (V1_DOC, V2_DOC, V1_SCHEMA, V2_SCHEMA, V2_RULES)
        },
        "packet_stock_count": len(manifest_items),
        "preferred_750_count": sum(row["preferred_750_met"] for row in manifest_items),
        "items": manifest_items,
        "market_source_items": source_manifest["items"],
    }
    write_json(RUN / "packet_manifest.json", packet_manifest)
    return packet_manifest


def compact(code: str) -> None:
    packet = read_json(PACKET_DIR / f"{code}.json")
    print(json.dumps(packet, ensure_ascii=False, separators=(",", ":")))


def asof_view(code: str, as_of: str) -> dict[str, Any]:
    if as_of > AS_OF:
        raise ValueError(f"asof date {as_of} exceeds historical research cutoff {AS_OF}")
    catalog = read_json(CATALOG)
    source_manifest = read_json(SOURCE_MANIFEST)
    selections, stock_meta = selection_maps(catalog)
    item = next(row for row in source_manifest["items"] if str(row["code"]) == code)
    frame = _causal_frame(item, as_of)
    first_selected = str(stock_meta[code]["first_selected_date"])
    monitor_on = str(stock_meta[code]["monitor_on"])
    start = max(pd.Timestamp(as_of) - pd.Timedelta(days=180), frame["date"].iloc[0])
    recent = frame[frame["date"] >= start]
    visible_selections = {
        day: sources for day, sources in selections[code].items() if day <= as_of
    }
    result = {
        "stock": {"code": code, "name": item["name"], "symbol": item["symbol"]},
        "as_of": as_of,
        "first_selected_on": first_selected,
        "monitor_on": monitor_on,
        "watchlist_entry_type": str(stock_meta[code]["watchlist_entry_type"]),
        "selection_timeline_to_date": visible_selections,
        "dividends_to_date": _read_dividends(Path(item["event_path"]), as_of),
        "corporate_actions_to_date": _read_all_events(Path(item["event_path"]), as_of),
        "macd_cycles_to_date": macd_cycles(frame, start=FETCH_FROM)[-10:],
        "confirmed_large_pivots_to_date": [
            row for row in confirmed_pivot_timeline(frame, start="2020-01-01")
            if row["scale"] == "LARGE"
        ][-14:],
        "recent_daily_facts": [
            _daily_fact_with_raw(frame, int(index), visible_selections)
            for index in recent.index
        ][-60:],
        "boundary": {
            "facts_only": True,
            "contains_classification_score_or_approval": False,
            "latest_visible_date": as_of,
            "future_bars_loaded_into_view": False,
        },
    }
    return result


def write_facts_tsv() -> Path:
    """Write a compact facts-only triage view; it does not approve candidates."""
    manifest = read_json(RUN / "packet_manifest.json")
    destination = RUN / "ai_facts_compact.tsv"
    structural = {
        "CLOSE_BREAK_LAST_CONFIRMED_SMALL_PIVOT_HIGH": "BS",
        "CLOSE_BREAK_LAST_CONFIRMED_LARGE_PIVOT_HIGH": "BL",
        "CLOSE_ABOVE_PRIOR_20D_HIGH": "B20",
        "MACD_HIST_TURN_POSITIVE": "MP",
    }
    lines = []
    for index, item in enumerate(manifest["items"]):
        packet = read_json(Path(item["packet"]))
        cycles = packet["macd_21_55_55_cycles"][-6:]
        cycle_text = ";".join(
            f"{row['sign'][0]}:{row['start']}~{row['end'] or 'F'}:"
            f"L{row['low_date']}@{row['low']}:H{row['high_date']}@{row['high']}"
            for row in cycles
        )
        event_text = []
        for row in packet["daily_visible_facts_from_monitoring"]:
            codes = [structural[fact] for fact in row["facts"] if fact in structural]
            codes.extend(
                "R" + fact.removeprefix("RECLAIM_MA")
                for fact in row["facts"] if fact.startswith("RECLAIM_MA")
            )
            if not codes:
                continue
            event_text.append(
                f"{row['date']}@{row['close']}:S{row['small_pivot_low_date']}@{row['small_pivot_low']}:"
                f"L{row['large_pivot_low_date']}@{row['large_pivot_low']}:{'/'.join(codes)}"
            )
        lines.append(
            "\t".join(
                [
                    str(index), packet["stock"]["code"], packet["stock"]["name"],
                    packet["monitor_on"], str(packet["data_quality"]["pre_monitor_bars"]),
                    ",".join(packet["all_selection_sources"]), cycle_text, ";".join(event_text),
                ]
            )
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def prepare(workers: int) -> dict[str, Any]:
    catalog = build_catalog()
    source_manifest = prepare_sources(workers)
    if source_manifest["prepared_stock_count"] != catalog["monitor_stock_count"]:
        return {
            "catalog": catalog,
            "source_manifest": source_manifest,
            "packet_manifest": None,
        }
    packet_manifest = prepare_packets()
    write_facts_tsv()
    return {
        "catalog": catalog,
        "source_manifest": source_manifest,
        "packet_manifest": packet_manifest,
    }


def validate() -> dict[str, Any]:
    catalog = read_json(CATALOG)
    source_manifest = read_json(SOURCE_MANIFEST)
    packet_manifest = read_json(RUN / "packet_manifest.json")
    errors: list[str] = []
    warnings: list[str] = []
    catalog_codes = [str(code) for code in catalog.get("monitor_codes", [])]
    source_items = {str(row["code"]): row for row in source_manifest.get("items", [])}
    packet_items = {str(row["code"]): row for row in packet_manifest.get("items", [])}
    if len(catalog_codes) != EXPECTED_STOCKS or len(set(catalog_codes)) != EXPECTED_STOCKS:
        errors.append(f"catalog must contain {EXPECTED_STOCKS} unique codes")
    carry_in = [
        row for row in catalog.get("monitor_stocks", [])
        if row.get("watchlist_entry_type") == "LEFT_CENSORED_CARRY_IN"
    ]
    expected_carry_in = int(catalog.get("left_censored_carry_in_count") or 0)
    if len(carry_in) != expected_carry_in:
        errors.append(
            f"LEFT_CENSORED_CARRY_IN count is {len(carry_in)}, expected {expected_carry_in}"
        )
    for row in catalog.get("monitor_stocks", []):
        expected_monitor = max(MONITOR_FLOOR, str(row["first_selected_date"]))
        if str(row.get("monitor_on")) != expected_monitor:
            errors.append(f"{row['code']}: monitor_on violates max(floor, first_selected)")
    if set(source_items) != set(catalog_codes):
        errors.append("source manifest codes do not exactly match catalog")
    if set(packet_items) != set(catalog_codes):
        errors.append("packet manifest codes do not exactly match catalog")
    corrections_path_value = source_manifest.get("source_corrections_path")
    corrections_hash = source_manifest.get("source_corrections_sha256")
    if corrections_path_value or corrections_hash:
        corrections_path = Path(str(corrections_path_value or ""))
        if not corrections_path.exists():
            errors.append("source corrections file is missing")
        elif digest(corrections_path) != str(corrections_hash or ""):
            errors.append("source corrections hash mismatch")

    required_price_columns = {
        "date", "raw_open", "raw_high", "raw_low", "raw_close", "adj_close",
        "open", "high", "low", "close", "volume",
    }
    pre_monitor_invalid_ohlc_rows = 0
    pre_monitor_invalid_ohlc_stocks: set[str] = set()
    for code, item in source_items.items():
        price_path = Path(item["price_path"])
        event_path = Path(item["event_path"])
        if digest(price_path) != item["price_sha256"]:
            errors.append(f"{code}: price hash mismatch")
        if digest(event_path) != item["event_sha256"]:
            errors.append(f"{code}: event hash mismatch")
        frame = pd.read_csv(price_path)
        if not required_price_columns.issubset(frame.columns):
            errors.append(f"{code}: missing raw/adjusted OHLC columns")
        if frame.empty or str(frame.iloc[-1]["date"]) != AS_OF:
            errors.append(f"{code}: frozen price does not end exactly on {AS_OF}")
        if not frame.empty and str(frame.iloc[0]["date"]) < FETCH_FROM:
            errors.append(f"{code}: frozen price precedes requested source boundary")
        for prefix in ("raw_", ""):
            open_col, high_col, low_col, close_col = (
                f"{prefix}open", f"{prefix}high", f"{prefix}low", f"{prefix}close"
            )
            if {open_col, high_col, low_col, close_col}.issubset(frame.columns):
                numeric = frame[[open_col, high_col, low_col, close_col]].apply(
                    pd.to_numeric, errors="coerce"
                )
                tolerance = pd.concat(
                    [
                        pd.Series(0.01, index=frame.index),
                        numeric[close_col].abs().clip(lower=0.01) * 0.001,
                    ],
                    axis=1,
                ).max(axis=1)
                invalid = (
                    numeric[high_col]
                    < numeric[[open_col, close_col, low_col]].max(axis=1) - tolerance
                ) | (
                    numeric[low_col]
                    > numeric[[open_col, close_col, high_col]].min(axis=1) + tolerance
                )
                monitor_mask = frame["date"].astype(str).between(MONITOR_FLOOR, AS_OF)
                monitored_invalid = invalid & monitor_mask
                historical_invalid = invalid & ~monitor_mask
                if historical_invalid.any():
                    pre_monitor_invalid_ohlc_rows += int(historical_invalid.sum())
                    pre_monitor_invalid_ohlc_stocks.add(code)
                if monitored_invalid.any():
                    bad_dates = frame.loc[monitored_invalid, "date"].astype(str).head(10).tolist()
                    errors.append(
                        f"{code}: invalid {prefix or 'adjusted_'}OHLC ordering on {bad_dates}"
                    )
        event_payload = read_json(event_path)
        if int(event_payload.get("schema_version") or 0) < 2:
            errors.append(f"{code}: event schema lacks all-event support")
        if "all_events" not in event_payload or "splits" not in event_payload:
            errors.append(f"{code}: dividends/splits/all_events not all persisted")
        audit = item.get("corporate_action_audit") or {}
        if "corporate_action_unresolved" not in audit:
            errors.append(f"{code}: missing corporate-action resolution flag")
        elif audit["corporate_action_unresolved"]:
            warnings.append(f"{code}: corporate_action_unresolved")
        for split in audit.get("share_count_adjustments", []):
            if split.get("ratio") is None or float(split["ratio"]) <= 0:
                errors.append(f"{code}: unparseable share-count adjustment on {split.get('date')}")

    for code, item in packet_items.items():
        path = Path(item["packet"])
        if digest(path) != item["packet_sha256"]:
            errors.append(f"{code}: packet hash mismatch")
            continue
        packet = read_json(path)
        if packet.get("boundary", {}).get("contains_classification_score_or_approval") is not False:
            errors.append(f"{code}: packet boundary is not facts-only")
        if packet.get("as_of") != AS_OF:
            errors.append(f"{code}: packet as_of mismatch")
        if packet.get("watchlist_entry", {}).get("type") == "LEFT_CENSORED_CARRY_IN":
            if packet["watchlist_entry"].get("left_censored") is not True:
                errors.append(f"{code}: carry-in packet not marked left-censored")
        daily = packet.get("daily_visible_facts_from_monitoring") or []
        if daily and max(str(row["date"]) for row in daily) > AS_OF:
            errors.append(f"{code}: packet exposes a future daily fact")
        if daily and min(str(row["date"]) for row in daily) < str(packet["monitor_on"]):
            errors.append(f"{code}: packet daily facts precede monitoring")
    facts_path = RUN / "ai_facts_compact.tsv"
    facts_rows = len(facts_path.read_text(encoding="utf-8").splitlines()) if facts_path.exists() else 0
    if facts_rows != EXPECTED_STOCKS:
        errors.append(f"facts TSV has {facts_rows} rows, expected {EXPECTED_STOCKS}")
    result = {
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "ok": not errors,
        "catalog_stocks": len(catalog_codes),
        "left_censored_carry_in": len(carry_in),
        "source_items": len(source_items),
        "packet_items": len(packet_items),
        "facts_tsv_rows": facts_rows,
        "stocks_requiring_share_count_replay": sum(
            bool(row.get("corporate_action_audit", {}).get("requires_share_count_replay"))
            for row in source_items.values()
        ),
        "corporate_action_unresolved": len(warnings),
        "pre_monitor_material_ohlc_anomalies": {
            "rows_across_raw_and_adjusted": pre_monitor_invalid_ohlc_rows,
            "stocks": len(pre_monitor_invalid_ohlc_stocks),
            "policy": "disclosed, not auto-repaired; no final trigger/stop/declared pivot directly cites these bars",
        },
        "errors": errors,
        "warnings": warnings,
    }
    write_json(RUN / "validation.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    source = sub.add_parser("prepare-sources")
    source.add_argument("--workers", type=int, default=12)
    sub.add_parser("prepare-packets")
    full = sub.add_parser("prepare")
    full.add_argument("--workers", type=int, default=12)
    show = sub.add_parser("show")
    show.add_argument("code")
    asof = sub.add_parser("asof")
    asof.add_argument("code")
    asof.add_argument("date")
    sub.add_parser("facts-tsv")
    sub.add_parser("validate")
    args = parser.parse_args()
    if args.command == "catalog":
        catalog = build_catalog()
        print(f"catalog={CATALOG} stocks={catalog['monitor_stock_count']} events={catalog['event_count']}")
    elif args.command == "prepare-sources":
        manifest = prepare_sources(args.workers)
        print(
            f"manifest={SOURCE_MANIFEST} prepared={manifest['prepared_stock_count']} "
            f"errors={len(manifest['errors'])}"
        )
    elif args.command == "prepare-packets":
        manifest = prepare_packets()
        print(f"packets={manifest['packet_stock_count']} preferred750={manifest['preferred_750_count']}")
    elif args.command == "prepare":
        result = prepare(args.workers)
        sources = result["source_manifest"]
        packets = result["packet_manifest"]
        print(
            f"stocks={sources['prepared_stock_count']}/{sources['requested_stock_count']} "
            f"errors={len(sources['errors'])} packets={0 if packets is None else packets['packet_stock_count']}"
        )
    elif args.command == "show":
        compact(args.code)
    elif args.command == "asof":
        print(json.dumps(asof_view(args.code, args.date), ensure_ascii=False, separators=(",", ":")))
    elif args.command == "validate":
        result = validate()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
    else:
        print(write_facts_tsv())


if __name__ == "__main__":
    main()
