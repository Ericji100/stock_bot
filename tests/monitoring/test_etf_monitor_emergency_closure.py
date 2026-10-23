from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
import requests

from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.data import (
    OfficialTwseTradingCalendar, install_daily_delta_file, load_daily_delta_file,
)
from stock_ai_bot.monitoring.etf_monitor.delta_pipeline import (
    EMERGENCY_CLOSURE_URL, HOLIDAY_URL, build_daily_delta_bundle,
    cached_emergency_notice, parse_emergency_closures, parse_holidays,
)
from stock_ai_bot.monitoring.etf_monitor.errors import EtfMonitorDataError, EtfMonitorRoutingError
from stock_ai_bot.monitoring.etf_monitor.models import MonitorPhase, stable_sha256
from stock_ai_bot.monitoring.etf_monitor.service import EtfMonitorService
from stock_ai_bot.monitoring.etf_monitor.store import EtfMonitorStore

DAY = date(2026, 10, 23)
NOTICE = (
    '<html><h3>115.10.23颱風假休市公告</h3><p>因應颱風來襲，臺灣證券交易所'
    '<span>115</span>年<span>10</span>月<span>23</span>日休市。</p></html>'
).encode()
EMPTY = '<html><body>臺灣證券交易所</body></html>'.encode()
HOLIDAYS = json.dumps([{"Date": "1150101"}, {"Date": "1151026"}]).encode()


def response(url, raw):
    return SimpleNamespace(url=url, content=raw, headers={"content-type": "text/html"}, raise_for_status=lambda: None)


def test_nested_span_roc_date_is_an_explicit_market_closure():
    assert parse_emergency_closures(NOTICE) == {DAY}


def test_annual_schedule_opening_and_last_trading_days_are_not_holidays():
    raw = json.dumps([
        {"Date": "1150101", "Name": "中華民國開國紀念日"},
        {"Date": "1150102", "Name": "國曆新年開始交易日"},
        {"Date": "1150211", "Name": "農曆春節前最後交易日"},
        {"Date": "1150212", "Name": "市場無交易，僅辦理結算交割作業"},
        {"Date": "1150223", "Name": "農曆春節後開始交易日"},
    ]).encode()
    assert parse_holidays(raw) == {date(2026, 1, 1), date(2026, 2, 12)}


def test_calendar_gate_keeps_annual_information_trading_days_open(monkeypatch):
    raw = json.dumps([{"Date": "1150101", "Name": "開國紀念日"}, {"Date": "1150223", "Name": "農曆春節後開始交易日"}]).encode()
    monkeypatch.setattr("stock_ai_bot.monitoring.etf_monitor.data.requests.get", lambda url, **k: response(url, raw if url == HOLIDAY_URL else EMPTY))
    assert OfficialTwseTradingCalendar().verify(date(2026, 2, 23))["is_trading_day"] is True


@pytest.mark.parametrize("text", [
    "臺灣證券交易所115年10月23日不休市。",
    "香港交易所115年10月23日休市。臺灣證券交易所",
    "臺灣證券交易所115年10月23日下午休市。",
    "臺灣證券交易所115年10月23日部分股票休市。",
    "<script>臺灣證券交易所115年10月23日休市。</script>臺灣證券交易所",
    "<!--臺灣證券交易所115年10月23日休市。-->臺灣證券交易所",
])
def test_does_not_infer_market_closure_from_unrelated_or_hidden_text(text):
    assert parse_emergency_closures(f"<html>{text}</html>".encode()) == set()


@pytest.mark.parametrize("raw", [b"[]", b"<html>maintenance</html>", b"\xff"])
def test_bad_notice_page_fails_closed(raw):
    with pytest.raises(EtfMonitorDataError):
        parse_emergency_closures(raw)


