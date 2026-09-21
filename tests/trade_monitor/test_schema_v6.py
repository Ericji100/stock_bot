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
    return json.loads((ROOT / "trade_monitor/examples/valid-analysis-v6.json").read_text(encoding="utf-8"))


def _schema() -> dict:
    return json.loads((ROOT / "trade_monitor/schemas/analysis-v6.json").read_text(encoding="utf-8"))


def test_valid_v6_example_passes_schema_contract_and_keeps_nine_sections() -> None:
    payload = _payload()
    Draft202012Validator(_schema(), format_checker=FormatChecker()).validate(payload)
    validated = validate_analysis_payload(payload)

    structure = validated["market_structure_state"]
    assert structure["version"] == 4
    assert structure["cclass_context"]["engine_mode"] == "TAIJI_ORDERED"
    assert structure["cclass_context"]["taiji_context"]["active_leg_id"] == "TAIJI-BULL-LEG4-1903"
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
    assert "cclass_context" not in message
    assert "taiji_context" not in message
    assert "momentum_context" not in message


def test_v6_schema_rejects_cclass_extra_field() -> None:
    payload = _payload()
    payload["market_structure_state"]["cclass_context"]["unexpected"] = True
    assert list(Draft202012Validator(_schema(), format_checker=FormatChecker()).iter_errors(payload))


def test_v6_output_schema_avoids_codex_unsupported_keywords() -> None:
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


def test_v6_keeps_exactly_the_original_four_setup_patterns() -> None:
    enum = _schema()["$defs"]["setupContext"]["properties"]["pattern"]["enum"]
    assert enum == [
        "NONE",
        "OPENING_RANGE_BREAKOUT_RETEST",
        "TREND_PULLBACK_CONTINUATION",
        "INTRADAY_COMPRESSION_BREAKOUT",
        "FALSE_BREAK_REVERSAL",
    ]


def test_contract_rejects_yizhi_mode_without_a_momentum_episode() -> None:
    payload = _payload()
    cclass = payload["market_structure_state"]["cclass_context"]
    cclass["engine_mode"] = "YIZHI_MOMENTUM"
    cclass["engine_changed_at"] = cclass["as_of"]
    with pytest.raises(AnalysisValidationError, match="active momentum stage"):
        validate_analysis_payload(payload)


def test_v3_to_v4_transition_rejects_backfilled_cclass_leg_sightings() -> None:
    current = deepcopy(_payload()["market_structure_state"])
    previous = deepcopy(current)
    previous["version"] = 3
    previous.pop("cclass_context")
    current["as_of"] = "2026-09-02T19:12:00+08:00"
    current["cclass_context"]["as_of"] = current["as_of"]
    current["cclass_context"]["engine_changed_at"] = current["as_of"]
    current["cclass_context"]["taiji_context"]["assessment_changed_at"] = current["as_of"]
    current["cclass_context"]["thesis_context"]["changed_at"] = current["as_of"]

    with pytest.raises(MarketStructureStateError, match="cannot backfill"):
        validate_market_structure_transition(previous, current, expected_as_of=current["as_of"])


def test_taiji_copy_and_correction_preserve_all_comparison_dimensions() -> None:
    legs = _payload()["market_structure_state"]["cclass_context"]["taiji_context"]["legs"]
    copy_leg = next(item for item in legs if item["sequence"] == "COPY_3")
    correction_leg = next(item for item in legs if item["sequence"] == "CORRECTION_4")
    dimensions = {
        "amplitude_relation",
        "duration_relation",
        "slope_relation",
        "cleanliness_relation",
        "destructive_relation",
    }
    assert dimensions.issubset(copy_leg)
    assert dimensions.issubset(correction_leg)
    assert copy_leg["copy_quality"] == "ACCEPTABLE"
    assert correction_leg["correction_quality"] == "ACCEPTABLE"
