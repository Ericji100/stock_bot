from __future__ import annotations

import asyncio
from datetime import date

import pandas as pd
from telegram import MessageEntity

import chip_strategies
import main
import stock_ai_bot.scanning.stock_scanner as stock_scanner
from stock_ai_bot.telegram.telegram_stock_formatting import (
    STOCK_MARK_END,
    STOCK_MARK_START,
    mark_stock_text,
    prepare_telegram_chunks,
    prepare_telegram_text,
    strip_stock_markers,
)


def test_prepare_telegram_text_strips_markers_and_adds_bold_underline_entities():
    chunk = prepare_telegram_text(f"觀察 {mark_stock_text('2330 台積電')} 轉強")

    assert chunk.text == "觀察 2330 台積電 轉強"
    assert STOCK_MARK_START not in chunk.text
    assert STOCK_MARK_END not in chunk.text
    assert [entity.type for entity in chunk.entities] == [MessageEntity.BOLD, MessageEntity.UNDERLINE]
    assert chunk.entities[0].offset == len("觀察 ")
    assert chunk.entities[0].length == len("2330 台積電")
    assert chunk.entities[1].offset == chunk.entities[0].offset
    assert chunk.entities[1].length == chunk.entities[0].length


def test_prepare_telegram_text_uses_utf16_offsets_for_emoji_prefix():
    chunk = prepare_telegram_text(f"📌 {mark_stock_text('2330 台積電')}")

    assert chunk.text == "📌 2330 台積電"
    assert chunk.entities[0].offset == 3
    assert chunk.entities[0].length == len("2330 台積電")


def test_prepare_telegram_text_supports_multiple_stock_markers():
    chunk = prepare_telegram_text(f"{mark_stock_text('2330 台積電')} / {mark_stock_text('2317 鴻海')}")

    bold_entities = [entity for entity in chunk.entities if entity.type == MessageEntity.BOLD]

    assert len(bold_entities) == 2
    assert chunk.text == "2330 台積電 / 2317 鴻海"
    assert bold_entities[0].offset == 0
    assert bold_entities[1].offset == len("2330 台積電 / ")


def test_prepare_telegram_chunks_recomputes_entity_offsets_after_split():
    text = f"前言\n{mark_stock_text('1111 測試一')}\n{'x' * 20}\n{mark_stock_text('2222 測試二')}"

    chunks = prepare_telegram_chunks(text, limit=18)
    stock_chunks = [chunk for chunk in chunks if "2222 測試二" in chunk.text]

    assert len(chunks) >= 3
    assert stock_chunks
    expected_offset = len(stock_chunks[0].text.split("2222 測試二", 1)[0].encode("utf-16-le")) // 2
    assert stock_chunks[0].entities[0].offset == expected_offset
    assert stock_chunks[0].entities[0].length == len("2222 測試二")


def test_strip_stock_markers_returns_user_visible_text():
    text = f"{mark_stock_text('2330 台積電')} (900.0)"

    assert strip_stock_markers(text) == "2330 台積電 (900.0)"


def test_safe_send_bot_message_removes_markers_and_sends_entities():
    class FakeBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)

    bot = FakeBot()

    asyncio.run(
        main.safe_send_bot_message(
            bot,
            123,
            f"Top1 {mark_stock_text('2330 台積電')}｜80分",
            parse_mode="HTML",
        )
    )

    assert bot.calls[0]["text"] == "Top1 2330 台積電｜80分"
    assert "parse_mode" not in bot.calls[0]
    assert [entity.type for entity in bot.calls[0]["entities"]] == [MessageEntity.BOLD, MessageEntity.UNDERLINE]


def test_safe_send_bot_message_without_stock_marker_keeps_plain_kwargs():
    class FakeBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)

    bot = FakeBot()

    asyncio.run(main.safe_send_bot_message(bot, 123, "一般訊息", disable_web_page_preview=True))

    assert bot.calls[0]["text"] == "一般訊息"
    assert "entities" not in bot.calls[0]
    assert bot.calls[0]["disable_web_page_preview"] is True


def test_safe_send_reply_removes_markers_and_sends_entities():
    class FakeMessage:
        def __init__(self):
            self.calls = []

        async def reply_text(self, text, **kwargs):
            self.calls.append((text, kwargs))

    class FakeUpdate:
        def __init__(self):
            self.effective_message = FakeMessage()
            self.callback_query = None

    update = FakeUpdate()

    asyncio.run(main.safe_send_reply(update, f"雷達 {mark_stock_text('2330 台積電')}"))

    text, kwargs = update.effective_message.calls[0]
    assert text == "雷達 2330 台積電"
    assert [entity.type for entity in kwargs["entities"]] == [MessageEntity.BOLD, MessageEntity.UNDERLINE]


def test_financial_scan_report_marks_stock_display_name():
    candidate = stock_scanner.ScanCandidate(
        code="2330",
        symbol="2330.TW",
        market="TWSE",
        name="台積電",
        industry="半導體業",
        revenue_group="group_1",
        gross_margin_rating="B",
        price=900.0,
        avg_volume_20d=1000.0,
        latest_monthly_revenue=100_000_000.0,
        revenue_history=[],
        gross_margins=[],
    )
    report = stock_scanner.ScanReport(
        generated_at="2026-06-30 20:30",
        total_symbols=1,
        hard_filter_passed=1,
        candidates=[candidate],
        scan_settings={},
    )

    text = stock_scanner.format_scan_report(report)

    assert STOCK_MARK_START in text
    assert "2330 台積電 (900)" in strip_stock_markers(text)


def test_chip_strategy_members_mark_stock_display_name():
    context = chip_strategies.ChipMarketContext(
        report_date=date(2026, 6, 30),
        latest_trading_date=date(2026, 6, 30),
        total_symbols=1,
        scan_settings={},
        candidates=pd.DataFrame(
            [
                {
                    "code": "2330",
                    "name": "台積電",
                    "industry": "半導體業",
                    "price": 900.0,
                }
            ]
        ),
        daily_data=pd.DataFrame(),
        weekly_data=pd.DataFrame(),
    )

    members = chip_strategies._candidate_members(context, lambda code: "B")
    text = "\n".join(members["B"]["半導體業"])

    assert STOCK_MARK_START in text
    assert "2330 台積電 (900)" in strip_stock_markers(text)
