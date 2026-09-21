"""Build the complete causal V3 AI review queue for the 2023-H2 cohort.

The builder is deliberately decision-free.  It may reject a checkpoint only
when an objective V3 common guard is impossible (no completed price trigger,
inactive watchlist, no confirmed stop) or when it repeats the exact same
structural episode.  It never assigns a V2/V3 route or approves a trade.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2"
SOURCE = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2_v3"
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_full_review"
INDEX_PATH = SOURCE / "v3_review_index.jsonl"
PARENT_LEDGER = PARENT / "ai_decisions_merged.jsonl"
QUEUE_PATH = RUN / "ai_review_queue.jsonl"
OBJECTIVE_REJECTIONS_PATH = RUN / "objective_checkpoint_rejections.jsonl"
MANIFEST_PATH = RUN / "queue_manifest.json"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _watch_active(events: list[dict[str, Any]], day: str) -> bool:
    active = False
    for event in sorted(events, key=lambda value: str(value.get("date") or "")):
        if str(event.get("date") or "") > day:
            break
        name = str(event.get("event") or "")
        if name.startswith("WATCHING") or name.startswith("RESELECTED"):
            active = True
        elif name.startswith("CAMPAIGN_INVALIDATED") or name.startswith("REMOVED_FROM_WATCHLIST"):
            active = False
    return active


def _causal_stop(packet: dict[str, Any]) -> dict[str, Any] | None:
    day = packet["candidate_day"]
    stop_date = day.get("small_pivot_low_date")
    stop_price = day.get("small_pivot_low")
    if not stop_date or stop_price is None:
        return None
    return next((
        pivot for pivot in packet["confirmed_pivots_asof"]
        if pivot["scale"] == "SMALL"
        and pivot["side"] == "LOW"
        and pivot["source_date"] == stop_date
        and abs(float(pivot["price"]) - float(stop_price)) <= 1e-4
        and str(pivot["confirmation_date"]) <= str(packet["review_as_of"])
    ), None)


def _pivots(packet: dict[str, Any], scale: str, side: str) -> list[dict[str, Any]]:
    return [
        value for value in packet["confirmed_pivots_asof"]
        if value["scale"] == scale and value["side"] == side
    ][-4:]


def _compact(index_row: dict[str, Any], packet_ref: dict[str, Any], packet: dict[str, Any], stop: dict[str, Any]) -> dict[str, Any]:
    day = packet["candidate_day"]
    close = float(day["close"])
    stop_price = float(stop["price"])
    atr = float(day["atr14"])
    recent = packet["recent_daily_visible_facts_asof"]
    return {
        "review_id": f"{int(index_row['index']):04d}-{index_row['code']}-{day['date']}",
        "index": index_row["index"],
        "code": str(index_row["code"]),
        "name": index_row["name"],
        "first_selected_on": index_row["first_selected_on"],
        "review_as_of": day["date"],
        "packet_path": packet_ref["path"],
        "packet_sha256": packet_ref["sha256"],
        "data_quality": packet["data_quality"],
        "selection_asof": packet["selection_asof"],
        "market": {
            "open": day["open"], "high": day["high"], "low": day["low"], "close": close,
            "return_1d_pct": day["return_1d_pct"], "volume_ratio_20": day["volume_ratio_20"],
            "atr14": atr, "ma5": day["ma5"], "ma13": day["ma13"], "ma21": day["ma21"],
            "ma55": day["ma55"], "ma105": day["ma105"], "ma144": day["ma144"],
            "macd_hist": day["macd_hist"], "macd_hist_delta": day["macd_hist_delta"],
            "facts": day["facts"],
        },
        "structure": {
            "small_pivot_high_date": day["small_pivot_high_date"],
            "small_pivot_high": day["small_pivot_high"],
            "small_pivot_low_date": day["small_pivot_low_date"],
            "small_pivot_low": day["small_pivot_low"],
            "large_pivot_high_date": day["large_pivot_high_date"],
            "large_pivot_high": day["large_pivot_high"],
            "large_pivot_low_date": day["large_pivot_low_date"],
            "large_pivot_low": day["large_pivot_low"],
            "confirmed_small_lows": _pivots(packet, "SMALL", "LOW"),
            "confirmed_small_highs": _pivots(packet, "SMALL", "HIGH"),
            "confirmed_large_lows": _pivots(packet, "LARGE", "LOW"),
            "confirmed_large_highs": _pivots(packet, "LARGE", "HIGH"),
        },
        "completed_macd_cycles_asof": packet["completed_macd_cycles_asof"][-4:],
        "recent_30d_range": {
            "start": recent[0]["date"],
            "end": recent[-1]["date"],
            "low": min(float(value["low"]) for value in recent),
            "high": max(float(value["high"]) for value in recent),
        },
        "risk": {
            "stop_date": stop["source_date"],
            "stop_confirmed_on": stop["confirmation_date"],
            "stop_price": stop_price,
            "distance_pct": (close - stop_price) / close * 100,
            "distance_atr": (close - stop_price) / atr if atr else None,
        },
        "review_contract": {
            "allowed_decisions": ["V2_CORE", "NEAR_PASS_MACRO_COPY", "NEAR_PASS_FRESH_Q1", "BEAR_REVERSAL_PROBE", "NO_TRADE"],
            "all_common_hard_guards_must_pass": True,
            "near_or_probe_requires_exactly_one_allowed_unknown": True,
            "allowed_fail_count_for_trade": 0,
            "future_data_present": False,
            "performance_present": False,
            "v1_signal_present": False,
        },
    }


def build() -> dict[str, Any]:
    index_rows = _jsonl(INDEX_PATH)
    ledgers = {str(row["code"]): row for row in _jsonl(PARENT_LEDGER)}
    queue: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    seen_structures: dict[str, set[tuple[Any, ...]]] = {}
    for row in index_rows:
        code = str(row["code"])
        if row["v2_core"]:
            continue
        seen_structures[code] = set()
        events = (ledgers[code].get("v2") or {}).get("watchlist_events") or []
        for packet_ref in row["review_packets"]:
            packet = _read(Path(packet_ref["path"]))
            day = packet["candidate_day"]
            day_key = str(day["date"])
            facts = set(day.get("facts") or [])
            reason = None
            if "CLOSE_BREAK_LAST_CONFIRMED_SMALL_PIVOT_HIGH" not in facts:
                reason = "TRIGGER_NOT_COMPLETED"
            elif not _watch_active(events, day_key):
                reason = "INACTIVE_WATCHLIST"
            else:
                stop = _causal_stop(packet)
                if stop is None:
                    reason = "NO_CAUSAL_CONFIRMED_STOP"
                else:
                    structure_key = (
                        day.get("small_pivot_high_date"), day.get("small_pivot_low_date"),
                        day.get("large_pivot_high_date"), day.get("large_pivot_low_date"),
                    )
                    if structure_key in seen_structures[code]:
                        reason = "DUPLICATE_SAME_STRUCTURE"
                    else:
                        seen_structures[code].add(structure_key)
                        queue.append(_compact(row, packet_ref, packet, stop))
            if reason:
                reasons[reason] += 1
                rejections.append({
                    "index": row["index"], "code": code, "name": row["name"],
                    "review_as_of": day_key, "decision": "OBJECTIVE_NO_TRADE", "reason": reason,
                    "packet_path": packet_ref["path"], "packet_sha256": packet_ref["sha256"],
                })
    _write_jsonl(QUEUE_PATH, queue)
    _write_jsonl(OBJECTIVE_REJECTIONS_PATH, rejections)
    shard_paths = []
    for start in range(0, len(queue), 250):
        path = RUN / "queue_shards" / f"queue_{start:04d}_{min(start + 249, len(queue) - 1):04d}.jsonl"
        _write_jsonl(path, queue[start:start + 250])
        shard_paths.append({"path": str(path.resolve()), "rows": len(queue[start:start + 250]), "sha256": _sha(path)})
    manifest = {
        "method_version": "formal-ai-v3-full-causal-review-queue-v1",
        "boundary": "FACTUAL_GUARD_FILTER_ONLY_NO_ROUTE_OR_TRADE_APPROVAL",
        "source_index": {"path": str(INDEX_PATH.resolve()), "sha256": _sha(INDEX_PATH)},
        "parent_ledger": {"path": str(PARENT_LEDGER.resolve()), "sha256": _sha(PARENT_LEDGER)},
        "non_core_stocks": sum(not row["v2_core"] for row in index_rows),
        "source_checkpoints": sum(len(row["review_packets"]) for row in index_rows if not row["v2_core"]),
        "objective_rejections": len(rejections),
        "objective_rejection_reasons": dict(reasons),
        "ai_review_queue": len(queue),
        "queue_stocks": len({row["code"] for row in queue}),
        "queue": {"path": str(QUEUE_PATH.resolve()), "sha256": _sha(QUEUE_PATH)},
        "objective_rejections_file": {"path": str(OBJECTIVE_REJECTIONS_PATH.resolve()), "sha256": _sha(OBJECTIVE_REJECTIONS_PATH)},
        "shards": shard_paths,
    }
    _write(MANIFEST_PATH, manifest)
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=True, indent=2))
