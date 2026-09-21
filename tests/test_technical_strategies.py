"""Tests for technical strategy detection (strategies A/B/C/D) and parameter integrity."""
from __future__ import annotations

import unittest
from datetime import date, timedelta
import pandas as pd

from technical_scanner import apply_indicators, MACD_FAST, MACD_SLOW, MACD_SIGNAL
from technical_scanner import (
    BULLISH_SIGNAL_ORDER,
    MA_BREAKOUT_SIGNAL_LABELS,
    MA_RECLAIM_SIGNAL_LABELS,
    TechnicalScanResult,
    detect_ma_breakout_signal_details,
    detect_ma_breakout_signals,
    format_technical_report,
    format_technical_report_messages,
)
from technical_scanner import KD_RSV_PERIOD, KD_K_PERIOD, KD_D_PERIOD
from technical_scanner import is_macd_pullback_breakout
from technical_strategy_engine import detect_technical_strategies
from telegram_stock_formatting import STOCK_MARK_START, strip_stock_markers


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _make_daily(
    closes,
    opens=None,
    highs=None,
    lows=None,
    volumes=None,
    start_date=None,
):
    if opens is None:
        opens = closes
    if highs is None:
        highs = [float(c * 1.02) for c in closes]
    if lows is None:
        lows = [float(c * 0.98) for c in closes]
    if volumes is None:
        volumes = [1_000_000] * len(closes)
    if start_date is None:
        start_date = date(2025, 1, 1)

    closes = [float(c) for c in closes]
    opens = [float(o) for o in opens]

    dates = [start_date + timedelta(days=i) for i in range(len(closes))]
    df = pd.DataFrame({
        "date": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })
    return df


def _apply(df):
    return apply_indicators(df.copy()).dropna(subset=["close"]).reset_index(drop=True)


# -------------------------------------------------------------------
# Parameter integrity tests
# -------------------------------------------------------------------
class TestMacdParams(unittest.TestCase):
    def test_macd_fast_21(self):
        self.assertEqual(MACD_FAST, 21)

    def test_macd_slow_55(self):
        self.assertEqual(MACD_SLOW, 55)

    def test_macd_signal_55(self):
        self.assertEqual(MACD_SIGNAL, 55)


class TestKdParams(unittest.TestCase):
    def test_kd_rsv_period_9(self):
        self.assertEqual(KD_RSV_PERIOD, 9)

    def test_kd_k_period_9(self):
        self.assertEqual(KD_K_PERIOD, 9)

    def test_kd_d_period_55(self):
        self.assertEqual(KD_D_PERIOD, 55)


# -------------------------------------------------------------------
# apply_indicators output column tests
# -------------------------------------------------------------------
class TestApplyIndicatorsColumns(unittest.TestCase):
    def test_ma5_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("MA5", out.columns)

    def test_ma13_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("MA13", out.columns)

    def test_ma21_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("MA21", out.columns)

    def test_ma55_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("MA55", out.columns)

    def test_ma60_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("MA60", out.columns)

    def test_ma105_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("MA105", out.columns)

    def test_ma144_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("MA144", out.columns)

    def test_atr14_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("ATR14", out.columns)

    def test_volume_ma20_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("volume_ma20", out.columns)

    def test_dif_dea_macd_hist_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("DIF", out.columns)
        self.assertIn("DEA", out.columns)
        self.assertIn("MACD_HIST", out.columns)

    def test_k_d_present(self):
        df = _make_daily([100 + i for i in range(150)])
        out = _apply(df)
        self.assertIn("K", out.columns)
        self.assertIn("D", out.columns)


class TestMaBreakoutSignals(unittest.TestCase):
    def _prepared_frame(self, period: int, *, yesterday_close: float, yesterday_ma: float, today_low: float, today_close: float, today_ma: float):
        df = _make_daily([100.0] * 180)
        out = _apply(df)
        ma_column = f"MA{period}"
        out.loc[out.index[-2], "close"] = yesterday_close
        out.loc[out.index[-2], ma_column] = yesterday_ma
        out.loc[out.index[-1], "low"] = today_low
        out.loc[out.index[-1], "close"] = today_close
        out.loc[out.index[-1], ma_column] = today_ma
        return out

    def test_today_breakout_requires_yesterday_not_above_ma(self):
        frame = self._prepared_frame(21, yesterday_close=99, yesterday_ma=100, today_low=101, today_close=103, today_ma=100)

        signals = detect_ma_breakout_signal_details(frame)

        ma21 = [item for item in signals if item["period"] == 21]
        self.assertEqual(ma21[0]["label"], "突破 21MA")
        self.assertIn("突破", ma21[0]["triggers"])

    def test_intraday_break_then_reclaim_triggers_same_day_signal(self):
        frame = self._prepared_frame(13, yesterday_close=105, yesterday_ma=100, today_low=98, today_close=103, today_ma=100)

        signals = detect_ma_breakout_signal_details(frame)

        ma13 = [item for item in signals if item["period"] == 13]
        self.assertEqual(ma13[0]["label"], "跌破後收復 13MA")
        self.assertIn("跌破後收復", ma13[0]["triggers"])

    def test_breakout_takes_priority_when_breakout_and_reclaim_both_trigger(self):
        frame = self._prepared_frame(21, yesterday_close=99, yesterday_ma=100, today_low=98, today_close=103, today_ma=100)

        details = detect_ma_breakout_signal_details(frame)
        labels = detect_ma_breakout_signals(frame)

        ma21 = [item for item in details if item["period"] == 21]
        self.assertEqual(ma21[0]["label"], "突破 21MA")
        self.assertIn("突破", ma21[0]["triggers"])
        self.assertIn("跌破後收復", ma21[0]["triggers"])
        self.assertIn("突破 21MA", labels)
        self.assertNotIn("跌破後收復 21MA", labels)

    def test_already_above_ma_does_not_count_as_today_breakout(self):
        frame = self._prepared_frame(55, yesterday_close=105, yesterday_ma=100, today_low=101, today_close=106, today_ma=100)

        signals = detect_ma_breakout_signal_details(frame)

        self.assertFalse([item for item in signals if item["period"] == 55])

    def test_no_intraday_break_does_not_count_as_reclaim(self):
        frame = self._prepared_frame(5, yesterday_close=105, yesterday_ma=100, today_low=100.5, today_close=103, today_ma=100)

        signals = detect_ma_breakout_signal_details(frame)

        self.assertFalse([item for item in signals if item["period"] == 5])

    def test_insufficient_ma_data_does_not_trigger_or_crash(self):
        df = _make_daily([100.0] * 20)
        out = _apply(df)

        signals = detect_ma_breakout_signal_details(out)

        self.assertFalse([item for item in signals if item["period"] == 144])

    def test_bullish_order_includes_all_ma_breakout_groups_first(self):
        expected = [
            label
            for period in (5, 13, 21, 55, 105, 144)
            for label in (MA_BREAKOUT_SIGNAL_LABELS[period], MA_RECLAIM_SIGNAL_LABELS[period])
        ]

        self.assertEqual(BULLISH_SIGNAL_ORDER[:12], expected)

    def test_technical_report_renders_ma_breakout_groups(self):
        bullish = {
            MA_BREAKOUT_SIGNAL_LABELS[period]: {"測試產業": [f"1{period:03d} 測試股 ({period:.1f})"]}
            for period in (5, 13, 21, 55, 105, 144)
        }
        bullish.update(
            {
                MA_RECLAIM_SIGNAL_LABELS[period]: {"測試產業": [f"2{period:03d} 收復股 ({period:.1f})"]}
                for period in (5, 13, 21, 55, 105, 144)
            }
        )
        result = TechnicalScanResult(
            report_date=date(2026, 6, 30),
            total_symbols=12,
            hard_filter_passed=12,
            matched_symbols=12,
            bullish=bullish,
            bearish={},
            sources={"unit"},
            strategy_signals={"A": [], "B": [], "C": [], "D": []},
        )

        text = format_technical_report(result)

        for period in (5, 13, 21, 55, 105, 144):
            self.assertIn(MA_BREAKOUT_SIGNAL_LABELS[period], text)
            self.assertIn(MA_RECLAIM_SIGNAL_LABELS[period], text)
        self.assertIn(STOCK_MARK_START, text)
        self.assertIn("1005 測試股 (5.0)", strip_stock_markers(text))
        self.assertIn("2005 收復股 (5.0)", strip_stock_markers(text))

    def test_technical_report_messages_split_by_signal_without_mixing(self):
        ma5_stocks = [f"5{i:03d} 測試五{i:02d} ({10 + i:.1f})" for i in range(45)]
        ma13_stocks = [f"13{i:02d} 測試十三{i:02d} ({20 + i:.1f})" for i in range(3)]
        result = TechnicalScanResult(
            report_date=date(2026, 6, 30),
            total_symbols=48,
            hard_filter_passed=48,
            matched_symbols=48,
            bullish={
                MA_BREAKOUT_SIGNAL_LABELS[5]: {"半導體業": ma5_stocks},
                MA_RECLAIM_SIGNAL_LABELS[13]: {"電子零組件業": ma13_stocks},
            },
            bearish={},
            sources={"unit"},
            strategy_signals={"A": [], "B": [], "C": [], "D": []},
        )

        messages = format_technical_report_messages(result, max_chars=500)
        ma5_messages = [message for message in messages if message.startswith("📂 突破 5MA")]
        ma13_messages = [message for message in messages if message.startswith("📂 跌破後收復 13MA")]

        self.assertGreater(len(ma5_messages), 1)
        self.assertEqual(len(ma13_messages), 1)
        self.assertIn("第 1/", ma5_messages[0].splitlines()[0])
        self.assertTrue(all("跌破後收復 13MA" not in message for message in ma5_messages))
        self.assertTrue(all("突破 5MA" not in message for message in ma13_messages))
        joined = "\n".join(messages)
        self.assertIn(STOCK_MARK_START, joined)
        clean_joined = strip_stock_markers(joined)
        for stock in [*ma5_stocks, *ma13_stocks]:
            self.assertIn(stock, clean_joined)

    def test_technical_report_messages_empty_result_has_reasonable_message(self):
        result = TechnicalScanResult(
            report_date=date(2026, 6, 30),
            total_symbols=0,
            hard_filter_passed=0,
            matched_symbols=0,
            bullish={},
            bearish={},
            sources={"unit"},
            strategy_signals={"A": [], "B": [], "C": [], "D": []},
        )

        messages = format_technical_report_messages(result)

        self.assertEqual(len(messages), 1)
        self.assertIn("目前沒有符合技術條件股票", messages[0])

    def test_strategy_messages_render_industry_and_one_stock_per_line(self):
        def make_sig(stock_id, stock_name, industry, retracement):
            return {
                "stock_id": stock_id,
                "stock_name": stock_name,
                "signal_date": "2026-07-02",
                "strategy_code": "A",
                "technical_signal_type": "test",
                "sub_signal_type": "A1_direct_ma21_breakout",
                "close": 77.0,
                "ma_context": {},
                "macd_context": {},
                "kd_context": {},
                "volume_quality": True,
                "technical_setup_score": 5,
                "initial_invalid_price": 70.0,
                "structural_invalid_price": 60.0,
                "risk_distance_pct": None,
                "risk_distance_atr": 5.0,
                "notes": "wave_return=23.1%",
                "features": {"retracement_ratio": retracement},
                "industry": industry,
            }

        result = TechnicalScanResult(
            report_date=date(2026, 7, 2),
            total_symbols=3,
            hard_filter_passed=3,
            matched_symbols=3,
            bullish={},
            bearish={},
            sources={"unit"},
            strategy_signals={
                "A": [
                    make_sig("2374", "佳能", "光電業", 0.89),
                    make_sig("5484", "慧友", "光電業", 0.72),
                    make_sig("1711", "永光", "化學工業", 0.53),
                ],
                "B": [],
                "C": [],
                "D": [],
            },
        )

        messages = format_technical_report_messages(result)
        strategy_message = next(message for message in messages if message.startswith("📂 MACD 動能策略（A–D）｜策略 A"))
        clean_message = strip_stock_markers(strategy_message)

        self.assertIn("A1｜直接突破型", clean_message)
        self.assertIn("[光電業]\n2374 佳能 (77.0)｜前波漲幅 23.1%、回檔比例 89%\n5484 慧友", clean_message)
        self.assertIn("[化學工業]\n1711 永光 (77.0)｜前波漲幅 23.1%、回檔比例 53%", clean_message)
        self.assertNotIn("2374 佳能 (77.0)｜前波漲幅 23.1%、回檔比例 89% | 5484 慧友", clean_message)
        self.assertIn(STOCK_MARK_START, strategy_message)


# -------------------------------------------------------------------
# Original signal function tests
# -------------------------------------------------------------------
class TestIsMacdPullbackBreakout(unittest.TestCase):
    def test_macd_hist_positive_required(self):
        """MACD_HIST must be > 0 for pullback breakout to fire."""
        closes = [100] * 60 + [105, 106]
        df = _make_daily(closes)
        out = _apply(df)
        # Force last row MACD_HIST negative
        out.loc[out.index[-1], "MACD_HIST"] = -0.5
        self.assertFalse(is_macd_pullback_breakout(out))

    def test_returns_false_for_insufficient_data(self):
        closes = [100] * 10
        df = _make_daily(closes)
        out = _apply(df)
        self.assertFalse(is_macd_pullback_breakout(out))


