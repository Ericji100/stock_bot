from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts import hybrid_v3_atomic_packets_v6 as v6


def _pivot(scale: str, side: str, day: str, price: float) -> dict:
    return {
        "scale": scale,
        "side": side,
        "source_date": day,
        "confirmation_date": day,
        "price": price,
        "ref": f"PIVOT:{scale}:{side}:{day}",
    }


def _anchor(candidate_id: str = "ANCHOR:LARGE:UP") -> dict:
    return {
        "candidate_id": candidate_id,
        "scale": "LARGE",
        "campaign_id": "CAMPAIGN:LARGE:BULLISH:2",
    }


def _relation(parent_scale: str = "LARGE") -> dict:
    return {
        "candidate_id": "RELATION:MATURE",
        "relation_type": "CURRENT_UP_COPY_TRIGGER",
        "eligible_scenarios": ["MATURE_TREND_PULLBACK"],
        "parent_anchor_ref": "ANCHOR:LARGE:UP",
        "correction_anchor_ref": "ANCHOR:LARGE:DOWN",
        "current_leg_ref": "CURRENT_CONTEXT",
        "current_attack_ref": "ATTACK:SMALL:UP:TODAY",
        "parent_scale": parent_scale,
        "correction_scale": parent_scale,
        "current_scale": "SMALL",
        "current_available_on": "2023-08-01",
        "current_direction": "UP",
        "parent_direction": "UP",
        "correction_direction": "DOWN",
        "campaign_id": "CAMPAIGN:LARGE:BULLISH:2",
        "taiji_generation": "COPY_LEG_5",
        "same_direction_attack_number": 3,
        "completed_prior_copy_count": 1,
    }


def _stops() -> list[dict]:
    return [
        {
            "candidate_id": "STOP:EPISODE",
            "defense_ref": "DEFENSE:SMALL:BULLISH:TODAY",
            "campaign_id": "CAMPAIGN:SMALL:BULLISH:4",
            "scale": "SMALL",
            "side": "BULLISH",
            "control_pivot_ref": "PIVOT:SMALL:HIGH:CONTROL",
            "established_on": "2023-08-01",
        },
        {
            "candidate_id": "STOP:CAMPAIGN",
            "defense_ref": "DEFENSE:LARGE:BULLISH:PARENT",
            "campaign_id": "CAMPAIGN:LARGE:BULLISH:2",
            "scale": "LARGE",
            "side": "BULLISH",
            "control_pivot_ref": "PIVOT:LARGE:HIGH:CONTROL",
            "established_on": "2023-07-01",
        },
        {
            "candidate_id": "STOP:STALE",
            "defense_ref": "DEFENSE:SMALL:BULLISH:OLD",
            "campaign_id": "CAMPAIGN:SMALL:BULLISH:3",
            "scale": "SMALL",
            "side": "BULLISH",
            "control_pivot_ref": "PIVOT:SMALL:HIGH:OLD",
            "established_on": "2023-06-01",
        },
    ]


def _objective(defense_result: str = "ESTABLISHED") -> dict:
    attack = {
        "attack_id": "ATTACK:SMALL:UP:TODAY",
        "scale": "SMALL",
        "direction": "UP",
        "confirmed_on": "2023-08-01",
        "control_pivot_ref": "PIVOT:SMALL:HIGH:CONTROL",
        "defense_result": defense_result,
    }
    if defense_result == "ESTABLISHED":
        attack["defense_id"] = "DEFENSE:SMALL:BULLISH:TODAY"
    return {"as_of": "2023-08-01", "attacks": [attack]}


def _candidates(relation: dict | None = None, stops: list[dict] | None = None) -> dict:
    return {
        "anchor_candidates": [
            _anchor(),
            {**_anchor("ANCHOR:LARGE:DOWN"), "campaign_id": "CAMPAIGN:LARGE:BEARISH:2"},
        ],
        "relation_candidates": [relation or _relation()],
        "stop_candidates": stops or _stops(),
    }


