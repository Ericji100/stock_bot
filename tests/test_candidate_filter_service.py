import unittest

import chip_strategies
import stock_scanner

from candidate_filter_service import (
    DEFAULT_HARD_FILTER_SETTINGS,
    apply_basic_hard_filter,
    hard_filter_display_text,
    resolve_hard_filter_settings,
)


class CandidateFilterServiceTests(unittest.TestCase):
    def test_default_monthly_revenue_is_40_million(self):
        self.assertEqual(DEFAULT_HARD_FILTER_SETTINGS["min_monthly_revenue"], 40_000_000.0)
        self.assertEqual(stock_scanner.DEFAULT_SCAN_SETTINGS["min_monthly_revenue"], 40_000_000.0)
        self.assertEqual(chip_strategies.HARD_FILTERS["min_monthly_revenue"], 40_000_000.0)

    def test_resolve_settings_ignores_invalid_override(self):
        settings = resolve_hard_filter_settings({"min_price": "bad", "max_price": 90})

        self.assertEqual(settings["min_price"], 10.0)
        self.assertEqual(settings["max_price"], 90.0)

    def test_basic_hard_filter_boundary_is_inclusive(self):
        result = apply_basic_hard_filter(
            price=10,
            avg_volume_20d=500,
            latest_monthly_revenue=40_000_000,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.reasons, ())

    def test_basic_hard_filter_returns_failure_reasons(self):
        result = apply_basic_hard_filter(
            price=9,
            avg_volume_20d=499,
            latest_monthly_revenue=39_999_999,
        )

        self.assertFalse(result.passed)
        self.assertEqual(
            result.reasons,
            ("price_out_of_range", "avg_volume_20d_below_min", "monthly_revenue_below_min"),
        )

    def test_basic_hard_filter_reports_missing_data(self):
        result = apply_basic_hard_filter(
            price=None,
            avg_volume_20d=None,
            latest_monthly_revenue=None,
        )

        self.assertEqual(
            result.reasons,
            ("missing_price", "missing_avg_volume_20d", "missing_monthly_revenue"),
        )

    def test_revenue_can_be_optional_for_price_volume_pool(self):
        result = apply_basic_hard_filter(
            price=80,
            avg_volume_20d=500,
            latest_monthly_revenue=None,
            require_revenue=False,
        )

        self.assertTrue(result.passed)

    def test_display_text_contains_current_thresholds(self):
        text = hard_filter_display_text()

        self.assertIn("股價 10~80", text)
        self.assertIn("均量 >= 500", text)
        self.assertIn("月營收 >= 4000萬", text)


if __name__ == "__main__":
    unittest.main()