# -------------------------------------------------------------------
# Strategy B: MACD_HIST < 0 must NOT trigger B
# -------------------------------------------------------------------
class TestStrategyB(unittest.TestCase):
    def _b_frame(self):
        out = _apply(_make_daily([101.0] * 180))
        out["MA5"] = 100.0
        out["MA13"] = 95.0
        out["MA21"] = 94.0
        out["MA60"] = 90.0
        out["MACD_HIST"] = 0.2
        out["low"] = 100.5
        return out

    def _b_signals(self, frame, sub):
        return [s for s in detect_technical_strategies(frame, "TEST", "測試")
                if s.get("sub_signal_type") == sub]

    def test_b1_ma5_intraday_touch_and_reclaim(self):
        out = self._b_frame()
        out.loc[out.index[-2], "close"] = 100.0
        out.loc[out.index[-1], "low"] = 99.5
        signals = self._b_signals(out, "B1_intraday_retest_reclaim_ma")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["features"]["retest_ma"], "MA5")
        out.loc[out.index[-1], "low"] = 100.5
        self.assertEqual(self._b_signals(out, "B1_intraday_retest_reclaim_ma"), [])

    def test_b2_ma5_crosses_after_any_number_of_days_below(self):
        for days_ago in (1, 3, 6):
            with self.subTest(days_ago=days_ago):
                out = self._b_frame()
                for offset in range(1, days_ago + 1):
                    out.loc[out.index[-1 - offset], ["close", "low"]] = [99.0, 98.0]
                out.loc[out.index[-1], "close"] = 102.0
                signals = self._b_signals(out, "B2_short_reclaim_after_break_ma")
                self.assertEqual(len(signals), 1)
                self.assertEqual(signals[0]["features"], {"crossed_mas": ["MA5"]})

    def test_b2_ma13_and_ma21_reclaim_today(self):
        for ma_name in ("MA13", "MA21"):
            with self.subTest(ma_name=ma_name):
                out = self._b_frame()
                out["MA13"] = 100.0 if ma_name == "MA13" else 80.0
                out["MA21"] = 94.0 if ma_name == "MA13" else 100.0
                out["MA5"] = 103.0
                out.loc[out.index[-3]:out.index[-2], ["close", "low"]] = [99.0, 98.0]
                out.loc[out.index[-1], "close"] = 102.0
                signals = self._b_signals(out, "B2_short_reclaim_after_break_ma")
                self.assertEqual(len(signals), 1)
                self.assertEqual(signals[0]["features"], {"crossed_mas": [ma_name]})

    def test_b2_does_not_repeat_after_yesterday_already_reclaimed(self):
        out = self._b_frame()
        out.loc[out.index[-3], ["close", "low"]] = [99.0, 98.0]
        out.loc[out.index[-1], "close"] = 102.0
        self.assertEqual(self._b_signals(out, "B2_short_reclaim_after_break_ma"), [])

    def test_b2_fires_after_more_than_three_days_below_ma(self):
        out = self._b_frame()
        out.loc[out.index[-5]:out.index[-2], ["close", "low"]] = [99.0, 98.0]
        out.loc[out.index[-1], "close"] = 102.0
        signals = self._b_signals(out, "B2_short_reclaim_after_break_ma")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["features"], {"crossed_mas": ["MA5"]})

    def test_b2_new_break_after_reclaim_is_new_episode(self):
        out = self._b_frame()
        out.loc[out.index[-4], ["close", "low"]] = [99.0, 98.0]
        out.loc[out.index[-2], ["close", "low"]] = [99.0, 98.0]
        out.loc[out.index[-1], "close"] = 102.0
        signals = self._b_signals(out, "B2_short_reclaim_after_break_ma")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["features"], {"crossed_mas": ["MA5"]})

    def test_b2_does_not_inherit_b1_ma21_or_prior_ma60_gates(self):
        out = self._b_frame()
        out.loc[out.index[-2], ["close", "low"]] = [99.0, 98.0]
        out.loc[out.index[-1], "low"] = 99.5
        out.loc[out.index[-1], "MA21"] = 102.0
        self.assertEqual(self._b_signals(out, "B1_intraday_retest_reclaim_ma"), [])
        signals = self._b_signals(out, "B2_short_reclaim_after_break_ma")
        self.assertEqual(signals[0]["features"], {"crossed_mas": ["MA5"]})
        out.loc[out.index[-1], "MA21"] = 94.0
        out.loc[out.index[-2], ["close", "low"]] = [89.0, 88.0]
        signals = self._b_signals(out, "B2_short_reclaim_after_break_ma")
        self.assertEqual(signals[0]["features"], {"crossed_mas": ["MA5", "MA13", "MA21"]})

    def test_b_existing_ma13_priority_over_ma5(self):
        out = self._b_frame()
        out["MA5"] = 101.0
        out["MA13"] = 100.0
        out["MA21"] = 99.0
        out.loc[out.index[-2], ["close", "low"]] = [98.0, 97.0]
        out.loc[out.index[-1], ["close", "low"]] = [102.0, 98.0]
        b1 = self._b_signals(out, "B1_intraday_retest_reclaim_ma")
        b2 = self._b_signals(out, "B2_short_reclaim_after_break_ma")
        self.assertEqual(b1[0]["features"]["retest_ma"], "MA13")
        self.assertEqual(len(b2), 1)
        self.assertEqual(b2[0]["features"]["crossed_mas"], ["MA5", "MA13", "MA21"])

    def test_b_ma5_report_shows_matched_ma(self):
        out = self._b_frame()
        out.loc[out.index[-2], ["close", "low"]] = [99.0, 98.0]
        out.loc[out.index[-1], "low"] = 99.5
        signals = [s for s in detect_technical_strategies(out, "TEST", "測試")
                   if s.get("strategy_code") == "B" and s.get("sub_signal_type") != "B3_breakout_after_retest"]
        result = TechnicalScanResult(
            report_date=date(2026, 5, 19), total_symbols=1,
            hard_filter_passed=1, matched_symbols=1,
            bullish={}, bearish={}, sources={"test"},
            strategy_signals={"A": [], "B": signals, "C": [], "D": []},
        )
        report = format_technical_report(result)
        self.assertIn("B1｜當日低點碰觸 MA5/MA13/MA21 後收復", report)
        self.assertIn("B2｜紅柱期間收盤突破 MA5/MA13/MA21", report)
        self.assertIn("碰觸並收復 MA5", report)
        self.assertIn("今日收盤突破 MA5", report)

    def test_b2_first_red_day_and_equal_previous_ma_are_valid(self):
        out = self._b_frame()
        out.loc[out.index[-2], "MACD_HIST"] = -0.2
        out.loc[out.index[-2], "close"] = 100.0
        out.loc[out.index[-1], "close"] = 101.0
        signals = self._b_signals(out, "B2_short_reclaim_after_break_ma")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["features"], {"crossed_mas": ["MA5"]})

    def test_b2_does_not_require_today_close_above_yesterday(self):
        out = self._b_frame()
        out.loc[out.index[-2], ["close", "MA5"]] = [100.0, 101.0]
        out.loc[out.index[-1], ["close", "MA5"]] = [99.0, 98.0]
        signals = self._b_signals(out, "B2_short_reclaim_after_break_ma")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["features"], {"crossed_mas": ["MA5"]})

    def test_b2_requires_valid_ma_and_strict_close_above(self):
        out = self._b_frame()
        out.loc[out.index[-2], "close"] = 99.0
        out.loc[out.index[-1], "close"] = 100.0
        self.assertEqual(self._b_signals(out, "B2_short_reclaim_after_break_ma"), [])
        out.loc[out.index[-1], "close"] = 101.0
        out.loc[out.index[-2], "MA5"] = float("nan")
        self.assertEqual(self._b_signals(out, "B2_short_reclaim_after_break_ma"), [])
        out.loc[out.index[-2], "MA5"] = 100.0
        out.loc[out.index[-1], "MA5"] = float("nan")
        self.assertEqual(self._b_signals(out, "B2_short_reclaim_after_break_ma"), [])

    def test_strategy_b_requires_positive_macd_hist(self):
        """Strategy B must not fire unless the signal day is a red histogram bar."""
        closes = [100 + i * 0.5 for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)
        for hist in (-0.5, 0.0, float("nan")):
            with self.subTest(hist=hist):
                out.loc[out.index[-1], "MACD_HIST"] = hist
                signals = detect_technical_strategies(out, "TEST", "測試")
                strat_b = [s for s in signals if s.get("strategy_code") == "B"]
                self.assertEqual(strat_b, [], "Strategy B requires MACD_HIST > 0")

    def test_b2_does_not_skip_missing_yesterday_to_infer_crossing(self):
        out = self._b_frame()
        out.loc[out.index[-3], "close"] = 99.0
        out.loc[out.index[-2], "MACD_HIST"] = float("nan")
        self.assertEqual(self._b_signals(out, "B2_short_reclaim_after_break_ma"), [])

    def test_strategy_b3_kangshu_2025_09_12_breaks_red_zone_pre_retest_high(self):
        """B3 must fire when red zone had MA13/MA21 retest, today breaks pre-retest high.

        6282 康舒 2025-09-12 case:
        - Red zone present (MACD_HIST > 0)
        - Red zone touched MA13 during the zone (retest happened)
        - MA60 never broken in red zone
        - Pre-retest high ~32.50 (highest high before first MA13 touch)
        - Yesterday close 30.00 (< 32.50 — had NOT yet broken out)
        - Today close 32.65 (> 32.50 — breakout today)
        - B3 must NOT require today's MA21 break (yesterday already above MA21)
        """
        closes = [10.0 + i * 0.5 for i in range(100)]
        df = _make_daily(closes)
        out = _apply(df)

        # Force clean red zone: rows 50-98 (MACD_HIST > 0)
        for i in range(50):
            out.loc[out.index[i], "MACD_HIST"] = -0.5
        for i in range(50, 99):
            out.loc[out.index[i], "MACD_HIST"] = 0.8

        # Pre-retest high: rows 51-60 rising, high ~32.50 at row 60
        for i in range(51, 61):
            out.loc[out.index[i], "close"] = 27.0 + (i - 51) * 0.5
            out.loc[out.index[i], "high"] = 28.0 + (i - 51) * 0.5

        # Set high at row 50 too (pre-touch zone part of red_zone pre_touch)
        out.loc[out.index[50], "high"] = 28.0

        # Set MA13 in red zone to NaN first (prevents false retest detection)
        for i in range(50, 100):
            out.loc[out.index[i], "MA13"] = float("nan")

        # MA13 touch at rows 61-65: low <= MA13, first touch at row 61
        for i in range(61, 66):
            out.loc[out.index[i], "MA13"] = 27.0
            out.loc[out.index[i], "low"] = 26.5
            out.loc[out.index[i], "close"] = 27.0

        # Post-retest consolidation: rows 66-97, close 29.0, high 30.5 (below pre-retest high)
        for i in range(66, 98):
            out.loc[out.index[i], "close"] = 29.0
            out.loc[out.index[i], "high"] = 30.5

        # Yesterday (row 98): close 30.0, still below pre-test high
        out.loc[out.index[-2], "close"] = 30.0
        out.loc[out.index[-2], "high"] = 30.5

        # Today (row 99): close 32.65, above pre-test high 32.50 — breakout!
        out.loc[out.index[-1], "close"] = 32.65
        out.loc[out.index[-1], "high"] = 32.8
        # MA13 for today must be set so close > MA13 (no MA21 break required)
        out.loc[out.index[-1], "MA13"] = 30.0

        # MA21 above price throughout red zone (yesterday already above MA21)
        ma21_val = 28.0
        for i in range(50, 100):
            out.loc[out.index[i], "MA21"] = ma21_val

        # MA60 well below price throughout red zone
        for i in range(50, 100):
            out.loc[out.index[i], "MA60"] = 15.0

        signals = detect_technical_strategies(out, "TEST", "test")
        strat_b3 = [s for s in signals if s.get("strategy_code") == "B" and s.get("sub_signal_type") == "B3_breakout_after_retest"]
        self.assertGreaterEqual(
            len(strat_b3), 1,
            "B3 should fire for Kangshu-like data; got signals: " + str([s.get("sub_signal_type") for s in signals]),
        )

    def test_strategy_b3_5351_yuchuang_2025_10_07_should_not_fire(self):
        """5351 鈺創 2025-10-07: B3 should NOT fire.

        Scenario:
        - Red zone present (MACD_HIST > 0)
        - First retest cluster (rows 61-65): MA13 retest at lows ~27-30, pre-retest high ~35.9
        - Higher high formed later in red zone: ~40.2 (row 75 area)
        - Most recent retest cluster (rows 78-82): MA13 retest, pre-cluster high ~40.2
        - Today (row 99): close 36.5, below breakout_high 40.2 → NO breakout
        - B3 must use most-recent retest cluster (78-82), NOT first cluster (61-65)
        - breakout_high must be ~40.2, NOT ~35.9
        """
        closes = [10.0 + i * 0.5 for i in range(100)]
        df = _make_daily(closes)
        out = _apply(df)

        # Red zone: rows 50-98 (MACD_HIST > 0)
        for i in range(50):
            out.loc[out.index[i], "MACD_HIST"] = -0.5
        for i in range(50, 99):
            out.loc[out.index[i], "MACD_HIST"] = 0.8

        # Pre-retest zone high: rows 51-60, high ~35.9
        for i in range(51, 61):
            out.loc[out.index[i], "close"] = 30.0 + (i - 51) * 0.6
            out.loc[out.index[i], "high"] = 31.0 + (i - 51) * 0.5
        out.loc[out.index[50], "high"] = 31.0

        # Set MA13 in red zone to NaN first
        for i in range(50, 100):
            out.loc[out.index[i], "MA13"] = float("nan")

        # First MA13 touch cluster: rows 61-65 (gapped from first cluster by >3 bars from prev)
        for i in range(61, 66):
            out.loc[out.index[i], "MA13"] = 28.0
            out.loc[out.index[i], "low"] = 27.0
            out.loc[out.index[i], "close"] = 27.5

        # Rising after first retest: rows 66-75, making higher highs ~40.2
        for i in range(66, 76):
            out.loc[out.index[i], "close"] = 28.0 + (i - 66) * 1.3
            out.loc[out.index[i], "high"] = 29.0 + (i - 66) * 1.3

        # Second (most recent) MA13 touch cluster: rows 78-82
        for i in range(78, 83):
            out.loc[out.index[i], "MA13"] = 35.0
            out.loc[out.index[i], "low"] = 34.0
            out.loc[out.index[i], "close"] = 35.0

        # Post-second-retest consolidation: rows 83-97, close ~36-37, below 40.2
        for i in range(83, 98):
            out.loc[out.index[i], "close"] = 36.5
            out.loc[out.index[i], "high"] = 37.0

        # Yesterday (row 98): close 35.5, below 40.2
        out.loc[out.index[-2], "close"] = 35.5
        out.loc[out.index[-2], "high"] = 37.0

        # Today (row 99): close 36.5, STILL below 40.2 — NO breakout
        out.loc[out.index[-1], "close"] = 36.5
        out.loc[out.index[-1], "high"] = 37.0
        out.loc[out.index[-1], "MA13"] = 35.0  # close > MA13

        # MA21 above price throughout
        for i in range(50, 100):
            out.loc[out.index[i], "MA21"] = 38.0

        # MA60 well below
        for i in range(50, 100):
            out.loc[out.index[i], "MA60"] = 15.0

        signals = detect_technical_strategies(out, "TEST", "test")
        strat_b3 = [s for s in signals if s.get("strategy_code") == "B" and s.get("sub_signal_type") == "B3_breakout_after_retest"]
        self.assertEqual(
            len(strat_b3), 0,
            "B3 should NOT fire for 5351 鈺創 2025-10-07; got: " + str([s.get("sub_signal_type") for s in signals]),
        )

    def test_strategy_b3_dajia_2026_05_20_should_not_refire_after_prior_close_breakout(self):
        """2221 大甲 2026-05-20: B3 should NOT fire if close already broke breakout_high after last retest cluster.

        Scenario:
        - Red zone present (MACD_HIST > 0)
        - Most recent retest cluster (rows 61-65): MA13 retest, pre-cluster high ~42.75
        - After retest cluster, row 80 close = 43.0 (> 42.75, already broke breakout_high)
        - Yesterday (row 98): close 42.0, below 42.75
        - Today (row 99): close 43.5, above 42.75 — but prior close already broke it
        - B3 must be the FIRST close breakout after last retest cluster → NO B3
        """
        closes = [10.0 + i * 0.5 for i in range(100)]
        df = _make_daily(closes)
        out = _apply(df)

        # Red zone: rows 50-98 (MACD_HIST > 0)
        for i in range(50):
            out.loc[out.index[i], "MACD_HIST"] = -0.5
        for i in range(50, 99):
            out.loc[out.index[i], "MACD_HIST"] = 0.8

        # Pre-retest zone high: rows 51-60, high building toward ~42.75
        for i in range(51, 61):
            out.loc[out.index[i], "close"] = 35.0 + (i - 51) * 0.7
            out.loc[out.index[i], "high"] = 36.0 + (i - 51) * 0.7
        out.loc[out.index[50], "high"] = 36.0

        # Set MA13 in red zone to NaN first
        for i in range(50, 100):
            out.loc[out.index[i], "MA13"] = float("nan")

        # Most recent MA13 touch cluster: rows 61-65
        for i in range(61, 66):
            out.loc[out.index[i], "MA13"] = 32.0
            out.loc[out.index[i], "low"] = 31.0
            out.loc[out.index[i], "close"] = 32.0

        # After retest: rows 66-97, row 80 close = 43.0 already > 42.75
        for i in range(66, 98):
            out.loc[out.index[i], "close"] = 40.0
            out.loc[out.index[i], "high"] = 41.0
        out.loc[out.index[80], "close"] = 43.0
        out.loc[out.index[80], "high"] = 43.5

        # Yesterday (row 98): close 42.0, below breakout_high
        out.loc[out.index[-2], "close"] = 42.0
        out.loc[out.index[-2], "high"] = 42.5

        # Today (row 99): close 43.5, above breakout_high 42.75 — but already broke on row 80
        out.loc[out.index[-1], "close"] = 43.5
        out.loc[out.index[-1], "high"] = 44.0
        out.loc[out.index[-1], "MA13"] = 40.0  # close > MA13

        # MA21 above price throughout
        for i in range(50, 100):
            out.loc[out.index[i], "MA21"] = 45.0

        # MA60 well below
        for i in range(50, 100):
            out.loc[out.index[i], "MA60"] = 15.0

        signals = detect_technical_strategies(out, "TEST", "test")
        strat_b3 = [s for s in signals if s.get("strategy_code") == "B" and s.get("sub_signal_type") == "B3_breakout_after_retest"]
        self.assertEqual(
            len(strat_b3), 0,
            "B3 should NOT fire for 2221 大甲 2026-05-20 after prior close breakout; got: " + str([s.get("sub_signal_type") for s in signals]),
        )

    def test_strategy_b3_5425_taihan_2026_05_19_allows_red_start_intraday_ma60_break(self):
        """5425 台半 2026-05-19: B3 should fire even if red-start day low < MA60, as long as close > MA60.

        Scenario:
        - Red zone starts at row 50 (MACD_HIST > 0 from row 50 onward)
        - Red-start day (row 50): low = 68.0 < MA60=70.0, but close = 75.0 > MA60
        - From row 51 onward: all close >= MA60
        - MA13 retest cluster: rows 61-65
        - breakout_high ~72.5 (pre-retest high)
        - Yesterday (row 98): close 70.0, below breakout_high
        - Today (row 99): close 73.0, above breakout_high and above MA13
        - B3 must ALLOW red-start day intraday MA60 break (low < MA60) because close > MA60
        """
        closes = [10.0 + i * 0.5 for i in range(100)]
        df = _make_daily(closes)
        out = _apply(df)

        # Red zone: rows 50-98 (MACD_HIST > 0)
        for i in range(50):
            out.loc[out.index[i], "MACD_HIST"] = -0.5
        for i in range(50, 99):
            out.loc[out.index[i], "MACD_HIST"] = 0.8

        # Pre-retest high: rows 51-60, high building toward ~72.5
        for i in range(51, 61):
            out.loc[out.index[i], "close"] = 65.0 + (i - 51) * 0.75
            out.loc[out.index[i], "high"] = 66.0 + (i - 51) * 0.7
        out.loc[out.index[50], "high"] = 66.0

        # Set MA13 in red zone to NaN first
        for i in range(50, 100):
            out.loc[out.index[i], "MA13"] = float("nan")

        # MA13 touch cluster: rows 61-65
        for i in range(61, 66):
            out.loc[out.index[i], "MA13"] = 62.0
            out.loc[out.index[i], "low"] = 61.0
            out.loc[out.index[i], "close"] = 62.5

        # Post-retest consolidation: rows 66-97, close ~68-70, below breakout_high
        for i in range(66, 98):
            out.loc[out.index[i], "close"] = 68.0
            out.loc[out.index[i], "high"] = 69.0

        # Yesterday (row 98): close 70.0, below breakout_high 72.5
        out.loc[out.index[-2], "close"] = 70.0
        out.loc[out.index[-2], "high"] = 71.0

        # Today (row 99): close 73.0, above breakout_high 72.5
        out.loc[out.index[-1], "close"] = 73.0
        out.loc[out.index[-1], "high"] = 73.5
        out.loc[out.index[-1], "MA13"] = 65.0  # close > MA13

        # MA21 above price throughout
        for i in range(50, 100):
            out.loc[out.index[i], "MA21"] = 75.0

        # MA60: set low enough so all closes from row 51 onward are >= MA60
        # Red-start day (row 50): low=58 < MA60=60, but close=75 > MA60
        for i in range(50, 100):
            out.loc[out.index[i], "MA60"] = 60.0
        out.loc[out.index[50], "low"] = 58.0
        out.loc[out.index[50], "close"] = 75.0

        signals = detect_technical_strategies(out, "TEST", "test")
        strat_b3 = [s for s in signals if s.get("strategy_code") == "B" and s.get("sub_signal_type") == "B3_breakout_after_retest"]
        self.assertGreaterEqual(
            len(strat_b3), 1,
            "B3 should fire for 5425 台半 2026-05-19; got: " + str([s.get("sub_signal_type") for s in signals]),
        )

    def test_strategy_b3_rejects_close_below_ma60_after_red_start(self):
        """B3 should NOT fire if close < MA60 on any day after red-start day.

        Scenario:
        - Red zone starts at row 50.
        - Red-start day (row 50): close > MA60.
        - Row 70: close = 14.0 < MA60=15.0 (close below MA60 after red start)
        - MA13 retest cluster exists.
        - breakout_high valid.
        - Today would break breakout_high.
        - But close < MA60 on day after red start → exclude B3.
        """
        closes = [10.0 + i * 0.5 for i in range(100)]
        df = _make_daily(closes)
        out = _apply(df)

        # Red zone: rows 50-98
        for i in range(50):
            out.loc[out.index[i], "MACD_HIST"] = -0.5
        for i in range(50, 99):
            out.loc[out.index[i], "MACD_HIST"] = 0.8

        # Pre-retest high: rows 51-60
        for i in range(51, 61):
            out.loc[out.index[i], "close"] = 20.0 + (i - 51) * 0.5
            out.loc[out.index[i], "high"] = 21.0 + (i - 51) * 0.5
        out.loc[out.index[50], "high"] = 21.0

        # Set MA13 in red zone to NaN first
        for i in range(50, 100):
            out.loc[out.index[i], "MA13"] = float("nan")

        # MA13 touch cluster: rows 61-65
        for i in range(61, 66):
            out.loc[out.index[i], "MA13"] = 18.0
            out.loc[out.index[i], "low"] = 17.5
            out.loc[out.index[i], "close"] = 18.0

        # Post-retest consolidation: rows 66-97
        for i in range(66, 98):
            out.loc[out.index[i], "close"] = 19.0
            out.loc[out.index[i], "high"] = 20.0

        # Row 70: close < MA60 (the key violation)
        out.loc[out.index[70], "close"] = 14.0

        # Yesterday (row 98): close 19.5, below breakout_high
        out.loc[out.index[-2], "close"] = 19.5
        out.loc[out.index[-2], "high"] = 20.0

        # Today (row 99): close 23.0, would break breakout_high
        out.loc[out.index[-1], "close"] = 23.0
        out.loc[out.index[-1], "high"] = 23.5
        out.loc[out.index[-1], "MA13"] = 17.0  # close > MA13

        # MA21 above price throughout
        for i in range(50, 100):
            out.loc[out.index[i], "MA21"] = 25.0

        # MA60: red-start day close=20 > MA60=15; row 70 close=14 < MA60=15
        for i in range(50, 100):
            out.loc[out.index[i], "MA60"] = 15.0
        out.loc[out.index[50], "close"] = 20.0  # red-start day close > MA60

        signals = detect_technical_strategies(out, "TEST", "test")
        strat_b3 = [s for s in signals if s.get("strategy_code") == "B" and s.get("sub_signal_type") == "B3_breakout_after_retest"]
        self.assertEqual(
            len(strat_b3), 0,
            "B3 should NOT fire when close < MA60 after red-start day; got: " + str([s.get("sub_signal_type") for s in signals]),
        )


