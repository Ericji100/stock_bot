from __future__ import annotations

from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

import pandas as pd

import monitor_service
from telegram_stock_formatting import STOCK_MARK_START


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def get(self, url, *args, **kwargs):
        if url == monitor_service.TWSE_NAME_API_URL:
            return _FakeResponse([{"公司代號": "2330", "公司簡稱": "台積電"}])
        if url == monitor_service.TPEX_NAME_API_URL:
            return _FakeResponse([{"SecuritiesCompanyCode": "5425", "CompanyAbbreviation": "台半"}])
        if url == monitor_service.TWSE_SECURITY_DAILY_API_URL:
            return _FakeResponse(
                [
                    {"Code": "00635U", "Name": "期元大S&P黃金"},
                    {"Code": "2330", "Name": "台積電"},
                ]
            )
        if url == monitor_service.TWSE_MIS_QUOTE_API_URL:
            return _FakeResponse({"msgArray": [{"c": "00635U", "n": "期元大S&P黃金"}]})
        raise AssertionError(f"unexpected URL: {url}")


class MonitorServiceSymbolTests(unittest.TestCase):
    def setUp(self):
        monitor_service.OFFICIAL_NAME_CACHE.clear()
        monitor_service.OFFICIAL_SYMBOL_CACHE.clear()
        monitor_service.OFFICIAL_NAME_CACHE_EXPIRES_AT = None

    def tearDown(self):
        monitor_service.OFFICIAL_NAME_CACHE.clear()
        monitor_service.OFFICIAL_SYMBOL_CACHE.clear()
        monitor_service.OFFICIAL_NAME_CACHE_EXPIRES_AT = None

    def _seed_name_cache(self):
        monitor_service.OFFICIAL_NAME_CACHE.update(
            {
                "2330.TW": "台積電",
                "2330": "台積電",
                "5425.TWO": "台半",
                "5425": "台半",
                "00635U.TW": "期元大S&P黃金",
                "00635U": "期元大S&P黃金",
            }
        )
        monitor_service.OFFICIAL_SYMBOL_CACHE.update(
            {
                "2330": "2330.TW",
                "5425": "5425.TWO",
                "00635U": "00635U.TW",
            }
        )
        monitor_service.OFFICIAL_NAME_CACHE_EXPIRES_AT = datetime.now() + timedelta(hours=1)

    @patch("monitor_service.httpx.Client", _FakeHttpClient)
    def test_fetch_official_stock_name_cache_includes_twse_etf(self):
        cache = monitor_service.fetch_official_stock_name_cache()

        self.assertEqual(cache["2330.TW"], "台積電")
        self.assertEqual(cache["5425.TWO"], "台半")
        self.assertEqual(cache["00635U.TW"], "期元大S&P黃金")
        self.assertEqual(monitor_service.OFFICIAL_SYMBOL_CACHE["00635U"], "00635U.TW")

    def test_add_monitor_stock_auto_names_twse_stock(self):
        self._seed_name_cache()
        config = {"monitor_stocks": []}

        changed, message = monitor_service.add_monitor_stock_to_config(config, ["2330"])

        self.assertTrue(changed)
        self.assertEqual(config["monitor_stocks"], [{"symbol": "2330.TW", "name": "台積電"}])
        self.assertIn("2330.TW (台積電)", message)
        self.assertIn(STOCK_MARK_START, message)

    def test_add_monitor_stock_auto_names_tpex_stock(self):
        self._seed_name_cache()
        config = {"monitor_stocks": []}

        changed, message = monitor_service.add_monitor_stock_to_config(config, ["5425"])

        self.assertTrue(changed)
        self.assertEqual(config["monitor_stocks"], [{"symbol": "5425.TWO", "name": "台半"}])
        self.assertIn("5425.TWO (台半)", message)

    def test_add_monitor_stock_auto_names_twse_etf(self):
        self._seed_name_cache()
        config = {"monitor_stocks": []}

        changed, message = monitor_service.add_monitor_stock_to_config(config, ["00635U"])

        self.assertTrue(changed)
        self.assertEqual(config["monitor_stocks"], [{"symbol": "00635U.TW", "name": "期元大S&P黃金"}])
        self.assertIn("00635U.TW (期元大S&P黃金)", message)

    def test_monitor_list_normalizes_bare_etf_and_shows_name(self):
        self._seed_name_cache()
        config = {"monitor_stocks": ["00635U"]}

        message = monitor_service.build_monitor_list_message(config)

        self.assertIn("00635U.TW (期元大S&P黃金)", message)

    def test_monitor_list_deduplicates_existing_bare_and_suffix_etf_entries(self):
        self._seed_name_cache()
        config = {"monitor_stocks": ["00635U", {"symbol": "00635U.TW", "name": "期元大S&P黃金"}]}

        stocks = monitor_service.get_monitor_stocks(config)
        message = monitor_service.build_monitor_list_message(config)

        self.assertEqual(stocks, [{"symbol": "00635U.TW", "name": "期元大S&P黃金"}])
        self.assertEqual(message.count("00635U.TW (期元大S&P黃金)"), 1)

    def test_add_monitor_stock_rejects_duplicate_etf_suffix_and_bare_code(self):
        self._seed_name_cache()
        config = {"monitor_stocks": [{"symbol": "00635U.TW", "name": "期元大S&P黃金"}]}

        changed, message = monitor_service.add_monitor_stock_to_config(config, ["00635U"])

        self.assertFalse(changed)
        self.assertEqual(len(config["monitor_stocks"]), 1)
        self.assertIn("00635U.TW (期元大S&P黃金)", message)

    def test_etf_symbol_falls_back_to_twse_mis_name_lookup(self):
        monitor_service.OFFICIAL_NAME_CACHE.update({"2330.TW": "台積電", "2330": "台積電"})
        monitor_service.OFFICIAL_SYMBOL_CACHE.update({"2330": "2330.TW"})
        monitor_service.OFFICIAL_NAME_CACHE_EXPIRES_AT = datetime.now() + timedelta(hours=1)

        with patch("monitor_service._fetch_twse_mis_stock_name", return_value="期元大S&P黃金"):
            self.assertEqual(monitor_service.get_canonical_stock_symbol("00635U"), "00635U.TW")
            self.assertEqual(monitor_service.get_official_stock_name("00635U.TW"), "期元大S&P黃金")


