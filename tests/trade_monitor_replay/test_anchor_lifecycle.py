from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import trade_monitor_replay.anchor_lifecycle as lifecycle_module

from trade_monitor_replay.anchor_lifecycle import (
    _anchored_leg_evidence, _derive_directional_anchor, _extend_active_anchor_extreme, _reverse_candidate,
    _working_leg, build_anchor_lifecycle,
)
from trade_monitor_replay.config import ReplayConfig
from trade_monitor_replay.deterministic_state import build_evidence_ledger
from trade_monitor_replay.presentation import _anchor_lifecycle_lines
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _reading_v3,
    _validate_setup_trigger_levels,
    _validate_preparation_contract,
    initial_position_state,
    validate_semantic_envelope,
)
from trade_monitor_replay.runner import ReplayRunner


ROOT = Path(__file__).parents[2]
V8_MANIFEST = ROOT / "trade_monitor_replay" / "rules" / "course-state-v2.1.8-replay-adapter-v8" / "rule-manifest.json"


@pytest.mark.parametrize("direction", ["BULL", "BEAR"])
def test_anchor_requires_causal_close_not_wick_and_does_not_backdate(direction: str) -> None:
    start = datetime.fromisoformat("2026-08-25T09:00:00+08:00")
    def stamp(minute: int) -> str:
        return (start + timedelta(minutes=minute)).isoformat()
    def price(value: float) -> float:
        return value if direction == "BULL" else 1000 - value
    kinds = ["LOW", "HIGH", "LOW", "HIGH"] if direction == "BULL" else ["HIGH", "LOW", "HIGH", "LOW"]
    pivots = [
        {"kind": kind, "time": stamp(at), "price": price(value),
         "confirmed": confirmed, "confirmation_time": stamp(known) if known is not None else None}
        for kind, at, value, confirmed, known in zip(
            kinds, [0, 2, 4, 6], [100, 200, 130, 220],
            [True, True, True, False], [1, 3, 6, None],
        )
    ]
    def derive(bars: list[dict]) -> dict | None:
        return _derive_directional_anchor(
            pivots, bars=bars, session_key="fixture", level="LARGE",
            require_closed_confirmation=True,
        )
    # The close beyond B at minute 5 cannot qualify C before its confirmation.
    bars = [{"time": stamp(5), "close": price(210)},
            {"time": stamp(6), "close": price(180)},
            {"time": stamp(7), "close": price(200)}]
    assert derive(bars) is None  # wick at 220 and equality at 200 are not a break
    bars.append({"time": stamp(8), "close": price(205)})
    anchor = derive(bars)
    assert anchor is not None
    assert anchor["first_seen_at"] == stamp(8)
    assert anchor["defense"]["first_seen_at"] == stamp(8)
    assert anchor["latest_extreme_time"] == stamp(6)  # actual wick time stays factual
    assert derive(bars[:-1]) is None  # removing future confirmation cannot retain it


def test_newer_same_grade_directional_defense_is_not_overwritten_by_old_child(monkeypatch) -> None:
    old_child_defense = {
        "id": "old-child",
        "direction": "BEAR",
        "time": "2026-08-20T10:35:00+08:00",
        "price": 44791.0,
        "first_seen_at": "2026-08-20T10:52:00+08:00",
        "state": "ACTIVE",
    }
    newer_directional = {
        "id": "new-directional",
        "direction": "BEAR",
        "time": "2026-08-20T11:20:00+08:00",
        "price": 44712.0,
        "first_seen_at": "2026-08-20T11:27:00+08:00",
        "state": "ACTIVE",
        "level": "SMALL",
        "role": "DIRECTIONAL_SLOT",
    }
    monkeypatch.setattr(
        lifecycle_module,
        "_directional_defenses",
        lambda *args, **kwargs: {"BEAR": newer_directional},
    )

    context = lifecycle_module._course_dow_context(
        background=None,
        child={
            "id": "child-bear",
            "direction": "BEAR",
            "qualification": "N2",
            "defense": old_child_defense,
        },
        history=[],
        small_pivots=[],
        bars=[],
        session_key="2026-08-20:DAY",
    )

    assert context["small_bear_defense"]["id"] == "new-directional"
    assert context["small_bear_defense"]["price"] == 44712.0


@pytest.mark.parametrize("mirror", [False, True])
def test_opposite_correction_does_not_replace_control_anchor_until_defense_break(
    mirror: bool,
) -> None:
    """A newer opposite A/B/C/D is a correction until control actually transfers."""

    start = datetime.fromisoformat("2026-08-24T09:00:00+08:00")

    def stamp(minute: int) -> str:
        return (start + timedelta(minutes=minute)).isoformat()

    def price(value: float) -> float:
        return 1000.0 - value if mirror else value

    base_kinds = ["LOW", "HIGH", "LOW", "HIGH", "LOW", "HIGH", "LOW"]
    kinds = (
        ["HIGH" if kind == "LOW" else "LOW" for kind in base_kinds]
        if mirror
        else base_kinds
    )
    pivots = [
        {
            "kind": kind,
            "time": stamp(at),
            "price": price(value),
            "confirmed": True,
            "confirmation_time": stamp(known),
        }
        for kind, at, value, known in zip(
            kinds,
            [0, 2, 4, 6, 8, 10, 12],
            [100, 200, 130, 220, 160, 210, 150],
            [1, 3, 5, 7, 9, 11, 13],
        )
    ]

    def bar(minute: int, close: float) -> dict[str, float | str]:
        center = price(close)
        edge_a = price(close - 1)
        edge_b = price(close + 1)
        return {
            "time": stamp(minute),
            "open": center,
            "high": max(edge_a, edge_b),
            "low": min(edge_a, edge_b),
            "close": center,
        }

    bars_before_break = [bar(5, 205), bar(7, 190), bar(11, 170), bar(12, 150)]
    active = _derive_directional_anchor(
        pivots,
        bars=bars_before_break,
        session_key="fixture",
        level="SMALL",
        require_closed_confirmation=True,
    )

    original_direction = "BEAR" if mirror else "BULL"
    opposite_direction = "BULL" if mirror else "BEAR"
    assert active is not None
    assert active["direction"] == original_direction
    assert active["origin_time"] == stamp(0)

    after_break = _derive_directional_anchor(
        pivots,
        bars=[*bars_before_break, bar(13, 120)],
        session_key="fixture",
        level="SMALL",
        require_closed_confirmation=True,
    )

    assert after_break is not None
    assert after_break["direction"] == opposite_direction
    assert after_break["first_seen_at"] == stamp(13)
    assert after_break["candidate_established_at"] == stamp(12)
    assert after_break["takeover_reason"] == "OPPOSITE_STRUCTURE_BROKE_ACTIVE_DEFENSE"


