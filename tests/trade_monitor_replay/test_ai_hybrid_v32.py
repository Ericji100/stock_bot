from __future__ import annotations

from pathlib import Path

import pytest

from trade_monitor_replay.config import ReplayConfig
from trade_monitor_replay.codex_analyzer import SYSTEM_PROMPT as CODEX_SYSTEM_PROMPT
from trade_monitor_replay.minimax_analyzer import (
    AI_HYBRID_EXECUTION_VERSION,
    build_replay_prompt,
)
from trade_monitor_replay.presentation import (
    _compact_public_text,
    _v3_course_method_lines,
    _v3_quadrant_line,
    _v3_reentry_line,
    _v3_structure_lines,
    render_programmatic_exit_signal,
    render_programmatic_exit_fill,
    render_programmatic_stop_fill,
)
from trade_monitor_replay.rules import ReplayRuleError, load_rule_package
from trade_monitor_replay.runner import (
    AI_HYBRID_PROMPT_VIEW_VERSION,
    ReplayRunError,
    ReplayRunner,
    _apply_program_stop_before_ai,
    _ai_hybrid_state_continuity_lock,
    _prompt_deterministic_constraints,
    _prompt_ledger_view,
    _prompt_structured_market_view,
    _program_reentry_expectation,
    _program_stop_fill_event,
    _requires_forced_trade_event_output,
    _saved_raw_revalidation_enabled,
    _suppress_redundant_analysis_after_exit_fill,
    _hybrid_hidden_tick_memory_for_persistence,
    _requires_ai_hybrid_off_cadence_decision,
    _stage_decision_authority,
    _retired_setup_keys,
    _retire_codex_session,
)
from trade_monitor_replay.program_constitution import apply_program_execution_event
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _ai_hybrid_expected_event_card_type,
    _canonicalize_program_action_envelope,
    _canonicalize_held_setup_stage,
    _reading_v3,
    _referenced_active_promotion,
    _q2_lower_grade_countermove_is_setup_context,
    _structure_control,
    _validate_ai_hybrid_public_language,
    _validate_ai_hybrid_one_contract_grade_conflict,
    _validate_v3_state_transition,
)
from trade_monitor_replay.program_analyzer import (
    _program_material_event_for_semantics,
    _program_memory_setups,
)


ROOT = Path(__file__).parents[2]
V32_MANIFEST = (
    ROOT
    / "trade_monitor_replay"
    / "rules"
    / "course-state-v2.1.8-replay-adapter-v32"
    / "rule-manifest.json"
)


def test_v32_manifest_declares_true_ai_hybrid_authority() -> None:
    package = load_rule_package(V32_MANIFEST)

    assert package.analysis_mode == "AI_HYBRID"
    assert package.decision_authority == "AI_WITH_PROGRAM_GUARDRAILS"
    assert package.execution_profile == "ai-hybrid-v32"
    assert package.contract_version == 6
    assert package.prompt_sha256 == "78a3487e90235e4b83ea835d30572b9207e41eb2f7d13fce5984f20e80209469"
    assert package.model_rules_sha256 == "bc360f1c33eb621074a6b715831da72aa92d75f87f3b04c9bf69cc89fb71ae87"
    assert "AI 課程決策規則包 v1" in package.model_rules
    assert len(package.model_rules) < len(package.prompt) // 2


def test_v32_runner_uses_distinct_execution_fingerprint() -> None:
    runner = ReplayRunner(ReplayConfig(rule_manifest_path=V32_MANIFEST))

    assert runner.ai_hybrid is True
    assert runner.course_chain_contract is True
    assert runner.execution_version == AI_HYBRID_EXECUTION_VERSION


def test_v32_static_prompt_uses_locked_compact_rubric() -> None:
    package = load_rule_package(V32_MANIFEST)
    prompt = build_replay_prompt(
        rules_text=package.model_rules,
        schema_text=package.schema_text,
        runtime_context={},
        deterministic_contract=True,
        course_chain_contract=True,
        ai_hybrid=True,
    )

    assert "AI 課程決策規則包 v1" in prompt
    assert "## 13. Telegram 同步" not in prompt
    assert len(prompt) < 25_000


def test_causal_rewind_retires_codex_session_instead_of_reusing_future_context() -> None:
    manifest = {
        "codex_session_id": "future-bearing-thread",
        "codex_session_turn_count": 7,
        "retired_codex_sessions": [],
    }

    _retire_codex_session(manifest, reason="causal_rewind")

    assert manifest["codex_session_id"] is None
    assert manifest["codex_session_turn_count"] == 0
    assert manifest["retired_codex_sessions"][0]["session_id"] == "future-bearing-thread"
    assert manifest["retired_codex_sessions"][0]["reason"] == "causal_rewind"


def test_program_invalidation_event_persists_retired_setup_across_ai_memory_turns(
    tmp_path: Path,
) -> None:
    (tmp_path / "analysis").mkdir()
    (tmp_path / "execution-events.jsonl").write_text(
        "\n".join(
            [
                '{"event_type":"PROGRAM_SETUP_INVALIDATED","setup_key":"old-or5",'
                '"invalidated_at":"2026-08-11T08:58:00+08:00"}',
                '{"event_type":"PROGRAM_SETUP_INVALIDATED","setup_key":"future-setup",'
                '"invalidated_at":"2026-08-11T09:30:00+08:00"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    previous_memory = {
        "as_of": "2026-08-11T09:00:00+08:00",
        "session_key": "2026-08-11:DAY",
        "active_setups": [],
    }

    retired = _retired_setup_keys(tmp_path, previous_memory)

    assert "old-or5" in retired
    assert "future-setup" not in retired


def test_public_text_removes_duplicate_opening_range_role() -> None:
    assert _compact_public_text("跌破OR5低點OR5低44,757點") == "跌破OR5低44,757點"
    assert _compact_public_text("突破OR15高點OR15高45,300點") == "突破OR15高45,300點"


def test_entry_eligibility_requires_immediate_hybrid_ai_decision_off_cadence() -> None:
    assert _requires_ai_hybrid_off_cadence_decision({"status": "ENTRY_ELIGIBLE"}) is True
    assert _requires_ai_hybrid_off_cadence_decision({"status": "ARMED"}) is False
    assert _requires_ai_hybrid_off_cadence_decision(None) is False


def test_hidden_hybrid_tick_keeps_hybrid_decision_authority() -> None:
    analyzer = object()

    assert _stage_decision_authority(ai_hybrid=True, analyzer=analyzer) == "AI_HYBRID"
    assert _stage_decision_authority(ai_hybrid=True, analyzer=None) == "PROGRAM"
    assert _stage_decision_authority(ai_hybrid=False, analyzer=analyzer) == "PROGRAM"


def test_one_contract_hybrid_rejects_enter_when_ai_declares_grade_conflict() -> None:
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        }
    }
    gate = {"status": "ENTRY_ELIGIBLE"}
    entering = {
        "course_reading": {"grade_relation": "CONFLICT"},
        "action": {"position_action": "ENTER", "entry_rejection_reason": "NONE"},
    }
    with pytest.raises(SemanticReplayError, match="固定1口"):
        _validate_ai_hybrid_one_contract_grade_conflict(
            entering,
            ledger=ledger,
            entry_gate=gate,
        )

    denied = {
        "course_reading": {"grade_relation": "CONFLICT"},
        "action": {
            "position_action": "NONE",
            "entry_rejection_reason": "GRADE_CONFLICT",
        },
    }
    _validate_ai_hybrid_one_contract_grade_conflict(
        denied,
        ledger=ledger,
        entry_gate=gate,
    )


