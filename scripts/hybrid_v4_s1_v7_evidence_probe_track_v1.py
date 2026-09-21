"""Freeze one outcome-blind V4-S1 probe from the V7 evidence packet."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .hybrid_v3_sharding_v2 import (
    _publish_immutable,
    build_case_records,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    write_shards,
)
from .hybrid_v4_s1_track_v1 import MODEL, PROMPT, RESEARCH_PROTOCOL, SCHEMA


ROOT = Path(__file__).resolve().parents[1]
FROZEN_TRACK_ROOT = (
    ROOT
    / "reports/course_backtest/2024-02-02"
    / "historical_scan_2023h2_formal_ai_v3_daily_scan"
    / "hybrid_monitoring_v4_s1_mature_v1"
    / "research_track_v3_cli_entry"
)
FROZEN_EXECUTION_CONTRACT = FROZEN_TRACK_ROOT / "research_execution_freeze.json"
VERSION = "hybrid-v4-s1-v7-evidence-probe-track-v1"
TARGET_SCENARIO = "MATURE_TREND_PULLBACK"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _canonical_jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in rows)


def _eligible(packet: dict[str, Any]) -> bool:
    objective = packet.get("objective_facts") or {}
    route = (objective.get("data_sufficiency_by_route") or {}).get(TARGET_SCENARIO) or {}
    hypotheses = (objective.get("scenario_hypotheses") or {}).get(TARGET_SCENARIO) or []
    comparisons = [
        row
        for row in packet.get("evidence") or []
        if row.get("kind") == "CAUSAL_RELATION_COMPARISON"
        and (row.get("values") or {}).get("scenario") == TARGET_SCENARIO
    ]
    return bool(
        objective.get("builder_version") == "hybrid-v3-atomic-packets-v7"
        and not objective.get("wait_boundary_reasons")
        and route.get("status") is True
        and len(hypotheses) == 1
        and len(comparisons) == 1
        and not (comparisons[0].get("values") or {}).get("missing_components")
        and all(
            hypotheses[0].get(field)
            for field in (
                "anchor_ref",
                "relation_ref",
                "episode_stop_ref",
                "campaign_stop_ref",
            )
        )
    )


def prepare(source: Path, output_dir: Path) -> dict[str, Any]:
    source = Path(source).resolve()
    packets = [packet for packet in _read_jsonl(source) if _eligible(packet)]
    if len(packets) != 1:
        raise RuntimeError(f"expected exactly one V7 evidence probe, found {len(packets)}")
    packet = packets[0]
    output_dir = Path(output_dir).resolve()
    packet_payload = _canonical_jsonl(packets)
    packet_path = output_dir / "packets.jsonl"
    _publish_immutable(packet_path, packet_payload)
    packet_manifest_core = {
        "manifest_version": VERSION,
        "status": "FROZEN_OUTCOME_BLIND_DIAGNOSTIC_ONLY",
        "rows": 1,
        "source_sha256": file_sha256(source),
        "packets_canonical_jsonl_sha256": hashlib.sha256(packet_payload).hexdigest(),
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": packet["input_packet_sha256"],
        "selection_reason": "V7_EXACT_CAUSAL_MATURE_ROUTE_WITH_COMPLETE_RELATION_COMPARISON",
        "selection_is_ai_label": False,
        "selection_is_course_gold": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
    }
    packet_manifest = {
        **packet_manifest_core,
        "manifest_sha256": canonical_sha256(packet_manifest_core),
    }
    packet_manifest_path = output_dir / "packets.manifest.json"
    _publish_immutable(packet_manifest_path, canonical_json_bytes(packet_manifest) + b"\n")

    case_records = build_case_records(
        packets,
        source_manifest_sha256=file_sha256(packet_path),
        protocol_sha256=file_sha256(RESEARCH_PROTOCOL),
        prompt_sha256=file_sha256(PROMPT),
        schema_sha256=file_sha256(SCHEMA),
        execution_contract_sha256=file_sha256(FROZEN_EXECUTION_CONTRACT),
        model=MODEL,
        reasoning_effort="xhigh",
    )
    assignment = write_shards(case_records, output_dir / "cases", shard_count=1)
    manifest_core = {
        "track_version": VERSION,
        "status": "FROZEN_DIAGNOSTIC_ONLY_NOT_FORMAL_ACCEPTANCE",
        "cases": 1,
        "runs": 3,
        "model": MODEL,
        "reasoning_effort": "xhigh",
        "packet_source_sha256": file_sha256(source),
        "packet_block_sha256": file_sha256(packet_path),
        "packet_manifest_sha256": file_sha256(packet_manifest_path),
        "case_shard_sha256": assignment["shards"][0]["sha256"],
        "assignment_manifest_sha256": file_sha256(
            output_dir / "cases/assignment.manifest.json"
        ),
        "execution_contract_path": str(FROZEN_EXECUTION_CONTRACT.resolve()),
        "execution_contract_sha256": file_sha256(FROZEN_EXECUTION_CONTRACT),
        "prompt_sha256": file_sha256(PROMPT),
        "schema_sha256": file_sha256(SCHEMA),
        "protocol_sha256": file_sha256(RESEARCH_PROTOCOL),
        "v4_s1_policy_sha256": file_sha256(
            ROOT / "scripts/hybrid_v4_s1_atomic_policy_v1.py"
        ),
        "v7_packet_builder_sha256": file_sha256(
            ROOT / "scripts/hybrid_v3_atomic_packets_v7.py"
        ),
        "future_or_performance_visible": False,
    }
    manifest = {**manifest_core, "manifest_sha256": canonical_sha256(manifest_core)}
    _publish_immutable(
        output_dir / "probe_manifest.json", canonical_json_bytes(manifest) + b"\n"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
