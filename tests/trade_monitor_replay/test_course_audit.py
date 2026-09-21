from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trade_monitor_replay import anchor_lifecycle as anchors
from trade_monitor_replay.deterministic_state import _window_quadrant_metrics
from trade_monitor_replay.execution_gate import schedule_pending_entry, fill_pending_entry
from trade_monitor_replay.presentation import _v3_quadrant_line, _v3_structure_summary
from trade_monitor_replay.runner import _validate_v3_trade_levels, ReplayRunError
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _opening_observation_memory,
    _preserve_active_session_anchor_probability,
    _reject_corrupt_text_fragments,
)


FIXTURE = Path(__file__).parent / "fixtures" / "tmf-20260825-0845-0922.json"


@pytest.mark.parametrize("mirror", [False, True])
def test_origin_breach_terminates_anchor_and_later_attack_gets_new_origin(mirror):
    at = datetime.fromisoformat("2026-08-25T01:00:00+08:00")
    stamp = lambda minute: (at + timedelta(minutes=minute)).isoformat()
    price = lambda value: 1000-value if mirror else value
    kinds = ["LOW", "HIGH", "LOW", "HIGH", "LOW", "HIGH"]
    if mirror:
        kinds = ["HIGH" if k == "LOW" else "LOW" for k in kinds]
    points = [dict(kind=k, time=stamp(t), price=price(p), confirmed=c is not None,
                   confirmation_time=stamp(c) if c is not None else None)
              for k,t,p,c in zip(kinds, [0,2,4,6,8,11], [100,200,130,220,90,240], [1,3,5,7,10,None])]
    bars = [dict(time=stamp(t), close=price(p)) for t,p in [(5,180),(6,210),(7,200),(8,95),(9,110),(10,150),(11,230)]]
    def derive(cutoff):
        history = []
        visible_points = copy.deepcopy([p for p in points if p["time"] <= stamp(cutoff)])
        for p in visible_points:
            if p["confirmation_time"] and p["confirmation_time"] > stamp(cutoff):
                p.update(confirmed=False, confirmation_time=None)
        active = anchors._derive_directional_anchor(visible_points,
            bars=[b for b in bars if b["time"] <= stamp(cutoff)], session_key="origin-test",
            level="LARGE", require_closed_confirmation=True, history_out=history)
        return active, history
    original, _ = derive(7)
    assert original["origin_time"] == stamp(0)
    for cutoff in (8,9,10):
        active, history = derive(cutoff)
        assert active is None
        assert history[-1]["status"] == "INVALIDATED"
        assert history[-1]["ended_at"] == stamp(8)
    active, history = derive(11)
    assert active["id"] != original["id"]
    assert active["origin_time"] == stamp(8)
    assert active["first_seen_at"] == stamp(11)
    assert active["defense"] is None  # destructive impulse != a completed Dow sequence
    frozen = anchors._historical_anchor(history[-1], bars)
    assert frozen["status"] == "INVALIDATED"
    assert frozen["defense"]["state"] == "BROKEN"
    assert frozen["defense"].get("reclaimed_at") is None
    events = anchors._lifecycle_events(background=active, child=None, candidate=None,
        history=[frozen], session_key="origin-test")
    assert any(e["event_type"] == "BACKGROUND_ANCHOR_INVALIDATED" and e["event_time"] == stamp(8) for e in events)


@pytest.mark.parametrize("direction", ["BULL", "BEAR"])
def test_origin_breach_requires_close_not_wick_or_equal(direction):
    anchor = dict(first_seen_at="2026-08-25T09:00:00+08:00", origin_price=100, direction=direction)
    bars = [dict(time="2026-08-25T09:01:00+08:00", close=100, high=110, low=90)]
    assert anchors._origin_breach(anchor, bars) is None
    bars.append(dict(time="2026-08-25T09:02:00+08:00", close=99 if direction == "BULL" else 101))
    assert anchors._origin_breach(anchor, bars) == bars[-1]["time"]
    assert anchors._origin_breach(anchor, bars, through=bars[0]["time"]) is None


def test_opening_narrative_plans_are_not_carried_as_active_setups():
    analysis = {"message_type": "OBSERVATION", "course_reading": {"setup_stage": "FORMING"},
                "action": {"position_action": "NONE", "setup_key": None, "stop_price": None},
                "scenario": {"bull_plan": "wait", "bear_plan": "wait"}}
    memory = {"active_setups": [{"stage": "FORMING", "trigger_level": None, "trigger_operator": "NONE"}]}
    original = copy.deepcopy((analysis, memory))
    actual = _opening_observation_memory(analysis, memory, ledger={"monitoring_session": {"bar_count": 2}})
    assert actual["active_setups"] == []
    assert (analysis, memory) == original
    assert _opening_observation_memory(analysis, memory, ledger={"monitoring_session": {"bar_count": 5}}) == memory


