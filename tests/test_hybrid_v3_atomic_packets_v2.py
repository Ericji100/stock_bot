import copy
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.hybrid_v3_atomic_packets_v2 import (
    AI_VISIBLE_RECENT_BAR_WINDOW_DRAFT,
    FORMAL_REVIEW_POINT_POLICY,
    DEFAULT_ATOMIC_SCHEMA,
    FORMAL_SOURCE_FILE,
    FORMAL_SOURCE_MANIFEST_FILE,
    PIVOT_SCALE_DISCLOSURE,
    AtomicPacketError,
    add_causal_indicators,
    assert_anonymous_and_causal,
    build_atomic_packet,
    build_daily_objective_states,
    build_required_question_manifest,
    build_stock_packets,
    build_formal_shard,
    causal_cycles_as_of,
    derive_review_points,
    load_formal_inventory,
    merge_formal_build_shards,
    prepare_formal_build_plan,
    _anchor_candidates,
    _relation_candidates,
    _review_manifest_state,
)
from scripts.hybrid_v3_sharding_v2 import canonical_sha256
from scripts.hybrid_v3_sharding_v2 import file_sha256
from scripts.hybrid_v3_atomic_policy_v2 import (
    SCHEMA_PATH, expected_question_manifest, reduce_atomic_v3, validate_atomic,
)
from scripts import hybrid_v3_freeze_v2 as freeze_v2
from scripts import hybrid_v3_course_gold_v2 as course_gold_v2
from scripts.hybrid_v3_freeze_v2 import FreezeValidationError, _validate_source_and_review_points


def _frame(count=240):
    dates = pd.bdate_range("2022-01-03", periods=count)
    close = [50 + index * 0.04 + 3 * __import__("math").sin(index / 8) for index in range(count)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [value - 0.2 for value in close],
            "high": [value + 0.7 for value in close],
            "low": [value - 0.8 for value in close],
            "close": close,
            "volume": [1000 + (index % 17) * 20 for index in range(count)],
        }
    )


def _day(frame, index):
    return pd.Timestamp(frame.iloc[index]["date"]).date().isoformat()


def _review_packet(frame):
    return {
        "selection_timeline": {
            _day(frame, 150): ["STRATEGY_A"],
            _day(frame, 175): ["STRATEGY_B"],
            _day(frame, 220): ["FUTURE_SELECTION"],
        },
        "confirmed_pivots": [
            {"confirmation_date": _day(frame, 80), "source_date": _day(frame, 77), "scale": "SMALL", "side": "LOW", "price": 48.0},
            {"confirmation_date": _day(frame, 90), "source_date": _day(frame, 87), "scale": "SMALL", "side": "HIGH", "price": 54.0},
            {"confirmation_date": _day(frame, 110), "source_date": _day(frame, 107), "scale": "SMALL", "side": "LOW", "price": 50.0},
            {"confirmation_date": _day(frame, 120), "source_date": _day(frame, 117), "scale": "SMALL", "side": "HIGH", "price": 55.0},
            {"confirmation_date": _day(frame, 70), "source_date": _day(frame, 60), "scale": "LARGE", "side": "LOW", "price": 46.0},
            {"confirmation_date": _day(frame, 100), "source_date": _day(frame, 90), "scale": "LARGE", "side": "HIGH", "price": 55.0},
            {"confirmation_date": _day(frame, 130), "source_date": _day(frame, 120), "scale": "LARGE", "side": "LOW", "price": 49.0},
            {"confirmation_date": _day(frame, 145), "source_date": _day(frame, 135), "scale": "LARGE", "side": "HIGH", "price": 57.0},
            {"confirmation_date": _day(frame, 220), "source_date": _day(frame, 217), "scale": "SMALL", "side": "HIGH", "price": 99.0},
        ],
        "macd_21_55_55_cycles": [
            {
                "sign": "POSITIVE", "start": _day(frame, 70), "end": _day(frame, 120), "status": "CONFIRMED",
                "bars": 51, "low_date": _day(frame, 72), "low": 48.0, "high_date": _day(frame, 115), "high": 57.0,
                "start_close": 50.0, "last_close": 56.0,
            },
            {
                "sign": "NEGATIVE", "start": _day(frame, 121), "end": _day(frame, 220), "status": "CONFIRMED",
                "bars": 100, "low_date": _day(frame, 215), "low": 30.0, "high_date": _day(frame, 221), "high": 100.0,
                "start_close": 56.0, "last_close": 31.0,
            },
        ],
    }


def _schema(groups=None):
    return {
        "$id": "test-schema-v2",
        "x-schema-version": "hybrid-atomic-semantics-v2",
        "x-contract-status": "FINAL",
        "x-question-groups": groups or {
            "anchor": ["ANCHOR_A", "ANCHOR_B"],
            "relation": ["TAIJI_REL", "LOCATION_REL", "EXH_REL", "BEAR_REL"],
            "stop": ["STOP_A"],
            "global": ["GLOBAL_A"],
        },
    }


def _atomic_verdict(result, ref):
    if result == "PASS":
        return {
            "result": result, "supporting_evidence_refs": [ref], "contradicting_evidence_refs": [],
            "missing_evidence_codes": [], "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
        }
    if result == "FAIL":
        return {
            "result": result, "supporting_evidence_refs": [], "contradicting_evidence_refs": [ref],
            "missing_evidence_codes": [], "reason_code": "VISIBLE_EVIDENCE_CONTRADICTS_DEFINITION",
        }
    return {
        "result": "UNKNOWN", "supporting_evidence_refs": [ref], "contradicting_evidence_refs": [],
        "missing_evidence_codes": ["MISSING_COMPARISON_SEGMENT"],
        "reason_code": "REQUIRED_EVIDENCE_MISSING",
    }


def _policy_semantic(packet, default="UNKNOWN"):
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    groups = schema["x-question-groups"]
    ref = f"BAR:{packet['as_of']}"

    def answers(group):
        return {question: _atomic_verdict(default, ref) for question in groups[group]}

    return {
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
                    "answers": {
                        question: _atomic_verdict(default, ref)
                        for question in entry["required_question_ids"]
                    },
                }
                for entry in packet["question_manifest"]["anchor_candidates"]
            ],
            "relation_candidates": [
                {
                    "subject_ref": entry["subject_ref"],
                    "answers": {
                        question: _atomic_verdict(default, ref)
                        for question in entry["required_question_ids"]
                    },
                }
                for entry in packet["question_manifest"]["relation_candidates"]
            ],
            "stop_candidates": [
                {
                    "subject_ref": entry["subject_ref"],
                    "answers": {
                        question: _atomic_verdict(default, ref)
                        for question in entry["required_question_ids"]
                    },
                }
                for entry in packet["question_manifest"]["stop_candidates"]
            ],
        },
        "global_answers": {
            question: _atomic_verdict(default, ref)
            for question in packet["question_manifest"]["global_question_ids"]
        },
        "causal_attestation": {
            "latest_visible_bar": packet["as_of"], "used_future_data": False,
            "identity_visible": False, "performance_visible": False,
            "invented_evidence_ref": False, "invented_candidate_ref": False,
            "answered_complete_manifest": True, "selected_scenario": False,
            "selected_phase": False, "selected_route": False,
            "decided_permission": False, "issued_trade_instruction": False,
        },
    }


