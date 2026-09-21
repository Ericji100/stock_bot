from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from trade_monitor_replay.config import ReplayConfig
from trade_monitor_replay.data_source import ReplayDataset
from trade_monitor_replay.deterministic_state import (
    DETERMINISTIC_TIMELINE_ENGINE_SHA256,
    DETERMINISTIC_TIMELINE_ENGINE_VERSION,
    DeterministicReplayError,
    apply_profit_milestone_protection,
    _continuation_arm_candidate,
    _confirmed_pullback_endpoint_candidate,
    _false_break_arm_candidate,
    _false_break_reclaim_events,
    _apply_program_course_entry_quality_gate,
    _candidate_is_countertrend_to_intact_controller,
    _derive_program_course_methods,
    _derive_numbered_market_state,
    _derive_program_scenario_weights,
    _derive_yizhi_state,
    _opening_range_pullback_candidate,
    _compression_breakout_candidate,
    _q2_failed_reverse_entry_candidate,
    _yizhi_momentum_entry_candidate,
    _merge_course_lifecycle_defenses,
    _position_protection_candidates,
    build_deterministic_timeline,
    build_evidence_ledger,
    evidence_events,
    program_setup_events,
    program_protection_events,
    program_course_state_events,
)
from trade_monitor_replay.execution_gate import derive_entry_eligibility, expire_stale_armed_setups
from trade_monitor_replay.minimax_analyzer import MiniMaxReplayResult
from trade_monitor_replay.presentation import (
    _compact_public_text,
    _v3_position_lines,
    _v3_public_text,
    _v3_annotate_known_levels,
    _v3_presentation_ledger,
    render_semantic_replay_event_card,
)
from trade_monitor_replay.presentation import _v3_event_title
from trade_monitor_replay.runner import (
    ReplayRunError,
    _apply_required_profit_protection,
    ReplayRunner,
    _annotate_course_cclass_constraint,
    _bind_continuation_candidate_to_available_reentry,
    _freeze_continuation_candidate,
    _message_is_current,
    _program_reentry_expectation,
    _prompt_event_lifecycle_view,
    _prompt_ledger_view,
    _reconcile_program_reentry_memory,
    _retired_setup_keys,
    _select_event_driven_bars,
    _suppress_exhausted_reentry_candidate,
    _suppress_retired_continuation_candidate,
    _suppress_countertrend_continuation_candidate,
    _terminal_setup_keys,
    _validate_retired_setup_keys,
    _validate_latest_ohlc_claims,
    _validate_v3_obstacles,
    _validate_v3_trade_levels,
)
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _aligned_entry_grade_upgrade,
    _canonicalize_program_owned_actionable_setups,
    _canonicalize_ai_hybrid_unbacked_event_card,
    _canonicalize_noop_action_labels,
    _canonicalize_program_action_envelope,
    _canonicalize_program_course_memory,
    _canonicalize_program_course_reading,
    _canonicalize_program_scenario_weights,
    _canonicalize_program_market_trends,
    _canonicalize_program_message_direction,
    _canonicalize_program_setup_and_entry,
    _canonicalize_program_reentry_summary,
    _canonicalize_pre_structure_working_quadrant,
    _program_setup_invalidation_event,
    _validate_program_owned_actionable_setups,
    _canonicalize_taiji_parent_citation,
    _canonicalize_unavailable_background_quadrant,
    _canonicalize_unavailable_background_quadrant_memory,
    _has_explicit_entry_risk_limit,
    _is_highest_active_anchor_aligned_entry,
    _is_large_aligned_false_break_reentry,
    _is_countertrend_to_highest_active_anchor,
    _large_grade_upgrade_quadrant_authority,
    _large_trend_for_promoted_structure,
    _latest_material_structure_event,
    _reading_v3,
    _structure_control,
    _preserve_active_session_anchor_probability,
    _preserve_promoted_structure_quadrants,
    _restore_program_held_setup,
    _restore_program_reentry_setup,
    _terminalize_explicit_exit_setup,
    _reject_corrupt_text_fragments,
    _validate_countertrend_preparation_maturity,
    _validate_v3_state_transition,
    _validate_program_reentry_state,
    _validate_held_setup_continuity,
    _validate_taiji_parent_role,
    _validate_no_same_trigger_setup_rekey,
    _validate_program_continuation_arm,
    _validate_setup_trigger_levels,
    initial_position_state,
    preview_position_transition,
    validate_semantic_envelope,
)


ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "trade_monitor_replay" / "rules" / "deterministic-v2.1.8-replay-adapter-v4" / "rule-manifest.json"
V5_MANIFEST = ROOT / "trade_monitor_replay" / "rules" / "course-state-v2.1.8-replay-adapter-v5" / "rule-manifest.json"
V6_MANIFEST = ROOT / "trade_monitor_replay" / "rules" / "course-state-v2.1.8-replay-adapter-v6" / "rule-manifest.json"
V7_MANIFEST = ROOT / "trade_monitor_replay" / "rules" / "course-state-v2.1.8-replay-adapter-v7" / "rule-manifest.json"


def _structured(*, latest_time: str, pivots: list[dict], dow_small: str = "UNDEFINED") -> dict:
    return {
        "latest_closed_k": {
            "time": latest_time,
            "open": 130,
            "high": 145,
            "low": 125,
            "close": 140,
            "volume": 10,
        },
        "indicators": {"sma21": 120, "sma105": 110, "atr14": 10},
        "opening_ranges": {"or5": None, "or15": None},
        "causal_structure_n2": {
            "dow_small": dow_small,
            "dow_large": "UNDEFINED",
            "recent_confirmed_pivots": pivots,
            "recent_large_pivots": [],
        },
    }


def _bars(end: int) -> list[dict]:
    prices = {
        0: (110, 112, 105, 108),
        1: (108, 109, 100, 105),
        2: (105, 115, 104, 113),
        3: (113, 120, 112, 119),
        4: (119, 130, 118, 128),
        5: (128, 135, 126, 132),
        6: (132, 140, 130, 138),
        7: (138, 145, 137, 140),
        8: (140, 143, 130, 132),
        9: (132, 136, 125, 128),
        10: (128, 133, 120, 125),
    }
    return [
        {
            "time": f"2026-08-26T09:{minute:02d}:00+08:00",
            "open": prices[minute][0],
            "high": prices[minute][1],
            "low": prices[minute][2],
            "close": prices[minute][3],
            "volume": 10,
        }
        for minute in range(end + 1)
    ]


def _low() -> dict:
    return {
        "kind": "LOW",
        "bar_time": "2026-08-26T09:01:00+08:00",
        "confirmation_time": "2026-08-26T09:03:00+08:00",
        "price": 100,
    }


def _high() -> dict:
    return {
        "kind": "HIGH",
        "bar_time": "2026-08-26T09:07:00+08:00",
        "confirmation_time": "2026-08-26T09:10:00+08:00",
        "price": 145,
    }


def _payload(ledger: dict, *, as_of: str, session_key: str) -> dict:
    legs = ledger["legs"]
    small_leg = next((item["id"] for item in reversed(legs) if item["level"] == "SMALL"), None)
    return {
        "analysis": {
            "original_decision": "NOTIFY",
            "notification_reason": "結構出現可解讀變化。",
            "latest_closed_k_price_estimate": "收140點",
            "large_trend": {"classification": "盤整", "details": ["大級方向尚未確認。"]},
            "current_trend": {"classification": "轉換中", "details": ["小級正在建立控制。"]},
            "market_summary": ["價格結構已更新。"],
            "course_reading": {
                "large_anchor_ref": None,
                "small_anchor_ref": small_leg,
                "large_defense_ref": None,
                "small_defense_ref": None,
                "quadrant": "TRANSITION",
                "taiji": "第一段正在形成，尚待修正與複製。",
                "yizhi": "未見合格異常動能。",
                "left_right": "未形成反轉成熟度。",
                "dow": "小級方向尚未完成道氏確認。",
                "primary_lens": "定錨加四象限。",
                "main_strategy": "第四象限順勢區域觀察。",
                "setup_stage": "FORMING",
                "strategy_reason": ["等待回檔收斂。"],
            },
            "scenario": {
                "bull_probability": 50,
                "range_probability": 35,
                "bear_probability": 15,
                "primary": "多方保持控制。",
                "alternative": "轉為盤整。",
                "view_change": "跌破結構低點後降級。",
            },
            "action": {
                "position_action": "NONE",
                "direction": "NONE",
                "observation_area": "觀察回檔區。",
                "trigger": "等待收盤突破修正高點。",
                "entry": "條件未完成，不進場。",
                "structural_stop": "進場後使用同級結構低點。",
                "stop_price": None,
                "nearest_obstacle": "前高。",
                "expected_behavior": "回檔應縮量放慢。",
                "max_wait": "最多等待三根K。",
                "no_chase": "急拉不得追價。",
                "management": "維持空手。",
            },
        },
        "memory": {
            "version": 2,
            "as_of": as_of,
            "session_key": session_key,
            "active_setup": "第四象限回檔候選",
            "thesis_bias": "CONDITIONAL",
            "maintain": "結構低點守住。",
            "downgrade": "回檔不再收斂。",
            "flip": "跌破同級防線後再評估。",
            "notes": ["等待確認。"],
        },
    }


def test_ledger_uses_confirmation_time_and_sparse_points_do_not_backdate() -> None:
    early_time = "2026-08-26T09:05:00+08:00"
    early = build_evidence_ledger(
        _structured(latest_time=early_time, pivots=[_low()]),
        bars=_bars(5),
        expected_as_of=early_time,
        session_key="2026-08-26:DAY",
    )
    late_time = "2026-08-26T09:10:00+08:00"
    late = build_evidence_ledger(
        _structured(latest_time=late_time, pivots=[_low(), _high()]),
        bars=_bars(10),
        expected_as_of=late_time,
        session_key="2026-08-26:DAY",
    )
    high = next(item for item in late["pivots"] if item["kind"] == "HIGH")
    assert high["bar_time"].endswith("09:07:00+08:00")
    assert high["first_seen_at"].endswith("09:10:00+08:00")
    new_pivot = next(item for item in evidence_events(early, late) if item["event_type"] == "PIVOT_CONFIRMED")
    assert new_pivot["event_time"].endswith("09:10:00+08:00")