def test_bull_q2_lower_grade_countermove_is_context_not_one_contract_veto() -> None:
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        },
        "latest_closed_k": {"close": 110.0},
        "anchor_lifecycle": {
            "background_anchor": None,
            "child_anchor": {"direction": "BEAR", "status": "ACTIVE"},
        },
        "structure_events": [
            {
                "id": "upgrade-bull",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "from_level": "SMALL",
                "to_level": "LARGE",
                "source_anchor_id": "large-bull",
                "replacement_defense_pivot_id": "large-defense",
                "replacement_defense_price": 100.0,
                "first_seen_at": "2026-08-26T09:34:00+08:00",
            }
        ],
    }
    gate = {
        "status": "ENTRY_ELIGIBLE",
        "direction": "LONG",
        "required_candidate_source": "FALSE_BREAK_RECLAIM",
        "required_entry_strategy": "Q2_FALSE_BREAK_RECLAIM",
    }
    passing_checks = {
        "grade_control": "PASS",
        "course_permission": "PASS",
    }
    entering = {
        "course_reading": {
            "large_anchor_ref": "large-bull",
            "large_defense_ref": "large-defense",
            "controlling_grade": "LARGE",
            "grade_relation": "CONFLICT",
        },
        "ai_course_assessment": {"checks": passing_checks},
        "action": {"position_action": "ENTER", "entry_rejection_reason": "NONE"},
    }

    assert _q2_lower_grade_countermove_is_setup_context(
        entering,
        ledger=ledger,
        entry_gate=gate,
    ) is True
    _validate_ai_hybrid_one_contract_grade_conflict(
        entering,
        ledger=ledger,
        entry_gate=gate,
    )

    denied = {
        **entering,
        "action": {
            "position_action": "NONE",
            "entry_rejection_reason": "GRADE_CONFLICT",
        },
    }
    with pytest.raises(SemanticReplayError, match="多方Q2的交易背景"):
        _validate_ai_hybrid_one_contract_grade_conflict(
            denied,
            ledger=ledger,
            entry_gate=gate,
        )


def test_hidden_program_tick_preserves_ai_interpretation_memory() -> None:
    previous = {
        "as_of": "2026-08-11T08:50:00+08:00",
        "active_setups": [],
        "thesis_bias": "BULL",
        "structure_control": {
            "background_quadrant": "UNDEFINED",
            "working_quadrant": "Q1",
        },
        "reentry": {"status": "NOT_APPLICABLE", "count": 0},
    }
    program = {
        "as_of": "2026-08-11T08:51:00+08:00",
        "active_setups": [{"setup_key": "program-only", "stage": "ARMED"}],
        "thesis_bias": "CONDITIONAL",
        "structure_control": {
            "background_quadrant": "Q1",
            "working_quadrant": "Q4",
        },
        "reentry": {"status": "AVAILABLE", "count": 1},
    }

    saved, source = _hybrid_hidden_tick_memory_for_persistence(
        validated_memory=program,
        previous_ai_memory=previous,
        ai_hybrid=True,
        program_only=True,
        forced_trade_event=False,
    )

    assert source == "prior_ai_with_program_lifecycle_updates"
    assert saved == previous
    assert saved is not previous


def test_hidden_program_trade_event_updates_only_transaction_lifecycle() -> None:
    previous = {
        "as_of": "2026-08-11T08:54:00+08:00",
        "active_setups": [{"setup_key": "held", "stage": "AGGRESSIVE_CONFIRMED"}],
        "thesis_bias": "BULL",
        "structure_control": {"working_quadrant": "Q2"},
        "reentry": {"status": "NOT_APPLICABLE", "count": 0},
    }
    program = {
        "as_of": "2026-08-11T08:55:00+08:00",
        "active_setups": [{"setup_key": "held", "stage": "INVALIDATED"}],
        "thesis_bias": "BEAR",
        "structure_control": {"working_quadrant": "Q1"},
        "reentry": {"status": "AVAILABLE", "count": 0},
    }

    saved, source = _hybrid_hidden_tick_memory_for_persistence(
        validated_memory=program,
        previous_ai_memory=previous,
        ai_hybrid=True,
        program_only=True,
        forced_trade_event=True,
    )

    assert source == "prior_ai_plus_program_trade_lifecycle"
    assert saved["as_of"].endswith("08:55:00+08:00")
    assert saved["active_setups"][0]["stage"] == "INVALIDATED"
    assert saved["reentry"]["status"] == "AVAILABLE"
    assert saved["thesis_bias"] == "BULL"
    assert saved["structure_control"]["working_quadrant"] == "Q2"


def test_filled_position_promotes_stale_entry_eligible_setup_stage() -> None:
    memory = {
        "active_setups": [
            {"setup_key": "filled-long", "stage": "ENTRY_ELIGIBLE"},
        ]
    }

    _canonicalize_held_setup_stage(
        memory,
        position={"status": "LONG", "active_setup_key": "filled-long"},
    )

    assert memory["active_setups"][0]["stage"] == "CONSERVATIVE_CONFIRMED"


def test_program_tick_keeps_filled_setup_confirmed_instead_of_entry_eligible() -> None:
    previous = {
        "active_setups": [
            {"setup_key": "filled-long", "stage": "ENTRY_ELIGIBLE"},
            {"setup_key": "old", "stage": "INVALIDATED"},
        ]
    }

    setups = _program_memory_setups(
        previous,
        selected_setup=None,
        position={"status": "LONG", "active_setup_key": "filled-long"},
    )

    assert setups[0]["setup_key"] == "filled-long"
    assert setups[0]["stage"] == "CONSERVATIVE_CONFIRMED"