def _long_causal_fixture():
    dates = pd.bdate_range("2020-01-02", periods=800)
    close = [10.0] * 800
    close[784:790] = [10, 8, 8.5, 9, 10, 12]
    frame = pd.DataFrame({
        "date": dates, "open": close, "high": [value + 0.4 for value in close],
        "low": [value - 0.4 for value in close], "close": close, "volume": [1000] * 800,
    })
    review = {
        "selection_timeline": {_day(frame, 785): ["S"]},
        "confirmed_pivots": [
            {"source_date": _day(frame, 740), "confirmation_date": _day(frame, 741), "scale": "LARGE", "side": "LOW", "price": 7},
            {"source_date": _day(frame, 750), "confirmation_date": _day(frame, 751), "scale": "LARGE", "side": "HIGH", "price": 10},
            {"source_date": _day(frame, 760), "confirmation_date": _day(frame, 761), "scale": "LARGE", "side": "LOW", "price": 8},
            {"source_date": _day(frame, 780), "confirmation_date": _day(frame, 781), "scale": "SMALL", "side": "LOW", "price": 9},
            {"source_date": _day(frame, 783), "confirmation_date": _day(frame, 784), "scale": "SMALL", "side": "HIGH", "price": 11},
            {"source_date": _day(frame, 786), "confirmation_date": _day(frame, 787), "scale": "SMALL", "side": "LOW", "price": 8},
            {"source_date": _day(frame, 788), "confirmation_date": _day(frame, 789), "scale": "SMALL", "side": "HIGH", "price": 12},
            {"source_date": _day(frame, 780), "confirmation_date": _day(frame, 781), "scale": "LARGE", "side": "HIGH", "price": 11},
            {"source_date": _day(frame, 783), "confirmation_date": _day(frame, 784), "scale": "LARGE", "side": "LOW", "price": 9},
        ],
        "macd_21_55_55_cycles": [],
    }
    return frame, review, _day(frame, 785), _day(frame, 789)


def _bear_causal_fixture():
    dates = pd.bdate_range("2020-01-02", periods=810)
    close = [10.0] * 810
    close[784:796] = [10, 8, 8.5, 9, 10, 12, 11, 10.5, 10, 11, 11.5, 13]
    frame = pd.DataFrame({
        "date": dates, "open": close, "high": [value + 0.4 for value in close],
        "low": [value - 0.4 for value in close], "close": close, "volume": [1000] * len(close),
    })
    rows = [
        ("LARGE", "HIGH", 700, 701, 15), ("LARGE", "LOW", 720, 721, 10),
        ("LARGE", "HIGH", 760, 761, 12), ("LARGE", "LOW", 780, 781, 9),
        ("LARGE", "HIGH", 783, 784, 11),
        ("SMALL", "HIGH", 700, 701, 9), ("SMALL", "LOW", 720, 721, 7),
        ("SMALL", "HIGH", 780, 781, 11), ("SMALL", "LOW", 783, 784, 8),
        ("SMALL", "HIGH", 789, 790, 12), ("SMALL", "LOW", 791, 792, 10),
    ]
    review = {
        "selection_timeline": {_day(frame, 785): ["S"]},
        "confirmed_pivots": [
            {
                "source_date": _day(frame, source), "confirmation_date": _day(frame, confirmed),
                "scale": scale, "side": side, "price": price,
            }
            for scale, side, source, confirmed, price in rows
        ],
        "macd_21_55_55_cycles": [],
    }
    return frame, review, _day(frame, 785), _day(frame, 795)


def test_full_history_engine_scans_every_monitored_day_and_rebuilds_events():
    frame = _frame()
    packet = _review_packet(frame)
    monitor = _day(frame, 150)
    as_of = _day(frame, 200)
    visible, states = build_daily_objective_states(frame, packet, monitor_on=monitor, as_of=as_of)
    assert len(visible) == 201
    assert len(states) == 51
    assert states[0]["events"] == ["INITIAL_SELECTION"]
    assert any("UPSTREAM_SELECTION_REFRESH" in row["events"] for row in states)
    assert all(row["as_of"] <= as_of for row in states)
    assert sum(row["review_required_before_lazy"] for row in states) > 1
    assert derive_review_points(states) == []
    assert states[0]["skip_reason"] == "INSUFFICIENT_HISTORY_PROGRAM_GUARD"
    assert states[-1]["skip_reason"] == "NO_PERMISSION_RELEVANT_CHANGE"
    assert derive_review_points(states, legacy_dates=set()) == []


def test_pre_monitor_attacks_initialize_state_without_becoming_new_monitor_events():
    frame = _frame()
    monitor = _day(frame, 150)
    _, states = build_daily_objective_states(
        frame, _review_packet(frame), monitor_on=monitor, as_of=_day(frame, 155)
    )
    first = states[0]
    assert any(attack["confirmed_on"] < monitor for attack in first["objective_state"]["attacks"])
    assert first["events"] == ["INITIAL_SELECTION"]
    assert first["objective_state"]["controls"]["small"]["state"] != "UNRESOLVED"


