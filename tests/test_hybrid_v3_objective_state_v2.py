from __future__ import annotations

import copy

import pytest

from scripts.hybrid_v3_objective_state_v2 import (
    ENGINE_STATUS,
    ENGINE_VERSION,
    EXPERIMENTAL_COURSE_PIVOT_DEFINITION,
    PIVOT_DEFINITION,
    course_l1_l2_experimental_interface,
    derive_attacks_and_defenses,
    derive_dow_states,
    derive_from_event_packet,
    derive_objective_state,
    material_action_signature,
)


def test_component_metadata_is_final_after_formal_validation():
    assert ENGINE_VERSION == "hybrid-v3-objective-state-v2"
    assert ENGINE_STATUS == "FINAL"


def bar(day: str, close: float) -> dict:
    return {"date": day, "open": close, "high": close, "low": close, "close": close}


def pivot(scale: str, side: str, source: str, confirmed: str, price: float) -> dict:
    return {
        "scale": scale,
        "side": side,
        "source_date": source,
        "confirmation_date": confirmed,
        "price": price,
    }


def basic_bull_pivots() -> list[dict]:
    return [
        pivot("SMALL", "HIGH", "2023-01-02", "2023-01-03", 10),
        pivot("SMALL", "LOW", "2023-01-04", "2023-01-05", 8),
        pivot("SMALL", "HIGH", "2023-01-07", "2023-01-08", 12),
        pivot("SMALL", "LOW", "2023-01-09", "2023-01-10", 9),
    ]


def reversal_fixture(as_of: str) -> tuple[list[dict], list[dict]]:
    bars = [
        bar("2023-01-03", 11),
        bar("2023-01-05", 11),
        bar("2023-01-06", 9),       # large DOWN attack; bear defense = 12
        bar("2023-01-08", 9.5),
        bar("2023-01-10", 9.5),
        bar("2023-01-11", 10.5),    # small UP attack => LR
        bar("2023-01-12", 12.5),    # breaks large bear defense => direct right
        bar("2023-01-13", 12.0),
        bar("2023-01-14", 11.5),    # first post-boundary low is confirmed => RL
        bar("2023-01-15", 12.0),    # new small UP attack => RR
    ]
    pivots = [
        pivot("LARGE", "LOW", "2023-01-02", "2023-01-03", 10),
        pivot("LARGE", "HIGH", "2023-01-04", "2023-01-05", 12),
        pivot("SMALL", "HIGH", "2023-01-07", "2023-01-08", 10),
        pivot("SMALL", "LOW", "2023-01-09", "2023-01-10", 9),
        pivot("SMALL", "HIGH", "2023-01-12", "2023-01-13", 11.8),
        pivot("SMALL", "LOW", "2023-01-13", "2023-01-14", 11),
    ]
    return [row for row in bars if row["date"] <= as_of], [row for row in pivots if row["confirmation_date"] <= as_of]


def test_official_output_always_discloses_legacy_pivot_definition() -> None:
    state = derive_objective_state(
        as_of="2023-01-10",
        bars=[bar("2023-01-03", 9), bar("2023-01-10", 9)],
        confirmed_pivots=basic_bull_pivots(),
    )
    assert state["pivot_definition"] == PIVOT_DEFINITION
    assert state["course_l1_l2_used"] is False
    assert all(row["pivot_definition"] == PIVOT_DEFINITION for row in state["candidate_hypotheses"])


def test_future_bar_pivot_and_cycle_are_rejected() -> None:
    with pytest.raises(ValueError, match="future bar"):
        derive_objective_state(
            as_of="2023-01-05", bars=[bar("2023-01-06", 10)], confirmed_pivots=[]
        )
    with pytest.raises(ValueError, match="future-confirmed pivot"):
        derive_objective_state(
            as_of="2023-01-05",
            bars=[bar("2023-01-05", 10)],
            confirmed_pivots=[pivot("SMALL", "LOW", "2023-01-04", "2023-01-06", 9)],
        )
    with pytest.raises(ValueError, match="future MACD"):
        derive_objective_state(
            as_of="2023-01-05",
            bars=[bar("2023-01-05", 10)],
            confirmed_pivots=[],
            completed_macd_cycles=[{"status": "CONFIRMED", "end": "2023-01-06"}],
        )


def test_nonlegacy_definition_cannot_enter_official_result() -> None:
    with pytest.raises(ValueError, match="official V2 baseline"):
        derive_objective_state(
            as_of="2023-01-05",
            bars=[bar("2023-01-05", 10)],
            confirmed_pivots=[],
            pivot_definition=EXPERIMENTAL_COURSE_PIVOT_DEFINITION,
        )


