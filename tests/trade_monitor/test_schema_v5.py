from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from trade_monitor.analysis_contract import AnalysisValidationError, render_analysis_markdown, validate_analysis_payload
from trade_monitor.market_structure_state import MarketStructureStateError, validate_market_structure_transition


ROOT = Path(__file__).resolve().parents[2]


def _payload() -> dict:
    return json.loads((ROOT / "trade_monitor/examples/valid-analysis-v5.json").read_text(encoding="utf-8"))


def test_valid_v5_example_passes_schema_contract_and_keeps_nine_sections() -> None:
    schema = json.loads((ROOT / "trade_monitor/schemas/analysis-v5.json").read_text(encoding="utf-8"))
    payload = _payload()

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    validated = validate_analysis_payload(payload)

    anchors = validated["market_structure_state"]["anchor_context"]
    assert validated["market_structure_state"]["version"] == 3
    assert anchors["active_large_anchor_id"] == "A-LARGE-BULL-1850"
    assert anchors["working_quadrant"] == "Q4"
    message = render_analysis_markdown(
        validated,
        {"expected_latest_closed_k_hhmm": "19:11", "current_unclosed_k_hhmm": "19:12"},
        resumed=False,
    )
    assert message.count("**") >= 18
    assert "anchor_context" not in message
    assert "market_structure_state" not in message


def test_v5_schema_rejects_anchor_extra_field() -> None:
    schema = json.loads((ROOT / "trade_monitor/schemas/analysis-v5.json").read_text(encoding="utf-8"))
    payload = _payload()
    payload["market_structure_state"]["anchor_context"]["anchors"][0]["unexpected"] = True

    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload))

    assert errors


def test_v5_output_schema_avoids_keywords_rejected_by_codex_structured_output() -> None:
    schema = json.loads((ROOT / "trade_monitor/schemas/analysis-v5.json").read_text(encoding="utf-8"))
    forbidden = {"allOf", "if", "then", "else", "not", "uniqueItems"}

    def walk(value: object) -> None:
        if isinstance(value, dict):
            assert not (set(value) & forbidden)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(schema)


def test_relaxed_output_schema_still_has_strict_anchor_lifecycle_contract() -> None:
    schema = json.loads((ROOT / "trade_monitor/schemas/analysis-v5.json").read_text(encoding="utf-8"))
    payload = _payload()
    anchor = payload["market_structure_state"]["anchor_context"]["anchors"][0]
    anchor["status"] = "FORMING"

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    with pytest.raises(AnalysisValidationError, match="forming anchor"):
        validate_analysis_payload(payload)


def test_v2_to_v3_transition_rejects_backfilled_anchor_first_seen() -> None:
    previous = deepcopy(_payload()["market_structure_state"])
    previous["version"] = 2
    previous.pop("anchor_context")
    current = deepcopy(_payload()["market_structure_state"])
    current["as_of"] = "2026-09-02T19:12:00+08:00"
    current["anchor_context"]["anchors"][0]["first_seen_at"] = "2026-09-02T18:50:00+08:00"

    try:
        validate_market_structure_transition(
            previous,
            current,
            expected_as_of="2026-09-02T19:12:00+08:00",
        )
    except MarketStructureStateError as exc:
        assert "backdate" in str(exc)
    else:
        raise AssertionError("v2 to v3 migration must not backfill newly observed anchor timestamps")
