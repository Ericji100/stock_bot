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
    return json.loads((ROOT / "trade_monitor/examples/valid-analysis-v7.json").read_text(encoding="utf-8-sig"))


def _schema() -> dict:
    return json.loads((ROOT / "trade_monitor/schemas/analysis-v7.json").read_text(encoding="utf-8"))


def test_valid_v7_example_passes_schema_contract_and_keeps_nine_sections() -> None:
    payload = _payload()
    Draft202012Validator(_schema(), format_checker=FormatChecker()).validate(payload)
    validated = validate_analysis_payload(payload)

    structure = validated["market_structure_state"]
    assert structure["version"] == 5
    assert structure["decision_chain_context"]["process_stage"] == "SETUP_EVALUATION"
    assert structure["decision_chain_context"]["opportunity_context"]["course_grade"] == "B_CANDIDATE"
    message = render_analysis_markdown(
        validated,
        {"expected_latest_closed_k_hhmm": "19:11", "current_unclosed_k_hhmm": "19:12"},
        resumed=False,
    )
    for title in (
        "時間／最新已收盤 K",
        "大趨勢",
        "當前趨勢",
        "市場狀態",
        "觀察型態與狀態",
        "尚缺條件／觸發",
        "進場與結構停損",
        "一倍初始風險與最近障礙",
        "單口管理／禁止原因",
    ):
        assert f"**{title}**" in message
    assert "decision_chain_context" not in message
    assert "course_grade" not in message


def test_v7_schema_rejects_decision_chain_extra_field() -> None:
    payload = _payload()
    payload["market_structure_state"]["decision_chain_context"]["unexpected"] = True
    assert list(Draft202012Validator(_schema(), format_checker=FormatChecker()).iter_errors(payload))


def test_v7_output_schema_avoids_codex_unsupported_keywords() -> None:
    forbidden = {"allOf", "if", "then", "else", "not", "uniqueItems"}

    def walk(value: object) -> None:
        if isinstance(value, dict):
            assert not (set(value) & forbidden)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(_schema())


def test_v7_keeps_exactly_the_original_four_setup_patterns() -> None:
    expected = [
        "NONE",
        "OPENING_RANGE_BREAKOUT_RETEST",
        "TREND_PULLBACK_CONTINUATION",
        "INTRADAY_COMPRESSION_BREAKOUT",
        "FALSE_BREAK_REVERSAL",
    ]
    assert _schema()["$defs"]["setupContext"]["properties"]["pattern"]["enum"] == expected
    assert (
        _schema()["$defs"]["decisionChainOpportunityContext"]["properties"]["mapped_pattern"]["enum"]
        == expected
    )


def test_contract_rejects_setup_mapping_disagreement() -> None:
    payload = _payload()
    payload["market_structure_state"]["decision_chain_context"]["opportunity_context"]["mapped_pattern"] = (
        "FALSE_BREAK_REVERSAL"
    )
    with pytest.raises(AnalysisValidationError, match="map to the existing scenario setup"):
        validate_analysis_payload(payload)


def test_contract_rejects_yizhi_lens_without_yizhi_engine() -> None:
    payload = _payload()
    payload["market_structure_state"]["decision_chain_context"]["structure_context"]["analysis_lens"] = (
        "YIZHI_OVERRIDE"
    )
    with pytest.raises(AnalysisValidationError, match="YIZHI"):
        validate_analysis_payload(payload)


def test_contract_enforces_two_axis_course_grade_semantics() -> None:
    payload = _payload()
    payload["market_structure_state"]["decision_chain_context"]["opportunity_context"]["payoff_evidence"] = (
        "HIGH"
    )
    with pytest.raises(AnalysisValidationError, match="probability/payoff"):
        validate_analysis_payload(payload)


def test_v4_to_v5_transition_rejects_backfilled_first_endpoint() -> None:
    current = deepcopy(_payload()["market_structure_state"])
    previous = deepcopy(current)
    previous["version"] = 4
    previous.pop("decision_chain_context")

    current["as_of"] = "2026-09-02T19:12:00+08:00"
    current["cclass_context"]["as_of"] = current["as_of"]
    decision = current["decision_chain_context"]
    decision["as_of"] = current["as_of"]
    decision["stage_changed_at"] = current["as_of"]
    decision["assessment_changed_at"] = current["as_of"]
    decision["opening_context"].update(
        {
            "first_endpoint_side": "DH",
            "first_endpoint_style": "TRUE_LIKE",
            "endpoint_first_seen_at": "2026-09-02T19:00:00+08:00",
            "endpoint_confirmed_at": current["as_of"],
        }
    )

    with pytest.raises(MarketStructureStateError, match="cannot backfill"):
        validate_market_structure_transition(previous, current, expected_as_of=current["as_of"])