def test_0914_wick_does_not_create_large_bull_anchor() -> None:
    start = datetime.fromisoformat("2026-08-25T09:07:00+08:00")
    ohlc = [
        (44205, 44289, 44176, 44266), (44266, 44347, 44198, 44345),
        (44340, 44377, 44310, 44343), (44354, 44368, 44288, 44309),
        (44320, 44352, 44271, 44273), (44282, 44327, 44233, 44322),
        (44316, 44365, 44297, 44365), (44359, 44387, 44308, 44330),
    ]
    bars = [dict(time=(start + timedelta(minutes=i)).isoformat(),
                 open=o, high=h, low=l, close=c)
            for i, (o, h, l, c) in enumerate(ohlc)]
    snapshot = build_anchor_lifecycle(
        bars, expected_as_of=str(bars[-1]["time"]), session_key="fixture",
        course_chain_enabled=True,
    )
    assert snapshot["background_anchor"] is None


@pytest.mark.parametrize("mirror", [False, True])
def test_candidate_recognition_does_not_truncate_confirmed_working_pivot(mirror: bool) -> None:
    start = datetime.fromisoformat("2026-08-25T09:07:00+08:00")
    ohlc = [
        (44205, 44289, 44176, 44266), (44266, 44347, 44198, 44345),
        (44340, 44377, 44310, 44343), (44354, 44368, 44288, 44309),
        (44320, 44352, 44271, 44273), (44282, 44327, 44233, 44322),
        (44316, 44365, 44297, 44365), (44359, 44387, 44308, 44330),
        (44325, 44343, 44260, 44302), (44305, 44358, 44294, 44350),
        (44341, 44387, 44322, 44376), (44380, 44383, 44329, 44351),
    ]
    def price(value: float) -> float:
        return 100000 - value if mirror else value
    bars = [dict(time=(start + timedelta(minutes=i)).isoformat(), open=price(o),
                 high=price(l if mirror else h), low=price(h if mirror else l), close=price(c))
            for i, (o, h, l, c) in enumerate(ohlc)]
    background = dict(anchor_origin_kind="SESSION_OPEN", direction="BULL" if mirror else "BEAR",
                      latest_extreme_time="2026-08-25T08:45:00+08:00", latest_extreme_price=price(44372))
    candidate = dict(direction="BEAR" if mirror else "BULL", first_seen_at="2026-08-25T09:16:00+08:00",
                     start_time="2026-08-25T09:07:00+08:00", start_price=price(44176),
                     current_extreme_time="2026-08-25T09:14:00+08:00", current_extreme_price=price(44387))
    before = _working_leg(background, candidate, bars[:10], session_key="fixture",
                          as_of=datetime.fromisoformat(str(bars[9]["time"])), threshold=30, strict_course_pivots=True)
    assert before["start_time"] == "2026-08-25T09:14:00+08:00"
    assert before["role"] == "REVERSE_CANDIDATE_PULLBACK"
    after = _working_leg(background, candidate, bars, session_key="fixture",
                         as_of=datetime.fromisoformat(str(bars[-1]["time"])), threshold=30, strict_course_pivots=True)
    assert after["start_time"] == "2026-08-25T09:15:00+08:00"
    assert after["start_price"] == price(44260)
    assert after["current_extreme_time"] == "2026-08-25T09:17:00+08:00"
    assert after["current_extreme_price"] == price(44387)
    assert after["role"] == "REVERSE_CANDIDATE_CONTINUATION"


@pytest.mark.parametrize("direction", ["BULL", "BEAR"])
def test_wick_extension_keeps_previous_defense_until_close_confirms(direction: str) -> None:
    start = datetime.fromisoformat("2026-08-25T09:00:00+08:00")
    def stamp(minute: int) -> str:
        return (start + timedelta(minutes=minute)).isoformat()
    def price(value: float) -> float:
        return value if direction == "BULL" else 1000 - value
    kinds = ["LOW", "HIGH", "LOW", "HIGH", "LOW", "HIGH"]
    if direction == "BEAR":
        kinds = ["HIGH" if kind == "LOW" else "LOW" for kind in kinds]
    pivots = [
        {"kind": kind, "time": stamp(at), "price": price(value),
         "confirmed": known is not None, "confirmation_time": stamp(known) if known else None}
        for kind, at, value, known in zip(
            kinds, [0, 2, 4, 6, 8, 10], [100, 200, 130, 210, 150, 230], [1, 3, 5, 7, 9, None],
        )
    ]
    bars = [{"time": stamp(6), "close": price(205)},
            {"time": stamp(8), "close": price(160)},
            {"time": stamp(10), "close": price(208)}]
    def derive() -> dict:
        return _derive_directional_anchor(
            pivots, bars=bars, session_key="fixture", level="LARGE", require_closed_confirmation=True,
        )
    before = derive()
    assert before["latest_extreme_price"] == price(230)
    assert before["defense"]["price"] == price(130)
    bars.append({"time": stamp(11), "close": price(225)})
    after = derive()
    assert after["id"] == before["id"]
    assert after["defense"]["price"] == price(150)
    assert after["defense"]["first_seen_at"] == stamp(11)


@pytest.mark.parametrize("direction", ["BULL", "BEAR"])
def test_active_anchor_extreme_cannot_regress_when_later_pivot_confirms(
    direction: str,
) -> None:
    def price(value: float) -> float:
        return value if direction == "BULL" else 1000.0 - value

    anchor = {
        "id": "ANCHOR-stable",
        "direction": direction,
        "origin_time": "2026-08-26T10:06:00+08:00",
        "origin_price": price(100.0),
        "first_extreme_time": "2026-08-26T10:49:00+08:00",
        "first_extreme_price": price(200.0),
        "latest_extreme_time": "2026-08-26T10:49:00+08:00",
        "latest_extreme_price": price(200.0),
        "defense": {"price": price(130.0)},
    }
    bars = [
        {
            "time": "2026-08-26T10:06:00+08:00",
            "high": max(price(100.0), price(110.0)),
            "low": min(price(100.0), price(110.0)),
        },
        {
            "time": "2026-08-26T10:45:00+08:00",
            "high": max(price(220.0), price(190.0)),
            "low": min(price(220.0), price(190.0)),
        },
        {
            "time": "2026-08-26T10:49:00+08:00",
            "high": max(price(200.0), price(195.0)),
            "low": min(price(200.0), price(195.0)),
        },
    ]

    _extend_active_anchor_extreme(anchor, bars)

    assert anchor["latest_extreme_time"] == "2026-08-26T10:45:00+08:00"
    assert anchor["latest_extreme_price"] == price(220.0)
    assert anchor["defense"]["price"] == price(130.0)