def test_held_position_action_owns_event_card_over_flat_false_break_template() -> None:
    common = {
        "material_type": "FALSE_BREAK_RECLAIM",
        "setup_stage": "CONSERVATIVE_CONFIRMED",
        "position_status": "LONG",
        "gate_is_eligible": False,
    }

    assert _ai_hybrid_expected_event_card_type(
        **common,
        position_action="EXIT",
    ) == "EXIT"
    assert _ai_hybrid_expected_event_card_type(
        **common,
        position_action="NONE",
    ) == "MANAGEMENT"
    assert _ai_hybrid_expected_event_card_type(
        material_type="FALSE_BREAK_RECLAIM",
        setup_stage="FORMING",
        position_status="FLAT",
        position_action="NONE",
        gate_is_eligible=False,
    ) == "OBSERVATION"


def test_hybrid_program_behavior_expiry_overlays_exit_on_hidden_tick() -> None:
    analysis = {
        "original_decision": "DONT_NOTIFY",
        "message_type": "UNCHANGED",
        "message_direction": "NEUTRAL",
        "notification_reason": "尚無變化。",
        "course_reading": {"setup_stage": "CONSERVATIVE_CONFIRMED"},
        "action": {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": "held-long",
            "trigger": "等待。",
            "entry": "無。",
            "stop_price": None,
            "expected_behavior": "等待。",
            "max_wait_bars": None,
            "management": "續抱。",
            "reentry_status": "NOT_APPLICABLE",
            "entry_rejection_reason": "NONE",
        },
    }
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        },
        "program_behavior_exit_audit": {
            "status": "PENDING_FILL",
        },
    }

    _canonicalize_program_action_envelope(
        analysis,
        ledger=ledger,
        position={"status": "LONG", "active_setup_key": "held-long"},
        entry_gate={"status": "NONE"},
        preopen=False,
    )

    assert analysis["message_type"] == "EXIT"
    assert analysis["original_decision"] == "NOTIFY"
    assert analysis["action"]["position_action"] == "EXIT"
    assert analysis["action"]["setup_key"] == "held-long"


def test_pending_behavior_exit_forces_hidden_tick_output() -> None:
    assert _requires_forced_trade_event_output(
        entry_gate={"status": "NONE"},
        protective_stop_audit={"status": "NOT_APPLICABLE"},
        program_behavior_exit_audit={"status": "PENDING_FILL"},
    ) is True


def test_same_tick_third_stop_disables_reentry_before_ai_turn() -> None:
    audit = {
        "status": "TRIGGERED",
        "event_id": "stop-3",
        "setup_key": "held-long",
        "direction": "LONG",
        "entry_time": "2026-08-11T10:29:00+08:00",
        "entry_price": 44833.0,
        "stop_price": 44822.8,
        "trigger_time": "2026-08-11T10:56:00+08:00",
        "fill_time": "2026-08-11T10:56:00+08:00",
        "fill_price": 44822.8,
        "gap_through_stop": False,
    }
    event = _program_stop_fill_event(
        audit,
        recorded_at="2026-08-11T10:56:00+08:00",
    )
    expectation = _program_reentry_expectation(
        {
            "status": "LONG",
            "active_setup_key": "held-long",
            "reentry_count": 0,
        },
        protective_stop_audit=audit,
        constitution_snapshot={
            "trading_locked": True,
            "cooldown_until": "2026-08-11T11:26:00+08:00",
        },
        as_of="2026-08-11T10:56:00+08:00",
    )

    assert event["event_type"] == "STOP_FILLED"
    assert event["event_id"] == "stop-3"
    assert event["fill_price"] == 44822.8
    assert event["realized_points"] == pytest.approx(-10.2)
    assert event["exit_classification"] == "LOSS_OR_BREAKEVEN_STOP"
    assert expectation == {
        "authority": "PROGRAM_OWNED",
        "required_position_action": "STOP",
        "setup_key": "held-long",
        "status_after_action": "NOT_APPLICABLE",
        "last_stop_at_after_action": "2026-08-11T10:56:00+08:00",
        "count_after_action": 0,
    }


def test_program_stop_event_labels_profitable_trailing_stop_without_changing_fill_type() -> None:
    event = _program_stop_fill_event(
        {
            "status": "TRIGGERED",
            "event_id": "profit-protection",
            "setup_key": "held-long",
            "direction": "LONG",
            "entry_time": "2026-08-26T10:10:00+08:00",
            "entry_price": 45311.0,
            "stop_price": 45813.8,
            "trigger_time": "2026-08-26T11:28:00+08:00",
            "fill_time": "2026-08-26T11:28:00+08:00",
            "fill_price": 45813.0,
            "gap_through_stop": True,
        },
        recorded_at="2026-08-26T11:28:00+08:00",
    )

    assert event["event_type"] == "STOP_FILLED"
    assert event["fill_price"] == 45813.0
    assert event["realized_points"] == 502.0
    assert event["exit_classification"] == "PROTECTIVE_PROFIT_EXIT"


def test_third_stop_updates_constitution_before_ai_prompt(tmp_path: Path) -> None:
    constitution_path = tmp_path / "constitution-state.json"
    for number, minute in enumerate((0, 10), start=1):
        apply_program_execution_event(
            constitution_path,
            {
                "event_type": "ENTRY_FILLED",
                "event_id": f"entry-{number}",
                "setup_key": f"setup-{number}",
                "direction": "LONG",
                "fill_time": f"2026-08-11T09:{minute:02d}:00+08:00",
                "fill_price": 100.0,
                "stop_price": 90.0,
            },
        )
        apply_program_execution_event(
            constitution_path,
            {
                "event_type": "STOP_FILLED",
                "event_id": f"stop-{number}",
                "setup_key": f"setup-{number}",
                "direction": "LONG",
                "fill_time": f"2026-08-11T09:{minute + 1:02d}:00+08:00",
                "fill_price": 90.0,
            },
        )
    apply_program_execution_event(
        constitution_path,
        {
            "event_type": "ENTRY_FILLED",
            "event_id": "entry-3",
            "setup_key": "held-long",
            "direction": "LONG",
            "fill_time": "2026-08-11T10:29:00+08:00",
            "fill_price": 44833.0,
            "stop_price": 44822.8,
        },
    )

    snapshot, event, lock_event = _apply_program_stop_before_ai(
        run_dir=tmp_path,
        protective_stop_audit={
            "status": "TRIGGERED",
            "event_id": "stop-3",
            "setup_key": "held-long",
            "direction": "LONG",
            "entry_time": "2026-08-11T10:29:00+08:00",
            "entry_price": 44833.0,
            "stop_price": 44822.8,
            "trigger_time": "2026-08-11T10:56:00+08:00",
            "fill_time": "2026-08-11T10:56:00+08:00",
            "fill_price": 44822.8,
            "gap_through_stop": False,
        },
        recorded_at="2026-08-11T10:56:00+08:00",
    )

    assert event["event_type"] == "STOP_FILLED"
    assert snapshot["consecutive_simulated_stops"] == 3
    assert snapshot["trading_locked"] is True
    assert snapshot["cooldown_until"] == "2026-08-11T11:26:00+08:00"
    assert lock_event is not None
    assert lock_event["event_type"] == "PROGRAM_CONSTITUTION_LOCKED"


