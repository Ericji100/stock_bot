"""Fail-closed three-run repeatability evaluator for the V4-S1 route.

The evaluator consumes only frozen anonymous packets and atomic AI outputs.  It
does not execute a model and it never opens an identity map, future-price file,
or performance artifact.  V3 remains the AI-visible atomic contract; V4-S1 is
used only for deterministic reduction and the single-route stage permission.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from . import hybrid_v3_codex_reviewer_v3 as reviewer_v3
    from .hybrid_v3_atomic_runner_v2 import (
        ExecutionIntegrityError,
        RUNNER_VERSION,
        _validate_merge_envelope,
    )
    from .hybrid_v3_consistency_v2 import critical_question_ids
    from .hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from .hybrid_v3_sharding_v2 import (
        _publish_immutable,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )
    from .hybrid_v3_triplicate_smoke_audit import result_map
    from .hybrid_v4_s1_atomic_policy_v1 import (
        POLICY_VERSION,
        reduce_atomic_v4_s1,
        stage_permission_v4_s1,
        validate_atomic,
    )
except ImportError:  # pragma: no cover - direct script execution
    from scripts import hybrid_v3_codex_reviewer_v3 as reviewer_v3
    from scripts.hybrid_v3_atomic_runner_v2 import (
        ExecutionIntegrityError,
        RUNNER_VERSION,
        _validate_merge_envelope,
    )
    from scripts.hybrid_v3_consistency_v2 import critical_question_ids
    from scripts.hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from scripts.hybrid_v3_sharding_v2 import (
        _publish_immutable,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
    )
    from scripts.hybrid_v3_triplicate_smoke_audit import result_map
    from scripts.hybrid_v4_s1_atomic_policy_v1 import (
        POLICY_VERSION,
        reduce_atomic_v4_s1,
        stage_permission_v4_s1,
        validate_atomic,
    )


ROOT = Path(__file__).resolve().parents[1]
REPORT_VERSION = "hybrid-v4-s1-consistency-v1"
TRACK_VERSION = "hybrid-v4-s1-research-track-v3-cli-entry"
FREEZE_VERSION = "hybrid-v4-s1-execution-freeze-v3-cli-entry"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_V4_S1_SINGLE_ROUTE_RESEARCH_ONLY"
EXPECTED_CASES = 36
EXPECTED_RUNS = 3
TARGET_SCENARIO = "MATURE_TREND_PULLBACK"
TARGET_ROUTE = "V2_CORE"
THRESHOLDS = {
    "schema_and_causal_fields": 1.0,
    "material_permission": 0.95,
    "scenario_and_left_right_phase": 0.90,
    "minimum_unanimous_s1_trade_cases": 3,
    "minimum_unanimous_s1_nontrade_cases": 3,
}

V2_COMPONENT_PATHS = {
    "launcher": "scripts/hybrid_v3_codex_launcher_v2.py",
    "reviewer": "scripts/hybrid_v3_codex_reviewer_v3.py",
    "runner": "scripts/hybrid_v3_atomic_runner_v2.py",
    "policy": "scripts/hybrid_v3_atomic_policy_v3.py",
    "prompt": "config/hybrid_semantic_prompt_v3.md",
    "schema": "config/hybrid_atomic_semantics_v2.schema.json",
    "protocol": "config/hybrid_monitoring_research_protocol_v1.json",
}
V4_COMPONENT_PATHS = {
    "stage_protocol": "config/hybrid_v4_s1_mature_stage_v1.json",
    "reachability_preflight_builder": "scripts/hybrid_v4_s1_reachability_preflight_v1.py",
    "policy": "scripts/hybrid_v4_s1_atomic_policy_v1.py",
    "track_builder": "scripts/hybrid_v4_s1_track_v1.py",
    "evaluator": "scripts/hybrid_v4_s1_consistency_v1.py",
    "execution_orchestrator": "scripts/hybrid_v4_s1_execution_orchestrator_v1.py",
}
V4_DYNAMIC_COMPONENTS = {"reachability_preflight"}

_ENVELOPE_FIELDS = frozenset(
    {
        "runner_version", "status", "run_number", "source_ordinal", "review_id",
        "case_key", "run_case_key", "packet_sha256", "source_manifest_sha256",
        "protocol_sha256", "prompt_sha256", "schema_sha256",
        "execution_contract_sha256", "model", "reasoning_effort",
        "successful_attempt", "output_sha256", "reviewer_audit_sha256",
        "reviewer_audit", "output",
    }
)
_VALIDATED_ATTEMPT_FIELDS = frozenset(
    {
        "runner_version", "case_key", "run_case_key", "run_number", "attempt",
        "status", "technical_failure", "started_at", "finished_at",
        "elapsed_seconds", "output_sha256", "reviewer_audit_sha256",
        "reviewer_audit", "output",
    }
)
_RETRY_ATTEMPT_FIELDS = frozenset(
    {
        "runner_version", "case_key", "run_case_key", "run_number", "attempt",
        "status", "technical_failure", "started_at", "finished_at",
        "elapsed_seconds", "error_type", "error",
    }
)
_PACKET_FORBIDDEN_KEYS = frozenset(
    {
        "stock_code", "stock_name", "ticker", "symbol", "isin", "company_name",
        "security_name", "identity_map", "identity_mapping", "future_open",
        "future_high", "future_low", "future_close", "future_return",
        "forward_return", "return_after", "exit_date", "exit_price", "mfe", "mae",
        "pnl", "profit", "profit_factor", "realized_return", "unrealized_return",
        "next_20d_return", "outcome", "winner", "win_rate",
        "performance_result", "performance_results", "sampling_focus",
        "_preflight_symbolic_only", "action_signature_sha256",
        "symbolic_witness_sha256", "symbolic_reachability",
        "symbolic_classification", "eligible_sampling_strata",
        "eligible_stage_focuses", "primary_sampling_focus",
        "sampling_stratum", "symbolic_witness", "expected_answer",
    }
)


class V4S1ConsistencyError(ValueError):
    """A supposedly frozen V4-S1 artifact is incomplete or changed."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V4S1ConsistencyError(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise V4S1ConsistencyError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise V4S1ConsistencyError(f"cannot read JSONL artifact: {path}") from exc
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise V4S1ConsistencyError(f"invalid JSON at {path}:{number}") from exc
        if not isinstance(row, dict):
            raise V4S1ConsistencyError(f"expected object at {path}:{number}")
        rows.append(row)
    return rows