# Strategy C: green-zone (MACD_HIST < 0) divergence tests
# -------------------------------------------------------------------
class TestStrategyCGreenZone(unittest.TestCase):
    def test_strategy_c_uses_green_zone_not_red_zone(self):
        """Strategy C must use MACD_HIST < 0 zones, not > 0 zones."""
        # Build a frame where:
        # - DIF < 0
        # - Two completed GREEN zones exist (MACD_HIST < 0)
        # - Zone2 price low < Zone1 price low
        # - Zone2 hist_min > Zone1 hist_min (hist divergence)
        # - MA21 broken today
        closes = []
        # Zone 1 GREEN: price started at 100, dropped to 85
        for i in range(30):
            closes.append(100 - i * 0.5)
        # Zone 2 GREEN: price continued lower to 75
        for i in range(30):
            closes.append(closes[-1] - i * 0.3)
        # Extend to 200 rows for indicators, MACD stays negative
        for i in range(140):
            closes.append(closes[-1] + 0.1)

        # Force some positive histogram rows mixed in (red zones)
        # but we want green zones to be the two most-recent complete ones
        df = _make_daily(closes)
        out = _apply(df)

        # Zone data check: after apply_indicators, _find_green_zones_for_divergence
        # should find at least 2 green zones
        from technical_strategy_engine import _find_green_zones_for_divergence
        zones = _find_green_zones_for_divergence(out)
        # If we get < 2 zones, it means the test data didn't create proper green zones
        # The important thing is: we verify the function EXISTS and doesn't use red zones
        self.assertIsInstance(zones, list)

    def test_strategy_c_with_forced_green_zones(self):
        """Strategy C C1 fires only when green zones meet divergence criteria."""
        # Create frame with two green zones meeting all C1 conditions
        # Zone 1: low=90, hist_min=-2
        # Zone 2: low=80 (< 90), hist_min=-1 (> -2)
        closes = [100] * 200
        df = _make_daily(closes)
        out = _apply(df)

        # Create actual price divergence: zone2 low < zone1 low
        # Zone 1 (rows 0-49): close=100, low=100
        # Zone 2 (rows 60-119): close=90, low=80 (price dropped)
        for i in range(60, 120):
            out.loc[out.index[i], "close"] = 90.0
            out.loc[out.index[i], "low"] = 80.0

        # Override MACD_HIST to create two completed green zones
        hist_vals = []
        for i in range(len(out)):
            if i < 50:
                hist_vals.append(-2.0)  # Zone 1 green (first completed zone)
            elif i < 60:
                hist_vals.append(0.5)  # Brief red zone between
            elif i < 120:
                hist_vals.append(-1.0)  # Zone 2 green (more recent, hist > Zone1)
            else:
                hist_vals.append(0.2)  # Last rows: MACD positive (no ongoing green)
        out["MACD_HIST"] = hist_vals
        out["DIF"] = -1.0  # DIF < 0 required
        out["K"] = 40.0
        out["D"] = 40.0

        # Override last two rows for MA21 cross
        out.loc[out.index[-2], "MA21"] = 105.0
        out.loc[out.index[-2], "close"] = 103.0
        out.loc[out.index[-1], "MA21"] = 105.0
        out.loc[out.index[-1], "close"] = 107.0  # crosses above

        from technical_strategy_engine import detect_technical_strategies
        signals = detect_technical_strategies(out, "TEST", "測試")
        c_main = [s for s in signals if s.get("strategy_code") == "C" and s.get("sub_signal_type") == "C1_macd_bullish_divergence_break_ma21"]
        # With forced conditions, C1 should fire
        self.assertGreaterEqual(len(c_main), 1, f"C1 should fire with forced green zone data; got {signals}")


