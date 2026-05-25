from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import patch

from research_center import recent_scans


class _FakeParentPath:
    def mkdir(self, *args, **kwargs):
        return None


class _FakeRecentScanPath:
    parent = _FakeParentPath()

    def __init__(self):
        self.written_text = ""

    def exists(self):
        return False

    def write_text(self, text, *args, **kwargs):
        self.written_text = text
        return len(text)


class RecentScanCodeExtractionTests(unittest.TestCase):
    def test_extract_stock_codes_ignores_dates_and_keeps_stock_rows(self):
        report = "\n".join(
            [
                "🔍 今日選股掃描報告",
                "📅 日期：2026-05-25 19:55",
                "【半導體業】 2330 台積電 (900.0) | 6282 康舒 (80.0)",
                "【鋼鐵工業】 2026 測試鋼鐵 (41.0)",
                "掃描統計：1975 檔，8184 筆資料",
            ]
        )

        with patch.object(recent_scans, "_load_valid_stock_codes", return_value={"2026", "2330", "6282"}):
            codes = recent_scans.extract_stock_codes(report)

        self.assertEqual(codes, ["2330", "6282", "2026"])

    def test_extract_stock_codes_does_not_read_statistics_as_stock_rows(self):
        report = "掃描統計：1975 檔\n資料筆數：8184 筆\n📅 日期：2026-05-25"

        with patch.object(recent_scans, "_load_valid_stock_codes", return_value={"1975", "8184", "2026"}):
            codes = recent_scans.extract_stock_codes(report)

        self.assertEqual(codes, [])

    def test_save_recent_scan_result_filters_non_stock_numbers(self):
        fake_path = _FakeRecentScanPath()
        report = "📅 日期：2026-05-25\n【半導體業】 2330 台積電 (900.0)\n掃描統計：1975 檔"

        with patch.object(recent_scans, "_load_valid_stock_codes", return_value={"2330"}), patch.object(
            recent_scans, "RECENT_SCAN_PATH", fake_path
        ):
            record = recent_scans.save_recent_scan_result("全部執行", date(2026, 5, 25), report)

        self.assertEqual(record["codes"], ["2330"])
        self.assertEqual(record["candidate_count"], 1)
        saved = json.loads(fake_path.written_text)
        self.assertEqual(saved[0]["codes"], ["2330"])

    def test_selected_codes_are_validated_against_stock_list_when_available(self):
        fake_path = _FakeRecentScanPath()

        with patch.object(recent_scans, "_load_valid_stock_codes", return_value={"2330"}), patch.object(
            recent_scans, "RECENT_SCAN_PATH", fake_path
        ):
            record = recent_scans.save_recent_scan_result(
                "精選選股",
                date(2026, 5, 25),
                "精選選股報告",
                selected_codes=["2330", "2026"],
            )

        self.assertEqual(record["codes"], ["2330"])

    def test_load_recent_scan_results_sanitizes_existing_cached_codes(self):
        cached_payload = json.dumps(
            [
                {
                    "scan_id": "全部執行_20260525_201431",
                    "scan_type": "全部執行",
                    "report_date": "2026-05-25",
                    "candidate_count": 4,
                    "codes": ["2026", "1975", "8184", "2330"],
                    "selected_codes": ["2026", "1975", "8184", "2330"],
                    "summary": "📅 日期：2026-05-25\n【半導體業】 2330 台積電 (900.0)\n掃描統計：1975 檔",
                }
            ],
            ensure_ascii=False,
        )

        class FakeReadablePath:
            def exists(self):
                return True

            def read_text(self, *args, **kwargs):
                return cached_payload

        with patch.object(recent_scans, "_load_valid_stock_codes", return_value={"2330"}), patch.object(
            recent_scans, "RECENT_SCAN_PATH", FakeReadablePath()
        ):
            records = recent_scans.load_recent_scan_results()

        self.assertEqual(records[0]["codes"], ["2330"])
        self.assertEqual(records[0]["selected_codes"], ["2330"])
        self.assertEqual(records[0]["candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
