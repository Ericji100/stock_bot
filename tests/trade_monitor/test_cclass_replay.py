from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trade_monitor.cclass_replay import ReplayLeg, _replay_momentum, assess_copy_quality
from trade_monitor.pivot_replay import Candle


TAIPEI = ZoneInfo("Asia/Taipei")


def _leg(*, amplitude: float, duration: int, cleanliness: float, end: float) -> ReplayLeg:
    return ReplayLeg(
        direction="BULL",
        start_index=0,
        end_index=duration,
        known_at_index=duration + 2,
        amplitude=amplitude,
        duration_bars=duration,
        slope=amplitude / duration,
        cleanliness=cleanliness,
        end_price=end,
    )


def test_copy_quality_preserves_all_five_comparison_dimensions() -> None:
    result = assess_copy_quality(
        _leg(amplitude=10, duration=5, cleanliness=0.7, end=110),
        _leg(amplitude=13, duration=3, cleanliness=0.9, end=115),
    )

    assert result == {
        "quality": "STRONG",
        "amplitude": "STRONGER",
        "duration": "STRONGER",
        "slope": "STRONGER",
        "cleanliness": "STRONGER",
        "destructive": "STRONGER",
    }


def test_copy_failure_does_not_encode_an_automatic_reverse_direction() -> None:
    result = assess_copy_quality(
        _leg(amplitude=10, duration=4, cleanliness=0.9, end=110),
        _leg(amplitude=5, duration=8, cleanliness=0.4, end=108),
    )

    assert result["quality"] == "FAILED"
    assert "direction" not in result


def test_momentum_gate_requires_closed_impulse_then_closed_compression_then_breakout() -> None:
    start = datetime(2026, 9, 2, 8, 45, tzinfo=TAIPEI)
    rows: list[Candle] = []
    price = 100.0
    for index in range(20):
        close = price + (0.15 if index % 2 == 0 else -0.1)
        rows.append(Candle(start + timedelta(minutes=index), price, max(price, close) + 0.25, min(price, close) - 0.25, close))
        price = close
    for body in (2.0, 2.4):
        close = price + body
        rows.append(Candle(start + timedelta(minutes=len(rows)), price, close + 0.25, price - 0.2, close))
        price = close
    for change in (0.1, -0.05, 0.08):
        close = price + change
        rows.append(Candle(start + timedelta(minutes=len(rows)), price, max(price, close) + 0.12, min(price, close) - 0.12, close))
        price = close
    close = price + 1.2
    rows.append(Candle(start + timedelta(minutes=len(rows)), price, close + 0.2, price - 0.1, close))

    result = _replay_momentum(rows, 2)

    assert result["centrifugal_confirmed"] >= 1
    assert result["life_death_gate_forming"] >= 1
    assert result["life_death_gate_armed"] >= 1
