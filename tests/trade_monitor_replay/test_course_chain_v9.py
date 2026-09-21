from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trade_monitor_replay.anchor_lifecycle import (
    build_anchor_lifecycle, _new_anchor, _apply_defense_lifecycle,
    _promote_reverse_child, _promote_from_terminated_history,
    _promote_background_through_child_sequence,
    _course_dow_context, _course_quadrant_context,
    _course_taiji_context, _working_leg, _resolve_dow_state, anchor_control,
)
from trade_monitor_replay.config import ReplayConfig
from trade_monitor_replay.deterministic_state import build_evidence_ledger
from trade_monitor_replay.presentation import (
    _anchor_lifecycle_lines,
    _v3_course_method_lines,
    _v3_quadrant_line,
)
from trade_monitor_replay.runner import ReplayRunner
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _large_trend_for_dow,
    _ordered_taiji_conflicts_with_lifecycle,
    _contains_positive_stale_copy_claim,
    _stale_anchor_opposes_current_dow,
    _reading_v3,
    _validate_v3_state_transition,
)


ROOT = Path(__file__).parents[2]
V9_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v9"
    / "rule-manifest.json"
)
V10_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v10"
    / "rule-manifest.json"
)
V11_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v24"
    / "rule-manifest.json"
)


def _bar(at: datetime, opened: float, close: float) -> dict[str, float | str]:
    return {
        "time": at.isoformat(),
        "open": float(opened),
        "high": max(float(opened), float(close)),
        "low": min(float(opened), float(close)),
        "close": float(close),
    }


