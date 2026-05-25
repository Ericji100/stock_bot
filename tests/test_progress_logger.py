import unittest
from datetime import datetime
from unittest.mock import patch

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from progress_logger import (
    now_timestamp,
    has_leading_timestamp,
    format_progress_message,
    format_cmd_message,
    print_cmd,
    print_progress,
    format_duration,
    print_scan_progress,
    print_research_progress,
    print_backfill_progress,
    print_chip_progress,
)


class TestNowTimestamp(unittest.TestCase):
    def test_format_matches_expected_pattern(self):
        ts = now_timestamp()
        # Should be YYYY-MM-DD HH:MM:SS
        self.assertRegex(ts, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_timestamp_is_current(self):
        ts = now_timestamp()
        expected = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(ts, expected)


class TestFormatProgressMessage(unittest.TestCase):
    def test_basic_message(self):
        msg = format_progress_message("選股進度", "掃描開始")
        self.assertRegex(msg, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[選股進度\] \| 掃描開始")

    def test_with_task(self):
        msg = format_progress_message("AI投研", "完成", task="research")
        self.assertRegex(msg, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[AI投研\] research \| 完成")

    def test_with_percent(self):
        msg = format_progress_message("選股進度", "50%", percent=50.0)
        self.assertRegex(msg, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[選股進度\] 50% \| 50%")

    def test_with_task_and_percent(self):
        msg = format_progress_message("選股進度", "完成", task="scan", percent=100.0)
        self.assertRegex(msg, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[選股進度\] scan 100% \| 完成")


class TestFormatDuration(unittest.TestCase):
    def test_seconds_only(self):
        self.assertEqual(format_duration(30.5), "30.5s")

    def test_minutes_and_seconds(self):
        self.assertEqual(format_duration(150.0), "2m 30s")

    def test_hours_and_minutes(self):
        self.assertEqual(format_duration(3660.0), "1h 1m")

    def test_zero(self):
        self.assertEqual(format_duration(0.0), "0.0s")


class TestPrintProgress(unittest.TestCase):
    @patch("builtins.print")
    def test_print_progress_calls_print(self, mock_print):
        print_progress("選股進度", "測試")
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        self.assertIn("[選股進度]", arg)

    @patch("builtins.print")
    def test_print_progress_with_task(self, mock_print):
        print_progress("AI投研", "完成", task="research")
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        self.assertIn("[AI投研]", arg)
        self.assertIn("research", arg)

    @patch("builtins.print")
    def test_print_progress_with_percent(self, mock_print):
        print_progress("選股進度", "50%", percent=50.0)
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        self.assertIn("50%", arg)


class TestConvenienceFunctions(unittest.TestCase):
    @patch("builtins.print")
    def test_print_scan_progress(self, mock_print):
        print_scan_progress("掃描開始", task="scan", percent=25.0)
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        self.assertIn("[選股進度]", arg)

    @patch("builtins.print")
    def test_print_research_progress(self, mock_print):
        print_research_progress("研究完成", task="deep")
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        self.assertIn("[AI投研]", arg)

    @patch("builtins.print")
    def test_print_backfill_progress(self, mock_print):
        print_backfill_progress("回填開始", task="backfill")
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        self.assertIn("[回填進度]", arg)

    @patch("builtins.print")
    def test_print_chip_progress(self, mock_print):
        print_chip_progress("籌碼資料", 50.0, "50% 完成")
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        self.assertIn("[籌碼資料]", arg)


class TestHasLeadingTimestamp(unittest.TestCase):
    def test_has_leading_timestamp_true(self):
        ts = now_timestamp()
        self.assertTrue(has_leading_timestamp(f"[{ts}] 測試訊息"))

    def test_has_leading_timestamp_false_no_timestamp(self):
        self.assertFalse(has_leading_timestamp("[AI投研] 測試訊息"))

    def test_has_leading_timestamp_false_wrong_format(self):
        self.assertFalse(has_leading_timestamp("[2026/05/21 10:00:00] 測試"))

    def test_has_leading_timestamp_false_empty(self):
        self.assertFalse(has_leading_timestamp(""))


class TestFormatProgressMessageDoubleTimestamp(unittest.TestCase):
    def test_format_progress_message_no_double_timestamp(self):
        ts = now_timestamp()
        already_timestamped = f"[{ts}] 已有時間戳的訊息"
        result = format_progress_message("AI投研", already_timestamped, task="research")
        # Should return the original message unchanged, not double-wrapped
        self.assertEqual(result, already_timestamped)
        # Count timestamps - should be exactly 1
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result)
        self.assertEqual(len(timestamps), 1)

    def test_format_progress_message_adds_timestamp_when_missing(self):
        result = format_progress_message("AI投研", "普通訊息", task="research")
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result)
        self.assertEqual(len(timestamps), 1)

    def test_format_progress_message_preserves_original_when_notimestamp(self):
        msg = "開始收集資料"
        result = format_progress_message("AI投研", msg, task="research")
        self.assertIn(msg, result)
        # Should have timestamp at start
        import re
        self.assertRegex(result, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")


class TestFormatCmdMessage(unittest.TestCase):
    def test_format_cmd_message_no_category(self):
        result = format_cmd_message("測試訊息")
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result)
        self.assertEqual(len(timestamps), 1)
        self.assertIn("測試訊息", result)
        # Format should be [timestamp] message
        self.assertEqual(result, f"{timestamps[0]} 測試訊息")

    def test_format_cmd_message_with_category(self):
        result = format_cmd_message("測試訊息", "選股進度")
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result)
        self.assertEqual(len(timestamps), 1)
        self.assertIn("[選股進度]", result)
        self.assertIn("測試訊息", result)

    def test_format_cmd_message_already_has_timestamp_with_category(self):
        """當訊息已帶時間戳且有分類時，應重組為 [時間] [分類] 內容"""
        ts = now_timestamp()
        already_timestamped = f"[{ts}] 已有時間戳"
        result = format_cmd_message(already_timestamped, "選股進度")
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result)
        self.assertEqual(len(timestamps), 1)
        self.assertEqual(timestamps[0], f"[{ts}]")
        # Should be [timestamp] [category] content
        self.assertEqual(result, f"[{ts}] [選股進度] 已有時間戳")

    def test_format_cmd_message_with_timestamp_and_category(self):
        """當訊息已帶時間戳且有分類時，應重組為 [時間] [分類] 內容"""
        ts = now_timestamp()
        already_timestamped = f"[{ts}] 毛利率快取進度 1720/1762"
        result = format_cmd_message(already_timestamped, "完整回補")
        import re
        # 驗證格式為 [timestamp] [category] content
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result)
        self.assertEqual(len(timestamps), 1)
        self.assertEqual(timestamps[0], f"[{ts}]")
        # 不應出現 [分類] [時間] 格式
        self.assertNotRegex(result, r"\[完整回補\] \[")
        self.assertIn("[完整回補]", result)
        self.assertIn("毛利率快取進度 1720/1762", result)

    def test_format_cmd_message_with_timestamp_no_double_timestamp(self):
        """當訊息已帶時間戳且有分類時，不應出現雙時間戳"""
        ts = now_timestamp()
        already_timestamped = f"[{ts}] 訊息內容"
        result = format_cmd_message(already_timestamped, "定時回補檢查")
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result)
        self.assertEqual(len(timestamps), 1)

    def test_format_cmd_message_with_timestamp_no_category_unchanged(self):
        """當訊息已帶時間戳但無分類時，應原樣回傳"""
        ts = now_timestamp()
        already_timestamped = f"[{ts}] 已有時間戳的訊息"
        result = format_cmd_message(already_timestamped, None)
        self.assertEqual(result, already_timestamped)


class TestPrintCmd(unittest.TestCase):
    @patch("builtins.print")
    def test_print_cmd_no_category(self, mock_print):
        print_cmd("測試訊息")
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        import re
        timestamps = re.findall(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", arg)
        self.assertEqual(len(timestamps), 1)

    @patch("builtins.print")
    def test_print_cmd_with_category(self, mock_print):
        print_cmd("測試訊息", "選股進度")
        mock_print.assert_called_once()
        arg = mock_print.call_args[0][0]
        self.assertIn("[選股進度]", arg)

    @patch("builtins.print")
    def test_print_cmd_flush_true(self, mock_print):
        print_cmd("測試訊息")
        # Verify flush=True is passed
        _, kwargs = mock_print.call_args
        self.assertTrue(kwargs.get("flush", False))


if __name__ == "__main__":
    unittest.main()