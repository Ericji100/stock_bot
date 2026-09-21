from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, TextIO
from zoneinfo import ZoneInfo

from .bridge import BridgeError, DeliveryFileLock


TAIPEI = ZoneInfo("Asia/Taipei")
STATE_VERSION = 1
DETAIL = "DETAIL"
OVERVIEW = "OVERVIEW"
UNKNOWN = "UNKNOWN"

LARGE_TRENDS = {
    "強勢偏多",
    "偏多但回檔",
    "盤整",
    "偏空但反彈",
    "強勢偏空",
    "資料不足",
}
WAVE_STAGES = {"推進初段", "推進中段", "推進末段", "拉回", "反彈", "轉換", "盤整", "資料不足"}
QUADRANTS = {"第一象限", "第二象限", "第三象限", "第四象限", "資料不足"}


class DualScaleError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def overview_slot(at: datetime, interval_minutes: int = 15) -> datetime:
    local = _as_taipei(at)
    if interval_minutes <= 0 or 60 % interval_minutes != 0:
        raise DualScaleError("dual_scale_interval_invalid", "Overview interval must divide one hour")
    minute = (local.minute // interval_minutes) * interval_minutes
    return local.replace(minute=minute, second=0, microsecond=0)


def plan_overview(
    path: Path,
    *,
    at: datetime,
    interval_minutes: int = 15,
    max_defer_minutes: int = 2,
    critical_event: bool = False,
) -> dict[str, Any]:
    local = _as_taipei(at)
    if max_defer_minutes < 0:
        raise DualScaleError("dual_scale_defer_invalid", "Overview defer window must be non-negative")
    with _state_lock(path):
        state = _load_or_new(path, local)
        changed = _expire_lease(state, local)
        slot = overview_slot(local, interval_minutes)
        elapsed_minutes = int((local - slot).total_seconds() // 60)

        if state["view_mode"] != DETAIL or state["recovery_required"]:
            if changed:
                _write_state_atomic(path, state)
            return _plan_payload(state, "RECOVERY_REQUIRED", slot, local)

        if state["last_overview_slot"] == slot.isoformat():
            state["pending_overview_slot"] = None
            if changed:
                _write_state_atomic(path, state)
            return _plan_payload(state, "ALREADY_COMPLETED", slot, local)

        if elapsed_minutes > max_defer_minutes:
            if state.get("pending_overview_slot") == slot.isoformat():
                state["pending_overview_slot"] = None
                state["last_missed_slot"] = slot.isoformat()
                state["updated_at"] = local.isoformat()
                changed = True
            if changed:
                _write_state_atomic(path, state)
            return _plan_payload(state, "NOT_DUE", slot, local)

        state["pending_overview_slot"] = slot.isoformat()
        state["updated_at"] = local.isoformat()
        _write_state_atomic(path, state)
        status = "DEFERRED_CRITICAL" if critical_event else "DUE"
        return _plan_payload(state, status, slot, local)


def acquire_overview(
    path: Path,
    *,
    owner: str,
    at: datetime,
    lease_seconds: int = 45,
) -> dict[str, Any]:
    local = _as_taipei(at)
    normalized_owner = _nonempty(owner, "owner", 128)
    if lease_seconds <= 0:
        raise DualScaleError("dual_scale_lease_invalid", "Overview lease must be positive")
    with _state_lock(path):
        state = _load_or_new(path, local)
        _expire_lease(state, local)
        lease = state.get("lease")
        if isinstance(lease, dict):
            return {"ok": True, "status": "BUSY", "lease": dict(lease)}
        if state["view_mode"] != DETAIL or state["recovery_required"]:
            return {"ok": False, "status": "RECOVERY_REQUIRED"}
        if not state.get("pending_overview_slot"):
            return {"ok": False, "status": "NOT_PLANNED"}
        state["lease"] = {
            "owner": normalized_owner,
            "role": OVERVIEW,
            "acquired_at": local.isoformat(),
            "expires_at": (local + timedelta(seconds=lease_seconds)).isoformat(),
        }
        state["view_mode"] = OVERVIEW
        state["updated_at"] = local.isoformat()
        _write_state_atomic(path, state)
        return {"ok": True, "status": "ACQUIRED", "lease": dict(state["lease"])}


def complete_overview(
    path: Path,
    *,
    owner: str,
    at: datetime,
    summary: Mapping[str, Any],
    restored_detail: bool,
    detail_bar_count: int = 180,
    overview_bar_count: int = 296,
) -> dict[str, Any]:
    local = _as_taipei(at)
    normalized_owner = _nonempty(owner, "owner", 128)
    normalized_summary = _validate_overview_summary(summary)
    if detail_bar_count <= 0 or overview_bar_count <= detail_bar_count:
        raise DualScaleError("dual_scale_bar_counts_invalid", "Overview bar count must exceed detail bar count")
    with _state_lock(path):
        state = _load_or_new(path, local)
        if _expire_lease(state, local):
            _write_state_atomic(path, state)
            raise DualScaleError("dual_scale_lease_expired", "Overview lease expired before completion")
        lease = state.get("lease")
        if not isinstance(lease, dict) or lease.get("role") != OVERVIEW or lease.get("owner") != normalized_owner:
            raise DualScaleError("dual_scale_lease_mismatch", "Overview completion does not own the active lease")
        slot = state.get("pending_overview_slot")
        state["lease"] = None
        state["updated_at"] = local.isoformat()
        if not restored_detail:
            state["view_mode"] = UNKNOWN
            state["recovery_required"] = True
            state["overview_context"] = None
            _write_state_atomic(path, state)
            return {"ok": False, "status": "RESTORE_REQUIRED"}

        state["view_mode"] = DETAIL
        state["recovery_required"] = False
        state["detail_bar_count"] = detail_bar_count
        state["overview_bar_count"] = overview_bar_count
        state["last_overview_slot"] = slot
        state["pending_overview_slot"] = None
        state["last_overview_at"] = local.isoformat()
        state["last_detail_restored_at"] = local.isoformat()
        state["overview_context"] = normalized_summary
        _write_state_atomic(path, state)
        return {"ok": True, "status": "COMPLETED", "overview_slot": slot}


def abort_overview(
    path: Path,
    *,
    owner: str,
    at: datetime,
    restored_detail: bool,
    detail_bar_count: int = 180,
) -> dict[str, Any]:
    local = _as_taipei(at)
    normalized_owner = _nonempty(owner, "owner", 128)
    if detail_bar_count <= 0:
        raise DualScaleError("dual_scale_bar_counts_invalid", "Detail bar count must be positive")
    with _state_lock(path):
        state = _load_or_new(path, local)
        if _expire_lease(state, local):
            _write_state_atomic(path, state)
            return {"ok": False, "status": "LEASE_EXPIRED_RECOVERY_REQUIRED"}
        lease = state.get("lease")
        if not isinstance(lease, dict) or lease.get("role") != OVERVIEW or lease.get("owner") != normalized_owner:
            raise DualScaleError("dual_scale_lease_mismatch", "Overview abort does not own the active lease")
        pending_slot = state.get("pending_overview_slot")
        state["lease"] = None
        state["pending_overview_slot"] = None
        state["last_missed_slot"] = pending_slot
        state["updated_at"] = local.isoformat()
        if restored_detail:
            state["view_mode"] = DETAIL
            state["recovery_required"] = False
            state["detail_bar_count"] = detail_bar_count
            state["last_detail_restored_at"] = local.isoformat()
            status = "ABORTED_DETAIL_RESTORED"
        else:
            state["view_mode"] = UNKNOWN
            state["recovery_required"] = True
            state["overview_context"] = None
            status = "ABORTED_RECOVERY_REQUIRED"
        _write_state_atomic(path, state)
        return {"ok": restored_detail, "status": status}


def restore_detail(
    path: Path,
    *,
    owner: str,
    at: datetime,
    detail_bar_count: int = 180,
) -> dict[str, Any]:
    local = _as_taipei(at)
    _nonempty(owner, "owner", 128)
    if detail_bar_count <= 0:
        raise DualScaleError("dual_scale_bar_counts_invalid", "Detail bar count must be positive")
    with _state_lock(path):
        state = _load_or_new(path, local)
        _expire_lease(state, local)
        lease = state.get("lease")
        if isinstance(lease, dict) and lease.get("owner") != owner:
            raise DualScaleError("dual_scale_lease_busy", "Another Chrome overview owner still holds the lease")
        state["view_mode"] = DETAIL
        state["recovery_required"] = False
        state["detail_bar_count"] = detail_bar_count
        state["last_detail_restored_at"] = local.isoformat()
        state["lease"] = None
        state["updated_at"] = local.isoformat()
        _write_state_atomic(path, state)
        return {"ok": True, "status": "DETAIL_READY", "detail_bar_count": detail_bar_count}


def detail_capture_guard(path: Path, *, at: datetime) -> dict[str, Any]:
    local = _as_taipei(at)
    with _state_lock(path):
        state = _load_or_new(path, local)
        changed = _expire_lease(state, local)
        if changed:
            _write_state_atomic(path, state)
        lease = state.get("lease")
        if isinstance(lease, dict) and lease.get("role") == OVERVIEW:
            return {"ok": False, "status": "OVERVIEW_BUSY", "expires_at": lease.get("expires_at")}
        if state["view_mode"] != DETAIL or state["recovery_required"]:
            return {"ok": False, "status": "RECOVERY_REQUIRED"}
        return {"ok": True, "status": "DETAIL_READY", "detail_bar_count": state["detail_bar_count"]}


def record_detail_capture(
    path: Path,
    *,
    captured_at: datetime,
    latest_closed_bar_time: datetime,
) -> dict[str, Any]:
    captured = _as_taipei(captured_at)
    closed = _as_taipei(latest_closed_bar_time)
    with _state_lock(path):
        state = _load_or_new(path, captured)
        _expire_lease(state, captured)
        if state["view_mode"] != DETAIL or state["recovery_required"] or state.get("lease") is not None:
            raise DualScaleError("dual_scale_detail_not_ready", "Detail capture is unsafe while overview is active")
        state["last_detail_capture_at"] = captured.isoformat()
        state["last_detail_closed_bar_time"] = closed.isoformat()
        state["updated_at"] = captured.isoformat()
        _write_state_atomic(path, state)
        return {"ok": True, "status": "RECORDED", "latest_closed_bar_time": closed.isoformat()}


def load_overview_context(
    path: Path,
    *,
    at: datetime,
    expected_latest_closed_bar_time: datetime,
    max_age_minutes: int = 20,
) -> dict[str, Any]:
    local = _as_taipei(at)
    expected = _as_taipei(expected_latest_closed_bar_time)
    if max_age_minutes <= 0:
        raise DualScaleError("dual_scale_max_age_invalid", "Overview max age must be positive")
    try:
        with _state_lock(path):
            state = _load_or_new(path, local)
            changed = _expire_lease(state, local)
            if changed:
                _write_state_atomic(path, state)
            if state["view_mode"] != DETAIL or state["recovery_required"]:
                return {"ok": False, "status": "RECOVERY_REQUIRED", "context": None}
            context = state.get("overview_context")
            if not isinstance(context, dict):
                return {"ok": True, "status": "UNAVAILABLE", "context": None}
            captured = _parse_time(context.get("captured_at"), "captured_at")
            context_closed = _parse_time(context.get("latest_closed_bar_time"), "latest_closed_bar_time")
            if context_closed > expected:
                return {"ok": False, "status": "FUTURE_CONTEXT", "context": None}
            age = (local - captured).total_seconds() / 60
            if age < 0:
                return {"ok": False, "status": "FUTURE_CONTEXT", "context": None}
            if age > max_age_minutes:
                return {"ok": True, "status": "STALE", "age_minutes": round(age, 3), "context": None}
            return {
                "ok": True,
                "status": "FRESH",
                "age_minutes": round(age, 3),
                "detail_bar_count": state["detail_bar_count"],
                "overview_bar_count": state["overview_bar_count"],
                "context": dict(context),
            }
    except (DualScaleError, BridgeError) as exc:
        code = exc.code if isinstance(exc, DualScaleError) else "dual_scale_state_locked"
        return {"ok": False, "status": "INVALID", "reason": code, "context": None}


def _new_state(at: datetime) -> dict[str, Any]:
    local = _as_taipei(at)
    return {
        "version": STATE_VERSION,
        "view_mode": UNKNOWN,
        "recovery_required": True,
        "detail_bar_count": 180,
        "overview_bar_count": 296,
        "lease": None,
        "pending_overview_slot": None,
        "last_overview_slot": None,
        "last_missed_slot": None,
        "last_overview_at": None,
        "last_detail_restored_at": None,
        "last_detail_capture_at": None,
        "last_detail_closed_bar_time": None,
        "overview_context": None,
        "updated_at": local.isoformat(),
    }


def _load_or_new(path: Path, at: datetime) -> dict[str, Any]:
    if not path.exists():
        return _new_state(at)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DualScaleError("dual_scale_state_invalid", "Dual-scale state is invalid") from exc
    _validate_state(payload)
    return payload


def _validate_state(payload: Any) -> None:
    required = {
        "version",
        "view_mode",
        "recovery_required",
        "detail_bar_count",
        "overview_bar_count",
        "lease",
        "pending_overview_slot",
        "last_overview_slot",
        "last_missed_slot",
        "last_overview_at",
        "last_detail_restored_at",
        "last_detail_capture_at",
        "last_detail_closed_bar_time",
        "overview_context",
        "updated_at",
    }
    if not isinstance(payload, dict) or set(payload) != required or payload.get("version") != STATE_VERSION:
        raise DualScaleError("dual_scale_state_invalid", "Dual-scale state shape is invalid")
    if payload.get("view_mode") not in {DETAIL, OVERVIEW, UNKNOWN}:
        raise DualScaleError("dual_scale_state_invalid", "Dual-scale view mode is invalid")
    if not isinstance(payload.get("recovery_required"), bool):
        raise DualScaleError("dual_scale_state_invalid", "Dual-scale recovery flag is invalid")
    detail = payload.get("detail_bar_count")
    overview = payload.get("overview_bar_count")
    if not isinstance(detail, int) or not isinstance(overview, int) or detail <= 0 or overview <= detail:
        raise DualScaleError("dual_scale_state_invalid", "Dual-scale bar counts are invalid")
    for key in (
        "pending_overview_slot",
        "last_overview_slot",
        "last_missed_slot",
        "last_overview_at",
        "last_detail_restored_at",
        "last_detail_capture_at",
        "last_detail_closed_bar_time",
    ):
        if payload[key] is not None:
            _parse_time(payload[key], key)
    if payload["lease"] is not None:
        lease = payload["lease"]
        if not isinstance(lease, dict) or set(lease) != {"owner", "role", "acquired_at", "expires_at"}:
            raise DualScaleError("dual_scale_state_invalid", "Dual-scale lease is invalid")
        _nonempty(lease.get("owner"), "lease.owner", 128)
        if lease.get("role") != OVERVIEW:
            raise DualScaleError("dual_scale_state_invalid", "Dual-scale lease role is invalid")
        _parse_time(lease.get("acquired_at"), "lease.acquired_at")
        _parse_time(lease.get("expires_at"), "lease.expires_at")
    if payload["overview_context"] is not None:
        _validate_overview_summary(payload["overview_context"])
    _parse_time(payload["updated_at"], "updated_at")


def _validate_overview_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(summary, Mapping):
        raise DualScaleError("dual_scale_summary_invalid", "Overview summary must be an object")
    required = {
        "latest_closed_bar_time",
        "captured_at",
        "session_key",
        "large_trend",
        "wave_stage",
        "quadrant",
        "position",
        "large_defense_context",
        "key_zones",
        "source",
    }
    if set(summary) != required:
        raise DualScaleError("dual_scale_summary_invalid", "Overview summary fields are invalid")
    closed = _parse_time(summary.get("latest_closed_bar_time"), "latest_closed_bar_time")
    captured = _parse_time(summary.get("captured_at"), "captured_at")
    if captured < closed:
        raise DualScaleError("dual_scale_summary_invalid", "Overview capture cannot predate its closed K anchor")
    session_key = _nonempty(summary.get("session_key"), "session_key", 80)
    large_trend = str(summary.get("large_trend") or "")
    wave_stage = str(summary.get("wave_stage") or "")
    quadrant = str(summary.get("quadrant") or "")
    if large_trend not in LARGE_TRENDS or wave_stage not in WAVE_STAGES or quadrant not in QUADRANTS:
        raise DualScaleError("dual_scale_summary_invalid", "Overview classifications are invalid")
    position = _nonempty(summary.get("position"), "position", 300)
    defense = _string_list(summary.get("large_defense_context"), "large_defense_context", 4, 240)
    zones = _string_list(summary.get("key_zones"), "key_zones", 4, 240)
    if summary.get("source") != "CHROME_CONTROL_OVERVIEW":
        raise DualScaleError("dual_scale_summary_invalid", "Overview source must be Chrome control")
    return {
        "latest_closed_bar_time": closed.isoformat(),
        "captured_at": captured.isoformat(),
        "session_key": session_key,
        "large_trend": large_trend,
        "wave_stage": wave_stage,
        "quadrant": quadrant,
        "position": position,
        "large_defense_context": defense,
        "key_zones": zones,
        "source": "CHROME_CONTROL_OVERVIEW",
    }


def _expire_lease(state: dict[str, Any], at: datetime) -> bool:
    lease = state.get("lease")
    if not isinstance(lease, dict):
        return False
    if _parse_time(lease.get("expires_at"), "lease.expires_at") > at:
        return False
    state["lease"] = None
    state["view_mode"] = UNKNOWN
    state["recovery_required"] = True
    state["overview_context"] = None
    state["updated_at"] = at.isoformat()
    return True


def _plan_payload(state: Mapping[str, Any], status: str, slot: datetime, at: datetime) -> dict[str, Any]:
    return {
        "ok": status not in {"RECOVERY_REQUIRED"},
        "status": status,
        "slot": slot.isoformat(),
        "checked_at": at.isoformat(),
        "detail_bar_count": state["detail_bar_count"],
        "overview_bar_count": state["overview_bar_count"],
    }


def _state_lock(path: Path) -> DeliveryFileLock:
    return DeliveryFileLock(path.with_suffix(path.suffix + ".lock"), timeout_seconds=5.0)


def _write_state_atomic(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise DualScaleError("dual_scale_state_write_failed", "Dual-scale state could not be saved") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise DualScaleError("dual_scale_time_invalid", f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DualScaleError("dual_scale_time_invalid", f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DualScaleError("dual_scale_time_invalid", f"{field} must include a timezone")
    return parsed.astimezone(TAIPEI)


def _as_taipei(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DualScaleError("dual_scale_time_invalid", "Timestamp must include a timezone")
    return value.astimezone(TAIPEI)


def _nonempty(value: object, field: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length:
        raise DualScaleError("dual_scale_summary_invalid", f"{field} is invalid")
    return text


def _string_list(value: object, field: str, max_items: int, max_length: int) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise DualScaleError("dual_scale_summary_invalid", f"{field} is invalid")
    return [_nonempty(item, field, max_length) for item in value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chrome dual-scale chart-view coordinator")
    parser.add_argument("--state", type=Path, default=Path(".runtime/trade_monitor/dual_scale_state.json"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--now", required=True)
    plan.add_argument("--critical-event", action="store_true")
    plan.add_argument("--interval-minutes", type=int, default=15)
    plan.add_argument("--max-defer-minutes", type=int, default=2)

    acquire = subparsers.add_parser("acquire-overview")
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--now", required=True)
    acquire.add_argument("--lease-seconds", type=int, default=45)

    complete = subparsers.add_parser("complete-overview")
    complete.add_argument("--owner", required=True)
    complete.add_argument("--now", required=True)
    complete.add_argument("--restored-detail", choices=("true", "false"), required=True)
    complete.add_argument("--detail-bars", type=int, default=180)
    complete.add_argument("--overview-bars", type=int, default=296)

    abort = subparsers.add_parser("abort-overview")
    abort.add_argument("--owner", required=True)
    abort.add_argument("--now", required=True)
    abort.add_argument("--restored-detail", choices=("true", "false"), required=True)
    abort.add_argument("--detail-bars", type=int, default=180)

    restore = subparsers.add_parser("restore-detail")
    restore.add_argument("--owner", required=True)
    restore.add_argument("--now", required=True)
    restore.add_argument("--detail-bars", type=int, default=180)

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--now", required=True)
    snapshot.add_argument("--expected-closed", required=True)
    snapshot.add_argument("--max-age-minutes", type=int, default=20)
    return parser


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    input_stream = stdin or sys.stdin
    output = stdout or sys.stdout
    try:
        args = build_parser().parse_args(argv)
        if args.command == "plan":
            payload = plan_overview(
                args.state,
                at=_parse_time(args.now, "now"),
                interval_minutes=args.interval_minutes,
                max_defer_minutes=args.max_defer_minutes,
                critical_event=args.critical_event,
            )
        elif args.command == "acquire-overview":
            payload = acquire_overview(
                args.state,
                owner=args.owner,
                at=_parse_time(args.now, "now"),
                lease_seconds=args.lease_seconds,
            )
        elif args.command == "complete-overview":
            summary = json.loads(input_stream.read())
            payload = complete_overview(
                args.state,
                owner=args.owner,
                at=_parse_time(args.now, "now"),
                summary=summary,
                restored_detail=args.restored_detail == "true",
                detail_bar_count=args.detail_bars,
                overview_bar_count=args.overview_bars,
            )
        elif args.command == "abort-overview":
            payload = abort_overview(
                args.state,
                owner=args.owner,
                at=_parse_time(args.now, "now"),
                restored_detail=args.restored_detail == "true",
                detail_bar_count=args.detail_bars,
            )
        elif args.command == "restore-detail":
            payload = restore_detail(
                args.state,
                owner=args.owner,
                at=_parse_time(args.now, "now"),
                detail_bar_count=args.detail_bars,
            )
        else:
            payload = load_overview_context(
                args.state,
                at=_parse_time(args.now, "now"),
                expected_latest_closed_bar_time=_parse_time(args.expected_closed, "expected_closed"),
                max_age_minutes=args.max_age_minutes,
            )
        code = 0 if payload.get("ok") else 1
    except (DualScaleError, BridgeError, json.JSONDecodeError) as exc:
        code = 2
        error_code = exc.code if isinstance(exc, DualScaleError) else "dual_scale_command_failed"
        payload = {"ok": False, "status": "ERROR", "error": error_code}
    output.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