def test_locked_constitution_reentry_line_overrides_one_bar_wait() -> None:
    line = _v3_reentry_line(
        {"reentry_status": "WAIT_ONE_BAR"},
        {
            "program_constitution": {
                "trading_locked": True,
                "cooldown_until": "2026-08-11T11:26:00+08:00",
            }
        },
    )

    assert line == (
        "再進場：交易憲法已鎖定至11:26；"
        "冷卻後仍須新的完整結構重新取得資格。"
    )


def test_programmatic_exit_fill_card_records_locked_next_open() -> None:
    rendered = render_programmatic_exit_fill(
        {
            "direction": "LONG",
            "entry_time": "2026-08-11T09:51:00+08:00",
            "entry_price": 44836,
            "signal_time": "2026-08-11T09:55:00+08:00",
            "fill_time": "2026-08-11T09:56:00+08:00",
            "fill_price": 44792,
            "reason_code": "EXPECTED_BEHAVIOR_EXPIRED_NO_PROGRESS",
        }
    )

    assert rendered.kind == "EXIT"
    assert "09:56｜🔴 多方模擬出場" in rendered.body
    assert "09:55已收盤K確認失效" in rendered.body
    assert "44,792點" in rendered.body
    assert "-44點" in rendered.body


def test_programmatic_exit_signal_card_uses_course_behavior_without_internal_language() -> None:
    rendered = render_programmatic_exit_signal(
        {
            "direction": "LONG",
            "entry_time": "2026-08-11T09:51:00+08:00",
            "entry_price": 44836.0,
            "signal_time": "2026-08-11T09:55:00+08:00",
            "reason_code": "EXPECTED_BEHAVIOR_EXPIRED_NO_PROGRESS",
            "behavior_audit": {"max_wait_bars": 5, "checkpoint_price": 44875.8},
        }
    )

    assert rendered.kind == "EXIT"
    assert "09:55｜🔴 多方出場訊號" in rendered.body
    assert "5根內至少推進至44,876點" in rendered.body
    assert "下一根1分K第一個可成交價" in rendered.body
    assert "程式" not in rendered.body
    assert "setup" not in rendered.body


def test_programmatic_stop_fill_card_is_compact_and_reports_gross_points() -> None:
    rendered = render_programmatic_stop_fill(
        {
            "direction": "LONG",
            "entry_time": "2026-08-11T09:59:00+08:00",
            "entry_price": 44799.0,
            "trigger_time": "2026-08-11T09:59:00+08:00",
            "fill_time": "2026-08-11T09:59:00+08:00",
            "fill_price": 44721.9,
            "gap_through_stop": False,
        }
    )

    assert rendered.kind == "STOP"
    assert "09:59｜🔴 多方模擬停損" in rendered.body
    assert "本筆毛損益：-77點" in rendered.body
    assert "新的獨立結構重新觸發" in rendered.body
    assert "程式" not in rendered.body


def test_saved_ai_raw_never_replaces_hidden_program_tick() -> None:
    assert _saved_raw_revalidation_enabled(
        reuse_saved_raw=True,
        program_only=True,
    ) is False
    assert _saved_raw_revalidation_enabled(
        reuse_saved_raw=True,
        program_only=False,
    ) is True


def test_exit_fill_suppresses_only_redundant_same_minute_observation() -> None:
    observation = {
        "original_decision": "NOTIFY",
        "message_type": "OBSERVATION",
    }
    event = {"event_type": "EXIT_FILLED"}

    assert _suppress_redundant_analysis_after_exit_fill(
        observation,
        exit_fill_event=event,
    ) is True
    assert observation["original_decision"] == "DONT_NOTIFY"

    preparation = {
        "original_decision": "NOTIFY",
        "message_type": "PREPARATION",
    }
    assert _suppress_redundant_analysis_after_exit_fill(
        preparation,
        exit_fill_event=event,
    ) is False
    assert preparation["original_decision"] == "NOTIFY"


def test_hidden_program_tick_defers_hybrid_structure_interpretation_to_ai() -> None:
    event = {
        "id": "upgrade-now",
        "event_type": "GRADE_UPGRADE",
    }
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        },
        "structure_events": [event],
    }

    assert _program_material_event_for_semantics(
        ledger,
        [{"event_id": "upgrade-now"}],
    ) is None

    program_ledger = {
        **ledger,
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_ONLY",
            "decision_authority": "PROGRAM",
        },
    }
    assert _program_material_event_for_semantics(
        program_ledger,
        [{"event_id": "upgrade-now"}],
    ) == event


def test_hybrid_prompt_assigns_judgment_to_ai_not_program() -> None:
    prompt = build_replay_prompt(
        rules_text="rules",
        schema_text=(
            '{"message_type":true,"ENTRY_ELIGIBLE":true,'
            '"reverse_anchor_candidate_ref":true}'
        ),
        runtime_context={},
        deterministic_contract=True,
        course_chain_contract=True,
        ai_hybrid=True,
    )

    assert "本版是AI_HYBRID" in prompt
    assert "AI才是課程判讀與交易決策者" in prompt
    assert "PROGRAM_CANDIDATES_AI_DECISION" in prompt
    assert "AI只能解釋程式事實" not in prompt
    assert "AI_HYBRID只把ledger.anchor_control當作各角色的可引用候選集合" in prompt
    assert "大小錨、工作段、反向候選及防線一律逐字引用" not in prompt
    assert "ai_hybrid_state_continuity_lock.required=true" in CODEX_SYSTEM_PROMPT


