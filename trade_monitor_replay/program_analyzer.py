"""Deterministic semantic envelope for causal replay.

This module produces the same schema consumed by the existing validator and
renderer, but it contains no language-model call.  All executable decisions
are still overlaid and checked by ``semantic_contract``.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


_Q2_ENTRY_STRATEGIES = {
    "Q2_FALSE_BREAK_RECLAIM",
    "Q2_FAILED_COUNTERTREND_REVERSAL",
    "Q2_SLOW_OUTER_EXPANSION_FAILURE",
}
_Q4_ENTRY_STRATEGIES = {
    "Q4_PULLBACK_CONTINUATION",
    "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
}
_Q2_CANDIDATE_SOURCES = {
    "FALSE_BREAK_RECLAIM",
    "Q2_FAILED_REVERSE_CANDIDATE",
    "Q2_SLOW_OUTER_EXPANSION_FAILURE",
}
_Q4_CANDIDATE_SOURCES = {
    "ANCHOR_LEG_SEQUENCE",
    "CONFIRMED_PULLBACK_ENDPOINT_N2",
    "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
}


def build_program_semantic_envelope(
    *,
    ledger: Mapping[str, Any],
    expected_as_of: str,
    expected_session_key: str,
    preopen: bool,
    position: Mapping[str, Any],
    previous_memory: Mapping[str, Any] | None,
    entry_gate: Mapping[str, Any] | None,
    evidence_events: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    lifecycle = ledger.get("anchor_lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    control = ledger.get("anchor_control")
    control = control if isinstance(control, Mapping) else {}
    quadrant = lifecycle.get("quadrant_context")
    quadrant = quadrant if isinstance(quadrant, Mapping) else {}
    methods = ledger.get("course_method_state")
    methods = methods if isinstance(methods, Mapping) else {}
    latest = ledger.get("latest_closed_k")
    latest = latest if isinstance(latest, Mapping) else {}
    levels = ledger.get("trade_levels")
    levels = levels if isinstance(levels, Mapping) else {}
    trade_policy = ledger.get("program_trade_policy")
    trade_policy = trade_policy if isinstance(trade_policy, Mapping) else {}
    q2_q4_output_contract = (
        trade_policy.get("trade_setup_policy") == "LONG_Q2_Q4_ONLY"
    )
    preentry_invalidation = ledger.get("preentry_invalidation_audit")
    preentry_invalidation = (
        preentry_invalidation
        if isinstance(preentry_invalidation, Mapping)
        and preentry_invalidation.get("status") == "INVALIDATED_BEFORE_ENTRY"
        else None
    )
    candidate = levels.get("continuation_arm_candidate")
    candidate = candidate if isinstance(candidate, Mapping) else None
    gate = entry_gate if isinstance(entry_gate, Mapping) else {}

    background = lifecycle.get("background_anchor")
    child = lifecycle.get("child_anchor")
    working = lifecycle.get("working_leg")
    reverse = lifecycle.get("reverse_candidate")
    controller = (
        background
        if isinstance(background, Mapping) and background.get("status") in {None, "ACTIVE"}
        else child
        if isinstance(child, Mapping) and child.get("status") in {None, "ACTIVE"}
        else None
    )
    direction = controller.get("direction") if isinstance(controller, Mapping) else None
    message_direction = direction if direction in {"BULL", "BEAR"} else "NEUTRAL"
    latest_close = latest.get("close")
    price_text = (
        f"{float(latest_close):,.0f}點"
        if isinstance(latest_close, (int, float)) and not isinstance(latest_close, bool)
        else "已收盤K"
    )

    large_ref = control.get("active_background_anchor_ref")
    small_ref = control.get("active_child_anchor_ref")
    working_ref = control.get("working_leg_ref")
    reverse_ref = control.get("reverse_anchor_candidate_ref")
    controlling_grade = "LARGE" if large_ref else "SMALL" if small_ref else "UNDEFINED"
    grade_relation = _grade_relation(background, child)
    material_event = _program_material_event_for_semantics(
        ledger,
        evidence_events or [],
    )
    structure_event_ref = material_event.get("id") if isinstance(material_event, Mapping) else None

    background_quadrant = str(quadrant.get("background_primary") or "UNDEFINED")
    working_quadrant = str(quadrant.get("working_primary") or "UNDEFINED")
    working_candidates = [
        str(item) for item in quadrant.get("working_candidates", [])
        if item in {"Q1", "Q2", "Q3", "Q4", "TRANSITION", "UNDEFINED"}
    ]
    setup_stage = (
        "ENTRY_ELIGIBLE" if gate.get("status") == "ENTRY_ELIGIBLE"
        else "INVALIDATED" if preentry_invalidation is not None
        else "ARMED" if candidate is not None and position.get("status") == "FLAT"
        else "CONSERVATIVE_CONFIRMED" if position.get("status") in {"LONG", "SHORT"}
        else "NONE"
    )
    selected_setup = gate if gate.get("status") == "ENTRY_ELIGIBLE" else candidate
    main_strategy_family = (
        _program_main_strategy_family(
            selected_setup=selected_setup,
            position=position,
            preentry_invalidation=preentry_invalidation,
        )
        if q2_q4_output_contract
        else None
    )
    setup_key = (
        selected_setup.get("setup_key")
        if isinstance(selected_setup, Mapping)
        else position.get("active_setup_key")
        if position.get("status") in {"LONG", "SHORT"}
        or isinstance(position.get("pending_entry"), Mapping)
        else None
    )
    if preentry_invalidation is not None:
        setup_key = preentry_invalidation.get("setup_key")
        message_direction = {
            "LONG": "BULL",
            "SHORT": "BEAR",
        }.get(str(preentry_invalidation.get("direction") or ""), message_direction)

    weights = methods.get("scenario_weights")
    weights = weights if isinstance(weights, Mapping) else {}
    probabilities = (
        int(weights.get("bull", 25)),
        int(weights.get("range", 50)),
        int(weights.get("bear", 25)),
    )
    if sum(probabilities) != 100:
        probabilities = (25, 50, 25)

    active_setups = [] if preopen else _program_memory_setups(
        previous_memory,
        selected_setup=(
            selected_setup
            if position.get("status") == "FLAT"
            and not isinstance(position.get("pending_entry"), Mapping)
            else None
        ),
        position=position,
    )
    reentry = _program_reentry_memory(ledger, previous_memory=previous_memory)
    previous_control = (
        previous_memory.get("structure_control")
        if isinstance(previous_memory, Mapping)
        and previous_memory.get("session_key") == expected_session_key
        else None
    )
    background_changed_at = _change_time(
        previous_control,
        key="background_quadrant",
        value=background_quadrant,
        expected_as_of=expected_as_of,
    )
    working_changed_at = _change_time(
        previous_control,
        key="working_quadrant",
        value=working_quadrant,
        expected_as_of=expected_as_of,
    )

    event_message_type = _material_event_message_type(material_event, lifecycle=lifecycle)
    if event_message_type == "PREPARATION" and selected_setup is None:
        # A raw false-break event may be useful market information while the
        # course-quality gate intentionally keeps it observation-only.  The
        # card type must follow the executable candidate, not the ungated
        # event, otherwise the semantic contract sees PREPARATION with no
        # legal setup.
        event_message_type = "OBSERVATION"
    position_open = position.get("status") in {"LONG", "SHORT"}
    management_due = _management_notification_due(
        ledger,
        evidence_events or [],
        position=position,
        material_event=material_event,
    )
    message_type = (
        "SNAPSHOT" if preopen
        else "MANAGEMENT" if position_open and management_due
        else "UNCHANGED" if position_open
        else "INVALIDATION" if preentry_invalidation is not None
        else "PREPARATION" if gate.get("status") == "ENTRY_ELIGIBLE"
        else "PREPARATION" if event_message_type == "OBSERVATION" and selected_setup is not None
        else event_message_type if event_message_type is not None
        else "PREPARATION" if selected_setup is not None
        else "OBSERVATION" if evidence_events
        else "UNCHANGED"
    )
    decision = "DONT_NOTIFY" if message_type == "UNCHANGED" else "NOTIFY"
    action = {
        "position_action": "NONE",
        "direction": "NONE",
        "entry_role": "NOT_APPLICABLE",
        "setup_key": setup_key,
        "observation_area": "依程式定錨、防線與候選結構觀察。",
        "trigger": "等待程式化已收盤K觸發。",
        "entry": "目前沒有新的程式成交指令。",
        "structural_stop": "停損只使用程式確認的完整結構外位置。",
        "stop_price": None,
        "obstacles": [],
        "expected_behavior": "觸發後必須在固定行為窗內產生有利推進。",
        "max_wait_bars": None,
        "no_chase": "未取得程式進場資格時不追價。",
        "management": "持倉、停損、出場與再進場由程式狀態機管理。",
        "reentry_status": "NOT_APPLICABLE",
        "entry_rejection_reason": "NONE",
    }
    if preentry_invalidation is not None:
        invalidated_at = str(preentry_invalidation.get("invalidated_at") or "")
        action.update(
            {
                "setup_key": preentry_invalidation.get("setup_key"),
                "trigger": f"進場前於{invalidated_at[11:16]}觸及原結構停損。",
                "entry": "舊進場計畫已取消；等待新的完整修正與新setup。",
                "structural_stop": (
                    f"原結構停損{float(preentry_invalidation['stop_price']):,.0f}點已在進場前失效。"
                    if isinstance(preentry_invalidation.get("stop_price"), (int, float))
                    else "原結構停損已在進場前失效。"
                ),
                "expected_behavior": "不得使用舊觸發線重新進場。",
                "management": "舊setup退役；僅新的因果結構可重新武裝。",
            }
        )

    return {
        "analysis": {
            "original_decision": decision,
            "message_type": message_type,
            "message_direction": message_direction,
            "notification_reason": (
                "程式確認原候選在進場前已觸及結構停損，舊setup退役。"
                if preentry_invalidation is not None
                else "程式依最新因果1分K更新課程結構與交易狀態。"
            ),
            "latest_closed_k_price_estimate": price_text,
            "large_trend": {"classification": "盤整", "details": ["等待程式大級結構判定。"]},
            "current_trend": {"classification": "轉換中", "details": ["等待程式工作方向判定。"]},
            "market_summary": [
                f"最新已收盤K={expected_as_of[11:16]}；收盤={price_text}。",
                "所有結構與交易欄位均取自因果程式狀態。",
            ],
            "course_reading": {
                "large_anchor_ref": large_ref,
                "small_anchor_ref": small_ref,
                "working_anchor_ref": working_ref,
                "reverse_anchor_candidate_ref": reverse_ref,
                "large_defense_ref": control.get("large_defense_ref"),
                "small_defense_ref": control.get("small_defense_ref"),
                "structure_event_ref": structure_event_ref,
                "controlling_grade": controlling_grade,
                "grade_relation": grade_relation,
                "background_quadrant": background_quadrant,
                "working_quadrant": working_quadrant,
                "primary_quadrant_candidate": working_candidates[0] if working_candidates else "UNDEFINED",
                "secondary_quadrant_candidate": working_candidates[1] if len(working_candidates) > 1 else "UNDEFINED",
                "background_trend_dynamics": str(quadrant.get("background_trend_dynamics") or "UNCLEAR"),
                "background_volatility_dynamics": str(quadrant.get("background_volatility_dynamics") or "UNCLEAR"),
                "working_trend_dynamics": str(quadrant.get("working_trend_dynamics") or "UNCLEAR"),
                "working_volatility_dynamics": str(quadrant.get("working_volatility_dynamics") or "UNCLEAR"),
                "focus_methods": ["QUADRANT", "X_PROCESS"],
                "cclass_mode": str(methods.get("cclass_mode") or "UNDEFINED"),
                "taiji": "太極狀態服從程式段序。",
                "yizhi": "一之狀態服從程式動能掃描。",
                "left_right": "左右狀態服從程式反轉生命週期。",
                "dow": "道氏方向與防線服從程式錨生命週期。",
                "x_stage": (
                    "PREOPEN_CONTEXT"
                    if preopen
                    else str(methods.get("x_stage") or "OPENING_EVIDENCE")
                ),
                "x_process": "X流程服從程式因果階段。",
                "primary_lens": "主鏡頭由程式狀態決定。",
                "main_strategy": (
                    f"{str(preentry_invalidation.get('setup_name') or '程式候選')}（進場前失效）"
                    if preentry_invalidation is not None
                    else "目前沒有程式可成交候選。"
                ),
                **(
                    {"main_strategy_family": main_strategy_family}
                    if q2_q4_output_contract
                    else {}
                ),
                "setup_stage": setup_stage,
                "strategy_reason": ["程式交易決策不可由AI新增、延後或取消。"],
            },
            "scenario": {
                "bull_probability": probabilities[0],
                "range_probability": probabilities[1],
                "bear_probability": probabilities[2],
                "bull_plan": "多方僅在程式多方候選完成收盤觸發後執行。",
                "range_plan": "沒有程式候選時維持空手，等待新結構。",
                "bear_plan": "空方僅在程式空方候選完成收盤觸發後執行。",
                "view_change": "只依程式錨失效、防線破壞、象限或戰法狀態轉移改變看法。",
            },
            "action": action,
        },
        "memory": {
            "version": 3,
            "as_of": expected_as_of,
            "session_key": expected_session_key,
            "active_setups": active_setups,
            "thesis_bias": direction if direction in {"BULL", "BEAR"} else "NEUTRAL",
            "maintain": "作用中錨、防線與程式候選未失效前維持目前結構。",
            "downgrade": "程式偵測複製轉弱、動能失效或小級防線破壞時降級。",
            "flip": "反向錨完成接管或同級防線破壞並延續時才翻向。",
            "structure_control": {
                "active_large_anchor_ref": large_ref,
                "active_small_anchor_ref": small_ref,
                "working_anchor_ref": working_ref,
                "active_reverse_candidate_ref": reverse_ref,
                "controlling_grade": controlling_grade,
                "background_quadrant": background_quadrant,
                "working_quadrant": working_quadrant,
                "background_quadrant_changed_at": background_changed_at,
                "working_quadrant_changed_at": working_changed_at,
                "last_structure_event_ref": structure_event_ref,
            },
            "reentry": reentry,
            "notes": ["確定性引擎輸出；AI不是交易決策來源。"],
        },
    }


def _program_main_strategy_family(
    *,
    selected_setup: Mapping[str, Any] | None,
    position: Mapping[str, Any],
    preentry_invalidation: Mapping[str, Any] | None,
) -> str:
    """Return the v34 PnL family without consulting interpretive methods."""

    pending = position.get("pending_entry")
    behavior = position.get("behavior_plan")
    if preentry_invalidation is not None:
        sources: tuple[Mapping[str, Any] | None, ...] = (preentry_invalidation,)
    elif position.get("status") in {"LONG", "SHORT"}:
        sources = (
            behavior if isinstance(behavior, Mapping) else None,
            selected_setup,
        )
    elif isinstance(pending, Mapping):
        sources = (pending, selected_setup)
    else:
        sources = (selected_setup,)

    for source in sources:
        if not isinstance(source, Mapping):
            continue
        strategy = str(
            source.get("required_entry_strategy")
            or source.get("entry_strategy")
            or ""
        )
        candidate_source = str(
            source.get("required_candidate_source")
            or source.get("candidate_source")
            or ""
        )
        strategy_family = (
            "Q2" if strategy in _Q2_ENTRY_STRATEGIES
            else "Q4" if strategy in _Q4_ENTRY_STRATEGIES
            else None
        )
        source_family = (
            "Q2" if candidate_source in _Q2_CANDIDATE_SOURCES
            else "Q4" if candidate_source in _Q4_CANDIDATE_SOURCES
            else None
        )
        if (
            strategy_family is not None
            and source_family is not None
            and strategy_family != source_family
        ):
            return "NONE"
        if strategy_family is not None:
            return strategy_family
        if source_family is not None:
            return source_family
    return "NONE"


def _grade_relation(background: Any, child: Any) -> str:
    if isinstance(background, Mapping) and isinstance(child, Mapping):
        return "ALIGNED" if background.get("direction") == child.get("direction") else "CONFLICT"
    if isinstance(background, Mapping):
        return "ONLY_LARGE"
    if isinstance(child, Mapping):
        return "ONLY_SMALL"
    return "UNDEFINED"


def _latest_material_event(
    ledger: Mapping[str, Any],
    evidence_events: list[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    visible_ids = {
        str(item.get("event_id"))
        for item in evidence_events
        if isinstance(item, Mapping) and item.get("event_id")
    }
    raw_events = [
        item
        for item in ledger.get("structure_events", [])
        if isinstance(item, Mapping) and str(item.get("id")) in visible_ids
    ]
    lifecycle = ledger.get("anchor_lifecycle")
    control = ledger.get("anchor_control")
    background = lifecycle.get("background_anchor") if isinstance(lifecycle, Mapping) else None
    active_ref = control.get("active_background_anchor_ref") if isinstance(control, Mapping) else None
    if (
        isinstance(background, Mapping)
        and background.get("status") == "ACTIVE"
        and background.get("id") == active_ref
    ):
        matching = [
            item
            for item in raw_events
            if item.get("event_type") == "GRADE_UPGRADE"
            and item.get("direction") == background.get("direction")
        ]
        if matching:
            return max(matching, key=lambda item: (str(item.get("first_seen_at")), str(item.get("id"))))
    priority = {
        "FALSE_BREAK_RECLAIM": 1,
        "GRADE_UPGRADE": 2,
        "GRADE_DOWNGRADE": 3,
        "STRUCTURE_INVALIDATED": 4,
    }
    return max(
        (item for item in raw_events if item.get("event_type") in priority),
        key=lambda item: (
            str(item.get("first_seen_at")),
            priority[str(item.get("event_type"))],
            str(item.get("id")),
        ),
        default=None,
    )


def _material_event_message_type(
    event: Mapping[str, Any] | None,
    *,
    lifecycle: Mapping[str, Any],
) -> str | None:
    if not isinstance(event, Mapping):
        return None
    event_type = str(event.get("event_type") or "")
    if event_type == "GRADE_UPGRADE":
        return "STRUCTURE_UPGRADE"
    if event_type == "GRADE_DOWNGRADE":
        return "STRUCTURE_DOWNGRADE"
    if event_type == "STRUCTURE_INVALIDATED":
        return "INVALIDATION"
    if event_type != "FALSE_BREAK_RECLAIM":
        return None
    background = lifecycle.get("background_anchor")
    child = lifecycle.get("child_anchor")
    controller = (
        background
        if isinstance(background, Mapping) and background.get("status") == "ACTIVE"
        else child
        if isinstance(child, Mapping) and child.get("status") == "ACTIVE"
        else None
    )
    if isinstance(controller, Mapping) and event.get("direction") != controller.get("direction"):
        return "OBSERVATION"
    return "PREPARATION"


def _program_material_event_for_semantics(
    ledger: Mapping[str, Any],
    evidence_events: list[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Do not let a hidden program tick accept an AI-hybrid structure event."""

    policy = ledger.get("program_trade_policy")
    if (
        isinstance(policy, Mapping)
        and policy.get("actionable_setups") == "PROGRAM_CANDIDATES_AI_DECISION"
        and policy.get("decision_authority") == "AI_HYBRID"
    ):
        return None
    return _latest_material_event(ledger, evidence_events)


