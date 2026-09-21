"""DRAFT lifecycle and freeze-binding plan for rebuilt Candidate 2.

This module creates no review points and performs no AI calls.  It preserves
Candidate 1 by hash and describes a separate Candidate 2 component tree and
output tree that a later formal freeze validator can consume explicitly.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

try:
    from .hybrid_v3_consistency_v3 import SELECTOR_VERSION, STRATA
    from .hybrid_v3_freeze_v2 import ComponentSpec, DEFAULT_COMPONENTS, FreezeInputs
except ImportError:
    from hybrid_v3_consistency_v3 import SELECTOR_VERSION, STRATA
    from hybrid_v3_freeze_v2 import ComponentSpec, DEFAULT_COMPONENTS, FreezeInputs


PLAN_VERSION = "hybrid-monitoring-candidate2-freeze-plan-v1"
PLAN_STATUS = "DRAFT_NOT_FINAL"
CANDIDATE_ID = "HYBRID_MONITORING_CANDIDATE2_REBUILT_HYPOTHESES"
SAMPLING_CONTRACT_VERSION = "hybrid-v3-multilabel-sampling-v3"
ASSIGNMENT_ALGORITHM = "GLOBAL_CASE_DISJOINT_DETERMINISTIC_MAX_FLOW_SEEDED_ORDER"


def _candidate2_components() -> tuple[ComponentSpec, ...]:
    replacements = {
        "protocol_doc": ComponentSpec(
            "protocol_doc",
            "docs/hybrid-monitoring-protocol-v2-candidate2.md",
            "markdown",
            "hybrid-monitoring-protocol-v2-candidate2",
        ),
        "protocol": ComponentSpec(
            "protocol",
            "config/hybrid_monitoring_protocol_v2_candidate2.json",
            "json",
            "hybrid-monitoring-protocol-v2-candidate2",
            ("protocol_version",),
            ("status",),
        ),
        "objective": ComponentSpec(
            "objective",
            "scripts/hybrid_v3_objective_state_v3.py",
            "python",
            "hybrid-v3-objective-state-v3",
            ("ENGINE_VERSION", "OBJECTIVE_ENGINE_VERSION", "COMPONENT_VERSION"),
            ("ENGINE_STATUS", "OBJECTIVE_ENGINE_STATUS", "COMPONENT_STATUS"),
        ),
        "packet": ComponentSpec(
            "packet",
            "scripts/hybrid_v3_atomic_packets_v3.py",
            "python",
            "hybrid-v3-atomic-packets-v3",
            ("BUILDER_VERSION", "PACKET_BUILDER_VERSION", "COMPONENT_VERSION"),
            ("BUILDER_STATUS", "PACKET_BUILDER_STATUS", "COMPONENT_STATUS"),
        ),
        "consistency": ComponentSpec(
            "consistency",
            "scripts/hybrid_v3_consistency_v3.py",
            "python",
            SELECTOR_VERSION,
            ("SELECTOR_VERSION", "CONSISTENCY_VERSION", "COMPONENT_VERSION"),
            ("SELECTOR_STATUS", "CONSISTENCY_STATUS", "COMPONENT_STATUS"),
        ),
        "course_gold": ComponentSpec(
            "course_gold",
            "scripts/hybrid_v3_course_gold_v3.py",
            "python",
            "hybrid-v3-course-gold-v3-multilabel",
            ("COURSE_GOLD_VERSION", "COMPONENT_VERSION"),
            ("COURSE_GOLD_STATUS", "COMPONENT_STATUS"),
        ),
    }
    components = tuple(replacements.get(spec.name, spec) for spec in DEFAULT_COMPONENTS)
    return components + (
        ComponentSpec(
            "sampling_contract",
            "config/hybrid_multilabel_sampling_v3.json",
            "json",
            SAMPLING_CONTRACT_VERSION,
            ("contract_version",),
            ("status",),
        ),
    )


class Candidate2PlanError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _pin(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve()), "sha256": file_sha256(path) if path.is_file() else None}


def _component(root: Path, relative_path: str, version: str) -> dict[str, Any]:
    path = root / relative_path
    return {
        "path": relative_path,
        "version": version,
        "sha256": file_sha256(path) if path.is_file() else None,
    }


def build_candidate2_plan(root: Path) -> dict[str, Any]:
    root = root.resolve()
    run = root / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan"
    candidate1 = run / "hybrid_monitoring_v2"
    candidate2 = run / "hybrid_monitoring_candidate2"
    work2 = run / "hybrid_monitoring_candidate2_build_work"
    components = {
        "protocol_doc": _component(
            root,
            "docs/hybrid-monitoring-protocol-v2-candidate2.md",
            "hybrid-monitoring-protocol-v2-candidate2",
        ),
        "protocol": _component(
            root,
            "config/hybrid_monitoring_protocol_v2_candidate2.json",
            "hybrid-monitoring-protocol-v2-candidate2",
        ),
        "objective": _component(root, "scripts/hybrid_v3_objective_state_v3.py", "hybrid-v3-objective-state-v3"),
        "packet": _component(root, "scripts/hybrid_v3_atomic_packets_v3.py", "hybrid-v3-atomic-packets-v3"),
        "selector": _component(root, "scripts/hybrid_v3_consistency_v3.py", SELECTOR_VERSION),
        "sampling_contract": _component(
            root,
            "config/hybrid_multilabel_sampling_v3.json",
            SAMPLING_CONTRACT_VERSION,
        ),
        "course_gold": _component(
            root,
            "scripts/hybrid_v3_course_gold_v3.py",
            "hybrid-v3-course-gold-v3-multilabel",
        ),
    }
    shared = {
        "schema": _pin(root / "config/hybrid_atomic_semantics_v2.schema.json"),
        "prompt": _pin(root / "config/hybrid_semantic_prompt_v2.md"),
        "policy": _pin(root / "scripts/hybrid_v3_atomic_policy_v2.py"),
        "runner": _pin(root / "scripts/hybrid_v3_atomic_runner_v2.py"),
        "sharding": _pin(root / "scripts/hybrid_v3_sharding_v2.py"),
        "reviewer": _pin(root / "scripts/hybrid_v3_codex_reviewer_v2.py"),
        "research_calibration": _pin(root / "scripts/hybrid_v2_research_calibration.py"),
    }
    lifecycle = {
        "PIN_CANDIDATE1": [],
        "FINALIZE_CANDIDATE2_OBJECTIVE_PACKET_SELECTOR": ["PIN_CANDIDATE1"],
        "BUILD_FULL_OUTCOME_BLIND_CANDIDATE2_SOURCE": ["FINALIZE_CANDIDATE2_OBJECTIVE_PACKET_SELECTOR"],
        "LOCK_CANDIDATE2_SOURCE_AND_LABEL_COUNTS": ["BUILD_FULL_OUTCOME_BLIND_CANDIDATE2_SOURCE"],
        "LOCK_CANDIDATE2_PROTOCOL_COUNT_AND_QUOTAS": ["LOCK_CANDIDATE2_SOURCE_AND_LABEL_COUNTS"],
        "MAX_FLOW_PRIMARY_PLUS_TWO_RESERVES": ["LOCK_CANDIDATE2_PROTOCOL_COUNT_AND_QUOTAS"],
        "FREEZE_RESEARCH_AND_BLINDED_GOLD": ["LOCK_CANDIDATE2_PROTOCOL_COUNT_AND_QUOTAS"],
        "LOCK_EXECUTION_MANIFEST": ["MAX_FLOW_PRIMARY_PLUS_TWO_RESERVES", "FREEZE_RESEARCH_AND_BLINDED_GOLD"],
        "FORMAL_CANDIDATE2_MASTER_FREEZE": ["LOCK_EXECUTION_MANIFEST"],
        "AI_EXECUTION": ["FORMAL_CANDIDATE2_MASTER_FREEZE"],
    }
    core = {
        "plan_version": PLAN_VERSION,
        "plan_status": PLAN_STATUS,
        "candidate_id": CANDIDATE_ID,
        "candidate1_preservation": {
            "protocol": _pin(root / "config/hybrid_monitoring_protocol_v2.json"),
            "review_point_manifest": _pin(candidate1 / "review_point_manifest.json"),
            "output_dir": str(candidate1.resolve()),
            "immutable_and_never_relabelled": True,
        },
        "candidate2_components": components,
        "shared_components_pinned_by_hash": shared,
        "candidate2_paths": {
            "work_dir": str(work2.resolve()),
            "output_dir": str(candidate2.resolve()),
            "review_point_source": str((candidate2 / "review_points.jsonl").resolve()),
            "review_point_manifest": str((candidate2 / "review_point_manifest.json").resolve()),
            "holdout_plan": str((candidate2 / "consistency/holdout_plan.json").resolve()),
            "execution_manifest": str((candidate2 / "execution_manifest.json").resolve()),
            "master_freeze": str((candidate2 / "freeze_manifest.json").resolve()),
        },
        "source_row_contract": {
            "eligible_sampling_strata_field": "eligible_sampling_strata",
            "primary_sampling_focus_field": "primary_sampling_focus",
            "labels_sha256_field": "eligible_sampling_strata_sha256",
            "legacy_sampling_stratum_forbidden": True,
            "sampling_metadata_inside_ai_packet_forbidden": True,
            "canonical_labels": list(STRATA),
        },
        "holdout_contract": {
            "algorithm": ASSIGNMENT_ALGORITHM,
            "blocks": ["PRIMARY", "RESERVE_01", "RESERVE_02"],
            "one_case_one_quota_slot_globally": True,
            "review_ids_globally_disjoint": True,
            "primary_sampling_focus_is_diagnostic_only": True,
            "quotas_locked_only_after_full_source_counts": True,
        },
        "lifecycle_dependencies": lifecycle,
        "no_ai_before_master_freeze": True,
        "future_or_performance_inputs_forbidden": True,
    }
    return {**core, "plan_sha256": canonical_sha256(core)}


def candidate2_freeze_inputs(root: Path) -> FreezeInputs:
    """Point the existing fail-closed freeze validator at Candidate2 only."""
    root = root.resolve()
    run = root / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan"
    v1 = run / "hybrid_monitoring_v1"
    candidate2 = run / "hybrid_monitoring_candidate2"
    return FreezeInputs(
        root=root,
        v1_protocol_manifest=v1 / "protocol_freeze_manifest.json",
        v1_execution_manifest=v1 / "execution_freeze_manifest.json",
        source_manifest=run / "packet_manifest.json",
        v2_review_point_manifest=candidate2 / "review_point_manifest.json",
        output=candidate2 / "freeze_manifest.json",
        components=_candidate2_components(),
        protocol_relative_path="config/hybrid_monitoring_protocol_v2_candidate2.json",
        builder_relative_path="scripts/hybrid_v3_atomic_packets_v3.py",
        protocol_version="hybrid-monitoring-protocol-v2-candidate2",
        review_point_manifest_version="hybrid-v3-review-points-v3",
        consistency_plan_version_field="plan_version",
        consistency_plan_version="hybrid-v3-multilabel-holdout-plan-v3",
        consistency_plan_locked_status="LOCKED",
        consistency_plan_sample_size_field="cases_per_block",
        course_gold_bundle_version="hybrid-v3-course-gold-bundle-v3",
        course_gold_rubrics_version="hybrid-v3-course-gold-rubrics-v3",
        research_calibration_manifest=candidate2 / "research_calibration/human_frozen_manifest.json",
        course_gold_bundle_manifest=candidate2 / "course_gold/bundle_manifest.json",
        course_gold_rubrics_manifest=candidate2 / "course_gold/human_frozen_rubrics_manifest.json",
        consistency_sample_manifest=candidate2 / "consistency/holdout_plan.json",
        execution_manifest=candidate2 / "execution_manifest.json",
    )


def _topological_order(graph: Mapping[str, list[str]]) -> list[str]:
    resolved: set[str] = set()
    remaining = {name: set(dependencies) for name, dependencies in graph.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(name for name, dependencies in remaining.items() if dependencies <= resolved)
        if not ready:
            raise Candidate2PlanError(f"circular lifecycle dependency: {sorted(remaining)}")
        for name in ready:
            order.append(name)
            resolved.add(name)
            remaining.pop(name)
    return order


def validate_candidate2_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if plan.get("plan_version") != PLAN_VERSION or plan.get("plan_status") != PLAN_STATUS:
        raise Candidate2PlanError("Candidate 2 plan must remain the declared DRAFT contract")
    core = dict(plan)
    supplied = core.pop("plan_sha256", None)
    if supplied != canonical_sha256(core):
        raise Candidate2PlanError("Candidate 2 plan hash mismatch")
    old = plan["candidate1_preservation"]
    paths = plan["candidate2_paths"]
    if old.get("immutable_and_never_relabelled") is not True:
        raise Candidate2PlanError("Candidate 1 preservation is mandatory")
    if Path(old["output_dir"]).resolve() == Path(paths["output_dir"]).resolve():
        raise Candidate2PlanError("Candidate 2 output must not overlap Candidate 1")
    if Path(paths["work_dir"]).resolve() == Path(paths["output_dir"]).resolve():
        raise Candidate2PlanError("Candidate 2 work and formal output directories must differ")
    components = plan["candidate2_components"]
    if components["objective"]["path"] != "scripts/hybrid_v3_objective_state_v3.py":
        raise Candidate2PlanError("freeze binding must use Candidate 2 objective v3")
    if components["packet"]["path"] != "scripts/hybrid_v3_atomic_packets_v3.py":
        raise Candidate2PlanError("freeze binding must use Candidate 2 packet v3")
    contract = plan["source_row_contract"]
    if contract.get("legacy_sampling_stratum_forbidden") is not True or contract.get(
        "sampling_metadata_inside_ai_packet_forbidden"
    ) is not True:
        raise Candidate2PlanError("Candidate 2 sampling isolation is mandatory")
    if contract.get("canonical_labels") != list(STRATA):
        raise Candidate2PlanError("Candidate 2 canonical label set changed")
    if components["selector"].get("path") != "scripts/hybrid_v3_consistency_v3.py" or components[
        "selector"
    ].get("version") != SELECTOR_VERSION:
        raise Candidate2PlanError("freeze binding must use the canonical Candidate 2 selector")
    if components["sampling_contract"].get("path") != "config/hybrid_multilabel_sampling_v3.json" or components[
        "sampling_contract"
    ].get("version") != SAMPLING_CONTRACT_VERSION:
        raise Candidate2PlanError("freeze binding must use the canonical Candidate 2 sampling contract")
    if components["course_gold"].get("path") != "scripts/hybrid_v3_course_gold_v3.py" or components[
        "course_gold"
    ].get("version") != "hybrid-v3-course-gold-v3-multilabel":
        raise Candidate2PlanError("freeze binding must use Candidate 2 course-gold v3")
    for component_name in ("objective", "packet", "selector", "sampling_contract", "course_gold"):
        if not components[component_name].get("sha256"):
            raise Candidate2PlanError(f"Candidate 2 {component_name} code/config hash is not pinned")
    holdout = plan["holdout_contract"]
    if holdout.get("algorithm") != ASSIGNMENT_ALGORITHM or holdout.get("blocks") != [
        "PRIMARY", "RESERVE_01", "RESERVE_02"
    ]:
        raise Candidate2PlanError("Candidate 2 multi-label holdout contract changed")
    order = _topological_order(plan["lifecycle_dependencies"])
    if order.index("LOCK_CANDIDATE2_SOURCE_AND_LABEL_COUNTS") > order.index(
        "LOCK_CANDIDATE2_PROTOCOL_COUNT_AND_QUOTAS"
    ):
        raise Candidate2PlanError("protocol count/quotas cannot precede the outcome-blind source")
    if order.index("FORMAL_CANDIDATE2_MASTER_FREEZE") > order.index("AI_EXECUTION"):
        raise Candidate2PlanError("AI cannot precede the Candidate 2 master freeze")
    return {
        "valid": True,
        "circular_dependency": False,
        "topological_order": order,
        "freeze_bindings": {
            "protocol": components["protocol"]["path"],
            "objective": components["objective"]["path"],
            "packet": components["packet"]["path"],
            "selector": components["selector"]["path"],
            "sampling_contract": components["sampling_contract"]["path"],
            "course_gold": components["course_gold"]["path"],
            "component_sha256": {
                name: value.get("sha256")
                for name, value in components.items()
                if name in {"protocol", "objective", "packet", "selector", "sampling_contract", "course_gold"}
            },
            "sample_source_manifest": paths["review_point_manifest"],
            "sample_plan": paths["holdout_plan"],
            "master_freeze": paths["master_freeze"],
        },
    }
