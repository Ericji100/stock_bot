from __future__ import annotations

import copy
from pathlib import Path

import pytest

from scripts.hybrid_v3_candidate2_freeze_v1 import (
    PLAN_STATUS,
    Candidate2PlanError,
    build_candidate2_plan,
    candidate2_freeze_inputs,
    canonical_sha256,
    validate_candidate2_plan,
)
from scripts.hybrid_v3_freeze_v2 import pre_freeze_readiness_audit
from scripts.hybrid_v3_freeze_v2 import FreezeValidationError, _validate_pre_ai_hash_chain


def _seed_candidate2_components(root: Path) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for relative_path in (
        "scripts/hybrid_v3_objective_state_v3.py",
        "scripts/hybrid_v3_atomic_packets_v3.py",
        "scripts/hybrid_v3_consistency_v3.py",
        "scripts/hybrid_v3_course_gold_v3.py",
        "config/hybrid_multilabel_sampling_v3.json",
    ):
        (root / relative_path).write_text(relative_path, encoding="utf-8")


def test_candidate2_plan_is_draft_separate_and_has_no_lifecycle_cycle(tmp_path: Path):
    root = tmp_path
    _seed_candidate2_components(root)
    (root / "config/hybrid_monitoring_protocol_v2.json").write_text("{}", encoding="utf-8")
    old = root / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan/hybrid_monitoring_v2"
    old.mkdir(parents=True)
    (old / "review_point_manifest.json").write_text("{}", encoding="utf-8")
    plan = build_candidate2_plan(root)
    result = validate_candidate2_plan(plan)
    assert plan["plan_status"] == PLAN_STATUS == "DRAFT_NOT_FINAL"
    assert result["circular_dependency"] is False
    assert result["freeze_bindings"]["objective"] == "scripts/hybrid_v3_objective_state_v3.py"
    assert result["freeze_bindings"]["packet"] == "scripts/hybrid_v3_atomic_packets_v3.py"
    assert result["freeze_bindings"]["selector"] == "scripts/hybrid_v3_consistency_v3.py"
    assert result["freeze_bindings"]["sampling_contract"] == "config/hybrid_multilabel_sampling_v3.json"
    assert result["freeze_bindings"]["course_gold"] == "scripts/hybrid_v3_course_gold_v3.py"
    assert all(
        result["freeze_bindings"]["component_sha256"][name]
        for name in ("objective", "packet", "selector", "sampling_contract", "course_gold")
    )
    assert "hybrid_monitoring_candidate2" in result["freeze_bindings"]["sample_source_manifest"]
    assert plan["candidate1_preservation"]["output_dir"] != plan["candidate2_paths"]["output_dir"]
    order = result["topological_order"]
    assert order.index("LOCK_CANDIDATE2_SOURCE_AND_LABEL_COUNTS") < order.index(
        "LOCK_CANDIDATE2_PROTOCOL_COUNT_AND_QUOTAS"
    )
    assert order.index("FORMAL_CANDIDATE2_MASTER_FREEZE") < order.index("AI_EXECUTION")


def test_candidate1_collision_cycle_and_tamper_are_rejected(tmp_path: Path):
    _seed_candidate2_components(tmp_path)
    plan = build_candidate2_plan(tmp_path)
    collision = copy.deepcopy(plan)
    collision["candidate2_paths"]["output_dir"] = collision["candidate1_preservation"]["output_dir"]
    core = dict(collision)
    core.pop("plan_sha256")
    collision["plan_sha256"] = canonical_sha256(core)
    with pytest.raises(Candidate2PlanError, match="overlap"):
        validate_candidate2_plan(collision)

    cycle = copy.deepcopy(plan)
    cycle["lifecycle_dependencies"]["PIN_CANDIDATE1"] = ["AI_EXECUTION"]
    core = dict(cycle)
    core.pop("plan_sha256")
    cycle["plan_sha256"] = canonical_sha256(core)
    with pytest.raises(Candidate2PlanError, match="circular"):
        validate_candidate2_plan(cycle)

    tampered = copy.deepcopy(plan)
    tampered["candidate2_components"]["objective"]["path"] = "scripts/wrong.py"
    with pytest.raises(Candidate2PlanError, match="hash mismatch"):
        validate_candidate2_plan(tampered)


