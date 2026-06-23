from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta

from research_center.command_runtime_service import CommandRuntimeService
from research_center.resource_guard_service import ResourceGuardService
from research_center.scheduled_task_service import (
    ScheduledJobRegistration,
    ScheduledTaskService,
    ScheduledTaskSpec,
    format_registered_scheduled_jobs,
)


class ScheduledTaskServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        worker = getattr(self, "worker", None)
        if worker and not worker.done():
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

    async def test_enqueue_runs_tasks_sequentially_and_records_runtime(self):
        runtime = CommandRuntimeService()
        logs: list[str] = []
        events: list[str] = []
        service = ScheduledTaskService(runtime=runtime, sink=logs.append)

        async def first():
            events.append("first:start")
            await asyncio.sleep(0.01)
            events.append("first:end")

        async def second():
            events.append("second")

        await service.enqueue(ScheduledTaskSpec("job:first", "first"), first, create_task=asyncio.create_task)
        await service.enqueue(ScheduledTaskSpec("job:second", "second"), second, create_task=asyncio.create_task)
        self.worker = service.worker
        await service.queue.join()

        self.assertEqual(events, ["first:start", "first:end", "second"])
        self.assertEqual(runtime.get_task("job:first")["status"], "completed")
        self.assertEqual(runtime.get_task("job:second")["status"], "completed")
        self.assertTrue(any("[定時任務]" in line and "first 開始" in line for line in logs))

    async def test_enqueue_rejects_duplicate_active_or_queued_task(self):
        runtime = CommandRuntimeService()
        service = ScheduledTaskService(runtime=runtime, sink=lambda message: None)

        async def slow():
            await asyncio.sleep(0.01)

        status1 = await service.enqueue(ScheduledTaskSpec("job:same", "same"), slow, create_task=asyncio.create_task)
        status2 = await service.enqueue(ScheduledTaskSpec("job:same", "same"), slow, create_task=asyncio.create_task)
        self.worker = service.worker
        await service.queue.join()

        self.assertEqual(status1, "queued")
        self.assertEqual(status2, "skipped_duplicate")

    async def test_failure_is_classified_in_runtime(self):
        runtime = CommandRuntimeService()
        service = ScheduledTaskService(runtime=runtime, sink=lambda message: None)

        async def failing():
            raise RuntimeError("HTTP 429 quota exceeded")

        await service.enqueue(ScheduledTaskSpec("job:fail", "fail"), failing, create_task=asyncio.create_task)
        self.worker = service.worker
        await service.queue.join()

        task = runtime.get_task("job:fail")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error"]["error_type"], "quota_exhausted")

    async def test_timeout_can_be_marked_while_scheduled_task_is_running(self):
        runtime = CommandRuntimeService()
        service = ScheduledTaskService(runtime=runtime, sink=lambda message: None)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow():
            started.set()
            await release.wait()

        await service.enqueue(
            ScheduledTaskSpec("job:timeout", "timeout", timeout_seconds=0.001),
            slow,
            create_task=asyncio.create_task,
        )
        self.worker = service.worker
        await started.wait()
        runtime._tasks["job:timeout"].started_at = (datetime.now().astimezone() - timedelta(seconds=5)).isoformat(timespec="seconds")

        changed = runtime.mark_timeouts()
        task = runtime.get_task("job:timeout")
        release.set()
        await service.queue.join()

        self.assertIn(changed, (0, 1))
        self.assertEqual(task["status"], "timeout")
        self.assertEqual(task["error"]["error_type"], "ai_timeout")

    async def test_start_background_locks_duplicate_task(self):
        runtime = CommandRuntimeService()
        service = ScheduledTaskService(runtime=runtime, sink=lambda message: None)
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner():
            started.set()
            await release.wait()

        status1, task1 = await service.start_background(
            ScheduledTaskSpec("job:bg", "bg", queued=False),
            runner,
            create_task=asyncio.create_task,
        )
        status2, task2 = await service.start_background(
            ScheduledTaskSpec("job:bg", "bg", queued=False),
            runner,
            create_task=asyncio.create_task,
        )
        await started.wait()
        release.set()
        await task1

        self.assertEqual(status1, "started")
        self.assertEqual(status2, "skipped_duplicate")
        self.assertIsNone(task2)
        self.assertEqual(runtime.get_task("job:bg")["status"], "completed")

    async def test_start_background_respects_resource_group(self):
        runtime = CommandRuntimeService()
        guard = ResourceGuardService({"background_backfill": 1})
        service = ScheduledTaskService(runtime=runtime, resource_guard=guard, sink=lambda message: None)
        events: list[str] = []
        release = asyncio.Event()

        async def first():
            events.append("first:start")
            await release.wait()
            events.append("first:end")

        async def second():
            events.append("second:start")

        status1, task1 = await service.start_background(
            ScheduledTaskSpec("job:bg1", "bg1", queued=False, resource_group="background_backfill"),
            first,
            create_task=asyncio.create_task,
        )
        status2, task2 = await service.start_background(
            ScheduledTaskSpec("job:bg2", "bg2", queued=False, resource_group="background_backfill"),
            second,
            create_task=asyncio.create_task,
        )
        await asyncio.sleep(0.02)
        self.assertEqual(status1, "started")
        self.assertEqual(status2, "started")
        self.assertEqual(events, ["first:start"])

        release.set()
        await asyncio.gather(task1, task2)
        self.assertEqual(events, ["first:start", "first:end", "second:start"])

    async def test_queued_task_uses_resource_group(self):
        runtime = CommandRuntimeService()
        guard = ResourceGuardService({"background_news": 1})
        service = ScheduledTaskService(runtime=runtime, resource_guard=guard, sink=lambda message: None)
        snapshots: list[dict] = []

        async def runner():
            snapshots.append(guard.snapshot())

        await service.enqueue(
            ScheduledTaskSpec("job:news", "news", resource_group="background_news"),
            runner,
            create_task=asyncio.create_task,
        )
        self.worker = service.worker
        await service.queue.join()

        self.assertEqual(snapshots[0]["pools"]["background_news"]["active"], 1)
        self.assertEqual(guard.snapshot()["pools"]["background_news"]["active"], 0)

    def test_format_registered_scheduled_jobs(self):
        text = format_registered_scheduled_jobs(
            [
                ScheduledJobRegistration("scan", "監控掃描", "每日 12:30", parameters="source=monitor"),
                ScheduledJobRegistration("backfill", "完整資料回補檢查", "每 2 小時", queued=False, parameters="force_refresh=false"),
            ]
        )

        self.assertIn("已註冊定時任務", text)
        self.assertIn("每日 12:30｜監控掃描｜參數：source=monitor｜排隊執行", text)
        self.assertIn("每 2 小時｜完整資料回補檢查｜參數：force_refresh=false｜背景執行", text)


if __name__ == "__main__":
    unittest.main()
