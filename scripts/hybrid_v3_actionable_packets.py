"""Create lazy semantic-review boundaries from the frozen hybrid event ledger.

All 71,205 objective change events remain in the event ledger.  Non-actionable
events mark the semantic cache DIRTY; the AI refresh is deferred until the next
point where policy could trade or remove.  No event is discarded and no market
or semantic classification is performed here.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan/hybrid_monitoring_v1"
SOURCE = RUN / "anonymous_event_packets.jsonl"
SOURCE_MANIFEST = RUN / "event_manifest.json"
OUTPUT = RUN / "anonymous_policy_boundary_packets.jsonl"
DIRTY_LEDGER = RUN / "semantic_dirty_events.jsonl"
MANIFEST = RUN / "policy_boundary_manifest.json"
ACTION_EVENTS = frozenset({"SMALL_CONTROL_BREAK", "LARGE_CONTROL_BREAK", "MACRO_DEFENSE_ALERT"})


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_line(handle, row: dict[str, Any]) -> None:
    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")


def build() -> dict[str, Any]:
    dirty_by_stock: dict[str, list[dict[str, Any]]] = defaultdict(list)
    counts: Counter[str] = Counter()
    source_rows = boundaries = dirty_rows = 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with SOURCE.open("r", encoding="utf-8-sig") as source, OUTPUT.open("w", encoding="utf-8") as output, DIRTY_LEDGER.open("w", encoding="utf-8") as dirty_handle:
        for line in source:
            if not line.strip():
                continue
            packet = json.loads(line)
            source_rows += 1
            anonymous_id = str(packet["anonymous_stock_id"])
            event_types = list(packet["event_types"])
            counts.update(event_types)
            actionable = bool(ACTION_EVENTS.intersection(event_types))
            if not actionable:
                dirty = {
                    "review_id": packet["review_id"], "anonymous_stock_id": anonymous_id,
                    "as_of": packet["as_of"], "event_types": event_types,
                    "state": "SEMANTIC_DIRTY_PENDING_LAZY_REFRESH",
                }
                dirty_by_stock[anonymous_id].append(dirty)
                _write_line(dirty_handle, dirty)
                dirty_rows += 1
                continue
            packet["lazy_refresh_contract"] = {
                "policy_boundary": True,
                "dirty_events_since_prior_boundary": dirty_by_stock.pop(anonymous_id, []),
                "all_intermediate_stock_days_still_scanned": True,
                "deferred_events_do_not_create_trade_permission": True,
            }
            _write_line(output, packet)
            boundaries += 1
    manifest = {
        "method_version": "hybrid-v3-lazy-semantic-boundaries-v1",
        "boundary": "NO_SEMANTIC_OR_TRADE_CLASSIFICATION; OBJECTIVE_EVENTS_ONLY",
        "source": {"path": str(SOURCE.resolve()), "sha256": _sha(SOURCE), "rows": source_rows},
        "source_manifest": {"path": str(SOURCE_MANIFEST.resolve()), "sha256": _sha(SOURCE_MANIFEST)},
        "policy_boundary_events": sorted(ACTION_EVENTS),
        "policy_boundary_rows": boundaries,
        "dirty_event_rows": dirty_rows,
        "dirty_events_after_last_boundary": sum(len(values) for values in dirty_by_stock.values()),
        "event_type_counts": dict(sorted(counts.items())),
        "output": {"path": str(OUTPUT.resolve()), "sha256": _sha(OUTPUT)},
        "dirty_ledger": {"path": str(DIRTY_LEDGER.resolve()), "sha256": _sha(DIRTY_LEDGER)},
        "identity_visible": False, "performance_visible": False,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    value = build()
    print(json.dumps({key: value[key] for key in ("policy_boundary_rows", "dirty_event_rows", "dirty_events_after_last_boundary", "policy_boundary_events")}, ensure_ascii=False, indent=2))