def test_live_gate_overrides_annual_table_and_retains_durable_proof(tmp_path, monkeypatch):
    def get(url, **kwargs):
        return response(url, HOLIDAYS if url == HOLIDAY_URL else NOTICE)
    monkeypatch.setattr("stock_ai_bot.monitoring.etf_monitor.data.requests.get", get)
    calendar = OfficialTwseTradingCalendar(receipt_root=tmp_path)
    result = calendar.verify(DAY)
    assert result["is_trading_day"] is False
    assert result["calendar_reason"] == "OFFICIAL_EMERGENCY_CLOSURE"
    assert (tmp_path / f"{result['receipt_sha256']}.raw").read_bytes() == NOTICE
    # Restart and an offline/rotated current notice cannot erase a known closure.
    monkeypatch.setattr("stock_ai_bot.monitoring.etf_monitor.data.requests.get", lambda *a, **k: pytest.fail("no network needed for pinned closure"))
    assert OfficialTwseTradingCalendar(receipt_root=tmp_path).verify(DAY)["is_trading_day"] is False
    assert cached_emergency_notice(tmp_path, date(2026, 10, 22)) is None


def test_live_gate_does_not_apply_notice_to_another_day(monkeypatch):
    monkeypatch.setattr("stock_ai_bot.monitoring.etf_monitor.data.requests.get", lambda url, **k: response(url, HOLIDAYS if url == HOLIDAY_URL else NOTICE))
    calendar = OfficialTwseTradingCalendar()
    assert calendar.verify(date(2026, 10, 22))["is_trading_day"] is True
    assert calendar.verify(date(2026, 10, 26))["is_trading_day"] is False
    assert calendar.verify(date(2026, 10, 27))["is_trading_day"] is True


@pytest.mark.parametrize("failure", ["redirect", "outage"])
def test_notice_source_redirect_or_outage_is_not_an_open_day(monkeypatch, failure):
    def get(url, **kwargs):
        if url == HOLIDAY_URL:
            return response(url, HOLIDAYS)
        if failure == "outage":
            raise requests.ConnectionError("offline")
        return response("https://untrusted.invalid/", NOTICE)
    monkeypatch.setattr("stock_ai_bot.monitoring.etf_monitor.data.requests.get", get)
    with pytest.raises(EtfMonitorDataError):
        OfficialTwseTradingCalendar().verify(DAY)


def test_negative_notice_cache_expires_and_sees_intraday_announcement(monkeypatch):
    from stock_ai_bot.monitoring.etf_monitor import data
    replies = iter([EMPTY, NOTICE])
    monkeypatch.setattr(data.requests, "get", lambda url, **k: response(url, HOLIDAYS if url == HOLIDAY_URL else next(replies)))
    calendar = OfficialTwseTradingCalendar()
    assert calendar.verify(DAY)["is_trading_day"] is True
    calendar._notice_cache[1]["fetched_at"] = "2026-01-01T00:00:00+08:00"
    assert calendar.verify(DAY)["is_trading_day"] is False


def test_cached_notice_tamper_is_rejected(tmp_path):
    (tmp_path / ("a" * 64 + ".raw")).write_bytes(NOTICE)
    with pytest.raises(EtfMonitorDataError, match="SHA-256"):
        cached_emergency_notice(tmp_path, DAY)


def test_utf8_bom_notice_retains_its_closure_after_restart(tmp_path):
    raw = b"\xef\xbb\xbf" + NOTICE
    (tmp_path / (hashlib.sha256(raw).hexdigest() + ".raw")).write_bytes(raw)
    assert cached_emergency_notice(tmp_path, DAY) == raw


class NoMarketAccess:
    async def load(self, *args):
        pytest.fail("closed market must not load a quote, delta, or evaluate strategy")


def closed_service(tmp_path, *, router=None, daily_status=True):
    config = replace(load_etf_monitor_config(), enabled=True, daily_shadow_status=daily_status)
    store = EtfMonitorStore(tmp_path / "closed.sqlite3")
    evidence = {"is_trading_day": False, "calendar_reason": "OFFICIAL_EMERGENCY_CLOSURE", "source": "TWSE_EMERGENCY_CLOSURE_OFFICIAL", "receipt_sha256": hashlib.sha256(NOTICE).hexdigest()}
    return EtfMonitorService(config=config, store=store, data_provider=NoMarketAccess(), router=router, trading_day_predicate=lambda _: evidence), store


