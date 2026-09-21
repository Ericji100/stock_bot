from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.hybrid_v3_atomic_policy_v2 import (  # noqa: E402
    POLICY_STATUS,
    POLICY_VERSION,
    SCHEMA_PATH,
    build_gate_matrix,
    contract_errors,
    derive_left_right_phase,
    derive_quadrants,
    expected_question_manifest,
    reduce_atomic_v3,
    validate_atomic,
    validate_course_invariants,
)


SCENARIOS = (
    "MATURE_TREND_PULLBACK",
    "MACRO_COPY_RESONANCE",
    "BEAR_REVERSAL_LEFT_RIGHT",
    "FRESH_Q1_EXPANSION",
)


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _verdict(result: str = "PASS", ref: str = "BAR:2023-06-01") -> dict[str, Any]:
    if result == "PASS":
        return {
            "result": "PASS",
            "supporting_evidence_refs": [ref],
            "contradicting_evidence_refs": [],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
        }
    if result == "FAIL":
        return {
            "result": "FAIL",
            "supporting_evidence_refs": [],
            "contradicting_evidence_refs": [ref],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_CONTRADICTS_DEFINITION",
        }
    return {
        "result": "UNKNOWN",
        "supporting_evidence_refs": [ref],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": ["MISSING_COMPARISON_SEGMENT"],
        "reason_code": "REQUIRED_EVIDENCE_MISSING",
    }


def _answers(
    group: str, default: str = "PASS", question_ids: list[str] | None = None
) -> dict[str, Any]:
    selected = question_ids or _schema()["x-question-groups"][group]
    answers = {question_id: _verdict(default) for question_id in selected}
    if group == "relation":
        for question_id in (
            "EXH_ATTACK_SHORTENING",
            "EXH_SLOPE_DECAY",
            "EXH_PRICE_VOLUME_DIVERGENCE",
            "EXH_FAILED_CONTINUATION",
            "EXH_TIME_SPACE_EXHAUSTION",
            "BEAR_ATTACK_IS_LATE_STAGE",
            "BEAR_LATE_STAGE_PARTIAL",
        ):
            if question_id in answers:
                answers[question_id] = _verdict("FAIL")
    return answers


