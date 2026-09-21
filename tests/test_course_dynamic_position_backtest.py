from __future__ import annotations

from scripts.course_dynamic_position_backtest import promotion_signal, time_release_signal


def test_promotion_requires_close_at_one_r() -> None:
    assert not promotion_signal(10.99, 10.0, 9.0)
    assert promotion_signal(11.0, 10.0, 9.0)


def test_five_bar_release_requires_less_than_half_r_progress() -> None:
    assert not time_release_signal(rule="NO_0_5R_CLOSE_BY_BAR_5", bars_held=4, max_close_r=0.1, promotion_attempted=False)
    assert time_release_signal(rule="NO_0_5R_CLOSE_BY_BAR_5", bars_held=5, max_close_r=0.49, promotion_attempted=False)
    assert not time_release_signal(rule="NO_0_5R_CLOSE_BY_BAR_5", bars_held=5, max_close_r=0.5, promotion_attempted=False)


def test_time_release_never_overrides_a_promotion_attempt() -> None:
    assert not time_release_signal(rule="NO_1R_CLOSE_BY_BAR_10", bars_held=10, max_close_r=0.9, promotion_attempted=True)