def test_previous_observed_undefined_state_is_not_a_new_empty_seed():
    from trade_monitor_replay.semantic_contract import _validate_v3_state_transition
    control = dict(active_large_anchor_ref=None, active_small_anchor_ref=None,
        working_anchor_ref=None, controlling_grade="UNDEFINED", background_quadrant="UNDEFINED",
        working_quadrant="UNDEFINED", last_structure_event_ref=None,
        background_quadrant_changed_at="2026-08-25T08:46:00+08:00",
        working_quadrant_changed_at="2026-08-25T08:46:00+08:00")
    before = dict(version=3, as_of="2026-08-25T08:46:00+08:00", session_key="DAY",
                  active_setups=[], structure_control=control)
    current = {**before, "as_of": "2026-08-25T08:48:00+08:00"}
    reading = dict(large_anchor_ref=None, small_anchor_ref=None, working_anchor_ref=None,
        controlling_grade="UNDEFINED", background_quadrant="UNDEFINED", working_quadrant="UNDEFINED",
        structure_event_ref=None)
    _validate_v3_state_transition({"course_reading": reading}, current, ledger={},
        previous_memory=before, evidence_events=[], expected_as_of=current["as_of"])


@pytest.mark.parametrize("mutation", ["armed", "numeric_trigger", "preparation", "enter", "stop"])
def test_opening_normalization_never_hides_an_executable_order(mutation):
    analysis = {"message_type": "OBSERVATION", "course_reading": {"setup_stage": "FORMING"},
                "action": {"position_action": "NONE", "setup_key": None, "stop_price": None}}
    memory = {"active_setups": [{"stage": "FORMING", "trigger_level": None, "trigger_operator": "NONE"}]}
    if mutation == "armed": memory["active_setups"][0]["stage"] = "ARMED"
    if mutation == "numeric_trigger": memory["active_setups"][0]["trigger_level"] = 100
    if mutation == "preparation": analysis["message_type"] = "PREPARATION"
    if mutation == "enter": analysis["action"]["position_action"] = "ENTER"
    if mutation == "stop": analysis["action"]["stop_price"] = 90
    actual = _opening_observation_memory(analysis, memory, ledger={"monitoring_session": {"bar_count": 2}})
    assert actual["active_setups"] == memory["active_setups"]
    with pytest.raises(Exception, match="OR5"):
        from trade_monitor_replay.semantic_contract import _validate_preparation_contract
        _validate_preparation_contract(analysis, actual, ledger={"monitoring_session": {"bar_count": 2}})


def bars_until(clock: str) -> list[dict]:
    return [b for b in json.loads(FIXTURE.read_text(encoding="utf-8"))
            if b["time"] <= f"2026-08-25T{clock}:00+08:00"]


def snapshot(bars: list[dict]) -> dict:
    return anchors.build_anchor_lifecycle(bars, expected_as_of=bars[-1]["time"],
        session_key="audit", session_start="2026-08-25T08:45:00+08:00", course_chain_enabled=True)


@pytest.mark.parametrize("clock", ["08:46", "08:49", "08:54"])
def test_opening_does_not_invent_large_dow_or_reverse_direction(clock):
    state = snapshot(bars_until(clock))
    assert state["background_anchor"] is None
    assert state["reverse_candidate"] is None
    if state["child_anchor"]:
        assert state["child_anchor"]["defense"] is None


def test_opening_extreme_extends_without_rewriting_initial_anchor():
    opening = snapshot(bars_until("08:49"))["child_anchor"]
    continued = snapshot(bars_until("09:02"))["child_anchor"]
    assert opening["id"] == continued["id"]
    assert continued["first_extreme_price"] == 44372
    assert continued["latest_extreme_price"] < 44372
    assert continued["latest_extreme_time"] >= "2026-08-25T09:00"
    assert continued["first_seen_at"] == opening["first_seen_at"]