def _packet() -> dict[str, Any]:
    anchors = [f"ANCHOR_CANDIDATE:A-{name}" for name in ("MATURE", "MACRO", "BEAR", "FRESH")]
    relations = [f"RELATION_CANDIDATE:R-{name}" for name in ("MATURE", "MACRO", "BEAR", "FRESH")]
    stops = [f"STOP_CANDIDATE:T-{name}" for name in ("MATURE", "MACRO", "BEAR", "FRESH")]
    generations = {
        "MATURE_TREND_PULLBACK": "COPY_LEG_3",
        "MACRO_COPY_RESONANCE": "COPY_LEG_3",
        "BEAR_REVERSAL_LEFT_RIGHT": "ANCHOR_LEG_1",
        "FRESH_Q1_EXPANSION": "ANCHOR_LEG_1",
    }
    prior_copies = {
        "MATURE_TREND_PULLBACK": 1,
        "MACRO_COPY_RESONANCE": 0,
        "BEAR_REVERSAL_LEFT_RIGHT": 0,
        "FRESH_Q1_EXPANSION": 0,
    }
    hypotheses = {
        scenario: [{
            "hypothesis_id": f"H-{scenario}",
            "anchor_ref": anchors[index],
            "relation_ref": relations[index],
            "episode_stop_ref": stops[index],
            "campaign_stop_ref": stops[index],
            "position_role": "MOTHER",
            "taiji_generation": generations[scenario],
            "same_direction_attack_number": 1,
            "completed_prior_copy_count": prior_copies[scenario],
        }]
        for index, scenario in enumerate(SCENARIOS)
    }
    windows = {}
    window_evidence = []
    endpoint_dates = ("2023-04-03", "2023-04-14", "2023-05-01", "2023-05-15")
    for scale in ("LARGE", "SMALL"):
        window = {
            "scale": scale,
            "direction": "UP",
            "selection_policy": "LATEST_TWO_COMPLETED_SAME_DIRECTION_LEGACY_PIVOT_LEGS",
            "baseline": {
                "start_date": endpoint_dates[0], "end_date": endpoint_dates[1],
                "start_ref": f"BAR:{endpoint_dates[0]}", "end_ref": f"BAR:{endpoint_dates[1]}",
                "start_price": 40.0, "end_price": 44.0, "bar_count": 10,
                "price_change_pct": 10.0, "slope_pct_per_bar": 1.0,
                "mean_true_range_pct": 2.0, "realized_close_volatility_pct": 1.5,
            },
            "current": {
                "start_date": endpoint_dates[2], "end_date": endpoint_dates[3],
                "start_ref": f"BAR:{endpoint_dates[2]}", "end_ref": f"BAR:{endpoint_dates[3]}",
                "start_price": 45.0, "end_price": 50.0, "bar_count": 11,
                "price_change_pct": 11.111, "slope_pct_per_bar": 1.011,
                "mean_true_range_pct": 2.2, "realized_close_volatility_pct": 1.7,
            },
            "baseline_available_on": endpoint_dates[1],
            "current_available_on": endpoint_dates[3],
            "available_on": endpoint_dates[3],
        }
        windows[scale] = window
        window_evidence.append({
            "ref": f"COMPARISON_WINDOW:{scale}:fixture",
            "kind": "CAUSAL_COMPARISON_WINDOW",
            "date": endpoint_dates[3],
            "values": window,
        })
    packet = {
        "review_id": "D-0123456789abcdef01234567",
        "anonymous_stock_id": "S-0123456789abcdef",
        "as_of": "2023-06-01",
        "input_packet_sha256": "1" * 64,
        "question_manifest_sha256": "2" * 64,
        "evidence_catalog_sha256": "3" * 64,
        "question_manifest": {},
        "evidence": [
            *[
                {"ref": f"BAR:{day}", "kind": "BAR", "date": day, "values": {"close": 40.0}}
                for day in endpoint_dates
            ],
            *window_evidence,
            {"ref": "BAR:2023-06-01", "date": "2023-06-01", "values": {"close": 50.0}},
            {"ref": "EVENT:SMALL_CONTROL_BREAK:2023-06-01", "date": "2023-06-01", "values": {}},
        ],
        "objective_facts": {
            "active_watchlist": True,
            "full_history_visible_bar_count": 750,
            "data_sufficiency_by_route": {
                scenario: {
                    "status": True,
                    "required_visible_bars": 200 if scenario == "FRESH_Q1_EXPANSION" else 750,
                    "actual_visible_bars": 750,
                    "reason_code": "SUFFICIENT_ROUTE_HISTORY_AND_OBJECTIVE_EVIDENCE",
                }
                for scenario in SCENARIOS
            },
            "working_comparison_windows": windows,
            "trigger_completed": True,
            "risk_executable": True,
            "macro_defense_alert": False,
            "parent_campaign_invalidated": False,
            "large_dow_state": "BULL",
            "small_dow_state": "BULL",
            "bear_reversal_context": False,
            "large_bear_defense_broken": None,
            "small_bull_control": True,
            "first_retest_after_large_break_held": None,
            "rr_break_completed": False,
            "direct_same_clean_impulse": False,
            "signal_event_ref": "EVENT:SMALL_CONTROL_BREAK:2023-06-01",
            "large_bull_defense_intact": True,
            "correction_bear_dow_line_causal": True,
            "small_up_control_break": True,
            "stop_causal_fields_valid": True,
            "large_bear_dow_defense_causal": True,
            "phase_stop_causal": True,
            "macd_is_support_only": True,
            "fresh_anchor_stop_causal": True,
            "scenario_hypotheses": hypotheses,
        },
    }
    packet["question_manifest"] = expected_question_manifest(packet)
    return packet


def _hypothesis(packet: dict[str, Any], scenario: str, index: int = 0) -> dict[str, Any]:
    return packet["objective_facts"]["scenario_hypotheses"][scenario][index]