def test_structural_child_early_return_also_preserves_favorable_raw_extreme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    structural = {
        "id": "ANCHOR-structural",
        "record_type": "ANCHOR",
        "level": "SMALL",
        "direction": "BULL",
        "status": "ACTIVE",
        "origin_time": "2026-08-26T09:00:00+08:00",
        "origin_price": 100.0,
        "first_extreme_time": "2026-08-26T09:02:00+08:00",
        "first_extreme_price": 180.0,
        "latest_extreme_time": "2026-08-26T09:02:00+08:00",
        "latest_extreme_price": 180.0,
        "first_seen_at": "2026-08-26T09:03:00+08:00",
        "defense": None,
        "amplitude_points": 80.0,
        "duration_minutes": 2,
    }
    derive_results = iter([None, structural])
    monkeypatch.setattr(
        lifecycle_module,
        "_derive_directional_anchor",
        lambda *args, **kwargs: next(derive_results),
    )
    monkeypatch.setattr(lifecycle_module, "_course_pivots", lambda *args, **kwargs: [])
    monkeypatch.setattr(lifecycle_module, "_opening_anchor", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle_module, "_reverse_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle_module, "_working_leg", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle_module, "_course_dow_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle_module, "_course_quadrant_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle_module, "_course_taiji_context", lambda *args, **kwargs: None)
    bars = [
        {"time": "2026-08-26T09:00:00+08:00", "open": 100, "high": 110, "low": 95, "close": 105},
        {"time": "2026-08-26T09:01:00+08:00", "open": 105, "high": 200, "low": 104, "close": 190},
        {"time": "2026-08-26T09:02:00+08:00", "open": 190, "high": 190, "low": 160, "close": 170},
    ]

    snapshot = build_anchor_lifecycle(
        bars,
        expected_as_of="2026-08-26T09:02:00+08:00",
        session_key="fixture:DAY",
        session_start="2026-08-26T09:00:00+08:00",
        course_chain_enabled=True,
    )

    assert snapshot["background_anchor"] is None
    assert snapshot["child_anchor"]["id"] == structural["id"]
    assert snapshot["child_anchor"]["latest_extreme_time"] == "2026-08-26T09:01:00+08:00"
    assert snapshot["child_anchor"]["latest_extreme_price"] == 200.0


def _bars_until(cutoff: str) -> list[dict[str, float | str]]:
    nodes = [
        ("2026-08-24T23:00:00+08:00", 44350.0),
        ("2026-08-24T23:10:00+08:00", 44276.0),
        ("2026-08-24T23:47:00+08:00", 44615.0),
        ("2026-08-24T23:58:00+08:00", 44494.0),
        ("2026-08-25T00:22:00+08:00", 44633.0),
        ("2026-08-25T01:55:00+08:00", 44460.0),
        ("2026-08-25T02:30:00+08:00", 44658.0),
        ("2026-08-25T02:39:00+08:00", 44594.0),
        ("2026-08-25T03:53:00+08:00", 44459.0),
        ("2026-08-25T03:54:00+08:00", 44499.0),
        ("2026-08-25T04:59:00+08:00", 44534.0),
    ]
    parsed = [(datetime.fromisoformat(at), price) for at, price in nodes]
    result: list[dict[str, float | str]] = []
    previous_close = parsed[0][1]
    for index in range(len(parsed) - 1):
        start, start_price = parsed[index]
        end, end_price = parsed[index + 1]
        minutes = int((end - start).total_seconds() // 60)
        if index == 0:
            result.append(_bar(start, previous_close, start_price))
        for offset in range(1, minutes + 1):
            at = start + timedelta(minutes=offset)
            close = round(start_price + ((end_price - start_price) * offset / minutes), 4)
            result.append(_bar(at, previous_close, close))
            previous_close = close
    # The actual 03:53 bar closed one point below the 44,460 defense but swept
    # to 44,453.  Keeping OHLC order unknown mirrors the real causal input.
    for item in result:
        if item["time"] == "2026-08-25T03:53:00+08:00":
            item["low"] = 44453.0
            item["close"] = 44459.0
        if item["time"] == "2026-08-25T03:54:00+08:00":
            item["high"] = 44500.0
            item["close"] = 44499.0
    expected = datetime.fromisoformat(cutoff)
    return [item for item in result if datetime.fromisoformat(str(item["time"])) <= expected]


def _bar(at: datetime, opened: float, close: float) -> dict[str, float | str]:
    return {
        "time": at.isoformat(),
        "open": float(opened),
        "high": max(float(opened), float(close)),
        "low": min(float(opened), float(close)),
        "close": float(close),
        "volume": 1.0,
    }


def _snapshot(cutoff: str) -> dict:
    bars = _bars_until(cutoff)
    return build_anchor_lifecycle(
        bars,
        expected_as_of=cutoff,
        session_key="2026-08-25:TRADE_DAY",
    )


def _structured_for(bar: dict[str, float | str]) -> dict:
    return {
        "latest_closed_k": dict(bar),
        "indicators": {"sma21": 44500.0, "sma105": 44520.0, "atr14": 20.0},
        "opening_ranges": {"or5": None, "or15": None},
        "causal_structure_n2": {
            "dow_small": "TRANSITION",
            "dow_large": "TRANSITION",
            "recent_confirmed_pivots": [],
            "recent_large_pivots": [],
        },
    }


def test_background_anchor_persists_while_latest_working_leg_changes() -> None:
    at_0030 = _snapshot("2026-08-25T02:30:00+08:00")
    at_0239 = _snapshot("2026-08-25T02:39:00+08:00")
    first = at_0030["background_anchor"]
    corrected = at_0239["background_anchor"]

    assert first["id"] == corrected["id"]
    assert corrected["origin_time"] == "2026-08-24T23:10:00+08:00"
    assert corrected["origin_price"] == 44276.0
    assert corrected["latest_extreme_time"] == "2026-08-25T02:30:00+08:00"
    assert corrected["latest_extreme_price"] == 44658.0
    assert at_0239["reverse_candidate"] is None
    assert at_0239["working_leg"]["role"] == "BACKGROUND_CORRECTION"
    assert at_0239["working_leg"]["start_time"] == "2026-08-25T02:30:00+08:00"


def test_defense_break_and_reclaim_degrade_but_do_not_replace_background() -> None:
    broken = _snapshot("2026-08-25T03:53:00+08:00")
    reclaimed = _snapshot("2026-08-25T03:54:00+08:00")

    assert broken["background_anchor"]["status"] == "DEGRADED"
    assert broken["background_anchor"]["defense"]["price"] == 44460.0
    assert broken["background_anchor"]["defense"]["broken_at"] == "2026-08-25T03:53:00+08:00"
    assert reclaimed["background_anchor"]["id"] == broken["background_anchor"]["id"]
    assert reclaimed["background_anchor"]["status"] == "DEGRADED_RECLAIMED"
    assert reclaimed["background_anchor"]["defense"]["reclaimed_at"] == "2026-08-25T03:54:00+08:00"
    assert reclaimed["reverse_candidate"]["direction"] == "BEAR"
    assert reclaimed["reverse_candidate"]["replaces_background"] is False
    assert reclaimed["reverse_candidate"]["status"] == "AWAITING_CONTINUATION"


def test_preopen_snapshot_keeps_four_distinct_anchor_roles() -> None:
    snapshot = _snapshot("2026-08-25T04:59:00+08:00")
    background = snapshot["background_anchor"]
    candidate = snapshot["reverse_candidate"]
    working = snapshot["working_leg"]

    assert background["origin_time"] == "2026-08-24T23:10:00+08:00"
    assert background["latest_extreme_time"] == "2026-08-25T02:30:00+08:00"
    assert candidate["start_time"] == "2026-08-25T02:30:00+08:00"
    assert candidate["current_extreme_time"] == "2026-08-25T03:53:00+08:00"
    assert working["start_time"] == "2026-08-25T03:53:00+08:00"
    assert working["current_extreme_time"] == "2026-08-25T04:59:00+08:00"
    assert len({background["id"], candidate["id"], working["id"]}) == 3


def test_future_endpoint_is_not_visible_before_its_bar() -> None:
    snapshot = _snapshot("2026-08-25T02:29:00+08:00")
    serialized = str(snapshot)
    assert "2026-08-25T02:30:00+08:00" not in serialized
    assert snapshot["as_of"] == "2026-08-25T02:29:00+08:00"


def test_same_bar_high_low_cannot_form_reverse_candidate_or_working_leg() -> None:
    night = _bars_until("2026-08-25T04:59:00+08:00")
    opening = {
        "time": "2026-08-25T08:45:00+08:00",
        "open": 44467.0,
        "high": 44509.0,
        "low": 44372.0,
        "close": 44442.0,
        "volume": 1.0,
    }
    at_0845 = build_anchor_lifecycle(
        [*night, opening],
        expected_as_of=opening["time"],
        session_key="2026-08-25:TRADE_DAY",
        course_chain_enabled=True,
    )

    # This old piecewise fixture has equal highs/lows at every turn, hence no
    # strict n=2 pivots. A point-threshold zigzag used to invent a large anchor.
    assert at_0845["background_anchor"] is None
    assert at_0845["reverse_candidate"] is None
    assert at_0845["working_leg"] is None

    next_bar = {
        "time": "2026-08-25T08:46:00+08:00",
        "open": 44442.0,
        "high": 44467.0,
        "low": 44403.0,
        "close": 44407.0,
        "volume": 1.0,
    }
    at_0846 = build_anchor_lifecycle(
        [*night, opening, next_bar],
        expected_as_of=next_bar["time"],
        session_key="2026-08-25:TRADE_DAY",
        course_chain_enabled=True,
    )

    assert at_0846["reverse_candidate"] is None
    assert at_0846["background_anchor"] is None
    assert at_0846["working_leg"] is None


def test_night_background_is_reference_only_after_day_session_reset() -> None:
    night = _bars_until("2026-08-25T04:59:00+08:00")
    preopen = build_evidence_ledger(
        _structured_for(night[-1]),
        bars=night,
        expected_as_of=str(night[-1]["time"]),
        session_key="2026-08-25:NIGHT",
        anchor_lifecycle_enabled=True,
    )
    day_0845 = {
        "time": "2026-08-25T08:45:00+08:00",
        "open": 44467.0,
        "high": 44509.0,
        "low": 44372.0,
        "close": 44442.0,
        "volume": 1.0,
    }
    first = build_evidence_ledger(
        _structured_for(day_0845),
        bars=[*night, day_0845],
        expected_as_of=str(day_0845["time"]),
        session_key="2026-08-25:DAY",
        previous_ledger=preopen,
        anchor_lifecycle_enabled=True,
    )
    day_0846 = {
        "time": "2026-08-25T08:46:00+08:00",
        "open": 44442.0,
        "high": 44467.0,
        "low": 44403.0,
        "close": 44407.0,
        "volume": 1.0,
    }
    second = build_evidence_ledger(
        _structured_for(day_0846),
        bars=[*night, day_0845, day_0846],
        expected_as_of=str(day_0846["time"]),
        session_key="2026-08-25:DAY",
        previous_ledger=first,
        anchor_lifecycle_enabled=True,
    )

    pre_anchor = preopen["anchor_lifecycle"]["background_anchor"]
    assert pre_anchor is not None
    assert first["anchor_lifecycle"]["background_anchor"] is None
    assert first["anchor_lifecycle"]["child_anchor"] is None
    assert first["anchor_lifecycle"]["working_leg"] is None
    assert second["anchor_lifecycle"]["background_anchor"] is None
    assert second["anchor_lifecycle"]["child_anchor"] is None
    assert first["monitoring_session"]["name"] == "DAY"
    assert first["monitoring_session"]["prior_session_role"] == "REFERENCE_ONLY"
    assert first["anchor_lifecycle"]["session_key"] == "2026-08-25:DAY:DAY"
    assert first["reference_anchor_lifecycle"]["session_key"].endswith(":REFERENCE:US_OPEN")
    assert first["reference_anchor_lifecycle"]["background_anchor"]["id"] != pre_anchor["id"]


def test_day_opening_anchor_does_not_invent_early_dow_or_reverse_candidates() -> None:
    night = _bars_until("2026-08-25T04:59:00+08:00")
    rows = [
        ("08:45", 44467, 44509, 44372, 44442),
        ("08:46", 44442, 44467, 44403, 44407),
        ("08:47", 44403, 44441, 44400, 44426),
        ("08:48", 44427, 44462, 44395, 44397),
        ("08:49", 44394, 44425, 44380, 44385),
        ("08:50", 44382, 44422, 44378, 44415),
        ("08:51", 44412, 44429, 44410, 44419),
        ("08:52", 44420, 44428, 44400, 44407),
        ("08:53", 44412, 44460, 44402, 44445),
        ("08:54", 44440, 44467, 44433, 44451),
    ]
    day = [
        {
            "time": f"2026-08-25T{clock}:00+08:00",
            "open": float(opened),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": 1.0,
        }
        for clock, opened, high, low, close in rows
    ]

    snapshots = []
    for count in (4, 5, 10):
        latest = day[count - 1]
        snapshots.append(
            build_evidence_ledger(
                _structured_for(latest),
                bars=[*night, *day[:count]],
                expected_as_of=str(latest["time"]),
                session_key="2026-08-25:DAY",
                anchor_lifecycle_enabled=True,
                course_chain_enabled=True,
            )
        )

    before_or5, at_or5, at_0854 = snapshots
    assert before_or5["anchor_control"]["active_background_anchor_ref"] is None
    assert before_or5["anchor_control"]["active_child_anchor_ref"] is None
    assert before_or5["anchor_control"]["working_leg_ref"] is None

    opening = at_or5["anchor_lifecycle"]["child_anchor"]
    assert at_or5["anchor_lifecycle"]["background_anchor"] is None
    assert opening["direction"] == "BEAR"
    assert opening["level"] == "SMALL"
    assert opening["origin_time"] == "2026-08-25T08:45:00+08:00"
    assert opening["origin_price"] == 44467.0
    assert opening["latest_extreme_time"] == "2026-08-25T08:45:00+08:00"
    assert opening["latest_extreme_price"] == 44372.0
    assert opening["first_seen_at"] == "2026-08-25T08:49:00+08:00"
    assert opening["defense"] is None
    assert "defense_candidate" not in opening
    assert at_or5["anchor_lifecycle"]["reverse_candidate"] is None
    assert at_or5["anchor_control"]["small_defense_ref"] is None
    assert at_or5["anchor_lifecycle"]["taiji_context"]["parent_start_kind"] == "SESSION_OPEN"
    assert at_or5["anchor_lifecycle"]["taiji_context"]["parent_end_kind"] == "LOW"
    at_or5_rendered = "\n".join(
        _anchor_lifecycle_lines(at_or5["anchor_lifecycle"])
    )
    assert "防線候選" not in at_or5_rendered
    assert "反向候選" not in at_or5_rendered
    assert "小級道氏：未成立" in at_or5_rendered
    assert "空防08:48高44,462點（作用中）" not in at_or5_rendered

    current = at_0854["anchor_lifecycle"]
    assert current["background_anchor"] is None
    assert current["child_anchor"]["id"] == opening["id"]
    assert current["child_anchor"]["status"] == "ACTIVE"
    assert current["reverse_candidate"] is None
    assert current["working_leg"]["role"] == "BACKGROUND_CORRECTION"
    assert current["working_leg"]["start_time"] == "2026-08-25T08:50:00+08:00"
    assert current["working_leg"]["current_extreme_time"] == "2026-08-25T08:54:00+08:00"
    assert current["dow_context"]["large_state"] == "UNDEFINED"
    assert current["dow_context"]["small_state"] == "UNDEFINED"
    assert current["dow_context"]["small_bear_defense"] is None
    assert at_0854["anchor_control"]["small_defense_ref"] is None
    assert "02:30" not in str(current)

    rendered = "\n".join(_anchor_lifecycle_lines(current))
    assert "大錨：" not in rendered
    assert "小錨：08:45開44,467點→08:45低44,372點" in rendered
    assert "08:49 OR5確認" in rendered
    assert "防線候選" not in rendered
    assert "反向候選" not in rendered
    assert "小級道氏：未成立" in rendered
    assert "空防08:48高44,462點（作用中）" not in rendered


def test_unconfirmed_bounce_does_not_split_the_same_bear_leg_after_opening_anchor() -> None:
    rows = [
        ("08:45", 44467, 44509, 44372, 44442),
        ("08:46", 44442, 44467, 44403, 44407),
        ("08:47", 44403, 44441, 44400, 44426),
        ("08:48", 44427, 44462, 44395, 44397),
        ("08:49", 44394, 44425, 44380, 44385),
        ("08:50", 44382, 44422, 44378, 44415),
        ("08:51", 44412, 44429, 44410, 44419),
        ("08:52", 44420, 44428, 44400, 44407),
        ("08:53", 44412, 44460, 44402, 44445),
        ("08:54", 44440, 44467, 44433, 44451),
        ("08:55", 44451, 44453, 44399, 44441),
        ("08:56", 44443, 44455, 44402, 44412),
        ("08:57", 44415, 44440, 44410, 44423),
        ("08:58", 44419, 44424, 44393, 44414),
        ("08:59", 44417, 44420, 44382, 44387),
        ("09:00", 44395, 44415, 44337, 44398),
        ("09:01", 44395, 44443, 44338, 44353),
        ("09:02", 44352, 44416, 44343, 44358),
    ]
    day = [
        {
            "time": f"2026-08-25T{clock}:00+08:00",
            "open": float(opened),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": 1.0,
        }
        for clock, opened, high, low, close in rows
    ]
    latest = day[-1]
    ledger = build_evidence_ledger(
        _structured_for(latest),
        bars=day,
        expected_as_of=str(latest["time"]),
        session_key="2026-08-25:DAY",
        anchor_lifecycle_enabled=True,
        course_chain_enabled=True,
    )

    current = ledger["anchor_lifecycle"]
    assert current["child_anchor"]["first_extreme_price"] == 44372.0
    assert current["child_anchor"]["latest_extreme_price"] == 44337.0
    assert current["working_leg"]["direction"] == "BEAR"
    assert current["working_leg"]["role"] == "BACKGROUND_RETEST"
    assert current["working_leg"]["start_time"] == "2026-08-25T08:54:00+08:00"
    assert current["working_leg"]["start_price"] == 44467.0
    assert current["working_leg"]["current_extreme_time"] == "2026-08-25T09:00:00+08:00"
    assert current["working_leg"]["current_extreme_price"] == 44337.0


def test_opening_defense_candidate_qualifies_only_after_causing_new_low() -> None:
    rows = [
        ("08:45", 44467, 44509, 44372, 44442),
        ("08:46", 44442, 44467, 44403, 44407),
        ("08:47", 44403, 44441, 44400, 44426),
        ("08:48", 44427, 44462, 44395, 44397),
        ("08:49", 44394, 44425, 44380, 44385),
        ("08:50", 44382, 44422, 44378, 44415),
        ("08:51", 44412, 44429, 44410, 44419),
        ("08:52", 44420, 44428, 44400, 44407),
        ("08:53", 44412, 44460, 44402, 44445),
        ("08:54", 44440, 44467, 44433, 44451),
        ("08:55", 44440, 44445, 44380, 44390),
        ("08:56", 44390, 44420, 44380, 44400),
        ("08:57", 44400, 44410, 44360, 44365),
    ]
    bars = [
        {
            "time": f"2026-08-25T{clock}:00+08:00",
            "open": float(opened),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": 1.0,
        }
        for clock, opened, high, low, close in rows
    ]
    latest = bars[-1]
    ledger = build_evidence_ledger(
        _structured_for(latest),
        bars=bars,
        expected_as_of=str(latest["time"]),
        session_key="2026-08-25:DAY",
        anchor_lifecycle_enabled=True,
        course_chain_enabled=True,
    )

    lifecycle = ledger["anchor_lifecycle"]
    opening = lifecycle["child_anchor"]
    defense = opening["defense"]
    assert defense["time"] == "2026-08-25T08:54:00+08:00"
    assert defense["price"] == 44467.0
    assert defense["first_seen_at"] == "2026-08-25T08:57:00+08:00"
    assert defense["state"] == "ACTIVE"
    assert opening["defense_candidate"]["state"] == "QUALIFIED"
    assert lifecycle["dow_context"]["small_state"] == "BEAR"
    assert lifecycle["dow_context"]["small_bear_defense"]["id"] == defense["id"]
    assert ledger["anchor_control"]["small_defense_ref"] == defense["id"]

    rendered = "\n".join(_anchor_lifecycle_lines(lifecycle))
    assert "空方防線候選" not in rendered
    assert "小級道氏：偏空｜空防08:54高44,467點（作用中）" in rendered


def test_reverse_candidate_uses_confirmed_abc_then_causal_close_beyond_b() -> None:
    def candle(clock: str, opened: float, high: float, low: float, close: float) -> dict:
        return {
            "time": f"2026-08-25T{clock}:00+08:00",
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1.0,
        }

    bars = [
        candle("08:45", 110, 111, 100, 110),
        candle("08:46", 110, 111, 108, 108),
        candle("08:47", 108, 109, 105, 105),
        candle("08:48", 105, 105, 100, 100),
        candle("08:49", 100, 104, 102, 103),
        candle("08:50", 103, 107, 104, 106),
        candle("08:51", 106, 115, 106, 115),
        candle("08:52", 115, 120, 115, 120),
        candle("08:53", 117, 118, 116, 117),
        candle("08:54", 116, 116, 112, 114),
        candle("08:55", 114, 114, 110, 111),
        candle("08:56", 111, 111, 108, 108),
        candle("08:57", 110, 112, 109, 110),
        candle("08:58", 112, 114, 112, 113),
        candle("08:59", 113, 125, 113, 125),
        candle("09:00", 125, 130, 125, 130),
        candle("09:01", 125, 126, 122, 124),
        candle("09:02", 124, 125, 120, 120),
    ]
    background = {
        "direction": "BEAR",
        "latest_extreme_time": "2026-08-25T08:45:00+08:00",
        "latest_extreme_price": 100.0,
        "defense": None,
    }

    before = _reverse_candidate(
        background,
        bars[:14],
        threshold=30,
        session_key="n2-reverse",
        as_of=datetime.fromisoformat("2026-08-25T08:58:00+08:00"),
        require_n2_structure=True,
    )
    qualified = _reverse_candidate(
        background,
        bars,
        threshold=30,
        session_key="n2-reverse",
        as_of=datetime.fromisoformat("2026-08-25T09:02:00+08:00"),
        require_n2_structure=True,
    )

    assert before is None
    assert qualified["direction"] == "BULL"
    assert qualified["start_time"] == "2026-08-25T08:48:00+08:00"
    assert qualified["current_extreme_time"] == "2026-08-25T09:00:00+08:00"
    assert qualified["first_seen_at"] == "2026-08-25T08:59:00+08:00"


def test_0826_reverse_candidate_is_visible_when_close_completes_hh_hl() -> None:
    rows = [
        ("08:45", 45285, 45300, 45135, 45166),
        ("08:46", 45155, 45193, 45141, 45188),
        ("08:47", 45193, 45222, 45185, 45188),
        ("08:48", 45192, 45199, 45166, 45179),
        ("08:49", 45185, 45196, 45160, 45164),
        ("08:50", 45163, 45180, 45138, 45163),
        ("08:51", 45162, 45186, 45146, 45153),
        ("08:52", 45152, 45170, 45140, 45149),
        ("08:53", 45150, 45151, 45087, 45104),
        ("08:54", 45100, 45106, 45067, 45069),
        ("08:55", 45066, 45078, 45030, 45033),
        ("08:56", 45033, 45054, 45016, 45054),
        ("08:57", 45048, 45053, 45016, 45033),
        ("08:58", 45036, 45048, 45008, 45030),
        ("08:59", 45027, 45032, 44940, 44942),
        ("09:00", 44947, 45011, 44861, 44987),
        ("09:01", 44993, 45025, 44954, 44993),
        ("09:02", 44996, 45058, 44988, 45031),
        ("09:03", 45025, 45093, 45018, 45092),
        ("09:04", 45093, 45130, 45090, 45122),
        ("09:05", 45127, 45142, 45088, 45102),
        ("09:06", 45108, 45145, 45088, 45104),
        ("09:07", 45103, 45106, 45035, 45041),
        ("09:08", 45040, 45086, 45025, 45035),
        ("09:09", 45034, 45092, 45030, 45078),
        ("09:10", 45080, 45154, 45051, 45150),
    ]
    bars = [
        {
            "time": f"2026-08-26T{clock}:00+08:00",
            "open": float(opened),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": 1.0,
        }
        for clock, opened, high, low, close in rows
    ]
    background = {
        "direction": "BEAR",
        "latest_extreme_time": "2026-08-26T09:00:00+08:00",
        "latest_extreme_price": 44861.0,
        "defense": None,
    }

    candidate = _reverse_candidate(
        background,
        bars,
        threshold=0,
        session_key="2026-08-26:DAY",
        as_of=datetime.fromisoformat("2026-08-26T09:10:00+08:00"),
        require_n2_structure=True,
    )

    assert candidate is not None
    assert candidate["direction"] == "BULL"
    assert candidate["start_time"] == "2026-08-26T09:00:00+08:00"
    assert candidate["current_extreme_time"] == "2026-08-26T09:10:00+08:00"
    assert candidate["current_extreme_price"] == 45154.0
    assert candidate["first_seen_at"] == "2026-08-26T09:10:00+08:00"


def test_preparation_is_rejected_before_first_or5_closes() -> None:
    analysis = {
        "message_type": "PREPARATION",
        "course_reading": {"setup_stage": "ARMED"},
        "action": {"setup_key": "opening-short"},
    }
    memory = {
        "active_setups": [
            {"setup_key": "opening-short", "stage": "ARMED"},
        ]
    }
    with pytest.raises(SemanticReplayError, match="OR5"):
        _validate_preparation_contract(
            analysis,
            memory,
            ledger={"monitoring_session": {"bar_count": 4}},
        )

    _validate_preparation_contract(
        analysis,
        memory,
        ledger={"monitoring_session": {"bar_count": 5}},
    )


def test_opening_anchor_and_reverse_candidate_are_valid_setup_trigger_levels() -> None:
    ledger = {
        "anchor_records": [
            {
                "origin_price": 44467.0,
                "latest_extreme_price": 44372.0,
                "defense_candidate": {"price": 44462.0},
            }
        ],
        "opening_ranges": {"or5": {"high": 44509.0, "low": 44372.0}},
    }
    _validate_setup_trigger_levels(
        [{"trigger_level": 44467.0}, {"trigger_level": 44462.0}],
        ledger,
    )
    with pytest.raises(SemanticReplayError, match="trigger_level"):
        _validate_setup_trigger_levels([{"trigger_level": 44000.0}], ledger)


def test_opposite_impulse_alone_cannot_take_over_background() -> None:
    snapshot = _snapshot("2026-08-25T04:59:00+08:00")
    assert snapshot["background_anchor"]["direction"] == "BULL"
    assert snapshot["reverse_candidate"]["direction"] == "BEAR"
    assert snapshot["reverse_candidate"]["takeover_condition"] == "PULLBACK_THEN_LOWER_LOW"


def test_reverse_candidate_takes_over_only_after_pullback_and_new_low() -> None:
    bars = _bars_until("2026-08-25T04:59:00+08:00")
    previous_close = float(bars[-1]["close"])
    for end_time, end_price in (
        (datetime.fromisoformat("2026-08-25T05:10:00+08:00"), 44600.0),
        (datetime.fromisoformat("2026-08-25T05:30:00+08:00"), 44380.0),
    ):
        start_time = datetime.fromisoformat(str(bars[-1]["time"]))
        minutes = int((end_time - start_time).total_seconds() // 60)
        start_price = previous_close
        for offset in range(1, minutes + 1):
            at = start_time + timedelta(minutes=offset)
            close = round(start_price + ((end_price - start_price) * offset / minutes), 4)
            bars.append(_bar(at, previous_close, close))
            previous_close = close
    snapshot = build_anchor_lifecycle(
        bars,
        expected_as_of="2026-08-25T05:30:00+08:00",
        session_key="2026-08-25:TRADE_DAY",
    )
    assert snapshot["background_anchor"]["direction"] == "BEAR"
    assert snapshot["background_anchor"]["origin_time"] == "2026-08-25T02:30:00+08:00"
    assert snapshot["background_anchor"]["origin_price"] == 44658.0
    assert snapshot["background_anchor"]["latest_extreme_price"] == 44380.0


def test_semantic_reading_must_use_program_owned_lifecycle_refs() -> None:
    snapshot = _snapshot("2026-08-25T04:59:00+08:00")
    controls = {
        "active_background_anchor_ref": snapshot["background_anchor"]["id"],
        "active_child_anchor_ref": snapshot["child_anchor"]["id"],
        "working_leg_ref": snapshot["working_leg"]["id"],
        "reverse_anchor_candidate_ref": snapshot["reverse_candidate"]["id"],
        "large_defense_ref": snapshot["background_anchor"]["defense"]["id"],
        "small_defense_ref": snapshot["child_anchor"]["defense"]["id"],
    }
    ledger = {
        "anchor_control": controls,
        "anchor_lifecycle": snapshot,
        "anchor_records": [
            snapshot[key]
            for key in ("background_anchor", "child_anchor", "working_leg", "reverse_candidate")
        ],
        "anchor_events": snapshot["events"],
        "pivots": [],
        "legs": [],
        "defenses": [],
        "structure_events": [],
        "quadrant_evidence": {},
    }
    reading = {
        "large_anchor_ref": controls["active_background_anchor_ref"],
        "small_anchor_ref": controls["active_child_anchor_ref"],
        "working_anchor_ref": controls["working_leg_ref"],
        "reverse_anchor_candidate_ref": controls["reverse_anchor_candidate_ref"],
        "large_defense_ref": controls["large_defense_ref"],
        "small_defense_ref": controls["small_defense_ref"],
        "structure_event_ref": None,
        "controlling_grade": "LARGE",
        "grade_relation": "CONFLICT",
        "background_quadrant": "Q4",
        "working_quadrant": "Q2",
        "primary_quadrant_candidate": "Q2",
        "secondary_quadrant_candidate": "Q3",
        "background_trend_dynamics": "INCREASING",
        "background_volatility_dynamics": "CONTRACTING",
        "working_trend_dynamics": "DECREASING",
        "working_volatility_dynamics": "EXPANDING",
        "focus_methods": ["DOW", "QUADRANT"],
        "cclass_mode": "RESETTING",
        "taiji": "多方背景中的反向候選修正。",
        "yizhi": "目前沒有異常動能接管。",
        "left_right": "等待反向候選完成右側延續。",
        "dow": "大級防線失守後已收復，背景降級但未翻空。",
        "x_stage": "ANCHOR_LENS_SELECTION",
        "x_process": "保留多方背景，並追蹤空方候選能否完成接管。",
        "primary_lens": "背景大錨與反向候選分開判讀。",
        "main_strategy": "空手等待接管或原方向重新發動。",
        "setup_stage": "FORMING",
        "strategy_reason": ["反向尚缺修正後再創低。"],
    }
    assert _reading_v3(reading, ledger=ledger)["large_anchor_ref"] == controls["active_background_anchor_ref"]
    stale = dict(reading)
    stale["large_anchor_ref"] = stale["working_anchor_ref"]
    with pytest.raises(SemanticReplayError, match="large_anchor_ref"):
        _reading_v3(stale, ledger=ledger)

    as_of = "2026-08-25T04:59:00+08:00"
    envelope = {
        "analysis": {
            "original_decision": "NOTIFY",
            "message_type": "SNAPSHOT",
            "message_direction": "BULL",
            "notification_reason": "建立盤前背景與雙向劇本。",
            "latest_closed_k_price_estimate": "44,534點",
            "large_trend": {"classification": "偏多但回檔", "details": ["多方背景降級後收復防線。"]},
            "current_trend": {"classification": "轉換中", "details": ["空方候選正在修正。"]},
            "market_summary": ["背景大錨與反向候選同時保留。"],
            "course_reading": reading,
            "scenario": {
                "bull_probability": 40,
                "range_probability": 35,
                "bear_probability": 25,
                "bull_plan": "守住防線並重新突破反向候選修正高點。",
                "range_plan": "候選未延續前維持雙向整理。",
                "bear_plan": "反彈形成低高後再跌破44,453才接管。",
                "view_change": "空方完成修正後再創低才正式取代多方背景。",
            },
            "action": {
                "position_action": "NONE",
                "direction": "NONE",
                "entry_role": "NOT_APPLICABLE",
                "setup_key": None,
                "observation_area": "觀察反向候選修正與大級防線。",
                "trigger": "等待日盤已收盤K建立新證據。",
                "entry": "盤前不追認夜盤成交。",
                "structural_stop": "目前空手，待主控戰法成立後再固定。",
                "stop_price": None,
                "obstacles": [],
                "expected_behavior": "日盤開盤後逐輪更新錨生命週期。",
                "max_wait_bars": None,
                "no_chase": "開盤急拉急殺不追價。",
                "management": "維持空手。",
                "reentry_status": "NOT_APPLICABLE",
                "entry_rejection_reason": "NONE",
            },
        },
        "memory": {
            "version": 3,
            "as_of": as_of,
            "session_key": "2026-08-25:NIGHT",
            "active_setups": [],
            "thesis_bias": "CONDITIONAL",
            "maintain": "大錨背景保留，候選尚未接管。",
            "downgrade": "防線已失守後收復，維持降級。",
            "flip": "空方修正後再創低才正式接管。",
            "structure_control": {
                "active_large_anchor_ref": controls["active_background_anchor_ref"],
                "active_small_anchor_ref": controls["active_child_anchor_ref"],
                "working_anchor_ref": controls["working_leg_ref"],
                "active_reverse_candidate_ref": controls["reverse_anchor_candidate_ref"],
                "controlling_grade": "LARGE",
                "background_quadrant": "Q4",
                "working_quadrant": "Q2",
                "background_quadrant_changed_at": as_of,
                "working_quadrant_changed_at": as_of,
                "last_structure_event_ref": None,
            },
            "reentry": {"status": "NOT_APPLICABLE", "last_stop_at": None, "count": 0},
            "notes": ["錨角色由程式管理。"],
        },
    }
    validated = validate_semantic_envelope(
        envelope,
        ledger=ledger,
        expected_as_of=as_of,
        expected_session_key="2026-08-25:NIGHT",
        preopen=True,
        position=initial_position_state(as_of=as_of, version=2),
        evidence_events=[],
        previous_memory=None,
        entry_gate={"status": "NONE"},
    )
    assert validated["memory"]["structure_control"]["active_reverse_candidate_ref"] == controls["reverse_anchor_candidate_ref"]


def test_lifecycle_renderer_names_roles_instead_of_calling_every_leg_an_anchor() -> None:
    lines = _anchor_lifecycle_lines(_snapshot("2026-08-25T04:59:00+08:00"))
    rendered = "\n".join(lines)
    assert "大錨：23:10低44,276點→02:30高44,658點" in rendered
    assert "大級道氏防線：01:55低44,460點" in rendered
    assert "03:53失守、03:54收復" in rendered
    assert "反向候選：02:30高44,658點→03:53低44,453點" in rendered
    assert "目前工作段：03:53低44,453點→04:59高44,534點" in rendered


def test_lifecycle_renderer_labels_session_open_anchor_origin_as_open() -> None:
    lifecycle = {
        "background_anchor": {
            "record_type": "ANCHOR",
            "anchor_origin_kind": "SESSION_OPEN",
            "direction": "BULL",
            "origin_time": "2026-08-24T08:45:00+08:00",
            "origin_price": 45045.0,
            "latest_extreme_time": "2026-08-24T09:25:00+08:00",
            "latest_extreme_price": 45280.0,
            "amplitude_points": 235.0,
            "duration_minutes": 40,
            "status": "ACTIVE",
        }
    }

    rendered = "\n".join(_anchor_lifecycle_lines(lifecycle))

    assert "大錨：08:45開45,045點→09:25高45,280點" in rendered
    assert "08:45低45,045點" not in rendered


def test_taiji_leg_omits_endpoint_pair_whose_price_direction_conflicts_with_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    points = [
        {"kind": "HIGH", "time": "2026-08-26T09:56:00+08:00", "price": 45420.0,
         "confirmed": True, "confirmation_time": "2026-08-26T10:17:00+08:00"},
        # This later structural LOW is above the earlier HIGH.  Joining the
        # sparse large-grade points must not manufacture a bullish HIGH->LOW
        # leg merely because its numeric price is larger.
        {"kind": "LOW", "time": "2026-08-26T11:01:00+08:00", "price": 45784.0,
         "confirmed": True, "confirmation_time": "2026-08-26T11:22:00+08:00"},
        {"kind": "HIGH", "time": "2026-08-26T11:19:00+08:00", "price": 45911.0,
         "confirmed": True, "confirmation_time": "2026-08-26T11:22:00+08:00"},
        {"kind": "LOW", "time": "2026-08-26T11:24:00+08:00", "price": 45825.0,
         "confirmed": False, "confirmation_time": None},
    ]
    monkeypatch.setattr(lifecycle_module, "_course_pivots", lambda *_args, **_kwargs: points)
    bars = [
        {"time": at, "open": price, "high": price + 10, "low": price - 10, "close": price + 2}
        for at, price in [
            ("2026-08-26T09:56:00+08:00", 45400.0),
            ("2026-08-26T10:06:00+08:00", 45258.0),
            ("2026-08-26T11:01:00+08:00", 45780.0),
            ("2026-08-26T11:19:00+08:00", 45900.0),
            ("2026-08-26T11:24:00+08:00", 45835.0),
        ]
    ]
    anchor = {
        "id": "A-large-bull",
        "level": "LARGE",
        "direction": "BULL",
        "origin_time": "2026-08-26T09:25:00+08:00",
        "origin_price": 44957.0,
        "first_seen_at": "2026-08-26T11:22:00+08:00",
        "first_extreme_price": 45911.0,
    }
    child = {
        "id": "A-small-bull",
        "direction": "BULL",
        "origin_time": "2026-08-26T10:06:00+08:00",
        "origin_price": 45248.0,
        "first_seen_at": "2026-08-26T10:17:00+08:00",
    }

    evidence = _anchored_leg_evidence(anchor, bars, structural_child=child)

    assert not any(
        leg["start_time"] == "2026-08-26T09:56:00+08:00"
        and leg["end_time"] == "2026-08-26T11:01:00+08:00"
        for leg in evidence["legs"]
    )
    assert any(
        leg["start_time"] == "2026-08-26T09:56:00+08:00"
        and leg["end_time"] == "2026-08-26T10:06:00+08:00"
        and leg["direction"] == "BEAR"
        for leg in evidence["legs"]
    )
    assert evidence["current_parent"]["start_time"] == "2026-08-26T10:06:00+08:00"
    assert evidence["current_parent"]["end_time"] == "2026-08-26T11:19:00+08:00"
    assert evidence["current_parent"]["direction"] == "BULL"


def test_v8_runner_enables_lifecycle_without_changing_formal_rule_source() -> None:
    runner = ReplayRunner(
        ReplayConfig(rule_manifest_path=V8_MANIFEST, ai_provider="codex", ai_model="fake"),
        analyzer=object(),
    )
    assert runner.rules.contract_version == 5
    assert runner.rules.memory_version == 3
    assert runner.deterministic_contract is True
    assert runner.execution_gate_contract is True
    assert runner.anchor_lifecycle_contract is True
    assert runner.execution_version == "replay-execution-v11-anchor-lifecycle"
    assert runner.rules.prompt_sha256 == "78a3487e90235e4b83ea835d30572b9207e41eb2f7d13fce5984f20e80209469"
