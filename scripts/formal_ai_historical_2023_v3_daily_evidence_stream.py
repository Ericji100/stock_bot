"""Build a lossless-for-V3, identity-blind daily evidence stream.

The source daily packets repeat long pivot/MACD histories on every trading day.
This module removes that transport duplication only.  It does not classify a V3
route, gate, state, or trade.  Every monitored stock-day remains in the stream.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan/ai_daily_review"
SOURCE_MANIFEST = SOURCE / "batch_manifest.json"
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan/ai_daily_review_v2"
BATCH_DIR = RUN / "batches"
MANIFEST = RUN / "batch_manifest.json"
LEAN_MINIMAL_START_ROUND = 5


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _pivot_tuple(row: dict[str, Any]) -> list[Any]:
    return [row["scale"][0], row["side"][0], row["source_date"], row["confirmation_date"], row["price"]]


def _cycle_tuple(row: dict[str, Any]) -> list[Any]:
    return [
        row["sign"][0], row["start"], row["end"], row["bars"],
        row["low_date"], row["low"], row["high_date"], row["high"],
    ]


def _latest_pivots(rows: list[dict[str, Any]], *, count: int) -> list[list[Any]]:
    selected: list[dict[str, Any]] = []
    for scale in ("LARGE", "SMALL"):
        for side in ("HIGH", "LOW"):
            candidates = [row for row in rows if row["scale"] == scale and row["side"] == side]
            selected.extend(candidates[-count:])
    selected.sort(key=lambda row: (str(row["confirmation_date"]), str(row["source_date"]), row["scale"], row["side"]))
    return [_pivot_tuple(row) for row in selected]


def _compress(row: dict[str, Any], *, lean_minimal: bool) -> list[Any]:
    market = row["market"]
    facts = set(market.get("facts") or [])
    pivots = row.get("confirmed_pivots_asof") or []
    large_lows = [item for item in pivots if item["scale"] == "LARGE" and item["side"] == "LOW"]
    latest_large_low = large_lows[-1] if large_lows else None
    close = float(market["close"])
    trigger_completed = "CLOSE_BREAK_LAST_CONFIRMED_SMALL_PIVOT_HIGH" in facts
    possible_macro_invalidation = bool(latest_large_low and close < float(latest_large_low["price"]))
    full_structure_card = trigger_completed or possible_macro_invalidation
    if lean_minimal and not full_structure_card:
        return [
            row["review_id"], row["anonymous_stock_id"], row["review_as_of"], row["stock_day_ordinal"],
            list((row.get("selection_asof") or {}).get("selected_today") or []),
            int((row.get("data_quality") or {}).get("pre_monitor_bars") or 0), 0, 0,
        ]
    stop = row.get("causal_stop_candidate")
    base: list[Any] = [
        row["review_id"],
        row["anonymous_stock_id"],
        row["review_as_of"],
        row["stock_day_ordinal"],
        list((row.get("selection_asof") or {}).get("selected_today") or []),
        int((row.get("data_quality") or {}).get("pre_monitor_bars") or 0),
        1 if trigger_completed else 0,
        1 if possible_macro_invalidation else 0,
        [
            market.get("open"), market.get("high"), market.get("low"), market.get("close"),
            market.get("return_1d_pct"), market.get("volume_ratio_20"), market.get("atr14"),
            market.get("macd_hist"), market.get("macd_hist_delta"),
        ],
        [
            (market.get("mas") or {}).get(key)
            for key in ("ma5", "ma13", "ma21", "ma55", "ma105", "ma144")
        ],
        [market.get("ma105_slope_20d_pct"), market.get("ma144_slope_20d_pct")],
        [
            (row.get("recent_context") or {}).get("return_5d_pct"),
            (row.get("recent_context") or {}).get("return_10d_pct"),
            (row.get("recent_context") or {}).get("return_20d_pct"),
            (row.get("recent_context") or {}).get("range_low"),
            (row.get("recent_context") or {}).get("range_high"),
        ],
        _latest_pivots(pivots, count=1),
        None if stop is None else [
            stop.get("source_date"), stop.get("confirmation_date"), stop.get("price"),
            stop.get("distance_pct"), stop.get("distance_atr"),
        ],
        1 if full_structure_card else 0,
    ]
    if full_structure_card:
        base.append([
            [bar.get("date"), bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close"), bar.get("macd_hist")]
            for bar in (row.get("recent_context") or {}).get("last_8_bars") or []
        ])
        base.append([_cycle_tuple(item) for item in (row.get("completed_macd_cycles_asof") or [])[-3:]])
        base.append(_latest_pivots(pivots, count=2))
    return base


def build() -> dict[str, Any]:
    source = _read(SOURCE_MANIFEST)
    outputs: list[dict[str, Any]] = []
    stock_days = 0
    full_cards = 0
    bytes_written = 0
    for item in source["files"]:
        source_path = Path(item["path"])
        if _sha(source_path) != str(item["sha256"]):
            raise ValueError(f"source batch changed: {source_path}")
        lean_minimal = int(item["round"]) >= LEAN_MINIMAL_START_ROUND
        rows = [_compress(row, lean_minimal=lean_minimal) for row in _jsonl(source_path)]
        full_cards += sum(len(row) > 14 and row[14] == 1 for row in rows)
        stock_days += len(rows)
        path = BATCH_DIR / f"shard_{int(item['shard'])}" / source_path.name
        _write_jsonl(path, rows)
        size = path.stat().st_size
        bytes_written += size
        outputs.append({
            "shard": int(item["shard"]), "round": int(item["round"]), "date": str(item["date"]),
            "rows": len(rows), "full_cards": sum(len(row) > 14 and row[14] == 1 for row in rows),
            "bytes": size, "path": str(path.resolve()), "sha256": _sha(path),
        })
    manifest = {
        "method_version": "formal-codex-ai-v3-every-day-evidence-stream-v2",
        "boundary": "IDENTITY_BLIND_CAUSAL_FACT_COMPRESSION_ONLY_NO_ROUTE_GATE_STATE_OR_TRADE_DECISION",
        "source_manifest": {"path": str(SOURCE_MANIFEST.resolve()), "sha256": _sha(SOURCE_MANIFEST)},
        "stocks": int(source["stocks"]), "stock_days": stock_days, "rounds": int(source["rounds"]),
        "shards": int(source["shards"]), "outcome_excluded_codes": [],
        "lean_minimal_start_round": LEAN_MINIMAL_START_ROUND,
        "full_structure_cards": full_cards, "bytes": bytes_written, "files": outputs,
        "legend": {
            "row": ["review_id", "anonymous_stock_id", "review_as_of", "stock_day_ordinal", "selected_today", "pre_monitor_bars", "trigger_completed_bit", "macro_break_alert_bit", "market", "moving_averages", "long_slopes", "recent_range", "latest_pivots", "causal_stop", "full_card_bit", "last_8_bars_if_full", "last_3_completed_macd_cycles_if_full", "recent_pivots_if_full"],
            "m": ["open", "high", "low", "close", "return_1d_pct", "volume_ratio_20", "atr14", "macd_hist", "macd_hist_delta"],
            "ma": ["ma5", "ma13", "ma21", "ma55", "ma105", "ma144"],
            "long_slope": ["ma105_slope_20d_pct", "ma144_slope_20d_pct"],
            "range": ["return_5d_pct", "return_10d_pct", "return_20d_pct", "recent_low", "recent_high"],
            "pivot": ["scale_initial", "side_initial", "source_date", "confirmation_date", "price"],
            "cycle": ["sign_initial", "start", "end", "bars", "low_date", "low", "high_date", "high"],
            "stop": ["source_date", "confirmation_date", "price", "distance_pct", "distance_atr"],
        },
    }
    _write_json(MANIFEST, manifest)
    return manifest


if __name__ == "__main__":
    payload = build()
    print(json.dumps({key: payload[key] for key in ("stocks", "stock_days", "rounds", "shards", "full_structure_cards", "bytes", "outcome_excluded_codes")}, ensure_ascii=False, indent=2))