def _management_notification_due(
    ledger: Mapping[str, Any],
    evidence_events: list[Mapping[str, Any]],
    *,
    position: Mapping[str, Any],
    material_event: Mapping[str, Any] | None,
) -> bool:
    if not position.get("status") in {"LONG", "SHORT"}:
        return False
    if isinstance(material_event, Mapping):
        return True
    significant = {
        "DEFENSE_BROKEN",
        "BACKGROUND_DEFENSE_BROKEN",
        "BACKGROUND_DEFENSE_RECLAIMED",
        "PROGRAM_QUADRANT_CHANGED",
        "PROGRAM_TAIJI_CHANGED",
        "PROGRAM_METHOD_CHANGED",
    }
    if any(str(item.get("event_type") or "") in significant for item in evidence_events):
        return True
    levels = ledger.get("trade_levels")
    protections = levels.get("position_protection_candidates") if isinstance(levels, Mapping) else None
    candidate = protections.get(position.get("status")) if isinstance(protections, Mapping) else None
    return bool(isinstance(candidate, Mapping) and candidate.get("protection_required") is True)


def _program_memory_setups(
    previous_memory: Mapping[str, Any] | None,
    *,
    selected_setup: Mapping[str, Any] | None,
    position: Mapping[str, Any],
) -> list[dict[str, Any]]:
    previous = previous_memory.get("active_setups") if isinstance(previous_memory, Mapping) else None
    result = [deepcopy(item) for item in previous if isinstance(item, Mapping)] if isinstance(previous, list) else []
    if position.get("status") in {"LONG", "SHORT"} or isinstance(position.get("pending_entry"), Mapping):
        active_key = str(position.get("active_setup_key") or "")
        kept = [
            item
            for item in result
            if str(item.get("setup_key") or "") == active_key
            or item.get("stage") in {"NO_CHASE", "INVALIDATED"}
        ][:3]
        if position.get("status") in {"LONG", "SHORT"}:
            for item in kept:
                if (
                    str(item.get("setup_key") or "") == active_key
                    and item.get("stage") in {"ARMED", "ENTRY_ELIGIBLE"}
                ):
                    item["stage"] = "CONSERVATIVE_CONFIRMED"
                    break
        return kept
    if not isinstance(selected_setup, Mapping) or not selected_setup.get("setup_key"):
        return result[:3]
    setup_key = str(selected_setup["setup_key"])
    result = [item for item in result if item.get("setup_key") != setup_key]
    role = str(selected_setup.get("entry_role") or "INITIAL")
    stage = "ENTRY_ELIGIBLE" if selected_setup.get("status") == "ENTRY_ELIGIBLE" else "ARMED"
    stop = selected_setup.get("required_stop_price") or selected_setup.get("stop_price")
    result.insert(0, {
        "setup_key": setup_key,
        "name": str(selected_setup.get("setup_name") or "程式化結構交易"),
        "direction": str(selected_setup.get("direction")),
        "stage": stage,
        "trigger": "程式化已收盤K觸發。",
        "stop": f"完整結構外停損{float(stop):,.1f}點。" if isinstance(stop, (int, float)) else "等待程式停損。",
        "reentry_status": "USED" if role == "REENTRY" and stage == "ENTRY_ELIGIBLE" else "AVAILABLE" if role == "REENTRY" else "NOT_APPLICABLE",
        "trigger_level": selected_setup.get("trigger_level"),
        "trigger_operator": str(selected_setup.get("trigger_operator") or "NONE"),
        "valid_bars": int(selected_setup.get("valid_bars") or 3),
    })
    return result[:3]


def _program_reentry_memory(
    ledger: Mapping[str, Any],
    *,
    previous_memory: Mapping[str, Any] | None,
) -> dict[str, Any]:
    expectation = ledger.get("program_reentry_expectation")
    if isinstance(expectation, Mapping):
        return {
            "status": str(expectation.get("status_after_action") or "NOT_APPLICABLE"),
            "last_stop_at": expectation.get("last_stop_at_after_action"),
            "count": int(expectation.get("count_after_action") or 0),
        }
    previous = previous_memory.get("reentry") if isinstance(previous_memory, Mapping) else None
    if isinstance(previous, Mapping):
        return {
            "status": str(previous.get("status") or "NOT_APPLICABLE"),
            "last_stop_at": previous.get("last_stop_at"),
            "count": int(previous.get("count") or 0),
        }
    return {"status": "NOT_APPLICABLE", "last_stop_at": None, "count": 0}


def _change_time(
    previous_control: Any,
    *,
    key: str,
    value: str,
    expected_as_of: str,
) -> str:
    if isinstance(previous_control, Mapping) and previous_control.get(key) == value:
        prior = previous_control.get(f"{key}_changed_at")
        if isinstance(prior, str):
            return prior
    return expected_as_of
