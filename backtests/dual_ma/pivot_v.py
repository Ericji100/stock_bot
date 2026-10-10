"""Causal V-pivot detector frozen by PIVOT_V_CONFIRM_V1.

The tracker is deliberately entry-agnostic.  It consumes completed daily bars
in order, exposes the earliest confirmation timestamp, and never revises a
confirmed pivot with future evidence.  The v0.5.0 experiment is the first and
only current consumer; v0.4.1 Dow/stop logic remains separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from .models import Bar


PATTERN_ID = "PIVOT_V_CONFIRM_V1"


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    return numerator / denominator if denominator > 0 else None


def _atr14(bars: Sequence[Bar]) -> Decimal | None:
    if len(bars) < 15:
        return None
    window = bars[-14:]
    previous = bars[-15].close
    values: list[Decimal] = []
    for bar in window:
        values.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous),
                abs(bar.low - previous),
            )
        )
        previous = bar.close
    return sum(values, Decimal("0")) / Decimal(len(values))


@dataclass(frozen=True)
class BullVPivotConfirmation:
    candidate_id: str
    pullback_event_id: str
    pivot_low: Decimal
    breakout_level: Decimal
    group_start_index: int
    group_end_index: int
    group_start_date: date
    group_end_date: date
    group_length: int
    left_arm_index: int
    left_arm_date: date
    left_arm_distance: int
    confirmed_index: int
    confirmed_date: date
    right_arm_distance: int
    confirmation_up_candle: bool
    upper_wick_ratio: Decimal | None
    close_breakout_margin_pct: Decimal
    close_breakout_margin_atr: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": PATTERN_ID,
            "candidate_id": self.candidate_id,
            "pullback_event_id": self.pullback_event_id,
            "pivot_low": self.pivot_low,
            "breakout_level": self.breakout_level,
            "group_start_index": self.group_start_index,
            "group_end_index": self.group_end_index,
            "group_start_date": self.group_start_date,
            "group_end_date": self.group_end_date,
            "group_length": self.group_length,
            "left_arm_index": self.left_arm_index,
            "left_arm_date": self.left_arm_date,
            "left_arm_distance": self.left_arm_distance,
            "confirmed_index": self.confirmed_index,
            "confirmed_date": self.confirmed_date,
            "right_arm_distance": self.right_arm_distance,
            "confirmation_up_candle": self.confirmation_up_candle,
            "upper_wick_ratio": self.upper_wick_ratio,
            "close_breakout_margin_pct": self.close_breakout_margin_pct,
            "close_breakout_margin_atr": self.close_breakout_margin_atr,
        }


@dataclass(frozen=True)
class _Candidate:
    candidate_id: str
    pivot_low: Decimal
    breakout_level: Decimal
    group_start_event_pos: int
    group_end_event_pos: int
    group_start_index: int
    group_end_index: int
    group_start_date: date
    group_end_date: date
    left_arm_index: int | None
    left_arm_date: date | None
    left_arm_distance: int | None

    @property
    def group_length(self) -> int:
        return self.group_end_event_pos - self.group_start_event_pos + 1

    @property
    def left_qualified(self) -> bool:
        return self.left_arm_index is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": PATTERN_ID,
            "candidate_id": self.candidate_id,
            "pivot_low": self.pivot_low,
            "breakout_level": self.breakout_level,
            "group_start_index": self.group_start_index,
            "group_end_index": self.group_end_index,
            "group_start_date": self.group_start_date,
            "group_end_date": self.group_end_date,
            "group_length": self.group_length,
            "left_arm_index": self.left_arm_index,
            "left_arm_date": self.left_arm_date,
            "left_arm_distance": self.left_arm_distance,
            "left_qualified": self.left_qualified,
        }


class BullVTracker:
    """Track one long pullback event without looking beyond the current bar."""

    def __init__(
        self,
        *,
        pullback_event_id: str,
        context_bars: Sequence[tuple[int, Bar]] = (),
    ) -> None:
        self.pullback_event_id = pullback_event_id
        self.context_bars = list(context_bars)[-15:]
        self.event_bars: list[tuple[int, Bar]] = []
        self.candidate: _Candidate | None = None
        self.confirmed: BullVPivotConfirmation | None = None
        self._candidate_status: str | None = None

    def update(self, *, index: int, bar: Bar) -> list[dict[str, Any]]:
        if self.event_bars and index <= self.event_bars[-1][0]:
            raise ValueError("V-pivot bars must be strictly increasing")
        events: list[dict[str, Any]] = []
        self.event_bars.append((index, bar))

        if self.confirmed is not None:
            if bar.low < self.confirmed.pivot_low:
                events.append(
                    {
                        "type": "PIVOT_V_INVALIDATED",
                        **self.confirmed.to_dict(),
                        "invalidated_index": index,
                        "invalidated_date": bar.date,
                        "reason": "LOW_BREACH_BEFORE_ENTRY",
                    }
                )
                self.confirmed = None
                self.candidate = None
                self._candidate_status = None
            else:
                return events

        previous = self.candidate
        candidate = self._latest_candidate()
        self.candidate = candidate
        if candidate is None:
            return events

        if previous is None or previous.candidate_id != candidate.candidate_id:
            reason = "CANDIDATE_CREATED"
            if previous is not None:
                if candidate.pivot_low < previous.pivot_low:
                    reason = "NEW_LOW_RESET"
                elif (
                    candidate.pivot_low == previous.pivot_low
                    and candidate.group_start_index > previous.group_end_index + 1
                ):
                    reason = "SAME_LOW_RETEST"
                else:
                    reason = "EQUAL_LOW_GROUP_EXTENDED"
            events.append(
                {
                    "type": "PIVOT_V_CANDIDATE",
                    **candidate.to_dict(),
                    "as_of_index": index,
                    "as_of_date": bar.date,
                    "reason": reason,
                }
            )
            self._candidate_status = "ACTIVE"

        right = self._right_bars(candidate)
        if candidate.left_qualified:
            for distance, (right_index, right_bar) in enumerate(right[:3], start=1):
                if right_bar.close > candidate.breakout_level:
                    all_bars = [value for _, value in self.context_bars + self.event_bars]
                    atr = _atr14(all_bars[: self._combined_pos(right_index) + 1])
                    candle_range = right_bar.high - right_bar.low
                    upper_wick = right_bar.high - max(right_bar.open, right_bar.close)
                    margin = right_bar.close - candidate.breakout_level
                    confirmation = BullVPivotConfirmation(
                        candidate_id=candidate.candidate_id,
                        pullback_event_id=self.pullback_event_id,
                        pivot_low=candidate.pivot_low,
                        breakout_level=candidate.breakout_level,
                        group_start_index=candidate.group_start_index,
                        group_end_index=candidate.group_end_index,
                        group_start_date=candidate.group_start_date,
                        group_end_date=candidate.group_end_date,
                        group_length=candidate.group_length,
                        left_arm_index=int(candidate.left_arm_index),
                        left_arm_date=candidate.left_arm_date,  # type: ignore[arg-type]
                        left_arm_distance=int(candidate.left_arm_distance),
                        confirmed_index=right_index,
                        confirmed_date=right_bar.date,
                        right_arm_distance=distance,
                        confirmation_up_candle=right_bar.close > right_bar.open,
                        upper_wick_ratio=_ratio(upper_wick, candle_range),
                        close_breakout_margin_pct=margin / candidate.breakout_level,
                        close_breakout_margin_atr=_ratio(margin, atr) if atr is not None else None,
                    )
                    self.confirmed = confirmation
                    self._candidate_status = "CONFIRMED"
                    events.append({"type": "PIVOT_V_CONFIRMED", **confirmation.to_dict()})
                    return events

        if len(right) >= 3 and self._candidate_status != "EXPIRED":
            self._candidate_status = "EXPIRED"
            events.append(
                {
                    "type": "PIVOT_V_EXPIRED",
                    **candidate.to_dict(),
                    "expired_index": right[2][0],
                    "expired_date": right[2][1].date,
                    "reason": (
                        "RIGHT_ARM_NO_STRICT_CLOSE_BREAKOUT"
                        if candidate.left_qualified
                        else "LEFT_ARM_NOT_QUALIFIED"
                    ),
                }
            )
        return events

    def _latest_candidate(self) -> _Candidate | None:
        if not self.event_bars:
            return None
        event_low = min(bar.low for _, bar in self.event_bars)
        end = max(
            pos for pos, (_, bar) in enumerate(self.event_bars) if bar.low == event_low
        )
        start = end
        while start > 0 and self.event_bars[start - 1][1].low == event_low:
            start -= 1
        group = self.event_bars[start : end + 1]
        breakout = max(bar.high for _, bar in group)
        combined = self.context_bars + self.event_bars
        combined_start = len(self.context_bars) + start
        left: tuple[int, Bar, int] | None = None
        for distance in range(1, 4):
            pos = combined_start - distance
            if pos < 0:
                break
            left_index, left_bar = combined[pos]
            if left_bar.low > event_low and left_bar.high >= breakout:
                left = (left_index, left_bar, distance)
                break
        group_start_index, group_start_bar = group[0]
        group_end_index, group_end_bar = group[-1]
        candidate_id = (
            f"{self.pullback_event_id}:V:{group_start_bar.date.isoformat()}:"
            f"{group_end_bar.date.isoformat()}:{event_low}:{breakout}"
        )
        return _Candidate(
            candidate_id=candidate_id,
            pivot_low=event_low,
            breakout_level=breakout,
            group_start_event_pos=start,
            group_end_event_pos=end,
            group_start_index=group_start_index,
            group_end_index=group_end_index,
            group_start_date=group_start_bar.date,
            group_end_date=group_end_bar.date,
            left_arm_index=left[0] if left else None,
            left_arm_date=left[1].date if left else None,
            left_arm_distance=left[2] if left else None,
        )

    def _right_bars(self, candidate: _Candidate) -> list[tuple[int, Bar]]:
        return self.event_bars[candidate.group_end_event_pos + 1 :]

    def _combined_pos(self, index: int) -> int:
        for pos, (bar_index, _) in enumerate(self.context_bars + self.event_bars):
            if bar_index == index:
                return pos
        raise ValueError("confirmation bar missing from causal tracker history")

    def snapshot(self) -> dict[str, Any]:
        return {
            "pattern_id": PATTERN_ID,
            "pullback_event_id": self.pullback_event_id,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "confirmed": self.confirmed.to_dict() if self.confirmed else None,
            "candidate_status": self._candidate_status,
            "event_bar_count": len(self.event_bars),
        }