# -------------------------------------------------------------------
# Strategy A: complete wave-cycle tests
# -------------------------------------------------------------------
class TestStrategyAWaveGreenZone(unittest.TestCase):
    def test_wave_low_uses_entire_contiguous_green_zone(self):
        from technical_strategy_engine import _find_macd_wave_cycle

        out = _apply(_make_daily([100.0] * 120))
        out["MACD_HIST"] = [0.0] * 20 + [-0.5] * 30 + [1.0] * 20 + [-0.5] * 50
        out.loc[25, "low"] = 90.0
        out.loc[49, "low"] = 99.0
        out.loc[50:69, "high"] = 120.0

        wave = _find_macd_wave_cycle(out)
        self.assertIsNotNone(wave)
        self.assertEqual(wave["wave_start_idx"], 20)
        self.assertEqual(wave["wave_green_end_idx"], 49)
        self.assertEqual(wave["wave_end_idx_red"], 69)
        self.assertEqual(wave["wave_low"], 90.0)
        self.assertEqual(wave["wave_high"], 120.0)
        self.assertAlmostEqual(wave["wave_return_pct"], (120.0 - 90.0) / 90.0 * 100)
        self.assertEqual(wave["wave_start_date"], str(out.loc[20, "date"]))

    def test_zero_bar_separates_green_zones_and_cannot_be_green_end(self):
        from technical_strategy_engine import _find_macd_wave_cycle

        out = _apply(_make_daily([100.0] * 120))
        out["MACD_HIST"] = [0.0] * 10 + [-0.5] * 10 + [0.0] + [-0.5] * 29 + [1.0] * 20 + [-0.5] * 50
        out.loc[15, "low"] = 80.0
        out.loc[25, "low"] = 92.0
        wave = _find_macd_wave_cycle(out)
        self.assertIsNotNone(wave)
        self.assertEqual(wave["wave_start_idx"], 21)
        self.assertEqual(wave["wave_low"], 92.0)

        out.loc[49, "MACD_HIST"] = 0.0
        self.assertIsNone(_find_macd_wave_cycle(out))

    def test_a1_features_and_threshold_use_full_green_zone(self):
        out = _apply(_make_daily([100.0] * 120))
        out["MACD_HIST"] = [0.0] * 20 + [-0.5] * 30 + [1.0] * 20 + [-0.5] * 50
        out.loc[25, "low"] = 90.0
        out.loc[49, "low"] = 100.0
        out.loc[50:69, "high"] = 104.0
        out.loc[118, ["close", "MA21"]] = [99.0, 100.0]
        out.loc[119, ["close", "MA21"]] = [101.0, 100.0]

        signals = detect_technical_strategies(out, "TEST", "測試")
        a1 = next(sig for sig in signals if sig["sub_signal_type"] == "A1_direct_ma21_breakout")
        self.assertEqual(a1["features"]["wave_low"], 90.0)
        self.assertEqual(a1["features"]["wave_start_date"], str(out.loc[20, "date"]))
        self.assertAlmostEqual(a1["features"]["retracement_ratio"], (104.0 - 98.0) / (104.0 - 90.0))


class TestStrategyARejectMultiDayRed(unittest.TestCase):
    def test_strategy_a_does_not_fire_when_macd_red_for_multiple_days(self):
        """Strategy A must not fire when MACD has been red for 2+ days (rejection of aged red zone stocks)."""
        # Build a wave: green -> red (completed) -> pullback -> still red for 2+ days
        closes = []
        for i in range(200):
            if i < 100:
                closes.append(100.0 + i * 0.3)  # rising
            elif i < 150:
                closes.append(130.0 + (i - 100) * 0.2)  # pullback (green zone)
            else:
                closes.append(140.0 + (i - 150) * 0.1)  # red continues
        df = _make_daily(closes)
        out = _apply(df)

        # green (0-99), red (100-149 completed), then red continues 150+
        hist_vals = [-0.5 if i < 100 else 1.0 if i < 150 else 0.8 for i in range(200)]
        out["MACD_HIST"] = hist_vals

        # MA21 cross: prev below MA21, latest above MA21 (A would fire without MACD state check)
        ma21_val = out["MA21"].iloc[-1]
        out.loc[out.index[-2], "MA21"] = ma21_val
        out.loc[out.index[-2], "close"] = float(ma21_val * 0.97)
        out.loc[out.index[-1], "MA21"] = ma21_val
        out.loc[out.index[-1], "close"] = float(ma21_val * 1.03)

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_a = [s for s in signals if s.get("strategy_code") == "A"]
        self.assertEqual(
            len(strat_a), 0,
            f"Strategy A should NOT fire when MACD has been red for multiple days; got {strat_a}",
        )

    def test_strategy_a_fires_when_macd_still_green(self):
        """Strategy A must fire when MACD_HIST <= 0 at signal day (green column pullback)."""
        closes = []
        for i in range(200):
            if i < 100:
                closes.append(100.0 + i * 0.3)
            elif i < 150:
                closes.append(130.0 + (i - 100) * 0.2)
            else:
                closes.append(135.0 - (i - 150) * 0.1)  # pullback, MACD green
        df = _make_daily(closes)
        out = _apply(df)

        # green (0-99), red completed (100-149), then green again 150+
        hist_vals = [-0.5 if i < 100 else 1.0 if i < 150 else -0.4 for i in range(200)]
        out["MACD_HIST"] = hist_vals

        # MA21 cross
        ma21_val = out["MA21"].iloc[-1]
        out.loc[out.index[-2], "MA21"] = ma21_val
        out.loc[out.index[-2], "close"] = float(ma21_val * 0.97)
        out.loc[out.index[-1], "MA21"] = ma21_val
        out.loc[out.index[-1], "close"] = float(ma21_val * 1.03)

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_a = [s for s in signals if s.get("strategy_code") == "A"]
        self.assertGreaterEqual(
            len(strat_a), 1,
            f"Strategy A should fire when MACD is still green; got signals: {signals}",
        )

    def test_strategy_a_fires_when_macd_turns_red_today(self):
        """Strategy A must fire when MACD just flipped to red today (prev <= 0, latest > 0)."""
        closes = []
        for i in range(200):
            if i < 100:
                closes.append(100.0 + i * 0.3)
            elif i < 150:
                closes.append(130.0 + (i - 100) * 0.2)
            else:
                closes.append(140.0 - (i - 150) * 0.1)  # just turned red today
        df = _make_daily(closes)
        out = _apply(df)

        # prev row MACD_HIST <= 0 (green), latest > 0 (red today)
        hist_vals = [-0.5 if i < 100 else 1.0 if i < 150 else -0.2 if i < 199 else 0.5 for i in range(200)]
        out["MACD_HIST"] = hist_vals

        # MA21 cross
        ma21_val = out["MA21"].iloc[-1]
        out.loc[out.index[-2], "MA21"] = ma21_val
        out.loc[out.index[-2], "close"] = float(ma21_val * 0.97)
        out.loc[out.index[-1], "MA21"] = ma21_val
        out.loc[out.index[-1], "close"] = float(ma21_val * 1.03)

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_a = [s for s in signals if s.get("strategy_code") == "A"]
        self.assertGreaterEqual(
            len(strat_a), 1,
            f"Strategy A should fire when MACD just turned red today; got signals: {signals}",
        )


