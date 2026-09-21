from __future__ import annotations

import main


def test_scan_menu_exposes_laoxiao_and_all_includes_it():
    assert main.SCAN_SELECTIONS["9"] == ["laoxiao"]
    assert "laoxiao" in main.SCAN_SELECTIONS["7"]
    assert main.SCAN_MENU_LABELS["9"] == "老蕭選股"
    assert "9. 老蕭選股" in main.SCAN_MENU_TEXT


def test_scan_keyboard_contains_laoxiao_button():
    keyboard = main.build_scan_strategy_keyboard()
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert any(button.text == "9. 老蕭選股" and button.callback_data == "scan_strategy:9" for button in buttons)
