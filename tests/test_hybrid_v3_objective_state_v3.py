from __future__ import annotations

import pytest

from scripts.hybrid_v3_objective_state_v3 import (
    ENGINE_STATUS,
    ENGINE_VERSION,
    PIVOT_DEFINITION,
    derive_objective_state,
)


def _bar(day: str, close: float) -> dict:
    return {"date": day, "open": close, "high": close, "low": close, "close": close}


def _pivot(scale: str, side: str, source: str, confirmed: str, price: float) -> dict:
    return {
        "scale": scale,
        "side": side,
        "source_date": source,
        "confirmation_date": confirmed,
        "price": price,
    }


def test_candidate2_objective_engine_is_final_and_discloses_legacy_pivots() -> None:
    assert ENGINE_VERSION == "hybrid-v3-objective-state-v3"
    assert ENGINE_STATUS == "FINAL"
    state = derive_objective_state(
        as_of="2023-01-10",
        bars=[_bar("2023-01-03", 9), _bar("2023-01-10", 11)],
        confirmed_pivots=[
            _pivot("SMALL", "HIGH", "2023-01-02", "2023-01-03", 10),
            _pivot("SMALL", "LOW", "2023-01-04", "2023-01-05", 8),
        ],
    )
    assert state["engine_version"] == ENGINE_VERSION
    assert state["pivot_definition"] == PIVOT_DEFINITION
    assert state["course_l1_l2_used"] is False


def test_candidate2_engine_rejects_every_kind_of_future_structure_input() -> None:
    with pytest.raises(ValueError, match="future bar"):
        derive_objective_state(
            as_of="2023-01-05",
            bars=[_bar("2023-01-06", 10)],
            confirmed_pivots=[],
        )
    with pytest.raises(ValueError, match="future-confirmed pivot"):
        derive_objective_state(
            as_of="2023-01-05",
            bars=[_bar("2023-01-05", 10)],
            confirmed_pivots=[
                _pivot("SMALL", "LOW", "2023-01-04", "2023-01-06", 9)
            ],
        )
    with pytest.raises(ValueError, match="future MACD"):
        derive_objective_state(
            as_of="2023-01-05",
            bars=[_bar("2023-01-05", 10)],
            confirmed_pivots=[],
            completed_macd_cycles=[{"status": "CONFIRMED", "end": "2023-01-06"}],
        )


def test_candidate2_attack_is_causal_and_not_same_day_control_break() -> None:
    pivots = [
        _pivot("SMALL", "HIGH", "2023-01-02", "2023-01-06", 10),
        _pivot("SMALL", "LOW", "2023-01-04", "2023-01-05", 8),
    ]
    same_day = derive_objective_state(
        as_of="2023-01-06",
        bars=[_bar("2023-01-05", 9), _bar("2023-01-06", 11)],
        confirmed_pivots=pivots,
    )
    assert same_day["attacks"] == []
    next_day = derive_objective_state(
        as_of="2023-01-07",
        bars=[_bar("2023-01-05", 9), _bar("2023-01-06", 9), _bar("2023-01-07", 11)],
        confirmed_pivots=pivots,
    )
    assert next_day["attacks"][0]["confirmed_on"] == "2023-01-07"
