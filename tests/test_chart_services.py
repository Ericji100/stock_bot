from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

import pandas as pd

import stock_chart_service
import technical_scanner
import tmf_chart_service


def _stock_bars(rows: int = 140) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=rows, freq="D")
    closes = [100 + index * 0.2 for index in range(rows)]
    return pd.DataFrame(
        {
            "date": dates.date,
            "datetime": dates,
            "time": [int(value.timestamp()) for value in dates],
            "open": [value - 0.5 for value in closes],
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": [1_000_000 + index * 1000 for index in range(rows)],
            "is_intraday": [False] * rows,
        }
    )


def _tmf_ticks(rows: int = 130) -> pd.DataFrame:
    start = pd.Timestamp("2026-06-17 15:00:00")
    times = pd.date_range(start, periods=rows, freq="5min")
    prices = [23000 + index for index in range(rows)]
    return pd.DataFrame(
        {
            "actual_datetime": times,
            "session_date": [pd.Timestamp("2026-06-17")] * rows,
            "session_type": ["night"] * rows,
            "price": prices,
            "volume": [1] * rows,
        }
    )


class ChartServiceSmokeTests(unittest.TestCase):
    def test_stock_chart_document_returns_non_empty_html(self):
        meta = stock_chart_service.StockChartMeta(
            code="2330",
            symbol="2330.TW",
            market="TWSE",
            name="台積電",
        )
        with (
            patch("stock_chart_service.resolve_stock_meta", return_value=meta),
            patch("stock_chart_service.load_chart_bars", return_value=_stock_bars()),
        ):
            buffer, filename, returned_meta = stock_chart_service.build_stock_chart_document(
                "2330",
                "2026-02-01",
                "2026-05-20",
                "1d",
            )

        html = buffer.getvalue().decode("utf-8")
        self.assertEqual(returned_meta, meta)
        self.assertIn("2330", filename)
        self.assertIn("台積電", html)
        self.assertGreater(len(html), 5000)

    def test_tmf_chart_report_returns_non_empty_html_file(self):
        with patch("tmf_chart_service.load_tmf_ticks", return_value=_tmf_ticks()):
            output_path = tmf_chart_service.build_tmf_chart_report(
                "2026-06-17",
                "2026-06-18",
                tmf_chart_service.SESSION_NIGHT,
                "5m",
            )

        html = output_path.read_text(encoding="utf-8")
        self.assertIn("TMF", html)
        self.assertGreater(len(html), 5000)


class TechnicalScannerProgressTests(unittest.TestCase):
    def test_strategy_progress_is_incremental_not_fixed_at_eighty(self):
        candidates = [
            technical_scanner.TechnicalCandidate("2330", "2330.TW", "TWSE", "台積電", "半導體業", 100.0, 1000, 1),
            technical_scanner.TechnicalCandidate("2317", "2317.TW", "TWSE", "鴻海", "其他電子業", 110.0, 1000, 1),
        ]
        progress_events: list[tuple[float, str]] = []

        def fake_progress(_label: str, progress: float, message: str) -> None:
            progress_events.append((progress, message))

        with (
            patch("technical_scanner.build_hard_filter_candidates", return_value=(candidates, 2)),
            patch("technical_scanner.fetch_daily_history", return_value=(_stock_bars(3), "unit")),
            patch("technical_scanner.detect_signals", return_value=([], [])),
            patch("technical_scanner.detect_technical_strategies", return_value=[]),
            patch("technical_scanner._print_progress", side_effect=fake_progress),
        ):
            technical_scanner.run_technical_scan(report_date=date(2026, 6, 18))

        strategy_progress = [progress for progress, message in progress_events if "偵測四大策略" in message]
        self.assertEqual(strategy_progress, [92.5, 95.0])
        self.assertNotIn(80.0, strategy_progress)


if __name__ == "__main__":
    unittest.main()
