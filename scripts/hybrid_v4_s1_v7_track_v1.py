"""Build an immutable 36-case V4-S1 track from the outcome-blind V7 source.

This is a research-only evidence revision.  It does not change the V4-S1
atomic questions, decision reducer, execution/position rules, model, or
reasoning effort.  Sampling labels and symbolic witnesses remain outside the
AI-visible packets.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from . import hybrid_v4_s1_track_v1 as base
    from .hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
        write_shards,
    )
except ImportError:  # pragma: no cover
    from scripts import hybrid_v4_s1_track_v1 as base
    from scripts.hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
        write_shards,
    )


REPORT_BASE = (
    ROOT
    / "reports/course_backtest/2024-02-02"
    / "historical_scan_2023h2_formal_ai_v3_daily_scan"
)
V4_ROOT = REPORT_BASE / "hybrid_monitoring_v4_s1_mature_v1"
SOURCE_ROOT = V4_ROOT / "v7_full_source_v1"

TRACK_VERSION = "hybrid-v4-s1-v7-evidence-research-track-v1"
FREEZE_VERSION = "hybrid-v4-s1-v7-evidence-execution-freeze-v1"
SELECTION_VERSION = "hybrid-v4-s1-v7-evidence-selection-v1"
PACKET_MANIFEST_VERSION = "hybrid-v4-s1-v7-evidence-packet-block-v1"
TRACK_STATUS = "FROZEN_OUTCOME_BLIND_V4_S1_V7_EVIDENCE_RESEARCH_ONLY"

MODEL = "gpt-5.6-sol"
REASONING = "xhigh"
STOP_REMAINING_PERCENT = 50

STAGE_PROTOCOL = ROOT / "config/hybrid_v4_s1_mature_stage_v1.json"
SOURCE_SAMPLING_CONTRACT = ROOT / "config/hybrid_multilabel_sampling_v3.json"
SOURCE = SOURCE_ROOT / "review_points.jsonl"
SOURCE_MANIFEST = SOURCE_ROOT / "review_point_manifest.json"
REACHABILITY_PREFLIGHT = V4_ROOT / "v7_reachability_preflight_v1/preflight.json"
REACHABILITY_PREFLIGHT_BUILDER = (
    ROOT / "scripts/hybrid_v4_s1_v7_reachability_preflight_v1.py"
)
V7_BUILDER = ROOT / "scripts/hybrid_v3_atomic_packets_v7.py"
V7_MERGE_ADAPTER = ROOT / "scripts/hybrid_v3_atomic_packets_v7_merge_adapter_v1.py"
V7_RANGE_WORKER = ROOT / "scripts/hybrid_v3_atomic_packets_v7_range_worker_v1.py"
V7_MERGE_WORKER = ROOT / "scripts/hybrid_v3_atomic_packets_v7_merge_adapter_v1.py"

PROMPT = base.PROMPT
SCHEMA = base.SCHEMA
RESEARCH_PROTOCOL = base.RESEARCH_PROTOCOL
V4_S1_POLICY = base.V4_S1_POLICY
SOURCE_VALIDATOR = base.SOURCE_VALIDATOR
SHARDING = base.SHARDING
EVALUATOR = ROOT / "scripts/hybrid_v4_s1_v7_consistency_v1.py"
EXECUTION_ORCHESTRATOR = (
    ROOT / "scripts/hybrid_v4_s1_v7_execution_orchestrator_v1.py"
)

EXPECTED_SOURCE_SHA256 = "f6a3ec650efeb60ea9bbe4b7d923d984e8ad833d1a61980f1b068e2317887b5e"
EXPECTED_SOURCE_MANIFEST_SHA256 = "c789c1efe03f91c5aa2480dbca1e1e8264fdc2886733b43eca5a4c2026ee58c3"
EXPECTED_V7_BUILDER_SHA256 = "09d61c36efdf6f19052f18a594f74a2278cc92ca217b3e4b3c5af2f51e8f6034"
EXPECTED_V7_MERGE_ADAPTER_SHA256 = "dd7a2d69623beaf6bc83814cd42436af2619f74d6ec752d3e636550975ae944a"
EXPECTED_PREFLIGHT_BUILDER_SHA256 = "eb415b33e077290c750b13bf06c0235102cd34f88e33c8e94fd4e728760aa2c2"
EXPECTED_PREFLIGHT_STATUS = "PASS_SYMBOLIC_TARGET_ROUTE_REACHABLE"
EXPECTED_PREFLIGHT_VERSION = "hybrid-v4-s1-v7-reachability-preflight-v1"
EXPECTED_SOURCE_STATUS = "BUILT_OUTCOME_BLIND_CANDIDATE_NOT_FORMAL_ACCEPTANCE"
EXPECTED_SOURCE_BUILDER_STATUS = "CANDIDATE_FOR_OUTCOME_BLIND_EVIDENCE_VALIDATION"
EXPECTED_SOURCE_SCOPE = {
    "stocks": 1029,
    "stock_days": 151804,
    "review_points": 7180,
    "monitoring_start": "2023-06-01",
    "monitoring_end": "2024-02-02",
}


class V7TrackError(ValueError):
    """The V7 source, preflight, selection, or freeze is not exact."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V7TrackError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise V7TrackError(f"expected JSON object: {path}")
    return value


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with Path(path).open(encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise V7TrackError(f"expected object at {path}:{line_number}")
                yield value
    except (OSError, json.JSONDecodeError) as exc:
        raise V7TrackError(f"cannot read JSONL: {path}") from exc


def _self_hash(value: Mapping[str, Any], field: str) -> bool:
    expected = value.get(field)
    core = {key: child for key, child in value.items() if key != field}
    return isinstance(expected, str) and expected == canonical_sha256(core)


def _canonical_jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def load_preflight(
    preflight_path: Path,
    *,
    source_path: Path,
    source_manifest_path: Path,
) -> dict[str, dict[str, Any]]:
    report = _read_json(preflight_path)
    if report.get("preflight_version") != EXPECTED_PREFLIGHT_VERSION:
        raise V7TrackError("unexpected V7 reachability preflight version")
    if report.get("status") != EXPECTED_PREFLIGHT_STATUS:
        raise V7TrackError("V7 target route did not pass reachability")
    if not _self_hash(report, "preflight_sha256"):
        raise V7TrackError("V7 preflight self-hash changed")
    for field, expected in {
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "ai_output_read": False,
        "symbolic_witness_inside_ai_packet": False,
        "symbolic_witness_has_schema_poison_pill": True,
    }.items():
        if report.get(field) is not expected:
            raise V7TrackError(f"V7 preflight blind flag changed: {field}")
    source = report.get("source") or {}
    if (
        source.get("sha256") != file_sha256(source_path)
        or source.get("manifest_sha256") != file_sha256(source_manifest_path)
        or source.get("manifest_status") != EXPECTED_SOURCE_STATUS
        or source.get("builder_status") != EXPECTED_SOURCE_BUILDER_STATUS
        or source.get("source_order_locked") is not False
    ):
        raise V7TrackError("V7 preflight/source binding changed")
    provenance = report.get("producer_provenance") or {}
    required_hashes = {
        "v7_packet_builder": EXPECTED_V7_BUILDER_SHA256,
        "merge_adapter": EXPECTED_V7_MERGE_ADAPTER_SHA256,
        "preflight_builder": EXPECTED_PREFLIGHT_BUILDER_SHA256,
    }
    for name, expected_hash in required_hashes.items():
        if (provenance.get(name) or {}).get("sha256") != expected_hash:
            raise V7TrackError(f"V7 preflight producer changed: {name}")
    if report.get("source_scope") != EXPECTED_SOURCE_SCOPE:
        raise V7TrackError("V7 preflight source scope changed")
    candidates = report.get("candidates")
    if not isinstance(candidates, list) or report.get("candidates_sha256") != canonical_sha256(candidates):
        raise V7TrackError("V7 preflight candidate index changed")
    minimal: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or candidate.get("classification") != "SYMBOLIC_REACHABLE_NOT_SEMANTIC_GOLD":
            raise V7TrackError("V7 symbolic candidate classification changed")
        review_id = str(candidate.get("review_id") or "")
        if not review_id or review_id in minimal:
            raise V7TrackError("V7 symbolic candidate identity is duplicated")
        minimal[review_id] = {
            "source_ordinal": int(candidate["source_ordinal"]),
            "anonymous_stock_id": str(candidate["anonymous_stock_id"]),
            "as_of": str(candidate["as_of"]),
            "packet_sha256": str(candidate["packet_sha256"]),
        }
    capacity = report.get("capacity") or {}
    actual = {
        "reachable_rows": len(minimal),
        "unique_anonymous_stocks": len({row["anonymous_stock_id"] for row in minimal.values()}),
        "distinct_months": len({row["as_of"][:7] for row in minimal.values()}),
    }
    if capacity != actual or actual["unique_anonymous_stocks"] < 18 or actual["distinct_months"] < 6:
        raise V7TrackError("V7 preflight capacity changed or is insufficient")
    return minimal


def scan_source(
    source_path: Path,
    source_manifest_path: Path,
    preflight_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]]]:
    if file_sha256(source_path) != EXPECTED_SOURCE_SHA256:
        raise V7TrackError("V7 source artifact hash changed")
    if file_sha256(source_manifest_path) != EXPECTED_SOURCE_MANIFEST_SHA256:
        raise V7TrackError("V7 source manifest hash changed")
    if file_sha256(V7_BUILDER) != EXPECTED_V7_BUILDER_SHA256:
        raise V7TrackError("V7 packet builder hash changed")
    if file_sha256(V7_MERGE_ADAPTER) != EXPECTED_V7_MERGE_ADAPTER_SHA256:
        raise V7TrackError("V7 merge adapter hash changed")
    if file_sha256(REACHABILITY_PREFLIGHT_BUILDER) != EXPECTED_PREFLIGHT_BUILDER_SHA256:
        raise V7TrackError("V7 preflight builder hash changed")
    manifest = _read_json(source_manifest_path)
    if (
        manifest.get("status") != EXPECTED_SOURCE_STATUS
        or manifest.get("builder_status") != EXPECTED_SOURCE_BUILDER_STATUS
        or manifest.get("builder_version") != "hybrid-v3-atomic-packets-v7"
        or manifest.get("source_order_locked") is not False
    ):
        raise V7TrackError("unexpected V7 candidate source state")
    for field, expected in {
        "outcome_blind": True,
        "identity_visible": False,
        "identity_isolated": True,
        "performance_visible": False,
        "future_data_visible": False,
    }.items():
        if manifest.get(field) is not expected:
            raise V7TrackError(f"V7 source blind flag changed: {field}")
    if manifest.get("artifact_sha256") != EXPECTED_SOURCE_SHA256:
        raise V7TrackError("V7 manifest artifact binding changed")
    if manifest.get("sampling_contract_sha256") != file_sha256(SOURCE_SAMPLING_CONTRACT):
        raise V7TrackError("V7 sampling contract binding changed")
    coverage = manifest.get("source_ordinal_coverage") or {}
    if coverage != {
        "covered": 7180, "expected": 7180, "first": 0, "last": 7179,
        "missing": 0, "overlap": 0, "unexpected": 0,
    }:
        raise V7TrackError("V7 source ordinal coverage changed")

    reachable = load_preflight(
        preflight_path,
        source_path=source_path,
        source_manifest_path=source_manifest_path,
    )
    reachable_ids = frozenset(reachable)
    source_contract = base.load_sampling_contract(SOURCE_SAMPLING_CONTRACT)
    records: list[dict[str, Any]] = []
    seen_reviews: set[str] = set()
    seen_ordinals: set[int] = set()
    matched_reachable: set[str] = set()
    for row in _jsonl(source_path):
        forbidden = base._find_source_leak(row)
        if forbidden is not None:
            raise V7TrackError(f"outcome/future key present in V7 source: {forbidden}")
        base.validate_source_record(row, source_contract)
        ordinal = int(row["source_ordinal"])
        review_id = str(row["review_id"])
        if ordinal in seen_ordinals or review_id in seen_reviews:
            raise V7TrackError("V7 source identity is duplicated")
        seen_ordinals.add(ordinal)
        seen_reviews.add(review_id)
        if review_id in reachable:
            base._validate_reachable_identity(row, reachable[review_id])
            matched_reachable.add(review_id)
        records.append({
            "source_ordinal": ordinal,
            "review_id": review_id,
            "anonymous_stock_id": row["anonymous_stock_id"],
            "packet_sha256": row["packet_sha256"],
            "as_of": row["packet"]["as_of"],
            "eligible_sampling_strata": list(row["eligible_sampling_strata"]),
            "eligible_stage_focuses": base.stage_focuses(row, reachable_ids),
        })
    if len(records) != 7180 or [row["source_ordinal"] for row in records] != list(range(7180)):
        raise V7TrackError("V7 source order/coverage is not exact")
    if matched_reachable != reachable_ids:
        raise V7TrackError("V7 preflight candidates are not exactly covered")
    if len({row["anonymous_stock_id"] for row in records}) != 1029:
        raise V7TrackError("V7 stock coverage changed")
    dates = [str(row["as_of"]) for row in records]
    if min(dates) != "2023-06-01" or max(dates) != "2024-02-02":
        raise V7TrackError("V7 date coverage changed")
    if manifest.get("stocks") != 1029 or manifest.get("stock_days_scanned") != 151804:
        raise V7TrackError("V7 source scope counts changed")
    return records, manifest, reachable


