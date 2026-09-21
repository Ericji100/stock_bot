from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trade_monitor import dual_scale


TAIPEI = ZoneInfo("Asia/Taipei")


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 2, hour, minute, second, tzinfo=TAIPEI)


def _summary(*, closed: str = "2026-09-02T10:14:00+08:00") -> dict[str, object]:
    return {
        "latest_closed_bar_time": closed,
        "captured_at": "2026-09-02T10:15:10+08:00",
        "session_key": "2026-09-02-DAY",
        "large_trend": "強勢偏空",
        "wave_stage": "推進中段",
        "quadrant": "第四象限",
        "position": "價格位於大級空方趨勢中的反彈壓力區",
        "large_defense_context": ["大級空方防線仍有效"],
        "key_zones": ["上方壓力區圖面估計", "下方前低區圖面估計"],
        "source": "CHROME_CONTROL_OVERVIEW",
    }


def test_initial_state_fails_safe_until_chrome_confirms_detail_view(tmp_path: Path) -> None:
    path = tmp_path / "dual-scale.json"

    plan = dual_scale.plan_overview(path, at=_at(10, 15))
    guard = dual_scale.detail_capture_guard(path, at=_at(10, 15))

    assert plan["status"] == "RECOVERY_REQUIRED"
    assert guard["status"] == "RECOVERY_REQUIRED"
    assert not path.exists()


def test_quarter_hour_plan_can_defer_two_minutes_for_critical_event(tmp_path: Path) -> None:
    path = tmp_path / "dual-scale.json"
    dual_scale.restore_detail(path, owner="chrome-rc", at=_at(10, 14, 50))

    deferred = dual_scale.plan_overview(path, at=_at(10, 15), critical_event=True)
    due = dual_scale.plan_overview(path, at=_at(10, 16), critical_event=False)

    assert deferred["status"] == "DEFERRED_CRITICAL"
    assert due["status"] == "DUE"
    assert due["slot"] == "2026-09-02T10:15:00+08:00"


def test_overview_lease_blocks_detail_until_chrome_restores_and_records_summary(tmp_path: Path) -> None:
    path = tmp_path / "dual-scale.json"
    dual_scale.restore_detail(path, owner="chrome-rc", at=_at(10, 14, 50))
    dual_scale.plan_overview(path, at=_at(10, 15))

    acquired = dual_scale.acquire_overview(path, owner="overview-1015", at=_at(10, 15, 5))
    blocked = dual_scale.detail_capture_guard(path, at=_at(10, 15, 6))
    completed = dual_scale.complete_overview(
        path,
        owner="overview-1015",
        at=_at(10, 15, 12),
        summary=_summary(),
        restored_detail=True,
    )
    ready = dual_scale.detail_capture_guard(path, at=_at(10, 15, 13))
    snapshot = dual_scale.load_overview_context(
        path,
        at=_at(10, 16),
        expected_latest_closed_bar_time=_at(10, 15),
    )

    assert acquired["status"] == "ACQUIRED"
    assert blocked["status"] == "OVERVIEW_BUSY"
    assert completed["status"] == "COMPLETED"
    assert ready["status"] == "DETAIL_READY"
    assert snapshot["status"] == "FRESH"
    assert snapshot["context"]["source"] == "CHROME_CONTROL_OVERVIEW"
    assert not list(tmp_path.glob(".*.tmp"))


def test_expired_overview_lease_requires_visible_chrome_recovery(tmp_path: Path) -> None:
    path = tmp_path / "dual-scale.json"
    dual_scale.restore_detail(path, owner="chrome-rc", at=_at(10, 14, 50))
    dual_scale.plan_overview(path, at=_at(10, 15))
    dual_scale.acquire_overview(path, owner="overview-1015", at=_at(10, 15), lease_seconds=5)

    guard = dual_scale.detail_capture_guard(path, at=_at(10, 15, 6))
    recovered = dual_scale.restore_detail(path, owner="chrome-recovery", at=_at(10, 15, 10))

    assert guard["status"] == "RECOVERY_REQUIRED"
    assert recovered["status"] == "DETAIL_READY"
    assert dual_scale.detail_capture_guard(path, at=_at(10, 15, 11))["ok"] is True


