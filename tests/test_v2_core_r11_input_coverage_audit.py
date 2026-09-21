"""R11 source-coverage checks must fail closed on causal data gaps."""

from __future__ import annotations

import copy

import pytest

from scripts.v2_core_r11_input_coverage_audit import ARTIFACT, _load, audit_case, audit_inputs


def _first_case():
    review_id = "FP-18b86f08f05563ff5886097b"
    anchor = _load(ARTIFACT / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{review_id}.json")
    source = _load(ARTIFACT / "feasibility_probe_packets_r2" / f"{review_id}.json")
    snapshot = _load(ARTIFACT / "legacy_position_snapshots_candidate_r11" / f"{review_id}.json")
    return anchor, source, snapshot


def test_real_calibration_inputs_are_source_bound_but_not_a_full_r11_flow():
    report = audit_inputs()
    assert report["case_count"] == report["full_750_bar_source_cases"] == 14
    assert report["sampled_context_incomplete_for_close_path_cases"] == 14
    assert report["position_snapshot_cases"] == 14
    assert report["no_confirmed_above_high_cases"] == 3
    assert report["cases_with_high_rows_before_full_daily_window"] == 14
    assert not report["full_historical_high_completeness_proven"]
    assert not report["end_to_end_new_policy_position_ledger"]
    assert not report["teacher_answer_read"]
    assert not report["future_outcome_read"]
    assert {row["position_review_role"] for row in report["rows"]} == {"MOTHER_OR_REENTRY", "ADD_1", "ADD_2"}


def test_sampled_ohlc_cannot_silently_replace_full_close_path():
    anchor, source, snapshot = _first_case()
    bad = copy.deepcopy(anchor)
    bad["daily_context_to_as_of"][-1]["close"] += 0.01
    with pytest.raises(ValueError, match="sample OHLC mismatch"):
        audit_case(bad, source, snapshot)


def test_future_confirmed_pivot_is_rejected():
    anchor, source, snapshot = _first_case()
    bad_anchor = copy.deepcopy(anchor)
    bad_source = copy.deepcopy(source)
    bad_anchor["confirmed_pivots_to_as_of"][0]["confirmation_date"] = "2023-05-09"
    bad_source["confirmed_pivots_to_as_of"][0]["confirmation_date"] = "2023-05-09"
    with pytest.raises(ValueError, match="future/unconfirmed pivot"):
        audit_case(bad_anchor, bad_source, snapshot)


def test_position_snapshot_cannot_change_without_role_revalidation():
    anchor, source, snapshot = _first_case()
    bad = copy.deepcopy(snapshot)
    bad["position_snapshot_as_of"]["closed_episode_count"] = 1
    with pytest.raises(ValueError, match="position role/digest mismatch"):
        audit_case(anchor, source, bad)
