import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from stock_ai_bot.monitoring.bot_runtime_health import write_bot_heartbeat
import tools.bot_watchdog as bot_watchdog
from tools.bot_watchdog import (
    acquire_watchdog_instance,
    append_watchdog_log,
    release_watchdog_instance,
    run_watchdog_once,
    stop_managed_processes,
)


class BotWatchdogTests(unittest.TestCase):
    def test_watchdog_does_not_restart_when_heartbeat_is_fresh_and_pid_alive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeat.json"
            launch = Path(tmpdir) / "啟動機器人_runner.bat"
            launch.write_text("@echo off\n", encoding="utf-8")
            now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            write_bot_heartbeat(job_queue_available=True, path=path, now=now)
            events: list[str] = []

            result = run_watchdog_once(
                heartbeat_path=path,
                stale_seconds=600,
                launch_script=launch,
                stop_bot=lambda: events.append("stop"),
                start_bot=lambda: events.append("start"),
                process_exists=lambda pid: True,
                now=now + timedelta(minutes=5),
            )

            self.assertTrue(result.startswith("healthy:"))
            self.assertIn("pid=", result)
            self.assertEqual(events, [])

    def test_watchdog_restarts_when_heartbeat_is_fresh_but_pid_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeat.json"
            launch = Path(tmpdir) / "啟動機器人_runner.bat"
            launch.write_text("@echo off\n", encoding="utf-8")
            now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            write_bot_heartbeat(job_queue_available=True, path=path, now=now)
            events: list[str] = []

            result = run_watchdog_once(
                heartbeat_path=path,
                stale_seconds=600,
                launch_script=launch,
                stop_bot=lambda: events.append("stop"),
                start_bot=lambda: events.append("start"),
                process_exists=lambda pid: False,
                now=now + timedelta(minutes=5),
            )

            self.assertIn("restarted: pid not running", result)
            self.assertIn("launched=啟動機器人_runner.bat", result)
            self.assertEqual(events, ["stop", "start"])

    def test_watchdog_restarts_when_heartbeat_is_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeat.json"
            launch = Path(tmpdir) / "啟動機器人_runner.bat"
            launch.write_text("@echo off\n", encoding="utf-8")
            now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            write_bot_heartbeat(job_queue_available=True, path=path, now=now)
            events: list[str] = []

            result = run_watchdog_once(
                heartbeat_path=path,
                stale_seconds=600,
                launch_script=launch,
                stop_bot=lambda: events.append("stop"),
                start_bot=lambda: events.append("start"),
                process_exists=lambda pid: True,
                now=now + timedelta(minutes=11),
            )

            self.assertIn("restarted: heartbeat stale", result)
            self.assertIn("launched=啟動機器人_runner.bat", result)
            self.assertEqual(events, ["stop", "start"])

    def test_watchdog_restarts_when_schedule_health_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeat.json"
            launch = Path(tmpdir) / "啟動機器人_runner.bat"
            launch.write_text("@echo off\n", encoding="utf-8")
            now = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            write_bot_heartbeat(
                job_queue_available=True,
                path=path,
                now=now,
                schedule_health_update={"schedule_unhealthy_reason": "queue stalled"},
            )
            events: list[str] = []

            result = run_watchdog_once(
                heartbeat_path=path,
                stale_seconds=600,
                launch_script=launch,
                stop_bot=lambda: events.append("stop"),
                start_bot=lambda: events.append("start"),
                process_exists=lambda pid: True,
                now=now + timedelta(minutes=5),
            )

            self.assertIn("restarted: schedule unhealthy: queue stalled", result)
            self.assertEqual(events, ["stop", "start"])

    def test_watchdog_restarts_when_heartbeat_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.json"
            launch = Path(tmpdir) / "啟動機器人_runner.bat"
            launch.write_text("@echo off\n", encoding="utf-8")
            events: list[str] = []

            result = run_watchdog_once(
                heartbeat_path=path,
                stale_seconds=600,
                launch_script=launch,
                stop_bot=lambda: events.append("stop"),
                start_bot=lambda: events.append("start"),
                process_exists=lambda pid: False,
            )

            self.assertIn("restarted: heartbeat missing", result)
            self.assertIn("launched=啟動機器人_runner.bat", result)
            self.assertEqual(events, ["stop", "start"])

    def test_watchdog_start_target_is_runner_not_total_entry(self):
        text = Path("啟動機器人.bat").read_text(encoding="utf-8")
        watchdog_text = Path("tools/bot_watchdog.py").read_text(encoding="utf-8")

        self.assertIn("--launch 啟動機器人_runner.bat", text)
        self.assertIn("pythonw.exe", text)
        self.assertIn("-WindowStyle Hidden", text)
        self.assertIn('default="啟動機器人_runner.bat"', watchdog_text)
        self.assertNotIn("--launch 啟動機器人.bat", text)

    def test_watchdog_runner_intermediary_uses_no_window_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            launch = Path(tmpdir) / "啟動機器人_runner.bat"
            launch.write_text("@echo off\n", encoding="utf-8")
            with mock.patch.object(bot_watchdog.subprocess, "Popen") as popen:
                bot_watchdog.default_start_bot(launch)

        self.assertEqual(
            popen.call_args.kwargs["creationflags"],
            bot_watchdog.WINDOWS_CREATE_NO_WINDOW,
        )

    def test_watchdog_pid_check_uses_no_window_flag(self):
        completed = SimpleNamespace(returncode=0)
        with mock.patch.object(bot_watchdog.subprocess, "run", return_value=completed) as run:
            self.assertTrue(bot_watchdog.default_process_exists(123))

        self.assertEqual(
            run.call_args.kwargs["creationflags"],
            bot_watchdog.WINDOWS_CREATE_NO_WINDOW,
        )

    def test_watchdog_pid_file_prevents_duplicate_instance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "bot_watchdog.pid"
            acquired, pid = acquire_watchdog_instance(
                pid_path,
                pid=111,
                process_exists=lambda candidate: candidate == 111,
            )
            duplicate_acquired, duplicate_pid = acquire_watchdog_instance(
                pid_path,
                pid=222,
                process_exists=lambda candidate: candidate == 111,
            )

            self.assertTrue(acquired)
            self.assertEqual(pid, 111)
            self.assertFalse(duplicate_acquired)
            self.assertEqual(duplicate_pid, 111)
            release_watchdog_instance(pid_path, pid=222)
            self.assertTrue(pid_path.exists())
            release_watchdog_instance(pid_path, pid=111)
            self.assertFalse(pid_path.exists())

    def test_watchdog_replaces_stale_pid_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "bot_watchdog.pid"
            pid_path.write_text("111", encoding="ascii")

            acquired, pid = acquire_watchdog_instance(
                pid_path,
                pid=222,
                process_exists=lambda candidate: False,
            )

            self.assertTrue(acquired)
            self.assertEqual(pid, 222)
            self.assertEqual(pid_path.read_text(encoding="ascii"), "222")

    def test_watchdog_log_is_written_and_rotated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "watchdog.log"
            now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

            append_watchdog_log("first", path=log_path, now=now, max_bytes=1, backup_count=2)
            append_watchdog_log("second", path=log_path, now=now, max_bytes=1, backup_count=2)

            self.assertIn("second", log_path.read_text(encoding="utf-8"))
            self.assertIn("first", Path(f"{log_path}.1").read_text(encoding="utf-8"))

    def test_stop_managed_processes_stops_runner_and_watchdog(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            heartbeat_path = Path(tmpdir) / "heartbeat.json"
            pid_path = Path(tmpdir) / "bot_watchdog.pid"
            heartbeat_path.write_text('{"pid": 123}', encoding="utf-8")
            pid_path.write_text("456", encoding="ascii")
            stopped_bot_pids: list[int | None] = []
            stopped_process_pids: list[int] = []

            result = stop_managed_processes(
                heartbeat_path=heartbeat_path,
                pid_path=pid_path,
                stop_bot=lambda pid: stopped_bot_pids.append(pid),
                stop_process=lambda pid: stopped_process_pids.append(pid),
            )

            self.assertEqual(stopped_bot_pids, [123])
            self.assertEqual(stopped_process_pids, [456])
            self.assertFalse(pid_path.exists())
            self.assertIn("bot_pid=123", result)
            self.assertIn("watchdog_pid=456", result)

    def test_stop_entry_uses_watchdog_stop_mode(self):
        text = Path("停止機器人.bat").read_text(encoding="utf-8")

        self.assertIn("tools\\bot_watchdog.py --stop", text)

    def test_windows_batch_entries_use_crlf_line_endings(self):
        for path in (
            Path("啟動機器人.bat"),
            Path("啟動機器人_watchdog.bat"),
            Path("停止機器人.bat"),
        ):
            data = path.read_bytes()
            self.assertIn(b"\r\n", data, path.name)
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""), path.name)

    def test_readme_documents_single_entry_runner_and_watchdog(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("`啟動機器人.bat` 是唯一日常入口", readme)
        self.assertIn("`啟動機器人_runner.bat` 是內部 runner", readme)
        self.assertIn("pid` 已不存在", readme)
        self.assertIn("重新啟動 `啟動機器人_runner.bat`", readme)
        self.assertIn("`停止機器人.bat`", readme)
        self.assertIn("`logs/watchdog/watchdog.log`", readme)


if __name__ == "__main__":
    unittest.main()
