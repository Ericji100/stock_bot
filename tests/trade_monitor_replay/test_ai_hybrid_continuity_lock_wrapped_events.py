from __future__ import annotations

import pytest

from trade_monitor_replay.runner import _ai_hybrid_state_continuity_lock


def _previous_large_control() -> dict:
    return {
        "version": 3,
        "structure_control": {
            "active_large_anchor_ref": "large-from-upgrade",
            "controlling_grade": "LARGE",
        },
    }


@pytest.mark.parametrize(
    "event_type",
    ["GRADE_UPGRADE", "GRADE_DOWNGRADE", "STRUCTURE_INVALIDATED"],
)
def test_wrapped_formal_structure_event_opens_continuity_lock(event_type: str) -> None:
    event_id = f"structure-{event_type.lower()}"
    lock = _ai_hybrid_state_continuity_lock(
        _previous_large_control(),
        [{"event_type": "STRUCTURE_EVENT", "event_id": event_id}],
        ledger={
            "structure_events": [
                {"id": event_id, "event_type": event_type, "direction": "BEAR"}
            ]
        },
    )

    assert lock["required"] is False
    assert lock["reason"] == "CONTROL_CHANGE_EVENT_AVAILABLE"
    assert lock["required_analysis_course_reading"] is None
    assert lock["required_memory_structure_control"] is None
    assert lock["permitted_change_events"] == [
        {"event_id": event_id, "event_type": event_type}
    ]


@pytest.mark.parametrize(
    ("evidence_event", "structure_events"),
    [
        ({"event_type": "STRUCTURE_EVENT", "event_id": "missing"}, []),
        (
            {"event_type": "STRUCTURE_EVENT", "event_id": "false-break"},
            [{"id": "false-break", "event_type": "FALSE_BREAK_RECLAIM"}],
        ),
        (
            {"event_type": "LEG_OBSERVED", "event_id": "downgrade"},
            [{"id": "downgrade", "event_type": "GRADE_DOWNGRADE"}],
        ),
    ],
)
def test_unresolved_or_non_control_wrapper_keeps_continuity_lock_fail_closed(
    evidence_event: dict,
    structure_events: list[dict],
) -> None:
    lock = _ai_hybrid_state_continuity_lock(
        _previous_large_control(),
        [evidence_event],
        ledger={"structure_events": structure_events},
    )

    assert lock["required"] is True
    assert lock["reason"] == "NO_EXPLICIT_CONTROL_CHANGE_EVENT"
    assert lock["permitted_change_events"] == []
    assert lock["required_analysis_course_reading"] == {
        "large_anchor_ref": "large-from-upgrade",
        "controlling_grade": "LARGE",
    }
