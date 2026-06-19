from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta

from research_center.command_runtime_service import CommandRuntimeService, format_task_status


class CommandRuntimeServiceTests(unittest.TestCase):
    def test_tracks_progress_finish_and_status_text(self):
        runtime = CommandRuntimeService()
        runtime.start_task("t1", label="unit", task_type="test")
        runtime.update_progress("t1", "step 1")
        runtime.finish_task("t1", report_path="reports/unit.json")

        task = runtime.get_task("t1")
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["progress_message"], "step 1")
        self.assertIn("狀態=completed", format_task_status(task))

    def test_request_stop_sets_event_and_status(self):
        runtime = CommandRuntimeService()
        event = threading.Event()
        runtime.start_task("t2", label="stop", task_type="test", stop_event=event)

        self.assertTrue(runtime.request_stop("t2"))
        task = runtime.get_task("t2")
        self.assertTrue(event.is_set())
        self.assertEqual(task["status"], "stopping")
        self.assertTrue(task["stop_requested"])

    def test_fail_task_records_error_classification(self):
        runtime = CommandRuntimeService()
        runtime.start_task("t3", label="fail", task_type="test")
        runtime.fail_task("t3", RuntimeError("HTTP 429 quota exceeded"), source="finmind", operation="fetch")

        task = runtime.get_task("t3")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error"]["error_type"], "quota_exhausted")

    def test_try_start_task_locks_existing_active_task(self):
        runtime = CommandRuntimeService()
        first, started_first = runtime.try_start_task("t4", label="first", task_type="test")
        second, started_second = runtime.try_start_task("t4", label="second", task_type="test")

        self.assertTrue(started_first)
        self.assertFalse(started_second)
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(second.label, "first")
        self.assertTrue(runtime.is_task_active("t4"))

    def test_try_start_task_allows_restart_after_finish(self):
        runtime = CommandRuntimeService()
        runtime.try_start_task("t5", label="first", task_type="test")
        runtime.finish_task("t5")

        task, started = runtime.try_start_task("t5", label="second", task_type="test")

        self.assertTrue(started)
        self.assertEqual(task.label, "second")

    def test_mark_timeouts_marks_expired_running_tasks(self):
        runtime = CommandRuntimeService()
        task = runtime.start_task("t6", label="timeout", task_type="test", timeout_seconds=1)
        task.started_at = (datetime.now().astimezone() - timedelta(seconds=5)).isoformat(timespec="seconds")

        changed = runtime.mark_timeouts()
        snapshot = runtime.get_task("t6")

        self.assertEqual(changed, 1)
        self.assertEqual(snapshot["status"], "timeout")
        self.assertEqual(snapshot["error"]["error_type"], "ai_timeout")


if __name__ == "__main__":
    unittest.main()
