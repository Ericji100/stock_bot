from __future__ import annotations

from pathlib import Path

from trade_monitor_replay.program_constitution import (
    apply_program_execution_event,
    apply_program_requalification,
    requalification_audit,
    retire_setups_for_constitution_lock,
    suppress_candidate_for_constitution_lock,
)


def _entry(at: str, *, number: int) -> dict:
    return {
        "event_type": "ENTRY_FILLED",
        "event_id": f"entry-{number}",
        "setup_key": f"setup-{number}",
        "direction": "LONG",
        "fill_time": at,
        "fill_price": 100.0,
        "stop_price": 90.0,
    }


def _stop(at: str, *, number: int) -> dict:
    return {
        "event_type": "STOP_FILLED",
        "event_id": f"stop-{number}",
        "setup_key": f"setup-{number}",
        "direction": "LONG",
        "fill_time": at,
        "fill_price": 90.0,
    }


def _qualified_ledger(at: str) -> dict:
    return {
        "latest_closed_k": {"time": at, "close": 110.0},
        "anchor_lifecycle": {
            "background_anchor": {
                "id": "anchor-new",
                "level": "LARGE",
                "direction": "BULL",
                "status": "ACTIVE",
                "first_seen_at": at,
            },
            "dow_context": {
                "large_state": "BULL",
                "small_state": "BULL",
            },
            "quadrant_context": {"working_primary": "Q4"},
        },
        "course_method_state": {
            "cclass_mode": "TAIJI_ORDERED",
            "numbered_market": {
                "status": "NUMBERED",
                "number": 1,
                "label": "1",
                "direction": "BULL",
            },
        },
        "trade_levels": {
            "continuation_arm_candidate": {
                "setup_key": "setup-new",
                "direction": "LONG",
                "first_seen_at": at,
                "course_entry_quality": "EXECUTABLE",
                "candidate_source": "ANCHOR_LEG_SEQUENCE",
            }
        },
    }


def _three_stops(path: Path) -> dict:
    pairs = [
        ("2026-08-07T09:00:00+08:00", "2026-08-07T09:01:00+08:00"),
        ("2026-08-07T09:10:00+08:00", "2026-08-07T09:11:00+08:00"),
        ("2026-08-07T09:20:00+08:00", "2026-08-07T09:21:00+08:00"),
    ]
    snapshot = {}
    for number, (entry_at, stop_at) in enumerate(pairs, start=1):
        snapshot, lock = apply_program_execution_event(
            path, _entry(entry_at, number=number)
        )
        assert lock is None
        snapshot, lock = apply_program_execution_event(
            path, _stop(stop_at, number=number)
        )
    assert lock is not None
    return snapshot


def test_three_program_stops_lock_trading_for_thirty_minutes(tmp_path: Path) -> None:
    path = tmp_path / "constitution-state.json"
    snapshot = _three_stops(path)

    assert snapshot["consecutive_simulated_stops"] == 3
    assert snapshot["trading_locked"] is True
    assert snapshot["cooldown_active"] is True
    assert snapshot["cooldown_until"] == "2026-08-07T09:51:00+08:00"
    assert snapshot["requalification_required"] is True


def test_profitable_protective_stop_closes_without_counting_as_loss_stop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "constitution-state.json"
    snapshot, lock = apply_program_execution_event(
        path,
        _entry("2026-08-07T09:00:00+08:00", number=1),
    )
    assert lock is None
    assert snapshot["simulated_position"] is not None

    snapshot, lock = apply_program_execution_event(
        path,
        {
            "event_type": "STOP_FILLED",
            "event_id": "profit-protection-1",
            "setup_key": "setup-1",
            "direction": "LONG",
            "entry_price": 100.0,
            "fill_time": "2026-08-07T09:30:00+08:00",
            "fill_price": 120.0,
        },
    )

    assert lock is None
    assert snapshot["simulated_position"] is None
    assert snapshot["consecutive_simulated_stops"] == 0
    assert snapshot["trading_locked"] is False


def test_time_alone_does_not_requalify_and_full_fresh_structure_does(
    tmp_path: Path,
) -> None:
    path = tmp_path / "constitution-state.json"
    snapshot = _three_stops(path)

    during = requalification_audit(
        snapshot,
        _qualified_ledger("2026-08-07T09:50:00+08:00"),
        as_of="2026-08-07T09:50:00+08:00",
    )
    assert during["eligible"] is False
    assert during["reason_codes"] == ["COOLDOWN_ACTIVE"]

    # Refresh the public snapshot at the end of the fixed cooldown by applying
    # no market event: the persisted flag must remain locked until evidence is
    # explicitly accepted.
    after_time = dict(snapshot)
    after_time["cooldown_active"] = False
    incomplete = _qualified_ledger("2026-08-07T09:51:00+08:00")
    incomplete["anchor_lifecycle"]["dow_context"]["large_state"] = "UNDEFINED"
    rejected = requalification_audit(
        after_time,
        incomplete,
        as_of="2026-08-07T09:51:00+08:00",
    )
    assert rejected["eligible"] is False
    assert "REQUALIFICATION_LARGE_DOW_ALIGNED_MISSING" in rejected["reason_codes"]

    accepted = requalification_audit(
        after_time,
        _qualified_ledger("2026-08-07T09:51:00+08:00"),
        as_of="2026-08-07T09:51:00+08:00",
    )
    assert accepted["eligible"] is True
    unlocked, event = apply_program_requalification(path, accepted)
    assert unlocked["trading_locked"] is False
    assert unlocked["requalification_required"] is False
    assert unlocked["consecutive_simulated_stops"] == 0
    assert event["event_type"] == "PROGRAM_CONSTITUTION_REQUALIFIED"
    assert event["large_anchor_ref"] == "anchor-new"


def test_constitution_lock_retires_old_setups_and_suppresses_candidate() -> None:
    memory = {
        "version": 3,
        "as_of": "2026-08-07T09:20:00+08:00",
        "active_setups": [
            {"setup_key": "old-1", "stage": "ARMED"},
            {"setup_key": "old-2", "stage": "INVALIDATED"},
        ],
        "notes": [],
    }
    retired_memory, retired = retire_setups_for_constitution_lock(
        memory,
        as_of="2026-08-07T09:21:00+08:00",
    )
    assert retired == {"old-1", "old-2"}
    assert [item["stage"] for item in retired_memory["active_setups"]] == [
        "NO_CHASE",
        "INVALIDATED",
    ]
    assert [item["reentry_status"] for item in retired_memory["active_setups"]] == [
        "NOT_APPLICABLE",
        "NOT_APPLICABLE",
    ]

    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": {
                "setup_key": "new-but-locked",
                "direction": "LONG",
            }
        }
    }
    locked = suppress_candidate_for_constitution_lock(
        ledger,
        {
            "cooldown_until": "2026-08-07T09:51:00+08:00",
            "cooldown_active": True,
            "requalification_required": True,
        },
        {"reason_codes": ["COOLDOWN_ACTIVE"]},
    )
    levels = locked["trade_levels"]
    assert levels["continuation_arm_candidate"] is None
    assert levels["constitution_filtered_candidate"]["setup_key"] == "new-but-locked"
    assert levels["constitution_gate"]["status"] == "BLOCKED"
