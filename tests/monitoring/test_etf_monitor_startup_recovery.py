from __future__ import annotations

import asyncio
import json
import threading
from datetime import date
from types import SimpleNamespace

import pytest

from stock_ai_bot.monitoring.etf_monitor import integration
from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.delta_pipeline import build_daily_delta_bundle, parse_stock_day
from stock_ai_bot.monitoring.etf_monitor.errors import EtfMonitorDataError
from stock_ai_bot.monitoring.etf_monitor.models import MonitorPhase
from stock_ai_bot.monitoring.etf_monitor.routing import FailClosedTelegramRouter
from stock_ai_bot.monitoring.etf_monitor.runtime_health import record_runtime_error, runtime_alert
from stock_ai_bot.monitoring.etf_monitor.store import EtfMonitorStore


class Bot:
    def __init__(self):
        self.sent = []

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, type="group")

    async def send_message(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(message_id=len(self.sent))


def make_runtime(tmp_path, updater):
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    cfg = load_etf_monitor_config()
    bot = Bot()
    runtime = integration.EtfMonitorRuntime(
        config=cfg, store=store,
        router=FailClosedTelegramRouter(config=cfg, store=store, bot=bot),
        service=SimpleNamespace(data_provider=SimpleNamespace(delta_updater=SimpleNamespace(ensure_through=updater))),
        locks={p: asyncio.Lock() for p in integration.JOB_NAMES},
    )
    app = SimpleNamespace(bot_data={integration.RUNTIME_KEY: runtime}, job_queue=True)
    return app, runtime, bot


@pytest.mark.parametrize("stage", ["recovery", "backfill", "route", "outbox"])
def test_transient_startup_failure_recovers_with_alert_once_and_durable_log(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(integration, "STARTUP_RETRY_SECONDS", (0.01,))

    async def scenario():
        app, state, bot = make_runtime(tmp_path, lambda day: None)
        original = integration._startup_attempt
        count = 0

        async def transient(app, runtime):
            nonlocal count
            count += 1
            if count <= 2:
                runtime.startup_stage = stage
                raise OSError("official endpoint temporarily unavailable")
            await original(app, runtime)

        monkeypatch.setattr(integration, "_startup_attempt", transient)
        await integration.initialize_etf_monitor(app)
        await asyncio.wait_for(state.startup_task, 2)
        assert state.startup_status == "READY"
        assert count == 3
        assert len(bot.sent) == 2
        assert "監控異常" in bot.sent[0]["text"]
        assert "監控已恢復" in bot.sent[1]["text"]
        assert {s["chat_id"] for s in bot.sent} == {state.config.chat_id}
        await integration.shutdown_etf_monitor(app)

    asyncio.run(scenario())
    log = tmp_path / "logs/etf_monitor/runtime-errors.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert "official endpoint" in rows[0]["error"]
    assert "OSError" in rows[0]["traceback"]


def test_timed_out_worker_never_overlaps_retry_or_becomes_late_ready(tmp_path, monkeypatch):
    release = threading.Event()
    count = 0

    def slow(day):
        nonlocal count
        count += 1
        release.wait(3)

    monkeypatch.setattr(integration, "STARTUP_RETRY_SECONDS", (0.01,))
    monkeypatch.setattr(integration, "STARTUP_BACKFILL_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        app, state, bot = make_runtime(tmp_path, slow)
        await integration.initialize_etf_monitor(app)
        for _ in range(300):
            if state.startup_status == "WAITING_WORKER":
                break
            await asyncio.sleep(0.005)
        assert count == 1
        assert state.startup_status == "WAITING_WORKER"
        assert len(bot.sent) == 1
        await integration.shutdown_etf_monitor(app)
        release.set()
        await asyncio.sleep(0.02)
        assert state.startup_status == "STOPPED"
        assert count == 1

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_alert_dedup_survives_restart_and_uncertain_delivery(tmp_path):
    async def scenario():
        app, state, bot = make_runtime(tmp_path, lambda day: None)
        async def uncertain(**kw):
            bot.sent.append(kw)
            raise TimeoutError("unknown send result")
        bot.send_message = uncertain
        with pytest.raises(Exception, match="uncertain"):
            await runtime_alert(state, stage="startup")
        # New runtime, same durable DB: never resend an uncertain alert.
        _, fresh, _ = make_runtime(tmp_path, lambda day: None)
        fresh.router.bot = bot
        await runtime_alert(fresh, stage="startup")
        assert len(bot.sent) == 1
    asyncio.run(scenario())


def test_error_trace_redacts_token_in_cause_chain(tmp_path):
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123456789"
    try:
        try:
            raise ValueError("https://api.telegram.org/bot" + token)
        except ValueError as exc:
            raise RuntimeError("request failed") from exc
    except RuntimeError as exc:
        record_runtime_error("route", exc, 1)
    raw = (tmp_path / "logs/etf_monitor/runtime-errors.jsonl").read_text(encoding="utf-8")
    assert token not in raw
    assert "[REDACTED]" in raw
    assert "ValueError" in raw and "RuntimeError" in raw


def test_opening_month_empty_response_only_allows_proven_closed_day(tmp_path):
    from urllib.parse import urlencode

    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, params=None, timeout=None):
            if "STOCK_DAY" in url:
                data = json.dumps({"stat": "很抱歉，沒有符合條件的資料!"}, ensure_ascii=False).encode()
            elif "holidaySchedule" in url:
                data = json.dumps([{"Name": "new year", "Date": "1150101"}]).encode()
            elif "TWT48U" in url:
                data = b"[]"
            elif "dividendList" in url:
                data = b"<table></table>"
            else:
                data = b"<html></html>"
            return SimpleNamespace(url=url + ("?" + urlencode(params) if params else ""),
                                   content=data, headers={}, raise_for_status=lambda: None)

    kwargs = dict(frozen_snapshot_id="test", frozen_manifest_sha256="A" * 64, session_factory=Session)
    build_daily_delta_bundle(start=date(2026, 11, 1), through=date(2026, 11, 1), output=tmp_path / "weekend", **kwargs)
    assert json.loads((tmp_path / "weekend/2026-11-01.json").read_text())["market_status"] == "CLOSED"
    from stock_ai_bot.monitoring.etf_monitor.data import load_daily_delta_file
    load_daily_delta_file(tmp_path / "weekend/2026-11-01.json", expected_date=date(2026, 11, 1),
                         snapshot_id="test", manifest_sha256="A" * 64)
    with pytest.raises(EtfMonitorDataError):
        build_daily_delta_bundle(start=date(2026, 11, 2), through=date(2026, 11, 2), output=tmp_path / "weekday", **kwargs)
    with pytest.raises(EtfMonitorDataError, match="not OK"):
        parse_stock_day(b'{"stat":"service unavailable"}', request_url="https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=20261101&stockNo=0050")


def test_catchup_marks_lost_preview_and_only_dispatches_due_unsucceeded_phases(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from stock_ai_bot.monitoring.etf_monitor.catchup import catch_up_today

    async def scenario():
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        state.startup_status = "READY"
        state.service.trading_day_predicate = lambda day: True
        calls = []
        async def dispatch(phase, **kwargs):
            calls.append((phase, kwargs))
            assert "補發通知" in kwargs["delivery_note"]
            state.store.begin_job(phase, date(2026, 11, 2))
            state.store.finish_job(phase, date(2026, 11, 2), succeeded=True)
        state.dispatch = dispatch
        now = datetime(2026, 11, 2, 15, tzinfo=ZoneInfo("Asia/Taipei"))
        await catch_up_today(state, now=now)
        await catch_up_today(state, now=now)
        assert [p for p, _ in calls] == [MonitorPhase.DECISION]
        assert len(bot.sent) == 1
        assert "當時行情未保存" in bot.sent[0]["text"]
        assert state.store.job_status(MonitorPhase.PREVIEW, now.date()) is None
        assert state.store.job_status(MonitorPhase.RECONCILIATION, now.date()) is None
    asyncio.run(scenario())


def test_closed_day_startup_never_emits_missed_trading_preview(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from stock_ai_bot.monitoring.etf_monitor.catchup import catch_up_today
    async def scenario():
        _, state, bot = make_runtime(tmp_path, lambda day: None)
        state.startup_status = "READY"
        state.service.trading_day_predicate = lambda day: False
        await catch_up_today(state, now=datetime(2026, 11, 1, 15, tzinfo=ZoneInfo("Asia/Taipei")))
        assert bot.sent == []
    asyncio.run(scenario())
