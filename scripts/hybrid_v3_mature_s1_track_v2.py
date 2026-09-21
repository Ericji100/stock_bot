"""Build the integrity-complete MATURE_TREND_PULLBACK stage-1 track.

The source is the immutable 36-case ``research_track_v1`` packet set.  This
builder never looks for model outputs, stock identities, future prices, or
performance.  It creates a new packet manifest, execution freeze, case keys,
and deterministic shards whose freeze contains the complete repository-local
Python dependency graph used by the S1 repeatability path.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
        write_shards,
    )
except ImportError:  # pragma: no cover - direct script execution
    from scripts.hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
        write_shards,
    )


ROOT = Path(__file__).resolve().parents[1]
TRACK_VERSION = "hybrid-v3-mature-s1-research-track-v2-integrity"
FREEZE_VERSION = "hybrid-v3-mature-s1-execution-freeze-v2-integrity"
PACKET_MANIFEST_VERSION = "hybrid-v3-mature-s1-packet-block-v2-integrity"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_SINGLE_ROUTE_RESEARCH_ONLY"
EXPECTED_CASES = 36
MODEL = "gpt-5.6-sol"
REASONING = "xhigh"

DEFAULT_SOURCE_TRACK = ROOT / (
    "reports/course_backtest/2024-02-02/"
    "historical_scan_2023h2_formal_ai_v3_daily_scan/"
    "hybrid_monitoring_v3_s1_mature_v1/research_track_v1"
)
DEFAULT_OUTPUT = DEFAULT_SOURCE_TRACK.parent / "research_track_v2_integrity"

# These anchors are mandatory even when an AST edge is refactored away.  The
# recursive graph walker adds every repository-local Python import reachable
# from them, which closes the indirect reviewer/policy/merge dependency gap.
REQUIRED_COMPONENTS = (
    "scripts/hybrid_v3_mature_s1_track_v1.py",
    "scripts/hybrid_v3_mature_s1_track_v2.py",
    "scripts/hybrid_v3_mature_s1_consistency_v1.py",
    "scripts/hybrid_v3_mature_s1_consistency_v2.py",
    "scripts/hybrid_v3_atomic_runner_v2.py",
    "scripts/hybrid_v3_sharding_v2.py",
    "scripts/hybrid_v3_codex_reviewer_v3.py",
    "scripts/hybrid_v3_codex_reviewer_v2.py",
    "scripts/hybrid_v3_atomic_policy_v3.py",
    "scripts/hybrid_v3_atomic_policy_v2.py",
    "scripts/hybrid_v3_atomic_packets_v2.py",
    "scripts/hybrid_v3_conservative_merge_v1.py",
    "scripts/hybrid_v3_consistency_v2.py",
    "scripts/hybrid_v3_consistency_v3.py",
    "scripts/hybrid_v3_triplicate_smoke_audit.py",
    "scripts/hybrid_v3_codex_launcher_v2.py",
    "config/hybrid_semantic_prompt_v3.md",
    "config/hybrid_atomic_semantics_v2.schema.json",
    "config/hybrid_monitoring_research_protocol_v1.json",
    "config/hybrid_v3_mature_s1_stage_v2.json",
)


class MatureS1TrackV2Error(ValueError):
    """The locked source or integrity graph is incomplete or changed."""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise MatureS1TrackV2Error(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise MatureS1TrackV2Error(
                    f"expected JSON object at {path}:{line_number}"
                )
            rows.append(value)
    return rows


def _canonical_jsonl(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _self_hash(value: Mapping[str, Any], field: str) -> bool:
    supplied = value.get(field)
    core = {key: child for key, child in value.items() if key != field}
    return supplied == canonical_sha256(core)


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise MatureS1TrackV2Error(f"artifact is outside repository root: {path}") from exc


def _module_path(module: str, root: Path) -> Path | None:
    if not module.startswith("scripts."):
        return None
    candidate = root / (module.replace(".", "/") + ".py")
    return candidate.resolve() if candidate.is_file() else None


def _local_python_imports(path: Path, root: Path) -> list[Path]:
    """Resolve direct repository-local imports without importing the module."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except SyntaxError as exc:
        raise MatureS1TrackV2Error(f"cannot parse frozen component: {path}") from exc
    found: set[Path] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and path.parent.resolve() == (root / "scripts").resolve():
                if node.module:
                    candidates.append(f"scripts.{node.module}")
                else:
                    candidates.extend(f"scripts.{alias.name}" for alias in node.names)
            elif node.module:
                candidates.append(node.module)
        for module in candidates:
            resolved = _module_path(module, root)
            if resolved is not None and resolved != path.resolve():
                found.add(resolved)
    return sorted(found, key=lambda value: _relative_to_root(value, root))


