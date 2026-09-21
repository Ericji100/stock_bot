from __future__ import annotations

from datetime import date

import pandas as pd

import technical_scanner as scanner


def _frame(rows: int = 20) -> pd.DataFrame:
    dates = pd.date_range("2026-08-01", periods=rows, freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "open": [99.0] * rows,
            "high": [101.0] * rows,
            "low": [98.5] * rows,
            "close": [99.0] * rows,
            "MA5": [100.0] * rows,
            "MA21": [120.0] * rows,
            "K": [20.0] * rows,
            "D": [30.0] * rows,
        }
    )


def _set_golden_cross(frame: pd.DataFrame, days_ago: int) -> None:
    index = len(frame) - 1 - days_ago
    frame.loc[index - 1, ["K", "D"]] = [20.0, 30.0]
    frame.loc[index, ["K", "D"]] = [40.0, 30.0]


def test_k1_accepts_cross_one_to_eight_days_ago_even_after_death_cross():
    for days_ago in (1, 4, 8):
        frame = _frame()
        _set_golden_cross(frame, days_ago)
        frame.loc[frame.index[-1], ["close", "K", "D"]] = [101.0, 15.0, 35.0]

        signals = scanner.detect_kd_ma_strategy(frame, "2330", "台積電")

        assert len(signals) == 1
        assert signals[0]["primary_group"] == "K1"
        assert signals[0]["recent_golden_cross_days"] == days_ago
        assert signals[0]["k"] < signals[0]["d"]


def test_k1_rejects_cross_outside_eight_day_window():
    frame = _frame()
    _set_golden_cross(frame, 9)
    frame.loc[frame.index[-1], "close"] = 101.0

    assert scanner.detect_kd_ma_strategy(frame, "2330", "台積電") == []


def test_k1_does_not_treat_signal_day_golden_cross_as_prior_cross():
    frame = _frame()
    frame.loc[frame.index[-2], ["K", "D"]] = [20.0, 30.0]
    frame.loc[frame.index[-1], ["close", "K", "D"]] = [101.0, 40.0, 30.0]

    assert scanner.detect_kd_ma_strategy(frame, "2330", "台積電") == []


def test_k2_requires_k_above_d_and_ma21_trigger():
    frame = _frame()
    frame["MA5"] = 80.0
    frame["MA21"] = 100.0
    frame.loc[frame.index[-1], ["close", "K", "D"]] = [101.0, 40.0, 30.0]

    signals = scanner.detect_kd_ma_strategy(frame, "2330", "台積電")

    assert len(signals) == 1
    assert signals[0]["primary_group"] == "K2"
    assert signals[0]["matched_groups"] == ["K2"]
    frame.loc[frame.index[-1], ["K", "D"]] = [20.0, 30.0]
    assert scanner.detect_kd_ma_strategy(frame, "2330", "台積電") == []


def test_k2_accepts_first_intraday_ma21_reclaim_without_close_cross():
    frame = _frame()
    frame["MA5"] = 80.0
    frame["MA21"] = 100.0
    frame["close"] = 101.0
    frame["low"] = 100.5
    frame.loc[frame.index[-1], ["low", "K", "D"]] = [99.0, 40.0, 30.0]

    signals = scanner.detect_kd_ma_strategy(frame, "2330", "台積電")

    assert len(signals) == 1
    assert signals[0]["primary_group"] == "K2"
    assert signals[0]["triggers"]["MA21"] == ["跌破後收復"]


def test_same_day_k1_and_k2_are_merged_under_k2():
    frame = _frame()
    _set_golden_cross(frame, 3)
    frame["MA21"] = 100.0
    frame.loc[frame.index[-1], ["close", "K", "D"]] = [101.0, 45.0, 35.0]

    signals = scanner.detect_kd_ma_strategy(frame, "2330", "台積電")

    assert len(signals) == 1
    assert signals[0]["primary_group"] == "K2"
    assert signals[0]["matched_groups"] == ["K1", "K2"]
    assert signals[0]["matched_target_mas"] == [5, 21]


def test_intraday_reclaim_does_not_repeat_during_same_above_ma_episode():
    frame = _frame()
    _set_golden_cross(frame, 2)
    frame.loc[frame.index[-3], "close"] = 99.0
    frame.loc[frame.index[-2], ["close", "low"]] = [101.0, 100.5]
    frame.loc[frame.index[-1], ["close", "low"]] = [101.0, 99.0]

    assert scanner.detect_kd_ma_strategy(frame, "2330", "台積電") == []


def test_report_groups_kd_strategy_by_industry_and_describes_overlap():
    frame = _frame()
    _set_golden_cross(frame, 3)
    frame["MA21"] = 100.0
    frame.loc[frame.index[-1], ["close", "K", "D"]] = [101.0, 45.0, 35.0]
    signal = scanner.detect_kd_ma_strategy(frame, "2330", "台積電")[0]
    signal["industry"] = "半導體業"
    result = scanner.TechnicalScanResult(
        report_date=date(2026, 8, 20),
        total_symbols=1,
        hard_filter_passed=1,
        matched_symbols=1,
        bullish={},
        bearish={},
        sources={"test"},
        strategy_signals={"A": [], "B": [], "C": [], "D": []},
        kd_ma_signals=[signal],
    )

    report = scanner.format_technical_report(result)

    assert "【KD 動能均線策略】" in report
    assert "K2｜KD 多方排列突破或收復 MA21" in report
    assert "[半導體業]" in report
    assert "同步符合 K1（金叉後 3 日）" in report
    assert "* KD 動能均線策略：1 檔" in report


def test_kd_signal_is_included_in_selected_codes():
    result = scanner.TechnicalScanResult(
        report_date=date(2026, 8, 20),
        total_symbols=1,
        hard_filter_passed=1,
        matched_symbols=1,
        bullish={},
        bearish={},
        sources={"test"},
        kd_ma_signals=[{"stock_id": "2330"}],
    )

    assert scanner.collect_technical_selected_codes(result) == ["2330"]


def test_technical_scan_and_messages_include_kd_only_candidate(monkeypatch):
    frame = _frame(150)
    _set_golden_cross(frame, 3)
    frame.loc[frame.index[-1], "close"] = 101.0
    candidate = scanner.TechnicalCandidate(
        "2330", "2330.TW", "TWSE", "台積電", "半導體業", 101.0, 1000.0, 50_000_000.0
    )
    monkeypatch.setattr(scanner, "build_hard_filter_candidates", lambda *args, **kwargs: ([candidate], 1))
    monkeypatch.setattr(scanner, "fetch_daily_history", lambda *args, **kwargs: (frame, "本機快取"))
    monkeypatch.setattr(scanner, "detect_signals", lambda *_: ([], []))
    monkeypatch.setattr(scanner, "apply_indicators", lambda history: history)
    monkeypatch.setattr(scanner, "detect_technical_strategies", lambda *args: [])
    monkeypatch.setattr(scanner, "detect_dual_ma_structure", lambda *args: [])

    result = scanner.run_technical_scan({}, date(2026, 8, 20))

    assert result.matched_symbols == 1
    assert result.kd_ma_signals[0]["industry"] == "半導體業"
    messages = scanner.format_technical_report_messages(result)
    assert len(messages) == 2
    assert "KD 動能均線策略" in messages[0]
    assert "K1｜KD 金叉後 MA5 轉強" in messages[0]
    assert messages[-1].startswith("📂 技術選股去重彙整")
    assert "K1｜KD 金叉後 MA5 轉強" in messages[-1]
