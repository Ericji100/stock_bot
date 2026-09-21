"""Native fail-closed evaluator for the integrity-complete MATURE S1 track V3.

This evaluator does not use the version-locked V1/V2 evaluator entry points.
It independently verifies the V3 track, freeze, component graph, selection,
packets, shards, envelopes, attempts, and reviewer identity before calculating
the unchanged repeatability metrics.  Raw V3 permission/route stability is
reported as an additional diagnostic and does not alter the frozen S1 gates.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from . import hybrid_v3_codex_reviewer_v3 as reviewer_v3
    from . import hybrid_v3_mature_s1_track_v3 as track_v3
    from .hybrid_v3_atomic_policy_v3 import reduce_atomic_v3, validate_atomic
    from .hybrid_v3_atomic_runner_v2 import (
        ExecutionIntegrityError,
        RUNNER_VERSION,
        _validate_case_record,
        _validate_merge_envelope,
    )
    from .hybrid_v3_consistency_v2 import critical_question_ids
    from .hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from .hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_key,
        build_run_case_key,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )
    from .hybrid_v3_triplicate_smoke_audit import decision_signature, result_map
except ImportError:  # pragma: no cover - direct script execution
    from scripts import hybrid_v3_codex_reviewer_v3 as reviewer_v3
    from scripts import hybrid_v3_mature_s1_track_v3 as track_v3
    from scripts.hybrid_v3_atomic_policy_v3 import reduce_atomic_v3, validate_atomic
    from scripts.hybrid_v3_atomic_runner_v2 import (
        ExecutionIntegrityError,
        RUNNER_VERSION,
        _validate_case_record,
        _validate_merge_envelope,
    )
    from scripts.hybrid_v3_consistency_v2 import critical_question_ids
    from scripts.hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from scripts.hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_key,
        build_run_case_key,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )
    from scripts.hybrid_v3_triplicate_smoke_audit import decision_signature, result_map


ROOT = Path(__file__).resolve().parents[1]
REPORT_VERSION = "hybrid-v3-mature-s1-consistency-v3-integrity-complete"
METRICS_SEMANTICS = "hybrid-v3-mature-s1-consistency-v1-metrics"
TRACK_VERSION = track_v3.TRACK_VERSION
FREEZE_VERSION = track_v3.FREEZE_VERSION
EXPECTED_CASES = track_v3.EXPECTED_CASES
EXPECTED_RUNS = 3
TARGET_SCENARIO = "MATURE_TREND_PULLBACK"
TARGET_ROUTE = "V2_CORE"
STAGE_RELATIVE_PATH = "config/hybrid_v3_mature_s1_stage_v2.json"
PROTOCOL_RELATIVE_PATH = "config/hybrid_monitoring_research_protocol_v1.json"
SCHEMA_RELATIVE_PATH = "config/hybrid_atomic_semantics_v2.schema.json"
PROMPT_RELATIVE_PATH = "config/hybrid_semantic_prompt_v3.md"

_SAMPLING_PACKET_KEYS = frozenset(
    {"eligible_sampling_strata", "primary_sampling_focus", "sampling_stratum", "sampling_focus"}
)
_ENVELOPE_FIELDS = frozenset(
    {
        "runner_version",
        "status",
        "run_number",
        "source_ordinal",
        "review_id",
        "case_key",
        "run_case_key",
        "packet_sha256",
        "source_manifest_sha256",
        "protocol_sha256",
        "prompt_sha256",
        "schema_sha256",
        "execution_contract_sha256",
        "model",
        "reasoning_effort",
        "successful_attempt",
        "output_sha256",
        "reviewer_audit_sha256",
        "reviewer_audit",
        "output",
    }
)
_VALIDATED_ATTEMPT_FIELDS = frozenset(
    {
        "runner_version",
        "case_key",
        "run_case_key",
        "run_number",
        "attempt",
        "status",
        "technical_failure",
        "started_at",
        "finished_at",
        "elapsed_seconds",
        "output_sha256",
        "reviewer_audit_sha256",
        "reviewer_audit",
        "output",
    }
)


class MatureS1ConsistencyV3Error(ValueError):
    """A frozen V3 input, run envelope, or attempt is not exact."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatureS1ConsistencyV3Error(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise MatureS1ConsistencyV3Error(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise MatureS1ConsistencyV3Error(f"cannot read JSONL artifact: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MatureS1ConsistencyV3Error(f"invalid JSON at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise MatureS1ConsistencyV3Error(f"expected JSON object at {path}:{line_number}")
        rows.append(value)
    return rows


def _self_hash(value: Mapping[str, Any], field: str) -> bool:
    core = dict(value)
    supplied = core.pop(field, None)
    return supplied == canonical_sha256(core)


def _canonical_jsonl(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def _resolved_repo_path(relative_path: Any) -> Path:
    text = str(relative_path or "")
    path = (ROOT / text).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise MatureS1ConsistencyV3Error(f"component path escaped repository: {text}") from exc
    return path


def _validate_component_rows(
    freeze: Mapping[str, Any],
    field: str,
    expected_paths: Mapping[str, str],
    graph_by_path: Mapping[str, Any],
) -> None:
    rows = freeze.get(field)
    if not isinstance(rows, list) or len(rows) != len(expected_paths):
        raise MatureS1ConsistencyV3Error(f"freeze {field} coverage is not exact")
    by_name = {
        str(row.get("name") or ""): row for row in rows if isinstance(row, Mapping)
    }
    if set(by_name) != set(expected_paths) or len(by_name) != len(rows):
        raise MatureS1ConsistencyV3Error(f"freeze {field} names are not exact")
    for name, row in by_name.items():
        relative = str(row.get("relative_path") or "")
        if relative != expected_paths[name]:
            raise MatureS1ConsistencyV3Error(
                f"frozen component role/path changed: {field}.{name}"
            )
        node = graph_by_path.get(relative)
        path = _resolved_repo_path(relative)
        if (
            row.get("status") != "FINAL"
            or not isinstance(node, Mapping)
            or node.get("sha256") != row.get("sha256")
            or not path.is_file()
            or file_sha256(path) != row.get("sha256")
        ):
            raise MatureS1ConsistencyV3Error(f"frozen component changed: {field}.{name}")


def _validate_parent_provenance(
    track: Mapping[str, Any], freeze: Mapping[str, Any], packet_manifest: Mapping[str, Any]
) -> set[str]:
    provenance = track.get("source_track_v1")
    if (
        not isinstance(provenance, Mapping)
        or provenance != freeze.get("source_track_v1")
        or provenance != packet_manifest.get("source_track_v1")
    ):
        raise MatureS1ConsistencyV3Error("source V1 provenance differs across frozen artifacts")
    required = {
        "track_manifest",
        "execution_freeze",
        "packet_artifact",
        "packet_manifest",
        "selection_plan",
    }
    if set(provenance) != required:
        raise MatureS1ConsistencyV3Error("source V1 provenance coverage is not exact")
    resolved: dict[str, Path] = {}
    for name, row in provenance.items():
        if not isinstance(row, Mapping):
            raise MatureS1ConsistencyV3Error(f"invalid source V1 provenance row: {name}")
        path = _resolved_repo_path(row.get("relative_path"))
        if not path.is_file() or file_sha256(path) != row.get("sha256"):
            raise MatureS1ConsistencyV3Error(f"source V1 provenance changed: {name}")
        resolved[name] = path
    parent_track = _read_json(resolved["track_manifest"])
    if (
        parent_track.get("track_version") != "hybrid-v3-mature-s1-research-track-v1"
        or not _self_hash(parent_track, "track_manifest_sha256")
        or file_sha256(resolved["execution_freeze"])
        != parent_track.get("execution_freeze_sha256")
    ):
        raise MatureS1ConsistencyV3Error("source V1 track/freeze is not authentic")
    assignment_path = Path(str(parent_track.get("assignment_manifest_path") or "")).resolve()
    if (
        not assignment_path.is_file()
        or file_sha256(assignment_path) != parent_track.get("assignment_manifest_sha256")
    ):
        raise MatureS1ConsistencyV3Error("source V1 assignment manifest changed")
    assignment = _read_json(assignment_path)
    keys: list[str] = []
    for shard in assignment.get("shards") or []:
        if not isinstance(shard, Mapping):
            raise MatureS1ConsistencyV3Error("source V1 shard row is invalid")
        path = Path(str(shard.get("path") or "")).resolve()
        if not path.is_file() or file_sha256(path) != shard.get("sha256"):
            raise MatureS1ConsistencyV3Error("source V1 shard changed")
        rows = _read_jsonl(path)
        if len(rows) != int(shard.get("rows", -1)):
            raise MatureS1ConsistencyV3Error("source V1 shard row count changed")
        keys.extend(str(row.get("case_key") or "") for row in rows)
    if len(keys) != EXPECTED_CASES or len(set(keys)) != EXPECTED_CASES or "" in keys:
        raise MatureS1ConsistencyV3Error("source V1 case-key coverage is not exact")
    return set(keys)


def _validate_rejected_v2(track: Mapping[str, Any], freeze: Mapping[str, Any]) -> None:
    path = Path(str(track.get("rejected_v2_preflight_path") or "")).resolve()
    frozen = freeze.get("rejected_v2_preflight") or {}
    if (
        not path.is_file()
        or Path(str(frozen.get("path") or "")).resolve() != path
        or file_sha256(path) != track.get("rejected_v2_preflight_sha256")
        or file_sha256(path) != frozen.get("sha256")
    ):
        raise MatureS1ConsistencyV3Error("rejected V2 preflight provenance changed")
    rejection = _read_json(path)
    if (
        rejection.get("status") != "PREFLIGHT_REJECTED_TECHNICAL_ONLY"
        or not _self_hash(rejection, "rejection_sha256")
        or rejection.get("rejection_sha256") != frozen.get("rejection_sha256")
        or rejection.get("ai_executed") is not False
        or rejection.get("performance_remains_sealed") is not True
        or rejection.get("strategy_or_gate_change") is not False
    ):
        raise MatureS1ConsistencyV3Error("rejected V2 preflight contract changed")


def validate_sample_invariants(
    records: Sequence[Mapping[str, Any]],
    stage: Mapping[str, Any],
    packet_manifest: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    """Recompute quotas, stock/month balance, and focus eligibility."""

    sample = stage.get("consistency_sample")
    if not isinstance(sample, Mapping):
        raise MatureS1ConsistencyV3Error("stage has no consistency_sample")
    if int(sample.get("cases", -1)) != EXPECTED_CASES or int(
        sample.get("runs", -1)
    ) != EXPECTED_RUNS:
        raise MatureS1ConsistencyV3Error("stage case/run count changed")
    if sample.get("one_case_per_anonymous_stock") is not True:
        raise MatureS1ConsistencyV3Error("one-case-per-stock is not frozen true")
    focus_order = [str(value) for value in sample.get("focus_order") or []]
    quota_source = sample.get("quotas")
    if not isinstance(quota_source, Mapping):
        raise MatureS1ConsistencyV3Error("stage focus quotas are missing")
    quotas = {str(key): int(value) for key, value in quota_source.items()}
    if list(quotas) != focus_order or sum(quotas.values()) != EXPECTED_CASES:
        raise MatureS1ConsistencyV3Error("stage focus quotas do not exactly cover the sample")

    mapping = packet_manifest.get("mapping")
    selected_rows = selection.get("rows")
    if (
        not isinstance(mapping, list)
        or not isinstance(selected_rows, list)
        or len(mapping) != EXPECTED_CASES
        or len(selected_rows) != EXPECTED_CASES
        or len(records) != EXPECTED_CASES
    ):
        raise MatureS1ConsistencyV3Error("sample mapping/selection coverage is not exact")
    for key, expected in {
        "sampling_metadata_inside_packet": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
    }.items():
        if packet_manifest.get(key) is not expected:
            raise MatureS1ConsistencyV3Error(f"packet manifest causal flag changed: {key}")

    focus_counts: Counter[str] = Counter()
    month_counts: Counter[str] = Counter()
    seen_reviews: set[str] = set()
    seen_stocks: set[str] = set()
    for ordinal, (record, mapped, selected) in enumerate(
        zip(records, mapping, selected_rows)
    ):
        if not isinstance(mapped, Mapping) or not isinstance(selected, Mapping):
            raise MatureS1ConsistencyV3Error("sample metadata row is not an object")
        packet = record.get("packet")
        if not isinstance(packet, Mapping):
            raise MatureS1ConsistencyV3Error("case record has no packet")
        review_id = str(record.get("review_id") or "")
        stock_id = str(record.get("anonymous_stock_id") or "")
        if not review_id or review_id in seen_reviews:
            raise MatureS1ConsistencyV3Error("sample review IDs are empty or duplicated")
        if not stock_id or stock_id in seen_stocks:
            raise MatureS1ConsistencyV3Error("sample repeats an anonymous stock")
        seen_reviews.add(review_id)
        seen_stocks.add(stock_id)
        shared = {
            "review_id": review_id,
            "anonymous_stock_id": stock_id,
            "packet_sha256": str(record.get("packet_sha256") or ""),
        }
        if mapped.get("selected_ordinal") != ordinal:
            raise MatureS1ConsistencyV3Error("sample mapping order changed")
        for key, expected in shared.items():
            if mapped.get(key) != expected or selected.get(key) != expected:
                raise MatureS1ConsistencyV3Error(
                    f"sample mapping/selection identity differs at ordinal {ordinal}: {key}"
                )
        if (
            packet.get("review_id") != review_id
            or packet.get("anonymous_stock_id") != stock_id
            or canonical_sha256(dict(packet)) != record.get("packet_sha256")
        ):
            raise MatureS1ConsistencyV3Error("sample packet identity/hash changed")
        if mapped.get("source_ordinal") != selected.get("source_ordinal"):
            raise MatureS1ConsistencyV3Error("source selection ordinal changed")
        focus = str(mapped.get("sampling_focus") or "")
        eligible = mapped.get("eligible_stage_focuses")
        if (
            focus != selected.get("sampling_focus")
            or eligible != selected.get("eligible_stage_focuses")
            or not isinstance(eligible, list)
            or not eligible
            or len(eligible) != len(set(eligible))
            or focus not in eligible
            or focus not in quotas
        ):
            raise MatureS1ConsistencyV3Error("sampling focus is changed or ineligible")
        if _SAMPLING_PACKET_KEYS.intersection(_walk_keys(packet)):
            raise MatureS1ConsistencyV3Error("sampling metadata entered an AI packet")
        try:
            parsed = date.fromisoformat(str(packet.get("as_of") or ""))
        except ValueError as exc:
            raise MatureS1ConsistencyV3Error("sample as_of is not an ISO date") from exc
        if parsed.isoformat() != packet.get("as_of"):
            raise MatureS1ConsistencyV3Error("sample as_of is not canonical")
        focus_counts[focus] += 1
        month_counts[parsed.strftime("%Y-%m")] += 1

    if dict(focus_counts) != quotas:
        raise MatureS1ConsistencyV3Error(
            f"sample focus quotas differ: actual={dict(focus_counts)} expected={quotas}"
        )
    minimum_months = int(sample.get("minimum_distinct_months", -1))
    maximum_per_month = int(sample.get("maximum_cases_per_month", -1))
    if len(month_counts) < minimum_months:
        raise MatureS1ConsistencyV3Error("sample month coverage is insufficient")
    if max(month_counts.values(), default=0) > maximum_per_month:
        raise MatureS1ConsistencyV3Error("sample exceeds the maximum cases per month")
    return {
        "status": "PASS",
        "cases": EXPECTED_CASES,
        "unique_anonymous_stocks": len(seen_stocks),
        "focus_counts": dict(sorted(focus_counts.items())),
        "month_counts": dict(sorted(month_counts.items())),
        "distinct_months": len(month_counts),
        "maximum_cases_in_one_month": max(month_counts.values(), default=0),
        "sampling_metadata_inside_packet": False,
    }


def verify_track(
    *,
    source: Path,
    source_manifest: Path,
    track_manifest: Path,
    assignment_manifest: Path,
    execution_freeze: Path,
    stage_protocol: Path,
    research_protocol: Path,
    schema: Path,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, str],
    dict[str, Any],
]:
    """Verify the complete V3 integrity track without reading any run output."""

    source = Path(source).resolve()
    source_manifest = Path(source_manifest).resolve()
    track_manifest = Path(track_manifest).resolve()
    assignment_manifest = Path(assignment_manifest).resolve()
    execution_freeze = Path(execution_freeze).resolve()
    stage_protocol = Path(stage_protocol).resolve()
    research_protocol = Path(research_protocol).resolve()
    schema = Path(schema).resolve()
    track = _read_json(track_manifest)
    freeze = _read_json(execution_freeze)
    packet_manifest = _read_json(source_manifest)
    stage = _read_json(stage_protocol)
    protocol = _read_json(research_protocol)
    assignment = _read_json(assignment_manifest)
    if track.get("track_version") != TRACK_VERSION or not _self_hash(
        track, "track_manifest_sha256"
    ):
        raise MatureS1ConsistencyV3Error("V3 track manifest changed")
    if track.get("status") != track_v3.TRACK_STATUS:
        raise MatureS1ConsistencyV3Error("V3 track is not frozen")
    if freeze.get("freeze_version") != FREEZE_VERSION or not _self_hash(
        freeze, "freeze_sha256"
    ):
        raise MatureS1ConsistencyV3Error("V3 execution freeze changed")
    if freeze.get("status") != "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION":
        raise MatureS1ConsistencyV3Error("V3 execution is not frozen")
    for key, expected in {
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "old_ai_output_visible": False,
        "strategy_or_gate_change": False,
    }.items():
        if freeze.get(key) is not expected:
            raise MatureS1ConsistencyV3Error(f"freeze causal flag changed: {key}")
    scope = freeze.get("single_route_scope") or {}
    if (
        scope.get("primary_scenario") != TARGET_SCENARIO
        or scope.get("trade_route") != TARGET_ROUTE
        or scope.get("other_routes_suspended") is not True
    ):
        raise MatureS1ConsistencyV3Error("freeze target/suspension changed")
    if (
        stage.get("stage_protocol_version") != "hybrid-v3-mature-s1-stage-v2"
        or stage.get("status") != "FINAL_RESEARCH_LOCKED"
    ):
        raise MatureS1ConsistencyV3Error("stage protocol is not frozen")
    if protocol.get("status") != "FINAL_RESEARCH_LOCKED":
        raise MatureS1ConsistencyV3Error("research protocol is not frozen")

    expected_graph = track_v3.build_component_graph(ROOT)
    graph = freeze.get("component_graph")
    if graph != expected_graph or graph.get("graph_sha256") != track.get(
        "component_graph_sha256"
    ):
        raise MatureS1ConsistencyV3Error("complete component graph changed")
    graph_by_path = {
        str(row.get("relative_path") or ""): row
        for row in graph.get("nodes") or []
        if isinstance(row, Mapping)
    }
    _validate_component_rows(
        freeze,
        "v2_components",
        {
            "launcher": "scripts/hybrid_v3_codex_launcher_v2.py",
            "reviewer": "scripts/hybrid_v3_codex_reviewer_v3.py",
            "runner": "scripts/hybrid_v3_atomic_runner_v2.py",
            "policy": "scripts/hybrid_v3_atomic_policy_v3.py",
            "prompt": PROMPT_RELATIVE_PATH,
            "schema": SCHEMA_RELATIVE_PATH,
            "protocol": PROTOCOL_RELATIVE_PATH,
        },
        graph_by_path,
    )
    _validate_component_rows(
        freeze,
        "stage_components",
        {
            "track_v1": "scripts/hybrid_v3_mature_s1_track_v1.py",
            "track_v2": "scripts/hybrid_v3_mature_s1_track_v2.py",
            "track_v3": "scripts/hybrid_v3_mature_s1_track_v3.py",
            "consistency_v1": "scripts/hybrid_v3_mature_s1_consistency_v1.py",
            "consistency_v2": "scripts/hybrid_v3_mature_s1_consistency_v2.py",
            "consistency_v3": "scripts/hybrid_v3_mature_s1_consistency_v3.py",
            "sharding": "scripts/hybrid_v3_sharding_v2.py",
            "triplicate_audit": "scripts/hybrid_v3_triplicate_smoke_audit.py",
            "stage_protocol": STAGE_RELATIVE_PATH,
        },
        graph_by_path,
    )
    if file_sha256(stage_protocol) != freeze.get("stage_protocol_sha256"):
        raise MatureS1ConsistencyV3Error("stage protocol hash changed")
    if Path(str(track.get("stage_protocol_path") or "")).resolve() != stage_protocol:
        raise MatureS1ConsistencyV3Error("track stage protocol path changed")
    _validate_rejected_v2(track, freeze)
    parent_case_keys = _validate_parent_provenance(track, freeze, packet_manifest)

    linked = {
        "source": (source, "packet_path", "packet_sha256"),
        "source manifest": (
            source_manifest,
            "packet_manifest_path",
            "packet_manifest_sha256",
        ),
        "selection": (
            Path(str(track.get("selection_plan_path") or "")).resolve(),
            "selection_plan_path",
            "selection_plan_sha256",
        ),
        "execution freeze": (
            execution_freeze,
            "execution_freeze_path",
            "execution_freeze_sha256",
        ),
        "assignment": (
            assignment_manifest,
            "assignment_manifest_path",
            "assignment_manifest_sha256",
        ),
    }
    for name, (path, path_field, hash_field) in linked.items():
        if Path(str(track.get(path_field) or "")).resolve() != path:
            raise MatureS1ConsistencyV3Error(f"track {name} path changed")
        if not path.is_file() or file_sha256(path) != track.get(hash_field):
            raise MatureS1ConsistencyV3Error(f"track {name} hash changed")
    selection_path = linked["selection"][0]
    selection = _read_json(selection_path)
    if (
        selection.get("status") != "LOCKED_OUTCOME_BLIND"
        or not _self_hash(selection, "selection_sha256")
        or file_sha256(selection_path) != freeze.get("selection_plan_sha256")
    ):
        raise MatureS1ConsistencyV3Error("selection plan changed")
    if file_sha256(source) != freeze.get("source_packet_sha256"):
        raise MatureS1ConsistencyV3Error("freeze packet hash changed")
    if file_sha256(source_manifest) != freeze.get("source_packet_manifest_sha256"):
        raise MatureS1ConsistencyV3Error("freeze packet manifest hash changed")
    if file_sha256(execution_freeze) != assignment.get("execution_contract_sha256"):
        raise MatureS1ConsistencyV3Error("assignment uses another execution freeze")
    if file_sha256(source_manifest) != assignment.get("source_manifest_sha256"):
        raise MatureS1ConsistencyV3Error("assignment uses another packet manifest")

    packets = _read_jsonl(source)
    if len(packets) != EXPECTED_CASES or int(packet_manifest.get("rows", -1)) != EXPECTED_CASES:
        raise MatureS1ConsistencyV3Error("packet count is not exact")
    if (
        packet_manifest.get("status") != "LOCKED_OUTCOME_BLIND"
        or not _self_hash(packet_manifest, "manifest_sha256")
        or hashlib.sha256(_canonical_jsonl(packets)).hexdigest()
        != packet_manifest.get("packets_canonical_jsonl_sha256")
    ):
        raise MatureS1ConsistencyV3Error("packet manifest/content changed")
    mapping = packet_manifest.get("mapping")
    if (
        not isinstance(mapping, list)
        or len(mapping) != EXPECTED_CASES
        or canonical_sha256(mapping) != packet_manifest.get("mapping_sha256")
    ):
        raise MatureS1ConsistencyV3Error("external packet mapping changed")

    if (
        int(assignment.get("expected_rows", -1)) != EXPECTED_CASES
        or int((assignment.get("coverage") or {}).get("covered", -1)) != EXPECTED_CASES
        or int(assignment.get("shard_count", -1)) != len(assignment.get("shards") or [])
    ):
        raise MatureS1ConsistencyV3Error("shard coverage is incomplete")
    assigned: list[dict[str, Any]] = []
    assignment_pairs: list[dict[str, Any]] = []
    for shard in assignment.get("shards") or []:
        if not isinstance(shard, Mapping):
            raise MatureS1ConsistencyV3Error("invalid shard manifest row")
        shard_path = Path(str(shard.get("path") or "")).resolve()
        if not shard_path.is_file() or file_sha256(shard_path) != shard.get("sha256"):
            raise MatureS1ConsistencyV3Error("shard artifact changed")
        rows = _read_jsonl(shard_path)
        if len(rows) != int(shard.get("rows", -1)):
            raise MatureS1ConsistencyV3Error("shard row count changed")
        for row in rows:
            try:
                _validate_case_record(row)
            except ExecutionIntegrityError as exc:
                raise MatureS1ConsistencyV3Error("case record identity changed") from exc
            assigned.append(row)
            assignment_pairs.append(
                {"case_key": row["case_key"], "shard_id": int(shard["shard_id"])}
            )
    if (
        len(assigned) != EXPECTED_CASES
        or canonical_sha256(assignment_pairs) != assignment.get("assignment_sha256")
    ):
        raise MatureS1ConsistencyV3Error("shard assignment changed")
    by_review = {str(row.get("review_id") or ""): row for row in assigned}
    if len(by_review) != EXPECTED_CASES or "" in by_review:
        raise MatureS1ConsistencyV3Error("assigned review IDs are not exact")
    ordered: list[dict[str, Any]] = []
    expected_identity = {
        "source_manifest_sha256": file_sha256(source_manifest),
        "protocol_sha256": file_sha256(research_protocol),
        "prompt_sha256": file_sha256(ROOT / PROMPT_RELATIVE_PATH),
        "schema_sha256": file_sha256(schema),
        "execution_contract_sha256": file_sha256(execution_freeze),
        "model": freeze.get("execution_contract", {}).get("model"),
        "reasoning_effort": freeze.get("execution_contract", {}).get("reasoning_effort"),
    }
    for ordinal, packet in enumerate(packets):
        review_id = str(packet.get("review_id") or "")
        record = by_review.get(review_id)
        if record is None or int(record.get("source_ordinal", -1)) != ordinal:
            raise MatureS1ConsistencyV3Error("case source order changed")
        if record.get("packet") != packet or canonical_sha256(packet) != record.get(
            "packet_sha256"
        ):
            raise MatureS1ConsistencyV3Error("case packet changed")
        for field, expected in expected_identity.items():
            if record.get(field) != expected:
                raise MatureS1ConsistencyV3Error(f"case execution identity changed: {field}")
        ordered.append(record)
    new_case_keys = [str(row["case_key"]) for row in ordered]
    if parent_case_keys.intersection(new_case_keys):
        raise MatureS1ConsistencyV3Error("V3 case keys overlap V1")
    if canonical_sha256(sorted(new_case_keys)) != track.get("case_keys_sha256"):
        raise MatureS1ConsistencyV3Error("V3 case-key manifest changed")

    sample_validation = validate_sample_invariants(
        ordered, stage, packet_manifest, selection
    )
    focus_by_review = {
        str(row["review_id"]): str(row["sampling_focus"])
        for row in mapping
    }
    track_validation = {
        "status": "PASS",
        "component_graph_sha256": graph["graph_sha256"],
        "track_manifest_sha256": file_sha256(track_manifest),
        "execution_freeze_sha256": file_sha256(execution_freeze),
        "packet_sha256": file_sha256(source),
        "sample": sample_validation,
    }
    return ordered, stage, protocol, focus_by_review, track_validation


def _reviewer_code_sha256(execution_freeze: Path) -> str:
    freeze = _read_json(execution_freeze)
    rows = freeze.get("v2_components")
    matches = [
        row
        for row in rows or []
        if isinstance(row, Mapping) and row.get("name") == "reviewer"
    ]
    if len(matches) != 1:
        raise MatureS1ConsistencyV3Error("freeze reviewer component is not exact")
    reviewer_path = Path(reviewer_v3.__file__).resolve()
    row = matches[0]
    if (
        row.get("status") != "FINAL"
        or _resolved_repo_path(row.get("relative_path")) != reviewer_path
        or file_sha256(reviewer_path) != row.get("sha256")
    ):
        raise MatureS1ConsistencyV3Error("reviewer V3 identity changed")
    return str(row["sha256"])


def _validate_attempt_chain(
    *,
    record: Mapping[str, Any],
    envelope: Mapping[str, Any],
    envelope_path: Path,
    run_number: int,
) -> int:
    successful = envelope.get("successful_attempt")
    if isinstance(successful, bool) or not isinstance(successful, int) or successful < 1:
        raise MatureS1ConsistencyV3Error("successful_attempt is invalid")
    run_case_key = str(envelope.get("run_case_key") or "")
    attempt_dir = envelope_path.parent.parent / "attempts" / run_case_key
    attempt_paths = sorted(attempt_dir.glob("attempt_*.json")) if attempt_dir.is_dir() else []
    expected_names = [f"attempt_{number:03d}.json" for number in range(1, successful + 1)]
    if [path.name for path in attempt_paths] != expected_names:
        raise MatureS1ConsistencyV3Error("attempt chain is missing, non-contiguous, or trailing")
    for number, path in enumerate(attempt_paths, 1):
        attempt = _read_json(path)
        for field, expected in {
            "runner_version": RUNNER_VERSION,
            "case_key": record["case_key"],
            "run_case_key": run_case_key,
            "run_number": run_number,
            "attempt": number,
        }.items():
            if attempt.get(field) != expected:
                raise MatureS1ConsistencyV3Error(f"attempt identity changed: {field}")
        if number < successful:
            if attempt.get("status") != "RETRYABLE_ERROR" or attempt.get(
                "technical_failure"
            ) is not True:
                raise MatureS1ConsistencyV3Error("pre-success attempt is not retryable")
            continue
        if set(attempt) != _VALIDATED_ATTEMPT_FIELDS:
            raise MatureS1ConsistencyV3Error("validated attempt fields are not exact")
        if attempt.get("status") != "VALIDATED" or attempt.get(
            "technical_failure"
        ) is not False:
            raise MatureS1ConsistencyV3Error("successful attempt is not VALIDATED")
        for field in (
            "output_sha256",
            "reviewer_audit_sha256",
            "output",
            "reviewer_audit",
        ):
            if attempt.get(field) != envelope.get(field):
                raise MatureS1ConsistencyV3Error(
                    f"envelope differs from successful attempt: {field}"
                )
    return successful


def validate_run_artifacts(
    records: Sequence[Mapping[str, Any]],
    run_roots: Sequence[Path],
    *,
    reviewer_code_sha256: str,
) -> tuple[list[dict[str, Any]], list[dict[str, dict[str, Any]]]]:
    """Return exact envelopes only after full runner/attempt validation."""

    if len(run_roots) != EXPECTED_RUNS:
        raise MatureS1ConsistencyV3Error("repeatability requires exactly three run roots")
    roots = [Path(root).resolve() for root in run_roots]
    if len(set(roots)) != EXPECTED_RUNS:
        raise MatureS1ConsistencyV3Error("run roots must be distinct")
    expected = {str(row.get("review_id") or ""): row for row in records}
    if len(expected) != EXPECTED_CASES or "" in expected:
        raise MatureS1ConsistencyV3Error("expected case identities are not exact")
    summaries: list[dict[str, Any]] = []
    runs: list[dict[str, dict[str, Any]]] = []
    for run_number, root in enumerate(roots, 1):
        found: dict[str, dict[str, Any]] = {}
        attempt_count = 0
        paths = sorted(root.glob("s*/cases/*.json"))
        for path in paths:
            envelope = _read_json(path)
            review_id = str(envelope.get("review_id") or "")
            if review_id not in expected or review_id in found:
                raise MatureS1ConsistencyV3Error("unexpected or duplicate run review ID")
            record = expected[review_id]
            run_key = build_run_case_key(str(record["case_key"]), run_number)
            if path.stem != run_key or envelope.get("run_case_key") != run_key:
                raise MatureS1ConsistencyV3Error("envelope filename/run_case_key changed")
            if set(envelope) != _ENVELOPE_FIELDS:
                raise MatureS1ConsistencyV3Error("run envelope fields are not exact")
            try:
                _validate_merge_envelope(dict(record), envelope, run_number)
            except ExecutionIntegrityError as exc:
                raise MatureS1ConsistencyV3Error("runner envelope validation failed") from exc
            audit = envelope.get("reviewer_audit") or {}
            if (
                audit.get("adapter_version") != reviewer_v3.ADAPTER_VERSION
                or audit.get("adapter_status") != reviewer_v3.ADAPTER_STATUS
                or audit.get("adapter_code_sha256") != reviewer_code_sha256
            ):
                raise MatureS1ConsistencyV3Error("reviewer V3 adapter identity changed")
            attempt_count += _validate_attempt_chain(
                record=record,
                envelope=envelope,
                envelope_path=path,
                run_number=run_number,
            )
            found[review_id] = envelope
        missing = sorted(set(expected) - set(found))
        if len(paths) != EXPECTED_CASES or missing:
            raise MatureS1ConsistencyV3Error(
                f"run {run_number} coverage is not exact; first_missing={missing[0] if missing else None}"
            )
        summaries.append(
            {
                "run_number": run_number,
                "status": "PASS",
                "cases": len(found),
                "attempt_artifacts": attempt_count,
                "reviewer_adapter_version": reviewer_v3.ADAPTER_VERSION,
                "reviewer_adapter_status": reviewer_v3.ADAPTER_STATUS,
                "reviewer_adapter_code_sha256": reviewer_code_sha256,
            }
        )
        runs.append(found)
    return summaries, runs


def stage_permission(signature: Mapping[str, Any]) -> str:
    """Collapse the full V3 decision to the single-route deployment permission."""

    if signature.get("permission") == "REMOVE":
        return "REMOVE"
    if (
        signature.get("permission") == "TRADE"
        and signature.get("route") == TARGET_ROUTE
        and signature.get("scenario") == TARGET_SCENARIO
    ):
        return "TRADE"
    return "WAIT"


def _rate(exact: int, total: int, threshold: float | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exact": exact,
        "total": total,
        "rate": exact / total if total else 0.0,
    }
    if threshold is not None:
        result["threshold"] = threshold
        result["passed"] = result["rate"] >= threshold
    return result


def _counter(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _raw_diagnostics(per_case: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    permission_exact = route_exact = pair_exact = masked = 0
    run_permissions: list[Counter[str]] = [Counter() for _ in range(EXPECTED_RUNS)]
    run_routes: list[Counter[str]] = [Counter() for _ in range(EXPECTED_RUNS)]
    rows: list[dict[str, Any]] = []
    for row in per_case:
        decisions = row.get("run_decisions")
        if not isinstance(decisions, list) or len(decisions) != EXPECTED_RUNS:
            raise MatureS1ConsistencyV3Error("raw decision diagnostics are incomplete")
        permissions = [value.get("permission") for value in decisions]
        routes = [value.get("route") for value in decisions]
        permission_ok = len(set(permissions)) == 1
        route_ok = len(set(routes)) == 1
        pair_ok = len(set(zip(permissions, routes))) == 1
        stage_ok = bool(row.get("stage_permission_exact"))
        permission_exact += int(permission_ok)
        route_exact += int(route_ok)
        pair_exact += int(pair_ok)
        masked += int(stage_ok and not pair_ok)
        for index, (permission, route) in enumerate(zip(permissions, routes)):
            run_permissions[index]["<NONE>" if permission is None else str(permission)] += 1
            run_routes[index]["<NONE>" if route is None else str(route)] += 1
        rows.append(
            {
                "review_id": row.get("review_id"),
                "raw_permissions": permissions,
                "raw_routes": routes,
                "raw_permission_exact": permission_ok,
                "raw_route_exact": route_ok,
                "raw_permission_route_exact": pair_ok,
                "stage_agreement_masks_raw_pair_disagreement": stage_ok and not pair_ok,
            }
        )
    return {
        "status": "DIAGNOSTIC_ONLY_NOT_AN_ACCEPTANCE_GATE",
        "raw_permission": _rate(permission_exact, EXPECTED_CASES),
        "raw_route": _rate(route_exact, EXPECTED_CASES),
        "raw_permission_and_route": _rate(pair_exact, EXPECTED_CASES),
        "stage_agreement_masked_raw_pair_disagreement_cases": masked,
        "permission_distributions_by_run": [
            {"run_number": index + 1, "counts": dict(sorted(values.items()))}
            for index, values in enumerate(run_permissions)
        ],
        "route_distributions_by_run": [
            {"run_number": index + 1, "counts": dict(sorted(values.items()))}
            for index, values in enumerate(run_routes)
        ],
        "per_case": rows,
    }


def evaluate(
    *,
    source: Path,
    source_manifest: Path,
    track_manifest: Path,
    assignment_manifest: Path,
    execution_freeze: Path,
    stage_protocol: Path,
    research_protocol: Path,
    schema: Path,
    run_roots: Sequence[Path],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Evaluate repeatability only after every integrity check succeeds."""

    records, stage, protocol, focus_by_review, track_validation = verify_track(
        source=source,
        source_manifest=source_manifest,
        track_manifest=track_manifest,
        assignment_manifest=assignment_manifest,
        execution_freeze=execution_freeze,
        stage_protocol=stage_protocol,
        research_protocol=research_protocol,
        schema=schema,
    )
    reviewer_sha = _reviewer_code_sha256(execution_freeze)
    run_validation, runs = validate_run_artifacts(
        records, run_roots, reviewer_code_sha256=reviewer_sha
    )
    critical_ids = critical_question_ids(protocol, _read_json(schema))

    schema_exact = permission_exact = scenario_lr_exact = 0
    pooled_exact = pooled_total = critical_exact_total = critical_total = 0
    unanimous_trade = unanimous_nontrade = 0
    run_permissions: list[list[str]] = [[] for _ in range(EXPECTED_RUNS)]
    run_scenarios: list[list[str]] = [[] for _ in range(EXPECTED_RUNS)]
    focus_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"cases": 0, "permission_exact": 0, "scenario_lr_exact": 0}
    )
    per_case: list[dict[str, Any]] = []
    merged_ledger: list[dict[str, Any]] = []
    for record in records:
        review_id = str(record["review_id"])
        packet = record["packet"]
        envelopes = [run[review_id] for run in runs]
        outputs = [envelope["output"] for envelope in envelopes]
        errors = [validate_atomic(packet, output) for output in outputs]
        if any(errors):
            raise MatureS1ConsistencyV3Error(
                f"atomic validation failed for {review_id}: {errors}"
            )
        schema_exact += 1
        signatures = [decision_signature(reduce_atomic_v3(packet, output)) for output in outputs]
        stage_values = [stage_permission(signature) for signature in signatures]
        permission_ok = len(set(stage_values)) == 1
        scenario_lr_ok = (
            len({signature.get("scenario") for signature in signatures}) == 1
            and len({signature.get("left_right_phase") for signature in signatures}) == 1
        )
        permission_exact += int(permission_ok)
        scenario_lr_exact += int(scenario_lr_ok)
        if permission_ok and stage_values[0] == "TRADE":
            unanimous_trade += 1
        if permission_ok and stage_values[0] != "TRADE":
            unanimous_nontrade += 1
        for index, value in enumerate(stage_values):
            run_permissions[index].append(value)
            run_scenarios[index].append(str(signatures[index].get("scenario")))

        maps = [result_map(output) for output in outputs]
        paths = sorted(set().union(*(set(mapping) for mapping in maps)))
        if any(set(mapping) != set(paths) for mapping in maps):
            raise MatureS1ConsistencyV3Error(f"atomic manifest differs for {review_id}")
        critical_paths = [path for path in paths if path.rsplit(".", 1)[-1] in critical_ids]
        atom_exact = sum(len({mapping[path] for mapping in maps}) == 1 for path in paths)
        critical_atom_exact = sum(
            len({mapping[path] for mapping in maps}) == 1 for path in critical_paths
        )
        pooled_exact += atom_exact
        pooled_total += len(paths)
        critical_exact_total += critical_atom_exact
        critical_total += len(critical_paths)

        merged, merge_metrics = conservative_merge_atomic_outputs(
            outputs, critical_ids=critical_ids
        )
        merged_errors = validate_atomic(packet, merged)
        if merged_errors:
            raise MatureS1ConsistencyV3Error(
                f"merged output invalid for {review_id}: {merged_errors}"
            )
        merged_signature = decision_signature(reduce_atomic_v3(packet, merged))
        focus = focus_by_review[review_id]
        focus_totals[focus]["cases"] += 1
        focus_totals[focus]["permission_exact"] += int(permission_ok)
        focus_totals[focus]["scenario_lr_exact"] += int(scenario_lr_ok)
        case_row = {
            "review_id": review_id,
            "sampling_focus": focus,
            "atom_agreement": _rate(atom_exact, len(paths)),
            "critical_atom_agreement": _rate(critical_atom_exact, len(critical_paths)),
            "stage_permission_exact": permission_ok,
            "scenario_left_right_exact": scenario_lr_ok,
            "run_decisions": signatures,
            "run_stage_permissions": stage_values,
            "conservative_merge_decision": merged_signature,
            "conservative_merge_stage_permission": stage_permission(merged_signature),
            "critical_disagreement_count": len(
                merge_metrics["critical_disagreement_paths"]
            ),
        }
        per_case.append(case_row)
        merged_ledger.append(
            {
                "review_id": review_id,
                "sampling_focus": focus,
                "packet_sha256": record["packet_sha256"],
                "merge_policy": "UNANIMOUS_ELSE_UNKNOWN",
                "atomic_semantics": merged,
                "full_v3_decision": merged_signature,
                "s1_stage_permission": stage_permission(merged_signature),
                "critical_disagreement_paths": merge_metrics[
                    "critical_disagreement_paths"
                ],
            }
        )

    thresholds = stage.get("repeatability_acceptance") or {}
    schema_metric = _rate(
        schema_exact, EXPECTED_CASES, float(thresholds["schema_and_causal_fields"])
    )
    permission_metric = _rate(
        permission_exact, EXPECTED_CASES, float(thresholds["material_permission"])
    )
    scenario_metric = _rate(
        scenario_lr_exact,
        EXPECTED_CASES,
        float(thresholds["scenario_and_left_right_phase"]),
    )
    checks = {
        "schema_and_causal_fields": bool(schema_metric["passed"]),
        "material_permission": bool(permission_metric["passed"]),
        "scenario_and_left_right_phase": bool(scenario_metric["passed"]),
        "minimum_unanimous_s1_trade_cases": unanimous_trade
        >= int(thresholds["minimum_unanimous_s1_trade_cases"]),
        "minimum_unanimous_s1_nontrade_cases": unanimous_nontrade
        >= int(thresholds["minimum_unanimous_s1_nontrade_cases"]),
    }
    passed = all(checks.values())
    focus_metrics = {
        focus: {
            "cases": values["cases"],
            "material_permission": _rate(values["permission_exact"], values["cases"]),
            "scenario_and_left_right_phase": _rate(
                values["scenario_lr_exact"], values["cases"]
            ),
        }
        for focus, values in sorted(focus_totals.items())
    }
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "metrics_semantics": METRICS_SEMANTICS,
        "status": "REPEATABILITY_PASS" if passed else "REPEATABILITY_FAIL",
        "classification": "OUTCOME_BLIND_MATURE_S1_REPEATABILITY_ONLY",
        "course_correctness_evaluated": False,
        "performance_evaluated": False,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "target_scenario": TARGET_SCENARIO,
        "target_route": TARGET_ROUTE,
        "expected_cases": EXPECTED_CASES,
        "run_count": EXPECTED_RUNS,
        "model": records[0]["model"],
        "reasoning_effort": records[0]["reasoning_effort"],
        "fail_closed_integrity": {
            "status": "PASS",
            "track": track_validation,
            "runs": run_validation,
        },
        "metrics": {
            "schema_and_causal_fields": schema_metric,
            "material_permission": permission_metric,
            "scenario_and_left_right_phase": scenario_metric,
            "all_atomic_answers_pooled": _rate(pooled_exact, pooled_total),
            "critical_atomic_answers_pooled": _rate(
                critical_exact_total, critical_total
            ),
            "unanimous_s1_trade_cases": unanimous_trade,
            "unanimous_s1_nontrade_cases": unanimous_nontrade,
        },
        "acceptance": {"checks": checks, "passed": passed},
        "stage_permission_distributions_by_run": [
            {"run_number": index + 1, "counts": _counter(values)}
            for index, values in enumerate(run_permissions)
        ],
        "scenario_distributions_by_run": [
            {"run_number": index + 1, "counts": _counter(values)}
            for index, values in enumerate(run_scenarios)
        ],
        "sampling_focus_metrics": focus_metrics,
        "per_case": per_case,
        "raw_v3_permission_route_diagnostics": _raw_diagnostics(per_case),
        "next_step": (
            "LOCK_COMMON_LEDGER_AND_RUN_OUTCOME_BLIND_COURSE_FIDELITY_AUDIT"
            if passed
            else "KEEP_PERFORMANCE_SEALED_AND_VERSION_BEFORE_RETEST"
        ),
        "limitations": [
            "REPEATABILITY_IS_NOT_COURSE_CORRECTNESS",
            "REPEATABILITY_IS_NOT_TRADING_PERFORMANCE",
            "OTHER_SIX_V3_ROUTES_REMAIN_SUSPENDED",
        ],
    }
    report["report_sha256"] = canonical_sha256(report)
    return report, merged_ledger


def render_markdown(report: Mapping[str, Any], ledger_path: Path | None = None) -> str:
    metrics = report["metrics"]
    raw = report["raw_v3_permission_route_diagnostics"]
    lines = [
        "# V3 情境1三輪一致性報告（integrity-complete）",
        "",
        f"- 結果：`{report['status']}`",
        "- 路徑：`MATURE_TREND_PULLBACK／V2_CORE（長多慣性拉回再發動完整合格）`",
        f"- 案例：{report['expected_cases']}例 × {report['run_count']}輪",
        "- 性質：結果盲化一致性驗收；不是課程正確性或交易績效。",
        "- 完整性：sample、freeze、傳遞依賴、envelope、attempt 與 reviewer identity 均採 fail-closed。",
        "",
        "| 驗收項目 | 實際 | 門檻 | 結果 |",
        "| --- | ---: | ---: | --- |",
    ]
    for key, label in (
        ("schema_and_causal_fields", "Schema與因果欄位"),
        ("material_permission", "情境1交易／等待／移除權限"),
        ("scenario_and_left_right_phase", "主要情境與左右階段"),
    ):
        value = metrics[key]
        lines.append(
            f"| {label} | {value['rate']:.2%}（{value['exact']}/{value['total']}） | "
            f"{value['threshold']:.0%} | {'通過' if value['passed'] else '未通過'} |"
        )
    lines.extend(
        [
            "",
            "## 覆蓋與診斷",
            "",
            f"- 三輪一致的情境1交易案例：{metrics['unanimous_s1_trade_cases']}例。",
            f"- 三輪一致的不交易／移除案例：{metrics['unanimous_s1_nontrade_cases']}例。",
            f"- 全部原子答案一致率：{metrics['all_atomic_answers_pooled']['rate']:.2%}。",
            f"- 關鍵原子答案一致率：{metrics['critical_atomic_answers_pooled']['rate']:.2%}。",
            f"- 原始 V3 permission 一致率：{raw['raw_permission']['rate']:.2%}。",
            f"- 原始 V3 route 一致率：{raw['raw_route']['rate']:.2%}。",
            f"- stage 一致但遮蔽 raw permission+route 分歧：{raw['stage_agreement_masked_raw_pair_disagreement_cases']}例。",
            "",
            "## 後續",
            "",
        ]
    )
    if report["acceptance"]["passed"]:
        lines.append(
            "一致性門檻通過；只可鎖定共同 ledger 並進入結果盲化課程符合度稽核，仍不得解封績效。"
        )
        if ledger_path is not None:
            lines.append(f"共同 ledger：`{Path(ledger_path).resolve()}`")
    else:
        lines.append("一致性或覆蓋門檻未通過；維持績效封存，必須另建版本後才能重測。")
    lines.extend(["", f"報告 SHA-256：`{report['report_sha256']}`", ""])
    return "\n".join(lines)


def _publish(path: Path, payload: bytes) -> None:
    try:
        track_v3._publish_immutable(Path(path), payload)
    except Exception as exc:
        raise MatureS1ConsistencyV3Error(
            f"refusing to overwrite immutable artifact: {path}"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--track-manifest", type=Path, required=True)
    parser.add_argument("--assignment-manifest", type=Path, required=True)
    parser.add_argument("--execution-freeze", type=Path, required=True)
    parser.add_argument("--stage-protocol", type=Path, required=True)
    parser.add_argument("--research-protocol", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, action="append", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--merged-ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    report, ledger = evaluate(
        source=args.source,
        source_manifest=args.source_manifest,
        track_manifest=args.track_manifest,
        assignment_manifest=args.assignment_manifest,
        execution_freeze=args.execution_freeze,
        stage_protocol=args.stage_protocol,
        research_protocol=args.research_protocol,
        schema=args.schema,
        run_roots=args.run_root,
    )
    ledger_path: Path | None = None
    if report["acceptance"]["passed"]:
        ledger_payload = _canonical_jsonl(ledger)
        _publish(args.merged_ledger, ledger_payload)
        ledger_path = args.merged_ledger
        report["merged_ledger"] = {
            "status": "LOCKED_OUTCOME_BLIND_MATURE_S1_COMMON_LEDGER",
            "rows": len(ledger),
            "path": str(args.merged_ledger.resolve()),
            "sha256": hashlib.sha256(ledger_payload).hexdigest(),
        }
        report.pop("report_sha256", None)
        report["report_sha256"] = canonical_sha256(report)
    _publish(args.output_json, canonical_json_bytes(report) + b"\n")
    _publish(args.output_md, render_markdown(report, ledger_path).encode("utf-8"))
    print(json.dumps({"status": report["status"], "report_sha256": report["report_sha256"]}))
    return 0 if report["acceptance"]["passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