def test_program_marks_first_close_break_and_disallows_broken_defense_reference() -> None:
    as_of = "2026-08-26T09:10:00+08:00"
    ledger = build_evidence_ledger(
        _structured(latest_time=as_of, pivots=[_low(), _high()], dow_small="BULL"),
        bars=_bars(10),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    defense = ledger["defenses"][0]
    assert defense["state"] == "ACTIVE"
    broken_bars = _bars(10) + [
        {"time": "2026-08-26T09:11:00+08:00", "open": 101, "high": 102, "low": 95, "close": 99, "volume": 10}
    ]
    late = build_evidence_ledger(
        _structured(latest_time="2026-08-26T09:11:00+08:00", pivots=[_low(), _high()], dow_small="BULL"),
        bars=broken_bars,
        expected_as_of="2026-08-26T09:11:00+08:00",
        session_key="2026-08-26:DAY",
    )
    assert late["defenses"][0]["state"] == "BROKEN"
    assert late["defenses"][0]["broken_at"].endswith("09:11:00+08:00")
    assert any(item["event_type"] == "DEFENSE_BROKEN" for item in evidence_events(ledger, late))


def test_defense_survives_dow_transition_until_program_records_break() -> None:
    as_of = "2026-08-26T09:10:00+08:00"
    active = build_evidence_ledger(
        _structured(latest_time=as_of, pivots=[_low(), _high()], dow_small="BULL"),
        bars=_bars(10),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    broken_bars = _bars(10) + [
        {"time": "2026-08-26T09:11:00+08:00", "open": 101, "high": 102, "low": 95, "close": 99, "volume": 10}
    ]
    transitioned = build_evidence_ledger(
        _structured(
            latest_time="2026-08-26T09:11:00+08:00",
            pivots=[_low(), _high()],
            dow_small="TRANSITION",
        ),
        bars=broken_bars,
        expected_as_of="2026-08-26T09:11:00+08:00",
        session_key="2026-08-26:DAY",
        previous_ledger=active,
    )
    assert len(transitioned["defenses"]) == 1
    assert transitioned["defenses"][0]["state"] == "BROKEN"
    assert transitioned["defenses"][0]["broken_at"].endswith("09:11:00+08:00")


def test_semantic_contract_rejects_invented_anchor_reference() -> None:
    as_of = "2026-08-26T09:05:00+08:00"
    ledger = build_evidence_ledger(
        _structured(latest_time=as_of, pivots=[_low()]),
        bars=_bars(5),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _payload(ledger, as_of=as_of, session_key="2026-08-26:DAY")
    payload["analysis"]["course_reading"]["small_anchor_ref"] = "L-INVENTED"
    with pytest.raises(SemanticReplayError, match="證據帳本"):
        validate_semantic_envelope(
            payload,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=initial_position_state(as_of=as_of),
        )


def test_semantic_contract_rejects_data_insufficient_when_ledger_is_fresh() -> None:
    as_of = "2026-08-26T09:05:00+08:00"
    ledger = build_evidence_ledger(
        _structured(latest_time=as_of, pivots=[_low()]),
        bars=_bars(5),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _payload(ledger, as_of=as_of, session_key="2026-08-26:DAY")
    payload["analysis"]["large_trend"]["classification"] = "資料不足"
    with pytest.raises(SemanticReplayError, match="不得標成資料不足"):
        validate_semantic_envelope(
            payload,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=initial_position_state(as_of=as_of),
        )


def test_program_owns_position_transitions_and_setup_cancellation_is_not_exit() -> None:
    as_of = "2026-08-26T09:05:00+08:00"
    ledger = build_evidence_ledger(
        _structured(latest_time=as_of, pivots=[_low()]),
        bars=_bars(5),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _payload(ledger, as_of=as_of, session_key="2026-08-26:DAY")["analysis"]
    flat = initial_position_state(as_of=as_of)
    payload["course_reading"]["setup_stage"] = "INVALIDATED"
    assert preview_position_transition(flat, payload, as_of=as_of, latest_close=132, preopen=False)["status"] == "FLAT"
    payload["action"].update({"position_action": "ENTER", "direction": "LONG", "stop_price": 120})
    entered = preview_position_transition(flat, payload, as_of=as_of, latest_close=132, preopen=False)
    assert entered["status"] == "LONG"
    assert entered["entry_price"] == 132


def test_management_stop_update_is_persisted_and_cannot_be_loosened() -> None:
    as_of = "2026-08-26T09:05:00+08:00"
    ledger = build_evidence_ledger(
        _structured(latest_time=as_of, pivots=[_low()]),
        bars=_bars(5),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _payload(ledger, as_of=as_of, session_key="2026-08-26:DAY")["analysis"]
    position = initial_position_state(as_of=as_of)
    position.update({"status": "LONG", "direction": "LONG", "entry_price": 120.0, "stop_price": 110.0})
    payload["action"].update({"position_action": "NONE", "direction": "NONE", "stop_price": 125.0})

    tightened = preview_position_transition(position, payload, as_of=as_of, latest_close=132.0, preopen=False)

    assert tightened["stop_price"] == 125.0
    payload["action"]["stop_price"] = 109.0
    with pytest.raises(SemanticReplayError, match="不得放寬"):
        preview_position_transition(position, payload, as_of=as_of, latest_close=132.0, preopen=False)


def test_position_protection_buffer_is_locked_when_defense_first_becomes_visible() -> None:
    start = datetime.fromisoformat("2026-08-26T09:00:00+08:00")
    bars = []
    for index in range(21):
        center = 1000 + index
        width = 20 if index <= 16 else 100 + index
        bars.append(
            {
                "time": (start + timedelta(minutes=index)).isoformat(),
                "open": center,
                "high": center + width,
                "low": center - width,
                "close": center + 1,
            }
        )
    state = {
        "background_anchor": None,
        "child_anchor": {
            "id": "A-bull",
            "direction": "BULL",
            "first_seen_at": bars[10]["time"],
            "defense": {
                "id": "D-bull",
                "time": bars[14]["time"],
                "price": 980.0,
                "state": "ACTIVE",
                "first_seen_at": bars[16]["time"],
            },
        },
    }

    first = _position_protection_candidates(state, bars=bars[:17])["LONG"]
    later = _position_protection_candidates(state, bars=bars)["LONG"]

    assert later["atr_buffer_points"] == first["atr_buffer_points"]
    assert later["stop_price"] == first["stop_price"]
    assert later["buffer_locked_at"] == bars[16]["time"]

    with_background = copy.deepcopy(state)
    with_background["background_anchor"] = {
        "id": "A-large-bull",
        "direction": "BULL",
        "first_seen_at": bars[18]["time"],
        "defense": {
            "id": "D-large-bull",
            "time": bars[15]["time"],
            "price": 950.0,
            "state": "ACTIVE",
            "first_seen_at": bars[18]["time"],
        },
    }
    selected = _position_protection_candidates(with_background, bars=bars)["LONG"]
    assert selected["source_defense_id"] == "D-bull"
    assert selected["stop_price"] == first["stop_price"]


def test_position_protection_prefers_newer_live_dow_slot_over_old_anchor_defense() -> None:
    bars = [
        {
            "time": f"2026-08-20T11:{minute:02d}:00+08:00",
            "open": 44650.0,
            "high": 44660.0,
            "low": 44630.0,
            "close": 44645.0,
        }
        for minute in range(14, 29)
    ]
    state = {
        "background_anchor": None,
        "child_anchor": {
            "id": "child-bear",
            "level": "SMALL",
            "direction": "BEAR",
            "defense": {
                "id": "old-defense",
                "direction": "BEAR",
                "time": "2026-08-20T10:35:00+08:00",
                "price": 44791.0,
                "first_seen_at": "2026-08-20T10:52:00+08:00",
                "state": "ACTIVE",
            },
        },
        "dow_context": {
            "small_bear_defense": {
                "id": "new-defense",
                "level": "SMALL",
                "role": "DIRECTIONAL_SLOT",
                "direction": "BEAR",
                "time": "2026-08-20T11:20:00+08:00",
                "price": 44712.0,
                "first_seen_at": "2026-08-20T11:27:00+08:00",
                "state": "ACTIVE",
            }
        },
    }

    candidate = _position_protection_candidates(state, bars=bars)["SHORT"]

    assert candidate["source_defense_id"] == "new-defense"
    assert candidate["source_time"].endswith("11:20:00+08:00")
    assert candidate["stop_price"] < 44791.0


def test_management_stop_accepts_only_old_or_new_program_candidate() -> None:
    payload = {
        "action": {
            "position_action": "NONE",
            "direction": "NONE",
            "stop_price": 125.0,
        },
        "course_reading": {"working_quadrant": "Q4"},
    }
    ledger = {
        "trade_levels": {
            "position_protection_candidates": {
                "LONG": {"stop_price": 125.0, "protection_required": True},
                "SHORT": None,
            }
        }
    }
    position = {"status": "LONG", "stop_price": 110.0}

    _validate_v3_trade_levels(payload, ledger=ledger, position=position)
    payload["action"]["stop_price"] = 110.0
    _validate_v3_trade_levels(payload, ledger=ledger, position=position)
    payload["action"]["stop_price"] = 124.6
    with pytest.raises(ReplayRunError, match="不得因後續ATR變動"):
        _validate_v3_trade_levels(payload, ledger=ledger, position=position)


def test_management_stop_rejects_reference_only_program_candidate() -> None:
    payload = {
        "action": {"position_action": "NONE", "direction": "NONE", "stop_price": 125.0},
        "course_reading": {"working_quadrant": "Q4"},
    }
    ledger = {
        "trade_levels": {
            "position_protection_candidates": {
                "LONG": {
                    "stop_price": 125.0,
                    "protection_required": False,
                    "protection_reason": "REFERENCE_ONLY",
                },
                "SHORT": None,
            }
        }
    }
    position = {"status": "LONG", "stop_price": 110.0}

    with pytest.raises(ReplayRunError, match="不得因後續ATR變動"):
        _validate_v3_trade_levels(payload, ledger=ledger, position=position)


def test_semantic_renderer_shows_effective_management_stop_update() -> None:
    as_of = "2026-08-26T09:05:00+08:00"
    ledger = build_evidence_ledger(
        _structured(latest_time=as_of, pivots=[_low()]),
        bars=_bars(5),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _payload(ledger, as_of=as_of, session_key="2026-08-26:DAY")["analysis"]
    payload["message_type"] = "MANAGEMENT"
    payload["action"].update({"position_action": "NONE", "direction": "NONE", "stop_price": 125.0})
    before = initial_position_state(as_of=as_of)
    before.update({"status": "LONG", "direction": "LONG", "entry_price": 120.0, "stop_price": 110.0})
    after = dict(before, stop_price=125.0)

    rendered = render_semantic_replay_event_card(
        payload,
        {"expected_latest_closed_k_hhmm": "09:05"},
        stage="day",
        ledger=ledger,
        position_before=before,
        position_after=after,
    )

    assert "停損110點→125點" in rendered.body


def test_semantic_renderer_hides_internal_ids_and_zero_minute_wording() -> None:
    as_of = "2026-08-26T09:01:00+08:00"
    immediate_low = dict(_low())
    immediate_low["confirmation_time"] = as_of
    ledger = build_evidence_ledger(
        _structured(latest_time=as_of, pivots=[immediate_low]),
        bars=_bars(1),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _payload(ledger, as_of=as_of, session_key="2026-08-26:DAY")["analysis"]
    payload["notification_reason"] = "條件更新；系統強制通知：內部原因很多"
    flat = initial_position_state(as_of=as_of)
    rendered = render_semantic_replay_event_card(
        payload,
        {"expected_latest_closed_k_hhmm": "09:01"},
        stage="day",
        ledger=ledger,
        position_before=flat,
        position_after=flat,
    )
    assert "L-" not in rendered.body
    assert "系統強制通知" not in rendered.body
    assert "0分鐘" not in rendered.body


class _SemanticFakeAnalyzer:
    def analyze(self, prompt: str) -> MiniMaxReplayResult:
        marker = '<RUNTIME_CONTEXT>\n'
        runtime_text = prompt.split(marker, 1)[1].split('\n</RUNTIME_CONTEXT>', 1)[0]
        runtime = json.loads(runtime_text)
        ledger = runtime["deterministic_evidence_ledger"]
        expected = runtime["expected_latest_closed_k_iso"]
        session = runtime["expected_session_key"]
        payload = _payload(ledger, as_of=expected, session_key=session)
        payload["analysis"]["course_reading"].update(
            {"large_anchor_ref": None, "small_anchor_ref": None, "large_defense_ref": None, "small_defense_ref": None}
        )
        if runtime["replay"]["preopen_snapshot"]:
            payload["analysis"]["action"].update({"position_action": "NONE", "direction": "NONE"})
        raw = json.dumps(payload, ensure_ascii=False)
        return MiniMaxReplayResult(payload=payload, raw_text=raw, diagnostics={"model": "fake", "usage": {}})


def _frame(times: list[str], prices: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "bar_time": pd.to_datetime(times).tz_localize("Asia/Taipei"),
            "open": prices,
            "high": [value + 2 for value in prices],
            "low": [value - 2 for value in prices],
            "close": [value + 1 for value in prices],
            "volume": [10] * len(times),
            "sma21": [None] * len(times),
            "sma105": [None] * len(times),
            "atr14": [None] * len(times),
        }
    )


def test_deterministic_runner_writes_isolated_ledger_not_formal_state(tmp_path: Path) -> None:
    dataset = ReplayDataset(
        target_date=date(2026, 8, 27),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(["2026-08-26 15:00", "2026-08-26 15:01"], [100, 101]),
        day_bars=_frame(["2026-08-27 08:45"], [102]),
        source_sha256={"2026-08-26": "a" * 64, "2026-08-27": "b" * 64},
    )
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=MANIFEST, ai_provider="codex", ai_model="fake"),
        analyzer=_SemanticFakeAnalyzer(),
        dataset_loader=lambda *_args, **_kwargs: dataset,
        runtime_root=tmp_path,
    )
    result = asyncio.run(runner.run(target_date=date(2026, 8, 27), max_bars=1))
    run_dir = Path(result["run_directory"])
    assert (run_dir / "deterministic-evidence-ledger.json").exists()
    assert (run_dir / "replay-position-state.json").exists()
    assert not (run_dir / "market-structure-state.json").exists()
    assert result["completed_day_bars"] == 1


def _upgrade_pivots() -> list[dict]:
    points = [
        ("LOW", "09:00", "09:02", 44861),
        ("HIGH", "09:06", "09:08", 45145),
        ("LOW", "09:08", "09:10", 45025),
        ("HIGH", "09:10", "09:12", 45154),
        ("LOW", "09:25", "09:27", 44957),
    ]
    return [
        {
            "kind": kind,
            "bar_time": f"2026-08-26T{bar_time}:00+08:00",
            "confirmation_time": f"2026-08-26T{confirmed}:00+08:00",
            "price": price,
        }
        for kind, bar_time, confirmed, price in points
    ]


def _upgrade_bars(*, include_breakout: bool = True) -> list[dict]:
    points = [
        ("09:00", 44910, 44920, 44861, 44880),
        ("09:06", 45120, 45145, 45100, 45130),
        ("09:08", 45060, 45070, 45025, 45035),
        # The 09:08 low earns Dow-defense status only after this closed bar
        # exceeds the prior 09:06 high.  A wick-only high is not sufficient.
        ("09:10", 45120, 45154, 45110, 45150),
        ("09:25", 44990, 45000, 44957, 44980),
        ("09:27", 45020, 45050, 45010, 45040),
        ("09:33", 45120, 45153, 45110, 45150),
    ]
    if include_breakout:
        points.append(("09:34", 45150, 45188, 45145, 45187))
    return [
        {
            "time": f"2026-08-26T{at}:00+08:00",
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 10,
        }
        for at, open_, high, low, close in points
    ]


def _upgrade_structured(as_of: str) -> dict:
    result = _structured(latest_time=as_of, pivots=_upgrade_pivots())
    result["latest_closed_k"].update(
        {"time": as_of, "open": 45150, "high": 45188, "low": 45145, "close": 45187}
    )
    result["indicators"].update({"sma21": 45080, "atr14": 42})
    return result


def _quadrant_baseline_bars() -> list[dict]:
    closes = [
        45000, 45030, 44990, 45025, 44995, 45020, 44985,
        45015, 44990, 45010, 44980, 45005, 44975, 45000,
        44970, 44980, 44990, 45000, 45010, 45020, 45030,
    ]
    result: list[dict] = []
    for index, close in enumerate(closes):
        width = 20 if index < 11 else 24 if index < 18 else 36
        minute = 14 + index
        result.append(
            {
                "time": f"2026-08-26T09:{minute:02d}:00+08:00",
                "open": close - 2,
                "high": close + width / 2,
                "low": close - width / 2,
                "close": close,
                "volume": 10,
            }
        )
    return result


def _v3_payload(ledger: dict, *, as_of: str, structure_event_ref: str | None) -> dict:
    large_anchor = ledger["control_candidates"]["forming_large_anchor_ref"]
    small_anchor = ledger["control_candidates"]["forming_small_anchor_ref"]
    return {
        "analysis": {
            "original_decision": "NOTIFY",
            "message_type": "STRUCTURE_UPGRADE" if structure_event_ref else "OBSERVATION",
            "message_direction": "BULL",
            "notification_reason": "小級防線失守後重新突破前高，控制級數升級。",
            "latest_closed_k_price_estimate": "45,187點",
            "large_trend": {"classification": "偏多但回檔", "details": ["大級多方結構仍有效。"]},
            "current_trend": {"classification": "偏多", "details": ["小級重新向上發動。"]},
            "market_summary": ["原小級震盪已併入較大級多方結構。"],
            "course_reading": {
                "large_anchor_ref": large_anchor,
                "small_anchor_ref": small_anchor,
                "working_anchor_ref": small_anchor,
                "large_defense_ref": None,
                "small_defense_ref": None,
                "structure_event_ref": structure_event_ref,
                "controlling_grade": "LARGE",
                "grade_relation": "ALIGNED",
                "background_quadrant": "Q2",
                "working_quadrant": "Q1",
                "primary_quadrant_candidate": "Q1",
                "secondary_quadrant_candidate": "Q4",
                "background_trend_dynamics": "DECREASING",
                "background_volatility_dynamics": "EXPANDING",
                "working_trend_dynamics": "INCREASING",
                "working_volatility_dynamics": "EXPANDING",
                "focus_methods": ["DOW", "QUADRANT"],
                "taiji": "較大級父代形成，小級等待修正後複製。",
                "yizhi": "未出現需要改用一之戰法的異常動能。",
                "left_right": "目前不以反轉戰法為主鏡頭。",
                "dow": "末小級多方防線失守後重新創高，結構升級。",
                "primary_lens": "大級Q4背景，小級等待良性修正。",
                "main_strategy": "Q4順勢回檔",
                "setup_stage": "FORMING",
                "strategy_reason": ["等待回踩原突破區守住。"],
            },
            "scenario": {
                "bull_probability": 60,
                "range_probability": 25,
                "bear_probability": 15,
                "bull_plan": "守住45,154附近後重新發動，延續大級多方結構。",
                "range_plan": "45,154附近反覆時維持區間觀察。",
                "bear_plan": "收破44,957且無法站回，才提高空方接管權重。",
                "view_change": "收破44,957後降級；再破44,861才評估大級翻空。",
            },
            "action": {
                "position_action": "NONE",
                "direction": "NONE",
                "entry_role": "NOT_APPLICABLE",
                "setup_key": "bull-q4-0900",
                "observation_area": "45,135～45,154",
                "trigger": "回踩守住後由已收盤K重新轉強。",
                "entry": "目前不追價，等待回踩確認。",
                "structural_stop": "未來多單使用44,957外的同級結構停損。",
                "stop_price": None,
                "obstacles": [
                    {"price": 45154, "role": "CHECKPOINT", "label": "前高", "reaction": "突破續抱，受阻再降級。"}
                ],
                "expected_behavior": "觸發後三至五根內應挑戰前高。",
                "max_wait_bars": 5,
                "no_chase": "直接拉離45,188時不追。",
                "management": "空手等待Q4回踩完成。",
                "reentry_status": "NOT_APPLICABLE",
            },
        },
        "memory": {
            "version": 3,
            "as_of": as_of,
            "session_key": "2026-08-26:DAY",
            "active_setups": [
                {
                    "setup_key": "bull-q4-0900", "name": "Q4順勢回檔", "direction": "LONG",
                    "stage": "FORMING", "trigger": "回踩守住後轉強。", "stop": "44,957外。",
                    "reentry_status": "NOT_APPLICABLE"
                },
                {
                    "setup_key": "bear-break-0845", "name": "大級跌破延續", "direction": "SHORT",
                    "stage": "FORMING", "trigger": "依序收破44,957與44,861。", "stop": "反彈高點外。",
                    "reentry_status": "NOT_APPLICABLE"
                },
            ],
            "thesis_bias": "BULL",
            "maintain": "44,957守住且回檔受控。",
            "downgrade": "收破44,957。",
            "flip": "再破44,861並形成空方延續。",
            "structure_control": {
                "active_large_anchor_ref": large_anchor,
                "active_small_anchor_ref": small_anchor,
                "working_anchor_ref": small_anchor,
                "controlling_grade": "LARGE",
                "background_quadrant": "Q2",
                "working_quadrant": "Q1",
                "background_quadrant_changed_at": as_of,
                "working_quadrant_changed_at": as_of,
                "last_structure_event_ref": structure_event_ref,
            },
            "reentry": {"status": "NOT_APPLICABLE", "last_stop_at": None, "count": 0},
            "notes": ["情境權重不是統計勝率。"],
        },
    }


def _v7_payload(ledger: dict, *, as_of: str, structure_event_ref: str | None = None) -> dict:
    payload = _v3_payload(ledger, as_of=as_of, structure_event_ref=structure_event_ref)
    reading = payload["analysis"]["course_reading"]
    reading.update(
        {
            "cclass_mode": "TAIJI_ORDERED",
            "x_stage": "ANCHOR_LENS_SELECTION",
            "x_process": "已完成定錨與主鏡頭選擇，等待已收盤觸發進入執行步驟。",
        }
    )
    payload["analysis"]["action"]["entry_rejection_reason"] = "NONE"
    for index, setup in enumerate(payload["memory"]["active_setups"]):
        setup.update(
            {
                "trigger_level": 45154 if index == 0 else 44957,
                "trigger_operator": "CLOSE_ABOVE" if index == 0 else "CLOSE_BELOW",
                "valid_bars": 3,
            }
        )
    return payload


def test_program_detects_0925_to_0934_small_to_large_upgrade_without_future_data() -> None:
    prior_as_of = "2026-08-26T09:33:00+08:00"
    prior = build_evidence_ledger(
        _upgrade_structured(prior_as_of),
        bars=_upgrade_bars(include_breakout=False),
        expected_as_of=prior_as_of,
        session_key="2026-08-26:DAY",
    )
    assert prior["structure_events"] == []

    as_of = "2026-08-26T09:34:00+08:00"
    current = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
        previous_ledger=prior,
    )
    event = next(item for item in current["structure_events"] if item["event_type"] == "GRADE_UPGRADE")
    assert event["event_type"] == "GRADE_UPGRADE"
    assert event["first_seen_at"] == as_of
    assert event["parent_origin_price"] == 44861
    assert event["absorbed_defense_price"] == 45025
    assert event["replacement_defense_price"] == 44957
    assert event["reclaimed_boundary_price"] == 45154
    assert event["absorbed_defense_qualified_at"] == "2026-08-26T09:10:00+08:00"
    assert event["absorbed_defense_broken_at"] == "2026-08-26T09:25:00+08:00"
    assert event["qualification_status"] == "PROGRAM_CAUSAL_CANDIDATE"
    assert event["grade_decision_authority"] == "AI_HYBRID"
    promoted = next(item for item in current["legs"] if item.get("source") == "GRADE_UPGRADE")
    assert promoted["start_price"] == 44861
    assert promoted["end_price"] == 45188
    assert any(item["event_id"] == event["id"] for item in evidence_events(prior, current))


def test_program_downgrades_promoted_structure_before_parent_invalidation() -> None:
    upgrade_as_of = "2026-08-26T09:34:00+08:00"
    prior = build_evidence_ledger(
        _upgrade_structured(upgrade_as_of),
        bars=_upgrade_bars(),
        expected_as_of=upgrade_as_of,
        session_key="2026-08-26:DAY",
    )
    downgrade_bar = {
        "time": "2026-08-26T09:35:00+08:00",
        "open": 44970,
        "high": 44980,
        "low": 44940,
        "close": 44950,
        "volume": 10,
    }
    structured = _upgrade_structured(downgrade_bar["time"])
    structured["latest_closed_k"] = dict(downgrade_bar)
    downgraded = build_evidence_ledger(
        structured,
        bars=_upgrade_bars() + [downgrade_bar],
        expected_as_of=downgrade_bar["time"],
        session_key="2026-08-26:DAY",
        previous_ledger=prior,
    )
    event = next(item for item in downgraded["structure_events"] if item["event_type"] == "GRADE_DOWNGRADE")
    assert event["broken_defense_price"] == 44957
    assert event["parent_origin_price"] == 44861
    assert event["first_seen_at"] == downgrade_bar["time"]
    assert not any(item["event_type"] == "STRUCTURE_INVALIDATED" for item in downgraded["structure_events"])

    invalidation_bar = {
        "time": "2026-08-26T09:36:00+08:00",
        "open": 44940,
        "high": 44945,
        "low": 44840,
        "close": 44850,
        "volume": 10,
    }
    invalid_structured = _upgrade_structured(invalidation_bar["time"])
    invalid_structured["latest_closed_k"] = dict(invalidation_bar)
    invalidated = build_evidence_ledger(
        invalid_structured,
        bars=_upgrade_bars() + [downgrade_bar, invalidation_bar],
        expected_as_of=invalidation_bar["time"],
        session_key="2026-08-26:DAY",
        previous_ledger=downgraded,
    )
    invalid_event = next(
        item for item in invalidated["structure_events"] if item["event_type"] == "STRUCTURE_INVALIDATED"
    )
    assert invalid_event["broken_parent_price"] == 44861
    assert invalid_event["first_seen_at"] == invalidation_bar["time"]


def test_broken_defense_cannot_create_a_second_false_break_after_its_first_breach() -> None:
    bars = [
        {
            "time": f"2026-08-26T{at}:00+08:00",
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": 10,
        }
        for at, close in [
            ("09:02", 101),
            ("09:03", 99),
            ("09:04", 98),
            ("09:05", 99),
            ("09:06", 99),
            ("09:07", 99),
            ("09:08", 99),
            ("09:09", 101),
            ("09:10", 99),
            ("09:11", 102),
        ]
    ]
    events = _false_break_reclaim_events(
        pivots=[],
        defenses=[
            {
                "id": "D-old",
                "direction": "BULL",
                "price": 100,
                "first_seen_at": "2026-08-26T09:00:00+08:00",
                "state": "BROKEN",
                "broken_at": "2026-08-26T09:03:00+08:00",
            }
        ],
        opening_ranges=None,
        bars=bars,
        expected=datetime.fromisoformat("2026-08-26T09:11:00+08:00"),
    )

    assert events == []


def test_course_lifecycle_defense_is_merged_into_false_break_evidence() -> None:
    defense = {
        "id": "ANCHOR_DEFENSE-live-bear",
        "direction": "BEAR",
        "time": "2026-08-21T11:10:00+08:00",
        "price": 45178,
        "first_seen_at": "2026-08-21T11:20:00+08:00",
        "state": "BROKEN_AND_RECLAIMED",
        "broken_at": "2026-08-21T11:32:00+08:00",
        "reclaimed_at": "2026-08-21T11:36:00+08:00",
        "role": "ACTIVE_CHILD",
    }

    merged = _merge_course_lifecycle_defenses(
        [],
        {
            "dow_context": {
                "large_bull_defense": None,
                "large_bear_defense": None,
                "small_bull_defense": None,
                "small_bear_defense": defense,
            }
        },
    )

    assert merged == [
        {
            **defense,
            "level": "SMALL",
            "bar_time": "2026-08-21T11:10:00+08:00",
        }
    ]


def test_false_break_waits_through_shallow_recross_for_decisive_reclaim() -> None:
    bars = [
        {
            "time": f"2026-08-21T09:{minute:02d}:00+08:00",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 10,
        }
        for minute in range(14)
    ]
    bars.extend(
        [
            {
                "time": "2026-08-21T09:14:00+08:00",
                "open": 100,
                "high": 104,
                "low": 100,
                "close": 102,
                "volume": 10,
            },
            {
                "time": "2026-08-21T09:15:00+08:00",
                "open": 102,
                "high": 102,
                "low": 99,
                "close": 99.8,
                "volume": 10,
            },
            {
                "time": "2026-08-21T09:16:00+08:00",
                "open": 100,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 10,
            },
            {
                "time": "2026-08-21T09:17:00+08:00",
                "open": 100,
                "high": 100,
                "low": 94,
                "close": 95,
                "volume": 10,
            },
        ]
    )
    events = _false_break_reclaim_events(
        pivots=[],
        defenses=[
            {
                "id": "ANCHOR_DEFENSE-live-bear",
                "direction": "BEAR",
                "price": 100,
                "first_seen_at": "2026-08-21T09:05:00+08:00",
                "state": "BROKEN_AND_RECLAIMED",
                "broken_at": "2026-08-21T09:14:00+08:00",
                "reclaimed_at": "2026-08-21T09:17:00+08:00",
            }
        ],
        opening_ranges=None,
        bars=bars,
        expected=datetime.fromisoformat("2026-08-21T09:17:00+08:00"),
    )

    assert len(events) == 1
    assert events[0]["source_id"] == "ANCHOR_DEFENSE-live-bear"
    assert events[0]["breach_time"] == "2026-08-21T09:14:00+08:00"
    assert events[0]["first_seen_at"] == "2026-08-21T09:17:00+08:00"
    assert events[0]["bars_to_reclaim"] == 3


def test_program_exposes_and_requires_pullback_continuation_arm_candidate() -> None:
    as_of = datetime.fromisoformat("2026-08-26T09:34:00+08:00")
    state = {
        "background_anchor": {"direction": "BEAR"},
        "working_leg": {
            "direction": "BEAR",
            "status": "FORMING",
            "role": "BACKGROUND_RETEST",
            "start_time": "2026-08-26T09:31:00+08:00",
            "start_price": 44330,
            "current_extreme_time": "2026-08-26T09:33:00+08:00",
            "current_extreme_price": 44203,
        },
        "quadrant_context": {
            "child_comparisons": {
                "legs": [
                    {
                        "direction": "BULL",
                        "status": "LOCAL_CONFIRMED",
                        "start_time": "2026-08-26T09:25:00+08:00",
                        "start_price": 44150,
                        "end_time": "2026-08-26T09:31:00+08:00",
                        "end_price": 44330,
                    }
                ]
            }
        },
    }
    candidate = _continuation_arm_candidate(state, expected=as_of)
    assert candidate is not None
    assert candidate["direction"] == "SHORT"
    assert candidate["trigger_operator"] == "CLOSE_BELOW"
    assert candidate["trigger_level"] == 44203
    assert candidate["stop_source_price"] == 44330

    ledger = {"trade_levels": {"continuation_arm_candidate": candidate}}
    analysis = {
        "message_type": "OBSERVATION",
        "course_reading": {"setup_stage": "FORMING", "structure_event_ref": None},
        "action": {"setup_key": None},
    }
    memory = {"active_setups": []}
    flat = initial_position_state(as_of=as_of.isoformat(), version=2)
    with pytest.raises(SemanticReplayError, match="不得再多等一個循環"):
        _validate_program_continuation_arm(
            analysis,
            memory,
            ledger=ledger,
            position=flat,
            entry_gate={"status": "NONE"},
        )

    analysis.update(
        {
            "message_type": "PREPARATION",
            "course_reading": {"setup_stage": "ARMED", "structure_event_ref": None},
            "action": {"setup_key": candidate["setup_key"]},
        }
    )
    memory["active_setups"] = [
        {
            "setup_key": candidate["setup_key"],
            "direction": "SHORT",
            "stage": "ARMED",
            "trigger_operator": "CLOSE_BELOW",
            "trigger_level": 44203.0,
            "valid_bars": candidate["valid_bars"],
        }
    ]
    _validate_program_continuation_arm(
        analysis,
        memory,
        ledger=ledger,
        position=flat,
        entry_gate={"status": "NONE"},
    )


def test_program_arms_qualified_reverse_candidate_after_confirmed_pullback() -> None:
    as_of = datetime.fromisoformat("2026-08-25T09:46:00+08:00")
    state = {
        "background_anchor": {"direction": "BEAR"},
        "child_anchor": {"direction": "BULL", "status": "ACTIVE"},
        "reverse_candidate": {"direction": "BULL", "status": "QUALIFIED"},
        "working_leg": {
            "direction": "BULL",
            "status": "FORMING",
            "role": "REVERSE_CANDIDATE_CONTINUATION",
            "start_time": "2026-08-25T09:43:00+08:00",
            "start_price": 44296,
            "current_extreme_time": "2026-08-25T09:45:00+08:00",
            "current_extreme_price": 44382,
        },
        "quadrant_context": {
            "child_comparisons": {
                "legs": [
                    {
                        "direction": "BEAR",
                        "status": "LOCAL_CONFIRMED",
                        "start_time": "2026-08-25T09:40:00+08:00",
                        "start_price": 44457,
                        "end_time": "2026-08-25T09:43:00+08:00",
                        "end_price": 44296,
                    }
                ]
            }
        },
    }

    candidate = _continuation_arm_candidate(state, expected=as_of)

    assert candidate is not None
    assert candidate["direction"] == "LONG"
    assert candidate["trigger_operator"] == "CLOSE_ABOVE"
    assert candidate["trigger_level"] == 44382
    assert candidate["stop_source_time"] == "2026-08-25T09:43:00+08:00"
    assert candidate["stop_source_price"] == 44296


@pytest.mark.parametrize(
    ("direction", "side", "trigger_operator"),
    [("BEAR", "SHORT", "CLOSE_BELOW"), ("BULL", "LONG", "CLOSE_ABOVE")],
)
def test_child_control_anchor_can_arm_aligned_q4_continuation(
    direction: str,
    side: str,
    trigger_operator: str,
) -> None:
    """No formal large anchor is required for the active child grade to trade."""

    as_of = datetime.fromisoformat("2026-08-24T11:06:00+08:00")

    def price(value: float) -> float:
        return 100000.0 - value if direction == "BULL" else value

    correction_direction = "BULL" if direction == "BEAR" else "BEAR"
    state = {
        "background_anchor": None,
        "child_anchor": {
            "id": "child-controller",
            "direction": direction,
            "status": "ACTIVE",
            "defense": {"state": "ACTIVE", "price": price(45130)},
        },
        "reverse_candidate": {
            "direction": correction_direction,
            "status": "QUALIFIED",
        },
        "working_leg": {
            "direction": direction,
            "status": "FORMING",
            "role": "REVERSE_CANDIDATE_PULLBACK",
            "start_time": "2026-08-24T11:00:00+08:00",
            "start_price": price(45130),
            "current_extreme_time": "2026-08-24T11:06:00+08:00",
            "current_extreme_price": price(45030),
        },
        "quadrant_context": {
            "child_comparisons": {
                "anchor_ref": "child-controller",
                "legs": [
                    {
                        "direction": correction_direction,
                        "status": "LOCAL_CONFIRMED",
                        "start_time": "2026-08-24T10:03:00+08:00",
                        "start_price": price(44812),
                        "end_time": "2026-08-24T11:00:00+08:00",
                        "end_price": price(45130),
                    }
                ],
            }
        },
    }

    candidate = _continuation_arm_candidate(state, expected=as_of)

    assert candidate is not None
    assert candidate["direction"] == side
    assert candidate["trigger_operator"] == trigger_operator
    assert candidate["trigger_level"] == price(45030)
    assert candidate["stop_source_time"] == "2026-08-24T11:00:00+08:00"
    assert candidate["stop_source_price"] == price(45130)


def test_0824_frozen_bear_continuation_becomes_entry_eligible_on_1106_close() -> None:
    memory = {
        "as_of": "2026-08-24T11:04:00+08:00",
        "active_setups": [
            {
                "stage": "ARMED",
                "setup_key": "SETUP-0824-bear-continuation",
                "name": "空方修正後複製",
                "direction": "SHORT",
                "trigger_operator": "CLOSE_BELOW",
                "trigger_level": 45048,
                "valid_bars": 3,
            }
        ],
    }
    bars = [
        {
            "time": "2026-08-24T11:05:00+08:00",
            "open": 45086,
            "high": 45086,
            "low": 45062,
            "close": 45065,
        },
        {
            "time": "2026-08-24T11:06:00+08:00",
            "open": 45060,
            "high": 45061,
            "low": 45030,
            "close": 45041,
        },
    ]

    gate = derive_entry_eligibility(
        memory,
        bars,
        as_of="2026-08-24T11:06:00+08:00",
        position=initial_position_state(
            as_of="2026-08-24T11:04:00+08:00",
            version=2,
        ),
        current_candidate=None,
    )

    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["direction"] == "SHORT"
    assert gate["trigger_level"] == 45048.0
    assert gate["signal_time"] == "2026-08-24T11:06:00+08:00"
    assert gate["signal_close"] == 45041.0
    assert gate["eligible_from"] == "2026-08-24T11:07:00+08:00"


def test_program_does_not_arm_unqualified_reverse_candidate() -> None:
    as_of = datetime.fromisoformat("2026-08-25T09:46:00+08:00")
    state = {
        "background_anchor": {"direction": "BEAR"},
        "child_anchor": {"direction": "BULL", "status": "ACTIVE"},
        "reverse_candidate": {"direction": "BULL", "status": "FORMING"},
        "working_leg": {
            "direction": "BULL",
            "status": "FORMING",
            "role": "REVERSE_CANDIDATE_CONTINUATION",
            "start_time": "2026-08-25T09:43:00+08:00",
            "start_price": 44296,
            "current_extreme_time": "2026-08-25T09:45:00+08:00",
            "current_extreme_price": 44382,
        },
    }

    assert _continuation_arm_candidate(state, expected=as_of) is None


def test_program_arms_same_cutoff_parent_correction_breakout() -> None:
    as_of = datetime.fromisoformat("2026-08-25T12:14:00+08:00")
    state = {
        "background_anchor": {"direction": "BULL"},
        "child_anchor": {
            "direction": "BULL",
            "status": "ACTIVE",
            "defense": {"state": "ACTIVE", "broken_at": None},
        },
        "working_leg": None,
        "quadrant_context": {
            "child_comparisons": {
                "legs": [
                    {
                        "direction": "BULL",
                        "status": "LOCAL_CONFIRMED",
                        "start_time": "2026-08-25T12:08:00+08:00",
                        "start_price": 44553,
                        "end_time": "2026-08-25T12:10:00+08:00",
                        "end_price": 44633,
                    },
                    {
                        "direction": "BEAR",
                        "status": "LOCAL_CONFIRMED",
                        "start_time": "2026-08-25T12:10:00+08:00",
                        "start_price": 44633,
                        "end_time": "2026-08-25T12:12:00+08:00",
                        "end_price": 44575,
                    },
                    {
                        "direction": "BULL",
                        "status": "FORMING",
                        "start_time": "2026-08-25T12:12:00+08:00",
                        "start_price": 44575,
                        "end_time": "2026-08-25T12:14:00+08:00",
                        "end_price": 44679,
                    },
                ]
            }
        },
    }

    candidate = _continuation_arm_candidate(state, expected=as_of)

    assert candidate is not None
    assert candidate["direction"] == "LONG"
    assert candidate["trigger_operator"] == "CLOSE_ABOVE"
    assert candidate["trigger_time"] == "2026-08-25T12:10:00+08:00"
    assert candidate["trigger_level"] == 44633.0
    assert candidate["stop_source_time"] == "2026-08-25T12:12:00+08:00"
    assert candidate["stop_source_price"] == 44575.0


def test_program_rearms_q4_from_new_held_pullback_endpoint() -> None:
    """08/21 10:10 must become a fresh setup, not reuse retired 10:02."""

    bars = [
        {"time": "2026-08-21T10:08:00+08:00", "open": 45038, "high": 45062, "low": 45009, "close": 45049},
        {"time": "2026-08-21T10:09:00+08:00", "open": 45047, "high": 45096, "low": 45044, "close": 45086},
        {"time": "2026-08-21T10:10:00+08:00", "open": 45090, "high": 45098, "low": 45013, "close": 45013},
        {"time": "2026-08-21T10:11:00+08:00", "open": 45015, "high": 45068, "low": 45015, "close": 45031},
        {"time": "2026-08-21T10:12:00+08:00", "open": 45030, "high": 45054, "low": 45027, "close": 45050},
    ]
    state = {
        "background_anchor": None,
        "child_anchor": {
            "id": "ANCHOR-0821-bull",
            "direction": "BULL",
            "status": "ACTIVE",
            "origin_time": "2026-08-21T09:07:00+08:00",
            "origin_price": 44576,
            "first_seen_at": "2026-08-21T09:44:00+08:00",
            "defense": {"state": "ACTIVE", "price": 44888},
        },
        "working_leg": None,
        "quadrant_context": {},
    }

    candidate = _continuation_arm_candidate(
        state,
        expected=datetime.fromisoformat("2026-08-21T10:12:00+08:00"),
        bars=bars,
    )

    assert candidate is not None
    assert candidate["candidate_source"] == "CONFIRMED_PULLBACK_ENDPOINT_N2"
    assert candidate["direction"] == "LONG"
    assert candidate["stop_source_time"] == "2026-08-21T10:10:00+08:00"
    assert candidate["stop_source_price"] == 45013.0
    assert candidate["trigger_time"] == "2026-08-21T10:11:00+08:00"
    assert candidate["trigger_level"] == 45068.0
    assert candidate["first_seen_at"] == "2026-08-21T10:12:00+08:00"
    assert candidate["setup_key"] != _continuation_arm_candidate(
        {
            **state,
            "working_leg": {
                "direction": "BULL",
                "status": "FORMING",
                "role": "BACKGROUND_RETEST",
                "start_time": "2026-08-21T10:02:00+08:00",
                "start_price": 44930,
                "current_extreme_time": "2026-08-21T10:08:00+08:00",
                "current_extreme_price": 45062,
            },
            "quadrant_context": {
                "same_grade_comparisons": {
                    "anchor_ref": "ANCHOR-0821-bull",
                    "legs": [
                        {
                            "direction": "BEAR",
                            "status": "LOCAL_CONFIRMED",
                            "start_time": "2026-08-21T10:01:00+08:00",
                            "start_price": 45035,
                            "end_time": "2026-08-21T10:02:00+08:00",
                            "end_price": 44930,
                        }
                    ],
                }
            },
        },
        expected=datetime.fromisoformat("2026-08-21T10:08:00+08:00"),
    )["setup_key"]


def test_program_does_not_rearm_pullback_before_two_right_bars_hold() -> None:
    state = {
        "background_anchor": None,
        "child_anchor": {
            "id": "ANCHOR-0821-bull",
            "direction": "BULL",
            "status": "ACTIVE",
            "origin_time": "2026-08-21T09:07:00+08:00",
            "origin_price": 44576,
            "first_seen_at": "2026-08-21T09:44:00+08:00",
            "defense": {"state": "ACTIVE", "price": 44888},
        },
        "working_leg": None,
        "quadrant_context": {},
    }
    bars = [
        {"time": "2026-08-21T10:08:00+08:00", "open": 45038, "high": 45062, "low": 45009, "close": 45049},
        {"time": "2026-08-21T10:09:00+08:00", "open": 45047, "high": 45096, "low": 45044, "close": 45086},
        {"time": "2026-08-21T10:10:00+08:00", "open": 45090, "high": 45098, "low": 45013, "close": 45013},
        {"time": "2026-08-21T10:11:00+08:00", "open": 45015, "high": 45068, "low": 45015, "close": 45031},
    ]

    assert _continuation_arm_candidate(
        state,
        expected=datetime.fromisoformat("2026-08-21T10:11:00+08:00"),
        bars=bars,
    ) is None


def test_frozen_armed_trigger_can_reference_a_recent_bar_extreme() -> None:
    ledger = {
        "latest_closed_k": {"open": 44375, "high": 44438, "low": 44365, "close": 44414},
        "recent_bar_levels": [
            {
                "time": "2026-08-25T09:45:00+08:00",
                "open": 44361,
                "high": 44382,
                "low": 44347,
                "close": 44375,
            }
        ],
    }

    _validate_setup_trigger_levels(
        [
            {
                "setup_key": "SETUP-frozen",
                "trigger_level": 44382.0,
            }
        ],
        ledger,
    )


def test_existing_setup_keeps_its_frozen_trigger_after_level_scrolls_out() -> None:
    ledger = {
        "latest_closed_k": {"open": 45900, "high": 45920, "low": 45887, "close": 45903},
        "recent_bar_levels": [],
    }
    setup = {
        "setup_key": "SETUP-existing",
        "trigger_level": 45863.0,
        "trigger_operator": "CLOSE_ABOVE",
    }

    _validate_setup_trigger_levels([setup], ledger, previous_setups=[dict(setup)])

    with pytest.raises(SemanticReplayError, match="本輪已揭露"):
        _validate_setup_trigger_levels(
            [{**setup, "trigger_level": 45864.0}],
            ledger,
            previous_setups=[dict(setup)],
        )


def test_programmatic_next_open_fill_is_current_only_after_covering_cutoff() -> None:
    manifest = {
        "selected_bar_times": [
            "2026-08-25T09:48:00+08:00",
            "2026-08-25T09:50:00+08:00",
        ],
        "next_day_index": 2,
    }
    message = {
        "stage": "day",
        "bar_time": "2026-08-25T09:49:00+08:00",
        "source": "programmatic_entry_fill",
    }

    assert _message_is_current(manifest, message) is True
    assert _message_is_current(
        manifest,
        {**message, "source": "programmatic_exit_fill"},
    ) is True
    assert _message_is_current({**manifest, "next_day_index": 1}, message) is False
    assert _message_is_current({**manifest, "next_day_index": 2}, {**message, "source": "ai"}) is False


def test_latest_bar_ohlc_prose_is_checked_by_field_not_only_known_price() -> None:
    latest = {"open": 44386, "high": 44433, "low": 44367, "close": 44381}
    _validate_latest_ohlc_claims(
        {"details": ["本根已收盤K開44,386、高44,433、低44,367、收44,381。"]},
        latest=latest,
    )
    with pytest.raises(ReplayRunError, match="latest_closed_k.high"):
        _validate_latest_ohlc_claims(
            {"details": ["本根高44,333後收44,381。"]},
            latest=latest,
        )


def test_latest_bar_ohlc_validator_does_not_treat_historical_pivot_as_current_low() -> None:
    latest = {"open": 44878, "high": 44916, "low": 44872, "close": 44897}

    _validate_latest_ohlc_claims(
        {
            "notification_reason": (
                "本根收盤站穩OR5高點44,875，09:19低點44,780後的多方工作段開始形成。"
            )
        },
        latest=latest,
    )


def test_latest_bar_ohlc_validator_still_checks_compact_sequence_after_open() -> None:
    latest = {"open": 44878, "high": 44916, "low": 44872, "close": 44897}

    with pytest.raises(ReplayRunError, match="latest_closed_k.low"):
        _validate_latest_ohlc_claims(
            {"details": ["本根開44,878、高44,916、低44,780、收44,897。"]},
            latest=latest,
        )


def test_taiji_dynasty_parent_cannot_be_replaced_by_a_later_copy() -> None:
    ledger = {
        "anchor_lifecycle": {
            "taiji_context": {"parent_start_price": 44467.0, "parent_end_price": 44176.0}
        }
    }
    _validate_taiji_parent_role(
        {"taiji": "大級父代由44,467推進至44,176；後續44,405至44,150為空方複製。"},
        ledger=ledger,
    )
    with pytest.raises(SemanticReplayError, match="朝代父代"):
        _validate_taiji_parent_role(
            {"taiji": "大級空方父代為44,405至44,150。"},
            ledger=ledger,
        )


def test_taiji_parent_requires_the_deterministic_endpoint_times_when_available() -> None:
    ledger = {
        "anchor_lifecycle": {
            "taiji_context": {
                "parent_direction": "BEAR",
                "parent_start_time": "2026-08-26T09:20:00+08:00",
                "parent_start_price": 45135.0,
                "parent_end_time": "2026-08-26T09:25:00+08:00",
                "parent_end_price": 44957.0,
            }
        }
    }
    _validate_taiji_parent_role(
        {"taiji": "小級空方父代由09:20高45,135點推進至09:25低44,957點。"},
        ledger=ledger,
    )
    with pytest.raises(SemanticReplayError, match="正確時間與價格"):
        _validate_taiji_parent_role(
            {"taiji": "小級空方父代由08:45低45,135點推進至09:25低44,957點。"},
            ledger=ledger,
        )
    with pytest.raises(SemanticReplayError, match="緊鄰其正確時間"):
        _validate_taiji_parent_role(
            {"taiji": "小級空方父代為09:20高點08:45低45,135點→09:25低44,957點。"},
            ledger=ledger,
        )


def test_taiji_parent_citation_is_canonicalized_from_program_owned_endpoints() -> None:
    ledger = {
        "anchor_lifecycle": {
            "taiji_context": {
                "parent_direction": "BULL",
                "parent_start_time": "2026-08-26T09:25:00+08:00",
                "parent_start_price": 44957.0,
                "parent_end_time": "2026-08-26T09:56:00+08:00",
                "parent_end_price": 45420.0,
            }
        }
    }
    reading = {
        "taiji": "大級多方父代為09:25的44957→09:56的45420；後續複製延伸但斜率放緩。"
    }

    _canonicalize_taiji_parent_citation(reading, ledger=ledger)

    assert reading["taiji"] == (
        "父代為09:25低點44,957點→09:56高點45,420點；後續複製延伸但斜率放緩。"
    )
    _validate_taiji_parent_role(reading, ledger=ledger)


def test_taiji_parent_citation_preserves_session_open_role() -> None:
    ledger = {
        "anchor_lifecycle": {
            "child_anchor": {
                "anchor_origin_kind": "SESSION_OPEN",
                "origin_time": "2026-08-24T08:45:00+08:00",
                "origin_price": 45045.0,
            },
            "taiji_context": {
                "parent_direction": "BULL",
                "parent_start_time": "2026-08-24T08:45:00+08:00",
                "parent_start_price": 45045.0,
                "parent_start_kind": "LOW",
                "parent_end_time": "2026-08-24T09:01:00+08:00",
                "parent_end_price": 45234.0,
                "parent_end_kind": "HIGH",
            }
        }
    }
    reading = {"taiji": "小級父代為08:45開盤價45045至09:01高點45234；目前進入修正。"}

    _canonicalize_taiji_parent_citation(reading, ledger=ledger)

    assert reading["taiji"] == (
        "父代為08:45開盤價45,045點→09:01高點45,234點；目前進入修正。"
    )
    _validate_taiji_parent_role(reading, ledger=ledger)


def test_defense_first_breach_can_still_be_reclaimed_within_the_event_window() -> None:
    bars = [
        {
            "time": f"2026-08-26T{at}:00+08:00",
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": 10,
        }
        for at, close in [("09:02", 101), ("09:03", 98), ("09:04", 103)]
    ]
    events = _false_break_reclaim_events(
        pivots=[],
        defenses=[
            {
                "id": "D-live-at-breach",
                "direction": "BULL",
                "price": 100,
                "first_seen_at": "2026-08-26T09:00:00+08:00",
                "state": "BROKEN",
                "broken_at": "2026-08-26T09:03:00+08:00",
            }
        ],
        opening_ranges=None,
        bars=bars,
        expected=datetime.fromisoformat("2026-08-26T09:04:00+08:00"),
    )

    assert len(events) == 1
    assert events[0]["source_id"] == "D-live-at-breach"
    assert events[0]["breach_time"] == "2026-08-26T09:03:00+08:00"


def test_program_detects_false_break_reclaim_and_publishes_reentry_stop() -> None:
    points = [
        ("09:41", 45194, 45199, 45162, 45177),
        ("09:42", 45180, 45240, 45171, 45210),
        ("09:43", 45207, 45214, 45181, 45181),
        ("09:44", 45188, 45233, 45179, 45222),
        ("09:45", 45223, 45230, 45196, 45213),
        ("09:46", 45216, 45217, 45180, 45186),
        ("09:47", 45184, 45209, 45174, 45196),
        ("09:48", 45194, 45204, 45160, 45160),
        ("09:49", 45156, 45182, 45148, 45159),
        ("09:50", 45159, 45214, 45143, 45212),
    ]
    bars = [
        {
            "time": f"2026-08-26T{at}:00+08:00",
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 10,
        }
        for at, open_, high, low, close in points
    ]
    as_of = bars[-1]["time"]
    structured = _structured(
        latest_time=as_of,
        pivots=[
            {
                "kind": "LOW",
                "bar_time": "2026-08-26T09:41:00+08:00",
                "confirmation_time": "2026-08-26T09:43:00+08:00",
                "price": 45162,
            }
        ],
    )
    structured["latest_closed_k"] = dict(bars[-1])
    structured["indicators"].update({"sma21": 45177.6, "atr14": 48.18})
    ledger = build_evidence_ledger(
        structured,
        bars=bars,
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    event = next(item for item in ledger["structure_events"] if item["event_type"] == "FALSE_BREAK_RECLAIM")
    assert event["direction"] == "BULL"
    assert event["level_price"] == 45162
    assert event["breach_time"] == "2026-08-26T09:48:00+08:00"
    assert event["breach_extreme"] == 45143
    assert event["first_seen_at"] == as_of
    assert event["bars_to_reclaim"] == 2
    reentry = ledger["trade_levels"]["latest_false_break_reentry"]
    assert reentry["source_event_id"] == event["id"]
    assert reentry["atr_buffer_points"] == 9.6
    assert reentry["stop_price"] == 45133.4
    payload = {
        "course_reading": {"working_quadrant": "Q2"},
        "action": {
            "position_action": "ENTER",
            "direction": "LONG",
            "entry_role": "REENTRY",
            "stop_price": 45133.4,
        },
    }
    _validate_v3_trade_levels(payload, ledger=ledger)


    payload["action"]["stop_price"] = 45152
    with pytest.raises(ReplayRunError, match="stop_price"):
        _validate_v3_trade_levels(payload, ledger=ledger)

    wrong_event_card = _v3_payload(ledger, as_of=as_of, structure_event_ref=event["id"])
    wrong_event_card["analysis"]["course_reading"].update(
        {
            "working_quadrant": "Q2",
            "primary_quadrant_candidate": "Q2",
            "working_trend_dynamics": "DECREASING",
            "working_volatility_dynamics": "EXPANDING",
        }
    )
    wrong_event_card["memory"]["structure_control"]["working_quadrant"] = "Q2"
    with pytest.raises(SemanticReplayError, match="PREPARATION"):
        validate_semantic_envelope(
            wrong_event_card,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=initial_position_state(as_of=as_of, version=2),
            evidence_events=evidence_events(None, ledger),
        )

    duplicate_ledger = copy.deepcopy(ledger)
    duplicate = copy.deepcopy(event)
    duplicate["id"] = "S-ffffffffffffffff"
    duplicate["source_id"] = "or5"
    duplicate_ledger["structure_events"].append(duplicate)
    duplicate_ledger["structure_events"].sort(
        key=lambda item: (item["first_seen_at"], item["id"])
    )
    canonical = _v3_payload(
        duplicate_ledger,
        as_of=as_of,
        structure_event_ref=duplicate["id"],
    )
    canonical["analysis"]["message_type"] = "PREPARATION"
    canonical["analysis"]["course_reading"].update(
        {
            "working_quadrant": "Q2",
            "primary_quadrant_candidate": "Q2",
            "working_trend_dynamics": "DECREASING",
            "working_volatility_dynamics": "EXPANDING",
            "setup_stage": "ARMED",
        }
    )
    canonical["memory"]["structure_control"]["working_quadrant"] = "Q2"
    canonical["memory"]["active_setups"][0]["stage"] = "ARMED"
    validate_semantic_envelope(
        canonical,
        ledger=duplicate_ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=initial_position_state(as_of=as_of, version=2),
        evidence_events=evidence_events(None, duplicate_ledger),
    )


def test_false_break_reclaim_arms_then_requires_later_confirmation_break() -> None:
    start = datetime.fromisoformat("2026-08-26T09:30:00+08:00")
    bars = [
        {
            "time": (start + timedelta(minutes=index)).isoformat(),
            "open": 100.0,
            "high": 103.0,
            "low": 97.0,
            "close": 101.0,
        }
        for index in range(15)
    ]
    bars[-1].update({"open": 98.0, "high": 104.0, "low": 96.0, "close": 102.0})
    as_of = bars[-1]["time"]
    event = {
        "id": "FB-bull",
        "event_type": "FALSE_BREAK_RECLAIM",
        "direction": "BULL",
        "level_price": 100.0,
        "breach_time": bars[-2]["time"],
        "breach_extreme": 95.0,
        "first_seen_at": as_of,
    }

    candidate = _false_break_arm_candidate(
        [event],
        expected=datetime.fromisoformat(as_of),
        bars=bars,
    )

    assert candidate is not None
    assert candidate["candidate_source"] == "FALSE_BREAK_RECLAIM"
    assert candidate["decision_authority"] == "PROGRAM"
    assert candidate["trigger_operator"] == "CLOSE_ABOVE"
    assert candidate["trigger_level"] == 104.0
    assert candidate["reclaim_boundary_level"] == 100.0
    assert candidate["stop_price"] < 95.0
    gate = derive_entry_eligibility(
        {"as_of": bars[-2]["time"], "active_setups": []},
        bars,
        as_of=as_of,
        position={"status": "FLAT", "pending_entry": None},
        current_candidate=candidate,
    )
    assert gate["status"] == "NONE"

    later = {
        "time": (start + timedelta(minutes=15)).isoformat(),
        "open": 102.0,
        "high": 106.0,
        "low": 101.0,
        "close": 105.0,
    }
    gate = derive_entry_eligibility(
        {
            "as_of": as_of,
            "active_setups": [
                {
                    "setup_key": candidate["setup_key"],
                    "name": candidate["setup_name"],
                    "direction": candidate["direction"],
                    "stage": "ARMED",
                    "trigger_operator": candidate["trigger_operator"],
                    "trigger_level": candidate["trigger_level"],
                    "valid_bars": candidate["valid_bars"],
                    "reentry_status": "NOT_APPLICABLE",
                }
            ],
        },
        [*bars, later],
        as_of=later["time"],
        position={"status": "FLAT", "pending_entry": None},
        current_candidate=candidate,
    )
    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["signal_time"] == later["time"]
    assert gate["required_position_action"] == "ENTER"
    assert gate["required_stop_price"] == candidate["stop_price"]


def test_event_timeline_is_causal_and_event_schedule_keeps_reclaim_bar() -> None:
    start = datetime.fromisoformat("2026-08-26T09:27:00+08:00")
    bars = []
    for index in range(14):
        close = 45190 + (index % 3 - 1) * 3
        bars.append(
            {
                "time": (start + timedelta(minutes=index)).isoformat(),
                "open": close,
                "high": close + 12,
                "low": close - 12,
                "close": close,
                "volume": 10,
            }
        )
    points = [
        ("09:41", 45194, 45199, 45162, 45177),
        ("09:42", 45180, 45240, 45171, 45210),
        ("09:43", 45207, 45214, 45181, 45181),
        ("09:44", 45188, 45233, 45179, 45222),
        ("09:45", 45223, 45230, 45196, 45213),
        ("09:46", 45216, 45217, 45180, 45186),
        ("09:47", 45184, 45209, 45174, 45196),
        ("09:48", 45194, 45204, 45160, 45160),
        ("09:49", 45156, 45182, 45148, 45159),
        ("09:50", 45159, 45214, 45143, 45212),
    ]
    bars.extend(
        {
            "time": f"2026-08-26T{at}:00+08:00",
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 10,
        }
        for at, open_, high, low, close in points
    )
    prefix = build_deterministic_timeline(bars[:-1], session_key="2026-08-26:DAY")
    assert not any(item["bar_time"].endswith("09:50:00+08:00") for item in prefix)

    timeline = build_deterministic_timeline(bars, session_key="2026-08-26:DAY")
    reclaim = next(item for item in timeline if item["bar_time"].endswith("09:50:00+08:00"))
    assert any(event["event_type"] == "FALSE_BREAK_RECLAIM" for event in reclaim["events"])

    frame = pd.DataFrame(
        {
            "bar_time": pd.to_datetime([item["time"] for item in bars]),
            "open": [item["open"] for item in bars],
            "high": [item["high"] for item in bars],
            "low": [item["low"] for item in bars],
            "close": [item["close"] for item in bars],
            "volume": [item["volume"] for item in bars],
        }
    )
    selected, audit = _select_event_driven_bars(
        frame,
        session_key="2026-08-26:DAY",
        start_time=None,
        end_time=None,
        max_bars=None,
    )
    assert "2026-08-26T09:50:00+08:00" in {
        item.isoformat() for item in selected["bar_time"]
    }
    assert len(audit) >= len(selected)


def test_event_timeline_rejects_missing_one_minute_bar() -> None:
    bars = [
        {"time": "2026-08-26T08:45:00+08:00", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1},
        {"time": "2026-08-26T08:47:00+08:00", "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1},
    ]
    with pytest.raises(DeterministicReplayError, match="連續1分K"):
        build_deterministic_timeline(bars, session_key="2026-08-26:DAY")


def test_program_setup_lifecycle_is_a_material_exact_timeline_event(monkeypatch) -> None:
    prior = {
        "as_of": "2026-08-26T09:36:00+08:00",
        "trade_levels": {"continuation_arm_candidate": None},
    }
    current = {
        "as_of": "2026-08-26T09:37:00+08:00",
        "trade_levels": {
            "continuation_arm_candidate": {
                "setup_key": "long-q4-0937",
                "direction": "LONG",
                "first_seen_at": "2026-08-26T09:37:00+08:00",
            }
        },
    }
    events = program_setup_events(prior, current)
    assert [item["event_type"] for item in events] == ["PROGRAM_SETUP_ARMED"]
    assert events[0]["event_time"].endswith("09:37:00+08:00")

    frame = pd.DataFrame(
        {
            "bar_time": pd.to_datetime(["2026-08-26T09:37:00+08:00"]),
            "open": [100],
            "high": [102],
            "low": [99],
            "close": [101],
            "volume": [1],
        }
    )
    monkeypatch.setattr(
        "trade_monitor_replay.runner.build_deterministic_timeline",
        lambda *args, **kwargs: [
            {
                "bar_time": "2026-08-26T09:37:00+08:00",
                "events": events,
            }
        ],
    )
    selected, _ = _select_event_driven_bars(
        frame,
        session_key="2026-08-26:DAY",
        start_time=None,
        end_time=None,
        max_bars=None,
    )
    assert [item.isoformat() for item in selected["bar_time"]] == [
        "2026-08-26T09:37:00+08:00"
    ]


def test_program_protection_event_retains_exact_first_observable_bar() -> None:
    prior = {
        "as_of": "2026-08-26T09:39:00+08:00",
        "trade_levels": {"position_protection_candidates": {"LONG": None, "SHORT": None}},
    }
    current = {
        "as_of": "2026-08-26T09:40:00+08:00",
        "trade_levels": {
            "position_protection_candidates": {
                "LONG": {
                    "source_anchor_id": "A-bull",
                    "source_defense_id": "D-low-0938",
                    "source_time": "2026-08-26T09:38:00+08:00",
                    "source_price": 100.0,
                    "defense_first_seen_at": "2026-08-26T09:40:00+08:00",
                    "atr_buffer_points": 2.0,
                    "stop_price": 98.0,
                },
                "SHORT": None,
            }
        },
    }
    events = program_protection_events(prior, current)
    assert len(events) == 1
    assert events[0]["event_type"] == "PROGRAM_DEFENSE_AVAILABLE"
    assert events[0]["event_time"].endswith("09:40:00+08:00")
    assert events[0]["stop_price"] == 98.0
    assert program_protection_events(current, current) == []


def test_program_action_overlay_replaces_model_wait_with_exact_entry() -> None:
    analysis = {
        "original_decision": "DONT_NOTIFY",
        "message_type": "UNCHANGED",
        "message_direction": "NEUTRAL",
        "notification_reason": "模型想繼續等待。",
        "course_reading": {"setup_stage": "FORMING", "main_strategy": "觀望"},
        "action": {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": None,
            "trigger": "等待。",
            "entry": "不進場。",
            "structural_stop": "尚無。",
            "stop_price": None,
            "obstacles": [],
            "expected_behavior": "等待。",
            "max_wait_bars": None,
            "management": "空手。",
            "reentry_status": "NOT_APPLICABLE",
            "entry_rejection_reason": "NONE",
        },
    }
    gate = {
        "status": "ENTRY_ELIGIBLE",
        "decision_authority": "PROGRAM",
        "required_position_action": "ENTER",
        "setup_key": "long-q4",
        "setup_name": "Q4順勢回檔",
        "direction": "LONG",
        "entry_role": "INITIAL",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 101.0,
        "required_stop_price": 94.0,
        "required_behavior_max_wait_bars": 5,
        "required_behavior_obstacles": [
            {"price": 101.0, "role": "TRIGGER"},
            {"price": 108.0, "role": "CHECKPOINT"},
        ],
    }
    _canonicalize_program_action_envelope(
        analysis,
        ledger={"program_trade_policy": {"actionable_setups": "PROGRAM_ONLY"}},
        position={"status": "FLAT", "pending_entry": None},
        entry_gate=gate,
        preopen=False,
    )
    assert analysis["message_type"] == "PREPARATION"
    assert analysis["action"]["position_action"] == "ENTER"
    assert analysis["action"]["setup_key"] == "long-q4"
    assert analysis["action"]["stop_price"] == 94.0


def test_program_action_overlay_replaces_model_hold_with_behavior_exit() -> None:
    analysis = {
        "original_decision": "DONT_NOTIFY",
        "message_type": "UNCHANGED",
        "message_direction": "NEUTRAL",
        "notification_reason": "模型想續抱。",
        "course_reading": {"setup_stage": "ARMED", "main_strategy": "Q4順勢回檔"},
        "action": {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": "long-q4",
            "trigger": "續抱。",
            "entry": "已持倉。",
            "structural_stop": "94點。",
            "stop_price": None,
            "obstacles": [],
            "expected_behavior": "等待。",
            "max_wait_bars": 5,
            "management": "續抱。",
            "reentry_status": "NOT_APPLICABLE",
            "entry_rejection_reason": "NONE",
        },
    }
    _canonicalize_program_action_envelope(
        analysis,
        ledger={
            "program_trade_policy": {"actionable_setups": "PROGRAM_ONLY"},
            "program_behavior_exit_audit": {
                "status": "TRIGGERED",
                "required_position_action": "EXIT",
            },
        },
        position={"status": "LONG", "active_setup_key": "long-q4"},
        entry_gate=None,
        preopen=False,
    )
    assert analysis["message_type"] == "EXIT"
    assert analysis["action"]["position_action"] == "EXIT"
    assert analysis["action"]["setup_key"] == "long-q4"
    assert analysis["action"]["stop_price"] is None


def test_ai_hybrid_hard_stop_canonicalizes_wrong_card_direction() -> None:
    analysis = {
        "original_decision": "NOTIFY",
        "message_type": "STOP",
        "message_direction": "BEAR",
        "notification_reason": "多單停損。",
        "course_reading": {"setup_stage": "INVALIDATED", "main_strategy": "等待重建"},
        "action": {
            "position_action": "STOP",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": "long-q4",
            "stop_price": 94.0,
        },
    }

    _canonicalize_program_action_envelope(
        analysis,
        ledger={
            "program_trade_policy": {
                "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
                "decision_authority": "AI_HYBRID",
            },
            "protective_stop_audit": {"status": "TRIGGERED", "stop_price": 94.0},
        },
        position={"status": "LONG", "active_setup_key": "long-q4"},
        entry_gate=None,
        preopen=False,
        ai_generated=True,
    )

    assert analysis["message_type"] == "STOP"
    assert analysis["message_direction"] == "BULL"
    assert analysis["action"]["position_action"] == "STOP"
    assert analysis["action"]["setup_key"] == "long-q4"


def test_program_reentry_summary_repairs_setup_and_action_labels() -> None:
    analysis = {
        "action": {
            "position_action": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": "stopped-setup",
            "reentry_status": "USED",
        }
    }
    memory = {
        "active_setups": [
            {"setup_key": "stopped-setup", "reentry_status": "USED"}
        ],
        "reentry": {"status": "USED", "last_stop_at": None, "count": 1},
    }
    position = {
        "status": "FLAT",
        "active_setup_key": "stopped-setup",
        "last_stop_time": "2026-08-21T09:14:00+08:00",
        "reentry_count": 0,
    }
    _canonicalize_program_reentry_summary(
        analysis,
        memory,
        ledger={},
        position=position,
        expected_as_of="2026-08-21T09:22:00+08:00",
    )
    assert memory["reentry"]["status"] == "AVAILABLE"
    assert memory["active_setups"][0]["reentry_status"] == "AVAILABLE"
    assert analysis["action"]["reentry_status"] == "AVAILABLE"


def test_v3_contract_preserves_dual_setups_quadrant_axes_and_compact_upgrade_card() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    event_ref = next(item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE")
    new_events = evidence_events(None, ledger)
    payload = _v3_payload(ledger, as_of=as_of, structure_event_ref=event_ref)
    flat = initial_position_state(as_of=as_of, version=2)
    validated = validate_semantic_envelope(
        payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=flat,
        evidence_events=new_events,
    )
    assert len(validated["memory"]["active_setups"]) == 2
    assert validated["analysis"]["course_reading"]["background_quadrant"] == "Q2"
    assert validated["analysis"]["course_reading"]["working_quadrant"] == "Q1"
    assert validated["analysis"]["action"]["obstacles"][0]["role"] == "CHECKPOINT"

    rendered = render_semantic_replay_event_card(
        validated["analysis"],
        {"expected_latest_closed_k_hhmm": "09:34"},
        stage="day",
        ledger=ledger,
        position_before=flat,
        position_after=flat,
    )
    assert "09:34｜🔄 多方結構升級" in rendered.body
    assert "小級防線09:08低45,025點失守" in rendered.body
    assert "父級09:00低44,861點守住" in rendered.body
    assert "再破09:10高45,154點" in rendered.body
    assert "09:25低44,957點成大級防線候選" in rendered.body
    assert "大錨：09:00低44,861點→09:34高45,188點" in rendered.body
    assert "大級Q2" in rendered.body
    assert "小級Q1" in rendered.body
    assert "3～5根" not in rendered.body  # max_wait_bars is rendered directly as 5根
    assert "三至五根" not in rendered.body
    assert "一之：" not in rendered.body
    assert "左右：" not in rendered.body
    assert "kind=" not in rendered.body and "bar_time=" not in rendered.body
    assert len(rendered.body) < 600
    assert len(rendered.body.splitlines()) <= 12


def test_program_quadrant_baseline_is_separate_by_grade_and_contract_enforces_it() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    bars = _quadrant_baseline_bars()
    structured = _structured(latest_time=as_of, pivots=[])
    structured["latest_closed_k"] = dict(bars[-1])
    structured["indicators"].update({"sma21": 45000, "atr14": 30})
    ledger = build_evidence_ledger(
        structured,
        bars=bars,
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    assert ledger["quadrant_evidence"]["background_20bars"]["recommended_quadrant"] is None
    assert ledger["quadrant_evidence"]["working_5bars"]["recommended_quadrant"] is None

    payload = _v3_payload(ledger, as_of=as_of, structure_event_ref=None)
    flat = initial_position_state(as_of=as_of, version=2)
    validate_semantic_envelope(
        payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=flat,
        evidence_events=[],
    )

    wrong = copy.deepcopy(payload)
    wrong["analysis"]["course_reading"]["background_quadrant"] = "Q1"
    wrong["analysis"]["course_reading"]["background_trend_dynamics"] = "INCREASING"
    wrong["memory"]["structure_control"]["background_quadrant"] = "Q1"
    validate_semantic_envelope(
        wrong, ledger=ledger, expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY", preopen=False,
        position=flat, evidence_events=[],
    )


def test_preparation_card_rounds_indicator_prices_and_stays_scannable() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    analysis = _v3_payload(
        ledger,
        as_of=as_of,
        structure_event_ref=next(
            item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE"
        ),
    )["analysis"]
    analysis["message_type"] = "PREPARATION"
    analysis["action"]["observation_area"] = "回踩21MA 45106.1905與45,154附近的收盤反應。"
    analysis["action"]["trigger"] = "守住45,154後由已收盤1分K重新轉強。"
    flat = initial_position_state(as_of=as_of, version=2)
    rendered = render_semantic_replay_event_card(
        analysis,
        {"expected_latest_closed_k_hhmm": "09:34"},
        stage="day",
        ledger=ledger,
        position_before=flat,
        position_after=flat,
    )
    assert "21MA45,106" in rendered.body
    assert "45106.1905" not in rendered.body
    assert "10.7238" not in rendered.body
    assert "結構：大級" in rendered.body
    assert "小級偏多" in rendered.body
    assert "障礙：檢查點09:10高45,154點" in rendered.body
    assert rendered.body.count("四象限：") + rendered.body.count("道氏：") == 1
    assert len(rendered.body) < 600


def test_armed_flat_setup_cannot_be_rendered_as_generic_observation() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _v3_payload(ledger, as_of=as_of, structure_event_ref=None)
    payload["analysis"]["course_reading"]["setup_stage"] = "ARMED"
    flat = initial_position_state(as_of=as_of, version=2)
    with pytest.raises(SemanticReplayError, match="PREPARATION"):
        validate_semantic_envelope(
            payload,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=flat,
            evidence_events=[],
        )


def test_setup_none_cannot_be_labeled_as_preparation() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _v7_payload(ledger, as_of=as_of)
    payload["analysis"]["message_type"] = "PREPARATION"
    payload["analysis"]["course_reading"]["setup_stage"] = "NONE"
    payload["analysis"]["action"]["setup_key"] = None
    payload["memory"]["active_setups"] = []
    flat = initial_position_state(as_of=as_of, version=2)

    with pytest.raises(SemanticReplayError, match="形成中或NONE只能輸出OBSERVATION"):
        validate_semantic_envelope(
            payload,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=flat,
            evidence_events=[],
        )


def test_public_price_levels_include_known_bar_time_but_indicators_do_not() -> None:
    ledger = {
        "latest_closed_k": {
            "time": "2026-08-25T08:50:00+08:00",
            "open": 44398,
            "high": 44422,
            "low": 44378,
            "close": 44415,
        },
        "pivots": [],
        "working_pivots": [],
        "defenses": [
            {
                "id": "D-bear",
                "direction": "BEAR",
                "bar_time": "2026-08-25T08:48:00+08:00",
                "price": 44462,
            }
        ],
        "opening_ranges": {
            "or5": {
                "start": "2026-08-25T08:45:00+08:00",
                "end": "2026-08-25T08:49:00+08:00",
                "high": 44509,
                "high_time": "2026-08-25T08:45:00+08:00",
                "low": 44372,
                "low_time": "2026-08-25T08:45:00+08:00",
            }
        },
        "anchor_records": [],
        "recent_bar_levels": [
            {
                "time": "2026-08-25T08:48:00+08:00",
                "open": 44420,
                "high": 44462,
                "low": 44410,
                "close": 44430,
            }
        ],
        "legs": [],
    }

    rendered = _v3_annotate_known_levels(
        "收盤突破44,462，再看44,509；跌破44,372則延續。21MA44,462保持原樣。最新收44415。",
        ledger,
    )

    assert "08:48高44,462點" in rendered
    assert "08:45高44,509點" in rendered
    assert "08:45低44,372點" in rendered
    assert "21MA44,462" in rendered
    assert "08:50收44,415點" in rendered


def test_public_level_prefers_active_session_anchor_origin_over_later_same_price() -> None:
    ledger = {
        "anchor_records": [
            {
                "record_type": "ANCHOR",
                "status": "ACTIVE",
                "direction": "BEAR",
                "anchor_origin_kind": "SESSION_OPEN",
                "origin_time": "2026-08-25T08:45:00+08:00",
                "origin_price": 44467,
                "first_extreme_time": "2026-08-25T08:45:00+08:00",
                "first_extreme_price": 44372,
            },
            {
                "record_type": "REVERSE_CANDIDATE",
                "status": "QUALIFIED",
                "direction": "BULL",
                "current_extreme_time": "2026-08-25T08:54:00+08:00",
                "current_extreme_price": 44467,
            },
            {
                "record_type": "WORKING_LEG",
                "status": "FORMING",
                "direction": "BULL",
                "start_time": "2026-08-25T08:50:00+08:00",
                "start_price": 44378,
                "current_extreme_time": "2026-08-25T08:54:00+08:00",
                "current_extreme_price": 44467,
            },
        ],
        "recent_bar_levels": [
            {"time": "2026-08-25T08:54:00+08:00", "high": 44467}
        ],
    }

    assert _v3_annotate_known_levels("空方錨由44467推進", ledger).startswith("空方錨由08:45開44,467點")
    assert _v3_annotate_known_levels("空方錨由08:45的44467推進", ledger).startswith("空方錨由08:45的44,467點")
    working = _v3_annotate_known_levels("目前工作段由44378延伸至44467", ledger)
    assert "08:50低44,378點" in working
    assert "08:54高44,467點" in working


def test_equal_price_endpoints_are_not_assigned_a_guessed_time() -> None:
    ledger = {"anchor_records": [
        {"record_type": "REVERSE_CANDIDATE", "direction": "BULL", "status": "QUALIFIED",
         "current_extreme_time": "2026-08-25T09:14:00+08:00", "current_extreme_price": 44387},
        {"record_type": "WORKING_LEG", "direction": "BULL", "status": "FORMING",
         "start_time": "2026-08-25T09:15:00+08:00", "start_price": 44260,
         "current_extreme_time": "2026-08-25T09:17:00+08:00", "current_extreme_price": 44387},
    ]}
    rendered = _v3_annotate_known_levels("目前工作段延伸中。多方由44233至44387推進，同為2分鐘。", ledger)
    assert "09:17" not in rendered and "09:14" not in rendered
    explicit = _v3_annotate_known_levels("09:14高44387", ledger)
    assert explicit == "09:14高44,387點"
    working = _v3_annotate_known_levels("目前工作段由44260延伸至44387", ledger)
    assert "09:15低44,260點" in working and "09:17高44,387點" in working


def test_explicit_time_and_high_low_role_are_not_annotated_twice() -> None:
    ledger = {
        "recent_bar_levels": [
            {"time": "2026-08-26T09:20:00+08:00", "high": 45135},
            {"time": "2026-08-26T08:45:00+08:00", "low": 45135},
            {"time": "2026-08-26T09:25:00+08:00", "low": 44957},
        ],
        "anchor_records": [],
        "pivots": [],
        "working_pivots": [],
        "defenses": [],
        "legs": [],
        "opening_ranges": {},
    }

    rendered = _v3_annotate_known_levels(
        "小級空方父代為09:20高點45135→09:25低點44957。",
        ledger,
    )

    assert rendered == "小級空方父代為09:20高點45,135點→09:25低點44,957點。"
    assert "08:45" not in rendered
    assert "09:20高點09:20" not in rendered


def test_explicit_close_prefers_recent_close_over_equal_historical_low() -> None:
    ledger = {
        "latest_closed_k": {
            "time": "2026-08-25T10:28:00+08:00",
            "open": 44326,
            "high": 44368,
            "low": 44310,
            "close": 44368,
        },
        "recent_bar_levels": [
            {
                "time": "2026-08-25T10:05:00+08:00",
                "open": 44350,
                "high": 44360,
                "low": 44323,
                "close": 44345,
            },
            {
                "time": "2026-08-25T10:27:00+08:00",
                "open": 44330,
                "high": 44340,
                "low": 44303,
                "close": 44323,
            },
            {
                "time": "2026-08-25T10:28:00+08:00",
                "open": 44326,
                "high": 44368,
                "low": 44310,
                "close": 44368,
            },
        ],
        "anchor_records": [],
        "pivots": [],
        "working_pivots": [],
        "defenses": [],
        "legs": [],
        "opening_ranges": {},
    }

    rendered = _v3_annotate_known_levels(
        "44323收盤跌破44328後，本根即收回至44368。",
        ledger,
    )

    assert "10:27以44,323點收盤" in rendered
    assert "10:05低44,323點" not in rendered
    assert "本根即收回至44,368點" in rendered


def test_latest_ohlc_validator_does_not_compare_spot_bar_with_futures_bar() -> None:
    payload = {
        "market_summary": [
            "期貨本根開44359、高44364、低44334、收44360。",
            "現貨本根收44362.63，與期貨收44360接近。",
        ]
    }

    _validate_latest_ohlc_claims(
        payload,
        latest={"open": 44359.0, "high": 44364.0, "low": 44334.0, "close": 44360.0},
    )

    _validate_latest_ohlc_claims(
        {
            "market_summary": [
                "現貨同步位於前收44,928.76點下方；本根收44,747.63點。",
                "期貨本根收44,727點。",
            ]
        },
        latest={"open": 44689.0, "high": 44750.0, "low": 44658.0, "close": 44727.0},
    )

    payload["market_summary"][0] = "期貨本根收44361。"
    with pytest.raises(ReplayRunError, match="latest_closed_k.close"):
        _validate_latest_ohlc_claims(
            payload,
            latest={"open": 44359.0, "high": 44364.0, "low": 44334.0, "close": 44360.0},
        )


def test_derived_stop_buffer_is_not_assigned_an_incidental_bar_time() -> None:
    ledger = {
        "recent_bar_levels": [
            {"time": "2026-08-25T08:58:00+08:00", "open": 44419, "high": 44424,
             "low": 44393, "close": 44414},
        ],
        "anchor_records": [
            {"record_type": "REVERSE_CANDIDATE", "direction": "BULL", "status": "QUALIFIED",
             "current_extreme_time": "2026-08-25T09:19:00+08:00", "current_extreme_price": 44405},
        ],
    }
    rendered = _v3_annotate_known_levels(
        "完整反彈高點44405外側，參考13.7點緩衝為44418.7。", ledger,
    )
    assert "09:19高44,405點" in rendered
    assert "08:58" not in rendered
    assert "44,419" in rendered


def test_reference_stop_after_atr_buffer_is_not_assigned_an_incidental_bar_time() -> None:
    ledger = {
        "latest_closed_k": {
            "time": "2026-08-25T11:54:00+08:00",
            "open": 44450,
            "high": 44465,
            "low": 44431,
            "close": 44436,
        },
        "recent_bar_levels": [
            {
                "time": "2026-08-25T09:14:00+08:00",
                "open": 44370,
                "high": 44387,
                "low": 44340,
                "close": 44360,
            }
        ],
        "legs": [],
        "pivots": [],
        "working_pivots": [],
        "defenses": [],
    }

    rendered = _v3_annotate_known_levels(
        "以11:52低44,394點外側為失效，ATR14工程緩衝7.1點的參考停損為44,387點。",
        ledger,
    )

    assert "參考停損為44,387點" in rendered
    assert "09:14" not in rendered


def test_management_protective_stop_is_not_assigned_an_incidental_bar_time() -> None:
    ledger = {
        "recent_bar_levels": [
            {
                "time": "2026-08-25T12:26:00+08:00",
                "open": 44721,
                "high": 44733,
                "low": 44688,
                "close": 44713,
            }
        ],
        "legs": [],
        "pivots": [],
        "working_pivots": [],
        "defenses": [],
    }

    rendered = _v3_annotate_known_levels(
        "既有多單續抱，實際保護停損調整為44688，不得放寬。",
        ledger,
    )

    assert "實際保護停損調整為44,688點" in rendered
    assert "12:26" not in rendered

    derived_price = _v3_annotate_known_levels(
        "既有多單續抱，保護價上移至44688並自下一根生效。",
        ledger,
    )
    assert "保護價上移至44,688點" in derived_price
    assert "12:26" not in derived_price

    trailing_price = _v3_annotate_known_levels(
        "維持44688保護價，前高只作檢查點。",
        ledger,
    )
    assert "維持44,688點保護價" in trailing_price
    assert "12:26" not in trailing_price

    touched = _v3_annotate_known_levels(
        "若觸及44688則執行保護停損。",
        ledger,
    )
    assert "觸及44,688點" in touched
    assert "12:26" not in touched

    card_ledger = _v3_presentation_ledger(
        ledger,
        action={"stop_price": 44688.0},
        position_before={"stop_price": 44600.0},
        position_after={"stop_price": 44688.0},
    )
    implicit_stop = _v3_annotate_known_levels(
        "既有多單依低點外側保護，未觸及44688前續抱；若未觸及44688，維持原單。",
        card_ledger,
    )
    assert implicit_stop.count("44,688點") == 2
    assert "12:26" not in implicit_stop


def test_trigger_level_prefers_current_setup_source_over_old_equal_pivot() -> None:
    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": {
                "direction": "SHORT",
                "trigger_level": 44423,
                "trigger_time": "2026-08-25T11:58:00+08:00",
            }
        },
        "pivots": [
            {
                "kind": "HIGH",
                "bar_time": "2026-08-25T10:25:00+08:00",
                "price": 44423,
            }
        ],
        "working_pivots": [],
        "defenses": [],
        "anchor_records": [],
        "legs": [],
        "recent_bar_levels": [],
    }

    rendered = _v3_annotate_known_levels(
        "回落後能否接近44,423點觸發線。",
        ledger,
    )

    assert "11:58低44,423點觸發線" in rendered
    assert "10:25" not in rendered


def test_reverse_candidate_cannot_outrank_unreplaced_opening_anchor() -> None:
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": None,
            "child_anchor": {"direction": "BEAR", "status": "ACTIVE"},
            "reverse_candidate": {
                "direction": "BULL",
                "status": "QUALIFIED",
                "replaces_background": False,
            },
        }
    }
    scenario = {
        "bull_probability": 40,
        "range_probability": 25,
        "bear_probability": 35,
        "bull_plan": "等待接管。",
        "range_plan": "區間。",
        "bear_plan": "原錨延續。",
        "view_change": "完成接管才翻向。",
    }

    normalized = _preserve_active_session_anchor_probability(scenario, ledger=ledger)

    assert normalized == scenario
    assert normalized["bull_probability"] == 40
    assert normalized["bear_probability"] == 35
    assert sum(
        normalized[key]
        for key in ("bull_probability", "range_probability", "bear_probability")
    ) == 100


def test_q4_continuation_level_cannot_be_a_hard_target() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _v3_payload(
        ledger,
        as_of=as_of,
        structure_event_ref=next(
            item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE"
        ),
    )["analysis"]
    payload["course_reading"]["working_quadrant"] = "Q4"
    payload["action"]["obstacles"][0]["price"] = 45188
    payload["action"]["obstacles"][0]["role"] = "HARD_TARGET"
    with pytest.raises(ReplayRunError, match="CHECKPOINT"):
        _validate_v3_obstacles(
            payload,
            ledger=ledger,
            night_summary={"high": 45466, "low": 44700, "close": 45000},
        )


def test_countertrend_observation_may_treat_active_defense_as_hard_target() -> None:
    ledger = {
        "latest_closed_k": {"open": 44890, "high": 44919, "low": 44876, "close": 44892},
        "pivots": [],
        "working_pivots": [],
        "legs": [],
        "defenses": [],
        "structure_events": [],
        "anchor_records": [],
        "recent_bar_levels": [],
        "opening_ranges": {},
        "indicators": {},
        "anchor_lifecycle": {
            "child_anchor": {
                "direction": "BEAR",
                "status": "ACTIVE",
                "defense": {"state": "ACTIVE", "price": 44970},
            },
            "quadrant_context": {"working_direction": "BEAR"},
        },
    }
    payload = {
        "message_direction": "BULL",
        "course_reading": {"working_quadrant": "Q1"},
        "action": {
            "obstacles": [
                {
                    "price": 44970,
                    "role": "HARD_TARGET",
                    "label": "作用中空方防線",
                    "reaction": "收復後才重新評估多方接管。",
                }
            ]
        },
    }

    _validate_v3_obstacles(payload, ledger=ledger, night_summary={})


def test_v3_reentry_requires_prior_stop_same_setup_and_one_complete_bar() -> None:
    position = initial_position_state(as_of="2026-08-26T09:44:00+08:00", version=2)
    action = {
        "notification_reason": "Q4進場。",
        "action": {
            "position_action": "ENTER", "direction": "LONG", "entry_role": "INITIAL",
            "setup_key": "bull-q4", "stop_price": 45152,
        },
    }
    entered = preview_position_transition(
        position, action, as_of="2026-08-26T09:44:00+08:00", latest_close=45222, preopen=False
    )
    stopped_payload = {
        "notification_reason": "結構停損。",
        "action": {"position_action": "STOP", "direction": "NONE"},
    }
    stopped = preview_position_transition(
        entered, stopped_payload, as_of="2026-08-26T09:49:00+08:00", latest_close=45159, preopen=False
    )
    reentry_payload = {
        "notification_reason": "假跌破後收復。",
        "action": {
            "position_action": "ENTER", "direction": "LONG", "entry_role": "REENTRY",
            "setup_key": "bull-q4", "stop_price": 45133,
        },
    }
    with pytest.raises(SemanticReplayError, match="至少等待1根"):
        preview_position_transition(
            stopped, reentry_payload, as_of="2026-08-26T09:49:00+08:00", latest_close=45212, preopen=False
        )
    reentered = preview_position_transition(
        stopped, reentry_payload, as_of="2026-08-26T09:50:00+08:00", latest_close=45212, preopen=False
    )
    assert reentered["status"] == "LONG"
    assert reentered["reentry_count"] == 1


def test_second_stop_uses_true_trigger_time_and_retires_consumed_setup() -> None:
    position = initial_position_state(as_of="2026-08-26T12:48:00+08:00", version=2)
    position.update(
        {
            "status": "LONG",
            "entry_time": "2026-08-26T11:35:00+08:00",
            "entry_price": 45860.0,
            "stop_price": 45894.6,
            "direction": "LONG",
            "active_setup_key": "used-reentry",
            "last_stop_time": "2026-08-26T11:28:00+08:00",
            "reentry_count": 1,
        }
    )
    stopped = preview_position_transition(
        position,
        {
            "notification_reason": "保護停損觸發。",
            "action": {"position_action": "STOP", "direction": "NONE"},
        },
        as_of="2026-08-26T12:50:00+08:00",
        latest_close=45910.0,
        preopen=False,
        protective_stop_audit={
            "status": "TRIGGERED",
            "trigger_time": "2026-08-26T12:49:00+08:00",
        },
    )

    assert stopped["status"] == "FLAT"
    assert stopped["last_stop_time"] == "2026-08-26T12:49:00+08:00"
    assert stopped["active_setup_key"] is None
    assert stopped["reentry_count"] == 1


def _program_reentry_test_payload(
    *,
    setup_key: str,
    action_status: str,
    memory_status: str,
    last_stop_at: str | None,
    count: int,
    event: str = "STOP",
    entry_role: str = "NOT_APPLICABLE",
) -> tuple[dict, dict]:
    analysis = {
        "action": {
            "position_action": event,
            "entry_role": entry_role,
            "setup_key": setup_key,
            "reentry_status": action_status,
        }
    }
    memory = {
        "active_setups": [
            {
                "setup_key": setup_key,
                "reentry_status": memory_status,
            }
        ],
        "reentry": {
            "status": memory_status,
            "last_stop_at": last_stop_at,
            "count": count,
        },
    }
    return analysis, memory


@pytest.mark.parametrize(
    ("as_of", "expected_status"),
    [
        ("2026-08-26T13:25:00+08:00", "WAIT_ONE_BAR"),
        ("2026-08-26T13:26:00+08:00", "AVAILABLE"),
    ],
)
def test_first_program_stop_preserves_unused_reentry_allowance(
    as_of: str,
    expected_status: str,
) -> None:
    setup_key = "initial-long"
    position = initial_position_state(as_of="2026-08-26T13:24:00+08:00", version=2)
    position.update(
        {
            "status": "LONG",
            "direction": "LONG",
            "active_setup_key": setup_key,
            "reentry_count": 0,
        }
    )
    analysis, memory = _program_reentry_test_payload(
        setup_key=setup_key,
        action_status=expected_status,
        memory_status=expected_status,
        last_stop_at="2026-08-26T13:25:00+08:00",
        count=0,
    )

    _validate_program_reentry_state(
        analysis,
        memory,
        ledger={
            "protective_stop_audit": {
                "status": "TRIGGERED",
                "trigger_time": "2026-08-26T13:25:00+08:00",
            }
        },
        position=position,
        expected_as_of=as_of,
    )


def test_missing_program_reentry_setup_is_restored_from_validated_memory() -> None:
    setup_key = "originating-long"
    previous_setup = {
        "setup_key": setup_key,
        "name": "原多方劇本",
        "direction": "LONG",
        "stage": "NO_CHASE",
        "trigger": "須等待全新結構。",
        "stop": "新停損須由新結構產生。",
        "reentry_status": "AVAILABLE",
        "trigger_level": None,
        "trigger_operator": "NONE",
        "valid_bars": 3,
    }
    memory = {
        "active_setups": [
            {
                **previous_setup,
                "setup_key": "current-bear",
                "name": "目前空方劇本",
                "direction": "SHORT",
                "reentry_status": "NOT_APPLICABLE",
            }
        ]
    }

    _restore_program_reentry_setup(
        memory,
        previous_memory={"active_setups": [previous_setup]},
        position={
            "status": "FLAT",
            "active_setup_key": setup_key,
            "last_stop_time": "2026-08-24T09:30:00+08:00",
            "reentry_count": 0,
        },
        expected_as_of="2026-08-24T10:02:00+08:00",
    )

    restored = next(item for item in memory["active_setups"] if item["setup_key"] == setup_key)
    assert restored == previous_setup


def test_stale_aggregate_reentry_summary_is_canonicalized_from_program_state() -> None:
    setup_key = "fresh-short"
    analysis, memory = _program_reentry_test_payload(
        setup_key=setup_key,
        action_status="NOT_APPLICABLE",
        memory_status="NOT_APPLICABLE",
        last_stop_at="2026-08-24T09:30:00+08:00",
        count=0,
        event="ENTER",
        entry_role="INITIAL",
    )
    # Only the aggregate copy is stale.  The chosen setup/action already agree
    # with the runner-owned fresh initial entry.
    memory["reentry"] = {
        "status": "AVAILABLE",
        "last_stop_at": "2026-08-24T09:30:00+08:00",
        "count": 0,
    }

    _canonicalize_program_reentry_summary(
        analysis,
        memory,
        ledger={},
        position={
            "status": "FLAT",
            "active_setup_key": None,
            "last_stop_time": None,
            "reentry_count": 0,
        },
        expected_as_of="2026-08-24T12:42:00+08:00",
    )

    assert memory["reentry"] == {
        "status": "NOT_APPLICABLE",
        "last_stop_at": None,
        "count": 0,
    }
    _validate_program_reentry_state(
        analysis,
        memory,
        ledger={},
        position={
            "status": "FLAT",
            "active_setup_key": None,
            "last_stop_time": None,
            "reentry_count": 0,
        },
        expected_as_of="2026-08-24T12:42:00+08:00",
    )


def test_live_position_origin_setup_is_restored_ahead_of_new_candidates() -> None:
    origin = {
        "setup_key": "origin-short",
        "name": "原空方假突破",
        "direction": "SHORT",
        "stage": "ENTRY_ELIGIBLE",
        "trigger": "收盤跌破44904。",
        "stop": "44954.5。",
        "reentry_status": "NOT_APPLICABLE",
        "trigger_level": 44904.0,
        "trigger_operator": "CLOSE_BELOW",
        "valid_bars": 3,
    }
    memory = {
        "active_setups": [
            {**origin, "setup_key": "new-bull-1", "direction": "LONG"},
            {**origin, "setup_key": "new-bull-2", "direction": "LONG"},
            {**origin, "setup_key": "new-bear-3"},
        ]
    }

    _restore_program_held_setup(
        memory,
        previous_memory={"active_setups": [origin]},
        position={
            "status": "SHORT",
            "active_setup_key": "origin-short",
            "reentry_count": 0,
        },
    )

    assert [item["setup_key"] for item in memory["active_setups"]] == [
        "origin-short",
        "new-bull-1",
        "new-bull-2",
    ]
    _validate_held_setup_continuity(
        memory,
        position={"status": "SHORT", "active_setup_key": "origin-short"},
        analysis={"action": {"position_action": "NONE"}},
    )


def test_armed_setup_cannot_rekey_same_trigger_to_extend_window() -> None:
    prior = {
        "old": {
            "setup_key": "old",
            "direction": "LONG",
            "stage": "ARMED",
            "trigger_level": 45076.0,
        }
    }
    memory = {
        "active_setups": [
            {
                "setup_key": "renamed",
                "direction": "LONG",
                "stage": "ARMED",
                "trigger_level": 45076.0,
            }
        ]
    }

    with pytest.raises(SemanticReplayError, match="更換setup_key"):
        _validate_no_same_trigger_setup_rekey(
            prior,
            memory,
            reading={"structure_event_ref": None},
            ledger={"trade_levels": {"continuation_arm_candidate": None}},
        )

    _validate_no_same_trigger_setup_rekey(
        prior,
        memory,
        reading={"structure_event_ref": "S-new"},
        ledger={"trade_levels": {"continuation_arm_candidate": None}},
    )


def test_first_program_stop_rejects_false_used_reentry_state() -> None:
    setup_key = "initial-long"
    position = initial_position_state(as_of="2026-08-26T13:24:00+08:00", version=2)
    position.update(
        {
            "status": "LONG",
            "direction": "LONG",
            "active_setup_key": setup_key,
            "reentry_count": 0,
        }
    )
    analysis, memory = _program_reentry_test_payload(
        setup_key=setup_key,
        action_status="USED",
        memory_status="USED",
        last_stop_at="2026-08-26T13:25:00+08:00",
        count=1,
    )

    with pytest.raises(SemanticReplayError, match="status=AVAILABLE"):
        _validate_program_reentry_state(
            analysis,
            memory,
            ledger={
                "protective_stop_audit": {
                    "status": "TRIGGERED",
                    "trigger_time": "2026-08-26T13:25:00+08:00",
                }
            },
            position=position,
            expected_as_of="2026-08-26T13:26:00+08:00",
        )


def test_second_program_stop_requires_used_reentry_state() -> None:
    setup_key = "reentered-long"
    position = initial_position_state(as_of="2026-08-26T12:48:00+08:00", version=2)
    position.update(
        {
            "status": "LONG",
            "direction": "LONG",
            "active_setup_key": setup_key,
            "last_stop_time": "2026-08-26T11:28:00+08:00",
            "reentry_count": 1,
        }
    )
    analysis, memory = _program_reentry_test_payload(
        setup_key=setup_key,
        action_status="USED",
        memory_status="USED",
        last_stop_at="2026-08-26T12:49:00+08:00",
        count=1,
    )

    _validate_program_reentry_state(
        analysis,
        memory,
        ledger={
            "protective_stop_audit": {
                "status": "TRIGGERED",
                "trigger_time": "2026-08-26T12:49:00+08:00",
            }
        },
        position=position,
        expected_as_of="2026-08-26T12:50:00+08:00",
    )


def test_new_initial_entry_resets_previous_setup_reentry_memory() -> None:
    setup_key = "new-initial-long"
    position = initial_position_state(as_of="2026-08-26T12:58:00+08:00", version=2)
    position.update(
        {
            "active_setup_key": None,
            "last_stop_time": "2026-08-26T12:49:00+08:00",
            "reentry_count": 1,
        }
    )
    analysis, memory = _program_reentry_test_payload(
        setup_key=setup_key,
        action_status="NOT_APPLICABLE",
        memory_status="NOT_APPLICABLE",
        last_stop_at=None,
        count=0,
        event="ENTER",
        entry_role="INITIAL",
    )

    _validate_program_reentry_state(
        analysis,
        memory,
        ledger={},
        position=position,
        expected_as_of="2026-08-26T13:00:00+08:00",
    )


def test_runner_reconciles_stale_used_memory_for_new_initial_position() -> None:
    setup_key = "new-initial-long"
    memory = {
        "version": 3,
        "active_setups": [
            {"setup_key": setup_key, "stage": "ARMED", "reentry_status": "NOT_APPLICABLE"}
        ],
        "reentry": {
            "status": "USED",
            "last_stop_at": "2026-08-26T12:49:00+08:00",
            "count": 1,
        },
    }
    position = initial_position_state(as_of="2026-08-26T13:24:00+08:00", version=2)
    position.update(
        {
            "status": "LONG",
            "direction": "LONG",
            "active_setup_key": setup_key,
            "last_stop_time": None,
            "reentry_count": 0,
        }
    )

    reconciled = _reconcile_program_reentry_memory(
        memory,
        position=position,
        as_of="2026-08-26T13:26:00+08:00",
    )

    assert reconciled is not None
    assert reconciled["reentry"] == {
        "status": "NOT_APPLICABLE",
        "last_stop_at": None,
        "count": 0,
    }
    assert reconciled["active_setups"][0]["reentry_status"] == "NOT_APPLICABLE"
    assert memory["reentry"]["status"] == "USED"


def test_runner_exposes_program_owned_available_state_for_first_stop_between_samples() -> None:
    position = initial_position_state(as_of="2026-08-26T13:24:00+08:00", version=2)
    position.update(
        {
            "status": "LONG",
            "direction": "LONG",
            "active_setup_key": "initial-long",
            "reentry_count": 0,
        }
    )

    expectation = _program_reentry_expectation(
        position,
        protective_stop_audit={
            "status": "TRIGGERED",
            "trigger_time": "2026-08-26T13:25:00+08:00",
        },
        as_of="2026-08-26T13:26:00+08:00",
    )

    assert expectation == {
        "authority": "PROGRAM_OWNED",
        "required_position_action": "STOP",
        "setup_key": "initial-long",
        "status_after_action": "AVAILABLE",
        "last_stop_at_after_action": "2026-08-26T13:25:00+08:00",
        "count_after_action": 0,
    }


def test_stop_card_uses_program_trigger_bar_time_instead_of_analysis_cutoff() -> None:
    lines = _v3_position_lines(
        "STOP",
        {"status": "LONG", "entry_price": 45921.0},
        {"status": "FLAT"},
        {
            "stop_price": 45860.7,
            "trigger": "13:25低點45,835點穿越保護價。",
            "management": "依既定保護價退出。",
        },
        ledger={
            "latest_closed_k": {
                "time": "2026-08-26T13:26:00+08:00",
                "close": 45848.0,
            },
            "protective_stop_audit": {
                "status": "TRIGGERED",
                "trigger_time": "2026-08-26T13:25:00+08:00",
            },
        },
    )

    assert any("模擬停損：13:25" in line for line in lines)
    assert all("模擬停損：13:26" not in line for line in lines)
    assert any("原因：13:25低點45,835點穿越保護價" in line for line in lines)


def test_compact_public_text_removes_repeated_high_low_point_suffix() -> None:
    assert (
        _compact_public_text("僅在13:25低45,835點低點上方整理。")
        == "僅在13:25低45,835點上方整理。"
    )


def test_annotated_public_text_removes_level_role_repetition() -> None:
    text = _v3_public_text(
        "新增兩根1分K僅在45835低點上方整理。",
        {
            "latest_closed_k": {
                "time": "2026-08-26T13:28:00+08:00",
                "open": 45860.0,
                "high": 45872.0,
                "low": 45846.0,
                "close": 45854.0,
            },
            "recent_bar_levels": [
                {
                    "time": "2026-08-26T13:25:00+08:00",
                    "open": 45849.0,
                    "high": 45872.0,
                    "low": 45835.0,
                    "close": 45850.0,
                }
            ],
        },
        100,
    )

    assert "13:25低45,835點上方" in text
    assert "45,835點低點" not in text


def test_large_aligned_false_break_reentry_cannot_be_denied_for_small_grade_conflict_alone() -> None:
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": {
                "direction": "BULL",
                "status": "ACTIVE",
                "defense": {"state": "ACTIVE", "price": 45784},
            }
        },
        "trade_levels": {
            "latest_false_break_reentry": {
                "direction": "BULL",
                "source_event_id": "false-break-1",
                "reclaimed_level": 45820,
            }
        },
    }
    gate = {"direction": "LONG", "entry_role": "REENTRY"}

    assert _is_large_aligned_false_break_reentry(ledger=ledger, entry_gate=gate)
    assert not _is_large_aligned_false_break_reentry(
        ledger=ledger,
        entry_gate={"direction": "SHORT", "entry_role": "REENTRY"},
    )
    ledger["anchor_lifecycle"]["background_anchor"]["defense"]["state"] = "BROKEN"
    assert not _is_large_aligned_false_break_reentry(ledger=ledger, entry_gate=gate)


