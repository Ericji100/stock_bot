"""Publish the integrity-complete S1 track and reject the superseded V2 track.

Only the locked anonymous 36-case V1 packet set is read.  No model output,
identity map, future price, or performance artifact is opened by this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from . import hybrid_v3_mature_s1_track_v2 as v2
    from .hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
        write_shards,
    )
except ImportError:  # pragma: no cover
    from scripts import hybrid_v3_mature_s1_track_v2 as v2
    from scripts.hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
        write_shards,
    )


ROOT = Path(__file__).resolve().parents[1]
TRACK_VERSION = "hybrid-v3-mature-s1-research-track-v3-integrity-complete"
FREEZE_VERSION = "hybrid-v3-mature-s1-execution-freeze-v3-integrity-complete"
PACKET_MANIFEST_VERSION = "hybrid-v3-mature-s1-packet-block-v3-integrity-complete"
TRACK_STATUS = v2.TRACK_STATUS
EXPECTED_CASES = v2.EXPECTED_CASES
MODEL = v2.MODEL
REASONING = v2.REASONING
DEFAULT_SOURCE_TRACK = v2.DEFAULT_SOURCE_TRACK
DEFAULT_V2_TRACK = DEFAULT_SOURCE_TRACK.parent / "research_track_v2_integrity"
DEFAULT_OUTPUT = DEFAULT_SOURCE_TRACK.parent / "research_track_v3_integrity_complete"

REQUIRED_COMPONENTS = tuple(
    dict.fromkeys(
        (*v2.REQUIRED_COMPONENTS,
         "config/hybrid_v3_mature_s1_stage_v2.json",
         "scripts/hybrid_v3_mature_s1_track_v3.py",
         "scripts/hybrid_v3_mature_s1_consistency_v3.py")
    )
)


class MatureS1TrackV3Error(ValueError):
    """The V3 integrity freeze cannot be proven complete."""


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatureS1TrackV3Error(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise MatureS1TrackV3Error(f"expected JSON object: {path}")
    return value


def build_component_graph(root: Path = ROOT) -> dict[str, Any]:
    """Pin mandatory anchors plus every reachable repository-local import."""
    root = Path(root).resolve()
    missing = [relative for relative in REQUIRED_COMPONENTS if not (root / relative).is_file()]
    if missing:
        raise MatureS1TrackV3Error(
            "required freeze component missing: " + ", ".join(missing)
        )
    pending = [(root / relative).resolve() for relative in REQUIRED_COMPONENTS]
    seen: set[Path] = set()
    direct: dict[Path, list[Path]] = {}
    while pending:
        path = pending.pop(0)
        if path in seen:
            continue
        seen.add(path)
        dependencies = v2._local_python_imports(path, root) if path.suffix == ".py" else []
        direct[path] = dependencies
        pending.extend(child for child in dependencies if child not in seen)
    nodes = []
    for path in sorted(seen, key=lambda item: v2._relative_to_root(item, root)):
        nodes.append(
            {
                "relative_path": v2._relative_to_root(path, root),
                "kind": "PYTHON" if path.suffix == ".py" else "CONTRACT",
                "status": "FINAL",
                "sha256": file_sha256(path),
                "direct_dependencies": [
                    v2._relative_to_root(child, root) for child in direct[path]
                ],
            }
        )
    core = {
        "graph_version": "hybrid-v3-local-dependency-graph-v2",
        "required_components": list(REQUIRED_COMPONENTS),
        "nodes": nodes,
    }
    return {**core, "graph_sha256": canonical_sha256(core)}


def publish_v2_rejection(v2_track_dir: Path = DEFAULT_V2_TRACK, root: Path = ROOT) -> dict[str, Any]:
    """Immutably record a technical-only rejection without opening AI output."""
    root = Path(root).resolve()
    v2_track_dir = Path(v2_track_dir).resolve()
    json_path = v2_track_dir / "preflight_rejection.json"
    markdown_path = v2_track_dir / "preflight_rejection.md"
    if json_path.is_file():
        existing = _read_json(json_path)
        core = {key: value for key, value in existing.items() if key != "rejection_sha256"}
        if (
            existing.get("rejection_sha256") != canonical_sha256(core)
            or existing.get("status") != "PREFLIGHT_REJECTED_TECHNICAL_ONLY"
            or existing.get("technical_only") is not True
            or existing.get("ai_executed") is not False
            or existing.get("performance_remains_sealed") is not True
            or existing.get("strategy_or_gate_change") is not False
            or not markdown_path.is_file()
        ):
            raise MatureS1TrackV3Error("existing V2 preflight rejection is invalid")
        return existing
    track_path = v2_track_dir / "research_track_manifest.json"
    freeze_path = v2_track_dir / "research_execution_freeze.json"
    old_track = _read_json(track_path)
    old_freeze = _read_json(freeze_path)
    mismatches = []
    graph = old_freeze.get("component_graph") or {}
    for node in graph.get("nodes") or []:
        relative = str(node.get("relative_path") or "")
        path = root / relative
        actual = file_sha256(path) if path.is_file() else None
        if actual != node.get("sha256"):
            mismatches.append(
                {"relative_path": relative, "frozen_sha256": node.get("sha256"), "actual_sha256": actual}
            )
    rejection_core = {
        "rejection_version": "hybrid-v3-mature-s1-v2-preflight-rejection-v1",
        "status": "PREFLIGHT_REJECTED_TECHNICAL_ONLY",
        "track_version": old_track.get("track_version"),
        "track_manifest_sha256": file_sha256(track_path),
        "execution_freeze_sha256": file_sha256(freeze_path),
        "reason_codes": [
            "SUPERSEDED_BEFORE_AI_EXECUTION",
            "INTEGRITY_GRAPH_OR_NATIVE_EVALUATOR_CONTRACT_NOT_FINAL",
            "REBUILD_AS_V3_INTEGRITY_COMPLETE",
        ],
        "component_mismatches_at_rejection": mismatches,
        "technical_only": True,
        "ai_executed": False,
        "performance_unsealed": False,
        "performance_remains_sealed": True,
        "strategy_or_gate_change": False,
        "v1_v2_v3_rules_unchanged": True,
    }
    rejection = {**rejection_core, "rejection_sha256": canonical_sha256(rejection_core)}
    _publish_immutable(json_path, canonical_json_bytes(rejection) + b"\n")
    lines = [
        "# MATURE S1 research track V2 preflight rejection",
        "",
        "- Status: `PREFLIGHT_REJECTED_TECHNICAL_ONLY`",
        "- AI executed: `false`",
        "- Performance remains sealed: `true`",
        "- Strategy or gate change: `false`",
        "- Successor: `research_track_v3_integrity_complete`",
        "",
        f"Rejection SHA-256: `{rejection['rejection_sha256']}`",
        "",
    ]
    _publish_immutable(
        markdown_path,
        "\n".join(lines).encode("utf-8"),
    )
    return rejection


def _compatibility_rows(graph: Mapping[str, Any], names: Mapping[str, str]) -> list[dict[str, Any]]:
    by_path = {str(row["relative_path"]): row for row in graph.get("nodes") or []}
    rows = []
    for name, relative in names.items():
        node = by_path.get(relative)
        if node is None:
            raise MatureS1TrackV3Error(f"component graph lost required node: {relative}")
        rows.append(
            {"name": name, "relative_path": relative, "status": "FINAL", "sha256": node["sha256"]}
        )
    return rows


def prepare(
    *,
    source_track_dir: Path = DEFAULT_SOURCE_TRACK,
    v2_track_dir: Path = DEFAULT_V2_TRACK,
    output_dir: Path = DEFAULT_OUTPUT,
    root: Path = ROOT,
    shard_count: int = 4,
) -> dict[str, Any]:
    root = Path(root).resolve()
    output_dir = Path(output_dir).resolve()
    packets, provenance, selection = v2._locked_source(source_track_dir, root)
    rejection = publish_v2_rejection(v2_track_dir, root)
    rejection_path = Path(v2_track_dir).resolve() / "preflight_rejection.json"
    graph = build_component_graph(root)

    packet_payload = _jsonl_bytes(packets)
    packet_path = output_dir / "primary_packets.jsonl"
    _publish_immutable(packet_path, packet_payload)
    selection_path = output_dir / "selection_plan.json"
    _publish_immutable(selection_path, canonical_json_bytes(selection) + b"\n")
    selection_by_review = {str(row["review_id"]): row for row in selection["rows"]}
    mapping = []
    for ordinal, packet in enumerate(packets):
        selected = selection_by_review[str(packet["review_id"])]
        mapping.append(
            {
                "selected_ordinal": ordinal,
                "source_ordinal": selected.get("source_ordinal"),
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "packet_sha256": canonical_sha256(packet),
                "sampling_focus": selected["sampling_focus"],
                "eligible_stage_focuses": selected["eligible_stage_focuses"],
            }
        )
    packet_manifest_core = {
        "manifest_version": PACKET_MANIFEST_VERSION,
        "status": "LOCKED_OUTCOME_BLIND",
        "block_id": "PRIMARY_MATURE_S1_INTEGRITY_COMPLETE_V3",
        "rows": EXPECTED_CASES,
        "packets_canonical_jsonl_sha256": hashlib.sha256(packet_payload).hexdigest(),
        "sampling_metadata_inside_packet": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
        "ai_output_read_by_builder": False,
        "selection_plan_path": str(selection_path),
        "selection_plan_sha256": file_sha256(selection_path),
        "mapping": mapping,
        "mapping_sha256": canonical_sha256(mapping),
        "source_track_v1": provenance,
    }
    packet_manifest = {
        **packet_manifest_core,
        "manifest_sha256": canonical_sha256(packet_manifest_core),
    }
    packet_manifest_path = output_dir / "primary_packets.manifest.json"
    _publish_immutable(packet_manifest_path, canonical_json_bytes(packet_manifest) + b"\n")

    v2_components = _compatibility_rows(
        graph,
        {
            "launcher": "scripts/hybrid_v3_codex_launcher_v2.py",
            "reviewer": "scripts/hybrid_v3_codex_reviewer_v3.py",
            "runner": "scripts/hybrid_v3_atomic_runner_v2.py",
            "policy": "scripts/hybrid_v3_atomic_policy_v3.py",
            "prompt": "config/hybrid_semantic_prompt_v3.md",
            "schema": "config/hybrid_atomic_semantics_v2.schema.json",
            "protocol": "config/hybrid_monitoring_research_protocol_v1.json",
        },
    )
    stage_components = _compatibility_rows(
        graph,
        {
            "track_v1": "scripts/hybrid_v3_mature_s1_track_v1.py",
            "track_v2": "scripts/hybrid_v3_mature_s1_track_v2.py",
            "track_v3": "scripts/hybrid_v3_mature_s1_track_v3.py",
            "consistency_v1": "scripts/hybrid_v3_mature_s1_consistency_v1.py",
            "consistency_v2": "scripts/hybrid_v3_mature_s1_consistency_v2.py",
            "consistency_v3": "scripts/hybrid_v3_mature_s1_consistency_v3.py",
            "sharding": "scripts/hybrid_v3_sharding_v2.py",
            "triplicate_audit": "scripts/hybrid_v3_triplicate_smoke_audit.py",
            "stage_protocol": "config/hybrid_v3_mature_s1_stage_v2.json",
        },
    )
    stage_path = root / "config/hybrid_v3_mature_s1_stage_v2.json"
    freeze_core = {
        "freeze_version": FREEZE_VERSION,
        "status": "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION",
        "classification": "OUTCOME_BLIND_MATURE_S1_REPEATABILITY_ONLY",
        "single_route_scope": {
            "primary_scenario": "MATURE_TREND_PULLBACK",
            "trade_route": "V2_CORE",
            "other_scenarios_are_negative_controls": True,
            "other_routes_suspended": True,
        },
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "old_ai_output_visible": False,
        "strategy_or_gate_change": False,
        "source_case_count": EXPECTED_CASES,
        "source_packet_sha256": file_sha256(packet_path),
        "source_packet_manifest_sha256": file_sha256(packet_manifest_path),
        "selection_plan_sha256": file_sha256(selection_path),
        "stage_protocol_sha256": file_sha256(stage_path),
        "rejected_v2_preflight": {
            "path": str(rejection_path),
            "sha256": file_sha256(rejection_path),
            "rejection_sha256": rejection["rejection_sha256"],
        },
        "source_track_v1": provenance,
        "v2_components": v2_components,
        "stage_components": stage_components,
        "component_graph": graph,
        "execution_contract": {"model": MODEL, "reasoning_effort": REASONING},
    }
    freeze = {**freeze_core, "freeze_sha256": canonical_sha256(freeze_core)}
    freeze_path = output_dir / "research_execution_freeze.json"
    _publish_immutable(freeze_path, canonical_json_bytes(freeze) + b"\n")

    records = build_case_records(
        packets,
        source_manifest_sha256=file_sha256(packet_manifest_path),
        protocol_sha256=file_sha256(root / "config/hybrid_monitoring_research_protocol_v1.json"),
        prompt_sha256=file_sha256(root / "config/hybrid_semantic_prompt_v3.md"),
        schema_sha256=file_sha256(root / "config/hybrid_atomic_semantics_v2.schema.json"),
        execution_contract_sha256=file_sha256(freeze_path),
        model=MODEL,
        reasoning_effort=REASONING,
    )
    new_keys = sorted(str(row["case_key"]) for row in records)
    prior_keys = set(v2._parent_case_keys(source_track_dir))
    if prior_keys.intersection(new_keys):
        raise MatureS1TrackV3Error("V3 did not produce new case keys")
    assignment = write_shards(records, output_dir / "primary_cases", shard_count=shard_count)
    assignment_path = output_dir / "primary_cases/assignment.manifest.json"
    track_core = {
        "track_version": TRACK_VERSION,
        "status": TRACK_STATUS,
        "classification": "OUTCOME_BLIND_MATURE_S1_REPEATABILITY_ONLY",
        "stage": "MATURE_TREND_PULLBACK_V2_CORE_ONLY",
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "ai_output_read_by_builder": False,
        "performance_must_remain_sealed_until_prior_gates_pass": True,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "cases": EXPECTED_CASES,
        "runs": 3,
        "source_track_v1": provenance,
        "rejected_v2_preflight_path": str(rejection_path),
        "rejected_v2_preflight_sha256": file_sha256(rejection_path),
        "packet_path": str(packet_path),
        "packet_sha256": file_sha256(packet_path),
        "packet_manifest_path": str(packet_manifest_path),
        "packet_manifest_sha256": file_sha256(packet_manifest_path),
        "selection_plan_path": str(selection_path),
        "selection_plan_sha256": file_sha256(selection_path),
        "execution_freeze_path": str(freeze_path),
        "execution_freeze_sha256": file_sha256(freeze_path),
        "stage_protocol_path": str(stage_path.resolve()),
        "stage_protocol_sha256": file_sha256(stage_path),
        "component_graph_sha256": graph["graph_sha256"],
        "assignment_manifest_path": str(assignment_path),
        "assignment_manifest_sha256": file_sha256(assignment_path),
        "assignment_sha256": assignment["assignment_sha256"],
        "case_keys_sha256": canonical_sha256(new_keys),
        "case_keys_are_new": True,
        "strategy_or_gate_change": False,
    }
    track = {**track_core, "track_manifest_sha256": canonical_sha256(track_core)}
    _publish_immutable(
        output_dir / "research_track_manifest.json",
        canonical_json_bytes(track) + b"\n",
    )
    return track


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-track-dir", type=Path, default=DEFAULT_SOURCE_TRACK)
    parser.add_argument("--v2-track-dir", type=Path, default=DEFAULT_V2_TRACK)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--shard-count", type=int, default=4)
    args = parser.parse_args()
    result = prepare(
        source_track_dir=args.source_track_dir,
        v2_track_dir=args.v2_track_dir,
        output_dir=args.output_dir,
        shard_count=args.shard_count,
    )
    print(json.dumps({"status": result["status"], "track_version": result["track_version"], "track_manifest_sha256": result["track_manifest_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