def test_candidate2_plan_rejects_noncanonical_selector_even_when_rehashed(tmp_path: Path):
    _seed_candidate2_components(tmp_path)
    plan = build_candidate2_plan(tmp_path)
    plan["candidate2_components"]["selector"]["path"] = "scripts/duplicate_selector.py"
    core = dict(plan)
    core.pop("plan_sha256")
    plan["plan_sha256"] = canonical_sha256(core)
    with pytest.raises(Candidate2PlanError, match="canonical Candidate 2 selector"):
        validate_candidate2_plan(plan)


def test_candidate2_freeze_inputs_point_only_to_candidate2_components_and_source(tmp_path: Path):
    inputs = candidate2_freeze_inputs(tmp_path)
    by_name = {component.name: component for component in inputs.components}
    assert inputs.protocol_relative_path == "config/hybrid_monitoring_protocol_v2_candidate2.json"
    assert inputs.builder_relative_path == "scripts/hybrid_v3_atomic_packets_v3.py"
    assert inputs.protocol_version == "hybrid-monitoring-protocol-v2-candidate2"
    assert inputs.review_point_manifest_version == "hybrid-v3-review-points-v3"
    assert inputs.consistency_plan_version_field == "plan_version"
    assert inputs.consistency_plan_version == "hybrid-v3-multilabel-holdout-plan-v3"
    assert inputs.consistency_plan_locked_status == "LOCKED"
    assert inputs.consistency_plan_sample_size_field == "cases_per_block"
    assert inputs.course_gold_bundle_version == "hybrid-v3-course-gold-bundle-v3"
    assert inputs.course_gold_rubrics_version == "hybrid-v3-course-gold-rubrics-v3"
    assert by_name["objective"].relative_path == "scripts/hybrid_v3_objective_state_v3.py"
    assert by_name["packet"].relative_path == "scripts/hybrid_v3_atomic_packets_v3.py"
    assert by_name["consistency"].relative_path == "scripts/hybrid_v3_consistency_v3.py"
    assert by_name["sampling_contract"].relative_path == "config/hybrid_multilabel_sampling_v3.json"
    assert by_name["course_gold"].relative_path == "scripts/hybrid_v3_course_gold_v3.py"
    assert "hybrid_monitoring_candidate2" in str(inputs.v2_review_point_manifest)
    assert "hybrid_monitoring_candidate2" in str(inputs.output)
    assert "hybrid_monitoring_v2" not in str(inputs.output)

    audit = pre_freeze_readiness_audit(inputs)
    assert audit["checks"]["protocol"]["path"].endswith("config\\hybrid_monitoring_protocol_v2_candidate2.json")
    assert audit["checks"]["review_point_builder_contract"]["path"].endswith(
        "scripts\\hybrid_v3_atomic_packets_v3.py"
    )


def test_candidate2_cannot_freeze_without_human_course_gold_rubrics(tmp_path: Path):
    inputs = candidate2_freeze_inputs(tmp_path)
    assert inputs.research_calibration_manifest is not None
    assert inputs.course_gold_bundle_manifest is not None
    inputs.research_calibration_manifest.parent.mkdir(parents=True, exist_ok=True)
    inputs.course_gold_bundle_manifest.parent.mkdir(parents=True, exist_ok=True)
    inputs.research_calibration_manifest.write_text("{}", encoding="utf-8")
    inputs.course_gold_bundle_manifest.write_text("{}", encoding="utf-8")
    assert inputs.course_gold_rubrics_manifest is not None
    assert not inputs.course_gold_rubrics_manifest.exists()
    with pytest.raises(FreezeValidationError, match="course-gold HUMAN_REVIEWED_FROZEN rubrics"):
        _validate_pre_ai_hash_chain(inputs, {"correctness_gate": {}, "holdout": {}})


def test_candidate2_plan_remains_draft_when_course_gold_tool_is_final(tmp_path: Path):
    _seed_candidate2_components(tmp_path)
    plan = build_candidate2_plan(tmp_path)
    assert plan["plan_status"] == "DRAFT_NOT_FINAL"