def _hypothesis_id(packet: dict[str, Any], scenario: str, index: int = 0) -> str:
    return str(_hypothesis(packet, scenario, index)["hypothesis_id"])


def _semantic(packet: dict[str, Any] | None = None) -> dict[str, Any]:
    packet = packet or _packet()
    manifest = packet["question_manifest"]
    payload = {
        "schema_version": "hybrid-atomic-semantics-v2",
        "protocol_version": "hybrid-monitoring-protocol-v2",
        "contract_status": "FINAL",
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": packet["input_packet_sha256"],
        "question_manifest_sha256": packet["question_manifest_sha256"],
        "evidence_catalog_sha256": packet["evidence_catalog_sha256"],
        "candidate_answers": {
            "anchor_candidates": [
                {
                    "subject_ref": entry["subject_ref"],
                    "answers": _answers("anchor", question_ids=entry["required_question_ids"]),
                }
                for entry in manifest["anchor_candidates"]
            ],
            "relation_candidates": [
                {
                    "subject_ref": entry["subject_ref"],
                    "answers": _answers("relation", question_ids=entry["required_question_ids"]),
                }
                for entry in manifest["relation_candidates"]
            ],
            "stop_candidates": [
                {
                    "subject_ref": entry["subject_ref"],
                    "answers": _answers("stop", question_ids=entry["required_question_ids"]),
                }
                for entry in manifest["stop_candidates"]
            ],
        },
        "global_answers": _answers("global", question_ids=manifest["global_question_ids"]),
        "causal_attestation": {
            "latest_visible_bar": packet["as_of"],
            "used_future_data": False,
            "identity_visible": False,
            "performance_visible": False,
            "invented_evidence_ref": False,
            "invented_candidate_ref": False,
            "answered_complete_manifest": True,
            "selected_scenario": False,
            "selected_phase": False,
            "selected_route": False,
            "decided_permission": False,
            "issued_trade_instruction": False,
        },
    }
    _set_atom(payload, "relation_candidates", "RELATION_CANDIDATE:R-MATURE", "LONG_CAMPAIGN_REPEATED_SUCCESS", "FAIL")
    _set_atom(payload, "anchor_candidates", "ANCHOR_CANDIDATE:A-BEAR", "ANCHOR_DIRECTION_COHERENT", "FAIL")
    _set_atom(payload, "relation_candidates", "RELATION_CANDIDATE:R-FRESH", "REL_CURRENT_LEG_IS_FRESH_ANCHOR", "FAIL")
    return payload


def _set_atom(payload: dict[str, Any], group: str, subject_ref: str, question_id: str, result: str) -> None:
    row = next(item for item in payload["candidate_answers"][group] if item["subject_ref"] == subject_ref)
    row["answers"][question_id] = _verdict(result)


def _set_global(payload: dict[str, Any], question_id: str, result: str) -> None:
    payload["global_answers"][question_id] = _verdict(result)


def test_contract_reads_exact_frozen_v2_v3_gate_sets() -> None:
    assert contract_errors() == []
    assert POLICY_VERSION == "hybrid-v3-atomic-policy-v2"
    assert POLICY_STATUS == "FINAL"


def test_valid_macro_copy_core_produces_trade_and_complete_derived_state() -> None:
    packet = _packet()
    result = reduce_atomic_v3(packet, _semantic(packet))
    assert result["permission"] == "TRADE"
    assert result["route"] == "V2_CORE"
    assert result["scenario"] == "MACRO_COPY_RESONANCE"
    assert result["derived_structure"]["quadrants"] == {"large": "Q1", "small": "Q1"}
    assert result["derived_structure"]["stage"] == "EARLY"
    assert result["derived_structure"]["left_right_phase"] == "NONE"
    assert result["action_signature"] == {
        "direction": "UP",
        "signal_event_ref": "EVENT:SMALL_CONTROL_BREAK:2023-06-01",
        "episode_stop_ref": "STOP_CANDIDATE:T-MACRO",
        "campaign_stop_ref": "STOP_CANDIDATE:T-MACRO",
        "position_role": "MOTHER",
    }