def test_hybrid_prompt_uses_stable_same_grade_quadrant_adjudication() -> None:
    prompt = build_replay_prompt(
        rules_text="rules",
        schema_text=(
            '{"message_type":true,"ENTRY_ELIGIBLE":true,'
            '"reverse_anchor_candidate_ref":true}'
        ),
        runtime_context={},
        deterministic_contract=True,
        course_chain_contract=True,
        ai_hybrid=True,
        trade_direction_policy="LONG_ONLY",
        trade_setup_policy="LONG_Q2_Q4_ONLY",
    )

    assert "背景象限只比較該背景錨內最近兩個已完成、同向且確實延伸同方向極值的可比攻擊段" in prompt
    assert "不得用尚在形成的小級段直接改寫background_quadrant" in prompt
    assert "僅看到1～2根K或ATR收斂" in prompt
    assert "交易家族仍歸原Q4" in prompt
    assert "EXACT_QUADRANT_REQUIRED_FOR_COMPLETE_AXES" in prompt
    assert "禁止再填TRANSITION或UNDEFINED" in prompt


def test_hybrid_state_continuity_lock_carries_prior_ai_large_control() -> None:
    previous_memory = {
        "version": 3,
        "structure_control": {
            "active_large_anchor_ref": "large-from-upgrade",
            "controlling_grade": "LARGE",
        },
    }

    lock = _ai_hybrid_state_continuity_lock(
        previous_memory,
        [{"event_id": "leg-now", "event_type": "LEG_OBSERVED"}],
    )

    assert lock["required"] is True
    assert lock["required_analysis_course_reading"] == {
        "large_anchor_ref": "large-from-upgrade",
        "controlling_grade": "LARGE",
    }
    assert lock["required_memory_structure_control"] == {
        "active_large_anchor_ref": "large-from-upgrade",
        "controlling_grade": "LARGE",
    }
    assert lock["permitted_change_events"] == []


@pytest.mark.parametrize(
    "event_type",
    ["GRADE_UPGRADE", "GRADE_DOWNGRADE", "STRUCTURE_INVALIDATED"],
)
def test_hybrid_state_continuity_lock_opens_only_for_formal_control_event(
    event_type: str,
) -> None:
    previous_memory = {
        "version": 3,
        "structure_control": {
            "active_large_anchor_ref": "large-from-upgrade",
            "controlling_grade": "LARGE",
        },
    }

    lock = _ai_hybrid_state_continuity_lock(
        previous_memory,
        [{"event_id": "structure-now", "event_type": event_type}],
    )

    assert lock["required"] is False
    assert lock["required_analysis_course_reading"] is None
    assert lock["required_memory_structure_control"] is None
    assert lock["permitted_change_events"] == [
        {"event_id": "structure-now", "event_type": event_type}
    ]


def test_hybrid_manifest_rejects_wrong_decision_authority(tmp_path: Path) -> None:
    raw = V32_MANIFEST.read_text(encoding="utf-8").replace(
        '"decision_authority": "AI_WITH_PROGRAM_GUARDRAILS"',
        '"decision_authority": "PROGRAM"',
    )
    path = tmp_path / "rule-manifest.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ReplayRuleError, match="AI_HYBRID"):
        load_rule_package(path)


def test_supervised_run_cannot_bulk_send_unreviewed_messages() -> None:
    runner = ReplayRunner(ReplayConfig(rule_manifest_path=V32_MANIFEST))

    with pytest.raises(ReplayRunError, match="監督式回放不得直接批次傳送"):
        import asyncio

        asyncio.run(
            runner.run(
                target_date=__import__("datetime").date(2026, 8, 11),
                telegram=True,
                review_mode="supervised",
            )
        )


def test_hybrid_public_card_uses_ai_quadrant_instead_of_program_baseline() -> None:
    reading = {
        "background_quadrant": "TRANSITION",
        "working_quadrant": "TRANSITION",
        "primary_quadrant_candidate": "Q4",
        "secondary_quadrant_candidate": "Q1",
        "background_trend_dynamics": "INCREASING",
        "background_volatility_dynamics": "UNSTABLE",
        "working_trend_dynamics": "UNCLEAR",
        "working_volatility_dynamics": "UNSTABLE",
    }
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        },
        "anchor_lifecycle": {
            "background_anchor": {"id": "program-anchor"},
            "quadrant_context": {
                "authority": "PROGRAM_POLICY_V1",
                "anchor_direction": "BEAR",
                "phase": "CORRECTION",
                "background_primary": "Q2",
                "working_primary": "Q3",
            },
        },
    }

    rendered = _v3_quadrant_line(reading, ledger)

    assert "大級象限轉換中（趨勢性↑／波動不穩）" in rendered
    assert "小級Q4候選（趨勢性未定／波動不穩）" in rendered
    assert "Q2" not in rendered and "Q3" not in rendered


def test_hybrid_public_structure_hides_program_candidates_rejected_by_ai() -> None:
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        },
        "anchor_records": [
            {
                "id": "large",
                "record_type": "ANCHOR",
                "direction": "BEAR",
                "status": "ACTIVE",
                "origin_time": "2026-08-10T22:12:00+08:00",
                "origin_price": 44982,
                "latest_extreme_time": "2026-08-11T00:44:00+08:00",
                "latest_extreme_price": 44681,
                "amplitude_points": -301,
                "duration_minutes": 152,
            },
            {
                "id": "program-small",
                "record_type": "ANCHOR",
                "direction": "BEAR",
                "status": "ACTIVE",
                "origin_time": "2026-08-10T23:54:00+08:00",
                "origin_price": 44855,
                "latest_extreme_time": "2026-08-11T00:44:00+08:00",
                "latest_extreme_price": 44681,
                "amplitude_points": -174,
                "duration_minutes": 50,
            },
        ],
        "anchor_lifecycle": {
            "background_anchor": {"id": "large"},
            "child_anchor": {"id": "program-small"},
        },
    }
    reading = {
        "large_anchor_ref": "large",
        "small_anchor_ref": None,
        "working_anchor_ref": None,
        "reverse_anchor_candidate_ref": None,
        "large_defense_ref": None,
        "small_defense_ref": None,
    }

    rendered = "\n".join(_v3_structure_lines(reading, ledger, message_type="OBSERVATION"))

    assert "大錨：22:12高44,982點→00:44低44,681點" in rendered
    assert "小錨" not in rendered
    assert "program-small" not in rendered


