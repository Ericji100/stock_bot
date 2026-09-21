from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import trade_monitor.resume_guard as guard


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def test_first_run_forces_resume_notification(tmp_path):
    state_path = tmp_path / "runtime.json"

    result = guard.begin_monitor_run(state_path, now=NOW, run_id="run-1")

    assert result["force_notify"] is True
    assert result["reason"] == "no_previous_success"
    assert result["gap_seconds"] is None


def test_continuous_monitoring_does_not_change_normal_decision(tmp_path):
    state_path = tmp_path / "runtime.json"
    guard.begin_monitor_run(state_path, now=NOW, run_id="run-1")
    guard.complete_monitor_run(state_path, run_id="run-1", resume_delivered=True, now=NOW)

    result = guard.begin_monitor_run(state_path, now=NOW + timedelta(seconds=60), run_id="run-2")

    assert result["force_notify"] is False
    assert result["reason"] == "continuous_monitoring"
    assert result["gap_seconds"] == 60


def test_exactly_three_minutes_is_not_considered_exceeded(tmp_path):
    state_path = tmp_path / "runtime.json"
    guard.begin_monitor_run(state_path, now=NOW, run_id="run-1")
    guard.complete_monitor_run(state_path, run_id="run-1", resume_delivered=True, now=NOW)

    result = guard.begin_monitor_run(state_path, now=NOW + timedelta(seconds=180), run_id="run-2")

    assert result["force_notify"] is False


def test_gap_over_three_minutes_forces_notification(tmp_path):
    state_path = tmp_path / "runtime.json"
    guard.begin_monitor_run(state_path, now=NOW, run_id="run-1")
    guard.complete_monitor_run(state_path, run_id="run-1", resume_delivered=True, now=NOW)

    result = guard.begin_monitor_run(state_path, now=NOW + timedelta(seconds=181), run_id="run-2")

    assert result["force_notify"] is True
    assert result["reason"] == "execution_gap_exceeded"
    assert result["gap_seconds"] == 181


def test_failed_resume_delivery_remains_pending_for_next_run(tmp_path):
    state_path = tmp_path / "runtime.json"
    guard.begin_monitor_run(state_path, now=NOW, run_id="run-1")
    completed = guard.complete_monitor_run(
        state_path,
        run_id="run-1",
        resume_delivered=False,
        now=NOW + timedelta(seconds=1),
    )

    next_run = guard.begin_monitor_run(
        state_path,
        now=NOW + timedelta(seconds=61),
        run_id="run-2",
    )

    assert completed["resume_pending"] is True
    assert next_run["force_notify"] is True
    assert next_run["reason"] == "resume_notification_pending"


def test_successful_resume_delivery_clears_pending_state(tmp_path):
    state_path = tmp_path / "runtime.json"
    guard.begin_monitor_run(state_path, now=NOW, run_id="run-1")
    completed = guard.complete_monitor_run(
        state_path,
        run_id="run-1",
        resume_delivered=True,
        now=NOW + timedelta(seconds=1),
    )
    next_run = guard.begin_monitor_run(
        state_path,
        now=NOW + timedelta(seconds=61),
        run_id="run-2",
    )

    assert completed["resume_pending"] is False
    assert next_run["force_notify"] is False


def test_overlapping_run_is_rejected_while_lease_is_active(tmp_path):
    state_path = tmp_path / "runtime.json"
    guard.begin_monitor_run(state_path, now=NOW, run_id="run-1")

    result = guard.begin_monitor_run(state_path, now=NOW + timedelta(seconds=1), run_id="run-2")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "busy"
    assert result["run_id"] is None
    assert state["active_run_id"] == "run-1"


def test_expired_lease_is_recovered_and_stale_completion_cannot_clear_it(tmp_path):
    state_path = tmp_path / "runtime.json"
    guard.begin_monitor_run(state_path, now=NOW, run_id="run-1", lease_seconds=180)
    recovered = guard.begin_monitor_run(
        state_path,
        now=NOW + timedelta(seconds=181),
        run_id="run-2",
        lease_seconds=180,
    )

    result = guard.complete_monitor_run(
        state_path,
        run_id="run-1",
        resume_delivered=True,
        now=NOW + timedelta(seconds=182),
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert recovered["status"] == "started"
    assert recovered["force_notify"] is True
    assert recovered["reason"] == "active_run_lease_expired"
    assert result["status"] == "stale"
    assert state["active_run_id"] == "run-2"
    assert state["resume_pending"] is True


def test_state_contains_only_runtime_metadata(tmp_path):
    state_path = tmp_path / "runtime.json"
    guard.begin_monitor_run(state_path, now=NOW, run_id="run-safe")
    guard.complete_monitor_run(state_path, run_id="run-safe", resume_delivered=True, now=NOW)
    text = state_path.read_text(encoding="utf-8")

    assert "api_token" not in text
    assert "chat_id" not in text
    assert "canonical_message" not in text
    assert "message_id" not in text


def test_cli_emits_machine_readable_json(tmp_path):
    stdout = io.StringIO()
    state_path = tmp_path / "runtime.json"

    code = guard.main(["--state-file", str(state_path), "begin", "--gap-seconds", "180"], stdout=stdout)
    payload = json.loads(stdout.getvalue())

    assert code == 0
    assert payload["ok"] is True
    assert payload["status"] == "started"
    assert payload["force_notify"] is True
