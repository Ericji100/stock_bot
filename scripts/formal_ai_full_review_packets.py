"""Prepare causal evidence packets for the 747-stock formal AI replay.

This module deliberately does not classify scenarios, score candidates, or
approve trades.  It only computes dated market facts that a human/LLM reviewer
can inspect under judgement specifications V1 and V2.  Pivots are exposed only
from their causal confirmation date onward.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-09-04"
CATALOG = ROOT / "reports/course_backtest/2026-09-05/tg_selection_catalog_v1/catalog.json"
SOURCE_RUN = ROOT / "reports/course_backtest/2026-09-06/tg_enlightenment_ai_v2"
SOURCE_MANIFEST = SOURCE_RUN / "input_manifest.json"
RUN = ROOT / "reports/course_backtest/2026-09-06/tg_formal_ai_v1_v2"
PACKET_DIR = RUN / "review_packets"
V1_DOC = ROOT / "docs/enlightenment-ai-judgement-v1.md"
V2_DOC = ROOT / "docs/enlightenment-ai-judgement-v2.md"
V1_SCHEMA = ROOT / "config/enlightenment_ai_judgement_v1.schema.json"
V2_SCHEMA = ROOT / "config/enlightenment_ai_judgement_v2.schema.json"
V2_RULES = ROOT / "config/enlightenment_ai_rules_v2.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"])
    data = data.sort_values("date").reset_index(drop=True)
    for window in (5, 13, 21, 55, 105, 144):
        data[f"ma{window}"] = data["close"].rolling(window).mean()
    previous_close = data["close"].shift(1)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    data["atr14"] = true_range.rolling(14).mean()
    fast = data["close"].ewm(span=21, adjust=False).mean()
    slow = data["close"].ewm(span=55, adjust=False).mean()
    data["macd"] = fast - slow
    data["macd_signal"] = data["macd"].ewm(span=55, adjust=False).mean()
    data["macd_hist"] = data["macd"] - data["macd_signal"]
    data["volume_ma20"] = data["volume"].rolling(20).mean()
    data["return_1d_pct"] = data["close"].pct_change() * 100.0
    data["high_20_prior"] = data["high"].shift(1).rolling(20).max()
    data["low_20_prior"] = data["low"].shift(1).rolling(20).min()
    return data


def add_causal_pivots(frame: pd.DataFrame, radius: int, prefix: str) -> pd.DataFrame:
    """Add last confirmed pivot facts without exposing a future-confirmed pivot early."""
    data = frame.copy()
    highs = data["high"].astype(float)
    lows = data["low"].astype(float)
    size = radius * 2 + 1
    centered_high = highs.shift(radius)
    centered_low = lows.shift(radius)
    confirmed_high = centered_high.where(centered_high.eq(highs.rolling(size).max()))
    confirmed_low = centered_low.where(centered_low.eq(lows.rolling(size).min()))
    source_dates = data["date"].shift(radius)

    high_value = confirmed_high.ffill()
    low_value = confirmed_low.ffill()
    high_date = source_dates.where(confirmed_high.notna()).ffill()
    low_date = source_dates.where(confirmed_low.notna()).ffill()
    data[f"{prefix}_pivot_high"] = high_value
    data[f"{prefix}_pivot_high_date"] = high_date
    data[f"{prefix}_pivot_low"] = low_value
    data[f"{prefix}_pivot_low_date"] = low_date
    data[f"{prefix}_new_high_confirmed"] = confirmed_high.notna()
    data[f"{prefix}_new_low_confirmed"] = confirmed_low.notna()
    return data


def selection_maps(catalog: dict[str, Any]) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, Any]]]:
    by_code: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    meta = {str(row["code"]): row for row in catalog["monitor_stocks"]}
    for event in catalog["events"]:
        if not event.get("long_eligible"):
            continue
        code = str(event["code"])
        day = str(event["date"])
        by_code[code][day].append(str(event["strategy"]))
    result = {
        code: {day: sorted(set(values)) for day, values in sorted(days.items())}
        for code, days in by_code.items()
    }
    return result, meta


def macd_cycles(frame: pd.DataFrame, start: str = "2022-01-01") -> list[dict[str, Any]]:
    data = frame[frame["date"] >= pd.Timestamp(start)].copy()
    if data.empty:
        return []
    sign = data["macd_hist"].fillna(0.0).ge(0.0)
    group = sign.ne(sign.shift()).cumsum()
    rows: list[dict[str, Any]] = []
    groups = list(data.groupby(group, sort=True))
    for number, (_, part) in enumerate(groups):
        positive = bool(part["macd_hist"].iloc[-1] >= 0)
        low_index = part["low"].astype(float).idxmin()
        high_index = part["high"].astype(float).idxmax()
        completed = number < len(groups) - 1
        rows.append(
            {
                "sign": "POSITIVE" if positive else "NEGATIVE",
                "start": part["date"].iloc[0].date().isoformat(),
                "end": part["date"].iloc[-1].date().isoformat() if completed else None,
                "status": "CONFIRMED" if completed else "FORMING",
                "bars": int(len(part)),
                "low_date": frame.loc[low_index, "date"].date().isoformat(),
                "low": round(float(frame.loc[low_index, "low"]), 4),
                "high_date": frame.loc[high_index, "date"].date().isoformat(),
                "high": round(float(frame.loc[high_index, "high"]), 4),
                "start_close": round(float(part["close"].iloc[0]), 4),
                "last_close": round(float(part["close"].iloc[-1]), 4),
            }
        )
    return rows


def _date(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date().isoformat()


def _number(value: Any, digits: int = 4) -> float | None:
    if value is None or pd.isna(value) or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _crossed_above(row: pd.Series, previous: pd.Series, column: str) -> bool:
    return bool(
        pd.notna(row[column])
        and pd.notna(previous[column])
        and float(row["close"]) > float(row[column])
        and float(previous["close"]) <= float(previous[column])
    )


def daily_fact_row(frame: pd.DataFrame, index: int, selections: dict[str, list[str]]) -> dict[str, Any]:
    row = frame.iloc[index]
    previous = frame.iloc[index - 1] if index else row
    day = row["date"].date().isoformat()
    facts: list[str] = []
    for window in (5, 13, 21, 55, 105, 144):
        if _crossed_above(row, previous, f"ma{window}"):
            facts.append(f"RECLAIM_MA{window}")
    if pd.notna(row["high_20_prior"]) and float(row["close"]) > float(row["high_20_prior"]):
        facts.append("CLOSE_ABOVE_PRIOR_20D_HIGH")
    if (
        pd.notna(row["small_pivot_high"])
        and float(row["close"]) > float(row["small_pivot_high"])
        and float(previous["close"]) <= float(previous["small_pivot_high"])
    ):
        facts.append("CLOSE_BREAK_LAST_CONFIRMED_SMALL_PIVOT_HIGH")
    if (
        pd.notna(row["large_pivot_high"])
        and float(row["close"]) > float(row["large_pivot_high"])
        and float(previous["close"]) <= float(previous["large_pivot_high"])
    ):
        facts.append("CLOSE_BREAK_LAST_CONFIRMED_LARGE_PIVOT_HIGH")
    if float(row["macd_hist"]) >= 0 > float(previous["macd_hist"]):
        facts.append("MACD_HIST_TURN_POSITIVE")
    elif float(row["macd_hist"]) < 0 <= float(previous["macd_hist"]):
        facts.append("MACD_HIST_TURN_NEGATIVE")
    if bool(row["small_new_high_confirmed"]):
        facts.append("SMALL_PIVOT_HIGH_CONFIRMED_TODAY")
    if bool(row["small_new_low_confirmed"]):
        facts.append("SMALL_PIVOT_LOW_CONFIRMED_TODAY")
    if bool(row["large_new_high_confirmed"]):
        facts.append("LARGE_PIVOT_HIGH_CONFIRMED_TODAY")
    if bool(row["large_new_low_confirmed"]):
        facts.append("LARGE_PIVOT_LOW_CONFIRMED_TODAY")
    if day in selections:
        facts.append("UPSTREAM_SELECTED_TODAY")
    return {
        "date": day,
        "open": _number(row["open"]),
        "high": _number(row["high"]),
        "low": _number(row["low"]),
        "close": _number(row["close"]),
        "return_1d_pct": _number(row["return_1d_pct"], 3),
        "volume_ratio_20": _number(float(row["volume"]) / float(row["volume_ma20"]) if pd.notna(row["volume_ma20"]) and float(row["volume_ma20"]) else None, 3),
        "atr14": _number(row["atr14"]),
        "ma5": _number(row["ma5"]),
        "ma13": _number(row["ma13"]),
        "ma21": _number(row["ma21"]),
        "ma55": _number(row["ma55"]),
        "ma105": _number(row["ma105"]),
        "ma144": _number(row["ma144"]),
        "macd_hist": _number(row["macd_hist"], 5),
        "macd_hist_delta": _number(float(row["macd_hist"]) - float(previous["macd_hist"]), 5),
        "small_pivot_high": _number(row["small_pivot_high"]),
        "small_pivot_high_date": _date(row["small_pivot_high_date"]),
        "small_pivot_low": _number(row["small_pivot_low"]),
        "small_pivot_low_date": _date(row["small_pivot_low_date"]),
        "large_pivot_high": _number(row["large_pivot_high"]),
        "large_pivot_high_date": _date(row["large_pivot_high_date"]),
        "large_pivot_low": _number(row["large_pivot_low"]),
        "large_pivot_low_date": _date(row["large_pivot_low_date"]),
        "selection_sources": selections.get(day, []),
        "facts": facts,
    }


def confirmed_pivot_timeline(frame: pd.DataFrame, start: str = "2023-01-01") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _, row in frame[frame["date"] >= pd.Timestamp(start)].iterrows():
        day = row["date"].date().isoformat()
        for scale, prefix in (("SMALL", "small"), ("LARGE", "large")):
            for side in ("high", "low"):
                if bool(row[f"{prefix}_new_{side}_confirmed"]):
                    rows.append(
                        {
                            "confirmation_date": day,
                            "source_date": _date(row[f"{prefix}_pivot_{side}_date"]),
                            "scale": scale,
                            "side": side.upper(),
                            "price": _number(row[f"{prefix}_pivot_{side}"]),
                        }
                    )
    return rows


def build_packet(item: dict[str, Any], selections: dict[str, list[str]], stock_meta: dict[str, Any]) -> dict[str, Any]:
    frame = pd.read_csv(item["price_path"])
    frame = add_indicators(frame)
    frame = add_causal_pivots(frame, 3, "small")
    frame = add_causal_pivots(frame, 10, "large")
    first_selected = str(stock_meta["first_selected_date"])
    pre_roll = int((frame["date"] < pd.Timestamp(first_selected)).sum())
    monitored = frame[(frame["date"] >= pd.Timestamp(first_selected)) & (frame["date"] <= pd.Timestamp(AS_OF))]
    daily = [daily_fact_row(frame, int(index), selections) for index in monitored.index]
    return {
        "packet_version": "formal-ai-causal-evidence-v1",
        "stock": {
            "code": str(item["code"]),
            "name": str(item["name"]),
            "symbol": str(item["symbol"]),
        },
        "as_of": AS_OF,
        "first_selected_on": first_selected,
        "last_selected_on": str(stock_meta["last_selected_date"]),
        "selection_event_count": int(stock_meta["selection_event_count"]),
        "selection_date_count": int(stock_meta["selection_date_count"]),
        "all_selection_sources": list(stock_meta["strategies"]),
        "selection_timeline": selections,
        "data_quality": {
            "first_bar": frame["date"].iloc[0].date().isoformat(),
            "last_bar": frame["date"].iloc[-1].date().isoformat(),
            "bars": int(len(frame)),
            "pre_monitor_bars": pre_roll,
            "preferred_750_met": pre_roll >= 750,
            "price_sha256": str(item["price_sha256"]),
            "event_sha256": str(item["event_sha256"]),
        },
        "macd_21_55_55_cycles": macd_cycles(frame),
        "confirmed_pivots": confirmed_pivot_timeline(frame),
        "daily_visible_facts_from_monitoring": daily,
        "boundary": {
            "ai_must_decide": ["anchors", "scale", "taiji", "quadrants", "dow", "left_right", "stage", "scenario", "trigger", "invalidation"],
            "packet_does_not_decide": True,
            "pivot_causality": "radius-3/radius-10 pivots appear only on confirmation date; source date is preserved",
            "future_outcomes_included": False,
        },
    }


def prepare() -> dict[str, Any]:
    catalog = read_json(CATALOG)
    source_manifest = read_json(SOURCE_MANIFEST)
    selections, stock_meta = selection_maps(catalog)
    items = {str(row["code"]): row for row in source_manifest["items"]}
    PACKET_DIR.mkdir(parents=True, exist_ok=True)
    manifest_items = []
    for number, code in enumerate(catalog["monitor_codes"], 1):
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
                "pre_monitor_bars": packet["data_quality"]["pre_monitor_bars"],
                "preferred_750_met": packet["data_quality"]["preferred_750_met"],
                "monitored_sessions": len(packet["daily_visible_facts_from_monitoring"]),
            }
        )
        if number % 50 == 0 or number == len(catalog["monitor_codes"]):
            print(f"evidence packets {number}/{len(catalog['monitor_codes'])}", flush=True)
    manifest = {
        "method_version": "formal-ai-full-review-packets-v1",
        "judgement_boundary": "FACTS_ONLY（只準備因果事實；無情境分類、分數或交易核准）",
        "as_of": AS_OF,
        "catalog_path": str(CATALOG.resolve()),
        "catalog_sha256": digest(CATALOG),
        "source_manifest_path": str(SOURCE_MANIFEST.resolve()),
        "source_manifest_sha256": digest(SOURCE_MANIFEST),
        "rule_files": {
            str(path.relative_to(ROOT)): digest(path)
            for path in (V1_DOC, V2_DOC, V1_SCHEMA, V2_SCHEMA, V2_RULES)
        },
        "stock_count": len(manifest_items),
        "preferred_750_count": sum(row["preferred_750_met"] for row in manifest_items),
        "items": manifest_items,
    }
    write_json(RUN / "input_manifest.json", manifest)
    return manifest


def compact(code: str) -> None:
    packet = read_json(PACKET_DIR / f"{code}.json")
    print(json.dumps({key: packet[key] for key in (
        "stock", "as_of", "first_selected_on", "last_selected_on",
        "selection_event_count", "selection_date_count", "all_selection_sources",
        "data_quality", "macd_21_55_55_cycles", "confirmed_pivots",
        "daily_visible_facts_from_monitoring", "boundary",
    )}, ensure_ascii=False, separators=(",", ":")))


def asof_view(code: str, as_of: str) -> None:
    """Print a facts-only view rebuilt with bars no later than ``as_of``."""
    catalog = read_json(CATALOG)
    source_manifest = read_json(SOURCE_MANIFEST)
    selections, stock_meta = selection_maps(catalog)
    item = next(row for row in source_manifest["items"] if str(row["code"]) == code)
    frame = pd.read_csv(item["price_path"])
    frame = add_indicators(frame)
    frame = frame[frame["date"] <= pd.Timestamp(as_of)].copy().reset_index(drop=True)
    frame = add_causal_pivots(frame, 3, "small")
    frame = add_causal_pivots(frame, 10, "large")
    first_selected = str(stock_meta[code]["first_selected_date"])
    start = max(pd.Timestamp(first_selected) - pd.Timedelta(days=120), frame["date"].iloc[0])
    recent = frame[frame["date"] >= start]
    daily = [daily_fact_row(frame, int(index), selections[code]) for index in recent.index]
    result = {
        "stock": {"code": code, "name": item["name"]},
        "as_of": as_of,
        "first_selected_on": first_selected,
        "selection_timeline_to_date": {
            day: sources for day, sources in selections[code].items() if day <= as_of
        },
        "macd_cycles_to_date": macd_cycles(frame)[-8:],
        "confirmed_large_pivots_to_date": [
            row for row in confirmed_pivot_timeline(frame) if row["scale"] == "LARGE"
        ][-12:],
        "recent_daily_facts": daily[-45:],
        "boundary": {
            "facts_only": True,
            "latest_visible_date": as_of,
            "future_bars_loaded_into_view": False,
        },
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def screening_view(packet: dict[str, Any]) -> dict[str, Any]:
    structural_facts = {
        "CLOSE_BREAK_LAST_CONFIRMED_SMALL_PIVOT_HIGH",
        "CLOSE_BREAK_LAST_CONFIRMED_LARGE_PIVOT_HIGH",
        "MACD_HIST_TURN_POSITIVE",
        "CLOSE_ABOVE_PRIOR_20D_HIGH",
    }
    candidate_rows = []
    for row in packet["daily_visible_facts_from_monitoring"]:
        if not structural_facts.intersection(row["facts"]):
            continue
        candidate_rows.append(
            {
                "d": row["date"],
                "c": row["close"],
                "r": row["return_1d_pct"],
                "vr": row["volume_ratio_20"],
                "atr": row["atr14"],
                "mh": row["macd_hist"],
                "mhd": row["macd_hist_delta"],
                "ma": [row["ma21"], row["ma55"], row["ma105"], row["ma144"]],
                "spl": [row["small_pivot_low_date"], row["small_pivot_low"]],
                "lpl": [row["large_pivot_low_date"], row["large_pivot_low"]],
                "sph": [row["small_pivot_high_date"], row["small_pivot_high"]],
                "facts": [fact for fact in row["facts"] if fact != "UPSTREAM_SELECTED_TODAY"],
            }
        )
    last = packet["daily_visible_facts_from_monitoring"][-1]
    large_pivots = [row for row in packet["confirmed_pivots"] if row["scale"] == "LARGE"][-12:]
    return {
        "code": packet["stock"]["code"],
        "name": packet["stock"]["name"],
        "first": packet["first_selected_on"],
        "pre": packet["data_quality"]["pre_monitor_bars"],
        "sources": packet["all_selection_sources"],
        "cycles": packet["macd_21_55_55_cycles"][-10:],
        "large_pivots": large_pivots,
        "candidates": candidate_rows,
        "last": {"d": last["date"], "c": last["close"], "ma": [last["ma21"], last["ma55"], last["ma105"], last["ma144"]], "mh": last["macd_hist"]},
    }


def write_screening() -> Path:
    manifest = read_json(RUN / "input_manifest.json")
    destination = RUN / "ai_screening_input.jsonl"
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        for item in manifest["items"]:
            packet = read_json(Path(item["packet"]))
            stream.write(json.dumps(screening_view(packet), ensure_ascii=False, separators=(",", ":")) + "\n")
    return destination


def _mini_cycle(row: dict[str, Any]) -> list[Any]:
    return [
        row["sign"][0], row["start"], row["end"] or "F",
        row["low_date"], row["low"], row["high_date"], row["high"], row["bars"],
    ]


def _mini_event(row: dict[str, Any]) -> list[Any]:
    fact_codes = []
    mapping = {
        "CLOSE_BREAK_LAST_CONFIRMED_SMALL_PIVOT_HIGH": "BS",
        "CLOSE_BREAK_LAST_CONFIRMED_LARGE_PIVOT_HIGH": "BL",
        "CLOSE_ABOVE_PRIOR_20D_HIGH": "B20",
        "MACD_HIST_TURN_POSITIVE": "MP",
    }
    for fact in row["facts"]:
        if fact in mapping:
            fact_codes.append(mapping[fact])
        elif fact.startswith("RECLAIM_MA"):
            fact_codes.append("R" + fact.removeprefix("RECLAIM_MA"))
    close = float(row["c"])
    above = "".join(
        key for key, value in zip(("2", "5", "A", "B"), row["ma"])
        if value is not None and close > float(value)
    ) or "-"
    risk = None
    if row["spl"][1] is not None and close:
        risk = round((close - float(row["spl"][1])) / close * 100.0, 1)
    return [row["d"], row["c"], row["r"], row["vr"], above, row["spl"][0], row["spl"][1], risk, row["lpl"][0], row["lpl"][1], fact_codes]


def write_mini_screening() -> Path:
    manifest = read_json(RUN / "input_manifest.json")
    destination = RUN / "ai_screening_mini.jsonl"
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        for item in manifest["items"]:
            packet = read_json(Path(item["packet"]))
            view = screening_view(packet)
            mini = {
                "s": [view["code"], view["name"], view["first"], view["pre"]],
                "cy": [_mini_cycle(row) for row in view["cycles"][-6:]],
                "ev": [_mini_event(row) for row in view["candidates"]],
                "z": [view["last"]["c"], view["last"]["ma"], view["last"]["mh"]],
            }
            stream.write(json.dumps(mini, ensure_ascii=False, separators=(",", ":")) + "\n")
    return destination


def write_tsv_screening() -> Path:
    manifest = read_json(RUN / "input_manifest.json")
    destination = RUN / "ai_screening_compact.tsv"
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        for item in manifest["items"]:
            packet = read_json(Path(item["packet"]))
            view = screening_view(packet)
            cycles = []
            for row in view["cycles"][-5:]:
                cycles.append(
                    f"{row['sign'][0]}:{row['start']}~{row['end'] or 'F'}:"
                    f"L{row['low_date']}@{row['low']}:H{row['high_date']}@{row['high']}"
                )
            events = []
            for row in view["candidates"]:
                mini = _mini_event(row)
                facts = "/".join(mini[-1])
                events.append(
                    f"{mini[0]}@{mini[1]}:A{mini[4]}:S{mini[5]}@{mini[6]}({mini[7]}%):"
                    f"L{mini[8]}@{mini[9]}:{facts}"
                )
            last_ma = "/".join("-" if value is None else str(value) for value in view["last"]["ma"])
            stream.write(
                "\t".join(
                    [
                        view["code"], view["name"], view["first"], str(view["pre"]),
                        ";".join(cycles), ";".join(events),
                        f"Z{view['last']['c']}:{last_ma}:{view['last']['mh']}",
                    ]
                ) + "\n"
            )
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    sub.add_parser("screening")
    sub.add_parser("screening-mini")
    sub.add_parser("screening-tsv")
    show = sub.add_parser("show")
    show.add_argument("code")
    asof = sub.add_parser("asof")
    asof.add_argument("code")
    asof.add_argument("date")
    args = parser.parse_args()
    if args.command == "prepare":
        manifest = prepare()
        print(
            f"prepared {manifest['stock_count']} packets; "
            f"preferred750={manifest['preferred_750_count']}"
        )
    elif args.command == "screening":
        print(write_screening())
    elif args.command == "screening-mini":
        print(write_mini_screening())
    elif args.command == "screening-tsv":
        print(write_tsv_screening())
    elif args.command == "show":
        compact(args.code)
    else:
        asof_view(args.code, args.date)


if __name__ == "__main__":
    main()
