from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from progress_logger import format_cmd_message

from .command_runtime_service import GLOBAL_COMMAND_RUNTIME, CommandRuntimeService
from .resource_guard_service import DEFAULT_RESOURCE_GUARD, ResourceGuardService

SCHEDULED_TASK_SCHEMA_VERSION = "scheduled_task_v1"
ScheduledTaskRunner = Callable[[], Awaitable[None]]
ScheduledTaskFactory = Callable[[Awaitable[None]], asyncio.Task]
ScheduledTaskSink = Callable[[str], None]
ScheduledTaskAuditSink = Callable[[Any, str, dict[str, Any]], None]
TAIPEI_TZ = timezone(timedelta(hours=8))
SCHEDULED_AUDIT_DIR = Path("logs") / "scheduled_tasks"


@dataclass(frozen=True)
class ScheduledTaskSpec:
    task_id: str
    label: str
    task_type: str = "scheduled_task"
    schedule: str = ""
    queued: bool = True
    timeout_seconds: float | None = None
    allow_overlap: bool = False
    category: str = "定時任務"
    resource_group: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEDULED_TASK_SCHEMA_VERSION, **asdict(self)}


def _now_taipei(now: datetime | None = None) -> datetime:
    current = now or datetime.now(TAIPEI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    return current.astimezone(TAIPEI_TZ)


def write_scheduled_task_audit_event(
    spec: ScheduledTaskSpec,
    status: str,
    event: dict[str, Any] | None = None,
    *,
    audit_dir: Path | str = SCHEDULED_AUDIT_DIR,
    now: datetime | None = None,
) -> None:
    current = _now_taipei(now)
    payload: dict[str, Any] = {
        "schema_version": SCHEDULED_TASK_SCHEMA_VERSION,
        "timestamp": current.isoformat(timespec="seconds"),
        "status": status,
        "task_id": spec.task_id,
        "label": spec.label,
        "task_type": spec.task_type,
        "schedule": spec.schedule,
        "queued": spec.queued,
        "resource_group": spec.resource_group,
        "timeout_seconds": spec.timeout_seconds,
    }
    if event:
        payload.update(event)

    try:
        from stock_ai_bot.monitoring.bot_runtime_health import record_scheduled_status_heartbeat_event

        record_scheduled_status_heartbeat_event(status, payload)
    except Exception:
        pass

    audit_path = Path(audit_dir) / f"{current.date().isoformat()}.jsonl"
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


@dataclass(frozen=True)
class ScheduledJobRegistration:
    task_id: str
    label: str
    schedule: str
    queued: bool = True
    task_type: str = "scheduled_task"
    parameters: str = ""

    def to_spec(self, *, category: str = "定時任務") -> ScheduledTaskSpec:
        return ScheduledTaskSpec(
            task_id=self.task_id,
            label=self.label,
            task_type=self.task_type,
            schedule=self.schedule,
            queued=self.queued,
            category=category,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEDULED_TASK_SCHEMA_VERSION, **asdict(self)}


class ScheduledTaskService:
    """Shared queue/runtime wrapper for scheduled Telegram jobs."""

    def __init__(
        self,
        *,
        runtime: CommandRuntimeService | None = None,
        resource_guard: ResourceGuardService | None = None,
        sink: ScheduledTaskSink | None = None,
        audit_sink: ScheduledTaskAuditSink | None = None,
        category: str = "定時任務",
    ) -> None:
        self.runtime = runtime or GLOBAL_COMMAND_RUNTIME
        self.resource_guard = resource_guard or DEFAULT_RESOURCE_GUARD
        self.sink = sink or (lambda message: print(message, flush=True))
        self.audit_sink = audit_sink or write_scheduled_task_audit_event
        self.category = category
        self._queue: asyncio.Queue[tuple[ScheduledTaskSpec, ScheduledTaskRunner]] | None = None
        self._worker: asyncio.Task | None = None
        self._queued_task_ids: set[str] = set()

    @property
    def queue(self) -> asyncio.Queue[tuple[ScheduledTaskSpec, ScheduledTaskRunner]] | None:
        return self._queue

    @property
    def worker(self) -> asyncio.Task | None:
        return self._worker

    async def enqueue(
        self,
        spec: ScheduledTaskSpec,
        runner: ScheduledTaskRunner,
        *,
        create_task: ScheduledTaskFactory,
    ) -> str:
        if self._queue is None:
            self._queue = asyncio.Queue()
        if not spec.allow_overlap and (spec.task_id in self._queued_task_ids or self.runtime.is_task_active(spec.task_id)):
            self._audit(spec, "skipped_duplicate", queue_size=self._queue.qsize())
            self._log(f"{spec.label} 已在排隊或執行中，本次略過", spec.category)
            return "skipped_duplicate"

        ahead = self._queue.qsize()
        self._queued_task_ids.add(spec.task_id)
        await self._queue.put((spec, runner))
        self._audit(spec, "queued", queue_size=self._queue.qsize(), queued_ahead=ahead)
        if ahead:
            self._log(f"{spec.label} 已排隊，目前前方 {ahead} 個任務", spec.category)
        else:
            self._log(f"{spec.label} 已排入定時任務佇列", spec.category)
        if self._worker is None or self._worker.done():
            self._worker = create_task(self._worker_loop())
            self._worker.set_name("定時任務序列佇列")
        return "queued"

    async def start_background(
        self,
        spec: ScheduledTaskSpec,
        runner: ScheduledTaskRunner,
        *,
        create_task: ScheduledTaskFactory,
    ) -> tuple[str, asyncio.Task | None]:
        task, started = self.runtime.try_start_task(
            spec.task_id,
            label=spec.label,
            task_type=spec.task_type,
            timeout_seconds=spec.timeout_seconds,
            metadata={"schedule": spec.schedule, "queued": spec.queued},
        )
        if not started and not spec.allow_overlap:
            self._audit(spec, "skipped_duplicate")
            self._log(f"{spec.label} 已在執行中，本次略過", spec.category)
            return "skipped_duplicate", None

        async def wrapped() -> None:
            started_at = time.monotonic()
            try:
                self.runtime.update_progress(spec.task_id, "started")
                self._audit(spec, "started")
                self._log(f"{spec.label} 開始", spec.category)
                await self._run_with_timeout(spec, runner)
                self.runtime.finish_task(spec.task_id)
                self._audit(spec, "completed", elapsed_seconds=round(time.monotonic() - started_at, 3))
                self._log(f"{spec.label} 完成", spec.category)
            except asyncio.CancelledError:
                self.runtime.fail_task(spec.task_id, "cancelled", source=spec.task_type, operation=spec.task_id)
                self._audit(spec, "failed", error_type="cancelled")
                raise
            except asyncio.TimeoutError as exc:
                self.runtime.fail_task(spec.task_id, exc, source=spec.task_type, operation=spec.task_id)
                self._audit(
                    spec,
                    "timeout",
                    elapsed_seconds=round(time.monotonic() - started_at, 3),
                    error_summary=f"exceeded {spec.timeout_seconds} seconds",
                )
                self._log(f"{spec.label} timeout: exceeded {spec.timeout_seconds} seconds", spec.category)
            except Exception as exc:
                self.runtime.fail_task(spec.task_id, exc, source=spec.task_type, operation=spec.task_id)
                self._audit(
                    spec,
                    "failed",
                    elapsed_seconds=round(time.monotonic() - started_at, 3),
                    error_type=type(exc).__name__,
                    error_summary=str(exc)[:300],
                )
                self._log(f"{spec.label} 失敗：{exc}", spec.category)
                raise

        async_task = create_task(wrapped())
        async_task.set_name(spec.label)
        return "started", async_task

    async def _worker_loop(self) -> None:
        if self._queue is None:
            return
        while True:
            spec, runner = await self._queue.get()
            self._queued_task_ids.discard(spec.task_id)
            try:
                await self._run_one(spec, runner)
            finally:
                self._queue.task_done()

    async def _run_one(self, spec: ScheduledTaskSpec, runner: ScheduledTaskRunner) -> None:
        task, started = self.runtime.try_start_task(
            spec.task_id,
            label=spec.label,
            task_type=spec.task_type,
            timeout_seconds=spec.timeout_seconds,
            metadata={"schedule": spec.schedule, "queued": spec.queued},
        )
        if not started and not spec.allow_overlap:
            self._audit(spec, "skipped_duplicate")
            self._log(f"{spec.label} 已在執行中，本次略過", spec.category)
            return
        started_at = time.monotonic()
        try:
            self.runtime.update_progress(spec.task_id, "started")
            self._audit(spec, "started")
            self._log(f"{spec.label} 開始", spec.category)
            await self._run_with_timeout(spec, runner)
            self.runtime.finish_task(spec.task_id)
            self._audit(spec, "completed", elapsed_seconds=round(time.monotonic() - started_at, 3))
            self._log(f"{spec.label} 完成", spec.category)
        except asyncio.CancelledError:
            self.runtime.fail_task(spec.task_id, "cancelled", source=spec.task_type, operation=spec.task_id)
            self._audit(spec, "failed", error_type="cancelled")
            raise
        except asyncio.TimeoutError as exc:
            self.runtime.fail_task(spec.task_id, exc, source=spec.task_type, operation=spec.task_id)
            self._audit(
                spec,
                "timeout",
                elapsed_seconds=round(time.monotonic() - started_at, 3),
                error_summary=f"exceeded {spec.timeout_seconds} seconds",
            )
            self._log(f"{spec.label} timeout: exceeded {spec.timeout_seconds} seconds", spec.category)
        except Exception as exc:
            self.runtime.fail_task(spec.task_id, exc, source=spec.task_type, operation=spec.task_id)
            self._audit(
                spec,
                "failed",
                elapsed_seconds=round(time.monotonic() - started_at, 3),
                error_type=type(exc).__name__,
                error_summary=str(exc)[:300],
            )
            self._log(f"{spec.label} 失敗：{exc}", spec.category)

    async def _run_with_timeout(self, spec: ScheduledTaskSpec, runner: ScheduledTaskRunner) -> None:
        async def guarded() -> None:
            if spec.resource_group:
                async with self.resource_guard.acquire(spec.resource_group):
                    await runner()
            else:
                await runner()

        if spec.timeout_seconds and spec.timeout_seconds > 0:
            await asyncio.wait_for(guarded(), timeout=float(spec.timeout_seconds))
        else:
            await guarded()

    def _audit(self, spec: ScheduledTaskSpec, status: str, **event: Any) -> None:
        try:
            self.audit_sink(spec, status, {key: value for key, value in event.items() if value is not None})
        except Exception:
            return

    def _log(self, message: str, category: str | None = None) -> None:
        self.sink(format_cmd_message(message, category or self.category))


def format_registered_scheduled_jobs(registrations: list[ScheduledJobRegistration] | tuple[ScheduledJobRegistration, ...]) -> str:
    lines = ["已註冊定時任務："]
    for item in registrations:
        queue_text = "排隊執行" if item.queued else "背景執行"
        parameter_text = f"｜參數：{item.parameters}" if item.parameters else ""
        lines.append(f"- {item.schedule}｜{item.label}{parameter_text}｜{queue_text}")
    return "\n".join(lines)
