from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

import curated_scan_service as curated
import stock_ai_bot.monitoring.radar_service as radar_service
from stock_ai_bot.telegram.telegram_stock_formatting import STOCK_MARK_START, strip_stock_markers


def test_curated_scan_scores_and_sorts_without_changing_candidates(monkeypatch):
    financial_report = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                code="1111",
                name="高分股",
                symbol="1111.TW",
                industry="電子零組件業",
                price=20.0,
                avg_volume_20d=1000.0,
                latest_monthly_revenue=200_000_000,
                revenue_group="group_1",
                gross_margin_rating="B",
                revenue_history=[
                    SimpleNamespace(month="2026-02", revenue=100_000_000, yoy=5.0),
                    SimpleNamespace(month="2026-03", revenue=200_000_000, yoy=18.0),
                ],
            ),
            SimpleNamespace(
                code="2222",
                name="低分股",
                symbol="2222.TW",
                industry="電子零組件業",
                price=30.0,
                avg_volume_20d=900.0,
                latest_monthly_revenue=150_000_000,
                revenue_group="group_2",
                gross_margin_rating="C",
                revenue_history=[
                    SimpleNamespace(month="2026-02", revenue=120_000_000, yoy=-2.0),
                    SimpleNamespace(month="2026-03", revenue=150_000_000, yoy=8.0),
                ],
            ),
        ]
    )
    chip_context = SimpleNamespace(
        candidates=pd.DataFrame(
            [
                {
                    "code": "1111",
                    "name": "高分股",
                    "symbol": "1111.TW",
                    "industry": "電子零組件業",
                    "price": 20.0,
                    "avg_volume_20d": 1000.0,
                    "monthly_revenue": 200_000_000,
                },
                {
                    "code": "2222",
                    "name": "低分股",
                    "symbol": "2222.TW",
                    "industry": "電子零組件業",
                    "price": 30.0,
                    "avg_volume_20d": 900.0,
                    "monthly_revenue": 150_000_000,
                },
            ]
        ),
        scan_settings={},
    )
    technical_result = SimpleNamespace(
        bullish={"突破 21MA": {"電子零組件業": ["2222 低分股 (30)", "1111 高分股 (20)"]}},
        hard_filter_passed=2,
        matched_symbols=2,
        sources={"cache"},
    )

    def fake_score(candidates, analysis_date, *, scoring_version=None, reason_limit=4, risk_limit=3):
        for item in candidates:
            assert item.revenue_history
            if item.code == "1111":
                item.total_score = 80
                item.score_components = {"technical": 25, "revenue": 18, "financial": 10, "chip": 12, "theme": 10, "sector": 5}
                item.key_reasons = ["突破 21MA", "營收轉強"]
                item.risk_flags = ["財報資料缺漏"]
            else:
                item.total_score = 50
                item.score_components = {"technical": 20, "revenue": 10, "financial": 5, "chip": 8, "theme": 5, "sector": 2}
                item.key_reasons = ["突破 21MA"]
                item.risk_flags = []
        return candidates

    monkeypatch.setattr(curated, "scan_tw_market", lambda *args, **kwargs: financial_report)
    monkeypatch.setattr(curated, "build_market_context", lambda *args, **kwargs: chip_context)
    monkeypatch.setattr(
        curated,
        "build_chip_grade_maps",
        lambda *args, **kwargs: {"chip_1": {"1111": "B", "2222": "B"}, "chip_2": {"2222": "A"}},
    )
    monkeypatch.setattr(curated.ts, "run_technical_scan", lambda *args, **kwargs: technical_result)
    monkeypatch.setattr(radar_service, "prepare_radar_scoring_data", lambda *args, **kwargs: None)
    monkeypatch.setattr(radar_service, "score_radar_candidates", fake_score)

    result = curated.build_curated_scan_result({}, date(2026, 5, 22))

    assert set(result.selected_codes) == {"1111", "2222"}
    assert result.selected_by_signal["突破 21MA"] == ["1111", "2222"]
    assert result.scores["1111"]["total_score"] == 80
    assert "📂 技術訊號精選｜共 2 檔" in result.report_text
    assert STOCK_MARK_START in result.report_text
    clean_report = strip_stock_markers(result.report_text)
    assert "1111 高分股 | 80分（技術25/營收18/財報10/籌碼12/題材10/族群5）" in clean_report
    assert "2222 低分股 | 50分（技術20/營收10/財報5/籌碼8/題材5/族群2）" in clean_report
    assert result.report_text.count("1111") == 1
    assert result.report_text.count("2222") == 1
    assert "  訊號：突破 21MA" in result.report_text
    assert "  加分：突破 21MA、營收轉強" in result.report_text
    assert "  風險/缺口：財報資料缺漏" in result.report_text
    assert "【命中" not in result.report_text
    for forbidden in ("True", "False", "sub_signal_type", "technical_signal_type"):
        assert forbidden not in result.report_text


