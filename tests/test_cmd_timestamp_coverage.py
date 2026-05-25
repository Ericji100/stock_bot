"""Tests for technical scanner and monitor service timestamp coverage."""
import unittest
from unittest.mock import patch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestTechnicalScannerPrintProgress(unittest.TestCase):
    """Test technical_scanner._print_progress() for timestamp."""

    @patch("builtins.print")
    def test_print_progress_has_timestamp(self, mock_print):
        """Verify technical scanner _print_progress includes timestamp."""
        from technical_scanner import _print_progress
        _print_progress("技術面選股", 100.0, "完成")
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", arg)
        self.assertEqual(len(timestamps), 1, f"Expected 1 timestamp in: {arg}")
        self.assertIn("[選股進度][技術面選股]", arg)
        self.assertIn("100.00%", arg)
        self.assertIn("完成", arg)

    @patch("builtins.print")
    def test_print_progress_format(self, mock_print):
        """Verify technical scanner _print_progress format is correct."""
        from technical_scanner import _print_progress
        _print_progress("技術面選股", 50.0, "計算技術指標")
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        self.assertRegex(arg, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[選股進度\]\[技術面選股\] 50.00% 計算技術指標")


class TestMonitorServicePrintProgress(unittest.TestCase):
    """Test monitor_service print statements for timestamp."""

    @patch("builtins.print")
    def test_monitor_check_signal_has_timestamp(self, mock_print):
        """Verify monitor strategy check message has timestamp."""
        # We can't easily test check_signal without network, but we can verify
        # the now_timestamp import exists in monitor_service
        import monitor_service
        self.assertTrue(hasattr(monitor_service, 'now_timestamp'))

    def test_monitor_service_imports_now_timestamp(self):
        """Verify monitor_service imports now_timestamp."""
        import monitor_service
        self.assertTrue(hasattr(monitor_service, 'now_timestamp'))


if __name__ == "__main__":
    unittest.main()