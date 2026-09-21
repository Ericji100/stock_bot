from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_enlightenment_ai_rules_v2 import RULES_PATH, SCHEMA_PATH, validate


def _rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _scale(direction: str, quadrant: str, state: str, side: str) -> dict:
    return {
        "direction": direction,
        "quadrant": quadrant,
        "quadrant_evidence": ["相鄰三段的方向延續與波幅已逐段比較"],
        "dow_state": state,
        "dow_defense_side": side,
        "dow_defense_price": 40.0,
        "dow_defense_source_date": "2025-01-02",
        "dow_defense_established_date": "2025-02-03",
        "dow_defense_version": "D1",
        "pivot_parameter": 3,
        "explanation": "防線只使用判讀日以前已因果確認的樞紐建立。",
    }


def _route(name: str, count: int, passed: bool = False) -> dict:
    return {
        "scenario": name,
        "result": "PASS" if passed else "FAIL",
        "all_required_pass": passed,
        "gates": [
            {
                "gate_id": f"{name}_G{number}",
                "result": "PASS" if passed else "FAIL",
                "evidence": ["使用判讀日以前的結構證據"],
            }
            for number in range(1, count + 1)
        ],
        "blocking_conditions_present": [] if passed else ["必要條件尚未完成"],
        "explanation": "逐項保留正面條件、反證與失效，沒有使用剩餘分類。",
    }


def _example(*, triggered: bool = False) -> dict:
    return {
        "schema_version": "enlightenment-ai-judgement-v2",
        "rule_version": "enlightenment-ai-rules-v2",
        "stock": {"code": "6282", "name": "康舒", "symbol": "6282.TW"},
        "as_of": "2025-06-23",
        "selection_context": {
            "first_selected_on": "2025-06-02",
            "active_sources": ["strategy-a"],
            "is_active_watchlist_member": True,
            "campaign_id": "6282-C1",
            "campaign_number": 1,
            "trade_episode_id": None,
        },
        "data_quality": {
            "first_bar_date": "2022-01-03",
            "last_bar_date": "2025-06-23",
            "valid_bars": 850,
            "pre_roll_bars": 200,
            "corporate_action_adjusted": True,
            "cash_ledger_includes_distributions": True,
            "sufficiency": "PREFERRED",
            "notes": ["測試資料只到判讀日收盤"],
        },
        "anchors": [
            {
                "id": "A-L-1",
                "role": "PRIMARY",
                "scale": "LARGE",
                "direction": "UP",
                "start_date": "2025-04-11",
                "end_date": None,
                "start_price": 30.0,
                "end_price": None,
                "status": "FORMING",
                "clean": "PASS",
                "meaty": "PASS",
                "destructive": "PASS",
                "traceable": "PASS",
                "quality_explanation": "向上段已破壞同級空頭防線，且內部推進方向一致。",
                "invalidation": {"description": "跌破新錨起點", "price": 30.0, "source_date": "2025-04-11", "scale": "LARGE"},
            }
        ],
        "macd_cycle_map": {
            "l0_date": "2025-04-11",
            "h1_date": None,
            "l1_date": None,
            "current_histogram_sign": "POSITIVE",
            "current_histogram_expanding": True,
            "role": "SUPPORT_ONLY（只作波段切分與動能輔助）",
            "explanation": "MACD只支持向上段動能，沒有用交叉日直接指定定錨。",
        },
        "structure": {
            "primary_scenario": "FRESH_Q1_EXPANSION" if triggered else "UNRESOLVED",
            "alternative_scenarios": ["UNRESOLVED"],
            "confidence": "MEDIUM",
            "large_scale": _scale("UP", "Q1", "TRANSITION", "BULLISH"),
            "small_scale": _scale("UP", "Q1", "BULL", "BULLISH"),
            "scale_relationship": "DIRECTION_RESONANCE",
            "current_controller": "SHARED",
            "taiji": {
                "phase": "ANCHOR",
                "generation": "ANCHOR_LEG_1",
                "parent_anchor_id": "A-L-1",
                "replication_quality": "NOT_APPLICABLE",
                "correction_quality": "NOT_APPLICABLE",
                "copy_to_correction": False,
                "reanchor_required": False,
                "explanation": "目前是第一段向上新錨，尚未進入父代後的複製比較。",
            },
            "left_right_phase": "DIRECT_TO_RIGHT",
            "stage_location": "EARLY",
            "same_direction_attack_number": 1,
            "rationale": "目前只使用截至判讀日可確認的新錨、道氏防線與象限證據。",
        },
        "scenario_gate_audit": {
            "mature_trend_pullback": _route("MATURE_TREND_PULLBACK", 8),
            "macro_copy_resonance": _route("MACRO_COPY_RESONANCE", 8),
            "bear_reversal_left_right": _route("BEAR_REVERSAL_LEFT_RIGHT", 6),
            "fresh_q1_expansion": _route("FRESH_Q1_EXPANSION", 7, triggered),
            "fallback_used": False,
        },
        "evidence": [
            {
                "date": "2025-06-23",
                "kind": "PRICE_STRUCTURE",
                "description": "新生向上定錨的因果證據",
                "values": {"close": 45.0},
            }
        ],
        "disqualifiers": [],
        "decision": {
            "status": "TRIGGERED" if triggered else "STRUCTURE_MAPPED",
            "status_label_zh": "已觸發進場" if triggered else "結構已辨識",
            "intent": "ENTER_MOTHER" if triggered else "NONE",
            "route_gate_complete": triggered,
            "trigger_state": "CONFIRMED" if triggered else "WAITING",
            "trigger_description": "向上新錨完成破壞並於收盤確認" if triggered else "等待正面情境全部成立",
            "invalidation": {"description": "跌破新錨起點", "price": 30.0, "source_date": "2025-04-11", "scale": "POSITION"},
            "execution_rule": "NEXT_TRADING_DAY_OPEN" if triggered else "NONE",
            "entry_role": "MOTHER" if triggered else "NONE",
            "planned_nominal_twd": 10000 if triggered else 0,
            "same_day_evidence_deduplicated": True,
            "campaign_id": "6282-C1",
            "trade_episode_id": None,
            "attempt_number": 0,
            "reentry_eligible": False,
            "reentry_reason": "尚未發生停損，不適用重新進場。",
            "reason": "所有判讀均依訊號日以前資料；目前狀態與失效條件已完整記錄。",
        },
        "causal_attestation": {
            "latest_visible_bar": "2025-06-23",
            "used_future_data": False,
            "outcome_visible_to_ai": False,
            "unfinished_leg_endpoint_backfilled": False,
        },
    }