def test_hh_hl_and_ll_lh_map_to_dow_causally() -> None:
    pivots = basic_bull_pivots() + [
        pivot("LARGE", "HIGH", "2023-01-01", "2023-01-02", 20),
        pivot("LARGE", "LOW", "2023-01-03", "2023-01-04", 15),
        pivot("LARGE", "HIGH", "2023-01-05", "2023-01-06", 18),
        pivot("LARGE", "LOW", "2023-01-07", "2023-01-08", 13),
    ]
    state = derive_objective_state(
        as_of="2023-01-10", bars=[bar("2023-01-02", 10), bar("2023-01-10", 10)], confirmed_pivots=pivots
    )
    assert (state["dow"]["small"]["high_relation"], state["dow"]["small"]["low_relation"]) == ("HH", "HL")
    assert state["dow"]["small"]["dow_state"] == "BULL"
    assert (state["dow"]["large"]["high_relation"], state["dow"]["large"]["low_relation"]) == ("LH", "LL")
    assert state["dow"]["large"]["dow_state"] == "BEAR"


def test_same_day_new_control_cannot_be_broken_or_create_defense() -> None:
    pivots = [
        pivot("SMALL", "HIGH", "2023-01-02", "2023-01-06", 10),
        pivot("SMALL", "LOW", "2023-01-04", "2023-01-05", 8),
    ]
    on_confirmation = derive_objective_state(
        as_of="2023-01-06",
        bars=[bar("2023-01-05", 9), bar("2023-01-06", 11)],
        confirmed_pivots=pivots,
    )
    assert on_confirmation["attacks"] == []

    later = derive_objective_state(
        as_of="2023-01-08",
        bars=[bar("2023-01-05", 9), bar("2023-01-06", 9), bar("2023-01-08", 11)],
        confirmed_pivots=pivots,
    )
    assert len(later["attacks"]) == 1
    assert later["attacks"][0]["confirmed_on"] == "2023-01-08"
    assert later["defenses"][0]["source_pivot_ref"].startswith("PIVOT:SMALL:LOW")


def test_unproductive_low_never_becomes_defense() -> None:
    state = derive_objective_state(
        as_of="2023-01-08",
        bars=[bar("2023-01-05", 9), bar("2023-01-08", 9.5)],
        confirmed_pivots=[
            pivot("SMALL", "HIGH", "2023-01-02", "2023-01-03", 10),
            pivot("SMALL", "LOW", "2023-01-04", "2023-01-05", 8),
        ],
    )
    assert state["defenses"] == []


def test_bull_defense_is_established_only_after_close_break() -> None:
    state = derive_objective_state(
        as_of="2023-01-06",
        bars=[bar("2023-01-03", 9), bar("2023-01-05", 9), bar("2023-01-06", 11)],
        confirmed_pivots=[
            pivot("SMALL", "HIGH", "2023-01-02", "2023-01-03", 10),
            pivot("SMALL", "LOW", "2023-01-04", "2023-01-05", 8),
        ],
    )
    assert state["attacks"][0]["direction"] == "UP"
    defense = state["defenses"][0]
    assert defense["side"] == "BULLISH"
    assert defense["price"] == 8
    assert defense["established_on"] == "2023-01-06"


def test_active_bull_defense_cannot_move_down() -> None:
    pivots = [
        pivot("SMALL", "HIGH", "2023-01-02", "2023-01-03", 10),
        pivot("SMALL", "LOW", "2023-01-04", "2023-01-05", 8),
        pivot("SMALL", "HIGH", "2023-01-07", "2023-01-08", 12),
        pivot("SMALL", "LOW", "2023-01-09", "2023-01-10", 7),
    ]
    state = derive_objective_state(
        as_of="2023-01-11",
        bars=[
            bar("2023-01-03", 9),
            bar("2023-01-05", 9),
            bar("2023-01-06", 11),
            bar("2023-01-10", 11),
            bar("2023-01-11", 13),
        ],
        confirmed_pivots=pivots,
    )
    assert len(state["defenses"]) == 1
    assert state["defenses"][0]["price"] == 8
    assert state["attacks"][-1]["defense_result"] == "REJECTED_NON_MONOTONIC"


def test_control_becomes_contested_when_its_defense_breaks_without_opposite_attack() -> None:
    state = derive_objective_state(
        as_of="2023-01-08",
        bars=[
            bar("2023-01-03", 9),
            bar("2023-01-05", 9),
            bar("2023-01-06", 11),
            bar("2023-01-08", 7.5),
        ],
        confirmed_pivots=[
            pivot("SMALL", "HIGH", "2023-01-02", "2023-01-03", 10),
            pivot("SMALL", "LOW", "2023-01-04", "2023-01-05", 8),
            # This later, lower control low prevents 7.5 from being a DOWN
            # attack, while the older bullish defense at 8 is still broken.
            pivot("SMALL", "LOW", "2023-01-07", "2023-01-07", 7),
        ],
    )
    assert [row["direction"] for row in state["attacks"]] == ["UP"]
    assert state["defenses"][0]["status"] == "BREACHED"
    assert state["controls"]["small"] == {
        "state": "CONTESTED",
        "attack_id": state["attacks"][0]["attack_id"],
        "confirmed_on": "2023-01-06",
        "reason": "LATEST_ATTACK_HAS_NO_ACTIVE_CAUSAL_DEFENSE",
    }


