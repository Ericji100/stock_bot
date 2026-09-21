"""Build compact, facts-only as-of summaries for AI decision review.

This helper deliberately makes no scenario, trigger, or approval decision.  It
recomputes indicators and confirmed pivots after cutting every price series at
the requested signal date, then emits only the evidence visible at that date.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.formal_ai_full_review_packets import (
    AS_OF,
    CATALOG,
    RUN,
    SOURCE_MANIFEST,
    add_causal_pivots,
    add_indicators,
    confirmed_pivot_timeline,
    daily_fact_row,
    macd_cycles,
    read_json,
    selection_maps,
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _last_completed_positive(cycles: list[dict[str, Any]]) -> dict[str, Any] | None:
    for cycle in reversed(cycles):
        if cycle["sign"] == "POSITIVE" and cycle["status"] == "CONFIRMED":
            return cycle
    return None


def _summarize(code: str, day: str, item: dict[str, Any], selections: dict[str, list[str]]) -> dict[str, Any]:
    frame = pd.read_csv(item["price_path"])
    frame = add_indicators(frame)
    frame = frame[frame["date"] <= pd.Timestamp(day)].copy().reset_index(drop=True)
    frame = add_causal_pivots(frame, 3, "small")
    frame = add_causal_pivots(frame, 10, "large")
    cycles = macd_cycles(frame)
    pivots = confirmed_pivot_timeline(frame)
    daily = [daily_fact_row(frame, int(index), selections) for index in frame.index[-12:]]
    current = daily[-1]
    return {
        "code": code,
        "name": item["name"],
        "as_of": day,
        "close": current["close"],
        "ohlc": [current["open"], current["high"], current["low"], current["close"]],
        "atr14": current["atr14"],
        "ma": {key: current[key] for key in ("ma5", "ma13", "ma21", "ma55", "ma105", "ma144")},
        "macd_hist": current["macd_hist"],
        "signal_facts": current["facts"],
        "signal_sources": current["selection_sources"],
        "small_stop_candidate": [current["small_pivot_low_date"], current["small_pivot_low"]],
        "small_break_candidate": [current["small_pivot_high_date"], current["small_pivot_high"]],
        "large_stop_candidate": [current["large_pivot_low_date"], current["large_pivot_low"]],
        "large_break_candidate": [current["large_pivot_high_date"], current["large_pivot_high"]],
        "last_completed_positive_cycle": _last_completed_positive(cycles),
        "recent_cycles": cycles[-4:],
        "recent_confirmed_large_pivots": [row for row in pivots if row["scale"] == "LARGE"][-6:],
        "recent_daily": [
            {
                "date": row["date"],
                "close": row["close"],
                "low": row["low"],
                "high": row["high"],
                "macd_hist": row["macd_hist"],
                "facts": row["facts"],
            }
            for row in daily
        ],
        "boundary": {"latest_visible_date": day, "future_bars_loaded_into_view": False, "facts_only": True},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screening", type=Path, default=RUN / "ai_screening_pass_v1_v2.jsonl")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=79)
    parser.add_argument("--output", type=Path, default=RUN / "root_000_079_asof_evidence.jsonl")
    args = parser.parse_args()

    catalog = read_json(CATALOG)
    source = read_json(SOURCE_MANIFEST)
    selections_by_code, _ = selection_maps(catalog)
    items = {str(row["code"]): row for row in source["items"]}
    screening = _load_jsonl(args.screening)
    rows: list[dict[str, Any]] = []
    for index in range(args.start, args.end + 1):
        screen = screening[index]
        code = str(screen["code"])
        for version in ("v1", "v2"):
            for day in screen[version].get("dates", []):
                if day > AS_OF:
                    raise ValueError(f"future review date: {code} {version} {day}")
                rows.append({"index": index, "version": version, **_summarize(code, day, items[code], selections_by_code[code])})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} causal as-of evidence rows to {args.output}")


if __name__ == "__main__":
    main()