def test_macro_near_pass_allows_exactly_one_frozen_soft_unknown() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    _set_atom(semantic, "relation_candidates", "RELATION_CANDIDATE:R-MACRO", "TAIJI_RELATION_TRACEABLE", "UNKNOWN")
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "TRADE"
    assert result["route"] == "NEAR_PASS_MACRO_COPY"
    assert result["unknown_gate"] == "TAIJI_GENERATION_MAPPED"
    assert result["action_signature"]["position_role"] == "PROBE_MOTHER"


def test_two_unknowns_or_missing_objective_fact_must_wait() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    _set_atom(semantic, "relation_candidates", "RELATION_CANDIDATE:R-MACRO", "TAIJI_RELATION_TRACEABLE", "UNKNOWN")
    _set_global(semantic, "LARGE_NEXT_UP_DIRECTION_SUPPORTED", "UNKNOWN")
    assert reduce_atomic_v3(packet, semantic)["permission"] == "WAIT"

    packet = _packet()
    del packet["objective_facts"]["risk_executable"]
    result = reduce_atomic_v3(packet, _semantic(packet))
    assert result["permission"] == "WAIT"
    hypothesis_id = _hypothesis_id(packet, "MACRO_COPY_RESONANCE")
    assert result["common_hard_guards"]["MACRO_COPY_RESONANCE"][hypothesis_id]["RISK_EXECUTABLE"] == "UNKNOWN"


def test_relation_bound_taiji_answer_does_not_pollute_other_hypothesis() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    _set_atom(semantic, "relation_candidates", "RELATION_CANDIDATE:R-FRESH", "TAIJI_RELATION_TRACEABLE", "UNKNOWN")
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "TRADE"
    macro_id = _hypothesis_id(packet, "MACRO_COPY_RESONANCE")
    fresh_id = _hypothesis_id(packet, "FRESH_Q1_EXPANSION")
    assert result["gate_matrix"]["MACRO_COPY_RESONANCE"][macro_id]["gates"]["TAIJI_GENERATION_MAPPED"]["result"] == "PASS"
    assert result["gate_matrix"]["FRESH_Q1_EXPANSION"][fresh_id]["gates"]["EARLY_TAIJI_GENERATION"]["result"] == "UNKNOWN"


def test_bear_probe_uses_program_phase_and_relation_bound_partial_evidence() -> None:
    packet = _packet()
    objective = packet["objective_facts"]
    objective.update({
        "bear_reversal_context": True,
        "large_dow_state": "BEAR",
        "small_dow_state": "BULL",
        "large_bear_defense_broken": False,
        "small_bull_control": True,
    })
    semantic = _semantic(packet)
    _set_global(semantic, "LARGE_NEXT_UP_DIRECTION_SUPPORTED", "FAIL")
    _set_atom(semantic, "anchor_candidates", "ANCHOR_CANDIDATE:A-BEAR", "ANCHOR_DIRECTION_COHERENT", "PASS")
    for ref in ("ANCHOR_CANDIDATE:A-MATURE", "ANCHOR_CANDIDATE:A-MACRO", "ANCHOR_CANDIDATE:A-FRESH"):
        _set_atom(semantic, "anchor_candidates", ref, "ANCHOR_DIRECTION_COHERENT", "FAIL")
    _set_atom(semantic, "relation_candidates", "RELATION_CANDIDATE:R-BEAR", "BEAR_ATTACK_IS_LATE_STAGE", "UNKNOWN")
    _set_atom(semantic, "relation_candidates", "RELATION_CANDIDATE:R-BEAR", "BEAR_LATE_STAGE_PARTIAL", "PASS")
    result = reduce_atomic_v3(packet, semantic)
    assert result["derived_structure"]["left_right_phase"] == "LR"
    assert result["permission"] == "TRADE"
    assert result["route"] == "BEAR_REVERSAL_PROBE"

    packet["objective_facts"]["small_bull_control"] = False
    result = reduce_atomic_v3(packet, semantic)
    assert result["derived_structure"]["left_right_phase"] == "LL"
    assert result["permission"] == "WAIT"


