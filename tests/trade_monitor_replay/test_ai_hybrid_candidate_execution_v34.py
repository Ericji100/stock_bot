from __future__ import annotations

from trade_monitor_replay.runner import (
    _ai_hybrid_execution_candidate,
    _ai_hybrid_preentry_invalidation_candidate,
    _filter_ai_hybrid_candidate_inventory,
)
from trade_monitor_replay.execution_gate import expire_stale_armed_setups


AS_OF = "2026-08-26T09:50:00+08:00"


def _candidate(key: str, *, first_seen: str = AS_OF) -> dict[str, object]:
    return {
        "setup_key": key,
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 100.0,
        "stop_price": 90.0,
        "first_seen_at": first_seen,
        "valid_bars": 3,
        "decision_authority": "AI_HYBRID",
    }


def _ledger(*candidates: dict[str, object]) -> dict[str, object]:
    return {
        "program_trade_policy": {
            "decision_authority": "AI_HYBRID",
            "trade_setup_policy": "LONG_Q2_Q4_ONLY",
        },
        "trade_levels": {
            "ai_candidate_inventory": list(candidates),
            "ai_armable_setup_keys": [item["setup_key"] for item in candidates],
            "continuation_arm_candidate": candidates[0] if candidates else None,
        },
    }


def _memory(key: str) -> dict[str, object]:
    return {
        "active_setups": [
            {
                "setup_key": key,
                "stage": "ARMED",
            }
        ]
    }


def test_previous_ai_choice_wins_over_legacy_program_priority() -> None:
    q4 = _candidate("q4")
    q2 = _candidate("q2")

    selected = _ai_hybrid_execution_candidate(
        _ledger(q4, q2),
        previous_memory=_memory("q2"),
    )

    assert selected is not None
    assert selected["setup_key"] == "q2"


def test_program_does_not_choose_between_two_unselected_ai_candidates() -> None:
    assert _ai_hybrid_execution_candidate(
        _ledger(_candidate("q4"), _candidate("q2")),
        previous_memory={"active_setups": []},
    ) is None


def test_hard_execution_filter_removes_retired_and_stale_unselected_candidates() -> None:
    current = _candidate("current")
    stale = _candidate("stale", first_seen="2026-08-26T09:40:00+08:00")
    retired = _candidate("retired")

    filtered = _filter_ai_hybrid_candidate_inventory(
        _ledger(current, stale, retired),
        retired_setup_keys={"retired"},
        previous_memory={"active_setups": []},
        as_of=AS_OF,
    )

    levels = filtered["trade_levels"]
    assert [item["setup_key"] for item in levels["ai_candidate_inventory"]] == ["current"]
    assert levels["ai_armable_setup_keys"] == ["current"]


def test_selected_candidate_is_not_removed_as_unseen_stale_before_entry_gate() -> None:
    selected = _candidate("selected", first_seen="2026-08-26T09:40:00+08:00")

    filtered = _filter_ai_hybrid_candidate_inventory(
        _ledger(selected),
        retired_setup_keys=set(),
        previous_memory=_memory("selected"),
        as_of=AS_OF,
    )

    assert filtered["trade_levels"]["ai_armable_setup_keys"] == ["selected"]


def test_structural_candidate_does_not_expire_by_arbitrary_elapsed_minutes() -> None:
    structural = {
        **_candidate("structural", first_seen="2026-08-26T09:30:00+08:00"),
        "validity_policy": {"kind": "STRUCTURAL"},
    }
    filtered = _filter_ai_hybrid_candidate_inventory(
        _ledger(structural),
        retired_setup_keys=set(),
        previous_memory={"active_setups": []},
        as_of=AS_OF,
    )
    assert filtered["trade_levels"]["ai_armable_setup_keys"] == ["structural"]

    memory = {
        "as_of": "2026-08-26T09:30:00+08:00",
        "active_setups": [_memory("structural")["active_setups"][0]],
    }
    kept = expire_stale_armed_setups(
        memory,
        armed_at_by_key={"structural": "2026-08-26T09:30:00+08:00"},
        as_of=AS_OF,
        structural_setup_keys={"structural"},
    )
    assert kept["active_setups"][0]["stage"] == "ARMED"


def test_constitution_lock_removes_every_ai_candidate() -> None:
    ledger = _ledger(_candidate("q4"), _candidate("q2"))
    ledger["trade_levels"]["constitution_gate"] = {"status": "BLOCKED"}

    filtered = _filter_ai_hybrid_candidate_inventory(
        ledger,
        retired_setup_keys=set(),
        previous_memory={"active_setups": []},
        as_of=AS_OF,
    )

    assert filtered["trade_levels"]["ai_candidate_inventory"] == []
    assert filtered["trade_levels"]["ai_armable_setup_keys"] == []


def test_missing_selected_structural_candidate_uses_prior_plan_for_stop_retirement() -> None:
    structural = {
        **_candidate("structural"),
        "validity_policy": {"kind": "STRUCTURAL"},
    }

    selected = _ai_hybrid_preentry_invalidation_candidate(
        _ledger(),
        previous_ledger=_ledger(structural),
        previous_memory=_memory("structural"),
    )

    assert selected is not None
    assert selected["setup_key"] == "structural"


def test_missing_fixed_window_candidate_does_not_bypass_normal_expiry() -> None:
    selected = _ai_hybrid_preentry_invalidation_candidate(
        _ledger(),
        previous_ledger=_ledger(_candidate("fixed-window")),
        previous_memory=_memory("fixed-window"),
    )

    assert selected is None
