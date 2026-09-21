"""Build outcome-blind, as-of packets for the 2023-H2 formal V3 AI review.

This module prepares facts only.  It does not classify a V3 route, approve a
trade, infer an UNKNOWN gate, or use V1 triggers as candidate authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2"
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2_v3"
PACKET_ROOT = RUN / "v3_review_packets"
PARENT_LEDGER = PARENT / "ai_decisions_merged.jsonl"
PARENT_MANIFEST = PARENT / "input_manifest.json"
PARENT_VALIDATION = PARENT / "decision_validation.json"
INDEX_PATH = RUN / "v3_review_index.jsonl"
MANIFEST_PATH = RUN / "input_manifest.json"
AS_OF = "2024-02-02"
MONITOR_FLOOR = "2023-06-01"
EXPECTED_STOCKS = 1029

STRUCTURAL_FACTS = {
    "CLOSE_BREAK_LAST_CONFIRMED_SMALL_PIVOT_HIGH",
    "CLOSE_BREAK_LAST_CONFIRMED_LARGE_PIVOT_HIGH",
    "SMALL_PIVOT_LOW_CONFIRMED_TODAY",
    "LARGE_PIVOT_LOW_CONFIRMED_TODAY",
}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_items() -> dict[str, dict[str, Any]]:
    manifest = _read(PARENT_MANIFEST)
    return {str(item["code"]): item for item in manifest["items"]}


def _candidate_dates(row: dict[str, Any], source_packet: dict[str, Any]) -> list[str]:
    """Return neutral review checkpoints, never an approval decision.

    Dates named in the already-frozen V2 assessment are retained.  Structural
    fact-change dates are also retained so the new V3 review is not limited to
    a V1 signal list or to a regex interpretation of the V2 prose.
    """
    first = str(row["first_selected_on"])
    valid_daily = {
        str(day["date"]): day
        for day in source_packet["daily_visible_facts_from_monitoring"]
        if first <= str(day["date"]) <= AS_OF
    }
    reason = str((row.get("v2") or {}).get("no_trade_reason") or "")
    mentioned = {
        day
        for day in re.findall(r"20\d{2}-\d{2}-\d{2}", reason)
        if day in valid_daily and first <= day <= AS_OF
    }
    structural = {
        day
        for day, facts in valid_daily.items()
        if STRUCTURAL_FACTS.intersection(set(facts.get("facts") or []))
    }
    return sorted(mentioned | structural)


def _completed_cycles_asof(packet: dict[str, Any], day: str) -> list[dict[str, Any]]:
    cycles = []
    for cycle in packet["macd_21_55_55_cycles"]:
        end = cycle.get("end")
        if end and str(end) <= day:
            cycles.append(cycle)
    return cycles[-8:]


def _pivots_asof(packet: dict[str, Any], day: str) -> list[dict[str, Any]]:
    eligible = [
        pivot
        for pivot in packet["confirmed_pivots"]
        if str(pivot["confirmation_date"]) <= day
    ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for pivot in eligible:
        grouped[(str(pivot["scale"]), str(pivot["side"]))].append(pivot)
    output = []
    for key in (("LARGE", "LOW"), ("LARGE", "HIGH"), ("SMALL", "LOW"), ("SMALL", "HIGH")):
        output.extend(grouped[key][-8:])
    return sorted(output, key=lambda value: (str(value["confirmation_date"]), str(value["scale"]), str(value["side"])))


def _selection_asof(packet: dict[str, Any], day: str) -> dict[str, Any]:
    timeline = {
        key: value
        for key, value in packet["selection_timeline"].items()
        if str(key) <= day
    }
    return {
        "selected_today": timeline.get(day, []),
        "accumulated_sources": sorted({source for sources in timeline.values() for source in sources}),
    }


def _context_asof(packet: dict[str, Any], day: str) -> list[dict[str, Any]]:
    rows = [row for row in packet["daily_visible_facts_from_monitoring"] if str(row["date"]) <= day]
    return rows[-30:]


def build() -> dict[str, Any]:
    parent_validation = _read(PARENT_VALIDATION)
    expected_hashes = {Path(row["path"]).name: row["sha256"] for row in parent_validation["ledger_files"]}
    parent_hash_errors = []
    for name, expected in expected_hashes.items():
        path = PARENT / name
        actual = _sha256(path)
        if actual != expected:
            parent_hash_errors.append({"path": str(path), "expected": expected, "actual": actual})
    expected_manifest_hash = parent_validation["input_manifest"]["sha256"]
    if _sha256(PARENT_MANIFEST) != expected_manifest_hash:
        parent_hash_errors.append(
            {"path": str(PARENT_MANIFEST), "expected": expected_manifest_hash, "actual": _sha256(PARENT_MANIFEST)}
        )
    if parent_hash_errors:
        raise ValueError(f"parent V1/V2 inputs changed: {parent_hash_errors}")

    rows = _read_jsonl(PARENT_LEDGER)
    items = _manifest_items()
    if len(rows) != EXPECTED_STOCKS or len(items) != EXPECTED_STOCKS:
        raise ValueError(f"expected {EXPECTED_STOCKS} rows/items, got {len(rows)}/{len(items)}")

    index_rows = []
    packet_count = 0
    for row in rows:
        code = str(row["code"])
        source_path = PARENT / "review_packets" / f"{code}.json"
        packet = _read(source_path)
        v2_triggers = (row.get("v2") or {}).get("triggers") or []
        dates = [] if v2_triggers else _candidate_dates(row, packet)
        packet_rows = []
        for day in dates:
            daily = next(value for value in packet["daily_visible_facts_from_monitoring"] if value["date"] == day)
            review = {
                "packet_version": "formal-ai-historical-2023-v3-asof-v1",
                "stock": packet["stock"],
                "index": row["index"],
                "first_selected_on": row["first_selected_on"],
                "review_as_of": day,
                "data_quality": packet["data_quality"],
                "selection_asof": _selection_asof(packet, day),
                "completed_macd_cycles_asof": _completed_cycles_asof(packet, day),
                "confirmed_pivots_asof": _pivots_asof(packet, day),
                "recent_daily_visible_facts_asof": _context_asof(packet, day),
                "candidate_day": daily,
                "v2_parent_status": str((row.get("v2") or {}).get("stock_status") or "NO_TRADE"),
                "review_contract": {
                    "ai_must_decide": [
                        "V3 route or NO_TRADE",
                        "all ten common hard guards",
                        "exact route hard/soft gates",
                        "at most one allowed UNKNOWN and zero FAIL for a trade",
                        "causal episode stop",
                    ],
                    "v1_trigger_used_as_input": False,
                    "future_bars_present": False,
                    "performance_present": False,
                    "candidate_date_is_approval": False,
                },
            }
            packet_path = PACKET_ROOT / f"{int(row['index']):04d}_{code}" / f"{day}.json"
            _write(packet_path, review)
            packet_rows.append({"date": day, "path": str(packet_path.resolve()), "sha256": _sha256(packet_path)})
            packet_count += 1
        index_rows.append(
            {
                "index": row["index"],
                "code": code,
                "name": row.get("name"),
                "first_selected_on": row["first_selected_on"],
                "v2_core": bool(v2_triggers),
                "v2_trigger_count": len(v2_triggers),
                "v2_parent_no_trade_reason": (row.get("v2") or {}).get("no_trade_reason"),
                "source_packet_sha256": _sha256(source_path),
                "review_packets": packet_rows,
            }
        )

    _write_jsonl(INDEX_PATH, index_rows)
    manifest = {
        "method_version": "formal-ai-historical-2023-v3-asof-packets-v1",
        "boundary": "FACTS_ONLY_NO_V3_CLASSIFICATION_OR_APPROVAL",
        "monitor_floor": MONITOR_FLOOR,
        "as_of": AS_OF,
        "expected_stocks": EXPECTED_STOCKS,
        "stocks": len(index_rows),
        "v2_core_stocks": sum(row["v2_core"] for row in index_rows),
        "non_core_stocks": sum(not row["v2_core"] for row in index_rows),
        "candidate_asof_packets": packet_count,
        "parent_run": str(PARENT.resolve()),
        "parent_decision_validation_sha256": _sha256(PARENT_VALIDATION),
        "parent_input_manifest_sha256": _sha256(PARENT_MANIFEST),
        "parent_merged_ledger_sha256": _sha256(PARENT_LEDGER),
        "v1_rule_sha256": _sha256(ROOT / "docs/enlightenment-ai-judgement-v1.md"),
        "v2_rule_sha256": _sha256(ROOT / "docs/enlightenment-ai-judgement-v2.md"),
        "v3_rule_sha256": _sha256(ROOT / "docs/enlightenment-ai-judgement-v3.md"),
        "v3_machine_rules_sha256": _sha256(ROOT / "config/enlightenment_ai_rules_v3.json"),
        "v3_schema_sha256": _sha256(ROOT / "config/enlightenment_ai_judgement_v3.schema.json"),
        "review_index": str(INDEX_PATH.resolve()),
        "review_index_sha256": _sha256(INDEX_PATH),
        "parent_hash_errors": parent_hash_errors,
    }
    _write(MANIFEST_PATH, manifest)
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
