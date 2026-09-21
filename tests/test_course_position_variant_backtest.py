from __future__ import annotations

from scripts.course_position_variant_backtest import _net_return_for_ranking, ranking_diagnostics


def _row(rank: int, gross_return: float, score: int = 80) -> dict:
    entry = 10.0
    return {
        "entry_price": entry,
        "same_day_rank": rank,
        "priority_score": score,
        "reference_mfe_pct": 35.0 if rank == 1 else 5.0,
        "exit_plan": {
            "partial_exits": [],
            "performance_price": entry * (1.0 + gross_return / 100.0),
        },
    }


def test_net_return_includes_both_commissions_and_sell_tax() -> None:
    result = _net_return_for_ranking(_row(1, 10.0), 0.001425, 0.003)
    assert result == 9.3708


def test_ranking_diagnostics_separates_same_day_rank_tiers() -> None:
    rows = [_row(1, 10.0, 90), _row(4, 2.0, 70), _row(6, -5.0, 50)]
    result = ranking_diagnostics(rows, 0.0, 0.0)
    assert result["same_day_rank_tiers"]["RANK_1_3"]["trade_count"] == 1
    assert result["same_day_rank_tiers"]["RANK_4_5"]["mean_net_return_pct"] == 2.0
    assert result["same_day_rank_tiers"]["RANK_6_8"]["positive_rate_pct"] == 0.0
    assert result["same_day_rank_tiers"]["RANK_1_3"]["mfe_30_count"] == 1