def test_confirmed_parent_invalidation_removes_before_entry_policy() -> None:
    packet = _packet()
    packet["objective_facts"]["macro_defense_alert"] = True
    packet["objective_facts"]["parent_campaign_invalidated"] = True
    result = reduce_atomic_v3(packet, _semantic(packet))
    assert result["permission"] == "REMOVE"
    assert result["reason_codes"] == ["LARGE_CAMPAIGN_INVALIDATED"]


def test_materially_different_eligible_action_signatures_must_wait() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    _set_atom(semantic, "relation_candidates", "RELATION_CANDIDATE:R-FRESH", "REL_CURRENT_LEG_IS_FRESH_ANCHOR", "PASS")
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "WAIT"
    assert result["reason_codes"] == ["MATERIAL_HYPOTHESIS_CONFLICT"]
    assert {row["scenario"] for row in result["hypotheses"]} == {
        "MACRO_COPY_RESONANCE", "FRESH_Q1_EXPANSION"
    }


def test_missing_current_context_relation_is_invalid_packet_not_guessed() -> None:
    packet = _packet()
    _hypothesis(packet, "FRESH_Q1_EXPANSION").pop("relation_ref")
    result = reduce_atomic_v3(packet, _semantic(packet))
    assert result["permission"] == "WAIT"
    assert result["reason_codes"] == ["INVALID_PACKET"]
    assert any("CURRENT_CONTEXT" in error for error in result["validation_errors"])


def test_schema_hash_subject_coverage_and_evidence_membership_are_validated() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    semantic["input_packet_sha256"] = "9" * 64
    assert any("input_packet_sha256" in error for error in validate_atomic(packet, semantic))

    semantic = _semantic(packet)
    semantic["candidate_answers"]["relation_candidates"].pop()
    assert any("exactly cover" in error for error in validate_atomic(packet, semantic))

    semantic = _semantic(packet)
    semantic["global_answers"]["SIGNAL_HAS_INDEPENDENT_STRUCTURE"]["supporting_evidence_refs"] = ["BAR:1999-01-01"]
    assert any("unknown evidence ref" in error for error in validate_atomic(packet, semantic))


def test_future_evidence_and_ai_derived_policy_fields_are_rejected() -> None:
    packet = _packet()
    packet["evidence"].append({"ref": "BAR:2023-06-02", "date": "2023-06-02", "values": {}})
    semantic = _semantic(packet)
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "WAIT"
    assert any("future evidence" in error for error in result["validation_errors"])

    semantic["global_answers"]["SIGNAL_HAS_INDEPENDENT_STRUCTURE"] = _verdict("PASS", "BAR:2023-06-02")
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "WAIT"
    assert any("future evidence" in error for error in result["validation_errors"])

    semantic = _semantic(_packet())
    semantic["primary_scenario"] = "MACRO_COPY_RESONANCE"
    result = reduce_atomic_v3(_packet(), semantic)
    assert result["permission"] == "WAIT"
    assert result["reason_codes"] == ["INVALID_AI_OUTPUT"]


def test_missing_atomic_fact_propagates_unknown_instead_of_program_guess() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    _set_atom(semantic, "relation_candidates", "RELATION_CANDIDATE:R-MACRO", "REL_CORRECTION_PRESERVES_PARENT", "UNKNOWN")
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "WAIT"
    hypothesis_id = _hypothesis_id(packet, "MACRO_COPY_RESONANCE")
    assert result["gate_matrix"]["MACRO_COPY_RESONANCE"][hypothesis_id]["gates"]["CORRECTION_INTACT"]["result"] == "UNKNOWN"


