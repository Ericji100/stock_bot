from __future__ import annotations

from pathlib import Path

import pytest

from trade_monitor_replay.program_analyzer import build_program_semantic_envelope
from trade_monitor_replay.rules import load_rule_package
from trade_monitor_replay.runner import _prompt_ledger_view
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _validate_trade_setup_policy,
)


ROOT = Path(__file__).parents[2]
V31_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v31"
    / "rule-manifest.json"
)
V34_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v34-long-only-q2-q4"
    / "rule-manifest.json"
)


def test_v34_ai_view_keeps_every_hard_fact_legal_q2_q4_candidate() -> None:
    """Program measurements may generate candidates, but AI owns course judgement."""

    q4 = {
        "setup_key": "Q4-PULLBACK-0907",
        "direction": "LONG",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
        "entry_strategy": "Q4_PULLBACK_CONTINUATION",
        "stage": "ARMED",
    }
    q2 = {
        "setup_key": "Q2-RECLAIM-0914",
        "direction": "LONG",
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "entry_strategy": "Q2_FALSE_BREAK_RECLAIM",
        "stage": "ARMED",
    }
    ledger = {
        "as_of": "2026-08-25T09:14:00+08:00",
        "program_trade_policy": {
            "decision_authority": "AI_HYBRID",
            "interpretive_authority": "AI",
            "hard_fact_authority": "PROGRAM",
            "entry_quality_authority": "PROGRAM_COURSE_GATE_V1",
            "trade_direction_policy": "LONG_ONLY",
            "trade_setup_policy": "LONG_Q2_Q4_ONLY",
        },
        "trade_levels": {
            "ai_candidate_inventory": [q4, q2],
            "ai_armable_setup_keys": [q4["setup_key"], q2["setup_key"]],
            # These objects exist only after their causal price/structure
            # generators have passed.  The course gate rejected Q4 using a
            # program-owned quadrant/Taiji opinion, not a hard-fact defect.
            "selected_entry_candidate_before_course_gate": q4,
            "course_filtered_candidate": q4,
            "false_break_arm_candidate": q2,
            "continuation_arm_candidate": None,
            "course_entry_quality_audit": {
                "authority": "PROGRAM_COURSE_GATE_V1",
                "status": "OBSERVATION_ONLY",
                "setup_key": q4["setup_key"],
                "candidate_source": q4["candidate_source"],
                "reason_codes": [
                    "CONTINUATION_WORKING_QUADRANT_Q2_DECREASING",
                    "CONTINUATION_TAIJI_COPY_FAILED",
                ],
            },
        },
    }

    view = _prompt_ledger_view(ledger, ai_hybrid=True)
    levels = view["trade_levels"]

    assert set(levels["ai_executable_setup_keys"]) == {
        q4["setup_key"],
        q2["setup_key"],
    }
    inventory_by_key = {
        item["setup_key"]: item for item in levels["ai_candidate_inventory"]
    }
    assert inventory_by_key[q4["setup_key"]]["stage"] == "ARMED"
    assert inventory_by_key[q2["setup_key"]]["stage"] == "ARMED"
    assert "selected_entry_candidate_before_course_gate" not in levels
    assert "course_entry_quality_audit" not in levels


def test_v34_enter_attributes_main_family_to_required_entry_strategy_only() -> None:
    """Taiji/Dow may support an entry, but must not become its PnL family."""

    ledger = {
        "program_trade_policy": {
            "decision_authority": "AI_HYBRID",
            "trade_direction_policy": "LONG_ONLY",
            "trade_setup_policy": "LONG_Q2_Q4_ONLY",
        }
    }
    cases = (
        (
            "Q2_FALSE_BREAK_RECLAIM",
            "FALSE_BREAK_RECLAIM",
            "Q2",
            "Q4",
            "DECREASING",
            "EXPANDING",
        ),
        (
            "Q4_PULLBACK_CONTINUATION",
            "CONFIRMED_PULLBACK_ENDPOINT_N2",
            "Q4",
            "Q2",
            "INCREASING",
            "CONTRACTING",
        ),
    )
    unguarded: list[str] = []

    for strategy, source, expected_family, wrong_family, trend, volatility in cases:
        gate = {
            "status": "ENTRY_ELIGIBLE",
            "direction": "LONG",
            "required_candidate_source": source,
            "required_entry_strategy": strategy,
        }
        base_reading = {
            "working_quadrant": expected_family,
            "primary_quadrant_candidate": expected_family,
            "working_trend_dynamics": trend,
            "working_volatility_dynamics": volatility,
            # Supporting methods explain confluence; neither owns attribution.
            "focus_methods": ["TAIJI", "DOW"],
        }
        matching = {
            "course_reading": {
                **base_reading,
                "main_strategy_family": expected_family,
            },
            "action": {"position_action": "ENTER", "direction": "LONG"},
        }
        _validate_trade_setup_policy(
            matching,
            ledger=ledger,
            position={"status": "FLAT"},
            entry_gate=gate,
        )

        mismatching = {
            "course_reading": {
                **base_reading,
                "main_strategy_family": wrong_family,
            },
            "action": {"position_action": "ENTER", "direction": "LONG"},
        }
        try:
            _validate_trade_setup_policy(
                mismatching,
                ledger=ledger,
                position={"status": "FLAT"},
                entry_gate=gate,
            )
        except SemanticReplayError:
            pass
        else:
            unguarded.append(f"{strategy}->{wrong_family}")

    assert unguarded == []


