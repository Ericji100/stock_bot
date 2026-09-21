from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

import scheduled_all_scan_prepare_service as prepare


def test_prepare_scheduled_all_scan_data_reuses_existing_warmup_functions(monkeypatch):
    calls: list[str] = []
    target_date = date(2026, 6, 24)
    universe = [SimpleNamespace(code="2330", symbol="2330.TW")]

    def fake_load_stock_universe(force_refresh=False):
        calls.append("universe")
        return universe

    def fake_warmup_market_screening_cache(items, report_date, force_refresh=False, progress=None):
        calls.append("market_cache")
        assert items is universe
        assert report_date == target_date
        return {
            "revenue_count": 1,
            "price_metric_count": 1,
            "technical_count": 1,
            "technical_adjusted_count": 1,
            "market_risk_count": 2,
            "market_risk_complete": True,
            "warnings": [],
        }

    def fake_scan_tw_market(force_refresh=False, max_symbols=None, scan_settings=None, **kwargs):
        calls.append("financial_scan")
        assert kwargs["report_date"] == target_date
        assert kwargs["historical_replay"] is False
        return SimpleNamespace(candidates=[object(), object()])

    def fake_warmup_chip_data_cache(**kwargs):
        calls.append("chip_cache")
        assert kwargs["full_backfill"] is True
        assert kwargs["report_date"] == target_date
        assert kwargs["scope"] == "scheduled_all_scan_prepare"
        return SimpleNamespace(
            candidates=[object()],
            latest_trading_date=target_date,
            daily_data=pd.DataFrame({"date": pd.date_range("2026-04-01", periods=60)}),
        )

    monkeypatch.setattr(prepare, "load_stock_universe", fake_load_stock_universe)
    monkeypatch.setattr(prepare, "warmup_market_screening_cache", fake_warmup_market_screening_cache)
    monkeypatch.setattr(prepare, "scan_tw_market", fake_scan_tw_market)
    monkeypatch.setattr(prepare, "warmup_chip_data_cache", fake_warmup_chip_data_cache)
    monkeypatch.setattr(prepare, "_add_file_status", lambda *args, **kwargs: None)

    result = prepare.prepare_scheduled_all_scan_data(target_date, {"min_price": 10})

    assert calls == ["universe", "market_cache", "financial_scan", "chip_cache"]
    assert result.ok is True
    assert result.counts["monthly_revenue"] == 1
    assert result.counts["price_metrics"] == 1
    assert result.counts["technical_adjusted_history"] == 1
    assert result.counts["market_risk_flags"] == 2
    assert result.counts["market_risk_complete"] == "是"
    assert result.counts["financial_candidates"] == 2
    assert result.counts["chip_candidates"] == 1
    assert result.counts["chip_coverage_days"] == 60


def test_prepare_message_marks_warnings_without_crashing():
    result = prepare.ScheduledAllScanPrepareResult(
        report_date=date(2026, 6, 24),
        ok=True,
        steps=["價量/月營收/技術日線"],
        warnings=["籌碼日資料僅 50/60 日，精選結果可能偏保守"],
        counts={"price_metrics": 100, "chip_coverage_days": 50},
    )

    text = prepare.format_scheduled_all_scan_prepare_message(result)

    assert "20:30 全部選股前置資料準備：完成" in text
    assert "價量 100" in text
    assert "籌碼天數 50" in text
    assert "籌碼日資料僅 50/60 日" in text
