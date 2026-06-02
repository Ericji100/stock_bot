from __future__ import annotations

from datetime import date
import json
from types import SimpleNamespace

import pandas as pd

from research_center.market_movers_service import build_market_movers
from stock_scanner import StockUniverseEntry, _extract_price_metric, load_price_metrics


def _stock(code: str, name: str, industry: str) -> SimpleNamespace:
    return SimpleNamespace(code=code, symbol=f"{code}.TW", name=name, industry=industry)


def test_market_movers_uses_full_market_without_strategy_hard_filters():
    universe = [
        _stock("0001", "低價強勢股", "電子零組件"),
        _stock("2330", "台積電", "半導體"),
        _stock("9999", "跌幅股", "觀光"),
    ]
    metrics = {
        "0001.TW": {"price": 3.2, "previous_close": 2.9, "volume": 5000, "avg_volume_20d": 100, "new_high_days": 60},
        "2330.TW": {"price": 900, "previous_close": 880, "volume": 30000, "avg_volume_20d": 20000},
        "9999.TW": {"price": 50, "previous_close": 55, "volume": 1000, "avg_volume_20d": 900},
    }

    data = build_market_movers(date(2026, 5, 22), universe=universe, price_metrics=metrics)

    assert data["command_role"] == "market_movers"
    assert "不套用 /scan" in data["hard_filter_policy"]
    assert data["top_gainers"][0]["code"] == "0001"
    assert data["top_losers"][0]["code"] == "9999"
    assert data["top_volume_surge"][0]["code"] == "0001"
    assert data["new_highs"][0]["code"] == "0001"
    assert data["data_quality"]["change_pct_coverage_pct"] == 100.0


def test_market_movers_keeps_recent_strong_pullback_in_active_set():
    universe = [
        _stock("2327", "國巨", "電子零組件業"),
        _stock("9999", "弱勢股", "電子零組件業"),
    ]
    metrics = {
        "2327.TW": {
            "price": 738,
            "previous_close": 742,
            "change_pct": -0.54,
            "change_pct_5d": 8.0,
            "change_pct_10d": 16.0,
            "change_pct_20d": 31.0,
            "volume": 27645,
            "avg_volume_20d": 55170,
            "near_high_20d": True,
            "pullback_from_high_pct": -3.2,
            "above_ma5": True,
            "above_ma10": True,
            "above_ma20": True,
        },
        "9999.TW": {
            "price": 50,
            "previous_close": 55,
            "change_pct": -9.0,
            "change_pct_20d": -20.0,
            "volume": 1000,
            "avg_volume_20d": 900,
        },
    }

    data = build_market_movers(date(2026, 5, 29), universe=universe, price_metrics=metrics)

    assert data["top_trend_strength"][0]["code"] == "2327"
    assert data["top_trend_strength"][0]["trend_state"] == "trend_pullback"
    assert any(row["code"] == "2327" for row in data["active_movers"])
    assert data["sector_mover_rankings"][0]["sector_state"] == "trend_pullback"


def test_market_movers_marks_missing_rank_fields_when_price_cache_is_limited():
    universe = [_stock("2330", "台積電", "半導體")]
    metrics = {"2330.TW": {"price": 900, "avg_volume_20d": 20000}}

    data = build_market_movers(date(2026, 5, 22), universe=universe, price_metrics=metrics)

    assert data["source_mode"] == "price_volume_proxy"
    assert "change_pct" in data["data_quality"]["missing_fields"]
    assert "volume_ratio" in data["data_quality"]["missing_fields"]
    assert data["active_movers"][0]["code"] == "2330"


