"""Prepare the Candidate4/V5 outcome-blind Hybrid V3 research track.

This track supersedes research-track v2 only because the packet/validator
contract was repaired.  It preserves the V3 reducer, gates, prompt, schema,
model, and reasoning effort.  No identity, future bar, or performance is read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

try:
    from . import hybrid_v3_research_track_v1 as v1
    from . import hybrid_v3_research_track_v2 as v2
    from .hybrid_v3_course_gold_v4 import (
        extract_selected_records,
        validate_candidate3_universe_manifest,
        validate_holdout_plan,
    )
    from .hybrid_v3_sharding_v2 import canonical_sha256, file_sha256
except ImportError:  # pragma: no cover - direct script execution
    from scripts import hybrid_v3_research_track_v1 as v1
    from scripts import hybrid_v3_research_track_v2 as v2
    from scripts.hybrid_v3_course_gold_v4 import (
        extract_selected_records,
        validate_candidate3_universe_manifest,
        validate_holdout_plan,
    )
    from scripts.hybrid_v3_sharding_v2 import canonical_sha256, file_sha256


ROOT = Path(__file__).resolve().parents[1]
TRACK_VERSION = "hybrid-v3-research-track-v3-candidate4"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_RESEARCH_ONLY"
MODEL = v1.MODEL
REASONING_EFFORT = v1.REASONING_EFFORT
PROMPT = ROOT / "config/hybrid_semantic_prompt_v3.md"
SCHEMA = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"
PROTOCOL = ROOT / "config/hybrid_monitoring_research_protocol_v1.json"
POLICY = ROOT / "scripts/hybrid_v3_atomic_policy_v3.py"
REVIEWER = ROOT / "scripts/hybrid_v3_codex_reviewer_v2.py"
RUNNER = ROOT / "scripts/hybrid_v3_atomic_runner_v2.py"
LAUNCHER = ROOT / "scripts/hybrid_v3_codex_launcher_v1.py"


def _read_json(path: Path) -> dict[str, Any]:
    return v1._read_json(path)


def _validate_preflight(path: Path) -> dict[str, Any]:
    report = _read_json(path)
    if report.get("status") != "PASS" or report.get("invalid_records") != 0:
        raise ValueError("Candidate4 source packet preflight did not pass")
    if report.get("identity_visible") is not False:
        raise ValueError("Candidate4 packet preflight exposed identity")
    if report.get("future_performance_visible") is not False:
        raise ValueError("Candidate4 packet preflight exposed future/performance")
    return report


def _validate_diff(path: Path) -> dict[str, Any]:
    report = _read_json(path)
    if report.get("status") != "PASS":
        raise ValueError("Candidate3/Candidate4 differential audit did not pass")
    if report.get("performance_or_future_fields_read") is not False:
        raise ValueError("Candidate3/Candidate4 differential audit read outcome data")
    return report


def build_execution_contract(
    *,
    candidate_manifest_path: Path,
    holdout_plan_path: Path,
    diff_audit_path: Path,
    preflight_path: Path,
    superseded_execution_path: Path,
) -> dict[str, Any]:
    v2._validate_prompt_delta()
    protocol = _read_json(PROTOCOL)
    if protocol.get("status") != "FINAL_RESEARCH_LOCKED":
        raise ValueError("research protocol is not locked")
    diff = _validate_diff(diff_audit_path)
    preflight = _validate_preflight(preflight_path)
    candidate = validate_candidate3_universe_manifest(_read_json(candidate_manifest_path))
    holdout = _read_json(holdout_plan_path)
    if holdout.get("status") != "LOCKED" or holdout.get("block_count") != 3:
        raise ValueError("Candidate4 holdout is not locked")
    if not superseded_execution_path.is_file():
        raise ValueError("superseded v2 execution freeze is missing")
    components = [
        v1._component("launcher", LAUNCHER),
        v1._component("reviewer", REVIEWER),
        v1._component("runner", RUNNER),
        v1._component("policy", POLICY),
        v1._component("prompt", PROMPT),
        v1._component("schema", SCHEMA),
        v1._component("protocol", PROTOCOL),
    ]
    return {
        "freeze_version": "hybrid-v3-research-execution-freeze-v3-candidate4",
        "status": v1.EXECUTION_FREEZE_STATUS,
        "track_version": TRACK_VERSION,
        "track_status": TRACK_STATUS,
        "classification": "RESEARCH_BACKTEST_NOT_COURSE_VALIDATED",
        "supersedes_execution_freeze_sha256": file_sha256(superseded_execution_path),
        "supersession_reason": [
            "EXCLUDE_COMPARISON_LEGS_WITH_FEWER_THAN_TWO_VISIBLE_BARS",
            "ALLOW_REVERSAL_PROBE_ONLY_FOR_BEAR_REVERSAL_LEFT_RIGHT",
            "PIN_VERSIONED_FORMAL_LAUNCHER",
        ],
        "contract_repair_only": True,
        "strategy_or_gate_change": False,
        "course_fidelity_claim_allowed": False,
        "human_course_gold_status": "DEFERRED_BY_USER_FOR_INITIAL_PERFORMANCE_CHECK",
        "candidate4_review_point_manifest_sha256": file_sha256(candidate_manifest_path),
        "candidate4_review_points_artifact_sha256": candidate["artifact_sha256"],
        "candidate4_holdout_plan_sha256": file_sha256(holdout_plan_path),
        "candidate3_candidate4_diff_audit_sha256": file_sha256(diff_audit_path),
        "candidate4_packet_preflight_sha256": file_sha256(preflight_path),
        "candidate4_packet_preflight_report_sha256": preflight.get("report_sha256"),
        "diff_audit_report_sha256": diff.get("report_sha256"),
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "old_ai_output_visible": False,
        "v3_strategy_rules_unchanged": True,
        "v2_components": components,
        "execution_contract": {"model": MODEL, "reasoning_effort": REASONING_EFFORT},
    }


def _block_artifacts(
    block: Mapping[str, Any], selected_records: Mapping[str, Mapping[str, Any]]
) -> tuple[bytes, dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    mapping: list[dict[str, Any]] = []
    for selected_ordinal, selected in enumerate(block["rows"]):
        review_id = str(selected["review_id"])
        source = selected_records[review_id]
        packet = dict(source["packet"])
        packets.append(packet)
        mapping.append(
            {
                "selected_ordinal": selected_ordinal,
                "candidate4_source_ordinal": int(source["source_ordinal"]),
                "review_id": review_id,
                "anonymous_stock_id": source["anonymous_stock_id"],
                "packet_sha256": source["packet_sha256"],
                "sampling_focus": selected["sampling_focus"],
                "eligible_sampling_strata": selected["eligible_sampling_strata"],
            }
        )
    packet_payload = v1._jsonl_bytes(packets)
    core = {
        "manifest_version": "hybrid-v3-research-packet-block-v2-candidate4",
        "status": "LOCKED_OUTCOME_BLIND",
        "block_id": block["block_id"],
        "rows": len(packets),
        "sampling_metadata_inside_packet": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
        "packets_canonical_jsonl_sha256": hashlib.sha256(packet_payload).hexdigest(),
        "mapping": mapping,
        "mapping_sha256": canonical_sha256(mapping),
    }
    return packet_payload, {**core, "manifest_sha256": canonical_sha256(core)}


def prepare(
    *,
    source_path: Path,
    source_manifest_path: Path,
    holdout_plan_path: Path,
    diff_audit_path: Path,
    preflight_path: Path,
    superseded_execution_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    candidate = validate_candidate3_universe_manifest(_read_json(source_manifest_path))
    holdout = validate_holdout_plan(
        _read_json(holdout_plan_path),
        source_manifest=candidate,
        source_path=source_path,
        source_manifest_path=source_manifest_path,
    )
    selected = extract_selected_records(source_path, holdout)
    execution = build_execution_contract(
        candidate_manifest_path=source_manifest_path,
        holdout_plan_path=holdout_plan_path,
        diff_audit_path=diff_audit_path,
        preflight_path=preflight_path,
        superseded_execution_path=superseded_execution_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    execution_path = output_dir / "research_execution_freeze.json"
    v1._publish(
        execution_path,
        json.dumps(execution, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )
    blocks: list[dict[str, Any]] = []
    for block in [holdout["primary"], *holdout["reserve_blocks"]]:
        name = str(block["block_id"]).lower()
        packet_path = output_dir / f"{name}_packets.jsonl"
        manifest_path = output_dir / f"{name}_packets.manifest.json"
        payload, manifest = _block_artifacts(block, selected)
        v1._publish(packet_path, payload)
        v1._publish(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )
        blocks.append(
            {
                "block_id": block["block_id"],
                "packet_path": str(packet_path.resolve()),
                "packet_sha256": file_sha256(packet_path),
                "manifest_path": str(manifest_path.resolve()),
                "manifest_sha256": file_sha256(manifest_path),
                "rows": manifest["rows"],
            }
        )
    core = {
        "track_version": TRACK_VERSION,
        "status": TRACK_STATUS,
        "classification": "RESEARCH_BACKTEST_NOT_COURSE_VALIDATED",
        "course_fidelity_claim_allowed": False,
        "outcome_blind": True,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "execution_freeze_path": str(execution_path.resolve()),
        "execution_freeze_sha256": file_sha256(execution_path),
        "candidate4_source_manifest_sha256": file_sha256(source_manifest_path),
        "candidate4_holdout_plan_sha256": file_sha256(holdout_plan_path),
        "blocks": blocks,
    }
    track = {**core, "track_manifest_sha256": canonical_sha256(core)}
    v1._publish(
        output_dir / "research_track_manifest.json",
        json.dumps(track, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )
    return track


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--holdout-plan", type=Path, required=True)
    parser.add_argument("--diff-audit", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--superseded-execution", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(
        source_path=args.source.resolve(),
        source_manifest_path=args.source_manifest.resolve(),
        holdout_plan_path=args.holdout_plan.resolve(),
        diff_audit_path=args.diff_audit.resolve(),
        preflight_path=args.preflight.resolve(),
        superseded_execution_path=args.superseded_execution.resolve(),
        output_dir=args.output_dir.resolve(),
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "track_version": result["track_version"],
                "blocks": {row["block_id"]: row["rows"] for row in result["blocks"]},
                "execution_freeze_sha256": result["execution_freeze_sha256"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