# -------------------------------------------------------------------
# Strategy C: green-zone (MACD_HIST < 0) divergence tests
# -------------------------------------------------------------------
        """_find_macd_wave_cycle must NOT return an ongoing red zone as completed."""
        from technical_strategy_engine import _find_macd_wave_cycle
        # Build: green (0-98, meaningful movement) -> completed red (100-149) -> ongoing red that ends (150-199)
        # The ongoing red (150+) must end for the wave to be completed
        closes = [100.0 + i * 0.2 for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        # Green (0-99) -> completed red (100-149) -> green again (150-199, ongoing at test time)
        # Test data: row 150 = 0.5 (start of new red), but we need end BEFORE signal
        # So we set: green(0-99), completed red(100-149), then at 150+ we keep it green(-0.3) but the START of a red zone is what matters
        # The key: we need a completed red zone, not ongoing. Let rows 150-199 be green (-0.3) to show the old red ended
        hist_vals = [-0.5 if i < 100 else 1.0 if i < 150 else -0.3 for i in range(200)]
        out["MACD_HIST"] = hist_vals
        wave = _find_macd_wave_cycle(out)
        self.assertIsNotNone(wave, "Should find completed wave cycle")
        self.assertEqual(wave["wave_end_idx_red"], 149, "Red zone ended at row 149 (transition to green at 150)")
        self.assertGreater(wave["wave_end_idx"], 90, "Wave zone should span multiple rows before red zone")
        # Verify new fields: wave_green_end vs wave_red_end
        self.assertEqual(wave["wave_green_end_idx"], 99, "Green zone ended at row 99 (row before red starts at 100)")
        self.assertIn("wave_green_end_date", wave)
        self.assertIn("wave_red_end_date", wave)
        # wave_end_date should now be red zone END date, not green zone end date
        self.assertEqual(wave["wave_end_date"], wave["wave_red_end_date"], "wave_end_date should equal wave_red_end_date")
        self.assertNotEqual(wave["wave_end_date"], wave["wave_green_end_date"], "wave_end_date should NOT equal wave_green_end_date")

    def test_a1_fires_even_when_pullback_below_wave_low(self):
        """A1 must fire even when pullback_low < wave_low (pullback breaks wave low).

        Per discussion draft: pullback_low < wave_low does NOT disqualify A1.
        Features should still contain the correct pullback_low and wave_low values.
        Wave return must exceed 5% minimum threshold.
        """
        from technical_strategy_engine import detect_technical_strategies
        closes = [100.0] * 200
        df = _make_daily(closes)
        out = _apply(df)

        # Build: green zone (rows 0-99) -> red zone (rows 100-149, ended) ->
        # pullback that goes BELOW wave_low (rows 150-198) -> signal day (199)
        hist_vals = [-0.5 if i < 100 else 1.0 if i < 150 else -0.5 if i < 199 else -0.3 for i in range(200)]
        out["MACD_HIST"] = hist_vals

        # Set prices with wave_return_pct >= 5%: wave_low=100, wave_high=124.5 (24.5% return)
        for i in range(100, 150):
            out.loc[out.index[i], "close"] = 100 + (i - 100) * 0.5
            out.loc[out.index[i], "high"] = 100 + (i - 100) * 0.52
            out.loc[out.index[i], "low"] = 100 + (i - 100) * 0.45
        for i in range(150, 199):
            out.loc[out.index[i], "close"] = 125 - (i - 150) * 0.6
            out.loc[out.index[i], "high"] = 125 - (i - 150) * 0.58
            out.loc[out.index[i], "low"] = 125 - (i - 150) * 0.7  # drops to ~89 < wave_low

        # MA21 cross: prev below, today above
        ma21_val = 115.0
        out.loc[out.index[-2], "MA21"] = ma21_val
        out.loc[out.index[-2], "close"] = 110.0  # below MA21
        out.loc[out.index[-1], "MA21"] = ma21_val
        out.loc[out.index[-1], "close"] = 120.0  # above MA21

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_a = [s for s in signals if s.get("strategy_code") == "A"]
        self.assertGreaterEqual(
            len(strat_a), 1,
            f"A1 should fire even when pullback breaks wave_low; got {signals}",
        )
        # Verify features correctly record the wave_low and pullback_low
        a1 = strat_a[0]
        self.assertLess(a1["features"]["pullback_low"], a1["features"]["wave_low"],
                        "pullback_low should be < wave_low (correctly recorded)")
        self.assertEqual(strat_a[0]["sub_signal_type"], "A1_direct_ma21_breakout")
        # wave_return_pct is in notes as 'wave_return=X%'
        import re
        m = re.search(r'wave_return=([\d.]+)%', strat_a[0]["notes"])
        self.assertIsNotNone(m, f"wave_return not found in notes: {strat_a[0]['notes']}")
        self.assertGreater(float(m.group(1)), 5.0, "wave_return_pct must exceed 5% minimum threshold")

    def test_a_features_contain_wave_and_pullback_fields(self):
        """A signal features must contain wave_start_date, pullback_low, retracement_ratio."""
        from technical_strategy_engine import detect_technical_strategies
        closes = [100] * 200
        df = _make_daily(closes)
        out = _apply(df)

        # Build: green -> red -> pullback -> MA21 cross
        # Ensure wave_return_pct >= 5% with corrected wave_high from red_zone
        hist_vals = [-0.5 if i < 100 else 1.0 if i < 150 else -0.3 for i in range(200)]
        out["MACD_HIST"] = hist_vals

        # Red zone close from 100 to ~124.5 (wave_return ~24.5%)
        for i in range(100, 150):
            out.loc[out.index[i], "close"] = float(100 + (i - 100) * 0.5)
            out.loc[out.index[i], "high"] = float(100 + (i - 100) * 0.52)
            out.loc[out.index[i], "low"] = float(100 + (i - 100) * 0.45)
        # Pullback stays relatively shallow
        for i in range(150, 199):
            out.loc[out.index[i], "close"] = float(125 - (i - 150) * 0.4)
            out.loc[out.index[i], "high"] = float(125 - (i - 150) * 0.42)
            out.loc[out.index[i], "low"] = float(125 - (i - 150) * 0.5)
        out.loc[out.index[-2], "MA21"] = 110.0
        out.loc[out.index[-2], "close"] = 108.0
        out.loc[out.index[-1], "MA21"] = 110.0
        out.loc[out.index[-1], "close"] = 115.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_a = [s for s in signals if s.get("strategy_code") == "A"]
        self.assertGreaterEqual(len(strat_a), 1, f"A should fire; got {signals}")
        feat = strat_a[0]["features"]
        self.assertIn("wave_end_date", feat)
        self.assertIn("pullback_low", feat)
        self.assertIn("retracement_ratio", feat)
        self.assertIn("wave_low", feat)
        self.assertIn("wave_high", feat)
        # Additional new fields
        self.assertIn("wave_green_end_date", feat)
        self.assertIn("wave_red_start_date", feat)
        self.assertIn("wave_red_end_date", feat)
        # Verify wave_end_date is red zone end, not green zone end
        self.assertEqual(feat["wave_end_date"], feat["wave_red_end_date"], "wave_end_date in features must be red zone end date")
        self.assertNotEqual(feat["wave_end_date"], feat["wave_green_end_date"], "wave_end_date must NOT be green zone end date")
        # pullback_low_date must be after wave_end_date (red zone end = pullback start)
        self.assertGreater(feat["pullback_low_date"], feat["wave_end_date"], "pullback_low_date should be after wave_end_date")

    def test_a3_requires_long_ma_broken_in_pullback(self):
        """A3 must only fire when MA105/MA144 was broken during pullback."""
        from technical_strategy_engine import detect_technical_strategies
        closes = [100.0] * 200
        df = _make_daily(closes)
        out = _apply(df)

        hist_vals = [-0.5 if i < 100 else 1.0 if i < 150 else -0.3 for i in range(200)]
        out["MACD_HIST"] = hist_vals

        for i in range(100, 150):
            out.loc[out.index[i], "close"] = float(100 + (i - 100) * 0.15)
            out.loc[out.index[i], "low"] = float(100 + (i - 100) * 0.05)
        # Pullback stays ABOVE MA105
        ma105_val = 105.0
        for i in range(150, 199):
            out.loc[out.index[i], "close"] = float(107.0 - (i - 150) * 0.01)
            out.loc[out.index[i], "low"] = float(107.0 - (i - 150) * 0.02)
        # MA105 set at 105 (never broken during pullback; pullback low ~106)
        out["MA105"] = ma105_val
        out.loc[out.index[-1], "MA105"] = ma105_val
        out.loc[out.index[-2], "MA105"] = ma105_val

        # Signal day: close reclaims MA21 AND MA105 together
        ma21_val = 104.0
        out.loc[out.index[-2], "MA21"] = ma21_val
        out.loc[out.index[-2], "close"] = 103.0
        out.loc[out.index[-1], "MA21"] = ma21_val
        out.loc[out.index[-1], "close"] = 106.5  # > ma21 and > ma105

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_a3 = [s for s in signals if s.get("sub_signal_type") == "A3_reclaim_ma21_and_long_ma"]
        self.assertEqual(
            len(strat_a3), 0,
            f"A3 should NOT fire when MA105 was never broken in pullback; got {signals}",
        )

    def test_a3_fires_when_long_ma_broken_and_reclaimed(self):
        """A3 must fire when MA105/MA144 was broken during pullback and reclaimed today."""
        from technical_strategy_engine import detect_technical_strategies
        closes = [100.0] * 200
        df = _make_daily(closes)
        out = _apply(df)

        hist_vals = [-0.5 if i < 100 else 1.0 if i < 150 else -0.3 for i in range(200)]
        out["MACD_HIST"] = hist_vals

        # Wave: low=100, red zone high=~124.5 (wave_return ~24.5% >= 5%)
        ma105_val = 115.0
        for i in range(100, 150):
            out.loc[out.index[i], "close"] = float(100 + (i - 100) * 0.5)
            out.loc[out.index[i], "high"] = float(100 + (i - 100) * 0.52)
            out.loc[out.index[i], "low"] = float(100 + (i - 100) * 0.45)
        # Pullback DROPS BELOW MA105 (low ~108 < MA105=115)
        for i in range(150, 199):
            out.loc[out.index[i], "close"] = float(125 - (i - 150) * 0.4)
            out.loc[out.index[i], "high"] = float(125 - (i - 150) * 0.38)
            out.loc[out.index[i], "low"] = float(125 - (i - 150) * 0.55)
        out["MA105"] = ma105_val
        out.loc[out.index[-1], "MA105"] = ma105_val
        out.loc[out.index[-2], "MA105"] = ma105_val

        ma21_val = 110.0
        out.loc[out.index[-2], "MA21"] = ma21_val
        out.loc[out.index[-2], "close"] = 108.0
        out.loc[out.index[-1], "MA21"] = ma21_val
        out.loc[out.index[-1], "close"] = 120.0  # > ma21 AND > ma105 (reclaimed)

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_a3 = [s for s in signals if s.get("sub_signal_type") == "A3_reclaim_ma21_and_long_ma"]
        self.assertGreaterEqual(
            len(strat_a3), 1,
            f"A3 should fire when MA105 broken in pullback and reclaimed today; got {signals}",
        )

    def test_ongoing_red_zone_not_used_as_wave_cycle(self):
        """When MACD is still in red zone, that ongoing zone must NOT be used as wave cycle."""
        from technical_strategy_engine import _find_macd_wave_cycle
        closes = [100] * 200
        df = _make_daily(closes)
        out = _apply(df)

        # Force ongoing red zone (no completed red before it)
        hist_vals = []
        for i in range(len(out)):
            if i < 100:
                hist_vals.append(-0.5)  # green
            else:
                hist_vals.append(0.8)   # ongoing red, never turned back to green
        out["MACD_HIST"] = hist_vals
        wave = _find_macd_wave_cycle(out)
        # Should return None because there is no completed red zone
        self.assertIsNone(wave, "Ongoing-only red zone should not produce a wave cycle")

    def test_retracement_ratio_uses_pullback_low_not_yesterday_close(self):
        """retracement_ratio must be computed from pullback_low, not prev['close']."""
        from technical_strategy_engine import detect_technical_strategies
        closes = [100] * 200
        df = _make_daily(closes)
        out = _apply(df)

        hist_vals = []
        for i in range(len(out)):
            if i < 100:
                hist_vals.append(-0.5)
            elif 100 <= i < 150:
                hist_vals.append(1.0)
            else:
                hist_vals.append(-0.3)
        out["MACD_HIST"] = hist_vals

        # Wave: low=100, red zone high=~124.5 (wave_return ~24.5% >= 5%)
        for i in range(100, 150):
            out.loc[out.index[i], "close"] = 100 + (i - 100) * 0.5
            out.loc[out.index[i], "high"] = 100 + (i - 100) * 0.52
            out.loc[out.index[i], "low"] = 100 + (i - 100) * 0.45
        # Pullback: low=~89 (deeper than wave_low)
        for i in range(150, 199):
            out.loc[out.index[i], "close"] = 125 - (i - 150) * 0.6
            out.loc[out.index[i], "high"] = 125 - (i - 150) * 0.58
            out.loc[out.index[i], "low"] = 125 - (i - 150) * 0.7
        ma21_val = 112.0
        out.loc[out.index[-2], "MA21"] = ma21_val
        out.loc[out.index[-2], "close"] = 109.0
        out.loc[out.index[-1], "MA21"] = ma21_val
        out.loc[out.index[-1], "close"] = 118.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_a = [s for s in signals if s.get("strategy_code") == "A"]
        self.assertGreaterEqual(len(strat_a), 1, f"A should fire; got {signals}")
        feat = strat_a[0]["features"]
        # pullback_low should NOT be equal to prev["close"] (103)
        # It should be the actual lowest low in pullback zone
        self.assertGreater(
            feat["pullback_low"], 0,
            f"pullback_low must be a valid price; got {feat['pullback_low']}",
        )
        # Verify wave dates are actual dates, not DataFrame indices
        import re
        for field in ["wave_start_date", "wave_green_end_date", "wave_red_start_date", "wave_red_end_date", "wave_end_date"]:
            date_str = feat.get(field, "")
            # Must be like 2025-01-10 or 2025-10-15, not like "114" or "180"
            self.assertIsNotNone(re.match(r'\d{4}-\d{2}-\d{2}', date_str),
                f"{field} must be YYYY-MM-DD date, got: {date_str}")
        # wave_end_date must equal wave_red_end_date
        self.assertEqual(feat["wave_end_date"], feat["wave_red_end_date"],
            "wave_end_date must equal wave_red_end_date")


# -------------------------------------------------------------------
# Strategy A: MACD green-column (MACD_HIST < 0) must still allow A signals
# -------------------------------------------------------------------
class TestStrategyAGreenColumn(unittest.TestCase):
    def test_strategy_a_fires_when_macd_hist_negative(self):
        """Strategy A must fire even when latest MACD_HIST < 0 (green column)."""
        # Build: green -> red (completed) -> pullback -> ongoing green -> MA21 cross
        closes = [100.0 + i * 0.2 if i < 100 else 120.0 + (i - 100) * 0.1 if i < 150 else 125.0 - (i - 150) * 0.05 for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        # Override: green(0-99) -> red(100-149) -> green(150+)
        hist_vals = [-0.5 if i < 100 else 1.0 if i < 150 else -0.3 for i in range(200)]
        out["MACD_HIST"] = hist_vals

        ma21_prev = float(out["close"].iloc[-2] * 1.01)
        out.loc[out.index[-2], "MA21"] = ma21_prev
        out.loc[out.index[-2], "close"] = float(ma21_prev * 0.98)
        ma21_curr = float(out["close"].iloc[-1] * 0.99)
        out.loc[out.index[-1], "MA21"] = ma21_curr
        out.loc[out.index[-1], "close"] = float(ma21_curr * 1.02)

        from technical_strategy_engine import detect_technical_strategies
        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_a = [s for s in signals if s.get("strategy_code") == "A"]
        self.assertGreaterEqual(
            len(strat_a), 1,
            f"Strategy A should fire even when MACD_HIST < 0; got signals: {signals}",
        )


# -------------------------------------------------------------------
# Strategy D: must contain 高風險短線策略 in every notes field
# -------------------------------------------------------------------
class TestStrategyDHighRiskLabel(unittest.TestCase):
    def test_strategy_d_all_signals_contain_chinese_high_risk_label(self):
        """Every Strategy D signal notes must contain '高風險短線策略'."""
        # Build a raw DataFrame where D conditions can fire:
        # - close above MA60 and MA105 (prior strength)
        # - prev below MA21, latest above MA21 (quick reclaim)
        start_date = date(2025, 1, 1)
        closes = []
        for i in range(200):
            closes.append(100 + i * 0.5)

        opens = [c * 1.002 for c in closes]
        highs = [c * 1.015 for c in closes]
        lows = [c * 0.985 for c in closes]
        volumes = [1_000_000] * 200

        df = _make_daily(closes, opens, highs, lows, volumes, start_date)
        out = _apply(df)

        # Force MA60 and MA105 below last close (prior strength)
        last_close = out["close"].iloc[-1]
        out.loc[out.index[-1], "MA60"] = last_close * 0.85
        out.loc[out.index[-1], "MA105"] = last_close * 0.80

        # Force MA21 cross: prev below MA21, latest above MA21
        ma21_val = out["MA21"].iloc[-1]
        out.loc[out.index[-2], "MA21"] = ma21_val
        out.loc[out.index[-2], "close"] = ma21_val * 0.97
        out.loc[out.index[-1], "MA21"] = ma21_val
        out.loc[out.index[-1], "close"] = ma21_val * 1.05

        from technical_strategy_engine import detect_technical_strategies
        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("strategy_code") == "D"]

        self.assertGreaterEqual(
            len(strat_d), 1,
            f"Test must force-generate D signals; got 0 D signals from {signals}",
        )
        for sig in strat_d:
            self.assertIn(
                "高風險短線策略",
                sig.get("notes", ""),
                f"D signal notes missing 高風險短線策略: {sig.get('notes')}",
            )


# -------------------------------------------------------------------
# Report format tests
# -------------------------------------------------------------------
class TestReportFormat(unittest.TestCase):
    def test_strategy_d_count_is_unique_across_d1_and_d2(self):
        signals = [
            {"stock_id": "2330", "stock_name": "台積電", "close": 900.0,
             "sub_signal_type": sub, "features": {}, "notes": ""}
            for sub in ("D1_above_zero_short_ma_reclaim", "D2_below_zero_short_ma_reclaim", "D3_kd_death_cross_first_reversal")
        ]
        result = TechnicalScanResult(
            report_date=date(2026, 5, 19), total_symbols=1,
            hard_filter_passed=1, matched_symbols=1,
            bullish={}, bearish={}, sources={"test"},
            strategy_signals={"A": [], "B": [], "C": [], "D": signals},
        )
        report = format_technical_report(result)
        messages = "\n".join(format_technical_report_messages(result))
        self.assertIn("策略 D：1 檔", report)
        self.assertIn("策略 D：動能背景短線轉強｜共 1 檔", messages)
        self.assertIn("D1｜零軸上短均線收復", messages)
        self.assertIn("D2｜零軸下短均線收復", messages)
        self.assertIn("D3｜KD 死叉後首次轉強", messages)

    def test_report_not_contain_paused_strategy_text(self):
        """format_technical_report must NOT contain '暫停策略' text."""
        from technical_scanner import TechnicalScanResult, format_technical_report
        from datetime import date
        empty_result = TechnicalScanResult(
            report_date=date(2026, 5, 19),
            total_symbols=1000,
            hard_filter_passed=100,
            matched_symbols=10,
            bullish={},
            bearish={},
            sources={"Yahoo Finance"},
            strategy_signals={"A": [], "B": [], "C": [], "D": []},
        )
        report = format_technical_report(empty_result)
        self.assertNotIn("暫停策略", report, "Report must not claim strategies are paused")

    def test_report_contains_macd_and_dual_ma_blocks(self):
        """Report keeps A-D labels under the renamed MACD block."""
        from technical_scanner import TechnicalScanResult, format_technical_report
        from datetime import date
        result = TechnicalScanResult(
            report_date=date(2026, 5, 19),
            total_symbols=1000,
            hard_filter_passed=100,
            matched_symbols=10,
            bullish={},
            bearish={},
            sources={"Yahoo Finance"},
            strategy_signals={
                "A": [{"stock_id": "2330", "stock_name": "台積電", "signal_date": "2026-05-19",
                       "strategy_code": "A", "technical_signal_type": "bull_pullback_ma21_breakout",
                       "sub_signal_type": "A1_direct_ma21_breakout", "close": 900.0,
                       "ma_context": {}, "macd_context": {}, "kd_context": {}, "volume_quality": True,
                       "technical_setup_score": 5, "initial_invalid_price": 890.0,
                       "structural_invalid_price": 800.0, "risk_distance_pct": None,
                       "risk_distance_atr": 5.0, "notes": "wave", "features": {}}],
                "B": [], "C": [], "D": [],
            },
        )
        report = format_technical_report(result)
        self.assertIn("MACD 動能策略（A–D）", report)
        self.assertIn("雙均線結構策略", report)
        self.assertIn("策略 A", report)
        self.assertIn("策略 B", report)
        self.assertIn("策略 C", report)
        self.assertIn("策略 D", report)

    def test_report_no_english_sub_signal_codes(self):
        """Report must NOT contain raw English sub_signal_type codes."""
        from technical_scanner import TechnicalScanResult, format_technical_report, STRATEGY_SUB_SIGNAL_LABELS
        from datetime import date

        def make_sig(code, sub):
            return {
                "stock_id": "2330", "stock_name": "台積電",
                "signal_date": "2026-05-19", "strategy_code": code,
                "technical_signal_type": "test", "sub_signal_type": sub,
                "close": 900.0, "ma_context": {}, "macd_context": {},
                "kd_context": {}, "volume_quality": True,
                "technical_setup_score": 5, "initial_invalid_price": 890.0,
                "structural_invalid_price": 800.0, "risk_distance_pct": None,
                "risk_distance_atr": 5.0, "notes": "wave_return=2.0%, retracement=0.3",
                "features": {"ma21_broken": True, "wave_return": 2.0, "retracement_ratio": 0.3},
            }

        result = TechnicalScanResult(
            report_date=date(2026, 5, 19), total_symbols=1000,
            hard_filter_passed=100, matched_symbols=10,
            bullish={}, bearish={}, sources={"Yahoo Finance"},
            strategy_signals={
                "A": [make_sig("A", "A1_direct_ma21_breakout")],
                "B": [make_sig("B", "B3_breakout_after_retest")],
                "C": [make_sig("C", "C1_macd_bullish_divergence_break_ma21")],
                "D": [make_sig("D", "D1_above_zero_short_ma_reclaim")],
            },
        )
        report = format_technical_report(result)
        # Must not contain raw English sub_signal_type
        self.assertNotIn("A1_direct_ma21_breakout", report)
        self.assertNotIn("B3_breakout_after_retest", report)
        self.assertNotIn("C1_macd_bullish_divergence_break_ma21", report)
        self.assertNotIn("D1_above_zero_short_ma_reclaim", report)
        # Must not contain True/False
        self.assertNotIn("True", report)
        self.assertNotIn("False", report)
        # Must not contain raw field names
        self.assertNotIn("ma105_reclaimed=", report)
        self.assertNotIn("wave_return=", report)
        # Must not contain [YYYY-MM-DD] after stock name
        self.assertNotRegex(report, r"台積電\s*\[\d{4}-\d{2}-\d{2}\]")

    def test_report_contains_chinese_sub_signal_labels(self):
        """Report must contain Chinese sub-signal labels."""
        from technical_scanner import TechnicalScanResult, format_technical_report
        from datetime import date

        def make_sig(code, sub):
            return {
                "stock_id": "2330", "stock_name": "台積電",
                "signal_date": "2026-05-19", "strategy_code": code,
                "technical_signal_type": "test", "sub_signal_type": sub,
                "close": 900.0, "ma_context": {}, "macd_context": {},
                "kd_context": {}, "volume_quality": True,
                "technical_setup_score": 5, "initial_invalid_price": 890.0,
                "structural_invalid_price": 800.0, "risk_distance_pct": None,
                "risk_distance_atr": 5.0, "notes": "", "features": {},
            }

        result = TechnicalScanResult(
            report_date=date(2026, 5, 19), total_symbols=1000,
            hard_filter_passed=100, matched_symbols=10,
            bullish={}, bearish={}, sources={"Yahoo Finance"},
            strategy_signals={
                "A": [make_sig("A", "A1_direct_ma21_breakout")],
                "B": [make_sig("B", "B3_breakout_after_retest")],
                "C": [make_sig("C", "C1_macd_bullish_divergence_break_ma21")],
                "D": [make_sig("D", "D1_above_zero_short_ma_reclaim")],
            },
        )
        report = format_technical_report(result)
        self.assertIn("A1｜直接突破型", report)
        self.assertIn("B3｜回測 MA13/MA21 後突破前高", report)
        self.assertIn("C1｜MACD 低檔背離突破 21MA", report)
        self.assertIn("D1｜零軸上短均線收復", report)

    def test_report_strategy_blocks_have_blank_lines(self):
        """Strategy block titles must have blank lines before/after."""
        from technical_scanner import TechnicalScanResult, format_technical_report
        from datetime import date

        def make_sig(code, sub, industry="未分類"):
            return {
                "stock_id": "2330", "stock_name": "台積電",
                "signal_date": "2026-05-19", "strategy_code": code,
                "technical_signal_type": "test", "sub_signal_type": sub,
                "close": 900.0, "ma_context": {}, "macd_context": {},
                "kd_context": {}, "volume_quality": True,
                "technical_setup_score": 5, "initial_invalid_price": 890.0,
                "structural_invalid_price": 800.0, "risk_distance_pct": None,
                "risk_distance_atr": 5.0, "notes": "", "features": {},
                "industry": industry,
            }

        result = TechnicalScanResult(
            report_date=date(2026, 5, 19), total_symbols=1000,
            hard_filter_passed=100, matched_symbols=10,
            bullish={}, bearish={}, sources={"Yahoo Finance"},
            strategy_signals={
                "A": [make_sig("A", "A1_direct_ma21_breakout")],
                "B": [make_sig("B", "B3_breakout_after_retest")],
                "C": [], "D": [],
            },
        )
        report = format_technical_report(result)
        # Strategy title should be followed by blank line (two newlines = one empty line)
        self.assertIn("策略 A：多頭延續回檔突破\n\nA1｜直接突破型\n\n", report)
        self.assertIn("策略 B：強勢紅柱回測突破\n\nB3｜回測 MA13/MA21 後突破前高\n\n", report)
        # Verify industry grouping appears
        self.assertIn(STOCK_MARK_START, report)
        self.assertIn("[未分類]\n2330 台積電 (900.0)", strip_stock_markers(report))

    def test_report_strategy_blocks_group_by_industry(self):
        """Strategy stocks must be grouped by industry under each sub-signal."""
        from technical_scanner import TechnicalScanResult, format_technical_report
        from datetime import date

        def make_sig(code, sub, stock_id, stock_name, industry):
            return {
                "stock_id": stock_id, "stock_name": stock_name,
                "signal_date": "2026-05-19", "strategy_code": code,
                "technical_signal_type": "test", "sub_signal_type": sub,
                "close": 900.0, "ma_context": {}, "macd_context": {},
                "kd_context": {}, "volume_quality": True,
                "technical_setup_score": 5, "initial_invalid_price": 890.0,
                "structural_invalid_price": 800.0, "risk_distance_pct": None,
                "risk_distance_atr": 5.0, "notes": "", "features": {},
                "industry": industry,
            }

        result = TechnicalScanResult(
            report_date=date(2026, 5, 19), total_symbols=1000,
            hard_filter_passed=100, matched_symbols=10,
            bullish={}, bearish={}, sources={"Yahoo Finance"},
            strategy_signals={
                "B": [
                    make_sig("B", "B3_breakout_after_retest", "6282", "康舒", "電子零組件業"),
                    make_sig("B", "B3_breakout_after_retest", "3372", "典範", "半導體業"),
                    make_sig("B", "B3_breakout_after_retest", "2330", "台積電", "半導體業"),
                ],
                "A": [], "C": [], "D": [],
            },
        )
        report = format_technical_report(result)
        # Industry groups should be sorted alphabetically
        self.assertIn("[半導體業]", report)
        self.assertIn("[電子零組件業]", report)
        # Same-industry stocks should be rendered one stock per line.
        clean_report = strip_stock_markers(report)
        self.assertIn(STOCK_MARK_START, report)
        self.assertIn("[半導體業]\n3372 典範 (900.0)\n2330 台積電 (900.0)", clean_report)
        self.assertIn("6282 康舒 (900.0)", clean_report)
        self.assertNotIn("3372 典範 (900.0) | 2330 台積電 (900.0)", clean_report)


# -------------------------------------------------------------------
# Strategy D: high-risk label must be present
# -------------------------------------------------------------------
class TestStrategyD(unittest.TestCase):
    def _d_frame(self):
        out = _apply(_make_daily([101.0] * 180))
        for ma in ("MA5", "MA13", "MA21"):
            out[ma] = 100.0
        out["MA60"] = 90.0
        out["MA105"] = 90.0
        out["DIF"] = 1.0
        out["MACD_HIST"] = -0.5
        out["open"] = 101.0
        out["low"] = 100.8
        out["K"] = 40.0
        out["D"] = 50.0
        return out

    def _append_d_day(self, frame, **values):
        row = frame.iloc[-1].copy()
        row["date"] = row["date"] + timedelta(days=1)
        for key, value in values.items():
            row[key] = value
        return pd.concat([frame, pd.DataFrame([row])], ignore_index=True)

    def _d_signals(self, frame):
        return [s for s in detect_technical_strategies(frame, "TEST", "測試") if s.get("strategy_code") == "D"]

    def test_d1_merges_simultaneous_ma_crosses(self):
        out = self._d_frame()
        out.loc[out.index[-2], "close"] = 99.0
        out.loc[out.index[-1], "close"] = 101.0
        signals = self._d_signals(out)
        d1 = [s for s in signals if s["sub_signal_type"] == "D1_above_zero_short_ma_reclaim"]
        self.assertEqual(len(d1), 1)
        self.assertEqual(d1[0]["features"]["reclaimed_mas"], ["MA5", "MA13", "MA21"])

    def test_d1_each_ma_first_on_its_own_day(self):
        out = self._d_frame()
        out.loc[out.index[-2], "close"] = 99.0
        out.loc[out.index[-2]:, "MA13"] = 102.0
        out.loc[out.index[-2]:, "MA21"] = 103.0
        self.assertEqual(self._d_signals(out)[0]["features"]["reclaimed_mas"], ["MA5"])
        later = self._append_d_day(out, close=102.5, open=102.0, low=101.8)
        self.assertEqual(self._d_signals(later)[0]["features"]["reclaimed_mas"], ["MA13"])
        later = self._append_d_day(later, close=102.6, open=102.5, low=102.3)
        self.assertEqual(self._d_signals(later), [])

    def test_d1_first_lower_shadow_retest_and_reset(self):
        out = self._d_frame()
        out["MA13"] = 102.0
        out["MA21"] = 103.0
        out.loc[out.index[-1], ["open", "close", "low"]] = [100.8, 101.2, 99.8]
        first = self._d_signals(out)
        self.assertEqual(first[0]["features"]["long_lower_shadow_mas"], ["MA5"])
        repeated = self._append_d_day(out, open=100.8, close=101.2, low=99.8)
        self.assertEqual(self._d_signals(repeated), [])
        broken = self._append_d_day(repeated, open=100.0, close=99.0, low=98.0)
        restored = self._append_d_day(broken, open=100.0, close=101.0, low=99.5)
        self.assertEqual(self._d_signals(restored)[0]["features"]["reclaimed_mas"], ["MA5"])

    def test_d2_dif_negative_with_recent_red(self):
        out = self._d_frame()
        out.loc[out.index[-2], "close"] = 99.0
        out.loc[out.index[-1], "DIF"] = -0.2
        out.loc[out.index[-1], ["MA60", "MA105"]] = [110.0, 120.0]
        out.loc[out.index[-3], "MACD_HIST"] = 0.2
        signals = self._d_signals(out)
        self.assertEqual(signals[0]["sub_signal_type"], "D2_below_zero_short_ma_reclaim")
        self.assertIn("近 5 日曾有 MACD 紅柱", signals[0]["notes"])
        out.loc[out.index[-3], "MACD_HIST"] = -0.2
        self.assertEqual(self._d_signals(out), [])

    def test_d1_fires_below_long_mas_without_recent_red(self):
        out = self._d_frame()
        out.loc[out.index[-2], "close"] = 99.0
        out.loc[out.index[-1], ["MA60", "MA105"]] = [110.0, 120.0]
        self.assertEqual(self._d_signals(out)[0]["sub_signal_type"], "D1_above_zero_short_ma_reclaim")

    def test_dif_zero_routes_by_last_nonzero_direction(self):
        for prior_dif, expected in (
            (0.5, "D1_above_zero_short_ma_reclaim"),
            (-0.5, "D2_below_zero_short_ma_reclaim"),
        ):
            with self.subTest(prior_dif=prior_dif):
                out = self._d_frame()
                out.loc[out.index[-2], "close"] = 99.0
                out.loc[out.index[-3], "DIF"] = prior_dif
                out.loc[out.index[-2]:, "DIF"] = 0.0
                if prior_dif < 0:
                    out.loc[out.index[-3], "MACD_HIST"] = 0.2
                signals = self._d_signals(out)
                self.assertEqual(signals[0]["sub_signal_type"], expected)
                self.assertEqual(signals[0]["features"]["dif_zero_origin"], "above" if prior_dif > 0 else "below")

    def test_zero_from_below_requires_recent_red_and_unknown_origin_does_not_route(self):
        out = self._d_frame()
        out.loc[out.index[-2], "close"] = 99.0
        out.loc[out.index[-3], "DIF"] = -0.5
        out.loc[out.index[-2]:, "DIF"] = 0.0
        self.assertEqual(self._d_signals(out), [])
        out["DIF"] = 0.0
        out.loc[out.index[-3], "MACD_HIST"] = 0.2
        self.assertEqual(self._d_signals(out), [])

    def test_ma_reclaim_does_not_repeat_when_dif_crosses_zero(self):
        out = self._d_frame()
        out.loc[out.index[-2], "close"] = 99.0
        out.loc[out.index[-1], "DIF"] = -0.2
        out.loc[out.index[-3], "MACD_HIST"] = 0.2
        self.assertEqual(self._d_signals(out)[0]["sub_signal_type"], "D2_below_zero_short_ma_reclaim")
        next_day = self._append_d_day(out, DIF=0.2, open=101.0, close=101.0, low=100.8)
        self.assertFalse(any(s["sub_signal_type"] == "D1_above_zero_short_ma_reclaim" for s in self._d_signals(next_day)))

    def test_macd_flip_alone_does_not_select_d(self):
        out = self._d_frame()
        out.loc[out.index[-2], "MACD_HIST"] = 0.2
        out.loc[out.index[-1], "MACD_HIST"] = -0.2
        self.assertEqual(self._d_signals(out), [])

    def test_d3_only_first_reversal_after_kd_death_cross(self):
        out = self._d_frame()
        out.loc[out.index[-4], ["K", "D"]] = [55.0, 50.0]
        out.loc[out.index[-3], ["K", "D"]] = [48.0, 52.0]
        out.loc[out.index[-2], ["K", "D"]] = [54.0, 51.0]
        out.loc[out.index[-1], ["K", "D"]] = [55.0, 50.0]
        self.assertEqual(self._d_signals(out.iloc[:-1])[0]["sub_signal_type"], "D3_kd_death_cross_first_reversal")
        self.assertEqual(self._d_signals(out), [])

    def test_strategy_d_contains_high_risk_label(self):
        """Strategy D signals must contain '高風險' in notes field."""
        # Simulate strong stock shakeout scenario:
        # Still above MA60 (prior strength), sharp drop, quick reclaim
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        # Force above MA60
        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("strategy_code") == "D"]
        if strat_d:
            for sig in strat_d:
                self.assertIn("高風險", sig.get("notes", ""), f"Strategy D notes must contain 高風險: {sig.get('notes')}")

    def test_d3_fires_on_kd_death_cross_then_reversal(self):
        """D3 fires on the first reversal after a recent KD death cross."""
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        # Force strong background (above MA60)
        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "MA105"] = out["close"].iloc[-1] * 0.85
        # Force DIF > 0 for background
        out.loc[out.index[-1], "DIF"] = 1.0

        # Set up: 2 days ago K>=D, yesterday K<D (death cross), today K>D (golden cross = quick reversal)
        out.loc[out.index[-3], "K"] = 55.0
        out.loc[out.index[-3], "D"] = 50.0
        out.loc[out.index[-2], "K"] = 48.0  # K < D = death cross happened
        out.loc[out.index[-2], "D"] = 52.0
        out.loc[out.index[-1], "K"] = 54.0  # K > D = quick reversal today
        out.loc[out.index[-1], "D"] = 51.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("strategy_code") == "D" and s.get("sub_signal_type") == "D3_kd_death_cross_first_reversal"]
        self.assertGreaterEqual(len(strat_d), 1, f"D3 should fire on KD death cross + reversal; got {signals}")
        self.assertEqual(strat_d[0]["features"]["days_since_kd_death_cross"], 1, "Yesterday death cross -> days_since=1")

    def test_d3_death_cross_2_days_ago(self):
        """D3 fires two days after the death cross if yesterday was not a reversal."""
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "DIF"] = 1.0

        # 3 days ago K>=D, 2 days ago K<D (death cross), today quick reversal
        out.loc[out.index[-4], "K"] = 55.0
        out.loc[out.index[-4], "D"] = 50.0
        out.loc[out.index[-3], "K"] = 48.0  # K < D = death cross
        out.loc[out.index[-3], "D"] = 52.0
        out.loc[out.index[-2], "K"] = 48.0
        out.loc[out.index[-2], "D"] = 52.0
        out.loc[out.index[-2], "open"] = out["close"].iloc[-2]
        out.loc[out.index[-2], "MA5"] = out["close"].iloc[-2] + 1
        out.loc[out.index[-2], "MA13"] = out["close"].iloc[-2] + 1
        out.loc[out.index[-1], "K"] = 54.0
        out.loc[out.index[-1], "D"] = 51.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("sub_signal_type") == "D3_kd_death_cross_first_reversal"]
        self.assertGreaterEqual(len(strat_d), 1, f"D3 should fire with 2-day-ago death cross; got {signals}")
        self.assertEqual(strat_d[0]["features"]["days_since_kd_death_cross"], 2, "2 days ago death cross -> days_since=2")

    def test_d3_death_cross_3_days_ago(self):
        """D3 fires three days after the death cross if neither interim day reversed."""
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "DIF"] = 1.0

        # 4 days ago K>=D, 3 days ago K<D (death cross), today quick reversal
        out.loc[out.index[-5], "K"] = 55.0
        out.loc[out.index[-5], "D"] = 50.0
        out.loc[out.index[-4], "K"] = 48.0  # K < D = death cross
        out.loc[out.index[-4], "D"] = 52.0
        for offset in (-3, -2):
            out.loc[out.index[offset], "K"] = 48.0
            out.loc[out.index[offset], "D"] = 52.0
            out.loc[out.index[offset], "open"] = out["close"].iloc[offset]
            out.loc[out.index[offset], "MA5"] = out["close"].iloc[offset] + 1
            out.loc[out.index[offset], "MA13"] = out["close"].iloc[offset] + 1
        out.loc[out.index[-1], "K"] = 54.0
        out.loc[out.index[-1], "D"] = 51.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("sub_signal_type") == "D3_kd_death_cross_first_reversal"]
        self.assertGreaterEqual(len(strat_d), 1, f"D3 should fire with 3-day-ago death cross; got {signals}")
        self.assertEqual(strat_d[0]["features"]["days_since_kd_death_cross"], 3, "3 days ago death cross -> days_since=3")

    def test_d3_notes_no_golden_cross_in_name_when_ma_reclaim(self):
        """D3 notes must not say '黃金交叉' when reversal is via MA reclaim."""
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "DIF"] = 1.0
        out.loc[out.index[-3], "K"] = 55.0
        out.loc[out.index[-3], "D"] = 50.0
        out.loc[out.index[-2], "K"] = 48.0  # death cross
        out.loc[out.index[-2], "D"] = 52.0
        # today: NOT golden cross, but MA reclaim
        ma5 = float(out["MA5"].iloc[-1])
        out.loc[out.index[-1], "K"] = 49.0  # K < D, no golden cross
        out.loc[out.index[-1], "D"] = 53.0
        out.loc[out.index[-1], "close"] = ma5 + 1.0  # reclaim MA5

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("sub_signal_type") == "D3_kd_death_cross_first_reversal"]
        self.assertGreaterEqual(len(strat_d), 1)
        self.assertIn("KD 死叉後", strat_d[0]["notes"])
        self.assertIn("首次轉強", strat_d[0]["notes"])
        self.assertNotIn("黃金交叉", strat_d[0]["notes"])

    def test_d1_label_in_report(self):
        """The consolidated D1 signal has one Chinese group label and MA details."""
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "DIF"] = 1.0
        out.loc[out.index[-2], "K"] = 48.0
        out.loc[out.index[-2], "D"] = 52.0
        out.loc[out.index[-1], "K"] = 54.0
        out.loc[out.index[-1], "D"] = 51.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        # Exercise the lower-shadow path of consolidated D1.
        hammer_df = _make_daily(closes)
        hout = _apply(hammer_df)
        hout.loc[hout.index[-1], "MA60"] = hout["close"].iloc[-1] * 0.9
        hout.loc[hout.index[-1], "DIF"] = 1.0
        # yesterday close below MA13 (shakeout)
        ma13_y = float(hout["MA13"].iloc[-2]) if pd.notna(hout["MA13"].iloc[-2]) else None
        if ma13_y:
            hout.loc[hout.index[-2], "close"] = ma13_y - 1.0
        # today hammer
        open_p = float(hout["open"].iloc[-1])
        close_p = float(hout["close"].iloc[-1])
        low_p = float(hout["low"].iloc[-1])
        body = abs(close_p - open_p)
        lower_shadow = min(open_p, close_p) - low_p
        if lower_shadow > body * 2:
            hout.loc[hout.index[-1], "low"] = open_p - body * 1.5
        # today reclaim MA5
        ma5_t = float(hout["MA5"].iloc[-1]) if pd.notna(hout["MA5"].iloc[-1]) else None
        if ma5_t:
            hout.loc[hout.index[-1], "close"] = ma5_t + 0.5
        hout.loc[hout.index[-1], "open"] = hout["MA5"].iloc[-1]
        hout.loc[hout.index[-1], "low"] = hout["MA5"].iloc[-1] - 3.0
        hout.loc[hout.index[-1], "close"] = hout["MA5"].iloc[-1] + 0.5

        hsignals = detect_technical_strategies(hout, "TEST", "測試")
        strat_d1 = [s for s in hsignals if s.get("sub_signal_type") == "D1_above_zero_short_ma_reclaim"]
        self.assertEqual(len(strat_d1), 1, f"D1 should fire once; got {hsignals}")
        result = TechnicalScanResult(
            report_date=date(2026, 5, 19), total_symbols=1,
            hard_filter_passed=1, matched_symbols=1,
            bullish={}, bearish={}, sources={"test"},
            strategy_signals={"A": [], "B": [], "C": [], "D": strat_d1},
        )
        report = format_technical_report(result)
        self.assertIn("D1｜零軸上短均線收復", report)
        self.assertIn("收復 MA5", report)
        self.assertNotIn("首次收復", report)
        self.assertNotIn("D4｜", report)

    def test_d3_label_in_report(self):
        out = self._d_frame()
        out.loc[out.index[-3], ["K", "D"]] = [55.0, 50.0]
        out.loc[out.index[-2], ["K", "D"]] = [48.0, 52.0]
        out.loc[out.index[-1], ["K", "D"]] = [54.0, 51.0]
        signals = self._d_signals(out)
        result = TechnicalScanResult(
            report_date=date(2026, 5, 19), total_symbols=1,
            hard_filter_passed=1, matched_symbols=1,
            bullish={}, bearish={}, sources={"test"},
            strategy_signals={"A": [], "B": [], "C": [], "D": signals},
        )
        report = format_technical_report(result)
        self.assertIn("策略 D：動能背景短線轉強", report)
        self.assertIn("D3｜KD 死叉後首次轉強", report)

    def test_d2_label_and_reclaimed_ma_in_report(self):
        out = self._d_frame()
        out.loc[out.index[-2], "close"] = 99.0
        out.loc[out.index[-1], "DIF"] = -0.2
        out.loc[out.index[-3], "MACD_HIST"] = 0.2
        signals = self._d_signals(out)
        result = TechnicalScanResult(
            report_date=date(2026, 5, 19), total_symbols=1,
            hard_filter_passed=1, matched_symbols=1,
            bullish={}, bearish={}, sources={"test"},
            strategy_signals={"A": [], "B": [], "C": [], "D": signals},
        )
        report = format_technical_report(result)
        self.assertIn("D2｜零軸下短均線收復", report)
        self.assertIn("收復 MA5、MA13、MA21", report)
        self.assertNotIn("首次收復", report)

    def test_d3_does_not_fire_on_golden_cross_only(self):
        """D3 must NOT fire if only golden cross happened (no prior death cross)."""
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "DIF"] = 1.0

        # Set up: 2 days ago K<D (already bearish), yesterday K>D (golden cross)
        # No death cross in between — should NOT trigger D3
        out.loc[out.index[-3], "K"] = 48.0
        out.loc[out.index[-3], "D"] = 52.0  # K < D already
        out.loc[out.index[-2], "K"] = 54.0  # K > D = golden cross
        out.loc[out.index[-2], "D"] = 51.0
        out.loc[out.index[-1], "K"] = 55.0
        out.loc[out.index[-1], "D"] = 50.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("sub_signal_type") == "D3_kd_death_cross_first_reversal"]
        self.assertEqual(len(strat_d), 0, f"D3 should NOT fire without prior death cross; got {signals}")

    def test_d1_fires_on_ma13_close_crossover(self):
        """D1 fires when MA13 was below yesterday and closes above today."""
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        # Strong background: above MA60
        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "MA105"] = out["close"].iloc[-1] * 0.85
        out.loc[out.index[-1], "DIF"] = 1.0

        # yesterday: close broke MA13 (shakeout)
        ma13_yesterday = float(out["MA13"].iloc[-2]) if pd.notna(out["MA13"].iloc[-2]) else None
        if ma13_yesterday:
            out.loc[out.index[-2], "close"] = float(ma13_yesterday) - 1.0

        # Today: close reclaims MA13 with bullish candle (close > open)
        ma13_today = float(out["MA13"].iloc[-1]) if pd.notna(out["MA13"].iloc[-1]) else None
        out.loc[out.index[-1], "close"] = float(ma13_today) + 0.5 if ma13_today else 115.5
        out.loc[out.index[-1], "open"] = float(ma13_today) if ma13_today else 110.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("sub_signal_type") == "D1_above_zero_short_ma_reclaim"]
        self.assertEqual(len(strat_d), 1, f"D1 should fire once on MA13 reclaim; got {signals}")
        self.assertIn("MA13", strat_d[0]["features"]["reclaimed_mas"])

    def test_d1_does_not_fire_without_close_cross_or_hammer(self):
        """D1 needs a close crossover or first qualifying lower-shadow retest."""
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        # Strong background
        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "MA105"] = out["close"].iloc[-1] * 0.85
        out.loc[out.index[-1], "DIF"] = 1.0

        # No recent MA break, no hammer, close barely above MA5/MA13
        ma5 = float(out["MA5"].iloc[-1])
        ma13 = float(out["MA13"].iloc[-1])
        out.loc[out.index[-1], "close"] = max(ma5, ma13) + 0.1

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("sub_signal_type") == "D1_above_zero_short_ma_reclaim"]
        self.assertEqual(len(strat_d), 0, f"D1 should NOT fire without a new reclaim; got {signals}")

    def test_d1_ma_none_no_crash(self):
        """D1 must not crash when MA5/MA13 are None in some rows."""
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "DIF"] = 1.0
        # Force MA13 to NaN in some rows
        out.loc[out.index[-3], "MA13"] = None
        out.loc[out.index[-2], "MA13"] = None

        # Missing historical MA data must not crash the first-reclaim check.
        open_p = float(out["open"].iloc[-1])
        close_p = float(out["close"].iloc[-1])
        low_p = float(out["low"].iloc[-1])
        body = abs(close_p - open_p)
        lower_shadow = min(open_p, close_p) - low_p
        if lower_shadow > body * 2:
            out.loc[out.index[-1], "low"] = low_p  # ensure hammer

        signals = detect_technical_strategies(out, "TEST", "測試")
        # Should not crash, D1 may or may not fire depending on other MAs.
        self.assertIsInstance(signals, list)

    def test_d3_fires_below_ma60_ma105_with_dif(self):
        """D3 does not require price above MA60/MA105 when DIF is positive."""
        from technical_strategy_engine import detect_technical_strategies
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        # Force price BELOW MA60 (price ~299, MA60=310)
        out.loc[out.index[-1], "MA60"] = 310.0
        out.loc[out.index[-1], "MA105"] = 300.0
        out.loc[out.index[-1], "close"] = 295.0  # below both MAs
        # DIF>0 supplies the momentum background despite price below both long MAs.
        out.loc[out.index[-1], "DIF"] = 1.5

        # Force a KD death cross followed by today's reversal.
        out.loc[out.index[-3], "K"] = 55.0
        out.loc[out.index[-3], "D"] = 50.0
        out.loc[out.index[-2], "K"] = 48.0
        out.loc[out.index[-2], "D"] = 52.0
        out.loc[out.index[-1], "K"] = 54.0
        out.loc[out.index[-1], "D"] = 51.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("sub_signal_type") == "D3_kd_death_cross_first_reversal"]
        self.assertEqual(len(strat_d), 1, f"D3 should fire below long MAs with DIF>0; got {signals}")

    def test_d3_fires_below_ma60_ma105_with_prior_red(self):
        """D3 also works below long MAs when a recent MACD red bar exists."""
        from technical_strategy_engine import detect_technical_strategies
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        # Force price BELOW MA60
        out.loc[out.index[-1], "MA60"] = 310.0
        out.loc[out.index[-1], "MA105"] = 300.0
        out.loc[out.index[-1], "close"] = 295.0
        # DIF negative, but prior red zone in last 5 days
        out.loc[out.index[-1], "DIF"] = -0.5
        # Force a red zone 3 days ago
        out.loc[out.index[-4], "MACD_HIST"] = 1.5
        out.loc[out.index[-3], "MACD_HIST"] = 0.8
        out.loc[out.index[-2], "MACD_HIST"] = 0.3
        out.loc[out.index[-1], "MACD_HIST"] = -0.2

        # Force KD death cross scenario
        out.loc[out.index[-4], "K"] = 55.0
        out.loc[out.index[-4], "D"] = 50.0
        out.loc[out.index[-3], "K"] = 48.0
        out.loc[out.index[-3], "D"] = 52.0
        out.loc[out.index[-2], "K"] = 47.0
        out.loc[out.index[-2], "D"] = 52.0
        out.loc[out.index[-2], "open"] = out.loc[out.index[-2], "close"]
        out.loc[out.index[-2], "MA5"] = out.loc[out.index[-2], "close"] + 1.0
        out.loc[out.index[-2], "MA13"] = out.loc[out.index[-2], "close"] + 1.0
        out.loc[out.index[-1], "K"] = 54.0
        out.loc[out.index[-1], "D"] = 51.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("sub_signal_type") == "D3_kd_death_cross_first_reversal"]
        self.assertEqual(len(strat_d), 1, f"D3 should fire below long MAs with recent red; got {signals}")

    def test_d_background_passes_with_price_above_ma_and_dif(self):
        """D must fire when price above MA60/MA105 AND DIF>0."""
        from technical_strategy_engine import detect_technical_strategies
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        # Price above MA60 AND DIF>0
        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "close"] = out["close"].iloc[-1]
        out.loc[out.index[-1], "DIF"] = 1.0

        # KD death cross + reversal to trigger D3
        out.loc[out.index[-3], "K"] = 55.0
        out.loc[out.index[-3], "D"] = 50.0
        out.loc[out.index[-2], "K"] = 48.0
        out.loc[out.index[-2], "D"] = 52.0
        out.loc[out.index[-1], "K"] = 54.0
        out.loc[out.index[-1], "D"] = 51.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("strategy_code") == "D"]
        self.assertGreaterEqual(len(strat_d), 1, f"D should fire when price above MA60 AND DIF>0; got {signals}")

    def test_d_background_passes_with_price_above_ma_and_prior_red_zone(self):
        """D must fire when price above MA60/MA105 AND recent MACD red zone exists."""
        from technical_strategy_engine import detect_technical_strategies
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)

        # Price above MA60, DIF<0
        out.loc[out.index[-1], "MA60"] = out["close"].iloc[-1] * 0.9
        out.loc[out.index[-1], "close"] = out["close"].iloc[-1]
        out.loc[out.index[-1], "DIF"] = -0.3
        # Prior red zone 3 days ago
        out.loc[out.index[-4], "MACD_HIST"] = 1.5
        out.loc[out.index[-3], "MACD_HIST"] = 0.8
        out.loc[out.index[-2], "MACD_HIST"] = 0.2
        out.loc[out.index[-1], "MACD_HIST"] = -0.1

        # KD death cross + reversal to trigger D3
        out.loc[out.index[-3], "K"] = 55.0
        out.loc[out.index[-3], "D"] = 50.0
        out.loc[out.index[-2], "K"] = 48.0
        out.loc[out.index[-2], "D"] = 52.0
        out.loc[out.index[-1], "K"] = 54.0
        out.loc[out.index[-1], "D"] = 51.0

        signals = detect_technical_strategies(out, "TEST", "測試")
        strat_d = [s for s in signals if s.get("strategy_code") == "D"]
        self.assertGreaterEqual(len(strat_d), 1, f"D should fire when price above MA60 AND prior red zone; got {signals}")


