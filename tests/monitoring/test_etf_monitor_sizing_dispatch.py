"""Real PTB Application/Update/HTTP serialization, isolated Telegram transport."""
import ast
import asyncio
import json
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, filters
from telegram.request import BaseRequest

from stock_ai_bot.monitoring.etf_monitor import sizing_reply
from stock_ai_bot.monitoring.etf_monitor.integration import RUNTIME_KEY
from stock_ai_bot.monitoring.etf_monitor.routing import FailClosedTelegramRouter
from test_etf_monitor_sizing_reply import CHAT, CFG, TIME, USER, fixture


class Request(BaseRequest):
    def __init__(self):
        self.methods = []
        self.messages = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self): pass
    async def shutdown(self): pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.methods.append(endpoint)
        parameters = {} if request_data is None else request_data.parameters
        bot = {"id": 12345, "is_bot": True, "first_name": "Fixture", "username": "fixture_bot"}
        user = {"id": USER, "is_bot": False, "first_name": "Fixture user"}
        chat = {"id": CHAT, "type": "group", "title": "Isolated fixture"}
        if endpoint == "getMe": result = bot
        elif endpoint == "getChat":
            result = dict(chat, accent_color_id=0, max_reaction_count=1,
                          accepted_gift_types={"unlimited_gifts": False, "limited_gifts": False,
                                               "unique_gifts": False, "premium_subscription": False,
                                               "gifts_from_channels": False})
        elif endpoint == "getChatMember": result = {"status": "member", "user": user}
        elif endpoint == "sendMessage":
            self.messages.append(parameters)
            result = {"message_id": 900 + len(self.messages), "date": int(TIME.timestamp()),
                      "chat": chat, "from": bot, "text": parameters["text"]}
        else: raise AssertionError("unexpected transport operation")
        return 200, json.dumps({"ok": True, "result": result}).encode()


def make_update(bot, *, source=201, message_id=500, user_id=USER, text="100000"):
    chat = {"id": CHAT, "type": "group", "title": "Fixture"}
    return Update.de_json({"update_id": message_id, "message": {
        "message_id": message_id, "date": int(TIME.timestamp()), "chat": chat,
        "from": {"id": user_id, "is_bot": False, "first_name": "Fixture user"}, "text": text,
        "reply_to_message": {"message_id": source, "date": int(TIME.timestamp()), "chat": chat,
            "from": {"id": 12345, "is_bot": True, "first_name": "Fixture", "username": "fixture_bot"},
            "text": "0050 趨勢策略｜觸發進場" if source == 201 else "AI 選單"},
    }}, bot)


def test_actual_dispatch_consumes_owned_replies_and_preserves_other_handlers(tmp_path, monkeypatch):
    from stock_ai_bot.monitoring.etf_monitor import store
    monkeypatch.setattr(store, "_utc_now", lambda: TIME.replace(hour=14).isoformat())
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return TIME.astimezone(tz)
    monkeypatch.setattr(sizing_reply, "datetime", Clock)
    monkeypatch.setattr(sizing_reply, "load_reply_config", lambda: CFG)
    monkeypatch.setattr(sizing_reply, "get_workdata_path", lambda _: tmp_path / "receipts.sqlite3")
    async def scenario():
        runtime, _, _ = await fixture(tmp_path)
        request = Request()
        app = (Application.builder().token("12345:isolated-fixture-only")
               .request(request).get_updates_request(request).job_queue(None).build())
        sizing_reply.register_position_sizing_handler(app, evidence_path=tmp_path / "handler.json")
        evidence = json.loads((tmp_path / "handler.json").read_text())
        assert evidence["status"] == "REGISTERED" and evidence["enabled"] and evidence["group"] == -1
        assert len(evidence["source_sha256"]) == 4 and "allowed_user_ids" not in evidence
        consumed_elsewhere = []
        async def other(update, context): consumed_elsewhere.append(update.update_id)
        app.add_handler(MessageHandler(filters.TEXT, other))
        app.add_handler(MessageHandler(filters.TEXT, other), group=1)
        app.bot_data[RUNTIME_KEY] = runtime
        runtime.router = FailClosedTelegramRouter(config=runtime.config, store=runtime.store, bot=app.bot)
        async with app:
            before = runtime.store.latest_state()
            await app.process_update(make_update(app.bot))
            await app.process_update(make_update(app.bot))
            assert len(request.messages) == 1 and "831 股" in request.messages[0]["text"]
            assert not consumed_elsewhere
            parameters = request.messages[0]
            assert parameters["chat_id"] == CHAT
            assert parameters["reply_parameters"]["message_id"] == 500
            assert parameters["reply_parameters"]["allow_sending_without_reply"] is False
            assert {"getChat", "getChatMember", "sendMessage"} <= set(request.methods)
            await app.process_update(make_update(app.bot, message_id=501, user_id=43))
            assert "可買股數" not in request.messages[-1]["text"] and not consumed_elsewhere
            await app.process_update(make_update(app.bot, source=999, message_id=502, text="ordinary input"))
            assert consumed_elsewhere == [502, 502]
            assert runtime.store.latest_state() == before
    asyncio.run(scenario())


def test_main_registers_the_interceptor_before_existing_text_handlers():
    tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    register = [node for node in ast.walk(main) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "register_position_sizing_handler"]
    ai = [node for node in ast.walk(main) if isinstance(node, ast.Call)
          and isinstance(node.func, ast.Name) and node.func.id == "build_research_handlers"]
    assert len(register) == 1 and register[0].lineno < ai[0].lineno
