from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import requests

from stock_ai_bot.monitoring.etf_monitor.data import (
    MonitorMarketDataProvider,
    OfficialTwseQuoteProvider,
    TWSE_MIS_URL,
)
from stock_ai_bot.monitoring.etf_monitor.errors import EtfMonitorDataError
from stock_ai_bot.monitoring.etf_monitor.models import MonitorPhase


TAIPEI = ZoneInfo("Asia/Taipei")


def _payload(*, closed=False):
    clock = "13:55:00" if closed else "12:45:00"
    trade_time = "13:30:00" if closed else "12:44:59"
    return {
        "rtcode": "0000", "rtmessage": "OK",
        "queryTime": {"sysDate": "20261027", "sysTime": clock},
        "msgArray": [{
            "c": "0050", "d": "20261027", "o": "107.10",
            "h": "107.55", "l": "105.80", "z": "106.60",
            "v": "139978", "t": trade_time, "ts": "0", "mt": "917113",
            "trade": {"z": "106.60", "t": trade_time, "v": 1967},
        }],
    }


def _install(monkeypatch, payload):
    class Response:
        status_code = 200
        content = json.dumps(payload).encode()

        def raise_for_status(self):
            pass

        def json(self):
            return payload

    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("stock_ai_bot.monitoring.etf_monitor.data.requests.get", get)
    return calls


@pytest.mark.parametrize("closed", [False, True])
def test_nonzero_mt_does_not_reject_an_actual_trade(monkeypatch, closed):
    payload = _payload(closed=closed)
    calls = _install(monkeypatch, payload)
    now = datetime(2026, 10, 27, 13 if closed else 12, 55 if closed else 45, tzinfo=TAIPEI)
    quote = OfficialTwseQuoteProvider()._fetch(now)
    assert quote.raw_last == Decimal("106.60")
    assert quote.is_market_closed is closed
    assert len(calls) == 1 and calls[0][0] == TWSE_MIS_URL
    assert calls[0][1]["params"]["ex_ch"] == "tse_0050.tw"


@pytest.mark.parametrize("closed", [False, True])
def test_missing_snapshot_price_uses_only_embedded_actual_trade(monkeypatch, closed):
    payload = _payload(closed=closed)
    row = payload["msgArray"][0]
    row.update(z="-", t="13:34:00" if closed else "12:44:59", pz="999", y="888")
    _install(monkeypatch, payload)
    now = datetime(2026, 10, 27, 13 if closed else 12, 55 if closed else 45, tzinfo=TAIPEI)
    quote = OfficialTwseQuoteProvider()._fetch(now)
    assert quote.raw_last == Decimal("106.60")
    assert quote.last_trade_at.strftime("%H:%M:%S") == row["trade"]["t"]
    assert quote.is_market_closed is closed


@pytest.mark.parametrize("change,reason", [
    ({"z": "-", "trade": None, "pz": "106.6", "y": "106.6"}, "MIS_MISSING_LAST_PRICE"),
    ({"ts": "1"}, "MIS_REFERENCE_QUOTE"),
    ({"ts": "2"}, "MIS_REFERENCE_QUOTE"),
    ({"c": "2330"}, "MIS_SYMBOL_MISMATCH"),
    ({"d": "20261026"}, "MIS_DATE_MISMATCH"),
    ({"t": "08:59:59"}, "MIS_TRADE_TIME_INVALID"),
    ({"t": "12:46:00"}, "MIS_TRADE_TIME_INVALID"),
    ({"t": "12:30:00"}, "MIS_STALE_TRADE"),
    ({"z": "NaN"}, "MIS_INVALID_LAST_PRICE"),
    ({"z": "Infinity"}, "MIS_INVALID_LAST_PRICE"),
    ({"z": "not-a-number"}, "MIS_INVALID_LAST_PRICE"),
    ({"z": "0"}, "MIS_INVALID_LAST_PRICE"),
    ({"z": "108"}, "MIS_OHLC_INCONSISTENT"),
    ({"l": "108"}, "MIS_OHLC_INCONSISTENT"),
    ({"v": "-"}, "MIS_MISSING_VOLUME_PRICE"),
    ({"z": "-", "trade": {"z": "106.6"}}, "MIS_TRADE_TIME_INVALID"),
    ({"z": "-", "trade": {"z": "106.6", "t": "12:20:00"}}, "MIS_STALE_TRADE"),
])
def test_invalid_quotes_remain_fail_closed_with_specific_reason(monkeypatch, change, reason):
    payload = _payload()
    payload["msgArray"][0].update(change)
    _install(monkeypatch, payload)
    with pytest.raises(EtfMonitorDataError) as caught:
        OfficialTwseQuoteProvider()._fetch(datetime(2026, 10, 27, 12, 45, tzinfo=TAIPEI))
    assert caught.value.reason_code == reason
    assert caught.value.details["source_endpoint"] == TWSE_MIS_URL
    assert len(caught.value.details["receipt_sha256"]) == 64


def test_stale_source_is_rejected_even_with_fresh_trade(monkeypatch):
    payload = _payload()
    payload["queryTime"]["sysTime"] = "12:30:00"
    _install(monkeypatch, payload)
    with pytest.raises(EtfMonitorDataError) as caught:
        OfficialTwseQuoteProvider()._fetch(datetime(2026, 10, 27, 12, 45, tzinfo=TAIPEI))
    assert caught.value.reason_code == "MIS_STALE_SOURCE"


def test_request_failure_has_structured_reason_without_request_secrets(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("private-proxy-password")

    monkeypatch.setattr("stock_ai_bot.monitoring.etf_monitor.data.requests.get", fail)
    with pytest.raises(EtfMonitorDataError) as caught:
        OfficialTwseQuoteProvider()._fetch(datetime(2026, 10, 27, 12, 45, tzinfo=TAIPEI))
    assert caught.value.reason_code == "MIS_REQUEST_FAILED"
    assert "private-proxy-password" not in str(caught.value)
    assert "private-proxy-password" not in json.dumps(caught.value.details)


@pytest.mark.parametrize("phase", [MonitorPhase.PREVIEW, MonitorPhase.DECISION])
def test_provider_preview_and_formal_paths_use_the_validated_quote(monkeypatch, phase):
    import asyncio
    from dataclasses import replace

    closed = phase is MonitorPhase.DECISION
    payload = _payload(closed=closed)
    payload["msgArray"][0]["z"] = "-"
    _install(monkeypatch, payload)
    provider = OfficialTwseQuoteProvider()
    observed = datetime(2026, 10, 27, 13 if closed else 12, 55 if closed else 45, tzinfo=TAIPEI)
    quote = provider._fetch(observed)

    class Quotes:
        async def fetch(self):
            return quote

    class Registry:
        def load_provisional(self, as_of, received):
            assert received is quote
            return received

    data = MonitorMarketDataProvider(Registry(), Quotes())
    assert asyncio.run(data.load(phase, observed.date())) is quote
    if closed:
        quote = replace(quote, is_market_closed=False)
        with pytest.raises(EtfMonitorDataError, match="closed official"):
            asyncio.run(data.load(phase, observed.date()))


def test_a_preclose_trade_never_confirms_the_close(monkeypatch):
    payload = _payload(closed=True)
    payload["msgArray"][0]["t"] = "13:26:00"
    _install(monkeypatch, payload)
    with pytest.raises(EtfMonitorDataError) as caught:
        OfficialTwseQuoteProvider()._fetch(datetime(2026, 10, 27, 13, 55, tzinfo=TAIPEI))
    assert caught.value.reason_code == "MIS_STALE_TRADE"
