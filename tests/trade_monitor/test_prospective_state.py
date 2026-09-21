from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from trade_monitor.analysis_contract import (
    AnalysisValidationError,
    render_analysis_markdown,
    validate_analysis_payload,
)
from trade_monitor.prospective_state import (
    ProspectiveStateError,
    empty_prospective_context,
    prospective_notification_reasons,
    validate_prospective_context,
    validate_prospective_transition,
    validate_taiji_evolution_alignment,
)


ROOT = Path(__file__).resolve().parents[2]


def _payload() -> dict:
    return json.loads((ROOT / "trade_monitor/examples/valid-analysis-v8.json").read_text(encoding="utf-8"))


def test_empty_prospective_context_keeps_both_direction_playbooks() -> None:
    at = "2026-09-02T19:10:00+08:00"
    context = validate_prospective_context(empty_prospective_context(as_of=at), as_of=at)

    assert context["market_bias"] == "UNDEFINED"
    assert context["long_playbook"]["direction"] == "LONG"
    assert context["short_playbook"]["direction"] == "SHORT"
    assert context["taiji_evolution"]["active_sequence"] == "NONE"
    assert context["assessment_changed_at"] is None


def test_inactive_taiji_evolution_clears_stale_assessment() -> None:
    at = "2026-09-02T19:10:00+08:00"
    context = empty_prospective_context(as_of=at)
    context["taiji_evolution"]["structural_assessment"] = "REVERSAL_RISK"

    validated = validate_prospective_context(context, as_of=at)

    assert validated["taiji_evolution"]["structural_assessment"] == "UNDEFINED"


def test_new_primary_and_alternative_hypotheses_are_causal() -> None:
    before_at = "2026-09-02T19:10:00+08:00"
    after_at = "2026-09-02T19:11:00+08:00"
    before = empty_prospective_context(as_of=before_at)
    after = _payload()["market_structure_state"]["prospective_context"]

    validate_prospective_transition(before, after, prior_as_of=before_at, current_as_of=after_at)
    assert after["primary_hypothesis"]["direction"] == "BULL"
    assert after["alternative_hypothesis"]["direction"] == "BEAR"


def test_same_direction_primary_and_alternative_are_rejected() -> None:
    context = copy.deepcopy(_payload()["market_structure_state"]["prospective_context"])
    context["alternative_hypothesis"]["direction"] = "BULL"

    with pytest.raises(ProspectiveStateError, match="different paths"):
        validate_prospective_context(context, as_of=context["as_of"])


def test_armed_playbook_without_executable_zones_is_downgraded_to_watching() -> None:
    context = copy.deepcopy(_payload()["market_structure_state"]["prospective_context"])
    context["long_playbook"]["trigger_zone"] = {
        "low": None,
        "high": None,
        "reliability": "UNAVAILABLE",
        "reason": "測試資料不足。",
    }

    validated = validate_prospective_context(context, as_of=context["as_of"])

    assert validated["long_playbook"]["status"] == "WATCHING"


def test_prospective_changes_force_notification() -> None:
    before = _payload()["market_structure_state"]["prospective_context"]
    after = copy.deepcopy(before)
    after["primary_hypothesis"]["status"] = "NEAR_CONFIRMATION"

    reasons = prospective_notification_reasons(before, after)

    assert "主要情境階段改變" in reasons


def test_renderer_places_forward_scenarios_and_both_playbooks_inside_nine_sections() -> None:
    payload = validate_analysis_payload(_payload())
    message = render_analysis_markdown(
        payload,
        {
            "expected_latest_closed_k_hhmm": "19:11",
            "current_unclosed_k_hhmm": "19:12",
            "new_closed_bar_count": 1,
        },
        resumed=False,
    )

    assert "主要盤勢推演" in message
    assert "備用盤勢推演" in message
    assert "偏多預案" in message
    assert "偏空預案" in message
    assert "進場後應有行為" in message
    assert "主控戰法為四型態－趨勢拉回延續" in message
    assert "太極演化" in message
    assert "修正結果依序為合格、合格" in message
    assert "作用中大錨：18:20 約46762點起始" in message
    assert "延伸至19:03 約46842點（圖面估計）" in message
    assert "父級為18:20起始的大錨" in message
    assert message.count("**時間／最新已收盤 K**") == 1
    assert message.count("**單口管理／禁止原因**") == 1
    assert "**前瞻情境**" not in message