def test_price_metric_extraction_includes_mover_fields():
    frame = pd.DataFrame(
        {
            "Close": list(range(100, 121)),
            "Volume": [1000 * 1000] * 20 + [3000 * 1000],
        },
        index=pd.date_range("2026-05-01", periods=21, freq="D"),
    )

    metric = _extract_price_metric(frame)

    assert metric is not None
    assert metric["price"] == 120.0
    assert metric["previous_close"] == 119.0
    assert metric["change_pct"] > 0
    assert metric["volume"] == 3000.0
    assert metric["volume_ratio"] > 1
    assert metric["turnover"] == 360000.0
    assert metric["new_high_days"] == 20
    assert metric["price_date"] == "2026-05-21"
    assert metric["change_pct_5d"] > 0
    assert metric["change_pct_10d"] > 0
    assert metric["change_pct_20d"] > 0
    assert metric["near_high_20d"] is True
    assert metric["above_ma5"] is True
    assert metric["above_ma20"] is True
    assert metric["days_since_high"] == 0


def test_price_metrics_keeps_existing_cache_when_refresh_download_fails(monkeypatch):
    cache_payload = {
        "generated_at": "2026-05-24T10:00:00",
        "metrics": {
            "2330.TW": {
                "price": 900.0,
                "previous_close": 880.0,
                "change_pct": 2.27,
                "volume_ratio": 1.2,
                "price_date": "2026-05-24",
            }
        },
    }
    written_payload = {}
    monkeypatch.setattr("stock_scanner.PRICE_CACHE_PATH", _FakeCachePath())
    monkeypatch.setattr("stock_scanner._read_json", lambda path: cache_payload)
    monkeypatch.setattr("stock_scanner._write_json", lambda path, payload: written_payload.update(payload))
    monkeypatch.setattr("stock_scanner._is_fresh", lambda path, ttl: False)
    monkeypatch.setattr("stock_scanner._download_chunk_price_metrics", lambda symbols: {})
    monkeypatch.setattr("stock_scanner.time.sleep", lambda seconds: None)

    universe = [StockUniverseEntry(code="2330", symbol="2330.TW", market="TWSE", name="台積電")]

    metrics = load_price_metrics(universe, force_refresh=True, chunk_size=1)

    assert metrics["2330.TW"]["price"] == 900.0
    assert written_payload == {}


def test_expired_price_metrics_refreshes_all_requested_symbols(monkeypatch):
    cache_payload = {
        "generated_at": "2026-05-24T10:00:00",
        "metrics": {
            "2330.TW": {"price": 900.0, "change_pct": 1.0, "volume_ratio": 1.0, "price_date": "2026-05-24"},
            "2317.TW": {"price": 150.0, "change_pct": 1.0, "volume_ratio": 1.0, "price_date": "2026-05-24"},
        },
    }
    fetched_symbols = []
    written_payload = {}

    def fake_download(symbols):
        fetched_symbols.extend(symbols)
        return {
            symbol: {
                "price": 1000.0 if symbol == "2330.TW" else 160.0,
                "previous_close": 990.0,
                "change_pct": 1.01,
                "volume_ratio": 1.3,
                "price_date": "2026-05-29",
            }
            for symbol in symbols
        }

    monkeypatch.setattr("stock_scanner.PRICE_CACHE_PATH", _FakeCachePath())
    monkeypatch.setattr("stock_scanner._read_json", lambda path: cache_payload)
    monkeypatch.setattr("stock_scanner._write_json", lambda path, payload: written_payload.update(payload))
    monkeypatch.setattr("stock_scanner._is_fresh", lambda path, ttl: False)
    monkeypatch.setattr("stock_scanner._download_chunk_price_metrics", fake_download)
    monkeypatch.setattr("stock_scanner.time.sleep", lambda seconds: None)

    universe = [
        StockUniverseEntry(code="2330", symbol="2330.TW", market="TWSE", name="TSMC"),
        StockUniverseEntry(code="2317", symbol="2317.TW", market="TWSE", name="Hon Hai"),
    ]

    metrics = load_price_metrics(universe, chunk_size=2)

    assert fetched_symbols == ["2330.TW", "2317.TW"]
    assert metrics["2330.TW"]["price"] == 1000.0
    assert metrics["2330.TW"]["price_date"] == "2026-05-29"
    assert written_payload["metrics"]["2317.TW"]["price_date"] == "2026-05-29"


class _FakeCachePath:
    def exists(self):
        return True