def test_hybrid_public_methods_keep_ai_taiji_and_dow_text() -> None:
    reading = {
        "focus_methods": ["TAIJI", "X_PROCESS"],
        "taiji": "AI判斷的太極父代與修正。",
        "dow": "AI判斷的大級空方道氏仍成立。",
        "yizhi": "AI判斷尚無異常動能。",
        "x_stage": "PREOPEN_CONTEXT",
        "x_process": "AI等待日盤開盤證據。",
    }
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        },
        "anchor_lifecycle": {
            "taiji_context": {"program_state": "PROGRAM_VALUE"},
        },
    }

    rendered = "\n".join(_v3_course_method_lines(reading, ledger))

    assert "太極：AI判斷的太極父代與修正。" in rendered
    assert "道氏：AI判斷的大級空方道氏仍成立。" in rendered
    assert "PROGRAM_VALUE" not in rendered


def _hybrid_reading() -> dict[str, object]:
    return {
        "large_anchor_ref": "large",
        "small_anchor_ref": "small",
        "working_anchor_ref": "working",
        "reverse_anchor_candidate_ref": None,
        "large_defense_ref": "large-defense",
        "small_defense_ref": None,
        "structure_event_ref": None,
        "controlling_grade": "LARGE",
        "grade_relation": "CONFLICT",
        "background_quadrant": "Q2",
        "working_quadrant": "Q4",
        "primary_quadrant_candidate": "Q4",
        "secondary_quadrant_candidate": "Q1",
        "background_trend_dynamics": "DECREASING",
        "background_volatility_dynamics": "EXPANDING",
        "working_trend_dynamics": "INCREASING",
        "working_volatility_dynamics": "CONTRACTING",
        "focus_methods": ["TAIJI", "X_PROCESS"],
        "cclass_mode": "TAIJI_ORDERED",
        "taiji": "AI依父代、修正與複製品質判斷為有序。",
        "yizhi": "尚無異常動能接管。",
        "left_right": "反向候選尚未取得控制。",
        "dow": "大級偏空、小級修正中。",
        "x_stage": "ANCHOR_LENS_SELECTION",
        "x_process": "依當下定錨選擇太極與四象限主鏡頭。",
        "primary_lens": "大級Q2背景、小級Q4修正。",
        "main_strategy": "等待修正完成後再評估。",
        "setup_stage": "FORMING",
        "strategy_reason": ["AI判斷與程式象限建議不同，因同級修正擴張。"],
    }


def _hybrid_ledger() -> dict[str, object]:
    return {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        },
        "anchor_control": {
            "active_background_anchor_ref": "large",
            "active_child_anchor_ref": "small",
            "working_leg_ref": "working",
            "reverse_anchor_candidate_ref": "reverse",
            "large_defense_ref": "large-defense",
            "small_defense_ref": "small-defense",
        },
        "anchor_lifecycle": {
            "background_anchor": {"id": "large", "direction": "BEAR", "status": "ACTIVE"},
            "child_anchor": {"id": "small", "direction": "BEAR", "status": "ACTIVE"},
            "quadrant_context": {
                "authority": "PROGRAM_POLICY_V1",
                "background_primary": "Q1",
                "background_trend_dynamics": "INCREASING",
                "background_volatility_dynamics": "EXPANDING",
                "working_primary": "Q1",
                "working_trend_dynamics": "INCREASING",
                "working_volatility_dynamics": "EXPANDING",
            },
        },
        "quadrant_evidence": {
            "background_20bars": {
                "recommended_quadrant": "Q1",
                "recommended_trend_dynamics": "INCREASING",
                "recommended_volatility_dynamics": "EXPANDING",
            },
            "working_5bars": {
                "recommended_quadrant": "Q1",
                "recommended_trend_dynamics": "INCREASING",
                "recommended_volatility_dynamics": "EXPANDING",
            },
        },
        "structure_events": [],
    }


def test_hybrid_semantics_preserve_ai_quadrant_against_program_recommendation() -> None:
    result = _reading_v3(_hybrid_reading(), ledger=_hybrid_ledger())

    assert result["background_quadrant"] == "Q2"
    assert result["working_quadrant"] == "Q4"
    assert result["background_trend_dynamics"] == "DECREASING"
    assert result["working_volatility_dynamics"] == "CONTRACTING"


def test_hybrid_semantics_reject_anchor_outside_role_candidate() -> None:
    reading = _hybrid_reading()
    reading["working_anchor_ref"] = "unrelated-old-leg"

    with pytest.raises(
        SemanticReplayError,
        match=r"本輪允許值：null, working；收到：unrelated-old-leg",
    ):
        _reading_v3(reading, ledger=_hybrid_ledger())


def test_hybrid_semantics_ai_selects_either_active_same_grade_directional_defense() -> None:
    ledger = _hybrid_ledger()
    ledger["anchor_lifecycle"]["dow_context"] = {
        "large_bull_defense": None,
        "large_bear_defense": None,
        "small_bull_defense": {
            "id": "small-bull-defense",
            "level": "SMALL",
            "direction": "BULL",
            "state": "ACTIVE",
        },
        "small_bear_defense": {
            "id": "small-bear-defense",
            "level": "SMALL",
            "direction": "BEAR",
            "state": "ACTIVE",
        },
    }
    reading = _hybrid_reading()
    reading["small_defense_ref"] = "small-bear-defense"

    result = _reading_v3(reading, ledger=ledger)

    assert result["small_anchor_ref"] == "small"
    assert result["small_defense_ref"] == "small-bear-defense"


def test_hybrid_semantics_rejects_inactive_or_wrong_grade_defense() -> None:
    ledger = _hybrid_ledger()
    ledger["anchor_lifecycle"]["dow_context"] = {
        "large_bull_defense": {
            "id": "large-bull-defense",
            "level": "LARGE",
            "direction": "BULL",
            "state": "ACTIVE",
        },
        "large_bear_defense": None,
        "small_bull_defense": {
            "id": "inactive-small-defense",
            "level": "SMALL",
            "direction": "BULL",
            "state": "BROKEN",
        },
        "small_bear_defense": None,
    }
    reading = _hybrid_reading()
    reading["small_defense_ref"] = "large-bull-defense"

    with pytest.raises(SemanticReplayError, match="同級作用中多方／空方防線候選"):
        _reading_v3(reading, ledger=ledger)