def test_directional_causal_breaks_create_only_causal_defenses_and_stops():
    dates = pd.bdate_range("2023-01-02", periods=10)
    close = [10, 10, 9.5, 10, 10.5, 10, 8, 8.5, 10, 12]
    frame = pd.DataFrame({
        "date": dates,
        "open": close,
        "high": [value + 0.4 for value in close],
        "low": [value - 0.4 for value in close],
        "close": close,
        "volume": [1000] * len(close),
    })
    review = {
        "selection_timeline": {_day(frame, 6): ["S"]},
        "confirmed_pivots": [
            {"source_date": _day(frame, 2), "confirmation_date": _day(frame, 3), "scale": "SMALL", "side": "LOW", "price": 9},
            {"source_date": _day(frame, 4), "confirmation_date": _day(frame, 5), "scale": "SMALL", "side": "HIGH", "price": 11},
            {"source_date": _day(frame, 7), "confirmation_date": _day(frame, 8), "scale": "SMALL", "side": "LOW", "price": 8},
        ],
        "macd_21_55_55_cycles": [],
    }
    visible, states = build_daily_objective_states(
        frame, review, monitor_on=_day(frame, 6), as_of=_day(frame, 9)
    )
    assert "SMALL_DOWN_ATTACK_CONFIRMED" in states[0]["events"]
    assert "SMALL_UP_ATTACK_CONFIRMED" in states[-1]["events"]
    built = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef", review_id="D-0123456789abcdef01234567",
        as_of=_day(frame, 9), visible_frame=visible, daily_state=states[-1],
        review_packet=review, atomic_schema_metadata=_schema(),
    )
    stops = [row for row in built["evidence"] if row["kind"] == "STOP_CANDIDATE"]
    assert len(stops) == 1
    assert stops[0]["values"]["side"] == "BULLISH"
    assert stops[0]["values"]["source_pivot_ref"].startswith("PIVOT:SMALL:LOW")


def test_recent_low_without_causal_attack_is_not_promoted_to_macro_defense():
    frame = _frame()
    packet = _review_packet(frame)
    as_of = _day(frame, 200)
    visible, states = build_daily_objective_states(
        frame, packet, monitor_on=_day(frame, 150), as_of=as_of
    )
    built = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef", review_id="D-0123456789abcdef01234567",
        as_of=as_of, visible_frame=visible, daily_state=states[-1], review_packet=packet,
        atomic_schema_metadata=_schema(),
    )
    assert built["question_manifest"]["stop_candidates"] == []
    assert built["objective_facts"]["large_bull_defense_intact"] is None
    assert built["objective_facts"]["stop_causal_fields_valid"] is False


def test_future_price_changes_do_not_change_asof_state_or_packet():
    frame = _frame()
    packet = _review_packet(frame)
    monitor, as_of = _day(frame, 150), _day(frame, 190)
    visible_a, states_a = build_daily_objective_states(frame, packet, monitor_on=monitor, as_of=as_of)
    changed = frame.copy()
    changed.loc[changed.index > 190, ["open", "high", "low", "close"]] *= 20
    visible_b, states_b = build_daily_objective_states(changed, packet, monitor_on=monitor, as_of=as_of)
    assert states_a == states_b
    built_a = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef", review_id="D-0123456789abcdef01234567", as_of=as_of,
        visible_frame=visible_a, daily_state=states_a[-1], review_packet=packet, atomic_schema_metadata=_schema(),
    )
    built_b = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef", review_id="D-0123456789abcdef01234567", as_of=as_of,
        visible_frame=visible_b, daily_state=states_b[-1], review_packet=packet, atomic_schema_metadata=_schema(),
    )
    assert canonical_sha256(built_a) == canonical_sha256(built_b)


def test_future_cycle_is_clipped_and_recomputed_from_visible_bars():
    frame = _frame()
    packet = _review_packet(frame)
    _, states = build_daily_objective_states(frame, packet, monitor_on=_day(frame, 150), as_of=_day(frame, 190))
    visible, _ = build_daily_objective_states(frame, packet, monitor_on=_day(frame, 150), as_of=_day(frame, 190))
    cycles = causal_cycles_as_of(packet, visible, _day(frame, 190))
    assert cycles[-1]["status"] == "FORMING"
    assert cycles[-1]["end"] is None
    assert cycles[-1]["low_date"] <= _day(frame, 190)
    assert cycles[-1]["high_date"] <= _day(frame, 190)
    assert states


def test_completed_macd_cycle_is_available_only_on_first_opposite_sign_bar():
    frame = _frame(8)
    review = {
        "macd_21_55_55_cycles": [
            {
                "sign": "POSITIVE", "start": _day(frame, 0), "end": _day(frame, 2),
                "status": "CONFIRMED", "bars": 3, "low_date": _day(frame, 0),
                "low": 49.2, "high_date": _day(frame, 2), "high": 51.0,
                "start_close": 50.0, "last_close": 50.5,
            },
            {
                "sign": "NEGATIVE", "start": _day(frame, 3), "end": None,
                "status": "FORMING", "bars": 5, "low_date": _day(frame, 3),
                "low": 49.0, "high_date": _day(frame, 7), "high": 52.0,
                "start_close": 50.4, "last_close": 51.0,
            },
        ]
    }
    enriched = add_causal_indicators(frame)

    before = causal_cycles_as_of(review, enriched.iloc[:3], _day(frame, 2))
    assert len(before) == 1
    assert before[0]["status"] == "FORMING"
    assert before[0]["end"] is None
    assert before[0]["confirmed_on"] is None

    at_confirmation = causal_cycles_as_of(review, enriched.iloc[:4], _day(frame, 3))
    assert [row["status"] for row in at_confirmation] == ["CONFIRMED", "FORMING"]
    assert at_confirmation[0]["end"] == _day(frame, 2)
    assert at_confirmation[0]["confirmed_on"] == _day(frame, 3)

    after = causal_cycles_as_of(review, enriched.iloc[:5], _day(frame, 4))
    assert after[0]["confirmed_on"] == _day(frame, 3)
    assert after[1]["status"] == "FORMING"


def test_ai_packet_is_bounded_anonymous_causal_and_has_three_hashes():
    frame = _frame()
    packet = _review_packet(frame)
    as_of = _day(frame, 200)
    visible, states = build_daily_objective_states(frame, packet, monitor_on=_day(frame, 150), as_of=as_of)
    built = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef", review_id="D-0123456789abcdef01234567", as_of=as_of,
        visible_frame=visible, daily_state=states[-1], review_packet=packet, atomic_schema_metadata=_schema(),
    )
    assert built["objective_facts"]["full_history_visible_bar_count"] == 201
    assert built["objective_facts"]["ai_visible_recent_bar_window_draft"] == AI_VISIBLE_RECENT_BAR_WINDOW_DRAFT
    visible_bars = [row for row in built["evidence"] if row["kind"] == "BAR"]
    assert len(visible_bars) < 201
    assert len(visible_bars) >= AI_VISIBLE_RECENT_BAR_WINDOW_DRAFT
    assert built["objective_facts"]["pivot_scale_disclosure"] == PIVOT_SCALE_DISCLOSURE
    assert {
        "question_manifest_sha256", "evidence_catalog_sha256", "input_packet_sha256",
    } <= set(built)
    assert set(built) == {
        "review_id", "anonymous_stock_id", "as_of", "input_packet_sha256",
        "question_manifest_sha256", "evidence_catalog_sha256", "question_manifest",
        "evidence", "objective_facts",
    }
    assert {"question_manifest", "evidence", "objective_facts"} <= set(built)
    assert not {
        "required_question_manifest", "evidence_catalog", "program_facts", "integrity", "fixed_candidates"
    } & set(built)
    assert built["objective_facts"]["full_history_input_sha256"] != built["input_packet_sha256"]
    assert built["objective_facts"]["ai_visible_evidence_sha256"] == built["evidence_catalog_sha256"]
    assert all(set(row) == {"ref", "kind", "date", "values"} for row in built["evidence"])
    assert all(
        str(value) <= as_of
        for row in built["evidence"]
        for value in [row["date"]]
    )
    serialized = json.dumps(built, ensure_ascii=False).lower()
    assert '"code"' not in serialized and '"name"' not in serialized and '"symbol"' not in serialized
    assert "future_selection" not in serialized
    assert_anonymous_and_causal(built, as_of=as_of)


