import asyncio
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main


class _FakeApplication:
    def create_task(self, coro):
        return asyncio.create_task(coro)


class _FakeContext:
    def __init__(self, job_data=None):
        self.application = _FakeApplication()
        self.bot = SimpleNamespace()
        self.job = SimpleNamespace(data=job_data or {}, name=None)


class ScheduledTaskQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._SCHEDULED_TASK_QUEUE = None
        main._SCHEDULED_TASK_WORKER = None
        main._SCHEDULED_CHIP_BACKFILL_TASKS.clear()

    async def asyncTearDown(self):
        worker = main._SCHEDULED_TASK_WORKER
        if worker and not worker.done():
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
        for task in list(main._SCHEDULED_CHIP_BACKFILL_TASKS.values()):
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        main._SCHEDULED_TASK_QUEUE = None
        main._SCHEDULED_TASK_WORKER = None
        main._SCHEDULED_CHIP_BACKFILL_TASKS.clear()

    async def test_scheduled_tasks_run_sequentially(self):
        events = []
        context = _FakeContext()

        async def first():
            events.append("first:start")
            await asyncio.sleep(0.01)
            events.append("first:end")

        async def second():
            events.append("second")

        await main.enqueue_scheduled_task(context, "first", first)
        await main.enqueue_scheduled_task(context, "second", second)

        await main._SCHEDULED_TASK_QUEUE.join()

        self.assertEqual(events, ["first:start", "first:end", "second"])

    async def test_2030_all_scan_uses_all_scan_selection_on_trading_day(self):
        context = _FakeContext()
        target_date = date(2026, 5, 18)

        with patch.object(main, "load_config", return_value={"chat_id": 123, "scan_settings": {}}), \
            patch.object(main, "get_tw_today", return_value=target_date), \
            patch.object(main, "is_possible_trading_day", return_value=True), \
            patch.object(main, "safe_send_bot_message", new=AsyncMock()), \
            patch.object(main, "run_selected_scan_reports_core", new=AsyncMock()) as run_core:
            await main._scheduled_all_scan_push(context)

        self.assertEqual(run_core.await_args.args[0], "7")
        self.assertEqual(run_core.await_args.args[1], target_date)

    async def test_2030_all_scan_skips_non_trading_day(self):
        context = _FakeContext()

        with patch.object(main, "load_config", return_value={"chat_id": 123, "scan_settings": {}}), \
            patch.object(main, "get_tw_today", return_value=date(2026, 5, 17)), \
            patch.object(main, "is_possible_trading_day", return_value=False), \
            patch.object(main, "run_selected_scan_reports_core", new=AsyncMock()) as run_core:
            await main._scheduled_all_scan_push(context)

        run_core.assert_not_awaited()

    async def test_chip_backfill_runs_in_background_not_scheduled_queue(self):
        context = _FakeContext({"label": "籌碼快取測試", "full_backfill": False})
        seen = []

        async def fake_chip_backfill(job_data):
            seen.append(job_data["label"])

        with patch.object(main, "_scheduled_chip_cache_backfill", side_effect=fake_chip_backfill):
            await main.scheduled_chip_cache_backfill(context)
            task = main._SCHEDULED_CHIP_BACKFILL_TASKS["籌碼快取測試"]
            await task
            await asyncio.sleep(0)

        self.assertEqual(seen, ["籌碼快取測試"])
        self.assertIsNone(main._SCHEDULED_TASK_QUEUE)


if __name__ == "__main__":
    unittest.main()
