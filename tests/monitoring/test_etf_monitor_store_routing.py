from __future__ import annotations

import asyncio
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

from stock_ai_bot.monitoring.etf_monitor.config import load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.errors import EtfMonitorRoutingError, EtfMonitorStateError
from stock_ai_bot.monitoring.etf_monitor.models import MonitorPhase
from stock_ai_bot.monitoring.etf_monitor.routing import FailClosedTelegramRouter
from stock_ai_bot.monitoring.etf_monitor.store import EtfMonitorStore


def _append(store: EtfMonitorStore, event_id: str = "evt-1") -> None:
    store.append_event_with_outbox(
        event_id=event_id,
        phase=MonitorPhase.DECISION,
        trading_date=date(2026, 10, 21),
        event_type="HOLD",
        payload={"decision": "HOLD"},
        target_chat_id="-5527055845",
        body="shadow",
    )


def _outbox(path, event_id="evt-1"):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        return dict(row)


def test_schema_idempotency_retry_and_restart_recovery(tmp_path) -> None:
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    _append(store)
    _append(store)
    with pytest.raises(EtfMonitorStateError):
        store.append_event_with_outbox(
            event_id="evt-1",
            phase=MonitorPhase.DECISION,
            trading_date=date(2026, 10, 21),
            event_type="HOLD",
            payload={"decision": "BUY"},
            target_chat_id="-5527055845",
            body="different",
        )
    claimed = store.claim_outbox("evt-1")
    assert claimed is not None and claimed.attempts == 1
    store.recover_after_restart()
    assert _outbox(store.path)["status"] == "DELIVERY_UNCERTAIN"
    assert store.claim_outbox("evt-1") is None
    store.resolve_uncertain_delivery("evt-1", delivered=False)
    claimed_again = store.claim_outbox("evt-1")
    assert claimed_again is not None and claimed_again.attempts == 2


def test_concurrent_claim_has_one_winner(tmp_path) -> None:
    path = tmp_path / "monitor.sqlite3"
    first = EtfMonitorStore(path)
    second = EtfMonitorStore(path)
    _append(first)
    assert first.claim_outbox("evt-1") is not None
    assert second.claim_outbox("evt-1") is None


def test_newer_database_migration_is_rejected(tmp_path) -> None:
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_meta VALUES('schema_version', '999')")
    with pytest.raises(EtfMonitorStateError):
        EtfMonitorStore(path)


def test_routing_smoke_reservation_is_atomic_and_one_shot(tmp_path) -> None:
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    assert store.reserve_routing_smoke("-5527055845") is True
    assert store.reserve_routing_smoke("-5527055845") is False
    store.mark_routing_smoke_sent("-5527055845", "123")
    assert store.routing_smoke_sent() is True


class _Bot:
    def __init__(self, *, returned_id="-5527055845", fail_send=False):
        self.returned_id = returned_id
        self.fail_send = fail_send
        self.sent = []

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=self.returned_id, type="supergroup")

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        if self.fail_send:
            raise RuntimeError("send failed")
        return SimpleNamespace(message_id=987)


def test_route_mismatch_fails_closed_without_fallback(tmp_path) -> None:
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    _append(store)
    bot = _Bot(returned_id="-9999999999")
    router = FailClosedTelegramRouter(
        config=load_etf_monitor_config(), store=store, bot=bot
    )
    with pytest.raises(EtfMonitorRoutingError):
        asyncio.run(router.deliver("evt-1"))
    assert bot.sent == []
    assert _outbox(store.path)["status"] == "RETRY"


def test_send_failure_is_quarantined_until_manual_non_delivery_confirmation(tmp_path) -> None:
    store = EtfMonitorStore(tmp_path / "monitor.sqlite3")
    _append(store)
    failed = _Bot(fail_send=True)
    with pytest.raises(EtfMonitorRoutingError):
        asyncio.run(FailClosedTelegramRouter(
            config=load_etf_monitor_config(), store=store, bot=failed
        ).deliver("evt-1"))
    assert failed.sent[0]["chat_id"] == "-5527055845"
    assert _outbox(store.path)["status"] == "DELIVERY_UNCERTAIN"
    assert asyncio.run(FailClosedTelegramRouter(
        config=load_etf_monitor_config(), store=store, bot=_Bot()
    ).deliver("evt-1")) is False
    store.resolve_uncertain_delivery("evt-1", delivered=False)
    success = _Bot()
    assert asyncio.run(FailClosedTelegramRouter(
        config=load_etf_monitor_config(), store=store, bot=success
    ).deliver("evt-1"))
    row = _outbox(store.path)
    assert row["status"] == "DELIVERED"
    assert row["telegram_message_id"] == "987"
    assert row["attempts"] == 2