@pytest.mark.parametrize(
    ("anchor_direction", "entry_direction"),
    [("BULL", "LONG"), ("BEAR", "SHORT")],
)
def test_entry_aligned_with_highest_active_anchor_is_not_a_grade_conflict(
    anchor_direction: str,
    entry_direction: str,
) -> None:
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": None,
            "child_anchor": {
                "direction": anchor_direction,
                "status": "ACTIVE",
                "defense": {"state": "ACTIVE", "price": 100},
            },
        }
    }
    gate = {"direction": entry_direction, "entry_role": "INITIAL"}

    assert _is_highest_active_anchor_aligned_entry(ledger=ledger, entry_gate=gate)
    opposite = "SHORT" if entry_direction == "LONG" else "LONG"
    assert not _is_highest_active_anchor_aligned_entry(
        ledger=ledger,
        entry_gate={**gate, "direction": opposite},
    )
    ledger["anchor_lifecycle"]["child_anchor"]["defense"]["state"] = "BROKEN"
    assert not _is_highest_active_anchor_aligned_entry(ledger=ledger, entry_gate=gate)


def test_v3_state_allows_only_lawful_post_stop_setup_reactivation() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    event_ref = next(
        item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE"
    )
    payload = _v7_payload(ledger, as_of=as_of, structure_event_ref=event_ref)
    current = payload["memory"]
    current["active_setups"][0].update({"stage": "ARMED", "reentry_status": "AVAILABLE"})
    current["reentry"] = {
        "status": "AVAILABLE",
        "last_stop_at": "2026-08-26T09:33:00+08:00",
        "count": 0,
    }
    previous = copy.deepcopy(current)
    previous["as_of"] = "2026-08-26T09:33:00+08:00"
    previous["active_setups"][0].update(
        {"stage": "INVALIDATED", "reentry_status": "WAIT_ONE_BAR"}
    )
    previous["reentry"] = {
        "status": "WAIT_ONE_BAR",
        "last_stop_at": "2026-08-26T09:33:00+08:00",
        "count": 0,
    }

    _validate_v3_state_transition(
        payload["analysis"],
        current,
        ledger=ledger,
        previous_memory=previous,
        evidence_events=evidence_events(None, ledger),
        expected_as_of=as_of,
    )