def test_three_scheduled_phases_send_only_one_chinese_closed_status(tmp_path):
    service, store = closed_service(tmp_path)
    for phase in (MonitorPhase.PREVIEW, MonitorPhase.DECISION, MonitorPhase.RECONCILIATION):
        result = asyncio.run(service.run(phase, now=datetime(2026, 10, 23, 22, tzinfo=ZoneInfo("Asia/Taipei"))))
        assert result.status == "SKIPPED_OFFICIAL_NON_TRADING_DAY"
        assert result.decision is None and result.message == "今日休市"
    with sqlite3.connect(store.path) as con:
        events = con.execute("select event_type from events").fetchall()
        rows = con.execute("select target_chat_id,body from outbox").fetchall()
        assert events == [("MARKET_CLOSED",)]
        assert len(rows) == 1 and rows[0][0] == "-5527055845"
        assert "今日休市" in rows[0][1]
        assert "資料不足" not in rows[0][1] and "觸發進場" not in rows[0][1] and "觸發出場" not in rows[0][1]
        for table in ("monitor_state", "pending_actions", "corporate_actions", "corrections"):
            assert con.execute(f"select count(*) from {table}").fetchone()[0] == 0
        assert con.execute("select count(*) from job_runs where status='SUCCEEDED'").fetchone()[0] == 3


def test_closed_delivery_retry_never_becomes_data_unavailable_or_duplicates(tmp_path):
    class Router:
        attempts = 0
        async def deliver(self, event_id):
            self.attempts += 1
            if self.attempts == 1:
                raise EtfMonitorRoutingError("offline")
    router = Router()
    service, store = closed_service(tmp_path, router=router)
    now = datetime(2026, 10, 23, 22, tzinfo=ZoneInfo("Asia/Taipei"))
    with pytest.raises(EtfMonitorRoutingError):
        asyncio.run(service.run(MonitorPhase.RECONCILIATION, now=now))
    assert asyncio.run(service.run(MonitorPhase.RECONCILIATION, now=now)).message == "今日休市"
    assert asyncio.run(service.run(MonitorPhase.RECONCILIATION, now=now)).status == "ALREADY_COMPLETED"
    with sqlite3.connect(store.path) as con:
        assert con.execute("select event_type from events").fetchall() == [("MARKET_CLOSED",)]
        assert con.execute("select count(*) from outbox").fetchone()[0] == 1


def test_quiet_closed_day_does_not_notify_or_change_state(tmp_path):
    service, store = closed_service(tmp_path, daily_status=False)
    now = datetime(2026, 10, 23, 22, tzinfo=ZoneInfo("Asia/Taipei"))
    asyncio.run(service.run(MonitorPhase.RECONCILIATION, now=now))
    with sqlite3.connect(store.path) as con:
        assert con.execute("select count(*) from events").fetchone()[0] == 0


def test_closed_day_preserves_existing_position_and_pending_action(tmp_path):
    service, store = closed_service(tmp_path)
    # Exercise real persisted rows, not only the initially empty database.
    with sqlite3.connect(store.path) as con:
        state_columns = con.execute("pragma table_info(monitor_state)").fetchall()
        pending_columns = con.execute("pragma table_info(pending_actions)").fetchall()
        def seed(table, columns):
            row = {}
            for _, name, kind, notnull, default, pk in columns:
                if pk or (notnull and default is None):
                    row[name] = 1 if "INT" in kind else "existing-value"
            names = list(row)
            con.execute(f"insert into {table}({','.join(names)}) values({','.join('?' for _ in names)})", tuple(row.values()))
        seed("monitor_state", state_columns)
        seed("pending_actions", pending_columns)
        before = {table: con.execute(f"select * from {table}").fetchall() for table in ("monitor_state", "pending_actions")}
    now = datetime(2026, 10, 23, 22, tzinfo=ZoneInfo("Asia/Taipei"))
    asyncio.run(service.run(MonitorPhase.RECONCILIATION, now=now))
    with sqlite3.connect(store.path) as con:
        for table in before:
            assert con.execute(f"select * from {table}").fetchall() == before[table]


