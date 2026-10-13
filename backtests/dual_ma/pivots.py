from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Bar, DowDefenseEvent, Pivot


class CausalPivotDetector:
    """Daily n=2 pivots; a zone is emitted only after two completed right bars."""

    def __init__(self) -> None:
        self._emitted: set[tuple[str, int, int]] = set()

    def update(self, bars: list[Bar]) -> list[Pivot]:
        confirmed_index = len(bars) - 1
        if confirmed_index < 4:
            return []
        zone_end = confirmed_index - 2
        emitted: list[Pivot] = []
        for kind in ("HIGH", "LOW"):
            field = kind.lower()
            value = getattr(bars[zone_end], field)
            zone_start = zone_end
            while zone_start > 0 and getattr(bars[zone_start - 1], field) == value:
                zone_start -= 1
            if zone_start < 2:
                continue
            left = [getattr(bars[index], field) for index in range(zone_start - 2, zone_start)]
            right = [
                getattr(bars[index], field)
                for index in range(zone_end + 1, confirmed_index + 1)
            ]
            is_pivot = (
                all(value > other for other in left + right)
                if kind == "HIGH"
                else all(value < other for other in left + right)
            )
            key = (kind, zone_start, zone_end)
            if not is_pivot or key in self._emitted:
                continue
            self._emitted.add(key)
            emitted.append(
                Pivot(
                    kind=kind,
                    value=value,
                    zone_start_index=zone_start,
                    zone_end_index=zone_end,
                    confirmed_index=confirmed_index,
                    zone_start_date=bars[zone_start].date,
                    zone_end_date=bars[zone_end].date,
                    confirmed_date=bars[confirmed_index].date,
                )
            )
        return emitted


@dataclass
class _Candidate:
    low: Pivot
    prior_high: Pivot
    activated: bool = False


class DowDefenseTracker:
    """Emit every causal bullish Dow event after prior-high confirmation.

    ``events`` is the raw event stream consumed by independent strategy
    lifecycles. ``defense`` remains an all-history maximum for diagnostics only.
    """

    def __init__(self) -> None:
        self.detector = CausalPivotDetector()
        self.confirmed_highs: list[Pivot] = []
        self.candidates: list[_Candidate] = []
        self.defense: Decimal | None = None
        self.events: list[DowDefenseEvent] = []

    def update(self, bars: list[Bar]) -> tuple[Decimal | None, list[Pivot], list[DowDefenseEvent]]:
        index = len(bars) - 1
        pivots = self.detector.update(bars)
        self.confirmed_highs.extend(pivot for pivot in pivots if pivot.kind == "HIGH")
        for low in (pivot for pivot in pivots if pivot.kind == "LOW"):
            prior = [
                high
                for high in self.confirmed_highs
                if high.confirmed_index < low.zone_start_index
            ]
            if prior:
                self.candidates.append(_Candidate(low=low, prior_high=prior[-1]))

        new_events: list[DowDefenseEvent] = []
        for candidate in self.candidates:
            if candidate.activated:
                continue
            breakout_index = next(
                (
                    cursor
                    for cursor in range(candidate.low.zone_end_index + 1, index + 1)
                    if bars[cursor].close > candidate.prior_high.value
                ),
                None,
            )
            if breakout_index is None:
                continue
            # If price broke out before the low became usable, activate now, never backfill.
            candidate.activated = True
            if self.defense is None or candidate.low.value > self.defense:
                self.defense = candidate.low.value
            event = DowDefenseEvent(
                defense=candidate.low.value,
                pivot_low=candidate.low,
                prior_pivot_high=candidate.prior_high,
                breakout_index=breakout_index,
                breakout_date=bars[breakout_index].date,
                activated_index=index,
                activated_date=bars[index].date,
                version=len(self.events) + 1,
            )
            self.events.append(event)
            new_events.append(event)
        return self.defense, pivots, new_events