def test_packet_exposes_one_causal_comparison_window_per_scale_with_endpoint_evidence():
    frame, review, monitor, as_of = _long_causal_fixture()
    visible, states = build_daily_objective_states(
        frame, review, monitor_on=monitor, as_of=as_of
    )
    built = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef",
        review_id="D-0123456789abcdef01234567",
        as_of=as_of,
        visible_frame=visible,
        daily_state=states[-1],
        review_packet=review,
    )
    windows = built["objective_facts"]["working_comparison_windows"]
    assert set(windows) == {"LARGE", "SMALL"}
    evidence = {row["ref"]: row for row in built["evidence"]}
    for scale, window in windows.items():
        assert window is not None
        assert window["scale"] == scale
        assert window["selection_policy"] == "LATEST_TWO_COMPLETED_SAME_DIRECTION_LEGACY_PIVOT_LEGS"
        assert window["available_on"] <= as_of
        for leg_name in ("baseline", "current"):
            leg = window[leg_name]
            assert leg["start_date"] <= leg["end_date"] <= as_of
            assert leg["bar_count"] >= 2
            assert leg["start_ref"] in evidence
            assert leg["end_ref"] in evidence
        matching = [
            row for row in built["evidence"]
            if row["kind"] == "CAUSAL_COMPARISON_WINDOW"
            and row["values"] == window
        ]
        assert len(matching) == 1
        assert matching[0]["date"] == window["available_on"]
    sufficiency = built["objective_facts"]["data_sufficiency_by_route"]
    assert sufficiency["FRESH_Q1_EXPANSION"]["status"] is True
    assert all(
        row["reason_code"] == "SUFFICIENT_ROUTE_HISTORY_AND_OBJECTIVE_EVIDENCE"
        for row in sufficiency.values() if row["status"] is True
    )


def test_remove_freezes_all_frontiers_until_explicit_upstream_reselection():
    dates = pd.bdate_range("2020-01-02", periods=800)
    close = [10.0] * len(dates)
    close[779:791] = [10, 10, 12, 7, 8, 8, 8, 10, 10, 10, 10, 12]
    frame = pd.DataFrame({
        "date": dates, "open": close, "high": [value + 0.4 for value in close],
        "low": [value - 0.4 for value in close], "close": close,
        "volume": [1000] * len(close),
    })
    rows = [
        ("LARGE", "HIGH", 760, 761, 11), ("LARGE", "LOW", 776, 777, 8),
        ("SMALL", "HIGH", 760, 761, 10.5), ("SMALL", "LOW", 776, 777, 8.5),
        ("SMALL", "HIGH", 783, 784, 9), ("SMALL", "LOW", 784, 785, 7.5),
        ("SMALL", "HIGH", 787, 788, 11), ("SMALL", "LOW", 788, 789, 8),
    ]
    review = {
        "selection_timeline": {_day(frame, 779): ["A"], _day(frame, 788): ["B"]},
        "confirmed_pivots": [
            {
                "scale": scale, "side": side, "source_date": _day(frame, source),
                "confirmation_date": _day(frame, confirmation), "price": price,
            }
            for scale, side, source, confirmation, price in rows
        ],
        "macd_21_55_55_cycles": [],
    }
    visible, states = build_daily_objective_states(
        frame, review, monitor_on=_day(frame, 779), as_of=_day(frame, 792)
    )
    by_day = {row["as_of"]: row for row in states}

    removed = by_day[_day(frame, 782)]
    assert removed["remove_frontier"] is True
    assert removed["watchlist_state"] == "REMOVE_BOUNDARY"
    inactive_signal = by_day[_day(frame, 786)]
    assert "SMALL_UP_ATTACK_CONFIRMED" in inactive_signal["events"]
    assert inactive_signal["active_watchlist"] is False
    assert inactive_signal["program_trade_frontier"] is False
    assert inactive_signal["review_required"] is False
    assert inactive_signal["skip_reason"] == "WATCHLIST_INACTIVE_AWAIT_RESELECTION"

    reselected = by_day[_day(frame, 788)]
    assert "RESELECTION_AFTER_REMOVAL" in reselected["events"]
    assert reselected["active_watchlist"] is True
    assert reselected["campaign_generation"] == 2
    assert reselected["program_trade_frontier"] is False
    after = by_day[_day(frame, 790)]
    assert after["program_trade_frontier"] is True
    assert after["review_required"] is True

    packets, _ = build_stock_packets(
        price_frame=frame,
        review_packet=review,
        anonymous_stock_id="S-0123456789abcdef",
        identity_salt=bytes.fromhex("01" * 32),
        private_code="PRIVATE",
        monitor_on=_day(frame, 779),
        as_of=_day(frame, 792),
        atomic_schema_metadata=_schema(),
    )
    packet_by_day = {row["as_of"]: row for row in packets}
    assert _day(frame, 786) not in packet_by_day
    assert packet_by_day[_day(frame, 782)]["objective_facts"]["parent_campaign_invalidated"] is True
    assert packet_by_day[_day(frame, 790)]["objective_facts"]["parent_campaign_invalidated"] is False
    assert all(row["objective_facts"]["active_watchlist"] is True for row in packets)


def test_required_questions_expand_from_schema_metadata_per_candidate():
    candidates = {
        "anchor_candidates": [{"candidate_id": "ANCHOR_CANDIDATE:A-1"}, {"candidate_id": "ANCHOR_CANDIDATE:A-2"}],
        "relation_candidates": [{"candidate_id": "RELATION_CANDIDATE:R-1"}],
        "stop_candidates": [{"candidate_id": "STOP_CANDIDATE:S-1"}],
    }
    manifest = build_required_question_manifest(_schema(), candidates)
    assert [row["subject_ref"] for row in manifest["anchor_candidates"]] == [
        "ANCHOR_CANDIDATE:A-1", "ANCHOR_CANDIDATE:A-2"
    ]
    assert manifest["anchor_candidates"][0]["required_question_ids"] == ["ANCHOR_A", "ANCHOR_B"]
    assert manifest["relation_candidates"][0]["subject_ref"] == "RELATION_CANDIDATE:R-1"
    assert manifest["stop_candidates"][0]["subject_ref"] == "STOP_CANDIDATE:S-1"
    with pytest.raises(AtomicPacketError, match="x-question-groups"):
        build_required_question_manifest({"x-question-groups": {"anchor": ["A"]}}, candidates)