def _self_hash(value: Mapping[str, Any], field: str) -> bool:
    core = dict(value)
    supplied = core.pop(field, None)
    return supplied == canonical_sha256(core)


def _canonical_jsonl(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _resolve_component(relative_path: Any) -> Path:
    text = str(relative_path or "")
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate.resolve()
    resolved = (ROOT / candidate).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise V4S1ConsistencyError(f"component path escaped repository: {text}") from exc
    return resolved


def _walk_forbidden_packet_keys(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if (
                normalized in _PACKET_FORBIDDEN_KEYS
                or normalized.startswith(("future_", "forward_"))
                or "winner" in normalized
                or normalized.endswith(("_mfe", "_mae", "_pnl"))
            ):
                return str(key)
            found = _walk_forbidden_packet_keys(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _walk_forbidden_packet_keys(child)
            if found:
                return found
    return None


def _component_index(
    freeze: Mapping[str, Any], field: str, expected_paths: Mapping[str, str],
    dynamic_names: set[str] | None = None,
    expected_statuses: Mapping[str, str] | None = None,
) -> dict[str, Mapping[str, Any]]:
    rows = freeze.get(field)
    dynamic_names = dynamic_names or set()
    expected_names = set(expected_paths) | dynamic_names
    if not isinstance(rows, list) or len(rows) != len(expected_names):
        raise V4S1ConsistencyError(f"freeze {field} component coverage is not exact")
    by_name = {
        str(row.get("name") or ""): row
        for row in rows if isinstance(row, Mapping)
    }
    if set(by_name) != expected_names or len(by_name) != len(rows):
        raise V4S1ConsistencyError(f"freeze {field} component names are not exact")
    expected_statuses = expected_statuses or {}
    for name, row in by_name.items():
        wanted_status = expected_statuses.get(name, "FINAL")
        if row.get("status") != wanted_status:
            raise V4S1ConsistencyError(f"frozen component status changed: {field}.{name}")
        if name in expected_paths and row.get("relative_path") != expected_paths[name]:
            raise V4S1ConsistencyError(f"frozen component path changed: {field}.{name}")
        path = _resolve_component(row.get("relative_path"))
        if not path.is_file() or file_sha256(path) != row.get("sha256"):
            raise V4S1ConsistencyError(f"frozen component hash changed: {field}.{name}")
    return by_name


def _linked_path(
    track: Mapping[str, Any], actual: Path, path_field: str, hash_field: str, label: str,
) -> None:
    actual = Path(actual).resolve()
    if Path(str(track.get(path_field) or "")).resolve() != actual:
        raise V4S1ConsistencyError(f"track {label} path changed")
    if not actual.is_file() or file_sha256(actual) != track.get(hash_field):
        raise V4S1ConsistencyError(f"track {label} hash changed")


def _packet_from_source_row(row: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
    packet = row.get("packet") if isinstance(row.get("packet"), Mapping) else row
    packet = dict(packet)
    if "packet" in row:
        if row.get("source_ordinal") != ordinal:
            raise V4S1ConsistencyError("source ordinal differs from exact file order")
        if row.get("packet_sha256") != canonical_sha256(packet):
            raise V4S1ConsistencyError("source wrapper packet hash changed")
    forbidden = _walk_forbidden_packet_keys(packet)
    if forbidden:
        raise V4S1ConsistencyError(f"identity/future/performance field entered packet: {forbidden}")
    return packet


def _verify_stage(stage: Mapping[str, Any]) -> None:
    if (
        stage.get("stage_protocol_version") != "hybrid-v4-s1-mature-stage-v1"
        or stage.get("status") != "FINAL_RESEARCH_LOCKED"
    ):
        raise V4S1ConsistencyError("V4-S1 stage protocol is not final and frozen")
    target = stage.get("target") or {}
    if (
        target.get("primary_scenario") != TARGET_SCENARIO
        or target.get("trade_route") != TARGET_ROUTE
        or target.get("other_v3_routes_suspended") is not True
    ):
        raise V4S1ConsistencyError("V4-S1 target or route suspension changed")
    sample = stage.get("consistency_sample") or {}
    if (
        sample.get("cases") != EXPECTED_CASES
        or sample.get("runs") != EXPECTED_RUNS
        or sample.get("one_case_per_anonymous_stock") is not True
    ):
        raise V4S1ConsistencyError("V4-S1 36-case/three-run sample contract changed")
    acceptance = stage.get("repeatability_acceptance") or {}
    if any(acceptance.get(key) != value for key, value in THRESHOLDS.items()):
        raise V4S1ConsistencyError("V4-S1 repeatability thresholds changed")
    if stage.get("disagreement_policy") != "UNANIMOUS_ELSE_UNKNOWN_AND_NO_TRADE":
        raise V4S1ConsistencyError("V4-S1 conservative disagreement policy changed")


def _verify_component_graph(
    freeze: Mapping[str, Any], track: Mapping[str, Any], component_rows: Sequence[Mapping[str, Any]],
) -> None:
    graph = freeze.get("component_graph")
    if graph is None:
        return
    if not isinstance(graph, Mapping):
        raise V4S1ConsistencyError("freeze component graph is invalid")
    core = dict(graph)
    supplied = core.pop("graph_sha256", None)
    if supplied != canonical_sha256(core) or track.get("component_graph_sha256") != supplied:
        raise V4S1ConsistencyError("component graph hash changed")
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        raise V4S1ConsistencyError("component graph nodes are missing")
    by_path: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, Mapping):
            raise V4S1ConsistencyError("component graph node is invalid")
        relative = str(node.get("relative_path") or "")
        if not relative or relative in by_path:
            raise V4S1ConsistencyError("component graph paths are empty or duplicated")
        path = _resolve_component(relative)
        if not node.get("status") or not path.is_file() or file_sha256(path) != node.get("sha256"):
            raise V4S1ConsistencyError(f"component graph node changed: {relative}")
        by_path[relative] = node
    for row in component_rows:
        node = by_path.get(str(row.get("relative_path") or ""))
        if node is None or node.get("sha256") != row.get("sha256"):
            raise V4S1ConsistencyError("frozen component is absent from component graph")


def verify_frozen_track(
    *, source: Path, source_manifest: Path, track_manifest: Path,
    assignment_manifest: Path, execution_freeze: Path, stage_protocol: Path,
    research_protocol: Path, prompt: Path, schema: Path, policy: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Verify every pre-run input and return ordered anonymous case records."""

    paths = [
        source, source_manifest, track_manifest, assignment_manifest,
        execution_freeze, stage_protocol, research_protocol, prompt, schema, policy,
    ]
    (source, source_manifest, track_manifest, assignment_manifest, execution_freeze,
     stage_protocol, research_protocol, prompt, schema, policy) = [Path(p).resolve() for p in paths]
    track = _read_json(track_manifest)
    freeze = _read_json(execution_freeze)
    manifest = _read_json(source_manifest)
    stage = _read_json(stage_protocol)
    protocol = _read_json(research_protocol)
    assignment = _read_json(assignment_manifest)

    if track.get("track_version") != TRACK_VERSION or not _self_hash(track, "track_manifest_sha256"):
        raise V4S1ConsistencyError("V4-S1 track manifest changed")
    if track.get("status") != TRACK_STATUS:
        raise V4S1ConsistencyError("V4-S1 track is not frozen")
    if freeze.get("freeze_version") != FREEZE_VERSION or not _self_hash(freeze, "freeze_sha256"):
        raise V4S1ConsistencyError("V4-S1 execution freeze changed")
    if freeze.get("track_version") != TRACK_VERSION:
        raise V4S1ConsistencyError("freeze points to another track version")
    if freeze.get("status") != "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION":
        raise V4S1ConsistencyError("V4-S1 execution is not frozen")
    required_freeze_flags = {
        "outcome_blind": True, "identity_visible": False,
        "future_or_performance_visible": False,
        "old_ai_output_visible": False, "strategy_or_gate_change": True,
        "policy_decision_changed_relative_to_v3": True, "v1_v2_v3_read_only": True,
        "expected_cases": EXPECTED_CASES, "expected_runs": EXPECTED_RUNS,
    }
    for field, expected in required_freeze_flags.items():
        if freeze.get(field) != expected:
            raise V4S1ConsistencyError(f"freeze field changed: {field}")
    scope = freeze.get("single_route_scope") or {}
    if (
        scope.get("primary_scenario") != TARGET_SCENARIO
        or scope.get("trade_route") != TARGET_ROUTE
        or scope.get("other_routes_suspended") is not True
    ):
        raise V4S1ConsistencyError("freeze single-route scope changed")
    required_track_flags = {
        "outcome_blind": True, "identity_visible": False,
        "future_or_performance_visible": False,
        "performance_must_remain_sealed_until_prior_gates_pass": True,
        "policy_decision_changed_relative_to_v3": True, "v1_v2_v3_read_only": True,
        "cases": EXPECTED_CASES, "runs": EXPECTED_RUNS,
    }
    for field, expected in required_track_flags.items():
        if track.get(field) != expected:
            raise V4S1ConsistencyError(f"track field changed: {field}")

    _verify_stage(stage)
    if protocol.get("status") != "FINAL_RESEARCH_LOCKED":
        raise V4S1ConsistencyError("research protocol is not frozen")
    v2_components = _component_index(freeze, "v2_components", V2_COMPONENT_PATHS)
    v4_components = _component_index(
        freeze, "v4_s1_components", V4_COMPONENT_PATHS, V4_DYNAMIC_COMPONENTS,
        {
            "stage_protocol": "FINAL_RESEARCH_LOCKED",
            "reachability_preflight": "PASS_SYMBOLIC_TARGET_ROUTE_REACHABLE",
            "policy": "DRAFT_FOR_OUTCOME_BLIND_RESEARCH",
        },
    )
    all_components = [*v2_components.values(), *v4_components.values()]
    _verify_component_graph(freeze, track, all_components)
    supplied_components = {
        "stage_protocol": stage_protocol, "policy": policy,
    }
    for name, path in supplied_components.items():
        if _resolve_component(v4_components[name].get("relative_path")) != path:
            raise V4S1ConsistencyError(f"CLI path differs from V4 component: {name}")
    for name, path in {"protocol": research_protocol, "prompt": prompt, "schema": schema}.items():
        if _resolve_component(v2_components[name].get("relative_path")) != path:
            raise V4S1ConsistencyError(f"CLI path differs from V3 atomic component: {name}")
    if POLICY_VERSION != "hybrid-v4-s1-atomic-policy-v1":
        raise V4S1ConsistencyError("loaded reducer is not V4-S1 policy v1")

    _linked_path(track, source, "packet_path", "packet_sha256", "packet source")
    _linked_path(track, source_manifest, "packet_manifest_path", "packet_manifest_sha256", "packet manifest")
    _linked_path(track, execution_freeze, "execution_freeze_path", "execution_freeze_sha256", "execution freeze")
    _linked_path(track, assignment_manifest, "assignment_manifest_path", "assignment_manifest_sha256", "assignment")
    selection_path = Path(str(track.get("selection_plan_path") or "")).resolve()
    _linked_path(track, selection_path, "selection_plan_path", "selection_plan_sha256", "selection plan")
    if file_sha256(source) != freeze.get("primary_packet_sha256"):
        raise V4S1ConsistencyError("freeze packet hash changed")
    if file_sha256(source_manifest) != freeze.get("primary_packet_manifest_sha256"):
        raise V4S1ConsistencyError("freeze packet manifest hash changed")
    if file_sha256(selection_path) != freeze.get("selection_plan_sha256"):
        raise V4S1ConsistencyError("freeze selection-plan hash changed")

    source_rows = _read_jsonl(source)
    if len(source_rows) != EXPECTED_CASES or int(manifest.get("rows", -1)) != EXPECTED_CASES:
        raise V4S1ConsistencyError("source is not exact 36-case coverage")
    if manifest.get("status") != "LOCKED_OUTCOME_BLIND" or not _self_hash(manifest, "manifest_sha256"):
        raise V4S1ConsistencyError("packet manifest is not locked or self-hash changed")
    if hashlib.sha256(_canonical_jsonl(source_rows)).hexdigest() != manifest.get("packets_canonical_jsonl_sha256"):
        raise V4S1ConsistencyError("packet manifest/content hash changed")
    for field in (
        "sampling_metadata_inside_packet", "identity_visible_inside_packet",
        "future_or_performance_visible_inside_packet", "symbolic_witness_inside_packet",
        "symbolic_classification_inside_packet",
    ):
        if manifest.get(field) is not False:
            raise V4S1ConsistencyError(f"packet manifest blind flag changed: {field}")

    packets = [_packet_from_source_row(row, ordinal) for ordinal, row in enumerate(source_rows)]
    reviews = [str(packet.get("review_id") or "") for packet in packets]
    stocks = [str(packet.get("anonymous_stock_id") or "") for packet in packets]
    if "" in reviews or len(set(reviews)) != EXPECTED_CASES:
        raise V4S1ConsistencyError("source review IDs are empty or duplicated")
    if "" in stocks or len(set(stocks)) != EXPECTED_CASES:
        raise V4S1ConsistencyError("sample is not one case per anonymous stock")
    months: Counter[str] = Counter()
    for packet in packets:
        try:
            parsed = date.fromisoformat(str(packet.get("as_of") or ""))
        except ValueError as exc:
            raise V4S1ConsistencyError("packet as_of is not an ISO date") from exc
        months[parsed.strftime("%Y-%m")] += 1
    sample = stage["consistency_sample"]
    if len(months) < int(sample["minimum_distinct_months"]):
        raise V4S1ConsistencyError("sample month coverage is insufficient")
    if max(months.values(), default=0) > int(sample["maximum_cases_per_month"]):
        raise V4S1ConsistencyError("sample exceeds maximum cases per month")

    if (
        assignment.get("expected_rows") != EXPECTED_CASES
        or (assignment.get("coverage") or {}).get("covered") != EXPECTED_CASES
        or file_sha256(execution_freeze) != assignment.get("execution_contract_sha256")
        or file_sha256(source) != assignment.get("source_manifest_sha256")
    ):
        raise V4S1ConsistencyError("assignment freeze or coverage changed")
    shard_rows = assignment.get("shards")
    if not isinstance(shard_rows, list) or assignment.get("shard_count") != len(shard_rows):
        raise V4S1ConsistencyError("assignment shard count changed")
    assigned: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    for shard in shard_rows:
        if not isinstance(shard, Mapping):
            raise V4S1ConsistencyError("assignment shard row is invalid")
        shard_path = Path(str(shard.get("path") or "")).resolve()
        if not shard_path.is_file() or file_sha256(shard_path) != shard.get("sha256"):
            raise V4S1ConsistencyError("assigned shard hash changed")
        rows = _read_jsonl(shard_path)
        if len(rows) != shard.get("rows"):
            raise V4S1ConsistencyError("assigned shard row count changed")
        for record in rows:
            try:
                from .hybrid_v3_atomic_runner_v2 import _validate_case_record
            except ImportError:  # pragma: no cover
                from scripts.hybrid_v3_atomic_runner_v2 import _validate_case_record
            try:
                _validate_case_record(record)
            except ExecutionIntegrityError as exc:
                raise V4S1ConsistencyError("case record identity changed") from exc
            assigned.append(record)
            pairs.append({"case_key": record["case_key"], "shard_id": int(shard["shard_id"])})
    if len(assigned) != EXPECTED_CASES or canonical_sha256(pairs) != assignment.get("assignment_sha256"):
        raise V4S1ConsistencyError("assignment membership changed")
    by_review = {str(row.get("review_id") or ""): row for row in assigned}
    if len(by_review) != EXPECTED_CASES or "" in by_review:
        raise V4S1ConsistencyError("assigned review coverage is not exact")
    execution = freeze.get("execution_contract") or {}
    expected_identity = {
        "source_manifest_sha256": file_sha256(source),
        "protocol_sha256": file_sha256(research_protocol),
        "prompt_sha256": file_sha256(prompt),
        "schema_sha256": file_sha256(schema),
        "execution_contract_sha256": file_sha256(execution_freeze),
        "model": execution.get("model"),
        "reasoning_effort": execution.get("reasoning_effort"),
    }
    ordered: list[dict[str, Any]] = []
    for ordinal, packet in enumerate(packets):
        review_id = str(packet["review_id"])
        record = by_review.get(review_id)
        if record is None or record.get("source_ordinal") != ordinal:
            raise V4S1ConsistencyError("case source order changed")
        if record.get("packet") != packet or record.get("packet_sha256") != canonical_sha256(packet):
            raise V4S1ConsistencyError("case packet differs from source")
        if record.get("anonymous_stock_id") != packet.get("anonymous_stock_id"):
            raise V4S1ConsistencyError("case anonymous stock differs from packet")
        for field, expected in expected_identity.items():
            if record.get(field) != expected:
                raise V4S1ConsistencyError(f"case frozen identity changed: {field}")
        ordered.append(record)
    if track.get("assignment_sha256") != assignment.get("assignment_sha256"):
        raise V4S1ConsistencyError("track assignment membership hash changed")
    return ordered, stage, protocol, {
        "status": "PASS", "status_zh": "完整性驗證通過",
        "track_manifest_sha256": file_sha256(track_manifest),
        "execution_freeze_sha256": file_sha256(execution_freeze),
        "packet_sha256": file_sha256(source),
        "cases": EXPECTED_CASES, "distinct_months": len(months),
    }


def _validate_attempt_chain(
    record: Mapping[str, Any], envelope: Mapping[str, Any], envelope_path: Path,
    run_number: int,
) -> int:
    successful = envelope.get("successful_attempt")
    if isinstance(successful, bool) or not isinstance(successful, int) or successful < 1:
        raise V4S1ConsistencyError("successful attempt is invalid")
    run_key = str(envelope.get("run_case_key") or "")
    attempt_dir = envelope_path.parent.parent / "attempts" / run_key
    paths = sorted(attempt_dir.glob("attempt_*.json")) if attempt_dir.is_dir() else []
    if [path.name for path in paths] != [f"attempt_{i:03d}.json" for i in range(1, successful + 1)]:
        raise V4S1ConsistencyError("attempt chain is incomplete, non-contiguous, or trailing")
    for number, path in enumerate(paths, 1):
        attempt = _read_json(path)
        for field, expected in {
            "runner_version": RUNNER_VERSION, "case_key": record["case_key"],
            "run_case_key": run_key, "run_number": run_number, "attempt": number,
        }.items():
            if attempt.get(field) != expected:
                raise V4S1ConsistencyError(f"attempt field changed: {field}")
        if number < successful:
            if set(attempt) != _RETRY_ATTEMPT_FIELDS or attempt.get("status") != "RETRYABLE_ERROR" or attempt.get("technical_failure") is not True:
                raise V4S1ConsistencyError("pre-success attempt was tampered")
        else:
            if set(attempt) != _VALIDATED_ATTEMPT_FIELDS or attempt.get("status") != "VALIDATED" or attempt.get("technical_failure") is not False:
                raise V4S1ConsistencyError("successful attempt was tampered")
            for field in ("output_sha256", "reviewer_audit_sha256", "reviewer_audit", "output"):
                if attempt.get(field) != envelope.get(field):
                    raise V4S1ConsistencyError("envelope differs from successful attempt")
    return successful


def validate_run_artifacts(
    records: Sequence[Mapping[str, Any]], run_roots: Sequence[Path], *, reviewer_sha256: str,
) -> tuple[list[dict[str, Any]], list[dict[str, dict[str, Any]]]]:
    if len(run_roots) != EXPECTED_RUNS:
        raise V4S1ConsistencyError("V4-S1 repeatability requires exactly three runs")
    roots = [Path(path).resolve() for path in run_roots]
    if len(set(roots)) != EXPECTED_RUNS:
        raise V4S1ConsistencyError("the three run roots must be distinct")
    expected = {str(row.get("review_id") or ""): row for row in records}
    if len(expected) != EXPECTED_CASES or "" in expected:
        raise V4S1ConsistencyError("expected run identities are not exact")
    runs: list[dict[str, dict[str, Any]]] = []
    summaries: list[dict[str, Any]] = []
    for run_number, root in enumerate(roots, 1):
        found: dict[str, dict[str, Any]] = {}
        attempts = 0
        paths = sorted(root.glob("s*/cases/*.json"))
        if not paths and (root / "cases").is_dir():
            paths = sorted((root / "cases").glob("*.json"))
        for path in paths:
            envelope = _read_json(path)
            review_id = str(envelope.get("review_id") or "")
            if review_id not in expected or review_id in found:
                raise V4S1ConsistencyError("unexpected or duplicate run review ID")
            record = expected[review_id]
            if set(envelope) != _ENVELOPE_FIELDS:
                raise V4S1ConsistencyError("run envelope fields are incomplete or unexpected")
            try:
                _validate_merge_envelope(dict(record), envelope, run_number)
            except ExecutionIntegrityError as exc:
                raise V4S1ConsistencyError("run envelope hash/identity changed") from exc
            if path.stem != envelope.get("run_case_key"):
                raise V4S1ConsistencyError("run envelope filename changed")
            audit = envelope.get("reviewer_audit") or {}
            if (
                audit.get("adapter_version") != reviewer_v3.ADAPTER_VERSION
                or audit.get("adapter_status") != reviewer_v3.ADAPTER_STATUS
                or audit.get("adapter_code_sha256") != reviewer_sha256
            ):
                raise V4S1ConsistencyError("reviewer adapter identity changed")
            attempts += _validate_attempt_chain(record, envelope, path, run_number)
            output = envelope.get("output")
            errors = validate_atomic(dict(record["packet"]), output)
            if errors:
                raise V4S1ConsistencyError(f"atomic schema/causal validation failed for {review_id}: {errors}")
            found[review_id] = envelope
        missing = sorted(set(expected) - set(found))
        if len(paths) != EXPECTED_CASES or missing:
            raise V4S1ConsistencyError(
                f"run {run_number} is not exact 36-case coverage; cases={len(paths)} missing={len(missing)}"
            )
        summaries.append({
            "run_number": run_number, "status": "PASS", "status_zh": "完整覆蓋通過",
            "cases": len(found), "attempt_artifacts": attempts,
        })
        runs.append(found)
    return summaries, runs


def decision_signature(decision: Mapping[str, Any]) -> dict[str, Any]:
    derived = decision.get("derived_structure") or {}
    return {
        "permission": decision.get("permission"), "route": decision.get("route"),
        "scenario": decision.get("scenario", derived.get("primary_scenario")),
        "stage": derived.get("stage"), "left_right_phase": derived.get("left_right_phase"),
        "reason_codes": list(decision.get("reason_codes") or []),
    }


def _rate(exact: int, total: int, threshold: float | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"exact": exact, "total": total, "rate": exact / total if total else 0.0}
    if threshold is not None:
        result.update({"threshold": threshold, "passed": result["rate"] >= threshold})
    return result


def evaluate_acceptance(
    *, schema_causal_rate: float, permission_rate: float, scenario_left_right_rate: float,
    unanimous_trade: int, unanimous_nontrade: int,
) -> dict[str, Any]:
    checks = {
        "schema_and_causal_fields": schema_causal_rate >= THRESHOLDS["schema_and_causal_fields"],
        "material_permission": permission_rate >= THRESHOLDS["material_permission"],
        "scenario_and_left_right_phase": scenario_left_right_rate >= THRESHOLDS["scenario_and_left_right_phase"],
        "minimum_unanimous_s1_trade_cases": unanimous_trade >= THRESHOLDS["minimum_unanimous_s1_trade_cases"],
        "minimum_unanimous_s1_nontrade_cases": unanimous_nontrade >= THRESHOLDS["minimum_unanimous_s1_nontrade_cases"],
    }
    passed = all(checks.values())
    return {
        "status": "PASS" if passed else "FAIL",
        "status_zh": "驗收通過" if passed else "驗收未通過",
        "checks": checks, "passed": passed,
    }


def conservative_decision(
    policy_decision: Mapping[str, Any], *, has_disagreement: bool
) -> tuple[str, str, dict[str, Any], str]:
    """Apply the case-level V4-S1 fail-closed overlay after atomic merge."""

    if not has_disagreement:
        return (
            "UNANIMOUS",
            "三輪一致",
            decision_signature(policy_decision),
            stage_permission_v4_s1(policy_decision),
        )
    signature = decision_signature(policy_decision)
    return (
        "UNKNOWN",
        "三輪不一致，保守視為未知",
        {
            "permission": "WAIT", "route": "NO_TRADE",
            "scenario": signature["scenario"], "stage": signature["stage"],
            "left_right_phase": signature["left_right_phase"],
            "reason_codes": ["THREE_RUN_DISAGREEMENT_CONSERVATIVE_UNKNOWN"],
        },
        "WAIT",
    )


def evaluate(
    *, source: Path, source_manifest: Path, track_manifest: Path,
    assignment_manifest: Path, execution_freeze: Path, stage_protocol: Path,
    research_protocol: Path, prompt: Path, schema: Path, policy: Path,
    run_roots: Sequence[Path],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records, _stage, protocol, track_integrity = verify_frozen_track(
        source=source, source_manifest=source_manifest, track_manifest=track_manifest,
        assignment_manifest=assignment_manifest, execution_freeze=execution_freeze,
        stage_protocol=stage_protocol, research_protocol=research_protocol,
        prompt=prompt, schema=schema, policy=policy,
    )
    freeze = _read_json(execution_freeze)
    v2_components = _component_index(freeze, "v2_components", V2_COMPONENT_PATHS)
    reviewer_sha = str(v2_components["reviewer"]["sha256"])
    run_integrity, runs = validate_run_artifacts(records, run_roots, reviewer_sha256=reviewer_sha)
    critical_ids = critical_question_ids(protocol, _read_json(schema))

    schema_exact = permission_exact = scenario_exact = 0
    unanimous_trade = unanimous_nontrade = 0
    pooled_exact = pooled_total = critical_exact = critical_total = 0
    permission_counts = [Counter() for _ in range(EXPECTED_RUNS)]
    per_case: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    for record in records:
        review_id = str(record["review_id"])
        packet = record["packet"]
        outputs = [run[review_id]["output"] for run in runs]
        validation = [list(validate_atomic(packet, output)) for output in outputs]
        if any(validation):  # defense in depth after envelope validation
            raise V4S1ConsistencyError(f"atomic schema/causal validation changed for {review_id}")
        schema_exact += 1
        decisions = [reduce_atomic_v4_s1(packet, output) for output in outputs]
        signatures = [decision_signature(value) for value in decisions]
        stage_permissions = [stage_permission_v4_s1(value) for value in decisions]
        permission_ok = len(set(stage_permissions)) == 1
        scenario_ok = (
            len({value["scenario"] for value in signatures}) == 1
            and len({value["left_right_phase"] for value in signatures}) == 1
        )
        permission_exact += int(permission_ok)
        scenario_exact += int(scenario_ok)
        if permission_ok and stage_permissions[0] == "TRADE":
            unanimous_trade += 1
        if permission_ok and stage_permissions[0] != "TRADE":
            unanimous_nontrade += 1
        for index, value in enumerate(stage_permissions):
            permission_counts[index][value] += 1

        maps = [result_map(output) for output in outputs]
        paths = sorted(set().union(*(set(mapping) for mapping in maps)))
        if any(set(mapping) != set(paths) for mapping in maps):
            raise V4S1ConsistencyError(f"three-run atomic manifest differs for {review_id}")
        critical_paths = [path for path in paths if path.rsplit(".", 1)[-1] in critical_ids]
        atom_exact = sum(len({mapping[path] for mapping in maps}) == 1 for path in paths)
        critical_case_exact = sum(len({mapping[path] for mapping in maps}) == 1 for path in critical_paths)
        pooled_exact += atom_exact
        pooled_total += len(paths)
        critical_exact += critical_case_exact
        critical_total += len(critical_paths)
        merged, merge_metrics = conservative_merge_atomic_outputs(outputs, critical_ids=critical_ids)
        merged_errors = validate_atomic(packet, merged)
        if merged_errors:
            raise V4S1ConsistencyError(f"conservative merge invalid for {review_id}: {merged_errors}")
        policy_merged = reduce_atomic_v4_s1(packet, merged)
        has_disagreement = atom_exact != len(paths) or not permission_ok or not scenario_ok
        (consensus_status, consensus_status_zh, conservative,
         conservative_stage) = conservative_decision(
            policy_merged, has_disagreement=has_disagreement
        )
        row = {
            "review_id": review_id,
            "atom_agreement": _rate(atom_exact, len(paths)),
            "critical_atom_agreement": _rate(critical_case_exact, len(critical_paths)),
            "material_permission_exact": permission_ok,
            "scenario_and_left_right_phase_exact": scenario_ok,
            "run_stage_permissions": stage_permissions,
            "run_decisions": signatures,
            "consensus_status": consensus_status,
            "consensus_status_zh": consensus_status_zh,
            "conservative_decision": conservative,
            "conservative_s1_stage_permission": conservative_stage,
            "disagreement_paths": merge_metrics["disagreement_paths"],
        }
        per_case.append(row)
        ledger.append({
            "review_id": review_id, "source_ordinal": record["source_ordinal"],
            "packet_sha256": record["packet_sha256"],
            "merge_policy": "UNANIMOUS_ELSE_UNKNOWN_AND_NO_TRADE",
            "consensus_status": consensus_status,
            "atomic_semantics": merged,
            "policy_reduced_v4_s1_decision": decision_signature(policy_merged),
            "conservative_decision": conservative,
            "s1_stage_permission": conservative_stage,
            "disagreement_paths": merge_metrics["disagreement_paths"],
            "critical_disagreement_paths": merge_metrics["critical_disagreement_paths"],
        })

    schema_metric = _rate(schema_exact, EXPECTED_CASES, THRESHOLDS["schema_and_causal_fields"])
    permission_metric = _rate(permission_exact, EXPECTED_CASES, THRESHOLDS["material_permission"])
    scenario_metric = _rate(scenario_exact, EXPECTED_CASES, THRESHOLDS["scenario_and_left_right_phase"])
    acceptance = evaluate_acceptance(
        schema_causal_rate=schema_metric["rate"], permission_rate=permission_metric["rate"],
        scenario_left_right_rate=scenario_metric["rate"], unanimous_trade=unanimous_trade,
        unanimous_nontrade=unanimous_nontrade,
    )
    passed = acceptance["passed"]
    execution = freeze.get("execution_contract") or {}
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "status": "REPEATABILITY_PASS" if passed else "REPEATABILITY_FAIL",
        "status_zh": "三輪一致性驗收通過" if passed else "三輪一致性驗收未通過",
        "classification": "OUTCOME_BLIND_V4_S1_REPEATABILITY_ONLY",
        "target_scenario": TARGET_SCENARIO, "target_route": TARGET_ROUTE,
        "expected_cases": EXPECTED_CASES, "run_count": EXPECTED_RUNS,
        "model": execution.get("model"), "reasoning_effort": execution.get("reasoning_effort"),
        "identity_visible": False, "future_or_performance_visible": False,
        "course_correctness_evaluated": False,
        "fail_closed_integrity": {
            "status": "PASS", "status_zh": "凍結輸入與三輪產物完整性通過",
            "track": track_integrity, "runs": run_integrity,
        },
        "thresholds": THRESHOLDS,
        "metrics": {
            "schema_and_causal_fields": schema_metric,
            "material_permission": permission_metric,
            "scenario_and_left_right_phase": scenario_metric,
            "all_atomic_answers_pooled": _rate(pooled_exact, pooled_total),
            "critical_atomic_answers_pooled": _rate(critical_exact, critical_total),
            "unanimous_s1_trade_cases": unanimous_trade,
            "unanimous_s1_nontrade_cases": unanimous_nontrade,
        },
        "acceptance": acceptance,
        "stage_permission_distributions_by_run": [
            {"run_number": i + 1, "counts": dict(sorted(values.items()))}
            for i, values in enumerate(permission_counts)
        ],
        "per_case": per_case,
        "performance": {
            "status": "SEALED", "status_zh": "績效維持封存",
            "evaluated": False,
            "reason": "CONSISTENCY_CANNOT_UNSEAL_OR_EVALUATE_PERFORMANCE",
        },
        "next_step": (
            "LOCK_OUTCOME_BLIND_COMMON_LEDGER_AND_RUN_COURSE_FIDELITY_AUDIT"
            if passed else "KEEP_PERFORMANCE_SEALED_AND_VERSION_BEFORE_RETEST"
        ),
        "limitations": [
            "REPEATABILITY_IS_NOT_COURSE_CORRECTNESS",
            "REPEATABILITY_IS_NOT_TRADING_PERFORMANCE",
            "IDENTITY_FUTURE_AND_PERFORMANCE_REMAIN_UNREAD",
        ],
    }
    report["report_sha256"] = canonical_sha256(report)
    return report, ledger


def render_markdown(report: Mapping[str, Any], ledger_path: Path | None = None) -> str:
    metrics = report["metrics"]
    lines = [
        "# V4-S1 情境1三輪一致性報告",
        "",
        f"- Status：`{report['status']}`（{report['status_zh']}）",
        f"- 範圍：{report['expected_cases']} 例 × {report['run_count']} 輪；`MATURE_TREND_PULLBACK / V2_CORE`。",
        "- 性質：結果盲化的一致性驗收，不是課程正確性或交易績效。",
        "- 股票身分、未來資料與績效：未讀取；績效仍為 `SEALED`（封存）。",
        "",
        "| 驗收項目 | 實際 | 門檻 | 結果 |",
        "| --- | ---: | ---: | --- |",
    ]
    for key, label in (
        ("schema_and_causal_fields", "Schema 與因果合法"),
        ("material_permission", "情境1 material permission"),
        ("scenario_and_left_right_phase", "情境與左右階段"),
    ):
        value = metrics[key]
        lines.append(
            f"| {label} | {value['rate']:.2%}（{value['exact']}/{value['total']}） | "
            f"{value['threshold']:.0%} | {'PASS（通過）' if value['passed'] else 'FAIL（未通過）'} |"
        )
    lines.extend([
        "", "## 覆蓋與保守合併", "",
        f"- 三輪一致的情境1交易案例：{metrics['unanimous_s1_trade_cases']}（最低 3）。",
        f"- 三輪一致的不交易／移除案例：{metrics['unanimous_s1_nontrade_cases']}（最低 3）。",
        f"- 全部原子答案一致率：{metrics['all_atomic_answers_pooled']['rate']:.2%}。",
        f"- 關鍵原子答案一致率：{metrics['critical_atomic_answers_pooled']['rate']:.2%}。",
        "- 任一三輪原子／material／情境左右階段分歧，common ledger 一律標 `UNKNOWN` 並強制 `WAIT / NO_TRADE`。",
        "", "## 後續", "",
    ])
    if report["acceptance"]["passed"]:
        lines.append("一致性驗收通過；只可鎖定結果盲化 common ledger 並進行課程符合度稽核，仍不可解封績效。")
        if ledger_path is not None:
            lines.append(f"Common ledger：`{Path(ledger_path).resolve()}`")
    else:
        lines.append("一致性驗收未通過；維持績效封存，另建版本後才能重測。")
    lines.extend(["", f"Report SHA-256：`{report['report_sha256']}`", ""])
    return "\n".join(lines)


def _publish(path: Path, payload: bytes) -> None:
    try:
        _publish_immutable(Path(path), payload)
    except Exception as exc:
        raise V4S1ConsistencyError(f"refusing to overwrite immutable artifact: {path}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--track-manifest", type=Path, required=True)
    parser.add_argument("--assignment-manifest", type=Path, required=True)
    parser.add_argument("--execution-freeze", type=Path, required=True)
    parser.add_argument("--stage-protocol", type=Path, required=True)
    parser.add_argument("--research-protocol", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, action="append", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--merged-ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    report, ledger = evaluate(
        source=args.source, source_manifest=args.source_manifest,
        track_manifest=args.track_manifest, assignment_manifest=args.assignment_manifest,
        execution_freeze=args.execution_freeze, stage_protocol=args.stage_protocol,
        research_protocol=args.research_protocol, prompt=args.prompt, schema=args.schema,
        policy=args.policy, run_roots=args.run_root,
    )
    ledger_path: Path | None = None
    if report["acceptance"]["passed"]:
        ledger_payload = _canonical_jsonl(ledger)
        _publish(args.merged_ledger, ledger_payload)
        ledger_path = args.merged_ledger
        report["merged_ledger"] = {
            "status": "LOCKED_OUTCOME_BLIND_COMMON_LEDGER",
            "status_zh": "結果盲化共同帳本已鎖定",
            "rows": len(ledger), "path": str(args.merged_ledger.resolve()),
            "sha256": hashlib.sha256(ledger_payload).hexdigest(),
        }
        report.pop("report_sha256", None)
        report["report_sha256"] = canonical_sha256(report)
    _publish(args.output_json, canonical_json_bytes(report) + b"\n")
    _publish(args.output_md, render_markdown(report, ledger_path).encode("utf-8"))
    print(json.dumps({
        "status": report["status"], "status_zh": report["status_zh"],
        "report_sha256": report["report_sha256"],
    }, ensure_ascii=False))
    return 0 if report["acceptance"]["passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
