from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research_center.telegram_handlers import (
    _compose_direct_theme_command,
    _compose_menu_command,
    _compose_topic_maintain_command,
    _date_keyboard,
    _extract_topic_change_id,
    build_topic_result_message,
    _macro_scope_keyboard,
    _maybe_start_menu,
    _topic_action_keyboard,
    _topic_action_keyboard_for_text,
    _topic_import_confirm_keyboard,
    _topic_import_confirm_text,
    _telegram_status_note,
    _value_source_keyboard,
    send_topic_result_to_chat,
    TOPIC_IMPORT_MAX_FILE_SIZE_BYTES,
    TOPIC_IMPORT_MAX_FILE_SIZE_MB,
)


class TelegramMenuTests(unittest.TestCase):
    def test_value_scan_portfolio_menu_no_top_flag(self):
        # Menu no longer adds --top; data service enforces internal limits (normal=10, deep=30)
        raw = _compose_menu_command(
            {
                "command": "value_scan",
                "source": "我的持股",
                "mode": "deep",
            }
        )
        self.assertEqual(raw, "/value_scan 我的持股 --deep --model gemini")
        self.assertNotIn("--top", raw)

    def test_value_scan_curated_no_top_flag(self):
        # Menu no longer adds --top; only explicit --top from direct command should appear
        raw = _compose_menu_command(
            {
                "command": "value_scan",
                "source": "精選選股",
                "mode": "normal",
            }
        )
        self.assertEqual(raw, "/value_scan 精選選股 --model gemini")
        self.assertNotIn("--top", raw)

    def test_value_top_keyboard_removed(self):
        # _value_top_keyboard should not exist in telegram_handlers
        import research_center.telegram_handlers as handlers
        self.assertFalse(
            hasattr(handlers, "_value_top_keyboard"),
            "_value_top_keyboard should be removed from telegram_handlers",
        )

    def test_value_source_keyboard_has_radar_monitor_and_single_stock(self):
        keyboard = _value_source_keyboard()
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("選股雷達", labels)
        self.assertIn("監控清單", labels)
        self.assertIn("單一股票", labels)
        self.assertIn("ai_menu:value_source:radar", callbacks)
        self.assertIn("ai_menu:value_source:monitor", callbacks)
        self.assertIn("ai_menu:value_source:single", callbacks)

    def test_macro_manual_scope_compose_command(self):
        raw = _compose_menu_command(
            {
                "command": "macro",
                "market_scope": "台幣匯率與電子股",
                "mode": "deep",
            }
        )
        self.assertEqual(raw, "/macro 台幣匯率與電子股 --deep --model gemini")

    def test_menu_can_choose_deepseek_model(self):
        raw = _compose_menu_command(
            {
                "command": "research",
                "target": "2330",
                "mode": "deep",
                "model": "deepseek",
            }
        )
        self.assertEqual(raw, "/research 2330 --deep --model deepseek")

    def test_theme_radar_menu_compose_latest_with_model(self):
        raw = _compose_menu_command({"command": "theme_radar", "model": "deepseek"})
        self.assertEqual(raw, "/theme_radar --model deepseek")

    def test_theme_radar_menu_compose_custom_date_with_model(self):
        raw = _compose_menu_command(
            {
                "command": "theme_radar",
                "date": "2026-05-22",
                "model": "minimax",
            }
        )
        self.assertEqual(raw, "/theme_radar --date 2026-05-22 --model minimax")

    def test_fallback_status_note_names_selected_model(self):
        result = SimpleNamespace(
            ai_used=False,
            ai_model=None,
            request=SimpleNamespace(source_only=False, ai_model="minimax"),
            fallback_reason="status_code=400; prompt_chars=1900000",
            report_json={"metadata": {"analysis_model_choice": "minimax"}},
        )

        note = _telegram_status_note(result)

        self.assertIn("AI 模型：MiniMax 調用失敗", note)
        self.assertIn("MiniMax 模型調用或公開來源整合未完整成功", note)
        self.assertNotIn("Gemini / 公開網路搜尋", note)

    def test_theme_radar_empty_command_starts_date_menu(self):
        sent = {}

        async def fake_send_reply(update, text, reply_markup=None):
            sent["text"] = text
            sent["reply_markup"] = reply_markup

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(_maybe_start_menu(SimpleNamespace(), context, "/theme_radar", fake_send_reply))

        self.assertTrue(handled)
        self.assertEqual(context.user_data["ai_menu"]["command"], "theme_radar")
        callbacks = [b.callback_data for row in sent["reply_markup"].inline_keyboard for b in row]
        self.assertIn("ai_menu:date:theme_radar:latest", callbacks)
        self.assertIn("ai_menu:date:theme_radar:custom", callbacks)

    def test_theme_radar_with_args_skips_menu(self):
        async def fake_send_reply(update, text, reply_markup=None):
            raise AssertionError("theme_radar with args should execute directly")

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(
            _maybe_start_menu(
                SimpleNamespace(),
                context,
                "/theme_radar --date 2026-05-22 --model deepseek",
                fake_send_reply,
            )
        )

        self.assertFalse(handled)
        self.assertEqual(context.user_data, {})

    def test_theme_flow_menu_compose_custom_date_with_model(self):
        raw = _compose_menu_command(
            {
                "command": "theme_flow",
                "theme": "AI伺服器",
                "date": "2026-05-22",
                "model": "deepseek",
            }
        )
        self.assertEqual(raw, "/theme_flow AI伺服器 --date 2026-05-22 --model deepseek")

    def test_theme_menu_compose_deep_custom_date_with_model(self):
        raw = _compose_menu_command(
            {
                "command": "theme",
                "theme": "AI電源",
                "mode": "deep",
                "date": "2026-05-22",
                "model": "deepseek",
            }
        )
        self.assertEqual(raw, "/theme AI電源 --deep --date 2026-05-22 --model deepseek")

    def test_theme_menu_compose_source_only_latest_with_model(self):
        raw = _compose_menu_command(
            {
                "command": "theme",
                "theme": "AI電源",
                "mode": "source_only",
                "model": "minimax",
            }
        )
        self.assertEqual(raw, "/theme AI電源 --source-only --model minimax")

    def test_theme_text_input_moves_to_mode_menu(self):
        source = Path("research_center/telegram_handlers.py").read_text(encoding="utf-8")
        self.assertIn('state["theme"] = " ".join(text.split())', source)
        self.assertIn("已收到題材或產業，請選擇題材分析模式", source)
        self.assertNotIn('已收到題材或產業，開始執行：\\n{raw}', source)

    def test_theme_flow_text_input_moves_to_date_menu(self):
        source = Path("research_center/telegram_handlers.py").read_text(encoding="utf-8")
        self.assertIn('state["theme"] = " ".join(text.split())', source)
        self.assertIn("已收到題材或產業，請選擇資料日期", source)

    def test_sector_strength_menu_compose_custom_date_with_model(self):
        raw = _compose_menu_command(
            {
                "command": "sector_strength",
                "date": "2026-05-22",
                "model": "minimax",
            }
        )
        self.assertEqual(raw, "/sector_strength --date 2026-05-22 --model minimax")

    def test_theme_flow_empty_command_asks_for_theme(self):
        sent = {}

        async def fake_send_reply(update, text, reply_markup=None):
            sent["text"] = text
            sent["reply_markup"] = reply_markup

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(_maybe_start_menu(SimpleNamespace(), context, "/theme_flow", fake_send_reply))

        self.assertTrue(handled)
        self.assertEqual(context.user_data["ai_menu"]["command"], "theme_flow")
        self.assertEqual(context.user_data["ai_menu"]["awaiting"], "theme_flow_query")
        self.assertIn("題材或產業", sent["text"])
        self.assertIsNone(sent["reply_markup"])

    def test_theme_flow_with_theme_starts_date_menu(self):
        sent = {}

        async def fake_send_reply(update, text, reply_markup=None):
            sent["text"] = text
            sent["reply_markup"] = reply_markup

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(_maybe_start_menu(SimpleNamespace(), context, "/theme_flow AI伺服器", fake_send_reply))

        self.assertTrue(handled)
        self.assertEqual(context.user_data["ai_menu"], {"command": "theme_flow", "theme": "AI伺服器"})
        callbacks = [b.callback_data for row in sent["reply_markup"].inline_keyboard for b in row]
        self.assertIn("ai_menu:date:theme_flow:latest", callbacks)
        self.assertIn("ai_menu:date:theme_flow:custom", callbacks)

    def test_theme_flow_with_flags_skips_menu(self):
        async def fake_send_reply(update, text, reply_markup=None):
            raise AssertionError("theme_flow with flags should execute directly")

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(
            _maybe_start_menu(
                SimpleNamespace(),
                context,
                "/theme_flow AI伺服器 --date 2026-05-22 --model deepseek",
                fake_send_reply,
            )
        )

        self.assertFalse(handled)
        self.assertEqual(context.user_data, {})

    def test_sector_strength_empty_command_starts_date_menu(self):
        sent = {}

        async def fake_send_reply(update, text, reply_markup=None):
            sent["text"] = text
            sent["reply_markup"] = reply_markup

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(_maybe_start_menu(SimpleNamespace(), context, "/sector_strength", fake_send_reply))

        self.assertTrue(handled)
        self.assertEqual(context.user_data["ai_menu"]["command"], "sector_strength")
        callbacks = [b.callback_data for row in sent["reply_markup"].inline_keyboard for b in row]
        self.assertIn("ai_menu:date:sector_strength:latest", callbacks)
        self.assertIn("ai_menu:date:sector_strength:custom", callbacks)

    def test_sector_strength_with_flags_skips_menu(self):
        async def fake_send_reply(update, text, reply_markup=None):
            raise AssertionError("sector_strength with flags should execute directly")

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(
            _maybe_start_menu(
                SimpleNamespace(),
                context,
                "/sector_strength --source radar --date 2026-05-22 --model minimax",
                fake_send_reply,
            )
        )

        self.assertFalse(handled)
        self.assertEqual(context.user_data, {})

    def test_topic_maintain_starts_model_menu(self):
        sent = {}

        async def fake_send_reply(update, text, reply_markup=None):
            sent["text"] = text
            sent["reply_markup"] = reply_markup

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(_maybe_start_menu(SimpleNamespace(), context, "/topic_maintain", fake_send_reply))

        self.assertTrue(handled)
        self.assertEqual(context.user_data["ai_menu"]["command"], "topic_maintain")
        self.assertNotIn("bootstrap", context.user_data["ai_menu"])
        callbacks = [b.callback_data for row in sent["reply_markup"].inline_keyboard for b in row]
        self.assertIn("ai_menu:topic_maintain:model:minimax", callbacks)

    def test_research_empty_command_prompts_for_target(self):
        sent = {}

        async def fake_send_reply(update, text, reply_markup=None):
            sent["text"] = text
            sent["reply_markup"] = reply_markup

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(_maybe_start_menu(SimpleNamespace(), context, "/research", fake_send_reply))

        self.assertTrue(handled)
        self.assertEqual(context.user_data["ai_menu"]["command"], "research")
        self.assertEqual(context.user_data["ai_menu"]["awaiting"], "research_target")
        self.assertIn("股票代號或名稱", sent["text"])
        self.assertIsNone(sent["reply_markup"])

    def test_research_target_command_runs_directly_with_parser_default_deep(self):
        async def fake_send_reply(update, text, reply_markup=None):
            raise AssertionError("direct /research target should not open mode menu")

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(_maybe_start_menu(SimpleNamespace(), context, "/research 2330", fake_send_reply))

        self.assertFalse(handled)
        self.assertNotIn("ai_menu", context.user_data)

    def test_theme_empty_command_prompts_for_query(self):
        sent = {}

        async def fake_send_reply(update, text, reply_markup=None):
            sent["text"] = text
            sent["reply_markup"] = reply_markup

        context = SimpleNamespace(user_data={})
        handled = asyncio.run(_maybe_start_menu(SimpleNamespace(), context, "/theme", fake_send_reply))

        self.assertTrue(handled)
        self.assertEqual(context.user_data["ai_menu"]["command"], "theme")
        self.assertEqual(context.user_data["ai_menu"]["awaiting"], "theme_query")
        self.assertIn("題材或產業", sent["text"])
        self.assertIsNone(sent["reply_markup"])

    def test_direct_theme_command_composes_after_text_input(self):
        self.assertEqual(_compose_direct_theme_command("theme", " AI   電源 "), "/theme AI 電源")
        self.assertEqual(_compose_direct_theme_command("theme_flow", "AI電源"), "/theme_flow AI電源")

    def test_macro_scope_keyboard_has_manual_input(self):
        keyboard = _macro_scope_keyboard()
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("手動輸入市場範圍", labels)

    def test_macro_scope_keyboard_has_china_and_europe(self):
        keyboard = _macro_scope_keyboard()
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("中國", labels)
        self.assertIn("歐洲", labels)

    def test_topic_import_document_limit_is_10mb(self):
        self.assertEqual(TOPIC_IMPORT_MAX_FILE_SIZE_BYTES, 10_000_000)
        self.assertEqual(TOPIC_IMPORT_MAX_FILE_SIZE_MB, 10)

    def test_topic_import_document_limit_message_mentions_10mb(self):
        source = Path("research_center/telegram_handlers.py").read_text(encoding="utf-8")
        self.assertIn("TOPIC_IMPORT_MAX_FILE_SIZE_BYTES", source)
        self.assertIn("TOPIC_IMPORT_MAX_FILE_SIZE_MB", source)
        self.assertIn("MB 以下", source)
        self.assertNotIn("2MB 以下", source)

    # ------------------------------------------------------------------
    # Topic command parser tests
    # ------------------------------------------------------------------
    def test_topic_maintain_parser(self):
        from research_center.command_parser import parse_command_text, CommandParseError
        # Valid command without date
        req = parse_command_text("/topic_maintain --deep --model deepseek")
        self.assertEqual(req.command, "topic_maintain")
        self.assertEqual(req.mode, "deep")
        self.assertEqual(req.ai_model, "deepseek")
        req_minimax = parse_command_text("/topic_maintain --model minimax")
        self.assertEqual(req_minimax.command, "topic_maintain")
        self.assertEqual(req_minimax.ai_model, "minimax")
        with self.assertRaises(CommandParseError):
            parse_command_text("/topic_maintain --bootstrap --model minimax")
        # --date should raise error for topic_maintain
        with self.assertRaises(CommandParseError):
            parse_command_text("/topic_maintain --date 2026-05-15")

    def test_topic_review_parser(self):
        from research_center.command_parser import parse_command_text
        req = parse_command_text("/topic_review change_001")
        self.assertEqual(req.command, "topic_review")
        self.assertEqual(req.target, "change_001")

    def test_topic_confirm_parser(self):
        from research_center.command_parser import parse_command_text
        req = parse_command_text("/topic_confirm change_001")
        self.assertEqual(req.command, "topic_confirm")
        self.assertEqual(req.target, "change_001")

    def test_topic_confirm_requires_change_id(self):
        from research_center.command_parser import parse_command_text, CommandParseError
        with self.assertRaises(CommandParseError):
            parse_command_text("/topic_confirm")

    def test_topic_reject_parser(self):
        from research_center.command_parser import parse_command_text
        req = parse_command_text("/topic_reject change_002")
        self.assertEqual(req.command, "topic_reject")
        self.assertEqual(req.target, "change_002")

    def test_topic_action_keyboard_callbacks(self):
        keyboard = _topic_action_keyboard("change_20260522_083953_import")
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("ai_menu:topic_action:confirm:change_20260522_083953_import", callbacks)
        self.assertIn("ai_menu:topic_action:reject:change_20260522_083953_import", callbacks)
        self.assertIn("ai_menu:topic_action:review:change_20260522_083953_import", callbacks)

    def test_extract_topic_change_id_supports_suffixes(self):
        self.assertEqual(
            _extract_topic_change_id("/topic_confirm change_20260522_083953_import"),
            "change_20260522_083953_import",
        )
        self.assertEqual(
            _extract_topic_change_id("/topic_review change_20260522_083953_import_r1"),
            "change_20260522_083953_import_r1",
        )

    def test_topic_action_keyboard_for_text_only_when_next_steps_exist(self):
        text = (
            "下一步操作：\n"
            "• /topic_confirm change_20260522_083953_import - 確認套用\n"
            "• /topic_reject change_20260522_083953_import - 拒絕"
        )
        self.assertIsNotNone(_topic_action_keyboard_for_text(text))
        self.assertIsNone(_topic_action_keyboard_for_text("ID：change_20260522_083953_import"))

    def test_build_topic_result_message_uses_action_keyboard(self):
        result = SimpleNamespace(
            summary=(
                "✅ 變更包已產生\n"
                "ID：change_20260619_114256\n\n"
                "下一步：/topic_review change_20260619_114256 查看詳情\n"
                "• /topic_confirm change_20260619_114256 - 確認套用\n"
                "• /topic_reject change_20260619_114256 - 拒絕"
            ),
            request=SimpleNamespace(command="topic_maintain", ai_model="minimax", source_only=False),
            ai_used=True,
            ai_model="minimax",
            fallback_reason=None,
            report_json={},
        )

        text, keyboard = build_topic_result_message(result)
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertIn("change_20260619_114256", text)
        self.assertIn("ai_menu:topic_action:review:change_20260619_114256", callbacks)
        self.assertIn("ai_menu:topic_action:confirm:change_20260619_114256", callbacks)
        self.assertIn("ai_menu:topic_action:reject:change_20260619_114256", callbacks)
        self.assertNotIn("ResearchCenterResult(", text)
        self.assertNotIn("raw_response_path", text)
        self.assertNotIn("prompt_log_path", text)

    def test_send_topic_result_to_chat_uses_same_keyboard(self):
        sent = {}

        async def fake_send_bot_message(bot, chat_id, text, **kwargs):
            sent["bot"] = bot
            sent["chat_id"] = chat_id
            sent["text"] = text
            sent["reply_markup"] = kwargs.get("reply_markup")

        result = SimpleNamespace(
            summary=(
                "✅ 變更包已產生\n"
                "ID：change_20260619_114256\n"
                "下一步：/topic_review change_20260619_114256 查看詳情"
            ),
            request=SimpleNamespace(command="topic_maintain", ai_model="minimax", source_only=False),
            ai_used=True,
            ai_model="minimax",
            fallback_reason=None,
            report_json={},
        )

        asyncio.run(send_topic_result_to_chat("bot", "chat", result, fake_send_bot_message))

        self.assertEqual(sent["bot"], "bot")
        self.assertEqual(sent["chat_id"], "chat")
        callbacks = [button.callback_data for row in sent["reply_markup"].inline_keyboard for button in row]
        self.assertIn("ai_menu:topic_action:review:change_20260619_114256", callbacks)

    def test_topic_profiles_parser(self):
        from research_center.command_parser import parse_command_text
        req = parse_command_text("/topic_profiles")
        self.assertEqual(req.command, "topic_profiles")

    def test_topic_seed_prompt_parser(self):
        from research_center.command_parser import parse_command_text
        req = parse_command_text("/topic_seed_prompt")
        self.assertEqual(req.command, "topic_seed_prompt")

    def test_topic_import_parser_preserves_pasted_json(self):
        from research_center.command_parser import parse_command_text
        payload = '{"summary":"ok","actions":[]}'
        req = parse_command_text(f"/topic_import --model minimax {payload}")
        self.assertEqual(req.command, "topic_import")
        self.assertEqual(req.ai_model, "minimax")
        self.assertEqual(req.target, payload)

    def test_topic_import_confirm_keyboard_has_no_model_choices(self):
        keyboard = _topic_import_confirm_keyboard()
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        labels = [button.text for button in buttons]
        callbacks = [button.callback_data for button in buttons]
        self.assertIn("建立變更包", labels)
        self.assertIn("取消", labels)
        self.assertIn("ai_menu:topic_import:confirm", callbacks)
        self.assertIn("ai_menu:topic_import:cancel", callbacks)
        self.assertFalse(any("model:" in callback for callback in callbacks))

    def test_topic_import_confirm_text_says_no_ai_call(self):
        text = _topic_import_confirm_text('{"actions":[{"action_type":"create_theme"}]}')
        self.assertIn("是否建立題材變更包", text)
        self.assertIn("不會呼叫 AI", text)
        self.assertIn("1 筆 actions", text)

    # ------------------------------------------------------------------
    # Old theme_refresh commands no longer supported
    # ------------------------------------------------------------------
    def test_theme_refresh_not_in_supported_commands(self):
        from research_center.command_parser import SUPPORTED_COMMANDS
        self.assertNotIn("theme_refresh", SUPPORTED_COMMANDS)

    def test_theme_management_not_in_supported_commands(self):
        from research_center.command_parser import SUPPORTED_COMMANDS
        for cmd in ["theme_profiles", "theme_drafts", "theme_draft", "theme_approve", "theme_reject", "theme_merge"]:
            self.assertNotIn(cmd, SUPPORTED_COMMANDS)


