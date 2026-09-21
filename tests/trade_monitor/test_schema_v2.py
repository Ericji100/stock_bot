from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from trade_monitor.analysis_contract import render_analysis_markdown


ROOT = Path(__file__).resolve().parents[2]


def test_valid_v2_example_passes_schema_and_renders_original_nine_sections() -> None:
    schema = json.loads((ROOT / "trade_monitor/schemas/analysis-v2.json").read_text(encoding="utf-8"))
    payload = json.loads((ROOT / "trade_monitor/examples/valid-analysis-v2.json").read_text(encoding="utf-8"))

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    message = render_analysis_markdown(
        payload,
        {
            "expected_latest_closed_k_hhmm": "19:11",
            "current_unclosed_k_hhmm": "19:12",
            "new_closed_bar_count": 1,
        },
        resumed=False,
    )

    expected_headings = [
        "**時間／最新已收盤 K**",
        "**大趨勢**",
        "**當前趨勢**",
        "**市場狀態**",
        "**觀察型態與狀態**",
        "**尚缺條件／觸發**",
        "**進場與結構停損**",
        "**一倍初始風險與最近障礙**",
        "**單口管理／禁止原因**",
    ]
    headings = [line for line in message.splitlines() if line in expected_headings]
    assert headings == expected_headings
    assert "constitution_event" not in message
    assert "第十" not in message
