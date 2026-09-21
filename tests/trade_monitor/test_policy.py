from __future__ import annotations

import pytest

from trade_monitor.policy import PlateState, advance_plate, assess_sstv, setup_admission


def test_zero_plate_is_first_confirmed_direction() -> None:
    result = advance_plate(
        PlateState(None, None),
        candidate_direction="UP",
        structural_invalidated=False,
        closed_bar_confirmed=True,
        continuation_confirmed=True,
    )

    assert result == PlateState(0, "UP")


def test_false_break_does_not_increment_plate() -> None:
    result = advance_plate(
        PlateState(0, "UP"),
        candidate_direction="DOWN",
        structural_invalidated=True,
        closed_bar_confirmed=True,
        continuation_confirmed=True,
        false_break=True,
    )

    assert result == PlateState(0, "UP")


def test_confirmed_flip_becomes_one_plate() -> None:
    result = advance_plate(
        PlateState(0, "UP"),
        candidate_direction="DOWN",
        structural_invalidated=True,
        closed_bar_confirmed=True,
        continuation_confirmed=True,
    )

    assert result == PlateState(1, "DOWN")


@pytest.mark.parametrize(
    ("quadrant", "pattern", "expected"),
    [
        (1, "OPENING_RANGE_BREAK_RETEST", "ALLOW"),
        (1, "TREND_PULLBACK_CONTINUATION", "ALLOW"),
        (1, "COMPRESSION_BREAKOUT", "ALLOW"),
        (1, "FALSE_BREAK_REVERSAL", "DENY"),
        (2, "OPENING_RANGE_BREAK_RETEST", "DENY"),
        (2, "TREND_PULLBACK_CONTINUATION", "DENY"),
        (2, "COMPRESSION_BREAKOUT", "DENY"),
        (2, "FALSE_BREAK_REVERSAL", "ALLOW"),
        (3, "OPENING_RANGE_BREAK_RETEST", "ALLOW"),
        (3, "TREND_PULLBACK_CONTINUATION", "DENY"),
        (3, "COMPRESSION_BREAKOUT", "ALLOW"),
        (3, "FALSE_BREAK_REVERSAL", "ALLOW"),
        (4, "OPENING_RANGE_BREAK_RETEST", "ALLOW"),
        (4, "TREND_PULLBACK_CONTINUATION", "ALLOW"),
        (4, "COMPRESSION_BREAKOUT", "ALLOW"),
        (4, "FALSE_BREAK_REVERSAL", "DENY"),
    ],
)
def test_quadrant_by_four_pattern_admission_matrix(quadrant: int, pattern: str, expected: str) -> None:
    assert setup_admission(
        quadrant=quadrant,
        pattern=pattern,
        plate_number=0,
        direction_aligned=True,
        sstv_quality="合格",
    ) == expected


def test_second_quadrant_is_not_misclassified_as_compression() -> None:
    assert setup_admission(
        quadrant=2,
        pattern="COMPRESSION_BREAKOUT",
        plate_number=0,
        direction_aligned=True,
        sstv_quality="合格",
    ) == "DENY"


def test_two_plate_or_higher_denies_ordinary_trend_setup() -> None:
    assert setup_admission(
        quadrant=1,
        pattern="TREND_PULLBACK_CONTINUATION",
        plate_number=2,
        direction_aligned=True,
    ) == "DENY"
    assert setup_admission(
        quadrant=2,
        pattern="FALSE_BREAK_REVERSAL",
        plate_number=2,
        direction_aligned=False,
        at_outer_edge=True,
    ) == "ALLOW"


def test_sstv_has_three_qualities_and_hard_veto() -> None:
    assert assess_sstv(size="PASS", stability="PASS", trend="PASS", volatility="PASS") == "合格"
    assert assess_sstv(size="PASS", stability="WARN", trend="PASS", volatility="PASS") == "警戒"
    assert assess_sstv(size="PASS", stability="FAIL", trend="PASS", volatility="PASS") == "不合格"
    assert setup_admission(
        quadrant=3,
        pattern="COMPRESSION_BREAKOUT",
        plate_number=0,
        direction_aligned=True,
        sstv_quality="不合格",
    ) == "DENY"