def test_expired_lease_cannot_be_completed_with_a_late_summary(tmp_path: Path) -> None:
    path = tmp_path / "dual-scale.json"
    dual_scale.restore_detail(path, owner="chrome-rc", at=_at(10, 14, 50))
    dual_scale.plan_overview(path, at=_at(10, 15))
    dual_scale.acquire_overview(path, owner="overview-1015", at=_at(10, 15), lease_seconds=5)

    try:
        dual_scale.complete_overview(
            path,
            owner="overview-1015",
            at=_at(10, 15, 6),
            summary=_summary(),
            restored_detail=True,
        )
    except dual_scale.DualScaleError as exc:
        assert exc.code == "dual_scale_lease_expired"
    else:
        raise AssertionError("Expected expired overview lease to fail")
    assert dual_scale.detail_capture_guard(path, at=_at(10, 15, 7))["status"] == "RECOVERY_REQUIRED"


def test_abort_requires_same_owner_and_records_restore_result(tmp_path: Path) -> None:
    path = tmp_path / "dual-scale.json"
    dual_scale.restore_detail(path, owner="chrome-rc", at=_at(10, 14, 50))
    dual_scale.plan_overview(path, at=_at(10, 15))
    dual_scale.acquire_overview(path, owner="overview-1015", at=_at(10, 15, 1))

    try:
        dual_scale.abort_overview(
            path,
            owner="wrong-owner",
            at=_at(10, 15, 2),
            restored_detail=True,
        )
    except dual_scale.DualScaleError as exc:
        assert exc.code == "dual_scale_lease_mismatch"
    else:
        raise AssertionError("Expected owner mismatch")

    aborted = dual_scale.abort_overview(
        path,
        owner="overview-1015",
        at=_at(10, 15, 3),
        restored_detail=True,
    )
    assert aborted["status"] == "ABORTED_DETAIL_RESTORED"
    assert dual_scale.detail_capture_guard(path, at=_at(10, 15, 4))["ok"] is True


def test_stale_and_future_overview_context_are_never_used(tmp_path: Path) -> None:
    path = tmp_path / "dual-scale.json"
    dual_scale.restore_detail(path, owner="chrome-rc", at=_at(10, 14, 50))
    dual_scale.plan_overview(path, at=_at(10, 15))
    dual_scale.acquire_overview(path, owner="overview-1015", at=_at(10, 15, 1))
    dual_scale.complete_overview(
        path,
        owner="overview-1015",
        at=_at(10, 15, 12),
        summary=_summary(),
        restored_detail=True,
    )

    stale = dual_scale.load_overview_context(
        path,
        at=_at(10, 36),
        expected_latest_closed_bar_time=_at(10, 35),
        max_age_minutes=20,
    )
    future = dual_scale.load_overview_context(
        path,
        at=_at(10, 16),
        expected_latest_closed_bar_time=_at(10, 13),
    )

    assert stale["status"] == "STALE"
    assert stale["context"] is None
    assert future["status"] == "FUTURE_CONTEXT"
    assert future["context"] is None


def test_corrupt_state_is_fail_safe_and_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "dual-scale.json"
    path.write_text("{broken", encoding="utf-8")

    snapshot = dual_scale.load_overview_context(
        path,
        at=_at(10, 16),
        expected_latest_closed_bar_time=_at(10, 15),
    )

    assert snapshot["ok"] is False
    assert snapshot["status"] == "INVALID"
    assert path.read_text(encoding="utf-8") == "{broken"


def test_state_json_contains_no_account_or_delivery_credentials(tmp_path: Path) -> None:
    path = tmp_path / "dual-scale.json"
    dual_scale.restore_detail(path, owner="chrome-rc", at=_at(10, 14, 50))
    payload = json.loads(path.read_text(encoding="utf-8"))

    serialized = json.dumps(payload, ensure_ascii=False).lower()
    assert "token" not in serialized
    assert "chat_id" not in serialized
    assert "account" not in serialized


def test_cli_escapes_chinese_for_windows_safe_machine_output(tmp_path: Path) -> None:
    path = tmp_path / "dual-scale.json"
    dual_scale.restore_detail(path, owner="chrome-rc", at=_at(10, 14, 50))
    dual_scale.plan_overview(path, at=_at(10, 15))
    dual_scale.acquire_overview(path, owner="overview-1015", at=_at(10, 15, 1))
    dual_scale.complete_overview(
        path,
        owner="overview-1015",
        at=_at(10, 15, 12),
        summary=_summary(),
        restored_detail=True,
    )
    stdout = io.StringIO()

    code = dual_scale.main(
        [
            "--state",
            str(path),
            "snapshot",
            "--now",
            "2026-09-02T10:16:00+08:00",
            "--expected-closed",
            "2026-09-02T10:15:00+08:00",
        ],
        stdout=stdout,
    )

    assert code == 0
    assert "偏空" not in stdout.getvalue()
    assert "\\u504f\\u7a7a" in stdout.getvalue()
    assert json.loads(stdout.getvalue())["context"]["large_trend"] == "強勢偏空"
