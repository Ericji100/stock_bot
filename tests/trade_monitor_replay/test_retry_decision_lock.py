from __future__ import annotations

import pytest

from trade_monitor_replay.runner import (
    ReplayRunError,
    _assert_retry_decision_unchanged,
    _register_retry_decision,
    _retry_decision_instruction,
    _substantive_retry_decision,
)


@pytest.mark.parametrize(
    ("position_action", "position_status", "expected"),
    [
        ("ENTER", "FLAT", "ENTER"),
        ("EXIT", "LONG", "EXIT"),
        ("STOP", "LONG", "STOP"),
        ("NONE", "LONG", "HOLD"),
        ("NONE", "FLAT", "WAIT"),
    ],
)
def test_retry_lock_extracts_substantive_trade_decision(
    position_action: str,
    position_status: str,
    expected: str,
) -> None:
    payload = {"analysis": {"action": {"position_action": position_action}}}

    assert _substantive_retry_decision(
        payload,
        position={"status": position_status},
    ) == expected


def test_malformed_first_attempt_does_not_invent_a_decision_lock() -> None:
    assert _substantive_retry_decision({}, position={"status": "FLAT"}) is None
    _assert_retry_decision_unchanged(None, "ENTER")


@pytest.mark.parametrize("locked", ["ENTER", "HOLD", "EXIT", "STOP", "WAIT"])
def test_retry_must_preserve_the_first_recognizable_decision(locked: str) -> None:
    _assert_retry_decision_unchanged(locked, locked)

    with pytest.raises(ReplayRunError, match="不接受重試改寫交易") as exc_info:
        _assert_retry_decision_unchanged(locked, "WAIT" if locked != "WAIT" else "ENTER")

    assert exc_info.value.code == "retry_decision_changed"


def test_retry_missing_locked_decision_fails_closed() -> None:
    with pytest.raises(ReplayRunError, match="遺失已鎖定") as exc_info:
        _assert_retry_decision_unchanged("ENTER", None)

    assert exc_info.value.code == "retry_decision_missing"


def test_retry_instruction_maps_hold_and_wait_back_to_none_action() -> None:
    assert "action.position_action=NONE" in _retry_decision_instruction("HOLD")
    assert "action.position_action=NONE" in _retry_decision_instruction("WAIT")
    assert "action.position_action=ENTER" in _retry_decision_instruction("ENTER")


def test_runner_registration_keeps_first_decision_and_rejects_later_rewrite() -> None:
    first = {"analysis": {"action": {"position_action": "ENTER"}}}
    changed = {"analysis": {"action": {"position_action": "NONE"}}}

    locked, observed = _register_retry_decision(
        None,
        first,
        position={"status": "FLAT"},
    )

    assert (locked, observed) == ("ENTER", "ENTER")
    with pytest.raises(ReplayRunError, match="由ENTER改為WAIT"):
        _register_retry_decision(
            locked,
            changed,
            position={"status": "FLAT"},
        )