def test_current_context_relation_exists_when_no_natural_pair_and_program_contract_complete():
    frame = _frame()
    packet = _review_packet(frame)
    packet["confirmed_pivots"] = []
    packet["macd_21_55_55_cycles"] = []
    as_of = _day(frame, 100)
    visible, states = build_daily_objective_states(frame, packet, monitor_on=_day(frame, 90), as_of=as_of)
    built = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef", review_id="D-0123456789abcdef01234567", as_of=as_of,
        visible_frame=visible, daily_state=states[-1], review_packet=packet, atomic_schema_metadata=_schema(),
    )
    relations = [row["subject_ref"] for row in built["question_manifest"]["relation_candidates"]]
    assert relations == []
    required_facts = {
        "active_watchlist", "trigger_completed", "risk_executable",
        "data_sufficiency_by_route", "working_comparison_windows",
        "macro_defense_alert", "parent_campaign_invalidated", "large_dow_state", "small_dow_state",
        "bear_reversal_context", "large_bear_defense_broken", "small_bull_control",
        "first_retest_after_large_break_held", "rr_break_completed", "direct_same_clean_impulse",
        "taiji_generation", "same_direction_attack_number", "completed_prior_copy_count",
        "signal_event_ref", "position_role", "scenario_hypotheses",
        "large_bull_defense_intact", "correction_bear_dow_line_causal", "small_up_control_break",
        "stop_causal_fields_valid", "large_bear_dow_defense_causal", "phase_stop_causal",
        "macd_is_support_only", "fresh_anchor_stop_causal",
        "builder_version", "builder_status", "pivot_scale_disclosure", "objective_engine_source",
        "full_history_visible_bar_count", "full_history_input_sha256",
        "causal_cutoff_as_of",
        "ai_visible_recent_bar_window_draft", "ai_visible_bar_count", "ai_visible_evidence_sha256",
        "candidate_enumeration_policy", "performance_used_for_ordering_or_truncation",
        "scenario_hypothesis_counts", "semantic_dirty_since_prior_review",
    }
    assert set(built["objective_facts"]) == required_facts
    assert set(built["objective_facts"]["scenario_hypotheses"]) == {
        "MATURE_TREND_PULLBACK", "MACRO_COPY_RESONANCE",
        "BEAR_REVERSAL_LEFT_RIGHT", "FRESH_Q1_EXPANSION",
    }
    assert all(
        hypothesis["relation_ref"] == "RELATION_CANDIDATE:CURRENT_CONTEXT"
        for rows in built["objective_facts"]["scenario_hypotheses"].values()
        for hypothesis in rows
    )


def test_build_stock_packets_is_anonymous_deterministic_and_not_tied_to_legacy_dates():
    frame = _frame()
    packet = _review_packet(frame)
    kwargs = {
        "price_frame": frame,
        "review_packet": packet,
        "anonymous_stock_id": "S-0123456789abcdef",
        "identity_salt": bytes.fromhex("01" * 32),
        "private_code": "SECRET-CODE",
        "monitor_on": _day(frame, 150),
        "as_of": _day(frame, 180),
        "atomic_schema_metadata": _schema(),
        "limit_review_points": 3,
    }
    rows_a, summary_a = build_stock_packets(**kwargs)
    rows_b, summary_b = build_stock_packets(**kwargs)
    assert canonical_sha256(rows_a) == canonical_sha256(rows_b)
    assert summary_a == summary_b
    assert summary_a["stock_days_scanned"] == 31
    assert summary_a["review_point_policy"] == FORMAL_REVIEW_POINT_POLICY
    assert summary_a["as_of_ceiling"] == _day(frame, 180)
    assert set(summary_a["scenario_hypothesis_counts"]) == {
        "MATURE_TREND_PULLBACK", "MACRO_COPY_RESONANCE",
        "BEAR_REVERSAL_LEFT_RIGHT", "FRESH_Q1_EXPANSION",
    }
    assert "SECRET-CODE" not in json.dumps(rows_a)

    legacy_rows, legacy_summary = build_stock_packets(**{**kwargs, "legacy_dates": set()})
    assert legacy_rows == []
    assert legacy_summary["review_point_policy"] == "LEGACY_V1_BOUNDARY_DATES_BEHAVIOR_COMPARISON_ONLY"


def test_integrity_and_future_guards_fail_closed():
    frame = _frame()
    packet = _review_packet(frame)
    as_of = _day(frame, 180)
    visible, states = build_daily_objective_states(frame, packet, monitor_on=_day(frame, 150), as_of=as_of)
    built = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef", review_id="D-0123456789abcdef01234567", as_of=as_of,
        visible_frame=visible, daily_state=states[-1], review_packet=packet, atomic_schema_metadata=_schema(),
    )
    tampered = copy.deepcopy(built)
    tampered["objective_facts"]["risk_executable"] = not tampered["objective_facts"]["risk_executable"]
    with pytest.raises(AtomicPacketError, match="integrity"):
        assert_anonymous_and_causal(tampered, as_of=as_of)
    future = copy.deepcopy(built)
    future["evidence"].append(
        {"ref": "SELECTION:2099-01-01", "kind": "SELECTION", "date": "2099-01-01", "values": {"sources": []}}
    )
    with pytest.raises(AtomicPacketError, match="future date"):
        assert_anonymous_and_causal(future, as_of=as_of)

    future_bar = copy.deepcopy(built)
    future_bar["evidence"].append(
        {
            "ref": "BAR:2099-01-04", "kind": "BAR", "date": "2099-01-04",
            "values": {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "return_1d_pct": 50.0},
        }
    )
    with pytest.raises(AtomicPacketError, match="future date"):
        assert_anonymous_and_causal(future_bar, as_of=as_of)

    outcome = copy.deepcopy(built)
    outcome["evidence"][0]["values"]["forward_return"] = 12.5
    with pytest.raises(AtomicPacketError, match="forbidden identity/performance key"):
        assert_anonymous_and_causal(outcome, as_of=as_of)