def _night_bars() -> list[dict[str, float | str]]:
    nodes = [
        ("2026-08-24T23:00:00+08:00", 44350.0),
        ("2026-08-24T23:10:00+08:00", 44276.0),
        ("2026-08-24T23:47:00+08:00", 44615.0),
        ("2026-08-24T23:58:00+08:00", 44494.0),
        ("2026-08-25T00:22:00+08:00", 44633.0),
        ("2026-08-25T01:55:00+08:00", 44460.0),
        ("2026-08-25T02:30:00+08:00", 44658.0),
        ("2026-08-25T03:03:00+08:00", 44550.0),
        ("2026-08-25T03:30:00+08:00", 44600.0),
        ("2026-08-25T03:52:00+08:00", 44461.0),
        ("2026-08-25T03:53:00+08:00", 44453.0),
        ("2026-08-25T04:26:00+08:00", 44529.0),
        ("2026-08-25T04:53:00+08:00", 44504.0),
        ("2026-08-25T04:59:00+08:00", 44534.0),
    ]
    parsed = [(datetime.fromisoformat(at), price) for at, price in nodes]
    bars: list[dict[str, float | str]] = []
    previous = parsed[0][1]
    for index in range(len(parsed) - 1):
        start, start_price = parsed[index]
        end, end_price = parsed[index + 1]
        if index == 0:
            bars.append(_bar(start, previous, start_price))
        minutes = int((end - start).total_seconds() // 60)
        for offset in range(1, minutes + 1):
            at = start + timedelta(minutes=offset)
            close = round(start_price + ((end_price - start_price) * offset / minutes), 4)
            bars.append(_bar(at, previous, close))
            previous = close
    return bars


def _v9_snapshot() -> dict:
    # Unit fixture for a known Type2 event. Its anchors are GIVEN evidence;
    # this is not proof that a fixed-points polygon qualifies n=2 pivots.
    bars = _night_bars()
    background = _new_anchor(
        direction="BULL", origin={"time": "2026-08-24T23:10:00+08:00", "price": 44276.0},
        first_extreme={"time": "2026-08-25T02:30:00+08:00", "price": 44658.0},
        defense={"time": "2026-08-25T01:55:00+08:00", "price": 44460.0},
        first_seen_at="2026-08-25T02:30:00+08:00", session_key="unit", level="LARGE",
    )
    background = _apply_defense_lifecycle(background, bars)
    child = _new_anchor(
        direction="BEAR", origin={"time": "2026-08-25T02:30:00+08:00", "price": 44658.0},
        first_extreme={"time": "2026-08-25T03:53:00+08:00", "price": 44453.0},
        defense={"time": "2026-08-25T03:30:00+08:00", "price": 44600.0},
        first_seen_at="2026-08-25T03:53:00+08:00", session_key="unit", level="SMALL",
    )
    promoted = _promote_reverse_child(background, child, bars=bars, session_key="unit")
    assert promoted is not None
    background["status"] = "REPLACED"
    working = _working_leg(promoted, None, bars, session_key="unit",
                           as_of=datetime.fromisoformat(str(bars[-1]["time"])), threshold=0,
                           strict_course_pivots=True)
    return {"background_anchor": promoted, "child_anchor": child, "working_leg": working,
            "anchor_history": [background], "reverse_candidate": None,
            "dow_context": _course_dow_context(background=promoted, child=child, history=[background],
                small_pivots=[], bars=bars, session_key="unit"),
            "quadrant_context": _course_quadrant_context(background=promoted, child=child, working=working, bars=bars),
            "taiji_context": _course_taiji_context(background=promoted, child=child, working=working, candidate=None, bars=bars)}


def test_type2_child_continuation_promotes_bear_background_at_large_defense_break() -> None:
    snapshot = _v9_snapshot()
    background = snapshot["background_anchor"]
    history = snapshot["anchor_history"]

    assert background["direction"] == "BEAR"
    assert background["origin_time"] == "2026-08-25T02:30:00+08:00"
    assert background["latest_extreme_time"] == "2026-08-25T03:53:00+08:00"
    assert background["takeover_type"] == "TYPE2"
    assert background["first_seen_at"] == "2026-08-25T03:53:00+08:00"
    assert history[0]["direction"] == "BULL"
    assert history[0]["status"] == "REPLACED"
    assert history[0]["defense"]["price"] == 44460.0


def test_type2_takeover_survives_later_origin_breach_of_displaced_parent() -> None:
    bars = [
        _bar(datetime.fromisoformat("2026-08-25T10:50:00+08:00"), 44431, 44210),
        _bar(datetime.fromisoformat("2026-08-25T11:28:00+08:00"), 44414, 44460),
        _bar(datetime.fromisoformat("2026-08-25T11:29:00+08:00"), 44461, 44499),
    ]
    parent = _new_anchor(
        direction="BEAR",
        origin={"time": "2026-08-25T09:54:00+08:00", "price": 44492.0},
        first_extreme={"time": "2026-08-25T10:50:00+08:00", "price": 44210.0},
        defense={"time": "2026-08-25T10:32:00+08:00", "price": 44431.0},
        first_seen_at="2026-08-25T10:56:00+08:00",
        session_key="unit",
        level="LARGE",
    )
    parent = _apply_defense_lifecycle(parent, bars)
    parent.update(status="INVALIDATED", ended_at=bars[-1]["time"])
    child = _new_anchor(
        direction="BULL",
        origin={"time": "2026-08-25T10:50:00+08:00", "price": 44210.0},
        first_extreme={"time": "2026-08-25T11:19:00+08:00", "price": 44427.0},
        defense={"time": "2026-08-25T11:24:00+08:00", "price": 44348.0},
        first_seen_at="2026-08-25T11:16:00+08:00",
        session_key="unit",
        level="SMALL",
    )
    child["latest_extreme_time"] = "2026-08-25T11:29:00+08:00"
    child["latest_extreme_price"] = 44517.0
    child["ended_at"] = "2026-08-25T11:47:00+08:00"

    stale_child = dict(child)
    stale_child["id"] = "old-child"
    stale_child["origin_time"] = "2026-08-25T09:25:00+08:00"
    stale_child["origin_price"] = 44150.0
    stale_child["ended_at"] = "2026-08-25T10:44:00+08:00"

    assert _promote_from_terminated_history(
        [parent],
        stale_child,
        bars=bars,
        session_key="unit",
    ) is None

    promoted = _promote_from_terminated_history(
        [parent],
        child,
        bars=bars,
        session_key="unit",
    )

    assert promoted is not None
    assert promoted["direction"] == "BULL"
    assert promoted["origin_price"] == 44210.0
    assert promoted["latest_extreme_price"] == 44517.0
    assert promoted["takeover_type"] == "TYPE2"


def test_later_same_direction_child_extends_first_promoted_background_without_reanchoring() -> None:
    bars = [
        _bar(datetime.fromisoformat("2026-08-25T11:28:00+08:00"), 44414, 44460),
        _bar(datetime.fromisoformat("2026-08-25T11:52:00+08:00"), 44415, 44394),
        _bar(datetime.fromisoformat("2026-08-25T12:06:00+08:00"), 44582, 44619),
    ]
    parent = _new_anchor(
        direction="BEAR",
        origin={"time": "2026-08-25T09:54:00+08:00", "price": 44492},
        first_extreme={"time": "2026-08-25T10:50:00+08:00", "price": 44210},
        defense={"time": "2026-08-25T10:32:00+08:00", "price": 44431},
        first_seen_at="2026-08-25T10:56:00+08:00",
        session_key="unit",
        level="LARGE",
    )
    parent = _apply_defense_lifecycle(parent, bars)
    first_bull = _new_anchor(
        direction="BULL",
        origin={"time": "2026-08-25T10:50:00+08:00", "price": 44210},
        first_extreme={"time": "2026-08-25T11:19:00+08:00", "price": 44427},
        defense={"time": "2026-08-25T11:24:00+08:00", "price": 44348},
        first_seen_at="2026-08-25T11:28:00+08:00",
        session_key="unit",
        level="SMALL",
    )
    first_bull["latest_extreme_time"] = "2026-08-25T11:34:00+08:00"
    first_bull["latest_extreme_price"] = 44602.0
    first_bull["ended_at"] = "2026-08-25T11:47:00+08:00"
    correction = _new_anchor(
        direction="BEAR",
        origin={"time": "2026-08-25T11:34:00+08:00", "price": 44602},
        first_extreme={"time": "2026-08-25T11:52:00+08:00", "price": 44394},
        defense={"time": "2026-08-25T11:42:00+08:00", "price": 44538},
        first_seen_at="2026-08-25T11:47:00+08:00",
        session_key="unit",
        level="SMALL",
    )
    correction["ended_at"] = "2026-08-25T12:01:00+08:00"
    later_bull = _new_anchor(
        direction="BULL",
        origin={"time": "2026-08-25T11:52:00+08:00", "price": 44394},
        first_extreme={"time": "2026-08-25T12:06:00+08:00", "price": 44619},
        defense={"time": "2026-08-25T11:52:00+08:00", "price": 44394},
        first_seen_at="2026-08-25T12:01:00+08:00",
        session_key="unit",
        level="SMALL",
    )

    current, replaced = _promote_background_through_child_sequence(
        parent,
        [first_bull, correction, later_bull],
        bars=bars,
        session_key="unit",
    )

    assert current["direction"] == "BULL"
    assert current["origin_time"] == "2026-08-25T10:50:00+08:00"
    assert current["origin_price"] == 44210.0
    assert current["latest_extreme_time"] == "2026-08-25T12:06:00+08:00"
    assert current["latest_extreme_price"] == 44619.0
    assert current["defense"]["price"] == 44394.0
    assert len(replaced) == 1


def test_broken_latest_pivot_starts_working_leg_from_prior_opposite_pivot() -> None:
    raw = [
        ("11:52", 44415, 44441, 44394, 44430),
        ("11:53", 44430, 44452, 44425, 44452),
        ("11:54", 44450, 44465, 44431, 44436),
        ("11:55", 44435, 44444, 44422, 44424),
        ("11:56", 44423, 44450, 44423, 44446),
        ("11:57", 44450, 44479, 44447, 44460),
        ("11:58", 44458, 44470, 44423, 44429),
        ("11:59", 44428, 44458, 44425, 44450),
        ("12:00", 44457, 44467, 44430, 44438),
        ("12:01", 44433, 44474, 44427, 44472),
        ("12:02", 44473, 44524, 44469, 44516),
        ("12:03", 44520, 44529, 44504, 44525),
        ("12:04", 44522, 44558, 44520, 44548),
    ]
    bars = [
        {
            "time": f"2026-08-25T{at}:00+08:00",
            "open": float(opened),
            "high": float(high),
            "low": float(low),
            "close": float(close),
        }
        for at, opened, high, low, close in raw
    ]
    background = _new_anchor(
        direction="BULL",
        origin={"time": "2026-08-25T10:50:00+08:00", "price": 44210},
        first_extreme={"time": "2026-08-25T11:34:00+08:00", "price": 44602},
        defense={"time": "2026-08-25T10:50:00+08:00", "price": 44210},
        first_seen_at="2026-08-25T11:28:00+08:00",
        session_key="unit",
        level="LARGE",
    )

    working = _working_leg(
        background,
        None,
        bars,
        session_key="unit",
        as_of=datetime.fromisoformat("2026-08-25T12:04:00+08:00"),
        threshold=0,
        strict_course_pivots=True,
    )

    assert working is not None
    assert working["direction"] == "BULL"
    assert working["start_time"] == "2026-08-25T11:55:00+08:00"
    assert working["start_price"] == 44422.0
    assert working["current_extreme_time"] == "2026-08-25T12:04:00+08:00"
    assert working["current_extreme_price"] == 44558.0


def test_course_dow_keeps_bull_and_bear_views_at_both_grades() -> None:
    context = _v9_snapshot()["dow_context"]

    assert context["large_state"] == "BEAR"
    assert context["small_state"] in {"BEAR", "BEAR_WITH_BULL_REVERSAL"}
    assert context["large_bull_defense"]["price"] == 44460.0
    assert context["large_bull_defense"]["broken_at"] == "2026-08-25T03:53:00+08:00"
    assert context["large_bear_defense"]["price"] == 44658.0
    assert context["large_bear_defense"]["state"] == "ACTIVE"
    assert context["small_bear_defense"]["price"] == 44600.0


def test_course_dow_direction_follows_only_active_defenses() -> None:
    assert _resolve_dow_state(base_direction="BULL", bull_active=False, bear_active=True) == "BEAR"
    assert _resolve_dow_state(base_direction="BULL", bull_active=False, bear_active=False) == "UNDEFINED"
    assert (
        _resolve_dow_state(base_direction="BULL", bull_active=True, bear_active=True)
        == "BULL_WITH_BEAR_REVERSAL"
    )


def test_anchor_control_never_exports_a_broken_defense_as_active() -> None:
    snapshot = {
        "background_anchor": None,
        "child_anchor": {"id": "child", "direction": "BULL"},
        "working_leg": None,
        "reverse_candidate": None,
        "dow_context": {
            "large_state": "UNDEFINED",
            "small_state": "BEAR",
            "small_bull_defense": {"id": "broken-bull", "state": "BROKEN"},
            "small_bear_defense": {"id": "active-bear", "state": "ACTIVE"},
        },
    }

    control = anchor_control(snapshot)

    assert control["small_defense_ref"] == "active-bear"


def test_public_child_anchor_preserves_degraded_reclaimed_lifecycle() -> None:
    lifecycle = {
        "background_anchor": None,
        "reverse_candidate": None,
        "working_leg": None,
        "child_anchor": {
            "id": "child",
            "record_type": "ANCHOR",
            "level": "SMALL",
            "direction": "BULL",
            "status": "DEGRADED_RECLAIMED",
            "origin_time": "2026-08-25T09:25:00+08:00",
            "origin_price": 44150.0,
            "latest_extreme_time": "2026-08-25T09:54:00+08:00",
            "latest_extreme_price": 44492.0,
            "amplitude_points": 342.0,
            "duration_minutes": 29,
        },
        "dow_context": {"large_state": "UNDEFINED", "small_state": "BEAR"},
    }

    rendered = "\n".join(_anchor_lifecycle_lines(lifecycle))

    assert "小錨：09:25低44,150點→09:54高44,492點（+342／29分，防線收復、背景仍降級）" in rendered


def test_public_working_leg_uses_current_dow_direction_not_stale_anchor_role() -> None:
    lifecycle = {
        "background_anchor": None,
        "child_anchor": None,
        "reverse_candidate": None,
        "working_leg": {
            "direction": "BULL",
            "status": "FORMING",
            "role": "BACKGROUND_RETEST",
            "start_time": "2026-08-25T10:10:00+08:00",
            "start_price": 44238.0,
            "current_extreme_time": "2026-08-25T10:12:00+08:00",
            "current_extreme_price": 44314.0,
            "amplitude_points": 76.0,
            "duration_minutes": 2,
        },
        "dow_context": {"large_state": "UNDEFINED", "small_state": "BEAR"},
    }

    rendered = "\n".join(_anchor_lifecycle_lines(lifecycle))

    assert "目前工作段：10:10低44,238點→10:12高44,314點（+76／2分，反向修正）" in rendered


def test_ordered_taiji_rejects_stale_degraded_anchor_direction() -> None:
    lifecycle = {
        "child_anchor": {"direction": "BULL", "status": "DEGRADED_RECLAIMED"},
        "reverse_candidate": None,
        "dow_context": {"small_state": "BEAR"},
    }

    assert _ordered_taiji_conflicts_with_lifecycle(lifecycle, "TAIJI_ORDERED") is True
    assert _ordered_taiji_conflicts_with_lifecycle(lifecycle, "RESETTING") is False
    assert _stale_anchor_opposes_current_dow(lifecycle) is True
    assert _contains_positive_stale_copy_claim("目前是形成中的多方複製") is True
    assert _contains_positive_stale_copy_claim("不是舊多方朝代的形成中複製") is False


def test_quadrant_and_taiji_context_start_from_promoted_anchor() -> None:
    snapshot = _v9_snapshot()
    quadrant = snapshot["quadrant_context"]
    taiji = snapshot["taiji_context"]

    assert quadrant["anchor_direction"] == "BEAR"
    assert quadrant["phase"] == "CORRECTION"
    assert quadrant["background_primary"] == "UNDEFINED"
    assert quadrant["background_candidates"] == []
    assert quadrant["authority"] == "PROGRAM_POLICY_V1"
    assert taiji["dynasty_anchor_ref"] == snapshot["child_anchor"]["id"]
    assert taiji["background_dynasty_anchor_ref"] == snapshot["background_anchor"]["id"]
    assert taiji["parent_start_time"] == "2026-08-25T02:30:00+08:00"
    assert taiji["parent_end_time"] == "2026-08-25T03:53:00+08:00"
    assert taiji["current_relation"] == "CORRECTION"


def test_course_chain_recomputes_every_new_closed_bar_without_replacing_anchor() -> None:
    night = _night_bars()
    day_0845 = _bar(datetime.fromisoformat("2026-08-25T08:45:00+08:00"), 44534.0, 44550.0)
    day_0846 = _bar(datetime.fromisoformat("2026-08-25T08:46:00+08:00"), 44550.0, 44570.0)
    first = build_anchor_lifecycle(
        [*night, day_0845],
        expected_as_of="2026-08-25T08:45:00+08:00",
        session_key="2026-08-25:TRADE_DAY",
        course_chain_enabled=True,
    )
    second = build_anchor_lifecycle(
        [*night, day_0845, day_0846],
        expected_as_of="2026-08-25T08:46:00+08:00",
        session_key="2026-08-25:TRADE_DAY",
        course_chain_enabled=True,
    )

    # Equal high/low plateaux cannot qualify strict n=2 just because they
    # reverse more than 120 points. Real-OHLC incremental cases live in the
    # course-audit regression suite.
    assert first["background_anchor"] is None
    assert second["background_anchor"] is None
    assert first["thresholds"] == second["thresholds"] == {}


def test_v9_public_lines_show_dual_dow_and_hide_cclass_label() -> None:
    snapshot = _v9_snapshot()
    rendered = "\n".join(_anchor_lifecycle_lines(snapshot))
    quadrant = _v3_quadrant_line({"background_quadrant": "Q4", "working_quadrant": "Q3"}, {"anchor_lifecycle": snapshot})
    methods = _v3_course_method_lines(
        {
            "cclass_mode": "TAIJI_ORDERED",
            "taiji": "空方父代02:30高44,658點至03:53低44,453點完成；目前是反彈修正。",
            "yizhi": "未出現異常動能接管。",
            "x_stage": "PREOPEN_CONTEXT",
            "x_process": "等待日盤開盤證據。",
        },
        {"anchor_lifecycle": snapshot},
    )

    assert "大錨：02:30高44,658點→03:53低44,453點" in rendered
    assert "小錨：02:30高44,658點→03:53低44,453點" not in rendered
    assert "大級道氏：偏空" in rendered
    assert "多防01:55低44,460點（03:53失守）" in rendered
    assert "空防02:30高44,658點（作用中）" in rendered
    assert "主看UNDEFINED" in quadrant
    assert "Q4" not in quadrant and "Q3" not in quadrant
    assert methods[0].startswith("太極：")
    assert all("C班" not in line for line in methods)


def test_public_quadrant_does_not_promote_small_anchor_to_large_grade() -> None:
    lifecycle = {
        "background_anchor": None,
        "quadrant_context": {
            "anchor_direction": "BEAR",
            "phase": "CORRECTION",
            "background_primary": "Q2",
            "background_confidence": "CANDIDATE",
            "background_candidates": ["Q2", "Q4"],
            "working_structure_direction": "BULL",
            "working_phase": "PUSH",
            "working_primary": "TRANSITION",
            "working_confidence": "CANDIDATE",
            "working_candidates": ["Q4", "Q1"],
        },
    }

    rendered = _v3_quadrant_line({}, {"anchor_lifecycle": lifecycle})

    assert rendered == "象限：大級尚未形成｜小級空方修正中，主看Q2候選；次看Q4"
    assert "大級空方" not in rendered


def test_public_quadrant_keeps_small_transition_candidates_visible() -> None:
    lifecycle = {
        "background_anchor": None,
        "quadrant_context": {
            "authority": "PROGRAM_POLICY_V1",
            "anchor_direction": "BEAR",
            "phase": "PUSH",
            "background_primary": "TRANSITION",
            "background_confidence": "TRANSITIONAL",
            "background_candidates": ["Q1", "Q4"],
            "working_structure_direction": "BEAR",
            "working_phase": "PUSH",
            "working_primary": "TRANSITION",
            "working_candidates": ["Q1", "Q4"],
        },
    }

    rendered = _v3_quadrant_line({}, {"anchor_lifecycle": lifecycle})

    assert rendered == "象限：大級尚未形成｜小級空方推進中，主看Q1候選；次看Q4"


def test_quadrant_uses_active_small_anchor_not_opposite_forming_leg_as_structure_direction() -> None:
    anchor = {
        "id": "ANCHOR-BEAR",
        "level": "SMALL",
        "direction": "BEAR",
        "origin_time": "2026-08-21T10:54:00+08:00",
        "origin_price": 45291.0,
        "latest_extreme_time": "2026-08-21T11:15:00+08:00",
        "latest_extreme_price": 45069.0,
        "first_seen_at": "2026-08-21T11:13:00+08:00",
        "amplitude_points": -222.0,
    }
    correction = {
        "id": "WORKING-BULL",
        "direction": "BULL",
        "start_time": "2026-08-21T11:15:00+08:00",
        "start_price": 45069.0,
        "current_extreme_time": "2026-08-21T11:16:00+08:00",
        "current_extreme_price": 45131.0,
        "amplitude_points": 62.0,
    }
    bars = [
        {"time": "2026-08-21T10:54:00+08:00", "open": 45279, "high": 45291, "low": 45250, "close": 45253},
        {"time": "2026-08-21T11:15:00+08:00", "open": 45079, "high": 45118, "low": 45069, "close": 45109},
        {"time": "2026-08-21T11:16:00+08:00", "open": 45110, "high": 45131, "low": 45082, "close": 45124},
    ]

    quadrant = _course_quadrant_context(
        background=anchor,
        child=None,
        working=correction,
        bars=bars,
    )

    assert quadrant["working_structure_direction"] == "BEAR"
    assert quadrant["working_direction"] == "BULL"
    assert quadrant["working_phase"] == "CORRECTION"


def test_v9_semantic_contract_rejects_quadrant_detached_from_anchor() -> None:
    bars = _night_bars()
    latest = bars[-1]
    ledger = build_evidence_ledger(
        {
            "latest_closed_k": {**latest, "volume": 1.0},
            "indicators": {"sma21": 44520.0, "sma105": 44580.0, "atr14": 20.0},
            "opening_ranges": {"or5": None, "or15": None},
            "causal_structure_n2": {
                "dow_small": "TRANSITION",
                "dow_large": "TRANSITION",
                "recent_confirmed_pivots": [],
                "recent_large_pivots": [],
            },
        },
        bars=bars,
        expected_as_of=str(latest["time"]),
        session_key="2026-08-25:NIGHT",
        anchor_lifecycle_enabled=True,
        course_chain_enabled=True,
    )
    control = ledger["anchor_control"]
    reading = {
        "large_anchor_ref": control["active_background_anchor_ref"],
        "small_anchor_ref": control["active_child_anchor_ref"],
        "working_anchor_ref": control["working_leg_ref"],
        "reverse_anchor_candidate_ref": control["reverse_anchor_candidate_ref"],
        "large_defense_ref": control["large_defense_ref"],
        "small_defense_ref": control["small_defense_ref"],
        "structure_event_ref": None,
        "controlling_grade": "LARGE",
        "grade_relation": "ALIGNED",
        "background_quadrant": "Q4",
        "working_quadrant": "Q4",
        "primary_quadrant_candidate": "Q4",
        "secondary_quadrant_candidate": "Q1",
        "background_trend_dynamics": "INCREASING",
        "background_volatility_dynamics": "CONTRACTING",
        "working_trend_dynamics": "INCREASING",
        "working_volatility_dynamics": "CONTRACTING",
        "focus_methods": ["TAIJI", "DOW"],
        "cclass_mode": "TAIJI_ORDERED",
        "taiji": "空方大錨為父代，目前是父代後的反彈修正。",
        "yizhi": "沒有異常動能接管。",
        "left_right": "仍在空方防線內。",
        "dow": "大級與小級空方道氏作用中。",
        "x_stage": "PREOPEN_CONTEXT",
        "x_process": "等待日盤開盤證據。",
        "primary_lens": "空方定錨後的Q4修正。",
        "main_strategy": "盤前不追價。",
        "setup_stage": "FORMING",
        "strategy_reason": ["等待日盤重新發動。"],
    }

    assert _reading_v3(reading, ledger=ledger)["background_quadrant"] == "Q4"
    detached = dict(reading)
    detached["background_quadrant"] = "Q2"
    detached["background_trend_dynamics"] = "DECREASING"
    detached["background_volatility_dynamics"] = "EXPANDING"
    assert _reading_v3(detached, ledger=ledger)["background_quadrant"] == "Q2"
    detached["background_volatility_dynamics"] = "CONTRACTING"
    with pytest.raises(SemanticReplayError, match="兩軸"):
        _reading_v3(detached, ledger=ledger)


def test_v9_normalizes_range_label_to_program_owned_bearish_background() -> None:
    trend = {"classification": "盤整", "details": ["空方大錨已接管，目前正在反彈修正。"]}
    normalized = _large_trend_for_dow(trend, {"large_state": "BEAR"})

    assert normalized["classification"] == "偏空但反彈"
    assert normalized["details"] == trend["details"]


def test_session_without_large_anchor_cannot_claim_directional_large_trend() -> None:
    trend = {"classification": "強勢偏多", "details": ["日盤只有小級開盤結構。"]}
    normalized = _large_trend_for_dow(trend, {"large_state": "UNDEFINED"})

    assert normalized["classification"] == "盤整"
    assert normalized["details"] == trend["details"]


def test_program_owned_anchor_quadrant_can_change_before_legacy_hysteresis() -> None:
    previous_time = "2026-08-25T08:46:00+08:00"
    current_time = "2026-08-25T08:50:00+08:00"
    previous_memory = {
        "version": 3,
        "session_key": "2026-08-25:DAY",
        "active_setups": [],
        "structure_control": {
            "background_quadrant": "Q4",
            "working_quadrant": "Q4",
            "background_quadrant_changed_at": previous_time,
            "working_quadrant_changed_at": previous_time,
        },
    }
    memory = {
        "version": 3,
        "session_key": "2026-08-25:DAY",
        "structure_control": {
            "background_quadrant": "Q1",
            "working_quadrant": "Q1",
            "background_quadrant_changed_at": current_time,
            "working_quadrant_changed_at": current_time,
        },
    }
    analysis = {
        "message_type": "OBSERVATION",
        "course_reading": {
            "background_quadrant": "Q1",
            "working_quadrant": "Q1",
        },
    }
    ledger = {
        "anchor_lifecycle": {
            "quadrant_context": {
                "background_primary": "Q1",
                "phase": "PUSH",
            }
        }
    }

    _validate_v3_state_transition(
        analysis,
        memory,
        ledger=ledger,
        previous_memory=previous_memory,
        evidence_events=[],
        expected_as_of=current_time,
    )


def test_v9_runner_uses_isolated_course_chain_contract() -> None:
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=V9_MANIFEST, ai_provider="codex", ai_model="fake"),
        analyzer=object(),
    )

    assert runner.rules.contract_version == 6
    assert runner.anchor_lifecycle_contract is True
    assert runner.course_chain_contract is True
    assert runner.execution_version == "replay-execution-v99-isolated-no-trade-minute"
    assert runner.rules.prompt_sha256 == "78a3487e90235e4b83ea835d30572b9207e41eb2f7d13fce5984f20e80209469"


def test_v10_keeps_formal_rules_read_only_and_uses_course_chain_contract() -> None:
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=V10_MANIFEST, ai_provider="codex", ai_model="fake"),
        analyzer=object(),
    )

    assert runner.rules.version == "course-state-v2.1.8-replay-adapter-v10"
    assert runner.rules.contract_version == 6
    assert runner.anchor_lifecycle_contract is True
    assert runner.course_chain_contract is True
    assert runner.rules.prompt_sha256 == "78a3487e90235e4b83ea835d30572b9207e41eb2f7d13fce5984f20e80209469"


def test_v11_program_owns_quadrant_taiji_without_editing_formal_rules() -> None:
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=V11_MANIFEST, ai_provider="codex", ai_model="fake"),
        analyzer=object(),
    )

    assert runner.rules.version == "course-state-v2.1.8-replay-adapter-v24"
    assert runner.rules.contract_version == 6
    assert runner.course_chain_contract is True
    assert runner.execution_version == "replay-execution-v99-isolated-no-trade-minute"
    assert runner.rules.prompt_sha256 == "78a3487e90235e4b83ea835d30572b9207e41eb2f7d13fce5984f20e80209469"