def test_v5_dependency_is_byte_pinned() -> None:
    v6.assert_base_builder_frozen()
    assert v6.BUILDER_VERSION == "hybrid-v3-atomic-packets-v6"
    assert Path(v6._BASE_FILE).name == "hybrid_v3_atomic_packets_v5.py"


def test_anchor_campaign_assignment_never_crosses_scale() -> None:
    pivots = [
        _pivot("SMALL", "LOW", "2023-01-02", 10),
        _pivot("SMALL", "HIGH", "2023-01-04", 12),
        _pivot("LARGE", "LOW", "2023-01-03", 9),
        _pivot("LARGE", "HIGH", "2023-01-06", 14),
    ]
    objective = {
        "defenses": [
            {
                "scale": "SMALL",
                "side": "BULLISH",
                "source_pivot_ref": pivots[0]["ref"],
                "campaign_id": "CAMPAIGN:SMALL:BULLISH:7",
            },
            {
                "scale": "LARGE",
                "side": "BULLISH",
                "source_pivot_ref": pivots[2]["ref"],
                "campaign_id": "CAMPAIGN:LARGE:BULLISH:3",
            },
        ]
    }
    rows = v6._anchor_candidates(objective, pivots, [])
    up_rows = [row for row in rows if row["direction_hint"] == "UP"]
    assert {row["scale"]: row["campaign_id"] for row in up_rows} == {
        "SMALL": "CAMPAIGN:SMALL:BULLISH:7",
        "LARGE": "CAMPAIGN:LARGE:BULLISH:3",
    }


def test_unassigned_macd_anchor_never_borrows_a_scale_campaign() -> None:
    cycle = {
        "status": "CONFIRMED",
        "end": "2023-01-05",
        "start": "2023-01-02",
        "sign": "POSITIVE",
        "low_date": "2023-01-02",
        "high_date": "2023-01-05",
        "low": 10,
        "high": 13,
    }
    rows = v6._anchor_candidates({"defenses": []}, [], [cycle])
    assert rows[0]["scale"] == "UNASSIGNED"
    assert rows[0]["campaign_id"] == "UNRESOLVED_CAMPAIGN:UNASSIGNED:UP"


def test_mature_hypothesis_uses_exact_current_attack_and_parent_campaign_stops() -> None:
    result = v6._scenario_hypotheses(
        _candidates(), position_role="MOTHER", objective=_objective()
    )
    hypothesis = result["MATURE_TREND_PULLBACK"][0]
    assert hypothesis["episode_stop_ref"] == "STOP:EPISODE"
    assert hypothesis["campaign_stop_ref"] == "STOP:CAMPAIGN"


def test_missing_current_attack_defense_is_null_without_stale_fallback() -> None:
    result = v6._scenario_hypotheses(
        _candidates(),
        position_role="MOTHER",
        objective=_objective("NO_CAUSAL_ORIGIN"),
    )
    hypothesis = result["MATURE_TREND_PULLBACK"][0]
    assert hypothesis["episode_stop_ref"] is None
    assert hypothesis["campaign_stop_ref"] == "STOP:CAMPAIGN"
    assert hypothesis["episode_stop_ref"] != "STOP:STALE"


def test_missing_exact_campaign_stop_is_null_and_hypothesis_is_preserved() -> None:
    stops = [row for row in _stops() if row["candidate_id"] != "STOP:CAMPAIGN"]
    result = v6._scenario_hypotheses(
        _candidates(stops=stops), position_role="MOTHER", objective=_objective()
    )
    hypotheses = result["MATURE_TREND_PULLBACK"]
    assert len(hypotheses) == 1
    assert hypotheses[0]["episode_stop_ref"] == "STOP:EPISODE"
    assert hypotheses[0]["campaign_stop_ref"] is None