def test_lossless_lazy_eligibility_skips_non_permission_pivots_but_keeps_trade_frontier():
    frame, review, monitor, as_of = _long_causal_fixture()
    _, states = build_daily_objective_states(frame, review, monitor_on=monitor, as_of=as_of)
    pivot_only = next(row for row in states if row["events"] == ["PIVOT_CONFIRMED", "OBJECTIVE_STATE_CHANGE"])
    assert pivot_only["review_required_before_lazy"] is True
    assert pivot_only["review_required"] is False
    assert pivot_only["skip_reason"] == "OBJECTIVE_CHANGE_CANNOT_CHANGE_PERMISSION"
    signal = states[-1]
    assert signal["program_trade_frontier"] is True
    assert signal["review_required"] is True
    assert "PROGRAM_TRADE_FRONTIER" in signal["lazy_review_reasons"]


def test_generation_is_computed_before_visible_candidate_truncation_and_relations_never_mix_scales():
    dates = pd.bdate_range("2022-01-03", periods=20)
    pivots = []
    for index in range(9):
        side = "LOW" if index % 2 == 0 else "HIGH"
        pivots.append({
            "ref": f"PIVOT:SMALL:{side}:{dates[index].date()}:{dates[index + 1].date()}",
            "source_date": dates[index].date().isoformat(),
            "confirmation_date": dates[index + 1].date().isoformat(),
            "scale": "SMALL", "side": side, "price": 10 + index,
        })
    objective = {"defenses": [], "attacks": []}
    anchors = _anchor_candidates(objective, pivots, [])
    assert len(anchors) == 4
    latest_up = [row for row in anchors if row["direction_hint"] == "UP"][-1]
    assert latest_up["same_direction_attack_number"] == 4
    assert latest_up["taiji_generation"] == "LATER_GENERATION"
    assert latest_up["completed_prior_copy_count"] == 2
    relations = _relation_candidates(anchors, objective, dates[-1].date().isoformat())
    completed = [row for row in relations if row["relation_state"] == "COMPLETED_ALTERNATING_TRIPLE"]
    assert completed
    assert all(
        row["parent_scale"] == row["correction_scale"] == row["current_scale"] == "SMALL"
        for row in completed
    )
    assert all(
        row["parent_available_on"] <= row["correction_available_on"] <= row["current_available_on"]
        for row in completed
    )


def test_real_policy_end_to_end_wait_on_unknown_semantics_from_builder_packet():
    frame, review, monitor, as_of = _long_causal_fixture()
    visible, states = build_daily_objective_states(frame, review, monitor_on=monitor, as_of=as_of)
    packet = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef", review_id="D-0123456789abcdef01234567",
        as_of=as_of, visible_frame=visible, daily_state=states[-1], review_packet=review,
    )
    semantic = _policy_semantic(packet, "UNKNOWN")
    assert packet["question_manifest"] == expected_question_manifest(packet)
    assert validate_atomic(packet, semantic) == []
    decision = reduce_atomic_v3(packet, semantic)
    assert decision["permission"] == "WAIT"


def test_real_policy_end_to_end_reachable_fresh_route_from_builder_packet():
    frame, review, monitor, as_of = _long_causal_fixture()
    visible, states = build_daily_objective_states(frame, review, monitor_on=monitor, as_of=as_of)
    packet = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef", review_id="D-0123456789abcdef01234567",
        as_of=as_of, visible_frame=visible, daily_state=states[-1], review_packet=review,
    )
    semantic = _policy_semantic(packet, "PASS")
    relation_rows = semantic["candidate_answers"]["relation_candidates"]
    for row in relation_rows:
        for question in (
            "REL_CURRENT_LEG_IS_REPLICATION", "REL_CURRENT_LEG_IS_FRESH_ANCHOR",
            "LONG_CAMPAIGN_REPEATED_SUCCESS",
        ):
            if question in row["answers"]:
                row["answers"][question] = _atomic_verdict("FAIL", f"BAR:{as_of}")
        for question in (
            "EXH_ATTACK_SHORTENING", "EXH_SLOPE_DECAY", "EXH_PRICE_VOLUME_DIVERGENCE",
            "EXH_FAILED_CONTINUATION", "EXH_TIME_SPACE_EXHAUSTION", "BEAR_ATTACK_IS_LATE_STAGE",
            "BEAR_LATE_STAGE_PARTIAL",
        ):
            if question in row["answers"]:
                row["answers"][question] = _atomic_verdict("FAIL", f"BAR:{as_of}")
    fresh_relation = next(
        hypothesis["relation_ref"]
        for hypothesis in packet["objective_facts"]["scenario_hypotheses"]["FRESH_Q1_EXPANSION"]
        if hypothesis["taiji_generation"] == "ANCHOR_LEG_1"
    )
    selected_row = next(row for row in relation_rows if row["subject_ref"] == fresh_relation)
    selected_row["answers"]["REL_CURRENT_LEG_IS_FRESH_ANCHOR"] = _atomic_verdict(
        "PASS", f"BAR:{as_of}"
    )
    assert validate_atomic(packet, semantic) == []
    decision = reduce_atomic_v3(packet, semantic)
    assert decision["permission"] == "TRADE"
    assert decision["route"] == "V2_CORE"
    assert decision["scenario"] == "FRESH_Q1_EXPANSION"


def test_real_policy_end_to_end_bear_probe_can_use_causal_right_side_low_for_both_stops():
    frame, review, monitor, as_of = _bear_causal_fixture()
    visible, states = build_daily_objective_states(frame, review, monitor_on=monitor, as_of=as_of)
    packet = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef", review_id="D-0123456789abcdef01234567",
        as_of=as_of, visible_frame=visible, daily_state=states[-1], review_packet=review,
    )
    objective = packet["objective_facts"]
    assert objective["large_dow_state"] == "BEAR"
    assert objective["large_bear_defense_broken"] is True
    assert objective["rr_break_completed"] is True
    bear_rows = objective["scenario_hypotheses"]["BEAR_REVERSAL_LEFT_RIGHT"]
    selected = next(row for row in bear_rows if row["taiji_generation"] == "COPY_LEG_3")
    assert selected["episode_stop_ref"] == selected["campaign_stop_ref"]

    semantic = _policy_semantic(packet, "PASS")
    for row in semantic["candidate_answers"]["relation_candidates"]:
        for question in (
            "REL_CURRENT_LEG_IS_REPLICATION", "REL_CURRENT_LEG_IS_FRESH_ANCHOR",
            "LONG_CAMPAIGN_REPEATED_SUCCESS", "BEAR_ATTACK_IS_LATE_STAGE",
            "BEAR_LATE_STAGE_PARTIAL", "EXH_ATTACK_SHORTENING", "EXH_SLOPE_DECAY",
            "EXH_PRICE_VOLUME_DIVERGENCE", "EXH_FAILED_CONTINUATION", "EXH_TIME_SPACE_EXHAUSTION",
        ):
            if question in row["answers"]:
                row["answers"][question] = _atomic_verdict("FAIL", f"BAR:{as_of}")
    selected_answer = next(
        row for row in semantic["candidate_answers"]["relation_candidates"]
        if row["subject_ref"] == selected["relation_ref"]
    )
    selected_answer["answers"]["BEAR_ATTACK_IS_LATE_STAGE"] = _atomic_verdict(
        "UNKNOWN", f"BAR:{as_of}"
    )
    selected_answer["answers"]["BEAR_LATE_STAGE_PARTIAL"] = _atomic_verdict("PASS", f"BAR:{as_of}")
    selected_answer["answers"]["EXH_ATTACK_SHORTENING"] = _atomic_verdict("PASS", f"BAR:{as_of}")
    assert validate_atomic(packet, semantic) == []
    decision = reduce_atomic_v3(packet, semantic)
    assert decision["permission"] == "TRADE"
    assert decision["route"] == "BEAR_REVERSAL_PROBE"
    assert decision["scenario"] == "BEAR_REVERSAL_LEFT_RIGHT"


