from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest
from telegram.error import TimedOut

from stock_ai_bot.monitoring.etf_monitor import store as store_module
from stock_ai_bot.monitoring.etf_monitor.config import ETF_MONITOR_CHAT_ID, load_etf_monitor_config
from stock_ai_bot.monitoring.etf_monitor.integration import RUNTIME_KEY
from stock_ai_bot.monitoring.etf_monitor.models import Decision, MonitorPhase
from stock_ai_bot.monitoring.etf_monitor.routing import FailClosedTelegramRouter
from stock_ai_bot.monitoring.etf_monitor.service import EtfMonitorService
from stock_ai_bot.monitoring.etf_monitor.sizing_config import ReplyConfig, load_reply_config
from stock_ai_bot.monitoring.etf_monitor.sizing_reply import ReplyReceipts, process_sizing_reply
from stock_ai_bot.monitoring.etf_monitor.store import EtfMonitorStore
from test_etf_monitor_service import DAY, NOW, _core, _snapshot


TIME = NOW.replace(hour=15, minute=0)
USER = 42
CHAT = int(ETF_MONITOR_CHAT_ID)
CFG = ReplyConfig(True, frozenset({USER}))


class Bot:
    id = 12345
    def __init__(self):
        self.sent = []
        self.attempts = 0
        self.member_status = "member"
        self.fail = False
        self.after_membership = None

    async def get_chat(self, **kwargs):
        return NS(id=CHAT, type="group")

    async def get_chat_member(self, **kwargs):
        if self.after_membership is not None:
            self.after_membership()
        return NS(status=self.member_status, user=NS(id=USER, is_bot=False))

    async def send_message(self, **kwargs):
        self.attempts += 1
        if self.fail:
            raise TimedOut("test ambiguous send")
        self.sent.append(kwargs)
        return NS(message_id=1000 + len(self.sent))


@pytest.fixture(autouse=True)
def fixed_receipt_clock(monkeypatch):
    monkeypatch.setattr(store_module, "_utc_now", lambda: NOW.isoformat())


async def fixture(tmp_path, *, eod=False, phase=MonitorPhase.DECISION, decision=Decision.BUY_NEXT_OPEN):
    cfg = replace(load_etf_monitor_config(), enabled=True)
    store = EtfMonitorStore(tmp_path / "signals.sqlite3")
    core = replace(_core(decision, reference_price=D("120")), fixed_initial_stop=D("108") if decision is Decision.BUY_NEXT_OPEN else None)
    class Data:
        async def load(self, phase, day):
            snapshot = _snapshot(complete=phase is MonitorPhase.RECONCILIATION)
            return replace(snapshot, raw_rows=({"date": DAY, "open": 118, "high": 122, "low": 116, "close": 120},))
    service = EtfMonitorService(config=cfg, store=store, data_provider=Data(), router=None,
        core=NS(evaluate=lambda _: core), trading_day_predicate=lambda _: True)
    outcome = await service.run(phase, now=NOW)
    source = outcome.event_ids[0]
    store.claim_outbox(source)
    store.mark_outbox_delivered(source, "201")
    if eod:
        outcome = await service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22, minute=0))
        store.claim_outbox(outcome.event_ids[0])
        store.mark_outbox_delivered(outcome.event_ids[0], "202")
    bot = Bot()
    router = FailClosedTelegramRouter(config=cfg, store=store, bot=bot)
    runtime = NS(config=cfg, store=store, service=service, router=router, startup_status="READY", shutting_down=False)
    context = NS(bot=bot, application=NS(bot_data={RUNTIME_KEY: runtime}), user_data={"awaiting_scan_date": True})
    return runtime, context, source


def update(*, text="100000", source_id=201, message_id=500, user_id=USER, chat_id=CHAT, when=TIME):
    reply = NS(chat_id=CHAT, message_id=source_id, from_user=NS(id=Bot.id, is_bot=True), text="0050 趨勢策略｜觸發進場")
    message = NS(chat_id=chat_id, message_id=message_id, text=text, reply_to_message=reply, date=when)
    return NS(effective_message=message, effective_user=NS(id=user_id, is_bot=False), edited_message=None)


async def process(tmp_path, context, request=None, *, now=TIME, config=CFG):
    return await process_sizing_reply(request or update(), context, config=config,
        receipts=ReplyReceipts(tmp_path / "receipts.sqlite3"), clock=lambda: now)


