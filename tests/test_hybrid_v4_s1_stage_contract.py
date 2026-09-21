from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAGE_PATH = ROOT / "config/hybrid_v4_s1_mature_stage_v1.json"
RULES_PATH = ROOT / "config/enlightenment_ai_rules_v4_s1.json"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_stage_is_exactly_one_route_and_36_by_3() -> None:
    stage = _json(STAGE_PATH)
    assert stage["status"] == "FINAL_RESEARCH_LOCKED"
    assert stage["target"] == {
        "primary_scenario": "MATURE_TREND_PULLBACK",
        "trade_route": "V2_CORE",
        "label_zh": "長多慣性拉回再發動完整合格",
        "other_scenarios_are_negative_controls": True,
        "other_v3_routes_suspended": True,
    }
    sample = stage["consistency_sample"]
    assert sample["cases"] == 36
    assert sample["runs"] == 3
    assert list(sample["quotas"]) == sample["focus_order"]
    assert sum(sample["quotas"].values()) == sample["cases"]
    assert sample["quotas"]["MATURE_SYMBOLIC_REACHABLE"] == 18
    assert sample["sampling_focus_must_not_enter_ai_packet"] is True


def test_stage_keeps_reachability_separate_from_ai_truth() -> None:
    stage = _json(STAGE_PATH)
    reachability = stage["reachability_preflight"]
    assert reachability["required_status"] == "PASS_SYMBOLIC_TARGET_ROUTE_REACHABLE"
    assert reachability["symbolic_witness_must_not_enter_ai_packet"] is True
    assert reachability["symbolic_classification_must_not_be_treated_as_expected_answer"] is True
    assert stage["rule_lineage"]["v1_v2_v3_are_read_only"] is True
    assert stage["rule_lineage"]["v4_s1_changes_v3"] is True


def test_stage_has_exact_acceptance_sequence_and_budget_guard() -> None:
    stage = _json(STAGE_PATH)
    assert stage["repeatability_acceptance"] == {
        "schema_and_causal_fields": 1.0,
        "material_permission": 0.95,
        "scenario_and_left_right_phase": 0.9,
        "minimum_unanimous_s1_trade_cases": 3,
        "minimum_unanimous_s1_nontrade_cases": 3,
    }
    assert stage["disagreement_policy"] == "UNANIMOUS_ELSE_UNKNOWN_AND_NO_TRADE"
    assert stage["sequence"][-1] == "STOP_BEFORE_SCENARIO_2"
    assert stage["position_order"] == ["V4_S1_FIXED", "V4_S1_ADD2"]
    assert stage["budget_guard"] == {
        "stop_when_remaining_percent_at_or_below": 20,
        "preserve_resume_checkpoint": True,
    }


def test_v1_v2_v3_protected_hashes_are_current() -> None:
    rules = _json(RULES_PATH)
    protected = rules["lineage"]["protected_files"]
    for relative, expected in protected.items():
        path = ROOT / relative
        assert path.is_file(), relative
        assert _sha256(path) == expected, relative
