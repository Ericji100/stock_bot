from datetime import date, timedelta
from decimal import Decimal

from backtests.dual_ma.models import Bar
from backtests.dual_ma.pivots import CausalPivotDetector, DowDefenseTracker


START = date(2026, 1, 1)


def make_bar(index: int, *, high: int, low: int, close: int) -> Bar:
    return Bar.from_values(
        START + timedelta(days=index),
        close,
        high,
        low,
        close,
    )


def test_equal_low_zone_confirms_only_after_two_completed_right_bars() -> None:
    bars = [
        make_bar(0, high=11, low=8, close=9),
        make_bar(1, high=10, low=7, close=8),
        make_bar(2, high=9, low=5, close=7),
        make_bar(3, high=9, low=5, close=7),
        make_bar(4, high=10, low=6, close=8),
        make_bar(5, high=11, low=7, close=9),
    ]
    detector = CausalPivotDetector()
    emitted = []
    for end in range(1, len(bars) + 1):
        today = detector.update(bars[:end])
        if end < 6:
            assert not [pivot for pivot in today if pivot.kind == "LOW"]
        emitted.extend(today)

    lows = [pivot for pivot in emitted if pivot.kind == "LOW"]
    assert len(lows) == 1
    assert lows[0].value == Decimal("5")
    assert lows[0].zone_start_index == 2
    assert lows[0].zone_end_index == 3
    assert lows[0].confirmed_index == 5


def test_breakout_before_low_confirmation_activates_on_confirmation_day_only() -> None:
    bars = [
        make_bar(0, high=10, low=6, close=8),
        make_bar(1, high=11, low=7, close=9),
        make_bar(2, high=15, low=8, close=10),
        make_bar(3, high=12, low=7, close=9),
        make_bar(4, high=11, low=6, close=8),  # high at 2 is now confirmed
        make_bar(5, high=12, low=7, close=9),
        make_bar(6, high=10, low=5, close=8),
        make_bar(7, high=17, low=6, close=16),  # breakout occurs, low not usable yet
        make_bar(8, high=16, low=7, close=15),  # low confirmation day
    ]
    tracker = DowDefenseTracker()
    seen = []
    for end in range(1, len(bars) + 1):
        _, _, events = tracker.update(bars[:end])
        seen.extend(events)
        if end <= 8:
            assert tracker.defense is None

    assert len(seen) == 1
    assert seen[0].breakout_index == 7
    assert seen[0].activated_index == 8
    assert seen[0].defense == Decimal("5")


def test_no_upgrade_before_prior_high_break_and_defense_never_decreases() -> None:
    bars = [
        make_bar(0, high=10, low=6, close=8),
        make_bar(1, high=11, low=7, close=9),
        make_bar(2, high=15, low=8, close=10),
        make_bar(3, high=12, low=7, close=9),
        make_bar(4, high=11, low=6, close=8),
        make_bar(5, high=12, low=7, close=9),
        make_bar(6, high=10, low=5, close=8),
        make_bar(7, high=11, low=6, close=9),
        make_bar(8, high=12, low=7, close=10),  # low confirmed, no >15 close
        make_bar(9, high=15, low=8, close=15),  # equality is not a break
        make_bar(10, high=17, low=9, close=16),
        make_bar(11, high=14, low=7, close=12),
        make_bar(12, high=13, low=6, close=11),  # high at 10 confirmed
        make_bar(13, high=12, low=5, close=10),
        make_bar(14, high=11, low=4, close=9),
        make_bar(15, high=12, low=5, close=10),
        make_bar(16, high=13, low=6, close=11),  # lower candidate confirmed
        make_bar(17, high=17, low=7, close=17),  # equality still no break
        make_bar(18, high=18, low=8, close=18),  # breakout cannot lower defense
    ]
    tracker = DowDefenseTracker()
    defenses = []
    for end in range(1, len(bars) + 1):
        defense, _, _ = tracker.update(bars[:end])
        defenses.append(defense)

    assert defenses[8] is None
    assert defenses[9] is None
    assert defenses[10] == Decimal("5")
    assert defenses[16] == Decimal("5")
    assert defenses[18] == Decimal("5")
    # The raw stream exposes later qualified events to per-strategy lifecycles,
    # while the tracker's diagnostic compatibility view remains a global max.
    assert tracker.defense == Decimal("5")
    assert [event.defense for event in tracker.events] == [Decimal("5"), Decimal("4")]
