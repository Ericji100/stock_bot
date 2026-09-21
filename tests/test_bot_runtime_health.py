import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_ai_bot.monitoring.bot_runtime_health import (
    is_bot_heartbeat_stale,
    is_schedule_health_unhealthy,
    read_bot_heartbeat,
    record_scheduled_heartbeat_event,
    record_scheduled_status_heartbeat_event,
    update_schedule_health,
    write_bot_heartbeat,
)


class BotRuntimeHealthTests(unittest.TestCase):
    def test_write_heartbeat_creates_expected_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeat.json"
            now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)

            payload = write_bot_heartbeat(job_queue_available=True, path=path, now=now)

            self.assertEqual(set(payload), {
                "updated_at",
                "pid",
                "job_queue_available",
                "last_scheduled_event",
                "last_scheduled_event_at",
                "schedule_health",
            })
            self.assertEqual(payload["pid"], os.getpid())
            self.assertTrue(payload["job_queue_available"])
            self.assertIsNone(payload["last_scheduled_event"])
            self.assertIsNone(payload["last_scheduled_event_at"])
            self.assertEqual(payload["schedule_health"], {})
            self.assertEqual(read_bot_heartbeat(path), payload)

    def test_heartbeat_preserves_last_scheduled_event_on_regular_tick(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeat.json"
            event_time = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            tick_time = datetime(2026, 6, 30, 12, 1, tzinfo=timezone.utc)

            record_scheduled_heartbeat_event(
                "08:45 定時新聞整理",
                job_queue_available=True,
                path=path,
                now=event_time,
            )
            payload = write_bot_heartbeat(job_queue_available=True, path=path, now=tick_time)

            self.assertEqual(payload["last_scheduled_event"], "08:45 定時新聞整理")
            self.assertIsNotNone(payload["last_scheduled_event_at"])
            self.assertNotEqual(payload["updated_at"], payload["last_scheduled_event_at"])
            self.assertEqual(payload["schedule_health"]["last_triggered"]["label"], payload["last_scheduled_event"])

    def test_schedule_health_tracks_status_and_unhealthy_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeat.json"
            base = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            write_bot_heartbeat(job_queue_available=True, path=path, now=base)

            record_scheduled_status_heartbeat_event(
                "started",
                {
                    "timestamp": "2026-06-30T20:00:00+08:00",
                    "task_id": "scheduled:news:1800",
                    "label": "18:00 news",
                },
                path=path,
                now=base,
            )
            payload = update_schedule_health(
                job_queue_available=True,
                path=path,
                now=base,
                queue_size=2,
                recent_event_loop_lag_seconds=1.25,
            )

            self.assertEqual(payload["schedule_health"]["current_task"]["task_id"], "scheduled:news:1800")
            self.assertEqual(payload["schedule_health"]["queue_size"], 2)
            self.assertEqual(payload["schedule_health"]["recent_event_loop_lag_seconds"], 1.25)
            unhealthy, reason = is_schedule_health_unhealthy(
                payload,
                unhealthy_seconds=60,
                now=datetime(2026, 6, 30, 20, 2, tzinfo=timezone(timedelta(hours=8))),
            )
            self.assertTrue(unhealthy)
            self.assertIn("18:00 news", reason)

    def test_stale_detection_handles_fresh_stale_and_missing_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeat.json"
            base = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)

            self.assertTrue(is_bot_heartbeat_stale(path, now=base))

            write_bot_heartbeat(job_queue_available=True, path=path, now=base)

            self.assertFalse(
                is_bot_heartbeat_stale(path, stale_seconds=600, now=base + timedelta(minutes=9))
            )
            self.assertTrue(
                is_bot_heartbeat_stale(path, stale_seconds=600, now=base + timedelta(minutes=11))
            )


if __name__ == "__main__":
    unittest.main()