def test_current_buy_reply_returns_831_in_same_group_without_state_or_cash_persistence(tmp_path):
    async def scenario():
        state, ctx, _ = await fixture(tmp_path)
        before = state.store.latest_state()
        with state.store.transaction() as c:
            audit_before = {name: [tuple(row) for row in c.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
                            for name in ("events", "outbox", "monitor_state", "pending_actions")}
        assert await process(tmp_path, ctx)
        text = ctx.bot.sent[0]["text"]
        assert "831 股" in text and "99,959.44" in text and "仍待晚間" in text
        assert ctx.bot.sent[0]["chat_id"] == CHAT and ctx.bot.sent[0]["reply_to_message_id"] == 500
        assert state.store.latest_state() == before and ctx.user_data == {"awaiting_scan_date": True}
        with state.store.transaction() as c:
            assert audit_before == {name: [tuple(row) for row in c.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
                                    for name in audit_before}
        with sqlite3.connect(tmp_path / "receipts.sqlite3") as c:
            assert [row[1] for row in c.execute("PRAGMA table_info(replies)")] == ["request_key", "created_at", "status", "response_id"]
            assert c.execute("SELECT status FROM replies").fetchone()[0] == "SENT"
        assert b"100000" not in (tmp_path / "receipts.sqlite3").read_bytes()
    asyncio.run(scenario())


@pytest.mark.parametrize("source_id", [201, 202])
def test_unchanged_eod_keeps_both_delivered_buy_messages_valid(tmp_path, source_id):
    async def scenario():
        _, ctx, _ = await fixture(tmp_path, eod=True)
        now = NOW.replace(hour=23, minute=0)
        assert await process(tmp_path, ctx, update(source_id=source_id, when=now), now=now)
        assert "831 股" in ctx.bot.sent[0]["text"] and "仍待晚間" not in ctx.bot.sent[0]["text"]
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["preview", "hold", "uncertain", "manual", "late", "expired", "missing_stop", "quarantined", "eod_missing"])
def test_ineligible_messages_never_report_quantity(tmp_path, reason):
    async def scenario():
        state, ctx, source = await fixture(tmp_path,
            phase=MonitorPhase.PREVIEW if reason == "preview" else MonitorPhase.DECISION,
            decision=Decision.HOLD if reason == "hold" else Decision.BUY_NEXT_OPEN)
        now = TIME
        with state.store.transaction() as c:
            if reason in {"uncertain", "manual"}:
                c.execute("UPDATE outbox SET status=?", ("DELIVERY_UNCERTAIN" if reason == "uncertain" else "DELIVERED_MANUAL",))
            if reason == "late":
                c.execute("UPDATE outbox SET delivered_at=?", ((NOW + timedelta(days=1)).replace(hour=9, minute=1).isoformat(),))
            if reason == "missing_stop":
                raw = json.loads(c.execute("SELECT payload_json FROM events WHERE event_id=?", (source,)).fetchone()[0])
                raw["core"]["fixed_initial_stop"] = None
                c.execute("UPDATE events SET payload_json=? WHERE event_id=?", (json.dumps(raw), source))
            if reason == "quarantined":
                c.execute("UPDATE pending_actions SET status='QUARANTINED_RECONCILIATION_REQUIRED'")
        if reason == "expired":
            now = (NOW + timedelta(days=1)).replace(hour=9, minute=0)
        if reason == "eod_missing":
            now = NOW.replace(hour=23, minute=0)
        assert await process(tmp_path, ctx, update(when=now), now=now)
        assert len(ctx.bot.sent) == 1 and "可買股數" not in ctx.bot.sent[0]["text"]
    asyncio.run(scenario())


@pytest.mark.parametrize("correction", [Decision.HOLD, Decision.BUY_NEXT_OPEN])
def test_cancellation_or_corrected_buy_invalidates_old_source(tmp_path, correction):
    async def scenario():
        state, ctx, _ = await fixture(tmp_path)
        changed = replace(_core(correction, reference_price=D("121")), fixed_initial_stop=D("108") if correction is Decision.BUY_NEXT_OPEN else None)
        state.service.core = NS(evaluate=lambda _: changed)
        new = await state.service.run(MonitorPhase.RECONCILIATION, now=NOW.replace(hour=22, minute=0))
        state.store.claim_outbox(new.event_ids[0])
        state.store.mark_outbox_delivered(new.event_ids[0], "202")
        now = NOW.replace(hour=23, minute=0)
        await process(tmp_path, ctx, update(when=now), now=now)
        assert "可買股數" not in ctx.bot.sent[0]["text"]
        if correction is Decision.BUY_NEXT_OPEN:
            await process(tmp_path, ctx, update(source_id=202, message_id=501, when=now), now=now)
            assert "可買股數" in ctx.bot.sent[-1]["text"] and "121" in ctx.bot.sent[-1]["text"]
    asyncio.run(scenario())


def test_repeated_update_is_deduped_but_new_cash_and_price_are_recalculated(tmp_path):
    async def scenario():
        _, ctx, _ = await fixture(tmp_path)
        await process(tmp_path, ctx)
        await process(tmp_path, ctx)
        assert ctx.bot.attempts == 1
        await process(tmp_path, ctx, update(text="200000", message_id=501))
        await process(tmp_path, ctx, update(text="100000 123.4", message_id=502))
        assert "831 股" in ctx.bot.sent[0]["text"]
        assert "1,000 股" in ctx.bot.sent[1]["text"] and "零股" in ctx.bot.sent[1]["text"]
        assert "自填委託價：123.4" in ctx.bot.sent[2]["text"] and "831 股" not in ctx.bot.sent[2]["text"]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["other_chat", "other_source_chat", "other_bot", "no_reply", "unrelated", "other_user", "left", "stale_cash", "bad_price", "edited"])