def test_hybrid_semantics_accept_grade_upgrade_source_anchor_atomically() -> None:
    reading = _hybrid_reading()
    reading["large_anchor_ref"] = "promoted-large"
    reading["structure_event_ref"] = "upgrade-event"
    reading["controlling_grade"] = "LARGE"
    ledger = _hybrid_ledger()
    ledger["anchor_control"]["active_background_anchor_ref"] = None
    ledger["anchor_lifecycle"]["background_anchor"] = None
    ledger["legs"] = [{"id": "promoted-large", "level": "LARGE"}]
    ledger["structure_events"] = [
        {
            "id": "upgrade-event",
            "event_type": "GRADE_UPGRADE",
            "source_anchor_id": "promoted-large",
        }
    ]

    result = _reading_v3(reading, ledger=ledger)

    assert result["large_anchor_ref"] == "promoted-large"

    memory_control = {
        "active_large_anchor_ref": "promoted-large",
        "active_small_anchor_ref": "small",
        "working_anchor_ref": "working",
        "active_reverse_candidate_ref": None,
        "controlling_grade": "LARGE",
        "background_quadrant": "Q2",
        "working_quadrant": "Q4",
        "background_quadrant_changed_at": "2026-08-25T10:08:00+08:00",
        "working_quadrant_changed_at": "2026-08-25T10:08:00+08:00",
        "last_structure_event_ref": "upgrade-event",
    }
    memory_result = _structure_control(memory_control, ledger=ledger)

    assert memory_result["active_large_anchor_ref"] == "promoted-large"


def test_hybrid_semantics_reject_unrelated_anchor_during_grade_upgrade() -> None:
    reading = _hybrid_reading()
    reading["large_anchor_ref"] = "unrelated-large"
    reading["structure_event_ref"] = "upgrade-event"
    ledger = _hybrid_ledger()
    ledger["anchor_control"]["active_background_anchor_ref"] = None
    ledger["legs"] = [
        {"id": "promoted-large", "level": "LARGE"},
        {"id": "unrelated-large", "level": "LARGE"},
    ]
    ledger["structure_events"] = [
        {
            "id": "upgrade-event",
            "event_type": "GRADE_UPGRADE",
            "source_anchor_id": "promoted-large",
        }
    ]

    with pytest.raises(SemanticReplayError, match="只能採用對應程式候選或null"):
        _reading_v3(reading, ledger=ledger)

    memory_control = {
        "active_large_anchor_ref": "unrelated-large",
        "active_small_anchor_ref": "small",
        "working_anchor_ref": "working",
        "active_reverse_candidate_ref": None,
        "controlling_grade": "LARGE",
        "background_quadrant": "Q2",
        "working_quadrant": "Q4",
        "background_quadrant_changed_at": "2026-08-25T10:08:00+08:00",
        "working_quadrant_changed_at": "2026-08-25T10:08:00+08:00",
        "last_structure_event_ref": "upgrade-event",
    }
    with pytest.raises(SemanticReplayError, match="只能採用對應程式候選或null"):
        _structure_control(memory_control, ledger=ledger)


def test_hybrid_semantics_carry_accepted_promoted_anchor_to_later_bar() -> None:
    ledger = _hybrid_ledger()
    ledger["session_key"] = "2026-08-25:DAY"
    ledger["anchor_control"]["active_background_anchor_ref"] = None
    ledger["legs"] = [{"id": "promoted-large", "level": "LARGE"}]
    ledger["structure_events"] = [
        {
            "id": "upgrade-event",
            "event_type": "GRADE_UPGRADE",
            "direction": "BEAR",
            "from_level": "SMALL",
            "to_level": "LARGE",
            "source_anchor_id": "promoted-large",
        }
    ]
    previous_memory = {
        "session_key": "2026-08-25:DAY",
        "structure_control": {
            "active_large_anchor_ref": "promoted-large",
            "controlling_grade": "LARGE",
        },
    }
    reading = _hybrid_reading()
    reading["large_anchor_ref"] = "promoted-large"
    reading["structure_event_ref"] = None
    reading["controlling_grade"] = "LARGE"

    reading_result = _reading_v3(
        reading,
        ledger=ledger,
        previous_memory=previous_memory,
    )

    assert reading_result["large_anchor_ref"] == "promoted-large"

    memory_control = {
        "active_large_anchor_ref": "promoted-large",
        "active_small_anchor_ref": "small",
        "working_anchor_ref": "working",
        "active_reverse_candidate_ref": None,
        "controlling_grade": "LARGE",
        "background_quadrant": "Q2",
        "working_quadrant": "Q4",
        "background_quadrant_changed_at": "2026-08-25T10:08:00+08:00",
        "working_quadrant_changed_at": "2026-08-25T10:10:00+08:00",
        "last_structure_event_ref": None,
    }
    memory_result = _structure_control(
        memory_control,
        ledger=ledger,
        previous_memory=previous_memory,
    )

    assert memory_result["active_large_anchor_ref"] == "promoted-large"


def test_hybrid_semantics_may_reference_active_upgrade_on_later_targeted_turn() -> None:
    ledger = _hybrid_ledger()
    ledger["anchor_control"]["active_background_anchor_ref"] = None
    ledger["anchor_lifecycle"]["background_anchor"] = None
    ledger["legs"] = [{"id": "promoted-large", "level": "LARGE"}]
    ledger["structure_events"] = [
        {
            "id": "upgrade-event",
            "event_type": "GRADE_UPGRADE",
            "direction": "BULL",
            "from_level": "SMALL",
            "to_level": "LARGE",
            "source_anchor_id": "promoted-large",
        }
    ]
    reading = _hybrid_reading()
    reading.update(
        {
            "large_anchor_ref": "promoted-large",
            "structure_event_ref": "upgrade-event",
            "controlling_grade": "LARGE",
        }
    )

    assert _referenced_active_promotion(reading, ledger=ledger)["id"] == "upgrade-event"
    assert _reading_v3(reading, ledger=ledger)["large_anchor_ref"] == "promoted-large"

    ledger["structure_events"].append(
        {
            "id": "downgrade-event",
            "event_type": "GRADE_DOWNGRADE",
            "source_upgrade_event_id": "upgrade-event",
        }
    )
    assert _referenced_active_promotion(reading, ledger=ledger) is None