def test_v3_state_allows_post_stop_reactivation_when_sparse_analysis_is_already_available() -> None:
    as_of = "2026-08-26T09:35:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured("2026-08-26T09:34:00+08:00"),
        bars=_upgrade_bars(),
        expected_as_of="2026-08-26T09:34:00+08:00",
        session_key="2026-08-26:DAY",
    )
    event_ref = next(
        item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE"
    )
    payload = _v7_payload(ledger, as_of=as_of, structure_event_ref=event_ref)
    current = payload["memory"]
    current["active_setups"][0].update({"stage": "ARMED", "reentry_status": "AVAILABLE"})
    current["reentry"] = {
        "status": "AVAILABLE",
        "last_stop_at": "2026-08-26T09:33:00+08:00",
        "count": 0,
    }
    previous = copy.deepcopy(current)
    previous["as_of"] = "2026-08-26T09:34:00+08:00"
    previous["active_setups"][0].update(
        {"stage": "INVALIDATED", "reentry_status": "AVAILABLE"}
    )

    _validate_v3_state_transition(
        payload["analysis"],
        current,
        ledger=ledger,
        previous_memory=previous,
        evidence_events=evidence_events(None, ledger),
        expected_as_of=as_of,
    )


@pytest.mark.parametrize(
    ("prior_status", "last_stop_at"),
    [
        ("NOT_APPLICABLE", "2026-08-26T09:33:00+08:00"),
        ("WAIT_ONE_BAR", "2026-08-26T09:34:00+08:00"),
    ],
)
def test_v3_state_rejects_non_stop_or_too_early_setup_reactivation(
    prior_status: str,
    last_stop_at: str,
) -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    event_ref = next(
        item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE"
    )
    payload = _v7_payload(ledger, as_of=as_of, structure_event_ref=event_ref)
    current = payload["memory"]
    current["active_setups"][0].update({"stage": "ARMED", "reentry_status": "AVAILABLE"})
    current["reentry"] = {"status": "AVAILABLE", "last_stop_at": last_stop_at, "count": 0}
    previous = copy.deepcopy(current)
    previous["as_of"] = "2026-08-26T09:33:00+08:00"
    previous["active_setups"][0].update(
        {"stage": "INVALIDATED", "reentry_status": prior_status}
    )
    previous["reentry"] = {"status": prior_status, "last_stop_at": last_stop_at, "count": 0}

    with pytest.raises(SemanticReplayError, match="不得重新啟用"):
        _validate_v3_state_transition(
            payload["analysis"],
            current,
            ledger=ledger,
            previous_memory=previous,
            evidence_events=evidence_events(None, ledger),
            expected_as_of=as_of,
        )


def test_v5_manifest_loads_as_isolated_deterministic_contract() -> None:
    dataset = ReplayDataset(
        target_date=date(2026, 8, 27),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(["2026-08-26 15:00", "2026-08-26 15:01"], [100, 101]),
        day_bars=_frame(["2026-08-27 08:45"], [102]),
        source_sha256={"2026-08-26": "a" * 64, "2026-08-27": "b" * 64},
    )
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=V5_MANIFEST, ai_provider="codex", ai_model="fake"),
        analyzer=_SemanticFakeAnalyzer(),
        dataset_loader=lambda *_args, **_kwargs: dataset,
    )
    plan = runner.data_plan(target_date=date(2026, 8, 27))
    assert runner.deterministic_contract is True
    assert runner.full_fidelity is False
    assert runner.rules.contract_version == 3
    assert runner.rules.memory_version == 3
    assert plan["rule_version"] == "course-state-v2.1.8-replay-adapter-v5"


def test_v6_manifest_loads_program_event_contract(tmp_path: Path) -> None:
    dataset = ReplayDataset(
        target_date=date(2026, 8, 27),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(["2026-08-26 15:00", "2026-08-26 15:01"], [100, 101]),
        day_bars=_frame(["2026-08-27 08:45"], [102]),
        source_sha256={"2026-08-26": "a" * 64, "2026-08-27": "b" * 64},
    )
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=V6_MANIFEST, ai_provider="codex", ai_model="fake"),
        analyzer=_SemanticFakeAnalyzer(),
        dataset_loader=lambda *_args, **_kwargs: dataset,
        runtime_root=tmp_path,
    )
    plan = runner.data_plan(target_date=date(2026, 8, 27))
    assert runner.deterministic_contract is True
    assert runner.execution_version == "replay-execution-v9-program-events"
    assert plan["rule_version"] == "course-state-v2.1.8-replay-adapter-v6"
    result = asyncio.run(
        runner.run(
            target_date=date(2026, 8, 27),
            event_driven=True,
            preopen_only=True,
        )
    )
    run_dir = Path(result["run_directory"])
    assert (run_dir / "deterministic-timeline.json").exists()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["event_driven"] is True
    assert manifest["telegram_requested"] is False


def test_course_timeline_cache_is_keyed_and_reused(tmp_path: Path, monkeypatch) -> None:
    dataset = ReplayDataset(
        target_date=date(2026, 8, 27),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(["2026-08-26 15:00"], [100]),
        day_bars=_frame(["2026-08-27 08:45"], [102]),
        source_sha256={"2026-08-27": "a" * 64},
    )
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=V6_MANIFEST, ai_provider="codex", ai_model="fake"),
        analyzer=_SemanticFakeAnalyzer(),
        dataset_loader=lambda *_args, **_kwargs: dataset,
        runtime_root=tmp_path,
    )
    calls = []

    def fake_timeline(*args, **kwargs):
        calls.append(1)
        return [{"bar_time": "2026-08-27T08:45:00+08:00", "events": []}]

    monkeypatch.setattr("trade_monitor_replay.runner.build_deterministic_timeline", fake_timeline)
    first = runner._cached_course_timeline(dataset, target_date=date(2026, 8, 27))
    second = runner._cached_course_timeline(dataset, target_date=date(2026, 8, 27))
    assert first == second
    assert len(calls) == 1
    cache_files = list((tmp_path / "timeline-cache").glob("*.json"))
    assert len(cache_files) == 1
    cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert cached["identity"]["version"] == 2
    assert (
        cached["identity"]["deterministic_timeline_engine_version"]
        == DETERMINISTIC_TIMELINE_ENGINE_VERSION
    )
    assert (
        cached["identity"]["deterministic_timeline_engine_sha256"]
        == DETERMINISTIC_TIMELINE_ENGINE_SHA256
    )


def test_prompt_view_compacts_prior_session_and_consumed_event_history() -> None:
    reference = {
        "version": 1,
        "as_of": "2026-08-21T04:59:00+08:00",
        "session_key": "reference",
        "background_anchor": {"id": "A-night"},
        "quadrant_context": {
            "authority": "EVIDENCE_ONLY",
            "reference_anchor_id": "A-night",
            "reference_grade": "LARGE",
            "anchor_direction": "BEAR",
            "phase": "PULLBACK",
            "same_grade_comparisons": [{"large": "payload"}],
        },
        "taiji_context": {
            "dynasty_anchor_ref": "A-night",
            "parent_direction": "BEAR",
            "leg_evidence": [{"large": "payload"}],
        },
        "events": [{"large": "history"}],
    }
    ledger = {
        "recent_bar_levels": [{"time": index} for index in range(30)],
        "reference_anchor_lifecycle": reference,
        "anchor_lifecycle": {"current": "kept in full"},
    }
    compact = _prompt_ledger_view(ledger)
    assert len(compact["recent_bar_levels"]) == 15
    assert "same_grade_comparisons" not in compact["reference_anchor_lifecycle"]["quadrant_context"]
    assert "leg_evidence" not in compact["reference_anchor_lifecycle"]["taiji_context"]
    assert compact["anchor_lifecycle"] == ledger["anchor_lifecycle"]

    lifecycle = _prompt_event_lifecycle_view(
        {
            "version": 1,
            "as_of": "2026-08-21T09:00:00+08:00",
            "events": [
                {"event_id": "active", "status": "ACTIVE", "analyzed": False},
                {"event_id": "old", "status": "ANALYZED", "analyzed": True},
            ],
        }
    )
    assert [item["event_id"] for item in lifecycle["events"]] == ["active"]
    assert lifecycle["historical_event_count"] == 2


def test_v7_manifest_loads_entry_gate_and_course_audit_contract() -> None:
    dataset = ReplayDataset(
        target_date=date(2026, 8, 27),
        instrument="TMF",
        expiry_month="202609",
        night_bars=_frame(["2026-08-26 15:00", "2026-08-26 15:01"], [100, 101]),
        day_bars=_frame(["2026-08-27 08:45"], [102]),
        source_sha256={"2026-08-26": "a" * 64, "2026-08-27": "b" * 64},
    )
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=V7_MANIFEST, ai_provider="codex", ai_model="fake"),
        analyzer=_SemanticFakeAnalyzer(),
        dataset_loader=lambda *_args, **_kwargs: dataset,
    )
    plan = runner.data_plan(target_date=date(2026, 8, 27))
    assert runner.deterministic_contract is True
    assert runner.execution_gate_contract is True
    assert runner.execution_version == "replay-execution-v10-entry-gate"
    assert runner.rules.contract_version == 4
    assert runner.rules.memory_version == 3
    assert plan["rule_version"] == "course-state-v2.1.8-replay-adapter-v7"


def test_v7_entry_eligible_requires_enter_or_fixed_denial_and_renders_cclass_x() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _v7_payload(ledger, as_of=as_of)
    payload["analysis"]["message_type"] = "PREPARATION"
    payload["analysis"]["course_reading"]["setup_stage"] = "ENTRY_ELIGIBLE"
    payload["analysis"]["action"].update(
        {
            "position_action": "ENTER",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "setup_key": "bull-q4-0900",
            "stop_price": 44947,
            "entry_rejection_reason": "NONE",
        }
    )
    payload["memory"]["active_setups"][0]["stage"] = "ENTRY_ELIGIBLE"
    gate = {
        "status": "ENTRY_ELIGIBLE",
        "event_id": "EG-test",
        "setup_key": "bull-q4-0900",
        "setup_name": "Q4順勢回檔",
        "direction": "LONG",
        "entry_role": "INITIAL",
        "signal_time": as_of,
        "signal_close": 45187,
        "eligible_from": "2026-08-26T09:35:00+08:00",
        "expires_at": "2026-08-26T09:37:00+08:00",
        "valid_bars": 3,
    }
    flat = initial_position_state(as_of=as_of, version=2)
    validated = validate_semantic_envelope(
        payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=flat,
        evidence_events=[],
        entry_gate=gate,
    )
    rendered = render_semantic_replay_event_card(
        validated["analysis"],
        {"expected_latest_closed_k_hhmm": "09:34"},
        stage="day",
        ledger=ledger,
        position_before=flat,
        position_after=flat,
    )
    assert "多方進場資格" in rendered.body
    assert "決策：接受訊號" in rendered.body
    assert "C班：" not in rendered.body
    assert "太極：" in rendered.body
    assert "一之：" in rendered.body
    assert "X流程：" in rendered.body

    mislabeled_signal = copy.deepcopy(payload)
    mislabeled_signal["analysis"]["message_type"] = "ENTRY"
    normalized = validate_semantic_envelope(
        mislabeled_signal,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=flat,
        evidence_events=[],
        entry_gate=gate,
    )
    assert normalized["analysis"]["message_type"] == "PREPARATION"
    assert normalized["analysis"]["action"]["position_action"] == "ENTER"

    program_gate = {
        **gate,
        "decision_authority": "PROGRAM",
        "required_position_action": "ENTER",
        "required_stop_price": 44947.0,
        "required_entry_rejection_reason": "NONE",
        "required_behavior_max_wait_bars": 5,
        "required_behavior_obstacles": [
            {"price": 45154.0, "role": "CHECKPOINT"}
        ],
    }
    program_denial = copy.deepcopy(payload)
    program_denial["analysis"]["action"].update(
        {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "stop_price": None,
            "entry_rejection_reason": "HARD_OBSTACLE_TOO_CLOSE",
        }
    )
    with pytest.raises(SemanticReplayError, match="AI只能解釋"):
        validate_semantic_envelope(
            program_denial,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=flat,
            evidence_events=[],
            entry_gate=program_gate,
        )

    moved_program_stop = copy.deepcopy(payload)
    moved_program_stop["analysis"]["action"]["stop_price"] = 44948
    with pytest.raises(SemanticReplayError, match="不得自行移動"):
        validate_semantic_envelope(
            moved_program_stop,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=flat,
            evidence_events=[],
            entry_gate=program_gate,
        )

    moved_program_wait = copy.deepcopy(payload)
    moved_program_wait["analysis"]["action"]["max_wait_bars"] = 3
    with pytest.raises(SemanticReplayError, match="不得自行選擇"):
        validate_semantic_envelope(
            moved_program_wait,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=flat,
            evidence_events=[],
            entry_gate=program_gate,
        )

    moved_program_checkpoint = copy.deepcopy(payload)
    moved_program_checkpoint["analysis"]["action"]["obstacles"][0]["price"] = 45160
    with pytest.raises(SemanticReplayError, match="逐項沿用"):
        validate_semantic_envelope(
            moved_program_checkpoint,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=flat,
            evidence_events=[],
            entry_gate=program_gate,
        )

    rejected = copy.deepcopy(payload)
    rejected["analysis"]["action"].update(
        {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "stop_price": None,
            "entry_rejection_reason": "NONE",
        }
    )
    with pytest.raises(SemanticReplayError, match="固定否決原因"):
        validate_semantic_envelope(
            rejected,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=flat,
            evidence_events=[],
            entry_gate=gate,
        )

    aligned_rejected = copy.deepcopy(payload)
    aligned_rejected["analysis"]["action"].update(
        {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "stop_price": None,
            "entry_rejection_reason": "GRADE_CONFLICT",
        }
    )
    aligned_ledger = copy.deepcopy(ledger)
    aligned_ledger["anchor_lifecycle"] = {
        "background_anchor": {
            "id": "ANCHOR-active-bull",
            "direction": "BULL",
            "status": "ACTIVE",
            "defense": {"state": "ACTIVE", "price": 44947},
        },
        "child_anchor": None,
    }
    with pytest.raises(SemanticReplayError, match="最高作用中控制錨同向"):
        validate_semantic_envelope(
            aligned_rejected,
            ledger=aligned_ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=flat,
            evidence_events=[],
            entry_gate=gate,
        )


def test_v7_entry_eligible_card_has_priority_over_simultaneous_structure_upgrade() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    event_ref = next(
        item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE"
    )
    payload = _v7_payload(ledger, as_of=as_of, structure_event_ref=event_ref)
    payload["analysis"]["message_type"] = "STRUCTURE_UPGRADE"
    payload["analysis"]["course_reading"]["setup_stage"] = "ENTRY_ELIGIBLE"
    payload["analysis"]["action"].update(
        {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": "bull-q4-0900",
            "stop_price": None,
            "entry_rejection_reason": "STOP_TOO_WIDE",
        }
    )
    payload["memory"]["active_setups"][0].update(
        {"setup_key": "bull-q4-0900", "stage": "ENTRY_ELIGIBLE"}
    )
    gate = {
        "status": "ENTRY_ELIGIBLE",
        "event_id": "EG-upgrade",
        "setup_key": "bull-q4-0900",
        "setup_name": "Q4順勢回檔",
        "direction": "LONG",
        "entry_role": "INITIAL",
        "signal_time": as_of,
        "signal_close": 45187,
        "eligible_from": "2026-08-26T09:35:00+08:00",
        "expires_at": "2026-08-26T09:37:00+08:00",
        "valid_bars": 3,
    }

    with pytest.raises(SemanticReplayError, match="事前數值風險上限"):
        validate_semantic_envelope(
            payload,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=initial_position_state(as_of=as_of, version=2),
            evidence_events=evidence_events(None, ledger),
            entry_gate=gate,
        )

    gate["risk_limit"] = {"max_risk_amount": 1200}
    validated = validate_semantic_envelope(
        payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=initial_position_state(as_of=as_of, version=2),
        evidence_events=evidence_events(None, ledger),
        entry_gate=gate,
    )

    assert validated["analysis"]["message_type"] == "PREPARATION"
    assert validated["analysis"]["course_reading"]["structure_event_ref"] == event_ref
    assert validated["analysis"]["action"]["entry_rejection_reason"] == "STOP_TOO_WIDE"


def test_v7_open_position_keeps_grade_upgrade_as_management_event() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    event_ref = next(
        item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE"
    )
    payload = _v7_payload(ledger, as_of=as_of, structure_event_ref=event_ref)
    payload["analysis"]["message_type"] = "STRUCTURE_UPGRADE"
    payload["analysis"]["course_reading"]["setup_stage"] = "CONSERVATIVE_CONFIRMED"
    payload["analysis"]["action"].update(
        {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": "bull-q4-0900",
            "stop_price": 44947,
            "entry_rejection_reason": "NONE",
        }
    )
    payload["memory"]["active_setups"][0]["stage"] = "CONSERVATIVE_CONFIRMED"
    position = initial_position_state(as_of=as_of, version=2)
    position.update(
        {
            "status": "LONG",
            "direction": "LONG",
            "entry_time": "2026-08-26T09:33:00+08:00",
            "entry_price": 45150.0,
            "stop_price": 44947.0,
            "active_setup_key": "bull-q4-0900",
        }
    )

    validated = validate_semantic_envelope(
        payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=position,
        evidence_events=evidence_events(None, ledger),
        entry_gate={"status": "NONE"},
    )

    analysis = validated["analysis"]
    assert analysis["message_type"] == "MANAGEMENT"
    assert analysis["course_reading"]["structure_event_ref"] == event_ref
    assert analysis["course_reading"]["controlling_grade"] == "LARGE"


def test_v7_fixed_entry_denial_normalizes_labels_and_consumes_old_trigger() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _v7_payload(ledger, as_of=as_of)
    payload["analysis"]["message_type"] = "PREPARATION"
    payload["analysis"]["course_reading"]["setup_stage"] = "ENTRY_ELIGIBLE"
    payload["analysis"]["action"].update(
        {
            "position_action": "NONE",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "setup_key": "bull-q4-0900",
            "stop_price": None,
            "entry_rejection_reason": "CONSTITUTION_BLOCKED",
        }
    )
    payload["memory"]["active_setups"][0]["stage"] = "ENTRY_ELIGIBLE"
    gate = {
        "status": "ENTRY_ELIGIBLE",
        "event_id": "EG-denied",
        "setup_key": "bull-q4-0900",
        "setup_name": "Q4順勢回檔",
        "direction": "LONG",
        "entry_role": "INITIAL",
        "signal_time": as_of,
        "signal_close": 45187,
        "eligible_from": "2026-08-26T09:35:00+08:00",
        "expires_at": "2026-08-26T09:37:00+08:00",
        "valid_bars": 3,
    }

    validated = validate_semantic_envelope(
        payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=initial_position_state(as_of=as_of, version=2),
        evidence_events=[],
        entry_gate=gate,
    )

    action = validated["analysis"]["action"]
    assert action["position_action"] == "NONE"
    assert action["direction"] == "NONE"
    assert action["entry_role"] == "NOT_APPLICABLE"
    assert action["entry_rejection_reason"] == "CONSTITUTION_BLOCKED"
    consumed = next(
        item
        for item in validated["memory"]["active_setups"]
        if item["setup_key"] == "bull-q4-0900"
    )
    assert consumed["stage"] == "NO_CHASE"
    assert "否決並消費" in consumed["trigger"]
    untouched = next(
        item
        for item in validated["memory"]["active_setups"]
        if item["setup_key"] == "bear-break-0845"
    )
    assert untouched["stage"] == "FORMING"


