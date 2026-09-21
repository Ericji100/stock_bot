from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from trade_monitor.analysis_contract import render_analysis_markdown, validate_analysis_payload


ROOT = Path(__file__).resolve().parents[2]


def test_valid_v3_example_persists_internal_structure_without_tenth_section() -> None:
    schema = json.loads((ROOT / "trade_monitor/schemas/analysis-v3.json").read_text(encoding="utf-8"))
    payload = json.loads((ROOT / "trade_monitor/examples/valid-analysis-v3.json").read_text(encoding="utf-8"))

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    validated = validate_analysis_payload(payload)
    message = render_analysis_markdown(
        validated,
        {"expected_latest_closed_k_hhmm": "19:11", "current_unclosed_k_hhmm": "19:12", "new_closed_bar_count": 1},
        resumed=False,
    )

    assert validated["market_structure_state"]["version"] == 2
    assert validated["market_structure_state"]["defense_lines"]["small_bull"]["pivot_id"] == "P1-L-1908"
    assert message.count("**時間／最新已收盤 K**") == 1
    assert "market_structure_state" not in message
    assert "第十" not in message
