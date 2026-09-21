from __future__ import annotations

from dataclasses import dataclass


PATTERNS = {
    "OPENING_RANGE_BREAK_RETEST",
    "TREND_PULLBACK_CONTINUATION",
    "COMPRESSION_BREAKOUT",
    "FALSE_BREAK_REVERSAL",
}
QUADRANTS = {1, 2, 3, 4}
QUALITIES = {"合格", "警戒", "不合格"}


@dataclass(frozen=True)
class PlateState:
    number: int | None
    direction: str | None


def advance_plate(
    current: PlateState,
    *,
    candidate_direction: str,
    structural_invalidated: bool,
    closed_bar_confirmed: bool,
    continuation_confirmed: bool,
    false_break: bool = False,
) -> PlateState:
    direction = _direction(candidate_direction)
    if current.number is None:
        if closed_bar_confirmed and continuation_confirmed and not false_break:
            return PlateState(0, direction)
        return current
    if direction == current.direction:
        return current
    if false_break or not (structural_invalidated and closed_bar_confirmed and continuation_confirmed):
        return current
    return PlateState(current.number + 1, direction)


def assess_sstv(*, size: str, stability: str, trend: str, volatility: str) -> str:
    values = (size, stability, trend, volatility)
    if any(value not in {"PASS", "WARN", "FAIL"} for value in values):
        raise ValueError("S/S/T/V inputs must be PASS, WARN or FAIL")
    if "FAIL" in values:
        return "不合格"
    if "WARN" in values:
        return "警戒"
    return "合格"


def setup_admission(
    *,
    quadrant: int,
    pattern: str,
    plate_number: int,
    direction_aligned: bool,
    sstv_quality: str = "合格",
    at_outer_edge: bool = True,
    far_from_structure: bool = False,
) -> str:
    if quadrant not in QUADRANTS or pattern not in PATTERNS or sstv_quality not in QUALITIES:
        raise ValueError("invalid setup admission input")
    if plate_number < 0:
        raise ValueError("plate_number must not be negative")
    if sstv_quality == "不合格":
        return "DENY"
    if plate_number >= 2:
        return "ALLOW" if pattern == "FALSE_BREAK_REVERSAL" and at_outer_edge else "DENY"
    if pattern != "FALSE_BREAK_REVERSAL" and not direction_aligned:
        return "DENY"

    if quadrant == 1:
        allowed = pattern in {
            "OPENING_RANGE_BREAK_RETEST",
            "TREND_PULLBACK_CONTINUATION",
            "COMPRESSION_BREAKOUT",
        }
    elif quadrant == 2:
        allowed = pattern == "FALSE_BREAK_REVERSAL" and at_outer_edge
    elif quadrant == 3:
        allowed = pattern in {
            "OPENING_RANGE_BREAK_RETEST",
            "COMPRESSION_BREAKOUT",
            "FALSE_BREAK_REVERSAL",
        } and (pattern == "FALSE_BREAK_REVERSAL" or sstv_quality == "合格")
    else:
        allowed = pattern in {
            "OPENING_RANGE_BREAK_RETEST",
            "TREND_PULLBACK_CONTINUATION",
            "COMPRESSION_BREAKOUT",
        } and not far_from_structure

    if not allowed:
        return "DENY"
    return "CONDITIONAL" if sstv_quality == "警戒" else "ALLOW"


def _direction(value: str) -> str:
    normalized = str(value).upper()
    if normalized not in {"UP", "DOWN"}:
        raise ValueError("direction must be UP or DOWN")
    return normalized
