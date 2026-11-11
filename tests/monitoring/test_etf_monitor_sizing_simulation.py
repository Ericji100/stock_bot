import asyncio
import json
import sqlite3
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from stock_ai_bot.monitoring.etf_monitor.sizing_simulation import SizingSimulations, SimulationRejected, RESULT_HEADER
from stock_ai_bot.monitoring.etf_monitor.sizing_reply import ReplyReceipts, process_sizing_reply
from test_etf_monitor_sizing_reply import CHAT, CFG, TIME, USER, Bot, fixture, update
from test_etf_monitor_sizing_dispatch import Request, make_update


def delivered(registry, *, now=TIME, message_id=701):
    body = registry.reserve('one-authorized-request', Bot.id, now)
    registry.sending('one-authorized-request')
    registry.delivered('one-authorized-request', message_id)
    return body


def test_session_is_single_send_expiring_and_bound_to_exact_bot_message(tmp_path):
    registry = SizingSimulations(tmp_path / 'sim.sqlite3')
    body = delivered(registry)
    with pytest.raises(sqlite3.IntegrityError):
        registry.reserve('one-authorized-request', Bot.id, TIME)
    args = dict(chat_id=CHAT, bot_id=Bot.id, message_id=701, text=body, now=TIME)
    assert registry.resolve(**args)['simulation'] is True
    for changed in ({'chat_id': -99}, {'bot_id': 99}, {'message_id': 702}, {'text': body + 'edited'},
                    {'now': TIME - timedelta(seconds=1)}, {'now': TIME + timedelta(hours=2)}):
        with pytest.raises(SimulationRejected): registry.resolve(**(args | changed))


def test_ambiguous_send_never_becomes_a_replyable_simulation(tmp_path):
    registry = SizingSimulations(tmp_path / 'sim.sqlite3')
    body = registry.reserve('uncertain', Bot.id, TIME)
    registry.sending('uncertain')
    registry.uncertain('uncertain', 'TimedOut')
    with pytest.raises(ValueError): registry.sending('uncertain')
    with pytest.raises(ValueError): registry.delivered('uncertain', 701)
    with pytest.raises(SimulationRejected):
        registry.resolve(chat_id=CHAT, bot_id=Bot.id, message_id=701, text=body, now=TIME)


@pytest.mark.parametrize('case', ['valid', 'override', 'expired', 'other_user', 'left', 'forged', 'bad_price', 'unknown_id', 'result_reply'])
def test_replyable_simulations_use_same_calculator_and_no_formal_state(tmp_path, case):
    async def scenario():
        runtime, context, _ = await fixture(tmp_path)
        registry = SizingSimulations(tmp_path / 'sim.sqlite3')
        body = delivered(registry)
        request = update(source_id=701)
        request.effective_message.reply_to_message.text = body
        now = TIME
        if case == 'override': request.effective_message.text = '100000 123.4'
        if case == 'expired':
            now += timedelta(hours=2)
            request.effective_message.date = now
        if case == 'other_user': request.effective_user.id = USER + 1
        if case == 'left': context.bot.member_status = 'left'
        if case == 'forged': request.effective_message.reply_to_message.text += 'changed'
        if case == 'bad_price': request.effective_message.text = '100000 120.03'
        if case == 'unknown_id': request.effective_message.reply_to_message.message_id = 799
        if case == 'result_reply':
            request.effective_message.reply_to_message.message_id = 799
            request.effective_message.reply_to_message.text = RESULT_HEADER
        with runtime.store.transaction() as c:
            before = {name: [tuple(row) for row in c.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
                      for name in ('events','outbox','monitor_state','pending_actions','execution_status','job_runs')}
        args = dict(config=CFG, receipts=ReplyReceipts(tmp_path / 'receipts.sqlite3'),
                    simulations=registry, clock=lambda: now)
        assert await process_sizing_reply(request, context, **args)
        assert context.bot.sent[0]['text'].startswith(RESULT_HEADER)
        if case in {'valid', 'override'}:
            assert '可買股數：' in context.bot.sent[0]['text']
            assert ('831 股' in context.bot.sent[0]['text']) == (case == 'valid')
            assert '真實成交或持倉變更' in context.bot.sent[0]['text']
            await process_sizing_reply(request, context, **args)
            assert len(context.bot.sent) == 1
        else:
            assert '可買股數：' not in context.bot.sent[0]['text']
        with runtime.store.transaction() as c:
            assert before == {name: [tuple(row) for row in c.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
                              for name in before}
        with sqlite3.connect(registry.path) as c:
            assert 'cash' not in [row[1] for row in c.execute('PRAGMA table_info(simulations)')]
    asyncio.run(scenario())


def test_actual_application_dispatches_simulation_while_formal_state_is_hold(tmp_path, monkeypatch):
    from telegram.ext import Application, MessageHandler, filters
    from stock_ai_bot.monitoring.etf_monitor import sizing_reply
    from stock_ai_bot.monitoring.etf_monitor.integration import RUNTIME_KEY
    from stock_ai_bot.monitoring.etf_monitor.models import Decision
    from stock_ai_bot.monitoring.etf_monitor.routing import FailClosedTelegramRouter

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return TIME.astimezone(tz)

    monkeypatch.setattr(sizing_reply, 'datetime', Clock)
    monkeypatch.setattr(sizing_reply, 'load_reply_config', lambda: CFG)
    monkeypatch.setattr(sizing_reply, 'get_workdata_path', lambda p: tmp_path / Path(p).name)

    async def scenario():
        runtime, _, _ = await fixture(tmp_path, decision=Decision.HOLD)
        registry = SizingSimulations(tmp_path / 'sizing-simulations.sqlite3')
        body = delivered(registry)
        request = Request()
        app = (Application.builder().token('12345:isolated-fixture-only')
               .request(request).get_updates_request(request).job_queue(None).build())
        sizing_reply.register_position_sizing_handler(app)
        consumed_elsewhere = []
        async def other(update, context): consumed_elsewhere.append(update.update_id)
        app.add_handler(MessageHandler(filters.TEXT, other))
        app.add_handler(MessageHandler(filters.TEXT, other), group=1)
        app.bot_data[RUNTIME_KEY] = runtime
        runtime.router = FailClosedTelegramRouter(config=runtime.config, store=runtime.store, bot=app.bot)
        with runtime.store.transaction() as c:
            before = {n: [tuple(r) for r in c.execute(f'SELECT * FROM "{n}" ORDER BY rowid')]
                      for n in ('events', 'outbox', 'monitor_state', 'pending_actions', 'execution_status', 'job_runs')}
        async with app:
            event = make_update(app.bot, source=701, source_text=body)
            await app.process_update(event)
            await app.process_update(event)
            assert len(request.messages) == 1
            assert request.messages[0]['text'].startswith(RESULT_HEADER)
            assert '831 股' in request.messages[0]['text']
            assert request.messages[0]['reply_parameters']['message_id'] == 500
            assert not consumed_elsewhere
            await app.process_update(make_update(app.bot, source=10502, message_id=501,
                source_text='0050 趨勢策略｜模擬進場（非真實交易訊號）'))
            assert '可買股數' not in request.messages[-1]['text']
            assert not consumed_elsewhere
        with runtime.store.transaction() as c:
            assert before == {n: [tuple(r) for r in c.execute(f'SELECT * FROM "{n}" ORDER BY rowid')] for n in before}
    asyncio.run(scenario())