def test_parent_impulse_can_exist_before_complete_large_dow():
    state = snapshot(bars_until("09:20"))
    parent = state["background_anchor"]
    assert parent["origin_time"].endswith("08:45:00+08:00")
    assert parent["latest_extreme_time"].endswith("09:07:00+08:00")
    assert parent["amplitude_points"] == -291
    assert parent["first_seen_at"].endswith("09:16:00+08:00")
    assert parent["qualification"] == "OPENING_EXTREME_HIGHER_PIVOT"
    assert parent["defense"] is None  # anchor origin != a qualified Dow defense
    assert state["dow_context"]["large_state"] == "UNDEFINED"
    assert state["dow_context"]["small_bear_defense"]["time"].endswith("08:54:00+08:00")
    assert state["reverse_candidate"]["level"] == "SMALL"


@pytest.mark.parametrize("scale", [0.01, 0.1, 1.0, 10.0])
@pytest.mark.parametrize("mirror", [False, True])
def test_current_path_is_scale_invariant_mirrored_and_never_calls_zigzag(monkeypatch, scale, mirror):
    original = snapshot(bars_until("09:20"))
    bars = bars_until("09:20")
    for b in bars:
        values = {k: 100000 + (-1 if mirror else 1) * b[k] * scale for k in ("open", "high", "low", "close")}
        if mirror:
            values["high"], values["low"] = values["low"], values["high"]
        b.update(values)
    def forbidden(*args, **kwargs):
        raise AssertionError("fixed-point zigzag entered the course path")
    monkeypatch.setattr(anchors, "_zigzag", forbidden)
    state = snapshot(bars)
    assert state["thresholds"] == {}
    assert state["background_anchor"]["origin_time"] == original["background_anchor"]["origin_time"]
    assert state["background_anchor"]["first_seen_at"] == original["background_anchor"]["first_seen_at"]
    assert state["background_anchor"]["direction"] == ("BULL" if mirror else "BEAR")
    assert abs(state["background_anchor"]["amplitude_points"]) == pytest.approx(291 * scale)


def test_reject_fixed_point_configuration_in_course_path():
    with pytest.raises(anchors.AnchorLifecycleError, match="固定點數"):
        anchors.build_anchor_lifecycle(bars_until("09:20"), expected_as_of="2026-08-25T09:20:00+08:00",
            session_key="audit", course_chain_enabled=True, major_reversal_points=120)


def test_parent_does_not_appear_before_its_causal_confirmation():
    assert snapshot(bars_until("09:15"))["background_anchor"] is None
    a = snapshot(bars_until("09:16"))["background_anchor"]
    b = snapshot(bars_until("09:22"))["background_anchor"]
    assert a["id"] == b["id"]
    assert a["first_seen_at"] == b["first_seen_at"]
    assert a["origin_time"] == b["origin_time"]


def test_reverse_candidate_keeps_its_first_valid_origin_and_extends_in_place():
    first = snapshot(bars_until("09:16"))["reverse_candidate"]
    extended = snapshot(bars_until("09:22"))["reverse_candidate"]
    assert first["start_time"].endswith("09:07:00+08:00")
    assert first["id"] == extended["id"]
    assert extended["start_time"] == first["start_time"]
    assert extended["first_seen_at"] == first["first_seen_at"]
    assert extended["current_extreme_time"].endswith("09:19:00+08:00")
    assert extended["current_extreme_price"] == 44405


def test_future_bar_rejected_and_parent_evidence_never_uses_future_time():
    bars = bars_until("09:22")
    with pytest.raises(anchors.AnchorLifecycleError, match="未來"):
        anchors.build_anchor_lifecycle(bars, expected_as_of="2026-08-25T09:20:00+08:00", session_key="audit")
    for i in range(5, len(bars)):
        state = snapshot(bars[:i])
        for record in anchors.anchor_records(state):
            assert record.get("first_seen_at", state["as_of"]) <= state["as_of"]
            assert record.get("latest_extreme_time", record.get("current_extreme_time", state["as_of"])) <= state["as_of"]


def test_single_outside_candle_does_not_create_two_ordered_pivots():
    at = datetime.fromisoformat("2026-08-25T08:45:00+08:00")
    rows = [(100, 102, 98, 100), (100, 103, 97, 100), (100, 120, 80, 100),
            (100, 105, 95, 100), (100, 104, 96, 100), (100, 101, 79, 79), (80, 121, 80, 121)]
    bars = [{"time": (at + timedelta(minutes=i)).isoformat(), **dict(zip(("open", "high", "low", "close"), r))}
            for i, r in enumerate(rows)]
    points = anchors._causal_paired_points(bars)
    assert not any(p["time"] == bars[2]["time"] for p in points)


