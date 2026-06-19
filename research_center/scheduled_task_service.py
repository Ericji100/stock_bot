from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from progress_logger import format_cmd_message

from .command_runtime_service import GLOBAL_COMMAND_RUNTIME, CommandRuntimeService
from .resource_guard_service import DEFAULT_RESOURCE_GUARD, ResourceGuardService

SCHEDULED_TASK_SCHEMA_VERSION = "scheduled_task_v1"
ScheduledTaskRunner = Callable[[], Awaitable[None]]
ScheduledTaskFactory = Callable[[Awaitable[None]], asyncio.Task]
ScheduledTaskSink = Callable[[str], None]


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


@dataclass(frozen=True)
class ScheduledJobRegistration:
    task_id: str
    label: str
    schedule: str
    queued: bool = True
    task_type: str = "scheduled_task"

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
        category: str = "定時任務",
    ) -> None:
        self.runtime = runtime or GLOBAL_COMMAND_RUNTIME
        self.resource_guard = resource_guard or DEFAULT_RESOURCE_GUARD
        self.sink = sink or (lambda message: print(message, flush=True))
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
            self._log(f"{spec.label} 已在排隊或執行中，本次略過", spec.category)
            return "skipped_duplicate"

        ahead = self._queue.qsize()
        self._queued_task_ids.add(spec.task_id)
        await self._queue.put((spec, runner))
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
            self._log(f"{spec.label} 已在執行中，本次略過", spec.category)
            return "skipped_duplicate", None

        async def wrapped() -> None:
            try:
                self.runtime.update_progress(spec.task_id, "started")
                self._log(f"{spec.label} 開始", spec.category)
                if spec.resource_group:
                    async with self.resource_guard.acquire(spec.resource_group):
                        await runner()
                else:
                    await runner()
                self.runtime.finish_task(spec.task_id)
                self._log(f"{spec.label} 完成", spec.category)
            except asyncio.CancelledError:
                self.runtime.fail_task(spec.task_id, "cancelled", source=spec.task_type, operation=spec.task_id)
                raise
            except Exception as exc:
                self.runtime.fail_task(spec.task_id, exc, source=spec.task_type, operation=spec.task_id)
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
            self._log(f"{spec.label} 已在執行中，本次略過", spec.category)
            return
        try:
            self.runtime.update_progress(spec.task_id, "started")
            self._log(f"{spec.label} 開始", spec.category)
            if spec.resource_group:
                async with self.resource_guard.acquire(spec.resource_group):
                    await runner()
            else:
                await runner()
            self.runtime.finish_task(spec.task_id)
            self._log(f"{spec.label} 完成", spec.category)
        except asyncio.CancelledError:
            self.runtime.fail_task(spec.task_id, "cancelled", source=spec.task_type, operation=spec.task_id)
            raise
        except Exception as exc:
            self.runtime.fail_task(spec.task_id, exc, source=spec.task_type, operation=spec.task_id)
            self._log(f"{spec.label} 失敗：{exc}", spec.category)

    def _log(self, message: str, category: str | None = None) -> None:
        self.sink(format_cmd_message(message, category or self.category))


def format_registered_scheduled_jobs(registrations: list[ScheduledJobRegistration] | tuple[ScheduledJobRegistration, ...]) -> str:
    lines = ["已註冊定時任務："]
    for item in registrations:
        queue_text = "排隊執行" if item.queued else "背景執行"
        lines.append(f"- {item.schedule}｜{item.label}｜{queue_text}")
    return "\n".join(lines)
