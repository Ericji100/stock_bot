from __future__ import annotations

import asyncio
import re
from typing import Any

from .config import EtfMonitorConfig
from .errors import EtfMonitorRoutingError
from .store import EtfMonitorStore


_TOKENISH = re.compile(r"(?:bot)?[0-9]{6,}:[A-Za-z0-9_-]{20,}", re.IGNORECASE)


def _safe_error(exc: BaseException) -> str:
    text = _TOKENISH.sub("[REDACTED]", str(exc)).replace("\r", " ").replace("\n", " ")
    return f"{type(exc).__name__}: {text}"[:500]


class FailClosedTelegramRouter:
    """Sends to one verified chat and quarantines ambiguous delivery results."""

    def __init__(self, *, config: EtfMonitorConfig, store: EtfMonitorStore, bot: Any):
        self.config = config
        self.store = store
        self.bot = bot

    async def verify_route(self) -> None:
        try:
            chat = await self.bot.get_chat(chat_id=self.config.chat_id)
        except Exception as exc:
            raise EtfMonitorRoutingError(f"ETF getChat verification failed: {_safe_error(exc)}") from exc
        if str(getattr(chat, "id", "")) != self.config.chat_id:
            raise EtfMonitorRoutingError("ETF getChat identity mismatch")
        chat_type = str(getattr(chat, "type", ""))
        if chat_type not in {"group", "supergroup"}:
            raise EtfMonitorRoutingError("ETF target is not a Telegram group")

    async def deliver(self, event_id: str | None = None) -> bool:
        item = self.store.claim_outbox(event_id)
        if item is None:
            return False
        if item.target_chat_id != self.config.chat_id:
            self.store.mark_outbox_retry(item.event_id, "TARGET_CHAT_ID_MISMATCH")
            raise EtfMonitorRoutingError("ETF outbox target does not match dedicated chat")
        try:
            await self.verify_route()
        except asyncio.CancelledError:
            self.store.mark_outbox_retry(item.event_id, "SHUTDOWN_BEFORE_SEND")
            raise
        except Exception:
            self.store.mark_outbox_retry(item.event_id, "ROUTE_VERIFICATION_FAILED_BEFORE_SEND")
            raise
        try:
            message = await self.bot.send_message(
                chat_id=self.config.chat_id,
                text=item.body,
                disable_web_page_preview=True,
            )
            message_id = getattr(message, "message_id", None)
            if message_id is None:
                raise EtfMonitorRoutingError("Telegram send returned no message id")
        except asyncio.CancelledError:
            self.store.mark_outbox_uncertain(item.event_id, "SEND_CANCELLED_DELIVERY_UNCERTAIN")
            raise
        except Exception as exc:
            self.store.mark_outbox_uncertain(item.event_id, _safe_error(exc))
            if isinstance(exc, EtfMonitorRoutingError):
                raise
            raise EtfMonitorRoutingError(
                f"ETF Telegram send result is uncertain; manual review required: {_safe_error(exc)}"
            ) from exc
        self.store.mark_outbox_delivered(item.event_id, str(message_id))
        return True

    async def drain(self, *, limit: int = 20) -> int:
        delivered = 0
        for _ in range(limit):
            if not await self.deliver():
                break
            delivered += 1
        return delivered