def test_curated_scan_backfills_revenue_history_for_chip_only_selected_candidates(monkeypatch):
    financial_report = SimpleNamespace(candidates=[])
    chip_context = SimpleNamespace(
        candidates=pd.DataFrame(
            [
                {
                    "code": "3333",
                    "name": "補營收",
                    "symbol": "3333.TW",
                    "industry": "電子業",
                    "price": 25.0,
                    "avg_volume_20d": 1200.0,
                    "monthly_revenue": 100_000_000,
                },
            ]
        ),
        scan_settings={},
    )
    technical_result = SimpleNamespace(
        bullish={"突破 21MA": {"電子業": ["3333 補營收 (25)"]}},
        hard_filter_passed=1,
        matched_symbols=1,
        sources={"cache"},
    )
    captured_history: dict[str, list[dict[str, object]]] = {}
    captured_revenue_entries = []

    def fake_score(candidates, analysis_date, *, scoring_version=None, reason_limit=4, risk_limit=3):
        for item in candidates:
            captured_history[item.code] = list(item.revenue_history)
            item.total_score = 60
            item.score_components = {"technical": 20, "revenue": 12, "financial": 5, "chip": 10, "theme": 8, "sector": 5}
            item.key_reasons = ["營收資料已補齊"]
            item.risk_flags = []
        return candidates

    monkeypatch.setattr(curated, "scan_tw_market", lambda *args, **kwargs: financial_report)
    monkeypatch.setattr(curated, "build_market_context", lambda *args, **kwargs: chip_context)
    monkeypatch.setattr(curated, "build_chip_grade_maps", lambda *args, **kwargs: {"chip_1": {"3333": "B"}, "chip_2": {"3333": "B"}})
    monkeypatch.setattr(curated.ts, "run_technical_scan", lambda *args, **kwargs: technical_result)
    def fake_load_recent_revenue_history(entries, **kwargs):
        captured_revenue_entries.extend(entries)
        return {
            "3333": [
                SimpleNamespace(month="2026-02", revenue=80_000_000, yoy=-5.0),
                SimpleNamespace(month="2026-03", revenue=100_000_000, yoy=15.0),
            ]
        }

    monkeypatch.setattr(curated, "load_recent_revenue_history", fake_load_recent_revenue_history)
    monkeypatch.setattr(radar_service, "prepare_radar_scoring_data", lambda *args, **kwargs: None)
    monkeypatch.setattr(radar_service, "score_radar_candidates", fake_score)

    result = curated.build_curated_scan_result({}, date(2026, 5, 22))

    assert result.selected_codes == ["3333"]
    assert captured_history["3333"] == [
        {"month": "2026-02", "revenue": 80_000_000.0, "yoy": -5.0},
        {"month": "2026-03", "revenue": 100_000_000.0, "yoy": 15.0},
    ]
    assert captured_revenue_entries[0].market == "TWSE"
    assert result.scores["3333"]["components"]["revenue"] == 12


def test_curated_scan_uses_actual_ma_signal_codes(monkeypatch):
    financial_report = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                code="4444",
                name="均線股",
                symbol="4444.TW",
                industry="電子業",
                price=44.0,
                avg_volume_20d=1000.0,
                latest_monthly_revenue=100_000_000,
                revenue_group="group_1",
                gross_margin_rating="B",
                revenue_history=[SimpleNamespace(month="2026-05", revenue=100_000_000, yoy=20.0)],
            )
        ]
    )
    chip_context = SimpleNamespace(
        candidates=pd.DataFrame(
            [
                {
                    "code": "4444",
                    "name": "均線股",
                    "symbol": "4444.TW",
                    "industry": "電子業",
                    "price": 44.0,
                    "avg_volume_20d": 1000.0,
                    "monthly_revenue": 100_000_000,
                }
            ]
        ),
        scan_settings={},
    )
    technical_result = SimpleNamespace(
        bullish={
            curated.ts.MA_BREAKOUT_SIGNAL_LABELS[5]: {"電子業": ["4444 均線股 (44)"]},
            curated.ts.MA_RECLAIM_SIGNAL_LABELS[144]: {"電子業": ["4444 均線股 (44)"]},
        },
        hard_filter_passed=1,
        matched_symbols=1,
        sources={"cache"},
    )
    captured_signals: dict[str, list[dict[str, object]]] = {}

    def fake_score(candidates, analysis_date, *, scoring_version=None, reason_limit=4, risk_limit=3):
        for item in candidates:
            captured_signals[item.code] = list(item.technical_signals)
            item.total_score = 70
            item.score_components = {"technical": 25, "revenue": 15, "financial": 8, "chip": 12, "theme": 7, "sector": 3}
            item.key_reasons = ["突破均線"]
            item.risk_flags = []
        return candidates

    monkeypatch.setattr(curated, "scan_tw_market", lambda *args, **kwargs: financial_report)
    monkeypatch.setattr(curated, "build_market_context", lambda *args, **kwargs: chip_context)
    monkeypatch.setattr(curated, "build_chip_grade_maps", lambda *args, **kwargs: {"chip_1": {"4444": "B"}})
    monkeypatch.setattr(curated.ts, "run_technical_scan", lambda *args, **kwargs: technical_result)
    monkeypatch.setattr(radar_service, "prepare_radar_scoring_data", lambda *args, **kwargs: None)
    monkeypatch.setattr(radar_service, "score_radar_candidates", fake_score)

    result = curated.build_curated_scan_result({}, date(2026, 6, 30))

    assert result.selected_codes == ["4444"]
    assert result.selected_by_signal[curated.ts.MA_BREAKOUT_SIGNAL_LABELS[5]] == ["4444"]
    assert result.selected_by_signal[curated.ts.MA_RECLAIM_SIGNAL_LABELS[144]] == ["4444"]
    assert {signal["strategy_code"] for signal in captured_signals["4444"]} == {"MA5", "MA144"}
    assert result.report_text.count("4444") == 1
    assert "📂 技術訊號精選｜共 1 檔" in result.report_text
    assert "  訊號：突破 5MA、跌破後收復 144MA" in result.report_text
    assert "【命中" not in result.report_text
    assert "📂 突破 5MA" not in result.report_text
    assert "📂 跌破後收復 144MA" not in result.report_text
