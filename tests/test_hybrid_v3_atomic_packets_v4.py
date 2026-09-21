import hashlib
from pathlib import Path

from scripts import hybrid_v3_atomic_packets_v3 as candidate2
from scripts import hybrid_v3_atomic_packets_v4 as candidate3


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE2_LOCKED_SHA256 = "1a5de03b7740c3956de4560d3f75d06f1365cff59c112eb5b3bb44dad1d1f9e2"


def _anchor(name: str, direction: str, available: str) -> dict:
    return {
        "candidate_id": f"ANCHOR_CANDIDATE:{name}",
        "objective_hypothesis_id": f"H-{name}",
        "hypothesis_type": "PIVOT_LEG",
        "scale": "LARGE",
        "direction_hint": direction,
        "start_ref": "BAR:2023-01-01",
        "end_ref": f"BAR:{available}",
        "available_on": available,
        "amplitude": 2.0,
        "macd_support_only": False,
        "campaign_id": "C-1",
        "taiji_generation": "ANCHOR_LEG_1",
        "same_direction_attack_number": 1,
        "completed_prior_copy_count": 0,
        "pivot_definition": "LEGACY_RADIUS_3_10_NOT_COURSE_L1_L2",
        "asserted_valid_anchor": False,
    }


def _objective(as_of: str) -> dict:
    return {
        "as_of": as_of,
        "attacks": [
            {
                "attack_id": "ATTACK:SMALL:UP:TODAY",
                "scale": "SMALL",
                "direction": "UP",
                "confirmed_on": as_of,
            }
        ],
        "defenses": [],
        "dow": {"large": {"dow_state": "TRANSITION"}},
        "controls": {"large": {"state": "UNRESOLVED"}},
        "left_right": {"phase": "NONE", "boundary_defense_ref": None},
    }


def test_candidate2_source_is_still_exactly_preserved() -> None:
    value = (ROOT / "scripts/hybrid_v3_atomic_packets_v3.py").read_bytes()
    assert hashlib.sha256(value).hexdigest() == CANDIDATE2_LOCKED_SHA256
    assert candidate2.BUILDER_VERSION == "hybrid-v3-atomic-packets-v3"


def test_candidate3_versions_are_distinct_and_final() -> None:
    assert candidate3.PACKET_VERSION == "hybrid-v3-atomic-question-packet-v4"
    assert candidate3.BUILDER_VERSION == "hybrid-v3-atomic-packets-v4"
    assert candidate3.BUILDER_STATUS == "FINAL"
    assert candidate3.FORMAL_REVIEW_POINT_POLICY.startswith("CANDIDATE3_")


def test_fresh_is_enumerated_as_a_semantic_challenge_despite_remote_copy_pair() -> None:
    as_of = "2023-01-10"
    anchors = [
        _anchor("UP_PARENT", "UP", "2023-01-05"),
        _anchor("DOWN_CORRECTION", "DOWN", "2023-01-08"),
    ]

    old_relations = candidate2._relation_candidates(anchors, _objective(as_of), as_of)
    assert not any(
        "FRESH_Q1_EXPANSION" in row["eligible_scenarios"] for row in old_relations
    )

    relations = candidate3._relation_candidates(anchors, _objective(as_of), as_of)
    assert any("MACRO_COPY_RESONANCE" in row["eligible_scenarios"] for row in relations)
    fresh = next(
        row for row in relations if "FRESH_Q1_EXPANSION" in row["eligible_scenarios"]
    )
    assert fresh["parent_anchor_ref"] is None
    assert fresh["relation_state"] == (
        "CURRENT_FRESH_UP_CANDIDATE_REQUIRES_SEMANTIC_EXCLUSIVITY"
    )
    assert fresh["enumeration_policy"] == (
        "CANDIDATE3_MULTI_ROUTE_CURRENT_TRIGGER_CHALLENGES"
    )