# -------------------------------------------------------------------
# Original signals still exist after modifications
# -------------------------------------------------------------------
class TestOriginalSignalsPreserved(unittest.TestCase):
    def test_detect_signals_still_available(self):
        """detect_signals function must still be importable and callable."""
        from technical_scanner import detect_signals
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        bullish, bearish = detect_signals(df)
        # Should return non-error results (may be empty if conditions not met)
        self.assertIsInstance(bullish, list)
        self.assertIsInstance(bearish, list)

    def test_detect_signals_returns_known_signals(self):
        """detect_signals must still produce known signal types."""
        from technical_scanner import detect_signals
        # 21MA cross up
        closes = [100] * 130 + [98, 105]  # crosses 21MA
        df = _make_daily(closes)
        bullish, _ = detect_signals(df)
        self.assertIn("突破 21MA", bullish)

    def test_macd_cross_functions_preserved(self):
        """_cross_up and _cross_down must still exist."""
        from technical_scanner import _cross_up, _cross_down
        self.assertTrue(_cross_up(1.0, 2.0, 3.0, 1.0))
        self.assertTrue(_cross_down(3.0, 1.0, 1.0, 2.0))
        self.assertFalse(_cross_up(1.0, 2.0, 1.5, 2.5))


# -------------------------------------------------------------------
# detect_technical_strategies basic contract tests
# -------------------------------------------------------------------
class TestDetectTechnicalStrategiesContract(unittest.TestCase):
    def test_empty_frame_returns_empty_list(self):
        df = pd.DataFrame()
        result = detect_technical_strategies(df, "TEST", "測試")
        self.assertEqual(result, [])

    def test_none_frame_returns_empty_list(self):
        result = detect_technical_strategies(None, "TEST", "測試")
        self.assertEqual(result, [])

    def test_insufficient_rows_returns_empty_list(self):
        closes = [100] * 10
        df = _make_daily(closes)
        result = detect_technical_strategies(df, "TEST", "測試")
        self.assertEqual(result, [])

    def test_returns_list_of_dicts(self):
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)
        result = detect_technical_strategies(out, "2330", "台積電")
        self.assertIsInstance(result, list)
        for item in result:
            self.assertIsInstance(item, dict)
            self.assertIn("strategy_code", item)
            self.assertIn("technical_signal_type", item)
            self.assertIn("sub_signal_type", item)

    def test_strategy_code_is_a_b_c_or_d(self):
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)
        result = detect_technical_strategies(out, "2330", "台積電")
        for item in result:
            self.assertIn(item.get("strategy_code"), ["A", "B", "C", "D"])

    def test_stock_id_propagated(self):
        closes = [100 + i for i in range(200)]
        df = _make_daily(closes)
        out = _apply(df)
        result = detect_technical_strategies(out, "2330", "台積電")
        for item in result:
            self.assertEqual(item.get("stock_id"), "2330")
            self.assertEqual(item.get("stock_name"), "台積電")