def test_non_original_strategy_method_is_executable_and_rendered_in_chinese() -> None:
    raw = _payload()
    playbook = raw["market_structure_state"]["prospective_context"]["long_playbook"]
    playbook["mapped_pattern"] = "TAIJI_COPY_AFTER_CORRECTION"
    playbook["execution_style"] = "WOODPECKER"
    playbook["method_selection_reason"] = "太極修正與父代關係比四型態邊界更清楚。"
    raw["market_structure_state"]["scenario_context"]["setup"]["pattern"] = (
        "TAIJI_COPY_AFTER_CORRECTION"
    )
    raw["market_structure_state"]["decision_chain_context"]["opportunity_context"][
        "mapped_pattern"
    ] = "TAIJI_COPY_AFTER_CORRECTION"

    message = render_analysis_markdown(
        validate_analysis_payload(raw),
        {
            "expected_latest_closed_k_hhmm": "19:11",
            "current_unclosed_k_hhmm": "19:12",
            "new_closed_bar_count": 1,
        },
        resumed=False,
    )

    assert "主控戰法為太極－良性修正後複製" in message
    assert "啄木鳥式快速失效" in message


def test_taiji_evolution_must_match_retained_causal_legs() -> None:
    state = _payload()["market_structure_state"]
    validate_taiji_evolution_alignment(
        state["prospective_context"], state["cclass_context"]
    )
    state["prospective_context"]["taiji_evolution"]["copy_outcomes"] = ["FAILED"]

    with pytest.raises(ProspectiveStateError, match="copy outcome history"):
        validate_taiji_evolution_alignment(
            state["prospective_context"], state["cclass_context"]
        )


def test_taiji_trend_requires_two_comparable_outcomes() -> None:
    context = copy.deepcopy(_payload()["market_structure_state"]["prospective_context"])
    context["taiji_evolution"]["copy_amplitude_trend"] = "CONTRACTING"

    with pytest.raises(ProspectiveStateError, match="at least two"):
        validate_prospective_context(context, as_of=context["as_of"])


def test_strategy_method_must_match_its_market_engine_and_scenario() -> None:
    raw = _payload()
    state = raw["market_structure_state"]
    state["scenario_context"]["setup"]["pattern"] = "YIZHI_CENTRIFUGAL"
    state["decision_chain_context"]["opportunity_context"]["mapped_pattern"] = (
        "YIZHI_CENTRIFUGAL"
    )
    state["prospective_context"]["long_playbook"]["mapped_pattern"] = "YIZHI_CENTRIFUGAL"
    state["prospective_context"]["long_playbook"]["execution_style"] = "MOMENTUM_STRIKE"

    with pytest.raises(AnalysisValidationError, match="Yizhi strategy method requires"):
        validate_analysis_payload(raw)


def test_confirmed_large_anchor_cannot_be_downgraded_to_range() -> None:
    raw = _payload()
    raw["large_trend"]["classification"] = "盤整"

    with pytest.raises(AnalysisValidationError, match="directional large-trend background"):
        validate_analysis_payload(raw)


def test_confirmed_large_anchor_requires_retained_taiji_assessment() -> None:
    raw = _payload()
    cclass = raw["market_structure_state"]["cclass_context"]
    cclass["engine_mode"] = "UNDEFINED"
    cclass["engine_changed_at"] = None
    cclass["order_state"] = "UNDEFINED"
    cclass["taiji_context"].update(
        {
            "dynasty_id": None,
            "anchor_id": None,
            "active_leg_id": None,
            "legs": [],
            "anchor_time_status": "UNDEFINED",
            "previous_context_alignment": "UNDEFINED",
            "assessment_reason": "錯誤地忽略可見歷史段落。",
            "assessment_changed_at": None,
        }
    )
    structure = raw["market_structure_state"]["decision_chain_context"]["structure_context"]
    structure.update(
        {
            "confirmed_leg_count": 0,
            "analysis_lens": "UNDEFINED",
            "lens_changed_at": None,
            "family_dna": "UNAVAILABLE",
            "confluences": [],
            "structure_reason": "錯誤地不重建可見歷史。",
        }
    )
    raw["market_structure_state"]["prospective_context"]["taiji_evolution"] = (
        empty_prospective_context(as_of=cclass["as_of"])["taiji_evolution"]
    )

    with pytest.raises(AnalysisValidationError, match="retained causal Taiji"):
        validate_analysis_payload(raw)


def test_taiji_first_leg_must_share_active_large_anchor_origin() -> None:
    raw = _payload()
    taiji = raw["market_structure_state"]["cclass_context"]["taiji_context"]
    taiji["legs"][0]["start_bar_time"] = "2026-09-02T18:21:00+08:00"

    with pytest.raises(AnalysisValidationError, match="start with its active large anchor"):
        validate_analysis_payload(raw)


def test_conflicting_anchor_grades_require_both_directional_hypotheses() -> None:
    raw = _payload()
    state = raw["market_structure_state"]
    small = state["anchor_context"]["anchors"][1]
    small["direction"] = "BEAR"
    small["start_price_estimate"] = 46825
    small["extreme_price_estimate"] = 46805
    state["anchor_context"]["grade_relation"] = "CONFLICT"
    state["anchor_context"]["control_reason"] = "大級多方錨內出現小級空方修正錨。"
    state["prospective_context"]["alternative_hypothesis"]["direction"] = "RANGE"

    with pytest.raises(AnalysisValidationError, match="both directional hypotheses"):
        validate_analysis_payload(raw)
