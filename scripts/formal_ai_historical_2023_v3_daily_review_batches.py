"""Compact and shard every-day V3 packets without making any decision."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan"
RUN = SOURCE / "ai_daily_review"
SOURCE_MANIFEST = SOURCE / "packet_manifest.json"
BATCH_DIR = RUN / "batches"
MANIFEST = RUN / "batch_manifest.json"
SHARDS = 3


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _stock_shard(anonymous_id: str) -> int:
    return int(hashlib.sha256(anonymous_id.encode("utf-8")).hexdigest(), 16) % SHARDS


def _pct_change(current: float, prior: float | None) -> float | None:
    if prior in (None, 0):
        return None
    return round((current / float(prior) - 1) * 100, 3)


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    current = row["candidate_day"]
    recent = row["recent_daily_visible_facts_asof"]
    close = float(current["close"])
    closes = [float(item["close"]) for item in recent]
    ma_values = {name: current.get(name) for name in ("ma5", "ma13", "ma21", "ma55", "ma105", "ma144")}
    above = [name for name, value in ma_values.items() if value is not None and close >= float(value)]
    confirmed = row["confirmed_pivots_asof"]
    small_lows = [item for item in confirmed if item["scale"] == "SMALL" and item["side"] == "LOW"]
    stop = small_lows[-1] if small_lows else None
    atr = float(current["atr14"] or 0)
    return {
        "review_id": row["review_id"],
        "anonymous_stock_id": row["anonymous_stock_id"],
        "review_as_of": row["review_as_of"],
        "stock_day_ordinal": row["stock_day_ordinal"],
        "selection_asof": row["selection_asof"],
        "data_quality": row["data_quality"],
        "market": {
            "open": current["open"], "high": current["high"], "low": current["low"], "close": close,
            "return_1d_pct": current["return_1d_pct"], "volume_ratio_20": current["volume_ratio_20"],
            "atr14": atr, "mas": ma_values, "above_mas": above,
            "ma105_slope_20d_pct": _pct_change(float(current["ma105"]), float(recent[-21]["ma105"])) if len(recent) >= 21 and current.get("ma105") and recent[-21].get("ma105") else None,
            "ma144_slope_20d_pct": _pct_change(float(current["ma144"]), float(recent[-21]["ma144"])) if len(recent) >= 21 and current.get("ma144") and recent[-21].get("ma144") else None,
            "macd_hist": current["macd_hist"], "macd_hist_delta": current["macd_hist_delta"],
            "facts": current["facts"],
        },
        "recent_context": {
            "start": recent[0]["date"], "end": recent[-1]["date"],
            "return_5d_pct": _pct_change(close, closes[-6] if len(closes) >= 6 else None),
            "return_10d_pct": _pct_change(close, closes[-11] if len(closes) >= 11 else None),
            "return_20d_pct": _pct_change(close, closes[-21] if len(closes) >= 21 else None),
            "range_low": min(float(item["low"]) for item in recent),
            "range_high": max(float(item["high"]) for item in recent),
            "last_8_bars": [
                {key: item.get(key) for key in ("date", "open", "high", "low", "close", "macd_hist", "facts")}
                for item in recent[-8:]
            ],
        },
        "completed_macd_cycles_asof": row["completed_macd_cycles_asof"][-4:],
        "confirmed_pivots_asof": confirmed,
        "causal_stop_candidate": None if stop is None else {
            "source_date": stop["source_date"], "confirmation_date": stop["confirmation_date"],
            "price": stop["price"],
            "distance_pct": round((close - float(stop["price"])) / close * 100, 3),
            "distance_atr": round((close - float(stop["price"])) / atr, 3) if atr else None,
        },
        "review_contract": {
            "identity_visible": False,
            "future_bars_present": False,
            "performance_present": False,
            "outcome_exclusion_allowed": False,
            "default_for_unlisted": "WATCHING_NO_EVENT",
            "ai_must_review_every_row": True,
            "event_states": ["ARMED", "TRIGGERED", "REMOVED"],
            "routes": ["V2_CORE", "NEAR_PASS_MACRO_COPY", "NEAR_PASS_FRESH_Q1", "BEAR_REVERSAL_PROBE", "NO_TRADE"],
        },
    }


def build() -> dict[str, Any]:
    source = _read(SOURCE_MANIFEST)
    outputs: list[dict[str, Any]] = []
    stock_days = 0
    shard_rows = [0] * SHARDS
    shard_ids: list[set[str]] = [set() for _ in range(SHARDS)]
    for round_item in source["rounds"]:
        source_path = Path(round_item["path"])
        if _sha(source_path) != str(round_item["sha256"]):
            raise ValueError(f"source daily round changed: {source_path}")
        grouped: list[list[dict[str, Any]]] = [[] for _ in range(SHARDS)]
        for line in source_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            compact = _compact(json.loads(line))
            shard = _stock_shard(str(compact["anonymous_stock_id"]))
            grouped[shard].append(compact)
            shard_ids[shard].add(str(compact["anonymous_stock_id"]))
            shard_rows[shard] += 1
            stock_days += 1
        for shard, values in enumerate(grouped):
            if not values:
                continue
            path = BATCH_DIR / f"shard_{shard}" / f"round_{int(round_item['round']):03d}_{round_item['date']}.jsonl"
            _write_jsonl(path, values)
            outputs.append({
                "shard": shard, "round": int(round_item["round"]), "date": round_item["date"],
                "rows": len(values), "path": str(path.resolve()), "sha256": _sha(path),
            })
    manifest = {
        "method_version": "formal-codex-ai-v3-every-day-review-batches-v1",
        "boundary": "IDENTITY_BLIND_FACT_COMPACTION_ONLY_NO_DECISIONS",
        "source_manifest": {"path": str(SOURCE_MANIFEST.resolve()), "sha256": _sha(SOURCE_MANIFEST)},
        "stocks": source["stocks"], "stock_days": stock_days, "rounds": source["trading_day_rounds"],
        "shards": SHARDS, "outcome_excluded_codes": [],
        "shard_stock_counts": [len(values) for values in shard_ids],
        "shard_stock_days": shard_rows,
        "files": outputs,
    }
    _write_json(MANIFEST, manifest)
    return manifest


if __name__ == "__main__":
    payload = build()
    print(json.dumps({key: payload[key] for key in ("stocks", "stock_days", "rounds", "shards", "shard_stock_counts", "shard_stock_days", "outcome_excluded_codes")}, ensure_ascii=False, indent=2))
