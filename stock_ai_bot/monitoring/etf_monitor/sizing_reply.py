"""Handle replies to delivered, current 0050 BUY messages with minimal receipts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from stock_ai_bot.workdata import get_workdata_path

from .config import ETF_MONITOR_CHAT_ID
from .core import STRATEGY_CORE_ID
from .execution import TAIPEI, refresh_execution_status
from .integration import RUNTIME_KEY
from .position_sizing import SizingInputError, estimate_position, parse_request, render_estimate, validate_prices
from .service import _material_state
from .sizing_config import ReplyConfig, load_reply_config


LOGGER = logging.getLogger(__name__)


class ReplyRejected(ValueError):
    pass


def find_reply_event(store, message_id):
    with store.transaction() as connection:
        rows = connection.execute(
            "SELECT e.event_id FROM events e JOIN outbox o USING(event_id) "
            "WHERE o.target_chat_id=? AND o.telegram_message_id=?",
            (ETF_MONITOR_CHAT_ID, str(message_id)),
        ).fetchall()
    return None if len(rows) != 1 else str(rows[0][0])


def resolve_quote(runtime, event_id: str, now: datetime):
    store = runtime.store
    refresh_execution_status(store, event_id=event_id, now=now)
    with store.transaction() as connection:
        event = connection.execute(
            "SELECT e.*,o.status AS delivery_status,o.target_chat_id,x.executable_status,x.expected_open_at "
            "FROM events e JOIN outbox o USING(event_id) LEFT JOIN execution_status x USING(event_id) "
            "WHERE e.event_id=?", (event_id,),
        ).fetchone()
        if (event is None or event["is_test"] or event["phase"] not in {"decision_1355", "reconciliation_2200"}
                or event["event_type"] not in {"BUY_NEXT_OPEN", "RECONCILIATION_CONFIRMED", "CORRECTION"}):
            raise ReplyRejected("請回覆正式進場通知；盤中預覽、測試或非進場訊息不能試算。")
        payload = json.loads(event["payload_json"])
        core = payload.get("core") or {}
        if (core.get("decision") != "BUY_NEXT_OPEN" or core.get("strategy") != STRATEGY_CORE_ID
                or core.get("symbol") != "0050"):
            raise ReplyRejected("這則訊息不是有效的 0050 正式進場通知。")
        if (event["delivery_status"] != "DELIVERED" or event["target_chat_id"] != ETF_MONITOR_CHAT_ID
                or event["executable_status"] not in {"PROVISIONAL_BEFORE_OPEN", "CONFIRMED_BEFORE_OPEN"}):
            raise ReplyRejected("原通知已過原定開盤、已更正／取消，或送達時間未確認，無法據以試算。")
        state = connection.execute("SELECT * FROM monitor_state WHERE singleton=1").fetchone()
        pending = connection.execute(
            "SELECT * FROM pending_actions WHERE (source_event_id=? OR reconciled_by_event_id=?) "
            "AND status='PENDING_NEXT_SESSION_OPEN' AND action='BUY_NEXT_OPEN'",
            (event_id, event_id),
        ).fetchone()
        if (state is None or pending is None or state["as_of"] != event["trading_date"]
                or state["decision"] != "BUY_NEXT_OPEN" or state["position_status"] != "LONG"
                or state["event_id"] not in {pending["source_event_id"], pending["reconciled_by_event_id"]}
                or _material_state(json.loads(state["core_state_json"])) != _material_state(core)):
            raise ReplyRejected("這則進場通知已非目前有效安排，請查看最新正式訊息。")
        job = connection.execute("SELECT status FROM job_runs WHERE run_key=?",
                                 (f"{event['phase']}:{event['trading_date']}",)).fetchone()
        current_delivery = connection.execute("SELECT status FROM outbox WHERE event_id=?", (state["event_id"],)).fetchone()
        if job is None or job[0] != "SUCCEEDED" or current_delivery is None or current_delivery[0] != "DELIVERED":
            raise ReplyRejected("最新正式通知或資料核對尚未完成，請稍後查看確認訊息。")
        confirmed = False
        if pending["reconciled_by_event_id"] is not None:
            confirmed = store._has_successful_matching_reconciliation(connection, pending)
            if not confirmed:
                raise ReplyRejected("晚間完整資料尚未通過核對，暫不報股數。")
        signal_day = datetime.fromisoformat(event["trading_date"]).date()
        if not confirmed and now >= datetime.combine(signal_day, runtime.config.schedule.reconciliation, TAIPEI):
            raise ReplyRejected("已到晚間核對時間，請等完整資料確認後再試算。")
        try:
            price = Decimal(str(core["signal_reference_price"]))
            stop = Decimal(str(core["fixed_initial_stop"]))
            validate_prices(price, stop)
        except (KeyError, TypeError, InvalidOperation):
            raise ReplyRejected("進場參考價或防守價資料不足，暫不報股數。") from None
        return {"price": price, "stop": stop, "regime": core.get("regime"),
                "trading_date": event["trading_date"], "confirmed": confirmed}


class ReplyReceipts:
    """48-hour idempotency metadata; never store amounts, prices, text or user IDs."""
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def claim(self, chat_id, message_id, now: datetime) -> str | None:
        key = hashlib.sha256(f"{chat_id}:{message_id}".encode()).hexdigest()
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS replies (request_key TEXT PRIMARY KEY, "
                               "created_at REAL NOT NULL,status TEXT NOT NULL,response_id TEXT)")
            connection.execute("DELETE FROM replies WHERE created_at<?", (now.timestamp() - 48 * 3600,))
            inserted = connection.execute("INSERT OR IGNORE INTO replies VALUES(?,?,'RESERVED',NULL)",
                                          (key, now.timestamp())).rowcount
        return key if inserted else None

    def finish(self, key, status, response_id=None):
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.execute("UPDATE replies SET status=?,response_id=? WHERE request_key=?",
                               (status, None if response_id is None else str(response_id), key))


async def process_sizing_reply(update, context, *, config=None, receipts=None, clock=None) -> bool:
    """True means this ETF reply is consumed, including safe rejections."""
    current_time = clock or (lambda: datetime.now(TAIPEI))
    message = update.effective_message
    if message is None or str(message.chat_id) != ETF_MONITOR_CHAT_ID:
        return False
    reply = getattr(message, "reply_to_message", None)
    if reply is None or str(reply.chat_id) != ETF_MONITOR_CHAT_ID:
        return False
    sender = getattr(reply, "from_user", None)
    if sender is None or not sender.is_bot or sender.id != context.bot.id:
        return False
    runtime = context.application.bot_data.get(RUNTIME_KEY)
    try:
        event_id = None if runtime is None else find_reply_event(runtime.store, reply.message_id)
    except Exception as exc:
        LOGGER.warning("0050 sizing source lookup stopped: %s", type(exc).__name__)
        return True
    reply_text = getattr(reply, "text", None) or ""
    if event_id is None and not reply_text.startswith(("0050 趨勢策略｜", "0050 股數試算")):
        return False
    # Edits are never new cash requests; also prevent AI/date handlers consuming them.
    if getattr(update, "edited_message", None) is not None:
        return True
    ledger = receipts or ReplyReceipts(get_workdata_path("runtime/etf_monitor/sizing-replies.sqlite3"))
    key = None
    sending = False
    try:
        now = current_time()
        key = ledger.claim(message.chat_id, message.message_id, now)
        if key is None:
            return True
        cfg = config or load_reply_config()
        user = update.effective_user
        if not cfg.enabled:
            text = "0050 股數試算尚未啟用。"
        elif user is None or user.is_bot or user.id not in cfg.allowed_user_ids:
            text = "這個帳號尚未開放 0050 股數試算。"
        elif runtime is None or runtime.startup_status != "READY" or runtime.shutting_down:
            text = "0050 監控尚未就緒，暫不報股數。"
        elif message.date.tzinfo is None or not -timedelta(seconds=30) <= now - message.date <= timedelta(minutes=5):
            text = "這次現金回覆已過期，請重新回覆最新可用現金。"
        else:
            text = None
        if runtime is None:
            ledger.finish(key, "BEFORE_SEND_FAILED")
            return True
        await runtime.router.verify_route()
        if text is None:
            member = await context.bot.get_chat_member(chat_id=int(ETF_MONITOR_CHAT_ID), user_id=user.id)
            active = member.status in {"member", "administrator", "creator"} or (
                member.status == "restricted" and getattr(member, "is_member", False))
            if (not active or member.user.id != user.id or member.user.is_bot):
                text = "這個帳號目前不是 0050 群組有效成員，暫不報股數。"
        if text is None:
            if runtime.startup_status != "READY" or runtime.shutting_down:
                text = "0050 監控尚未就緒，暫不報股數。"
            elif current_time() - message.date > timedelta(minutes=5):
                text = "這次現金回覆已過期，請重新回覆最新可用現金。"
        if text is None:
            try:
                if event_id is None:
                    raise ReplyRejected("請回覆原本有效的正式進場通知，輸入可用現金或「現金 委託價」；此訊息不能作為試算依據。")
                # Recheck after the network await: EOD may cancel/correct while routing.
                quote = resolve_quote(runtime, event_id, current_time())
                cash, override = parse_request(message.text or "")
                value = estimate_position(cash, quote["price"] if override is None else override, quote["stop"])
                text = render_estimate(value, quote=quote, cash=cash, override=override is not None)
            except (ReplyRejected, SizingInputError) as exc:
                text = str(exc)
        sending = True
        if not text.startswith("0050 股數試算"):
            text = "0050 股數試算\n" + text
        response = await context.bot.send_message(chat_id=int(ETF_MONITOR_CHAT_ID), text=text,
            reply_to_message_id=message.message_id, allow_sending_without_reply=False,
            disable_web_page_preview=True, parse_mode=None)
        response_id = getattr(response, "message_id", None)
        if type(response_id) is not int or response_id <= 0:
            raise RuntimeError("sizing reply acknowledgement missing")
        ledger.finish(key, "SENT", response_id)
    except asyncio.CancelledError:
        if key is not None:
            ledger.finish(key, "UNCERTAIN" if sending else "BEFORE_SEND_FAILED")
        raise
    except Exception as exc:
        if key is not None:
            try:
                ledger.finish(key, "UNCERTAIN" if sending else "BEFORE_SEND_FAILED")
            except Exception:
                pass
        LOGGER.warning("0050 sizing reply stopped: %s", type(exc).__name__)
    return True


def register_position_sizing_handler(application, *, evidence_path: Path | None = None):
    from telegram.ext import ApplicationHandlerStop, MessageHandler, filters

    async def handler(update, context):
        if await process_sizing_reply(update, context):
            raise ApplicationHandlerStop

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.REPLY & filters.Chat(chat_id=int(ETF_MONITOR_CHAT_ID)),
        handler), group=-1)
    if evidence_path is not None:
        try:
            cfg = load_reply_config()
            root = Path(__file__).parent
            evidence = {"status": "REGISTERED", "pid": os.getpid(), "group": -1,
                        "registered_at": datetime.now(TAIPEI).isoformat(), "enabled": cfg.enabled,
                        "allowed_user_count": len(cfg.allowed_user_ids),
                        "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                          for name in ("sizing_reply.py", "sizing_config.py", "position_sizing.py", "execution.py")}}
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            LOGGER.warning("0050 sizing registration evidence unavailable: %s", type(exc).__name__)
