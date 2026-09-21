from __future__ import annotations

from pathlib import Path

from scripts.v2_core_legacy_teacher_trace_v1 import build_trace


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)


def test_teacher_trace_preserves_scope_and_exact_reference_objects() -> None:
    trace = build_trace(ARTIFACT_DIR)
    assert trace["case_count"] == 14
    assert trace["formal_ai_calls"] == 0
    assert trace["future_performance_used"] is False
    assert trace["locked_reproduction_set_opened"] is False
    assert trace["identity_used"] is False
    assert trace["r4_outputs_modified"] is False
    assert trace["summary"]["r4_exact_teacher_working_anchor_count"] == 1
    for case in trace["cases"]:
        teacher = case["teacher"]["working_anchor"]
        assert case["teacher"]["matching_fixed_candidates"]
        for candidate in case["teacher"]["matching_fixed_candidates"]:
            assert candidate["direction"] == teacher["direction"]
            assert candidate["start_date"] == teacher["start_date"]
            assert candidate["confirmed_end_date"] == teacher["end_date"]


def test_teacher_trace_contains_no_performance_fields() -> None:
    trace = build_trace(ARTIFACT_DIR)
    keys: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            keys.update(str(key).lower() for key in value)
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(trace)
    forbidden = {"mfe", "mae", "profit", "return_pct", "future_bars", "winner"}
    assert keys.isdisjoint(forbidden)


def test_teacher_trace_documents_systematic_pivot_bias() -> None:
    trace = build_trace(ARTIFACT_DIR)
    selected = trace["summary"]["r4_selected_basis_counts"]
    assert selected == {
        "PIVOT_CONFIRMED_CAMPAIGN": 5,
        "PIVOT_FORMING_CAMPAIGN": 9,
    }
    assert trace["summary"]["mismatched_teacher_candidate_in_r4_alternatives_count"] == 0
    pairs = trace["summary"]["teacher_to_r4_scenario_pairs"]
    assert pairs["MACRO_COPY_RESONANCE|MATURE_TREND_PULLBACK"] == 4
    assert pairs["FRESH_Q1_EXPANSION|MATURE_TREND_PULLBACK"] == 3
