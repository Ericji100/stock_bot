from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from trade_monitor_bridge import BridgeError, DeliveryFileLock


DEFAULT_STATE_PATH = Path(".runtime/trade_monitor_runtime_state.json")
DEFAULT_GAP_SECONDS = 180
STATE_VERSION = 1


def begin_monitor_run(
    state_path: Path,
    *,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
    now: datetime | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if gap_seconds <= 0:
        raise BridgeError("gap_seconds_invalid", "gap_seconds must be positive")

    current_time = _as_utc(now or datetime.now(timezone.utc))
    current_run_id = str(run_id or uuid.uuid4())
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")

    with DeliveryFileLock(lock_path):
        state = _read_state(state_path)
        last_success = _parse_timestamp(state.get("last_success_at"))
        elapsed = None if last_success is None else max(0.0, (current_time - last_success).total_seconds())
        pending = state.get("resume_pending", False) is True
        gap_exceeded = elapsed is not None and elapsed > gap_seconds
        first_run = last_success is None
        force_notify = pending or first_run or gap_exceeded

        if pending:
            reason = "resume_notification_pending"
        elif first_run:
            reason = "no_previous_success"
        elif gap_exceeded:
            reason = "execution_gap_exceeded"
        else:
            reason = "continuous_monitoring"

        if force_notify and not pending:
            state["interruption_detected_at"] = current_time.isoformat()
        state.update(
            {
                "version": STATE_VERSION,
                "active_run_id": current_run_id,
                "last_started_at": current_time.isoformat(),
                "resume_pending": force_notify,
            }
        )
        _write_state_atomic(state_path, state)

    return {
        "ok": True,
        "status": "started",
        "run_id": current_run_id,
        "force_notify": force_notify,
        "reason": reason,
        "gap_seconds": None if elapsed is None else round(elapsed, 3),
        "threshold_seconds": gap_seconds,
    }


def complete_monitor_run(
    state_path: Path,
    *,
    run_id: str,
    resume_delivered: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_run_id = str(run_id or "").strip()
    if not current_run_id:
        raise BridgeError("run_id_missing", "run_id is required")

    current_time = _as_utc(now or datetime.now(timezone.utc))
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with DeliveryFileLock(lock_path):
        state = _read_state(state_path)
        if state.get("active_run_id") != current_run_id:
            return {
                "ok": True,
                "status": "stale",
                "run_id": current_run_id,
                "resume_pending": state.get("resume_pending", False) is True,
            }

        was_pending = state.get("resume_pending", False) is True
        state.update(
            {
                "version": STATE_VERSION,
                "last_success_at": current_time.isoformat(),
                "last_completed_run_id": current_run_id,
                "resume_pending": was_pending and not resume_delivered,
            }
        )
        state.pop("active_run_id", None)
        if was_pending and resume_delivered:
            state["resume_delivered_at"] = current_time.isoformat()
        _write_state_atomic(state_path, state)

    return {
        "ok": True,
        "status": "completed",
        "run_id": current_run_id,
        "resume_pending": was_pending and not resume_delivered,
        "resume_delivered": bool(resume_delivered),
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track trade-monitor interruptions and resume notifications")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    begin_parser = subparsers.add_parser("begin")
    begin_parser.add_argument("--gap-seconds", type=int, default=DEFAULT_GAP_SECONDS)

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--run-id", required=True)
    complete_parser.add_argument("--resume-delivered", required=True, choices=("true", "false"))
    return parser


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    output = stdout or sys.stdout
    parser = build_argument_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "begin":
            payload = begin_monitor_run(args.state_file, gap_seconds=args.gap_seconds)
        else:
            payload = complete_monitor_run(
                args.state_file,
                run_id=args.run_id,
                resume_delivered=args.resume_delivered == "true",
            )
        exit_code = 0
    except BridgeError as exc:
        exit_code = 2
        payload = {"ok": False, "status": "failed", "error_code": exc.code, "error": str(exc)}
    except SystemExit:
        raise
    except Exception:
        exit_code = 2
        payload = {
            "ok": False,
            "status": "failed",
            "error_code": "resume_guard_internal_error",
            "error": "Trade monitor resume guard failed",
        }

    output.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()
    return exit_code


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "resume_pending": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError("runtime_state_invalid", "Trade monitor runtime state is invalid") from exc
    if not isinstance(payload, dict):
        raise BridgeError("runtime_state_invalid", "Trade monitor runtime state is invalid")
    return payload


def _write_state_atomic(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise BridgeError("runtime_state_write_failed", "Trade monitor runtime state could not be saved") from exc
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BridgeError("runtime_state_invalid", "Trade monitor runtime timestamp is invalid") from exc
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise BridgeError("runtime_time_invalid", "Runtime timestamps must include a timezone")
    return value.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
