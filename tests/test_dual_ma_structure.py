from datetime import date

import pandas as pd

import technical_scanner as scanner
from telegram_stock_formatting import strip_stock_markers


def _frame(*, yesterday=9.5, today=11.0, today_low=10.8, ma5=10.0, ma21=9.0, ma105=20.0, ma144=22.0):
    closes = [11.0] * 148 + [yesterday, today]
    lows = [close - 0.2 for close in closes]
    lows[-1] = today_low
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2026-01-01", periods=150),
            "close": closes,
            "low": lows,
            "MA5": [ma5] * 150,
            "MA21": [ma21] * 150,
            "MA105": [ma105] * 150,
            "MA144": [ma144] * 150,
        }
    )


def test_ma5_structure_uses_price_and_reports_long_ma_position():
    frame = _frame(ma105=10.5, ma144=12.0)
    signals = scanner.detect_dual_ma_structure(frame, "2330", "台積電")

    assert len(signals) == 1
    assert signals[0]["primary_group"] == "ma5_ma21"
    assert signals[0]["long_position"] == "mixed"
    assert signals[0]["matched_target_mas"] == [5]
    assert "高於 MA105、低於 MA144" == signals[0]["long_relation"]
    assert signals[0]["signal_date"] == frame.iloc[-1]["date"].date().isoformat()


def test_ma21_alignment_requires_only_one_long_ma_and_has_priority():
    frame = _frame(ma5=10.5, ma21=10.0, ma105=9.0, ma144=12.0)
    signals = scanner.detect_dual_ma_structure(frame, "2330", "台積電")

    assert len(signals) == 1
    assert signals[0]["primary_group"] == "ma21_long"
    assert signals[0]["matched_target_mas"] == [5, 21]
    assert signals[0]["aligned_long_mas"] == [105]
    assert "同步命中 MA5" in scanner.dual_ma_signal_label(signals[0])


def test_first_retest_only_once_until_close_falls_below_same_ma():
    frame = _frame(yesterday=11, today=11, today_low=9.8)
    frame.loc[147, "close"] = 9.5
    assert scanner.detect_dual_ma_structure(frame, "2330", "台積電") == []

    frame.loc[148, "close"] = 9.5
    assert scanner.detect_dual_ma_structure(frame, "2330", "台積電")


def test_first_intraday_retest_in_existing_above_episode_can_fire_once():
    frame = _frame(yesterday=11, today=11, today_low=9.8)
    assert scanner.detect_dual_ma_structure(frame, "2330", "台積電")
    frame.loc[147, "low"] = 9.8
    assert scanner.detect_dual_ma_structure(frame, "2330", "台積電") == []


def test_missing_long_ma_is_not_imputed_and_no_future_data_is_used():
    frame = _frame(ma5=8.0, ma21=10.0, ma105=9.0, ma144=float("nan"))
    assert scanner.detect_dual_ma_structure(frame, "2330", "台積電")[0]["primary_group"] == "ma21_long"
    frame["MA105"] = float("nan")
    assert scanner.detect_dual_ma_structure(frame, "2330", "台積電") == []
    frame["MA105"] = 9.0
    old_signal = scanner.detect_dual_ma_structure(frame, "2330", "台積電")
    future = frame.iloc[-1:].copy()
    future["date"] = pd.Timestamp("2026-12-31")
    future["close"] = 30.0
    assert scanner.detect_dual_ma_structure(pd.concat([frame, future]).iloc[:-1], "2330", "台積電") == old_signal


def test_technical_scan_and_reports_include_dual_ma_only_candidate(monkeypatch):
    frame = _frame()
    candidate = scanner.TechnicalCandidate("2330", "2330.TW", "TWSE", "台積電", "半導體業", 11.0, 1000.0, 50_000_000.0)
    monkeypatch.setattr(scanner, "build_hard_filter_candidates", lambda *args, **kwargs: ([candidate], 1))
    monkeypatch.setattr(scanner, "fetch_daily_history", lambda *args, **kwargs: (frame, "本機快取"))
    monkeypatch.setattr(scanner, "detect_signals", lambda *_: ([], []))
    monkeypatch.setattr(scanner, "apply_indicators", lambda history: history)
    monkeypatch.setattr(scanner, "detect_technical_strategies", lambda *args: [])

    result = scanner.run_technical_scan({}, date(2026, 7, 29))
    assert result.matched_symbols == 1
    assert result.dual_ma_signals[0]["industry"] == "半導體業"
    assert scanner.collect_technical_selected_codes(result) == ["2330"]
    report = strip_stock_markers(scanner.format_technical_report(result))
    assert "MACD 動能策略（A–D）" in report
    assert "雙均線結構策略" in report
    assert "MA5 > MA21｜長均線下方" in report
    assert "[半導體業]" in report
    messages = [strip_stock_markers(message) for message in scanner.format_technical_report_messages(result)]
    assert len(messages) == 2
    assert "雙均線結構策略" in messages[0]
    assert messages[0].count("2330 台積電") == 1
    assert messages[-1].startswith("📂 技術選股去重彙整")
    assert messages[-1].count("2330 台積電") == 1
