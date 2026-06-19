from datetime import date, datetime
import unittest
from unittest.mock import patch

import pytz

import market_summary
from market_summary import QuoteSnapshot


YAHOO_FUTURES_HTML = """
<div id="main-1-FutureHeader-Proxy">
  <h1>台指期近二</h1>
  <span>WTX@</span>
  <span class="Fz(32px) C($c-trend-up)">47,792.00</span>
  <span class="Fz(20px) Mend(4px) C($c-trend-up)">
    <span></span>794.00
  </span>
  <span class="Fz(20px) C($c-trend-up)">(1.69%)</span>
  <span>收盤 | 2026/06/19 04:59 更新</span>
</div>
"""


class MarketSummaryTest(unittest.TestCase):
    def test_parse_yahoo_tx_futures_header(self):
        quote = market_summary.parse_yahoo_tx_futures_header(YAHOO_FUTURES_HTML)

        self.assertIsNotNone(quote)
        self.assertEqual(quote.label, "台指期近二")
        self.assertEqual(quote.close, 47792.0)
        self.assertEqual(quote.change, 794.0)
        self.assertEqual(quote.percent_change, 1.69)
        self.assertEqual(quote.quote_date, date(2026, 6, 19))
        self.assertEqual(quote.decimals, 0)

    def test_tx_night_prefers_yahoo_quote(self):
        yahoo_quote = QuoteSnapshot("台指期近二", 47792, 794, 1.69, date(2026, 6, 19), 0)

        with (
            patch("market_summary.fetch_latest_tx_quote_from_yahoo", return_value=yahoo_quote),
            patch("market_summary.fetch_latest_tx_night_session_quote_from_taifex") as taifex,
        ):
            quote = market_summary.fetch_latest_tx_night_session_quote(date(2026, 6, 19))

        self.assertEqual(quote, yahoo_quote)
        taifex.assert_not_called()

    def test_tx_night_falls_back_to_current_taifex_session(self):
        sessions = [
            QuoteSnapshot("day", 46998, 0, 0, date(2026, 6, 18), 0),
            QuoteSnapshot("night", 47792, 0, 0, date(2026, 6, 18), 0),
        ]

        with (
            patch("market_summary.fetch_latest_tx_quote_from_yahoo", return_value=None),
            patch("market_summary.load_tx_session_closes", return_value=sessions),
        ):
            quote = market_summary.fetch_latest_tx_night_session_quote(date(2026, 6, 19))

        self.assertEqual(quote.close, 47792)
        self.assertEqual(quote.change, 794)
        self.assertAlmostEqual(quote.percent_change, 1.6894, places=3)
        self.assertEqual(quote.quote_date, date(2026, 6, 18))

    def test_taifex_stale_night_session_is_not_used(self):
        sessions = [
            QuoteSnapshot("day", 45668, 0, 0, date(2026, 6, 17), 0),
            QuoteSnapshot("night", 46036, 0, 0, date(2026, 6, 17), 0),
        ]

        with (
            patch("market_summary.fetch_latest_tx_quote_from_yahoo", return_value=None),
            patch("market_summary.load_tx_session_closes", return_value=sessions),
        ):
            with self.assertRaises(market_summary.MarketSummaryError):
                market_summary.fetch_latest_tx_night_session_quote(date(2026, 6, 19))

    def test_morning_report_does_not_print_stale_taifex_price(self):
        us_quote = QuoteSnapshot("道瓊工業", 40000, 100, 0.25, date(2026, 6, 18), 2)
        sessions = [
            QuoteSnapshot("day", 45668, 0, 0, date(2026, 6, 17), 0),
            QuoteSnapshot("night", 46036, 0, 0, date(2026, 6, 17), 0),
        ]
        reference_time = pytz.timezone("Asia/Taipei").localize(datetime(2026, 6, 19, 7, 0))

        with (
            patch("market_summary.fetch_latest_yfinance_quote", return_value=us_quote),
            patch("market_summary.fetch_latest_tx_quote_from_yahoo", return_value=None),
            patch("market_summary.load_tx_session_closes", return_value=sessions),
        ):
            report = market_summary.build_morning_market_report(reference_time)

        self.assertIn("最新夜盤資料暫時無法取得", report)
        self.assertNotIn("46,036", report)


if __name__ == "__main__":
    unittest.main()