class DeltaSession:
    def __init__(self, *, bar=False, notice=NOTICE):
        self.bar, self.notice = bar, notice
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def get(self, url, params=None, timeout=None):
        final = url if not params else url + "?" + urlencode(params)
        if "holidaySchedule" in url:
            raw = HOLIDAYS
        elif "TWT48U_ALL" in url:
            raw = b"[]"
        elif "dividendList" in url:
            raw = b"<table></table>"
        elif "STOCK_DAY" in url:
            raw = json.dumps({"stat": "OK", "data": [["115/10/23", "100", "0", "52", "54", "51", "53"]] if self.bar else []}).encode()
        elif url == EMERGENCY_CLOSURE_URL:
            raw = self.notice
        else:
            pytest.fail(url)
        return response(final, raw)


def build(tmp_path, session, **kwargs):
    return build_daily_delta_bundle(start=DAY, through=DAY, output=tmp_path / "bundle", frozen_snapshot_id="test", frozen_manifest_sha256="a" * 64, session_factory=lambda: session, **kwargs)


def test_verified_closed_delta_installs_with_raw_notice_and_no_fake_bar(tmp_path):
    build(tmp_path, DeltaSession())
    path = tmp_path / "bundle" / "2026-10-23.json"
    payload = install_daily_delta_file(path, destination_root=tmp_path / "installed", expected_date=DAY, snapshot_id="test", manifest_sha256="a" * 64)
    assert payload["market_status"] == "CLOSED" and payload["bar"] is None
    assert (tmp_path / "installed" / payload["emergency_closure_evidence"]["receipt_relative_path"]).read_bytes() == NOTICE
    load_daily_delta_file(tmp_path / "installed" / path.name, expected_date=DAY, snapshot_id="test", manifest_sha256="a" * 64)


def test_missing_bar_alone_remains_data_unavailable(tmp_path):
    with pytest.raises(EtfMonitorDataError, match="manual evidence"):
        build(tmp_path, DeltaSession(notice=EMPTY))


def test_cached_notice_survives_rotation_in_later_delta_backfill(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / (hashlib.sha256(NOTICE).hexdigest() + ".raw")).write_bytes(NOTICE)
    build(tmp_path, DeltaSession(notice=EMPTY), closure_receipt_root=cache)
    assert (tmp_path / "bundle" / "2026-10-23.json").is_file()


def test_known_closure_cannot_be_overridden_by_a_conflicting_monthly_bar(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / (hashlib.sha256(NOTICE).hexdigest() + ".raw")).write_bytes(NOTICE)
    with pytest.raises(EtfMonitorDataError, match="conflicts"):
        build(tmp_path, DeltaSession(bar=True), closure_receipt_root=cache)


@pytest.mark.parametrize("tamper", ["wrong_date", "wrong_source", "sha", "bar_conflict"])
def test_delta_emergency_evidence_cannot_override_missing_or_conflicting_data(tmp_path, tamper):
    build(tmp_path, DeltaSession())
    path = tmp_path / "bundle" / "2026-10-23.json"
    payload = json.loads(path.read_text())
    evidence = payload["emergency_closure_evidence"]
    if tamper == "wrong_date":
        raw = NOTICE.replace(b"23", b"22")
        digest = hashlib.sha256(raw).hexdigest()
        (path.parent / "receipts" / (digest + ".raw")).write_bytes(raw)
        evidence.update(receipt_sha256=digest, receipt_relative_path=f"receipts/{digest}.raw")
    elif tamper == "wrong_source":
        evidence["request_url"] = "https://untrusted.invalid/"
    elif tamper == "sha":
        evidence["receipt_sha256"] = "b" * 64
    else:
        raw = json.dumps({"stat": "OK", "data": [["115/10/23", "100", "0", "52", "54", "51", "53"]]}).encode()
        digest = hashlib.sha256(raw).hexdigest()
        (path.parent / "receipts" / (digest + ".raw")).write_bytes(raw)
        payload["bar_evidence"].update(receipt_sha256=digest, receipt_relative_path=f"receipts/{digest}.raw")
        payload.update(market_status="OPEN", bar={"open": "52", "high": "54", "low": "51", "close": "53", "volume": "100"})
    payload["content_sha256"] = stable_sha256({k:v for k,v in payload.items() if k != "content_sha256"})
    path.write_text(json.dumps(payload))
    with pytest.raises(EtfMonitorDataError):
        load_daily_delta_file(path, expected_date=DAY, snapshot_id="test", manifest_sha256="a" * 64)
