from __future__ import annotations

import asyncio
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import main


class FakeBot:
    def __init__(self, *, fail_on_part: int | None = None) -> None:
        self.fail_on_part = fail_on_part
        self.messages: list[str] = []

    async def send_message(self, chat_id, text: str, **kwargs) -> None:
        if self.fail_on_part is not None and len(self.messages) + 1 == self.fail_on_part:
            raise RuntimeError("send failed")
        self.messages.append(text)


class ScheduledRadarPushTests(unittest.TestCase):
    def test_scheduled_radar_uses_concise_push_summary(self) -> None:
        bot = FakeBot()
        context = SimpleNamespace(bot=bot)
        push_summary = "Radar concise summary"

        with (
            patch.object(main, "load_config", return_value={"chat_id": "chat-1", "scan_settings": {}}),
            patch.object(main, "get_tw_today", return_value=date(2026, 6, 10)),
            patch("chip_strategies.is_possible_trading_day", return_value=True),
            patch.object(main, "run_radar", return_value=SimpleNamespace()) as run_radar,
            patch.object(main, "format_radar_push_summary", return_value=push_summary) as format_push,
            patch.object(main, "format_radar_report") as format_full,
        ):
            asyncio.run(main._scheduled_radar_push(context))

        self.assertEqual(bot.messages, [push_summary])
        request = run_radar.call_args.args[0]
        self.assertEqual(request.ai_top, 15)
        self.assertEqual(request.source, "technical")
        format_push.assert_called_once()
        self.assertEqual(format_push.call_args.kwargs.get("limit"), 15)
        format_full.assert_not_called()

    def test_split_telegram_message_splits_single_long_line(self) -> None:
        chunks = main.split_telegram_message("x" * 9001, limit=4000)

        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        self.assertEqual("".join(chunks), "x" * 9001)

    def test_scheduled_radar_send_failure_is_not_swallowed(self) -> None:
        bot = FakeBot(fail_on_part=2)
        context = SimpleNamespace(bot=bot)
        long_report = "\n".join([f"候選 {index} " + ("x" * 180) for index in range(80)])

        with (
            patch.object(main, "load_config", return_value={"chat_id": "chat-1", "scan_settings": {}}),
            patch.object(main, "get_tw_today", return_value=date(2026, 6, 10)),
            patch("chip_strategies.is_possible_trading_day", return_value=True),
            patch.object(main, "run_radar", return_value=SimpleNamespace()),
            patch.object(main, "format_radar_push_summary", return_value=long_report),
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(main._scheduled_radar_push(context))

        self.assertEqual(len(bot.messages), 1)


if __name__ == "__main__":
    unittest.main()
