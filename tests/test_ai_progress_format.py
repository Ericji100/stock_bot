"""Tests for AI progress output format (avoiding double timestamps)."""
import unittest
from unittest.mock import patch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestPrintProgressFormat(unittest.TestCase):
    """Test _print_progress in telegram_handlers for double timestamp prevention."""

    @patch("builtins.print")
    def test_print_progress_without_timestamp_adds_full_format(self, mock_print):
        """When message has no timestamp, full format should be added."""
        from research_center.telegram_handlers import _print_progress
        raw_text = "/research 2330 --deep"
        message = "開始收集資料"

        _print_progress(raw_text, message)

        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        # Should have exactly one timestamp
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", arg)
        self.assertEqual(len(timestamps), 1, f"Expected 1 timestamp, got {len(timestamps)}: {arg}")
        # Should have [AI投研] and command
        self.assertIn("[AI投研]", arg)
        self.assertIn("/research 2330 --deep", arg)
        self.assertIn("開始收集資料", arg)

    @patch("builtins.print")
    def test_print_progress_with_timestamp_no_double(self, mock_print):
        """When message already has timestamp, should not add another."""
        from research_center.telegram_handlers import _print_progress
        raw_text = "/research 2330 --deep"
        # Message already has a timestamp at the start
        message = "[2026-05-21 10:00:00] 開始收集資料"

        _print_progress(raw_text, message)

        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        # Should have exactly one timestamp
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", arg)
        self.assertEqual(len(timestamps), 1, f"Expected 1 timestamp, got {len(timestamps)}: {arg}")
        # Should be the original message unchanged
        self.assertEqual(arg, message)

    @patch("builtins.print")
    def test_print_progress_empty_raw_text(self, mock_print):
        """When raw_text is empty, should use default command name."""
        from research_center.telegram_handlers import _print_progress
        raw_text = ""
        message = "進度訊息"

        _print_progress(raw_text, message)

        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        self.assertIn("[AI投研]", arg)
        self.assertIn("進度訊息", arg)


if __name__ == "__main__":
    unittest.main()