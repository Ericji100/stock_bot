from __future__ import annotations

from datetime import date

import pandas as pd

import chip_strategies
import stock_ai_bot.data_sources.historical_price_service as historical_prices
import stock_scanner
from stock_scanner import StockUniverseEntry, load_price_metrics
import technical_scanner


def _history() -> pd.DataFrame:
    dates = pd.bdate_range("2022-11-01", periods=80)
    return pd.DataFrame(
        {
            "date": dates,
            "open": range(80, 160),
            "high": range(81, 161),
            "low": range(79, 159),
            "close": range(80, 160),
            "volume": [1_000_000] * 80,
            "adj_close": range(80, 160),
        }
    )


def test_historical_price_metrics_are_cut_off_and_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(historical_prices, "HISTORICAL_PRICE_CACHE_DIR", tmp_path)
    historical_prices._read_cached_history.cache_clear()
    historical_prices.save_history("2330.TW", _history())
    target = date(2023, 1, 10)
    entry = StockUniverseEntry("2330", "2330.TW", "TWSE", "TSMC", "semi")

    metrics = load_price_metrics([entry], as_of_date=target)

    assert metrics["2330.TW"]["price_date"] == target.isoformat()
    assert metrics["2330.TW"]["price"] == 130.0


def test_historical_price_metrics_do_not_refetch_before_listing(monkeypatch, tmp_path):
    monkeypatch.setattr(historical_prices, "HISTORICAL_PRICE_CACHE_DIR", tmp_path)
    historical_prices._read_cached_history.cache_clear()
    future_history = _history().copy()
    future_history["date"] = pd.bdate_range("2023-06-01", periods=len(future_history))
    historical_prices.save_history("6863.TW", future_history)
    entry = StockUniverseEntry("6863", "6863.TW", "TWSE", "future", "semi")
    monkeypatch.setattr(
        historical_prices,
        "fetch_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not refetch")),
    )

    metrics = load_price_metrics([entry], as_of_date=date(2023, 1, 10))

    assert metrics == {}


def test_historical_price_metrics_refresh_stale_cache_even_with_enough_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(historical_prices, "HISTORICAL_PRICE_CACHE_DIR", tmp_path)
    historical_prices._read_cached_history.cache_clear()
    history = _history()
    historical_prices.save_history("2330.TW", history)
    target = history["date"].dt.date.max() + pd.offsets.BDay(1)
    target = target.date() if hasattr(target, "date") else target
    entry = StockUniverseEntry("2330", "2330.TW", "TWSE", "TSMC", "semi")
    calls = []

    def fetch(symbol, end_date, **_kwargs):
        calls.append((symbol, end_date))
        latest = history.tail(1).copy()
        latest["date"] = pd.Timestamp(end_date)
        refreshed = pd.concat([history, latest], ignore_index=True)
        return refreshed, "test"

    monkeypatch.setattr(historical_prices, "fetch_history", fetch)

    metrics = load_price_metrics([entry], as_of_date=target)

    assert calls == [("2330.TW", target)]
    assert metrics["2330.TW"]["price_date"] == target.isoformat()


def test_historical_fetch_does_not_query_yahoo_before_listing(monkeypatch, tmp_path):
    monkeypatch.setattr(historical_prices, "HISTORICAL_PRICE_CACHE_DIR", tmp_path)
    historical_prices._read_cached_history.cache_clear()
    future_history = _history().copy()
    future_history["date"] = pd.bdate_range("2023-06-01", periods=len(future_history))
    historical_prices.save_history("6863.TW", future_history)
    monkeypatch.setattr(
        historical_prices.yf,
        "download",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not query Yahoo")),
    )

    frame, source = historical_prices.fetch_history("6863.TW", date(2023, 1, 10))

    assert frame.empty
    assert "尚未上市" in source


def test_technical_hard_filter_forwards_report_date(monkeypatch):
    target = date(2023, 1, 10)
    entry = StockUniverseEntry("2330", "2330.TW", "TWSE", "TSMC", "semi")
    calls = {}

    monkeypatch.setattr(technical_scanner, "load_stock_universe", lambda _force: [entry])

    def revenue(_universe, **kwargs):
        calls["revenue"] = kwargs
        return {}

    def prices(_universe, **kwargs):
        calls["prices"] = kwargs
        return {}

    monkeypatch.setattr(technical_scanner, "load_recent_revenue_history", revenue)
    monkeypatch.setattr(technical_scanner, "load_price_metrics", prices)

    technical_scanner.build_hard_filter_candidates(report_date=target, historical_replay=True)

    assert calls["revenue"]["as_of_date"] == target
    assert calls["prices"]["as_of_date"] == target


