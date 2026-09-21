"""Build an outcome-blind V4-S1 diagnostic for exclusive mature candidates.

This is deliberately a diagnostic track, not a replacement for the frozen
36-case repeatability track.  It selects only rows whose validation-only focus
is exactly ``MATURE_SYMBOLIC_REACHABLE``.  Sampling labels and source identity
metadata never enter the AI packet.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
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
from .hybrid_v4_s1_track_v1 import (
    MODEL,
    PROMPT,
    REACHABILITY_PREFLIGHT,
    RESEARCH_PROTOCOL,
    SCHEMA,
    SOURCE,
    SOURCE_MANIFEST,
    extract_packets,
    scan_source,
)


ROOT = Path(__file__).resolve().parents[1]
FROZEN_TRACK_ROOT = (
    ROOT
    / "reports/course_backtest/2024-02-02"
    / "historical_scan_2023h2_formal_ai_v3_daily_scan"
    / "hybrid_monitoring_v4_s1_mature_v1"
    / "research_track_v3_cli_entry"
)
FROZEN_EXECUTION_CONTRACT = FROZEN_TRACK_ROOT / "research_execution_freeze.json"
TARGET_FOCUS = "MATURE_SYMBOLIC_REACHABLE"
VERSION = "hybrid-v4-s1-clean-positive-diagnostic-v1"


def _canonical_jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in rows)


def prepare(output_dir: Path) -> dict[str, Any]:
    records, source_manifest, reachable = scan_source(
        SOURCE, SOURCE_MANIFEST, REACHABILITY_PREFLIGHT
    )
    selected = [
        deepcopy(row)
        for row in records
        if set(row["eligible_stage_focuses"]) == {TARGET_FOCUS}
    ]
    selected.sort(key=lambda row: int(row["source_ordinal"]))
    if len(selected) != 2:
        raise RuntimeError(
            f"exclusive mature capacity changed: expected 2, found {len(selected)}"
        )
    if len({row["anonymous_stock_id"] for row in selected}) != len(selected):
        raise RuntimeError("exclusive mature diagnostic repeats an anonymous stock")

    packets = extract_packets(SOURCE, selected)
    output_dir = Path(output_dir).resolve()
    plan_core = {
        "diagnostic_version": VERSION,
        "status": "LOCKED_OUTCOME_BLIND_DIAGNOSTIC_ONLY",
        "formal_acceptance_claim_allowed": False,
        "purpose": "DISTINGUISH_SAMPLE_DESIGN_FAILURE_FROM_AI_OR_POLICY_CONSERVATISM",
        "target_focus": TARGET_FOCUS,
        "exclusive_focus_required": True,
        "cases": len(selected),
        "runs": 3,
        "source_rows": int(source_manifest["review_points"]),
        "preflight_reachable_capacity": len(reachable),
        "source_artifact_sha256": file_sha256(SOURCE),
        "source_manifest_sha256": file_sha256(SOURCE_MANIFEST),
        "reachability_preflight_sha256": file_sha256(REACHABILITY_PREFLIGHT),
        "frozen_v4_s1_execution_contract_sha256": file_sha256(
            FROZEN_EXECUTION_CONTRACT
        ),
        "model": MODEL,
        "reasoning_effort": "xhigh",
        "identity_visible_inside_ai_packet": False,
        "future_or_performance_visible": False,
        "sampling_metadata_inside_ai_packet": False,
        "rows": selected,
        "rows_sha256": canonical_sha256(selected),
    }
    plan = {**plan_core, "diagnostic_sha256": canonical_sha256(plan_core)}
    plan_path = output_dir / "selection_plan.json"
    _publish_immutable(plan_path, canonical_json_bytes(plan) + b"\n")

    packet_payload = _canonical_jsonl(packets)
    packet_path = output_dir / "packets.jsonl"
    _publish_immutable(packet_path, packet_payload)
    mapping = [
        {
            "diagnostic_ordinal": index,
            "source_ordinal": int(row["source_ordinal"]),
            "review_id": row["review_id"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "packet_sha256": row["packet_sha256"],
        }
        for index, row in enumerate(selected)
    ]
    packet_manifest_core = {
        "manifest_version": VERSION,
        "status": "LOCKED_OUTCOME_BLIND_DIAGNOSTIC_ONLY",
        "rows": len(packets),
        "packets_canonical_jsonl_sha256": hashlib.sha256(packet_payload).hexdigest(),
        "mapping": mapping,
        "mapping_sha256": canonical_sha256(mapping),
        "sampling_metadata_inside_packet": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
    }
    packet_manifest = {
        **packet_manifest_core,
        "manifest_sha256": canonical_sha256(packet_manifest_core),
    }
    packet_manifest_path = output_dir / "packets.manifest.json"
    _publish_immutable(
        packet_manifest_path, canonical_json_bytes(packet_manifest) + b"\n"
    )

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
        "diagnostic_version": VERSION,
        "status": "FROZEN_DIAGNOSTIC_ONLY_NOT_FORMAL_ACCEPTANCE",
        "cases": len(case_records),
        "runs": 3,
        "model": MODEL,
        "reasoning_effort": "xhigh",
        "selection_plan_sha256": file_sha256(plan_path),
        "packet_sha256": file_sha256(packet_path),
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
        "future_or_performance_visible": False,
    }
    manifest = {**manifest_core, "manifest_sha256": canonical_sha256(manifest_core)}
    manifest_path = output_dir / "diagnostic_manifest.json"
    _publish_immutable(manifest_path, canonical_json_bytes(manifest) + b"\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
