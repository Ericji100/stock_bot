"""Build frozen, identity-blind event-driven packets for hybrid V3 review.

Every stock-day remains represented by the source daily ledger.  This builder
selects only objective change points where the semantic AI must refresh.  It
does not classify a scenario, gate, route, permission, or trade.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan"
SOURCE_MANIFEST = SOURCE / "packet_manifest.json"
RUN = SOURCE / "hybrid_monitoring_v1"
EVENTS = RUN / "anonymous_event_packets.jsonl"
SAMPLE = RUN / "consistency_sample.jsonl"
RESELECTION_CANDIDATES = RUN / "reselection_candidates.jsonl"
MANIFEST = RUN / "event_manifest.json"
PROTOCOL = ROOT / "config/hybrid_monitoring_protocol_v1.json"
PROTOCOL_DOC = ROOT / "docs/hybrid-monitoring-protocol-v1.md"
SCHEMA = ROOT / "config/hybrid_common_structure_v1.schema.json"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
            count += 1
    return count


def _regime(close: float, first: float | None, second: float | None) -> str:
    values = [float(v) for v in (first, second) if v is not None]
    if len(values) != 2:
        return "UNAVAILABLE"
    if close >= max(values):
        return "ABOVE_BOTH"
    if close < min(values):
        return "BELOW_BOTH"
    return "BETWEEN"


def _pivot_ref(row: dict[str, Any]) -> str:
    return "PIVOT:{scale}:{side}:{source_date}:{confirmation_date}".format(**row)


def _cycle_ref(row: dict[str, Any]) -> str:
    return f"MACD:{row['sign']}:{row['start']}:{row['end']}"


def _bar_ref(day: str) -> str:
    return f"BAR:{day}"


def _event_types(row: dict[str, Any], state: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    current = row["candidate_day"]
    facts = set(current.get("facts") or [])
    events: list[str] = []
    if int(row["stock_day_ordinal"]) == 0:
        events.append("INITIAL_SELECTION")
    if any(value.endswith("PIVOT_HIGH_CONFIRMED_TODAY") or value.endswith("PIVOT_LOW_CONFIRMED_TODAY") for value in facts):
        events.append("PIVOT_CONFIRMED")
    if {"MACD_HIST_TURN_POSITIVE", "MACD_HIST_TURN_NEGATIVE"}.intersection(facts):
        events.append("MACD_SIGN_CHANGE")
    if "CLOSE_BREAK_LAST_CONFIRMED_SMALL_PIVOT_HIGH" in facts:
        events.append("SMALL_CONTROL_BREAK")
    if "CLOSE_BREAK_LAST_CONFIRMED_LARGE_PIVOT_HIGH" in facts:
        events.append("LARGE_CONTROL_BREAK")

    close = float(current["close"])
    long_regime = _regime(close, current.get("ma105"), current.get("ma144"))
    working_regime = _regime(close, current.get("ma21"), current.get("ma55"))
    if state.get("long_regime") not in (None, long_regime):
        events.append("LONG_REGIME_CHANGE")
    if state.get("working_regime") not in (None, working_regime):
        events.append("WORKING_REGIME_CHANGE")

    large_lows = [p for p in (row.get("confirmed_pivots_asof") or []) if p["scale"] == "LARGE" and p["side"] == "LOW"]
    below_macro = bool(large_lows and close < float(large_lows[-1]["price"]))
    if below_macro and not state.get("below_macro", False):
        events.append("MACRO_DEFENSE_ALERT")
    return list(dict.fromkeys(events)), {
        "long_regime": long_regime,
        "working_regime": working_regime,
        "below_macro": str(int(below_macro)),
    }


def _packet(row: dict[str, Any], event_types: list[str], regimes: dict[str, str]) -> dict[str, Any]:
    pivots = row.get("confirmed_pivots_asof") or []
    cycles = row.get("completed_macd_cycles_asof") or []
    recent = row.get("recent_daily_visible_facts_asof") or []
    evidence: list[dict[str, Any]] = []
    for bar in recent:
        evidence.append({
            "ref": _bar_ref(str(bar["date"])), "kind": "BAR", "date": bar["date"],
            "values": {key: bar.get(key) for key in ("open", "high", "low", "close", "volume_ratio_20", "atr14", "ma5", "ma13", "ma21", "ma55", "ma105", "ma144", "macd_hist", "macd_hist_delta", "facts")},
        })
    for pivot in pivots:
        evidence.append({"ref": _pivot_ref(pivot), "kind": "CONFIRMED_PIVOT", "date": pivot["confirmation_date"], "values": pivot})
    for cycle in cycles:
        evidence.append({"ref": _cycle_ref(cycle), "kind": "COMPLETED_MACD_CYCLE", "date": cycle["end"], "values": cycle})
    current = row["candidate_day"]
    stop_candidates = [p for p in pivots if p["scale"] == "SMALL" and p["side"] == "LOW" and float(p["price"]) < float(current["close"])]
    return {
        "packet_version": "hybrid-event-packet-v1",
        "protocol_version": "hybrid-monitoring-protocol-v1",
        "review_id": row["review_id"],
        "anonymous_stock_id": row["anonymous_stock_id"],
        "as_of": row["review_as_of"],
        "stock_day_ordinal": row["stock_day_ordinal"],
        "event_types": event_types,
        "selection_asof": row["selection_asof"],
        "data_quality": row["data_quality"],
        "objective_regimes": regimes,
        "candidate_stop_refs": [_pivot_ref(item) for item in stop_candidates[-3:]],
        "evidence": evidence,
        "ai_contract": {
            "identity_visible": False, "future_data_visible": False, "performance_visible": False,
            "may_decide_trade": False, "may_invent_evidence": False,
        },
    }


def _stratified_sample(events: list[dict[str, Any]], size: int, seed: str) -> list[dict[str, Any]]:
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        for event_type in row["event_types"]:
            strata[event_type].append(row)
    chosen: dict[str, dict[str, Any]] = {}
    keys = sorted(strata)
    cursor = 0
    while len(chosen) < min(size, len(events)) and keys:
        key = keys[cursor % len(keys)]
        available = [row for row in strata[key] if row["review_id"] not in chosen]
        if available:
            available.sort(key=lambda row: hashlib.sha256(f"{seed}|{key}|{row['review_id']}".encode()).hexdigest())
            chosen[available[0]["review_id"]] = available[0]
        elif all(all(row["review_id"] in chosen for row in strata[value]) for value in keys):
            break
        cursor += 1
    return sorted(chosen.values(), key=lambda row: (row["as_of"], row["anonymous_stock_id"]))


def build() -> dict[str, Any]:
    source = _read(SOURCE_MANIFEST)
    protocol = _read(PROTOCOL)
    if int(source["stocks"]) != int(protocol["monitoring_window"]["expected_stocks"]):
        raise ValueError("source stock count differs from frozen protocol")
    if int(source["stock_days"]) != int(protocol["monitoring_window"]["expected_stock_days"]):
        raise ValueError("source stock-day count differs from frozen protocol")

    stock_state: dict[str, dict[str, Any]] = defaultdict(dict)
    event_rows: list[dict[str, Any]] = []
    reselection_candidates: list[dict[str, Any]] = []
    event_counts: Counter[str] = Counter()
    scanned = 0
    for item in source["rounds"]:
        path = Path(item["path"])
        if _sha(path) != item["sha256"]:
            raise ValueError(f"source round changed: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            scanned += 1
            selected_today = list((row.get("selection_asof") or {}).get("selected_today") or [])
            if int(row["stock_day_ordinal"]) > 0 and selected_today:
                reselection_candidates.append({
                    "review_id": row["review_id"], "anonymous_stock_id": row["anonymous_stock_id"],
                    "as_of": row["review_as_of"], "selected_today": selected_today,
                    "meaning": "RESELECTION_ONLY_IF_PROGRAM_LIFECYCLE_IS_INACTIVE",
                })
            state = stock_state[str(row["anonymous_stock_id"])]
            event_types, regimes = _event_types(row, state)
            state.update({"long_regime": regimes["long_regime"], "working_regime": regimes["working_regime"], "below_macro": regimes["below_macro"] == "1"})
            if not event_types:
                continue
            packet = _packet(row, event_types, regimes)
            event_rows.append(packet)
            event_counts.update(event_types)

    if scanned != int(source["stock_days"]):
        raise ValueError(f"scanned {scanned}, expected {source['stock_days']}")
    _write_jsonl(EVENTS, event_rows)
    _write_jsonl(RESELECTION_CANDIDATES, reselection_candidates)
    sampling = protocol["event_sampling"]
    sample = _stratified_sample(event_rows, int(sampling["sample_size"]), str(sampling["seed"]))
    _write_jsonl(SAMPLE, sample)
    manifest = {
        "method_version": "hybrid-v3-objective-events-v1",
        "boundary": "OBJECTIVE_EVENTS_ONLY_NO_AI_SEMANTICS_NO_POLICY_NO_PERFORMANCE",
        "source_manifest": {"path": str(SOURCE_MANIFEST.resolve()), "sha256": _sha(SOURCE_MANIFEST)},
        "protocol": {"path": str(PROTOCOL.resolve()), "sha256": _sha(PROTOCOL)},
        "protocol_doc": {"path": str(PROTOCOL_DOC.resolve()), "sha256": _sha(PROTOCOL_DOC)},
        "ai_schema": {"path": str(SCHEMA.resolve()), "sha256": _sha(SCHEMA)},
        "stocks": source["stocks"], "stock_days_scanned": scanned,
        "event_review_points": len(event_rows), "event_type_counts": dict(sorted(event_counts.items())),
        "events": {"path": str(EVENTS.resolve()), "sha256": _sha(EVENTS)},
        "reselection_candidates": {"rows": len(reselection_candidates), "path": str(RESELECTION_CANDIDATES.resolve()), "sha256": _sha(RESELECTION_CANDIDATES), "ai_refresh_required": "ONLY_WHEN_PROGRAM_LIFECYCLE_IS_INACTIVE"},
        "consistency_sample": {"rows": len(sample), "path": str(SAMPLE.resolve()), "sha256": _sha(SAMPLE)},
        "identity_visible": False, "performance_visible": False, "outcome_excluded_codes": [],
    }
    _write_json(MANIFEST, manifest)
    return manifest


if __name__ == "__main__":
    result = build()
    print(json.dumps({key: result[key] for key in ("stocks", "stock_days_scanned", "event_review_points", "event_type_counts", "consistency_sample")}, ensure_ascii=False, indent=2))
