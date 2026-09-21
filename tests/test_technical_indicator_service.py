from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import stock_ai_bot.scanning.technical_scanner as technical_scanner

from stock_ai_bot.scanning.technical_indicator_service import (
    INDICATOR_VERSION,
    PRICE_BASIS_ADJUSTED,
    apply_point_in_time_adjustment,
    apply_technical_indicators,
)


def _frame(closes: list[float], *, adj_close: list[float] | None = None) -> pd.DataFrame:
    values = [float(value) for value in closes]
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=len(values), freq="D"),
            "open": values,
            "high": [value + 1.0 for value in values],
            "low": [value - 1.0 for value in values],
            "close": values,
            "volume": [1_000_000] * len(values),
        }
    )
    if adj_close is not None:
        frame["adj_close"] = adj_close
    return frame


class TechnicalIndicatorServiceTests(unittest.TestCase):
    def test_kd_uses_mitake_recurrence_instead_of_span_shortcut(self):
        closes = [100 + ((index * 7) % 19) - index * 0.03 for index in range(140)]
        frame = _frame(closes)

        actual = apply_technical_indicators(frame, adjust_prices=False)
        low_min = frame["low"].rolling(9).min()
        high_max = frame["high"].rolling(9).max()
        rsv = ((frame["close"] - low_min) / (high_max - low_min) * 100).clip(0, 100)
        expected_k = rsv.ewm(alpha=1 / 9, adjust=False).mean()
        expected_d = expected_k.ewm(alpha=1 / 55, adjust=False).mean()
        old_span_k = rsv.ewm(span=9, adjust=False).mean()

        self.assertAlmostEqual(actual["K"].iloc[-1], expected_k.iloc[-1], places=10)
        self.assertAlmostEqual(actual["D"].iloc[-1], expected_d.iloc[-1], places=10)
        self.assertGreater(abs(actual["K"].iloc[-1] - old_span_k.iloc[-1]), 0.01)

    def test_point_in_time_rebase_does_not_leak_future_corporate_action(self):
        historical = _frame([100.0, 102.0], adj_close=[50.0, 51.0])
        full = _frame([100.0, 102.0, 51.0], adj_close=[50.0, 51.0, 51.0])

        historical_adjusted = apply_point_in_time_adjustment(historical)
        full_adjusted = apply_point_in_time_adjustment(full)

        self.assertEqual(historical_adjusted["close"].tolist(), [100.0, 102.0])
        self.assertEqual(full_adjusted["close"].tolist(), [50.0, 51.0, 51.0])
        self.assertEqual(historical_adjusted["price_basis"].iloc[-1], PRICE_BASIS_ADJUSTED)
        self.assertEqual(historical_adjusted["indicator_version"].iloc[-1], INDICATOR_VERSION)

    def test_full_adjustment_removes_corporate_action_price_gap(self):
        frame = _frame([100.0, 102.0, 51.0], adj_close=[50.0, 51.0, 51.0])

        adjusted = apply_point_in_time_adjustment(frame)

        self.assertAlmostEqual(adjusted["close"].iloc[1], adjusted["close"].iloc[2])
        self.assertEqual(adjusted["source_close"].tolist(), [100.0, 102.0, 51.0])
        self.assertEqual(adjusted["adjustment_factor"].tolist(), [0.5, 0.5, 1.0])

    def test_latest_price_stays_on_source_quote_scale(self):
        frame = _frame([100.0, 102.0, 51.0, 53.0], adj_close=[50.0, 51.0, 51.0, 53.0])

        adjusted = apply_point_in_time_adjustment(frame)

        self.assertEqual(adjusted["close"].iloc[-1], 53.0)
        self.assertEqual(adjusted["adjustment_factor"].iloc[-1], 1.0)

    def test_macd_remains_21_55_55_on_adjusted_close(self):
        closes = [100 + index * 0.2 for index in range(180)]
        adj_close = [value * 0.8 if index < 120 else value for index, value in enumerate(closes)]
        frame = _frame(closes, adj_close=adj_close)

        actual = apply_technical_indicators(frame)
        expected_dif = actual["close"].ewm(span=21, adjust=False).mean() - actual["close"].ewm(
            span=55,
            adjust=False,
        ).mean()
        expected_dea = expected_dif.ewm(span=55, adjust=False).mean()

        self.assertAlmostEqual(actual["DIF"].iloc[-1], expected_dif.iloc[-1], places=10)
        self.assertAlmostEqual(actual["DEA"].iloc[-1], expected_dea.iloc[-1], places=10)
        self.assertAlmostEqual(actual["MACD_HIST"].iloc[-1], (expected_dif - expected_dea).iloc[-1], places=10)

    def test_flat_nine_day_range_keeps_kd_numeric(self):
        frame = _frame([50.0] * 30)
        frame["high"] = 50.0
        frame["low"] = 50.0

        actual = apply_technical_indicators(frame)

        self.assertTrue(pd.api.types.is_numeric_dtype(actual["RSV"]))
        self.assertTrue(pd.api.types.is_numeric_dtype(actual["K"]))
        self.assertTrue(pd.api.types.is_numeric_dtype(actual["D"]))
        self.assertTrue(actual["K"].isna().all())

    def test_technical_cache_records_indicator_version_and_price_basis(self):
        with TemporaryDirectory() as temp_dir, patch(
            "stock_ai_bot.scanning.technical_scanner.TECH_CACHE_DIR",
            Path(temp_dir),
        ):
            technical_scanner._save_history(
                "2330.TW",
                _frame([100.0, 101.0], adj_close=[100.0, 101.0]),
                source="unit",
            )

            metadata = technical_scanner._cache_meta_path("2330.TW").read_text(encoding="utf-8")

        self.assertIn(INDICATOR_VERSION, metadata)
        self.assertIn("source_ohlc_with_adj_close", metadata)


if __name__ == "__main__":
    unittest.main()
