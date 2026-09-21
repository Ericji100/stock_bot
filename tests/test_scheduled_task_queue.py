import asyncio
import time as time_module
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

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
        main._SCHEDULED_TASK_SERVICE._queue = None
        main._SCHEDULED_TASK_SERVICE._worker = None
        main._SCHEDULED_TASK_SERVICE._queued_task_ids.clear()
        self.heartbeat_patcher = patch.object(main, "record_scheduled_heartbeat_event")
        self.record_heartbeat = self.heartbeat_patcher.start()
        self.audit_patcher = patch.object(main, "write_scheduled_task_audit_event")
        self.audit_event = self.audit_patcher.start()

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
        main._SCHEDULED_TASK_SERVICE._queue = None
        main._SCHEDULED_TASK_SERVICE._worker = None
        main._SCHEDULED_TASK_SERVICE._queued_task_ids.clear()
        self.heartbeat_patcher.stop()
        self.audit_patcher.stop()

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

    async def test_enqueue_scheduled_task_records_heartbeat_event(self):
        context = _FakeContext()

        async def runner():
            return None

        await main.enqueue_scheduled_task(context, "heartbeat 測試任務", runner)
        await main._SCHEDULED_TASK_QUEUE.join()

        self.record_heartbeat.assert_any_call("heartbeat 測試任務", job_queue_available=False)
        self.audit_event.assert_any_call(ANY, "triggered")

    async def test_2030_all_scan_uses_all_scan_selection_on_trading_day(self):
        context = _FakeContext()
        target_date = date(2026, 5, 18)
        events: list[str] = []

        def fake_prepare(*args, **kwargs):
            events.append("prepare")
            return SimpleNamespace(ok=True, report_date=target_date, steps=["測試"], warnings=[], errors=[], counts={})

        async def fake_run_core(*args, **kwargs):
            events.append("scan")

        with patch.object(main, "load_config", return_value={"chat_id": 123, "scan_settings": {}}), \
            patch.object(main, "get_tw_today", return_value=target_date), \
            patch.object(main, "is_possible_trading_day", return_value=True), \
            patch.object(main, "safe_send_bot_message", new=AsyncMock()), \
            patch.object(main, "prepare_scheduled_all_scan_data", side_effect=fake_prepare) as prepare, \
            patch.object(main, "run_selected_scan_reports_core", new=AsyncMock(side_effect=fake_run_core)) as run_core:
            await main._scheduled_all_scan_push(context)

        self.assertEqual(events, ["prepare", "scan"])
        self.assertEqual(prepare.call_args.args[0], target_date)
        self.assertEqual(run_core.await_args.args[0], "7")
        self.assertEqual(run_core.await_args.args[1], target_date)
        self.assertIs(run_core.await_args.kwargs["historical_replay"], False)

    async def test_2030_all_scan_continues_when_prepare_fails(self):
        context = _FakeContext()
        target_date = date(2026, 5, 18)
        sent_messages: list[str] = []

        async def fake_send(_bot, _chat_id, text):
            sent_messages.append(text)

        with patch.object(main, "load_config", return_value={"chat_id": 123, "scan_settings": {}}), \
            patch.object(main, "get_tw_today", return_value=target_date), \
            patch.object(main, "is_possible_trading_day", return_value=True), \
            patch.object(main, "safe_send_bot_message", new=AsyncMock(side_effect=fake_send)), \
            patch.object(main, "prepare_scheduled_all_scan_data", side_effect=RuntimeError("cache timeout")), \
            patch.object(main, "run_selected_scan_reports_core", new=AsyncMock()) as run_core:
            await main._scheduled_all_scan_push(context)

        self.assertTrue(any("前置資料準備：發生例外" in message for message in sent_messages))
        run_core.assert_awaited_once()

    async def test_2030_all_scan_skips_non_trading_day(self):
        context = _FakeContext()

        with patch.object(main, "load_config", return_value={"chat_id": 123, "scan_settings": {}}), \
            patch.object(main, "get_tw_today", return_value=date(2026, 5, 17)), \
            patch.object(main, "is_possible_trading_day", return_value=False), \
            patch.object(main, "prepare_scheduled_all_scan_data") as prepare, \
            patch.object(main, "run_selected_scan_reports_core", new=AsyncMock()) as run_core:
            await main._scheduled_all_scan_push(context)

        prepare.assert_not_called()
        run_core.assert_not_awaited()

    async def test_technical_scan_core_sends_segmented_technical_messages(self):
        sent_messages: list[str] = []
        target_date = date(2026, 6, 30)

        async def send_text(text: str) -> None:
            sent_messages.append(text)

        fake_result = SimpleNamespace()
        with patch.object(main, "load_config", return_value={"scan_settings": {}}), \
            patch.object(main.ts, "run_technical_scan", return_value=fake_result) as run_scan, \
            patch.object(main.ts, "format_technical_report_messages", return_value=["技術分段 1", "技術分段 2"]), \
            patch.object(main.ts, "collect_technical_selected_codes", return_value=[]):
            await main.run_selected_scan_reports_core("6", target_date, send_text)

        run_scan.assert_called_once_with({}, target_date, historical_replay=False)
        self.assertIn("技術分段 1", sent_messages)
        self.assertIn("技術分段 2", sent_messages)
        self.assertNotIn("技術分段 1\n\n技術分段 2", sent_messages)

    async def test_laoxiao_scan_core_sends_messages_and_saves_selected_codes(self):
        sent_messages: list[str] = []
        target_date = date(2026, 6, 30)
        fake_result = SimpleNamespace(
            report_text="老蕭完整報告",
            report_messages=["老蕭分段 1", "老蕭分段 2"],
            selected_codes=["2330", "2317"],
            diagnostics={"scoring_version": "laoxiao_v1"},
        )

        async def send_text(text: str) -> None:
            sent_messages.append(text)

        with patch.object(main, "load_config", return_value={"scan_settings": {}}), \
            patch.object(main.laoxiao_scan_service, "build_laoxiao_scan_result", return_value=fake_result) as build_scan, \
            patch.object(main, "save_recent_scan_result") as save_recent:
            await main.run_selected_scan_reports_core("9", target_date, send_text)

        self.assertEqual(build_scan.call_args.args[:2], ({}, target_date))
        self.assertIn("老蕭分段 1", sent_messages)
        self.assertIn("老蕭分段 2", sent_messages)
        self.assertEqual(save_recent.call_args.args[3], ["2330", "2317"])

    async def test_1000_topic_maintain_uses_scheduled_queue(self):
        context = _FakeContext()

        with patch.object(main, "enqueue_scheduled_task", new=AsyncMock()) as enqueue:
            await main.scheduled_topic_maintain(context)

        enqueue.assert_awaited_once()
        self.assertIs(enqueue.await_args.args[0], context)
        self.assertIn("10:00 AI題材庫維護", enqueue.await_args.args[1])
        self.assertTrue(callable(enqueue.await_args.args[2]))

    async def test_1000_topic_maintain_runs_minimax_command(self):
        context = _FakeContext()
        fake_request = SimpleNamespace(command="topic_maintain", ai_model="minimax")
        fake_result = SimpleNamespace(summary="變更包已產生\nID：change_test")
        parse_command = MagicMock(return_value=fake_request)

        class FakeCenter:
            def run(self, request, progress):
                progress("[AI題材庫] /topic_maintain | 測試進度")
                self.request = request
                return fake_result

        center = FakeCenter()

        with patch.object(main, "load_config", return_value={"chat_id": 123}), \
            patch("research_center.command_parser.parse_command_text", parse_command), \
            patch("research_center.orchestrator.ResearchCenter", return_value=center), \
            patch.object(main, "safe_send_bot_message", new=AsyncMock()) as send:
            await main._scheduled_topic_maintain(context)

        parse_command.assert_called_once_with("/topic_maintain --deep --model minimax")
        self.assertIs(center.request, fake_request)
        send.assert_awaited_once_with(context.bot, 123, "變更包已產生\nID：change_test", reply_markup=None)

    async def test_1000_topic_maintain_does_not_send_result_repr(self):
        context = _FakeContext()
        fake_request = SimpleNamespace(command="topic_maintain", ai_model="minimax")

        class FakeResult:
            summary = "摘要文字"
            markdown = "markdown 文字"

            def __str__(self):
                return "ResearchCenterResult(status='success')"

        class FakeCenter:
            def run(self, request, progress):
                return FakeResult()

        with patch.object(main, "load_config", return_value={"chat_id": 123}), \
            patch("research_center.command_parser.parse_command_text", return_value=fake_request), \
            patch("research_center.orchestrator.ResearchCenter", return_value=FakeCenter()), \
            patch.object(main, "safe_send_bot_message", new=AsyncMock()) as send:
            await main._scheduled_topic_maintain(context)

        send.assert_awaited_once_with(context.bot, 123, "摘要文字", reply_markup=None)
        self.assertNotIn("ResearchCenterResult(", send.await_args.args[2])

    def test_main_registers_1000_topic_maintain_job(self):
        source = Path(main.__file__).read_text(encoding="utf-8")

        self.assertIn("scheduled_topic_maintain", source)
        self.assertIn("time(hour=10, minute=0, tzinfo=tw_tz)", source)
        self.assertIn('name="10:00 AI題材庫維護（MiniMax M3）"', source)

    def test_registered_scheduled_jobs_summary_covers_expected_jobs(self):
        from research_center.scheduled_task_service import format_registered_scheduled_jobs

        summary = format_registered_scheduled_jobs(main.SCHEDULED_JOB_REGISTRATIONS)

        for expected in (
            "12:30",
            "13:50",
            "17:45",
            "08:45",
            "18:00",
            "20:30",
            "21:30",
            "16:30",
            "18:30",
            "21:00",
            "每 2 小時",
            "10:00",
        ):
            self.assertIn(expected, summary)
        self.assertIn("排隊執行", summary)
        self.assertIn("背景執行", summary)
        self.assertIn("參數：", summary)
        self.assertIn("/topic_maintain --deep --model minimax", summary)
        self.assertIn("source=technical, ai_top=15, model=minimax", summary)
        self.assertIn("run_backfill_if_needed(report_date=None, force_refresh=false)", summary)

    def test_all_registered_scheduled_jobs_have_actual_parameters(self):
        missing = [item.label for item in main.SCHEDULED_JOB_REGISTRATIONS if not item.parameters]

        self.assertEqual(missing, [])

    def test_startup_message_uses_registered_scheduled_jobs_summary(self):
        source = Path(main.__file__).read_text(encoding="utf-8")

        self.assertIn("format_registered_scheduled_jobs(SCHEDULED_JOB_REGISTRATIONS)", source)
        self.assertNotIn("定時設定：12:30", source)

    def test_news_jobs_have_stable_names_for_task_ids(self):
        source = Path(main.__file__).read_text(encoding="utf-8")

        self.assertIn('name="08:45 定時新聞整理"', source)
        self.assertIn('name="18:00 定時新聞整理"', source)
        self.assertIn('ScheduledJobRegistration("scheduled:news:0845", "08:45 定時新聞整理"', source)
        self.assertIn('ScheduledJobRegistration("scheduled:news:1800", "18:00 定時新聞整理"', source)

    async def test_event_loop_block_with_grace_still_runs_job(self):
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except ModuleNotFoundError:
            self.skipTest("apscheduler is not importable in this test environment")

        fired = asyncio.Event()

        async def mark_fired() -> None:
            fired.set()

        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            mark_fired,
            "date",
            run_date=datetime.now().astimezone() + timedelta(seconds=0.08),
            misfire_grace_time=2,
        )
        scheduler.start()
        try:
            await asyncio.sleep(0.03)
            time_module.sleep(0.18)
            await asyncio.wait_for(fired.wait(), timeout=1.0)
        finally:
            scheduler.shutdown(wait=False)

    def test_run_daily_jobs_have_misfire_grace_time(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        run_daily_count = source.count("app.job_queue.run_daily(")
        run_daily_with_grace = 0
        start = 0
        while True:
            index = source.find("app.job_queue.run_daily(", start)
            if index == -1:
                break
            end = source.find("\n        )", index)
            self.assertNotEqual(end, -1)
            block = source[index:end]
            if "job_kwargs=SCHEDULED_JOB_KWARGS" in block:
                run_daily_with_grace += 1
            start = end + 1

        self.assertGreaterEqual(run_daily_count, 8)
        self.assertEqual(run_daily_count, run_daily_with_grace)
        self.assertIn("SCHEDULED_MISFIRE_GRACE_SECONDS = 30 * 60", source)

    def test_scheduled_jobs_map_to_background_resource_groups(self):
        self.assertEqual(main._scheduled_resource_group("scheduled:news:0845"), "background_news")
        self.assertEqual(main._scheduled_resource_group("scheduled:topic_maintain:1000"), "background_ai_maintenance")
        self.assertEqual(main._scheduled_resource_group("scheduled:radar:2130"), "background_ai_maintenance")
        self.assertEqual(main._scheduled_resource_group("scheduled:chip_cache:2100"), "background_backfill")
        self.assertEqual(main._scheduled_resource_group("scheduled:full_backfill_check"), "background_backfill")
        self.assertIsNone(main._scheduled_resource_group("scheduled:portfolio:1745"))

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