def test_v2_contract_self_audit_passes() -> None:
    result = validate()
    assert result["passed"], [check for check in result["checks"] if not check["passed"]]


def test_v2_schema_is_valid_and_accepts_complete_examples() -> None:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(_example(triggered=False), schema)
    jsonschema.validate(_example(triggered=True), schema)


def test_triggered_requires_route_completion_and_next_open() -> None:
    schema = _schema()
    invalid = _example(triggered=True)
    invalid["decision"]["route_gate_complete"] = False
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)
    invalid = _example(triggered=True)
    invalid["decision"]["execution_rule"] = "NONE"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)
    invalid = _example(triggered=True)
    invalid["scenario_gate_audit"]["fresh_q1_expansion"] = _route("FRESH_Q1_EXPANSION", 7, False)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)
    invalid = _example(triggered=True)
    invalid["disqualifiers"] = [{"code": "LATE_STAGE", "severity": "BLOCKING", "description": "末段風險阻擋本次觸發"}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)


def test_all_four_scenarios_are_closed_positive_routes() -> None:
    routes = _rules()["scenario_rules"]
    assert set(routes) == {
        "MATURE_TREND_PULLBACK",
        "MACRO_COPY_RESONANCE",
        "BEAR_REVERSAL_LEFT_RIGHT",
        "FRESH_Q1_EXPANSION",
    }
    assert [len(routes[name]["all_required"]) for name in routes] == [8, 8, 6, 7]
    assert all(route["fallback_allowed"] is False for route in routes.values())


def test_mature_trend_has_full_structure_trigger_and_failure_contract() -> None:
    route = _rules()["scenario_rules"]["MATURE_TREND_PULLBACK"]
    text = json.dumps(route, ensure_ascii=False)
    for token in ("成熟多頭", "長多慣性", "太極父代", "Q4", "Q2", "Q3", "小級", "REENTRY_WATCHING", "CAMPAIGN_INVALIDATED"):
        assert token in text
    assert len(route["preferred_trigger_paths"]) == 4
    assert len(route["blocking_conditions"]) >= 8


def test_backtest_v2_parameters_are_not_rule_v2_requirements() -> None:
    rules = _rules()
    assert "minimum_score" not in rules
    assert "chase_cap" not in rules
    assert "五分門檻" in rules["version_lineage"]["not_imported_from_backtest_v2"]
