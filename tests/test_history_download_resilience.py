"""Offline contracts: missing downloads are not prices or permanent no-data."""
from datetime import date
import json
from unittest.mock import Mock

import pandas as pd
import pytest

from stock_ai_bot.data_sources import history_retry as retry
from stock_ai_bot.data_sources import historical_price_service as historical
from stock_ai_bot.data_sources import fugle_data as fugle
from stock_ai_bot.scanning import technical_scanner as technical
from stock_ai_bot.scanning import stock_scanner as scanner


def bars(count=25):
    return pd.DataFrame({
        "date": pd.date_range("2026-09-28", periods=count),
        "open": [10.0] * count, "high": [12.0] * count,
        "low": [9.0] * count, "close": [11.0] * count,
        "volume": [1000.0] * count, "adj_close": [11.0] * count,
    })


@pytest.fixture(autouse=True)
def isolated_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(technical, "TECH_CACHE_DIR", tmp_path / "live")
    monkeypatch.setattr(historical, "HISTORICAL_PRICE_CACHE_DIR", tmp_path / "historical")
    monkeypatch.setattr(retry.time, "sleep", lambda _: None)
    historical._read_cached_history.cache_clear()
    yield
    historical._read_cached_history.cache_clear()


@pytest.mark.parametrize("response", [None, {}, [], "bad", pd.DataFrame()])
def test_missing_frame_retries_are_bounded(response):
    fetch = Mock(return_value=response)
    assert retry.fetch_history_with_retry(fetch, attempts=99).empty
    assert fetch.call_count == 3


def test_retry_succeeds_without_mutating_provider_frame():
    expected = bars()
    fetch = Mock(side_effect=[TypeError("NoneType"), expected])
    result = retry.fetch_history_with_retry(fetch)
    pd.testing.assert_frame_equal(result, expected)
    assert fetch.call_count == 2


def test_rate_limit_goes_to_fallback_without_retry_or_secret_log(caplog):
    fetch = Mock(side_effect=RuntimeError("HTTP 429 secret-key-do-not-print"))
    assert retry.fetch_history_with_retry(fetch).empty
    assert fetch.call_count == 1
    assert "secret-key" not in caplog.text


def test_malformed_nonempty_frame_is_retried_after_normalization():
    fetch = Mock(side_effect=[pd.DataFrame({"unexpected": [1]}), bars()])
    result = retry.fetch_history_with_retry(fetch, normalize=technical._standardize_history)
    assert len(result) == 25
    assert fetch.call_count == 2


@pytest.mark.parametrize("response", [None, {}, [], "bad"])
def test_history_normalizers_and_metric_accept_missing_objects(response):
    assert technical._standardize_history(response).empty
    assert historical.standardize_history(response).empty
    assert scanner._extract_price_metric(response) is None


def test_technical_retries_primary_before_fallback(monkeypatch):
    download = Mock(side_effect=[None, bars()])
    fallback = Mock(return_value=bars())
    monkeypatch.setattr(technical.yf, "download", download)
    monkeypatch.setattr(technical, "fetch_fugle_history", fallback)
    result, source = technical.fetch_daily_history("3290.TWO", date(2026, 10, 23))
    assert len(result) == 25 and source == "Yahoo Finance"
    assert download.call_count == 2
    assert download.call_args.kwargs["timeout"] == 10
    assert download.call_args.kwargs["threads"] is False
    fallback.assert_not_called()


def test_technical_fallback_is_once_and_records_real_source(monkeypatch):
    monkeypatch.setattr(technical.yf, "download", Mock(return_value=None))
    fallback = Mock(return_value=bars())
    monkeypatch.setattr(technical, "fetch_fugle_history", fallback)
    result, source = technical.fetch_daily_history("4154.TWO", date(2026, 10, 23))
    assert len(result) == 25 and source == "Fugle"
    fallback.assert_called_once()
    metadata = json.loads(technical._cache_meta_path("4154.TWO").read_text(encoding="utf-8"))
    assert metadata["source"] == "Fugle"


@pytest.mark.parametrize("failure", [None, RuntimeError("fallback failed")])
def test_failed_sources_preserve_good_live_cache(monkeypatch, failure):
    technical._save_history("5328.TWO", bars(), source="known-good")
    path = technical._cache_path("5328.TWO")
    before = path.read_bytes()
    monkeypatch.setattr(technical.yf, "download", Mock(return_value=None))
    fallback = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=None)
    monkeypatch.setattr(technical, "fetch_fugle_history", fallback)
    result, _ = technical.fetch_daily_history("5328.TWO", date(2026, 10, 23))
    assert result.empty
    assert path.read_bytes() == before
    assert technical._load_cached_history("5328.TWO", require_fresh=False)["date"].max().date() == date(2026, 10, 22)


