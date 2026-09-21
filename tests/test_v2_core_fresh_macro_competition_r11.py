import pytest

from scripts.v2_core_fresh_macro_competition_r11 import compete_fresh_macro_asof


REFS = {"PARENT:2023-04-17", "CORRECTION:2023-04-21", "ANCHOR:2023-05-04", "STOP:2023-04-21"}


def _hypothesis(scenario, verdict, parent):
    return {
        "as_of": "2023-05-04", "scenario": scenario, "verdict": verdict,
        "replicable_parent": parent, "direction": "UP",
        "episode_stop_id": "STOP:2023-04-21", "position_role": "MOTHER_OR_REENTRY",
        "support_refs": ["PARENT:2023-04-17", "STOP:2023-04-21"],
        "counter_refs": ["CORRECTION:2023-04-21"],
        "reason": "The completed parent and correction have competing causal readings.",
    }


def _compete(fresh, macro):
    return compete_fresh_macro_asof(
        as_of="2023-05-04", frozen_position_role="MOTHER_OR_REENTRY",
        fresh=fresh, macro=macro, available_evidence_refs=REFS,
    )


def test_unique_macro_route_is_only_a_candidate():
    fresh = _hypothesis("FRESH_Q1_EXPANSION", "FAIL", "PASS")
    macro = _hypothesis("MACRO_COPY_RESONANCE", "PASS", "PASS")
    result = _compete(fresh, macro)
    assert result["status"] == "SINGLE_ROUTE_CANDIDATE_NOT_TRADE"
    assert result["provisional_scenario"] == "MACRO_COPY_RESONANCE"
    assert not result["trade_permission_granted"]


def test_two_pass_or_one_pass_with_unknown_cannot_silently_route():
    fresh = _hypothesis("FRESH_Q1_EXPANSION", "PASS", "FAIL")
    macro = _hypothesis("MACRO_COPY_RESONANCE", "PASS", "PASS")
    assert _compete(fresh, macro)["status"] == "ADJUDICATION_REQUIRED_MUTUALLY_EXCLUSIVE_PASS"
    macro["verdict"] = "UNKNOWN"
    assert _compete(fresh, macro)["status"] == "ADJUDICATION_REQUIRED_ALTERNATIVE_UNKNOWN"


def test_neither_pass_is_not_else_classified_fresh():
    fresh = _hypothesis("FRESH_Q1_EXPANSION", "FAIL", "UNKNOWN")
    macro = _hypothesis("MACRO_COPY_RESONANCE", "FAIL", "UNKNOWN")
    result = _compete(fresh, macro)
    assert result["status"] == "NO_FRESH_OR_MACRO_ROUTE"
    assert result["provisional_scenario"] is None


@pytest.mark.parametrize("field,value", [
    ("position_role", "ADD_1"),
    ("episode_stop_id", None),
    ("support_refs", ["FUTURE:2023-08-01"]),
    ("replicable_parent", "PASS"),
])
def test_rejects_role_stop_evidence_or_parent_boundary_violation(field, value):
    fresh = _hypothesis("FRESH_Q1_EXPANSION", "PASS", "FAIL")
    macro = _hypothesis("MACRO_COPY_RESONANCE", "FAIL", "UNKNOWN")
    fresh[field] = value
    with pytest.raises(ValueError):
        _compete(fresh, macro)