def _formal_source_fixture(tmp_path: Path, stock_count: int = 6):
    private = tmp_path / "private"
    prices = private / "prices"
    reviews = private / "reviews"
    prices.mkdir(parents=True)
    reviews.mkdir(parents=True)
    frame, review, monitor_on, as_of = _long_causal_fixture()
    input_items = []
    packet_items = []
    identities = []
    monitored_sessions = int(
        ((pd.to_datetime(frame["date"]) >= pd.Timestamp(monitor_on))
         & (pd.to_datetime(frame["date"]) <= pd.Timestamp(as_of))).sum()
    )
    for index in range(stock_count):
        code = f"T{index:03d}"
        anonymous_id = f"S-{index:016x}"
        price_path = prices / f"source-{index:03d}.csv"
        review_path = reviews / f"source-{index:03d}.json"
        frame.to_csv(price_path, index=False)
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        input_items.append(
            {
                "code": code, "monitor_on": monitor_on, "price_path": str(price_path),
                "price_sha256": file_sha256(price_path),
            }
        )
        packet_items.append(
            {
                "code": code, "monitor_on": monitor_on, "packet": str(review_path),
                "packet_sha256": file_sha256(review_path), "monitored_sessions": monitored_sessions,
            }
        )
        identities.append({"index": index, "code": code, "anonymous_id": anonymous_id})
    input_manifest = private / "input_manifest.json"
    packet_manifest = private / "packet_manifest.json"
    identity_map = private / "sealed_identity_map.json"
    input_manifest.write_text(
        json.dumps({"prepared_stock_count": stock_count, "as_of": as_of, "items": input_items}),
        encoding="utf-8",
    )
    packet_manifest.write_text(
        json.dumps({"as_of": as_of, "items": packet_items}), encoding="utf-8",
    )
    identity_map.write_text(
        json.dumps({"salt_hex": "12" * 32, "mapping": identities}), encoding="utf-8",
    )
    freeze_source_manifest = private / "freeze_source_manifest.json"
    freeze_source_manifest.write_text(
        json.dumps({"stocks": stock_count, "stock_days": monitored_sessions * stock_count}),
        encoding="utf-8",
    )
    return {
        "input_manifest_path": input_manifest,
        "packet_manifest_path": packet_manifest,
        "identity_map_path": identity_map,
        "atomic_schema_path": DEFAULT_ATOMIC_SCHEMA,
        "freeze_source_manifest_path": freeze_source_manifest,
        "work_dir": tmp_path / "public-work",
        "formal_output_dir": tmp_path / "ai-source",
        "expected_stock_count": stock_count,
        "expected_stock_days": monitored_sessions * stock_count,
    }