def test_hybrid_semantics_do_not_carry_promoted_anchor_across_sessions() -> None:
    ledger = _hybrid_ledger()
    ledger["session_key"] = "2026-08-26:DAY"
    ledger["anchor_control"]["active_background_anchor_ref"] = None
    ledger["legs"] = [{"id": "old-promoted-large", "level": "LARGE"}]
    reading = _hybrid_reading()
    reading["large_anchor_ref"] = "old-promoted-large"
    previous_memory = {
        "session_key": "2026-08-25:DAY",
        "structure_control": {
            "active_large_anchor_ref": "old-promoted-large",
            "controlling_grade": "LARGE",
        },
    }

    with pytest.raises(SemanticReplayError, match="只能採用對應程式候選或null"):
        _reading_v3(
            reading,
            ledger=ledger,
            previous_memory=previous_memory,
        )


def test_hybrid_state_transition_rejects_silent_promoted_anchor_deletion() -> None:
    changed_at = "2026-08-25T10:08:00+08:00"
    as_of = "2026-08-25T10:10:00+08:00"
    reading = {
        "large_anchor_ref": None,
        "small_anchor_ref": "small",
        "working_anchor_ref": "working",
        "structure_event_ref": "false-break",
        "controlling_grade": "LARGE",
        "background_quadrant": "Q4",
        "working_quadrant": "Q3",
    }
    memory = {
        "session_key": "2026-08-25:DAY",
        "active_setups": [],
        "structure_control": {
            "active_large_anchor_ref": None,
            "active_small_anchor_ref": "small",
            "working_anchor_ref": "working",
            "controlling_grade": "LARGE",
            "background_quadrant": "Q4",
            "working_quadrant": "Q3",
            "background_quadrant_changed_at": changed_at,
            "working_quadrant_changed_at": as_of,
            "last_structure_event_ref": "false-break",
        },
    }
    previous_memory = {
        "version": 3,
        "as_of": changed_at,
        "session_key": "2026-08-25:DAY",
        "active_setups": [],
        "structure_control": {
            "active_large_anchor_ref": "promoted-large",
            "active_small_anchor_ref": "small",
            "working_anchor_ref": "working",
            "controlling_grade": "LARGE",
            "background_quadrant": "Q4",
            "working_quadrant": "Q4",
            "background_quadrant_changed_at": changed_at,
            "working_quadrant_changed_at": changed_at,
            "last_structure_event_ref": "upgrade-event",
        },
    }
    ledger = {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        },
        "anchor_control": {"active_background_anchor_ref": None},
        "structure_events": [
            {"id": "false-break", "event_type": "FALSE_BREAK_RECLAIM"}
        ],
    }

    with pytest.raises(SemanticReplayError, match="已接受的大錨必須持續引用原錨"):
        _validate_v3_state_transition(
            {"course_reading": reading},
            memory,
            ledger=ledger,
            previous_memory=previous_memory,
            evidence_events=[],
            expected_as_of=as_of,
        )


def test_hybrid_prompt_view_redacts_program_answers_but_keeps_measurements() -> None:
    ledger = _hybrid_ledger()
    ledger["course_method_state"] = {
        "scenario_weights": {"bull": 90, "range": 5, "bear": 5},
        "x_stage": "PROGRAM_ANSWER",
    }
    ledger["anchor_lifecycle"]["dow_context"] = {
        "large_state": "BEAR",
        "small_state": "BULL",
        "large_bear_defense": {"id": "large-defense", "price": 44855},
    }
    ledger["anchor_lifecycle"]["taiji_context"] = {
        "parent_start_price": 44982,
        "parent_end_price": 44681,
        "program_state": "COPY_SUCCESS",
        "program_quality": "STRONGER",
    }

    view = _prompt_ledger_view(ledger, ai_hybrid=True)

    assert view["ai_input_view"]["version"] == AI_HYBRID_PROMPT_VIEW_VERSION
    assert "course_method_state" not in view
    assert "background_primary" not in view["anchor_lifecycle"]["quadrant_context"]
    assert "large_state" not in view["anchor_lifecycle"]["dow_context"]
    assert view["anchor_lifecycle"]["dow_context"]["large_bear_defense"]["price"] == 44855
    assert "program_state" not in view["anchor_lifecycle"]["taiji_context"]
    assert view["anchor_lifecycle"]["taiji_context"]["parent_start_price"] == 44982


def test_hybrid_prompt_view_marks_unselected_diagnostic_candidate_observation_only() -> None:
    ledger = _hybrid_ledger()
    ledger["trade_levels"] = {
        "continuation_arm_candidate": {
            "setup_key": "selected",
            "direction": "LONG",
            "stage": "ARMED",
        },
        "false_break_arm_candidate": {
            "setup_key": "diagnostic",
            "direction": "LONG",
            "stage": "ARMED",
        },
        "course_filtered_candidate": {
            "setup_key": "diagnostic",
            "direction": "LONG",
            "stage": "ENTRY_ELIGIBLE",
        },
    }

    view = _prompt_ledger_view(ledger, ai_hybrid=True)

    levels = view["trade_levels"]
    assert levels["ai_executable_setup_keys"] == ["selected"]
    assert levels["continuation_arm_candidate"]["stage"] == "ARMED"
    assert levels["false_break_arm_candidate"]["stage"] == "FORMING"
    assert levels["false_break_arm_candidate"]["execution_status"] == "OBSERVATION_ONLY"
    assert levels["course_filtered_candidate"]["stage"] == "FORMING"
    assert ledger["trade_levels"]["false_break_arm_candidate"]["stage"] == "ARMED"


def test_hybrid_prompt_view_redacts_legacy_dow_labels() -> None:
    structured = {
        "causal_structure_n2": {
            "dow_small": "BULL",
            "dow_large": "BEAR",
            "recent_confirmed_pivots": [{"price": 44800}],
        }
    }

    market_view = _prompt_structured_market_view(structured, ai_hybrid=True)
    constraints = _prompt_deterministic_constraints(
        structured,
        stage="day",
        ai_hybrid=True,
    )

    assert "dow_small" not in market_view["causal_structure_n2"]
    assert market_view["causal_structure_n2"]["recent_confirmed_pivots"] == [{"price": 44800}]
    assert "dow_small" not in constraints and "dow_large" not in constraints
    assert constraints["market_interpretation_authority"] == "AI_HYBRID"


def test_hybrid_public_language_rejects_internal_program_authority_wording() -> None:
    analysis = {
        "notification_reason": "目前無程式候選。",
        "market_summary": [],
        "large_trend": {"details": []},
        "current_trend": {"details": []},
        "course_reading": {},
        "scenario": {},
        "action": {},
    }

    with pytest.raises(SemanticReplayError, match="使用者可見文字不得"):
        _validate_ai_hybrid_public_language(analysis, ledger=_hybrid_ledger())
