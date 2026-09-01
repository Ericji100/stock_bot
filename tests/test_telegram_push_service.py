from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram import MessageEntity

from telegram_push_service import prepare_telegram_push_chunks, sanitize_telegram_error, send_telegram_message
from telegram_stock_formatting import prepare_telegram_chunks, prepare_telegram_text


class FakeBot:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise RuntimeError("temporary Telegram failure")
        return SimpleNamespace(message_id=100 + len(self.calls))


def test_markdown_bold_uses_utf16_offsets_for_chinese_and_emoji():
    chunk = prepare_telegram_text("📈 前綴 **台指期偏空** / +12")

    assert chunk.text == "📈 前綴 台指期偏空 / +12"
    assert len(chunk.entities) == 1
    assert chunk.entities[0].type == MessageEntity.BOLD
    assert chunk.entities[0].offset == len("📈 前綴 ".encode("utf-16-le")) // 2
    assert chunk.entities[0].length == len("台指期偏空".encode("utf-16-le")) // 2


def test_unclosed_or_empty_markdown_bold_is_kept_as_plain_text():
    assert prepare_telegram_text("未閉合 **內容").text == "未閉合 **內容"
    assert prepare_telegram_text("空標記 ****").text == "空標記 ****"


def test_html_like_text_remains_literal_and_is_not_parse_mode_input():
    bot = FakeBot()
    result = asyncio.run(
        send_telegram_message(bot, "chat", "<b>不可注入</b> & **真正粗體**", parse_mode="HTML")
    )

    assert result.ok is True
    assert bot.calls[0]["text"] == "<b>不可注入</b> & 真正粗體"
    assert "parse_mode" not in bot.calls[0]
    assert bot.calls[0]["entities"][0].type == MessageEntity.BOLD


def test_long_message_is_split_with_exact_visible_text_and_safe_lengths():
    message = "**標題**\n" + ("繁體中文🙂+-/<> &\n" * 450)
    chunks = prepare_telegram_chunks(message, limit=4000)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 4000 for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == prepare_telegram_text(message).text


def test_split_does_not_break_a_bold_span_when_it_fits_in_one_chunk():
    message = ("前言\n" * 8) + "**這一整段都要保持粗體**\n結尾"
    chunks = prepare_telegram_chunks(message, limit=25)
    matching = [chunk for chunk in chunks if "這一整段都要保持粗體" in chunk.text]

    assert len(matching) == 1
    bold = [entity for entity in matching[0].entities if entity.type == MessageEntity.BOLD]
    assert len(bold) == 1
    assert bold[0].length == len("這一整段都要保持粗體".encode("utf-16-le")) // 2


def test_forced_split_of_oversized_bold_span_keeps_each_part_bold():
    message = "**" + ("粗體🙂" * 30) + "**"
    chunks = prepare_telegram_chunks(message, limit=20)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 20 for chunk in chunks)
    assert all(any(entity.type == MessageEntity.BOLD for entity in chunk.entities) for chunk in chunks)


def test_send_success_reports_chunk_and_message_ids():
    bot = FakeBot()
    result = asyncio.run(send_telegram_message(bot, 123, "第一段\n第二段", chunk_limit=4))

    assert result.ok is True
    assert result.status == "sent"
    assert result.chunk_count == 2
    assert result.sent_chunk_count == 2
    assert result.message_ids == [101, 102]


def test_chunk_prefix_is_added_to_every_chunk_and_shifts_utf16_entities():
    chunks = prepare_telegram_push_chunks(
        "**粗體🙂標題**\n" + ("內容\n" * 8),
        limit=18,
        chunk_prefix="🔥 ",
    )

    assert len(chunks) > 1
    assert all(chunk.text.startswith("🔥 ") for chunk in chunks)
    assert all(len(chunk.text) <= 18 for chunk in chunks)
    bold_chunk = next(chunk for chunk in chunks if "粗體🙂標題" in chunk.text)
    bold = next(entity for entity in bold_chunk.entities if entity.type == MessageEntity.BOLD)
    assert bold.offset == len("🔥 ".encode("utf-16-le")) // 2


def test_temporary_failure_retries_at_most_three_total_attempts():
    bot = FakeBot(failures=2)
    result = asyncio.run(send_telegram_message(bot, 123, "訊息", retry_delay_seconds=0))

    assert result.ok is True
    assert len(bot.calls) == 3


def test_three_failures_return_sanitized_failure_without_credentials():
    token = "123456:abcdefghijklmnopqrstuvwxyzABCDE"
    chat_id = "-1001234567890"

    class SensitiveFailureBot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, **kwargs):
            self.calls += 1
            raise RuntimeError(f"request https://api.telegram.org/bot{token}/sendMessage chat={chat_id}")

    bot = SensitiveFailureBot()
    result = asyncio.run(send_telegram_message(bot, chat_id, "訊息", retry_delay_seconds=0))

    assert result.ok is False
    assert result.status == "failed"
    assert result.error_code == "send_chunk_1_failed"
    assert bot.calls == 3
    assert token not in result.error
    assert chat_id not in result.error


def test_error_sanitizer_redacts_bot_token_pattern_without_explicit_secret():
    token = "123456:abcdefghijklmnopqrstuvwxyzABCDE"
    sanitized = sanitize_telegram_error(f"bad bot{token} request")

    assert token not in sanitized
    assert "REDACTED_TOKEN" in sanitized
