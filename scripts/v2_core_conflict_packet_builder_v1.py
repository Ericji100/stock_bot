"""Create outcome/identity/legacy-label blind packets for V2 boundary conflicts."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from scripts.v2_core_source_audit_v1 import sha256
except ModuleNotFoundError:  # Direct ``python scripts\\...`` execution.
    from v2_core_source_audit_v1 import sha256


VERSION = "v2-core-conflict-packet-v1"
POSITIVE_KIND = "POSITIVE_REFERENCE（舊V2核准正例）"
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
    return "S-" + hashlib.sha256(f"{VERSION}|{code}".encode("utf-8")).hexdigest()[:16]


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


def trim_packet(source: dict[str, Any], code: str, as_of: str, source_sha: str) -> dict[str, Any]:
    cycles = []
    for cycle in source.get("macd_21_55_55_cycles") or []:
        if str(cycle.get("end") or "9999-12-31") <= as_of:
            cycles.append(cycle)
    pivots = [
        pivot
        for pivot in (source.get("confirmed_pivots") or [])
        if str(pivot.get("confirmation_date") or "9999-12-31") <= as_of
    ]
    daily = [
        row
        for row in (source.get("daily_visible_facts_from_monitoring") or [])
        if str(row.get("date") or "9999-12-31") <= as_of
    ]
    actions = []
    for action in (source.get("corporate_action_audit") or {}).get("events_in_window") or []:
        if str(action.get("date") or "9999-12-31") <= as_of:
            actions.append(action)
    packet = {
        "packet_version": VERSION,
        "anonymous_stock_id": anonymous_id(code),
        "as_of": as_of,
        "task": "RESULT_BLIND_V2_SCENARIO_AND_ROUTE_ADJUDICATION",
        "source_packet_sha256": source_sha,
        "data_quality": {
            "preferred_750_met": bool((source.get("data_quality") or {}).get("preferred_750_met")),
            "raw_and_adjusted_ohlc_present": bool((source.get("data_quality") or {}).get("raw_and_adjusted_ohlc_present")),
            "corporate_action_unresolved_as_of": bool((source.get("corporate_action_audit") or {}).get("corporate_action_unresolved")),
            "corporate_actions_visible_to_as_of": actions,
        },
        "completed_macd_21_55_55_cycles_to_as_of": cycles,
        "confirmed_pivots_to_as_of": pivots,
        "daily_visible_facts_to_as_of": daily,
        "review_constraints": {
            "identity_blind": True,
            "legacy_answer_blind": True,
            "future_performance_blind": True,
            "position_state_must_not_choose_structural_scenario": True,
            "monitor_start_must_not_choose_structural_scenario": True,
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
    return packet


def build_packets(cases_path: Path, output_dir: Path) -> dict[str, Any]:
    cases = read_jsonl(cases_path)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        if case.get("case_kind") == POSITIVE_KIND:
            grouped[(str(case["code"]), str(case["as_of"]))].append(case)
    duplicate_groups = {key: rows for key, rows in grouped.items() if len(rows) > 1}
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows = []
    for (code, as_of), rows in sorted(duplicate_groups.items()):
        # Earliest monitoring packet exposes the longest causal daily window.
        selected = min(rows, key=lambda row: (str(row.get("monitor_on") or "9999-12-31"), str(row["batch_id"])))
        source_path = Path(str(selected["packet_path"]))
        source_sha = sha256(source_path)
        if source_sha != selected.get("packet_sha256"):
            raise RuntimeError(f"source packet changed: {selected['batch_id']}:{code}")
        source = json.loads(source_path.read_text(encoding="utf-8-sig"))
        packet = trim_packet(source, code, as_of, source_sha)
        packet_id = "B-" + hashlib.sha256(f"{VERSION}|{code}|{as_of}".encode("utf-8")).hexdigest()[:24]
        packet["review_id"] = packet_id
        path = output_dir / f"{packet_id}.json"
        path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_rows.append(
            {
                "review_id": packet_id,
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "as_of": as_of,
                "packet_file": path.name,
                "packet_sha256": sha256(path),
                "source_packet_sha256": source_sha,
                "future_performance_included": False,
                "legacy_answer_included": False,
                "identity_included": False,
            }
        )
    manifest = {
        "version": VERSION,
        "status": "BLIND_CALIBRATION_REVIEW（盲化校準複核）",
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
