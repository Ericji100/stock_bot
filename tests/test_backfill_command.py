"""Tests for /backfill command argument parsing."""
from __future__ import annotations

import unittest
from datetime import date

from backfill_service import parse_backfill_args


class TestParseBackfillArgs(unittest.TestCase):
    def test_no_args_returns_today_and_no_force(self):
        report_date, force_refresh = parse_backfill_args([])
        self.assertIsNone(report_date)
        self.assertFalse(force_refresh)

    def test_date_only(self):
        report_date, force_refresh = parse_backfill_args(["2026-05-15"])
        self.assertEqual(report_date, date(2026, 5, 15))
        self.assertFalse(force_refresh)

    def test_force_only(self):
        report_date, force_refresh = parse_backfill_args(["force"])
        self.assertTrue(force_refresh)

    def test_date_and_force(self):
        report_date, force_refresh = parse_backfill_args(["2026-05-15", "force"])
        self.assertEqual(report_date, date(2026, 5, 15))
        self.assertTrue(force_refresh)

    def test_chinese_force_keyword(self):
        report_date, force_refresh = parse_backfill_args(["強制"])
        self.assertTrue(force_refresh)

    def test_chinese_force_keyword_2(self):
        report_date, force_refresh = parse_backfill_args(["強制刷新"])
        self.assertTrue(force_refresh)

    def test_today_keyword(self):
        report_date, force_refresh = parse_backfill_args(["today"])
        self.assertEqual(report_date, date.today())

    def test_chinese_today_keyword(self):
        report_date, force_refresh = parse_backfill_args(["今日"])
        self.assertEqual(report_date, date.today())

    def test_invalid_date_raises_error(self):
        with self.assertRaises(ValueError):
            parse_backfill_args(["abc"])

    def test_date_and_refresh_keyword(self):
        report_date, force_refresh = parse_backfill_args(["2026-01-10", "refresh"])
        self.assertEqual(report_date, date(2026, 1, 10))
        self.assertTrue(force_refresh)


if __name__ == "__main__":
    unittest.main()