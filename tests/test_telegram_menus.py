from __future__ import annotations

import unittest

from research_center.telegram_handlers import _compose_menu_command, _macro_scope_keyboard


class TelegramMenuTests(unittest.TestCase):
    def test_value_scan_portfolio_menu_defaults_to_all_holdings(self):
        raw = _compose_menu_command(
            {
                "command": "value_scan",
                "source": "我的持股",
                "top": "9999",
                "mode": "deep",
            }
        )
        self.assertEqual(raw, "/value_scan 我的持股 --deep --top 9999")

    def test_value_scan_curated_keeps_explicit_top(self):
        raw = _compose_menu_command(
            {
                "command": "value_scan",
                "source": "精選選股",
                "top": "30",
                "mode": "normal",
            }
        )
        self.assertEqual(raw, "/value_scan 精選選股 --top 30")



    def test_macro_manual_scope_compose_command(self):
        raw = _compose_menu_command(
            {
                "command": "macro",
                "market_scope": "台幣匯率與電子股",
                "mode": "deep",
            }
        )
        self.assertEqual(raw, "/macro 台幣匯率與電子股 --deep")

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
