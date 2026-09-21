from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from trade_monitor.analysis_contract import render_analysis_markdown, validate_analysis_payload


ROOT = Path(__file__).resolve().parents[2]


def test_valid_v4_example_passes_schema_and_preserves_nine_sections() -> None:
    schema = json.loads((ROOT / "trade_monitor/schemas/analysis-v4.json").read_text(encoding="utf-8"))
    payload = json.loads((ROOT / "trade_monitor/examples/valid-analysis-v4.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)

    validated = validate_analysis_payload(payload)
    structure = validated["market_structure_state"]
    assert structure["version"] == 2
    assert structure["defense_lines"]["small_bull"]["pivot_id"] == "P1-L-1908"
    assert structure["defense_lines"]["small_bear"] is None
    assert structure["scenario_context"]["setup"]["stage"] == "ARMED"

    message = render_analysis_markdown(
        validated,
        {
            "expected_latest_closed_k_hhmm": "19:11",
            "current_unclosed_k_hhmm": "19:12",
        },
        resumed=False,
    )
    assert message.count("**") >= 18
    assert "market_structure_state" not in message


def test_v4_schema_rejects_prices_in_an_unavailable_zone() -> None:
    schema = json.loads((ROOT / "trade_monitor/schemas/analysis-v4.json").read_text(encoding="utf-8"))
    payload = json.loads((ROOT / "trade_monitor/examples/valid-analysis-v4.json").read_text(encoding="utf-8"))
    zone = payload["market_structure_state"]["scenario_context"]["setup"]["observation_zone"]
    zone["reliability"] = "UNAVAILABLE"
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload))
    assert errors