def test_quadrant_policy_is_program_owned_without_fixed_points_or_bar_windows():
    context = snapshot(bars_until("09:20"))["quadrant_context"]
    assert context["authority"] == "PROGRAM_POLICY_V1"
    assert context["background_primary"] == "TRANSITION"
    assert context["background_candidates"] == ["Q1", "Q4"]
    assert context["working_primary"] == "TRANSITION"
    assert context["working_candidates"] == ["Q2", "Q3"]
    assert context["rule"] == "PROGRAM_ORDINAL_SAME_GRADE_POLICY_V1_NO_FIXED_POINTS_OR_BARS"
    assert context["same_grade_comparisons"]["legs"]
    for window in (5, 20):
        diagnostic = _window_quadrant_metrics(bars_until("09:20"), movement_bars=window, ma21=None)
        assert diagnostic["recommended_quadrant"] is None
        assert diagnostic["true_range_ratio_recent_to_prior"] is not None


@pytest.mark.parametrize("q", ["Q1", "Q2", "Q3", "Q4"])
def test_renderer_uses_program_quadrant_verdict_not_model_label(q):
    lifecycle = snapshot(bars_until("09:20"))
    rendered = _v3_quadrant_line({"background_quadrant": q, "working_quadrant": q}, {"anchor_lifecycle": lifecycle})
    assert "主看TRANSITION" in rendered
    assert "次看Q1／Q4" in rendered
    assert "次看Q2／Q3" in rendered
    assert f"大級{q}" not in rendered


def test_renderer_does_not_force_current_trend_by_retracement_ratio():
    payload = {"large_trend": {"classification": "盤整"}, "current_trend": {"classification": "轉換中"},
               "message_direction": "BULL"}
    assert "小級轉換中" in _v3_structure_summary(payload, {"working_quadrant": "Q4"},
        ledger={"anchor_lifecycle": snapshot(bars_until("09:20"))})


def test_quadrant_transition_does_not_claim_structure_absent():
    result = _v3_quadrant_line({"background_quadrant": "TRANSITION", "working_quadrant": "UNDEFINED"})
    assert "大級象限轉換中" in result
    assert "小級象限未定" in result
    assert "尚未形成" not in result


def test_taiji_exposes_generational_measures_without_a_fake_verdict():
    state = snapshot(bars_until("09:15"))
    context = state["taiji_context"]
    evidence = context["leg_evidence"]
    assert evidence["quality_verdict"] is None
    assert evidence["same_direction_comparisons"]
    assert all("duration_ratio" in c for c in evidence["same_direction_comparisons"])
    assert all("wick_fraction" in leg and "slope_points_per_minute" in leg for leg in evidence["legs"])
    assert context["parent_end_time"] != state["child_anchor"]["first_extreme_time"]
    assert context["assessment_authority"] == "PROGRAM_POLICY_V1"
    assert context["program_state"] in {
        "FIRST_PUSH", "CORRECTION_FORMING", "CORRECTION_HELD",
        "CORRECTION_DESTRUCTIVE", "COPY_FORMING", "COPY_SUCCESS",
        "COPY_WEAKENED", "COPY_FAILED",
    }


@pytest.mark.parametrize("mirror", [False, True])
def test_opening_taiji_does_not_splice_an_interior_pivot_to_the_open(mirror):
    bars = bars_until("08:54")
    if mirror:
        for b in bars:
            b.update(open=100000-b["open"], close=100000-b["close"],
                     high=100000-b["low"], low=100000-b["high"])
    state = snapshot(bars)
    context = state["taiji_context"]
    assert context["parent_start_time"].endswith("08:45:00+08:00")
    assert context["parent_end_time"].endswith("08:45:00+08:00")
    assert context["parent_end_price"] == (100000-44372 if mirror else 44372)
    assert not any(leg["start_time"].endswith("08:45:00+08:00")
                   and leg["end_time"].endswith("08:50:00+08:00")
                   for leg in context["leg_evidence"]["legs"])


def test_course_dow_is_single_source_for_ledger_and_hard_constraints():
    from trade_monitor_replay.deterministic_state import build_evidence_ledger, _causal_structured_snapshot
    from trade_monitor_replay.runner import _deterministic_constraints
    bars = bars_until("09:00")
    structured = _causal_structured_snapshot(bars)
    ledger = build_evidence_ledger(structured, bars=bars, expected_as_of=bars[-1]["time"],
        session_key="2026-08-25:DAY", anchor_lifecycle_enabled=True, course_chain_enabled=True)
    assert ledger["legacy_dow_diagnostic"]["small"] == "UNDEFINED"
    assert ledger["dow"]["small"] == "BEAR"
    assert ledger["anchor_lifecycle"]["dow_context"]["small_bear_defense"]["time"].endswith("08:54:00+08:00")
    constraints = _deterministic_constraints(structured, stage="day", ledger=ledger)
    assert constraints["dow_small"] == "BEAR"
    assert constraints["dow_status_source"] == "COURSE_ANCHOR_LIFECYCLE"


