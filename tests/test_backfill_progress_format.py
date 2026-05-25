"""Tests for backfill progress output format (avoiding double timestamps)."""
import unittest
from unittest.mock import patch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestBackfillProgressFormat(unittest.TestCase):
    """Test backfill progress callbacks in main.py for timestamp correctness."""

    @patch("builtins.print")
    def test_manual_backfill_progress_without_timestamp_adds_full_format(self, mock_print):
        """When message has no timestamp, full format should be added."""
        from progress_logger import format_cmd_message

        # Simulate the manual backfill progress callback
        def progress(message: str) -> None:
            print(format_cmd_message(message, "完整回補"), flush=True)

        message = "毛利率快取進度 1720/1762"
        progress(message)

        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        # Should have exactly one timestamp
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", arg)
        self.assertEqual(len(timestamps), 1, f"Expected 1 timestamp, got {len(timestamps)}: {arg}")
        # Should have [完整回補]
        self.assertIn("[完整回補]", arg)
        self.assertIn("毛利率快取進度 1720/1762", arg)
        # Should NOT have [分類] [時間] format
        self.assertNotRegex(arg, r"\[完整回補\] \[\d{4}")

    @patch("builtins.print")
    def test_manual_backfill_progress_with_timestamp_no_double(self, mock_print):
        """When message already has timestamp and category, should produce [時間] [分類] 內容."""
        from progress_logger import format_cmd_message

        def progress(message: str) -> None:
            print(format_cmd_message(message, "完整回補"), flush=True)

        message = "[2026-05-21 10:00:00] 毛利率快取進度 1720/1762"
        progress(message)

        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", arg)
        self.assertEqual(len(timestamps), 1, f"Expected 1 timestamp, got {len(timestamps)}: {arg}")
        # Should be [timestamp] [完整回補] content format
        self.assertEqual(timestamps[0], "[2026-05-21 10:00:00]")
        self.assertIn("[完整回補]", arg)
        # Should NOT have [完整回補] [timestamp]
        self.assertNotRegex(arg, r"\[完整回補\] \[2026")

    @patch("builtins.print")
    def test_manual_backfill_progress_with_timestamp_outputs_correct_format(self, mock_print):
        """Manual backfill with timestamp should output [時間] [完整回補] 訊息."""
        from progress_logger import format_cmd_message

        def progress(message: str) -> None:
            print(format_cmd_message(message, "完整回補"), flush=True)

        message = "[2026-05-21 10:00:00] 毛利率快取進度 1720/1762"
        progress(message)

        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        # Format must be [timestamp] [category] content, NOT [category] [timestamp] content
        import re
        # Check it doesn't have [完整回補] followed immediately by timestamp
        self.assertNotRegex(arg, r"\[完整回補\] \[\d{4}-\d{2}-\d{2}")
        # Should have exactly one timestamp
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", arg)
        self.assertEqual(len(timestamps), 1)
        self.assertIn("[完整回補]", arg)

    @patch("builtins.print")
    def test_scheduled_backfill_progress_without_timestamp_adds_full_format(self, mock_print):
        """When scheduled backfill message has no timestamp, full format should be added."""
        from progress_logger import format_cmd_message

        def progress(message: str) -> None:
            print(format_cmd_message(message, "定時回補檢查"), flush=True)

        message = "毛利率快取完成：0 檔更新"
        progress(message)

        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", arg)
        self.assertEqual(len(timestamps), 1, f"Expected 1 timestamp, got {len(timestamps)}: {arg}")
        self.assertIn("[定時回補檢查]", arg)
        self.assertIn("毛利率快取完成：0 檔更新", arg)
        # Should NOT have [分類] [時間] format
        self.assertNotRegex(arg, r"\[定時回補檢查\] \[\d{4}")

    @patch("builtins.print")
    def test_scheduled_backfill_progress_with_timestamp_no_double(self, mock_print):
        """When scheduled backfill message already has timestamp and category, should produce [時間] [分類] 內容."""
        from progress_logger import format_cmd_message

        def progress(message: str) -> None:
            print(format_cmd_message(message, "定時回補檢查"), flush=True)

        message = "[2026-05-21 12:00:01] 毛利率快取完成：0 檔更新"
        progress(message)

        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", arg)
        self.assertEqual(len(timestamps), 1, f"Expected 1 timestamp, got {len(timestamps)}: {arg}")
        # Should be [timestamp] [定時回補檢查] content format
        self.assertEqual(timestamps[0], "[2026-05-21 12:00:01]")
        self.assertIn("[定時回補檢查]", arg)
        # Should NOT have [定時回補檢查] [timestamp]
        self.assertNotRegex(arg, r"\[定時回補檢查\] \[2026")

    @patch("builtins.print")
    def test_scheduled_backfill_progress_with_timestamp_outputs_correct_format(self, mock_print):
        """Scheduled backfill with timestamp should output [時間] [定時回補檢查] 訊息."""
        from progress_logger import format_cmd_message

        def progress(message: str) -> None:
            print(format_cmd_message(message, "定時回補檢查"), flush=True)

        message = "[2026-05-21 12:00:01] 毛利率快取完成：0 檔更新"
        progress(message)

        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        # Format must be [timestamp] [category] content, NOT [category] [timestamp] content
        import re
        self.assertNotRegex(arg, r"\[定時回補檢查\] \[\d{4}-\d{2}-\d{2}")
        # Should have exactly one timestamp
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", arg)
        self.assertEqual(len(timestamps), 1)
        self.assertIn("[定時回補檢查]", arg)


if __name__ == "__main__":
    unittest.main()