def test_long_only_ai_hybrid_entry_denial_has_causal_event_index_available() -> None:
    """A valid AI denial must not crash before structure-event validation."""

    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    ledger["program_trade_policy"] = {
        "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
        "decision_authority": "AI_HYBRID",
        "trade_direction_policy": "LONG_ONLY",
    }
    payload = _v7_payload(ledger, as_of=as_of)
    payload["analysis"]["message_type"] = "PREPARATION"
    payload["analysis"]["course_reading"]["setup_stage"] = "ENTRY_ELIGIBLE"
    payload["analysis"]["action"].update(
        {
            "position_action": "NONE",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "setup_key": "bull-q4-0900",
            "stop_price": None,
            "entry_rejection_reason": "CONSTITUTION_BLOCKED",
        }
    )
    payload["memory"]["active_setups"] = [payload["memory"]["active_setups"][0]]
    payload["memory"]["active_setups"][0]["stage"] = "ENTRY_ELIGIBLE"
    gate = {
        "status": "ENTRY_ELIGIBLE",
        "event_id": "EG-long-only-denied",
        "setup_key": "bull-q4-0900",
        "setup_name": "Q4順勢回檔",
        "direction": "LONG",
        "entry_role": "INITIAL",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 45154,
        "signal_time": as_of,
        "signal_close": 45187,
        "eligible_from": "2026-08-26T09:35:00+08:00",
        "expires_at": "2026-08-26T09:37:00+08:00",
        "valid_bars": 3,
        "decision_authority": "AI_HYBRID",
    }

    validated = validate_semantic_envelope(
        payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=initial_position_state(as_of=as_of, version=2),
        evidence_events=[],
        entry_gate=gate,
    )

    assert validated["analysis"]["action"]["entry_rejection_reason"] == "CONSTITUTION_BLOCKED"


def test_v7_normalizes_redundant_position_direction_on_management_noop() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _v7_payload(ledger, as_of=as_of)
    payload["analysis"]["message_type"] = "MANAGEMENT"
    payload["analysis"]["message_direction"] = "BULL"
    payload["analysis"]["course_reading"]["setup_stage"] = "CONSERVATIVE_CONFIRMED"
    payload["analysis"]["action"].update(
        {
            "position_action": "NONE",
            "direction": "LONG",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": "bull-q4-0900",
            "stop_price": 44947,
            "entry_rejection_reason": "NONE",
        }
    )
    payload["memory"]["active_setups"][0]["stage"] = "CONSERVATIVE_CONFIRMED"
    position = initial_position_state(as_of=as_of, version=2)
    position.update(
        {
            "status": "LONG",
            "direction": "LONG",
            "entry_time": "2026-08-26T09:33:00+08:00",
            "entry_price": 45150.0,
            "stop_price": 44947.0,
            "active_setup_key": "bull-q4-0900",
        }
    )

    validated = validate_semantic_envelope(
        payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=position,
        evidence_events=[],
        entry_gate={"status": "NONE"},
    )

    assert validated["analysis"]["message_type"] == "MANAGEMENT"
    assert validated["analysis"]["action"]["direction"] == "NONE"

    exit_payload = copy.deepcopy(payload)
    exit_payload["analysis"]["message_type"] = "EXIT"
    exit_payload["analysis"]["action"]["position_action"] = "EXIT"
    exited = validate_semantic_envelope(
        exit_payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=position,
        evidence_events=[],
        entry_gate={"status": "NONE"},
    )
    assert exited["analysis"]["action"]["direction"] == "NONE"

    failed_behavior_ledger = copy.deepcopy(ledger)
    failed_behavior_ledger["position_behavior_audit"] = {
        "status": "FAILED_HOLD",
        "checkpoint_price": 45200.0,
    }
    with pytest.raises(SemanticReplayError, match="應有行為已失效"):
        validate_semantic_envelope(
            payload,
            ledger=failed_behavior_ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=position,
            evidence_events=[],
            entry_gate={"status": "NONE"},
        )


def test_v7_normalizes_redundant_setup_direction_on_preparation_noop() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _v7_payload(ledger, as_of=as_of)
    payload["analysis"]["message_type"] = "PREPARATION"
    payload["analysis"]["course_reading"]["setup_stage"] = "ARMED"
    payload["analysis"]["action"].update(
        {
            "position_action": "NONE",
            "direction": "LONG",
            "entry_role": "REENTRY",
            "setup_key": "bull-q4-0900",
            "stop_price": None,
            "entry_rejection_reason": "NONE",
        }
    )
    payload["memory"]["active_setups"][0]["stage"] = "ARMED"

    validated = validate_semantic_envelope(
        payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=initial_position_state(as_of=as_of, version=2),
        evidence_events=[],
        entry_gate=None,
    )

    assert validated["analysis"]["action"]["position_action"] == "NONE"
    assert validated["analysis"]["action"]["direction"] == "NONE"
    assert validated["analysis"]["action"]["entry_role"] == "NOT_APPLICABLE"


def test_countertrend_setup_cannot_arm_before_highest_active_anchor_defense_breaks() -> None:
    analysis = {
        "message_direction": "BULL",
        "course_reading": {"setup_stage": "ARMED"},
    }
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": None,
            "child_anchor": {
                "direction": "BEAR",
                "status": "ACTIVE",
                "defense": {"state": "ACTIVE", "price": 44970},
            },
        }
    }

    with pytest.raises(SemanticReplayError, match="Type1"):
        _validate_countertrend_preparation_maturity(analysis, ledger=ledger)
    assert _is_countertrend_to_highest_active_anchor("BULL", ledger=ledger)


def test_background_aligned_setup_may_arm_against_temporary_child_anchor() -> None:
    analysis = {
        "message_direction": "BULL",
        "course_reading": {"setup_stage": "ARMED"},
    }
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": {
                "direction": "BULL",
                "status": "ACTIVE",
                "defense": {"state": "ACTIVE", "price": 44861},
            },
            "child_anchor": {
                "direction": "BEAR",
                "status": "ACTIVE",
                "defense": {"state": "ACTIVE", "price": 45162},
            },
        }
    }

    _validate_countertrend_preparation_maturity(analysis, ledger=ledger)
    assert not _is_countertrend_to_highest_active_anchor("BULL", ledger=ledger)


def test_management_title_uses_position_direction_over_opposite_structure_event() -> None:
    payload = {
        "message_direction": "BULL",
        "course_reading": {"structure_event_ref": "S-bear-invalidated"},
    }
    ledger = {
        "structure_events": [
            {"id": "S-bear-invalidated", "direction": "BEAR", "event_type": "STRUCTURE_INVALIDATED"}
        ]
    }

    assert _v3_event_title(
        payload,
        "MANAGEMENT",
        ledger=ledger,
        position_before={"status": "LONG"},
    ) == "🟠 多方持倉管理"


def test_invalidation_title_uses_sole_prior_actionable_setup_when_key_is_omitted() -> None:
    payload = {
        "message_direction": "BEAR",
        "course_reading": {"structure_event_ref": None},
        "action": {"setup_key": None},
    }
    previous_memory = {
        "active_setups": [
            {"setup_key": "bull-false-break", "direction": "LONG", "stage": "ARMED"},
            {"setup_key": "bear-forming", "direction": "SHORT", "stage": "FORMING"},
        ]
    }

    assert _v3_event_title(
        payload,
        "INVALIDATION",
        ledger={"structure_events": []},
        previous_memory=previous_memory,
    ) == "⚠️ 多方候選失效"


def test_v7_normalizes_stale_but_valid_duplicate_structure_event_memory_ref() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    event_ref = next(
        item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE"
    )
    payload = _v7_payload(ledger, as_of=as_of, structure_event_ref=event_ref)
    current_events = evidence_events(None, ledger)
    stale_ref = "S-stale-valid-ledger-event"
    ledger["structure_events"].append(
        {"id": stale_ref, "event_type": "FALSE_BREAK_RECLAIM", "direction": "BULL"}
    )
    payload["memory"]["structure_control"]["last_structure_event_ref"] = stale_ref

    validated = validate_semantic_envelope(
        payload,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-26:DAY",
        preopen=False,
        position=initial_position_state(as_of=as_of, version=2),
        evidence_events=current_events,
        entry_gate={"status": "NONE"},
    )

    assert validated["memory"]["structure_control"]["last_structure_event_ref"] == event_ref


def test_structure_event_title_uses_structure_direction_not_model_guess() -> None:
    downgrade_ledger = {
        "structure_events": [
            {"id": "S-down", "event_type": "GRADE_DOWNGRADE", "direction": "BEAR"}
        ]
    }
    payload = {
        "message_direction": "BULL",
        "course_reading": {"structure_event_ref": "S-down", "setup_stage": "INVALIDATED"},
    }
    assert _v3_event_title(payload, "STRUCTURE_DOWNGRADE", ledger=downgrade_ledger) == "🔄 空方大級結構降級"

    invalid_ledger = {
        "structure_events": [
            {"id": "S-invalid", "event_type": "STRUCTURE_INVALIDATED", "direction": "BEAR"}
        ]
    }
    payload["course_reading"]["structure_event_ref"] = "S-invalid"
    assert _v3_event_title(payload, "INVALIDATION", ledger=invalid_ledger) == "⚠️ 空方升級結構失效"

    setup_payload = {
        "message_direction": "BULL",
        "course_reading": {"structure_event_ref": None, "setup_stage": "FORMING"},
        "action": {"setup_key": "short-pullback"},
    }
    prior_memory = {
        "active_setups": [
            {"setup_key": "short-pullback", "direction": "SHORT", "stage": "ARMED"}
        ]
    }
    assert _v3_event_title(
        setup_payload,
        "INVALIDATION",
        ledger={"structure_events": []},
        previous_memory=prior_memory,
    ) == "⚠️ 空方候選失效"


def test_structure_invalidation_line_names_the_invalidated_direction() -> None:
    from trade_monitor_replay.presentation import _v3_structure_event_line

    ledger = {
        "pivots": [
            {"id": "P-old-high", "kind": "HIGH", "bar_time": "2026-08-25T09:54:00+08:00", "price": 44492.0}
        ],
        "defenses": [],
        "structure_events": [
            {
                "id": "S-old-bear-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BEAR",
                "parent_origin_pivot_id": "P-old-high",
            },
            {
                "id": "S-old-bear-invalid",
                "event_type": "STRUCTURE_INVALIDATED",
                "direction": "BEAR",
                "source_upgrade_event_id": "S-old-bear-upgrade",
                "broken_parent_price": 44492.0,
            },
        ],
    }

    assert _v3_structure_event_line(
        {"structure_event_ref": "S-old-bear-invalid"},
        ledger,
    ) == "失效：原空方升級結構的父級起點09:54高44,492點已被收盤破壞。"


@pytest.mark.parametrize(
    ("direction", "kind", "label", "expected"),
    [
        (
            "BULL", "LOW", "P-low", "假跌破收復：11:01低45,784點於11:02跌破，11:03收復；掃低45,770點（1根）。",
        ),
        (
            "BEAR", "HIGH", "P-high", "假突破收回：11:09高45,877點於11:14向上突破，11:15收回其下；掃高45,893點（1根）。",
        ),
    ],
)
def test_false_break_line_uses_directional_break_and_reclaim_words(
    direction: str,
    kind: str,
    label: str,
    expected: str,
) -> None:
    from trade_monitor_replay.presentation import _v3_structure_event_line

    pivot_time = "2026-08-26T11:01:00+08:00" if kind == "LOW" else "2026-08-26T11:09:00+08:00"
    level = 45784.0 if kind == "LOW" else 45877.0
    breach = "2026-08-26T11:02:00+08:00" if direction == "BULL" else "2026-08-26T11:14:00+08:00"
    seen = "2026-08-26T11:03:00+08:00" if direction == "BULL" else "2026-08-26T11:15:00+08:00"
    extreme = 45770.0 if direction == "BULL" else 45893.0
    event_id = f"S-{direction.lower()}"
    ledger = {
        "pivots": [
            {"id": label, "kind": kind, "bar_time": pivot_time, "price": level}
        ],
        "defenses": [],
        "structure_events": [
            {
                "id": event_id,
                "event_type": "FALSE_BREAK_RECLAIM",
                "direction": direction,
                "source_type": f"PIVOT_{kind}",
                "source_id": label,
                "level_price": level,
                "breach_time": breach,
                "first_seen_at": seen,
                "breach_extreme": extreme,
                "bars_to_reclaim": 1,
            }
        ],
    }

    assert _v3_structure_event_line({"structure_event_ref": event_id}, ledger) == expected


def test_v3_obstacle_must_be_a_revealed_structure_level() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    payload = _v3_payload(
        ledger,
        as_of=as_of,
        structure_event_ref=next(
            item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE"
        ),
    )["analysis"]
    _validate_v3_obstacles(
        payload,
        ledger=ledger,
        night_summary={"high": 45466, "low": 44700, "close": 45000},
    )
    payload["action"]["obstacles"][0]["price"] = 45232
    with pytest.raises(ReplayRunError, match="障礙價位"):
        _validate_v3_obstacles(
            payload,
            ledger=ledger,
            night_summary={"high": 45466, "low": 44700, "close": 45000},
        )


def test_v3_obstacle_accepts_program_derived_position_protection_price() -> None:
    payload = {
        "message_direction": "BEAR",
        "course_reading": {"working_quadrant": "TRANSITION"},
        "action": {
            "obstacles": [
                {
                    "price": 45813.8,
                    "role": "TRIGGER",
                    "label": "既有保護停損",
                    "reaction": "觸及即執行。",
                },
                {
                    "price": 45863.0,
                    "role": "TRIGGER",
                    "label": "既有進場觸發線",
                    "reaction": "只沿用原交易計畫。",
                },
            ]
        },
    }
    ledger = {
        "latest_closed_k": {"open": 45815, "high": 45823, "low": 45808, "close": 45814},
        "trade_levels": {
            "position_protection_candidates": {
                "LONG": {"source_price": 45784.0, "stop_price": 45778.2},
                "SHORT": None,
            }
        },
    }

    _validate_v3_obstacles(
        payload,
        ledger=ledger,
        night_summary={},
        position={
            "status": "LONG",
            "entry_price": 45860.0,
            "stop_price": 45813.8,
            "behavior_plan": {
                "trigger_level": 45863.0,
                "obstacles": [{"price": 45911.0, "role": "CHECKPOINT"}],
            },
        },
    )


def test_v3_obstacle_accepts_only_the_exposed_program_behavior_checkpoint() -> None:
    payload = {
        "message_direction": "BULL",
        "course_reading": {"working_quadrant": "Q1"},
        "action": {
            "obstacles": [
                {
                    "price": 44972.5,
                    "role": "CHECKPOINT",
                    "label": "Q1早期動能檢查",
                    "reaction": "在3根行為窗內未達時退出。",
                }
            ]
        },
    }
    ledger = {
        "latest_closed_k": {"open": 44955, "high": 44964, "low": 44891, "close": 44901},
        "position_behavior_audit": {
            "status": "ACTIVE",
            "entry_price": 44955.0,
            "stop_price": 44885.0,
            "trigger_level": 44899.0,
            "checkpoint_price": 44972.5,
            "hold_price": None,
        },
    }

    _validate_v3_obstacles(payload, ledger=ledger, night_summary={})

    payload["action"]["obstacles"][0]["price"] = 44973.0
    with pytest.raises(ReplayRunError, match="障礙價位"):
        _validate_v3_obstacles(payload, ledger=ledger, night_summary={})


def test_v3_obstacle_accepts_a_revealed_working_pivot() -> None:
    ledger = {
        "latest_closed_k": {"open": 12000, "high": 12010, "low": 11990, "close": 12005},
        "pivots": [],
        "working_pivots": [{"id": "WP-1", "kind": "HIGH", "price": 12345}],
        "legs": [],
        "defenses": [],
        "structure_events": [],
        "opening_ranges": {},
        "indicators": {},
    }
    payload = {
        "message_direction": "NEUTRAL",
        "course_reading": {"working_quadrant": "Q3"},
        "action": {
            "obstacles": [
                {"price": 12345, "role": "CHECKPOINT", "label": "形成中高點", "reaction": "觀察"}
            ]
        },
    }

    _validate_v3_obstacles(
        payload,
        ledger=ledger,
        night_summary={"high": 45466, "low": 44700, "close": 45000},
    )


def test_v3_obstacle_accepts_course_lifecycle_dow_defense() -> None:
    ledger = {
        "latest_closed_k": {"open": 45250, "high": 45255, "low": 45222, "close": 45229},
        "pivots": [],
        "working_pivots": [],
        "legs": [],
        "defenses": [],
        "structure_events": [],
        "anchor_records": [],
        "recent_bar_levels": [],
        "opening_ranges": {},
        "indicators": {},
        "anchor_lifecycle": {
            "dow_context": {
                "small_bull_defense": {
                    "time": "2026-08-24T09:14:00+08:00",
                    "price": 45133.0,
                    "state": "ACTIVE",
                }
            }
        },
    }
    payload = {
        "message_direction": "NEUTRAL",
        "course_reading": {"working_quadrant": "TRANSITION"},
        "action": {
            "obstacles": [
                {
                    "price": 45133.0,
                    "role": "TRIGGER",
                    "label": "小級多頭防線",
                    "reaction": "收破後取消多方延續。",
                }
            ]
        },
    }

    _validate_v3_obstacles(payload, ledger=ledger, night_summary={})


def test_v3_obstacle_accepts_revealed_prior_night_open_as_checkpoint() -> None:
    ledger = {
        "latest_closed_k": {"open": 44520, "high": 44558, "low": 44505, "close": 44556},
        "pivots": [],
        "working_pivots": [],
        "legs": [],
        "defenses": [],
        "structure_events": [],
        "anchor_records": [],
        "recent_bar_levels": [],
        "opening_ranges": {},
        "indicators": {},
    }
    payload = {
        "message_direction": "BULL",
        "course_reading": {"working_quadrant": "Q1"},
        "action": {
            "obstacles": [
                {
                    "price": 44687,
                    "role": "CHECKPOINT",
                    "label": "前一夜盤開盤",
                    "reaction": "到達後依已收盤K反應管理。",
                }
            ]
        },
    }

    _validate_v3_obstacles(
        payload,
        ledger=ledger,
        night_summary={"open": 44687, "high": 45000, "low": 44276, "close": 44534},
    )


def test_consumed_continuation_candidate_is_suppressed_and_cannot_reactivate() -> None:
    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": {
                "setup_key": "old-copy",
                "stage": "ARMED",
                "trigger_level": 44602,
            }
        }
    }
    suppressed = _suppress_retired_continuation_candidate(
        ledger,
        retired_setup_keys={"old-copy"},
    )
    assert suppressed["trade_levels"]["continuation_arm_candidate"] is None
    assert suppressed["trade_levels"]["retired_continuation_setup_key"] == "old-copy"

    envelope = {
        "analysis": {"action": {"setup_key": "old-copy"}},
        "memory": {
            "active_setups": [
                {"setup_key": "old-copy", "stage": "ARMED"}
            ]
        },
    }
    with pytest.raises(ReplayRunError, match="不得重新使用"):
        _validate_retired_setup_keys(
            envelope,
            retired_setup_keys={"old-copy"},
        )


def test_retired_setup_key_may_be_named_only_in_its_terminal_invalidation_notice() -> None:
    envelope = {
        "analysis": {
            "message_type": "INVALIDATION",
            "action": {"position_action": "NONE", "setup_key": "old-copy"},
        },
        "memory": {
            "active_setups": [
                {"setup_key": "old-copy", "stage": "INVALIDATED"}
            ]
        },
    }

    _validate_retired_setup_keys(envelope, retired_setup_keys={"old-copy"})

    without_terminal_memory = copy.deepcopy(envelope)
    without_terminal_memory["memory"]["active_setups"] = []
    without_terminal_memory["analysis"]["course_reading"] = {
        "setup_stage": "INVALIDATED"
    }
    _validate_retired_setup_keys(
        without_terminal_memory,
        retired_setup_keys={"old-copy"},
    )


def test_hybrid_setup_invalidation_notice_must_match_current_program_event() -> None:
    events = [
        {
            "event_type": "PROGRAM_SETUP_INVALIDATED",
            "event_id": "EG-new",
            "setup_key": "or5-long",
            "event_time": "2026-08-11T08:58:00+08:00",
            "recorded_at": "2026-08-11T08:58:00+08:00",
        }
    ]
    reading = {"setup_stage": "INVALIDATED", "structure_event_ref": None}
    action = {"position_action": "NONE", "setup_key": "or5-long"}

    assert _program_setup_invalidation_event(
        message_type="INVALIDATION",
        reading=reading,
        action=action,
        evidence_events=events,
    ) == events[0]
    assert _program_setup_invalidation_event(
        message_type="INVALIDATION",
        reading=reading,
        action={**action, "setup_key": None},
        evidence_events=events,
    ) == events[0]
    assert not _program_setup_invalidation_event(
        message_type="INVALIDATION",
        reading=reading,
        action={**action, "setup_key": "retired-other"},
        evidence_events=events,
    )
    assert not _program_setup_invalidation_event(
        message_type="OBSERVATION",
        reading=reading,
        action=action,
        evidence_events=events,
    )


def test_candidate_is_suppressed_when_it_expires_at_current_cutoff() -> None:
    memory = {
        "version": 3,
        "as_of": "2026-08-21T10:10:00+08:00",
        "active_setups": [
            {
                "setup_key": "expiring-copy",
                "stage": "ARMED",
                "valid_bars": 3,
            }
        ],
    }
    expired = expire_stale_armed_setups(
        memory,
        armed_at_by_key={"expiring-copy": "2026-08-21T10:08:00+08:00"},
        as_of="2026-08-21T10:12:00+08:00",
    )
    retired = _terminal_setup_keys(expired)
    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": {
                "setup_key": "expiring-copy",
                "stage": "ARMED",
            }
        }
    }

    suppressed = _suppress_retired_continuation_candidate(
        ledger,
        retired_setup_keys=retired,
    )

    assert retired == {"expiring-copy"}
    assert suppressed["trade_levels"]["continuation_arm_candidate"] is None
    assert suppressed["trade_levels"]["retired_continuation_candidate"]["setup_key"] == "expiring-copy"


def test_expired_continuation_cannot_be_rekeyed_without_new_program_candidate() -> None:
    analysis = {
        "message_type": "PREPARATION",
        "course_reading": {
            "setup_stage": "ARMED",
            "main_strategy": "多方良性修正後複製",
            "structure_event_ref": None,
        },
        "action": {"setup_key": "renamed-copy"},
    }
    memory = {
        "active_setups": [
            {"setup_key": "renamed-copy", "stage": "ARMED", "direction": "LONG"}
        ]
    }
    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": None,
            "retired_continuation_candidate": {
                "setup_key": "expired-copy",
                "direction": "LONG",
                "stop_source_time": "2026-08-21T10:02:00+08:00",
            },
        }
    }

    with pytest.raises(SemanticReplayError, match="移動觸發線"):
        _validate_program_continuation_arm(
            analysis,
            memory,
            ledger=ledger,
            position={"status": "FLAT"},
            entry_gate={"status": "NONE"},
        )

    analysis["course_reading"]["main_strategy"] = "開盤區間假跌破反轉"
    _validate_program_continuation_arm(
        analysis,
        memory,
        ledger=ledger,
        position={"status": "FLAT"},
        entry_gate={"status": "NONE"},
    )


def test_same_continuation_setup_keeps_first_armed_trigger_and_window() -> None:
    first = {
        "setup_key": "same-structure",
        "direction": "SHORT",
        "trigger_level": 44888.0,
        "trigger_time": "2026-08-24T12:42:00+08:00",
        "first_seen_at": "2026-08-24T12:56:00+08:00",
        "valid_bars": 3,
    }
    rebuilt = {
        **first,
        "trigger_level": 44850.0,
        "trigger_time": "2026-08-24T12:58:00+08:00",
        "first_seen_at": "2026-08-24T12:58:00+08:00",
    }

    frozen = _freeze_continuation_candidate(
        {"trade_levels": {"continuation_arm_candidate": rebuilt}},
        previous_ledger={"trade_levels": {"continuation_arm_candidate": first}},
    )

    assert frozen["trade_levels"]["continuation_arm_candidate"] == first


def test_active_continuation_is_kept_until_terminal_and_new_plan_is_queued() -> None:
    old = {
        "setup_key": "old-pullback",
        "direction": "LONG",
        "trigger_level": 100.0,
        "first_seen_at": "2026-08-21T10:08:00+08:00",
        "valid_bars": 3,
    }
    new = {
        "setup_key": "new-pullback",
        "direction": "LONG",
        "trigger_level": 108.0,
        "first_seen_at": "2026-08-21T10:12:00+08:00",
        "valid_bars": 3,
    }
    frozen = _freeze_continuation_candidate(
        {"trade_levels": {"continuation_arm_candidate": new}},
        previous_ledger={"trade_levels": {"continuation_arm_candidate": old}},
        previous_memory={
            "active_setups": [{"setup_key": "old-pullback", "stage": "ARMED"}]
        },
    )

    assert frozen["trade_levels"]["continuation_arm_candidate"] == old
    assert frozen["trade_levels"]["next_continuation_candidate"] == new

    promoted = _suppress_retired_continuation_candidate(
        frozen,
        retired_setup_keys={"old-pullback"},
        as_of="2026-08-21T10:12:00+08:00",
    )
    assert promoted["trade_levels"]["continuation_arm_candidate"] == new
    assert "next_continuation_candidate" not in promoted["trade_levels"]


def test_frozen_plan_is_replaced_when_objective_controller_takes_over_opposite_direction() -> None:
    old = {
        "setup_key": "old-long",
        "direction": "LONG",
        "first_seen_at": "2026-08-24T09:50:00+08:00",
        "valid_bars": 5,
    }
    new = {
        "setup_key": "new-short-yizhi",
        "direction": "SHORT",
        "first_seen_at": "2026-08-24T09:58:00+08:00",
        "valid_bars": 2,
        "decision_authority": "PROGRAM",
    }
    frozen = _freeze_continuation_candidate(
        {
            "anchor_lifecycle": {
                "background_anchor": None,
                "child_anchor": {
                    "direction": "BEAR",
                    "status": "ACTIVE",
                    "defense": {"state": "ACTIVE"},
                },
            },
            "trade_levels": {"continuation_arm_candidate": new},
        },
        previous_ledger={"trade_levels": {"continuation_arm_candidate": old}},
        previous_memory={"active_setups": [{"setup_key": "old-long", "stage": "ARMED"}]},
    )

    levels = frozen["trade_levels"]
    assert levels["continuation_arm_candidate"] == new
    assert levels["invalidated_program_candidate"] == old
    assert "next_continuation_candidate" not in levels


def test_armed_program_plan_keeps_locked_stop_when_rebuilt_candidate_temporarily_disappears() -> None:
    armed = {
        "setup_key": "locked-plan",
        "setup_name": "多方Q4修正後複製",
        "direction": "LONG",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 100.0,
        "stop_price": 92.0,
        "first_seen_at": "2026-08-21T09:00:00+08:00",
        "valid_bars": 3,
        "decision_authority": "PROGRAM",
        "behavior_max_wait_bars": 5,
        "behavior_trigger_level": 100.0,
        "behavior_obstacles": [],
    }
    frozen = _freeze_continuation_candidate(
        {"trade_levels": {"continuation_arm_candidate": None}},
        previous_ledger={"trade_levels": {"continuation_arm_candidate": armed}},
        previous_memory={
            "as_of": "2026-08-21T09:00:00+08:00",
            "active_setups": [
                {
                    "setup_key": "locked-plan",
                    "stage": "ARMED",
                    "direction": "LONG",
                    "trigger_operator": "CLOSE_ABOVE",
                    "trigger_level": 100.0,
                    "valid_bars": 3,
                }
            ],
        },
    )

    candidate = frozen["trade_levels"]["continuation_arm_candidate"]
    assert candidate == armed
    gate = derive_entry_eligibility(
        {
            "as_of": "2026-08-21T09:00:00+08:00",
            "active_setups": [
                {
                    "setup_key": "locked-plan",
                    "name": "多方Q4修正後複製",
                    "stage": "ARMED",
                    "direction": "LONG",
                    "trigger_operator": "CLOSE_ABOVE",
                    "trigger_level": 100.0,
                    "valid_bars": 3,
                }
            ],
        },
        [
            {
                "time": "2026-08-21T09:01:00+08:00",
                "open": 99.0,
                "high": 102.0,
                "low": 98.0,
                "close": 101.0,
            }
        ],
        as_of="2026-08-21T09:01:00+08:00",
        position={"status": "FLAT", "pending_entry": None},
        current_candidate=candidate,
    )
    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["decision_authority"] == "PROGRAM"
    assert gate["required_stop_price"] == 92.0
    assert gate["required_behavior_max_wait_bars"] == 5


def test_frozen_plan_keeps_original_executable_audit_when_live_rebuild_is_filtered() -> None:
    armed = {
        "setup_key": "locked-plan",
        "setup_name": "多方修正後複製",
        "direction": "LONG",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
        "trigger_level": 100.0,
        "first_seen_at": "2026-08-19T10:13:00+08:00",
        "valid_bars": 5,
    }
    rebuilt_audit = {
        "version": 1,
        "authority": "PROGRAM_COURSE_GATE_V1",
        "status": "OBSERVATION_ONLY",
        "setup_key": "locked-plan",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
        "reason_codes": ["CONTINUATION_WORKING_QUADRANT_UNDEFINED_UNCLEAR"],
    }
    prior_audit = {
        "version": 1,
        "authority": "PROGRAM_COURSE_GATE_V1",
        "status": "EXECUTABLE",
        "setup_key": "locked-plan",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
        "reason_codes": ["ALL_PROGRAM_COURSE_ENTRY_GATES_PASSED"],
    }

    frozen = _freeze_continuation_candidate(
        {
            "trade_levels": {
                "continuation_arm_candidate": None,
                "course_entry_quality_audit": rebuilt_audit,
                "course_filtered_candidate": armed,
            }
        },
        previous_ledger={
            "trade_levels": {
                "continuation_arm_candidate": armed,
                "course_entry_quality_audit": prior_audit,
            }
        },
        previous_memory={
            "active_setups": [{"setup_key": "locked-plan", "stage": "ARMED"}]
        },
    )

    levels = frozen["trade_levels"]
    assert levels["continuation_arm_candidate"] == armed
    assert levels["course_entry_quality_audit"]["status"] == "EXECUTABLE"
    assert levels["course_entry_quality_audit"]["frozen_plan"] is True
    assert "FROZEN_PROGRAM_PLAN_REMAINS_VALID" in (
        levels["course_entry_quality_audit"]["reason_codes"]
    )
    assert levels["rebuilt_course_entry_quality_audit"] == rebuilt_audit
    assert levels["rebuilt_course_filtered_candidate"] == armed
    assert "course_filtered_candidate" not in levels


def test_queued_continuation_does_not_restart_after_original_window_expired() -> None:
    old = {
        "setup_key": "old-pullback",
        "direction": "LONG",
        "first_seen_at": "2026-08-20T09:02:00+08:00",
        "valid_bars": 3,
    }
    queued = {
        "setup_key": "queued-false-break",
        "direction": "SHORT",
        "first_seen_at": "2026-08-20T09:04:00+08:00",
        "valid_bars": 3,
    }
    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": old,
            "next_continuation_candidate": queued,
        }
    }

    suppressed = _suppress_retired_continuation_candidate(
        ledger,
        retired_setup_keys={"old-pullback"},
        as_of="2026-08-20T09:08:00+08:00",
    )

    levels = suppressed["trade_levels"]
    assert levels["continuation_arm_candidate"] is None
    assert levels["stale_unseen_continuation_candidate"] == queued
    assert levels["stale_unseen_reason"] == (
        "ORIGINAL_TRIGGER_WINDOW_EXPIRED_WHILE_QUEUED"
    )


def test_retired_prior_continuation_never_blocks_fresh_plan() -> None:
    old = {"setup_key": "retired-pullback", "direction": "SHORT"}
    new = {"setup_key": "fresh-pullback", "direction": "SHORT"}

    frozen = _freeze_continuation_candidate(
        {"trade_levels": {"continuation_arm_candidate": new}},
        previous_ledger={"trade_levels": {"continuation_arm_candidate": old}},
        previous_memory={
            "active_setups": [{"setup_key": "retired-pullback", "stage": "ARMED"}]
        },
        retired_setup_keys={"retired-pullback"},
    )

    assert frozen["trade_levels"]["continuation_arm_candidate"] == new
    assert "next_continuation_candidate" not in frozen["trade_levels"]


def test_program_policy_strips_extra_order_without_erasing_position_exit() -> None:
    analysis = {
        "message_type": "EXIT",
        "original_decision": "NOTIFY",
        "course_reading": {"setup_stage": "INVALIDATED"},
        "action": {
            "position_action": "EXIT",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": "held-short",
            "stop_price": None,
            "reentry_status": "NOT_APPLICABLE",
            "entry_rejection_reason": "NONE",
        },
    }
    memory = {
        "active_setups": [
            {"setup_key": "held-short", "stage": "INVALIDATED"},
            {"setup_key": "extra-long", "stage": "ARMED"},
        ]
    }
    ledger = {
        "program_trade_policy": {"actionable_setups": "PROGRAM_ONLY"},
        "trade_levels": {
            "continuation_arm_candidate": {"setup_key": "held-short"},
        },
    }

    _canonicalize_program_owned_actionable_setups(
        analysis,
        memory,
        ledger=ledger,
        position={"status": "SHORT", "active_setup_key": "held-short"},
        entry_gate={"status": "NONE"},
        preopen=False,
    )

    assert [item["setup_key"] for item in memory["active_setups"]] == ["held-short"]
    assert analysis["action"]["position_action"] == "EXIT"


def test_program_policy_downgrades_model_only_entry_to_observation() -> None:
    analysis = {
        "message_type": "PREPARATION",
        "original_decision": "NOTIFY",
        "course_reading": {"setup_stage": "ENTRY_ELIGIBLE"},
        "action": {
            "position_action": "ENTER",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "setup_key": "model-only",
            "stop_price": 95.0,
            "reentry_status": "NOT_APPLICABLE",
            "entry_rejection_reason": "NONE",
        },
    }
    memory = {"active_setups": [{"setup_key": "model-only", "stage": "ARMED"}]}
    ledger = {
        "program_trade_policy": {"actionable_setups": "PROGRAM_ONLY"},
        "trade_levels": {"continuation_arm_candidate": None},
    }

    _canonicalize_program_owned_actionable_setups(
        analysis,
        memory,
        ledger=ledger,
        position={"status": "FLAT", "active_setup_key": None},
        entry_gate={"status": "NONE"},
        preopen=False,
    )

    assert memory["active_setups"] == []
    assert analysis["message_type"] == "OBSERVATION"
    assert analysis["course_reading"]["setup_stage"] == "FORMING"
    assert analysis["action"]["position_action"] == "NONE"
    assert analysis["action"]["setup_key"] is None


