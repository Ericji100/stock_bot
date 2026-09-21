from scripts import hybrid_v3_atomic_policy_v3 as v3


def _packet(generation: str, scenario: str = "BEAR_REVERSAL_LEFT_RIGHT") -> dict:
    return {
        "objective_facts": {
            "scenario_hypotheses": {
                scenario: [
                    {"hypothesis_id": "SH-abc123", "taiji_generation": generation}
                ]
            }
        }
    }


def test_reversal_probe_false_positive_is_removed(monkeypatch) -> None:
    monkeypatch.setattr(
        v3._v2,
        "validate_atomic",
        lambda packet, semantic: [
            "packet: BEAR_REVERSAL_LEFT_RIGHT SH-abc123 taiji_generation is invalid",
            "another real error",
        ],
    )
    assert v3.validate_atomic(_packet("REVERSAL_PROBE"), {}) == ["another real error"]


def test_other_invalid_generation_is_not_removed(monkeypatch) -> None:
    error = "packet: BEAR_REVERSAL_LEFT_RIGHT SH-abc123 taiji_generation is invalid"
    monkeypatch.setattr(v3._v2, "validate_atomic", lambda packet, semantic: [error])
    assert v3.validate_atomic(_packet("BOGUS"), {}) == [error]


def test_same_value_in_other_scenario_is_not_whitelisted(monkeypatch) -> None:
    error = "packet: MACRO_COPY_RESONANCE SH-abc123 taiji_generation is invalid"
    monkeypatch.setattr(v3._v2, "validate_atomic", lambda packet, semantic: [error])
    assert v3.validate_atomic(_packet("REVERSAL_PROBE", "MACRO_COPY_RESONANCE"), {}) == [error]


def test_reducer_is_exact_frozen_v2_callable() -> None:
    assert v3.reduce_atomic_v3 is v3._v2.reduce_atomic_v3
    assert v3.validate_course_invariants is v3._v2.validate_course_invariants