def test_left_right_lr_direct_rl_rr_sequence() -> None:
    expected = {
        "2023-01-11": "LR",
        "2023-01-12": "DIRECT_TO_RIGHT",
        "2023-01-14": "RL",
        "2023-01-15": "RR",
    }
    for as_of, phase in expected.items():
        bars, pivots = reversal_fixture(as_of)
        state = derive_objective_state(as_of=as_of, bars=bars, confirmed_pivots=pivots)
        assert state["left_right"]["phase"] == phase
        assert state["left_right"]["legacy_proxy"] is True


def test_scale_relationship_comes_only_from_objective_controls() -> None:
    bars, pivots = reversal_fixture("2023-01-11")
    state = derive_objective_state(as_of="2023-01-11", bars=bars, confirmed_pivots=pivots)
    assert state["controls"]["large"]["state"] == "DOWN_CONTROL"
    assert state["controls"]["small"]["state"] == "UP_CONTROL"
    assert state["scale_relationship"] == "SMALL_COUNTER_LARGE_DOWN"


def test_candidate_hypotheses_are_candidates_not_anchor_truth_and_are_deterministic() -> None:
    kwargs = {
        "as_of": "2023-01-10",
        "bars": [bar("2023-01-03", 9), bar("2023-01-10", 9)],
        "confirmed_pivots": basic_bull_pivots(),
        "completed_macd_cycles": [
            {
                "status": "CONFIRMED",
                "sign": "POSITIVE",
                "start": "2023-01-01",
                "end": "2023-01-10",
                "low_date": "2023-01-01",
                "low": 7,
                "high_date": "2023-01-08",
                "high": 12,
            }
        ],
    }
    first = derive_objective_state(**kwargs)
    shuffled = copy.deepcopy(kwargs)
    shuffled["confirmed_pivots"] = list(reversed(shuffled["confirmed_pivots"]))
    second = derive_objective_state(**shuffled)
    assert first["candidate_hypotheses"] == second["candidate_hypotheses"]
    assert all(row["asserted_valid_anchor"] is False for row in first["candidate_hypotheses"])
    assert any(row["hypothesis_type"] == "MACD_SKELETON" for row in first["candidate_hypotheses"])


def test_material_action_signature_changes_only_for_material_fields() -> None:
    first = material_action_signature(direction="LONG", stop_ref="P1", position_role="MOTHER")
    same = material_action_signature(direction="LONG", stop_ref="P1", position_role="MOTHER")
    changed = material_action_signature(direction="LONG", stop_ref="P2", position_role="MOTHER")
    assert first == same
    assert first["pivot_definition"] == PIVOT_DEFINITION
    assert first["signature_sha256"] != changed["signature_sha256"]


def test_standalone_objective_primitives_also_disclose_pivot_definition() -> None:
    normalized = [
        {
            **row,
            "ref": f"PIVOT:{row['scale']}:{row['side']}:{row['source_date']}:{row['confirmation_date']}",
            "pivot_definition": PIVOT_DEFINITION,
        }
        for row in basic_bull_pivots()
    ]
    dow = derive_dow_states(normalized)
    replay = derive_attacks_and_defenses(
        bars=[bar("2023-01-03", 9), bar("2023-01-05", 9), bar("2023-01-06", 11)],
        confirmed_pivots=normalized,
    )
    assert dow["pivot_definition"] == PIVOT_DEFINITION
    assert replay["pivot_definition"] == PIVOT_DEFINITION


def test_event_packet_adapter_remains_identity_blind_and_marks_left_truncation() -> None:
    packet = {
        "review_id": "D-000000000000000000000001",
        "anonymous_stock_id": "S-0000000000000001",
        "as_of": "2023-01-06",
        "selection_asof": {"selected_before_or_on_day": True},
        "evidence": [
            {"kind": "BAR", "date": "2023-01-05", "values": {"close": 9}},
            {"kind": "BAR", "date": "2023-01-06", "values": {"close": 11}},
            {
                "kind": "CONFIRMED_PIVOT",
                "date": "2023-01-03",
                "values": pivot("SMALL", "HIGH", "2023-01-02", "2023-01-03", 10),
            },
            {
                "kind": "CONFIRMED_PIVOT",
                "date": "2023-01-05",
                "values": pivot("SMALL", "LOW", "2023-01-04", "2023-01-05", 8),
            },
        ],
    }
    state = derive_from_event_packet(packet)
    assert state["review_id"] == packet["review_id"]
    assert "stock" not in state
    assert state["pivot_definition"] == PIVOT_DEFINITION
    assert state["input_quality"]["replay_may_be_left_truncated"] is True


def test_course_l1_l2_interface_is_explicitly_disabled() -> None:
    interface = course_l1_l2_experimental_interface()
    assert interface == {
        "pivot_definition": EXPERIMENTAL_COURSE_PIVOT_DEFINITION,
        "status": "DISABLED_NOT_IMPLEMENTED",
        "allowed_in_official_result": False,
        "mixed_with_legacy": False,
    }
