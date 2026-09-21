from scripts.hybrid_v3_triplicate_smoke_audit import decision_signature, exact_rate


def test_exact_rate_reports_disagreements() -> None:
    result = exact_rate(
        [
            {"global.A": "PASS", "global.B": "FAIL"},
            {"global.A": "PASS", "global.B": "UNKNOWN"},
            {"global.A": "PASS", "global.B": "FAIL"},
        ],
        ["global.A", "global.B"],
    )
    assert result["exact"] == 1
    assert result["total"] == 2
    assert result["rate"] == 0.5
    assert result["disagreements"] == [
        {"path": "global.B", "results": ["FAIL", "UNKNOWN", "FAIL"]}
    ]


def test_decision_signature_uses_deterministic_derived_structure() -> None:
    result = decision_signature(
        {
            "permission": "WAIT",
            "route": "NO_TRADE",
            "scenario": "MACRO_COPY_RESONANCE",
            "reason_codes": ["NO_ELIGIBLE_ROUTE"],
            "derived_structure": {"stage": "UNRESOLVED", "left_right_phase": "NONE"},
        }
    )
    assert result == {
        "permission": "WAIT",
        "route": "NO_TRADE",
        "scenario": "MACRO_COPY_RESONANCE",
        "stage": "UNRESOLVED",
        "left_right_phase": "NONE",
        "reason_codes": ["NO_ELIGIBLE_ROUTE"],
    }