def test_technical_hard_filter_uses_live_inputs_for_normal_dated_scan(monkeypatch):
    target = date(2026, 9, 8)
    entry = StockUniverseEntry("2330", "2330.TW", "TWSE", "TSMC", "semi")
    calls = {}
    monkeypatch.setattr(technical_scanner, "load_stock_universe", lambda _force: [entry])
    monkeypatch.setattr(
        technical_scanner,
        "load_recent_revenue_history",
        lambda _universe, **kwargs: calls.setdefault("revenue", kwargs) or {},
    )
    monkeypatch.setattr(
        technical_scanner,
        "load_price_metrics",
        lambda _universe, **kwargs: calls.setdefault("prices", kwargs) or {},
    )

    technical_scanner.build_hard_filter_candidates(report_date=target)

    assert calls["revenue"]["as_of_date"] is None
    assert calls["prices"]["as_of_date"] is None


def test_financial_scan_uses_live_inputs_unless_historical_replay_is_explicit(monkeypatch):
    target = date(2026, 9, 8)
    entry = StockUniverseEntry("2330", "2330.TW", "TWSE", "TSMC", "semi")
    calls = []
    monkeypatch.setattr(stock_scanner, "load_stock_universe", lambda **_kwargs: [entry])
    monkeypatch.setattr(stock_scanner, "_load_gross_margin_cache", lambda: {})
    monkeypatch.setattr(
        stock_scanner,
        "load_recent_revenue_history",
        lambda _universe, **kwargs: calls.append(("revenue", kwargs["as_of_date"])) or {},
    )
    monkeypatch.setattr(
        stock_scanner,
        "load_price_metrics",
        lambda _universe, **kwargs: calls.append(("prices", kwargs["as_of_date"])) or {},
    )

    stock_scanner.scan_tw_market(report_date=target)
    stock_scanner.scan_tw_market(report_date=target, historical_replay=True)

    assert calls == [
        ("revenue", None),
        ("prices", None),
        ("revenue", target),
        ("prices", target),
    ]


def test_chip_hard_filter_uses_live_inputs_unless_historical_replay_is_explicit(monkeypatch):
    target = date(2026, 9, 8)
    entry = StockUniverseEntry("2330", "2330.TW", "TWSE", "TSMC", "semi")
    calls = []
    monkeypatch.setattr(chip_strategies, "load_stock_universe", lambda **_kwargs: [entry])
    monkeypatch.setattr(chip_strategies, "_load_issued_shares_map", lambda _universe: {})
    monkeypatch.setattr(
        chip_strategies,
        "load_recent_revenue_history",
        lambda _universe, **kwargs: calls.append(("revenue", kwargs["as_of_date"])) or {},
    )
    monkeypatch.setattr(
        chip_strategies,
        "load_price_metrics",
        lambda _universe, **kwargs: calls.append(("prices", kwargs["as_of_date"])) or {},
    )

    chip_strategies._build_hard_filter_candidates(target)
    chip_strategies._build_hard_filter_candidates(target, historical_replay=True)

    assert calls == [
        ("revenue", None),
        ("prices", None),
        ("revenue", target),
        ("prices", target),
    ]


def test_historical_tdcc_never_uses_newer_snapshot(monkeypatch):
    target = date(2023, 1, 10)
    candidates = pd.DataFrame([{"code": "2330"}])
    tdcc = pd.DataFrame(
        [
            {"snapshot_date": date(2023, 1, 6), "code": "2330", "level": 12, "pct": 50.0},
            {"snapshot_date": date(2026, 9, 4), "code": "2330", "level": 12, "pct": 60.0},
        ]
    )
    monkeypatch.setattr(chip_strategies, "_load_cached_tdcc_frames", lambda: tdcc)
    monkeypatch.setattr(
        chip_strategies,
        "update_tdcc_snapshot_cache",
        lambda: (_ for _ in ()).throw(AssertionError("historical replay must not fetch latest TDCC")),
    )

    result = chip_strategies._build_weekly_distribution(candidates, target)

    assert not result.empty
    assert result["snapshot_date"].max() <= target


def test_historical_chip_cached_only_does_not_refetch_missing_rows(monkeypatch):
    target = date(2023, 1, 3)
    candidates = pd.DataFrame(
        [
            {"code": "2330", "market": "TWSE", "issued_shares": 10_000_000.0},
            {"code": "6488", "market": "TPEX", "issued_shares": 5_000_000.0},
        ]
    )
    cached = pd.DataFrame(
        [
            {
                "date": target,
                "code": "2330",
                "market": "TWSE",
                "foreign_net_lots": 100.0,
                "trust_net_lots": 10.0,
                "source": "TWSE",
            }
        ]
    )
    monkeypatch.setattr(chip_strategies, "_load_daily_chip_cache", lambda *_args: cached)
    monkeypatch.setattr(
        chip_strategies,
        "_fetch_twse_net_buy_for_date",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not refetch")),
    )
    monkeypatch.setattr(
        chip_strategies,
        "_fetch_tpex_net_buy_for_date",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not refetch")),
    )

    frame, latest = chip_strategies._fetch_recent_daily_chip_data(
        target,
        candidates,
        target_trading_days=1,
        trading_calendar=[target],
        cached_only=True,
    )

    assert latest == target
    assert frame["code"].tolist() == ["2330"]
