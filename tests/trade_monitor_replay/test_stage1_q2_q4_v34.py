from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from trade_monitor_replay.deterministic_state import (
    _false_break_arm_candidate,
    _select_program_entry_candidate,
)
from trade_monitor_replay.rules import ReplayRuleError, load_rule_package
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _long_only_promoted_large_trend,
    _valid_post_upgrade_background_quadrant,
    _validate_trade_setup_policy,
)
from trade_monitor_replay.stage1_cases import analysis_times, load_stage1_cases


ROOT = Path(__file__).parents[2]
V33_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v33-long-only"
    / "rule-manifest.json"
)
V34_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v34-long-only-q2-q4"
    / "rule-manifest.json"
)


def _candidate(source: str, *, seen: str, key: str) -> dict[str, object]:
    return {
        "setup_key": key,
        "direction": "LONG",
        "candidate_source": source,
        "first_seen_at": seen,
    }


def _entry_analysis(quadrant: str, *, primary: str | None = None) -> dict[str, object]:
    axes = {
        "Q2": ("DECREASING", "EXPANDING"),
        "Q4": ("INCREASING", "CONTRACTING"),
    }
    expected = primary if quadrant == "TRANSITION" else quadrant
    trend, volatility = axes.get(str(expected), ("UNCLEAR", "UNCLEAR"))
    return {
        "course_reading": {
            "working_quadrant": quadrant,
            "primary_quadrant_candidate": primary or quadrant,
            "working_trend_dynamics": trend,
            "working_volatility_dynamics": volatility,
        },
        "action": {"position_action": "ENTER"},
    }


def _stage_policy_ledger(*, decision_authority: str = "AI_HYBRID") -> dict[str, object]:
    return {
        "program_trade_policy": {
            "trade_direction_policy": "LONG_ONLY",
            "trade_setup_policy": "LONG_Q2_Q4_ONLY",
            "decision_authority": decision_authority,
        }
    }


def test_v34_extends_v33_without_mutating_v33_contract() -> None:
    v33 = load_rule_package(V33_MANIFEST)
    v34 = load_rule_package(V34_MANIFEST)

    assert v33.trade_direction_policy == "LONG_ONLY"
    assert v33.trade_setup_policy == "ALL"
    assert v34.trade_direction_policy == "LONG_ONLY"
    assert v34.trade_setup_policy == "LONG_Q2_Q4_ONLY"
    assert v34.prompt_sha256 == v33.prompt_sha256
    assert v34.schema_sha256 != v33.schema_sha256
    v34_schema = json.loads(v34.schema_text)
    reading = v34_schema["$defs"]["courseReading"]
    assert "main_strategy_family" in reading["required"]
    assert v34.model_rules_sha256 != v33.model_rules_sha256
    assert "假跌破收復只是Q2的其中一種可執行設置" in v34.model_rules
    assert "沒有事前數值上限" in v34.model_rules
    assert "CHECKPOINT`不得冒充硬障礙" in v34.model_rules
    assert "較低級空方錨／空方段就是Q2的交易背景" in v34.model_rules
    assert "AGGRESSIVE_CONFIRMED`只用於原候選明載" in v34.model_rules
    assert "工作軸應寫`INCREASING／EXPANDING`" in v34.model_rules
    assert "Q1只是觸發後的動態市場狀態" in v34.model_rules


def test_stage_one_catalog_contains_only_causal_locators_not_trade_answers() -> None:
    cases = load_stage1_cases()

    assert len(cases) == 6
    assert {item["target_date"] for item in cases} == {
        "2026-08-11",
        "2026-08-21",
        "2026-08-25",
        "2026-08-26",
    }
    assert {item["family_hypothesis"] for item in cases} == {"Q2", "Q4", "Q2_OR_Q4"}
    for case in cases:
        assert not {
            "expected_action",
            "expected_entry_price",
            "expected_exit_price",
            "expected_profit",
            "future_bars",
        }.intersection(case)
        times = analysis_times(case)
        assert times[0].strftime("%H:%M") == case["analysis_start"]
        assert times[-1].strftime("%H:%M") == case["analysis_end"]
        assert case["locator_time"] in {value.strftime("%H:%M") for value in times}


