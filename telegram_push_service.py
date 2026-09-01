from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from telegram import MessageEntity

from telegram_stock_formatting import TelegramTextChunk, prepare_telegram_chunks


TELEGRAM_SAFE_CHUNK_LIMIT = 4000
TELEGRAM_MAX_ATTEMPTS = 3
_BOT_TOKEN_RE = re.compile(r"(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}", re.IGNORECASE)


@dataclass(frozen=True)
class TelegramPushResult:
    ok: bool
    status: str
    chunk_count: int
    sent_chunk_count: int = 0
    message_ids: list[int] = field(default_factory=list)
    error_code: str | None = None
    error: str | None = None


def sanitize_telegram_error(error: object, *sensitive_values: object) -> str:
    text = _BOT_TOKEN_RE.sub("[REDACTED_TOKEN]", str(error or "Telegram request failed"))
    values = sorted(
        {str(value) for value in sensitive_values if value is not None and str(value)},
        key=len,
        reverse=True,
    )
    for value in values:
        text = text.replace(value, "[REDACTED]")
    return text[:500]


def prepare_telegram_push_chunks(
    text: str,
    limit: int = TELEGRAM_SAFE_CHUNK_LIMIT,
    *,
    chunk_prefix: str = "",
) -> list[TelegramTextChunk]:
    prefix = str(chunk_prefix or "")
    content_limit = limit - len(prefix)
    if content_limit <= 0:
        raise ValueError("Telegram chunk prefix must be shorter than the chunk limit")
    chunks = prepare_telegram_chunks(text, limit=content_limit)
    if not prefix:
        return chunks
    prefix_offset = len(prefix.encode("utf-16-le")) // 2
    return [
        TelegramTextChunk(
            text=prefix + chunk.text,
            entities=[_shift_entity(entity, prefix_offset) for entity in chunk.entities],
        )
        for chunk in chunks
    ]


async def send_telegram_message(
    bot: Any,
    chat_id: int | str,
    text: str,
    *,
    max_attempts: int = TELEGRAM_MAX_ATTEMPTS,
    retry_delay_seconds: float = 3.0,
    chunk_limit: int = TELEGRAM_SAFE_CHUNK_LIMIT,
    chunk_prefix: str = "",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **send_kwargs: Any,
) -> TelegramPushResult:
    if not str(text or "").strip():
        return TelegramPushResult(ok=True, status="empty", chunk_count=0)
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")

    chunks = prepare_telegram_push_chunks(str(text), limit=chunk_limit, chunk_prefix=chunk_prefix)
    message_ids: list[int] = []
    sent_chunk_count = 0

    for chunk_index, chunk in enumerate(chunks):
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                kwargs = dict(send_kwargs)
                if chunk.entities:
                    kwargs.pop("parse_mode", None)
                    kwargs["entities"] = chunk.entities
                response = await bot.send_message(chat_id=chat_id, text=chunk.text, **kwargs)
                message_id = _extract_message_id(response)
                if message_id is not None:
                    message_ids.append(message_id)
                sent_chunk_count += 1
                last_error = None
                break
            except Exception as exc:  # The caller receives only a sanitized summary.
                last_error = exc
                if attempt + 1 < max_attempts and retry_delay_seconds > 0:
                    await sleep(retry_delay_seconds)

        if last_error is not None:
            return TelegramPushResult(
                ok=False,
                status="failed",
                chunk_count=len(chunks),
                sent_chunk_count=sent_chunk_count,
                message_ids=message_ids,
                error_code=f"send_chunk_{chunk_index + 1}_failed",
                error=sanitize_telegram_error(last_error, chat_id),
            )

    return TelegramPushResult(
        ok=True,
        status="sent",
        chunk_count=len(chunks),
        sent_chunk_count=sent_chunk_count,
        message_ids=message_ids,
    )


def _extract_message_id(response: object) -> int | None:
    if response is None:
        return None
    value = response.get("message_id") if isinstance(response, dict) else getattr(response, "message_id", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _shift_entity(entity: MessageEntity, offset: int) -> MessageEntity:
    return MessageEntity(
        type=entity.type,
        offset=entity.offset + offset,
        length=entity.length,
        url=entity.url,
        user=entity.user,
        language=entity.language,
        custom_emoji_id=entity.custom_emoji_id,
    )
