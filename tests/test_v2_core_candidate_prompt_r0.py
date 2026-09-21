from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
PROMPT = OUT / "v2_core_reproducible_r1.prompt.candidate.md"
SCHEMA = OUT / "v2_core_reproducible_r1.schema.candidate.json"


def test_candidate_prompt_is_not_mislabeled_frozen() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    assert "CANDIDATE_NOT_FROZEN（候選、尚未凍結）" in text
    assert "狀態：`FROZEN（已凍結）`" not in text
    assert "gpt-5.6-sol" in text
    assert "xhigh" in text


def test_prompt_contains_all_schema_scenarios_gates_and_routes() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    scenarios = schema["$defs"]["scenarioEvaluations"]["required"]
    for scenario in scenarios:
        assert scenario in text
        ref = schema["$defs"]["scenarioEvaluations"]["properties"][scenario]["$ref"].split("/")[-1]
        gate_schema = schema["$defs"][ref]["allOf"][1]["properties"]["gates"]
        for gate in gate_schema["required"]:
            assert gate in text
    for route in schema["$defs"]["decision"]["properties"]["canonical_trigger_route"]["enum"]:
        assert route in text


def test_prompt_separates_structure_from_position_and_performance() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    assert "先判結構，再判交易角色" in text
    assert "未來漲跌" in text
    assert "position state 只能決定母單、再進場或加碼角色，不能決定結構情境" in text
    assert "UNKNOWN（證據不足）" in text
