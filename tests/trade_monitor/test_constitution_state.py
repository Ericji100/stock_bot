from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trade_monitor import constitution_state as constitution


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def event(
    event_type: str,
    timestamp: str,
    sequence: int,
    *,
    setup_id: str | None = None,
    position_id: str | None = None,
) -> dict[str, object]:
    entering = event_type == "SIM_ENTER"
    return {
        "event_type": event_type,
        "event_id": f"event-{sequence}",
        "setup_id": setup_id,
        "position_id": position_id,
        "direction": "LONG" if entering else None,
        "entry_price_estimate": 46800 if entering else None,
        "stop_price_estimate": 46780 if entering else None,
        "risk_points": 20 if entering else None,
        "latest_closed_bar_time": timestamp,
        "reason": "test transition",
    }


def apply(path: Path, payload: dict[str, object]) -> dict[str, object]:
    return constitution.apply_constitution_event(
        path,
        payload,
        expected_latest_closed_bar_time=str(payload["latest_closed_bar_time"]),
    )


@pytest.mark.parametrize(
    ("timestamp", "trading_day", "entry_open"),
    [
        ("2026-09-02T13:44:00+08:00", "2026-09-02", True),
        ("2026-09-02T13:45:00+08:00", "2026-09-02", False),
        ("2026-09-02T14:59:00+08:00", "2026-09-02", False),
        ("2026-09-02T15:00:00+08:00", "2026-09-03", True),
        ("2026-09-02T23:59:00+08:00", "2026-09-03", True),
        ("2026-09-03T00:00:00+08:00", "2026-09-03", True),
        ("2026-09-04T15:00:00+08:00", "2026-09-07", True),
        ("2026-09-05T00:00:00+08:00", "2026-09-07", True),
        ("2026-09-07T08:45:00+08:00", "2026-09-07", True),
    ],
)
def test_exchange_trading_day_boundaries(timestamp: str, trading_day: str, entry_open: bool) -> None:
    value = at(timestamp)

    assert constitution.trading_day_for(value) == trading_day
    assert constitution.entry_window_open(value) is entry_open


def test_new_trading_day_resets_counters(tmp_path: Path) -> None:
    path = tmp_path / "constitution.json"
    enter = event("SIM_ENTER", "2026-09-02T15:05:00+08:00", 1, setup_id="setup-a", position_id="p1")
    apply(path, enter)
    apply(path, event("SIM_STOP", "2026-09-02T15:06:00+08:00", 2, position_id="p1"))

    snapshot = constitution.load_constitution_snapshot(path, at=at("2026-09-03T15:00:00+08:00"))

    assert snapshot["trading_day"] == "2026-09-04"
    assert snapshot["simulated_entry_count"] == 0
    assert snapshot["consecutive_simulated_stops"] == 0


def test_daily_entries_have_no_hard_limit_and_one_reentry_per_setup(tmp_path: Path) -> None:
    path = tmp_path / "constitution.json"
    timestamps = ["15:01", "15:03", "15:05"]
    setups = ["setup-a", "setup-a", "setup-b"]
    for index, (clock, setup_id) in enumerate(zip(timestamps, setups), start=1):
        stamp = f"2026-09-02T{clock}:00+08:00"
        position_id = f"p{index}"
        apply(path, event("SIM_ENTER", stamp, index * 2 - 1, setup_id=setup_id, position_id=position_id))
        apply(path, event("SIM_EXIT", stamp, index * 2, position_id=position_id))

    third_for_same_setup = event(
        "SIM_ENTER",
        "2026-09-02T15:07:00+08:00",
        7,
        setup_id="setup-a",
        position_id="p4",
    )
    with pytest.raises(constitution.ConstitutionStateError, match="one allowed re-entry"):
        apply(path, third_for_same_setup)

    fourth = event(
        "SIM_ENTER",
        "2026-09-02T15:08:00+08:00",
        8,
        setup_id="setup-c",
        position_id="p4",
    )
    result = apply(path, fourth)

    snapshot = constitution.load_constitution_snapshot(path, at=at("2026-09-02T15:08:00+08:00"))
    assert result["status"] == "applied"
    assert snapshot["simulated_entry_count"] == 4
    assert snapshot["remaining_entry_quota"] is None
    assert snapshot["trading_locked"] is False
    assert snapshot["setup_entry_counts"]["setup-a"] == 2


def test_third_consecutive_stop_starts_cooldown_and_requires_requalification(tmp_path: Path) -> None:
    path = tmp_path / "constitution.json"
    last_stop = None
    for index in range(1, 4):
        minute = index * 2
        enter_at = f"2026-09-02T15:{minute:02d}:00+08:00"
        stop_at = f"2026-09-02T15:{minute + 1:02d}:00+08:00"
        position_id = f"p{index}"
        apply(path, event("SIM_ENTER", enter_at, index * 2 - 1, setup_id=f"setup-{index}", position_id=position_id))
        last_stop = event("SIM_STOP", stop_at, index * 2, position_id=position_id)
        apply(path, last_stop)

    assert last_stop is not None
    snapshot = constitution.load_constitution_snapshot(path, at=at("2026-09-02T15:07:00+08:00"))
    assert snapshot["consecutive_simulated_stops"] == 3
    assert snapshot["cooldown_active"] is True
    assert snapshot["cooldown_until"] == "2026-09-02T15:37:00+08:00"
    assert snapshot["requalification_required"] is True

    early = event("COOLDOWN_REQUALIFIED", "2026-09-02T15:36:00+08:00", 7)
    with pytest.raises(constitution.ConstitutionStateError, match="cooldown"):
        apply(path, early)

    qualified = event("COOLDOWN_REQUALIFIED", "2026-09-02T15:37:00+08:00", 8)
    result = apply(path, qualified)
    assert result["state"]["requalification_required"] is False
    assert result["state"]["consecutive_simulated_stops"] == 0


def test_same_event_is_applied_exactly_once(tmp_path: Path) -> None:
    path = tmp_path / "constitution.json"
    payload = event(
        "SIM_ENTER",
        "2026-09-02T15:01:00+08:00",
        1,
        setup_id="setup-a",
        position_id="p1",
    )

    first = apply(path, payload)
    changed_non_identity_text = dict(payload)
    changed_non_identity_text["reason"] = "same event rerun with different explanatory prose"
    second = apply(path, changed_non_identity_text)

    assert first["status"] == "applied"
    assert second["status"] == "duplicate"
    assert second["state"]["simulated_entry_count"] == 1


def test_corrupt_state_fails_safe_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "constitution.json"
    path.write_text("{broken", encoding="utf-8")

    snapshot = constitution.load_constitution_snapshot(path, at=at("2026-09-02T15:01:00+08:00"))

    assert snapshot["ok"] is False
    assert snapshot["trading_locked"] is True
    assert snapshot["remaining_entry_quota"] == 0
    assert path.read_text(encoding="utf-8") == "{broken"


def test_atomic_write_leaves_valid_json_and_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "constitution.json"
    payload = event(
        "SIM_ENTER",
        "2026-09-02T15:01:00+08:00",
        1,
        setup_id="setup-a",
        position_id="p1",
    )

    apply(path, payload)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["simulated_entry_count"] == 1
    assert list(tmp_path.glob(".constitution.json.*.tmp")) == []


def test_event_time_must_match_prepared_bar() -> None:
    payload = event("NONE", "2026-09-02T15:01:00+08:00", 1)

    with pytest.raises(constitution.ConstitutionStateError, match="anchored"):
        constitution.validate_constitution_event(
            payload,
            expected_latest_closed_bar_time="2026-09-02T15:02:00+08:00",
        )