def _monitor_frame(length: int = 180, *, yesterday_close: float = 99.0, today_close: float = 103.0, today_low: float = 98.0) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=length, freq="D")
    frame = pd.DataFrame(
        {
            "Open": [100.0] * length,
            "High": [104.0] * length,
            "Low": [100.0] * length,
            "Close": [100.0] * length,
            "Volume": [1_000_000] * length,
        },
        index=index,
    )
    frame.iloc[-2, frame.columns.get_loc("Close")] = yesterday_close
    frame.iloc[-1, frame.columns.get_loc("Close")] = today_close
    frame.iloc[-1, frame.columns.get_loc("Low")] = today_low
    return frame


class MonitorServiceMaSignalTests(unittest.TestCase):
    def test_check_ma_breakout_signal_detects_today_breakout(self):
        stock = {"symbol": "2330.TW", "name": "台積電"}
        frame = _monitor_frame(yesterday_close=99.0, today_close=103.0, today_low=101.0)

        with patch("monitor_service._prepare_daily_frame", return_value=frame), patch(
            "monitor_service.get_current_market_price", return_value=(103.0, "unit")
        ):
            signal = monitor_service.check_ma_breakout_signal(stock, 5)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["label"], "突破 5MA")
        self.assertIn("突破", signal["trigger_type"])

    def test_check_ma_breakout_signal_detects_intraday_reclaim(self):
        stock = {"symbol": "2330.TW", "name": "台積電"}
        frame = _monitor_frame(yesterday_close=105.0, today_close=103.0, today_low=98.0)

        with patch("monitor_service._prepare_daily_frame", return_value=frame), patch(
            "monitor_service.get_current_market_price", return_value=(103.0, "unit")
        ):
            signal = monitor_service.check_ma_breakout_signal(stock, 13)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["label"], "跌破後收復 13MA")
        self.assertIn("跌破後收復", signal["trigger_type"])

    def test_check_ma_breakout_signal_prioritizes_breakout_when_both_triggers(self):
        stock = {"symbol": "2330.TW", "name": "台積電"}
        frame = _monitor_frame(yesterday_close=99.0, today_close=103.0, today_low=98.0)

        with patch("monitor_service._prepare_daily_frame", return_value=frame), patch(
            "monitor_service.get_current_market_price", return_value=(103.0, "unit")
        ):
            signal = monitor_service.check_ma_breakout_signal(stock, 21)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["label"], "突破 21MA")
        self.assertIn("突破", signal["trigger_type"])
        self.assertIn("跌破後收復", signal["trigger_type"])

    def test_collect_monitor_signals_merges_multiple_ma_signals_by_stock(self):
        frame = _monitor_frame(yesterday_close=99.0, today_close=103.0, today_low=98.0)

        with patch("monitor_service.get_monitor_stocks", return_value=[{"symbol": "2330.TW", "name": "台積電"}]), patch(
            "monitor_service._prepare_daily_frame", return_value=frame
        ), patch("monitor_service.get_current_market_price", return_value=(103.0, "unit")):
            blocks = monitor_service.collect_monitor_signals({"monitor_stocks": []})

        text = "\n\n".join(blocks)
        self.assertEqual(text.count("2330.TW (台積電)"), 1)
        self.assertIn(STOCK_MARK_START, text)
        self.assertIn("訊號：突破 5MA、突破 13MA、突破 21MA", text)
        self.assertIn("突破 144MA", text)
        self.assertIn("MA：MA5", text)
        self.assertIn("MA144", text)
        self.assertNotIn("觸發：突破 / 跌破後收復", text)
        self.assertNotIn("📂 突破 5MA", text)
        self.assertNotIn("True", text)
        self.assertNotIn("False", text)

    def test_monitor_signal_groups_support_reclaim_labels(self):
        def fake_check(stock, period):
            if period == 13:
                return {
                    "symbol": stock["symbol"],
                    "stock_display": "2330.TW (台積電)",
                    "period": 13,
                    "label": "跌破後收復 13MA",
                    "trigger_type": "跌破後收復",
                    "current_price": 103.0,
                    "ma_value": 100.0,
                    "stop_loss": 98.0,
                    "price_source": "unit",
                }
            return None

        with patch("monitor_service.get_monitor_stocks", return_value=[{"symbol": "2330.TW", "name": "台積電"}]), patch(
            "monitor_service.check_ma_breakout_signal", side_effect=fake_check
        ):
            groups = monitor_service.collect_monitor_signal_groups({"monitor_stocks": []})

        self.assertEqual(list(groups), ["跌破後收復 13MA"])
        blocks = monitor_service._format_monitor_signal_groups(groups)
        self.assertEqual(len(blocks), 1)
        self.assertIn("📂 跌破後收復 13MA", blocks[0])
        self.assertNotIn("觸發：跌破後收復", blocks[0])

    def test_build_monitor_scan_report_includes_merged_count_without_trigger_line(self):
        merged = [
            {
                "stock_display": "2330.TW (台積電)",
                "current_price": 103.0,
                "stop_loss": 98.0,
                "price_source": "unit",
                "signals": ["突破 13MA", "跌破後收復 21MA"],
                "ma_values": [
                    {"period": 13, "ma_value": 145.46, "trigger_type": "突破", "label": "突破 13MA"},
                    {"period": 21, "ma_value": 144.81, "trigger_type": "跌破後收復", "label": "跌破後收復 21MA"},
                ],
            }
        ]
        with patch("monitor_service.collect_monitor_signal_items", return_value=merged):
            text = monitor_service.build_monitor_scan_report({"monitor_stocks": []}, title="監控測試")

        self.assertIn("監控測試", text)
        self.assertIn("觸發：1 檔", text)
        self.assertIn("2330.TW (台積電)", text)
        self.assertIn(STOCK_MARK_START, text)
        self.assertIn("訊號：突破 13MA、跌破後收復 21MA", text)
        self.assertIn("MA：MA13 145.46、MA21 144.81", text)
        self.assertNotIn("觸發：突破", text)
        self.assertNotIn("trigger_type", text)
        self.assertNotIn("True", text)


if __name__ == "__main__":
    unittest.main()
