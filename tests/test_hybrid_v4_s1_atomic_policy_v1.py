from __future__ import annotations

import copy
import json
from pathlib import Path

from scripts import hybrid_v3_atomic_policy_v2 as v2
from scripts import hybrid_v3_atomic_policy_v3 as v3
from scripts import hybrid_v4_s1_atomic_policy_v1 as v4
from tests.test_hybrid_v3_atomic_policy_v2 import (
    _hypothesis,
    _packet,
    _semantic,
    _set_atom,
)


def _mature_case(generation: str = "COPY_LEG_5") -> tuple[dict, dict]:
    packet = _packet()
    mature = _hypothesis(packet, "MATURE_TREND_PULLBACK")
    mature["taiji_generation"] = generation
    mature["same_direction_attack_number"] = (
        3 if generation == "COPY_LEG_5" else 4
    )
    mature["completed_prior_copy_count"] = (
        1 if generation == "COPY_LEG_5" else 2
    )
    packet["question_manifest"] = v2.expected_question_manifest(packet)
    semantic = _semantic(packet)
    _set_atom(
        semantic,
        "relation_candidates",
        "RELATION_CANDIDATE:R-MACRO",
        "REL_CURRENT_LEG_IS_REPLICATION",
        "FAIL",
    )
    _set_atom(
        semantic,
        "relation_candidates",
        "RELATION_CANDIDATE:R-MATURE",
        "LONG_CAMPAIGN_REPEATED_SUCCESS",
        "PASS",
    )
    return packet, semantic


def test_fifth_generation_is_not_automatically_late() -> None:
    packet, semantic = _mature_case("COPY_LEG_5")
    old = v3.reduce_atomic_v3(packet, semantic)
    new = v4.reduce_atomic_v4_s1(packet, semantic)

    assert old["permission"] == "WAIT"
    assert new["permission"] == "TRADE"
    assert new["route"] == "V2_CORE"
    assert new["scenario"] == "MATURE_TREND_PULLBACK"
    assert new["derived_structure"]["campaign_maturity"] == "MATURE_CONFIRMED"
    assert new["derived_structure"]["local_entry_stage"] == "EARLY"
    assert new["derived_structure"]["generation_risk"] == "DEGRADED_LATE_GENERATION_OR_ATTACK"
    assert new["action_signature"]["setup_grade"] == "DEGRADED_LATE_GENERATION_OR_ATTACK"
    assert new["action_signature"]["required_risk_disclosures"] == [
        "REMAINING_SPACE",
        "CHASE_RISK",
        "COPY_FAILURE_RISK",
    ]
    assert new["derived_structure"]["hypothesis_states"]["MATURE_TREND_PULLBACK"]["H-MATURE_TREND_PULLBACK"]["legacy_generation_stage"] == "LATE"
    assert v4.validate_course_invariants(packet, semantic, new) == []


def test_later_generation_with_one_weakness_is_middle_and_can_trade() -> None:
    packet, semantic = _mature_case("LATER_GENERATION")
    _set_atom(
        semantic,
        "relation_candidates",
        "RELATION_CANDIDATE:R-MATURE",
        "EXH_ATTACK_SHORTENING",
        "PASS",
    )
    decision = v4.reduce_atomic_v4_s1(packet, semantic)
    assert decision["permission"] == "TRADE"
    assert decision["derived_structure"]["local_entry_stage"] == "MIDDLE"


def test_two_weaknesses_make_local_entry_late_and_block_trade() -> None:
    packet, semantic = _mature_case()
    for atom in ("EXH_ATTACK_SHORTENING", "EXH_SLOPE_DECAY"):
        _set_atom(
            semantic,
            "relation_candidates",
            "RELATION_CANDIDATE:R-MATURE",
            atom,
            "PASS",
        )
    decision = v4.reduce_atomic_v4_s1(packet, semantic)
    hypothesis_id = "H-MATURE_TREND_PULLBACK"
    assert decision["permission"] == "WAIT"
    assert decision["derived_structure"]["local_entry_stage"] == "LATE"
    assert decision["common_hard_guards"]["MATURE_TREND_PULLBACK"][hypothesis_id]["LOCAL_ENTRY_NOT_LATE_OR_EXHAUSTED"] == "FAIL"


def test_space_exhaustion_has_priority_and_blocks_trade() -> None:
    packet, semantic = _mature_case()
    _set_atom(
        semantic,
        "relation_candidates",
        "RELATION_CANDIDATE:R-MATURE",
        "LOCATION_REMAINING_SPACE_ADEQUATE",
        "FAIL",
    )
    decision = v4.reduce_atomic_v4_s1(packet, semantic)
    assert decision["permission"] == "WAIT"
    assert decision["derived_structure"]["local_entry_stage"] == "EXHAUSTED"