def test_v34_schema_requires_bounded_main_strategy_family_without_changing_v31() -> None:
    v34 = load_rule_package(V34_MANIFEST)
    v31 = load_rule_package(V31_MANIFEST)

    v34_reading = v34.schema["$defs"]["courseReading"]
    old_reading = v31.schema["$defs"]["courseReading"]
    assert "main_strategy_family" in v34_reading["required"]
    assert v34_reading["properties"]["main_strategy_family"] == {
        "type": "string",
        "enum": ["NONE", "Q2", "Q4"],
    }
    assert "main_strategy_family" not in old_reading["required"]
    assert "main_strategy_family" not in old_reading["properties"]
    assert "ai_course_assessment" in v34.schema["properties"]["analysis"]["properties"]
    assert "ai_course_assessment" in v34.schema["properties"]["analysis"]["required"]
    assessment_schema = v34.schema["$defs"]["courseAssessment"]
    checks_schema = assessment_schema["properties"]["checks"]
    assert "propertyNames" not in checks_schema
    assert set(checks_schema["required"]) == {
        "anchor_control",
        "grade_control",
        "dow_defense",
        "quadrant_axes",
        "taiji_relation",
        "location_quality",
        "trigger_quality",
        "stop_integrity",
        "expected_behavior",
        "course_permission",
    }


@pytest.mark.parametrize(
    ("strategy", "source", "expected_family"),
    (
        ("Q2_FALSE_BREAK_RECLAIM", "FALSE_BREAK_RECLAIM", "Q2"),
        ("Q2_FAILED_COUNTERTREND_REVERSAL", "", "Q2"),
        (
            "",
            "Q2_FAILED_REVERSE_CANDIDATE",
            "Q2",
        ),
        (
            "Q4_PULLBACK_CONTINUATION",
            "CONFIRMED_PULLBACK_ENDPOINT_N2",
            "Q4",
        ),
        ("", "ANCHOR_LEG_SEQUENCE", "Q4"),
    ),
)
def test_v34_program_output_attributes_gate_strategy_family(
    strategy: str,
    source: str,
    expected_family: str,
) -> None:
    envelope = build_program_semantic_envelope(
        ledger={
            "program_trade_policy": {
                "decision_authority": "PROGRAM",
                "trade_direction_policy": "LONG_ONLY",
                "trade_setup_policy": "LONG_Q2_Q4_ONLY",
            },
            "latest_closed_k": {"close": 45_100},
            "trade_levels": {"continuation_arm_candidate": None},
        },
        expected_as_of="2026-08-25T09:14:00+08:00",
        expected_session_key="2026-08-25:DAY",
        preopen=False,
        position={"status": "FLAT", "pending_entry": None},
        previous_memory=None,
        entry_gate={
            "status": "ENTRY_ELIGIBLE",
            "setup_key": f"{expected_family}-SETUP",
            "required_entry_strategy": strategy,
            "required_candidate_source": source,
        },
    )

    assert (
        envelope["analysis"]["course_reading"]["main_strategy_family"]
        == expected_family
    )


def test_program_output_uses_none_without_v34_setup_and_omits_field_for_legacy() -> None:
    common = {
        "latest_closed_k": {"close": 45_100},
        "trade_levels": {"continuation_arm_candidate": None},
    }
    call = {
        "expected_as_of": "2026-08-25T09:14:00+08:00",
        "expected_session_key": "2026-08-25:DAY",
        "preopen": False,
        "position": {"status": "FLAT", "pending_entry": None},
        "previous_memory": None,
        "entry_gate": {"status": "NONE"},
    }
    v34 = build_program_semantic_envelope(
        ledger={
            **common,
            "program_trade_policy": {
                "decision_authority": "PROGRAM",
                "trade_direction_policy": "LONG_ONLY",
                "trade_setup_policy": "LONG_Q2_Q4_ONLY",
            },
        },
        **call,
    )
    legacy = build_program_semantic_envelope(
        ledger={
            **common,
            "program_trade_policy": {
                "decision_authority": "PROGRAM",
                "trade_direction_policy": "BOTH",
                "trade_setup_policy": "ALL",
            },
        },
        **call,
    )

    assert v34["analysis"]["course_reading"]["main_strategy_family"] == "NONE"
    assert "main_strategy_family" not in legacy["analysis"]["course_reading"]
