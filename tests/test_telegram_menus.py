from __future__ import annotations

import unittest

from research_center.telegram_handlers import _compose_menu_command, _macro_scope_keyboard


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

    def test_macro_scope_keyboard_has_manual_input(self):
        keyboard = _macro_scope_keyboard()
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("手動輸入市場範圍", labels)



    def test_macro_scope_keyboard_has_china_and_europe(self):
        keyboard = _macro_scope_keyboard()
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("中國", labels)
        self.assertIn("歐洲", labels)
if __name__ == "__main__":
    unittest.main()
