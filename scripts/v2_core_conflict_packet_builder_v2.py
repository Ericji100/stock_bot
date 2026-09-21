"""Build full-history, outcome-blind packets for V2 boundary conflicts.

V1 exposed daily MA/price facts only from the monitoring date.  That was
enough to judge the local trigger, but not enough to prove a repeated long-MA
habit or choose between a mature campaign and a first macro replication.  V2
keeps the same six identity/answer/performance-blind cases and adds up to 750
causal daily rows ending at AS-OF.  It never opens a legacy answer or a
performance artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.formal_ai_full_review_packets import confirmed_pivot_timeline
from scripts.formal_ai_historical_2023_packets import _causal_frame, _daily_fact_with_raw
from scripts.v2_core_source_audit_v1 import sha256


VERSION = "v2-core-conflict-packet-v2"
POSITIVE_KIND = "POSITIVE_REFERENCE（舊V2核准正例）"
CONTEXT_BARS = 750
FORBIDDEN_KEYS = {
    "code",
    "name",
    "symbol",
    "legacy_v2_trigger",
    "legacy_v2_no_trade_reason",
    "scenario",
    "trigger_path",
    "future_outcome",
    "mfe",
    "mae",
    "pnl",
    "profit",
    "return_pct",
    "exit_date",
    "exit_price",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def anonymous_id(code: str) -> str:
    return "S2-" + hashlib.sha256(f"{VERSION}|{code}".encode("utf-8")).hexdigest()[:16]


def review_id(code: str, as_of: str) -> str:
    return "B2-" + hashlib.sha256(f"{VERSION}|{code}|{as_of}".encode("utf-8")).hexdigest()[:24]


def contains_forbidden_key(value: Any) -> set[str]:
    hits: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                hits.add(str(key))
            hits.update(contains_forbidden_key(child))
    elif isinstance(value, list):
        for child in value:
            hits.update(contains_forbidden_key(child))
    return hits


def _manifest_item(run_root: Path, code: str) -> dict[str, Any]:
    # ``packet_manifest.json`` inventories the rendered review packets only;
    # the immutable market-data paths and hashes live in ``input_manifest``.
    manifest_path = run_root / "input_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    items = [item for item in manifest.get("items") or [] if str(item.get("code")) == code]
    if len(items) != 1:
        raise RuntimeError(f"expected one market-data item for {code} in {manifest_path}, found {len(items)}")
    item = items[0]
    price_path = Path(str(item["price_path"]))
    if not price_path.is_file():
        raise RuntimeError(f"price file missing: {price_path}")
    if sha256(price_path) != str(item.get("price_sha256")):
        raise RuntimeError(f"price file changed: {price_path}")
    return item


def build_full_history_packet(source_case: dict[str, Any]) -> dict[str, Any]:
    code = str(source_case["code"])
    as_of = str(source_case["as_of"])
    source_path = Path(str(source_case["packet_path"]))
    if sha256(source_path) != str(source_case["packet_sha256"]):
        raise RuntimeError(f"source packet changed: {source_case['batch_id']}:{code}")
    source = json.loads(source_path.read_text(encoding="utf-8-sig"))
    run_root = source_path.parent.parent
    item = _manifest_item(run_root, code)
    frame = _causal_frame(item, as_of)
    context = frame.tail(CONTEXT_BARS)
    # Selection events are intentionally absent: monitoring provenance must not
    # influence the structural scenario or the controlling anchor.
    daily = [_daily_fact_with_raw(frame, int(index), {}) for index in context.index]
    cycles = [
        cycle
        for cycle in (source.get("macd_21_55_55_cycles") or [])
        if str(cycle.get("end") or "9999-12-31") <= as_of
    ]
    pivots = confirmed_pivot_timeline(frame, start="2020-01-01")
    actions = [
        action
        for action in ((source.get("corporate_action_audit") or {}).get("events_in_window") or [])
        if str(action.get("date") or "9999-12-31") <= as_of
    ]
    packet = {
        "packet_version": VERSION,
        "review_id": review_id(code, as_of),
        "anonymous_stock_id": anonymous_id(code),
        "as_of": as_of,
        "task": "RESULT_BLIND_V2_SCENARIO_BOUNDARY_WITH_FULL_CAUSAL_CONTEXT",
        "source_packet_sha256": sha256(source_path),
        "source_price_sha256": str(item["price_sha256"]),
        "data_quality": {
            "preferred_750_met": len(frame) >= CONTEXT_BARS,
            "available_bars_to_as_of": int(len(frame)),
            "context_bars": int(len(context)),
            "context_start": daily[0]["date"],
            "context_end": daily[-1]["date"],
            "raw_and_adjusted_ohlc_present": bool((source.get("data_quality") or {}).get("raw_and_adjusted_ohlc_present")),
            "corporate_action_unresolved_as_of": bool((source.get("corporate_action_audit") or {}).get("corporate_action_unresolved")),
            "corporate_actions_visible_to_as_of": actions,
        },
        "completed_macd_21_55_55_cycles_to_as_of": cycles,
        "confirmed_pivots_to_as_of": pivots,
        "daily_structure_context_to_as_of": daily,
        "review_constraints": {
            "identity_blind": True,
            "legacy_answer_blind": True,
            "future_performance_blind": True,
            "position_state_must_not_choose_structural_scenario": True,
            "monitor_start_and_upstream_selection_hidden": True,
            "controlling_anchor_must_be_selected_before_scenario": True,
            "mature_requires_proven_repeated_long_ma_habit": True,
            "first_independent_completed_parent_replication_prefers_macro": True,
            "allowed_primary_scenarios": [
                "MATURE_TREND_PULLBACK",
                "MACRO_COPY_RESONANCE",
                "BEAR_REVERSAL_LEFT_RIGHT",
                "FRESH_Q1_EXPANSION",
                "UNRESOLVED_NO_TRADE",
            ],
            "required_output": [
                "primary_scenario",
                "secondary_scenarios",
                "canonical_trigger_route",
                "anchor_start",
                "anchor_end_or_forming",
                "parent_correction_replication_relationship",
                "campaign_maturity",
                "taiji_phase",
                "taiji_leg_index",
                "large_direction",
                "small_direction",
                "episode_stop_date",
                "episode_stop_price",
                "permission",
                "evidence",
                "uncertainties",
            ],
        },
    }
    forbidden = contains_forbidden_key(packet)
    if forbidden:
        raise RuntimeError(f"forbidden keys in blind packet: {sorted(forbidden)}")
    if daily[-1]["date"] != as_of:
        raise RuntimeError(f"latest daily row is not AS-OF for {packet['review_id']}")
    return packet


def build_packets(cases_path: Path, output_dir: Path) -> dict[str, Any]:
    cases = read_jsonl(cases_path)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        if case.get("case_kind") == POSITIVE_KIND:
            grouped[(str(case["code"]), str(case["as_of"]))].append(case)
    duplicate_groups = {key: rows for key, rows in grouped.items() if len(rows) > 1}
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows: list[dict[str, Any]] = []
    for (code, as_of), rows in sorted(duplicate_groups.items()):
        # The earliest monitoring packet is selected only as an immutable data
        # provenance source.  Monitoring metadata is not copied to the packet.
        selected = min(rows, key=lambda row: (str(row.get("monitor_on") or "9999-12-31"), str(row["batch_id"])))
        packet = build_full_history_packet(selected)
        path = output_dir / f"{packet['review_id']}.json"
        path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_rows.append({
            "review_id": packet["review_id"],
            "anonymous_stock_id": packet["anonymous_stock_id"],
            "as_of": as_of,
            "packet_file": path.name,
            "packet_sha256": sha256(path),
            "source_packet_sha256": packet["source_packet_sha256"],
            "source_price_sha256": packet["source_price_sha256"],
            "context_bars": packet["data_quality"]["context_bars"],
            "context_start": packet["data_quality"]["context_start"],
            "context_end": packet["data_quality"]["context_end"],
            "future_performance_included": False,
            "legacy_answer_included": False,
            "identity_included": False,
            "monitoring_metadata_included": False,
        })
    manifest = {
        "version": VERSION,
        "status": "BLIND_CALIBRATION_REVIEW（盲化校準複核）",
        "supersedes_packet_version": "v2-core-conflict-packet-v1",
        "reason": "V1缺少監控日前的逐日均線與價格證據，無法穩定判斷長均線慣性及控制定錨。",
        "source_cases_sha256": sha256(cases_path),
        "packet_count": len(manifest_rows),
        "rows": manifest_rows,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_packets(args.cases, args.output_dir)
    print(json.dumps({"packet_count": manifest["packet_count"], "output_dir": str(args.output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