def test_historical_old_empty_marker_is_not_permanent_proof(monkeypatch):
    marker = historical._empty_marker_path("3290.TWO")
    marker.parent.mkdir(parents=True)
    marker.write_text('{"empty_through":"2026-10-23"}', encoding="utf-8")
    download = Mock(return_value=bars())
    monkeypatch.setattr(historical.yf, "download", download)
    monkeypatch.setattr(historical, "fetch_fugle_history", Mock(return_value=None))
    result, _ = historical.fetch_history("3290.TWO", date(2026, 10, 22))
    assert len(result) == 25
    download.assert_called_once()
    assert not marker.exists()


def test_historical_transient_failure_does_not_create_empty_marker(monkeypatch):
    monkeypatch.setattr(historical.yf, "download", Mock(side_effect=TypeError("NoneType")))
    monkeypatch.setattr(historical, "fetch_fugle_history", Mock(return_value=None))
    result, source = historical.fetch_history("8047.TWO", date(2026, 10, 22))
    assert result.empty and "資料不足" in source
    assert not historical._empty_marker_path("8047.TWO").exists()
    assert not historical.historical_price_cache_path("8047.TWO").exists()


def test_historical_fallback_writes_only_isolated_cache(monkeypatch):
    monkeypatch.setattr(historical.yf, "download", Mock(return_value=None))
    monkeypatch.setattr(historical, "fetch_fugle_history", Mock(return_value=bars()))
    result, source = historical.fetch_history("4154.TWO", date(2026, 10, 22))
    assert len(result) == 25 and source.startswith("Fugle")
    assert historical.historical_price_cache_path("4154.TWO").exists()
    assert not technical._cache_path("4154.TWO").exists()


def test_prefetch_null_result_retries_without_permanent_negative_cache(monkeypatch):
    monkeypatch.setattr(historical.yf, "download", Mock(return_value=None))
    monkeypatch.setattr(historical, "fetch_fugle_history", Mock(return_value=None))
    stats = historical.prefetch_histories(["8047.TWO"], start_date=date(2026, 9, 1), end_date=date(2026, 10, 22), retry_workers=1)
    assert stats["empty"] == 1
    assert not historical._empty_marker_path("8047.TWO").exists()


def test_metric_downloads_handle_null_primary_and_failed_fallback(monkeypatch):
    monkeypatch.setattr(scanner.yf, "download", Mock(return_value=None))
    monkeypatch.setattr(scanner.yf, "Ticker", Mock(return_value=Mock(history=Mock(return_value=None))))
    monkeypatch.setattr(scanner, "fetch_fugle_history", Mock(side_effect=RuntimeError("failed")))
    assert scanner._download_chunk_price_metrics(["8047.TWO"]) == {}


def fugle_request(monkeypatch, payload):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get.return_value = Mock(json=Mock(return_value=payload))
    health, limiter = Mock(), Mock()
    limiter.can_use.return_value = True
    health.is_available.return_value = True
    monkeypatch.setattr(fugle.httpx, "Client", Mock(return_value=client))
    monkeypatch.setattr(fugle, "get_fugle_api_key", lambda: "test-secret")
    monkeypatch.setattr(fugle, "_wait_for_fugle_slot", lambda: None)
    monkeypatch.setattr(fugle, "_FUGLE_HEALTH", health)
    monkeypatch.setattr(fugle, "_FUGLE_LIMITER", limiter)
    return health, limiter


@pytest.mark.parametrize("payload", [None, [], {}, {"data": None}, {"data": "bad"}])
def test_malformed_fugle_payload_cannot_crash_or_report_success(monkeypatch, payload):
    health, limiter = fugle_request(monkeypatch, payload)
    assert fugle.fetch_fugle_history("3290.TWO", date(2026, 10, 1), date(2026, 10, 22)).empty
    health.record_failure.assert_called_once()
    health.record_success.assert_not_called()
    limiter.record_use.assert_called_once_with("historical")


def test_fugle_skips_malformed_rows_and_nonfinite_prices(monkeypatch):
    rows = [None, {}, {"date": ["2026-10-22"]}, {"date": "2026-10-22", "open": "inf", "high": "inf", "low": 1, "close": 2, "volume": 1},
            {"date": "2026-10-22", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 0}]
    health, limiter = fugle_request(monkeypatch, {"data": rows})
    frame = fugle.fetch_fugle_history("3290.TWO", date(2026, 10, 1), date(2026, 10, 22))
    assert len(frame) == 1 and frame.iloc[0]["close"] == 11
    assert frame.iloc[0]["volume"] == 0  # A real zero-volume observation is not invented data.
    health.record_failure.assert_not_called()
    limiter.record_use.assert_called_once_with("historical")


def test_symbol_specific_empty_fugle_range_does_not_disable_whole_provider(monkeypatch):
    health, _ = fugle_request(monkeypatch, {"data": []})
    assert fugle.fetch_fugle_history("8047.TWO", date(2026, 10, 1), date(2026, 10, 22)).empty
    health.record_failure.assert_not_called()