def test_mature_route_requires_large_parent_correction_and_small_trigger() -> None:
    result = v6._scenario_hypotheses(
        _candidates(relation=_relation(parent_scale="SMALL")),
        position_role="MOTHER",
        objective=_objective(),
    )
    assert result["MATURE_TREND_PULLBACK"] == []


def test_fresh_campaign_stop_must_match_current_anchor_campaign_and_scale() -> None:
    relation = {
        "candidate_id": "RELATION:FRESH",
        "relation_type": "FRESH_UP_ANCHOR_CURRENT_TRIGGER",
        "eligible_scenarios": ["FRESH_Q1_EXPANSION"],
        "parent_anchor_ref": None,
        "current_leg_ref": "ANCHOR:FRESH",
        "current_attack_ref": "ATTACK:SMALL:UP:TODAY",
        "current_scale": "SMALL",
        "current_available_on": "2023-08-01",
        "current_direction": "UP",
        "completed_prior_copy_count": 0,
        "taiji_generation": "ANCHOR_LEG_1",
        "same_direction_attack_number": 1,
    }
    candidates = {
        "anchor_candidates": [
            {
                "candidate_id": "ANCHOR:FRESH",
                "scale": "SMALL",
                "campaign_id": "CAMPAIGN:SMALL:BULLISH:4",
            }
        ],
        "relation_candidates": [relation],
        "stop_candidates": _stops(),
    }
    result = v6._scenario_hypotheses(
        candidates, position_role="MOTHER", objective=_objective()
    )
    assert result["FRESH_Q1_EXPANSION"][0]["campaign_stop_ref"] == "STOP:EPISODE"

    candidates["anchor_candidates"][0]["campaign_id"] = "CAMPAIGN:SMALL:BULLISH:99"
    mismatch = v6._scenario_hypotheses(
        candidates, position_role="MOTHER", objective=_objective()
    )
    assert mismatch["FRESH_Q1_EXPANSION"][0]["episode_stop_ref"] == "STOP:EPISODE"
    assert mismatch["FRESH_Q1_EXPANSION"][0]["campaign_stop_ref"] is None


def test_route_sufficiency_and_risk_fail_closed_when_exact_stop_is_missing() -> None:
    facts = {
        "scenario_hypotheses": {
            scenario: [] for scenario in v6._v4.SCENARIOS
        },
        "data_sufficiency_by_route": {
            scenario: {
                "status": False,
                "reason_code": "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
            }
            for scenario in v6._v4.SCENARIOS
        },
        "risk_executable": True,
        "trigger_completed": True,
        "signal_event_ref": "ATTACK:SMALL:UP:TODAY",
        "wait_boundary_reasons": [],
        "material_objective_hypothesis_conflict": False,
        "material_objective_signature_count": 0,
    }
    incomplete = {
        "hypothesis_id": "SH-INCOMPLETE",
        "anchor_ref": "ANCHOR:LARGE:UP",
        "relation_ref": "RELATION:MATURE",
        "episode_stop_ref": None,
        "campaign_stop_ref": "STOP:CAMPAIGN",
        "position_role": "MOTHER",
    }
    facts["scenario_hypotheses"]["MATURE_TREND_PULLBACK"] = [incomplete]
    facts["data_sufficiency_by_route"]["MATURE_TREND_PULLBACK"] = {
        "status": True,
        "reason_code": "SUFFICIENT_ROUTE_HISTORY_AND_OBJECTIVE_EVIDENCE",
    }
    with patch.object(v6, "_ORIGINAL_OBJECTIVE_FACTS", return_value=deepcopy(facts)):
        result = v6._objective_facts()
    assert result["data_sufficiency_by_route"]["MATURE_TREND_PULLBACK"] == {
        "status": False,
        "reason_code": "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
    }
    assert result["stop_causal_fields_valid"] is False
    assert result["risk_executable"] is False
    assert "UP_ATTACK_WITHOUT_CAUSAL_STOP" in result["wait_boundary_reasons"]
    assert (
        "UP_ATTACK_WITH_INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE"
        in result["wait_boundary_reasons"]
    )


