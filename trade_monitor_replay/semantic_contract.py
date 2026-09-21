from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Mapping

from .deterministic_state import allowed_references


class SemanticReplayError(ValueError):
    pass


TOP_KEYS = {"analysis", "memory"}
ANALYSIS_KEYS = {
    "original_decision", "notification_reason", "latest_closed_k_price_estimate",
    "large_trend", "current_trend", "market_summary", "course_reading", "scenario", "action",
}
ANALYSIS_V3_KEYS = ANALYSIS_KEYS | {"message_type", "message_direction"}
ANALYSIS_V34_KEYS = ANALYSIS_V3_KEYS | {"ai_course_assessment"}
COURSE_ASSESSMENT_CHECK_KEYS = {
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
READING_KEYS = {
    "large_anchor_ref", "small_anchor_ref", "large_defense_ref", "small_defense_ref",
    "quadrant", "taiji", "yizhi", "left_right", "dow", "primary_lens",
    "main_strategy", "setup_stage", "strategy_reason",
}
READING_V3_KEYS = {
    "large_anchor_ref", "small_anchor_ref", "working_anchor_ref", "large_defense_ref",
    "small_defense_ref", "structure_event_ref", "controlling_grade", "grade_relation",
    "background_quadrant", "working_quadrant", "primary_quadrant_candidate",
    "secondary_quadrant_candidate", "background_trend_dynamics", "background_volatility_dynamics",
    "working_trend_dynamics", "working_volatility_dynamics", "focus_methods",
    "taiji", "yizhi", "left_right", "dow", "primary_lens", "main_strategy",
    "setup_stage", "strategy_reason",
}
READING_V4_KEYS = READING_V3_KEYS | {"cclass_mode", "x_stage", "x_process"}
READING_V5_KEYS = READING_V4_KEYS | {"reverse_anchor_candidate_ref"}
READING_V34_KEYS = READING_V5_KEYS | {"main_strategy_family"}
ACTION_KEYS = {
    "position_action", "direction", "observation_area", "trigger", "entry",
    "structural_stop", "stop_price", "nearest_obstacle", "expected_behavior",
    "max_wait", "no_chase", "management",
}
MEMORY_KEYS = {
    "version", "as_of", "session_key", "active_setup", "thesis_bias", "maintain",
    "downgrade", "flip", "notes",
}
MEMORY_V3_KEYS = {
    "version", "as_of", "session_key", "active_setups", "thesis_bias", "maintain",
    "downgrade", "flip", "structure_control", "reentry", "notes",
}

MESSAGE_TYPES = {
    "SNAPSHOT", "OBSERVATION", "PREPARATION", "ENTRY", "REENTRY", "MANAGEMENT",
    "STOP", "EXIT", "STRUCTURE_UPGRADE", "STRUCTURE_DOWNGRADE", "INVALIDATION", "UNCHANGED",
}
ENTRY_DENIAL_REASONS = {
    "STOP_TOO_WIDE", "HARD_OBSTACLE_TOO_CLOSE", "GRADE_CONFLICT", "CONSTITUTION_BLOCKED",
}


def _trade_policy_mode(ledger: Mapping[str, Any]) -> str:
    policy = ledger.get("program_trade_policy")
    return str(policy.get("actionable_setups") or "") if isinstance(policy, Mapping) else ""


def _ai_hybrid_policy(ledger: Mapping[str, Any]) -> bool:
    policy = ledger.get("program_trade_policy")
    return bool(
        isinstance(policy, Mapping)
        and policy.get("actionable_setups") == "PROGRAM_CANDIDATES_AI_DECISION"
        and policy.get("decision_authority") == "AI_HYBRID"
    )


def _long_only_policy(ledger: Mapping[str, Any]) -> bool:
    policy = ledger.get("program_trade_policy")
    return bool(
        isinstance(policy, Mapping)
        and policy.get("trade_direction_policy") == "LONG_ONLY"
    )


def _long_only_persisted_large_anchor_ref(
    reference: Any,
    previous_reference: str | None,
    *,
    ledger: Mapping[str, Any],
) -> Any:
    """Preserve the AI's explicit anchor choice without filling ``null``.

    The semantic schema requires the field on every turn, so ``null`` is an
    explicit decision rather than an omitted repeat field.  A still-causal
    previous anchor may be admitted by the reference validator, but only the AI
    may choose whether to keep using it.
    """

    return reference


def _upgrade_controlling_anchor_ref(
    event: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> str | None:
    """Resolve one accepted upgrade to the same controlling anchor everywhere.

    A lifecycle background anchor represents the installed course-level
    structure and therefore outranks the lower-level source leg that detected
    the upgrade.  The source leg remains the fallback for the causal instant
    before the lifecycle has installed a background anchor.
    """

    anchor_control = ledger.get("anchor_control")
    active_background = (
        anchor_control.get("active_background_anchor_ref")
        if isinstance(anchor_control, Mapping)
        else None
    )
    if isinstance(active_background, str) and active_background:
        return active_background
    source_anchor = event.get("source_anchor_id")
    return source_anchor if isinstance(source_anchor, str) and source_anchor else None


def _canonicalize_long_only_upgrade_references(
    analysis: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    memory: dict[str, Any] | None = None,
    ai_generated: bool = True,
) -> str | None:
    """Validate or canonicalize an accepted upgrade as one atomic decision."""

    if not _long_only_policy(ledger):
        return None
    reading = analysis.get("course_reading")
    if not isinstance(reading, dict):
        return None
    event_ref = reading.get("structure_event_ref")
    event = next(
        (
            item
            for item in ledger.get("structure_events", [])
            if isinstance(item, Mapping)
            and item.get("id") == event_ref
            and item.get("event_type") == "GRADE_UPGRADE"
        ),
        None,
    )
    if not isinstance(event, Mapping):
        return None
    expected = _upgrade_controlling_anchor_ref(event, ledger=ledger)
    if not isinstance(expected, str) or not expected:
        return None
    if _ai_hybrid_policy(ledger) and ai_generated:
        if reading.get("large_anchor_ref") != expected:
            raise SemanticReplayError(
                "AI採用結構升級事件時，large_anchor_ref必須引用作用中背景大錨；"
                "尚未建立背景大錨時才可引用該事件的source_anchor_id；"
                "驗證器不會代替AI改寫。"
            )
        if reading.get("controlling_grade") != "LARGE":
            raise SemanticReplayError(
                "AI採用結構升級事件時，controlling_grade必須為LARGE；"
                "驗證器不會代替AI改寫。"
            )
        if isinstance(memory, dict):
            control = memory.get("structure_control")
            if isinstance(control, Mapping):
                if control.get("active_large_anchor_ref") != expected:
                    raise SemanticReplayError(
                        "AI採用結構升級事件時，memory.active_large_anchor_ref必須與analysis一致；"
                        "驗證器不會代替AI改寫。"
                    )
                if control.get("controlling_grade") != "LARGE":
                    raise SemanticReplayError(
                        "AI採用結構升級事件時，memory.controlling_grade必須為LARGE；"
                        "驗證器不會代替AI改寫。"
                    )
        return expected

    reading["large_anchor_ref"] = expected
    reading["controlling_grade"] = "LARGE"
    if isinstance(memory, dict):
        control = memory.get("structure_control")
        if isinstance(control, dict):
            control["active_large_anchor_ref"] = expected
            control["controlling_grade"] = "LARGE"
    return expected


def _program_only_policy(ledger: Mapping[str, Any]) -> bool:
    # Existing v31 fixtures and legacy helper calls predate the explicit
    # policy envelope.  Preserve their program-owned behavior; only the new
    # v32 marker opts out of canonicalization.
    return not _ai_hybrid_policy(ledger)


def empty_semantic_memory(
    *,
    as_of: str,
    session_key: str,
    version: int = 2,
    anchor_lifecycle: bool = False,
) -> dict[str, Any]:
    if version == 3:
        memory = {
            "version": 3,
            "as_of": as_of,
            "session_key": session_key,
            "active_setups": [],
            "thesis_bias": "UNDEFINED",
            "maintain": "等待第一次課程判讀。",
            "downgrade": "等待第一次課程判讀。",
            "flip": "等待第一次課程判讀。",
            "structure_control": {
                "active_large_anchor_ref": None,
                "active_small_anchor_ref": None,
                "working_anchor_ref": None,
                "controlling_grade": "UNDEFINED",
                "background_quadrant": "UNDEFINED",
                "working_quadrant": "UNDEFINED",
                "background_quadrant_changed_at": as_of,
                "working_quadrant_changed_at": as_of,
                "last_structure_event_ref": None,
            },
            "reentry": {"status": "NOT_APPLICABLE", "last_stop_at": None, "count": 0},
            "notes": ["客觀樞紐、段、結構升級、道氏防線與持倉由回放程式管理。"],
        }
        if anchor_lifecycle:
            memory["structure_control"]["active_reverse_candidate_ref"] = None
        return memory
    if version != 2:
        raise SemanticReplayError("不支援的semantic memory版本。")
    return {
        "version": 2,
        "as_of": as_of,
        "session_key": session_key,
        "active_setup": None,
        "thesis_bias": "UNDEFINED",
        "maintain": "等待第一次課程判讀。",
        "downgrade": "等待第一次課程判讀。",
        "flip": "等待第一次課程判讀。",
        "notes": ["客觀樞紐、段、道氏防線與持倉由回放程式管理。"],
    }


def validate_semantic_envelope(
    value: Any,
    *,
    ledger: Mapping[str, Any],
    expected_as_of: str,
    expected_session_key: str,
    preopen: bool,
    position: Mapping[str, Any],
    evidence_events: list[Mapping[str, Any]] | None = None,
    previous_memory: Mapping[str, Any] | None = None,
    entry_gate: Mapping[str, Any] | None = None,
    ai_generated: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != TOP_KEYS:
        raise SemanticReplayError("回放輸出頂層必須只有analysis與memory。")
    _reject_corrupt_text_fragments(value)
    raw_memory = value.get("memory")
    memory_version = raw_memory.get("version") if isinstance(raw_memory, Mapping) else None
    if memory_version == 3:
        analysis = _analysis_v3(
            value.get("analysis"),
            ledger=ledger,
            preopen=preopen,
            position=position,
            evidence_events=evidence_events or [],
            entry_gate=entry_gate,
            previous_memory=previous_memory,
            ai_generated=ai_generated,
        )
        if ai_generated:
            _validate_ai_hybrid_public_language(analysis, ledger=ledger)
        memory = _memory_v3(
            raw_memory,
            ledger=ledger,
            expected_as_of=expected_as_of,
            expected_session_key=expected_session_key,
            previous_memory=previous_memory,
        )
        _canonicalize_long_only_upgrade_references(
            analysis,
            ledger=ledger,
            memory=memory,
            ai_generated=ai_generated,
        )
        _validate_trade_direction_policy(
            analysis,
            memory,
            ledger=ledger,
            position=position,
            entry_gate=entry_gate,
        )
        _validate_trade_setup_policy(
            analysis,
            ledger=ledger,
            position=position,
            entry_gate=entry_gate,
        )
        _canonicalize_program_course_memory(
            analysis,
            memory,
            ledger=ledger,
            previous_memory=previous_memory,
            expected_as_of=expected_as_of,
            expected_session_key=expected_session_key,
        )
        _canonicalize_program_setup_and_entry(
            analysis,
            memory,
            ledger=ledger,
            position=position,
            entry_gate=entry_gate,
            preopen=preopen,
        )
        _canonicalize_unavailable_background_quadrant_memory(
            analysis,
            memory,
            ledger=ledger,
            previous_memory=previous_memory,
            expected_as_of=expected_as_of,
            ai_generated=ai_generated,
        )
        _canonicalize_pre_structure_working_quadrant(
            analysis,
            memory,
            ledger=ledger,
            previous_memory=previous_memory,
            expected_as_of=expected_as_of,
            ai_generated=ai_generated,
        )
        _preserve_promoted_structure_quadrants(
            analysis,
            memory,
            ledger=ledger,
            previous_memory=previous_memory,
        )
        _restore_program_held_setup(
            memory,
            previous_memory=previous_memory,
            position=position,
        )
        _canonicalize_held_setup_stage(memory, position=position)
        _terminalize_explicit_exit_setup(
            analysis,
            memory,
            position=position,
        )
        _canonicalize_program_owned_actionable_setups(
            analysis,
            memory,
            ledger=ledger,
            position=position,
            entry_gate=entry_gate,
            preopen=preopen,
            ai_generated=ai_generated,
        )
        _canonicalize_ai_hybrid_mechanical_memory_fields(
            analysis,
            memory,
            ledger=ledger,
            position=position,
            entry_gate=entry_gate,
            previous_memory=previous_memory,
            expected_as_of=expected_as_of,
            expected_session_key=expected_session_key,
            ai_generated=ai_generated,
        )
        _validate_held_setup_continuity(memory, position=position, analysis=analysis)
        memory = _opening_observation_memory(
            analysis,
            memory,
            ledger=ledger,
            ai_generated=ai_generated,
        )
        _validate_preparation_contract(analysis, memory, ledger=ledger)
        _validate_countertrend_preparation_maturity(
            analysis,
            ledger=ledger,
        )
        _validate_program_continuation_arm(
            analysis,
            memory,
            ledger=ledger,
            position=position,
            entry_gate=entry_gate,
            preopen=preopen,
        )
        _validate_program_owned_actionable_setups(
            analysis,
            memory,
            ledger=ledger,
            position=position,
            entry_gate=entry_gate,
            preopen=preopen,
        )
        _restore_program_reentry_setup(
            memory,
            previous_memory=previous_memory,
            position=position,
            expected_as_of=expected_as_of,
        )
        _canonicalize_program_reentry_summary(
            analysis,
            memory,
            ledger=ledger,
            position=position,
            expected_as_of=expected_as_of,
        )
        _validate_program_reentry_state(
            analysis,
            memory,
            ledger=ledger,
            position=position,
            expected_as_of=expected_as_of,
        )
        # This is one fact duplicated for presentation and memory continuity,
        # not two independent model decisions.  Both references have already
        # been validated against the current causal ledger.  Keep the analysis
        # reference canonical so a stale-but-valid event ID copied into memory
        # cannot discard an otherwise valid market reading.
        analysis_event_ref = analysis["course_reading"]["structure_event_ref"]
        memory_event_ref = memory["structure_control"]["last_structure_event_ref"]
        if _ai_hybrid_policy(ledger) and ai_generated:
            if memory_event_ref != analysis_event_ref:
                raise SemanticReplayError(
                    "AI_HYBRID的analysis與memory結構事件引用必須一致；"
                    "驗證器不會代替AI改寫memory。"
                )
        else:
            memory["structure_control"]["last_structure_event_ref"] = analysis_event_ref
        _validate_v3_state_transition(
            analysis,
            memory,
            ledger=ledger,
            previous_memory=previous_memory,
            evidence_events=evidence_events or [],
            expected_as_of=expected_as_of,
            ai_generated=ai_generated,
        )
        memory = _consume_denied_entry_setup(
            analysis,
            memory,
            entry_gate=entry_gate,
        )
        # Canonicalizers may restore a program-owned setup or action after the
        # model payload is parsed.  Recheck the final envelope so LONG_ONLY is
        # enforced on the state that will actually be persisted and executed.
        _validate_trade_direction_policy(
            analysis,
            memory,
            ledger=ledger,
            position=position,
            entry_gate=entry_gate,
        )
        _validate_trade_setup_policy(
            analysis,
            ledger=ledger,
            position=position,
            entry_gate=entry_gate,
        )
    else:
        analysis = _analysis(value.get("analysis"), ledger=ledger, preopen=preopen, position=position)
        memory = _memory(
            raw_memory,
            expected_as_of=expected_as_of,
            expected_session_key=expected_session_key,
        )
    return {"analysis": analysis, "memory": memory}


def _validate_trade_direction_policy(
    analysis: Mapping[str, Any],
    memory: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    entry_gate: Mapping[str, Any] | None,
) -> None:
    """Fail closed when a long-only replay attempts any short execution state."""

    if not _long_only_policy(ledger):
        return
    if position.get("status") == "SHORT":
        raise SemanticReplayError("LONG_ONLY回放不得承接SHORT持倉。")
    if isinstance(entry_gate, Mapping) and entry_gate.get("direction") == "SHORT":
        raise SemanticReplayError("LONG_ONLY回放不得產生SHORT進場資格。")
    action = analysis.get("action")
    if isinstance(action, Mapping) and action.get("direction") == "SHORT":
        raise SemanticReplayError("LONG_ONLY回放的action.direction不得為SHORT。")
    for setup in memory.get("active_setups", []):
        if isinstance(setup, Mapping) and setup.get("direction") == "SHORT":
            raise SemanticReplayError("LONG_ONLY回放不得保存SHORT setup。")


def _validate_trade_setup_policy(
    analysis: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    entry_gate: Mapping[str, Any] | None,
) -> None:
    """Keep the stage-one execution universe at long Q2/Q4 only.

    The program owns the candidate family while AI owns the course-level
    quadrant reading.  This validator joins the two only when AI actually
    accepts an eligible entry; observation and risk diagnosis remain free to
    discuss every course method.
    """

    policy = ledger.get("program_trade_policy")
    if not isinstance(policy, Mapping) or policy.get("trade_setup_policy") != "LONG_Q2_Q4_ONLY":
        return
    q4_sources = {
        "ANCHOR_LEG_SEQUENCE",
        "CONFIRMED_PULLBACK_ENDPOINT_N2",
        "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
    }
    q2_sources = {
        "FALSE_BREAK_RECLAIM",
        "Q2_FAILED_REVERSE_CANDIDATE",
        "Q2_SLOW_OUTER_EXPANSION_FAILURE",
    }
    allowed_sources = q2_sources | q4_sources

    behavior_plan = position.get("behavior_plan")
    if isinstance(behavior_plan, Mapping):
        held_source = behavior_plan.get("candidate_source")
        if held_source not in allowed_sources:
            raise SemanticReplayError("Q2／Q4限定回放不得承接其他戰法持倉。")

    assessment = analysis.get("ai_course_assessment")
    assessed_armable_candidate: Mapping[str, Any] | None = None
    if isinstance(assessment, Mapping):
        levels = ledger.get("trade_levels")
        armable_candidate_by_key = {
            str(item.get("setup_key")): item
            for item in _ai_armable_program_candidates(levels)
            if item.get("setup_key")
        }
        candidate_by_key = dict(armable_candidate_by_key)
        if isinstance(entry_gate, Mapping) and entry_gate.get("setup_key"):
            candidate_by_key.setdefault(str(entry_gate["setup_key"]), entry_gate)
        assessed_key = str(assessment.get("setup_key") or "")
        assessed_armable_candidate = armable_candidate_by_key.get(assessed_key)
        assessed_candidate = candidate_by_key.get(assessed_key)
        if not isinstance(assessed_candidate, Mapping):
            raise SemanticReplayError(
                "ai_course_assessment只能引用本輪硬事實合格的Q2／Q4候選。"
            )
        expected_hash = (
            assessed_candidate.get("facts_hash")
            or assessed_candidate.get("required_facts_hash")
        )
        if expected_hash is None or assessment.get("facts_hash") != expected_hash:
            raise SemanticReplayError(
                "ai_course_assessment.facts_hash必須逐字引用本輪候選版本。"
            )

    if not isinstance(entry_gate, Mapping) or entry_gate.get("status") != "ENTRY_ELIGIBLE":
        return
    source = entry_gate.get("required_candidate_source")
    if source not in allowed_sources:
        raise SemanticReplayError("Q2／Q4限定回放不得產生其他戰法進場資格。")
    action = analysis.get("action")
    if not isinstance(action, Mapping) or action.get("position_action") != "ENTER":
        return
    # In program-only diagnostics the deterministic engine owns both the
    # candidate family and the ENTER decision.  Requiring an AI quadrant here
    # would make an AI-free replay depend on a judgement source that does not
    # exist.  Keep the source whitelist above, but reserve the semantic
    # quadrant join below for the actual AI_HYBRID path.
    if policy.get("decision_authority") == "PROGRAM":
        return
    reading = analysis.get("course_reading")
    if not isinstance(reading, Mapping):
        raise SemanticReplayError("Q2／Q4進場必須包含課程象限判讀。")

    required_strategy = str(entry_gate.get("required_entry_strategy") or "")
    if required_strategy.startswith("Q4_"):
        expected = "Q4"
    elif required_strategy.startswith("Q2_"):
        expected = "Q2"
    else:
        expected = "Q4" if source in q4_sources else "Q2"
    working = str(reading.get("working_quadrant") or "")
    primary = str(reading.get("primary_quadrant_candidate") or "")
    trend = str(reading.get("working_trend_dynamics") or "")
    volatility = str(reading.get("working_volatility_dynamics") or "")
    # ``main_strategy_family`` records the causal setup that earned entry
    # permission.  ``working_quadrant`` records the market state after the
    # latest closed bar.  They normally agree while the setup is forming, but
    # a valid Q2/Q4 relaunch can already have expanded into Q1 on its trigger
    # bar.  Validate the frozen assessment before allowing that transition so
    # an unrelated Q1 move can never be smuggled into the stage-one universe.
    strategy_family = reading.get("main_strategy_family")
    if strategy_family is not None and strategy_family != expected:
        raise SemanticReplayError(
            "ENTER的main_strategy_family必須與entry_gate.required_entry_strategy的Q2／Q4家族一致。"
        )
    required_facts_hash = entry_gate.get("required_facts_hash")
    frozen_assessment_passed = False
    if required_facts_hash is not None:
        assessment = analysis.get("ai_course_assessment")
        if not isinstance(assessment, Mapping):
            raise SemanticReplayError("新Q2／Q4因果候選缺少AI課程品質判讀，不得ENTER。")
        if assessment.get("setup_key") != entry_gate.get("setup_key"):
            raise SemanticReplayError("AI課程品質判讀的setup_key與ENTRY_ELIGIBLE不一致。")
        if assessment.get("facts_hash") != required_facts_hash:
            raise SemanticReplayError("AI課程品質判讀使用的facts_hash已過期或不一致。")
        overall = str(assessment.get("overall") or "UNKNOWN").upper()
        checks = assessment.get("checks")
        check_values = (
            [str(value).upper() for value in checks.values()]
            if isinstance(checks, Mapping)
            else []
        )
        if overall == "UNKNOWN" or "UNKNOWN" in check_values:
            raise SemanticReplayError("AI課程品質仍為UNKNOWN（證據不明），不得ENTER。")
        if overall != "PASS" or not check_values or any(
            value != "PASS" for value in check_values
        ):
            raise SemanticReplayError("AI課程品質未全部PASS，不得ENTER。")
        frozen_assessment_passed = True
    expected_axes = (
        ("INCREASING", "CONTRACTING")
        if expected == "Q4"
        else ("DECREASING", "EXPANDING")
    )
    matches_origin_state = (trend, volatility) == expected_axes and (
        working == expected
        or (working == "TRANSITION" and primary == expected)
    )
    matches_post_trigger_q1 = (
        frozen_assessment_passed
        and strategy_family == expected
        and working == "Q1"
        and primary == "Q1"
        and (trend, volatility) == ("INCREASING", "EXPANDING")
        and isinstance(assessed_armable_candidate, Mapping)
        and assessed_armable_candidate.get("candidate_source") == source
        and assessed_armable_candidate.get("entry_strategy") == required_strategy
        and assessed_armable_candidate.get("facts_hash") == required_facts_hash
        and isinstance(assessed_armable_candidate.get("hard_fact_gate"), Mapping)
        and assessed_armable_candidate["hard_fact_gate"].get("status") == "PASSED"
    )
    if not (matches_origin_state or matches_post_trigger_q1):
        raise SemanticReplayError(
            f"{expected}交易候選只有在AI確認原{expected}設置，或其合法重新發動已轉為Q1時才能ENTER。"
        )


def _preserve_promoted_structure_quadrants(
    analysis: dict[str, Any],
    memory: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    previous_memory: Mapping[str, Any] | None,
) -> None:
    """Prevent an active promoted structure's known axes from vanishing.

    Q1/Q2/Q3/Q4 may still change normally when the analyzer sees new two-axis
    evidence. This guard only treats a later ``UNDEFINED`` as missing output:
    it carries forward the last causally known quadrant until an explicit new
    quadrant or a programmatic downgrade/invalidation arrives.
    """

    if _ai_hybrid_policy(ledger):
        return

    reading = analysis.get("course_reading")
    control = memory.get("structure_control")
    previous = previous_memory if isinstance(previous_memory, Mapping) else None
    previous_control = previous.get("structure_control") if previous else None
    if not (
        isinstance(reading, dict)
        and isinstance(control, dict)
        and isinstance(previous_control, Mapping)
        and previous.get("session_key") == memory.get("session_key")
        and previous_control.get("controlling_grade") == "LARGE"
        and isinstance(_latest_active_promoted_structure(ledger), Mapping)
    ):
        return

    # Grade control is a deterministic lifecycle state. A model may describe
    # the current move as a correction, but it cannot demote or erase LARGE
    # without a program-generated downgrade/parent-invalidation event.
    reading["controlling_grade"] = "LARGE"
    control["controlling_grade"] = "LARGE"

    axes_for_quadrant = {
        "Q1": ("INCREASING", "EXPANDING"),
        "Q2": ("DECREASING", "EXPANDING"),
        "Q3": ("DECREASING", "CONTRACTING"),
        "Q4": ("INCREASING", "CONTRACTING"),
    }
    for prefix, control_key, changed_key in (
        ("background", "background_quadrant", "background_quadrant_changed_at"),
        ("working", "working_quadrant", "working_quadrant_changed_at"),
    ):
        current = reading.get(f"{prefix}_quadrant")
        prior = previous_control.get(control_key)
        if current != "UNDEFINED" or prior not in axes_for_quadrant:
            continue
        trend_axis, volatility_axis = axes_for_quadrant[str(prior)]
        reading[f"{prefix}_quadrant"] = prior
        reading[f"{prefix}_trend_dynamics"] = trend_axis
        reading[f"{prefix}_volatility_dynamics"] = volatility_axis
        if prefix == "working" and reading.get("primary_quadrant_candidate") == "UNDEFINED":
            reading["primary_quadrant_candidate"] = prior
        control[control_key] = prior
        control[changed_key] = previous_control.get(changed_key)

    # Resolve the public trend only after any missing causal quadrant axes have
    # been restored.  The background trend axis is what distinguishes a
    # continuing promoted move from its correction phase.
    analysis["large_trend"] = _large_trend_for_promoted_structure(
        analysis["large_trend"], reading, ledger=ledger
    )

    # ``notes`` is fed back into the next model call.  Do not allow free-form
    # prose there to contradict the program-owned grade lifecycle even when the
    # structured field has already been corrected.  Keep one deterministic
    # statement instead of date-specific wording from the analyzer.
    notes = memory.get("notes")
    if isinstance(notes, list):
        memory["notes"] = [
            str(note)
            for note in notes
            if "控制級數" not in str(note)
        ] + ["控制級數維持大級；只有程式化降級或父級失效事件可解除。"]


def _validate_held_setup_continuity(
    memory: Mapping[str, Any],
    *,
    position: Mapping[str, Any],
    analysis: Mapping[str, Any] | None = None,
) -> None:
    """A live simulated position keeps ownership of its originating setup.

    NO_CHASE/INVALIDATED describe flat-position entry eligibility.  Applying
    either state to the setup that already owns an open position silently
    retires the trade and later prevents legitimate management references.
    """

    if position.get("status") not in {"LONG", "SHORT"}:
        return
    setup_key = position.get("active_setup_key")
    if not setup_key:
        return
    setups = memory.get("active_setups")
    matching = next(
        (
            item
            for item in setups
            if isinstance(item, Mapping) and item.get("setup_key") == setup_key
        ),
        None,
    ) if isinstance(setups, list) else None
    if not isinstance(matching, Mapping):
        raise SemanticReplayError("持倉中必須在memory保留目前部位的active_setup_key。")
    action = analysis.get("action") if isinstance(analysis, Mapping) else None
    closing_position = (
        isinstance(action, Mapping)
        and action.get("position_action") in {"STOP", "EXIT"}
    )
    if matching.get("stage") in {"NO_CHASE", "INVALIDATED"} and not closing_position:
        raise SemanticReplayError("持倉中的作用中setup不得標成NO_CHASE或INVALIDATED；只能由STOP或EXIT結束。")


def _canonicalize_held_setup_stage(
    memory: dict[str, Any],
    *,
    position: Mapping[str, Any],
) -> None:
    """Prevent execution-owned setup stage from moving backwards after fill."""

    if position.get("status") not in {"LONG", "SHORT"}:
        return
    setup_key = position.get("active_setup_key")
    setups = memory.get("active_setups")
    if not setup_key or not isinstance(setups, list):
        return
    for item in setups:
        if not isinstance(item, dict) or item.get("setup_key") != setup_key:
            continue
        if item.get("stage") in {"ARMED", "ENTRY_ELIGIBLE"}:
            item["stage"] = "CONSERVATIVE_CONFIRMED"
        return


def _terminalize_explicit_exit_setup(
    analysis: Mapping[str, Any],
    memory: dict[str, Any],
    *,
    position: Mapping[str, Any],
) -> None:
    """Retire a setup when its live position is closed by an explicit EXIT.

    ``EXIT`` means the accepted setup's time, motive, or structure thesis has
    ended.  Leaving its memory stage as ARMED/CONFIRMED lets the next replay
    cutoff detect the old trigger again and queue a second ``INITIAL`` entry
    with the same setup key.  A later trade must therefore come from a new
    deterministic structure (new key).  ``STOP`` is intentionally excluded:
    the course contract permits one separately controlled re-entry after a
    protective stop.
    """

    action = analysis.get("action") if isinstance(analysis, Mapping) else None
    if (
        not isinstance(action, Mapping)
        or action.get("position_action") != "EXIT"
        or position.get("status") not in {"LONG", "SHORT"}
    ):
        return
    setup_key = position.get("active_setup_key") or action.get("setup_key")
    if not setup_key:
        return
    setups = memory.get("active_setups")
    if not isinstance(setups, list):
        return
    for raw in setups:
        if not isinstance(raw, dict) or raw.get("setup_key") != setup_key:
            continue
        raw["stage"] = "INVALIDATED"
        raw["trigger"] = (
            "本setup已依時間、動機或結構失效主動出場；不得沿用原觸發再次進場，"
            "須等待新的同級結構並建立新setup_key。"
        )
        raw["reentry_status"] = "NOT_APPLICABLE"
        return


def _reject_corrupt_text_fragments(value: Any, *, path: str = "$") -> None:
    """Reject obvious serialization debris embedded inside prose fields.

    A model response can remain syntactically valid JSON while accidentally
    copying an object-boundary fragment such as ``},{`` into a quoted string.
    That is not valid trading analysis and must never enter the checkpoint or
    delivery audit, even when the shortened public card would omit the field.
    """

    if isinstance(value, str):
        self_correction_debris = (
            "等等？" in value
            or "等等?" in value
            or "。・。" in value
        )
        foreign_meta_debris = bool(
            re.search(r"[\u3040-\u30ff]", value)
            or re.search(
                r"(?i)\b(?:however,\s*)?i\s+(?:must|need|should|will)\b"
                r"|\b(?:last|final)\s+(?:answer|response)\b"
                r"|\bthe user\b",
                value,
            )
        )
        invisible_control_debris = bool(re.search(r"[\u200b-\u200f\u2060-\u206f\ufeff]", value))
        if (
            "\ufffd" in value
            or "\x00" in value
            or "},{" in value
            or self_correction_debris
            or foreign_meta_debris
            or invisible_control_debris
        ):
            raise SemanticReplayError(f"文字欄位含有疑似截斷或序列化殘片：{path}。")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_corrupt_text_fragments(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_corrupt_text_fragments(child, path=f"{path}[{index}]")


def _latest_material_structure_event(
    new_structure_events: list[Mapping[str, Any]],
    *,
    ledger: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Choose the one event that owns the public event card.

    A reversal bar can both downgrade the old background direction and upgrade
    the opposite direction. When the anchor lifecycle has already installed a
    new active background anchor, the matching upgrade is the canonical event;
    reporting only the displaced direction's downgrade would describe the old
    structure instead of the structure that now controls the market.
    """

    anchor_control = ledger.get("anchor_control")
    lifecycle = ledger.get("anchor_lifecycle")
    background = lifecycle.get("background_anchor") if isinstance(lifecycle, Mapping) else None
    active_ref = (
        anchor_control.get("active_background_anchor_ref")
        if isinstance(anchor_control, Mapping)
        else None
    )
    if (
        isinstance(background, Mapping)
        and background.get("status") == "ACTIVE"
        and background.get("id") == active_ref
    ):
        active_direction = background.get("direction")
        matching_upgrades = [
            item
            for item in new_structure_events
            if item.get("event_type") == "GRADE_UPGRADE"
            and item.get("direction") == active_direction
        ]
        if matching_upgrades:
            return max(
                matching_upgrades,
                key=lambda item: (str(item.get("first_seen_at")), str(item.get("id"))),
            )

    event_priority = {
        "FALSE_BREAK_RECLAIM": 1,
        "GRADE_UPGRADE": 2,
        "GRADE_DOWNGRADE": 3,
        "STRUCTURE_INVALIDATED": 4,
    }
    return max(
        (
            item
            for item in new_structure_events
            if item.get("event_type") in event_priority
        ),
        key=lambda item: (
            str(item.get("first_seen_at")),
            event_priority[str(item.get("event_type"))],
            str(item.get("id")),
        ),
        default=None,
    )


def _canonicalize_long_only_latest_material_event_ref(
    reading: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    evidence_events: list[Mapping[str, Any]],
) -> str | None:
    """Return a newly visible AI-selected event without changing its meaning.

    This compatibility helper intentionally performs no canonical selection.
    More than one objective event can become visible on the same closed bar
    (for example a false-break reclaim and a grade-upgrade candidate). Choosing
    which event has course/control significance belongs to AI_HYBRID; the
    program only verifies that the selected event was causally visible now.
    """

    if not _long_only_policy(ledger):
        return None
    raw_by_id = {
        str(item.get("id")): item
        for item in ledger.get("structure_events", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    new_structure_events = [
        raw_by_id[str(item.get("event_id"))]
        for item in evidence_events
        if isinstance(item, Mapping) and str(item.get("event_id")) in raw_by_id
    ]
    selected_ref = reading.get("structure_event_ref")
    visible_refs = {str(item.get("id")) for item in new_structure_events}
    if selected_ref not in visible_refs:
        return None
    return str(selected_ref)


def _aligned_entry_grade_upgrade(
    *,
    ledger: Mapping[str, Any],
    evidence_events: list[Mapping[str, Any]],
    entry_gate: Mapping[str, Any],
) -> bool:
    """Return whether the eligible entry is reinforced by a new grade upgrade.

    A same-direction SMALL -> LARGE upgrade changes the observation/control
    grade; it does not invalidate an already-triggered entry in that direction.
    The lifecycle must have installed the matching active background anchor and
    the upgrade must be newly visible in this causal evaluation.  This keeps
    the exception narrow: a stale upgrade, opposite-direction takeover or a
    genuine stop/obstacle problem may still deny the entry.
    """

    expected_direction = {
        "LONG": "BULL",
        "SHORT": "BEAR",
    }.get(str(entry_gate.get("direction")))
    if expected_direction is None:
        return False

    lifecycle = ledger.get("anchor_lifecycle")
    background = (
        lifecycle.get("background_anchor")
        if isinstance(lifecycle, Mapping)
        else None
    )
    if not (
        isinstance(background, Mapping)
        and background.get("status") == "ACTIVE"
        and background.get("direction") == expected_direction
    ):
        return False

    visible_ids = {
        str(item.get("event_id"))
        for item in evidence_events
        if isinstance(item, Mapping) and item.get("event_id")
    }
    return any(
        isinstance(item, Mapping)
        and str(item.get("id")) in visible_ids
        and item.get("event_type") == "GRADE_UPGRADE"
        and item.get("direction") == expected_direction
        and item.get("from_level") == "SMALL"
        and item.get("to_level") == "LARGE"
        for item in ledger.get("structure_events", [])
    )


def _is_large_aligned_false_break_reentry(
    *,
    ledger: Mapping[str, Any],
    entry_gate: Mapping[str, Any],
) -> bool:
    """Recognize a re-entry whose small-grade conflict is strategy context.

    A false-break reclaim re-entry is deliberately evaluated before the
    opposite small-grade defense is fully broken. When its direction agrees
    with an active background anchor and that anchor's large-grade defense is
    intact, the small-grade conflict is a checkpoint, not by itself a valid
    veto. Other concrete denials (hard obstacle, predeclared risk ceiling, or
    constitution lock) remain available.
    """

    expected_direction = {"LONG": "BULL", "SHORT": "BEAR"}.get(
        str(entry_gate.get("direction"))
    )
    if entry_gate.get("entry_role") != "REENTRY" or expected_direction is None:
        return False
    lifecycle = ledger.get("anchor_lifecycle")
    background = lifecycle.get("background_anchor") if isinstance(lifecycle, Mapping) else None
    defense = background.get("defense") if isinstance(background, Mapping) else None
    levels = ledger.get("trade_levels")
    reclaim = levels.get("latest_false_break_reentry") if isinstance(levels, Mapping) else None
    return bool(
        isinstance(background, Mapping)
        and background.get("direction") == expected_direction
        and background.get("status") == "ACTIVE"
        and isinstance(defense, Mapping)
        and defense.get("state") == "ACTIVE"
        and isinstance(reclaim, Mapping)
        and reclaim.get("direction") == expected_direction
        and reclaim.get("source_event_id")
    )


def _is_highest_active_anchor_aligned_entry(
    *,
    ledger: Mapping[str, Any],
    entry_gate: Mapping[str, Any],
) -> bool:
    """An aligned continuation cannot be vetoed as a grade conflict.

    The active background anchor has priority; when no large-grade background
    exists, the active child anchor controls.  A working correction naturally
    points the other way, but that is the setup context for Q4/Taiji
    continuation rather than a conflict with the controlling grade.
    """

    expected_direction = {"LONG": "BULL", "SHORT": "BEAR"}.get(
        str(entry_gate.get("direction"))
    )
    if expected_direction is None:
        return False
    if _long_only_policy(ledger):
        # This helper has no access to the last validated AI reading. Using the
        # program lifecycle here would therefore let the evidence engine veto
        # the model's GRADE_CONFLICT decision. Internal consistency with an
        # AI-selected upgrade is checked separately by the semantic envelope.
        return False
    lifecycle = ledger.get("anchor_lifecycle")
    if not isinstance(lifecycle, Mapping):
        return False
    background = lifecycle.get("background_anchor")
    child = lifecycle.get("child_anchor")
    controlling = (
        background
        if isinstance(background, Mapping) and background.get("status") == "ACTIVE"
        else child
        if isinstance(child, Mapping) and child.get("status") == "ACTIVE"
        else None
    )
    if not isinstance(controlling, Mapping):
        return False
    defense = controlling.get("defense")
    return bool(
        controlling.get("direction") == expected_direction
        and (
            not isinstance(defense, Mapping)
            or defense.get("state") == "ACTIVE"
        )
    )


def _has_explicit_entry_risk_limit(
    *,
    ledger: Mapping[str, Any],
    entry_gate: Mapping[str, Any],
) -> bool:
    """Whether a numeric, predeclared risk ceiling can justify STOP_TOO_WIDE.

    The course fixes monetary risk and derives position size from the structural
    stop distance.  A model cannot invent a maximum number of points after the
    setup triggers.  Keep STOP_TOO_WIDE available only when the program/input
    actually supplied a numeric ceiling before the decision.
    """

    candidates = [
        entry_gate,
        entry_gate.get("risk_limit"),
        ledger.get("risk_policy"),
        ledger.get("trade_constitution"),
    ]
    limit_keys = {
        "max_stop_points",
        "max_risk_points",
        "max_risk_amount",
        "max_loss_amount",
    }
    return any(
        isinstance(candidate, Mapping)
        and any(
            key in candidate
            and isinstance(candidate.get(key), (int, float))
            and not isinstance(candidate.get(key), bool)
            and float(candidate[key]) > 0
            for key in limit_keys
        )
        for candidate in candidates
    )


def _validate_ai_hybrid_one_contract_grade_conflict(
    analysis: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    entry_gate: Mapping[str, Any],
) -> None:
    """Make the one-contract response to an AI-declared conflict repeatable.

    The course permits respecting a lower-grade controller with reduced size
    when grades conflict.  This replay trades exactly one TMF contract and
    cannot reduce below that unit, so its explicit execution policy is to
    consume the eligible signal as ``GRADE_CONFLICT``.  AI still owns the
    market interpretation; this guard only prevents the same response from
    declaring CONFLICT and ENTER simultaneously.
    """

    if not _ai_hybrid_policy(ledger) or entry_gate.get("status") != "ENTRY_ELIGIBLE":
        return
    reading = analysis.get("course_reading")
    action = analysis.get("action")
    if not isinstance(reading, Mapping) or not isinstance(action, Mapping):
        return
    if reading.get("grade_relation") != "CONFLICT":
        return
    if _q2_lower_grade_countermove_is_setup_context(
        analysis,
        ledger=ledger,
        entry_gate=entry_gate,
    ):
        assessment = analysis.get("ai_course_assessment")
        checks = (
            assessment.get("checks")
            if isinstance(assessment, Mapping)
            else None
        )
        if (
            isinstance(checks, Mapping)
            and (
                checks.get("grade_control") != "PASS"
                or checks.get("course_permission") != "PASS"
            )
        ):
            raise SemanticReplayError(
                "大級多方升級結構與防線仍有效時，小級空方擴張是多方Q2的交易背景；"
                "grade_control與course_permission必須為PASS，不得把較低級反向段當成同級接管。"
            )
        if (
            action.get("position_action") == "NONE"
            and action.get("entry_rejection_reason") == "GRADE_CONFLICT"
        ):
            raise SemanticReplayError(
                "大級多方升級結構與防線仍有效時，小級空方擴張是多方Q2的交易背景；"
                "不得只因較低級反向段或固定1口以GRADE_CONFLICT否決。"
            )
        return
    if (
        action.get("position_action") == "NONE"
        and action.get("entry_rejection_reason") == "GRADE_CONFLICT"
    ):
        return
    raise SemanticReplayError(
        "本回放固定1口且AI已判定大小級CONFLICT；本輪必須以GRADE_CONFLICT否決，"
        "不得同時ENTER。這是回放執行政策，不冒稱課程規則。"
    )


def _q2_lower_grade_countermove_is_setup_context(
    analysis: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    entry_gate: Mapping[str, Any],
) -> bool:
    """Recognize the lower-grade counter move that a bullish Q2 trades.

    Q2 is intentionally entered before the opposing lower-grade swing has
    completed a same-grade bullish takeover.  When AI has selected a still
    active bullish SMALL->LARGE promotion and its replacement defense remains
    intact, the smaller bearish expansion is the setup context rather than an
    independent one-contract grade veto.  A same/higher-grade bearish takeover,
    a broken parent defense, or a non-Q2 candidate remains a real conflict.
    """

    if entry_gate.get("direction") != "LONG":
        return False
    source = str(entry_gate.get("required_candidate_source") or "")
    strategy = str(entry_gate.get("required_entry_strategy") or "")
    if source not in {
        "FALSE_BREAK_RECLAIM",
        "Q2_FAILED_REVERSE_CANDIDATE",
        "Q2_SLOW_OUTER_EXPANSION_FAILURE",
    } or not strategy.startswith("Q2_"):
        return False
    reading = analysis.get("course_reading")
    if not isinstance(reading, Mapping) or reading.get("controlling_grade") != "LARGE":
        return False
    promotion = _active_promoted_structure(reading, ledger=ledger)
    if not (
        isinstance(promotion, Mapping)
        and promotion.get("direction") == "BULL"
        and promotion.get("from_level") == "SMALL"
        and promotion.get("to_level") == "LARGE"
    ):
        return False
    defense_ref = promotion.get("replacement_defense_pivot_id")
    defense_price = promotion.get("replacement_defense_price")
    if (
        not defense_ref
        or reading.get("large_defense_ref") != defense_ref
        or not isinstance(defense_price, (int, float))
        or isinstance(defense_price, bool)
    ):
        return False
    latest = ledger.get("latest_closed_k")
    latest_close = latest.get("close") if isinstance(latest, Mapping) else None
    return bool(
        isinstance(latest_close, (int, float))
        and not isinstance(latest_close, bool)
        and float(latest_close) > float(defense_price)
    )


def _opening_observation_memory(
    analysis: Mapping[str, Any],
    memory: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    ai_generated: bool = True,
) -> dict[str, Any]:
    """Keep early-session narratives in scenarios, never in executable memory.

    The model contract permits FORMING narrative plans. Before OR5, strip only
    wholly non-executable observation notes; an actual setup/order still fails
    the unchanged preparation guard. The saved raw output remains auditable.
    """
    session = ledger.get("monitoring_session")
    action = analysis.get("action", {})
    reading = analysis.get("course_reading", {})
    setups = memory.get("active_setups", [])
    if (isinstance(session, Mapping) and int(session.get("bar_count") or 0) < 5
        and analysis.get("message_type") in {"OBSERVATION", "UNCHANGED"}
        and reading.get("setup_stage") in {"NONE", "FORMING"}
        and action.get("position_action") == "NONE" and action.get("setup_key") is None
        and action.get("stop_price") is None and setups
        and all(s.get("stage") == "FORMING" and s.get("trigger_level") is None
                and s.get("trigger_operator") == "NONE" for s in setups)):
        if _ai_hybrid_policy(ledger) and ai_generated:
            raise SemanticReplayError(
                "OR5尚未完成時，AI_HYBRID不得保存形成中交易setup；"
                "驗證器不會代替AI刪除。"
            )
        return {**memory, "active_setups": []}
    return memory


def _validate_program_continuation_arm(
    analysis: Mapping[str, Any],
    memory: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    entry_gate: Mapping[str, Any] | None,
    preopen: bool = False,
) -> None:
    """Do not let a confirmed correction endpoint wait one extra swing.

    The deterministic ledger owns the trigger, stop and resulting entry.  The
    analyzer must surface the candidate as an ARMED setup and may only explain
    the program decision.
    """

    if preopen or not _program_only_policy(ledger):
        # The completed prior session is context only.  Its objective
        # continuation candidate may be described in the snapshot, but it must
        # never force an executable PREPARATION card or carry an order into the
        # next monitoring segment.
        return
    trade_levels = ledger.get("trade_levels")
    candidate = (
        trade_levels.get("continuation_arm_candidate")
        if isinstance(trade_levels, Mapping)
        else None
    )
    retired_candidate = (
        trade_levels.get("retired_continuation_candidate")
        if isinstance(trade_levels, Mapping)
        else None
    )
    gate = entry_gate if isinstance(entry_gate, Mapping) else {}
    reading = analysis.get("course_reading")
    action = analysis.get("action")
    if (
        not isinstance(candidate, Mapping)
        and isinstance(retired_candidate, Mapping)
        and position.get("status") == "FLAT"
        and not isinstance(position.get("pending_entry"), Mapping)
        and gate.get("status") != "ENTRY_ELIGIBLE"
        and isinstance(reading, Mapping)
        and reading.get("setup_stage") == "ARMED"
        and "修正後複製" in str(reading.get("main_strategy") or "")
    ):
        # Once the program-owned correction setup expires, the analyzer may
        # not keep the same correction/stop origin and merely move the trigger
        # or rename its setup.  A later objective correction sequence will
        # produce a new continuation_arm_candidate with a new structural key.
        raise SemanticReplayError(
            "程式化修正後複製候選已過期；新的確定性修正端點出現前，不得換setup_key或移動觸發線重新武裝。"
        )
    if (
        not isinstance(candidate, Mapping)
        or position.get("status") != "FLAT"
        or isinstance(position.get("pending_entry"), Mapping)
        or gate.get("status") == "ENTRY_ELIGIBLE"
        or not isinstance(reading, Mapping)
        or not isinstance(action, Mapping)
        or reading.get("structure_event_ref") is not None
    ):
        return

    setup_key = candidate.get("setup_key")
    setups = memory.get("active_setups")
    setup_items = setups if isinstance(setups, list) else []
    matching = next(
        (
            item
            for item in setup_items
            if isinstance(item, Mapping)
            if item.get("setup_key") == setup_key
        ),
        None,
    )
    if not isinstance(matching, Mapping):
        raise SemanticReplayError(
            "程式已確認反向修正端點與同向工作段；必須把trade_levels.continuation_arm_candidate保存為ARMED，不得再多等一個循環。"
        )
    expected = {
        "direction": candidate.get("direction"),
        "stage": "ARMED",
        "trigger_operator": candidate.get("trigger_operator"),
        "trigger_level": candidate.get("trigger_level"),
        "valid_bars": candidate.get("valid_bars"),
    }
    if any(matching.get(key) != value for key, value in expected.items()):
        raise SemanticReplayError(
            "程式化修正後複製setup必須逐字沿用候選方向、ARMED、觸發運算、價位與有效根數。"
        )
    if (
        analysis.get("message_type") != "PREPARATION"
        or reading.get("setup_stage") != "ARMED"
        or action.get("setup_key") != setup_key
    ):
        raise SemanticReplayError("程式化修正後複製候選必須輸出PREPARATION待觸發卡。")


def _canonicalize_program_action_envelope(
    analysis: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    entry_gate: Mapping[str, Any] | None,
    preopen: bool,
    ai_generated: bool = False,
) -> None:
    """Overlay every program-owned executable action before semantic checks.

    This deliberately runs inside ``_analysis_v3``. Applying the overlay only
    after that validator would still make a correct program stop, exit or entry
    depend on the model volunteering the same action first.
    """

    policy = ledger.get("program_trade_policy")
    policy_mode = (
        str(policy.get("actionable_setups") or "")
        if isinstance(policy, Mapping)
        else ""
    )
    if preopen or policy_mode not in {
        "PROGRAM_ONLY",
        "PROGRAM_CANDIDATES_AI_DECISION",
    }:
        return
    action = analysis.get("action")
    reading = analysis.get("course_reading")
    if not isinstance(action, dict) or not isinstance(reading, dict):
        return

    stop_audit = ledger.get("protective_stop_audit")
    behavior_exit = ledger.get("program_behavior_exit_audit")
    position_side = str(position.get("status") or "")
    if (
        position_side in {"LONG", "SHORT"}
        and isinstance(stop_audit, Mapping)
        and stop_audit.get("status") == "TRIGGERED"
    ):
        stop = stop_audit.get("stop_price")
        if not isinstance(stop, (int, float)) or isinstance(stop, bool):
            return
        expectation = ledger.get("program_reentry_expectation")
        reentry_status = (
            str(expectation.get("status_after_action"))
            if isinstance(expectation, Mapping)
            else "NOT_APPLICABLE"
        )
        # A filled protective stop is an execution fact, not a fresh model
        # decision.  Canonicalize the card even when the analyzer copied the
        # current market direction or an old stop price.  The raw analyzer
        # payload is retained by the runner for comparison/audit.
        analysis.update(
            {
                "original_decision": "NOTIFY",
                "message_type": "STOP",
                "message_direction": "BULL" if position_side == "LONG" else "BEAR",
                "notification_reason": "程式逐根檢查已確認保護停損成交。",
            }
        )
        reading["setup_stage"] = "INVALIDATED"
        action.update(
            {
                "position_action": "STOP",
                "direction": "NONE",
                "entry_role": "NOT_APPLICABLE",
                "setup_key": position.get("active_setup_key"),
                "trigger": "已收盤1分K的高低價觸及程式鎖定保護停損。",
                "entry": "持倉已依保護停損退出。",
                "stop_price": float(stop),
                "expected_behavior": "停損後等待新結構；不得直接追認舊訊號。",
                "max_wait_bars": None,
                "management": "停損成交、再進資格與setup生命週期均由程式記錄。",
                "reentry_status": reentry_status,
                "entry_rejection_reason": "NONE",
            }
        )
        return

    if (
        position_side in {"LONG", "SHORT"}
        and isinstance(behavior_exit, Mapping)
        and behavior_exit.get("status") in {"PENDING_FILL", "TRIGGERED"}
    ):
        # The program has already made the time/behaviour exit executable.
        # Rendering it must not depend on the model repeating every mechanical
        # field exactly.
        analysis.update(
            {
                "original_decision": "NOTIFY",
                "message_type": "EXIT",
                "message_direction": "BULL" if position_side == "LONG" else "BEAR",
                "notification_reason": "程式確認事前應有行為未完成，依時間／動機失效退出。",
            }
        )
        reading["setup_stage"] = "INVALIDATED"
        action.update(
            {
                "position_action": "EXIT",
                "direction": "NONE",
                "entry_role": "NOT_APPLICABLE",
                "setup_key": position.get("active_setup_key"),
                "trigger": "事前行為窗屆滿仍未完成程式檢查點。",
                "entry": "退出訊號於收盤確認，成交使用下一根1分K開盤。",
                "stop_price": None,
                "expected_behavior": "原進場動機已消失；等待新的獨立setup。",
                "max_wait_bars": None,
                "management": "程式已鎖定失效收盤與下一根開盤成交，不由AI延後。",
                "reentry_status": "NOT_APPLICABLE",
                "entry_rejection_reason": "NONE",
            }
        )
        return

    # Stops and expired behavior exits are execution facts in both program-only
    # and AI-hybrid modes.  Only automatic entry acceptance remains exclusive
    # to PROGRAM_ONLY; hybrid entry quality is still decided by the model.
    if policy_mode != "PROGRAM_ONLY":
        return

    gate = entry_gate if isinstance(entry_gate, Mapping) else {}
    if not (
        gate.get("status") == "ENTRY_ELIGIBLE"
        and gate.get("decision_authority") == "PROGRAM"
        and gate.get("required_position_action") == "ENTER"
    ):
        return
    direction = str(gate.get("direction") or "")
    stop = gate.get("required_stop_price")
    if direction not in {"LONG", "SHORT"} or not isinstance(stop, (int, float)) or isinstance(stop, bool):
        return
    setup_key = str(gate.get("setup_key") or "")
    entry_role = str(gate.get("entry_role") or "INITIAL")
    trigger_operator = str(gate.get("trigger_operator") or "NONE")
    trigger_level = gate.get("trigger_level")
    required_obstacles = [
        {
            "price": float(item["price"]),
            "role": str(item.get("role") or "CHECKPOINT"),
            "label": "程式觸發線" if item.get("role") == "TRIGGER" else "程式檢查點",
            "reaction": "依已收盤K是否守住或有效通過管理。",
        }
        for item in gate.get("required_behavior_obstacles", [])
        if isinstance(item, Mapping)
        and isinstance(item.get("price"), (int, float))
        and not isinstance(item.get("price"), bool)
    ]
    analysis.update(
        {
            "original_decision": "NOTIFY",
            "message_type": "PREPARATION",
            "message_direction": "BULL" if direction == "LONG" else "BEAR",
            "notification_reason": "程式已確認進場條件，排入下一根可交易1分K開盤。",
        }
    )
    reading["setup_stage"] = "ENTRY_ELIGIBLE"
    reading["main_strategy"] = str(
        gate.get("setup_name") or reading.get("main_strategy") or "程式化結構交易"
    )
    action.update(
        {
            "position_action": "ENTER",
            "direction": direction,
            "entry_role": entry_role,
            "setup_key": setup_key,
            "trigger": _program_trigger_text(trigger_operator, trigger_level),
            "entry": "程式已確認收盤觸發；模擬成交排在下一根可交易1分K開盤。",
            "structural_stop": f"程式鎖定完整結構外停損{float(stop):,.1f}點。",
            "stop_price": float(stop),
            "obstacles": required_obstacles,
            "expected_behavior": "進場後須在程式設定的行為窗內守住觸發線並出現有利推進。",
            "max_wait_bars": gate.get("required_behavior_max_wait_bars"),
            "management": "進場、停損與行為窗由程式執行；AI只說明盤勢。",
            "reentry_status": "USED" if entry_role == "REENTRY" else "NOT_APPLICABLE",
            "entry_rejection_reason": "NONE",
        }
    )


def _canonicalize_ai_hybrid_discretionary_exit_card(
    analysis: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    preopen: bool,
    ai_generated: bool = False,
) -> None:
    """Preserve an explicit AI exit while repairing only its event-card label.

    In AI_HYBRID the model owns the course-level decision to actively exit an
    open position.  If it clearly emits ``position_action=EXIT`` but copies the
    ordinary held-position ``MANAGEMENT`` card, retrying the whole analysis can
    stochastically change EXIT into HOLD.  The action is the substantive choice;
    canonicalizing its presentation envelope to EXIT keeps that choice stable.
    """

    if preopen or not ai_generated or not _ai_hybrid_policy(ledger):
        return
    if position.get("status") not in {"LONG", "SHORT"}:
        return
    action = analysis.get("action")
    if not isinstance(action, Mapping) or action.get("position_action") != "EXIT":
        return
    analysis["original_decision"] = "NOTIFY"
    analysis["message_type"] = "EXIT"


def _canonicalize_program_setup_and_entry(
    analysis: dict[str, Any],
    memory: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    entry_gate: Mapping[str, Any] | None,
    preopen: bool,
) -> None:
    """Overlay the executable envelope from deterministic program evidence."""

    policy = ledger.get("program_trade_policy")
    if (
        preopen
        or not isinstance(policy, Mapping)
        or policy.get("actionable_setups") != "PROGRAM_ONLY"
    ):
        return
    levels = ledger.get("trade_levels")
    candidate = (
        levels.get("continuation_arm_candidate")
        if isinstance(levels, Mapping)
        else None
    )
    gate = entry_gate if isinstance(entry_gate, Mapping) else {}
    action = analysis.get("action")
    reading = analysis.get("course_reading")
    if not isinstance(action, dict) or not isinstance(reading, dict):
        return

    selected: Mapping[str, Any] | None = None
    stage: str | None = None
    if gate.get("status") == "ENTRY_ELIGIBLE" and gate.get("decision_authority") == "PROGRAM":
        selected = gate
        stage = "ENTRY_ELIGIBLE"
    elif (
        isinstance(candidate, Mapping)
        and position.get("status") == "FLAT"
        and not isinstance(position.get("pending_entry"), Mapping)
    ):
        selected = candidate
        stage = "ARMED"
    if selected is None or stage is None:
        return

    setup_key = str(selected.get("setup_key") or "")
    direction = str(selected.get("direction") or "")
    if not setup_key or direction not in {"LONG", "SHORT"}:
        return
    setup_name = str(selected.get("setup_name") or "程式化結構交易")
    trigger_operator = str(selected.get("trigger_operator") or "NONE")
    trigger_level = selected.get("trigger_level")
    valid_bars = selected.get("valid_bars")
    entry_role = str(selected.get("entry_role") or "INITIAL")
    reentry_status = (
        "USED"
        if entry_role == "REENTRY" and stage == "ENTRY_ELIGIBLE"
        else "AVAILABLE"
        if entry_role == "REENTRY"
        else "NOT_APPLICABLE"
    )
    stop = (
        selected.get("required_stop_price")
        if stage == "ENTRY_ELIGIBLE"
        else selected.get("stop_price")
    )
    setup = {
        "setup_key": setup_key,
        "name": setup_name,
        "direction": direction,
        "stage": stage,
        "trigger": _program_trigger_text(trigger_operator, trigger_level),
        "stop": (
            f"程式鎖定完整結構外停損{float(stop):,.1f}點。"
            if isinstance(stop, (int, float)) and not isinstance(stop, bool)
            else "等待程式取得完整結構停損。"
        ),
        "reentry_status": reentry_status,
        "trigger_level": trigger_level,
        "trigger_operator": trigger_operator,
        "valid_bars": valid_bars,
    }
    prior = [
        item
        for item in memory.get("active_setups", [])
        if isinstance(item, Mapping) and str(item.get("setup_key") or "") != setup_key
    ]
    memory["active_setups"] = [setup, *prior][:3]

    reading["setup_stage"] = stage
    reading["main_strategy"] = setup_name
    analysis["message_direction"] = "BULL" if direction == "LONG" else "BEAR"
    analysis["original_decision"] = "NOTIFY"
    # A newly visible structure card may remain the public event title while
    # the setup is armed in memory.  ENTRY_ELIGIBLE, however, is always the
    # time-sensitive preparation/entry decision card.
    if stage == "ENTRY_ELIGIBLE" or reading.get("structure_event_ref") is None:
        analysis["message_type"] = "PREPARATION"

    action.update(
        {
            "setup_key": setup_key,
            "trigger": _program_trigger_text(trigger_operator, trigger_level),
            "structural_stop": setup["stop"],
            "reentry_status": reentry_status,
            "entry_rejection_reason": "NONE",
        }
    )
    if stage == "ENTRY_ELIGIBLE":
        required_obstacles = [
            {
                "price": float(item["price"]),
                "role": str(item.get("role") or "CHECKPOINT"),
                "label": "程式觸發線" if item.get("role") == "TRIGGER" else "程式檢查點",
                "reaction": "依已收盤K是否守住或有效通過管理。",
            }
            for item in selected.get("required_behavior_obstacles", [])
            if isinstance(item, Mapping)
            and isinstance(item.get("price"), (int, float))
            and not isinstance(item.get("price"), bool)
        ]
        action.update(
            {
                "position_action": "ENTER",
                "direction": direction,
                "entry_role": entry_role,
                "entry": "程式已確認收盤觸發；模擬成交排在下一根可交易1分K開盤。",
                "stop_price": float(stop),
                "obstacles": required_obstacles,
                "expected_behavior": "進場後須在程式設定的行為窗內守住觸發線並出現有利推進。",
                "max_wait_bars": selected.get("required_behavior_max_wait_bars"),
                "management": "進場、停損與行為窗由程式執行；AI只說明盤勢。",
            }
        )
    else:
        action.update(
            {
                "position_action": "NONE",
                "direction": "NONE",
                "entry_role": "NOT_APPLICABLE",
                "entry": "尚未觸發；等待程式確認已收盤K後才排入下一根開盤。",
                "stop_price": None,
            }
        )


def _program_trigger_text(operator: str, level: Any) -> str:
    if not isinstance(level, (int, float)) or isinstance(level, bool):
        return "等待程式化收盤觸發。"
    verb = "收盤站上" if operator in {"CLOSE_ABOVE", "RECLAIM_ABOVE"} else "收盤跌破"
    return f"{verb}{float(level):,.0f}點時，由程式建立進場資格。"


def _program_setup_invalidation_event(
    *,
    message_type: Any,
    reading: Mapping[str, Any],
    action: Any,
    evidence_events: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Recognize only the current program-owned setup terminal event.

    ``INVALIDATION`` also represents structural invalidation elsewhere in the
    schema.  A pre-entry setup cancellation has no structure_event_ref, so it
    must be matched to the newest explicit PROGRAM_SETUP_INVALIDATED event by
    setup key.  This permits an auditable terminal notice without allowing a
    retired setup to become actionable again.
    """

    if (
        message_type != "INVALIDATION"
        or not isinstance(action, Mapping)
        or action.get("position_action") != "NONE"
        or reading.get("setup_stage") != "INVALIDATED"
        or reading.get("structure_event_ref") is not None
    ):
        return None
    candidates = [
        item
        for item in evidence_events
        if isinstance(item, Mapping)
        and item.get("event_type") == "PROGRAM_SETUP_INVALIDATED"
        and item.get("setup_key")
    ]
    if not candidates:
        return None
    latest = max(
        candidates,
        key=lambda item: str(item.get("recorded_at") or item.get("event_time") or ""),
    )
    action_key = action.get("setup_key")
    if action_key not in {None, ""} and str(action_key) != str(latest.get("setup_key") or ""):
        return None
    return latest


def _canonicalize_ai_hybrid_unbacked_event_card(
    result: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    evidence_events: list[Mapping[str, Any]],
    preopen: bool,
    ai_generated: bool = True,
) -> None:
    """Reserve structure event cards for a new auditable program event.

    The AI may legitimately describe an interpretive downgrade without a new
    deterministic structure event.  That is an observation, not a formal
    GRADE_UPGRADE/GRADE_DOWNGRADE/STRUCTURE_INVALIDATED event card.  Normalize
    only the presentation class; keep the model's anchors, quadrants,
    scenarios, action and explanatory text unchanged.
    """

    if preopen or not _ai_hybrid_policy(ledger):
        return
    message_type = result.get("message_type")
    if message_type not in {
        "STRUCTURE_UPGRADE",
        "STRUCTURE_DOWNGRADE",
        "INVALIDATION",
    }:
        return
    reading = result.get("course_reading")
    action = result.get("action")
    if not isinstance(reading, Mapping) or reading.get("structure_event_ref") is not None:
        return
    if isinstance(
        _program_setup_invalidation_event(
            message_type=message_type,
            reading=reading,
            action=action,
            evidence_events=evidence_events,
        ),
        Mapping,
    ):
        return
    if ai_generated:
        raise SemanticReplayError(
            "AI_HYBRID的結構事件卡必須引用本輪可稽核事件；"
            "驗證器不會代替AI改成一般觀察卡。"
        )
    if (
        position.get("status") in {"LONG", "SHORT"}
        and isinstance(action, Mapping)
        and action.get("position_action") == "NONE"
    ):
        result["message_type"] = "MANAGEMENT"
    else:
        result["message_type"] = "OBSERVATION"
    result["original_decision"] = "NOTIFY"


def _canonicalize_noop_action_labels(action: Any) -> None:
    """Remove duplicated order-side labels from a non-order action.

    Position direction and setup ownership remain in the authoritative
    position/memory objects.  A NONE action cannot itself be LONG/SHORT or an
    INITIAL/REENTRY order, so these two labels are safe deterministic fields.
    """

    if isinstance(action, dict) and action.get("position_action") == "NONE":
        action["direction"] = "NONE"
        action["entry_role"] = "NOT_APPLICABLE"


def _canonicalize_flat_noop_action(
    action: Any, *, position: Mapping[str, Any]
) -> None:
    """Remove an executable stop price from a flat no-op action."""

    if (
        isinstance(action, dict)
        and position.get("status") == "FLAT"
        and action.get("position_action") == "NONE"
    ):
        # A model may repeat the hypothetical structural stop here as well as
        # in structural_stop.  Keep the narrative, but make the no-op envelope
        # mechanically non-executable.
        action["stop_price"] = None


def _ai_armable_program_candidates(levels: Any) -> list[Mapping[str, Any]]:
    """Return the v34 hard-fact candidate inventory with a legacy fallback.

    The deterministic engine may expose several Q2/Q4 candidates to the
    hybrid analyzer, while an older saved run exposes only the singular
    ``continuation_arm_candidate``.  ``ai_armable_setup_keys`` is the hard-gate
    allow-list; interpretive program audits must never add a setup to it.
    """

    if not isinstance(levels, Mapping):
        return []
    raw_inventory = levels.get("ai_candidate_inventory")
    armable = levels.get("ai_armable_setup_keys")
    armable_keys = (
        {str(item) for item in armable if str(item)}
        if isinstance(armable, list)
        else None
    )
    candidates: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw_inventory, list):
        for item in raw_inventory:
            if not isinstance(item, Mapping):
                continue
            setup_key = str(item.get("setup_key") or "")
            if not setup_key or setup_key in seen:
                continue
            if armable_keys is not None and setup_key not in armable_keys:
                continue
            candidates.append(item)
            seen.add(setup_key)
    legacy = levels.get("continuation_arm_candidate")
    if isinstance(legacy, Mapping):
        setup_key = str(legacy.get("setup_key") or "")
        if setup_key and setup_key not in seen and (
            armable_keys is None or setup_key in armable_keys
        ):
            candidates.append(legacy)
    return candidates


def _validate_program_owned_actionable_setups(
    analysis: Mapping[str, Any],
    memory: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    entry_gate: Mapping[str, Any] | None,
    preopen: bool,
) -> None:
    """Reject executable setup identities invented only by model prose."""

    policy = ledger.get("program_trade_policy")
    if preopen or not isinstance(policy, Mapping):
        return
    policy_mode = str(policy.get("actionable_setups") or "")
    if policy_mode not in {"PROGRAM_ONLY", "PROGRAM_CANDIDATES_AI_DECISION"}:
        return
    levels = ledger.get("trade_levels")
    candidates = _ai_armable_program_candidates(levels)
    gate = entry_gate if isinstance(entry_gate, Mapping) else {}
    allowed = {
        str(value)
        for value in (
            *(item.get("setup_key") for item in candidates),
            gate.get("setup_key"),
            position.get("active_setup_key"),
        )
        if value
    }
    actionable = {
        "ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED",
    }
    invented = [
        str(item.get("setup_key"))
        for item in memory.get("active_setups", [])
        if isinstance(item, Mapping)
        and item.get("stage") in actionable
        and str(item.get("setup_key") or "") not in allowed
    ]
    if invented:
        raise SemanticReplayError(
            "可執行setup只能來自程式化候選、程式進場閘門或既有持倉；"
            "AI可描述其他戰法，但不得自行建立可成交setup。"
        )
    selected_actionable = [
        item
        for item in memory.get("active_setups", [])
        if isinstance(item, Mapping)
        and item.get("stage") in actionable
        and str(item.get("setup_key") or "") in allowed
    ]
    if (
        policy_mode == "PROGRAM_CANDIDATES_AI_DECISION"
        and position.get("status") == "FLAT"
        and len(selected_actionable) > 1
    ):
        raise SemanticReplayError(
            "AI_HYBRID每輪可以比較多個Q2／Q4候選，但空手時最多只能選一個可執行setup。"
        )
    action = analysis.get("action")
    if (
        isinstance(action, Mapping)
        and action.get("position_action") == "ENTER"
        and gate.get("decision_authority") not in {"PROGRAM", "AI_HYBRID"}
    ):
        raise SemanticReplayError("沒有程式化ENTRY_ELIGIBLE決策時，AI不得自行進場。")
    if policy_mode != "PROGRAM_CANDIDATES_AI_DECISION":
        return
    # In hybrid mode the analyzer owns whether a candidate is worth arming,
    # but it cannot change the candidate's identity, direction or trigger.
    candidate_by_key = {
        str(item.get("setup_key")): item
        for item in (*candidates, gate)
        if isinstance(item, Mapping) and item.get("setup_key")
    }
    held_setup_key = (
        str(position.get("active_setup_key") or "")
        if position.get("status") in {"LONG", "SHORT"}
        else ""
    )
    for item in memory.get("active_setups", []):
        if not isinstance(item, Mapping) or item.get("stage") not in actionable:
            continue
        expected = candidate_by_key.get(str(item.get("setup_key") or ""))
        if not isinstance(expected, Mapping):
            continue
        is_held_setup = bool(
            held_setup_key
            and str(item.get("setup_key") or "") == held_setup_key
        )
        if is_held_setup and item.get("stage") not in {
            "AGGRESSIVE_CONFIRMED",
            "CONSERVATIVE_CONFIRMED",
        }:
            raise SemanticReplayError(
                "已成交持倉的setup必須由ENTRY_ELIGIBLE推進為積極或保守確認，"
                "不得退回ARMED或繼續標示等待成交。"
            )
        expected_stage = (
            "ENTRY_ELIGIBLE"
            if gate.get("status") == "ENTRY_ELIGIBLE"
            and str(gate.get("setup_key") or "") == str(item.get("setup_key") or "")
            else "ARMED"
        )
        for key, expected_value in (
            ("direction", expected.get("direction")),
            ("trigger_operator", expected.get("trigger_operator")),
            ("trigger_level", expected.get("trigger_level")),
            ("valid_bars", expected.get("valid_bars")),
        ):
            if item.get(key) != expected_value:
                raise SemanticReplayError(
                    "AI_HYBRID可決定是否採用候選，但setup方向、階段、觸發價與有效根數必須沿用程式證據。"
                )
        if not is_held_setup and item.get("stage") != expected_stage:
            raise SemanticReplayError(
                "AI_HYBRID可決定是否採用候選，但setup方向、階段、觸發價與有效根數必須沿用程式證據。"
            )


def _canonicalize_program_owned_actionable_setups(
    analysis: dict[str, Any],
    memory: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    entry_gate: Mapping[str, Any] | None,
    preopen: bool,
    ai_generated: bool = True,
) -> None:
    """Normalize order state while preserving non-executable course analysis.

    Rejecting a whole market update because a model copied an extra actionable
    setup makes availability model-dependent.  Under PROGRAM_ONLY the safe
    behavior is deterministic: discard unauthorized order state.  If the model
    tried to make that setup the current action, downgrade only the executable
    envelope to an observation; its course narrative remains auditable.  In
    AI_HYBRID, a ledger-listed but non-selected diagnostic candidate remains a
    FORMING scenario, never a second executable order.  Truly model-invented
    setup keys are left intact here so the validator rejects them explicitly.
    """

    policy = ledger.get("program_trade_policy")
    if preopen or not isinstance(policy, Mapping):
        return
    policy_mode = str(policy.get("actionable_setups") or "")
    if policy_mode not in {"PROGRAM_ONLY", "PROGRAM_CANDIDATES_AI_DECISION"}:
        return
    levels = ledger.get("trade_levels")
    candidates = _ai_armable_program_candidates(levels)
    gate = entry_gate if isinstance(entry_gate, Mapping) else {}
    allowed = {
        str(value)
        for value in (
            *(item.get("setup_key") for item in candidates),
            gate.get("setup_key"),
            position.get("active_setup_key"),
        )
        if value
    }
    actionable = {
        "ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED",
    }
    candidate_catalog = {
        str(value.get("setup_key"))
        for value in (levels.values() if isinstance(levels, Mapping) else [])
        if isinstance(value, Mapping)
        and value.get("setup_key")
        and value.get("direction") in {"LONG", "SHORT"}
    }
    candidate_catalog.update(
        str(value.get("setup_key"))
        for value in candidates
        if value.get("setup_key")
    )
    if policy_mode == "PROGRAM_CANDIDATES_AI_DECISION":
        demoted: set[str] = set()
        normalized_hybrid: list[Any] = []
        for item in memory.get("active_setups", []):
            if not isinstance(item, Mapping):
                normalized_hybrid.append(item)
                continue
            setup_key = str(item.get("setup_key") or "")
            if (
                item.get("stage") in actionable
                and setup_key not in allowed
                and setup_key in candidate_catalog
            ):
                if ai_generated:
                    raise SemanticReplayError(
                        "AI_HYBRID只能把本輪程式允許的候選標為可執行階段；"
                        "驗證器不會將其他候選靜默降級。"
                    )
                normalized_item = dict(item)
                normalized_item["stage"] = "FORMING"
                normalized_hybrid.append(normalized_item)
                demoted.add(setup_key)
            else:
                normalized_hybrid.append(item)
        memory["active_setups"] = normalized_hybrid
        action = analysis.get("action")
        reading = analysis.get("course_reading")
        if not isinstance(action, dict) or str(action.get("setup_key") or "") not in demoted:
            return
        if action.get("position_action") in {"STOP", "EXIT"}:
            raise SemanticReplayError("持倉退出引用了非程式持倉setup，無法安全正規化。")
        action.update(
            {
                "position_action": "NONE",
                "direction": "NONE",
                "entry_role": "NOT_APPLICABLE",
                "setup_key": None,
                "stop_price": None,
                "reentry_status": "NOT_APPLICABLE",
                "entry_rejection_reason": "NONE",
            }
        )
        if isinstance(reading, dict):
            reading["setup_stage"] = "FORMING"
        analysis["message_type"] = "OBSERVATION"
        analysis["original_decision"] = "NOTIFY"
        return

    removed: set[str] = set()
    normalized: list[Any] = []
    for item in memory.get("active_setups", []):
        if (
            isinstance(item, Mapping)
            and item.get("stage") in actionable
            and str(item.get("setup_key") or "") not in allowed
        ):
            removed.add(str(item.get("setup_key") or ""))
            continue
        normalized.append(item)
    if not removed:
        return
    memory["active_setups"] = normalized
    action = analysis.get("action")
    reading = analysis.get("course_reading")
    if not isinstance(action, dict) or str(action.get("setup_key") or "") not in removed:
        return
    if action.get("position_action") in {"STOP", "EXIT"}:
        # A real position owner is part of `allowed`, so this branch is merely
        # defensive and must never erase an executable protective action.
        raise SemanticReplayError("持倉退出引用了非程式持倉setup，無法安全正規化。")
    action.update(
        {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": None,
            "stop_price": None,
            "reentry_status": "NOT_APPLICABLE",
            "entry_rejection_reason": "NONE",
        }
    )
    if isinstance(reading, dict):
        reading["setup_stage"] = "FORMING"
    analysis["message_type"] = "OBSERVATION"
    analysis["original_decision"] = "NOTIFY"


def _validate_preparation_contract(
    analysis: Mapping[str, Any],
    memory: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> None:
    """Keep an observation from being mislabeled as a trade preparation.

    PREPARATION is a public, actionable state.  It must be backed by a setup
    carried in memory at a preparation-capable stage.  A directional swing or
    reverse-anchor candidate by itself is only market observation.
    """

    reading = analysis.get("course_reading")
    action = analysis.get("action")
    reading = reading if isinstance(reading, Mapping) else {}
    action = action if isinstance(action, Mapping) else {}
    stage = str(reading.get("setup_stage") or "NONE")
    allowed_stages = {
        "ARMED",
        "ENTRY_ELIGIBLE",
        "AGGRESSIVE_CONFIRMED",
        "CONSERVATIVE_CONFIRMED",
    }
    monitoring = ledger.get("monitoring_session")
    if isinstance(monitoring, Mapping) and int(monitoring.get("bar_count") or 0) < 5:
        active_setups = memory.get("active_setups")
        active_setups = active_setups if isinstance(active_setups, list) else []
        if (
            analysis.get("message_type") == "PREPARATION"
            or stage in allowed_stages
            or bool(active_setups)
        ):
            raise SemanticReplayError("新監控時段OR5尚未收完，只能累積開盤證據，且不得沿用前一時段setup。")
    if analysis.get("message_type") != "PREPARATION":
        return
    if stage not in allowed_stages:
        raise SemanticReplayError("PREPARATION必須有已準備或已確認的setup；形成中或NONE只能輸出OBSERVATION。")

    setup_key = action.get("setup_key")
    active_setups = memory.get("active_setups")
    active_setups = active_setups if isinstance(active_setups, list) else []
    matching = next(
        (
            item
            for item in active_setups
            if isinstance(item, Mapping) and item.get("setup_key") == setup_key
        ),
        None,
    )
    if not setup_key or not isinstance(matching, Mapping):
        raise SemanticReplayError("PREPARATION必須引用memory.active_setups中的同一個setup_key。")
    if matching.get("stage") != stage:
        raise SemanticReplayError("PREPARATION的setup_stage必須與memory.active_setups一致。")


def _validate_countertrend_preparation_maturity(
    analysis: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> None:
    """Keep a left-side warning from becoming an executable preparation.

    The one-contract course policy treats a reversal against the highest active
    anchor as Type1/left-side while that anchor's defense remains intact.  A
    false-break event can strengthen the warning, but it cannot by itself arm
    the opposite trade.  Prefer the active background anchor when one exists;
    this intentionally preserves a same-background-direction re-entry even if
    a smaller child anchor temporarily points the other way.
    """

    reading = analysis.get("course_reading")
    if not isinstance(reading, Mapping):
        return
    message_type = analysis.get("message_type")
    if message_type is not None and message_type != "PREPARATION":
        # Once a position exists, an opposite anchor or defense is a
        # management/exit input.  It must not retroactively make the original
        # entry illegal.  This guard is only for arming a new trade.
        return
    stage = str(reading.get("setup_stage") or "NONE")
    if stage not in {
        "ARMED",
        "ENTRY_ELIGIBLE",
        "AGGRESSIVE_CONFIRMED",
        "CONSERVATIVE_CONFIRMED",
    }:
        return

    setup_direction = {
        "BULL": "BULL",
        "BEAR": "BEAR",
    }.get(str(analysis.get("message_direction")))
    if setup_direction is None:
        return

    if _is_countertrend_to_highest_active_anchor(setup_direction, ledger=ledger):
        raise SemanticReplayError(
            "最高有效錨及其防線仍與本setup反向；依一口單Type1／左側規則只能觀察或條件式，"
            "不得進入ARMED或ENTRY_ELIGIBLE。"
        )


def _is_countertrend_to_highest_active_anchor(
    direction: str,
    *,
    ledger: Mapping[str, Any],
) -> bool:
    """Whether `direction` opposes the highest active anchor with intact defense."""

    lifecycle = ledger.get("anchor_lifecycle")
    if not isinstance(lifecycle, Mapping):
        return False
    background = lifecycle.get("background_anchor")
    child = lifecycle.get("child_anchor")
    controlling_anchor = None
    if isinstance(background, Mapping) and background.get("status") == "ACTIVE":
        controlling_anchor = background
    elif isinstance(child, Mapping) and child.get("status") == "ACTIVE":
        controlling_anchor = child
    if not isinstance(controlling_anchor, Mapping):
        return False
    defense = controlling_anchor.get("defense")
    return bool(
        controlling_anchor.get("direction") != direction
        and isinstance(defense, Mapping)
        and defense.get("state") == "ACTIVE"
    )


def _consume_denied_entry_setup(
    analysis: Mapping[str, Any],
    memory: dict[str, Any],
    *,
    entry_gate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Consume a program-qualified trigger after the analyzer rejects entry.

    The current event card remains an auditable ENTRY_ELIGIBLE decision, but
    the saved continuation memory must not offer the same historical crossing
    again.  A new trade therefore requires a newly armed same-grade structure.
    """

    gate = entry_gate if isinstance(entry_gate, Mapping) else {}
    action = analysis.get("action")
    reading = analysis.get("course_reading")
    if (
        gate.get("status") != "ENTRY_ELIGIBLE"
        or not isinstance(action, Mapping)
        or not isinstance(reading, Mapping)
        or reading.get("setup_stage") != "ENTRY_ELIGIBLE"
        or action.get("position_action") != "NONE"
        or action.get("entry_rejection_reason") not in ENTRY_DENIAL_REASONS
    ):
        return memory

    setup_key = gate.get("setup_key")
    reason = str(action.get("entry_rejection_reason"))
    consumed = False
    setups: list[dict[str, Any]] = []
    for item in memory.get("active_setups", []):
        normalized = dict(item)
        if normalized.get("setup_key") == setup_key:
            normalized["stage"] = "NO_CHASE"
            normalized["trigger"] = (
                f"本次進場資格已以{reason}否決並消費；"
                "不得重複使用舊觸發，須等待新的同級結構。"
            )
            consumed = True
        setups.append(normalized)
    if not consumed:
        raise SemanticReplayError("被否決的ENTRY_ELIGIBLE事件找不到對應setup，無法消費舊觸發。")
    return {**memory, "active_setups": setups}


def initial_position_state(*, as_of: str, version: int = 1) -> dict[str, Any]:
    if version == 2:
        return {
            "version": 2,
            "as_of": as_of,
            "status": "FLAT",
            "entry_time": None,
            "entry_price": None,
            "stop_price": None,
            "direction": None,
            "active_setup_key": None,
            "last_stop_time": None,
            "reentry_count": 0,
            "last_action": "NONE",
            "last_reason": "尚未建立模擬持倉。",
            "pending_entry": None,
            "pending_exit": None,
        }
    return {
        "version": 1,
        "as_of": as_of,
        "status": "FLAT",
        "entry_time": None,
        "entry_price": None,
        "stop_price": None,
        "direction": None,
        "last_action": "NONE",
        "last_reason": "尚未建立模擬持倉。",
    }


def preview_position_transition(
    position: Mapping[str, Any],
    analysis: Mapping[str, Any],
    *,
    as_of: str,
    latest_close: float,
    preopen: bool,
    protective_stop_audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = dict(position)
    action = analysis["action"]
    event = action["position_action"]
    direction = action["direction"]
    status = str(current.get("status") or "FLAT")
    position_version = int(current.get("version") or 1)
    if preopen and event != "NONE":
        raise SemanticReplayError("盤前快照不得建立、停止或退出模擬持倉。")
    if event == "ENTER":
        if status != "FLAT":
            raise SemanticReplayError("已有模擬持倉時不得再次進場。")
        if direction not in {"LONG", "SHORT"}:
            raise SemanticReplayError("模擬進場必須指定LONG或SHORT。")
        stop = action.get("stop_price")
        if not isinstance(stop, (int, float)) or isinstance(stop, bool) or stop <= 0:
            raise SemanticReplayError("模擬進場必須提供正數結構停損價。")
        if direction == "LONG" and float(stop) >= latest_close:
            raise SemanticReplayError("多單停損必須低於進場收盤價。")
        if direction == "SHORT" and float(stop) <= latest_close:
            raise SemanticReplayError("空單停損必須高於進場收盤價。")
        entry_role = str(action.get("entry_role") or "INITIAL")
        setup_key = action.get("setup_key")
        if position_version >= 2:
            if not isinstance(setup_key, str) or not setup_key.strip():
                raise SemanticReplayError("v3模擬進場必須提供setup_key。")
            if entry_role == "REENTRY":
                last_stop = current.get("last_stop_time")
                if not isinstance(last_stop, str):
                    raise SemanticReplayError("沒有先前停損事件，不得標示再進場。")
                elapsed = _aware(as_of, "as_of") - _aware(last_stop, "last_stop_time")
                if elapsed.total_seconds() < 60:
                    raise SemanticReplayError("停損後至少等待1根完整1分K才能再進場。")
                if int(current.get("reentry_count") or 0) >= 1:
                    raise SemanticReplayError("同一setup最多只允許再進場1次。")
                if current.get("active_setup_key") != setup_key:
                    raise SemanticReplayError("再進場必須延續同一setup_key。")
                current["reentry_count"] = 1
            elif entry_role == "INITIAL":
                current["reentry_count"] = 0
                current["last_stop_time"] = None
            else:
                raise SemanticReplayError("ENTER的entry_role必須為INITIAL或REENTRY。")
            current["active_setup_key"] = setup_key
        current.update(
            {
                "status": direction,
                "entry_time": as_of,
                "entry_price": latest_close,
                "stop_price": float(stop),
                "direction": direction,
            }
        )
    elif event in {"STOP", "EXIT"}:
        if status not in {"LONG", "SHORT"}:
            raise SemanticReplayError("空手時不得輸出持倉停損或出場事件。")
        if direction not in {"NONE", status}:
            raise SemanticReplayError("出場方向與既有模擬持倉不一致。")
        current.update(
            {
                "status": "FLAT",
                "entry_time": None,
                "entry_price": None,
                "stop_price": None,
                "direction": None,
                "behavior_plan": None,
                "pending_exit": None,
            }
        )
        if position_version >= 2:
            if event == "STOP":
                stop_audit = protective_stop_audit if isinstance(protective_stop_audit, Mapping) else {}
                current["last_stop_time"] = (
                    stop_audit.get("trigger_time")
                    if stop_audit.get("status") == "TRIGGERED" and isinstance(stop_audit.get("trigger_time"), str)
                    else as_of
                )
                # Keeping the originating setup is useful only while its one
                # course-authorized re-entry remains available.  Once that
                # allowance has already been used, a second stop ends the
                # setup and a genuinely new opportunity must get a new key.
                if int(current.get("reentry_count") or 0) >= 1:
                    current["active_setup_key"] = None
            else:
                current["active_setup_key"] = None
                current["last_stop_time"] = None
                current["reentry_count"] = 0
    else:
        if direction != "NONE":
            raise SemanticReplayError("沒有持倉事件時direction必須為NONE。")
        requested_stop = action.get("stop_price")
        if requested_stop is not None:
            if status not in {"LONG", "SHORT"}:
                raise SemanticReplayError("空手時不得設定持倉保護停損。")
            if not isinstance(requested_stop, (int, float)) or isinstance(requested_stop, bool) or requested_stop <= 0:
                raise SemanticReplayError("持倉保護停損必須為正數。")
            old_stop = current.get("stop_price")
            if not isinstance(old_stop, (int, float)) or isinstance(old_stop, bool) or old_stop <= 0:
                raise SemanticReplayError("既有持倉缺少有效停損，不能調整保護位置。")
            new_stop = float(requested_stop)
            old_stop = float(old_stop)
            if status == "LONG":
                if new_stop < old_stop:
                    raise SemanticReplayError("多單保護停損只能維持或上移，不得放寬。")
                if new_stop >= latest_close:
                    raise SemanticReplayError("多單保護停損必須低於最新收盤價。")
            else:
                if new_stop > old_stop:
                    raise SemanticReplayError("空單保護停損只能維持或下移，不得放寬。")
                if new_stop <= latest_close:
                    raise SemanticReplayError("空單保護停損必須高於最新收盤價。")
            current["stop_price"] = new_stop
    current.update(
        {
            "version": position_version,
            "as_of": as_of,
            "last_action": event,
            "last_reason": analysis["notification_reason"],
        }
    )
    return current


def _analysis(
    value: Any,
    *,
    ledger: Mapping[str, Any],
    preopen: bool,
    position: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != ANALYSIS_KEYS:
        raise SemanticReplayError("analysis欄位不完整或含額外欄位。")
    result = dict(value)
    result["original_decision"] = _enum(value["original_decision"], {"NOTIFY", "DONT_NOTIFY"}, "original_decision")
    result["notification_reason"] = _text(value["notification_reason"], "notification_reason", 400)
    result["latest_closed_k_price_estimate"] = _text(value["latest_closed_k_price_estimate"], "latest_closed_k_price_estimate", 120)
    result["large_trend"] = _trend(value["large_trend"], "large_trend")
    result["current_trend"] = _trend(value["current_trend"], "current_trend")
    if result["large_trend"]["classification"] == "資料不足" and ledger.get("latest_closed_k"):
        raise SemanticReplayError("結構化資料完整時，大趨勢不得標成資料不足；可保守標示盤整。")
    result["market_summary"] = _text_list(value["market_summary"], "market_summary", 4)
    result["course_reading"] = _reading(value["course_reading"], ledger=ledger)
    result["scenario"] = _scenario(value["scenario"])
    result["action"] = _action(value["action"])
    event = result["action"]["position_action"]
    if preopen and event != "NONE":
        raise SemanticReplayError("盤前快照不得輸出持倉事件。")
    if event in {"STOP", "EXIT"} and position.get("status") not in {"LONG", "SHORT"}:
        raise SemanticReplayError("沒有既有模擬持倉，不得輸出STOP或EXIT。")
    return result


def _course_assessment(value: Any) -> dict[str, Any]:
    """Validate the model's version-bound judgement of one causal candidate."""

    keys = {
        "setup_key",
        "facts_hash",
        "overall",
        "checks",
        "evidence_refs",
        "reason_codes",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise SemanticReplayError("ai_course_assessment格式無效。")
    checks = value.get("checks")
    if not isinstance(checks, Mapping) or set(checks) != COURSE_ASSESSMENT_CHECK_KEYS:
        raise SemanticReplayError("ai_course_assessment.checks必須包含固定10項課程檢查。")
    normalized_checks: dict[str, str] = {}
    for raw_key, raw_value in checks.items():
        key = str(raw_key)
        normalized_checks[key] = _enum(
            raw_value,
            {"PASS", "FAIL", "UNKNOWN"},
            f"ai_course_assessment.checks.{key}",
        )
    def optional_text_list(raw: Any, field: str) -> list[str]:
        if not isinstance(raw, list) or len(raw) > 12:
            raise SemanticReplayError(f"{field}必須是0至12項文字陣列。")
        return [_text(item, field, 180) for item in raw]

    return {
        "setup_key": _text(value["setup_key"], "ai_course_assessment.setup_key", 180),
        "facts_hash": _text(value["facts_hash"], "ai_course_assessment.facts_hash", 128),
        "overall": _enum(
            value["overall"],
            {"PASS", "FAIL", "UNKNOWN"},
            "ai_course_assessment.overall",
        ),
        "checks": normalized_checks,
        "evidence_refs": optional_text_list(
            value["evidence_refs"], "ai_course_assessment.evidence_refs"
        ),
        "reason_codes": optional_text_list(
            value["reason_codes"], "ai_course_assessment.reason_codes"
        ),
    }


def _analysis_v3(
    value: Any,
    *,
    ledger: Mapping[str, Any],
    preopen: bool,
    position: Mapping[str, Any],
    evidence_events: list[Mapping[str, Any]],
    entry_gate: Mapping[str, Any] | None = None,
    previous_memory: Mapping[str, Any] | None = None,
    ai_generated: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) not in {
        frozenset(ANALYSIS_V3_KEYS),
        frozenset(ANALYSIS_V34_KEYS),
    }:
        raise SemanticReplayError("analysis v3欄位不完整或含額外欄位。")
    result = dict(value)
    # Build the causal structure-event index before any entry-decision checks.
    # A LONG_ONLY AI rejection can legitimately be evaluated before the later
    # event-card validation block; keeping this index local to that later block
    # made GRADE_CONFLICT/CONSTITUTION_BLOCKED crash with UnboundLocalError.
    raw_by_id = {
        str(item.get("id")): item
        for item in ledger.get("structure_events", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    result["original_decision"] = _enum(value["original_decision"], {"NOTIFY", "DONT_NOTIFY"}, "original_decision")
    result["message_type"] = _enum(value["message_type"], MESSAGE_TYPES, "message_type")
    result["message_direction"] = _enum(value["message_direction"], {"BULL", "BEAR", "NEUTRAL"}, "message_direction")
    result["notification_reason"] = _text(value["notification_reason"], "notification_reason", 240)
    result["latest_closed_k_price_estimate"] = _text(value["latest_closed_k_price_estimate"], "latest_closed_k_price_estimate", 80)
    if "ai_course_assessment" in value:
        result["ai_course_assessment"] = (
            None
            if value["ai_course_assessment"] is None
            else _course_assessment(value["ai_course_assessment"])
        )
    result["large_trend"] = _trend(value["large_trend"], "large_trend")
    result["current_trend"] = _trend(value["current_trend"], "current_trend")
    lifecycle = ledger.get("anchor_lifecycle")
    dow_context = lifecycle.get("dow_context") if isinstance(lifecycle, Mapping) else None
    quadrant_context = lifecycle.get("quadrant_context") if isinstance(lifecycle, Mapping) else None
    program_market_authority = (
        not _ai_hybrid_policy(ledger)
        and
        isinstance(quadrant_context, Mapping)
        and str(quadrant_context.get("authority")).startswith("PROGRAM_POLICY_")
    )
    if program_market_authority:
        # Program-owned market state is already the resolved result of anchor,
        # grade-lifecycle, Dow and quadrant transitions.  Canonicalize it
        # before the older analyzer-facing grade/Dow guards run; otherwise an
        # earlier promoted leg can temporarily label the large trend opposite
        # to the current program Dow state and be rejected before the later
        # canonicalization step gets a chance to resolve it.
        _canonicalize_program_market_trends(result, ledger=ledger)
    elif (
        isinstance(quadrant_context, Mapping) and quadrant_context.get("authority") == "EVIDENCE_ONLY"
        and not isinstance(lifecycle.get("background_anchor"), Mapping)
        and result["large_trend"]["classification"] not in {"盤整", "資料不足"}
    ):
        raw_reading = (
            value.get("course_reading")
            if isinstance(value.get("course_reading"), Mapping)
            else {}
        )
        if _large_grade_upgrade_quadrant_authority(raw_reading, ledger=ledger):
            # Course grade-upgrade promotes the observation/control scale even
            # before a separate formal background anchor is installed. The
            # program owns that direction until an explicit downgrade or
            # parent invalidation, so a model cannot erase it on a later bar.
            if not (_ai_hybrid_policy(ledger) and ai_generated):
                result["large_trend"] = _large_trend_for_promoted_structure(
                    result["large_trend"], raw_reading, ledger=ledger
                )
        else:
            raise SemanticReplayError("本時段尚無大級錨，不得把小級方向宣稱為已成立大趨勢。")
    elif not _ai_hybrid_policy(ledger) and _large_grade_upgrade_quadrant_authority(
        value.get("course_reading") if isinstance(value.get("course_reading"), Mapping) else {},
        ledger=ledger,
    ):
        result["large_trend"] = _large_trend_for_promoted_structure(
            result["large_trend"],
            value.get("course_reading") if isinstance(value.get("course_reading"), Mapping) else {},
            ledger=ledger,
        )
    if not _ai_hybrid_policy(ledger) and not program_market_authority and isinstance(dow_context, Mapping) and not (
        isinstance(quadrant_context, Mapping) and quadrant_context.get("authority") == "EVIDENCE_ONLY"
    ):
        result["large_trend"] = _large_trend_for_dow(result["large_trend"], dow_context)
    monitoring_session = ledger.get("monitoring_session")
    if (
        result["large_trend"]["classification"] == "資料不足"
        and ledger.get("latest_closed_k")
        and isinstance(monitoring_session, Mapping)
        and not _ai_hybrid_policy(ledger)
    ):
        # A newly opened independent monitoring segment has complete OHLCV even
        # before OR5 can establish its first anchor.  The honest structural
        # classification is therefore flat/unconfirmed, not missing data.
        result["large_trend"]["classification"] = "盤整"
    if result["large_trend"]["classification"] == "資料不足" and ledger.get("latest_closed_k"):
        raise SemanticReplayError("結構化資料完整時，大趨勢不得標成資料不足；可保守標示盤整。")
    result["market_summary"] = _text_list(value["market_summary"], "market_summary", 2)
    raw_course_reading = (
        dict(value["course_reading"])
        if isinstance(value.get("course_reading"), Mapping)
        else value["course_reading"]
    )
    if isinstance(raw_course_reading, dict):
        _canonicalize_program_course_reading(
            raw_course_reading,
            ledger=ledger,
            position=position,
            entry_gate=entry_gate,
            preopen=preopen,
        )
        _canonicalize_long_only_latest_material_event_ref(
            raw_course_reading,
            ledger=ledger,
            evidence_events=evidence_events,
        )
    result["course_reading"] = _reading_v3(
        raw_course_reading,
        ledger=ledger,
        previous_memory=previous_memory,
        ai_generated=ai_generated,
    )
    _canonicalize_long_only_upgrade_references(
        result,
        ledger=ledger,
        ai_generated=ai_generated,
    )
    _canonicalize_program_market_trends(result, ledger=ledger)
    if _long_only_policy(ledger):
        # The separate long-only experiment still needs bearish promoted
        # structure as no-long/risk evidence.  A bullish child anchor may
        # create a counter-trend opportunity, but it cannot silently relabel a
        # still-active LARGE bearish background.
        result["large_trend"] = _long_only_promoted_large_trend(
            result["large_trend"],
            result["course_reading"],
            ledger=ledger,
            ai_generated=ai_generated,
        )
    if not _ai_hybrid_policy(ledger):
        _canonicalize_taiji_parent_citation(result["course_reading"], ledger=ledger)
        _validate_taiji_parent_role(result["course_reading"], ledger=ledger)
    result["scenario"] = _scenario_v3(value["scenario"])
    _canonicalize_program_scenario_weights(result["scenario"], ledger=ledger)
    result["scenario"] = _preserve_active_session_anchor_probability(
        result["scenario"], ledger=ledger
    )
    gate = entry_gate if isinstance(entry_gate, Mapping) else {}
    raw_action = dict(value["action"]) if isinstance(value.get("action"), Mapping) else value["action"]
    if (
        isinstance(raw_action, dict)
        and raw_action.get("position_action") != "ENTER"
        and raw_action.get("entry_role") == "NOT_APPLICABLE"
        and raw_action.get("direction") in {"LONG", "SHORT"}
    ):
        # `message_direction`, the referenced setup and the position state
        # already carry directional context.  Some analyzers repeat it in
        # action.direction on management, stop or exit even though direction
        # is only an input for ENTER. Normalize only that redundant label;
        # entry/exit decisions and every price condition remain untouched.
        raw_action["direction"] = "NONE"
    if (
        isinstance(raw_action, dict)
        and gate.get("status") == "ENTRY_ELIGIBLE"
        and result["course_reading"].get("setup_stage") == "ENTRY_ELIGIBLE"
        and raw_action.get("position_action") == "NONE"
        and raw_action.get("entry_rejection_reason") in ENTRY_DENIAL_REASONS
        and raw_action.get("direction") == gate.get("direction")
        and raw_action.get("entry_role") in {"INITIAL", "REENTRY", "NOT_APPLICABLE"}
    ):
        # A fixed denial is a flat-position decision.  Some analyzers copy the
        # eligible signal's direction/role into an otherwise valid no-op
        # action.  Normalize only those redundant labels; the denial reason,
        # setup identity and substantive decision remain unchanged.
        raw_action["direction"] = "NONE"
        raw_action["entry_role"] = "NOT_APPLICABLE"
    if (
        isinstance(raw_action, dict)
        and gate.get("status") != "ENTRY_ELIGIBLE"
        and raw_action.get("position_action") == "NONE"
        and result["message_type"] == "PREPARATION"
        and result["course_reading"].get("setup_stage")
        in {"FORMING", "ARMED", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED"}
    ):
        # Direction and initial/re-entry role only become executable when the
        # deterministic gate is ENTRY_ELIGIBLE and the model chooses ENTER.
        # A preparation card already carries its directional setup in memory;
        # normalize duplicated no-op labels without changing that setup,
        # trigger, expiry or re-entry availability.
        raw_action["direction"] = "NONE"
        raw_action["entry_role"] = "NOT_APPLICABLE"
    _canonicalize_flat_noop_action(raw_action, position=position)
    _canonicalize_noop_action_labels(raw_action)
    result["action"] = _action_v3(raw_action)
    _canonicalize_program_action_envelope(
        result,
        ledger=ledger,
        position=position,
        entry_gate=gate,
        preopen=preopen,
        ai_generated=ai_generated,
    )
    _canonicalize_ai_hybrid_discretionary_exit_card(
        result,
        ledger=ledger,
        position=position,
        preopen=preopen,
        ai_generated=ai_generated,
    )
    _canonicalize_program_message_direction(
        result,
        ledger=ledger,
        position=position,
        ai_generated=ai_generated,
    )
    _canonicalize_ai_hybrid_unbacked_event_card(
        result,
        ledger=ledger,
        position=position,
        evidence_events=evidence_events,
        preopen=preopen,
        ai_generated=ai_generated,
    )

    if (
        position.get("status") in {"LONG", "SHORT"}
        and result["action"]["position_action"] == "NONE"
        and result["message_type"] in {"STRUCTURE_UPGRADE", "STRUCTURE_DOWNGRADE"}
    ):
        # A structure-grade event can occur while a position is open.  The
        # event still belongs in the causal reading, but the trading card must
        # remain a position-management decision so the open trade is never
        # hidden behind a second event type.  Flat positions continue to use
        # the dedicated structure upgrade/downgrade cards below.
        result["message_type"] = "MANAGEMENT"

    gate_is_eligible = gate.get("status") == "ENTRY_ELIGIBLE"
    if (
        gate_is_eligible
        and result["course_reading"].get("setup_stage") == "ENTRY_ELIGIBLE"
        and result["message_type"] in {
            "OBSERVATION", "ENTRY", "REENTRY", "STRUCTURE_UPGRADE", "STRUCTURE_DOWNGRADE", "INVALIDATION",
        }
    ):
        # The entry decision is program-owned and time-sensitive.  When a
        # structure event becomes visible on the same closed bar, keep its
        # reference and explanation but render the canonical entry-eligibility
        # card.  Models also sometimes call an accepted signal ENTRY even
        # though the fill belongs to the next tradable open; normalize only
        # that event label.  ENTER vs a fixed denial remains the model's
        # explicit choice and is validated below.
        if _ai_hybrid_policy(ledger) and ai_generated:
            raise SemanticReplayError(
                "AI_HYBRID接受ENTRY_ELIGIBLE時仍須輸出PREPARATION；"
                "實際ENTRY只在下一根可成交開盤成立，驗證器不會代替AI改寫。"
            )
        result["message_type"] = "PREPARATION"

    message_type = result["message_type"]
    decision = result["original_decision"]
    event = result["action"]["position_action"]
    entry_signal_accept = (
        gate_is_eligible
        and message_type == "PREPARATION"
        and result["course_reading"].get("setup_stage") == "ENTRY_ELIGIBLE"
        and event == "ENTER"
    )
    if decision == "DONT_NOTIFY" and message_type != "UNCHANGED":
        raise SemanticReplayError("DONT_NOTIFY只能搭配UNCHANGED事件卡。")
    if decision == "NOTIFY" and message_type == "UNCHANGED":
        raise SemanticReplayError("UNCHANGED必須使用DONT_NOTIFY，由節流政策決定是否傳送。")
    expected_position_event = {
        "ENTRY": "ENTER",
        "REENTRY": "ENTER",
        "STOP": "STOP",
        "EXIT": "EXIT",
    }.get(message_type)
    if expected_position_event is not None and event != expected_position_event:
        raise SemanticReplayError("訊息類型與position_action不一致。")
    if expected_position_event is None and event != "NONE" and not entry_signal_accept:
        raise SemanticReplayError("非進出場事件不得改變模擬持倉。")
    behavior_audit = ledger.get("position_behavior_audit")
    protective_stop_audit = ledger.get("protective_stop_audit")
    if (
        isinstance(protective_stop_audit, Mapping)
        and protective_stop_audit.get("status") == "TRIGGERED"
        and position.get("status") in {"LONG", "SHORT"}
    ):
        if event != "STOP":
            raise SemanticReplayError(
                "程式已確認保護停損觸發；本輪必須輸出STOP，不得由AI維持或改寫持倉。"
            )
        stop_level = protective_stop_audit.get("stop_price")
        action_stop = result["action"].get("stop_price")
        if not isinstance(action_stop, (int, float)) or abs(float(action_stop) - float(stop_level)) > 1e-9:
            raise SemanticReplayError("STOP必須引用程式鎖定的實際保護停損價。")
    if (
        isinstance(behavior_audit, Mapping)
        and behavior_audit.get("status") in {"EXPIRED_NO_PROGRESS", "FAILED_HOLD"}
        and position.get("status") in {"LONG", "SHORT"}
        and event not in {"EXIT", "STOP"}
    ):
        hold_price = behavior_audit.get("hold_price", behavior_audit.get("checkpoint_price"))
        hold_label = behavior_audit.get("hold_label") or "應守位置"
        raise SemanticReplayError(
            f"事前應有行為已失效（{behavior_audit.get('status')}，{hold_label}{hold_price}）；"
            "依時間／動機失效規則必須輸出EXIT或已觸及停損時輸出STOP，不得只等結構停損。"
        )
    if message_type == "REENTRY" and result["action"]["entry_role"] != "REENTRY":
        raise SemanticReplayError("REENTRY事件必須標示entry_role=REENTRY。")
    if message_type == "ENTRY" and result["action"]["entry_role"] != "INITIAL":
        raise SemanticReplayError("ENTRY事件必須標示entry_role=INITIAL。")
    if preopen and (message_type != "SNAPSHOT" or event != "NONE"):
        raise SemanticReplayError("盤前只能輸出SNAPSHOT且不得改變模擬持倉。")
    if event in {"STOP", "EXIT"} and position.get("status") not in {"LONG", "SHORT"}:
        raise SemanticReplayError("沒有既有模擬持倉，不得輸出STOP或EXIT。")

    reading = result["course_reading"]
    v4_entry_contract = "entry_rejection_reason" in result["action"]
    if v4_entry_contract:
        denial = result["action"]["entry_rejection_reason"]
        if gate_is_eligible:
            _validate_ai_hybrid_one_contract_grade_conflict(
                result,
                ledger=ledger,
                entry_gate=gate,
            )
            if message_type != "PREPARATION" or reading.get("setup_stage") != "ENTRY_ELIGIBLE":
                raise SemanticReplayError("程式已標記ENTRY_ELIGIBLE時必須輸出PREPARATION進場資格卡。")
            if result["action"].get("setup_key") != gate.get("setup_key"):
                raise SemanticReplayError("ENTRY_ELIGIBLE事件必須沿用程式觸發的setup_key。")
            if gate.get("required_position_action") == "ENTER":
                if event != "ENTER":
                    raise SemanticReplayError(
                        "程式化交易政策已決定ENTER；AI只能解釋，不得否決或改成等待。"
                    )
                if denial != "NONE":
                    raise SemanticReplayError("程式化ENTER不得填入AI否決原因。")
                required_stop = gate.get("required_stop_price")
                actual_stop = result["action"].get("stop_price")
                if (
                    not isinstance(required_stop, (int, float))
                    or isinstance(required_stop, bool)
                    or not isinstance(actual_stop, (int, float))
                    or isinstance(actual_stop, bool)
                    or abs(float(actual_stop) - float(required_stop)) > 1e-6
                ):
                    raise SemanticReplayError(
                        "AI停損必須逐點沿用程式化完整修正外停損，不得自行移動。"
                    )
                required_wait = gate.get("required_behavior_max_wait_bars")
                if (
                    isinstance(required_wait, int)
                    and not isinstance(required_wait, bool)
                    and result["action"].get("max_wait_bars") != required_wait
                ):
                    raise SemanticReplayError(
                        "AI等待窗必須沿用程式化戰法設定，不得自行選擇3～5根。"
                    )
                required_obstacles = gate.get("required_behavior_obstacles")
                if isinstance(required_obstacles, list):
                    expected_roles_and_prices = [
                        (str(item.get("role") or ""), float(item["price"]))
                        for item in required_obstacles
                        if isinstance(item, Mapping)
                        and isinstance(item.get("price"), (int, float))
                        and not isinstance(item.get("price"), bool)
                    ]
                    actual_roles_and_prices = [
                        (str(item.get("role") or ""), float(item["price"]))
                        for item in result["action"].get("obstacles", [])
                        if isinstance(item, Mapping)
                        and isinstance(item.get("price"), (int, float))
                        and not isinstance(item.get("price"), bool)
                    ]
                    if actual_roles_and_prices != expected_roles_and_prices:
                        raise SemanticReplayError(
                            "AI觸發線與管理檢查點必須逐項沿用程式化交易計畫。"
                        )
            if event == "ENTER":
                if denial != "NONE":
                    raise SemanticReplayError("接受ENTER時entry_rejection_reason必須為NONE。")
                if result["action"].get("direction") != gate.get("direction"):
                    raise SemanticReplayError("ENTER方向必須與程式觸發的setup方向一致。")
                required_stop = gate.get("required_stop_price")
                actual_stop = result["action"].get("stop_price")
                if (
                    isinstance(required_stop, (int, float))
                    and not isinstance(required_stop, bool)
                    and (
                        not isinstance(actual_stop, (int, float))
                        or isinstance(actual_stop, bool)
                        or abs(float(actual_stop) - float(required_stop)) > 1e-6
                    )
                ):
                    raise SemanticReplayError("AI_HYBRID接受進場時不得移動程式鎖定的結構停損。")
                required_wait = gate.get("required_behavior_max_wait_bars")
                actual_wait = result["action"].get("max_wait_bars")
                if (
                    isinstance(required_wait, int)
                    and not isinstance(required_wait, bool)
                    and (
                        not isinstance(actual_wait, int)
                        or isinstance(actual_wait, bool)
                        or not 1 <= actual_wait <= required_wait
                    )
                ):
                    raise SemanticReplayError("AI_HYBRID等待窗必須為程式上限內的正整數。")
                required_obstacles = gate.get("required_behavior_obstacles")
                if isinstance(required_obstacles, list):
                    expected_pairs = [
                        (str(item.get("role") or ""), float(item["price"]))
                        for item in required_obstacles
                        if isinstance(item, Mapping)
                        and isinstance(item.get("price"), (int, float))
                        and not isinstance(item.get("price"), bool)
                    ]
                    actual_pairs = [
                        (str(item.get("role") or ""), float(item["price"]))
                        for item in result["action"].get("obstacles", [])
                        if isinstance(item, Mapping)
                        and isinstance(item.get("price"), (int, float))
                        and not isinstance(item.get("price"), bool)
                    ]
                    if actual_pairs != expected_pairs:
                        raise SemanticReplayError("AI_HYBRID不得改寫程式可核對的觸發線與檢查點。")
            elif event == "NONE":
                if denial not in ENTRY_DENIAL_REASONS:
                    raise SemanticReplayError("不進場時必須選擇固定否決原因，不得繼續模糊等待。")
                if denial == "STOP_TOO_WIDE" and not _has_explicit_entry_risk_limit(
                    ledger=ledger,
                    entry_gate=gate,
                ):
                    raise SemanticReplayError(
                        "未提供事前數值風險上限；結構停損應由事前金額風險與部位大小管理，"
                        "不得在訊號成立後臨時以停損點數過寬取消進場。"
                    )
                if _long_only_policy(ledger):
                    selected_upgrade = raw_by_id.get(
                        str(reading.get("structure_event_ref") or "")
                    )
                    expected_upgrade_direction = {
                        "LONG": "BULL",
                        "SHORT": "BEAR",
                    }.get(str(gate.get("direction")))
                    aligned_upgrade = bool(
                        isinstance(selected_upgrade, Mapping)
                        and selected_upgrade.get("event_type") == "GRADE_UPGRADE"
                        and selected_upgrade.get("direction") == expected_upgrade_direction
                        and selected_upgrade.get("from_level") == "SMALL"
                        and selected_upgrade.get("to_level") == "LARGE"
                    )
                else:
                    aligned_upgrade = _aligned_entry_grade_upgrade(
                        ledger=ledger,
                        evidence_events=evidence_events,
                        entry_gate=gate,
                    )
                if aligned_upgrade and denial in {"GRADE_CONFLICT", "CONSTITUTION_BLOCKED"}:
                    raise SemanticReplayError(
                        "同方向SMALL→LARGE結構升級是既有進場訊號的支持證據；"
                        "不得只以控制級數切換、級數衝突或交易憲法作為取消理由。"
                    )
                if denial == "GRADE_CONFLICT" and _is_highest_active_anchor_aligned_entry(
                    ledger=ledger,
                    entry_gate=gate,
                ):
                    raise SemanticReplayError(
                        "此進場訊號與目前最高作用中控制錨同向，且該級防線仍有效；"
                        "反向修正是Q4或太極延續的交易背景，不得單獨以GRADE_CONFLICT否決。"
                    )
                if (
                    denial == "GRADE_CONFLICT"
                    and not _long_only_policy(ledger)
                    and _is_large_aligned_false_break_reentry(
                        ledger=ledger,
                        entry_gate=gate,
                    )
                ):
                    raise SemanticReplayError(
                        "此再進訊號與作用中大級錨同向，且大級防線仍有效；"
                        "小級反向道氏正是本假跌破戰法要處理的衝突，只能列為檢查點，"
                        "不得單獨以GRADE_CONFLICT否決。"
                    )
            else:
                raise SemanticReplayError("ENTRY_ELIGIBLE時AI只能接受ENTER或使用固定原因否決。")
        else:
            if (
                reading.get("setup_stage") == "ENTRY_ELIGIBLE"
                and position.get("status") not in {"LONG", "SHORT"}
            ):
                raise SemanticReplayError("沒有程式觸發事件時不得自行宣告ENTRY_ELIGIBLE。")
            if denial != "NONE":
                raise SemanticReplayError("非進場資格事件不得填入否決原因。")
            if event == "ENTER":
                raise SemanticReplayError("新版回放不得在訊號K直接成交；必須先由程式建立ENTRY_ELIGIBLE。")
        if position.get("status") in {"LONG", "SHORT"} and message_type not in {"MANAGEMENT", "STOP", "EXIT", "UNCHANGED"}:
            raise SemanticReplayError("已有模擬持倉時只能輸出管理、停損、出場或不變事件。")
    if (
        not preopen
        and message_type == "OBSERVATION"
        and reading.get("setup_stage") in {"ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED"}
    ):
        raise SemanticReplayError("setup已待觸發或確認時必須使用PREPARATION，不得退回一般OBSERVATION。")
    anchor_control = ledger.get("anchor_control")
    expected_working = (
        anchor_control.get("working_leg_ref")
        if isinstance(anchor_control, Mapping)
        else (ledger.get("control_candidates") or {}).get("forming_small_anchor_ref")
        if isinstance(ledger.get("control_candidates"), Mapping)
        else None
    )
    if (
        not _ai_hybrid_policy(ledger)
        and expected_working is not None
        and reading.get("working_anchor_ref") != expected_working
    ):
        raise SemanticReplayError("working_anchor_ref必須引用最新形成中的小級工作錨。")
    new_structure_events = [
        raw_by_id[str(item.get("event_id"))]
        for item in evidence_events
        if isinstance(item, Mapping) and str(item.get("event_id")) in raw_by_id
    ]
    latest_material_event = _latest_material_structure_event(
        new_structure_events,
        ledger=ledger,
    )
    if _ai_hybrid_policy(ledger) and not preopen:
        selected_event_ref = reading.get("structure_event_ref")
        if selected_event_ref is not None:
            visible_by_id = {
                str(item.get("id")): item
                for item in new_structure_events
                if item.get("id")
            }
            selected_material_event = visible_by_id.get(str(selected_event_ref))
            selected_event_is_current = isinstance(selected_material_event, Mapping)
            if not selected_event_is_current:
                selected_material_event = raw_by_id.get(str(selected_event_ref))
                active_promotion = _referenced_active_promotion(
                    reading,
                    ledger=ledger,
                )
                if not (
                    _long_only_policy(ledger)
                    and isinstance(selected_material_event, Mapping)
                    and selected_material_event.get("event_type") == "GRADE_UPGRADE"
                    and isinstance(active_promotion, Mapping)
                    and active_promotion.get("id") == selected_material_event.get("id")
                ):
                    raise SemanticReplayError(
                        "AI_HYBRID只能採用本輪新可見或仍作用中的結構升級候選，"
                        "不得重播已失效事件。"
                    )
                if message_type in {
                    "STRUCTURE_UPGRADE",
                    "STRUCTURE_DOWNGRADE",
                    "INVALIDATION",
                }:
                    raise SemanticReplayError(
                        "後續輪次引用仍作用中的升級只代表控制依據；"
                        "不得重播結構升級事件卡。"
                    )
            if not _long_only_policy(ledger) and (
                not isinstance(latest_material_event, Mapping)
                or selected_event_ref != latest_material_event.get("id")
            ):
                # Preserve the frozen bidirectional v32 behavior. The
                # long-only experiment is the first version with explicit AI
                # semantic ownership among same-close sibling candidates.
                raise SemanticReplayError(
                    "AI_HYBRID只能採用本輪最新可見的結構事件候選，不得重播舊事件。"
                )
            if selected_event_is_current:
                material_type = str(selected_material_event.get("event_type"))
                expected_type = _ai_hybrid_expected_event_card_type(
                    material_type=material_type,
                    setup_stage=str(reading.get("setup_stage") or "NONE"),
                    position_status=str(position.get("status") or "FLAT"),
                    position_action=str(event or "NONE"),
                    gate_is_eligible=gate_is_eligible,
                )
                if message_type != expected_type:
                    raise SemanticReplayError(
                        f"AI_HYBRID採用{material_type}時必須使用{expected_type}事件卡。"
                    )
        elif message_type in {
            "STRUCTURE_UPGRADE",
            "STRUCTURE_DOWNGRADE",
            "INVALIDATION",
        }:
            setup_invalidation_event = _program_setup_invalidation_event(
                message_type=message_type,
                reading=reading,
                action=result.get("action"),
                evidence_events=evidence_events,
            )
            if not isinstance(setup_invalidation_event, Mapping):
                raise SemanticReplayError(
                    "AI_HYBRID未引用本輪結構事件候選時，不得輸出結構升降級或失效事件卡；"
                    "setup失效卡則必須逐項對應本輪PROGRAM_SETUP_INVALIDATED事件。"
                )
            # ``action.setup_key`` is normally null for a NONE action.  Bind the
            # single program-owned terminal event after the model chooses the
            # invalidation card, so auditability does not depend on asking the
            # model to violate the no-op action convention.
            result["action"]["setup_key"] = str(setup_invalidation_event["setup_key"])
    if (
        not _ai_hybrid_policy(ledger)
        and not preopen
        and isinstance(latest_material_event, Mapping)
        and position.get("status") == "FLAT"
    ):
        material_type = str(latest_material_event.get("event_type"))
        false_break_is_left_side = (
            material_type == "FALSE_BREAK_RECLAIM"
            and _is_countertrend_to_highest_active_anchor(
                str(latest_material_event.get("direction")),
                ledger=ledger,
            )
        )
        setup_is_actionable_context = reading.get("setup_stage") in {
            "ARMED",
            "ENTRY_ELIGIBLE",
            "AGGRESSIVE_CONFIRMED",
            "CONSERVATIVE_CONFIRMED",
        }
        trade_levels = ledger.get("trade_levels")
        trade_levels = trade_levels if isinstance(trade_levels, Mapping) else {}
        quality_audit = trade_levels.get("course_entry_quality_audit")
        filtered_candidate = trade_levels.get("course_filtered_candidate")
        false_break_is_course_filtered = bool(
            material_type == "FALSE_BREAK_RECLAIM"
            and isinstance(quality_audit, Mapping)
            and quality_audit.get("status") == "OBSERVATION_ONLY"
            and isinstance(filtered_candidate, Mapping)
            and filtered_candidate.get("candidate_source") == "FALSE_BREAK_RECLAIM"
            and filtered_candidate.get("source_event_id") == latest_material_event.get("id")
        )
        program_policy = ledger.get("program_trade_policy")
        course_entry_gate_enabled = bool(
            isinstance(program_policy, Mapping)
            and program_policy.get("entry_quality_authority") == "PROGRAM_COURSE_GATE_V1"
        )
        expected_message_type = {
            # A countertrend false-break is only an observation when there is
            # no still-active program setup.  If an independent setup remains
            # armed, its preparation card owns the action layer and the
            # false-break remains cited as structural context.
            "FALSE_BREAK_RECLAIM": (
                (
                    "PREPARATION" if setup_is_actionable_context else "OBSERVATION"
                )
                if course_entry_gate_enabled
                else (
                    "OBSERVATION"
                    if (false_break_is_left_side or false_break_is_course_filtered)
                    and not setup_is_actionable_context
                    else "PREPARATION"
                )
            ),
            "GRADE_UPGRADE": "STRUCTURE_UPGRADE",
            "GRADE_DOWNGRADE": "STRUCTURE_DOWNGRADE",
            "STRUCTURE_INVALIDATED": "INVALIDATION",
        }[material_type]
        if not gate_is_eligible and message_type != expected_message_type:
            raise SemanticReplayError(
                f"本輪最新客觀事件為{material_type}，空手時必須使用{expected_message_type}事件卡。"
            )
        if reading.get("structure_event_ref") != latest_material_event.get("id"):
            raise SemanticReplayError("事件卡必須引用本輪最新客觀結構事件。")
    elif (
        not _ai_hybrid_policy(ledger)
        and not preopen
        and isinstance(latest_material_event, Mapping)
        and position.get("status") in {"LONG", "SHORT"}
        and str(latest_material_event.get("event_type")) in {"GRADE_UPGRADE", "GRADE_DOWNGRADE"}
        and event == "NONE"
    ):
        material_type = str(latest_material_event.get("event_type"))
        if message_type != "MANAGEMENT":
            raise SemanticReplayError("持倉中發生結構升降級時必須保留事件，但輸出持倉管理卡。")
        if reading.get("structure_event_ref") != latest_material_event.get("id"):
            raise SemanticReplayError("持倉管理卡必須引用本輪最新客觀結構升降級事件。")
        if material_type == "GRADE_UPGRADE":
            program_anchor_control = (
                isinstance(ledger.get("program_trade_policy"), Mapping)
                and isinstance(anchor_control, Mapping)
            )
            active_large_after_event = (
                anchor_control.get("active_background_anchor_ref")
                if isinstance(anchor_control, Mapping)
                else None
            )
            active_promoted_structure = _latest_active_promoted_structure(ledger)
            upgrade_controls_large = bool(
                active_large_after_event
                or active_promoted_structure
                or not program_anchor_control
            )
            if upgrade_controls_large and reading.get("controlling_grade") != "LARGE":
                raise SemanticReplayError("控制結構升級後controlling_grade必須切換為LARGE。")
            if not upgrade_controls_large and reading.get("controlling_grade") == "LARGE":
                raise SemanticReplayError("局部結構升級尚未接管時controlling_grade不得提前切換為LARGE。")
            if upgrade_controls_large and not _valid_post_upgrade_background_quadrant(reading, ledger=ledger):
                raise SemanticReplayError("結構升級後大級背景必須依大級兩軸判為Q1至Q4，不得繼續停在TRANSITION。")
        else:
            active_large_after_event = (
                anchor_control.get("active_background_anchor_ref")
                if isinstance(anchor_control, Mapping)
                else None
            )
            if active_large_after_event and reading.get("controlling_grade") != "LARGE":
                raise SemanticReplayError("局部結構降級後仍有作用中大級背景，controlling_grade必須維持LARGE。")
            if not active_large_after_event and reading.get("controlling_grade") == "LARGE":
                raise SemanticReplayError("控制大級結構降級後controlling_grade不得繼續標示LARGE。")
    if message_type == "STRUCTURE_UPGRADE":
        reference = reading.get("structure_event_ref")
        event_by_id = {
            str(item.get("event_id")): item
            for item in evidence_events
            if isinstance(item, Mapping) and item.get("event_id")
        }
        if reference not in event_by_id or raw_by_id.get(str(reference), {}).get("event_type") != "GRADE_UPGRADE":
            raise SemanticReplayError("STRUCTURE_UPGRADE必須引用本輪新發生的GRADE_UPGRADE證據。")
        raw_event = raw_by_id[str(reference)]
        if not isinstance(anchor_control, Mapping) and reading.get("large_anchor_ref") != raw_event.get("source_anchor_id"):
            raise SemanticReplayError("結構升級後large_anchor_ref必須引用程式建立的升級大錨。")
        program_anchor_control = (
            isinstance(ledger.get("program_trade_policy"), Mapping)
            and isinstance(anchor_control, Mapping)
        )
        active_large_after_event = (
            anchor_control.get("active_background_anchor_ref")
            if isinstance(anchor_control, Mapping)
            else None
        )
        if _ai_hybrid_policy(ledger):
            accepted_upgrade_anchor = _upgrade_controlling_anchor_ref(raw_event, ledger=ledger)
            if (
                not accepted_upgrade_anchor
                or reading.get("large_anchor_ref") != accepted_upgrade_anchor
                or reading.get("controlling_grade") != "LARGE"
            ):
                raise SemanticReplayError(
                    "AI_HYBRID採用結構升級事件時，必須同時採用對應大錨並把控制級數切換為LARGE。"
                )
        else:
            active_promoted_structure = _latest_active_promoted_structure(ledger)
            upgrade_controls_large = bool(
                active_large_after_event
                or active_promoted_structure
                or not program_anchor_control
            )
            if upgrade_controls_large and reading.get("controlling_grade") != "LARGE":
                raise SemanticReplayError("控制結構升級後controlling_grade必須切換為LARGE。")
            if not upgrade_controls_large and reading.get("controlling_grade") == "LARGE":
                raise SemanticReplayError("局部結構升級尚未接管時controlling_grade不得提前切換為LARGE。")
        if (
            not _ai_hybrid_policy(ledger)
            and upgrade_controls_large
            and not _valid_post_upgrade_background_quadrant(reading, ledger=ledger)
        ):
            raise SemanticReplayError("結構升級後大級背景必須依大級兩軸判為Q1至Q4，不得繼續停在TRANSITION。")
    if message_type == "STRUCTURE_DOWNGRADE":
        reference = reading.get("structure_event_ref")
        event_by_id = {
            str(item.get("event_id")): item
            for item in evidence_events
            if isinstance(item, Mapping) and item.get("event_id")
        }
        if reference not in event_by_id or raw_by_id.get(str(reference), {}).get("event_type") != "GRADE_DOWNGRADE":
            raise SemanticReplayError("STRUCTURE_DOWNGRADE必須引用本輪新發生的GRADE_DOWNGRADE證據。")
        if not _ai_hybrid_policy(ledger):
            active_large_after_event = (
                anchor_control.get("active_background_anchor_ref")
                if isinstance(anchor_control, Mapping)
                else None
            )
            if active_large_after_event and reading.get("controlling_grade") != "LARGE":
                raise SemanticReplayError("局部結構降級後仍有作用中大級背景，controlling_grade必須維持LARGE。")
            if not active_large_after_event and reading.get("controlling_grade") == "LARGE":
                raise SemanticReplayError("控制大級結構降級後controlling_grade不得繼續標示LARGE。")
    if message_type == "INVALIDATION" and reading.get("structure_event_ref") is not None:
        raw_event = raw_by_id.get(str(reading.get("structure_event_ref")))
        if not isinstance(raw_event, Mapping) or raw_event.get("event_type") != "STRUCTURE_INVALIDATED":
            raise SemanticReplayError("引用客觀結構事件的INVALIDATION必須對應STRUCTURE_INVALIDATED。")
    return result


def _validate_ai_hybrid_public_language(
    analysis: Mapping[str, Any], *, ledger: Mapping[str, Any]
) -> None:
    if not _ai_hybrid_policy(ledger):
        return
    reading = analysis.get("course_reading")
    scenario = analysis.get("scenario")
    action = analysis.get("action")
    visible_values: list[Any] = [
        analysis.get("notification_reason"),
        analysis.get("market_summary"),
        analysis.get("large_trend"),
        analysis.get("current_trend"),
    ]
    if isinstance(reading, Mapping):
        visible_values.extend(
            reading.get(key)
            for key in (
                "taiji",
                "yizhi",
                "left_right",
                "dow",
                "primary_lens",
                "main_strategy",
            )
        )
    if isinstance(scenario, Mapping):
        visible_values.extend(
            scenario.get(key)
            for key in ("bull_plan", "range_plan", "bear_plan", "view_change")
        )
    if isinstance(action, Mapping):
        visible_values.extend(
            action.get(key)
            for key in (
                "observation_area",
                "trigger",
                "entry",
                "structural_stop",
                "expected_behavior",
                "no_chase",
                "management",
            )
        )

    def strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, Mapping):
            result: list[str] = []
            for child in value.values():
                result.extend(strings(child))
            return result
        if isinstance(value, list):
            result = []
            for child in value:
                result.extend(strings(child))
            return result
        return []

    prohibited = re.compile(r"程式(?:候選|提供|決定|主控|判讀|化)|PROGRAM_[A-Z_]+")
    if any(prohibited.search(text) for value in visible_values for text in strings(value)):
        raise SemanticReplayError(
            "AI_HYBRID使用者可見文字不得把程式候選或內部權責當成盤勢結論；"
            "請直接寫課程上的結構、戰法與操作判斷。"
        )


def _canonicalize_program_course_reading(
    reading: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    entry_gate: Mapping[str, Any] | None,
    preopen: bool,
) -> None:
    """Replace model labels with the deterministic course-state verdicts.

    The model still explains the state.  It does not get a veto over quadrant
    axes, Taiji ordering, or the X-process stage once the replay engine has
    enough causal evidence to publish those fields.
    """

    if not _program_only_policy(ledger):
        return
    lifecycle = ledger.get("anchor_lifecycle")
    if not isinstance(lifecycle, Mapping):
        return
    quadrant = lifecycle.get("quadrant_context")
    if isinstance(quadrant, Mapping) and str(quadrant.get("authority")).startswith("PROGRAM_POLICY_"):
        mapping = {
            "background_quadrant": "background_primary",
            "background_trend_dynamics": "background_trend_dynamics",
            "background_volatility_dynamics": "background_volatility_dynamics",
            "working_quadrant": "working_primary",
            "working_trend_dynamics": "working_trend_dynamics",
            "working_volatility_dynamics": "working_volatility_dynamics",
        }
        for target, source in mapping.items():
            value = quadrant.get(source)
            if value is not None:
                reading[target] = value
        candidates = [
            str(item)
            for item in quadrant.get("working_candidates", [])
            if item in {"Q1", "Q2", "Q3", "Q4", "TRANSITION", "UNDEFINED"}
        ]
        reading["primary_quadrant_candidate"] = candidates[0] if candidates else "UNDEFINED"
        reading["secondary_quadrant_candidate"] = candidates[1] if len(candidates) > 1 else "UNDEFINED"

    constraints = ledger.get("course_constraints")
    required_mode = constraints.get("required_cclass_mode") if isinstance(constraints, Mapping) else None
    methods = ledger.get("course_method_state")
    taiji = lifecycle.get("taiji_context")
    program_taiji = isinstance(taiji, Mapping) and str(taiji.get("assessment_authority")).startswith(
        "PROGRAM_POLICY_"
    )
    if required_mode == "RESETTING":
        reading["cclass_mode"] = "RESETTING"
    elif isinstance(methods, Mapping) and methods.get("cclass_mode") in {
        "TAIJI_ORDERED", "YIZHI_MOMENTUM", "UNORDERED", "RESETTING", "UNDEFINED"
    }:
        reading["cclass_mode"] = methods["cclass_mode"]
    elif program_taiji and taiji.get("engine_mode") in {
        "TAIJI_ORDERED", "UNORDERED", "RESETTING", "UNDEFINED"
    }:
        reading["cclass_mode"] = taiji["engine_mode"]
    if required_mode == "RESETTING":
        reading["taiji"] = "舊小錨已降級且反向道氏控制；目前僅屬反向修正／重建嘗試，尚非同向太極複製。"
    elif program_taiji:
        reading["taiji"] = _program_taiji_text(taiji)
    if isinstance(methods, Mapping) and str(methods.get("authority")).startswith("PROGRAM_POLICY_"):
        reading["yizhi"] = _program_yizhi_text(methods.get("yizhi"))
        reading["left_right"] = _program_left_right_text(methods.get("left_right"))
    reading["dow"] = _program_dow_text(lifecycle.get("dow_context"))

    gate = entry_gate if isinstance(entry_gate, Mapping) else {}
    levels = ledger.get("trade_levels")
    program_candidate = (
        levels.get("continuation_arm_candidate")
        if isinstance(levels, Mapping)
        else None
    )
    preentry_invalidation = ledger.get("preentry_invalidation_audit")
    preentry_invalidation = (
        preentry_invalidation
        if isinstance(preentry_invalidation, Mapping)
        and preentry_invalidation.get("status") == "INVALIDATED_BEFORE_ENTRY"
        else None
    )
    session = ledger.get("monitoring_session")
    bar_count = int(session.get("bar_count") or 0) if isinstance(session, Mapping) else 0
    if preopen:
        x_stage = "PREOPEN_CONTEXT"
    elif position.get("status") in {"LONG", "SHORT"}:
        x_stage = "POSITION_MANAGEMENT"
    elif gate.get("status") == "ENTRY_ELIGIBLE":
        x_stage = "ENTRY_EXECUTION"
    elif isinstance(program_candidate, Mapping):
        x_stage = "OPPORTUNITY_GRADING"
    elif preentry_invalidation is not None:
        x_stage = "RESET"
    elif required_mode == "RESETTING":
        x_stage = "RESET"
    elif isinstance(lifecycle.get("background_anchor"), Mapping) or isinstance(
        lifecycle.get("child_anchor"), Mapping
    ):
        x_stage = "ANCHOR_LENS_SELECTION"
    elif bar_count < 5:
        x_stage = "OPENING_EVIDENCE"
    else:
        x_stage = "FIRST_ENDPOINT"
    reading["x_stage"] = x_stage
    reading["x_process"] = _program_x_process_text(
        x_stage,
        program_candidate=program_candidate,
        entry_gate=gate,
        position=position,
    )
    if preentry_invalidation is not None:
        invalidated_at = str(preentry_invalidation.get("invalidated_at") or "")
        invalidated_time = invalidated_at[11:16] if len(invalidated_at) >= 16 else "本輪"
        reading["x_process"] = (
            f"原候選於{invalidated_time}在進場前觸及結構停損；"
            "舊setup已退役，等待新的完整修正重新武裝。"
        )

    if reading.get("cclass_mode") == "YIZHI_MOMENTUM":
        reading["focus_methods"] = ["YIZHI", "X_PROCESS"]
    elif program_taiji and reading.get("cclass_mode") == "TAIJI_ORDERED":
        reading["focus_methods"] = ["TAIJI", "X_PROCESS"]
    else:
        reading["focus_methods"] = ["QUADRANT", "X_PROCESS"]
    working_quadrant = str(reading.get("working_quadrant") or "UNDEFINED")
    reading["primary_lens"] = (
        f"程式主鏡頭={reading.get('cclass_mode')}；工作象限={working_quadrant}；"
        f"X階段={x_stage}。"
    )
    if preentry_invalidation is not None:
        reading["main_strategy"] = (
            f"{preentry_invalidation.get('setup_name') or '程式候選'}（進場前失效）。"
        )
    elif isinstance(program_candidate, Mapping):
        reading["main_strategy"] = (
            f"{program_candidate.get('setup_name') or '程式候選'}；"
            f"狀態={program_candidate.get('stage') or 'ARMED'}。"
        )
    else:
        reading["main_strategy"] = {
            "Q1": "Q1順勢突破觀察；尚無程式可成交候選。",
            "Q2": "Q2反向拉回觀察；尚無程式可成交候選。",
            "Q3": "Q3低效率盤；程式維持觀望。",
            "Q4": "Q4順勢修正觀察；尚無程式可成交候選。",
            "TRANSITION": "象限轉換中；程式維持觀望。",
        }.get(working_quadrant, "結構證據不足；程式維持觀望。")


def _canonicalize_program_course_memory(
    analysis: Mapping[str, Any],
    memory: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    previous_memory: Mapping[str, Any] | None,
    expected_as_of: str,
    expected_session_key: str,
) -> None:
    """Mirror program-owned quadrant state and preserve exact change times."""

    if not _program_only_policy(ledger):
        return
    lifecycle = ledger.get("anchor_lifecycle")
    quadrant = lifecycle.get("quadrant_context") if isinstance(lifecycle, Mapping) else None
    reading = analysis.get("course_reading")
    control = memory.get("structure_control")
    if not (
        isinstance(quadrant, Mapping)
        and str(quadrant.get("authority")).startswith("PROGRAM_POLICY_")
        and isinstance(reading, Mapping)
        and isinstance(control, dict)
    ):
        return
    previous_control = (
        previous_memory.get("structure_control")
        if isinstance(previous_memory, Mapping)
        and previous_memory.get("version") == 3
        and previous_memory.get("session_key") == expected_session_key
        else None
    )
    for key in ("background_quadrant", "working_quadrant"):
        value = str(reading.get(key) or "UNDEFINED")
        control[key] = value
        changed_key = f"{key}_changed_at"
        if isinstance(previous_control, Mapping) and previous_control.get(key) == value:
            control[changed_key] = previous_control.get(changed_key) or expected_as_of
        else:
            control[changed_key] = expected_as_of


def _canonicalize_ai_hybrid_mechanical_memory_fields(
    analysis: Mapping[str, Any],
    memory: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    entry_gate: Mapping[str, Any] | None,
    previous_memory: Mapping[str, Any] | None,
    expected_as_of: str,
    expected_session_key: str,
    ai_generated: bool = True,
) -> None:
    """Derive duplicated bookkeeping fields without changing AI judgement.

    Quadrant labels and the ENTER/deny decision remain model-owned.  Once those
    decisions exist, their change timestamps and the program gate's matching
    setup stage are deterministic duplicates.  Normalizing only these fields
    avoids spending another model call on copy errors while preserving strict
    checks for setup identity, direction, trigger and price.
    """

    if not (_ai_hybrid_policy(ledger) and ai_generated):
        return
    reading = analysis.get("course_reading")
    control = memory.get("structure_control")
    if isinstance(reading, Mapping) and isinstance(control, dict):
        previous_control = (
            previous_memory.get("structure_control")
            if isinstance(previous_memory, Mapping)
            and previous_memory.get("version") == 3
            and previous_memory.get("session_key") == expected_session_key
            else None
        )
        for key in ("background_quadrant", "working_quadrant"):
            current_value = control.get(key)
            changed_key = f"{key}_changed_at"
            if isinstance(previous_control, Mapping) and previous_control.get(key) == current_value:
                control[changed_key] = previous_control.get(changed_key) or expected_as_of
            else:
                control[changed_key] = expected_as_of

    gate = entry_gate if isinstance(entry_gate, Mapping) else {}
    if (
        position.get("status") != "FLAT"
        or gate.get("status") != "ENTRY_ELIGIBLE"
        or not isinstance(reading, Mapping)
        or reading.get("setup_stage") != "ENTRY_ELIGIBLE"
    ):
        return
    setup_key = str(gate.get("setup_key") or "")
    if not setup_key:
        return
    for item in memory.get("active_setups", []):
        if not isinstance(item, dict) or str(item.get("setup_key") or "") != setup_key:
            continue
        expected_fields = {
            "direction": gate.get("direction"),
            "trigger_operator": gate.get("trigger_operator"),
            "trigger_level": gate.get("trigger_level"),
            "valid_bars": gate.get("valid_bars"),
        }
        if all(item.get(key) == value for key, value in expected_fields.items()):
            item["stage"] = "ENTRY_ELIGIBLE"
        return


def _program_taiji_text(context: Mapping[str, Any]) -> str:
    state = {
        "INSUFFICIENT": "段序不足",
        "FIRST_PUSH": "第1段定錨推進",
        "CORRECTION_FORMING": "修正形成中",
        "CORRECTION_HELD": "修正守住父代",
        "CORRECTION_DESTRUCTIVE": "修正已破壞父代",
        "COPY_FORMING": "同向複製形成中",
        "COPY_SUCCESS": "同向複製成功",
        "COPY_WEAKENED": "同向複製轉弱",
        "COPY_FAILED": "同向複製失敗",
    }.get(str(context.get("program_state")), "狀態未定")
    grade = "大級" if context.get("operating_grade") == "LARGE" else "小級"
    start_time = str(context.get("parent_start_time") or "")[11:16]
    end_time = str(context.get("parent_end_time") or "")[11:16]
    start_price = context.get("parent_start_price")
    end_price = context.get("parent_end_price")
    if start_time and end_time and isinstance(start_price, (int, float)) and isinstance(end_price, (int, float)):
        parent = f"；父代{start_time} {float(start_price):,.0f}→{end_time} {float(end_price):,.0f}"
    else:
        parent = ""
    quality = str(context.get("program_quality") or "UNDEFINED")
    return f"{grade}{state}{parent}；品質={quality}。"


def _program_yizhi_text(value: Any) -> str:
    state = value.get("state") if isinstance(value, Mapping) else None
    label = {
        "NONE": "未成立",
        "CENTRIFUGAL_CANDIDATE": "離心力候選",
        "CENTRIFUGAL_CONFIRMED": "離心力成立",
        "GOLD_DRAGON": "金龍動能成立",
        "K_GOLD_DRAGON": "K金龍動能成立",
        "MOMENTUM_CONTINUING": "一之動能延續",
        "MOMENTUM_FAILED": "一之動能失效",
        "COUNTERTREND_ACCELERATION": "反向加速觀察",
    }.get(str(state), "未成立")
    direction = {"BULL": "多方", "BEAR": "空方"}.get(
        str(value.get("direction")) if isinstance(value, Mapping) else "", ""
    )
    at = str(value.get("latest_time") or "")[11:16] if isinstance(value, Mapping) else ""
    suffix = f"（{at}）" if at else ""
    return f"{direction}{label}{suffix}；僅程式確認後才切換動能模式。"


def _program_left_right_text(value: Any) -> str:
    state = value.get("state") if isinstance(value, Mapping) else None
    direction = {"BULL": "多方", "BEAR": "空方"}.get(
        str(value.get("direction")) if isinstance(value, Mapping) else "", ""
    )
    label = {
        "NONE": "尚未進入左右反轉流程",
        "LEFT_CANDIDATE": "左側反向候選，尚未接管",
        "RIGHT_CONFIRMING": "右側確認中",
        "RIGHT_CONFIRMED": "右側反向錨已確認",
    }.get(str(state), "尚未進入左右反轉流程")
    return f"{direction}{label}。"


def _program_dow_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "大小級道氏未定。"
    labels = {"BULL": "偏多", "BEAR": "偏空", "UNDEFINED": "未定", "CONFLICT": "衝突"}
    large = labels.get(str(value.get("large_state")), str(value.get("large_state") or "未定"))
    small = labels.get(str(value.get("small_state")), str(value.get("small_state") or "未定"))
    return f"大級{large}；小級{small}；防線點位服從程式錨生命週期。"


def _program_x_process_text(
    stage: str,
    *,
    program_candidate: Any,
    entry_gate: Mapping[str, Any],
    position: Mapping[str, Any],
) -> str:
    if stage == "PREOPEN_CONTEXT":
        return "已整理盤前參考，等待本監控時段開盤證據。"
    if stage == "OPENING_EVIDENCE":
        return "蒐集開盤證據；尚未建立合格定錨。"
    if stage == "FIRST_ENDPOINT":
        return "等待第一組因果端點完成，未提前指定方向。"
    if stage == "ANCHOR_LENS_SELECTION":
        return "定錨已建立；依程式象限與太極狀態選主鏡頭。"
    if stage == "OPPORTUNITY_GRADING" and isinstance(program_candidate, Mapping):
        return f"程式候選已武裝：{program_candidate.get('setup_name') or program_candidate.get('setup_key')}。"
    if stage == "ENTRY_EXECUTION":
        return f"程式進場閘門={entry_gate.get('status')}；執行欄位不得由AI延後。"
    if stage == "POSITION_MANAGEMENT":
        return f"程式持倉={position.get('status')}；依固定停損與應有行為管理。"
    if stage == "RESET":
        return "原結構重置中；等待新定錨接管。"
    return "程式持續掃描因果結構。"


def _canonicalize_program_scenario_weights(
    scenario: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> None:
    """Own the displayed condition weights while leaving prose explanatory."""

    if not _program_only_policy(ledger):
        return
    methods = ledger.get("course_method_state")
    weights = methods.get("scenario_weights") if isinstance(methods, Mapping) else None
    if not (
        isinstance(weights, Mapping)
        and str(weights.get("authority")).startswith("PROGRAM_POLICY_")
    ):
        return
    mapping = {
        "bull_probability": "bull",
        "range_probability": "range",
        "bear_probability": "bear",
    }
    normalized: dict[str, int] = {}
    for target, source in mapping.items():
        value = weights.get(source)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            return
        normalized[target] = value
    if sum(normalized.values()) != 100:
        return
    scenario.update(normalized)


def _canonicalize_program_market_trends(
    analysis: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> None:
    if not _program_only_policy(ledger):
        return
    lifecycle = ledger.get("anchor_lifecycle")
    quadrant = lifecycle.get("quadrant_context") if isinstance(lifecycle, Mapping) else None
    if not (
        isinstance(lifecycle, Mapping)
        and isinstance(quadrant, Mapping)
        and str(quadrant.get("authority")).startswith("PROGRAM_POLICY_")
    ):
        return
    background = lifecycle.get("background_anchor")
    child = lifecycle.get("child_anchor")
    large_direction = _program_large_direction(lifecycle)
    controller = (
        child
        if isinstance(child, Mapping) and child.get("status") in {None, "ACTIVE"}
        else background
        if isinstance(background, Mapping) and background.get("status") in {None, "ACTIVE"}
        else None
    )
    if large_direction in {"BULL", "BEAR"}:
        direction = str(large_direction)
        correcting = quadrant.get("phase") == "CORRECTION" or quadrant.get(
            "background_trend_dynamics"
        ) == "DECREASING"
        classification = (
            "偏多但回檔" if direction == "BULL" and correcting
            else "強勢偏多" if direction == "BULL"
            else "偏空但反彈" if correcting
            else "強勢偏空"
        )
        analysis["large_trend"] = {
            "classification": classification,
            "details": [
                f"程式大級道氏={direction}；背景象限={quadrant.get('background_primary') or 'UNDEFINED'}。"
            ],
        }
    else:
        analysis["large_trend"] = {
            "classification": "盤整",
            "details": ["本監控時段尚無作用中大級錨。"],
        }

    # ``working_direction`` is the direction of the newest forming leg.  During
    # a pullback it is intentionally opposite to the active small-structure
    # controller, so it must not overwrite the public "current trend" label.
    # Use the course structure direction first and keep the leg direction as
    # separate context.  Otherwise a valid BEAR Q2/Q4 entry can be rendered as
    # "small bullish" merely because its final correction bar is rising.
    working_leg_direction = quadrant.get("working_direction")
    working_direction = quadrant.get("working_structure_direction")
    if working_direction not in {"BULL", "BEAR"} and isinstance(controller, Mapping):
        working_direction = controller.get("direction")
    if working_direction not in {"BULL", "BEAR"}:
        working_direction = working_leg_direction
    current_classification = {
        "BULL": "偏多",
        "BEAR": "偏空",
    }.get(str(working_direction), "轉換中" if isinstance(controller, Mapping) else "盤整")
    analysis["current_trend"] = {
        "classification": current_classification,
        "details": [
            f"程式小級控制方向={working_direction or 'UNDEFINED'}；"
            f"目前工作段方向={working_leg_direction or 'UNDEFINED'}；"
            f"工作象限={quadrant.get('working_primary') or 'UNDEFINED'}。"
        ],
    }


def _program_large_direction(lifecycle: Mapping[str, Any]) -> str | None:
    """Resolve the current program-owned large direction from one authority chain.

    A same-grade opposite ``GRADE_UPGRADE`` can be a local promotion candidate
    without taking control from an existing background anchor.  Likewise,
    ``BULL_WITH_BEAR_REVERSAL`` and ``BEAR_WITH_BULL_REVERSAL`` describe an
    intact direction with an opposing reversal candidate, not a neutral large
    structure.  Public trend labels and promoted-structure persistence must use
    the same resolved direction or they can contradict each other on one bar.
    """

    dow_context = lifecycle.get("dow_context")
    large_state = (
        str(dow_context.get("large_state") or "")
        if isinstance(dow_context, Mapping)
        else ""
    )
    dow_direction = {
        "BULL": "BULL",
        "BULL_WITH_BEAR_REVERSAL": "BULL",
        "BEAR": "BEAR",
        "BEAR_WITH_BULL_REVERSAL": "BEAR",
    }.get(large_state)
    if dow_direction is not None:
        return dow_direction

    background = lifecycle.get("background_anchor")
    if (
        isinstance(background, Mapping)
        and background.get("status") in {None, "ACTIVE", "DEGRADED", "DEGRADED_RECLAIMED"}
        and background.get("direction") in {"BULL", "BEAR"}
    ):
        return str(background["direction"])
    return None


def _valid_post_upgrade_background_quadrant(
    reading: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> bool:
    value = reading.get("background_quadrant")
    if value in {"Q1", "Q2", "Q3", "Q4"}:
        return True
    lifecycle = ledger.get("anchor_lifecycle")
    quadrant = lifecycle.get("quadrant_context") if isinstance(lifecycle, Mapping) else None
    # A program-only diagnostic can establish the promoted control grade before
    # it has enough deterministic two-axis evidence to name Q1..Q4. Preserve
    # UNKNOWN instead of fabricating a quadrant. AI_HYBRID remains responsible
    # for the course-level interpretation when evidence is exposed to a model.
    if (
        value in {"UNDEFINED", "TRANSITION"}
        and not _ai_hybrid_policy(ledger)
    ):
        return True
    if not (
        value == "TRANSITION"
        and isinstance(quadrant, Mapping)
        and str(quadrant.get("authority")).startswith("PROGRAM_POLICY_")
        and quadrant.get("background_primary") == "TRANSITION"
    ):
        return False
    candidates = quadrant.get("background_candidates")
    return (
        isinstance(candidates, list)
        and len(candidates) >= 2
        and all(item in {"Q1", "Q2", "Q3", "Q4"} for item in candidates)
    )


def _canonicalize_program_message_direction(
    analysis: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    ai_generated: bool = True,
) -> None:
    if not isinstance(ledger.get("program_trade_policy"), Mapping):
        return
    action = analysis.get("action")
    if isinstance(action, Mapping) and action.get("direction") in {"LONG", "SHORT"}:
        expected = "BULL" if action["direction"] == "LONG" else "BEAR"
        # The model already made the substantive ENTER decision.  Card
        # direction is a duplicated presentation field derived from that
        # decision, not a second opportunity to reverse it.
        analysis["message_direction"] = expected
        return
    if position.get("status") in {"LONG", "SHORT"}:
        expected = "BULL" if position["status"] == "LONG" else "BEAR"
        # Market bias may be neutral or bearish while a long is being managed,
        # but the card's subject remains the actual open position.
        analysis["message_direction"] = expected
        return
    if _ai_hybrid_policy(ledger):
        return
    levels = ledger.get("trade_levels")
    candidate = levels.get("continuation_arm_candidate") if isinstance(levels, Mapping) else None
    if isinstance(candidate, Mapping) and candidate.get("direction") in {"LONG", "SHORT"}:
        analysis["message_direction"] = "BULL" if candidate["direction"] == "LONG" else "BEAR"
        return
    lifecycle = ledger.get("anchor_lifecycle")
    child = lifecycle.get("child_anchor") if isinstance(lifecycle, Mapping) else None
    background = lifecycle.get("background_anchor") if isinstance(lifecycle, Mapping) else None
    controller = child if isinstance(child, Mapping) and child.get("status") == "ACTIVE" else background
    direction = controller.get("direction") if isinstance(controller, Mapping) else None
    analysis["message_direction"] = direction if direction in {"BULL", "BEAR"} else "NEUTRAL"


def _same_price(left: Any, right: Any, *, tolerance: float = 0.01) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def _canonicalize_taiji_parent_citation(
    reading: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> None:
    """Render the program-owned Taiji parent endpoints in one traceable form.

    The model still owns the Taiji interpretation after the first separator.
    Only the identity of the dynasty parent is normalized because its two
    endpoints already come from the causal anchor lifecycle.  This prevents
    repeated retries over prose such as ``09:25的44957`` that names the right
    values but omits their high/low roles.
    """

    lifecycle = ledger.get("anchor_lifecycle")
    context = lifecycle.get("taiji_context") if isinstance(lifecycle, Mapping) else None
    text = reading.get("taiji")
    if not isinstance(context, Mapping) or not isinstance(text, str) or "父代" not in text:
        return
    direction = str(context.get("parent_direction") or "")
    if direction not in {"BULL", "BEAR"}:
        return
    try:
        start_time = _aware(context.get("parent_start_time"), "parent_start_time").strftime("%H:%M")
        end_time = _aware(context.get("parent_end_time"), "parent_end_time").strftime("%H:%M")
    except (SemanticReplayError, ValueError):
        return
    start_price = context.get("parent_start_price")
    end_price = context.get("parent_end_price")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (start_price, end_price)):
        return
    start_role = str(context.get("parent_start_kind") or "")
    end_role = str(context.get("parent_end_kind") or "")
    for anchor_key in ("background_anchor", "child_anchor"):
        anchor = lifecycle.get(anchor_key) if isinstance(lifecycle, Mapping) else None
        if (
            isinstance(anchor, Mapping)
            and anchor.get("anchor_origin_kind") == "SESSION_OPEN"
            and str(anchor.get("origin_time")) == str(context.get("parent_start_time"))
            and _same_price(anchor.get("origin_price"), start_price)
        ):
            start_role = "SESSION_OPEN"
            break
    if start_role not in {"SESSION_OPEN", "HIGH", "LOW"}:
        start_role = "LOW" if direction == "BULL" else "HIGH"
    if end_role not in {"SESSION_OPEN", "HIGH", "LOW"}:
        end_role = "HIGH" if direction == "BULL" else "LOW"
    role_label = {"SESSION_OPEN": "開盤價", "HIGH": "高點", "LOW": "低點"}
    prefix = (
        f"父代為{start_time}{role_label[start_role]}{float(start_price):,.0f}點→"
        f"{end_time}{role_label[end_role]}{float(end_price):,.0f}點"
    )
    suffix = ""
    parent_index = text.find("父代")
    for separator in ("；", "。"):
        separator_index = text.find(separator, parent_index)
        if separator_index >= 0:
            suffix = text[separator_index + 1 :].strip()
            break
    reading["taiji"] = prefix + (f"；{suffix}" if suffix else "。")


def _validate_taiji_parent_role(
    reading: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> None:
    """Keep the dynasty parent distinct from later same-direction copies."""

    lifecycle = ledger.get("anchor_lifecycle")
    taiji_context = lifecycle.get("taiji_context") if isinstance(lifecycle, Mapping) else None
    text = reading.get("taiji")
    if not isinstance(taiji_context, Mapping) or not isinstance(text, str) or "父代" not in text:
        return
    start = taiji_context.get("parent_start_price")
    end = taiji_context.get("parent_end_price")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (start, end)):
        return
    claimed = {
        float(token.replace(",", ""))
        for token in re.findall(r"(?<![0-9:])[0-9][0-9,]*(?:\.[0-9]+)?", text)
    }
    if float(start) not in claimed or float(end) not in claimed:
        raise SemanticReplayError(
            "太極文字提到父代時，必須使用anchor_lifecycle.taiji_context的朝代父代"
            f"（{float(start):.1f}→{float(end):.1f}）；後續同向推進只能稱複製或比較基準。"
        )
    # Prices alone are not enough to identify a leg.  The same price can occur
    # at several session pivots, which previously allowed the model to pair a
    # stale 08:45 low with a current 09:25 low while still passing validation.
    # When the deterministic lifecycle supplies timestamps, require both
    # endpoints to be named in the prose as well.  This keeps the visible
    # Taiji parent traceable to one alternating high/low leg.
    expected_times: list[str] = []
    for key in ("parent_start_time", "parent_end_time"):
        raw = taiji_context.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            expected_times.append(_aware(raw, key).strftime("%H:%M"))
        except (SemanticReplayError, ValueError):
            continue
    direction = str(taiji_context.get("parent_direction") or "")
    start_role = str(taiji_context.get("parent_start_kind") or "")
    end_role = str(taiji_context.get("parent_end_kind") or "")
    for anchor_key in ("background_anchor", "child_anchor"):
        anchor = lifecycle.get(anchor_key) if isinstance(lifecycle, Mapping) else None
        if (
            isinstance(anchor, Mapping)
            and anchor.get("anchor_origin_kind") == "SESSION_OPEN"
            and str(anchor.get("origin_time")) == str(taiji_context.get("parent_start_time"))
            and _same_price(anchor.get("origin_price"), start)
        ):
            start_role = "SESSION_OPEN"
            break
    if start_role not in {"SESSION_OPEN", "HIGH", "LOW"}:
        start_role = "LOW" if direction == "BULL" else "HIGH" if direction == "BEAR" else ""
    if end_role not in {"SESSION_OPEN", "HIGH", "LOW"}:
        end_role = "HIGH" if direction == "BULL" else "LOW" if direction == "BEAR" else ""
    role_hint = {"SESSION_OPEN": "開盤價", "HIGH": "高點", "LOW": "低點", "": "端點"}
    endpoint_hint = f"{role_hint[start_role]}→{role_hint[end_role]}"
    missing_times = [item for item in expected_times if item not in text]
    if missing_times:
        raise SemanticReplayError(
            "太極文字提到父代時，必須同時引用程式父代的正確時間與價格"
            f"（缺少{','.join(missing_times)}；端點應為{endpoint_hint}），不得以同價位的舊樞紐代替。"
        )
    if len(expected_times) == 2:
        expected_kinds = (
            "開" if start_role == "SESSION_OPEN" else "高" if start_role == "HIGH" else "低" if start_role == "LOW" else None,
            "開" if end_role == "SESSION_OPEN" else "高" if end_role == "HIGH" else "低" if end_role == "LOW" else None,
        )

        def endpoint_is_associated(expected_time: str, expected_price: float, expected_kind: str | None) -> bool:
            # Associate a price with the closest preceding HH:MM token.  This
            # rejects prose such as "09:20高點08:45低45,135", which contains
            # every expected token globally but actually attaches 45,135 to
            # the stale 08:45 pivot.
            time_matches = list(re.finditer(r"(?<!\d)\d{2}:\d{2}(?!\d)", text))
            number_matches = list(re.finditer(r"(?<![0-9:])[0-9][0-9,]*(?:\.[0-9]+)?", text))
            for number in number_matches:
                try:
                    value = float(number.group(0).replace(",", ""))
                except ValueError:
                    continue
                if value != expected_price:
                    continue
                preceding = [item for item in time_matches if item.end() <= number.start()]
                if not preceding:
                    continue
                nearest = preceding[-1]
                if nearest.group(0) != expected_time or number.start() - nearest.end() > 24:
                    continue
                local = text[nearest.start() : min(len(text), number.end() + 5)]
                if expected_kind is None or expected_kind in local:
                    return True
            return False

        endpoints = (
            (expected_times[0], float(start), expected_kinds[0]),
            (expected_times[1], float(end), expected_kinds[1]),
        )
        if not all(endpoint_is_associated(*item) for item in endpoints):
            raise SemanticReplayError(
                "太極父代每個價位都必須緊鄰其正確時間與高低角色"
                f"（{expected_times[0]}→{expected_times[1]}，{endpoint_hint}）；"
                "不得插入另一個時間後再接同價位。"
            )


def _large_trend_for_dow(
    trend: Mapping[str, Any], dow_context: Mapping[str, Any]
) -> dict[str, Any]:
    large_state = dow_context.get("large_state")
    if large_state == "UNDEFINED":
        result = dict(trend)
        result["classification"] = "盤整"
        return result
    allowed = {
        "BULL": {"強勢偏多", "偏多但回檔"},
        "BEAR": {"強勢偏空", "偏空但反彈"},
    }.get(str(large_state))
    result = dict(trend)
    if not allowed or result.get("classification") in allowed:
        return result
    # The program owns the confirmed large Dow direction. A model may still
    # call the whole session a range while its details correctly describe the
    # active takeover. Normalize only that display label; never touch prices,
    # evidence or explanatory details.
    if result.get("classification") == "盤整":
        result["classification"] = "偏空但反彈" if large_state == "BEAR" else "偏多但回檔"
        return result
    raise SemanticReplayError("大趨勢必須服從程式化大級雙向道氏控制狀態。")


def _latest_active_promoted_structure(
    ledger: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return the newest still-active SMALL -> LARGE structure promotion."""

    events = [
        item
        for item in ledger.get("structure_events", [])
        if isinstance(item, Mapping)
    ]
    superseded = {
        str(item.get("source_upgrade_event_id"))
        for item in events
        if item.get("event_type") in {"GRADE_DOWNGRADE", "STRUCTURE_INVALIDATED"}
        and item.get("source_upgrade_event_id")
    }
    active = [
        item
        for item in events
        if item.get("event_type") == "GRADE_UPGRADE"
        and item.get("direction") in {"BULL", "BEAR"}
        and item.get("from_level") == "SMALL"
        and item.get("to_level") == "LARGE"
        and str(item.get("id")) not in superseded
    ]
    lifecycle = ledger.get("anchor_lifecycle")
    current_large_direction = (
        _program_large_direction(lifecycle)
        if isinstance(lifecycle, Mapping)
        else None
    )
    if current_large_direction in {"BULL", "BEAR"}:
        # A newer opposite-side local upgrade does not automatically replace an
        # already active background controller.  Only promotions aligned with
        # the current program-owned large direction may preserve its grade and
        # quadrant.  If none align, the anchor/Dow controller remains the
        # authority and there is no active promoted event to carry forward.
        active = [
            item for item in active
            if item.get("direction") == current_large_direction
        ]
    return max(
        active,
        key=lambda item: (str(item.get("first_seen_at") or ""), str(item.get("id") or "")),
        default=None,
    )


def _active_promoted_structure(
    reading: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    if reading.get("controlling_grade") != "LARGE":
        return None
    if _ai_hybrid_policy(ledger):
        reference = reading.get("large_anchor_ref")
        if not isinstance(reference, str) or not reference:
            return None
        return _active_promotion_for_anchor(reference, ledger=ledger)
    return _latest_active_promoted_structure(ledger)


def _active_promotion_for_anchor(
    reference: str,
    *,
    ledger: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return an unsuperseded grade upgrade that owns ``reference``."""

    events = [
        item
        for item in ledger.get("structure_events", [])
        if isinstance(item, Mapping)
    ]
    superseded = {
        str(item.get("source_upgrade_event_id"))
        for item in events
        if item.get("event_type") in {"GRADE_DOWNGRADE", "STRUCTURE_INVALIDATED"}
        and item.get("source_upgrade_event_id")
    }
    lifecycle = ledger.get("anchor_lifecycle")
    current_large_direction = (
        _program_large_direction(lifecycle)
        if isinstance(lifecycle, Mapping)
        else None
    )
    candidates = [
        item
        for item in events
        if item.get("event_type") == "GRADE_UPGRADE"
        and item.get("source_anchor_id") == reference
        and str(item.get("id")) not in superseded
        and (
            current_large_direction not in {"BULL", "BEAR"}
            or item.get("direction") not in {"BULL", "BEAR"}
            or item.get("direction") == current_large_direction
        )
    ]
    return max(
        candidates,
        key=lambda item: (str(item.get("first_seen_at") or ""), str(item.get("id") or "")),
        default=None,
    )


def _referenced_active_promotion(
    reading: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Resolve a durable upgrade reference without replaying its event card."""

    reference = reading.get("large_anchor_ref")
    event_ref = reading.get("structure_event_ref")
    if not isinstance(reference, str) or not reference or not event_ref:
        return None
    promotion = _active_promotion_for_anchor(reference, ledger=ledger)
    if not isinstance(promotion, Mapping) or promotion.get("id") != event_ref:
        return None
    return promotion


def _large_trend_for_promoted_structure(
    trend: Mapping[str, Any],
    reading: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep the public large trend aligned with program-owned grade control."""

    promotion = _active_promoted_structure(reading, ledger=ledger)
    result = dict(trend)
    if not isinstance(promotion, Mapping):
        return result
    direction = str(promotion.get("direction"))
    allowed = {
        "BULL": {"強勢偏多", "偏多但回檔"},
        "BEAR": {"強勢偏空", "偏空但反彈"},
    }[direction]
    # The course quadrant's trend axis describes whether the controlling
    # structure is strengthening or weakening; it is independent of bullish /
    # bearish direction.  Once a SMALL -> LARGE promotion owns the direction,
    # use that axis to distinguish continuation from correction.  Otherwise a
    # stale model label such as ``偏多但回檔`` can survive indefinitely even
    # while the promoted leg keeps making new highs.
    trend_dynamics = reading.get("background_trend_dynamics")
    if trend_dynamics == "INCREASING":
        result["classification"] = "強勢偏多" if direction == "BULL" else "強勢偏空"
        return result
    if trend_dynamics == "DECREASING":
        result["classification"] = "偏多但回檔" if direction == "BULL" else "偏空但反彈"
        return result
    if result.get("classification") in allowed:
        return result
    if result.get("classification") != "盤整":
        raise SemanticReplayError("大趨勢不得違反仍作用中的程式化結構升級方向。")

    first_seen = str(promotion.get("first_seen_at") or "")
    match = re.search(r"T(\d{2}:\d{2})", first_seen)
    time_text = match.group(1) if match else "先前"
    side = "多方" if direction == "BULL" else "空方"
    result["classification"] = "偏多但回檔" if direction == "BULL" else "偏空但反彈"
    result["details"] = [
        f"{time_text}{side}結構升級仍作用中；尚無降級或父級失效事件，"
        f"大級控制維持{side}。"
    ]
    return result


def _long_only_promoted_large_trend(
    trend: Mapping[str, Any],
    reading: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    ai_generated: bool = True,
) -> dict[str, Any]:
    """Keep an *AI-selected* grade-upgrade internally consistent.

    A program-detected promotion that the AI did not select is only evidence;
    it must not rewrite the model's large-trend judgment. Once the AI selects
    that event and switches to LARGE, however, its directional label, anchor
    reference and public trend must agree atomically.
    """

    result = dict(trend)
    selected_ref = reading.get("structure_event_ref")
    promotion = next(
        (
            item
            for item in ledger.get("structure_events", [])
            if isinstance(item, Mapping)
            and item.get("id") == selected_ref
            and item.get("event_type") == "GRADE_UPGRADE"
        ),
        None,
    )
    if not isinstance(promotion, Mapping):
        return result

    direction = str(promotion.get("direction"))
    if direction not in {"BULL", "BEAR"}:
        return result
    allowed = {
        "BULL": {"強勢偏多", "偏多但回檔"},
        "BEAR": {"強勢偏空", "偏空但反彈"},
    }[direction]
    trend_dynamics = reading.get("background_trend_dynamics")
    if _ai_hybrid_policy(ledger) and ai_generated:
        expected = None
        if trend_dynamics == "INCREASING":
            expected = "強勢偏多" if direction == "BULL" else "強勢偏空"
        elif trend_dynamics == "DECREASING":
            expected = "偏多但回檔" if direction == "BULL" else "偏空但反彈"
        if expected is not None and result.get("classification") != expected:
            if result.get("classification") in allowed:
                # The AI already selected the upgrade event, its direction and
                # the trend axis.  Strong-vs-pullback is therefore a derived
                # label; canonicalize only within the same chosen direction.
                result["classification"] = expected
            else:
                raise SemanticReplayError(
                    "AI採用結構升級事件後，大趨勢分類必須與事件方向及自己填寫的趨勢軸一致；"
                    "驗證器不會代替AI改寫。"
                )
        if expected is None and result.get("classification") not in allowed:
            raise SemanticReplayError(
                "AI採用結構升級事件後，大趨勢方向必須與自己選擇的事件一致。"
            )
        return result

    if trend_dynamics == "INCREASING":
        result["classification"] = "強勢偏多" if direction == "BULL" else "強勢偏空"
    elif trend_dynamics == "DECREASING":
        result["classification"] = "偏多但回檔" if direction == "BULL" else "偏空但反彈"
    elif result.get("classification") not in allowed:
        # Program-only output starts from a deliberately neutral presentation
        # shell.  Once its own GRADE_UPGRADE event is canonicalized, derive the
        # event direction even when the program quadrant axes are not available
        # yet.  The strict no-rewrite rule above still applies to AI_HYBRID.
        result["classification"] = "偏多但回檔" if direction == "BULL" else "偏空但反彈"

    pivots: dict[str, Mapping[str, Any]] = {}
    for key in ("pivots", "working_pivots"):
        for item in ledger.get(key, []):
            if isinstance(item, Mapping) and item.get("id"):
                pivots[str(item["id"])] = item

    def pivot_label(ref_key: str, price_key: str, fallback_role: str) -> str:
        pivot = pivots.get(str(promotion.get(ref_key) or ""))
        price = promotion.get(price_key)
        if isinstance(pivot, Mapping):
            raw_time = pivot.get("bar_time")
            time_text = ""
            if isinstance(raw_time, str):
                try:
                    time_text = _aware(raw_time, ref_key).strftime("%H:%M")
                except SemanticReplayError:
                    time_text = ""
            role = {"HIGH": "高", "LOW": "低"}.get(
                str(pivot.get("kind")), fallback_role
            )
            pivot_price = pivot.get("price")
            if isinstance(pivot_price, (int, float)) and not isinstance(pivot_price, bool):
                price = pivot_price
            if isinstance(price, (int, float)) and not isinstance(price, bool):
                return f"{time_text}{role}{float(price):,.0f}點"
        if isinstance(price, (int, float)) and not isinstance(price, bool):
            return f"{fallback_role}{float(price):,.0f}點"
        return "未揭露端點"

    direction_text = "多方" if direction == "BULL" else "空方"
    first_seen = str(promotion.get("first_seen_at") or "")
    try:
        seen_text = _aware(first_seen, "first_seen_at").strftime("%H:%M")
    except SemanticReplayError:
        seen_text = "先前"
    parent = pivot_label("parent_origin_pivot_id", "parent_origin_price", "起點")
    replacement = pivot_label(
        "replacement_defense_pivot_id",
        "replacement_defense_price",
        "防線",
    )
    result["details"] = [
        f"{seen_text}{direction_text}結構已由小級升級為大級；"
        f"父級起點{parent}，{replacement}為大級{direction_text}防線候選。",
        "此控制方向只由正式降級、父級失效或合法反向接管事件解除。",
    ]
    return result


def _large_grade_upgrade_quadrant_authority(
    reading: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> bool:
    """Allow a promoted structure, not a child anchor, to own the large axes.

    Course grade-upgrade and anchor confirmation answer different questions.  A
    confirmed upgrade promotes the structure-control lens to LARGE even when
    the anchor lifecycle has not yet created a background anchor.  The promoted
    lens persists until its own downgrade or parent invalidation; it is not a
    one-card permission that disappears on the next pair of bars.
    """

    return _active_promoted_structure(reading, ledger=ledger) is not None


def _ai_hybrid_expected_event_card_type(
    *,
    material_type: str,
    setup_stage: str,
    position_status: str,
    position_action: str,
    gate_is_eligible: bool,
) -> str:
    """Resolve event-card precedence without mixing flat and held contracts."""

    if position_status in {"LONG", "SHORT"}:
        if position_action in {"STOP", "EXIT"}:
            return position_action
        return "MANAGEMENT"
    if gate_is_eligible:
        return "PREPARATION"
    return {
        "FALSE_BREAK_RECLAIM": (
            "PREPARATION"
            if setup_stage
            in {
                "ARMED",
                "ENTRY_ELIGIBLE",
                "AGGRESSIVE_CONFIRMED",
                "CONSERVATIVE_CONFIRMED",
            }
            else "OBSERVATION"
        ),
        "GRADE_UPGRADE": "STRUCTURE_UPGRADE",
        "GRADE_DOWNGRADE": "STRUCTURE_DOWNGRADE",
        "STRUCTURE_INVALIDATED": "INVALIDATION",
    }[material_type]


def _previous_ai_hybrid_large_anchor_ref(
    previous_memory: Mapping[str, Any] | None,
    *,
    ledger: Mapping[str, Any],
    refs: Mapping[str, set[str]],
) -> str | None:
    if not _ai_hybrid_policy(ledger) or not isinstance(previous_memory, Mapping):
        return None
    if previous_memory.get("session_key") != ledger.get("session_key"):
        return None
    previous_control = previous_memory.get("structure_control")
    if not isinstance(previous_control, Mapping) or previous_control.get("controlling_grade") != "LARGE":
        return None
    reference = previous_control.get("active_large_anchor_ref")
    if reference not in refs["legs"]:
        return None
    return (
        str(reference)
        if isinstance(_active_promotion_for_anchor(str(reference), ledger=ledger), Mapping)
        else None
    )


def _active_directional_defense_refs(
    ledger: Mapping[str, Any],
    *,
    level: str,
) -> dict[str, str]:
    """Return the causal active Dow candidates for one structure grade.

    In AI_HYBRID the program publishes observable, same-grade candidates; it
    does not decide which side currently controls the course interpretation.
    Keeping the direction beside each reference also prevents a bearish risk
    line from being rejected merely because the currently adopted child anchor
    is bullish (and vice versa).
    """

    lifecycle = ledger.get("anchor_lifecycle")
    dow_context = lifecycle.get("dow_context") if isinstance(lifecycle, Mapping) else None
    result: dict[str, str] = {}
    if isinstance(dow_context, Mapping):
        for direction, suffix in (("BULL", "bull"), ("BEAR", "bear")):
            defense = dow_context.get(f"{level}_{suffix}_defense")
            if not (
                isinstance(defense, Mapping)
                and defense.get("state") == "ACTIVE"
                and defense.get("id")
            ):
                continue
            result[direction] = str(defense["id"])

    if level == "large":
        events = [
            item
            for item in ledger.get("structure_events", [])
            if isinstance(item, Mapping)
        ]
        superseded = {
            str(item.get("source_upgrade_event_id"))
            for item in events
            if item.get("event_type") in {"GRADE_DOWNGRADE", "STRUCTURE_INVALIDATED"}
            and item.get("source_upgrade_event_id")
        }
        promotions = [
            item
            for item in events
            if item.get("event_type") == "GRADE_UPGRADE"
            and item.get("from_level") == "SMALL"
            and item.get("to_level") == "LARGE"
            and item.get("direction") in {"BULL", "BEAR"}
            and item.get("replacement_defense_pivot_id")
            and str(item.get("id") or "") not in superseded
        ]
        for promotion in sorted(
            promotions,
            key=lambda item: (str(item.get("first_seen_at") or ""), str(item.get("id") or "")),
        ):
            result[str(promotion["direction"])] = str(
                promotion["replacement_defense_pivot_id"]
            )
    return result


def _reading_v3(
    value: Any,
    *,
    ledger: Mapping[str, Any],
    previous_memory: Mapping[str, Any] | None = None,
    ai_generated: bool = True,
) -> dict[str, Any]:
    lifecycle_control = ledger.get("anchor_control")
    allowed_shapes = (
        {frozenset(READING_V5_KEYS), frozenset(READING_V34_KEYS)}
        if isinstance(lifecycle_control, Mapping)
        else {frozenset(READING_V3_KEYS), frozenset(READING_V4_KEYS)}
    )
    if not isinstance(value, Mapping) or set(value) not in allowed_shapes:
        raise SemanticReplayError("course_reading v3格式無效。")
    refs = allowed_references(ledger)
    result = dict(value)
    if isinstance(lifecycle_control, Mapping) and _ai_hybrid_policy(ledger):
        selected_upgrade_anchor_ref = None
        previous_large_anchor_ref = _previous_ai_hybrid_large_anchor_ref(
            previous_memory,
            ledger=ledger,
            refs=refs,
        )
        selected_structure_ref = value.get("structure_event_ref")
        if selected_structure_ref is not None:
            for item in ledger.get("structure_events", []):
                if (
                    isinstance(item, Mapping)
                    and item.get("id") == selected_structure_ref
                    and item.get("event_type") == "GRADE_UPGRADE"
                ):
                    # The causal upgrade event is itself the authority for its
                    # source leg on the decision turn. That source can have
                    # disappeared from the generic current-leg collection
                    # after the lifecycle switched child anchors, so requiring
                    # membership in refs["legs"] made the event impossible to
                    # adopt. Use the same installed-background-first identity
                    # rule as every later upgrade validator.
                    selected_upgrade_anchor_ref = _upgrade_controlling_anchor_ref(
                        item,
                        ledger=ledger,
                    )
                    break
        exact = {
            "large_anchor_ref": "active_background_anchor_ref",
            "small_anchor_ref": "active_child_anchor_ref",
            "working_anchor_ref": "working_leg_ref",
            "reverse_anchor_candidate_ref": "reverse_anchor_candidate_ref",
        }
        for key, control_key in exact.items():
            reference = value[key]
            candidate_ref = lifecycle_control.get(control_key)
            allowed_role_refs = {None, candidate_ref}
            # AI_HYBRID deliberately leaves anchor_control empty until the
            # model accepts a newly observed grade upgrade.  On that decision
            # turn the event's source anchor is the causal large-anchor choice.
            if key == "large_anchor_ref" and selected_upgrade_anchor_ref is not None:
                allowed_role_refs.add(selected_upgrade_anchor_ref)
            # A still-active promotion may be adopted on a later scheduled AI
            # turn without replaying the old GRADE_UPGRADE event card.  Its
            # source anchor remains an auditable large-control option until a
            # downgrade/invalidation supersedes that exact promotion.
            if (
                key == "large_anchor_ref"
                and isinstance(reference, str)
                and isinstance(
                    _active_promotion_for_anchor(reference, ledger=ledger),
                    Mapping,
                )
            ):
                allowed_role_refs.add(reference)
            if key == "large_anchor_ref" and previous_large_anchor_ref is not None:
                allowed_role_refs.add(previous_large_anchor_ref)
                # A missing repeated ID is a transcription omission, not a new
                # market decision.  Keep the already validated large anchor
                # while its promotion remains active.
                reference = _long_only_persisted_large_anchor_ref(
                    reference,
                    previous_large_anchor_ref,
                    ledger=ledger,
                )
            if reference not in allowed_role_refs:
                allowed_text = ", ".join(
                    "null" if item is None else str(item)
                    for item in sorted(
                        allowed_role_refs,
                        key=lambda item: (item is not None, str(item or "")),
                    )
                )
                raise SemanticReplayError(
                    f"AI_HYBRID的{key}只能採用對應程式候選或null，不得改接其他歷史波段；"
                    f"本輪允許值：{allowed_text}；收到：{reference}。"
                )
            result[key] = reference
        for key, level, control_key in (
            ("large_defense_ref", "large", "large_defense_ref"),
            ("small_defense_ref", "small", "small_defense_ref"),
        ):
            reference = value[key]
            directional_refs = set(
                _active_directional_defense_refs(ledger, level=level).values()
            )
            program_role_ref = lifecycle_control.get(control_key)
            if program_role_ref is not None:
                directional_refs.add(str(program_role_ref))
            if reference is not None and reference not in directional_refs:
                raise SemanticReplayError(
                    f"AI_HYBRID的{key}只能採用同級作用中多方／空方防線候選或null，"
                    "不得改接失效或其他級數防線。"
                )
            result[key] = reference
    elif isinstance(lifecycle_control, Mapping):
        exact = {
            "large_anchor_ref": "active_background_anchor_ref",
            "small_anchor_ref": "active_child_anchor_ref",
            "working_anchor_ref": "working_leg_ref",
            "reverse_anchor_candidate_ref": "reverse_anchor_candidate_ref",
            "large_defense_ref": "large_defense_ref",
            "small_defense_ref": "small_defense_ref",
        }
        for key, control_key in exact.items():
            reference = value[key]
            if reference != lifecycle_control.get(control_key):
                raise SemanticReplayError(f"{key}必須逐字引用程式錨生命週期的{control_key}。")
            result[key] = reference
    else:
        for key in ("large_anchor_ref", "small_anchor_ref", "working_anchor_ref"):
            reference = value[key]
            if reference is not None and reference not in refs["legs"]:
                raise SemanticReplayError(f"{key}必須引用本輪程式證據帳本內的段ID或null。")
            result[key] = reference
        for key in ("large_defense_ref", "small_defense_ref"):
            reference = value[key]
            if reference is not None and reference not in refs["defenses"]:
                raise SemanticReplayError(f"{key}必須引用作用中道氏防線ID或null。")
            result[key] = reference
    structure_ref = value["structure_event_ref"]
    if structure_ref is not None and structure_ref not in refs["structure_events"]:
        raise SemanticReplayError("structure_event_ref必須引用程式結構事件或null。")
    result["structure_event_ref"] = structure_ref
    result["controlling_grade"] = _enum(value["controlling_grade"], {"LARGE", "SMALL", "UNDEFINED"}, "controlling_grade")
    if _ai_hybrid_policy(ledger) and ai_generated and result["controlling_grade"] == "LARGE":
        large_ref = result.get("large_anchor_ref")
        program_background_ref = (
            lifecycle_control.get("active_background_anchor_ref")
            if isinstance(lifecycle_control, Mapping)
            else None
        )
        has_program_background = bool(
            isinstance(large_ref, str)
            and large_ref
            and large_ref == program_background_ref
        )
        has_adopted_promotion = bool(
            isinstance(large_ref, str)
            and large_ref
            and isinstance(_active_promotion_for_anchor(large_ref, ledger=ledger), Mapping)
        )
        if not has_program_background and not has_adopted_promotion:
            raise SemanticReplayError(
                "AI_HYBRID不得只靠controlling_grade=LARGE宣稱大級控制；"
                "必須引用作用中程式背景錨或已採用且未失效的結構升級來源錨。"
            )
    result["grade_relation"] = _enum(value["grade_relation"], {"ALIGNED", "CONFLICT", "ONLY_LARGE", "ONLY_SMALL", "UNDEFINED"}, "grade_relation")
    quadrants = {"Q1", "Q2", "Q3", "Q4", "TRANSITION", "UNDEFINED"}
    for key in ("background_quadrant", "working_quadrant", "primary_quadrant_candidate", "secondary_quadrant_candidate"):
        result[key] = _enum(value[key], quadrants, key)
    for key in ("background_trend_dynamics", "working_trend_dynamics"):
        result[key] = _enum(value[key], {"INCREASING", "DECREASING", "UNCLEAR"}, key)
    for key in ("background_volatility_dynamics", "working_volatility_dynamics"):
        result[key] = _enum(value[key], {"EXPANDING", "CONTRACTING", "UNSTABLE", "UNCLEAR"}, key)
    _canonicalize_unavailable_background_quadrant(
        result,
        ledger=ledger,
        ai_generated=ai_generated,
    )
    methods = value["focus_methods"]
    allowed_methods = {"QUADRANT", "TAIJI", "YIZHI", "LEFT_RIGHT", "DOW", "X_PROCESS"}
    if not isinstance(methods, list) or not 1 <= len(methods) <= 2 or len(set(methods)) != len(methods):
        raise SemanticReplayError("focus_methods必須是1至2個不重複的主鏡頭。")
    if any(item not in allowed_methods for item in methods):
        raise SemanticReplayError("focus_methods含無效戰法。")
    result["focus_methods"] = list(methods)
    expected_background = {
        ("INCREASING", "EXPANDING"): "Q1",
        ("INCREASING", "CONTRACTING"): "Q4",
        ("DECREASING", "EXPANDING"): "Q2",
        ("DECREASING", "CONTRACTING"): "Q3",
    }.get((result["background_trend_dynamics"], result["background_volatility_dynamics"]))
    if expected_background and result["background_quadrant"] != expected_background:
        raise SemanticReplayError("大級背景象限與大級趨勢／波動兩軸不一致。")
    expected_working = {
        ("INCREASING", "EXPANDING"): "Q1",
        ("INCREASING", "CONTRACTING"): "Q4",
        ("DECREASING", "EXPANDING"): "Q2",
        ("DECREASING", "CONTRACTING"): "Q3",
    }.get((result["working_trend_dynamics"], result["working_volatility_dynamics"]))
    if expected_working and expected_working not in {
        result["working_quadrant"], result["primary_quadrant_candidate"]
    }:
        raise SemanticReplayError("小級工作象限與小級趨勢／波動兩軸不一致。")
    lifecycle = ledger.get("anchor_lifecycle")
    anchor_quadrant = lifecycle.get("quadrant_context") if isinstance(lifecycle, Mapping) else None
    if isinstance(anchor_quadrant, Mapping) and not _ai_hybrid_policy(ledger):
        if anchor_quadrant.get("authority") == "EVIDENCE_ONLY":
            promoted_structure_controls_large = _large_grade_upgrade_quadrant_authority(
                result,
                ledger=ledger,
            )
            if (
                not isinstance(lifecycle.get("background_anchor"), Mapping)
                and result["background_quadrant"] not in {"UNDEFINED", "TRANSITION"}
                and not promoted_structure_controls_large
            ):
                raise SemanticReplayError("尚無本時段大級錨，不得把小級象限填成大級。")
        expected = {
            "background_quadrant": anchor_quadrant.get("background_primary"),
            "background_trend_dynamics": anchor_quadrant.get("background_trend_dynamics"),
            "background_volatility_dynamics": anchor_quadrant.get("background_volatility_dynamics"),
            "working_quadrant": anchor_quadrant.get("working_primary"),
            "working_trend_dynamics": anchor_quadrant.get("working_trend_dynamics"),
            "working_volatility_dynamics": anchor_quadrant.get("working_volatility_dynamics"),
        }
        for key, expected_value in expected.items():
            if expected_value is not None and result[key] != expected_value:
                raise SemanticReplayError(f"{key}必須服從作用中定錨後的程式化象限證據。")
        candidates = list(anchor_quadrant.get("working_candidates") or [])
        if candidates and result["primary_quadrant_candidate"] != candidates[0]:
            raise SemanticReplayError("primary_quadrant_candidate必須引用定錨後的首要發展候選。")
        if len(candidates) > 1 and result["secondary_quadrant_candidate"] != candidates[1]:
            raise SemanticReplayError("secondary_quadrant_candidate必須引用定錨後的次要發展候選。")
    elif not _ai_hybrid_policy(ledger):
        quadrant_evidence = ledger.get("quadrant_evidence")
        quadrant_evidence = quadrant_evidence if isinstance(quadrant_evidence, Mapping) else {}
        for window_key, prefix, label in (
            ("background_20bars", "background", "大級背景"),
            ("working_5bars", "working", "小級工作"),
        ):
            baseline = quadrant_evidence.get(window_key)
            if not isinstance(baseline, Mapping):
                continue
            recommended_trend = baseline.get("recommended_trend_dynamics")
            if recommended_trend in {"INCREASING", "DECREASING"} and result[f"{prefix}_trend_dynamics"] != recommended_trend:
                raise SemanticReplayError(f"{label}趨勢軸必須符合程式的同尺度確定性基線。")
            recommended_volatility = baseline.get("recommended_volatility_dynamics")
            if recommended_volatility in {"EXPANDING", "CONTRACTING", "UNSTABLE"} and result[f"{prefix}_volatility_dynamics"] != recommended_volatility:
                raise SemanticReplayError(f"{label}波動軸必須符合程式的同尺度確定性基線。")
            recommended_quadrant = baseline.get("recommended_quadrant")
            if recommended_quadrant in {"Q1", "Q2", "Q3", "Q4"} and result[f"{prefix}_quadrant"] != recommended_quadrant:
                raise SemanticReplayError(f"{label}象限必須符合程式的同尺度確定性基線。")
    for key in ("taiji", "yizhi", "left_right", "dow", "primary_lens", "main_strategy"):
        result[key] = _text(value[key], key, 240)
    if "main_strategy_family" in value:
        result["main_strategy_family"] = _enum(
            value["main_strategy_family"],
            {"NONE", "Q2", "Q4"},
            "main_strategy_family",
        )
    if "x_process" in value:
        result["cclass_mode"] = _enum(
            value["cclass_mode"],
            {"TAIJI_ORDERED", "YIZHI_MOMENTUM", "UNORDERED", "RESETTING", "UNDEFINED"},
            "cclass_mode",
        )
        result["x_stage"] = _enum(
            value["x_stage"],
            {
                "PREOPEN_CONTEXT", "OPENING_EVIDENCE", "FIRST_ENDPOINT", "ANCHOR_LENS_SELECTION",
                "OPPORTUNITY_GRADING", "ENTRY_EXECUTION", "POSITION_MANAGEMENT", "RESET",
            },
            "x_stage",
        )
        result["x_process"] = _text(value["x_process"], "x_process", 240)
        stale_anchor_conflict = _stale_anchor_opposes_current_dow(lifecycle)
        if stale_anchor_conflict and result["cclass_mode"] == "TAIJI_ORDERED":
            raise SemanticReplayError(
                "舊小錨已降級且目前小級道氏由反向控制；新反向錨尚未形成前，C班模式必須維持RESETTING／UNORDERED，不得把反彈稱為舊方向太極有序複製。"
            )
        if stale_anchor_conflict and _contains_positive_stale_copy_claim(result["taiji"]):
            raise SemanticReplayError(
                "舊小錨已降級且反向道氏控制時，目前反彈只能稱反向修正／重建嘗試；不得稱形成中的同向或舊方向太極複製。"
            )
    result["setup_stage"] = _enum(
        value["setup_stage"],
        {"NONE", "FORMING", "ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED", "INVALIDATED", "NO_CHASE"},
        "setup_stage",
    )
    result["strategy_reason"] = _text_list(value["strategy_reason"], "strategy_reason", 3)
    return result


def _background_quadrant_lacks_authority(
    reading: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
) -> bool:
    lifecycle = ledger.get("anchor_lifecycle")
    if not isinstance(lifecycle, Mapping):
        return False
    quadrant_context = lifecycle.get("quadrant_context")
    if _ai_hybrid_policy(ledger):
        return bool(
            reading.get("large_anchor_ref") is None
            and not _large_grade_upgrade_quadrant_authority(reading, ledger=ledger)
        )
    return bool(
        isinstance(quadrant_context, Mapping)
        and quadrant_context.get("authority") == "EVIDENCE_ONLY"
        and not isinstance(lifecycle.get("background_anchor"), Mapping)
        and not _large_grade_upgrade_quadrant_authority(reading, ledger=ledger)
    )


def _canonicalize_unavailable_background_quadrant(
    reading: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    ai_generated: bool = True,
) -> None:
    """Enforce the absence of a large lens without rewriting hybrid semantics."""

    if not _background_quadrant_lacks_authority(reading, ledger=ledger):
        return
    if _ai_hybrid_policy(ledger) and ai_generated:
        # Absence of a large anchor/upgrade is an objective reference fact.
        # Clearing an invented background quadrant does not choose a quadrant
        # for the model; it prevents a large-scale conclusion that has no
        # causal structure to refer to.
        reading["background_quadrant"] = "UNDEFINED"
        reading["background_trend_dynamics"] = "UNCLEAR"
        reading["background_volatility_dynamics"] = "UNCLEAR"
        return
    reading["background_quadrant"] = "UNDEFINED"
    reading["background_trend_dynamics"] = "UNCLEAR"
    reading["background_volatility_dynamics"] = "UNCLEAR"


def _canonicalize_unavailable_background_quadrant_memory(
    analysis: Mapping[str, Any],
    memory: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    previous_memory: Mapping[str, Any] | None,
    expected_as_of: str,
    ai_generated: bool = True,
) -> None:
    """Mirror the deterministic no-large-anchor fact into replay memory."""

    reading = analysis.get("course_reading")
    control = memory.get("structure_control")
    if not (
        isinstance(reading, Mapping)
        and isinstance(control, dict)
        and _background_quadrant_lacks_authority(reading, ledger=ledger)
    ):
        return
    if _ai_hybrid_policy(ledger) and ai_generated:
        control["background_quadrant"] = reading.get("background_quadrant")
        return
    control["background_quadrant"] = "UNDEFINED"
    previous_control = (
        previous_memory.get("structure_control")
        if isinstance(previous_memory, Mapping)
        and previous_memory.get("session_key") == memory.get("session_key")
        else None
    )
    if (
        isinstance(previous_control, Mapping)
        and previous_control.get("background_quadrant") == "UNDEFINED"
        and isinstance(previous_control.get("background_quadrant_changed_at"), str)
    ):
        control["background_quadrant_changed_at"] = previous_control[
            "background_quadrant_changed_at"
        ]
    else:
        control["background_quadrant_changed_at"] = expected_as_of


def _canonicalize_pre_structure_working_quadrant(
    analysis: dict[str, Any],
    memory: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    previous_memory: Mapping[str, Any] | None,
    expected_as_of: str,
    ai_generated: bool = True,
) -> None:
    """Keep a newly established anchor from becoming a finished quadrant.

    The X course starts Taiji/quadrant lens selection only after the anchor has
    at least one *completed* same-grade post-anchor leg.  A forming correction
    is useful evidence for the next quadrant, but it is not yet a completed
    relative comparison.  Preserve the analyzer's two-axis result as a
    candidate while making the public working state explicitly transitional.
    """

    if _ai_hybrid_policy(ledger) and ai_generated:
        # With evidence-only authority, the model owns early probabilistic
        # quadrant/Taiji interpretation.  Replacing it with TRANSITION here
        # would silently turn the validator into a second analyst.
        return

    lifecycle = ledger.get("anchor_lifecycle")
    if not isinstance(lifecycle, Mapping):
        return
    quadrant = lifecycle.get("quadrant_context")
    if not (
        isinstance(quadrant, Mapping)
        and quadrant.get("authority") == "EVIDENCE_ONLY"
        and (
            isinstance(lifecycle.get("background_anchor"), Mapping)
            or isinstance(lifecycle.get("child_anchor"), Mapping)
        )
    ):
        return
    comparisons = quadrant.get("same_grade_comparisons")
    legs = comparisons.get("legs") if isinstance(comparisons, Mapping) else None
    has_completed_post_anchor_leg = any(
        isinstance(item, Mapping)
        and item.get("status") in {"LOCAL_CONFIRMED", "PAIRED_CONFIRMED", "CONFIRMED"}
        for item in legs or []
    )
    if has_completed_post_anchor_leg:
        return

    reading = analysis.get("course_reading")
    control = memory.get("structure_control")
    if not isinstance(reading, dict) or not isinstance(control, dict):
        return

    prior_working = str(reading.get("working_quadrant") or "UNDEFINED")
    if prior_working in {"Q1", "Q2", "Q3", "Q4"}:
        if reading.get("primary_quadrant_candidate") in {"UNDEFINED", "TRANSITION", None}:
            reading["primary_quadrant_candidate"] = prior_working
    reading["working_quadrant"] = "TRANSITION"
    reading["focus_methods"] = ["X_PROCESS"]
    if reading.get("cclass_mode") == "TAIJI_ORDERED":
        reading["cclass_mode"] = "UNDEFINED"
    reading["taiji"] = (
        "太極：定錨後尚無完成的同級修正或推進；目前只記錄形成中的工作段，"
        "不以太極複製／修正作主判讀。"
    )
    reading["primary_lens"] = (
        "作用中錨已建立，但錨後同級轉折尚未完成；本輪只用定錨與開盤證據，"
        "象限僅列發展候選。"
    )

    previous_control = (
        previous_memory.get("structure_control")
        if isinstance(previous_memory, Mapping)
        and previous_memory.get("session_key") == memory.get("session_key")
        else None
    )
    previous_quadrant = (
        previous_control.get("working_quadrant")
        if isinstance(previous_control, Mapping)
        else None
    )
    control["working_quadrant"] = "TRANSITION"
    if previous_quadrant == "TRANSITION" and isinstance(previous_control, Mapping):
        control["working_quadrant_changed_at"] = previous_control.get(
            "working_quadrant_changed_at", expected_as_of
        )
    else:
        control["working_quadrant_changed_at"] = expected_as_of


def _ordered_taiji_conflicts_with_lifecycle(lifecycle: Any, cclass_mode: str) -> bool:
    return cclass_mode == "TAIJI_ORDERED" and _stale_anchor_opposes_current_dow(lifecycle)


def _stale_anchor_opposes_current_dow(lifecycle: Any) -> bool:
    if not isinstance(lifecycle, Mapping):
        return False
    lifecycle_dow = lifecycle.get("dow_context")
    lifecycle_child = lifecycle.get("child_anchor")
    lifecycle_reverse = lifecycle.get("reverse_candidate")
    return bool(
        isinstance(lifecycle_dow, Mapping)
        and isinstance(lifecycle_child, Mapping)
        and str(lifecycle_child.get("status") or "").startswith("DEGRADED")
        and lifecycle_dow.get("small_state") in {"BULL", "BEAR"}
        and lifecycle_dow.get("small_state") != lifecycle_child.get("direction")
        and not isinstance(lifecycle_reverse, Mapping)
    )


def _contains_positive_stale_copy_claim(text: str) -> bool:
    pattern = re.compile(r"形成中(?:的)?(?:多方|空方|同向)?複製|形成中的?(?:多方|空方|同向)?複製|同向複製")
    for clause in re.split(r"[。；;]", text):
        match = pattern.search(clause)
        if not match:
            continue
        prefix = clause[: match.start()]
        if re.search(r"(?:不是|不能|不得|不可|非).{0,24}$", prefix):
            continue
        return True
    return False


def _scenario_v3(value: Any) -> dict[str, Any]:
    keys = {"bull_probability", "range_probability", "bear_probability", "bull_plan", "range_plan", "bear_plan", "view_change"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise SemanticReplayError("scenario v3格式無效。")
    numbers: dict[str, int] = {}
    for key in ("bull_probability", "range_probability", "bear_probability"):
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 100:
            raise SemanticReplayError(f"scenario.{key}必須為0至100整數。")
        numbers[key] = item
    if sum(numbers.values()) != 100:
        raise SemanticReplayError("多方、盤整、空方情境權重合計必須為100。")
    return {
        **numbers,
        "bull_plan": _text(value["bull_plan"], "scenario.bull_plan", 300),
        "range_plan": _text(value["range_plan"], "scenario.range_plan", 300),
        "bear_plan": _text(value["bear_plan"], "scenario.bear_plan", 300),
        "view_change": _text(value["view_change"], "scenario.view_change", 300),
    }


def _preserve_active_session_anchor_probability(
    scenario: Mapping[str, Any], *, ledger: Mapping[str, Any]
) -> dict[str, Any]:
    """Compatibility hook: keep model weights; an intact anchor is not a bound.

    Numeric validity is checked by _scenario_v3. Never silently exchange the
    bull/bear numbers while leaving the accompanying analysis unchanged.
    """
    return dict(scenario)


def _action_v3(value: Any) -> dict[str, Any]:
    keys = {
        "position_action", "direction", "entry_role", "setup_key", "observation_area", "trigger",
        "entry", "structural_stop", "stop_price", "obstacles", "expected_behavior", "max_wait_bars",
        "no_chase", "management", "reentry_status",
    }
    v4_keys = keys | {"entry_rejection_reason"}
    if not isinstance(value, Mapping) or set(value) not in {frozenset(keys), frozenset(v4_keys)}:
        raise SemanticReplayError("action v3格式無效。")
    result = dict(value)
    result["position_action"] = _enum(value["position_action"], {"NONE", "ENTER", "STOP", "EXIT"}, "position_action")
    result["direction"] = _enum(value["direction"], {"NONE", "LONG", "SHORT"}, "direction")
    result["entry_role"] = _enum(value["entry_role"], {"NOT_APPLICABLE", "INITIAL", "REENTRY"}, "entry_role")
    setup_key = value["setup_key"]
    result["setup_key"] = None if setup_key is None else _text(setup_key, "action.setup_key", 120)
    for key in ("observation_area", "trigger", "entry", "structural_stop", "expected_behavior", "no_chase", "management"):
        result[key] = _text(value[key], f"action.{key}", 240)
    stop = value["stop_price"]
    if stop is not None and (isinstance(stop, bool) or not isinstance(stop, (int, float)) or stop <= 0):
        raise SemanticReplayError("action.stop_price必須為正數或null。")
    result["stop_price"] = None if stop is None else float(stop)
    obstacles = value["obstacles"]
    if not isinstance(obstacles, list) or len(obstacles) > 3:
        raise SemanticReplayError("action.obstacles最多3項。")
    result["obstacles"] = [_obstacle(item) for item in obstacles]
    wait = value["max_wait_bars"]
    if wait is not None and (isinstance(wait, bool) or not isinstance(wait, int) or not 1 <= wait <= 10):
        raise SemanticReplayError("action.max_wait_bars必須為1至10或null。")
    result["max_wait_bars"] = wait
    result["reentry_status"] = _enum(value["reentry_status"], {"NOT_APPLICABLE", "WAIT_ONE_BAR", "AVAILABLE", "USED"}, "reentry_status")
    if "entry_rejection_reason" in value:
        result["entry_rejection_reason"] = _enum(
            value["entry_rejection_reason"],
            {"NONE", *ENTRY_DENIAL_REASONS},
            "entry_rejection_reason",
        )
    if result["position_action"] == "ENTER":
        if result["direction"] not in {"LONG", "SHORT"} or result["stop_price"] is None:
            raise SemanticReplayError("ENTER必須提供方向與stop_price。")
    elif result["direction"] != "NONE" or result["entry_role"] != "NOT_APPLICABLE":
        raise SemanticReplayError("非ENTER事件的direction必須為NONE且entry_role為NOT_APPLICABLE。")
    return result


def _obstacle(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"price", "role", "label", "reaction"}:
        raise SemanticReplayError("obstacle格式無效。")
    price = value["price"]
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        raise SemanticReplayError("obstacle.price必須為正數。")
    return {
        "price": float(price),
        "role": _enum(value["role"], {"TRIGGER", "CHECKPOINT", "HARD_TARGET"}, "obstacle.role"),
        "label": _text(value["label"], "obstacle.label", 80),
        "reaction": _text(value["reaction"], "obstacle.reaction", 180),
    }


def _trend(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"classification", "details"}:
        raise SemanticReplayError(f"{field}格式無效。")
    allowed = (
        {"強勢偏多", "偏多但回檔", "盤整", "偏空但反彈", "強勢偏空", "資料不足"}
        if field == "large_trend"
        else {"偏多", "偏空", "盤整", "轉換中"}
    )
    return {
        "classification": _enum(value["classification"], allowed, f"{field}.classification"),
        "details": _text_list(value["details"], f"{field}.details", 3),
    }


def _reading(value: Any, *, ledger: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != READING_KEYS:
        raise SemanticReplayError("course_reading格式無效。")
    refs = allowed_references(ledger)
    result = dict(value)
    for key, collection in (
        ("large_anchor_ref", "legs"),
        ("small_anchor_ref", "legs"),
        ("large_defense_ref", "defenses"),
        ("small_defense_ref", "defenses"),
    ):
        reference = value[key]
        if reference is not None and reference not in refs[collection]:
            raise SemanticReplayError(f"{key}必須引用本輪程式證據帳本內的ID或null。")
        result[key] = reference
    result["quadrant"] = _enum(value["quadrant"], {"Q1", "Q2", "Q3", "Q4", "TRANSITION", "UNDEFINED"}, "quadrant")
    for key in ("taiji", "yizhi", "left_right", "dow", "primary_lens", "main_strategy"):
        result[key] = _text(value[key], key, 360)
    result["setup_stage"] = _enum(
        value["setup_stage"],
        {"NONE", "FORMING", "ARMED", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED", "INVALIDATED", "NO_CHASE"},
        "setup_stage",
    )
    result["strategy_reason"] = _text_list(value["strategy_reason"], "strategy_reason", 4)
    return result


def _scenario(value: Any) -> dict[str, Any]:
    keys = {"bull_probability", "range_probability", "bear_probability", "primary", "alternative", "view_change"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise SemanticReplayError("scenario格式無效。")
    numbers: dict[str, int] = {}
    for key in ("bull_probability", "range_probability", "bear_probability"):
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 100:
            raise SemanticReplayError(f"scenario.{key}必須為0至100整數。")
        numbers[key] = item
    if sum(numbers.values()) != 100:
        raise SemanticReplayError("多方、盤整、空方情境權重合計必須為100。")
    return {
        **numbers,
        "primary": _text(value["primary"], "scenario.primary", 500),
        "alternative": _text(value["alternative"], "scenario.alternative", 500),
        "view_change": _text(value["view_change"], "scenario.view_change", 500),
    }


def _action(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != ACTION_KEYS:
        raise SemanticReplayError("action格式無效。")
    result = dict(value)
    result["position_action"] = _enum(value["position_action"], {"NONE", "ENTER", "STOP", "EXIT"}, "position_action")
    result["direction"] = _enum(value["direction"], {"NONE", "LONG", "SHORT"}, "direction")
    for key in ACTION_KEYS - {"position_action", "direction", "stop_price"}:
        result[key] = _text(value[key], f"action.{key}", 500)
    stop = value["stop_price"]
    if stop is not None and (isinstance(stop, bool) or not isinstance(stop, (int, float)) or stop <= 0):
        raise SemanticReplayError("action.stop_price必須為正數或null。")
    result["stop_price"] = None if stop is None else float(stop)
    if result["position_action"] == "ENTER" and result["stop_price"] is None:
        raise SemanticReplayError("ENTER必須提供stop_price。")
    return result


def _memory(value: Any, *, expected_as_of: str, expected_session_key: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != MEMORY_KEYS:
        raise SemanticReplayError("memory欄位不完整或含額外欄位。")
    if value["version"] != 2:
        raise SemanticReplayError("memory.version必須為2。")
    if value["as_of"] != expected_as_of:
        raise SemanticReplayError("memory.as_of必須等於本輪最新已收盤K。")
    parsed = _aware(value["as_of"], "memory.as_of")
    if parsed.isoformat() != expected_as_of:
        raise SemanticReplayError("memory.as_of格式與本輪時間不一致。")
    if value["session_key"] != expected_session_key:
        raise SemanticReplayError("memory.session_key與模擬時段不一致。")
    active = value["active_setup"]
    if active is not None:
        active = _text(active, "memory.active_setup", 180)
    return {
        "version": 2,
        "as_of": expected_as_of,
        "session_key": expected_session_key,
        "active_setup": active,
        "thesis_bias": _enum(value["thesis_bias"], {"BULL", "BEAR", "NEUTRAL", "CONDITIONAL", "UNDEFINED"}, "thesis_bias"),
        "maintain": _text(value["maintain"], "memory.maintain", 500),
        "downgrade": _text(value["downgrade"], "memory.downgrade", 500),
        "flip": _text(value["flip"], "memory.flip", 500),
        "notes": _text_list(value["notes"], "memory.notes", 4),
    }


def _memory_v3(
    value: Any,
    *,
    ledger: Mapping[str, Any],
    expected_as_of: str,
    expected_session_key: str,
    previous_memory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != MEMORY_V3_KEYS:
        raise SemanticReplayError("memory v3欄位不完整或含額外欄位。")
    if value["version"] != 3:
        raise SemanticReplayError("memory.version必須為3。")
    if value["as_of"] != expected_as_of or _aware(value["as_of"], "memory.as_of").isoformat() != expected_as_of:
        raise SemanticReplayError("memory.as_of必須等於本輪最新已收盤K。")
    if value["session_key"] != expected_session_key:
        raise SemanticReplayError("memory.session_key與模擬時段不一致。")
    setups = value["active_setups"]
    if not isinstance(setups, list) or len(setups) > 3:
        raise SemanticReplayError("memory.active_setups最多3項。")
    normalized_setups = [_setup_state(item) for item in setups]
    previous_setups = (
        previous_memory.get("active_setups")
        if isinstance(previous_memory, Mapping)
        else None
    )
    _validate_setup_trigger_levels(
        normalized_setups,
        ledger,
        previous_setups=(previous_setups if isinstance(previous_setups, list) else None),
    )
    setup_keys = [item["setup_key"] for item in normalized_setups]
    if len(set(setup_keys)) != len(setup_keys):
        raise SemanticReplayError("memory.active_setups的setup_key不得重複。")
    control = _structure_control(
        value["structure_control"],
        ledger=ledger,
        previous_memory=previous_memory,
    )
    reentry = _reentry_memory(value["reentry"])
    return {
        "version": 3,
        "as_of": expected_as_of,
        "session_key": expected_session_key,
        "active_setups": normalized_setups,
        "thesis_bias": _enum(value["thesis_bias"], {"BULL", "BEAR", "NEUTRAL", "CONDITIONAL", "UNDEFINED"}, "thesis_bias"),
        "maintain": _text(value["maintain"], "memory.maintain", 300),
        "downgrade": _text(value["downgrade"], "memory.downgrade", 300),
        "flip": _text(value["flip"], "memory.flip", 300),
        "structure_control": control,
        "reentry": reentry,
        "notes": _text_list(value["notes"], "memory.notes", 3),
    }


def _validate_program_reentry_state(
    analysis: Mapping[str, Any],
    memory: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    expected_as_of: str,
) -> None:
    """Keep every AI re-entry field aligned with the program-owned position.

    Re-entry allowance is execution state, not a discretionary market reading.
    The model may decide whether a later eligible signal is worth taking, but it
    cannot consume, restore, or transfer the one permitted re-entry merely by
    writing a different label into analysis memory.
    """

    action = analysis["action"]
    event = action["position_action"]
    prior_stop_at = position.get("last_stop_time")
    (
        expected_status,
        expected_stop_at,
        expected_count,
        expected_setup_key,
    ) = _program_reentry_expectation(
        analysis,
        ledger=ledger,
        position=position,
        expected_as_of=expected_as_of,
    )
    if event == "STOP" and action.get("setup_key") != expected_setup_key:
        raise SemanticReplayError("STOP必須沿用程式持倉的active_setup_key。")

    reentry = memory.get("reentry")
    if not isinstance(reentry, Mapping) or (
        reentry.get("status") != expected_status
        or reentry.get("last_stop_at") != expected_stop_at
        or reentry.get("count") != expected_count
    ):
        raise SemanticReplayError(
            "memory.reentry必須服從程式持倉狀態："
            f"status={expected_status}、last_stop_at={expected_stop_at}、count={expected_count}。"
        )

    matching_setup = next(
        (
            item
            for item in memory.get("active_setups", [])
            if isinstance(item, Mapping) and item.get("setup_key") == expected_setup_key
        ),
        None,
    )
    setup_must_remain = bool(expected_setup_key) and (
        event in {"ENTER", "STOP"}
        or (position.get("status") == "FLAT" and isinstance(prior_stop_at, str))
    )
    if setup_must_remain and not isinstance(matching_setup, Mapping):
        raise SemanticReplayError("仍具有程式再進狀態的原setup_key必須保留在memory.active_setups。")
    if isinstance(matching_setup, Mapping) and matching_setup.get("reentry_status") != expected_status:
        raise SemanticReplayError(
            f"原setup的reentry_status必須為{expected_status}，不得由AI自行消耗或恢復再進權。"
        )

    action_refers_to_expected_setup = (
        bool(expected_setup_key) and action.get("setup_key") == expected_setup_key
    )
    if (event in {"ENTER", "STOP"} or action_refers_to_expected_setup) and (
        action.get("reentry_status") != expected_status
    ):
        raise SemanticReplayError(
            f"action.reentry_status必須為{expected_status}，並與程式持倉及memory一致。"
        )


def _restore_program_held_setup(
    memory: dict[str, Any],
    *,
    previous_memory: Mapping[str, Any] | None,
    position: Mapping[str, Any],
) -> None:
    """Keep the runner-owned live position setup in bounded model memory.

    A management or exit answer may focus on a newly forming opposite setup
    and omit the origin setup.  The open position remains owned by that exact
    setup until the program closes it, so restore the last validated copy
    before continuity validation.  The v3 memory contract allows at most three
    setups; the live owner takes priority over non-owning candidates.
    """

    if position.get("status") not in {"LONG", "SHORT"}:
        return
    setup_key = position.get("active_setup_key")
    if not isinstance(setup_key, str) or not setup_key:
        return
    setups = memory.get("active_setups")
    if not isinstance(setups, list):
        return
    if any(isinstance(item, Mapping) and item.get("setup_key") == setup_key for item in setups):
        return
    if not isinstance(previous_memory, Mapping):
        return
    previous_setup = next(
        (
            item
            for item in previous_memory.get("active_setups", [])
            if isinstance(item, Mapping) and item.get("setup_key") == setup_key
        ),
        None,
    )
    if not isinstance(previous_setup, Mapping):
        return
    restored = dict(previous_setup)
    restored["reentry_status"] = (
        "USED" if int(position.get("reentry_count") or 0) >= 1 else "NOT_APPLICABLE"
    )
    other = [
        item
        for item in setups
        if not (isinstance(item, Mapping) and item.get("setup_key") == setup_key)
    ]
    memory["active_setups"] = [restored, *other][:3]


def _canonicalize_program_reentry_summary(
    analysis: Mapping[str, Any],
    memory: dict[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    expected_as_of: str,
) -> None:
    """Write all duplicated program-owned re-entry fields canonically.

    The aggregate is execution bookkeeping already known by the runner.  A
    stale model copy must not consume a retry or stop a long replay.  The
    originating setup and any action referring to it carry the same execution
    fact, so they are canonicalized as well rather than left model-dependent.
    """

    status, last_stop_at, count, setup_key = _program_reentry_expectation(
        analysis,
        ledger=ledger,
        position=position,
        expected_as_of=expected_as_of,
    )
    memory["reentry"] = {
        "status": status,
        "last_stop_at": last_stop_at,
        "count": count,
    }
    if setup_key:
        for item in memory.get("active_setups", []):
            if isinstance(item, dict) and item.get("setup_key") == setup_key:
                item["reentry_status"] = status
        action = analysis.get("action")
        if isinstance(action, dict) and action.get("setup_key") == setup_key:
            action["reentry_status"] = status


def _program_reentry_expectation(
    analysis: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    position: Mapping[str, Any],
    expected_as_of: str,
) -> tuple[str, str | None, int, str | None]:
    action = analysis["action"]
    event = action["position_action"]
    entry_role = action["entry_role"]
    prior_count = int(position.get("reentry_count") or 0)
    prior_stop_at = position.get("last_stop_time")
    active_setup_key = position.get("active_setup_key")

    if event == "ENTER" and entry_role == "INITIAL":
        return "NOT_APPLICABLE", None, 0, action.get("setup_key")
    if event == "ENTER" and entry_role == "REENTRY":
        return "USED", prior_stop_at, 1, action.get("setup_key")
    if event == "STOP":
        stop_audit = ledger.get("protective_stop_audit")
        stop_at = (
            stop_audit.get("trigger_time")
            if isinstance(stop_audit, Mapping)
            and stop_audit.get("status") == "TRIGGERED"
            and isinstance(stop_audit.get("trigger_time"), str)
            else expected_as_of
        )
        status = (
            "USED"
            if prior_count >= 1
            else _available_reentry_status(
                last_stop_at=stop_at,
                expected_as_of=expected_as_of,
            )
        )
        return status, stop_at, prior_count, active_setup_key
    if event == "EXIT":
        return "NOT_APPLICABLE", None, 0, None
    if prior_count >= 1:
        return "USED", prior_stop_at, prior_count, active_setup_key
    if isinstance(prior_stop_at, str) and active_setup_key:
        return (
            _available_reentry_status(
                last_stop_at=prior_stop_at,
                expected_as_of=expected_as_of,
            ),
            prior_stop_at,
            0,
            active_setup_key,
        )
    return "NOT_APPLICABLE", None, 0, active_setup_key


def _restore_program_reentry_setup(
    memory: dict[str, Any],
    *,
    previous_memory: Mapping[str, Any] | None,
    position: Mapping[str, Any],
    expected_as_of: str,
) -> None:
    """Carry the one program-owned post-stop re-entry record forward.

    The analyzer may change its market lens, but it is not the authority that
    consumes or silently deletes execution state.  Preserve only the exact
    originating setup already present in the prior validated memory; never
    synthesize a new setup from prose.
    """

    setup_key = position.get("active_setup_key")
    last_stop_at = position.get("last_stop_time")
    if (
        position.get("status") != "FLAT"
        or not isinstance(setup_key, str)
        or not setup_key
        or not isinstance(last_stop_at, str)
        or int(position.get("reentry_count") or 0) >= 1
        or not isinstance(previous_memory, Mapping)
    ):
        return
    setups = memory.get("active_setups")
    if not isinstance(setups, list):
        return
    if any(isinstance(item, Mapping) and item.get("setup_key") == setup_key for item in setups):
        return
    previous_setup = next(
        (
            item
            for item in previous_memory.get("active_setups", [])
            if isinstance(item, Mapping) and item.get("setup_key") == setup_key
        ),
        None,
    )
    if not isinstance(previous_setup, Mapping):
        return
    restored = dict(previous_setup)
    restored["reentry_status"] = _available_reentry_status(
        last_stop_at=last_stop_at,
        expected_as_of=expected_as_of,
    )
    setups.append(restored)


def _available_reentry_status(*, last_stop_at: str, expected_as_of: str) -> str:
    stop_at = _aware(last_stop_at, "last_stop_at")
    now = _aware(expected_as_of, "expected_as_of")
    return "AVAILABLE" if now - stop_at >= timedelta(minutes=1) else "WAIT_ONE_BAR"


def _validate_v3_state_transition(
    analysis: Mapping[str, Any],
    memory: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    previous_memory: Mapping[str, Any] | None,
    evidence_events: list[Mapping[str, Any]],
    expected_as_of: str,
    ai_generated: bool = True,
) -> None:
    reading = analysis["course_reading"]
    control = memory["structure_control"]
    mirrored = [
        ("large_anchor_ref", "active_large_anchor_ref"),
        ("small_anchor_ref", "active_small_anchor_ref"),
        ("working_anchor_ref", "working_anchor_ref"),
        ("controlling_grade", "controlling_grade"),
        ("background_quadrant", "background_quadrant"),
        ("working_quadrant", "working_quadrant"),
        ("structure_event_ref", "last_structure_event_ref"),
    ]
    if "reverse_anchor_candidate_ref" in reading:
        mirrored.append(("reverse_anchor_candidate_ref", "active_reverse_candidate_ref"))
    for analysis_key, memory_key in mirrored:
        if reading.get(analysis_key) != control.get(memory_key):
            raise SemanticReplayError(f"analysis與memory的{analysis_key}必須一致。")
    now = _aware(expected_as_of, "expected_as_of")
    for key in ("background_quadrant_changed_at", "working_quadrant_changed_at"):
        changed_at = _aware(control[key], key)
        if changed_at > now:
            raise SemanticReplayError(f"{key}不得晚於本輪已收盤K。")

    previous = previous_memory if isinstance(previous_memory, Mapping) else None
    previous_control = previous.get("structure_control") if previous and previous.get("version") == 3 else None
    previous_is_empty_seed = (
        isinstance(previous_control, Mapping)
        and previous.get("as_of") == expected_as_of
        and previous_control.get("background_quadrant") == "UNDEFINED"
        and previous_control.get("working_quadrant") == "UNDEFINED"
        and previous_control.get("active_large_anchor_ref") is None
        and previous_control.get("active_small_anchor_ref") is None
        and not previous.get("active_setups")
    )
    if (
        not isinstance(previous_control, Mapping)
        or previous.get("session_key") != memory.get("session_key")
        or previous_is_empty_seed
    ):
        if control["background_quadrant_changed_at"] != expected_as_of:
            raise SemanticReplayError("新時段第一輪background_quadrant_changed_at必須等於本輪時間。")
        if control["working_quadrant_changed_at"] != expected_as_of:
            raise SemanticReplayError("新時段第一輪working_quadrant_changed_at必須等於本輪時間。")
        return

    if (
        (ai_generated or not _ai_hybrid_policy(ledger))
        and
        previous_control.get("controlling_grade") == "LARGE"
        and control.get("controlling_grade") != "LARGE"
    ):
        structure_ref = reading.get("structure_event_ref")
        referenced_event = next(
            (
                item
                for item in ledger.get("structure_events", [])
                if isinstance(item, Mapping) and item.get("id") == structure_ref
            ),
            None,
        )
        anchor_control = ledger.get("anchor_control")
        authoritative_parent_disappeared = (
            not _ai_hybrid_policy(ledger)
            and bool(previous_control.get("active_large_anchor_ref"))
            and isinstance(anchor_control, Mapping)
            and anchor_control.get("active_background_anchor_ref") is None
            and control.get("active_large_anchor_ref") is None
            and reading.get("large_anchor_ref") is None
        )
        has_explicit_release = (
            isinstance(referenced_event, Mapping)
            and referenced_event.get("event_type") in {
                "GRADE_DOWNGRADE",
                "STRUCTURE_INVALIDATED",
            }
        )
        if not has_explicit_release and not authoritative_parent_disappeared:
            raise SemanticReplayError(
                "已升級的控制級數必須持續為LARGE；只有GRADE_DOWNGRADE或父級失效事件可以解除。"
            )

    previous_large_anchor_ref = previous_control.get("active_large_anchor_ref")
    current_large_anchor_ref = control.get("active_large_anchor_ref")
    released_previous_anchor = any(
        isinstance(item, Mapping)
        and item.get("event_type") in {"GRADE_DOWNGRADE", "STRUCTURE_INVALIDATED"}
        and item.get("source_anchor_id") == previous_large_anchor_ref
        for item in evidence_events
    )
    if (
        _ai_hybrid_policy(ledger)
        and ai_generated
        and previous_large_anchor_ref
        and released_previous_anchor
        and current_large_anchor_ref == previous_large_anchor_ref
    ):
        raise SemanticReplayError(
            "本輪GRADE_DOWNGRADE或STRUCTURE_INVALIDATED已解除前一大錨；"
            "analysis與memory必須清除或合法切換該引用。"
        )
    if (
        (ai_generated or not _ai_hybrid_policy(ledger))
        and previous_large_anchor_ref
        and current_large_anchor_ref != previous_large_anchor_ref
    ):
        structure_ref = reading.get("structure_event_ref")
        referenced_event = next(
            (
                item
                for item in ledger.get("structure_events", [])
                if isinstance(item, Mapping) and item.get("id") == structure_ref
            ),
            None,
        )
        has_explicit_release = bool(
            isinstance(referenced_event, Mapping)
            and referenced_event.get("event_type")
            in {"GRADE_DOWNGRADE", "STRUCTURE_INVALIDATED"}
        )
        has_explicit_replacement = bool(
            isinstance(referenced_event, Mapping)
            and referenced_event.get("event_type") == "GRADE_UPGRADE"
            and _upgrade_controlling_anchor_ref(referenced_event, ledger=ledger)
            == current_large_anchor_ref
        )
        anchor_control = ledger.get("anchor_control")
        authoritative_parent_disappeared = bool(
            not _ai_hybrid_policy(ledger)
            and isinstance(anchor_control, Mapping)
            and anchor_control.get("active_background_anchor_ref") is None
            and current_large_anchor_ref is None
            and reading.get("large_anchor_ref") is None
        )
        if (
            not has_explicit_release
            and not has_explicit_replacement
            and not authoritative_parent_disappeared
        ):
            raise SemanticReplayError(
                "已接受的大錨必須持續引用原錨；只有GRADE_DOWNGRADE、父級失效或新的合法GRADE_UPGRADE可以更換。"
            )

    if (
        control.get("active_large_anchor_ref")
        and control.get("active_large_anchor_ref")
        == previous_control.get("active_large_anchor_ref")
    ):
        transition_text = " ".join(
            str(value or "")
            for value in (
                analysis.get("notification_reason"),
                reading.get("x_process"),
                reading.get("primary_lens"),
                reading.get("main_strategy"),
            )
        )
        if re.search(r"(?:大錨|背景錨).{0,8}(?:正式)?(?:切換|更換|取代|另立|重設)", transition_text):
            raise SemanticReplayError(
                "active_large_anchor_ref未變；本輪只能描述原大錨延伸或內部級數調整，不得宣稱大錨切換、更換或取代。"
            )

    previous_setups = {
        str(item.get("setup_key")): item
        for item in previous.get("active_setups", [])
        if isinstance(item, Mapping) and item.get("setup_key")
    }
    _validate_no_same_trigger_setup_rekey(
        previous_setups,
        memory,
        reading=reading,
        ledger=ledger,
    )
    for item in memory.get("active_setups", []):
        if not isinstance(item, Mapping):
            continue
        prior = previous_setups.get(str(item.get("setup_key")))
        if (
            isinstance(prior, Mapping)
            and prior.get("stage") in {"NO_CHASE", "INVALIDATED"}
            and item.get("stage") not in {"NO_CHASE", "INVALIDATED"}
            and not _is_lawful_post_stop_reentry_reactivation(
                prior,
                item,
                previous_memory=previous,
                current_memory=memory,
                expected_as_of=expected_as_of,
            )
        ):
            raise SemanticReplayError(
                "已過期、否決或失效的setup_key不得重新啟用；新機會必須建立新setup_key。"
            )

    background_changed = control["background_quadrant"] != previous_control.get("background_quadrant")
    if background_changed:
        if control["background_quadrant_changed_at"] != expected_as_of:
            raise SemanticReplayError("大級背景象限改變時changed_at必須等於本輪時間。")
    elif control["background_quadrant_changed_at"] != previous_control.get("background_quadrant_changed_at"):
        raise SemanticReplayError("大級背景象限未變時必須保留原changed_at。")

    working_changed = control["working_quadrant"] != previous_control.get("working_quadrant")
    if working_changed and control["working_quadrant_changed_at"] != expected_as_of:
        raise SemanticReplayError("小級工作象限改變時changed_at必須等於本輪時間。")
    if not working_changed and control["working_quadrant_changed_at"] != previous_control.get("working_quadrant_changed_at"):
        raise SemanticReplayError("小級工作象限未變時必須保留原changed_at。")


def _validate_no_same_trigger_setup_rekey(
    previous_setups: Mapping[str, Mapping[str, Any]],
    memory: Mapping[str, Any],
    *,
    reading: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> None:
    """Do not let an expired preparation extend itself under a new ID."""

    current = {
        str(item.get("setup_key")): item
        for item in memory.get("active_setups", [])
        if isinstance(item, Mapping) and item.get("setup_key")
    }
    trade_levels = ledger.get("trade_levels")
    candidate = (
        trade_levels.get("continuation_arm_candidate")
        if isinstance(trade_levels, Mapping)
        else None
    )
    candidate_key = candidate.get("setup_key") if isinstance(candidate, Mapping) else None
    has_structure_event = bool(reading.get("structure_event_ref"))
    for prior_key, prior in previous_setups.items():
        if prior.get("stage") != "ARMED" or prior_key in current:
            continue
        trigger_level = prior.get("trigger_level")
        if not isinstance(trigger_level, (int, float)) or isinstance(trigger_level, bool):
            continue
        replacement = next(
            (
                item
                for key, item in current.items()
                if key != prior_key
                and item.get("stage") == "ARMED"
                and item.get("direction") == prior.get("direction")
                and _same_price(item.get("trigger_level"), trigger_level)
            ),
            None,
        )
        if not isinstance(replacement, Mapping):
            continue
        if replacement.get("setup_key") == candidate_key or has_structure_event:
            continue
        raise SemanticReplayError(
            "已ARMED的同方向同觸發價不得只更換setup_key來延長有效窗；"
            "須有新的程式候選或正式結構事件。"
        )


def _is_lawful_post_stop_reentry_reactivation(
    prior_setup: Mapping[str, Any],
    current_setup: Mapping[str, Any],
    *,
    previous_memory: Mapping[str, Any],
    current_memory: Mapping[str, Any],
    expected_as_of: str,
) -> bool:
    """Allow only the one explicit setup resurrection created by a STOP.

    INVALIDATED normally retires a setup forever.  A program-owned STOP is the
    single exception: the originating setup key is intentionally retained so
    the course rule can permit one re-entry after at least one complete bar.
    Both the setup-local and session-wide re-entry state must agree, which
    prevents ordinary invalidation, expiry, or no-chase decisions from using
    this path.
    """

    if prior_setup.get("stage") != "INVALIDATED":
        return False
    if prior_setup.get("reentry_status") not in {"WAIT_ONE_BAR", "AVAILABLE"}:
        return False
    if current_setup.get("stage") not in {
        "FORMING",
        "ARMED",
        "ENTRY_ELIGIBLE",
        "AGGRESSIVE_CONFIRMED",
        "CONSERVATIVE_CONFIRMED",
    }:
        return False
    if current_setup.get("reentry_status") != "AVAILABLE":
        return False

    previous_reentry = previous_memory.get("reentry")
    current_reentry = current_memory.get("reentry")
    if not isinstance(previous_reentry, Mapping) or not isinstance(current_reentry, Mapping):
        return False
    last_stop_at = previous_reentry.get("last_stop_at")
    if (
        previous_reentry.get("status") not in {"WAIT_ONE_BAR", "AVAILABLE"}
        or previous_reentry.get("count") != 0
        or not last_stop_at
        or current_reentry.get("status") != "AVAILABLE"
        or current_reentry.get("count") != 0
        or current_reentry.get("last_stop_at") != last_stop_at
    ):
        return False

    stop_at = _aware(last_stop_at, "memory.reentry.last_stop_at")
    now = _aware(expected_as_of, "expected_as_of")
    return now - stop_at >= timedelta(minutes=1)


def _setup_state(value: Any) -> dict[str, Any]:
    keys = {"setup_key", "name", "direction", "stage", "trigger", "stop", "reentry_status"}
    v4_keys = keys | {"trigger_level", "trigger_operator", "valid_bars"}
    if not isinstance(value, Mapping) or set(value) not in {frozenset(keys), frozenset(v4_keys)}:
        raise SemanticReplayError("active_setup格式無效。")
    result = {
        "setup_key": _text(value["setup_key"], "setup_key", 120),
        "name": _text(value["name"], "setup.name", 120),
        "direction": _enum(value["direction"], {"LONG", "SHORT"}, "setup.direction"),
        "stage": _enum(value["stage"], {"FORMING", "ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED", "INVALIDATED", "NO_CHASE"}, "setup.stage"),
        "trigger": _text(value["trigger"], "setup.trigger", 240),
        "stop": _text(value["stop"], "setup.stop", 240),
        "reentry_status": _enum(value["reentry_status"], {"NOT_APPLICABLE", "WAIT_ONE_BAR", "AVAILABLE", "USED"}, "setup.reentry_status"),
    }
    if "trigger_level" in value:
        trigger_level = value["trigger_level"]
        if trigger_level is not None and (
            isinstance(trigger_level, bool)
            or not isinstance(trigger_level, (int, float))
            or trigger_level <= 0
        ):
            raise SemanticReplayError("setup.trigger_level必須為正數或null。")
        trigger_operator = _enum(
            value["trigger_operator"],
            {"NONE", "CLOSE_ABOVE", "CLOSE_BELOW", "RECLAIM_ABOVE", "RECLAIM_BELOW"},
            "setup.trigger_operator",
        )
        if (trigger_level is None) != (trigger_operator == "NONE"):
            raise SemanticReplayError("setup.trigger_level與trigger_operator必須同時存在或同時停用。")
        if result["direction"] == "LONG" and trigger_operator in {"CLOSE_BELOW", "RECLAIM_BELOW"}:
            raise SemanticReplayError("多方setup的程式觸發方向不得使用向下跌破。")
        if result["direction"] == "SHORT" and trigger_operator in {"CLOSE_ABOVE", "RECLAIM_ABOVE"}:
            raise SemanticReplayError("空方setup的程式觸發方向不得使用向上突破。")
        valid_bars = value["valid_bars"]
        if isinstance(valid_bars, bool) or not isinstance(valid_bars, int) or not 1 <= valid_bars <= 10:
            raise SemanticReplayError("setup.valid_bars必須為1至10。")
        result.update(
            {
                "trigger_level": None if trigger_level is None else float(trigger_level),
                "trigger_operator": trigger_operator,
                "valid_bars": valid_bars,
            }
        )
    return result


def _structure_control(
    value: Any,
    *,
    ledger: Mapping[str, Any],
    previous_memory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    keys = {
        "active_large_anchor_ref", "active_small_anchor_ref", "working_anchor_ref",
        "controlling_grade", "background_quadrant", "working_quadrant",
        "background_quadrant_changed_at", "working_quadrant_changed_at", "last_structure_event_ref",
    }
    lifecycle_control = ledger.get("anchor_control")
    if isinstance(lifecycle_control, Mapping):
        keys.add("active_reverse_candidate_ref")
    if not isinstance(value, Mapping) or set(value) != keys:
        raise SemanticReplayError("memory.structure_control格式無效。")
    refs = allowed_references(ledger)
    result = dict(value)
    if isinstance(lifecycle_control, Mapping) and _ai_hybrid_policy(ledger):
        selected_upgrade_anchor_ref = None
        previous_large_anchor_ref = _previous_ai_hybrid_large_anchor_ref(
            previous_memory,
            ledger=ledger,
            refs=refs,
        )
        selected_structure_ref = value.get("last_structure_event_ref")
        if selected_structure_ref is not None:
            for item in ledger.get("structure_events", []):
                if (
                    isinstance(item, Mapping)
                    and item.get("id") == selected_structure_ref
                    and item.get("event_type") == "GRADE_UPGRADE"
                    and item.get("source_anchor_id") in refs["legs"]
                ):
                    selected_upgrade_anchor_ref = item.get("source_anchor_id")
                    break
        exact = {
            "active_large_anchor_ref": "active_background_anchor_ref",
            "active_small_anchor_ref": "active_child_anchor_ref",
            "working_anchor_ref": "working_leg_ref",
            "active_reverse_candidate_ref": "reverse_anchor_candidate_ref",
        }
        for key, control_key in exact.items():
            reference = value[key]
            allowed_role_refs = {None, lifecycle_control.get(control_key)}
            # Mirror the same atomic upgrade allowance used by course_reading:
            # the accepted event's source leg becomes memory's active large
            # anchor on that exact decision turn, before program anchor_control
            # has advanced on the following bar.
            if key == "active_large_anchor_ref" and selected_upgrade_anchor_ref is not None:
                allowed_role_refs.add(selected_upgrade_anchor_ref)
            if (
                key == "active_large_anchor_ref"
                and isinstance(reference, str)
                and isinstance(
                    _active_promotion_for_anchor(reference, ledger=ledger),
                    Mapping,
                )
            ):
                allowed_role_refs.add(reference)
            if key == "active_large_anchor_ref" and previous_large_anchor_ref is not None:
                allowed_role_refs.add(previous_large_anchor_ref)
                reference = _long_only_persisted_large_anchor_ref(
                    reference,
                    previous_large_anchor_ref,
                    ledger=ledger,
                )
            if reference not in allowed_role_refs:
                allowed_text = ", ".join(
                    "null" if item is None else str(item)
                    for item in sorted(
                        allowed_role_refs,
                        key=lambda item: (item is not None, str(item or "")),
                    )
                )
                raise SemanticReplayError(
                    f"AI_HYBRID的memory.{key}只能採用對應程式候選或null；"
                    f"本輪允許值：{allowed_text}；收到：{reference}。"
                )
            result[key] = reference
    elif isinstance(lifecycle_control, Mapping):
        exact = {
            "active_large_anchor_ref": "active_background_anchor_ref",
            "active_small_anchor_ref": "active_child_anchor_ref",
            "working_anchor_ref": "working_leg_ref",
            "active_reverse_candidate_ref": "reverse_anchor_candidate_ref",
        }
        for key, control_key in exact.items():
            reference = value[key]
            if reference != lifecycle_control.get(control_key):
                raise SemanticReplayError(f"memory.{key}必須逐字引用程式錨生命週期。")
            result[key] = reference
    else:
        for key in ("active_large_anchor_ref", "active_small_anchor_ref", "working_anchor_ref"):
            reference = value[key]
            if reference is not None and reference not in refs["legs"]:
                raise SemanticReplayError(f"memory.{key}引用不存在的段。")
            result[key] = reference
    structure_ref = value["last_structure_event_ref"]
    if structure_ref is not None and structure_ref not in refs["structure_events"]:
        raise SemanticReplayError("memory.last_structure_event_ref引用不存在的事件。")
    result["last_structure_event_ref"] = structure_ref
    result["controlling_grade"] = _enum(value["controlling_grade"], {"LARGE", "SMALL", "UNDEFINED"}, "memory.controlling_grade")
    quadrants = {"Q1", "Q2", "Q3", "Q4", "TRANSITION", "UNDEFINED"}
    result["background_quadrant"] = _enum(value["background_quadrant"], quadrants, "memory.background_quadrant")
    result["working_quadrant"] = _enum(value["working_quadrant"], quadrants, "memory.working_quadrant")
    for key in ("background_quadrant_changed_at", "working_quadrant_changed_at"):
        result[key] = _aware(value[key], f"memory.{key}").isoformat()
    return result


def _reentry_memory(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"status", "last_stop_at", "count"}:
        raise SemanticReplayError("memory.reentry格式無效。")
    last_stop = value["last_stop_at"]
    if last_stop is not None:
        _aware(last_stop, "memory.reentry.last_stop_at")
    count = value["count"]
    if isinstance(count, bool) or not isinstance(count, int) or count not in {0, 1}:
        raise SemanticReplayError("memory.reentry.count只能是0或1。")
    return {
        "status": _enum(value["status"], {"NOT_APPLICABLE", "WAIT_ONE_BAR", "AVAILABLE", "USED"}, "memory.reentry.status"),
        "last_stop_at": last_stop,
        "count": count,
    }


def _validate_setup_trigger_levels(
    setups: list[Mapping[str, Any]],
    ledger: Mapping[str, Any],
    *,
    previous_setups: list[Mapping[str, Any]] | None = None,
) -> None:
    known: set[float] = set()
    latest = ledger.get("latest_closed_k")
    if isinstance(latest, Mapping):
        for key in ("open", "high", "low", "close"):
            if isinstance(latest.get(key), (int, float)) and not isinstance(latest.get(key), bool):
                known.add(float(latest[key]))
    # An ARMED setup freezes the structural high/low visible when it was
    # created.  The current working leg can extend before the later close
    # triggers, so retain causal recent-bar extremes as valid sources instead
    # of forcing the trigger to move with the newest extreme.
    recent = ledger.get("recent_bar_levels")
    for bar in recent if isinstance(recent, list) else []:
        if not isinstance(bar, Mapping):
            continue
        for key in ("high", "low"):
            value = bar.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                known.add(float(value))
    for collection in (
        "pivots", "working_pivots", "legs", "defenses", "structure_events", "anchor_records"
    ):
        values = ledger.get(collection)
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, Mapping):
                continue
            for key in (
                "price", "start_price", "end_price", "origin_price", "first_extreme_price",
                "latest_extreme_price", "current_extreme_price", "takeover_level",
                "parent_origin_price",
                "absorbed_defense_price", "replacement_defense_price", "reclaimed_boundary_price",
            ):
                value = item.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    known.add(float(value))
            nested_defense = item.get("defense")
            if isinstance(nested_defense, Mapping):
                value = nested_defense.get("price")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    known.add(float(value))
            defense_candidate = item.get("defense_candidate")
            if isinstance(defense_candidate, Mapping):
                value = defense_candidate.get("price")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    known.add(float(value))
    opening = ledger.get("opening_ranges")
    if isinstance(opening, Mapping):
        for value in opening.values():
            if isinstance(value, Mapping):
                for key in ("high", "low"):
                    if isinstance(value.get(key), (int, float)) and not isinstance(value.get(key), bool):
                        known.add(float(value[key]))
    indicators = ledger.get("indicators")
    if isinstance(indicators, Mapping):
        for value in indicators.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                known.add(float(value))
    previous_by_key = {
        str(item.get("setup_key")): item
        for item in previous_setups or []
        if isinstance(item, Mapping) and item.get("setup_key")
    }
    for setup in setups:
        level = setup.get("trigger_level")
        if level is None:
            continue
        previous = previous_by_key.get(str(setup.get("setup_key")))
        previous_level = previous.get("trigger_level") if isinstance(previous, Mapping) else None
        unchanged_existing_trigger = (
            isinstance(previous_level, (int, float))
            and not isinstance(previous_level, bool)
            and abs(float(level) - float(previous_level)) <= 0.01
            and setup.get("trigger_operator") == previous.get("trigger_operator")
        )
        if unchanged_existing_trigger:
            # A setup freezes its trigger when created.  It remains causal
            # even after that old bar scrolls out of the current evidence
            # window; only a new or changed trigger must be grounded again.
            continue
        if not any(abs(float(level) - reference) <= 0.51 for reference in known):
            raise SemanticReplayError("setup.trigger_level必須來自本輪已揭露的結構、開盤區間或指標價位。")


def _text_list(value: Any, field: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise SemanticReplayError(f"{field}必須是1至{maximum}項文字陣列。")
    return [_text(item, field, 500) for item in value]


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SemanticReplayError(f"{field}文字無效。")
    return value.strip()


def _enum(value: Any, allowed: set[str], field: str) -> str:
    if value not in allowed:
        raise SemanticReplayError(f"{field}值無效。")
    return str(value)


def _aware(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise SemanticReplayError(f"{field}必須是含時區時間。")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SemanticReplayError(f"{field}不是有效時間。") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise SemanticReplayError(f"{field}必須包含時區。")
    return result