def test_unknown_local_evidence_stays_unknown_and_cannot_trade() -> None:
    packet, semantic = _mature_case()
    _set_atom(
        semantic,
        "relation_candidates",
        "RELATION_CANDIDATE:R-MATURE",
        "LOCATION_NOT_EXTENDED_FROM_ORIGIN",
        "UNKNOWN",
    )
    decision = v4.reduce_atomic_v4_s1(packet, semantic)
    assert decision["permission"] == "WAIT"
    assert decision["derived_structure"]["local_entry_stage"] == "UNRESOLVED"


def test_non_target_macro_is_fail_closed_at_the_only_v4_s1_exit() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    old = v3.reduce_atomic_v3(packet, semantic)
    new = v4.reduce_atomic_v4_s1(packet, semantic)
    assert old["permission"] == "TRADE"
    assert old["scenario"] == "MACRO_COPY_RESONANCE"
    assert new["permission"] == "WAIT"
    assert new["route"] == "NO_TRADE"
    assert new["reason_codes"] == ["OUTSIDE_V4_S1_STAGE_SCOPE"]
    assert new["suppressed_candidate"] == {
        "scenario": old["scenario"],
        "route": old["route"],
        "hypothesis_id": "H-MACRO_COPY_RESONANCE",
    }
    assert v4.stage_permission_v4_s1(new) == "WAIT"
    assert v4.validate_course_invariants(packet, semantic, new) == []


def test_third_attack_is_explicitly_downgraded_without_changing_fixed_unit() -> None:
    packet, semantic = _mature_case("COPY_LEG_3")
    mature = _hypothesis(packet, "MATURE_TREND_PULLBACK")
    mature["same_direction_attack_number"] = 3
    mature["completed_prior_copy_count"] = 1
    packet["question_manifest"] = v2.expected_question_manifest(packet)
    decision = v4.reduce_atomic_v4_s1(packet, semantic)
    assert decision["permission"] == "TRADE"
    disclosure = decision["derived_structure"]["risk_disclosure"]
    assert disclosure["third_or_later_attack"] is True
    assert disclosure["setup_grade"] == "DEGRADED_LATE_GENERATION_OR_ATTACK"
    assert decision["action_signature"]["position_role"] == "MOTHER"


def test_failed_continuation_alone_is_late_and_blocks_trade() -> None:
    packet, semantic = _mature_case()
    _set_atom(
        semantic,
        "relation_candidates",
        "RELATION_CANDIDATE:R-MATURE",
        "EXH_FAILED_CONTINUATION",
        "PASS",
    )
    decision = v4.reduce_atomic_v4_s1(packet, semantic)
    assert decision["permission"] == "WAIT"
    assert decision["derived_structure"]["local_entry_stage"] == "LATE"


def test_long_hh_hl_history_can_confirm_maturity_without_counted_copy() -> None:
    packet, semantic = _mature_case("COPY_LEG_3")
    mature = _hypothesis(packet, "MATURE_TREND_PULLBACK")
    mature["same_direction_attack_number"] = 1
    mature["completed_prior_copy_count"] = 0
    packet["question_manifest"] = v2.expected_question_manifest(packet)
    decision = v4.reduce_atomic_v4_s1(packet, semantic)
    assert decision["permission"] == "TRADE"
    assert decision["scenario"] == "MATURE_TREND_PULLBACK"
    assert decision["derived_structure"]["campaign_maturity"] == "MATURE_CONFIRMED"


def test_invariants_reject_a_non_s1_trade_even_if_v3_would_allow_it() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    non_s1_trade = v3.reduce_atomic_v3(packet, semantic)
    assert non_s1_trade["permission"] == "TRADE"
    assert any(
        "material signature differs" in error
        for error in v4.validate_course_invariants(
            packet, semantic, non_s1_trade
        )
    )


def test_v3_reversal_probe_validator_exception_is_not_rejected_again() -> None:
    packet = _packet()
    _hypothesis(packet, "BEAR_REVERSAL_LEFT_RIGHT")["taiji_generation"] = "REVERSAL_PROBE"
    packet["question_manifest"] = v2.expected_question_manifest(packet)
    semantic = _semantic(packet)
    assert v4.validate_atomic(packet, semantic) == []
    decision = v4.reduce_atomic_v4_s1(packet, semantic)
    assert "INVALID_PACKET" not in (decision.get("reason_codes") or [])


def test_protected_v1_v2_v3_artifacts_match_pins() -> None:
    v4.assert_frozen_ancestors()
    rules = json.loads(v4.RULES_PATH.read_text(encoding="utf-8"))
    for relative, expected in rules["lineage"]["protected_files"].items():
        assert v4._file_sha256(v4.ROOT / relative) == expected


def test_validator_rejects_tampered_material_decision() -> None:
    packet, semantic = _mature_case()
    decision = v4.reduce_atomic_v4_s1(packet, semantic)
    tampered = copy.deepcopy(decision)
    tampered["route"] = "NO_TRADE"
    assert any(
        "material signature differs" in error
        for error in v4.validate_course_invariants(packet, semantic, tampered)
    )