def test_hybrid_rejects_nonselected_actionable_candidate_without_rewriting() -> None:
    selected = {
        "setup_key": "q1-selected",
        "setup_name": "多方一之早期動能",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 44899.0,
        "valid_bars": 2,
    }
    secondary = {
        "setup_key": "or5-secondary",
        "setup_name": "OR5突破回踩重新發動",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 44914.0,
        "valid_bars": 5,
    }
    analysis = {
        "message_type": "ENTRY",
        "original_decision": "NOTIFY",
        "course_reading": {"setup_stage": "ENTRY_ELIGIBLE"},
        "action": {
            "position_action": "ENTER",
            "setup_key": "q1-selected",
        },
    }
    memory = {
        "active_setups": [
            {**selected, "stage": "ENTRY_ELIGIBLE"},
            secondary,
        ]
    }
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
        },
        "trade_levels": {
            "continuation_arm_candidate": selected,
            "opening_range_pullback_candidate": secondary,
        },
    }

    with pytest.raises(SemanticReplayError, match="不會將其他候選靜默降級"):
        _canonicalize_program_owned_actionable_setups(
            analysis,
            memory,
            ledger=ledger,
            position={"status": "FLAT", "active_setup_key": None},
            entry_gate={
                **selected,
                "status": "ENTRY_ELIGIBLE",
                "decision_authority": "AI_HYBRID",
            },
            preopen=False,
        )

    assert [item["stage"] for item in memory["active_setups"]] == [
        "ENTRY_ELIGIBLE",
        "ARMED",
    ]
    assert analysis["action"]["position_action"] == "ENTER"


def test_hybrid_unbacked_structure_card_is_rejected_without_rewriting() -> None:
    result = {
        "message_type": "STRUCTURE_DOWNGRADE",
        "original_decision": "NOTIFY",
        "course_reading": {
            "structure_event_ref": None,
            "working_quadrant": "Q2",
            "main_strategy": "原多方品質降級，等待新結構。",
        },
        "action": {"position_action": "NONE", "setup_key": None},
    }
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        }
    }

    with pytest.raises(SemanticReplayError, match="不會代替AI改成一般觀察卡"):
        _canonicalize_ai_hybrid_unbacked_event_card(
            result,
            ledger=ledger,
            position={"status": "FLAT"},
            evidence_events=[],
            preopen=False,
        )

    assert result["message_type"] == "STRUCTURE_DOWNGRADE"
    assert result["course_reading"]["working_quadrant"] == "Q2"
    assert result["course_reading"]["main_strategy"] == "原多方品質降級，等待新結構。"


def test_noop_action_drops_redundant_trade_side_without_changing_setup() -> None:
    action = {
        "position_action": "NONE",
        "direction": "LONG",
        "entry_role": "INITIAL",
        "setup_key": "held-long",
        "management": "續抱並檢查應有行為。",
    }

    _canonicalize_noop_action_labels(action)

    assert action["direction"] == "NONE"
    assert action["entry_role"] == "NOT_APPLICABLE"
    assert action["setup_key"] == "held-long"
    assert action["management"] == "續抱並檢查應有行為。"


def test_program_entry_overlay_makes_model_action_fields_irrelevant() -> None:
    analysis = {
        "message_type": "OBSERVATION",
        "message_direction": "NEUTRAL",
        "original_decision": "DONT_NOTIFY",
        "course_reading": {
            "setup_stage": "NONE",
            "main_strategy": "模型原本只想等待",
            "structure_event_ref": None,
        },
        "action": {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": None,
            "trigger": "模型沒有觸發",
            "entry": "模型沒有進場",
            "structural_stop": "模型沒有停損",
            "stop_price": None,
            "obstacles": [],
            "expected_behavior": "模型沒有計畫",
            "max_wait_bars": None,
            "management": "模型等待",
            "reentry_status": "NOT_APPLICABLE",
            "entry_rejection_reason": "NONE",
        },
    }
    memory = {"active_setups": []}
    gate = {
        "status": "ENTRY_ELIGIBLE",
        "decision_authority": "PROGRAM",
        "setup_key": "program-long",
        "setup_name": "多方修正後複製",
        "direction": "LONG",
        "entry_role": "INITIAL",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 105.0,
        "valid_bars": 3,
        "required_stop_price": 94.5,
        "required_behavior_max_wait_bars": 5,
        "required_behavior_obstacles": [
            {"price": 105.0, "role": "TRIGGER"},
            {"price": 112.0, "role": "CHECKPOINT"},
        ],
    }
    ledger = {
        "program_trade_policy": {"actionable_setups": "PROGRAM_ONLY"},
        "trade_levels": {"continuation_arm_candidate": None},
    }

    _canonicalize_program_setup_and_entry(
        analysis,
        memory,
        ledger=ledger,
        position={"status": "FLAT", "pending_entry": None},
        entry_gate=gate,
        preopen=False,
    )

    assert analysis["message_type"] == "PREPARATION"
    assert analysis["original_decision"] == "NOTIFY"
    assert analysis["course_reading"]["setup_stage"] == "ENTRY_ELIGIBLE"
    assert analysis["action"]["position_action"] == "ENTER"
    assert analysis["action"]["stop_price"] == 94.5
    assert analysis["action"]["max_wait_bars"] == 5
    assert [(item["role"], item["price"]) for item in analysis["action"]["obstacles"]] == [
        ("TRIGGER", 105.0),
        ("CHECKPOINT", 112.0),
    ]
    assert memory["active_setups"][0]["setup_key"] == "program-long"
    assert memory["active_setups"][0]["stage"] == "ENTRY_ELIGIBLE"


def test_preopen_snapshot_does_not_force_prior_session_candidate_to_arm() -> None:
    _validate_program_continuation_arm(
        {},
        {"active_setups": []},
        ledger={
            "trade_levels": {
                "continuation_arm_candidate": {
                    "setup_key": "night-only",
                    "direction": "LONG",
                    "stage": "ARMED",
                }
            }
        },
        position={"status": "FLAT"},
        entry_gate=None,
        preopen=True,
    )


def test_countertrend_continuation_candidate_is_observation_not_armed() -> None:
    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": {
                "setup_key": "bull-small-copy",
                "direction": "LONG",
                "stage": "ARMED",
                "trigger_level": 44924,
            }
        },
        "anchor_lifecycle": {
            "background_anchor": {
                "direction": "BEAR",
                "status": "ACTIVE",
                "defense": {"state": "ACTIVE", "price": 44964},
            },
            "child_anchor": {
                "direction": "BULL",
                "status": "ACTIVE",
                "defense": {"state": "ACTIVE", "price": 44889},
            },
        },
    }

    suppressed = _suppress_countertrend_continuation_candidate(ledger)

    assert suppressed["trade_levels"]["continuation_arm_candidate"] is None
    assert suppressed["trade_levels"]["countertrend_continuation_observation"]["setup_key"] == "bull-small-copy"
    assert suppressed["trade_levels"]["countertrend_continuation_reason"] == (
        "TYPE1_HIGHEST_ACTIVE_ANCHOR_NOT_INVALIDATED"
    )


def test_countertrend_candidate_is_observation_even_before_controller_earns_defense() -> None:
    candidate = {
        "setup_key": "early-countertrend",
        "direction": "SHORT",
        "candidate_source": "FALSE_BREAK_RECLAIM",
    }
    state = {
        "background_anchor": {
            "direction": "BULL",
            "status": "ACTIVE",
            "defense": None,
        }
    }

    assert _candidate_is_countertrend_to_intact_controller(candidate, state) is True


def test_broken_controller_defense_alone_does_not_release_countertrend_guard() -> None:
    candidate = {
        "setup_key": "post-break-reversal",
        "direction": "SHORT",
        "candidate_source": "FALSE_BREAK_RECLAIM",
    }
    state = {
        "background_anchor": {
            "direction": "BULL",
            "status": "ACTIVE",
            "defense": {"state": "BROKEN"},
        }
    }

    assert _candidate_is_countertrend_to_intact_controller(candidate, state) is True


def test_active_opposite_child_releases_countertrend_guard_after_right_side_takeover() -> None:
    candidate = {
        "setup_key": "right-side-reversal",
        "direction": "SHORT",
        "candidate_source": "FALSE_BREAK_RECLAIM",
    }
    state = {
        "background_anchor": {
            "direction": "BULL",
            "status": "DEGRADED",
            "defense": {"state": "BROKEN"},
        },
        "child_anchor": {"direction": "BEAR", "status": "ACTIVE"},
    }

    assert _candidate_is_countertrend_to_intact_controller(candidate, state) is False


def test_program_course_gate_rejects_mechanical_turn_during_taiji_reset() -> None:
    candidate = {
        "setup_key": "mechanical-turn",
        "direction": "LONG",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
    }
    anchor_state = {
        "background_anchor": {"direction": "BULL", "status": "ACTIVE"},
        "quadrant_context": {
            "working_primary": "Q4",
            "working_candidates": ["Q4"],
            "working_trend_dynamics": "INCREASING",
        },
        "taiji_context": {"program_state": "CORRECTION_DESTRUCTIVE"},
    }

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state=anchor_state,
        course_method_state={"cclass_mode": "RESETTING"},
        structure_events=[],
    )

    assert qualified is None
    assert audit["status"] == "OBSERVATION_ONLY"
    assert "CONTINUATION_CCLASS_RESETTING" in audit["reason_codes"]
    assert "CONTINUATION_TAIJI_CORRECTION_DESTRUCTIVE" in audit["reason_codes"]


def test_program_course_gate_accepts_aligned_ordered_q4_continuation() -> None:
    candidate = {
        "setup_key": "ordered-q4",
        "direction": "LONG",
        "candidate_source": "ANCHOR_LEG_SEQUENCE",
    }
    anchor_state = {
        "background_anchor": {"direction": "BULL", "status": "ACTIVE"},
        "quadrant_context": {
            "working_primary": "Q4",
            "working_candidates": ["Q4"],
            "working_trend_dynamics": "INCREASING",
        },
        "taiji_context": {"program_state": "CORRECTION_HELD"},
    }

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state=anchor_state,
        course_method_state={
            "cclass_mode": "TAIJI_ORDERED",
            "numbered_market": {"status": "NUMBERED", "number": 0, "direction": "BULL"},
        },
        structure_events=[],
    )

    assert qualified is not None
    assert qualified["course_entry_quality"] == "EXECUTABLE"
    assert audit["status"] == "EXECUTABLE"


def test_program_course_gate_does_not_apply_q4_stop_to_extended_q1_momentum() -> None:
    candidate = {
        "setup_key": "late-q1-push",
        "direction": "SHORT",
        "candidate_source": "ANCHOR_LEG_SEQUENCE",
        "stop_price": 45287.8,
        "trigger_level": 44938.0,
    }
    anchor_state = {
        "child_anchor": {"direction": "BEAR", "status": "ACTIVE"},
        "quadrant_context": {
            "working_primary": "Q1",
            "working_candidates": ["Q1"],
            "working_trend_dynamics": "INCREASING",
        },
        "taiji_context": {"program_state": "COPY_FORMING"},
    }

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state=anchor_state,
        course_method_state={"cclass_mode": "YIZHI_MOMENTUM"},
        structure_events=[],
    )

    assert qualified is None
    assert audit["status"] == "OBSERVATION_ONLY"
    assert "CONTINUATION_WORKING_QUADRANT_Q1_INCREASING" in audit["reason_codes"]


def test_q4_pullback_endpoint_accepts_equal_high_plateau_and_uses_last_extreme_bar() -> None:
    controller = {
        "id": "bear-controller",
        "direction": "BEAR",
        "status": "ACTIVE",
        "origin_time": "2026-08-24T09:00:00+08:00",
        "origin_price": 200.0,
        "first_seen_at": "2026-08-24T09:00:00+08:00",
        "defense": {"price": 210.0, "state": "ACTIVE"},
    }
    bars = [
        {"time": "2026-08-24T09:00:00+08:00", "open": 160, "high": 165, "low": 150, "close": 155},
        {"time": "2026-08-24T09:01:00+08:00", "open": 155, "high": 160, "low": 145, "close": 150},
        {"time": "2026-08-24T09:02:00+08:00", "open": 150, "high": 170, "low": 149, "close": 165},
        {"time": "2026-08-24T09:03:00+08:00", "open": 165, "high": 180, "low": 160, "close": 175},
        {"time": "2026-08-24T09:04:00+08:00", "open": 175, "high": 180, "low": 165, "close": 170},
        {"time": "2026-08-24T09:05:00+08:00", "open": 170, "high": 175, "low": 155, "close": 160},
        {"time": "2026-08-24T09:06:00+08:00", "open": 160, "high": 170, "low": 145, "close": 150},
    ]

    candidate = _confirmed_pullback_endpoint_candidate(
        controller,
        bars=bars,
        expected=datetime.fromisoformat("2026-08-24T09:06:00+08:00"),
    )

    assert candidate is not None
    assert candidate["stop_source_time"] == "2026-08-24T09:04:00+08:00"
    assert candidate["stop_source_price"] == 180.0
    assert candidate["trigger_level"] == 145.0
    assert candidate["valid_bars"] == 5


def test_q1_yizhi_uses_early_signal_candle_stop_instead_of_q4_correction_stop() -> None:
    bars = [
        {"time": "2026-08-24T09:56:00+08:00", "open": 120, "high": 125, "low": 110, "close": 112},
        {"time": "2026-08-24T09:57:00+08:00", "open": 112, "high": 114, "low": 98, "close": 99},
    ]
    methods = {
        "cclass_mode": "YIZHI_MOMENTUM",
        "numbered_market": {"status": "NUMBERED", "number": 0, "direction": "BEAR"},
        "yizhi": {
            "state": "MOMENTUM_CONTINUING",
            "direction": "BEAR",
            "activation_time": "2026-08-24T09:56:00+08:00",
            "latest_time": "2026-08-24T09:57:00+08:00",
        },
    }
    anchor_state = {
        "child_anchor": {"id": "bear", "direction": "BEAR", "status": "ACTIVE"},
        "quadrant_context": {
            "working_primary": "Q1",
            "working_candidates": ["Q1"],
            "working_trend_dynamics": "INCREASING",
        },
    }

    candidate = _yizhi_momentum_entry_candidate(
        anchor_state,
        course_method_state=methods,
        bars=bars,
        expected=datetime.fromisoformat("2026-08-24T09:57:00+08:00"),
    )

    assert candidate is not None
    assert candidate["candidate_source"] == "YIZHI_EARLY_BREAKOUT"
    assert candidate["trigger_level"] == 110.0
    assert candidate["stop_source_time"] == "2026-08-24T09:57:00+08:00"
    assert candidate["stop_price"] == 115.0
    assert candidate["behavior_max_wait_bars"] == 3

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state=anchor_state,
        course_method_state=methods,
        structure_events=[],
    )
    assert qualified is not None
    assert audit["status"] == "EXECUTABLE"


def test_yizhi_breakout_uses_previous_cutoff_controller_extreme() -> None:
    controller = {
        "id": "bear-controller",
        "direction": "BEAR",
        "first_extreme_price": 98.0,
        "latest_extreme_price": 90.0,
        "latest_extreme_time": "2026-08-24T09:03:00+08:00",
    }
    previous = {
        "state": "NONE",
        "direction": "BEAR",
        "controller_ref": "bear-controller",
        "controller_extreme_price": 95.0,
    }
    bars = [
        {"time": "2026-08-24T09:02:00+08:00", "open": 99, "high": 100, "low": 96, "close": 97},
        {"time": "2026-08-24T09:03:00+08:00", "open": 97, "high": 98, "low": 90, "close": 92},
    ]

    state = _derive_yizhi_state(
        controller,
        taiji={"leg_evidence": {"current_parent": {"slope_points_per_minute": 1}}},
        bars=bars,
        opening_ranges={},
        previous=previous,
    )

    assert state["breakout_reference"] == "PREVIOUS_CUTOFF_CONTROLLER_EXTREME"
    assert state["breakout_reference_price"] == 95.0
    assert "STRUCTURAL_BREAKOUT" in state["reason_codes"]


def test_q2_failed_reverse_candidate_requires_mature_countermove_and_active_defense() -> None:
    continuation = {
        "setup_key": "raw-bear-endpoint",
        "setup_name": "空方修正後複製",
        "direction": "SHORT",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
        "stop_source_time": "2026-08-24T10:42:00+08:00",
        "stop_source_price": 45110.0,
        "trigger_level": 45075.0,
        "first_seen_at": "2026-08-24T10:44:00+08:00",
        "decision_authority": "PROGRAM",
    }
    anchor_state = {
        "child_anchor": {
            "id": "bear-controller",
            "direction": "BEAR",
            "status": "ACTIVE",
            "defense": {"id": "bear-defense", "state": "ACTIVE", "price": 45277.0},
        },
        "reverse_candidate": {
            "id": "bull-reverse",
            "direction": "BULL",
            "status": "QUALIFIED",
            "current_extreme_price": 45110.0,
        },
        "working_leg": {"direction": "BULL"},
        "quadrant_context": {
            "working_primary": "TRANSITION",
            "working_candidates": ["Q2", "Q3"],
            "working_trend_dynamics": "DECREASING",
        },
        "taiji_context": {"program_state": "CORRECTION_DESTRUCTIVE"},
    }
    methods = {
        "cclass_mode": "RESETTING",
        "numbered_market": {"status": "NUMBERED", "number": 1, "direction": "BEAR"},
    }

    candidate = _q2_failed_reverse_entry_candidate(
        continuation,
        anchor_state=anchor_state,
        course_method_state=methods,
    )

    assert candidate is not None
    assert candidate["candidate_source"] == "Q2_FAILED_REVERSE_CANDIDATE"
    assert candidate["entry_strategy"] == "Q2_FAILED_COUNTERTREND_REVERSAL"
    assert candidate["behavior_policy"] == "Q2_FAST_REVERSAL_CONFIRMATION"
    assert candidate["stop_source_price"] == 45110.0
    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state=anchor_state,
        course_method_state=methods,
        structure_events=[],
    )
    assert qualified is not None
    assert audit["status"] == "EXECUTABLE"

    anchor_state["child_anchor"]["defense"]["state"] = "BROKEN"
    assert _q2_failed_reverse_entry_candidate(
        continuation,
        anchor_state=anchor_state,
        course_method_state=methods,
    ) is None


def test_program_course_gate_accepts_current_aligned_dow_false_break() -> None:
    candidate = {
        "setup_key": "false-break-current-dow",
        "direction": "SHORT",
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "source_event_id": "event-1",
        "source_type": "DOW_DEFENSE",
        "source_id": "bear-defense",
    }

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state={
            "child_anchor": {
                "direction": "BEAR",
                "status": "DEGRADED_RECLAIMED",
                "defense": {"id": "bear-defense", "state": "BROKEN_AND_RECLAIMED"},
            },
            "dow_context": {
                "small_bear_defense": {"id": "bear-defense"},
            },
            "quadrant_context": {},
            "taiji_context": {},
        },
        course_method_state={
            "cclass_mode": "RESETTING",
            "numbered_market": {"status": "NUMBERED", "number": 1, "direction": "BEAR"},
        },
        structure_events=[],
    )

    assert qualified is not None
    assert audit["status"] == "EXECUTABLE"
    assert audit["boundary_type"] == "DOW_DEFENSE"


def test_program_course_gate_rejects_stale_dow_false_break_from_old_slot() -> None:
    candidate = {
        "setup_key": "false-break-old-dow",
        "direction": "LONG",
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "source_event_id": "event-1",
        "source_type": "DOW_DEFENSE",
        "source_id": "old-bull-defense",
    }
    anchor_state = {
        "child_anchor": {
            "direction": "BULL",
            "status": "ACTIVE",
            "defense": {"id": "current-bull-defense", "state": "ACTIVE"},
        },
        "dow_context": {
            "small_bull_defense": {"id": "current-bull-defense"},
        },
        "quadrant_context": {},
        "taiji_context": {},
    }

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state=anchor_state,
        course_method_state={
            "cclass_mode": "TAIJI_ORDERED",
            "numbered_market": {"status": "NUMBERED", "number": 1, "direction": "BEAR"},
        },
        structure_events=[],
    )

    assert qualified is None
    assert "FALSE_BREAK_DOW_DEFENSE_NOT_CURRENT_COURSE_SLOT" in audit["reason_codes"]


def test_program_course_gate_keeps_first_opening_endpoint_false_break_observational() -> None:
    candidate = {
        "setup_key": "false-break-or5",
        "direction": "SHORT",
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "source_type": "OR5_HIGH",
    }

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state={
            "child_anchor": {"direction": "BEAR", "status": "ACTIVE", "defense": None},
            "quadrant_context": {},
            "taiji_context": {},
        },
        course_method_state={
            "cclass_mode": "TAIJI_ORDERED",
            "numbered_market": {"status": "NUMBERED", "number": 1, "direction": "BEAR"},
        },
        structure_events=[],
    )

    assert qualified is None
    assert "FALSE_BREAK_OPENING_BOUNDARY_FIRST_ENDPOINT_ONLY" in audit["reason_codes"]


def test_program_course_gate_accepts_aligned_opening_false_break_after_defense_exists() -> None:
    candidate = {
        "setup_key": "false-break-or15-mature",
        "direction": "SHORT",
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "source_type": "OR15_HIGH",
    }

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state={
            "child_anchor": {
                "direction": "BEAR",
                "status": "ACTIVE",
                "defense": {"id": "bear-defense", "state": "ACTIVE"},
            },
            "quadrant_context": {},
            "taiji_context": {},
        },
        course_method_state={
            "cclass_mode": "TAIJI_ORDERED",
            "numbered_market": {"status": "NUMBERED", "number": 1, "direction": "BEAR"},
        },
        structure_events=[],
    )

    assert qualified is not None
    assert audit["status"] == "EXECUTABLE"


def test_program_course_gate_keeps_arbitrary_pivot_false_break_observational() -> None:
    candidate = {
        "setup_key": "noisy-pivot-reclaim",
        "direction": "LONG",
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "source_event_id": "event-1",
        "source_type": "PIVOT_LOW",
    }

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state={"quadrant_context": {}, "taiji_context": {}},
        course_method_state={"cclass_mode": "RESETTING"},
        structure_events=[],
    )

    assert qualified is None
    assert audit["reason_codes"] == [
        "NUMBERED_MARKET_UNDEFINED",
        "FALSE_BREAK_BOUNDARY_NOT_QUALIFIED_PIVOT_LOW",
    ]


def test_program_scenario_bias_uses_large_controller_and_keeps_child_conflict() -> None:
    state = {
        "background_anchor": {"direction": "BEAR", "status": "ACTIVE"},
        "child_anchor": {"direction": "BULL", "status": "ACTIVE"},
        "dow_context": {},
        "quadrant_context": {"working_primary": "TRANSITION"},
        "taiji_context": {},
    }

    result = _derive_program_scenario_weights(
        state,
        cclass_mode="UNDEFINED",
        yizhi={"state": "NONE"},
    )

    assert "ACTIVE_CONTROLLER_BEAR_PLUS3" in result["reason_codes"]
    assert "OPPOSITE_CHILD_BULL_TIMING_PLUS1_RANGE_PLUS1" in result["reason_codes"]
    assert result["bear"] > result["bull"]


def test_numbered_market_waits_for_dow_backed_anchor_then_establishes_zero() -> None:
    incomplete = {
        "child_anchor": {
            "id": "first-bull",
            "direction": "BULL",
            "status": "ACTIVE",
            "first_seen_at": "2026-08-25T08:49:00+08:00",
            "defense": None,
        },
        "dow_context": {"small_state": "UNDEFINED"},
    }
    waiting = _derive_numbered_market_state(
        incomplete,
        as_of="2026-08-25T08:49:00+08:00",
    )
    assert waiting["status"] == "UNDEFINED"
    assert waiting["restriction"] == "WAIT_FOR_FIRST_DIRECTION"

    confirmed = copy.deepcopy(incomplete)
    confirmed["child_anchor"]["defense"] = {"id": "bull-defense", "state": "ACTIVE"}
    confirmed["child_anchor"]["first_seen_at"] = "2026-08-25T08:54:00+08:00"
    confirmed["dow_context"]["small_state"] = "BULL"
    zero = _derive_numbered_market_state(
        confirmed,
        previous=waiting,
        as_of="2026-08-25T08:57:00+08:00",
    )
    assert zero["label"] == "0"
    assert zero["direction"] == "BULL"
    assert zero["first_confirmed_at"] == "2026-08-25T08:57:00+08:00"
    assert zero["transitions"][-1]["source_anchor_first_seen_at"] == "2026-08-25T08:54:00+08:00"
    assert zero["transitions"][-1]["change_type"] == "FIRST_CONFIRMED_SESSION_DIRECTION"


def test_numbered_market_changes_only_on_formal_type2_or_type3_takeover() -> None:
    zero = {
        "status": "NUMBERED",
        "number": 0,
        "label": "0",
        "direction": "BULL",
        "first_direction": "BULL",
        "first_confirmed_at": "2026-08-25T09:00:00+08:00",
        "last_changed_at": "2026-08-25T09:00:00+08:00",
        "transitions": [],
        "consumed_takeover_anchor_refs": [],
    }
    reclassified = _derive_numbered_market_state(
        {
            "background_anchor": {
                "id": "bear-grade-upgrade",
                "direction": "BEAR",
                "status": "ACTIVE",
                "first_seen_at": "2026-08-25T09:30:00+08:00",
            },
            "dow_context": {"large_state": "BEAR"},
        },
        previous=zero,
        as_of="2026-08-25T09:30:00+08:00",
    )
    assert reclassified["label"] == "0"
    assert reclassified["direction"] == "BULL"

    one = _derive_numbered_market_state(
        {
            "background_anchor": {
                "id": "bear-type2",
                "direction": "BEAR",
                "status": "ACTIVE",
                "first_seen_at": "2026-08-25T09:40:00+08:00",
                "takeover_type": "TYPE2",
                "replaced_anchor_ref": "bull-background",
            },
            "dow_context": {"large_state": "BEAR"},
        },
        previous=reclassified,
        as_of="2026-08-25T09:40:00+08:00",
    )
    assert one["label"] == "1"
    assert one["direction"] == "BEAR"
    assert one["transitions"][-1]["change_type"] == "TYPE2"


def test_numbered_market_small_type2_requires_prior_defense_break_and_no_intact_large_parent() -> None:
    zero = {
        "status": "NUMBERED",
        "number": 0,
        "label": "0",
        "direction": "BULL",
        "first_direction": "BULL",
        "first_confirmed_at": "2026-08-20T08:55:00+08:00",
        "last_changed_at": "2026-08-20T08:55:00+08:00",
        "active_defense_ref": "bull-defense",
        "transitions": [],
        "consumed_takeover_anchor_refs": [],
    }
    broken = _derive_numbered_market_state(
        {
            "child_anchor": {
                "id": "bull-anchor",
                "level": "SMALL",
                "direction": "BULL",
                "status": "DEGRADED",
                "defense": {
                    "id": "bull-defense",
                    "time": "2026-08-20T08:51:00+08:00",
                    "price": 44963.0,
                    "state": "BROKEN",
                    "broken_at": "2026-08-20T09:01:00+08:00",
                },
            },
            "dow_context": {"small_state": "UNDEFINED"},
        },
        previous=zero,
        as_of="2026-08-20T09:01:00+08:00",
    )
    assert broken["label"] == "0"
    assert broken["pending_flip"]["broken_at"] == "2026-08-20T09:01:00+08:00"

    one = _derive_numbered_market_state(
        {
            "child_anchor": {
                "id": "bear-anchor",
                "level": "SMALL",
                "direction": "BEAR",
                "status": "ACTIVE",
                "first_seen_at": "2026-08-20T09:21:00+08:00",
                "defense": {
                    "id": "bear-defense",
                    "time": "2026-08-20T09:19:00+08:00",
                    "price": 44903.0,
                    "state": "ACTIVE",
                },
            },
            "dow_context": {"small_state": "BEAR"},
        },
        previous=broken,
        as_of="2026-08-20T09:21:00+08:00",
    )
    assert one["label"] == "1"
    assert one["direction"] == "BEAR"
    assert one["transitions"][-1]["change_type"] == "TYPE2_SMALL_DEFENSE_BREAK"

    blocked = _derive_numbered_market_state(
        {
            "background_anchor": {
                "id": "large-bear",
                "level": "LARGE",
                "direction": "BEAR",
                "status": "ACTIVE",
                "defense": {"id": "large-bear-defense", "state": "ACTIVE"},
            },
            "child_anchor": {
                "id": "small-bull",
                "level": "SMALL",
                "direction": "BULL",
                "status": "ACTIVE",
                "defense": {"id": "small-bull-defense", "state": "ACTIVE"},
            },
            "dow_context": {"small_state": "BULL", "large_state": "BEAR"},
        },
        previous={
            **one,
            "pending_flip": {
                "from_direction": "BEAR",
                "defense_ref": "old-bear-defense",
                "broken_at": "2026-08-20T10:33:00+08:00",
            },
        },
        as_of="2026-08-20T10:34:00+08:00",
    )
    assert blocked["label"] == "1"
    assert blocked["direction"] == "BEAR"


def test_numbered_market_accepts_explicit_small_type2_takeover_without_waiting_for_lost_old_child() -> None:
    prior = {
        "status": "NUMBERED",
        "number": 0,
        "label": "0",
        "direction": "BULL",
        "first_direction": "BULL",
        "first_confirmed_at": "2026-08-24T08:55:00+08:00",
        "last_changed_at": "2026-08-24T08:55:00+08:00",
        "transitions": [],
        "consumed_takeover_anchor_refs": [],
    }
    one = _derive_numbered_market_state(
        {
            "background_anchor": None,
            "child_anchor": {
                "id": "small-bear-type2",
                "level": "SMALL",
                "direction": "BEAR",
                "status": "ACTIVE",
                "first_seen_at": "2026-08-24T09:56:00+08:00",
                "takeover_type": "TYPE2",
                "replaced_anchor_ref": "small-bull-old",
                "defense": {
                    "id": "small-bear-defense",
                    "time": "2026-08-24T09:51:00+08:00",
                    "price": 45277,
                    "state": "ACTIVE",
                },
            },
            "dow_context": {"small_state": "BEAR", "large_state": "UNDEFINED"},
        },
        previous=prior,
        as_of="2026-08-24T09:56:00+08:00",
    )
    assert one["label"] == "1"
    assert one["direction"] == "BEAR"
    assert one["last_changed_at"].endswith("09:56:00+08:00")
    assert one["transitions"][-1]["change_type"] == "TYPE2_SMALL_TAKEOVER"


def test_two_plus_numbered_market_marks_normal_trend_entry_for_ai_risk_review() -> None:
    candidate = {
        "setup_key": "trend-after-two-flips",
        "direction": "LONG",
        "candidate_source": "ANCHOR_LEG_SEQUENCE",
    }
    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state={
            "background_anchor": {"direction": "BULL", "status": "ACTIVE"},
            "quadrant_context": {
                "working_primary": "Q4",
                "working_candidates": ["Q4"],
                "working_trend_dynamics": "INCREASING",
            },
            "taiji_context": {"program_state": "CORRECTION_HELD"},
        },
        course_method_state={
            "cclass_mode": "TAIJI_ORDERED",
            "numbered_market": {"status": "NUMBERED", "number": 2, "direction": "BULL"},
        },
        structure_events=[],
    )
    assert qualified is not None
    assert {key: qualified[key] for key in candidate} == candidate
    assert audit["status"] == "EXECUTABLE"
    assert audit["requires_ai_quality_review"] is True
    assert "NUMBERED_MARKET_2_PLUS_HIGH_NOISE_AI_REVIEW" in audit["risk_flags"]
    assert "NUMBERED_MARKET_2_PLUS_STRATEGY_RESTRICTED" not in audit["reason_codes"]


def test_opening_range_requires_breakout_retest_and_later_relaunch() -> None:
    rows = [
        ("08:45", 100, 102, 99, 101),
        ("08:46", 101, 103, 100, 102),
        ("08:47", 102, 104, 101, 103),
        ("08:48", 103, 105, 102, 104),
        ("08:49", 104, 105, 103, 104),
        ("08:50", 104, 108, 104, 107),
        ("08:51", 107, 107, 105, 106),
        ("08:52", 106, 109, 105, 108),
    ]
    bars = [
        {
            "time": f"2026-08-25T{at}:00+08:00",
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
        }
        for at, opened, high, low, close in rows
    ]
    opening = {
        "or5": {
            "start": bars[0]["time"],
            "end": bars[4]["time"],
            "high": 105,
            "low": 99,
        }
    }
    before_relaunch = _opening_range_pullback_candidate(
        opening,
        expected=datetime.fromisoformat(bars[-2]["time"]),
        bars=bars[:-1],
    )
    candidate = _opening_range_pullback_candidate(
        opening,
        expected=datetime.fromisoformat(bars[-1]["time"]),
        bars=bars,
    )
    assert before_relaunch is None
    assert candidate is not None
    assert candidate["candidate_source"] == "OPENING_RANGE_BREAKOUT_RETEST"
    assert candidate["opening_range"] == "OR5"
    assert candidate["breakout_time"].endswith("08:50:00+08:00")
    assert candidate["retest_time"].endswith("08:51:00+08:00")
    assert candidate["first_seen_at"].endswith("08:52:00+08:00")
    assert candidate["stop_source_time"].endswith("08:51:00+08:00")


def test_opening_range_candidate_dies_when_retest_closes_back_inside() -> None:
    rows = [
        ("08:45", 100, 102, 99, 101),
        ("08:46", 101, 103, 100, 102),
        ("08:47", 102, 104, 101, 103),
        ("08:48", 103, 105, 102, 104),
        ("08:49", 104, 105, 103, 104),
        ("08:50", 104, 108, 104, 107),
        ("08:51", 107, 107, 104, 104.5),
        ("08:52", 104.5, 109, 104, 108),
    ]
    bars = [
        {
            "time": f"2026-08-25T{at}:00+08:00",
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
        }
        for at, opened, high, low, close in rows
    ]
    assert _opening_range_pullback_candidate(
        {"or5": {"start": bars[0]["time"], "end": bars[4]["time"], "high": 105, "low": 99}},
        expected=datetime.fromisoformat(bars[-1]["time"]),
        bars=bars,
    ) is None