def build_component_graph(root: Path = ROOT) -> dict[str, Any]:
    """Return a hash-pinned direct and transitive repository dependency graph."""
    root = Path(root).resolve()
    missing = [relative for relative in REQUIRED_COMPONENTS if not (root / relative).is_file()]
    if missing:
        raise MatureS1TrackV2Error(
            "required freeze component missing: " + ", ".join(missing)
        )

    pending = [Path(root / relative).resolve() for relative in REQUIRED_COMPONENTS]
    seen: set[Path] = set()
    direct: dict[Path, list[Path]] = {}
    while pending:
        path = pending.pop(0)
        if path in seen:
            continue
        seen.add(path)
        dependencies = _local_python_imports(path, root) if path.suffix == ".py" else []
        direct[path] = dependencies
        pending.extend(dependency for dependency in dependencies if dependency not in seen)

    nodes = []
    for path in sorted(seen, key=lambda value: _relative_to_root(value, root)):
        nodes.append(
            {
                "relative_path": _relative_to_root(path, root),
                "kind": "PYTHON" if path.suffix == ".py" else "CONTRACT",
                "status": "FINAL",
                "sha256": file_sha256(path),
                "direct_dependencies": [
                    _relative_to_root(dependency, root) for dependency in direct[path]
                ],
            }
        )
    required = list(REQUIRED_COMPONENTS)
    graph_core = {
        "graph_version": "hybrid-v3-local-dependency-graph-v1",
        "required_components": required,
        "nodes": nodes,
    }
    return {**graph_core, "graph_sha256": canonical_sha256(graph_core)}