def _component(name: str, path: Path, *, status: str = "FINAL", role: str | None = None) -> dict[str, Any]:
    return base._component(name, path, status=status, role=role)


def _v4_components(preflight_path: Path) -> list[dict[str, Any]]:
    return [
        _component("stage_protocol", STAGE_PROTOCOL, status="FINAL_RESEARCH_LOCKED"),
        _component(
            "reachability_preflight", preflight_path,
            status=EXPECTED_PREFLIGHT_STATUS,
            role="SAMPLING_ONLY_NEVER_AI_VISIBLE",
        ),
        _component(
            "reachability_preflight_builder", REACHABILITY_PREFLIGHT_BUILDER,
            role="OUTCOME_BLIND_V7_SYMBOLIC_CLASSIFIER_PRODUCER",
        ),
        _component(
            "policy", V4_S1_POLICY,
            status="DRAFT_FOR_OUTCOME_BLIND_RESEARCH",
            role="UNCHANGED_V4_S1_DECISION_REDUCER",
        ),
        _component("track_builder", Path(__file__).resolve()),
        _component("evaluator", EVALUATOR),
        _component("execution_orchestrator", EXECUTION_ORCHESTRATOR),
    ]


def prepare(
    *,
    source_path: Path,
    source_manifest_path: Path,
    preflight_path: Path,
    output_dir: Path,
    shard_count: int = 4,
) -> dict[str, Any]:
    protocol = base._stage_protocol()
    records, source_manifest, reachable = scan_source(
        Path(source_path), Path(source_manifest_path), Path(preflight_path)
    )
    selected = base.allocate(records, protocol)
    packets = base.extract_packets(Path(source_path), selected)
    output_dir = Path(output_dir).resolve()

    focus_counts = Counter(row["sampling_focus"] for row in selected)
    month_counts = Counter(str(row["as_of"])[:7] for row in selected)
    plan_core = {
        "selection_version": SELECTION_VERSION,
        "status": "LOCKED_OUTCOME_BLIND",
        "classification": protocol["classification"],
        "stage_protocol_sha256": file_sha256(STAGE_PROTOCOL),
        "reachability_preflight_sha256": file_sha256(preflight_path),
        "source_artifact_sha256": file_sha256(source_path),
        "source_manifest_sha256": file_sha256(source_manifest_path),
        "source_manifest_status": source_manifest["status"],
        "source_rows": int(source_manifest["review_points"]),
        "preflight_reachable_capacity": len(reachable),
        "cases": len(selected),
        "one_case_per_anonymous_stock": True,
        "minimum_distinct_months": int(protocol["consistency_sample"]["minimum_distinct_months"]),
        "maximum_cases_per_month": int(protocol["consistency_sample"]["maximum_cases_per_month"]),
        "sampling_focus_is_validation_only": True,
        "symbolic_classification_is_not_expected_answer": True,
        "symbolic_witness_present": False,
        "identity_visible_inside_ai_packet": False,
        "future_or_performance_visible": False,
        "focus_counts": {
            focus: focus_counts[focus]
            for focus in protocol["consistency_sample"]["focus_order"]
        },
        "month_counts": dict(sorted(month_counts.items())),
        "rows": selected,
        "rows_sha256": canonical_sha256(selected),
    }
    selection_plan = {**plan_core, "selection_sha256": canonical_sha256(plan_core)}
    selection_path = output_dir / "selection_plan.json"
    _publish_immutable(selection_path, canonical_json_bytes(selection_plan) + b"\n")

    packet_payload = _canonical_jsonl(packets)
    packet_path = output_dir / "primary_packets.jsonl"
    _publish_immutable(packet_path, packet_payload)
    mapping = [
        {
            "selected_ordinal": index,
            "source_ordinal": int(row["source_ordinal"]),
            "review_id": row["review_id"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "packet_sha256": row["packet_sha256"],
            "sampling_focus": row["sampling_focus"],
            "eligible_stage_focuses": row["eligible_stage_focuses"],
        }
        for index, row in enumerate(selected)
    ]
    packet_manifest_core = {
        "manifest_version": PACKET_MANIFEST_VERSION,
        "status": "LOCKED_OUTCOME_BLIND",
        "block_id": "V4_S1_MATURE_TREND_PULLBACK_V2_CORE_V7_EVIDENCE",
        "rows": len(packets),
        "packets_canonical_jsonl_sha256": hashlib.sha256(packet_payload).hexdigest(),
        "sampling_metadata_inside_packet": False,
        "symbolic_witness_inside_packet": False,
        "symbolic_classification_inside_packet": False,
        "all_scenario_hypotheses_preserved_inside_packet": True,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
        "mapping": mapping,
        "mapping_sha256": canonical_sha256(mapping),
    }
    packet_manifest = {
        **packet_manifest_core,
        "manifest_sha256": canonical_sha256(packet_manifest_core),
    }
    packet_manifest_path = output_dir / "primary_packets.manifest.json"
    _publish_immutable(packet_manifest_path, canonical_json_bytes(packet_manifest) + b"\n")

    freeze_core = {
        "freeze_version": FREEZE_VERSION,
        "status": "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION",
        "track_version": TRACK_VERSION,
        "track_status": TRACK_STATUS,
        "classification": protocol["classification"],
        "expected_cases": 36,
        "expected_runs": 3,
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "old_ai_output_visible": False,
        "sampling_metadata_inside_ai_packet": False,
        "symbolic_witness_inside_ai_packet": False,
        "symbolic_classification_inside_ai_packet": False,
        "symbolic_classification_is_expected_answer": False,
        "policy_decision_changed_relative_to_v3": True,
        "strategy_or_gate_change": True,
        "v4_s1_is_rule_correction_relative_to_v3": True,
        "v4_s1_changes_v3": True,
        "v1_v2_v3_read_only": True,
        "v1_v2_v3_are_read_only": True,
        "v1_v2_v3_rules_unchanged": True,
        "v1_v2_v3_artifacts_modified": False,
        "ai_atomic_questions_changed": False,
        "ai_evidence_format_changed": True,
        "ai_evidence_format_changed_relative_to_old_v4_s1": True,
        "execution_or_position_rules_changed": False,
        "single_route_scope": {
            "primary_scenario": base.TARGET_SCENARIO,
            "trade_route": base.TARGET_ROUTE,
            "other_scenarios_are_negative_controls": True,
            "other_routes_suspended": True,
        },
        "repeatability_acceptance": deepcopy(protocol["repeatability_acceptance"]),
        "disagreement_policy": protocol["disagreement_policy"],
        "budget_guard": {
            "stop_when_remaining_percent_at_or_below": STOP_REMAINING_PERCENT,
            "preserve_resume_checkpoint": True,
            "enforcement_boundary": "EXTERNAL_EXECUTION_ORCHESTRATOR",
            "enforcement_component": "execution_orchestrator",
            "stage_protocol_default_percent": protocol["budget_guard"]["stop_when_remaining_percent_at_or_below"],
            "runtime_user_override": "2026-09-09_REMAINING_50_PERCENT",
        },
        "stage_sequence": list(protocol["sequence"]),
        "stage_protocol_sha256": file_sha256(STAGE_PROTOCOL),
        "reachability_preflight_sha256": file_sha256(preflight_path),
        "candidate_source_artifact_sha256": file_sha256(source_path),
        "candidate_source_manifest_sha256": file_sha256(source_manifest_path),
        "selection_plan_sha256": file_sha256(selection_path),
        "primary_packet_sha256": file_sha256(packet_path),
        "primary_packet_manifest_sha256": file_sha256(packet_manifest_path),
        "v2_components": base._v2_components(),
        "v4_s1_components": _v4_components(preflight_path),
        "support_components": [
            _component("v7_packet_builder", V7_BUILDER, status=EXPECTED_SOURCE_BUILDER_STATUS),
            _component("v7_merge_adapter", V7_MERGE_ADAPTER, status="FINAL_TECHNICAL_ADAPTER"),
            _component("source_sampling_contract", SOURCE_SAMPLING_CONTRACT),
            _component("source_validator", SOURCE_VALIDATOR),
            _component("sharding", SHARDING),
        ],
        "execution_contract": {"model": MODEL, "reasoning_effort": REASONING},
    }
    freeze = {**freeze_core, "freeze_sha256": canonical_sha256(freeze_core)}
    freeze_path = output_dir / "research_execution_freeze.json"
    _publish_immutable(freeze_path, canonical_json_bytes(freeze) + b"\n")

    case_records = build_case_records(
        packets,
        source_manifest_sha256=file_sha256(packet_path),
        protocol_sha256=file_sha256(RESEARCH_PROTOCOL),
        prompt_sha256=file_sha256(PROMPT),
        schema_sha256=file_sha256(SCHEMA),
        execution_contract_sha256=file_sha256(freeze_path),
        model=MODEL,
        reasoning_effort=REASONING,
    )
    assignment = write_shards(
        case_records, output_dir / "primary_cases", shard_count=shard_count
    )
    assignment_path = output_dir / "primary_cases/assignment.manifest.json"
    track_core = {
        "track_version": TRACK_VERSION,
        "status": TRACK_STATUS,
        "classification": protocol["classification"],
        "stage": "MATURE_TREND_PULLBACK_V2_CORE_ONLY_V7_EVIDENCE",
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "model": MODEL,
        "reasoning_effort": REASONING,
        "cases": len(packets),
        "runs": int(protocol["consistency_sample"]["runs"]),
        "selection_plan_path": str(selection_path),
        "selection_plan_sha256": file_sha256(selection_path),
        "packet_path": str(packet_path),
        "packet_sha256": file_sha256(packet_path),
        "packet_manifest_path": str(packet_manifest_path),
        "packet_manifest_sha256": file_sha256(packet_manifest_path),
        "execution_freeze_path": str(freeze_path),
        "execution_freeze_sha256": file_sha256(freeze_path),
        "assignment_manifest_path": str(assignment_path),
        "assignment_manifest_sha256": file_sha256(assignment_path),
        "assignment_sha256": assignment["assignment_sha256"],
        "policy_decision_changed_relative_to_v3": True,
        "v4_s1_changes_v3": True,
        "v1_v2_v3_read_only": True,
        "v7_source_is_candidate_not_formal_course_acceptance": True,
        "performance_must_remain_sealed_until_prior_gates_pass": True,
    }
    track = {**track_core, "track_manifest_sha256": canonical_sha256(track_core)}
    track_path = output_dir / "research_track_manifest.json"
    _publish_immutable(track_path, canonical_json_bytes(track) + b"\n")
    return track


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--source-manifest", type=Path, default=SOURCE_MANIFEST)
    parser.add_argument("--preflight", type=Path, default=REACHABILITY_PREFLIGHT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--shard-count", type=int, default=4)
    args = parser.parse_args()
    result = prepare(
        source_path=args.source.resolve(),
        source_manifest_path=args.source_manifest.resolve(),
        preflight_path=args.preflight.resolve(),
        output_dir=args.output_dir.resolve(),
        shard_count=args.shard_count,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