def test_exact_episode_can_remain_risk_valid_when_parent_campaign_is_missing() -> None:
    facts = {
        "scenario_hypotheses": {scenario: [] for scenario in v6._v4.SCENARIOS},
        "data_sufficiency_by_route": {
            scenario: {
                "status": False,
                "reason_code": "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
            }
            for scenario in v6._v4.SCENARIOS
        },
        "risk_executable": True,
        "trigger_completed": True,
        "signal_event_ref": "ATTACK:SMALL:UP:TODAY",
        "wait_boundary_reasons": [],
        "material_objective_hypothesis_conflict": False,
        "material_objective_signature_count": 0,
    }
    facts["scenario_hypotheses"]["MATURE_TREND_PULLBACK"] = [
        {
            "anchor_ref": "ANCHOR:LARGE:UP",
            "relation_ref": "RELATION:MATURE",
            "episode_stop_ref": "STOP:EPISODE",
            "campaign_stop_ref": None,
            "position_role": "MOTHER",
        }
    ]
    facts["data_sufficiency_by_route"]["MATURE_TREND_PULLBACK"] = {
        "status": True,
        "reason_code": "SUFFICIENT_ROUTE_HISTORY_AND_OBJECTIVE_EVIDENCE",
    }
    with patch.object(v6, "_ORIGINAL_OBJECTIVE_FACTS", return_value=deepcopy(facts)):
        result = v6._objective_facts()
    assert result["data_sufficiency_by_route"]["MATURE_TREND_PULLBACK"]["status"] is False
    assert result["stop_causal_fields_valid"] is True
    assert result["risk_executable"] is True
    assert "UP_ATTACK_WITHOUT_CAUSAL_STOP" not in result["wait_boundary_reasons"]


def test_causal_link_audit_rejects_stale_episode_stop() -> None:
    packet = {
        "evidence": [
            {
                "ref": "RELATION:MATURE",
                "kind": "RELATION_CANDIDATE",
                "values": {
                    "current_attack_ref": "ATTACK:SMALL:UP:TODAY",
                    "parent_anchor_ref": "ANCHOR:LARGE:UP",
                    "campaign_id": "CAMPAIGN:LARGE:BULLISH:2",
                    "parent_scale": "LARGE",
                },
            },
            {
                "ref": "ATTACK:SMALL:UP:TODAY",
                "kind": "CAUSAL_CONTROL_ATTACK",
                "values": {
                    "defense_result": "ESTABLISHED",
                    "defense_id": "DEFENSE:SMALL:BULLISH:TODAY",
                    "scale": "SMALL",
                    "control_pivot_ref": "PIVOT:SMALL:HIGH:CONTROL",
                    "confirmed_on": "2023-08-01",
                },
            },
            {
                "ref": "STOP:STALE",
                "kind": "STOP_CANDIDATE",
                "values": next(
                    row for row in _stops() if row["candidate_id"] == "STOP:STALE"
                ),
            },
            {
                "ref": "ANCHOR:LARGE:UP",
                "kind": "ANCHOR_CANDIDATE",
                "values": _anchor(),
            },
            {
                "ref": "STOP:CAMPAIGN",
                "kind": "STOP_CANDIDATE",
                "values": next(
                    row for row in _stops() if row["candidate_id"] == "STOP:CAMPAIGN"
                ),
            },
        ],
        "objective_facts": {
            "scenario_hypotheses": {
                "MATURE_TREND_PULLBACK": [
                    {
                        "relation_ref": "RELATION:MATURE",
                        "episode_stop_ref": "STOP:STALE",
                        "campaign_stop_ref": "STOP:CAMPAIGN",
                    }
                ]
            }
        },
    }
    assert v6.causal_link_audit(packet) == [
        "MATURE_TREND_PULLBACK: episode stop is not the current attack defense"
    ]
