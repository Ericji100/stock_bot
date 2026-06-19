from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .error_classification_service import classify_error

COMMAND_RUNTIME_SCHEMA_VERSION = "command_runtime_v1"
ACTIVE_TASK_STATUSES = {"running", "stopping", "timeout"}


@dataclass
class CommandRuntimeTask:
    task_id: str
    label: str
    task_type: str
    status: str = "running"
    user_id: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    timeout_seconds: float | None = None
    report_path: str | None = None
    progress_message: str | None = None
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = COMMAND_RUNTIME_SCHEMA_VERSION
    _stop_event: threading.Event | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "label": self.label,
            "task_type": self.task_type,
            "status": self.status,
            "user_id": self.user_id,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "timeout_seconds": self.timeout_seconds,
            "report_path": self.report_path,
            "progress_message": self.progress_message,
            "error": self.error,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
            "stop_requested": self.stop_requested,
            "elapsed_seconds": self.elapsed_seconds,
        }

    @property
    def stop_requested(self) -> bool:
        return bool(self._stop_event and self._stop_event.is_set())

    @property
    def elapsed_seconds(self) -> float:
        try:
            started = datetime.fromisoformat(self.started_at)
            return max(0.0, (datetime.now(started.tzinfo).astimezone() - started).total_seconds())
        except Exception:
            return 0.0

    @property
    def timed_out(self) -> bool:
        return bool(self.timeout_seconds and self.elapsed_seconds > float(self.timeout_seconds))


class CommandRuntimeService:
    def __init__(self):
        self._lock = threading.RLock()
        self._tasks: dict[str, CommandRuntimeTask] = {}

    def start_task(
        self,
        task_id: str,
        *,
        label: str,
        task_type: str,
        user_id: str | None = None,
        timeout_seconds: float | None = None,
        stop_event: threading.Event | None = None,
        report_path: str | Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommandRuntimeTask:
        with self._lock:
            task = CommandRuntimeTask(
                task_id=task_id,
                label=label,
                task_type=task_type,
                user_id=user_id,
                timeout_seconds=timeout_seconds,
                report_path=str(report_path) if report_path is not None else None,
                metadata=metadata or {},
                _stop_event=stop_event,
            )
            self._tasks[task_id] = task
            return task

    def try_start_task(
        self,
        task_id: str,
        *,
        label: str,
        task_type: str,
        user_id: str | None = None,
        timeout_seconds: float | None = None,
        stop_event: threading.Event | None = None,
        report_path: str | Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[CommandRuntimeTask, bool]:
        """Start a task only when the same task_id is not already active."""

        with self._lock:
            existing = self._tasks.get(task_id)
            if existing and existing.status in ACTIVE_TASK_STATUSES:
                return existing, False
            return (
                self.start_task(
                    task_id,
                    label=label,
                    task_type=task_type,
                    user_id=user_id,
                    timeout_seconds=timeout_seconds,
                    stop_event=stop_event,
                    report_path=report_path,
                    metadata=metadata,
                ),
                True,
            )

    def is_task_active(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            return bool(task and task.status in ACTIVE_TASK_STATUSES)

    def update_progress(self, task_id: str, message: str) -> CommandRuntimeTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            task.progress_message = str(message)
            task.updated_at = _now()
            if task.timed_out and task.status == "running":
                task.status = "timeout"
                task.error = classify_error(TimeoutError("task timeout"), operation=task.task_type).to_dict()
            return task

    def finish_task(self, task_id: str, *, report_path: str | Path | None = None, metadata: dict[str, Any] | None = None) -> CommandRuntimeTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            task.status = "completed"
            task.updated_at = _now()
            if report_path is not None:
                task.report_path = str(report_path)
            if metadata:
                task.metadata.update(metadata)
            return task

    def fail_task(self, task_id: str, exc: BaseException | str, *, source: str | None = None, operation: str | None = None) -> CommandRuntimeTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            task.status = "failed"
            task.updated_at = _now()
            task.error = classify_error(exc, source=source, operation=operation).to_dict()
            return task

    def request_stop(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task._stop_event:
                task._stop_event.set()
            task.status = "stopping"
            task.updated_at = _now()
            return True

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def active_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                task.to_dict()
                for task in self._tasks.values()
                if task.status in ACTIVE_TASK_STATUSES
            ]

    def mark_timeouts(self) -> int:
        changed = 0
        with self._lock:
            for task in self._tasks.values():
                if task.status == "running" and task.timed_out:
                    task.status = "timeout"
                    task.updated_at = _now()
                    task.error = classify_error(TimeoutError("task timeout"), operation=task.task_type).to_dict()
                    changed += 1
        return changed

    def remove_task(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def cleanup_finished(self, *, older_than_seconds: float = 3600) -> int:
        cutoff = datetime.now().astimezone() - timedelta(seconds=older_than_seconds)
        removed = 0
        with self._lock:
            for task_id, task in list(self._tasks.items()):
                if task.status in {"completed", "failed", "cancelled"}:
                    try:
                        updated = datetime.fromisoformat(task.updated_at)
                    except Exception:
                        updated = cutoff - timedelta(seconds=1)
                    if updated < cutoff:
                        self._tasks.pop(task_id, None)
                        removed += 1
        return removed


def format_task_status(task: dict[str, Any] | None) -> str:
    if not task:
        return "任務不存在"
    parts = [
        f"{task.get('label') or task.get('task_id')}",
        f"狀態={task.get('status')}",
        f"耗時={float(task.get('elapsed_seconds') or 0):.1f}s",
    ]
    if task.get("progress_message"):
        parts.append(f"進度={task['progress_message']}")
    if task.get("error"):
        parts.append(f"錯誤={task['error'].get('error_type')}")
    return "；".join(parts)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


GLOBAL_COMMAND_RUNTIME = CommandRuntimeService()