def test_route_specific_history_allows_fresh_at_operational_200_but_blocks_macro() -> None:
    packet = _packet()
    packet["objective_facts"]["full_history_visible_bar_count"] = 200
    for scenario, row in packet["objective_facts"]["data_sufficiency_by_route"].items():
        row["actual_visible_bars"] = 200
        if scenario == "FRESH_Q1_EXPANSION":
            row.update({
                "status": True,
                "reason_code": "SUFFICIENT_ROUTE_HISTORY_AND_OBJECTIVE_EVIDENCE",
            })
        else:
            row.update({"status": False, "reason_code": "INSUFFICIENT_VISIBLE_BARS"})
    semantic = _semantic(packet)
    _set_atom(
        semantic, "relation_candidates", "RELATION_CANDIDATE:R-MACRO",
        "REL_CURRENT_LEG_IS_REPLICATION", "FAIL",
    )
    _set_atom(
        semantic, "relation_candidates", "RELATION_CANDIDATE:R-FRESH",
        "REL_CURRENT_LEG_IS_FRESH_ANCHOR", "PASS",
    )
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "TRADE"
    assert result["route"] == "V2_CORE"
    assert result["scenario"] == "FRESH_Q1_EXPANSION"

    packet = _packet()
    packet["objective_facts"]["full_history_visible_bar_count"] = 200
    for scenario, row in packet["objective_facts"]["data_sufficiency_by_route"].items():
        row["actual_visible_bars"] = 200
        row.update({
            "status": False,
            "reason_code": (
                "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE"
                if scenario == "FRESH_Q1_EXPANSION" else "INSUFFICIENT_VISIBLE_BARS"
            ),
        })
    result = reduce_atomic_v3(packet, _semantic(packet))
    macro_id = _hypothesis_id(packet, "MACRO_COPY_RESONANCE")
    assert result["permission"] == "WAIT"
    assert result["common_hard_guards"]["MACRO_COPY_RESONANCE"][macro_id]["DATA_SUFFICIENT_FOR_ROUTE"] == "FAIL"


def test_missing_comparison_window_forces_unknown_and_wait() -> None:
    packet = _packet()
    packet["objective_facts"]["working_comparison_windows"]["SMALL"] = None
    packet["evidence"] = [
        row for row in packet["evidence"]
        if row.get("ref") != "COMPARISON_WINDOW:SMALL:fixture"
    ]
    for row in packet["objective_facts"]["data_sufficiency_by_route"].values():
        row.update({
            "status": False,
            "reason_code": "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
        })
    semantic = _semantic(packet)
    semantic["global_answers"]["SMALL_TREND_STRENGTH_INCREASED"] = _verdict("UNKNOWN")
    semantic["global_answers"]["SMALL_VOLATILITY_EXPANDED"] = _verdict("UNKNOWN")
    assert validate_atomic(packet, semantic) == []
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "WAIT"
    assert result["reason_codes"] == ["NO_ELIGIBLE_ROUTE"]

    semantic["global_answers"]["SMALL_TREND_STRENGTH_INCREASED"] = _verdict("PASS")
    assert any(
        "requires UNKNOWN when its comparison window is unavailable" in error
        for error in validate_atomic(packet, semantic)
    )


def test_route_data_contract_rejects_global_legacy_fact_and_false_threshold_claim() -> None:
    packet = _packet()
    packet["objective_facts"]["data_sufficient_for_route"] = True
    assert any("legacy nested fields" in error for error in validate_atomic(packet, _semantic(packet)))

    packet = _packet()
    packet["objective_facts"]["data_sufficiency_by_route"]["MACRO_COPY_RESONANCE"][
        "required_visible_bars"
    ] = 200
    assert any(
        "required_visible_bars differs" in error
        for error in validate_atomic(packet, _semantic(packet))
    )


def test_unknown_execution_signature_is_wait_not_a_trade() -> None:
    packet = _packet()
    _hypothesis(packet, "MACRO_COPY_RESONANCE")["position_role"] = "UNKNOWN"
    result = reduce_atomic_v3(packet, _semantic(packet))
    assert result["permission"] == "WAIT"
    assert result["reason_codes"] == ["ACTION_SIGNATURE_INCOMPLETE"]
    assert "position_role" in result["hypotheses"][0]["missing_signature_fields"]


def test_campaign_stop_requires_parent_campaign_atom_pass() -> None:
    for verdict in ("UNKNOWN", "FAIL"):
        packet = _packet()
        semantic = _semantic(packet)
        _set_atom(
            semantic,
            "stop_candidates",
            "STOP_CANDIDATE:T-MACRO",
            "STOP_BELONGS_TO_PARENT_CAMPAIGN",
            verdict,
        )
        result = reduce_atomic_v3(packet, semantic)
        hypothesis_id = _hypothesis_id(packet, "MACRO_COPY_RESONANCE")
        assert result["permission"] == "WAIT"
        assert result["common_hard_guards"]["MACRO_COPY_RESONANCE"][hypothesis_id]["CAMPAIGN_STOP_CAUSAL"] == verdict