def test_stop_can_use_full_correction_not_exact_point_two_atr_buffer():
    payload = {"action": {"position_action": "ENTER", "direction": "LONG", "entry_role": "INITIAL", "stop_price": 90},
               "course_reading": {"working_quadrant": "Q4"}}
    ledger = {"trade_levels": {"long_structural_stop": {"source_price": 100, "stop_price": 98}}}
    _validate_v3_trade_levels(payload, ledger=ledger)
    payload["action"]["stop_price"] = 101
    with pytest.raises(ReplayRunError, match="stop_price"):
        _validate_v3_trade_levels(payload, ledger=ledger)


def test_q4_stop_uses_matching_setup_correction_before_generic_pivot():
    payload = {
        "action": {
            "position_action": "ENTER",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "setup_key": "SETUP-q4-long",
            "stop_price": 44877.2,
        },
        "course_reading": {"working_quadrant": "Q4"},
    }
    ledger = {
        "trade_levels": {
            # This older generic pivot is outside the later, strategy-native
            # 09:31 -> 09:38 correction and must not replace its boundary.
            "long_structural_stop": {"source_price": 44873.0, "stop_price": 44862.3},
            "continuation_arm_candidate": {
                "setup_key": "SETUP-q4-long",
                "direction": "LONG",
                "stop_source_price": 44888.0,
            },
        }
    }
    _validate_v3_trade_levels(payload, ledger=ledger)

    payload["action"]["stop_price"] = 44888.0
    with pytest.raises(ReplayRunError, match="stop_price"):
        _validate_v3_trade_levels(payload, ledger=ledger)

    # A different setup cannot borrow this correction boundary.
    payload["action"].update({"setup_key": "SETUP-other", "stop_price": 44877.2})
    with pytest.raises(ReplayRunError, match="stop_price"):
        _validate_v3_trade_levels(payload, ledger=ledger)


def test_sparse_analysis_never_fills_at_an_already_seen_open():
    position = {"status": "FLAT"}
    analysis = {"course_reading": {"setup_stage": "ENTRY_ELIGIBLE"},
                "action": {"position_action": "ENTER", "direction": "LONG", "stop_price": 90, "entry_role": "INITIAL"}}
    gate = {"status": "ENTRY_ELIGIBLE", "direction": "LONG", "signal_time": "2026-08-25T09:01:00+08:00",
            "eligible_from": "2026-08-25T09:02:00+08:00", "expires_at": "2026-08-25T09:04:00+08:00"}
    pending = schedule_pending_entry(position, analysis, gate, as_of="2026-08-25T09:02:00+08:00")
    assert pending["pending_entry"]["eligible_from"] == "2026-08-25T09:03:00+08:00"
    bars = [{"time": f"2026-08-25T09:0{i}:00+08:00", "open": 100+i} for i in (2, 3)]
    filled, event = fill_pending_entry(pending, bars, as_of=bars[-1]["time"])
    assert event["fill_time"] == bars[-1]["time"] and filled["entry_price"] == 103


def test_probabilities_are_not_silently_swapped_or_mutated():
    scenario = {"bull_probability": 60, "range_probability": 30, "bear_probability": 10}
    before = copy.deepcopy(scenario)
    result = _preserve_active_session_anchor_probability(scenario, ledger={"anchor_lifecycle": snapshot(bars_until("09:15"))})
    assert result == before == scenario and result is not scenario


@pytest.mark.parametrize("fragment", ["正常說明。陶},{", "含有\ufffd替代字元", "含有\x00空字元"])
def test_corrupt_text_fragments_are_rejected_even_in_hidden_analysis_fields(fragment):
    with pytest.raises(SemanticReplayError, match="序列化殘片"):
        _reject_corrupt_text_fragments({"analysis": {"obstacles": [{"reaction": fragment}]}})


def test_normal_chinese_trading_text_is_not_rejected_as_corrupt():
    _reject_corrupt_text_fragments({"analysis": {"reaction": "收盤跌破後，觀察是否向下延續。"}})
