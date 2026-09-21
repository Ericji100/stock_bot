from __future__ import annotations

import pytest

from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _validate_program_owned_actionable_setups,
)


def _candidate(key: str, family: str, trigger: float) -> dict[str, object]:
    return {
        "setup_key": key,
        "setup_name": f"{family} setup",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": trigger,
        "valid_bars": 3,
        "candidate_source": (
            "FALSE_BREAK_RECLAIM"
            if family == "Q2"
            else "CONFIRMED_PULLBACK_ENDPOINT_N2"
        ),
        "entry_strategy": (
            "Q2_FALSE_BREAK_RECLAIM"
            if family == "Q2"
            else "Q4_PULLBACK_CONTINUATION"
        ),
    }


def _ledger() -> dict[str, object]:
    q2 = _candidate("q2", "Q2", 101.0)
    q4 = _candidate("q4", "Q4", 102.0)
    return {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
            "trade_direction_policy": "LONG_ONLY",
            "trade_setup_policy": "LONG_Q2_Q4_ONLY",
        },
        "trade_levels": {
            "ai_candidate_inventory": [q2, q4],
            "ai_armable_setup_keys": ["q2", "q4"],
            "continuation_arm_candidate": None,
        },
    }


def _armed(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "setup_key": candidate["setup_key"],
        "direction": candidate["direction"],
        "stage": "ARMED",
        "trigger_operator": candidate["trigger_operator"],
        "trigger_level": candidate["trigger_level"],
        "valid_bars": candidate["valid_bars"],
    }


def test_ai_hybrid_may_select_either_hard_pass_candidate() -> None:
    ledger = _ledger()
    candidates = ledger["trade_levels"]["ai_candidate_inventory"]  # type: ignore[index]

    for candidate in candidates:  # type: ignore[union-attr]
        _validate_program_owned_actionable_setups(
            {"action": {"position_action": "NONE"}},
            {"active_setups": [_armed(candidate)]},
            ledger=ledger,
            position={"status": "FLAT"},
            entry_gate=None,
            preopen=False,
        )


def test_ai_hybrid_flat_state_cannot_arm_two_candidates_at_once() -> None:
    ledger = _ledger()
    candidates = ledger["trade_levels"]["ai_candidate_inventory"]  # type: ignore[index]

    with pytest.raises(SemanticReplayError, match="最多只能選一個"):
        _validate_program_owned_actionable_setups(
            {"action": {"position_action": "NONE"}},
            {"active_setups": [_armed(item) for item in candidates]},  # type: ignore[union-attr]
            ledger=ledger,
            position={"status": "FLAT"},
            entry_gate=None,
            preopen=False,
        )


def test_ai_hybrid_cannot_arm_candidate_outside_hard_pass_inventory() -> None:
    with pytest.raises(SemanticReplayError, match="只能來自程式化候選"):
        _validate_program_owned_actionable_setups(
            {"action": {"position_action": "NONE"}},
            {"active_setups": [_armed(_candidate("invented", "Q2", 99.0))]},
            ledger=_ledger(),
            position={"status": "FLAT"},
            entry_gate=None,
            preopen=False,
        )