def test_manifest_rejects_q2_q4_scope_without_long_only(tmp_path: Path) -> None:
    manifest = json.loads(V34_MANIFEST.read_text(encoding="utf-8"))
    manifest["trade_direction_policy"] = "BOTH"
    path = tmp_path / "rule-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ReplayRuleError, match="LONG_Q2_Q4_ONLY"):
        load_rule_package(path)


def test_q2_q4_scope_ignores_newer_q1_candidate() -> None:
    q4 = _candidate(
        "CONFIRMED_PULLBACK_ENDPOINT_N2",
        seen="2026-08-26T09:44:00+08:00",
        key="q4",
    )
    q1 = _candidate(
        "YIZHI_EARLY_BREAKOUT",
        seen="2026-08-26T09:45:00+08:00",
        key="q1",
    )

    selected = _select_program_entry_candidate(
        q4,
        None,
        q1,
        None,
        None,
        None,
        allowed_directions={"LONG"},
        allowed_sources={
            "ANCHOR_LEG_SEQUENCE",
            "CONFIRMED_PULLBACK_ENDPOINT_N2",
            "FALSE_BREAK_RECLAIM",
            "Q2_FAILED_REVERSE_CANDIDATE",
        },
    )

    assert selected is not None
    assert selected["setup_key"] == "q4"


def test_q2_q4_scope_returns_no_trade_when_only_other_method_exists() -> None:
    selected = _select_program_entry_candidate(
        None,
        None,
        _candidate(
            "YIZHI_EARLY_BREAKOUT",
            seen="2026-08-26T09:45:00+08:00",
            key="q1",
        ),
        None,
        None,
        None,
        allowed_directions={"LONG"},
        allowed_sources={
            "ANCHOR_LEG_SEQUENCE",
            "CONFIRMED_PULLBACK_ENDPOINT_N2",
            "FALSE_BREAK_RECLAIM",
            "Q2_FAILED_REVERSE_CANDIDATE",
        },
    )

    assert selected is None


def test_false_break_candidate_keeps_q2_setup_identity_and_observed_reclaim_bars() -> None:
    event = {
        "id": "S-q2",
        "event_type": "FALSE_BREAK_RECLAIM",
        "direction": "BULL",
        "source_type": "DOW_DEFENSE",
        "source_id": "D-bull",
        "level_price": 100.0,
        "breach_time": "2026-08-11T10:26:00+08:00",
        "breach_extreme": 96.0,
        "reclaim_close": 101.0,
        "bars_to_reclaim": 4,
        "first_seen_at": "2026-08-11T10:30:00+08:00",
    }
    bars = [
        {
            "time": "2026-08-11T10:30:00+08:00",
            "open": 99.0,
            "high": 102.0,
            "low": 98.0,
            "close": 101.0,
        }
    ]

    candidate = _false_break_arm_candidate(
        [event],
        expected=datetime.fromisoformat(event["first_seen_at"]),
        bars=bars,
    )

    assert candidate is not None
    assert candidate["entry_strategy"] == "Q2_FALSE_BREAK_RECLAIM"
    assert candidate["q2_reclaim_bars"] == 4
    assert candidate["behavior_max_wait_bars"] == 3


@pytest.mark.parametrize(
    ("source", "analysis"),
    [
        ("FALSE_BREAK_RECLAIM", _entry_analysis("Q2")),
        ("Q2_FAILED_REVERSE_CANDIDATE", _entry_analysis("TRANSITION", primary="Q2")),
        ("ANCHOR_LEG_SEQUENCE", _entry_analysis("Q4")),
        ("CONFIRMED_PULLBACK_ENDPOINT_N2", _entry_analysis("TRANSITION", primary="Q4")),
    ],
)
def test_ai_can_enter_only_when_course_quadrant_matches_candidate_family(
    source: str,
    analysis: dict[str, object],
) -> None:
    _validate_trade_setup_policy(
        analysis,
        ledger=_stage_policy_ledger(),
        position={"status": "FLAT"},
        entry_gate={
            "status": "ENTRY_ELIGIBLE",
            "required_candidate_source": source,
        },
    )