def test_compression_breakout_requires_qualified_sstv_and_is_program_owned() -> None:
    rows: list[tuple[float, float, float, float]] = [
        (100, 103, 98, 102) if index % 2 == 0 else (102, 103, 98, 100)
        for index in range(12)
    ]
    rows.extend(
        (100, 104, 98, 102) if index % 2 == 0 else (102, 104, 98, 100)
        for index in range(6)
    )
    rows.extend(
        (100, 103, 99, 102) if index % 2 == 0 else (102, 103, 99, 100)
        for index in range(6)
    )
    rows.append((102, 108, 101, 107))
    start = datetime.fromisoformat("2026-08-25T09:00:00+08:00")
    bars = [
        {
            "time": (start + timedelta(minutes=index)).isoformat(),
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
        }
        for index, (opened, high, low, close) in enumerate(rows)
    ]
    anchor_state = {"quadrant_context": {"working_primary": "Q3"}}
    methods = {
        "cclass_mode": "TAIJI_ORDERED",
        "numbered_market": {"status": "NUMBERED", "number": 0, "direction": "BULL"},
    }
    candidate = _compression_breakout_candidate(
        anchor_state,
        course_method_state=methods,
        expected=datetime.fromisoformat(bars[-1]["time"]),
        bars=bars,
    )
    assert candidate is not None
    assert candidate["candidate_source"] == "COMPRESSION_BREAKOUT_SSTV"
    assert candidate["sstv_audit"]["status"] == "QUALIFIED"
    assert candidate["sstv_audit"]["compression_prerequisites"] == {
        "range_contracting": True,
        "ma21_flat_then_turn": True,
    }
    assert candidate["compression_start"].endswith("09:12:00+08:00")
    assert candidate["first_seen_at"].endswith("09:24:00+08:00")


def test_compression_breakout_rejects_three_of_four_without_range_contraction() -> None:
    rows: list[tuple[float, float, float, float]] = [
        (100, 103, 98, 102) if index % 2 == 0 else (102, 103, 98, 100)
        for index in range(12)
    ]
    rows.extend(
        (100, 103, 99, 102) if index % 2 == 0 else (102, 103, 99, 100)
        for index in range(6)
    )
    rows.extend(
        (99, 104, 98, 103) if index % 2 == 0 else (103, 104, 98, 99)
        for index in range(6)
    )
    rows.append((102, 108, 101, 107))
    start = datetime.fromisoformat("2026-08-25T09:00:00+08:00")
    bars = [
        {
            "time": (start + timedelta(minutes=index)).isoformat(),
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
        }
        for index, (opened, high, low, close) in enumerate(rows)
    ]

    assert _compression_breakout_candidate(
        {"quadrant_context": {"working_primary": "Q3"}},
        course_method_state={
            "cclass_mode": "TAIJI_ORDERED",
            "numbered_market": {"status": "NUMBERED", "number": 0, "direction": "BULL"},
        },
        expected=datetime.fromisoformat(bars[-1]["time"]),
        bars=bars,
    ) is None


def test_compression_breakout_is_forbidden_in_q2_even_with_clean_box() -> None:
    candidate = {
        "setup_key": "compression-q2",
        "direction": "LONG",
        "candidate_source": "COMPRESSION_BREAKOUT_SSTV",
        "sstv_audit": {"status": "QUALIFIED", "checks": {}, "hard_reject_reasons": []},
    }
    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state={
            "background_anchor": {"direction": "BULL", "status": "ACTIVE"},
            "quadrant_context": {
                "working_primary": "Q2",
                "working_candidates": ["Q2"],
                "working_trend_dynamics": "DECREASING",
            },
            "taiji_context": {"program_state": "COPY_FAILED"},
        },
        course_method_state={
            "cclass_mode": "RESETTING",
            "numbered_market": {"status": "NUMBERED", "number": 0, "direction": "BULL"},
        },
        structure_events=[],
    )
    assert qualified is None
    assert "COMPRESSION_FORBIDDEN_IN_Q2" in audit["reason_codes"]


def test_disappeared_actionable_setup_is_retired_and_cannot_resurrect(tmp_path: Path) -> None:
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()
    old_key = "old-expired-trigger"
    historic_memory = {
        "version": 3,
        "as_of": "2026-08-24T12:04:00+08:00",
        "session_key": "2026-08-24:DAY",
        "active_setups": [{"setup_key": old_key, "stage": "ARMED"}],
    }
    (analysis_dir / "day-20260824-1204-validated.json").write_text(
        json.dumps({"analysis": {}, "memory": historic_memory}),
        encoding="utf-8",
    )
    disappeared_memory = {
        "version": 3,
        "as_of": "2026-08-24T12:06:00+08:00",
        "session_key": "2026-08-24:DAY",
        "active_setups": [{"setup_key": "new-trigger", "stage": "ARMED"}],
    }
    (analysis_dir / "day-20260824-1206-validated.json").write_text(
        json.dumps({"analysis": {}, "memory": disappeared_memory}),
        encoding="utf-8",
    )
    current_memory = {
        "version": 3,
        "as_of": "2026-08-24T12:08:00+08:00",
        "session_key": "2026-08-24:DAY",
        "active_setups": [
            {"setup_key": old_key, "stage": "ARMED"},
            {"setup_key": "new-trigger", "stage": "ARMED"},
        ],
    }
    (analysis_dir / "day-20260824-1208-validated.json").write_text(
        json.dumps({"analysis": {}, "memory": current_memory}),
        encoding="utf-8",
    )

    assert old_key in _retired_setup_keys(
        tmp_path,
        current_memory,
        active_setup_key=None,
    )


def test_post_stop_continuation_candidate_keeps_originating_reentry_setup_key() -> None:
    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": {
                "setup_key": "new-derived-key",
                "setup_name": "多方修正後複製",
                "direction": "LONG",
                "stage": "ARMED",
                "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
                "trigger_level": 45863,
                "stop_source_time": "2026-08-26T11:29:00+08:00",
            }
        }
    }
    previous_memory = {
        "active_setups": [
            {
                "setup_key": "originating-long",
                "name": "多方假跌破收復再進",
                "direction": "LONG",
                "stage": "ARMED",
                "reentry_status": "AVAILABLE",
            }
        ],
        "reentry": {
            "status": "AVAILABLE",
            "last_stop_at": "2026-08-26T11:28:00+08:00",
            "count": 0,
        },
    }
    position = {
        "status": "FLAT",
        "active_setup_key": "originating-long",
        "last_stop_time": "2026-08-26T11:28:00+08:00",
        "reentry_count": 0,
    }

    rebound = _bind_continuation_candidate_to_available_reentry(
        ledger,
        previous_memory=previous_memory,
        position=position,
    )["trade_levels"]["continuation_arm_candidate"]

    assert rebound["setup_key"] == "originating-long"
    assert rebound["setup_name"] == "多方假跌破收復再進"
    assert rebound["entry_role"] == "REENTRY"
    assert rebound["reentry_binding"] == "ORIGINATING_STOPPED_SETUP"
    assert rebound["trigger_level"] == 45863


def test_post_stop_different_strategy_candidate_keeps_its_own_identity() -> None:
    fresh = {
        "setup_key": "fresh-false-break",
        "setup_name": "多方假跌破收復",
        "direction": "LONG",
        "stage": "ARMED",
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "stop_source_time": "2026-08-11T10:28:00+08:00",
    }
    ledger = {"trade_levels": {"continuation_arm_candidate": fresh}}
    previous_memory = {
        "active_setups": [
            {
                "setup_key": "old-compression",
                "name": "多方盤中壓縮突破",
                "direction": "LONG",
                "stage": "ARMED",
                "reentry_status": "AVAILABLE",
            }
        ],
        "reentry": {
            "status": "AVAILABLE",
            "last_stop_at": "2026-08-11T10:12:00+08:00",
            "count": 0,
        },
    }
    position = {
        "status": "FLAT",
        "active_setup_key": "old-compression",
        "last_stop_time": "2026-08-11T10:12:00+08:00",
        "reentry_count": 0,
    }

    result = _bind_continuation_candidate_to_available_reentry(
        ledger,
        previous_memory=previous_memory,
        position=position,
    )

    assert result["trade_levels"]["continuation_arm_candidate"] == fresh


def test_used_reentry_terminalizes_setup_and_suppresses_frozen_candidate() -> None:
    setup_key = "originating-long"
    memory = {
        "version": 3,
        "as_of": "2026-08-11T12:30:00+08:00",
        "active_setups": [
            {
                "setup_key": setup_key,
                "name": "多方修正後複製",
                "direction": "LONG",
                "stage": "ENTRY_ELIGIBLE",
                "reentry_status": "USED",
            }
        ],
        "reentry": {"status": "USED", "last_stop_at": None, "count": 1},
    }
    position = {
        "status": "FLAT",
        "active_setup_key": None,
        "last_stop_time": "2026-08-11T12:31:00+08:00",
        "reentry_count": 1,
    }
    reconciled = _reconcile_program_reentry_memory(
        memory,
        position=position,
        as_of="2026-08-11T12:32:00+08:00",
    )
    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": {
                "setup_key": setup_key,
                "direction": "LONG",
                "stage": "ARMED",
                "entry_role": "REENTRY",
            }
        }
    }

    suppressed = _suppress_exhausted_reentry_candidate(
        ledger,
        previous_memory=reconciled,
        position=position,
    )

    assert reconciled["active_setups"][0]["stage"] == "NO_CHASE"
    assert reconciled["active_setups"][0]["reentry_status"] == "USED"
    assert suppressed["trade_levels"]["continuation_arm_candidate"] is None
    assert suppressed["trade_levels"]["exhausted_reentry_candidate"]["setup_key"] == setup_key


def test_reentry_from_new_pullback_uses_new_correction_stop_not_old_sweep() -> None:
    payload = {
        "course_reading": {"working_quadrant": "TRANSITION"},
        "action": {
            "position_action": "ENTER",
            "entry_role": "REENTRY",
            "direction": "SHORT",
            "setup_key": "originating-false-break",
            "stop_price": 45074.6,
        },
    }
    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": {
                "setup_key": "originating-false-break",
                "direction": "SHORT",
                "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
                "stop_source_price": 45068.0,
            },
            "latest_false_break_reentry": {
                "direction": "BEAR",
                "breach_extreme": 45277.0,
            },
        }
    }

    _validate_v3_trade_levels(payload, ledger=ledger, position={"status": "FLAT"})


def test_continuation_candidate_is_not_bound_without_available_same_direction_reentry() -> None:
    candidate = {
        "setup_key": "new-bear",
        "setup_name": "空方修正後複製",
        "direction": "SHORT",
        "stage": "ARMED",
        "stop_source_time": "2026-08-26T11:29:00+08:00",
    }
    ledger = {"trade_levels": {"continuation_arm_candidate": candidate}}
    previous_memory = {
        "active_setups": [
            {
                "setup_key": "originating-long",
                "name": "原多方",
                "direction": "LONG",
                "stage": "ARMED",
                "reentry_status": "AVAILABLE",
            }
        ],
        "reentry": {
            "status": "AVAILABLE",
            "last_stop_at": "2026-08-26T11:28:00+08:00",
            "count": 0,
        },
    }
    position = {
        "status": "FLAT",
        "active_setup_key": "originating-long",
        "last_stop_time": "2026-08-26T11:28:00+08:00",
        "reentry_count": 0,
    }

    unchanged = _bind_continuation_candidate_to_available_reentry(
        ledger,
        previous_memory=previous_memory,
        position=position,
    )

    assert unchanged["trade_levels"]["continuation_arm_candidate"] == candidate


def test_runtime_marks_degraded_child_under_opposite_small_dow_as_cclass_reset() -> None:
    ledger = {
        "anchor_lifecycle": {
            "child_anchor": {"direction": "BULL", "status": "DEGRADED_RECLAIMED"},
            "reverse_candidate": None,
            "dow_context": {"small_state": "BEAR"},
        }
    }

    constraints = _annotate_course_cclass_constraint(ledger)["course_constraints"]

    assert constraints["required_cclass_mode"] == "RESETTING"
    assert constraints["taiji_current_role"] == "REBUILDING_NOT_ORDERED_COPY"


def test_runtime_does_not_force_cclass_reset_after_reverse_anchor_exists() -> None:
    ledger = {
        "anchor_lifecycle": {
            "child_anchor": {"direction": "BULL", "status": "DEGRADED"},
            "reverse_candidate": {"direction": "BEAR"},
            "dow_context": {"small_state": "BEAR"},
        }
    }

    constraints = _annotate_course_cclass_constraint(ledger)["course_constraints"]

    assert constraints["required_cclass_mode"] is None


def test_open_position_setup_cannot_be_retired_or_dropped_from_memory() -> None:
    position = {
        "status": "LONG",
        "active_setup_key": "held-long",
    }
    with pytest.raises(SemanticReplayError, match="不得標成NO_CHASE"):
        _validate_held_setup_continuity(
            {"active_setups": [{"setup_key": "held-long", "stage": "NO_CHASE"}]},
            position=position,
        )
    with pytest.raises(SemanticReplayError, match="必須在memory保留"):
        _validate_held_setup_continuity(
            {"active_setups": []},
            position=position,
        )
    _validate_held_setup_continuity(
        {"active_setups": [{"setup_key": "held-long", "stage": "CONSERVATIVE_CONFIRMED"}]},
        position=position,
    )
    _validate_held_setup_continuity(
        {"active_setups": [{"setup_key": "held-long", "stage": "INVALIDATED"}]},
        position=position,
        analysis={"action": {"position_action": "EXIT"}},
    )


def test_hybrid_held_setup_advances_to_confirmed_without_changing_program_identity() -> None:
    candidate = {
        "setup_key": "held-long",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 44899.0,
        "valid_bars": 2,
    }
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
        },
        "trade_levels": {"continuation_arm_candidate": candidate},
    }
    memory = {
        "active_setups": [
            {
                **candidate,
                "stage": "AGGRESSIVE_CONFIRMED",
            }
        ]
    }
    position = {
        "status": "LONG",
        "direction": "LONG",
        "active_setup_key": "held-long",
    }

    _validate_program_owned_actionable_setups(
        {"action": {"position_action": "NONE"}},
        memory,
        ledger=ledger,
        position=position,
        entry_gate={"status": "NONE"},
        preopen=False,
    )

    still_armed = copy.deepcopy(memory)
    still_armed["active_setups"][0]["stage"] = "ARMED"
    with pytest.raises(SemanticReplayError, match="已成交持倉"):
        _validate_program_owned_actionable_setups(
            {"action": {"position_action": "NONE"}},
            still_armed,
            ledger=ledger,
            position=position,
            entry_gate={"status": "NONE"},
            preopen=False,
        )

    moved_trigger = copy.deepcopy(memory)
    moved_trigger["active_setups"][0]["trigger_level"] = 44900.0
    with pytest.raises(SemanticReplayError, match="觸發價"):
        _validate_program_owned_actionable_setups(
            {"action": {"position_action": "NONE"}},
            moved_trigger,
            ledger=ledger,
            position=position,
            entry_gate={"status": "NONE"},
            preopen=False,
        )


def test_explicit_exit_terminalizes_setup_and_prevents_initial_retrigger() -> None:
    memory = {
        "as_of": "2026-08-21T11:22:00+08:00",
        "active_setups": [
            {
                "setup_key": "short-copy",
                "name": "空方修正後複製",
                "direction": "SHORT",
                "stage": "CONSERVATIVE_CONFIRMED",
                "trigger": "收盤跌破45152",
                "trigger_level": 45152.0,
                "trigger_operator": "CLOSE_BELOW",
                "valid_bars": 3,
                "reentry_status": "NOT_APPLICABLE",
            }
        ],
    }
    position = {"status": "SHORT", "active_setup_key": "short-copy"}

    _terminalize_explicit_exit_setup(
        {"action": {"position_action": "EXIT", "setup_key": "short-copy"}},
        memory,
        position=position,
    )

    setup = memory["active_setups"][0]
    assert setup["stage"] == "INVALIDATED"
    assert setup["reentry_status"] == "NOT_APPLICABLE"
    assert "不得沿用原觸發" in setup["trigger"]
    gate = derive_entry_eligibility(
        memory,
        [
            {
                "time": "2026-08-21T11:23:00+08:00",
                "open": 45126.0,
                "high": 45132.0,
                "low": 45116.0,
                "close": 45124.0,
            },
            {
                "time": "2026-08-21T11:24:00+08:00",
                "open": 45125.0,
                "high": 45133.0,
                "low": 45085.0,
                "close": 45089.0,
            },
        ],
        as_of="2026-08-21T11:24:00+08:00",
        position={"status": "FLAT", "pending_entry": None},
    )
    assert gate["status"] == "NONE"


def test_stop_does_not_terminalize_setup_needed_for_one_reentry() -> None:
    memory = {
        "active_setups": [
            {
                "setup_key": "stopped-long",
                "stage": "CONSERVATIVE_CONFIRMED",
                "reentry_status": "AVAILABLE",
            }
        ]
    }

    _terminalize_explicit_exit_setup(
        {"action": {"position_action": "STOP", "setup_key": "stopped-long"}},
        memory,
        position={"status": "LONG", "active_setup_key": "stopped-long"},
    )

    assert memory["active_setups"][0]["stage"] == "CONSERVATIVE_CONFIRMED"


def test_v3_upgrade_rejects_stale_working_anchor_and_transition_background() -> None:
    as_of = "2026-08-26T09:34:00+08:00"
    ledger = build_evidence_ledger(
        _upgrade_structured(as_of),
        bars=_upgrade_bars(),
        expected_as_of=as_of,
        session_key="2026-08-26:DAY",
    )
    event_ref = next(item["id"] for item in ledger["structure_events"] if item["event_type"] == "GRADE_UPGRADE")
    new_events = evidence_events(None, ledger)
    flat = initial_position_state(as_of=as_of, version=2)

    stale = _v3_payload(ledger, as_of=as_of, structure_event_ref=event_ref)
    stale["analysis"]["course_reading"]["working_anchor_ref"] = stale["analysis"]["course_reading"]["large_anchor_ref"]
    with pytest.raises(SemanticReplayError, match="小級工作錨"):
        validate_semantic_envelope(
            stale,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=flat,
            evidence_events=new_events,
        )

    transition = _v3_payload(ledger, as_of=as_of, structure_event_ref=event_ref)
    transition["analysis"]["course_reading"]["background_quadrant"] = "TRANSITION"
    with pytest.raises(SemanticReplayError, match="大級背景象限"):
        validate_semantic_envelope(
            transition,
            ledger=ledger,
            expected_as_of=as_of,
            expected_session_key="2026-08-26:DAY",
            preopen=False,
            position=flat,
            evidence_events=new_events,
        )


def test_large_quadrant_without_background_anchor_requires_active_grade_upgrade() -> None:
    reading = {
        "controlling_grade": "LARGE",
        "structure_event_ref": "S-upgrade",
    }
    ledger = {
        "structure_events": [
            {
                "id": "S-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "from_level": "SMALL",
                "to_level": "LARGE",
            }
        ]
    }

    assert _large_grade_upgrade_quadrant_authority(reading, ledger=ledger)
    assert _large_grade_upgrade_quadrant_authority(
        {**reading, "structure_event_ref": "S-other"},
        ledger=ledger,
    )
    assert not _large_grade_upgrade_quadrant_authority(
        {**reading, "controlling_grade": "SMALL"},
        ledger=ledger,
    )
    assert not _large_grade_upgrade_quadrant_authority(
        reading,
        ledger={"structure_events": [{**ledger["structure_events"][0], "event_type": "LEG_OBSERVED"}]},
    )
    assert not _large_grade_upgrade_quadrant_authority(
        reading,
        ledger={
            "structure_events": ledger["structure_events"]
            + [
                {
                    "id": "S-down",
                    "event_type": "GRADE_DOWNGRADE",
                    "source_upgrade_event_id": "S-upgrade",
                }
            ]
        },
    )


def test_ai_hybrid_can_adopt_still_active_upgrade_without_replaying_event_card() -> None:
    as_of = "2026-08-26T10:10:00+08:00"
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
            "trade_direction_policy": "LONG_ONLY",
        },
        "anchor_lifecycle": {"background_anchor": None},
        "anchor_control": {
            "active_background_anchor_ref": None,
            "active_child_anchor_ref": None,
            "working_leg_ref": None,
            "reverse_anchor_candidate_ref": None,
            "large_defense_ref": None,
            "small_defense_ref": None,
        },
        "structure_events": [
            {
                "id": "S-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "source_anchor_id": "L-upgraded-controller",
                "replacement_defense_pivot_id": "P-large-defense",
                "first_seen_at": "2026-08-26T09:34:00+08:00",
            }
        ],
        "structural_legs": [],
        "confirmed_pivots": [
            {
                "id": "P-large-defense",
                "kind": "LOW",
                "bar_time": "2026-08-26T09:25:00+08:00",
                "price": 44957.0,
                "first_seen_at": "2026-08-26T09:27:00+08:00",
            }
        ],
    }
    reading = {
        "large_anchor_ref": "L-upgraded-controller",
        "small_anchor_ref": None,
        "working_anchor_ref": None,
        "reverse_anchor_candidate_ref": None,
        "large_defense_ref": "P-large-defense",
        "small_defense_ref": None,
        "structure_event_ref": None,
        "controlling_grade": "LARGE",
        "grade_relation": "ONLY_LARGE",
        "background_quadrant": "Q1",
        "working_quadrant": "Q4",
        "primary_quadrant_candidate": "Q4",
        "secondary_quadrant_candidate": "Q1",
        "background_trend_dynamics": "INCREASING",
        "background_volatility_dynamics": "EXPANDING",
        "working_trend_dynamics": "INCREASING",
        "working_volatility_dynamics": "CONTRACTING",
        "focus_methods": ["QUADRANT", "TAIJI"],
        "cclass_mode": "TAIJI_ORDERED",
        "taiji": "多方父代修正後重新複製。",
        "yizhi": "一之不接管。",
        "left_right": "多方右側再發動。",
        "dow": "大級多方防線仍有效。",
        "x_stage": "ENTRY_EXECUTION",
        "x_process": "完成Q4觸發核對。",
        "primary_lens": "大級多方背景中的Q4。",
        "main_strategy": "多方Q4積極型拉回反轉。",
        "main_strategy_family": "Q4",
        "setup_stage": "ENTRY_ELIGIBLE",
        "strategy_reason": ["作用中的升級結構仍支持多方。"],
    }
    memory_control = {
        "active_large_anchor_ref": "L-upgraded-controller",
        "active_small_anchor_ref": None,
        "working_anchor_ref": None,
        "active_reverse_candidate_ref": None,
        "controlling_grade": "LARGE",
        "background_quadrant": "Q1",
        "working_quadrant": "Q4",
        "background_quadrant_changed_at": as_of,
        "working_quadrant_changed_at": as_of,
        "last_structure_event_ref": None,
    }

    assert _reading_v3(reading, ledger=ledger)["large_anchor_ref"] == "L-upgraded-controller"
    assert (
        _structure_control(memory_control, ledger=ledger)["active_large_anchor_ref"]
        == "L-upgraded-controller"
    )


def test_no_large_anchor_canonicalizes_model_large_quadrant_and_memory() -> None:
    as_of = "2026-08-21T12:26:00+08:00"
    prior_changed_at = "2026-08-21T08:46:00+08:00"
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": None,
            "quadrant_context": {"authority": "EVIDENCE_ONLY"},
        },
        "structure_events": [],
    }
    reading = {
        "controlling_grade": "SMALL",
        "structure_event_ref": None,
        "background_quadrant": "Q2",
        "background_trend_dynamics": "DECREASING",
        "background_volatility_dynamics": "EXPANDING",
    }
    _canonicalize_unavailable_background_quadrant(reading, ledger=ledger)

    assert reading["background_quadrant"] == "UNDEFINED"
    assert reading["background_trend_dynamics"] == "UNCLEAR"
    assert reading["background_volatility_dynamics"] == "UNCLEAR"

    analysis = {"course_reading": reading}
    memory = {
        "session_key": "2026-08-21:DAY",
        "structure_control": {
            "background_quadrant": "Q2",
            "background_quadrant_changed_at": as_of,
        },
    }
    previous_memory = {
        "session_key": "2026-08-21:DAY",
        "structure_control": {
            "background_quadrant": "UNDEFINED",
            "background_quadrant_changed_at": prior_changed_at,
        },
    }
    _canonicalize_unavailable_background_quadrant_memory(
        analysis,
        memory,
        ledger=ledger,
        previous_memory=previous_memory,
        expected_as_of=as_of,
    )

    assert memory["structure_control"]["background_quadrant"] == "UNDEFINED"
    assert (
        memory["structure_control"]["background_quadrant_changed_at"]
        == prior_changed_at
    )


def test_ai_hybrid_no_large_anchor_clears_invented_background() -> None:
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
            "trade_direction_policy": "LONG_ONLY",
        },
        "anchor_lifecycle": {
            "background_anchor": None,
            "quadrant_context": {"authority": "EVIDENCE_ONLY"},
        },
        "structure_events": [],
    }
    reading = {
        "large_anchor_ref": None,
        "controlling_grade": "SMALL",
        "structure_event_ref": None,
        "background_quadrant": "Q1",
        "background_trend_dynamics": "INCREASING",
        "background_volatility_dynamics": "EXPANDING",
    }

    _canonicalize_unavailable_background_quadrant(reading, ledger=ledger)

    assert reading["background_quadrant"] == "UNDEFINED"
    assert reading["background_trend_dynamics"] == "UNCLEAR"
    assert reading["background_volatility_dynamics"] == "UNCLEAR"


def test_ai_hybrid_hidden_program_tick_may_normalize_internal_background() -> None:
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
            "trade_direction_policy": "LONG_ONLY",
        },
        "anchor_lifecycle": {
            "background_anchor": None,
            "quadrant_context": {"authority": "EVIDENCE_ONLY"},
        },
        "structure_events": [],
    }
    reading = {
        "large_anchor_ref": None,
        "controlling_grade": "SMALL",
        "structure_event_ref": None,
        "background_quadrant": "Q1",
        "background_trend_dynamics": "INCREASING",
        "background_volatility_dynamics": "EXPANDING",
    }

    _canonicalize_unavailable_background_quadrant(
        reading,
        ledger=ledger,
        ai_generated=False,
    )

    assert reading["background_quadrant"] == "UNDEFINED"
    assert reading["background_trend_dynamics"] == "UNCLEAR"
    assert reading["background_volatility_dynamics"] == "UNCLEAR"


def test_new_anchor_keeps_quadrant_transitional_until_post_anchor_leg_completes() -> None:
    as_of = "2026-08-21T08:50:00+08:00"
    previous_time = "2026-08-21T08:48:00+08:00"
    analysis = {
        "course_reading": {
            "working_quadrant": "Q1",
            "primary_quadrant_candidate": "Q1",
            "focus_methods": ["QUADRANT", "TAIJI"],
            "cclass_mode": "TAIJI_ORDERED",
            "taiji": "模型把形成中修正當成已完成太極。",
            "primary_lens": "模型直接以Q1為主鏡頭。",
        }
    }
    memory = {
        "session_key": "2026-08-21:DAY",
        "structure_control": {
            "working_quadrant": "Q1",
            "working_quadrant_changed_at": as_of,
        },
    }
    previous_memory = {
        "session_key": "2026-08-21:DAY",
        "structure_control": {
            "working_quadrant": "UNDEFINED",
            "working_quadrant_changed_at": previous_time,
        },
    }
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": None,
            "child_anchor": {"id": "ANCHOR-opening"},
            "quadrant_context": {
                "authority": "EVIDENCE_ONLY",
                "same_grade_comparisons": {
                    "legs": [
                        {
                            "direction": "BEAR",
                            "status": "FORMING",
                        }
                    ]
                },
            },
        }
    }

    _canonicalize_pre_structure_working_quadrant(
        analysis,
        memory,
        ledger=ledger,
        previous_memory=previous_memory,
        expected_as_of=as_of,
    )

    reading = analysis["course_reading"]
    assert reading["working_quadrant"] == "TRANSITION"
    assert reading["primary_quadrant_candidate"] == "Q1"
    assert reading["focus_methods"] == ["X_PROCESS"]
    assert reading["cclass_mode"] == "UNDEFINED"
    assert "尚無完成" in reading["taiji"]
    assert memory["structure_control"]["working_quadrant"] == "TRANSITION"
    assert memory["structure_control"]["working_quadrant_changed_at"] == as_of


def test_completed_post_anchor_leg_keeps_analyst_quadrant() -> None:
    as_of = "2026-08-21T09:00:00+08:00"
    analysis = {
        "course_reading": {
            "working_quadrant": "Q1",
            "primary_quadrant_candidate": "Q1",
            "focus_methods": ["QUADRANT", "TAIJI"],
            "cclass_mode": "TAIJI_ORDERED",
            "taiji": "已完成修正，正在複製。",
            "primary_lens": "太極與Q1。",
        }
    }
    memory = {
        "session_key": "2026-08-21:DAY",
        "structure_control": {
            "working_quadrant": "Q1",
            "working_quadrant_changed_at": as_of,
        },
    }
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": {"id": "ANCHOR-bear"},
            "quadrant_context": {
                "authority": "EVIDENCE_ONLY",
                "same_grade_comparisons": {
                    "legs": [{"direction": "BEAR", "status": "LOCAL_CONFIRMED"}]
                },
            },
        }
    }

    _canonicalize_pre_structure_working_quadrant(
        analysis,
        memory,
        ledger=ledger,
        previous_memory=None,
        expected_as_of=as_of,
    )

    assert analysis["course_reading"]["working_quadrant"] == "Q1"
    assert analysis["course_reading"]["focus_methods"] == ["QUADRANT", "TAIJI"]


def test_ai_hybrid_keeps_early_probabilistic_quadrant_without_silent_rewrite() -> None:
    as_of = "2026-08-21T08:54:00+08:00"
    analysis = {
        "course_reading": {
            "working_quadrant": "Q1",
            "primary_quadrant_candidate": "Q1",
            "focus_methods": ["QUADRANT", "TAIJI"],
            "cclass_mode": "TAIJI_ORDERED",
            "taiji": "太極：形成中的修正支持Q1候選。",
            "primary_lens": "定錨後依形成中工作段判讀Q1候選。",
        }
    }
    memory = {
        "session_key": "2026-08-21:DAY",
        "structure_control": {
            "working_quadrant": "Q1",
            "working_quadrant_changed_at": as_of,
        },
    }
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
            "trade_direction_policy": "LONG_ONLY",
        },
        "anchor_lifecycle": {
            "background_anchor": None,
            "child_anchor": {"id": "ANCHOR-opening"},
            "quadrant_context": {
                "authority": "EVIDENCE_ONLY",
                "same_grade_comparisons": {
                    "legs": [{"direction": "BEAR", "status": "FORMING"}]
                },
            },
        },
    }

    _canonicalize_pre_structure_working_quadrant(
        analysis,
        memory,
        ledger=ledger,
        previous_memory=None,
        expected_as_of=as_of,
        ai_generated=True,
    )

    assert analysis["course_reading"]["working_quadrant"] == "Q1"
    assert analysis["course_reading"]["focus_methods"] == ["QUADRANT", "TAIJI"]
    assert analysis["course_reading"]["cclass_mode"] == "TAIJI_ORDERED"
    assert memory["structure_control"]["working_quadrant"] == "Q1"


def test_one_point_five_r_without_better_structure_requires_near_cost_stop() -> None:
    position = {
        "status": "SHORT",
        "entry_time": "2026-08-21T09:01:00+08:00",
        "entry_price": 44848.0,
        "stop_price": 44924.0,
        "behavior_plan": {"initial_stop_price": 44924.0},
    }
    bars = [
        {"time": "2026-08-21T09:01:00+08:00", "open": 44848, "high": 44884, "low": 44813, "close": 44819},
        {"time": "2026-08-21T09:02:00+08:00", "open": 44809, "high": 44860, "low": 44795, "close": 44840},
        {"time": "2026-08-21T09:03:00+08:00", "open": 44832, "high": 44868, "low": 44766, "close": 44770},
        {"time": "2026-08-21T09:04:00+08:00", "open": 44771, "high": 44775, "low": 44681, "close": 44714},
    ]
    candidates = apply_profit_milestone_protection(
        {
            "LONG": None,
            "SHORT": {"direction": "SHORT", "stop_price": 44924.0},
        },
        position=position,
        bars=bars,
        as_of="2026-08-21T09:04:00+08:00",
    )

    candidate = candidates["SHORT"]
    assert candidate["stop_price"] == 44848.0
    assert candidate["protection_required"] is True
    assert candidate["max_favorable_r"] > 1.5

    payload = {
        "original_decision": "NOTIFY",
        "message_type": "MANAGEMENT",
        "action": {
            "position_action": "NONE",
            "stop_price": 44924.0,
            "structural_stop": "原始停損",
            "management": "維持原始停損",
        },
    }
    _apply_required_profit_protection(
        payload,
        ledger={"trade_levels": {"position_protection_candidates": candidates}},
        position=position,
    )

    assert payload["action"]["stop_price"] == 44848.0
    assert "+1.5R" in payload["action"]["structural_stop"]
    assert "不得放寬" in payload["action"]["management"]


def test_new_favorable_defense_after_entry_requires_program_stop_improvement() -> None:
    position = {
        "status": "LONG",
        "entry_time": "2026-08-21T09:01:00+08:00",
        "entry_price": 100.0,
        "stop_price": 90.0,
        "behavior_plan": {"initial_stop_price": 90.0},
    }
    structural = {
        "direction": "LONG",
        "stop_price": 95.0,
        "source_defense_id": "DEF-new",
        "source_time": "2026-08-21T09:02:00+08:00",
        "defense_first_seen_at": "2026-08-21T09:03:00+08:00",
    }
    candidates = apply_profit_milestone_protection(
        {"LONG": structural, "SHORT": None},
        position=position,
        bars=[
            {"time": "2026-08-21T09:01:00+08:00", "open": 100, "high": 104, "low": 99, "close": 102},
            {"time": "2026-08-21T09:03:00+08:00", "open": 102, "high": 108, "low": 101, "close": 106},
        ],
        as_of="2026-08-21T09:03:00+08:00",
    )

    candidate = candidates["LONG"]
    assert candidate["protection_required"] is True
    assert candidate["protection_reason"] == "NEW_FAVORABLE_DEFENSE"

    payload = {
        "original_decision": "DONT_NOTIFY",
        "message_type": "UNCHANGED",
        "action": {
            "position_action": "NONE",
            "stop_price": 90.0,
            "structural_stop": "原始停損",
            "management": "維持原始停損",
        },
    }
    _apply_required_profit_protection(
        payload,
        ledger={"trade_levels": {"position_protection_candidates": candidates}},
        position=position,
    )

    assert payload["action"]["stop_price"] == 95.0
    assert payload["message_type"] == "MANAGEMENT"
    assert payload["original_decision"] == "NOTIFY"
    assert "09:02" in payload["action"]["structural_stop"]


