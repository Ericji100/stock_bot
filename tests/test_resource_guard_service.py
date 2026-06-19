from __future__ import annotations

import asyncio
import unittest

from research_center.resource_guard_service import ResourceGuardService


class ResourceGuardServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_pool_runs_one_at_a_time(self):
        guard = ResourceGuardService({"background_backfill": 1})
        events: list[str] = []
        release = asyncio.Event()
        second_started = asyncio.Event()

        async def first():
            async with guard.acquire("background_backfill"):
                events.append("first:start")
                await release.wait()
                events.append("first:end")

        async def second():
            async with guard.acquire("background_backfill"):
                events.append("second:start")
                second_started.set()

        task1 = asyncio.create_task(first())
        await asyncio.sleep(0)
        task2 = asyncio.create_task(second())
        await asyncio.sleep(0.02)

        self.assertEqual(events, ["first:start"])
        self.assertFalse(second_started.is_set())

        release.set()
        await asyncio.gather(task1, task2)
        self.assertEqual(events, ["first:start", "first:end", "second:start"])

    async def test_snapshot_reports_pool_usage(self):
        guard = ResourceGuardService({"background_backfill": 1})
        async with guard.acquire("background_backfill"):
            snapshot = guard.snapshot()

        pool = snapshot["pools"]["background_backfill"]
        self.assertEqual(snapshot["schema_version"], "resource_guard_v1")
        self.assertEqual(pool["active"], 1)
        self.assertEqual(pool["limit"], 1)
        self.assertEqual(pool["available"], 0)


if __name__ == "__main__":
    unittest.main()