def test_formal_three_shard_build_resume_merge_exact_order_and_frozen_hashes(tmp_path, monkeypatch):
    kwargs = _formal_source_fixture(tmp_path)
    plan = prepare_formal_build_plan(**kwargs)
    assert plan["identity_isolated"] is True
    assert len(plan["public_catalog"]) == 6
    assert "private_code" not in json.dumps(plan)
    assert plan["builder_version"] == "hybrid-v3-atomic-packets-v2"
    assert plan["builder_status"] == "FINAL"
    assert plan["pivot_scale_disclosure"] == "LEGACY_RADIUS_3_10_NOT_COURSE_L1_L2"
    assert plan["course_l1_l2_used"] is False
    assert plan["macd_parameters"] == [21, 55, 55]
    assert plan["preferred_structure_bars"] == 750
    assert plan["indicator_warmup_bars"] == 200
    assert plan["builder_code_sha256"] == file_sha256(
        Path(__file__).parents[1] / "scripts/hybrid_v3_atomic_packets_v2.py"
    )
    assert plan["atomic_schema_sha256"] == file_sha256(DEFAULT_ATOMIC_SCHEMA)

    shard_results = [build_formal_shard(**kwargs, shard_id=shard_id) for shard_id in range(3)]
    assert sum(row["covered_stocks"] for row in shard_results) == 6
    assert all(row["resumed_stocks"] == 0 for row in shard_results)
    resumed = build_formal_shard(**kwargs, shard_id=1)
    assert resumed["resumed_stocks"] == resumed["covered_stocks"] == 2

    manifest = merge_formal_build_shards(**kwargs)
    source_path = kwargs["formal_output_dir"] / FORMAL_SOURCE_FILE
    manifest_path = kwargs["formal_output_dir"] / FORMAL_SOURCE_MANIFEST_FILE
    records = [json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines()]
    assert manifest["expected_v2_review_points"] == len(records) > 0
    assert manifest["manifest_version"] == "hybrid-v3-review-points-v2"
    assert manifest["status"] == "LOCKED_OUTCOME_BLIND"
    assert manifest["source_order_locked"] is True
    assert manifest["outcome_blind"] is True
    assert manifest["identity_visible"] is False
    assert manifest["performance_visible"] is False
    assert manifest["future_data_visible"] is False
    assert manifest["pivot_scale_disclosure"] == "LEGACY_RADIUS_3_10_NOT_COURSE_L1_L2"
    assert manifest["course_l1_l2_used"] is False
    assert manifest["macd_parameters"] == [21, 55, 55]
    assert manifest["outcome_excluded_codes"] == []
    assert manifest["universe_scope"] == "FULL_V2_REVIEW_POINT_UNIVERSE"
    assert manifest["sampling_strata_basis"] == "OBJECTIVE_ONLY_AS_OF"
    assert sum(manifest["sampling_stratum_counts"].values()) == len(records)
    assert manifest["stocks"] == 6
    assert manifest["stock_days_scanned"] == kwargs["expected_stock_days"]
    assert Path(manifest["source_manifest"]["path"]) == kwargs["freeze_source_manifest_path"].resolve()
    assert manifest["source_manifest"]["sha256"] == file_sha256(kwargs["freeze_source_manifest_path"])
    assert Path(manifest["review_points_artifact"]["path"]) == source_path.resolve()
    assert manifest["review_points_artifact"]["rows"] == len(records)
    assert manifest["source_ordinal_coverage"] == {
        "expected": len(records), "covered": len(records), "missing": 0,
        "overlap": 0, "unexpected": 0, "first": 0, "last": len(records) - 1,
    }
    assert file_sha256(source_path) == manifest["review_points_artifact"]["sha256"]
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        key: value for key, value in manifest.items() if key != "resumed"
    }
    anonymous_order = [row["anonymous_stock_id"] for row in records]
    assert anonymous_order == sorted(anonymous_order)
    assert [row["source_ordinal"] for row in records] == list(range(len(records)))
    assert all(row["packet_sha256"] == canonical_sha256(row["packet"]) for row in records)
    serialized = source_path.read_text(encoding="utf-8")
    assert all(f"T{index:03d}" not in serialized for index in range(6))
    assert all(set(row) == {
        "source_ordinal", "review_id", "anonymous_stock_id", "sampling_stratum",
        "packet_sha256", "packet",
    } for row in records)
    assert all(set(row["packet"]) == {
        "review_id", "anonymous_stock_id", "as_of", "input_packet_sha256",
        "question_manifest_sha256", "evidence_catalog_sha256", "question_manifest",
        "evidence", "objective_facts",
    } for row in records)
    assert merge_formal_build_shards(**kwargs)["resumed"] is True

    monkeypatch.setattr(freeze_v2, "EXPECTED_STOCKS", 6)
    monkeypatch.setattr(freeze_v2, "EXPECTED_STOCK_DAYS", kwargs["expected_stock_days"])
    validated_source, validated_points = _validate_source_and_review_points(
        source_path=kwargs["freeze_source_manifest_path"],
        review_manifest_path=manifest_path,
        expected_review_points=len(records),
    )
    assert validated_source["stocks"] == 6
    assert validated_points["status"] == "LOCKED_OUTCOME_BLIND"

    locked_dir = tmp_path / "locked-consumer-fixture"
    locked_dir.mkdir()
    locked_source = locked_dir / FORMAL_SOURCE_FILE
    locked_source.write_bytes(source_path.read_bytes())
    locked_manifest = {
        **{key: value for key, value in manifest.items() if key != "resumed"},
        "builder_status": "FINAL",
        "status": "LOCKED_OUTCOME_BLIND",
        "source_order_locked": True,
        "review_points_artifact": {
            "path": str(locked_source.resolve()),
            "sha256": file_sha256(locked_source),
            "rows": len(records),
        },
    }
    locked_manifest_path = locked_dir / FORMAL_SOURCE_MANIFEST_FILE
    locked_manifest_path.write_text(json.dumps(locked_manifest), encoding="utf-8")
    validated_source, validated_points = _validate_source_and_review_points(
        source_path=kwargs["freeze_source_manifest_path"],
        review_manifest_path=locked_manifest_path,
        expected_review_points=len(records),
    )
    assert validated_source["stocks"] == 6
    assert validated_points["status"] == "LOCKED_OUTCOME_BLIND"

    monkeypatch.setattr(course_gold_v2, "EXPECTED_STOCKS", 6)
    monkeypatch.setattr(course_gold_v2, "EXPECTED_STOCK_DAYS", kwargs["expected_stock_days"])
    course_gold_v2.validate_formal_universe_manifest(
        locked_manifest, manifest_path=locked_manifest_path
    )


def test_review_manifest_state_maps_only_supported_component_statuses():
    assert _review_manifest_state("DRAFT_NOT_FINAL") == ("BUILT_OUTCOME_BLIND_DRAFT", False)
    assert _review_manifest_state("FINAL") == ("LOCKED_OUTCOME_BLIND", True)
    with pytest.raises(AtomicPacketError, match="unsupported builder status"):
        _review_manifest_state("EXPERIMENTAL")


def test_formal_plan_rejects_builder_status_or_schema_drift_before_resume(tmp_path):
    kwargs = _formal_source_fixture(tmp_path, stock_count=3)
    prepare_formal_build_plan(**kwargs)
    plan_path = kwargs["work_dir"] / "build_plan.manifest.json"
    original = json.loads(plan_path.read_text(encoding="utf-8"))
    changed_status = {**original, "builder_status": "DRAFT_NOT_FINAL"}
    plan_path.write_text(json.dumps(changed_status), encoding="utf-8")
    with pytest.raises(AtomicPacketError, match="builder_status"):
        build_formal_shard(**kwargs, shard_id=0)

    plan_path.write_text(json.dumps(original), encoding="utf-8")
    copied_schema = tmp_path / "private" / "schema.json"
    copied_schema.write_bytes(DEFAULT_ATOMIC_SCHEMA.read_bytes())
    schema_kwargs = {**kwargs, "atomic_schema_path": copied_schema}
    # The plan pins the original schema path's content hash. A changed copy may
    # not resume even when all market inputs are otherwise identical.
    schema = json.loads(copied_schema.read_text(encoding="utf-8"))
    schema["title"] = "tampered after plan"
    copied_schema.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(AtomicPacketError, match="atomic_schema_sha256"):
        build_formal_shard(**schema_kwargs, shard_id=0)


def test_formal_merge_rejects_fragment_tamper_and_identity_map_must_be_isolated(tmp_path):
    kwargs = _formal_source_fixture(tmp_path, stock_count=3)
    prepare_formal_build_plan(**kwargs)
    for shard_id in range(3):
        build_formal_shard(**kwargs, shard_id=shard_id)
    fragment = next((kwargs["work_dir"] / "shard_00" / "stocks").glob("*.jsonl"))
    fragment.write_bytes(fragment.read_bytes() + b"{}\n")
    with pytest.raises(AtomicPacketError, match="fragment hash mismatch"):
        merge_formal_build_shards(**kwargs)

    inside = dict(kwargs)
    inside["identity_map_path"] = kwargs["work_dir"] / "sealed_identity_map.json"
    inside["identity_map_path"].write_text(
        kwargs["identity_map_path"].read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(AtomicPacketError, match="identity map must be outside"):
        prepare_formal_build_plan(**inside)