# ------------------------------------------------------------------
# /scan menu tests
# ------------------------------------------------------------------
class ScanMenuTests(unittest.TestCase):
    def test_scan_all_selection_includes_curated(self):
        from main import SCAN_SELECTIONS
        self.assertIn("curated", SCAN_SELECTIONS["7"])
        self.assertEqual(SCAN_SELECTIONS["8"], ["curated"])

    def test_resolve_scan_latest_report_date_uses_previous_trading_day_on_weekend(self):
        from datetime import date
        import main

        with (
            patch.object(main, "get_tw_today", return_value=date(2026, 5, 30)),
            patch.object(main, "is_possible_trading_day", side_effect=lambda d: d == date(2026, 5, 29)),
        ):
            resolved, note = main.resolve_scan_latest_report_date()

        self.assertEqual(resolved, date(2026, 5, 29))
        self.assertIn("2026-05-30", note)
        self.assertIn("2026-05-29", note)

    def test_resolve_scan_latest_report_date_keeps_trading_day(self):
        from datetime import date
        import main

        with (
            patch.object(main, "get_tw_today", return_value=date(2026, 5, 29)),
            patch.object(main, "is_possible_trading_day", return_value=True),
        ):
            resolved, note = main.resolve_scan_latest_report_date()

        self.assertEqual(resolved, date(2026, 5, 29))
        self.assertEqual(note, "")

    def test_run_all_scan_sends_curated_last_and_saves_combined_recent_scan(self):
        import main
        from datetime import date

        sent_messages: list[str] = []
        saved_records: list[tuple[str, date, str]] = []

        async def fake_safe_send_reply(update, text, **kwargs):
            sent_messages.append(text)

        def fake_run_tw_market_scan(*args, **kwargs):
            return "財報報告 1111"

        def fake_build_chip_reports(*args, **kwargs):
            return (
                {
                    "chip_1": "籌碼一 2222",
                    "chip_2": "籌碼二 3333",
                    "chip_3": "籌碼三 4444",
                    "chip_4": "籌碼四 5555",
                },
                {},
            )

        def fake_build_technical_scan_report(*args, **kwargs):
            return "技術報告 6666"

        def fake_build_curated_scan_result(*args, **kwargs):
            return SimpleNamespace(report_text="精選報告 7777", selected_codes=["7777"])

        def fake_save_recent_scan_result(scan_type, report_date, report_text, selected_codes=None):
            saved_records.append((scan_type, report_date, report_text))

        original_safe_send_reply = main.safe_send_reply
        original_load_config = main.load_config
        original_run_tw_market_scan = main.run_tw_market_scan
        original_build_chip_reports = main.build_chip_reports
        original_build_technical_scan_report = main.ts.build_technical_scan_report
        original_build_curated_scan_result = main.curated_scan_service.build_curated_scan_result
        original_save_recent_scan_result = main.save_recent_scan_result
        try:
            main.safe_send_reply = fake_safe_send_reply
            main.load_config = lambda: {"scan_settings": {}}
            main.run_tw_market_scan = fake_run_tw_market_scan
            main.build_chip_reports = fake_build_chip_reports
            main.ts.build_technical_scan_report = fake_build_technical_scan_report
            main.curated_scan_service.build_curated_scan_result = fake_build_curated_scan_result
            main.save_recent_scan_result = fake_save_recent_scan_result

            asyncio.run(main.run_selected_scan_reports(SimpleNamespace(), "7", date(2026, 5, 22)))
        finally:
            main.safe_send_reply = original_safe_send_reply
            main.load_config = original_load_config
            main.run_tw_market_scan = original_run_tw_market_scan
            main.build_chip_reports = original_build_chip_reports
            main.ts.build_technical_scan_report = original_build_technical_scan_report
            main.curated_scan_service.build_curated_scan_result = original_build_curated_scan_result
            main.save_recent_scan_result = original_save_recent_scan_result

        expected_reports = {
            "財報報告 1111",
            "籌碼一 2222",
            "籌碼二 3333",
            "籌碼三 4444",
            "籌碼四 5555",
            "技術報告 6666",
            "精選報告 7777",
        }
        report_messages = [msg for msg in sent_messages if msg in expected_reports]
        self.assertEqual(
            report_messages,
            ["財報報告 1111", "籌碼一 2222", "籌碼二 3333", "籌碼三 4444", "籌碼四 5555", "技術報告 6666", "精選報告 7777"],
        )
        self.assertEqual(len(saved_records), 1)
        scan_type, report_date, report_text = saved_records[0]
        self.assertEqual(scan_type, "全部執行")
        self.assertEqual(report_date.isoformat(), "2026-05-22")
        for marker in ["財報報告 1111", "籌碼一 2222", "技術報告 6666", "精選報告 7777"]:
            self.assertIn(marker, report_text)

    def test_scan_strategy_menu_has_8_options(self):
        from main import build_scan_strategy_keyboard
        keyboard = build_scan_strategy_keyboard()
        buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 8, "scan menu should have exactly 8 options")

    def test_scan_strategy_menu_callbacks_have_no_date(self):
        from main import build_scan_strategy_keyboard, SCAN_CALLBACK_PREFIX
        keyboard = build_scan_strategy_keyboard()
        for btn in keyboard.inline_keyboard:
            for b in btn:
                # callback should be like "scan_strategy:1" or "scan_strategy:6", NOT "scan_strategy:6:2026-05-20"
                self.assertNotIn(":", b.callback_data[len(SCAN_CALLBACK_PREFIX):],
                    f"callback {b.callback_data} should not contain date")

    def test_scan_strategy_menu_with_date_callbacks_contain_date(self):
        from datetime import date
        from main import build_scan_strategy_keyboard, SCAN_CALLBACK_PREFIX
        keyboard = build_scan_strategy_keyboard(date(2026, 5, 20))
        for btn in keyboard.inline_keyboard:
            for b in btn:
                # All callbacks must contain the date
                self.assertIn("2026-05-20", b.callback_data,
                    f"callback {b.callback_data} should contain date 2026-05-20")
                # Should be scan_strategy:N:2026-05-20 format
                parts = b.callback_data.replace(SCAN_CALLBACK_PREFIX, "").split(":")
                self.assertEqual(len(parts), 2, f"callback {b.callback_data} should have mode:date format")
                self.assertEqual(parts[1], "2026-05-20")

    def test_scan_strategy_menu_without_date_callbacks_have_no_date(self):
        from main import build_scan_strategy_keyboard, SCAN_CALLBACK_PREFIX
        keyboard = build_scan_strategy_keyboard(None)
        for btn in keyboard.inline_keyboard:
            for b in btn:
                # No date in callback when report_date is None
                self.assertNotIn(":", b.callback_data[len(SCAN_CALLBACK_PREFIX):],
                    f"callback {b.callback_data} should not contain date when report_date is None")

    def test_scan_date_menu_has_two_options(self):
        from main import build_scan_date_menu, SCAN_DATE_CALLBACK_PREFIX
        keyboard = build_scan_date_menu("1")
        buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        labels = [b.text for b in buttons]
        self.assertIn("📅 最新日期", labels)
        self.assertIn("📝 指定日期", labels)
        # 只有兩個選項，沒有返回上一層
        self.assertEqual(len(buttons), 2, "date menu should have exactly 2 options")
        self.assertNotIn("返回上一層", labels)

    def test_scan_date_menu_latest_callback(self):
        from main import build_scan_date_menu, SCAN_DATE_CALLBACK_PREFIX
        keyboard = build_scan_date_menu("6")
        # Find the "latest" button
        latest_btn = None
        for row in keyboard.inline_keyboard:
            for b in row:
                if "最新日期" in b.text:
                    latest_btn = b
                    break
        self.assertIsNotNone(latest_btn)
        self.assertEqual(latest_btn.callback_data, f"{SCAN_DATE_CALLBACK_PREFIX}latest:6")

    def test_scan_date_menu_custom_callback(self):
        from main import build_scan_date_menu, SCAN_DATE_CALLBACK_PREFIX
        keyboard = build_scan_date_menu("6")
        custom_btn = None
        for row in keyboard.inline_keyboard:
            for b in row:
                if "指定日期" in b.text:
                    custom_btn = b
                    break
        self.assertIsNotNone(custom_btn)
        self.assertEqual(custom_btn.callback_data, f"{SCAN_DATE_CALLBACK_PREFIX}custom:6")

    def test_scan_date_menu_no_cancel_in_labels(self):
        from main import build_scan_date_menu
        keyboard = build_scan_date_menu("6")
        for row in keyboard.inline_keyboard:
            for b in row:
                self.assertNotIn("取消", b.text,
                    f"date menu button should not contain '取消': {b.text}")

    def test_scan_date_menu_labels(self):
        from main import build_scan_date_menu
        keyboard = build_scan_date_menu("1")
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertEqual(len(labels), 2)
        self.assertIn("📅 最新日期", labels)
        self.assertIn("📝 指定日期", labels)

    def test_parse_scan_report_date_custom_input(self):
        from main import parse_scan_report_date
        # YYYY-MM-DD
        d = parse_scan_report_date(["2026-05-20"])
        self.assertEqual(d.isoformat(), "2026-05-20")
        # YYYY/MM/DD
        d2 = parse_scan_report_date(["2026/05/20"])
        self.assertEqual(d2.isoformat(), "2026-05-20")
        # YYYYMMDD
        d3 = parse_scan_report_date(["20260520"])
        self.assertEqual(d3.isoformat(), "2026-05-20")

    # ------------------------------------------------------------------
    # telegram_handlers no longer has theme_refresh keyboards
    # ------------------------------------------------------------------
    def test_theme_refresh_keyboards_removed(self):
        import research_center.telegram_handlers as handlers
        for name in ["_theme_refresh_mode_keyboard", "_theme_refresh_model_keyboard", "_theme_refresh_date_keyboard"]:
            self.assertFalse(
                hasattr(handlers, name),
                f"{name} should be removed from telegram_handlers",
            )

    # ------------------------------------------------------------------
    # telegram_handlers has topic handlers
    # ------------------------------------------------------------------
    def test_topic_handlers_registered(self):
        # Verify topic handlers are registered by checking the source of telegram_handlers.py
        import inspect
        from research_center import telegram_handlers
        src = inspect.getsource(telegram_handlers)
        for cmd in ["topic_maintain", "topic_review", "topic_confirm", "topic_reject", "topic_profiles"]:
            self.assertIn(f'"{cmd}":', src, f"{cmd} should be registered in telegram_handlers dict")

    # ------------------------------------------------------------------
    # help text contains topic commands
    # ------------------------------------------------------------------
    def test_help_text_has_topic_commands(self):
        from research_center.telegram_handlers import RESEARCH_HELP_TEXT
        for cmd in ["/topic_maintain", "/topic_review", "/topic_confirm", "/topic_reject", "/topic_profiles", "/topic_reset", "/topic_seed_prompt", "/topic_import", "/topic_source_sync"]:
            self.assertIn(cmd, RESEARCH_HELP_TEXT, f"{cmd} should be in RESEARCH_HELP_TEXT")

    def test_help_text_has_theme_radar_command(self):
        from research_center.telegram_handlers import RESEARCH_HELP_TEXT
        self.assertIn("/theme_radar", RESEARCH_HELP_TEXT)
        self.assertIn("/theme_flow", RESEARCH_HELP_TEXT)
        self.assertIn("/sector_strength", RESEARCH_HELP_TEXT)
        self.assertIn("--date 2026-05-22", RESEARCH_HELP_TEXT)
        self.assertIn("gemini|deepseek|minimax", RESEARCH_HELP_TEXT)

    def test_start_message_has_theme_radar_command(self):
        from main import START_TEXT
        self.assertIn("/theme_radar", START_TEXT)
        self.assertIn("/sector_strength", START_TEXT)
        self.assertIn("/help", START_TEXT)
        self.assertNotIn("/ai_help", START_TEXT)

    def test_help_text_no_theme_refresh(self):
        from research_center.telegram_handlers import RESEARCH_HELP_TEXT
        self.assertNotIn("theme_refresh", RESEARCH_HELP_TEXT.lower())

    def test_topic_reset_handler_registered(self):
        """build_research_handlers should return dict containing topic_reset."""
        from research_center.telegram_handlers import build_research_handlers
        handlers = build_research_handlers(None, None, lambda *a, **k: None, lambda h, *a, **k: (lambda u, c: None))
        self.assertIn("topic_reset", handlers)
        # Verify it's callable
        self.assertTrue(callable(handlers["topic_reset"]))

    def test_external_topic_handlers_registered(self):
        from research_center.telegram_handlers import build_research_handlers
        handlers = build_research_handlers(None, None, lambda *a, **k: None, lambda h, *a, **k: (lambda u, c: None))
        self.assertIn("topic_seed_prompt", handlers)
        self.assertIn("topic_import", handlers)
        self.assertIn("topic_source_sync", handlers)
        self.assertIn("ai_menu_document", handlers)

    def test_topic_seed_prompt_is_sent_as_txt_document(self):
        handlers_path = Path("d:/code/stock_ai_bot/research_center/telegram_handlers.py")
        source = handlers_path.read_text(encoding="utf-8")
        self.assertIn("_send_runtime_document", source)
        self.assertIn("telegram_document", source)
        orchestrator_source = Path("d:/code/stock_ai_bot/research_center/orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("topic_seed_prompt.txt", orchestrator_source)

    def test_main_py_has_topic_reset_command_handler(self):
        """main.py should register CommandHandler for topic_reset."""
        main_path = "d:/code/stock_ai_bot/main.py"
        content = Path(main_path).read_text(encoding="utf-8")
        self.assertIn('CommandHandler("topic_reset"', content)

    def test_main_py_has_topic_external_command_handlers(self):
        main_path = "d:/code/stock_ai_bot/main.py"
        content = Path(main_path).read_text(encoding="utf-8")
        self.assertIn('CommandHandler("topic_seed_prompt"', content)
        self.assertIn('CommandHandler("topic_import"', content)
        self.assertIn('CommandHandler("topic_source_sync"', content)
        self.assertIn('ai_menu_document', content)

    def test_topic_source_sync_parser(self):
        from research_center.command_parser import parse_command_text

        all_req = parse_command_text("/topic_source_sync")
        self.assertEqual(all_req.command, "topic_source_sync")
        self.assertEqual(all_req.target, "all")

        tpex_req = parse_command_text("/topic_source_sync --tpex")
        self.assertEqual(tpex_req.target, "tpex")

        udn_req = parse_command_text("/topic_source_sync --udn")
        self.assertEqual(udn_req.target, "udn")

    def test_start_message_has_minimax_model_info(self):
        """Main start message should stay concise and only show no-parameter commands."""
        from main import START_TEXT
        self.assertIn("/topic_maintain", START_TEXT)
        self.assertNotIn("--bootstrap", START_TEXT)
        self.assertNotIn("MiniMax M3", START_TEXT)

    def test_help_text_has_minimax_model_info(self):
        """RESEARCH_HELP_TEXT should show MiniMax M3 and gemini|deepseek|minimax."""
        from research_center.telegram_handlers import RESEARCH_HELP_TEXT
        self.assertIn("MiniMax M3", RESEARCH_HELP_TEXT)
        self.assertIn("gemini|deepseek|minimax", RESEARCH_HELP_TEXT)
        self.assertNotIn("[--model deepseek] - 直接執行", RESEARCH_HELP_TEXT)

    def test_help_text_source_no_legacy_deepseek_string(self):
        """telegram_handlers.py source should not contain legacy [--model deepseek] string."""
        handlers_path = Path("d:/code/stock_ai_bot/research_center/telegram_handlers.py")
        source = handlers_path.read_text(encoding="utf-8")
        self.assertNotIn("[--model deepseek] - 直接執行", source)
        self.assertIn("MiniMax M3", source)
        self.assertIn("gemini|deepseek|minimax", source)

    def test_prompt_topic_directory_preferred_over_config_prompts(self):
        """_load_prompt should prefer prompt/topic/ over config/prompts/ (verified via source inspection)."""
        from research_center.topic_maintain_service import _load_prompt
        import inspect

        source = inspect.getsource(_load_prompt)
        self.assertIn("prompt/topic/", source)
        self.assertIn("config/prompts/", source)
        # Verify topic_path check comes before config_path (priority)
        topic_check_pos = source.find("prompt/topic/")
        config_check_pos = source.find("config/prompts/")
        self.assertGreater(config_check_pos, topic_check_pos)

    def test_no_theme_refresh_formal_references(self):
        """No formal references to theme_refresh in research_center code."""
        import os
        violations = []
        for root_dir, dirs, files in os.walk("d:/code/stock_ai_bot/research_center"):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv")]
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(root_dir, f)
                    with open(path, encoding="utf-8") as fh:
                        content = fh.read()
                    if "theme_refresh" in content or "theme_draft" in content:
                        violations.append(path)
        self.assertEqual(violations, [], f"theme_refresh references found: {violations}")

    def test_scheduled_topic_maintain_uses_shared_topic_sender(self):
        text = Path("main.py").read_text(encoding="utf-8")
        start = text.index("async def _scheduled_topic_maintain")
        end = text.index("async def _scheduled_all_scan_push", start)
        body = text[start:end]

        self.assertIn("send_topic_result_to_chat", body)
        self.assertNotIn("str(result)", body)
        self.assertNotIn('getattr(result, "summary"', body)
        self.assertNotIn("ResearchCenterResult(", body)

    def test_topic_maintain_compose_default_is_full_mode(self):
        # /topic_maintain defaults to full maintenance, so --deep is no longer emitted.
        raw = _compose_topic_maintain_command({"model": "gemini"})
        self.assertEqual(raw, "/topic_maintain")

    def test_topic_maintain_compose_gemini_default(self):
        # Gemini is default, no need to emit --model gemini
        raw = _compose_topic_maintain_command({"mode": "deep", "model": "gemini"})
        self.assertEqual(raw, "/topic_maintain")

    def test_topic_maintain_compose_deepseek(self):
        raw = _compose_topic_maintain_command({"mode": "deep", "model": "deepseek"})
        self.assertEqual(raw, "/topic_maintain --model deepseek")

    def test_topic_maintain_compose_minimax(self):
        raw = _compose_topic_maintain_command({"model": "minimax"})
        self.assertEqual(raw, "/topic_maintain --model minimax")

    def test_topic_maintain_help_has_no_bootstrap_example(self):
        from research_center.telegram_handlers import RESEARCH_HELP_TEXT
        self.assertNotIn("--bootstrap", RESEARCH_HELP_TEXT)

    def test_topic_maintain_no_date_in_compose(self):
        # _compose_topic_maintain_command should never produce --date
        for state in [
            {"mode": "deep", "model": "gemini"},
            {"mode": "deep", "model": "deepseek"},
            {"model": "gemini"},
        ]:
            raw = _compose_topic_maintain_command(state)
            self.assertNotIn("--date", raw)

    def test_topic_maintain_help_has_no_date(self):
        from research_center.telegram_handlers import RESEARCH_HELP_TEXT
        # Check that /topic_maintain lines in help do not contain --date
        for line in RESEARCH_HELP_TEXT.splitlines():
            if "/topic_maintain" in line and "--date" in line:
                self.fail(f"Help text contains --date in topic_maintain line: {line}")

    def test_topic_maintain_help_says_full_maintenance(self):
        from research_center.telegram_handlers import RESEARCH_HELP_TEXT
        self.assertIn("完整維護", RESEARCH_HELP_TEXT)

    def test_topic_maintain_no_mode_keyboard(self):
        """topic_maintain:mode: callback should not exist (no normal/deep menu)."""
        import inspect
        from research_center import telegram_handlers
        source = inspect.getsource(telegram_handlers)
        self.assertNotIn("topic_maintain:mode:", source)

    def test_topic_adjust_not_in_supported_commands(self):
        from research_center.command_parser import SUPPORTED_COMMANDS
        self.assertNotIn("topic_adjust", SUPPORTED_COMMANDS)

    def test_topic_adjust_not_in_help_text(self):
        from research_center.telegram_handlers import RESEARCH_HELP_TEXT
        self.assertNotIn("topic_adjust", RESEARCH_HELP_TEXT)

    # ------------------------------------------------------------------
    # _date_keyboard and _analysis_model_keyboard should exist
    # ------------------------------------------------------------------
    def test_date_keyboard_exists_for_research_macro_theme_value_scan(self):
        for command in ["research", "macro", "theme", "value_scan", "theme_radar", "theme_flow", "sector_strength"]:
            keyboard = _date_keyboard(command)
            self.assertIsNotNone(keyboard)
            callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
            self.assertIn(f"ai_menu:date:{command}:latest", callbacks)
            self.assertIn(f"ai_menu:date:{command}:custom", callbacks)

    def test_analysis_model_keyboard_exists(self):
        from research_center.telegram_handlers import _analysis_model_keyboard
        keyboard = _analysis_model_keyboard()
        self.assertIsNotNone(keyboard)
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn("ai_menu:analysis_model:gemini", callbacks)
        self.assertIn("ai_menu:analysis_model:deepseek", callbacks)
        self.assertIn("ai_menu:analysis_model:minimax", callbacks)

    def test_analysis_model_keyboard_deepseek_model_flow(self):
        from research_center.telegram_handlers import _analysis_model_keyboard
        keyboard = _analysis_model_keyboard()
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn("ai_menu:analysis_model:deepseek", callbacks)

    def test_radar_model_keyboard_exists(self):
        from main import build_radar_model_keyboard
        keyboard = build_radar_model_keyboard()
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("MiniMax M3", labels)
        self.assertNotIn("MiniMax M2.7", labels)
        self.assertIn("radar_model:gemini", callbacks)
        self.assertIn("radar_model:deepseek", callbacks)
        self.assertIn("radar_model:minimax", callbacks)
        self.assertIn("radar_model:skip", callbacks)

    def test_radar_date_keyboard_exists(self):
        from main import build_radar_date_keyboard
        keyboard = build_radar_date_keyboard()
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn("radar_date:latest", callbacks)
        self.assertIn("radar_date:custom", callbacks)

    def test_main_py_has_radar_model_callback_handler(self):
        content = Path("d:/code/stock_ai_bot/main.py").read_text(encoding="utf-8")
        self.assertIn("handle_radar_model_callback", content)
        self.assertIn("RADAR_MODEL_CALLBACK_PREFIX", content)
        self.assertIn('CallbackQueryHandler(handle_radar_model_callback', content)

    def test_main_py_has_radar_date_callback_handler(self):
        content = Path("d:/code/stock_ai_bot/main.py").read_text(encoding="utf-8")
        self.assertIn("handle_radar_date_callback", content)
        self.assertIn("RADAR_DATE_CALLBACK_PREFIX", content)
        self.assertIn('CallbackQueryHandler(handle_radar_date_callback', content)
        self.assertIn("resolve_radar_report_date", content)

    def test_scheduled_radar_uses_minimax_comment(self):
        content = Path("d:/code/stock_ai_bot/main.py").read_text(encoding="utf-8")
        self.assertIn('source="technical"', content)
        self.assertIn("ai_top=5", content)
        self.assertIn('model="minimax"', content)
        self.assertIn("ai_comment_enabled=True", content)


# ------------------------------------------------------------------
# /news tests
# ------------------------------------------------------------------
class NewsMenuTests(unittest.TestCase):
    def test_news_handler_registered(self):
        """build_research_handlers should return dict containing news."""
        from research_center.telegram_handlers import build_research_handlers
        handlers = build_research_handlers(None, None, lambda *a, **k: None, lambda h, *a, **k: (lambda u, c: None))
        self.assertIn("news", handlers)
        self.assertIn("news_detail", handlers)
        self.assertIn("news_save", handlers)
        self.assertIn("news_url_message", handlers)
        self.assertTrue(callable(handlers["news"]))
        self.assertTrue(callable(handlers["news_detail"]))
        self.assertTrue(callable(handlers["news_save"]))
        self.assertTrue(callable(handlers["news_url_message"]))

    def test_news_menu_keyboard_action_first(self):
        """_news_menu_keyboard should show actions first, not model first."""
        from research_center.telegram_handlers import _news_menu_keyboard, AI_CALLBACK_PREFIX
        keyboard = _news_menu_keyboard()
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        callbacks = [b.callback_data for b in buttons]
        self.assertEqual(len(buttons), 3)
        self.assertIn(f"{AI_CALLBACK_PREFIX}news_action:latest", callbacks)
        self.assertIn(f"{AI_CALLBACK_PREFIX}news_action:7d", callbacks)
        self.assertIn(f"{AI_CALLBACK_PREFIX}news_action:refresh", callbacks)
        self.assertNotIn(f"{AI_CALLBACK_PREFIX}news_action:holdings", callbacks)
        self.assertNotIn(f"{AI_CALLBACK_PREFIX}news_model:gemini", callbacks)

    def test_news_model_keyboard_three_models(self):
        """_news_model_keyboard should offer gemini, deepseek, minimax."""
        from research_center.telegram_handlers import _news_model_keyboard, AI_CALLBACK_PREFIX
        keyboard = _news_model_keyboard()
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        labels = [b.text for b in buttons]
        callbacks = [b.callback_data for b in buttons]
        self.assertEqual(len(buttons), 3)
        self.assertIn("Gemini", labels)
        self.assertIn("DeepSeek V4 Pro (OpenCode Go)", labels)
        self.assertIn("MiniMax M3", labels)
        self.assertIn(f"{AI_CALLBACK_PREFIX}news_model:gemini", callbacks)
        self.assertIn(f"{AI_CALLBACK_PREFIX}news_model:deepseek", callbacks)
        self.assertIn(f"{AI_CALLBACK_PREFIX}news_model:minimax", callbacks)

    def test_news_action_keyboard_three_actions(self):
        """_news_action_keyboard should offer latest, 7d, refresh."""
        from research_center.telegram_handlers import _news_action_keyboard, AI_CALLBACK_PREFIX
        keyboard = _news_action_keyboard()
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        labels = [b.text for b in buttons]
        callbacks = [b.callback_data for b in buttons]
        self.assertEqual(len(buttons), 3)
        self.assertIn("📰 最新新聞", labels)
        self.assertIn("📰 過去7天新聞", labels)
        self.assertIn("🔄 搜尋並更新新聞", labels)
        self.assertIn(f"{AI_CALLBACK_PREFIX}news_action:latest", callbacks)
        self.assertIn(f"{AI_CALLBACK_PREFIX}news_action:7d", callbacks)
        self.assertIn(f"{AI_CALLBACK_PREFIX}news_action:refresh", callbacks)
        self.assertNotIn(f"{AI_CALLBACK_PREFIX}news_action:holdings", callbacks)

    def test_compose_news_command_gemini_default(self):
        """gemini should not add --model flag."""
        from research_center.telegram_handlers import _compose_news_command
        self.assertEqual(_compose_news_command("gemini", "latest"), "/news latest")
        self.assertEqual(_compose_news_command("gemini", "refresh"), "/news refresh")

    def test_compose_news_command_deepseek(self):
        """deepseek should only add --model for refresh."""
        from research_center.telegram_handlers import _compose_news_command
        self.assertEqual(_compose_news_command("deepseek", "latest"), "/news latest")
        self.assertEqual(_compose_news_command("deepseek", "7d"), "/news 7d")
        self.assertEqual(_compose_news_command("deepseek", "refresh"), "/news refresh --model deepseek")

    def test_compose_news_command_minimax(self):
        """minimax should add --model minimax."""
        from research_center.telegram_handlers import _compose_news_command
        self.assertEqual(_compose_news_command("minimax", "latest"), "/news latest")
        self.assertEqual(_compose_news_command("minimax", "refresh"), "/news refresh --model minimax")

    def test_main_py_has_news_command_handler(self):
        """main.py should register CommandHandler for news."""
        main_path = "d:/code/stock_ai_bot/main.py"
        content = Path(main_path).read_text(encoding="utf-8")
        self.assertIn('CommandHandler("news"', content)
        self.assertIn('CommandHandler("news_detail"', content)
        self.assertIn('CommandHandler("news_save"', content)

    def test_scheduled_news_refresh_uses_minimax(self):
        """Scheduled news refresh should default to MiniMax M3 via the minimax model key."""
        main_path = "d:/code/stock_ai_bot/main.py"
        content = Path(main_path).read_text(encoding="utf-8")
        self.assertIn('run_news_refresh, center, repository, progress, ai_model="minimax"', content)
        self.assertNotIn('run_news_refresh, center, repository, progress, ai_model="deepseek"', content)

    def test_main_py_news_url_handler_before_generic_text_handlers(self):
        """Pasted news URLs must be handled before generic text handlers."""
        main_path = "d:/code/stock_ai_bot/main.py"
        content = Path(main_path).read_text(encoding="utf-8")
        url_handler = 'app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & (filters.Regex(r"https?://") | filters.Entity("url")), research_handlers["news_url_message"]))'
        scan_text_handler = "app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_scan_date_text_input), group=1)"
        ai_menu_text_handler = 'app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, research_handlers["ai_menu_text"]))'
        self.assertIn(url_handler, content)
        self.assertIn(scan_text_handler, content)
        self.assertIn(ai_menu_text_handler, content)
        self.assertLess(content.index(url_handler), content.index(ai_menu_text_handler))
        self.assertLess(content.index(ai_menu_text_handler), content.index(scan_text_handler))

    def test_bot_command_specs_include_core_commands(self):
        """Telegram slash command menu should include frequently used commands."""
        from main import BOT_COMMAND_SPECS

        commands = {command for command, _description in BOT_COMMAND_SPECS}
        for command in {
            "start",
            "stop",
            "news",
            "news_detail",
            "news_save",
            "scan",
            "research",
            "macro",
            "theme",
            "value_scan",
            "theme_radar",
            "theme_flow",
            "sector_strength",
            "report",
            "backfill",
            "help",
        }:
            self.assertIn(command, commands)
        self.assertNotIn("ai_help", commands)

    def test_research_handlers_include_help_aliases(self):
        from research_center.telegram_handlers import build_research_handlers
        handlers = build_research_handlers(None, None, lambda *a, **k: None, lambda h, *a, **k: (lambda u, c: None))
        self.assertIn("help", handlers)
        self.assertIn("ai_help", handlers)
        self.assertIs(handlers["help"], handlers["ai_help"])

    def test_build_bot_commands_are_valid_telegram_commands(self):
        """Commands registered through set_my_commands must not include leading slash."""
        from main import build_bot_commands

        commands = build_bot_commands()
        self.assertGreaterEqual(len(commands), 10)
        for command in commands:
            self.assertFalse(command.command.startswith("/"))
            self.assertRegex(command.command, r"^[a-z0-9_]{1,32}$")
            self.assertTrue(command.description)

    def test_start_message_has_news(self):
        """Main start message should show /news."""
        from main import START_TEXT
        self.assertIn("/news", START_TEXT)

    def test_help_has_news(self):
        from research_center.telegram_handlers import RESEARCH_HELP_TEXT
        self.assertIn("/news", RESEARCH_HELP_TEXT)
        self.assertIn("新聞", RESEARCH_HELP_TEXT)

    # ------------------------------------------------------------------
    # /news command parser tests
    # ------------------------------------------------------------------
    def test_news_parser_latest(self):
        from research_center.command_parser import parse_command_text
        req = parse_command_text("/news latest")
        self.assertEqual(req.command, "news")
        self.assertEqual(req.target, "latest")

    def test_news_parser_7d(self):
        from research_center.command_parser import parse_command_text
        req = parse_command_text("/news 7d")
        self.assertEqual(req.command, "news")
        self.assertEqual(req.target, "7d")

    def test_news_parser_holdings_removed(self):
        from research_center.command_parser import parse_command_text, CommandParseError
        with self.assertRaises(CommandParseError):
            parse_command_text("/news holdings")

    def test_news_parser_refresh(self):
        from research_center.command_parser import parse_command_text
        req = parse_command_text("/news refresh")
        self.assertEqual(req.command, "news")
        self.assertEqual(req.target, "refresh")

    def test_news_parser_no_args(self):
        from research_center.command_parser import parse_command_text
        req = parse_command_text("/news")
        self.assertEqual(req.command, "news")
        self.assertEqual(req.target, "")

    def test_news_detail_parser(self):
        from research_center.command_parser import parse_command_text
        req = parse_command_text("/news_detail N123")
        self.assertEqual(req.command, "news_detail")
        self.assertEqual(req.target, "N123")


if __name__ == "__main__":
    unittest.main()
