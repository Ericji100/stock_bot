import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


TAIPEI_TZ = timezone(timedelta(hours=8))
HEARTBEAT_PATH = Path(".runtime") / "bot_heartbeat.json"
HEARTBEAT_STALE_SECONDS = 10 * 60


def _now_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(TAIPEI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    return current.astimezone(TAIPEI_TZ).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TAIPEI_TZ)
    return parsed.astimezone(TAIPEI_TZ)


def read_bot_heartbeat(path: Path | str = HEARTBEAT_PATH) -> dict[str, Any]:
    heartbeat_path = Path(path)
    if not heartbeat_path.exists():
        return {}
    try:
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_bot_heartbeat(
    *,
    job_queue_available: bool,
    last_scheduled_event: str | None = None,
    last_scheduled_event_at: str | None = None,
    schedule_health_update: dict[str, Any] | None = None,
    path: Path | str = HEARTBEAT_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    heartbeat_path = Path(path)
    previous = read_bot_heartbeat(heartbeat_path)
    timestamp = _now_iso(now)
    event = last_scheduled_event
    if event is None:
        event = previous.get("last_scheduled_event")
    event_at = last_scheduled_event_at
    if event_at is None:
        event_at = previous.get("last_scheduled_event_at")
    schedule_health = previous.get("schedule_health")
    if not isinstance(schedule_health, dict):
        schedule_health = {}
    if schedule_health_update:
        schedule_health.update(schedule_health_update)

    payload = {
        "updated_at": timestamp,
        "pid": os.getpid(),
        "job_queue_available": bool(job_queue_available),
        "last_scheduled_event": event,
        "last_scheduled_event_at": event_at,
        "schedule_health": schedule_health,
    }
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = heartbeat_path.with_suffix(heartbeat_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(heartbeat_path)
    return payload


def record_scheduled_heartbeat_event(
    label: str,
    *,
    job_queue_available: bool,
    path: Path | str = HEARTBEAT_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    event_at = _now_iso(now)
    return write_bot_heartbeat(
        job_queue_available=job_queue_available,
        last_scheduled_event=str(label),
        last_scheduled_event_at=event_at,
        schedule_health_update={
            "last_triggered": {"label": str(label), "at": event_at},
            "last_status": "triggered",
            "last_status_at": event_at,
        },
        path=path,
        now=now,
    )


def record_scheduled_status_heartbeat_event(
    status: str,
    event: dict[str, Any],
    *,
    path: Path | str = HEARTBEAT_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = str(event.get("timestamp") or _now_iso(now))
    task_event = {
        "task_id": event.get("task_id"),
        "label": event.get("label"),
        "status": status,
        "at": timestamp,
    }
    if event.get("elapsed_seconds") is not None:
        task_event["elapsed_seconds"] = event.get("elapsed_seconds")
    if event.get("error_summary"):
        task_event["error_summary"] = event.get("error_summary")
    update: dict[str, Any] = {
        "last_status": status,
        "last_status_at": timestamp,
    }
    if event.get("queue_size") is not None:
        update["queue_size"] = event.get("queue_size")
    if status == "queued":
        update["last_queued"] = task_event
    elif status == "started":
        update["current_task"] = task_event
        update["last_started"] = task_event
    elif status == "completed":
        update["current_task"] = None
        update["last_completed"] = task_event
    elif status in {"failed", "timeout"}:
        update["current_task"] = None
        update["last_failed"] = task_event
    elif status == "skipped_duplicate":
        update["last_skipped_duplicate"] = task_event

    previous = read_bot_heartbeat(path)
    return write_bot_heartbeat(
        job_queue_available=bool(previous.get("job_queue_available")),
        schedule_health_update=update,
        path=path,
        now=now,
    )


def update_schedule_health(
    *,
    job_queue_available: bool,
    path: Path | str = HEARTBEAT_PATH,
    now: datetime | None = None,
    **fields: Any,
) -> dict[str, Any]:
    return write_bot_heartbeat(
        job_queue_available=job_queue_available,
        schedule_health_update={key: value for key, value in fields.items()},
        path=path,
        now=now,
    )


def is_schedule_health_unhealthy(
    payload: dict[str, Any],
    *,
    unhealthy_seconds: int = 2 * 60 * 60,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    schedule_health = payload.get("schedule_health")
    if not isinstance(schedule_health, dict):
        return False, None
    reason = schedule_health.get("schedule_unhealthy_reason")
    if isinstance(reason, str) and reason.strip():
        return True, reason.strip()

    current_task = schedule_health.get("current_task")
    if isinstance(current_task, dict):
        started_at = _parse_iso(current_task.get("at"))
        if started_at is not None:
            current = now or datetime.now(TAIPEI_TZ)
            if current.tzinfo is None:
                current = current.replace(tzinfo=TAIPEI_TZ)
            age = current.astimezone(TAIPEI_TZ) - started_at
            if age.total_seconds() > unhealthy_seconds:
                label = current_task.get("label") or current_task.get("task_id") or "scheduled task"
                return True, f"{label} running too long ({int(age.total_seconds())}s)"
    return False, None


def is_bot_heartbeat_stale(
    path: Path | str = HEARTBEAT_PATH,
    *,
    stale_seconds: int = HEARTBEAT_STALE_SECONDS,
    now: datetime | None = None,
) -> bool:
    payload = read_bot_heartbeat(path)
    updated_at = _parse_iso(payload.get("updated_at"))
    if updated_at is None:
        return True
    current = now or datetime.now(TAIPEI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    age = current.astimezone(TAIPEI_TZ) - updated_at
    return age.total_seconds() > stale_seconds