def test_course_invariant_rejects_trade_when_campaign_atom_is_no_longer_pass() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    prior_trade = reduce_atomic_v3(packet, semantic)
    assert prior_trade["permission"] == "TRADE"
    _set_atom(
        semantic,
        "stop_candidates",
        "STOP_CANDIDATE:T-MACRO",
        "STOP_BELONGS_TO_PARENT_CAMPAIGN",
        "UNKNOWN",
    )
    errors = validate_course_invariants(packet, semantic, prior_trade)
    assert any("campaign stop lacks causal" in error for error in errors)


def test_macro_first_copy_precedes_mature_and_unknown_first_copy_blocks_later_route() -> None:
    packet = _packet()
    # Equalize execution semantics to prove the boundary order, not a signature conflict.
    mature = _hypothesis(packet, "MATURE_TREND_PULLBACK")
    macro = _hypothesis(packet, "MACRO_COPY_RESONANCE")
    mature["episode_stop_ref"] = macro["episode_stop_ref"]
    mature["campaign_stop_ref"] = macro["campaign_stop_ref"]
    packet["question_manifest"] = expected_question_manifest(packet)
    semantic = _semantic(packet)
    _set_atom(
        semantic,
        "relation_candidates",
        "RELATION_CANDIDATE:R-MATURE",
        "LONG_CAMPAIGN_REPEATED_SUCCESS",
        "PASS",
    )
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "TRADE"
    assert result["scenario"] == "MACRO_COPY_RESONANCE"

    macro["completed_prior_copy_count"] = "UNKNOWN"
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "WAIT"
    assert result["scenario"] == "UNRESOLVED_NO_TRADE"
    assert result["reason_codes"] == ["PRIMARY_SCENARIO_UNRESOLVED"]


def test_same_scenario_two_hypotheses_with_same_signature_can_trade() -> None:
    packet = _packet()
    duplicate = copy.deepcopy(_hypothesis(packet, "MACRO_COPY_RESONANCE"))
    duplicate["hypothesis_id"] = "H-MACRO-ALTERNATIVE"
    packet["objective_facts"]["scenario_hypotheses"]["MACRO_COPY_RESONANCE"].append(duplicate)
    result = reduce_atomic_v3(packet, _semantic(packet))
    assert result["permission"] == "TRADE"
    assert result["scenario"] == "MACRO_COPY_RESONANCE"
    macro_rows = [row for row in result["hypotheses"] if row["scenario"] == "MACRO_COPY_RESONANCE"]
    assert len(macro_rows) == 2
    assert len({json.dumps(row["action_signature"], sort_keys=True) for row in macro_rows}) == 1


def test_same_scenario_two_hypotheses_with_different_stop_must_wait() -> None:
    packet = _packet()
    duplicate = copy.deepcopy(_hypothesis(packet, "MACRO_COPY_RESONANCE"))
    duplicate["hypothesis_id"] = "H-MACRO-DIFFERENT-STOP"
    duplicate["episode_stop_ref"] = "STOP_CANDIDATE:T-FRESH"
    duplicate["campaign_stop_ref"] = "STOP_CANDIDATE:T-FRESH"
    packet["objective_facts"]["scenario_hypotheses"]["MACRO_COPY_RESONANCE"].append(duplicate)
    result = reduce_atomic_v3(packet, _semantic(packet))
    assert result["permission"] == "WAIT"
    assert result["reason_codes"] == ["MATERIAL_HYPOTHESIS_CONFLICT"]


def test_formal_packet_contract_rejects_legacy_builder_shapes() -> None:
    packet = _packet()
    packet["program_facts"] = {}
    result = reduce_atomic_v3(packet, _semantic(packet))
    assert result["permission"] == "WAIT"
    assert result["reason_codes"] == ["INVALID_PACKET"]
    assert any("legacy top-level" in error for error in result["validation_errors"])