def _locked_source(
    source_track_dir: Path, root: Path
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    source_track_dir = Path(source_track_dir).resolve()
    track_path = source_track_dir / "research_track_manifest.json"
    freeze_path = source_track_dir / "research_execution_freeze.json"
    track = _read_json(track_path)
    freeze = _read_json(freeze_path)
    if track.get("track_version") != "hybrid-v3-mature-s1-research-track-v1":
        raise MatureS1TrackV2Error("source is not the locked mature S1 track V1")
    if track.get("status") != TRACK_STATUS or not _self_hash(track, "track_manifest_sha256"):
        raise MatureS1TrackV2Error("source track manifest changed")
    if int(track.get("cases", -1)) != EXPECTED_CASES or int(track.get("runs", -1)) != 3:
        raise MatureS1TrackV2Error("source track is not the exact 36-case triplicate set")
    for field, expected in {
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "performance_must_remain_sealed_until_prior_gates_pass": True,
    }.items():
        if track.get(field) is not expected:
            raise MatureS1TrackV2Error(f"source track visibility changed: {field}")
    if freeze.get("status") != "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION":
        raise MatureS1TrackV2Error("source execution freeze is not immutable")
    if file_sha256(freeze_path) != track.get("execution_freeze_sha256"):
        raise MatureS1TrackV2Error("source execution freeze hash changed")

    selection_path = Path(str(track.get("selection_plan_path") or "")).resolve()
    if selection_path.parent != source_track_dir or not selection_path.is_file():
        raise MatureS1TrackV2Error("source selection plan escaped or is missing")
    if file_sha256(selection_path) != track.get("selection_plan_sha256"):
        raise MatureS1TrackV2Error("source selection plan hash changed")
    selection = _read_json(selection_path)
    if selection.get("status") != "LOCKED_OUTCOME_BLIND" or int(
        selection.get("cases", -1)
    ) != EXPECTED_CASES:
        raise MatureS1TrackV2Error("source selection plan is not the locked 36-case plan")
    selection_rows = selection.get("rows") or []
    if len(selection_rows) != EXPECTED_CASES or selection.get(
        "rows_sha256"
    ) != canonical_sha256(selection_rows):
        raise MatureS1TrackV2Error("source selection-plan rows changed")
    for row in selection_rows:
        if not row.get("sampling_focus") or not row.get("eligible_stage_focuses"):
            raise MatureS1TrackV2Error("source selection plan lost sampling invariants")

    packet_path = Path(str(track["packet_path"])).resolve()
    packet_manifest_path = Path(str(track["packet_manifest_path"])).resolve()
    if packet_path.parent != source_track_dir or packet_manifest_path.parent != source_track_dir:
        raise MatureS1TrackV2Error("source packet paths escaped locked track V1")
    if file_sha256(packet_path) != track.get("packet_sha256"):
        raise MatureS1TrackV2Error("source packet hash changed")
    if file_sha256(packet_manifest_path) != track.get("packet_manifest_sha256"):
        raise MatureS1TrackV2Error("source packet manifest hash changed")
    packet_manifest = _read_json(packet_manifest_path)
    if packet_manifest.get("status") != "LOCKED_OUTCOME_BLIND" or not _self_hash(
        packet_manifest, "manifest_sha256"
    ):
        raise MatureS1TrackV2Error("source packet manifest is not locked")
    if int(packet_manifest.get("rows", -1)) != EXPECTED_CASES:
        raise MatureS1TrackV2Error("source packet manifest row count changed")

    packets = _read_jsonl(packet_path)
    mapping = packet_manifest.get("mapping") or []
    if len(packets) != EXPECTED_CASES or len(mapping) != EXPECTED_CASES:
        raise MatureS1TrackV2Error("source packet coverage is not exact")
    if hashlib.sha256(_canonical_jsonl(packets)).hexdigest() != packet_manifest.get(
        "packets_canonical_jsonl_sha256"
    ):
        raise MatureS1TrackV2Error("source canonical packet hash changed")
    for ordinal, (packet, mapped) in enumerate(zip(packets, mapping)):
        if mapped.get("selected_ordinal") != ordinal:
            raise MatureS1TrackV2Error("source packet order changed")
        if packet.get("review_id") != mapped.get("review_id") or packet.get(
            "anonymous_stock_id"
        ) != mapped.get("anonymous_stock_id"):
            raise MatureS1TrackV2Error("source anonymous identity mapping changed")
        if canonical_sha256(packet) != mapped.get("packet_sha256"):
            raise MatureS1TrackV2Error("source packet content changed")
        if any(key in packet for key in ("ai_output", "model_output", "performance", "future_outcome")):
            raise MatureS1TrackV2Error("source packet unexpectedly contains output or outcome data")
        selected = selection_rows[ordinal]
        if (
            selected.get("selected_ordinal", ordinal) != ordinal
            or selected.get("review_id") != packet.get("review_id")
            or selected.get("anonymous_stock_id") != packet.get("anonymous_stock_id")
        ):
            raise MatureS1TrackV2Error("source selection plan differs from packet order")

    provenance = {
        "track_manifest": {
            "relative_path": _relative_to_root(track_path, root),
            "sha256": file_sha256(track_path),
        },
        "execution_freeze": {
            "relative_path": _relative_to_root(freeze_path, root),
            "sha256": file_sha256(freeze_path),
        },
        "packet_artifact": {
            "relative_path": _relative_to_root(packet_path, root),
            "sha256": file_sha256(packet_path),
        },
        "packet_manifest": {
            "relative_path": _relative_to_root(packet_manifest_path, root),
            "sha256": file_sha256(packet_manifest_path),
        },
        "selection_plan": {
            "relative_path": _relative_to_root(selection_path, root),
            "sha256": file_sha256(selection_path),
        },
    }
    return packets, provenance, selection


def _compatibility_components(
    graph: Mapping[str, Any], names: Mapping[str, str]
) -> list[dict[str, Any]]:
    by_path = {
        str(node["relative_path"]): node for node in graph.get("nodes") or []
    }
    rows = []
    for name, relative in names.items():
        node = by_path.get(relative)
        if node is None:
            raise MatureS1TrackV2Error(f"component graph lost required node: {relative}")
        rows.append(
            {
                "name": name,
                "relative_path": relative,
                "status": "FINAL",
                "sha256": node["sha256"],
            }
        )
    return rows


def _parent_case_keys(source_track_dir: Path) -> list[str]:
    assignment = _read_json(Path(source_track_dir) / "primary_cases/assignment.manifest.json")
    keys: list[str] = []
    for shard in assignment.get("shards") or []:
        rows = _read_jsonl(Path(str(shard["path"])))
        if len(rows) != int(shard.get("rows", -1)):
            raise MatureS1TrackV2Error("source shard row count changed")
        if file_sha256(Path(str(shard["path"]))) != shard.get("sha256"):
            raise MatureS1TrackV2Error("source shard hash changed")
        keys.extend(str(row["case_key"]) for row in rows)
    if len(keys) != EXPECTED_CASES or len(set(keys)) != EXPECTED_CASES:
        raise MatureS1TrackV2Error("source case-key coverage changed")
    return sorted(keys)


def prepare(
    *,
    source_track_dir: Path = DEFAULT_SOURCE_TRACK,
    output_dir: Path = DEFAULT_OUTPUT,
    root: Path = ROOT,
    shard_count: int = 4,
) -> dict[str, Any]:
    root = Path(root).resolve()
    source_track_dir = Path(source_track_dir).resolve()
    output_dir = Path(output_dir).resolve()
    packets, provenance, selection = _locked_source(source_track_dir, root)
    graph = build_component_graph(root)

    packet_payload = _canonical_jsonl(packets)
    packet_path = output_dir / "primary_packets.jsonl"
    _publish_immutable(packet_path, packet_payload)
    # Sampling metadata remains external to every AI packet, but is copied and
    # pinned directly so the V2 evaluator need not discover it through V1.
    selection_path = output_dir / "selection_plan.json"
    _publish_immutable(selection_path, canonical_json_bytes(selection) + b"\n")
    selection_by_review = {
        str(row["review_id"]): row for row in selection.get("rows") or []
    }
    packet_manifest_core = {
        "manifest_version": PACKET_MANIFEST_VERSION,
        "status": "LOCKED_OUTCOME_BLIND",
        "block_id": "PRIMARY_MATURE_S1_INTEGRITY_V2",
        "rows": EXPECTED_CASES,
        "parent_v1": provenance,
        "packets_canonical_jsonl_sha256": hashlib.sha256(packet_payload).hexdigest(),
        "sampling_metadata_inside_packet": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
        "ai_output_read_by_builder": False,
        "selection_plan_path": str(selection_path),
        "selection_plan_sha256": file_sha256(selection_path),
        "mapping": [
            {
                "selected_ordinal": ordinal,
                "source_ordinal": selection_by_review[str(packet["review_id"])].get(
                    "source_ordinal"
                ),
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "packet_sha256": canonical_sha256(packet),
                "sampling_focus": selection_by_review[str(packet["review_id"])][
                    "sampling_focus"
                ],
                "eligible_stage_focuses": selection_by_review[str(packet["review_id"])][
                    "eligible_stage_focuses"
                ],
            }
            for ordinal, packet in enumerate(packets)
        ],
    }
    packet_manifest_core["mapping_sha256"] = canonical_sha256(packet_manifest_core["mapping"])
    packet_manifest = {
        **packet_manifest_core,
        "manifest_sha256": canonical_sha256(packet_manifest_core),
    }
    packet_manifest_path = output_dir / "primary_packets.manifest.json"
    _publish_immutable(packet_manifest_path, canonical_json_bytes(packet_manifest) + b"\n")

    v2_components = _compatibility_components(
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
    stage_components = _compatibility_components(
        graph,
        {
            "track_v1": "scripts/hybrid_v3_mature_s1_track_v1.py",
            "track_v2": "scripts/hybrid_v3_mature_s1_track_v2.py",
            "consistency_v1": "scripts/hybrid_v3_mature_s1_consistency_v1.py",
            "consistency_v2": "scripts/hybrid_v3_mature_s1_consistency_v2.py",
            "sharding": "scripts/hybrid_v3_sharding_v2.py",
            "triplicate_audit": "scripts/hybrid_v3_triplicate_smoke_audit.py",
            "stage_protocol": "config/hybrid_v3_mature_s1_stage_v2.json",
        },
    )

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
        "stage_protocol_sha256": file_sha256(
            root / "config/hybrid_v3_mature_s1_stage_v2.json"
        ),
        "parent_v1": provenance,
        "v2_components": v2_components,
        "stage_components": stage_components,
        "component_graph": graph,
        "execution_contract": {"model": MODEL, "reasoning_effort": REASONING},
    }
    freeze = {**freeze_core, "freeze_sha256": canonical_sha256(freeze_core)}
    freeze_path = output_dir / "research_execution_freeze.json"
    _publish_immutable(freeze_path, canonical_json_bytes(freeze) + b"\n")

    case_records = build_case_records(
        packets,
        source_manifest_sha256=file_sha256(packet_manifest_path),
        protocol_sha256=file_sha256(root / "config/hybrid_monitoring_research_protocol_v1.json"),
        prompt_sha256=file_sha256(root / "config/hybrid_semantic_prompt_v3.md"),
        schema_sha256=file_sha256(root / "config/hybrid_atomic_semantics_v2.schema.json"),
        execution_contract_sha256=file_sha256(freeze_path),
        model=MODEL,
        reasoning_effort=REASONING,
    )
    parent_keys = _parent_case_keys(source_track_dir)
    new_keys = sorted(str(row["case_key"]) for row in case_records)
    if set(parent_keys) & set(new_keys):
        raise MatureS1TrackV2Error("integrity V2 did not produce new case keys")
    assignment = write_shards(
        case_records, output_dir / "primary_cases", shard_count=shard_count
    )
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
        "packet_path": str(packet_path),
        "packet_sha256": file_sha256(packet_path),
        "packet_manifest_path": str(packet_manifest_path),
        "packet_manifest_sha256": file_sha256(packet_manifest_path),
        "selection_plan_path": str(selection_path),
        "selection_plan_sha256": file_sha256(selection_path),
        "stage_protocol_path": str(
            (root / "config/hybrid_v3_mature_s1_stage_v2.json").resolve()
        ),
        "stage_protocol_sha256": file_sha256(
            root / "config/hybrid_v3_mature_s1_stage_v2.json"
        ),
        "execution_freeze_path": str(freeze_path),
        "execution_freeze_sha256": file_sha256(freeze_path),
        "component_graph_sha256": graph["graph_sha256"],
        "assignment_manifest_path": str(assignment_path),
        "assignment_manifest_sha256": file_sha256(assignment_path),
        "assignment_sha256": assignment["assignment_sha256"],
        "parent_case_keys_sha256": canonical_sha256(parent_keys),
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
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--shard-count", type=int, default=4)
    args = parser.parse_args()
    track = prepare(
        source_track_dir=args.source_track_dir,
        output_dir=args.output_dir,
        shard_count=args.shard_count,
    )
    print(
        json.dumps(
            {
                "status": track["status"],
                "track_version": track["track_version"],
                "track_manifest_sha256": track["track_manifest_sha256"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
