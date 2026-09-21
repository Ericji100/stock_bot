from scripts.course_corporate_action_data import read
from scripts.course_quality_pullback_resonance_backtest import (
    SOURCE_DUAL,
    _reconstruct_proposals,
    is_quality_pullback,
)


def test_quality_pullback_is_fixed_to_relaunch_not_reclaim():
    assert is_quality_pullback("PULLBACK_RELAUNCH")
    assert not is_quality_pullback("RECLAIM")
    assert not is_quality_pullback("BREAKOUT")
    assert not is_quality_pullback(None)


def test_frozen_report_proposals_reconstruct_with_original_pattern_ids():
    baseline, resonance = _reconstruct_proposals(read(SOURCE_DUAL))

    assert len(baseline) == 72
    assert len(resonance) == 47
    assert sum(is_quality_pullback(row["small_pattern"]) for row in baseline) == 17