def test_subject_manifest_and_ai_answers_require_exact_question_coverage() -> None:
    packet = _packet()
    malformed = copy.deepcopy(packet)
    malformed["question_manifest"]["relation_candidates"][0]["required_question_ids"].pop()
    semantic = _semantic(malformed)
    result = reduce_atomic_v3(malformed, semantic)
    assert result["permission"] == "WAIT"
    assert any("deterministic hypothesis/gate requirements" in error for error in result["validation_errors"])

    packet = _packet()
    semantic = _semantic(packet)
    row = semantic["candidate_answers"]["relation_candidates"][0]
    row["answers"].pop(next(iter(row["answers"])))
    result = reduce_atomic_v3(packet, semantic)
    assert result["permission"] == "WAIT"
    assert any("exactly cover required_question_ids" in error for error in result["validation_errors"])


def test_subject_specific_manifest_reduces_fixture_verdicts_without_changing_policy() -> None:
    packet = _packet()
    manifest = packet["question_manifest"]
    actual = sum(
        len(entry["required_question_ids"])
        for group in ("anchor_candidates", "relation_candidates", "stop_candidates")
        for entry in manifest[group]
    ) + len(manifest["global_question_ids"])
    full_cross_product = (
        4 * len(_schema()["x-question-groups"]["anchor"])
        + 4 * len(_schema()["x-question-groups"]["relation"])
        + 4 * len(_schema()["x-question-groups"]["stop"])
        + len(_schema()["x-question-groups"]["global"])
    )
    assert actual == 76
    assert full_cross_product == 115
    assert reduce_atomic_v3(packet, _semantic(packet))["permission"] == "TRADE"


def test_course_invariants_recompute_decision_and_reject_material_tampering() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    decision = reduce_atomic_v3(packet, semantic)
    assert validate_course_invariants(packet, semantic, decision) == []

    tampered = copy.deepcopy(decision)
    tampered["action_signature"]["direction"] = "DOWN"
    errors = validate_course_invariants(packet, semantic, tampered)
    assert any("action_signature" in error for error in errors)


def test_course_invariants_accept_conflict_wait_and_reject_false_remove() -> None:
    packet = _packet()
    semantic = _semantic(packet)
    _set_atom(semantic, "relation_candidates", "RELATION_CANDIDATE:R-FRESH", "REL_CURRENT_LEG_IS_FRESH_ANCHOR", "PASS")
    decision = reduce_atomic_v3(packet, semantic)
    assert decision["reason_codes"] == ["MATERIAL_HYPOTHESIS_CONFLICT"]
    assert validate_course_invariants(packet, semantic, decision) == []

    false_remove = copy.deepcopy(decision)
    false_remove.update({"permission": "REMOVE", "route": "NO_TRADE"})
    errors = validate_course_invariants(packet, semantic, false_remove)
    assert any("REMOVE requires" in error for error in errors)


def test_phase_state_machine_does_not_use_ai_labels() -> None:
    assert derive_left_right_phase({
        "bear_reversal_context": True,
        "large_dow_state": "BEAR",
        "large_bear_defense_broken": False,
        "small_bull_control": False,
        "direct_same_clean_impulse": False,
    }) == "LL"
    assert derive_left_right_phase({
        "bear_reversal_context": True,
        "large_dow_state": "BEAR",
        "large_bear_defense_broken": True,
        "small_bull_control": True,
        "first_retest_after_large_break_held": True,
        "rr_break_completed": False,
        "direct_same_clean_impulse": False,
    }) == "RL"


def test_quadrant_mapping_is_deterministic() -> None:
    semantic = _semantic(_packet())
    assert derive_quadrants(semantic) == {"large": "Q1", "small": "Q1"}
    _set_global(semantic, "LARGE_TREND_STRENGTH_INCREASED", "FAIL")
    _set_global(semantic, "LARGE_VOLATILITY_EXPANDED", "FAIL")
    assert derive_quadrants(semantic)["large"] == "Q3"