def test_defense_visible_before_entry_is_reference_only() -> None:
    position = {
        "status": "SHORT",
        "entry_time": "2026-08-21T09:03:00+08:00",
        "entry_price": 100.0,
        "stop_price": 110.0,
        "behavior_plan": {"initial_stop_price": 110.0},
    }
    candidates = apply_profit_milestone_protection(
        {
            "LONG": None,
            "SHORT": {
                "direction": "SHORT",
                "stop_price": 105.0,
                "source_defense_id": "DEF-old",
                "defense_first_seen_at": "2026-08-21T09:02:00+08:00",
            },
        },
        position=position,
        bars=[
            {"time": "2026-08-21T09:03:00+08:00", "open": 100, "high": 101, "low": 96, "close": 97}
        ],
        as_of="2026-08-21T09:03:00+08:00",
    )

    assert candidates["SHORT"]["protection_required"] is False
    assert candidates["SHORT"]["protection_reason"] == "REFERENCE_ONLY"


def test_better_structural_protection_outranks_near_cost_milestone() -> None:
    position = {
        "status": "LONG",
        "entry_time": "2026-08-21T09:01:00+08:00",
        "entry_price": 100.0,
        "stop_price": 90.0,
        "behavior_plan": {"initial_stop_price": 90.0},
    }
    structural = {
        "direction": "LONG",
        "stop_price": 105.0,
        "source_defense_id": "DEF-new",
        "source_time": "2026-08-21T09:02:00+08:00",
        "defense_first_seen_at": "2026-08-21T09:02:00+08:00",
    }
    candidates = apply_profit_milestone_protection(
        {"LONG": structural, "SHORT": None},
        position=position,
        bars=[
            {"time": "2026-08-21T09:01:00+08:00", "open": 100, "high": 104, "low": 99, "close": 102},
            {"time": "2026-08-21T09:02:00+08:00", "open": 102, "high": 116, "low": 101, "close": 110},
        ],
        as_of="2026-08-21T09:02:00+08:00",
    )

    assert candidates["LONG"]["stop_price"] == structural["stop_price"]
    assert candidates["LONG"]["protection_required"] is True
    assert candidates["LONG"]["protection_reason"] == "NEW_FAVORABLE_DEFENSE"
def test_model_self_correction_debris_is_rejected_before_position_memory() -> None:
    debris_samples = [
        "守住則防範反彈。等等？ 修正：收回後觀察。",
        "守住則收縮期待。いいえ、最後の応答を修正します。",
        "守住則收縮期待。 However, I must now correct the final response.",
        "守住則收縮期待。\u200b收回後觀察。",
    ]
    for sample in debris_samples:
        with pytest.raises(SemanticReplayError, match="殘片"):
            _reject_corrupt_text_fragments({"reaction": sample})


def test_new_active_background_upgrade_owns_simultaneous_reversal_events() -> None:
    downgrade = {
        "id": "S-old-bear-down",
        "event_type": "GRADE_DOWNGRADE",
        "direction": "BEAR",
        "first_seen_at": "2026-08-25T11:28:00+08:00",
    }
    upgrade = {
        "id": "S-new-bull-up",
        "event_type": "GRADE_UPGRADE",
        "direction": "BULL",
        "first_seen_at": "2026-08-25T11:28:00+08:00",
    }
    ledger = {
        "anchor_control": {"active_background_anchor_ref": "ANCHOR-new-bull"},
        "anchor_lifecycle": {
            "background_anchor": {
                "id": "ANCHOR-new-bull",
                "status": "ACTIVE",
                "direction": "BULL",
            }
        },
    }

    assert _latest_material_structure_event(
        [downgrade, upgrade],
        ledger=ledger,
    ) == upgrade


def test_same_direction_new_grade_upgrade_reinforces_entry_gate() -> None:
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": {
                "id": "ANCHOR-new-bull",
                "status": "ACTIVE",
                "direction": "BULL",
            }
        },
        "structure_events": [
            {
                "id": "S-new-bull-up",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "from_level": "SMALL",
                "to_level": "LARGE",
            }
        ],
    }
    gate = {"status": "ENTRY_ELIGIBLE", "direction": "LONG"}
    evidence = [{"event_id": "S-new-bull-up"}]

    assert _aligned_entry_grade_upgrade(
        ledger=ledger,
        evidence_events=evidence,
        entry_gate=gate,
    )
    assert not _aligned_entry_grade_upgrade(
        ledger=ledger,
        evidence_events=[],
        entry_gate=gate,
    )
    assert not _aligned_entry_grade_upgrade(
        ledger=ledger,
        evidence_events=evidence,
        entry_gate={**gate, "direction": "SHORT"},
    )
    assert not _has_explicit_entry_risk_limit(ledger=ledger, entry_gate=gate)
    assert _has_explicit_entry_risk_limit(
        ledger=ledger,
        entry_gate={**gate, "risk_limit": {"max_risk_amount": 1200}},
    )


def test_promoted_structure_does_not_manufacture_a_formal_large_anchor() -> None:
    reading = {
        "controlling_grade": "LARGE",
        "structure_event_ref": None,
    }
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": None,
            "quadrant_context": {"authority": "EVIDENCE_ONLY"},
        },
        "structure_events": [
            {
                "id": "S-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "from_level": "SMALL",
                "to_level": "LARGE",
            }
        ],
    }

    assert _large_grade_upgrade_quadrant_authority(reading, ledger=ledger)
    assert ledger["anchor_lifecycle"]["background_anchor"] is None


def test_promoted_structure_keeps_large_direction_and_known_quadrants() -> None:
    as_of = "2026-08-26T10:00:00+08:00"
    reading = {
        "controlling_grade": "UNDEFINED",
        "background_quadrant": "UNDEFINED",
        "working_quadrant": "UNDEFINED",
        "primary_quadrant_candidate": "UNDEFINED",
        "background_trend_dynamics": "UNCLEAR",
        "background_volatility_dynamics": "UNCLEAR",
        "working_trend_dynamics": "UNCLEAR",
        "working_volatility_dynamics": "UNCLEAR",
    }
    ledger = {
        "structure_events": [
            {
                "id": "S-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "first_seen_at": "2026-08-26T09:34:00+08:00",
            }
        ]
    }
    trend = _large_trend_for_promoted_structure(
        {"classification": "盤整", "details": ["模型忘記升級狀態。"]},
        {**reading, "controlling_grade": "LARGE"},
        ledger=ledger,
    )
    assert trend["classification"] == "偏多但回檔"
    assert "09:34多方結構升級仍作用中" in trend["details"][0]

    analysis = {
        "large_trend": {"classification": "盤整", "details": ["模型忘記升級狀態。"]},
        "course_reading": dict(reading),
    }
    memory = {
        "session_key": "2026-08-26:DAY",
        "notes": ["本輪沒有新事件；控制級數依錨生命週期修正為小級。", "保留其他因果備註。"],
        "structure_control": {
            "controlling_grade": "UNDEFINED",
            "background_quadrant": "UNDEFINED",
            "working_quadrant": "UNDEFINED",
            "background_quadrant_changed_at": as_of,
            "working_quadrant_changed_at": as_of,
        },
    }
    previous_memory = {
        "session_key": "2026-08-26:DAY",
        "structure_control": {
            "controlling_grade": "LARGE",
            "background_quadrant": "Q1",
            "working_quadrant": "Q4",
            "background_quadrant_changed_at": "2026-08-26T09:34:00+08:00",
            "working_quadrant_changed_at": "2026-08-26T09:58:00+08:00",
        },
    }
    _preserve_promoted_structure_quadrants(
        analysis,
        memory,
        ledger=ledger,
        previous_memory=previous_memory,
    )
    normalized = analysis["course_reading"]
    assert normalized["controlling_grade"] == "LARGE"
    assert memory["structure_control"]["controlling_grade"] == "LARGE"
    assert analysis["large_trend"]["classification"] == "強勢偏多"
    assert normalized["background_quadrant"] == "Q1"
    assert normalized["background_trend_dynamics"] == "INCREASING"
    assert normalized["background_volatility_dynamics"] == "EXPANDING"
    assert normalized["working_quadrant"] == "Q4"
    assert normalized["working_trend_dynamics"] == "INCREASING"
    assert normalized["working_volatility_dynamics"] == "CONTRACTING"
    assert memory["structure_control"]["background_quadrant_changed_at"] == "2026-08-26T09:34:00+08:00"
    assert memory["structure_control"]["working_quadrant_changed_at"] == "2026-08-26T09:58:00+08:00"
    assert memory["notes"] == [
        "保留其他因果備註。",
        "控制級數維持大級；只有程式化降級或父級失效事件可解除。",
    ]


@pytest.mark.parametrize(
    ("direction", "dynamics", "expected"),
    [
        ("BULL", "INCREASING", "強勢偏多"),
        ("BULL", "DECREASING", "偏多但回檔"),
        ("BEAR", "INCREASING", "強勢偏空"),
        ("BEAR", "DECREASING", "偏空但反彈"),
    ],
)
def test_promoted_structure_large_trend_follows_its_own_trend_axis(
    direction: str, dynamics: str, expected: str
) -> None:
    ledger = {
        "structure_events": [
            {
                "id": "S-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": direction,
                "from_level": "SMALL",
                "to_level": "LARGE",
                "first_seen_at": "2026-08-26T09:34:00+08:00",
            }
        ]
    }
    reading = {
        "controlling_grade": "LARGE",
        "background_trend_dynamics": dynamics,
    }

    normalized = _large_trend_for_promoted_structure(
        {"classification": "偏多但回檔", "details": ["模型沿用舊標籤。"]},
        reading,
        ledger=ledger,
    )

    assert normalized["classification"] == expected


def test_opposite_local_upgrade_cannot_override_active_large_controller() -> None:
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": {
                "id": "ANCHOR-bull",
                "direction": "BULL",
                "status": "ACTIVE",
            },
            "dow_context": {
                "large_state": "BULL",
                "small_state": "BULL_WITH_BEAR_REVERSAL",
            },
            "quadrant_context": {
                "authority": "PROGRAM_POLICY_V1",
                "background_primary": "UNDEFINED",
                "background_trend_dynamics": "UNCLEAR",
                "working_primary": "Q1",
                "working_structure_direction": "BULL",
                "working_direction": "BULL",
            },
        },
        "structure_events": [
            {
                "id": "S-bull-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "first_seen_at": "2026-08-12T10:43:00+08:00",
            },
            {
                "id": "S-newer-local-bear-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BEAR",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "first_seen_at": "2026-08-12T11:38:00+08:00",
            },
        ],
    }
    analysis = {
        "large_trend": {"classification": "盤整", "details": ["程式初始值。"]},
        "current_trend": {"classification": "轉換中", "details": ["程式初始值。"]},
    }

    _canonicalize_program_market_trends(analysis, ledger=ledger)
    normalized = _large_trend_for_promoted_structure(
        analysis["large_trend"],
        {
            "controlling_grade": "LARGE",
            "background_trend_dynamics": "INCREASING",
        },
        ledger=ledger,
    )

    assert analysis["large_trend"]["classification"] == "強勢偏多"
    assert normalized["classification"] == "強勢偏多"
    assert "程式大級道氏=BULL" in normalized["details"][0]


def test_large_reversal_candidate_keeps_intact_background_direction() -> None:
    analysis = {
        "large_trend": {"classification": "盤整", "details": []},
        "current_trend": {"classification": "轉換中", "details": []},
    }
    ledger = {
        "anchor_lifecycle": {
            "background_anchor": {
                "id": "ANCHOR-bull",
                "direction": "BULL",
                "status": "ACTIVE",
            },
            "dow_context": {"large_state": "BULL_WITH_BEAR_REVERSAL"},
            "quadrant_context": {
                "authority": "PROGRAM_POLICY_V1",
                "phase": "CORRECTION",
                "background_primary": "Q2",
                "background_trend_dynamics": "DECREASING",
                "working_primary": "Q2",
                "working_structure_direction": "BULL",
                "working_direction": "BEAR",
            },
        }
    }

    _canonicalize_program_market_trends(analysis, ledger=ledger)

    assert analysis["large_trend"]["classification"] == "偏多但回檔"


def test_large_control_grade_cannot_disappear_without_downgrade_event() -> None:
    as_of = "2026-08-26T09:36:00+08:00"
    previous_control = {
        "active_large_anchor_ref": None,
        "active_small_anchor_ref": None,
        "working_anchor_ref": None,
        "controlling_grade": "LARGE",
        "background_quadrant": "Q1",
        "working_quadrant": "Q1",
        "background_quadrant_changed_at": "2026-08-26T09:34:00+08:00",
        "working_quadrant_changed_at": "2026-08-26T09:34:00+08:00",
        "last_structure_event_ref": "S-upgrade",
    }
    current_control = {
        **previous_control,
        "controlling_grade": "SMALL",
        "background_quadrant": "UNDEFINED",
        "background_quadrant_changed_at": as_of,
        "last_structure_event_ref": None,
    }
    analysis = {
        "course_reading": {
            "large_anchor_ref": None,
            "small_anchor_ref": None,
            "working_anchor_ref": None,
            "controlling_grade": "SMALL",
            "background_quadrant": "UNDEFINED",
            "working_quadrant": "Q1",
            "structure_event_ref": None,
        }
    }
    memory = {
        "version": 3,
        "session_key": "2026-08-26:DAY",
        "structure_control": current_control,
    }
    previous_memory = {
        "version": 3,
        "as_of": "2026-08-26T09:34:00+08:00",
        "session_key": "2026-08-26:DAY",
        "active_setups": [],
        "structure_control": previous_control,
    }
    with pytest.raises(SemanticReplayError, match="必須持續為LARGE"):
        _validate_v3_state_transition(
            analysis,
            memory,
            ledger={"structure_events": []},
            previous_memory=previous_memory,
            evidence_events=[],
            expected_as_of=as_of,
        )


def test_ai_hybrid_upgrade_may_replace_carried_source_leg_with_installed_background_anchor() -> None:
    as_of = "2026-08-11T13:00:00+08:00"
    previous_control = {
        "active_large_anchor_ref": "L-previous-source",
        "active_small_anchor_ref": "ANCHOR-small",
        "working_anchor_ref": None,
        "controlling_grade": "LARGE",
        "background_quadrant": "Q2",
        "working_quadrant": "Q3",
        "background_quadrant_changed_at": "2026-08-11T12:34:00+08:00",
        "working_quadrant_changed_at": "2026-08-11T12:56:00+08:00",
        "last_structure_event_ref": None,
    }
    current_control = {
        "active_large_anchor_ref": "ANCHOR-installed-background",
        "active_small_anchor_ref": "ANCHOR-small",
        "working_anchor_ref": None,
        "controlling_grade": "LARGE",
        "background_quadrant": "Q1",
        "working_quadrant": "Q1",
        "background_quadrant_changed_at": as_of,
        "working_quadrant_changed_at": as_of,
        "last_structure_event_ref": "S-new-upgrade",
    }
    analysis = {
        "notification_reason": "正式升級後採用已安裝的大錨。",
        "course_reading": {
            "large_anchor_ref": "ANCHOR-installed-background",
            "small_anchor_ref": "ANCHOR-small",
            "working_anchor_ref": None,
            "controlling_grade": "LARGE",
            "background_quadrant": "Q1",
            "working_quadrant": "Q1",
            "structure_event_ref": "S-new-upgrade",
            "x_process": "正式升級。",
            "primary_lens": "大級多方結構",
            "main_strategy": "等待回踩",
        },
    }
    memory = {
        "version": 3,
        "session_key": "2026-08-11:DAY",
        "active_setups": [],
        "structure_control": current_control,
    }
    previous_memory = {
        "version": 3,
        "as_of": "2026-08-11T12:58:00+08:00",
        "session_key": "2026-08-11:DAY",
        "active_setups": [],
        "structure_control": previous_control,
    }
    event = {
        "id": "S-new-upgrade",
        "event_type": "GRADE_UPGRADE",
        "direction": "BULL",
        "source_anchor_id": "L-new-source",
    }
    ledger = {
        "program_trade_policy": {"decision_authority": "AI_HYBRID"},
        "anchor_control": {"active_background_anchor_ref": "ANCHOR-installed-background"},
        "structure_events": [event],
    }

    _validate_v3_state_transition(
        analysis,
        memory,
        ledger=ledger,
        previous_memory=previous_memory,
        evidence_events=[{"event_id": "S-new-upgrade", "event_type": "STRUCTURE_EVENT"}],
        expected_as_of=as_of,
    )


def test_formal_large_anchor_disappearance_releases_large_control_grade() -> None:
    as_of = "2026-08-24T09:58:00+08:00"
    previous_control = {
        "active_large_anchor_ref": "ANCHOR-open-bull",
        "active_small_anchor_ref": "ANCHOR-small-bear",
        "working_anchor_ref": "WORKING-old",
        "controlling_grade": "LARGE",
        "background_quadrant": "Q2",
        "working_quadrant": "Q1",
        "background_quadrant_changed_at": "2026-08-24T09:56:00+08:00",
        "working_quadrant_changed_at": "2026-08-24T09:50:00+08:00",
        "last_structure_event_ref": None,
    }
    current_control = {
        **previous_control,
        "active_large_anchor_ref": None,
        "working_anchor_ref": "WORKING-new",
        "controlling_grade": "SMALL",
        "background_quadrant": "UNDEFINED",
        "background_quadrant_changed_at": as_of,
    }
    analysis = {
        "course_reading": {
            "large_anchor_ref": None,
            "small_anchor_ref": "ANCHOR-small-bear",
            "working_anchor_ref": "WORKING-new",
            "controlling_grade": "SMALL",
            "background_quadrant": "UNDEFINED",
            "working_quadrant": "Q1",
            "structure_event_ref": None,
        }
    }
    memory = {
        "version": 3,
        "session_key": "2026-08-24:DAY",
        "active_setups": [],
        "structure_control": current_control,
    }
    previous_memory = {
        "version": 3,
        "as_of": "2026-08-24T09:56:00+08:00",
        "session_key": "2026-08-24:DAY",
        "active_setups": [],
        "structure_control": previous_control,
    }

    _validate_v3_state_transition(
        analysis,
        memory,
        ledger={
            "anchor_control": {"active_background_anchor_ref": None},
            "structure_events": [],
        },
        previous_memory=previous_memory,
        evidence_events=[],
        expected_as_of=as_of,
    )


def test_program_course_reading_overrides_model_quadrant_taiji_and_x_stage() -> None:
    reading = {
        "background_quadrant": "Q2",
        "working_quadrant": "Q1",
        "primary_quadrant_candidate": "Q1",
        "secondary_quadrant_candidate": "Q2",
        "background_trend_dynamics": "DECREASING",
        "background_volatility_dynamics": "EXPANDING",
        "working_trend_dynamics": "INCREASING",
        "working_volatility_dynamics": "EXPANDING",
        "focus_methods": ["YIZHI"],
        "cclass_mode": "YIZHI_MOMENTUM",
        "x_stage": "RESET",
        "taiji": "模型自行判斷。",
    }
    ledger = {
        "monitoring_session": {"bar_count": 30},
        "anchor_lifecycle": {
            "background_anchor": {"id": "large"},
            "child_anchor": {"id": "small"},
            "quadrant_context": {
                "authority": "PROGRAM_POLICY_V1",
                "background_primary": "Q4",
                "background_trend_dynamics": "INCREASING",
                "background_volatility_dynamics": "CONTRACTING",
                "working_primary": "TRANSITION",
                "working_trend_dynamics": "DECREASING",
                "working_volatility_dynamics": "UNSTABLE",
                "working_candidates": ["Q2", "Q3"],
            },
            "taiji_context": {
                "assessment_authority": "PROGRAM_POLICY_V1",
                "engine_mode": "TAIJI_ORDERED",
                "program_state": "COPY_FORMING",
                "program_quality": "PROVISIONALLY_MIXED",
                "operating_grade": "SMALL",
                "parent_start_time": "2026-08-25T09:00:00+08:00",
                "parent_start_price": 100,
                "parent_end_time": "2026-08-25T09:05:00+08:00",
                "parent_end_price": 120,
            },
        },
        "course_method_state": {
            "authority": "PROGRAM_POLICY_V1",
            "cclass_mode": "YIZHI_MOMENTUM",
            "yizhi": {
                "state": "CENTRIFUGAL_CONFIRMED",
                "direction": "BULL",
                "latest_time": "2026-08-25T09:20:00+08:00",
            },
            "left_right": {"state": "LEFT_CANDIDATE", "direction": "BEAR"},
        },
    }

    _canonicalize_program_course_reading(
        reading,
        ledger=ledger,
        position={"status": "FLAT"},
        entry_gate=None,
        preopen=False,
    )

    assert reading["background_quadrant"] == "Q4"
    assert reading["working_quadrant"] == "TRANSITION"
    assert reading["primary_quadrant_candidate"] == "Q2"
    assert reading["secondary_quadrant_candidate"] == "Q3"
    assert reading["cclass_mode"] == "YIZHI_MOMENTUM"
    assert reading["x_stage"] == "ANCHOR_LENS_SELECTION"
    assert reading["focus_methods"] == ["YIZHI", "X_PROCESS"]
    assert "09:00 100→09:05 120" in reading["taiji"]
    assert reading["yizhi"].startswith("多方離心力成立")
    assert reading["left_right"] == "空方左側反向候選，尚未接管。"


def test_program_course_reading_preserves_preentry_invalidation_strategy() -> None:
    reading = {
        "background_quadrant": "UNDEFINED",
        "working_quadrant": "TRANSITION",
        "focus_methods": ["QUADRANT"],
        "cclass_mode": "UNDEFINED",
        "main_strategy": "模型原始文字",
    }
    ledger = {
        "monitoring_session": {"bar_count": 80},
        "anchor_lifecycle": {
            "background_anchor": {"id": "large", "status": "ACTIVE"},
            "quadrant_context": {
                "authority": "PROGRAM_POLICY_V1",
                "background_primary": "Q4",
                "working_primary": "TRANSITION",
                "working_candidates": ["Q2", "Q4"],
            },
            "dow_context": {"large_state": "BULL", "small_state": "BEAR"},
        },
        "trade_levels": {"continuation_arm_candidate": None},
        "preentry_invalidation_audit": {
            "status": "INVALIDATED_BEFORE_ENTRY",
            "setup_key": "bear-q4-old",
            "setup_name": "空方修正後複製",
            "invalidated_at": "2026-08-24T10:55:00+08:00",
            "stop_price": 45075.0,
            "direction": "SHORT",
            "reason": "STRUCTURAL_STOP_TOUCHED_BEFORE_ENTRY",
        },
    }

    _canonicalize_program_course_reading(
        reading,
        ledger=ledger,
        position={"status": "FLAT"},
        entry_gate=None,
        preopen=False,
    )

    assert reading["x_stage"] == "RESET"
    assert reading["main_strategy"] == "空方修正後複製（進場前失效）。"
    assert "10:55" in reading["x_process"]
    assert "舊setup已退役" in reading["x_process"]


def test_program_course_memory_owns_quadrants_and_preserves_change_time() -> None:
    analysis = {"course_reading": {"background_quadrant": "Q4", "working_quadrant": "Q2"}}
    memory = {"structure_control": {
        "background_quadrant": "Q1",
        "working_quadrant": "Q1",
        "background_quadrant_changed_at": "2026-08-25T09:20:00+08:00",
        "working_quadrant_changed_at": "2026-08-25T09:20:00+08:00",
    }}
    previous = {
        "version": 3,
        "session_key": "day",
        "structure_control": {
            "background_quadrant": "Q4",
            "working_quadrant": "Q4",
            "background_quadrant_changed_at": "2026-08-25T09:10:00+08:00",
            "working_quadrant_changed_at": "2026-08-25T09:10:00+08:00",
        },
    }
    ledger = {"anchor_lifecycle": {"quadrant_context": {"authority": "PROGRAM_POLICY_V1"}}}

    _canonicalize_program_course_memory(
        analysis,
        memory,
        ledger=ledger,
        previous_memory=previous,
        expected_as_of="2026-08-25T09:20:00+08:00",
        expected_session_key="day",
    )

    control = memory["structure_control"]
    assert control["background_quadrant"] == "Q4"
    assert control["background_quadrant_changed_at"] == "2026-08-25T09:10:00+08:00"
    assert control["working_quadrant"] == "Q2"
    assert control["working_quadrant_changed_at"] == "2026-08-25T09:20:00+08:00"


def test_program_course_state_events_publish_only_actual_verdict_changes() -> None:
    previous = {
        "as_of": "2026-08-25T09:19:00+08:00",
        "anchor_lifecycle": {
            "quadrant_context": {
                "authority": "PROGRAM_POLICY_V1",
                "background_primary": "Q4",
                "background_trend_dynamics": "INCREASING",
                "background_volatility_dynamics": "CONTRACTING",
                "working_primary": "Q4",
                "working_trend_dynamics": "INCREASING",
                "working_volatility_dynamics": "CONTRACTING",
            },
            "taiji_context": {
                "assessment_authority": "PROGRAM_POLICY_V1",
                "program_state": "CORRECTION_FORMING",
                "program_quality": "ORDERLY",
                "last_copy_status": "SUCCESS",
                "engine_mode": "TAIJI_ORDERED",
                "dynasty_anchor_ref": "a1",
            },
        },
    }
    unchanged = copy.deepcopy(previous)
    unchanged["as_of"] = "2026-08-25T09:20:00+08:00"
    assert program_course_state_events(previous, unchanged) == []

    changed = copy.deepcopy(unchanged)
    changed["anchor_lifecycle"]["quadrant_context"]["working_primary"] = "Q1"
    changed["anchor_lifecycle"]["quadrant_context"]["working_volatility_dynamics"] = "EXPANDING"
    changed["anchor_lifecycle"]["taiji_context"]["program_state"] = "COPY_FORMING"
    events = program_course_state_events(previous, changed)
    assert [item["event_type"] for item in events] == [
        "PROGRAM_QUADRANT_CHANGED",
        "PROGRAM_TAIJI_CHANGED",
    ]
    assert all(item["event_time"] == changed["as_of"] for item in events)


@pytest.mark.parametrize("scale", [0.1, 1.0, 10.0])
def test_program_yizhi_scan_is_scale_invariant_and_uses_relative_momentum(scale: float) -> None:
    def price(value: float) -> float:
        return 1000 + (value - 100) * scale

    rows = [(100, 102, 99, 101), (101, 103, 100, 102), (102, 107, 101, 106), (106, 113, 105, 112)]
    bars = [
        {
            "time": f"2026-08-25T09:0{index}:00+08:00",
            "open": price(opened),
            "high": price(high),
            "low": price(low),
            "close": price(close),
        }
        for index, (opened, high, low, close) in enumerate(rows)
    ]
    anchor_state = {
        "child_anchor": {
            "id": "a",
            "level": "SMALL",
            "direction": "BULL",
            "status": "ACTIVE",
            "latest_extreme_price": price(103),
        },
        "background_anchor": None,
        "reverse_candidate": None,
        "dow_context": {"small_state": "BULL", "large_state": "UNDEFINED"},
        "taiji_context": {
            "engine_mode": "TAIJI_ORDERED",
            "leg_evidence": {"current_parent": {"slope_points_per_minute": 1 * scale}},
        },
    }

    state = _derive_program_course_methods(anchor_state, bars=bars, opening_ranges={})

    assert state["authority"] == "PROGRAM_POLICY_V2"
    assert state["cclass_mode"] == "YIZHI_MOMENTUM"
    assert state["yizhi"]["state"] == "GOLD_DRAGON"
    assert state["yizhi"]["direction"] == "BULL"


def test_ordered_highs_do_not_confirm_yizhi_without_medium_bodies_and_range_expansion() -> None:
    bars = [
        {"time": "2026-08-11T08:50:00+08:00", "open": 44829, "high": 44893, "low": 44822, "close": 44889},
        {"time": "2026-08-11T08:51:00+08:00", "open": 44895, "high": 44914, "low": 44870, "close": 44881},
        {"time": "2026-08-11T08:52:00+08:00", "open": 44885, "high": 44899, "low": 44873, "close": 44886},
        {"time": "2026-08-11T08:53:00+08:00", "open": 44893, "high": 44950, "low": 44886, "close": 44950},
    ]
    controller = {
        "id": "bull-controller",
        "direction": "BULL",
        "first_extreme_price": 44875,
        "latest_extreme_price": 44950,
    }

    state = _derive_yizhi_state(
        controller,
        taiji={},
        bars=bars,
        opening_ranges={"or5": {"high": 44875, "low": 44757}},
        previous=None,
    )

    assert state["state"] == "NONE"
    assert state["quality"] == "NOT_ESTABLISHED"
    assert "BODIES_NOT_MEDIUM_LONG" in state["reason_codes"]
    assert "RANGE_NOT_EXPANDED" in state["reason_codes"]
    assert "STRUCTURAL_BREAKOUT" in state["reason_codes"]


def test_program_scenario_weights_replace_model_numbers_but_keep_plans() -> None:
    scenario = {
        "bull_probability": 90,
        "range_probability": 5,
        "bear_probability": 5,
        "bull_plan": "模型多方文字",
        "range_plan": "模型盤整文字",
        "bear_plan": "模型空方文字",
        "view_change": "模型切換文字",
    }
    ledger = {
        "course_method_state": {
            "scenario_weights": {
                "bull": 25,
                "range": 50,
                "bear": 25,
                "authority": "PROGRAM_POLICY_V1",
            }
        }
    }

    _canonicalize_program_scenario_weights(scenario, ledger=ledger)

    assert (scenario["bull_probability"], scenario["range_probability"], scenario["bear_probability"]) == (25, 50, 25)
    assert scenario["bull_plan"] == "模型多方文字"


def test_yizhi_cannot_take_control_against_active_anchor_and_has_explicit_failure() -> None:
    anchor = {
        "id": "bear-anchor",
        "direction": "BEAR",
        "latest_extreme_price": 95,
    }
    taiji = {"leg_evidence": {"current_parent": {"slope_points_per_minute": 1}}}
    bullish = [
        {"time": "2026-08-25T09:00:00+08:00", "open": 100, "high": 102, "low": 99, "close": 101},
        {"time": "2026-08-25T09:01:00+08:00", "open": 101, "high": 103, "low": 100, "close": 102},
        {"time": "2026-08-25T09:02:00+08:00", "open": 102, "high": 108, "low": 101, "close": 107},
        {"time": "2026-08-25T09:03:00+08:00", "open": 107, "high": 114, "low": 106, "close": 113},
    ]
    counter = _derive_yizhi_state(anchor, taiji=taiji, bars=bullish, opening_ranges={}, previous=None)
    assert counter["state"] == "COUNTERTREND_ACCELERATION"

    bull_anchor = {**anchor, "id": "bull-anchor", "direction": "BULL", "latest_extreme_price": 103}
    active = _derive_yizhi_state(bull_anchor, taiji=taiji, bars=bullish, opening_ranges={}, previous=None)
    assert active["state"] in {"GOLD_DRAGON", "K_GOLD_DRAGON", "CENTRIFUGAL_CONFIRMED"}
    failed_bars = [
        *bullish,
        {"time": "2026-08-25T09:04:00+08:00", "open": 113, "high": 114, "low": 108, "close": 109},
    ]
    failed = _derive_yizhi_state(
        bull_anchor,
        taiji=taiji,
        bars=failed_bars,
        opening_ranges={},
        previous=active,
    )
    assert failed["state"] == "MOMENTUM_FAILED"


def test_destructive_taiji_correction_forces_cclass_reset() -> None:
    state = _derive_program_course_methods(
        {
            "child_anchor": {"id": "a", "level": "SMALL", "direction": "BULL", "status": "ACTIVE"},
            "background_anchor": None,
            "reverse_candidate": None,
            "dow_context": {"small_state": "BULL", "large_state": "UNDEFINED"},
            "taiji_context": {
                "engine_mode": "TAIJI_ORDERED",
                "program_state": "CORRECTION_DESTRUCTIVE",
                "leg_evidence": {},
            },
            "quadrant_context": {"working_primary": "TRANSITION"},
        },
        bars=[
            {"time": "2026-08-25T09:00:00+08:00", "open": 100, "high": 101, "low": 99, "close": 100},
            {"time": "2026-08-25T09:01:00+08:00", "open": 100, "high": 101, "low": 99, "close": 100},
        ],
        opening_ranges={},
    )
    assert state["cclass_mode"] == "RESETTING"


def test_program_market_trends_and_message_direction_ignore_model_labels() -> None:
    analysis = {
        "large_trend": {"classification": "強勢偏多", "details": ["模型"]},
        "current_trend": {"classification": "偏多", "details": ["模型"]},
        "message_direction": "BULL",
        "action": {"direction": "NONE"},
    }
    ledger = {
        "program_trade_policy": {"decision_authority": "PROGRAM"},
        "anchor_lifecycle": {
            "background_anchor": {"direction": "BEAR", "status": "ACTIVE"},
            "child_anchor": {"direction": "BEAR", "status": "ACTIVE"},
            "quadrant_context": {
                "authority": "PROGRAM_POLICY_V1",
                "phase": "CORRECTION",
                "background_primary": "Q4",
                "working_primary": "Q2",
                "working_structure_direction": "BEAR",
                "working_direction": "BULL",
                "background_trend_dynamics": "INCREASING",
            },
        },
        "trade_levels": {},
    }

    _canonicalize_program_market_trends(analysis, ledger=ledger)
    _canonicalize_program_message_direction(analysis, ledger=ledger, position={"status": "FLAT"})

    assert analysis["large_trend"]["classification"] == "偏空但反彈"
    assert analysis["current_trend"]["classification"] == "偏空"
    assert "小級控制方向=BEAR" in analysis["current_trend"]["details"][0]
    assert "目前工作段方向=BULL" in analysis["current_trend"]["details"][0]
    assert analysis["message_direction"] == "BEAR"


def test_program_market_trend_falls_back_to_controller_before_forming_leg() -> None:
    analysis = {
        "large_trend": {"classification": "盤整", "details": []},
        "current_trend": {"classification": "轉換中", "details": []},
    }
    ledger = {
        "program_trade_policy": {"decision_authority": "PROGRAM"},
        "anchor_lifecycle": {
            "background_anchor": None,
            "child_anchor": {"direction": "BULL", "status": "ACTIVE"},
            "quadrant_context": {
                "authority": "PROGRAM_POLICY_V1",
                "working_primary": "Q4",
                "working_structure_direction": "UNDEFINED",
                "working_direction": "BEAR",
            },
        },
    }

    _canonicalize_program_market_trends(analysis, ledger=ledger)

    assert analysis["current_trend"]["classification"] == "偏多"
    assert "小級控制方向=BULL" in analysis["current_trend"]["details"][0]
    assert "目前工作段方向=BEAR" in analysis["current_trend"]["details"][0]