@pytest.mark.parametrize(
    ("source", "wrong_quadrant"),
    [
        ("FALSE_BREAK_RECLAIM", "Q4"),
        ("Q2_FAILED_REVERSE_CANDIDATE", "Q4"),
        ("ANCHOR_LEG_SEQUENCE", "Q2"),
        ("CONFIRMED_PULLBACK_ENDPOINT_N2", "Q2"),
    ],
)
def test_ai_entry_fails_closed_when_quadrant_does_not_match_candidate_family(
    source: str,
    wrong_quadrant: str,
) -> None:
    with pytest.raises(SemanticReplayError, match="交易候選"):
        _validate_trade_setup_policy(
            _entry_analysis(wrong_quadrant),
            ledger=_stage_policy_ledger(),
            position={"status": "FLAT"},
            entry_gate={
                "status": "ENTRY_ELIGIBLE",
                "required_candidate_source": source,
            },
        )


def test_q2_q4_scope_rejects_other_entry_source_before_ai_decision() -> None:
    with pytest.raises(SemanticReplayError, match="其他戰法進場資格"):
        _validate_trade_setup_policy(
            _entry_analysis("Q4"),
            ledger=_stage_policy_ledger(),
            position={"status": "FLAT"},
            entry_gate={
                "status": "ENTRY_ELIGIBLE",
                "required_candidate_source": "YIZHI_EARLY_BREAKOUT",
            },
        )


def test_program_only_q2_q4_entry_does_not_require_ai_quadrant_match() -> None:
    _validate_trade_setup_policy(
        _entry_analysis("Q4"),
        ledger=_stage_policy_ledger(decision_authority="PROGRAM"),
        position={"status": "FLAT"},
        entry_gate={
            "status": "ENTRY_ELIGIBLE",
            "required_candidate_source": "FALSE_BREAK_RECLAIM",
        },
    )


def test_program_only_upgrade_derives_direction_from_its_own_event() -> None:
    result = _long_only_promoted_large_trend(
        {"classification": "盤整", "details": ["程式初始中性呈現。"]},
        {
            "structure_event_ref": "bull-upgrade",
            "controlling_grade": "LARGE",
            "background_trend_dynamics": "UNCLEAR",
        },
        ledger={
            "program_trade_policy": {
                "actionable_setups": "PROGRAM_ONLY",
                "decision_authority": "PROGRAM",
                "trade_direction_policy": "LONG_ONLY",
                "trade_setup_policy": "LONG_Q2_Q4_ONLY",
            },
            "structure_events": [
                {
                    "id": "bull-upgrade",
                    "event_type": "GRADE_UPGRADE",
                    "direction": "BULL",
                    "first_seen_at": "2026-08-26T09:34:00+08:00",
                }
            ],
        },
        ai_generated=False,
    )

    assert result["classification"] == "偏多但回檔"
    assert "多方結構已由小級升級為大級" in result["details"][0]


@pytest.mark.parametrize(
    ("quadrant", "authority"),
    [
        ("UNDEFINED", "EVIDENCE_ONLY"),
        ("TRANSITION", "PROGRAM_POLICY_V1"),
    ],
)
def test_program_only_upgrade_keeps_unresolved_quadrant_when_axes_are_not_available(
    quadrant: str,
    authority: str,
) -> None:
    assert _valid_post_upgrade_background_quadrant(
        {"background_quadrant": quadrant},
        ledger={
            "program_trade_policy": {
                "actionable_setups": "PROGRAM_ONLY",
                "decision_authority": "PROGRAM",
            },
            "anchor_lifecycle": {
                "quadrant_context": {"authority": authority},
            },
        },
    )


def test_program_only_upgrade_allows_unresolved_quadrant_without_context() -> None:
    assert _valid_post_upgrade_background_quadrant(
        {"background_quadrant": "TRANSITION"},
        ledger={
            "program_trade_policy": {
                "actionable_setups": "PROGRAM_ONLY",
                "decision_authority": "PROGRAM",
            },
        },
    )
