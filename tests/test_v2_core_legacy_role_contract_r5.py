from __future__ import annotations

import json
from pathlib import Path
import re

from scripts.v2_core_legacy_role_contract_fit_v1 import build_fit_audit


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)


def load_json(name: str) -> dict:
    return json.loads((ARTIFACT_DIR / name).read_text(encoding="utf-8-sig"))


def test_r5_contract_has_all_roles_and_scenarios() -> None:
    contract = load_json("legacy_role_selection_contract.candidate_r5.json")
    assert set(contract["role_definitions"]) == {
        "CAMPAIGN_CONTEXT",
        "SCENARIO_WORKING",
        "PARENT",
        "EPISODE_STRUCTURE",
    }
    assert set(contract["scenario_role_profiles"]) == {
        "MATURE_TREND_PULLBACK",
        "MACRO_COPY_RESONANCE",
        "BEAR_REVERSAL_LEFT_RIGHT",
        "FRESH_Q1_EXPANSION",
        "UNRESOLVED_NO_TRADE",
    }
    assert contract["formal_model"] == "gpt-5.6-sol"
    assert contract["reasoning_effort"] == "xhigh"
    assert contract["program_responsibility"]["invent_subjective_role_answers"] is False
    assert contract["program_responsibility"]["grant_trade_permission_at_this_stage"] is False


def test_r5_contract_contains_no_case_specific_or_performance_rule() -> None:
    contract_text = (
        ARTIFACT_DIR / "legacy_role_selection_contract.candidate_r5.json"
    ).read_text(encoding="utf-8-sig")
    assert "FP-" not in contract_text
    assert re.search(r"20\d{2}-\d{2}-\d{2}", contract_text) is None
    lowered = contract_text.lower()
    for forbidden in ('"mfe"', '"mae"', '"return_pct"', '"winner"'):
        assert forbidden not in lowered


def test_r5_contract_markdown_and_json_are_structurally_synced() -> None:
    contract = load_json("legacy_role_selection_contract.candidate_r5.json")
    markdown = (ARTIFACT_DIR / "LEGACY_ROLE_SELECTION_CONTRACT_R5.md").read_text(
        encoding="utf-8-sig"
    )
    for role in contract["role_definitions"]:
        assert role in markdown
    for scenario in contract["scenario_role_profiles"]:
        if scenario != "UNRESOLVED_NO_TRADE":
            assert scenario in markdown
    for constraint in contract["anti_drift_constraints"]:
        assert constraint["code"] in markdown or constraint["code"] == "EQUIVALENT_BOUNDARY_CANDIDATES_ALLOWED"


def test_r5_calibration_coverage_is_complete_but_not_called_reproduction() -> None:
    audit = build_fit_audit(ARTIFACT_DIR)
    assert audit["case_count"] == 14
    assert audit["formal_ai_calls"] == 0
    assert audit["future_performance_used"] is False
    assert audit["identity_used"] is False
    assert audit["locked_reproduction_set_opened"] is False
    assert audit["summary"]["teacher_profile_represented_count"] == 14
    assert audit["summary"]["r4_mismatch_count"] == 13
    assert (
        audit["summary"]["r4_mismatch_targeted_by_at_least_one_constraint_count"]
        == 13
    )
    assert "NOT_FORMAL_REPRODUCTION" in audit["purpose"]
