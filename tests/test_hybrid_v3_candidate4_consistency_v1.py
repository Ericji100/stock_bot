import pytest

from scripts.hybrid_v3_candidate4_consistency_v1 import (
    _decision_exact,
    _rate,
    evaluate_acceptance,
)


THRESHOLDS = {
    "schema_and_causal_fields": 1.0,
    "material_permission": 0.95,
    "scenario_and_left_right_phase": 0.9,
}


def test_acceptance_uses_all_three_frozen_thresholds() -> None:
    result = evaluate_acceptance(
        schema_causal_rate=1.0,
        permission_rate=0.95,
        scenario_left_right_rate=0.9,
        thresholds=THRESHOLDS,
    )
    assert result["passed"] is True
    assert all(result["checks"].values())


@pytest.mark.parametrize(
    ("schema", "permission", "scenario"),
    [(0.999, 1.0, 1.0), (1.0, 0.949, 1.0), (1.0, 1.0, 0.899)],
)
def test_acceptance_fails_each_frozen_boundary(
    schema: float, permission: float, scenario: float
) -> None:
    result = evaluate_acceptance(
        schema_causal_rate=schema,
        permission_rate=permission,
        scenario_left_right_rate=scenario,
        thresholds=THRESHOLDS,
    )
    assert result["passed"] is False


def test_decision_exact_requires_unanimity_for_every_requested_field() -> None:
    signatures = [
        {"permission": "WAIT", "scenario": "A", "left_right_phase": "LR"},
        {"permission": "WAIT", "scenario": "A", "left_right_phase": "LR"},
        {"permission": "WAIT", "scenario": "A", "left_right_phase": "RL"},
    ]
    assert _decision_exact(signatures, ("permission",)) is True
    assert _decision_exact(signatures, ("scenario", "left_right_phase")) is False


def test_rate_uses_exact_case_denominator_and_threshold() -> None:
    value = _rate(114, 120, 0.95)
    assert value == {
        "exact": 114,
        "total": 120,
        "rate": 0.95,
        "threshold": 0.95,
        "passed": True,
    }