def test_scoping_authorization_and_input_fail_closed(tmp_path, kind):
    async def scenario():
        _, ctx, _ = await fixture(tmp_path)
        req = update()
        if kind == "other_chat": req.effective_message.chat_id = -123456
        if kind == "other_source_chat": req.effective_message.reply_to_message.chat_id = -123456
        if kind == "other_bot": req.effective_message.reply_to_message.from_user.id = 765
        if kind == "no_reply": req.effective_message.reply_to_message = None
        if kind == "unrelated":
            req.effective_message.reply_to_message.message_id = 999
            req.effective_message.reply_to_message.text = "AI 選單"
        if kind == "other_user": req.effective_user.id = 43
        if kind == "left": ctx.bot.member_status = "left"
        if kind == "stale_cash": req.effective_message.date = TIME - timedelta(minutes=6)
        if kind == "bad_price": req.effective_message.text = "100000 120.03"
        if kind == "edited": req.edited_message = req.effective_message
        handled = await process(tmp_path, ctx, req)
        assert handled == (kind not in {"other_chat", "other_source_chat", "other_bot", "no_reply", "unrelated"})
        assert all("可買股數" not in row["text"] for row in ctx.bot.sent)
    asyncio.run(scenario())


def test_revalidation_after_route_detects_new_uncertainty_and_timeout_is_not_resent(tmp_path, caplog):
    async def scenario():
        state, ctx, _ = await fixture(tmp_path)
        def uncertain():
            with state.store.transaction() as c:
                c.execute("UPDATE outbox SET status='DELIVERY_UNCERTAIN'")
        ctx.bot.after_membership = uncertain
        await process(tmp_path, ctx)
        assert "可買股數" not in ctx.bot.sent[0]["text"]
        ctx.bot.after_membership = None
        ctx.bot.fail = True
        await process(tmp_path, ctx, update(message_id=501))
        await process(tmp_path, ctx, update(message_id=501))
        assert ctx.bot.attempts == 2
        with sqlite3.connect(tmp_path / "receipts.sqlite3") as c:
            assert c.execute("SELECT COUNT(*) FROM replies WHERE status='UNCERTAIN'").fetchone()[0] == 1
        assert "100000" not in caplog.text
    asyncio.run(scenario())


def test_separate_config_is_explicit_and_never_expands_to_all_group_members(tmp_path):
    path = tmp_path / "config.json"
    assert not load_reply_config(path).enabled
    for value in ({"enabled": True, "allowed_user_ids": []}, {"enabled": True, "allowed_user_ids": [True]}, {"enabled": True, "allowed_user_ids": [42], "other": 1}):
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError): load_reply_config(path)
    path.write_text(json.dumps({"enabled": True, "allowed_user_ids": [42]}))
    assert load_reply_config(path) == CFG


def test_reply_to_an_estimate_points_back_to_formal_buy_and_never_hits_other_handlers(tmp_path):
    async def scenario():
        _, ctx, _ = await fixture(tmp_path)
        request = update(source_id=999)
        request.effective_message.reply_to_message.text = "0050 股數試算｜前次結果"
        assert await process(tmp_path, ctx, request)
        assert "請回覆原本有效的正式進場通知" in ctx.bot.sent[0]["text"]
        assert "可買股數" not in ctx.bot.sent[0]["text"]
    asyncio.run(scenario())


def test_buy_help_is_removed_when_a_retry_crosses_the_original_open(tmp_path, monkeypatch):
    from stock_ai_bot.monitoring.etf_monitor import execution, sizing_config
    from test_etf_monitor_closeout import append_trade, OPEN
    monkeypatch.setattr(sizing_config, "load_reply_config", lambda: CFG)
    store = EtfMonitorStore(tmp_path / "message.sqlite3")
    append_trade(store)
    store.claim_outbox("trade")
    before = execution.prepare_execution_message(store, "trade", now=OPEN.replace(hour=8))
    assert "股數試算：回覆這則進場通知" in before
    store.mark_outbox_retry("trade", "route")
    store.claim_outbox("trade")
    after = execution.prepare_execution_message(store, "trade", now=OPEN.replace(hour=10))
    assert "股數試算" not in after and "原定開盤價已無法據以執行" in after


@pytest.mark.parametrize("change", ["cash_expired", "runtime_stopping", "open_passed"])
def test_network_wait_does_not_allow_a_stale_request_or_expired_window(tmp_path, change):
    async def scenario():
        state, ctx, _ = await fixture(tmp_path)
        clock = [TIME]
        def change_during_lookup():
            if change == "cash_expired": clock[0] += timedelta(minutes=6)
            if change == "runtime_stopping": state.shutting_down = True
            if change == "open_passed": clock[0] = (TIME + timedelta(days=1)).replace(hour=9, minute=0)
        ctx.bot.after_membership = change_during_lookup
        assert await process_sizing_reply(update(), ctx, config=CFG,
            receipts=ReplyReceipts(tmp_path / "receipts.sqlite3"), clock=lambda: clock[0])
        assert "可買股數" not in ctx.bot.sent[0]["text"]
    asyncio.run(scenario())
