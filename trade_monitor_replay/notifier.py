from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import Bot
from telegram.request import HTTPXRequest

from telegram_push_service import TelegramPushResult, sanitize_telegram_error, send_telegram_message
from trade_monitor.bridge import DeliveryFileLock

from .state import read_json, write_json_atomic


SendFunction = Callable[[str, str, str], Awaitable[TelegramPushResult]]


class ReplayNotificationError(RuntimeError):
    pass


class ReplayNotifier:
    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        state_path: Path,
        send_function: SendFunction | None = None,
    ) -> None:
        self._bot_token = str(bot_token or "").strip()
        self._chat_id = str(chat_id or "").strip()
        self._state_path = state_path
        self._send_function = send_function or _send_default
        if not self._bot_token:
            raise ReplayNotificationError("Telegram Bot Token尚未設定。")
        if not self._chat_id:
            raise ReplayNotificationError("模擬Telegram群組ID尚未設定；不得回退正式群組。")

    async def send(self, *, event_id: str, message: str) -> dict[str, Any]:
        if not str(message or "").strip():
            return {"ok": True, "status": "empty", "event_id": event_id, "chunk_count": 0}
        if self._bot_token in message or self._chat_id in message:
            raise ReplayNotificationError("模擬訊息含Telegram憑證或聊天室ID。")
        lock_path = self._state_path.with_suffix(self._state_path.suffix + ".lock")
        with DeliveryFileLock(lock_path):
            state = read_json(self._state_path, {"version": 1, "events": {}})
            if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
                raise ReplayNotificationError("模擬Telegram去重狀態損壞。")
            previous = state["events"].get(event_id)
            if isinstance(previous, dict) and previous.get("status") == "sent":
                return {
                    "ok": True,
                    "status": "duplicate",
                    "event_id": event_id,
                    "chunk_count": int(previous.get("chunk_count") or 0),
                    "message_ids": list(previous.get("message_ids") or []),
                }
            result = await self._send_function(self._bot_token, self._chat_id, message)
            state["events"][event_id] = {
                "event_id": event_id,
                "status": result.status,
                "chunk_count": result.chunk_count,
                "message_ids": result.message_ids,
                "error_code": result.error_code,
            }
            if len(state["events"]) > 2000:
                state["events"] = dict(list(state["events"].items())[-2000:])
            write_json_atomic(self._state_path, state)
        return {
            "ok": result.ok,
            "status": result.status,
            "event_id": event_id,
            "chunk_count": result.chunk_count,
            "sent_chunk_count": result.sent_chunk_count,
            "message_ids": result.message_ids,
            "error_code": result.error_code,
            "error": result.error,
        }


def replay_event_id(
    *,
    rule_version: str,
    instrument: str,
    target_date: str,
    bar_time: str,
    decision: str,
    message: str,
) -> str:
    material = "\n".join(("trade-monitor-replay-v1", rule_version, instrument, target_date, bar_time, decision, message))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _send_default(token: str, chat_id: str, message: str) -> TelegramPushResult:
    request = HTTPXRequest(connect_timeout=5, read_timeout=15, write_timeout=15, pool_timeout=5)
    bot = Bot(token=token, request=request, get_updates_request=request)
    initialized = False
    try:
        await request.initialize()
        initialized = True
        return await send_telegram_message(bot, chat_id, message, chunk_prefix="🔥 ")
    except Exception as exc:
        return TelegramPushResult(
            ok=False,
            status="failed",
            chunk_count=0,
            error_code="telegram_client_failed",
            error=sanitize_telegram_error(exc, token, chat_id),
        )
    finally:
        if initialized:
            try:
                await request.shutdown()
            except Exception:
                pass


def load_bot_token(config_path: Path) -> str:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayNotificationError("無法讀取既有Telegram設定。") from exc
    token = str(payload.get("api_token") or "").strip() if isinstance(payload, dict) else ""
    if not token:
        raise ReplayNotificationError("既有Telegram Bot Token尚未設定。")
    return token
