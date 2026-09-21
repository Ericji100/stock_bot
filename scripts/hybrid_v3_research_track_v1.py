"""Prepare the frozen Candidate3 repeatability research track.

This preparation is outcome-blind and does not call a model.  It pins the
exact semantic execution environment, extracts the locked primary and reserve
packets without sampling metadata, and records their original Candidate3
source ordinals.  Human course-gold is intentionally deferred by the user;
therefore this track may support only research-performance claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from .hybrid_v3_course_gold_v4 import (
        extract_selected_records,
        validate_candidate3_universe_manifest,
        validate_holdout_plan,
    )
    from .hybrid_v3_sharding_v2 import canonical_json_bytes, canonical_sha256, file_sha256
except ImportError:
    from hybrid_v3_course_gold_v4 import (
        extract_selected_records,
        validate_candidate3_universe_manifest,
        validate_holdout_plan,
    )
    from hybrid_v3_sharding_v2 import canonical_json_bytes, canonical_sha256, file_sha256


TRACK_VERSION = "hybrid-v3-research-track-v1"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_RESEARCH_ONLY"
EXECUTION_FREEZE_STATUS = "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION"
MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "xhigh"


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = ROOT / "config/hybrid_monitoring_research_protocol_v1.json"
DEFAULT_PROMPT = ROOT / "config/hybrid_semantic_prompt_v2.md"
DEFAULT_SCHEMA = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"
DEFAULT_POLICY = ROOT / "scripts/hybrid_v3_atomic_policy_v2.py"
DEFAULT_REVIEWER = ROOT / "scripts/hybrid_v3_codex_reviewer_v2.py"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _publish(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite frozen research artifact: {path}")
        return
    path.write_bytes(payload)


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _component(name: str, path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "name": name,
        "relative_path": resolved.relative_to(ROOT.resolve()).as_posix(),
        "status": "FINAL",
        "sha256": file_sha256(resolved),
    }


def build_execution_contract(
    *,
    candidate_manifest_path: Path,
    holdout_plan_path: Path,
    diff_audit_path: Path,
    protocol_path: Path = DEFAULT_PROTOCOL,
    prompt_path: Path = DEFAULT_PROMPT,
    schema_path: Path = DEFAULT_SCHEMA,
    policy_path: Path = DEFAULT_POLICY,
    reviewer_path: Path = DEFAULT_REVIEWER,
) -> dict[str, Any]:
    protocol = _read_json(protocol_path)
    if protocol.get("status") != "FINAL_RESEARCH_LOCKED":
        raise ValueError("research protocol is not FINAL_RESEARCH_LOCKED")
    diff = _read_json(diff_audit_path)
    if diff.get("status") != "PASS" or diff.get("performance_or_future_fields_read") is not False:
        raise ValueError("Candidate2/Candidate3 outcome-blind differential audit did not pass")
    candidate = validate_candidate3_universe_manifest(_read_json(candidate_manifest_path))
    holdout = _read_json(holdout_plan_path)
    if holdout.get("status") != "LOCKED" or holdout.get("block_count") != 3:
        raise ValueError("Candidate3 primary/reserve plan is not locked")
    components = [
        _component("reviewer", reviewer_path),
        _component("policy", policy_path),
        _component("prompt", prompt_path),
        _component("schema", schema_path),
        _component("protocol", protocol_path),
    ]
    return {
        "freeze_version": "hybrid-v3-research-execution-freeze-v1",
        "status": EXECUTION_FREEZE_STATUS,
        "track_version": TRACK_VERSION,
        "track_status": TRACK_STATUS,
        "classification": "RESEARCH_BACKTEST_NOT_COURSE_VALIDATED",
        "course_fidelity_claim_allowed": False,
        "human_course_gold_status": "DEFERRED_BY_USER_FOR_INITIAL_PERFORMANCE_CHECK",
        "candidate3_review_point_manifest_sha256": file_sha256(candidate_manifest_path),
        "candidate3_review_points_artifact_sha256": candidate["artifact_sha256"],
        "candidate3_holdout_plan_sha256": file_sha256(holdout_plan_path),
        "candidate3_diff_audit_sha256": file_sha256(diff_audit_path),
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "old_ai_output_visible": False,
        "v3_strategy_rules_unchanged": True,
        "v2_components": components,
        "execution_contract": {
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
        },
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
                "candidate3_source_ordinal": int(source["source_ordinal"]),
                "review_id": review_id,
                "anonymous_stock_id": source["anonymous_stock_id"],
                "packet_sha256": source["packet_sha256"],
                "sampling_focus": selected["sampling_focus"],
                "eligible_sampling_strata": selected["eligible_sampling_strata"],
            }
        )
    packet_payload = _jsonl_bytes(packets)
    manifest_core = {
        "manifest_version": "hybrid-v3-research-packet-block-v1",
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
    return packet_payload, {**manifest_core, "manifest_sha256": canonical_sha256(manifest_core)}


def prepare(
    *,
    source_path: Path,
    source_manifest_path: Path,
    holdout_plan_path: Path,
    diff_audit_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    candidate_manifest = validate_candidate3_universe_manifest(_read_json(source_manifest_path))
    holdout = validate_holdout_plan(
        _read_json(holdout_plan_path),
        source_manifest=candidate_manifest,
        source_path=source_path,
        source_manifest_path=source_manifest_path,
    )
    selected_records = extract_selected_records(source_path, holdout)
    execution_contract = build_execution_contract(
        candidate_manifest_path=source_manifest_path,
        holdout_plan_path=holdout_plan_path,
        diff_audit_path=diff_audit_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    execution_path = output_dir / "research_execution_freeze.json"
    _publish(execution_path, json.dumps(execution_contract, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")

    block_entries: list[dict[str, Any]] = []
    for block in [holdout["primary"], *holdout["reserve_blocks"]]:
        safe_name = str(block["block_id"]).lower()
        packet_path = output_dir / f"{safe_name}_packets.jsonl"
        manifest_path = output_dir / f"{safe_name}_packets.manifest.json"
        packet_payload, manifest = _block_artifacts(block, selected_records)
        _publish(packet_path, packet_payload)
        _publish(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        block_entries.append(
            {
                "block_id": block["block_id"],
                "packet_path": str(packet_path.resolve()),
                "packet_sha256": file_sha256(packet_path),
                "manifest_path": str(manifest_path.resolve()),
                "manifest_sha256": file_sha256(manifest_path),
                "rows": manifest["rows"],
            }
        )
    track_core = {
        "track_version": TRACK_VERSION,
        "status": TRACK_STATUS,
        "classification": "RESEARCH_BACKTEST_NOT_COURSE_VALIDATED",
        "course_fidelity_claim_allowed": False,
        "outcome_blind": True,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "execution_freeze_path": str(execution_path.resolve()),
        "execution_freeze_sha256": file_sha256(execution_path),
        "candidate3_source_manifest_sha256": file_sha256(source_manifest_path),
        "candidate3_holdout_plan_sha256": file_sha256(holdout_plan_path),
        "blocks": block_entries,
    }
    track = {**track_core, "track_manifest_sha256": canonical_sha256(track_core)}
    _publish(
        output_dir / "research_track_manifest.json",
        json.dumps(track, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )
    return track


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--holdout-plan", type=Path, required=True)
    parser.add_argument("--diff-audit", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(
        source_path=args.source.resolve(),
        source_manifest_path=args.source_manifest.resolve(),
        holdout_plan_path=args.holdout_plan.resolve(),
        diff_audit_path=args.diff_audit.resolve(),
        output_dir=args.output_dir.resolve(),
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "blocks": {block["block_id"]: block["rows"] for block in result["blocks"]},
                "execution_freeze_sha256": result["execution_freeze_sha256"],
                "track_manifest_sha256": result["track_manifest_sha256"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
