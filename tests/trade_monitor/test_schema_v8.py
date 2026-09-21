from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[2]


def _payload() -> dict:
    return json.loads((ROOT / "trade_monitor/examples/valid-analysis-v8.json").read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((ROOT / "trade_monitor/schemas/analysis-v8.json").read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def test_v8_example_is_valid_and_contains_persistent_forward_context() -> None:
    payload = _payload()
    _validator().validate(payload)

    structure = payload["market_structure_state"]
    assert structure["version"] == 6
    assert structure["prospective_context"]["market_bias"] == "BULL_PRIMARY"
    assert structure["prospective_context"]["long_playbook"]["direction"] == "LONG"
    assert structure["prospective_context"]["short_playbook"]["direction"] == "SHORT"
    assert structure["prospective_context"]["taiji_evolution"]["active_sequence"] == "CORRECTION_4"
    assert structure["prospective_context"]["long_playbook"]["execution_style"] == "STANDARD_STRUCTURAL"


def test_v8_rejects_missing_short_playbook() -> None:
    payload = copy.deepcopy(_payload())
    del payload["market_structure_state"]["prospective_context"]["short_playbook"]

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(payload)


def test_v8_accepts_course_strategy_method_outside_original_four() -> None:
    payload = copy.deepcopy(_payload())
    structure = payload["market_structure_state"]
    structure["scenario_context"]["setup"]["pattern"] = "YIZHI_LIFE_DEATH_GATE"
    structure["decision_chain_context"]["opportunity_context"]["mapped_pattern"] = (
        "YIZHI_LIFE_DEATH_GATE"
    )
    structure["prospective_context"]["long_playbook"]["mapped_pattern"] = (
        "YIZHI_LIFE_DEATH_GATE"
    )
    structure["prospective_context"]["long_playbook"]["execution_style"] = "MOMENTUM_STRIKE"

    _validator().validate(payload)
