from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence


ENTRY_DENIAL_REASONS = {
    "STOP_TOO_WIDE",
    "HARD_OBSTACLE_TOO_CLOSE",
    "GRADE_CONFLICT",
    "CONSTITUTION_BLOCKED",
}
ENTRY_TRIGGER_OPERATORS = {
    "CLOSE_ABOVE",
    "CLOSE_BELOW",
    "RECLAIM_ABOVE",
    "RECLAIM_BELOW",
}
DEFAULT_EVENT_VALID_BARS = 3


class ReplayExecutionGateError(ValueError):
    pass


def derive_entry_eligibility(
    previous_memory: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    position: Mapping[str, Any] | None = None,
    current_candidate: Mapping[str, Any] | None = None,
    blocked_setup_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Detect the first closed-bar trigger from an already armed setup.

    The model defines a setup and its objective trigger while it is forming.
    On later bars the program owns the crossing/reclaim test and stamps the
    true signal time.  A signal that is older than its declared validity is
    exposed as expired and cannot be revived by sparse AI sampling.
    """

    now = _aware(as_of, "as_of")
    current_position = position if isinstance(position, Mapping) else {}
    if current_position.get("status") != "FLAT" or isinstance(current_position.get("pending_entry"), Mapping):
        return _empty_entry_gate(now)
    memory = previous_memory if isinstance(previous_memory, Mapping) else {}
    previous_as_of = _optional_aware(memory.get("as_of"))
    setups = memory.get("active_setups")
    if not isinstance(setups, list) or previous_as_of is None:
        setups = []
    blocked = {str(item) for item in (blocked_setup_keys or set()) if str(item)}
    authoritative_program_key = (
        str(current_candidate.get("setup_key") or "")
        if isinstance(current_candidate, Mapping)
        and current_candidate.get("decision_authority") in {"PROGRAM", "AI_HYBRID"}
        else ""
    )

    ordered = sorted(
        (dict(item) for item in bars if isinstance(item, Mapping) and _optional_aware(item.get("time")) is not None),
        key=lambda item: str(item.get("time")),
    )
    for setup in setups:
        if not isinstance(setup, Mapping) or setup.get("stage") not in {
            "ARMED",
            "ENTRY_ELIGIBLE",
            "AGGRESSIVE_CONFIRMED",
            "CONSERVATIVE_CONFIRMED",
        }:
            continue
        if setup.get("reentry_status") == "USED":
            # An exhausted setup is historical audit state, not a new initial
            # opportunity. Treating it as INITIAL would reset the counter and
            # allow a third fill under the same stable setup key.
            continue
        setup_key = str(setup.get("setup_key") or "")
        if setup_key in blocked:
            continue
        # The course-chain engine publishes exactly one program-owned plan per
        # cutoff.  Once a newer plan has objectively replaced the old one, a
        # stale memory row must not win merely because it is iterated first.
        if authoritative_program_key and setup_key != authoritative_program_key:
            continue
        stopped_originating_setup = bool(
            current_position.get("status") == "FLAT"
            and current_position.get("last_stop_time")
            and str(current_position.get("active_setup_key") or "") == setup_key
        )
        if stopped_originating_setup:
            # A stopped setup does not become valid again merely because its
            # old trigger remains crossed.  Course-authorized re-entry first
            # needs a fresh post-stop correction/false-break candidate, which
            # the deterministic runner binds back to the original setup key.
            # This prevents a stale trigger from creating an automatic
            # re-entry with no new structural stop.
            if not (
                setup.get("reentry_status") == "AVAILABLE"
                and isinstance(current_candidate, Mapping)
                and str(current_candidate.get("setup_key") or "") == setup_key
                and current_candidate.get("entry_role") == "REENTRY"
                and current_candidate.get("decision_authority") in {"PROGRAM", "AI_HYBRID"}
            ):
                continue
        operator = str(setup.get("trigger_operator") or "NONE")
        level = _positive_number(setup.get("trigger_level"))
        if operator not in ENTRY_TRIGGER_OPERATORS or level is None:
            continue
        valid_bars = _valid_bars(setup.get("valid_bars"))
        signal = _latest_still_active_trigger(
            ordered,
            after=previous_as_of,
            through=now,
            operator=operator,
            level=level,
        )
        if signal is None:
            continue
        signal_time = _aware(signal["time"], "signal.time")
        eligible_from = signal_time + timedelta(minutes=1)
        expires_at = signal_time + timedelta(minutes=valid_bars)
        # The analyzer decides only after the current cutoff has closed, so a
        # newly accepted signal can fill no earlier than the next one-minute
        # bar.  Do not advertise ENTRY_ELIGIBLE when that first unrevealed bar
        # is already outside the setup's validity window; doing so produces an
        # impossible ENTER card followed by an immediate ENTRY_EXPIRED event.
        first_fill_after_decision = now + timedelta(minutes=1)
        status = "ENTRY_ELIGIBLE" if first_fill_after_decision <= expires_at else "EXPIRED"
        event_id = _stable_id("ENTRY_ELIGIBLE", setup_key, operator, level, signal_time.isoformat())
        return {
            "version": 1,
            "status": status,
            "event_id": event_id,
            "setup_key": setup_key,
            "setup_name": str(setup.get("name") or ""),
            "direction": str(setup.get("direction") or ""),
            "entry_role": "REENTRY" if setup.get("reentry_status") == "AVAILABLE" else "INITIAL",
            "trigger_operator": operator,
            "trigger_level": level,
            "signal_time": signal_time.isoformat(),
            "signal_close": float(signal["close"]),
            "eligible_from": eligible_from.isoformat(),
            "valid_bars": valid_bars,
            "expires_at": expires_at.isoformat(),
            **_program_entry_decision(current_candidate, setup_key=setup_key),
        }
    if isinstance(current_candidate, Mapping) and current_candidate.get("stage") == "ARMED":
        operator = str(current_candidate.get("trigger_operator") or "NONE")
        level = _positive_number(current_candidate.get("trigger_level"))
        first_seen = _optional_aware(current_candidate.get("first_seen_at"))
        # Event-driven and two-minute analysis can first observe a candidate
        # one or more closed bars after its causal first_seen_at.  It remains a
        # new candidate only when it appeared after the previous AI decision;
        # this permits next-cutoff consumption without reviving an old setup.
        candidate_is_new_since_decision = bool(
            first_seen is not None
            and first_seen <= now
            and (previous_as_of is None or first_seen > previous_as_of)
        )
        if operator in ENTRY_TRIGGER_OPERATORS and level is not None and candidate_is_new_since_decision:
            search_after = first_seen - timedelta(minutes=1)
            signal = _latest_still_active_trigger(
                ordered,
                after=search_after,
                through=now,
                operator=operator,
                level=level,
            )
            if signal is not None:
                valid_bars = _valid_bars(current_candidate.get("valid_bars"))
                setup_key = str(current_candidate.get("setup_key") or "")
                entry_role = str(current_candidate.get("entry_role") or "INITIAL")
                if entry_role == "REENTRY":
                    last_stop = _optional_aware(current_position.get("last_stop_time"))
                    if (
                        int(current_position.get("reentry_count") or 0) >= 1
                        or last_stop is None
                        or str(current_position.get("active_setup_key") or "") != setup_key
                        or now - last_stop < timedelta(minutes=1)
                    ):
                        return _empty_entry_gate(now)
                elif entry_role != "INITIAL":
                    return _empty_entry_gate(now)
                signal_time = _aware(signal["time"], "signal.time")
                expires_at = signal_time + timedelta(minutes=valid_bars)
                first_fill_after_decision = now + timedelta(minutes=1)
                status = (
                    "ENTRY_ELIGIBLE"
                    if first_fill_after_decision <= expires_at
                    else "EXPIRED"
                )
                return {
                    "version": 1,
                    "status": status,
                    "event_id": _stable_id(
                        "ENTRY_ELIGIBLE", setup_key, operator, level, signal_time.isoformat()
                    ),
                    "setup_key": setup_key,
                    "setup_name": str(current_candidate.get("setup_name") or ""),
                    "direction": str(current_candidate.get("direction") or ""),
                    "entry_role": entry_role,
                    "trigger_operator": operator,
                    "trigger_level": level,
                    "signal_time": signal_time.isoformat(),
                    "signal_close": float(signal["close"]),
                    "eligible_from": first_fill_after_decision.isoformat(),
                    "valid_bars": valid_bars,
                    "expires_at": expires_at.isoformat(),
                    **_program_entry_decision(current_candidate, setup_key=setup_key),
                }
    return _empty_entry_gate(now)


def invalidate_preentry_stop_touched_setup(
    memory: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    current_candidate: Mapping[str, Any] | None,
    position: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retire an armed plan whose structural stop was touched before entry.

    A correction setup freezes its trigger and complete-correction stop when it
    first becomes causal. If price subsequently trades through that stop while
    the account is still flat, a later close cannot revive the old trigger.
    """

    now = _aware(as_of, "as_of")
    source_memory = memory if isinstance(memory, Mapping) else {}
    result = dict(source_memory)
    audit: dict[str, Any] = {
        "version": 1,
        "status": "NONE",
        "event_id": None,
        "setup_key": None,
        "invalidated_at": None,
        "stop_price": None,
        "direction": None,
        "reason": None,
        "evaluated_at": now.isoformat(),
    }
    current_position = position if isinstance(position, Mapping) else {}
    if (
        current_position.get("status") != "FLAT"
        or isinstance(current_position.get("pending_entry"), Mapping)
        or not isinstance(current_candidate, Mapping)
        or (
            current_position.get("last_stop_time")
            and str(current_position.get("active_setup_key") or "")
            == str(current_candidate.get("setup_key") or "")
            and current_candidate.get("entry_role") != "REENTRY"
        )
        or current_candidate.get("decision_authority") not in {"PROGRAM", "AI_HYBRID"}
        or current_candidate.get("stage") != "ARMED"
    ):
        return result, audit

    setup_key = str(current_candidate.get("setup_key") or "")
    direction = str(current_candidate.get("direction") or "")
    stop = _positive_number(current_candidate.get("stop_price"))
    first_seen = _optional_aware(current_candidate.get("first_seen_at"))
    setups = source_memory.get("active_setups")
    setup_items = setups if isinstance(setups, list) else []
    matching = next(
        (
            item for item in setup_items
            if isinstance(item, Mapping)
            and str(item.get("setup_key") or "") == setup_key
            and item.get("stage") in {
                "ARMED", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED",
            }
        ),
        None,
    )
    if (
        not setup_key
        or direction not in {"LONG", "SHORT"}
        or stop is None
        or first_seen is None
    ):
        return result, audit

    touched: dict[str, Any] | None = None
    for raw in sorted(
        (dict(item) for item in bars if isinstance(item, Mapping)),
        key=lambda item: str(item.get("time") or ""),
    ):
        at = _optional_aware(raw.get("time"))
        if at is None or at <= first_seen or at > now:
            continue
        low = _positive_number(raw.get("low"))
        high = _positive_number(raw.get("high"))
        if low is None or high is None:
            continue
        if (direction == "LONG" and low <= stop) or (direction == "SHORT" and high >= stop):
            touched = raw
            break
    if touched is None:
        return result, audit

    invalidated_at = _aware(touched.get("time"), "invalidated_bar.time")
    normalized_setups: list[Any] = []
    matched_existing_setup = False
    for item in setup_items:
        if not isinstance(item, Mapping) or str(item.get("setup_key") or "") != setup_key:
            normalized_setups.append(item)
            continue
        matched_existing_setup = True
        normalized = dict(item)
        normalized["stage"] = "INVALIDATED"
        normalized["trigger"] = (
            f"進場前於{invalidated_at.strftime('%H:%M')}觸及原結構停損；"
            "舊setup退役，須等待新的完整修正。"
        )
        normalized["reentry_status"] = "NOT_APPLICABLE"
        normalized_setups.append(normalized)
    if not matched_existing_setup:
        # A deterministic candidate can first become visible to the AI on the
        # same sampled cutoff at which a later causal bar has already crossed
        # its frozen stop.  It was never present in AI memory, but the historic
        # plan is still consumed and must be published as terminal; otherwise
        # it can surface on a hidden minute and be resurrected later.
        normalized_setups.append(
            {
                "setup_key": setup_key,
                "name": str(
                    current_candidate.get("setup_name") or "程式化結構候選"
                ),
                "direction": direction,
                "stage": "INVALIDATED",
                "trigger": (
                    f"進場前於{invalidated_at.strftime('%H:%M')}觸及原結構停損；"
                    "舊setup退役，須等待新的完整修正。"
                ),
                "stop": f"原程式結構停損{stop:g}點已被觸及，不得沿用。",
                "reentry_status": "NOT_APPLICABLE",
                "trigger_level": current_candidate.get("trigger_level"),
                "trigger_operator": current_candidate.get("trigger_operator"),
                "valid_bars": current_candidate.get("valid_bars"),
            }
        )
    result["active_setups"] = normalized_setups
    audit.update(
        {
            "status": "INVALIDATED_BEFORE_ENTRY",
            "event_id": _stable_id(
                "PROGRAM_SETUP_INVALIDATED", setup_key, invalidated_at.isoformat(), stop
            ),
            "setup_key": setup_key,
            "setup_name": str(current_candidate.get("setup_name") or "程式化結構候選"),
            "invalidated_at": invalidated_at.isoformat(),
            "stop_price": stop,
            "direction": direction,
            "reason": "STRUCTURAL_STOP_TOUCHED_BEFORE_ENTRY",
        }
    )
    return result, audit


def _program_entry_decision(
    current_candidate: Mapping[str, Any] | None,
    *,
    setup_key: str,
) -> dict[str, Any]:
    """Publish the immutable action when the candidate is program-owned.

    The AI may explain the setup, but cannot veto an already triggered setup or
    substitute its own stop.  Candidates without a locked program stop retain
    the older reviewer contract for backward-compatible replay fixtures.
    """

    if (
        not isinstance(current_candidate, Mapping)
        or str(current_candidate.get("setup_key") or "") != setup_key
        or current_candidate.get("decision_authority") not in {"PROGRAM", "AI_HYBRID"}
    ):
        return {}
    stop = _positive_number(current_candidate.get("stop_price"))
    if stop is None:
        return {}
    authority = str(current_candidate.get("decision_authority"))
    return {
        "decision_authority": authority,
        "required_position_action": "ENTER" if authority == "PROGRAM" else None,
        "required_candidate_source": current_candidate.get("candidate_source"),
        "required_entry_strategy": current_candidate.get("entry_strategy"),
        "required_facts_hash": current_candidate.get("facts_hash"),
        "required_behavior_policy": current_candidate.get("behavior_policy"),
        "required_stop_price": stop,
        "required_entry_rejection_reason": "NONE",
        "required_behavior_max_wait_bars": current_candidate.get(
            "behavior_max_wait_bars"
        ),
        "required_behavior_trigger_level": current_candidate.get(
            "behavior_trigger_level"
        ),
        "required_behavior_obstacles": [
            dict(item)
            for item in current_candidate.get("behavior_obstacles", [])
            if isinstance(item, Mapping)
        ],
    }


def _canonical_behavior_obstacle(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the program-owned portion of a post-entry behavior level.

    ``label`` and ``reaction`` in an AI response are presentation prose. They
    must not become part of position state because the behavior audit is a
    deterministic artifact. Keep only the objective level, role, and its
    causal source; derive a stable display label from those hard fields.
    """

    price = _positive_number(item.get("price"))
    role = str(item.get("role") or "")
    if price is None or role not in {"TRIGGER", "CHECKPOINT", "HARD_OBSTACLE"}:
        return None
    result: dict[str, Any] = {"price": price, "role": role}
    source_time = item.get("source_time")
    source_field = str(item.get("source_field") or "")
    if source_time not in {None, ""}:
        result["source_time"] = str(source_time)
    if source_field:
        result["source_field"] = source_field
    result["label"] = _canonical_behavior_obstacle_label(result)
    return result


def _canonical_behavior_obstacles(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [
        obstacle
        for item in items
        if isinstance(item, Mapping)
        and (obstacle := _canonical_behavior_obstacle(item)) is not None
    ]


def _canonical_behavior_obstacle_label(item: Mapping[str, Any]) -> str:
    role = str(item.get("role") or "")
    source_field = str(item.get("source_field") or "")
    raw_time = str(item.get("source_time") or "")
    time_label = ""
    if raw_time:
        parsed = _optional_aware(raw_time)
        if parsed is not None:
            time_label = parsed.strftime("%H:%M")
        elif len(raw_time) >= 5 and raw_time[2:3] == ":":
            time_label = raw_time[:5]
    field_label = {
        "open": "開盤",
        "high": "高點",
        "low": "低點",
        "close": "收盤",
    }.get(source_field, "")
    if time_label and field_label:
        return f"{time_label}{field_label}"
    if time_label:
        return f"{time_label}結構點"
    return {
        "TRIGGER": "觸發線",
        "CHECKPOINT": "結構檢查點",
        "HARD_OBSTACLE": "硬障礙",
    }.get(role, "結構點")


def expire_stale_armed_setups(
    memory: Mapping[str, Any] | None,
    *,
    armed_at_by_key: Mapping[str, str],
    as_of: str,
    protected_setup_key: object = None,
    structural_setup_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Programmatically consume setups whose armed window has elapsed.

    ``valid_bars`` is counted in real one-minute bars from the first validated
    ARMED state, not in later AI sampling calls.  The terminal NO_CHASE state
    remains in memory for audit and prevents the same setup key from being
    silently revived.
    """

    current = dict(memory) if isinstance(memory, Mapping) else {}
    setups = current.get("active_setups")
    if not isinstance(setups, list):
        return current
    now = _aware(as_of, "as_of")
    active_stages = {"ARMED", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED"}
    structural = {str(item) for item in (structural_setup_keys or set()) if str(item)}
    normalized: list[Any] = []
    for raw in setups:
        if not isinstance(raw, Mapping) or raw.get("stage") not in active_stages:
            normalized.append(dict(raw) if isinstance(raw, Mapping) else raw)
            continue
        setup = dict(raw)
        setup_key = str(setup.get("setup_key") or "")
        if protected_setup_key and setup_key == str(protected_setup_key):
            normalized.append(setup)
            continue
        if setup_key in structural:
            # New v34 paths use event invalidation (defense/anchor/extreme),
            # not an arbitrary number of elapsed minutes, while waiting for
            # their closed-bar trigger.  The post-trigger pending fill still
            # keeps the gate's short, deterministic expiry.
            normalized.append(setup)
            continue
        armed_at = _optional_aware(armed_at_by_key.get(setup_key))
        if armed_at is None:
            armed_at = _optional_aware(current.get("as_of"))
        valid_bars = _valid_bars(setup.get("valid_bars"))
        if armed_at is not None and now > armed_at + timedelta(minutes=valid_bars):
            setup["stage"] = "NO_CHASE"
            setup["trigger"] = (
                f"本setup的{valid_bars}根1分K有效窗已過期；不得重用舊觸發，"
                "須等待新的同級結構並建立新setup_key。"
            )
        normalized.append(setup)
    current["active_setups"] = normalized
    return current


def schedule_pending_entry(
    position: Mapping[str, Any],
    analysis: Mapping[str, Any],
    gate: Mapping[str, Any],
    *,
    as_of: str,
) -> dict[str, Any]:
    if gate.get("status") != "ENTRY_ELIGIBLE":
        raise ReplayExecutionGateError("沒有有效ENTRY_ELIGIBLE事件，不得排入成交。")
    action = analysis.get("action")
    reading = analysis.get("course_reading")
    if not isinstance(action, Mapping) or action.get("position_action") != "ENTER":
        raise ReplayExecutionGateError("只有驗證完成的ENTER決策才可排入下一根成交。")
    if not isinstance(reading, Mapping) or reading.get("setup_stage") != "ENTRY_ELIGIBLE":
        raise ReplayExecutionGateError("排入成交時setup_stage必須為ENTRY_ELIGIBLE。")
    if position.get("status") != "FLAT" or isinstance(position.get("pending_entry"), Mapping):
        raise ReplayExecutionGateError("已有持倉或待成交訊號，不得重複排入。")
    direction = str(action.get("direction") or "")
    if direction != gate.get("direction") or direction not in {"LONG", "SHORT"}:
        raise ReplayExecutionGateError("AI進場方向必須與程式觸發的setup一致。")
    stop = _positive_number(action.get("stop_price"))
    if stop is None:
        raise ReplayExecutionGateError("接受進場必須提供結構停損。")
    required_stop = _positive_number(gate.get("required_stop_price"))
    if required_stop is not None and abs(stop - required_stop) > 1e-6:
        raise ReplayExecutionGateError("排入成交的停損必須符合程式化交易政策。")
    entry_role = str(action.get("entry_role") or "")
    if entry_role == "REENTRY":
        last_stop = _optional_aware(position.get("last_stop_time"))
        if last_stop is None:
            raise ReplayExecutionGateError("沒有先前停損事件，不得排入再進場。")
        if int(position.get("reentry_count") or 0) >= 1:
            raise ReplayExecutionGateError("同一setup最多只允許再進場1次。")
        if position.get("active_setup_key") != gate.get("setup_key"):
            raise ReplayExecutionGateError("再進場必須沿用原setup_key。")
        if _aware(as_of, "as_of") - last_stop < timedelta(minutes=1):
            raise ReplayExecutionGateError("停損後至少等待1根完整1分K才能排入再進場。")
    elif entry_role != "INITIAL":
        raise ReplayExecutionGateError("接受進場的entry_role必須為INITIAL或REENTRY。")
    current = dict(position)
    current.update(
        {
            "as_of": as_of,
            "pending_entry": {
                "event_id": gate.get("event_id"),
                "setup_key": gate.get("setup_key"),
                "setup_name": gate.get("setup_name"),
                "direction": direction,
                "entry_role": entry_role,
                "signal_time": gate.get("signal_time"),
                "signal_close": gate.get("signal_close"),
                # With two-bar/sparse analysis the original signal may precede
                # the AI's decision. Never fill at an open already revealed to it.
                "accepted_at": as_of,
                "eligible_from": max(
                    _aware(gate.get("eligible_from"), "gate.eligible_from"),
                    _aware(as_of, "as_of") + timedelta(minutes=1),
                ).isoformat(),
                "expires_at": gate.get("expires_at"),
                "valid_bars": gate.get("valid_bars"),
                "stop_price": stop,
                "candidate_source": gate.get("required_candidate_source"),
                "entry_strategy": gate.get("required_entry_strategy"),
                "facts_hash": gate.get("required_facts_hash"),
                "main_strategy_family": reading.get("main_strategy_family"),
                "post_achievement_policy": (
                    "Q2_TARGET_OR_CONFIRMED_TREND_STRUCTURE_MANAGEMENT"
                    if reading.get("main_strategy_family") == "Q2"
                    else "Q4_STRUCTURE_AND_COPY_MANAGEMENT"
                    if reading.get("main_strategy_family") == "Q4"
                    else "PREDECLARED_BEHAVIOR_AND_STRUCTURE_MANAGEMENT"
                ),
                "trigger": action.get("trigger"),
                "expected_behavior": action.get("expected_behavior"),
                "behavior_max_wait_bars": (
                    gate.get("required_behavior_max_wait_bars")
                    if gate.get("decision_authority") == "PROGRAM"
                    else action.get("max_wait_bars")
                ),
                # The program already owns the objective role/price/source of
                # every behavior level. AI_HYBRID may explain those levels,
                # but its free-form label/reaction must not enter deterministic
                # position state or hard-fact hashes.
                "behavior_obstacles": _canonical_behavior_obstacles(
                    gate.get("required_behavior_obstacles")
                    if isinstance(gate.get("required_behavior_obstacles"), list)
                    else action.get("obstacles")
                ),
                # Entry qualification and post-entry behavior can use
                # different levels.  For example an OR retest may enter above
                # the retest mini-range while the course-native hold boundary
                # remains the OR edge.  Preserve the predeclared behavior
                # level for both PROGRAM and AI_HYBRID decisions; substituting
                # the entry trigger makes the first normal pullback look like
                # an immediate behavior failure.
                "behavior_trigger_level": gate.get(
                    "required_behavior_trigger_level"
                ),
                "behavior_policy": gate.get("required_behavior_policy"),
            },
            "last_action": "ENTRY_QUEUED",
            "last_reason": analysis.get("notification_reason"),
        }
    )
    return current


def fill_pending_entry(
    position: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Fill an accepted signal at the first following bar open, never its signal close."""

    current = dict(position)
    pending = current.get("pending_entry")
    if not isinstance(pending, Mapping):
        return current, None
    if current.get("status") != "FLAT":
        raise ReplayExecutionGateError("待成交訊號存在時持倉必須仍為FLAT。")
    now = _aware(as_of, "as_of")
    eligible_from = _aware(pending.get("eligible_from"), "pending.eligible_from")
    expires_at = _aware(pending.get("expires_at"), "pending.expires_at")
    candidates = sorted(
        (
            dict(item)
            for item in bars
            if isinstance(item, Mapping)
            and (at := _optional_aware(item.get("time"))) is not None
            and eligible_from <= at <= now
        ),
        key=lambda item: str(item.get("time")),
    )
    first = candidates[0] if candidates else None
    if first is None:
        if now > expires_at:
            current.update(
                {
                    "as_of": as_of,
                    "pending_entry": None,
                    "last_action": "ENTRY_EXPIRED",
                    "last_reason": "進場事件超過有效根數，未追認成交。",
                }
            )
            return current, {
                "event_type": "ENTRY_EXPIRED",
                "event_id": pending.get("event_id"),
                "signal_time": pending.get("signal_time"),
                "expires_at": pending.get("expires_at"),
                "recorded_at": as_of,
            }
        return current, None
    fill_time = _aware(first.get("time"), "fill.time")
    if fill_time > expires_at:
        current.update(
            {
                "as_of": as_of,
                "pending_entry": None,
                "last_action": "ENTRY_EXPIRED",
                "last_reason": "第一個可成交K已超過訊號有效根數，未追價。",
            }
        )
        return current, {
            "event_type": "ENTRY_EXPIRED",
            "event_id": pending.get("event_id"),
            "signal_time": pending.get("signal_time"),
            "expires_at": pending.get("expires_at"),
            "recorded_at": as_of,
        }
    fill_price = _positive_number(first.get("open"))
    stop_price = _positive_number(pending.get("stop_price"))
    direction = str(pending.get("direction") or "")
    if fill_price is None or stop_price is None:
        raise ReplayExecutionGateError("待成交事件缺少開盤價或停損價。")
    gap_beyond_stop = (
        direction == "LONG" and fill_price <= stop_price
    ) or (
        direction == "SHORT" and fill_price >= stop_price
    )
    if gap_beyond_stop:
        current.update(
            {
                "as_of": as_of,
                "pending_entry": None,
                "last_action": "ENTRY_REJECTED_GAP",
                "last_reason": "第一個可成交價已越過原結構停損，取消進場。",
            }
        )
        return current, {
            "event_type": "ENTRY_REJECTED_GAP",
            "event_id": pending.get("event_id"),
            "signal_time": pending.get("signal_time"),
            "fill_time": fill_time.isoformat(),
            "fill_price": fill_price,
            "stop_price": stop_price,
            "recorded_at": as_of,
        }
    entry_role = str(pending.get("entry_role") or "INITIAL")
    current.update(
        {
            "as_of": as_of,
            "status": direction,
            "entry_time": fill_time.isoformat(),
            "entry_price": fill_price,
            "stop_price": stop_price,
            "direction": direction,
            "active_setup_key": pending.get("setup_key"),
            "pending_entry": None,
            "last_action": "ENTER",
            "last_reason": "以前一根已收盤觸發後的第一個可成交價建立模擬倉。",
            "behavior_plan": {
                "started_at": fill_time.isoformat(),
                "initial_stop_price": stop_price,
                "expected_behavior": pending.get("expected_behavior"),
                "max_wait_bars": pending.get("behavior_max_wait_bars"),
                "trigger_level": pending.get("behavior_trigger_level"),
                "obstacles": list(pending.get("behavior_obstacles") or []),
                "policy": pending.get("behavior_policy"),
                "entry_strategy": pending.get("entry_strategy"),
                "candidate_source": pending.get("candidate_source"),
                "facts_hash": pending.get("facts_hash"),
                "main_strategy_family": pending.get("main_strategy_family"),
                "post_achievement_policy": pending.get("post_achievement_policy"),
            },
        }
    )
    if entry_role == "REENTRY":
        current["reentry_count"] = 1
    else:
        current["reentry_count"] = 0
        current["last_stop_time"] = None
    return current, {
        "event_type": "ENTRY_FILLED",
        "event_id": pending.get("event_id"),
        "setup_key": pending.get("setup_key"),
        "setup_name": pending.get("setup_name"),
        "direction": direction,
        "entry_role": entry_role,
        "signal_time": pending.get("signal_time"),
        "fill_time": fill_time.isoformat(),
        "fill_price": fill_price,
        "stop_price": stop_price,
        "trigger": pending.get("trigger"),
        "expected_behavior": pending.get("expected_behavior"),
        "behavior_max_wait_bars": pending.get("behavior_max_wait_bars"),
        "behavior_obstacles": list(pending.get("behavior_obstacles") or []),
        "behavior_trigger_level": pending.get("behavior_trigger_level"),
        "behavior_policy": pending.get("behavior_policy"),
        "entry_strategy": pending.get("entry_strategy"),
        "candidate_source": pending.get("candidate_source"),
        "facts_hash": pending.get("facts_hash"),
        "main_strategy_family": pending.get("main_strategy_family"),
        "post_achievement_policy": pending.get("post_achievement_policy"),
        "recorded_at": as_of,
    }


def schedule_pending_exit(
    position: Mapping[str, Any],
    analysis: Mapping[str, Any],
    audit: Mapping[str, Any],
    *,
    as_of: str,
) -> dict[str, Any]:
    """Keep the position live until a close-confirmed behavior exit can fill.

    A behavior failure is only known after its signal bar closes.  The exit is
    therefore committed at that close and filled at the first later 1-minute
    open.  Keeping an explicit pending exit prevents the audit state from
    claiming FLAT one minute before the simulated fill actually exists.
    """

    if audit.get("status") != "PENDING_FILL":
        raise ReplayExecutionGateError("只有PENDING_FILL的程式出場才能排入下一根開盤。")
    action = analysis.get("action")
    if not isinstance(action, Mapping) or action.get("position_action") != "EXIT":
        raise ReplayExecutionGateError("排入程式出場時analysis.action必須為EXIT。")
    direction = str(position.get("status") or "")
    if direction not in {"LONG", "SHORT"}:
        raise ReplayExecutionGateError("空手時不得排入程式出場。")
    if isinstance(position.get("pending_entry"), Mapping):
        raise ReplayExecutionGateError("持倉出場與待成交進場不得同時存在。")
    if isinstance(position.get("pending_exit"), Mapping):
        raise ReplayExecutionGateError("已有待成交出場，不得重複排入。")
    if str(audit.get("direction") or "") != direction:
        raise ReplayExecutionGateError("程式出場方向與持倉方向不一致。")
    signal_time = _aware(audit.get("signal_time"), "audit.signal_time")
    if signal_time != _aware(as_of, "as_of"):
        raise ReplayExecutionGateError("首次排入程式出場時必須位於失效訊號收盤。")
    current = dict(position)
    current.update(
        {
            "as_of": as_of,
            "pending_exit": {
                "setup_key": position.get("active_setup_key"),
                "direction": direction,
                "entry_time": position.get("entry_time"),
                "entry_price": position.get("entry_price"),
                "signal_time": signal_time.isoformat(),
                "eligible_from": (signal_time + timedelta(minutes=1)).isoformat(),
                "reason_code": audit.get("reason_code"),
            },
            "last_action": "EXIT_QUEUED",
            "last_reason": "事前應有行為於收盤失效；出場已鎖定於下一根1分K開盤。",
        }
    )
    return current


def derive_analysis_exit_audit(
    position: Mapping[str, Any],
    analysis: Mapping[str, Any],
    *,
    as_of: str,
) -> dict[str, Any]:
    """Turn an AI course exit into the same causal next-open contract.

    The deterministic behavior scanner is not the only legitimate source of
    an exit.  The AI may identify a course-defined loss of motive before the
    generic maximum-wait rule expires.  That decision is still known only
    after the signal bar closes, so it must be queued and filled at the first
    later 1-minute open instead of making the position disappear at the signal
    close.
    """

    action = analysis.get("action")
    status = str(position.get("status") or "")
    if not isinstance(action, Mapping) or action.get("position_action") != "EXIT":
        return {"version": 1, "status": "NOT_APPLICABLE", "as_of": as_of}
    if status not in {"LONG", "SHORT"}:
        raise ReplayExecutionGateError("空手時不得建立AI課程出場排程。")
    if isinstance(position.get("pending_exit"), Mapping):
        raise ReplayExecutionGateError("已有待成交出場，不得重複建立AI課程出場排程。")
    return {
        "version": 1,
        "status": "PENDING_FILL",
        "direction": status,
        "signal_time": as_of,
        "reason_code": "AI_COURSE_EXIT",
    }


def fill_pending_exit(
    position: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Fill a committed behavior exit at the first causally available open."""

    current = dict(position)
    pending = current.get("pending_exit")
    if not isinstance(pending, Mapping):
        return current, None
    direction = str(current.get("status") or "")
    if direction not in {"LONG", "SHORT"}:
        raise ReplayExecutionGateError("待成交出場存在時持倉必須仍為LONG或SHORT。")
    if isinstance(current.get("pending_entry"), Mapping):
        raise ReplayExecutionGateError("待成交出場期間不得同時存在待成交進場。")
    if str(pending.get("direction") or "") != direction:
        raise ReplayExecutionGateError("待成交出場方向與目前持倉不一致。")
    now = _aware(as_of, "as_of")
    eligible_from = _aware(pending.get("eligible_from"), "pending_exit.eligible_from")
    first = next(
        iter(
            sorted(
                (
                    dict(item)
                    for item in bars
                    if isinstance(item, Mapping)
                    and (at := _optional_aware(item.get("time"))) is not None
                    and eligible_from <= at <= now
                ),
                key=lambda item: str(item.get("time")),
            )
        ),
        None,
    )
    if first is None:
        return current, None
    fill_time = _aware(first.get("time"), "pending_exit.fill_time")
    fill_price = _positive_number(first.get("open"))
    if fill_price is None:
        raise ReplayExecutionGateError("待成交出場的下一根1分K缺少有效開盤價。")
    event_id = _stable_id(
        "EXIT_FILLED",
        pending.get("setup_key"),
        pending.get("entry_time"),
        pending.get("reason_code"),
        pending.get("signal_time"),
        fill_time.isoformat(),
        fill_price,
    )
    event = {
        "event_type": "EXIT_FILLED",
        "event_id": event_id,
        "setup_key": pending.get("setup_key"),
        "direction": direction,
        "entry_time": pending.get("entry_time"),
        "entry_price": pending.get("entry_price"),
        "signal_time": pending.get("signal_time"),
        "fill_time": fill_time.isoformat(),
        "fill_price": fill_price,
        "reason_code": pending.get("reason_code"),
        "recorded_at": as_of,
    }
    current.update(
        {
            "as_of": as_of,
            "status": "FLAT",
            "entry_time": None,
            "entry_price": None,
            "stop_price": None,
            "direction": None,
            "active_setup_key": None,
            "last_stop_time": None,
            "reentry_count": 0,
            "pending_exit": None,
            "behavior_plan": None,
            "last_action": "EXIT",
            "last_reason": "事前鎖定的行為失效出場已於下一根1分K開盤成交。",
        }
    )
    return current, event


def derive_protective_stop_audit(
    position: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    after: str | None = None,
) -> dict[str, Any]:
    """Deterministically scan newly visible bars for an active protective stop.

    The AI may explain the stop, but it must not decide whether a known order
    was touched.  ``after`` is exclusive.  A newly filled position can pass a
    timestamp immediately before its fill bar so the fill bar itself is
    checked.  A gap through the stop fills at the opening price; otherwise the
    declared stop price is used.
    """

    now = _aware(as_of, "as_of")
    direction = str(position.get("status") or "")
    stop_price = _positive_number(position.get("stop_price"))
    if direction not in {"LONG", "SHORT"} or stop_price is None:
        return {"version": 1, "status": "NOT_APPLICABLE", "as_of": now.isoformat()}

    scan_after = _optional_aware(after)
    if scan_after is None:
        scan_after = _optional_aware(position.get("as_of"))
    if scan_after is None:
        scan_after = _optional_aware(position.get("entry_time"))
    if scan_after is None:
        raise ReplayExecutionGateError("持倉缺少可用的停損掃描起點。")

    ordered = sorted(
        (
            dict(item)
            for item in bars
            if isinstance(item, Mapping)
            and (at := _optional_aware(item.get("time"))) is not None
            and scan_after < at <= now
        ),
        key=lambda item: str(item.get("time")),
    )
    result: dict[str, Any] = {
        "version": 1,
        "status": "ACTIVE",
        "as_of": now.isoformat(),
        "direction": direction,
        "setup_key": position.get("active_setup_key"),
        "entry_time": position.get("entry_time"),
        "entry_price": _positive_number(position.get("entry_price")),
        "stop_price": stop_price,
        "scanned_after": scan_after.isoformat(),
        "trigger_time": None,
        "fill_time": None,
        "fill_price": None,
        "gap_through_stop": False,
        "event_id": None,
    }
    for bar in ordered:
        low = _positive_number(bar.get("low"))
        high = _positive_number(bar.get("high"))
        open_price = _positive_number(bar.get("open"))
        at = _aware(bar.get("time"), "bar.time")
        if low is None or high is None or open_price is None:
            continue
        touched = (direction == "LONG" and low <= stop_price) or (
            direction == "SHORT" and high >= stop_price
        )
        if not touched:
            continue
        gap = (direction == "LONG" and open_price <= stop_price) or (
            direction == "SHORT" and open_price >= stop_price
        )
        fill_price = open_price if gap else stop_price
        event_id = _stable_id(
            "STOP_FILLED",
            position.get("active_setup_key"),
            position.get("entry_time"),
            stop_price,
            at.isoformat(),
            fill_price,
        )
        result.update(
            {
                "status": "TRIGGERED",
                "trigger_time": at.isoformat(),
                "fill_time": at.isoformat(),
                "fill_price": fill_price,
                "gap_through_stop": gap,
                "event_id": event_id,
            }
        )
        break
    return result


def advance_program_risk_controls(
    position: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    after: str | None = None,
    structural_protection_events: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Advance stops, new favorable defenses and +1.5R on every revealed 1分K.

    AI sampling may be sparse, but a resting stop and the formal +1.5R
    near-cost rule are not.  Existing protection is checked before considering
    a new close-based stop improvement on each bar; this is conservative when
    OHLC cannot reveal whether the intrabar high or low occurred first.
    """

    current = dict(position)
    now = _aware(as_of, "as_of")
    direction = str(current.get("status") or "")
    stop = _positive_number(current.get("stop_price"))
    entry = _positive_number(current.get("entry_price"))
    entry_time = _optional_aware(current.get("entry_time"))
    plan = current.get("behavior_plan")
    initial_stop = (
        _positive_number(plan.get("initial_stop_price"))
        if isinstance(plan, Mapping)
        else None
    )
    if initial_stop is None:
        initial_stop = stop
    if direction not in {"LONG", "SHORT"} or stop is None or entry is None or entry_time is None:
        return current, [], {
            "version": 1,
            "status": "NOT_APPLICABLE",
            "as_of": now.isoformat(),
        }
    scan_after = _optional_aware(after) or _optional_aware(current.get("as_of")) or entry_time
    ordered = sorted(
        (
            dict(item)
            for item in bars
            if isinstance(item, Mapping)
            and (at := _optional_aware(item.get("time"))) is not None
            and scan_after < at <= now
        ),
        key=lambda item: str(item.get("time")),
    )
    structural_by_time: dict[str, list[dict[str, Any]]] = {}
    for raw in structural_protection_events or []:
        if not isinstance(raw, Mapping) or raw.get("event_type") != "PROGRAM_DEFENSE_AVAILABLE":
            continue
        if raw.get("direction") != direction:
            continue
        effective = _optional_aware(raw.get("event_time"))
        candidate_stop = _positive_number(raw.get("stop_price"))
        if effective is None or candidate_stop is None or effective <= entry_time or effective > now:
            continue
        structural_by_time.setdefault(effective.isoformat(), []).append(dict(raw))
    events: list[dict[str, Any]] = []
    risk = abs(entry - initial_stop) if initial_stop is not None else 0.0
    audit: dict[str, Any] = {
        "version": 2,
        "status": "ACTIVE",
        "as_of": now.isoformat(),
        "direction": direction,
        "setup_key": current.get("active_setup_key"),
        "entry_time": entry_time.isoformat(),
        "entry_price": entry,
        "stop_price": stop,
        "scanned_after": scan_after.isoformat(),
        "trigger_time": None,
        "fill_time": None,
        "fill_price": None,
        "gap_through_stop": False,
        "event_id": None,
    }
    for bar in ordered:
        at = _aware(bar.get("time"), "bar.time")
        open_price = _positive_number(bar.get("open"))
        high = _positive_number(bar.get("high"))
        low = _positive_number(bar.get("low"))
        close = _positive_number(bar.get("close"))
        if None in {open_price, high, low, close}:
            continue
        touched = (direction == "LONG" and low <= stop) or (
            direction == "SHORT" and high >= stop
        )
        if touched:
            gap = (direction == "LONG" and open_price <= stop) or (
                direction == "SHORT" and open_price >= stop
            )
            fill_price = open_price if gap else stop
            audit.update(
                {
                    "status": "TRIGGERED",
                    "stop_price": stop,
                    "trigger_time": at.isoformat(),
                    "fill_time": at.isoformat(),
                    "fill_price": fill_price,
                    "gap_through_stop": gap,
                    "event_id": _stable_id(
                        "STOP_FILLED",
                        current.get("active_setup_key"),
                        current.get("entry_time"),
                        stop,
                        at.isoformat(),
                        fill_price,
                    ),
                }
            )
            break
        if risk <= 0:
            favorable = 0.0
        else:
            favorable = high - entry if direction == "LONG" else entry - low
        still_unprotected = stop < entry if direction == "LONG" else stop > entry
        favorable_close = close > entry if direction == "LONG" else close < entry
        if risk > 0 and favorable / risk >= 1.5 and still_unprotected and favorable_close:
            old_stop = stop
            stop = entry
            current["stop_price"] = stop
            current["as_of"] = at.isoformat()
            audit["stop_price"] = stop
            events.append(
                {
                    "event_type": "STOP_PROTECTION_MOVED",
                    "event_id": _stable_id(
                        "STOP_PROTECTION_MOVED",
                        current.get("active_setup_key"),
                        current.get("entry_time"),
                        at.isoformat(),
                        stop,
                    ),
                    "setup_key": current.get("active_setup_key"),
                    "direction": direction,
                    "effective_after": at.isoformat(),
                    "old_stop_price": old_stop,
                    "stop_price": stop,
                    "protection_reason": "PROFIT_MILESTONE_1_5R_NEAR_COST",
                    "recorded_at": now.isoformat(),
                }
            )
        # A defense becomes actionable only after the bar that confirms it has
        # closed. It may improve the stop for the next bar, never retroactively
        # inside its own confirmation candle.
        for protection in structural_by_time.get(at.isoformat(), []):
            candidate_stop = _positive_number(protection.get("stop_price"))
            if candidate_stop is None:
                continue
            improves = (
                direction == "LONG" and stop < candidate_stop < close
            ) or (
                direction == "SHORT" and stop > candidate_stop > close
            )
            if not improves:
                continue
            old_stop = stop
            stop = candidate_stop
            current["stop_price"] = stop
            current["as_of"] = at.isoformat()
            audit["stop_price"] = stop
            events.append(
                {
                    "event_type": "STOP_PROTECTION_MOVED",
                    "event_id": _stable_id(
                        "STOP_PROTECTION_MOVED",
                        current.get("active_setup_key"),
                        current.get("entry_time"),
                        at.isoformat(),
                        stop,
                        protection.get("source_defense_id"),
                    ),
                    "setup_key": current.get("active_setup_key"),
                    "direction": direction,
                    "effective_after": at.isoformat(),
                    "old_stop_price": old_stop,
                    "stop_price": stop,
                    "protection_reason": "NEW_FAVORABLE_DEFENSE",
                    "source_anchor_id": protection.get("source_anchor_id"),
                    "source_defense_id": protection.get("source_defense_id"),
                    "source_time": protection.get("source_time"),
                    "recorded_at": now.isoformat(),
                }
            )
    current["as_of"] = now.isoformat()
    return current, events, audit


def derive_position_behavior_audit(
    position: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
) -> dict[str, Any]:
    """Measure the accepted setup's promised behavior without making a trade decision."""

    now = _aware(as_of, "as_of")
    direction = str(position.get("status") or "")
    plan = position.get("behavior_plan")
    if direction not in {"LONG", "SHORT"} or not isinstance(plan, Mapping):
        return {"version": 1, "status": "NOT_APPLICABLE", "as_of": now.isoformat()}
    entry_time = _optional_aware(position.get("entry_time"))
    entry_price = _positive_number(position.get("entry_price"))
    if entry_time is None or entry_price is None:
        return {"version": 1, "status": "NOT_APPLICABLE", "as_of": now.isoformat()}
    ordered = sorted(
        (
            dict(item)
            for item in bars
            if isinstance(item, Mapping)
            and (at := _optional_aware(item.get("time"))) is not None
            and entry_time <= at <= now
        ),
        key=lambda item: str(item.get("time")),
    )
    max_wait = plan.get("max_wait_bars")
    max_wait = int(max_wait) if isinstance(max_wait, int) and not isinstance(max_wait, bool) else None
    checkpoints = []
    for item in plan.get("obstacles") or []:
        if not isinstance(item, Mapping) or item.get("role") != "CHECKPOINT":
            continue
        price = _positive_number(item.get("price"))
        if price is None:
            continue
        if (direction == "LONG" and price > entry_price) or (direction == "SHORT" and price < entry_price):
            checkpoints.append((price, _canonical_behavior_obstacle_label(item)))
    # A nearby structure level is often included as the first inspection point,
    # but merely closing a few points beyond it must not consume the whole
    # strategy-specific behavior window.  Prefer the nearest checkpoint that
    # represents at least 0.25R of favorable progress.  This keeps a genuine
    # completed move under structural management while preventing a tiny first
    # hurdle from masking an otherwise expired/no-progress entry thesis.
    initial_stop = _positive_number(plan.get("initial_stop_price"))
    if initial_stop is None:
        initial_stop = _positive_number(position.get("stop_price"))
    initial_r = abs(entry_price - initial_stop) if initial_stop is not None else None
    meaningful_checkpoints = checkpoints
    if initial_r is not None and initial_r > 0:
        filtered = [
            item
            for item in checkpoints
            if abs(item[0] - entry_price) / initial_r >= 0.25
        ]
        # An existing checkpoint that is too close is not meaningful merely
        # because no farther historical level is available.  Preserve the
        # empty filtered result so the policy-specific 0.25R behavior floor
        # below is used instead of declaring success after a negligible move.
        meaningful_checkpoints = filtered
    behavior_policy = str(plan.get("policy") or "")
    fast_extension_policies = {
        "Q1_YIZHI_FAST_CONTINUATION",
        "Q2_FAST_REVERSAL_CONFIRMATION",
    }
    if (
        not meaningful_checkpoints
        and behavior_policy in fast_extension_policies
        and initial_r is not None
        and initial_r > 0
    ):
        progress_price = (
            entry_price + initial_r * 0.25
            if direction == "LONG"
            else entry_price - initial_r * 0.25
        )
        meaningful_checkpoints = [(
            progress_price,
            "Q1早期0.25R動能檢查"
            if behavior_policy == "Q1_YIZHI_FAST_CONTINUATION"
            else "Q2回轉0.25R動能檢查",
        )]
    if (
        behavior_policy == "Q4_AGGRESSIVE_STRUCTURE_AND_COPY_REVIEW"
        and initial_r is not None
        and initial_r > 0
    ):
        # A Q4 entry promises timely favorable progress *toward* the next
        # structure checkpoint; it does not promise that a possibly distant
        # prior high/low will be fully reached inside the short entry window.
        # Once a closed bar has advanced 0.25R, consume that entry-validation
        # clock and hand the open trade to Q4 structure/copy management.  The
        # declared obstacle remains available to the AI as an inspection point,
        # but is not silently converted into a fixed profit target.
        progress_price = (
            entry_price + initial_r * 0.25
            if direction == "LONG"
            else entry_price - initial_r * 0.25
        )
        meaningful_checkpoints = [(
            progress_price,
            "Q4順向0.25R進展檢查",
        )]
    checkpoint = (
        min(meaningful_checkpoints, key=lambda item: item[0])
        if direction == "LONG" and meaningful_checkpoints
        else max(meaningful_checkpoints, key=lambda item: item[0])
        if direction == "SHORT" and meaningful_checkpoints
        else None
    )
    result = {
        "version": 1,
        "status": "ACTIVE",
        "as_of": now.isoformat(),
        "direction": direction,
        "entry_time": entry_time.isoformat(),
        "entry_price": entry_price,
        "bars_since_entry": len(ordered),
        "max_wait_bars": max_wait,
        "trigger_level": _positive_number(plan.get("trigger_level")),
        "checkpoint_price": checkpoint[0] if checkpoint else None,
        "checkpoint_label": checkpoint[1] if checkpoint else None,
        "initial_r": initial_r,
        "minimum_meaningful_progress_r": 0.25 if initial_r is not None else None,
        "hold_price": None,
        "hold_label": None,
        "achieved_at": None,
        "consecutive_failed_hold_closes": 0,
    }
    if checkpoint is None and initial_r is not None and initial_r > 0:
        # Every accepted setup needs an observable minimum behavior even when
        # no historic structure happens to sit in front of entry.  Without
        # this fallback, max_wait_bars was silently bypassed forever under the
        # old ACTIVE_NO_CHECKPOINT state.  This is an execution/audit floor,
        # not a profit target and not a course-specific fixed-points rule.
        progress_price = (
            entry_price + initial_r * 0.25
            if direction == "LONG"
            else entry_price - initial_r * 0.25
        )
        checkpoint = (progress_price, "進場後最低0.25R行為檢查")
        result["checkpoint_price"] = progress_price
        result["checkpoint_label"] = checkpoint[1]
    if checkpoint is None:
        # A malformed legacy position can still lack an initial stop/R.  Do
        # not call this healthy active management: make the missing contract
        # explicit so validators and reports can fail closed.
        result["status"] = "INVALID_BEHAVIOR_PLAN"
        return result
    checkpoint_price = checkpoint[0]
    checkpoint_uses_extreme = behavior_policy == "FALSE_BREAK_RECLAIM_CAUSAL_CHECKPOINT"
    achieved_index = next(
        (
            index
            for index, bar in enumerate(ordered)
            if (
                observed := _positive_number(
                    bar.get("high" if direction == "LONG" else "low")
                    if checkpoint_uses_extreme
                    else bar.get("close")
                )
            ) is not None
            and (
                (direction == "LONG" and observed >= checkpoint_price)
                or (direction == "SHORT" and observed <= checkpoint_price)
            )
        ),
        None,
    )
    result["checkpoint_evidence"] = (
        "CLOSED_BAR_FAVORABLE_EXTREME"
        if checkpoint_uses_extreme
        else "CLOSED_BAR_CLOSE"
    )
    if achieved_index is None:
        if max_wait is not None and len(ordered) >= max_wait:
            result["status"] = "EXPIRED_NO_PROGRESS"
            result["failed_at"] = str(ordered[max_wait - 1].get("time"))
        return result
    result["achieved_at"] = str(ordered[achieved_index].get("time"))
    if behavior_policy in fast_extension_policies:
        favorable_extremes = [
            _positive_number(item.get("high" if direction == "LONG" else "low"))
            for item in ordered
        ]
        if all(value is not None for value in favorable_extremes):
            known = [float(value) for value in favorable_extremes if value is not None]
            best = (
                max(known[: achieved_index + 1])
                if direction == "LONG"
                else min(known[: achieved_index + 1])
            )
            non_extension = 0
            for index in range(achieved_index + 1, len(ordered)):
                value = known[index]
                extended = value > best if direction == "LONG" else value < best
                if extended:
                    best = value
                    non_extension = 0
                    continue
                non_extension += 1
                if non_extension >= 2:
                    result["status"] = "FAILED_HOLD"
                    result["failed_at"] = str(ordered[index].get("time"))
                    result["failure_reason"] = (
                        "Q1_TWO_BARS_WITHOUT_NEW_FAVORABLE_EXTREME"
                        if behavior_policy == "Q1_YIZHI_FAST_CONTINUATION"
                        else "Q2_TWO_BARS_WITHOUT_NEW_FAVORABLE_EXTREME"
                    )
                    return result
    # max_wait_bars validates the entry thesis only until its first declared
    # checkpoint is achieved.  After that milestone the original trigger is
    # no longer a permanent hold line and the entry window must not be started
    # again.  Ongoing exits belong to the subsequently declared structural
    # management plan (defense, stop, or a newly stated behavior condition).
    # Treating the entry trigger as permanent caused a completed Q4 move to be
    # exited by the already-consumed five-bar window on a normal pullback.
    result["status"] = "ACHIEVED"
    return result


def derive_program_behavior_exit_audit(
    position: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
) -> dict[str, Any]:
    """Lock the first course behavior failure and its next-open exit fill.

    The result is independent of analyzer cadence.  A behavior condition is
    evaluated only after each closed bar.  Its exit therefore fills at the
    first later one-minute bar open.  When that bar is not visible yet the
    audit remains ``PENDING_FILL`` and never invents a historical fill.
    """

    now = _aware(as_of, "as_of")
    direction = str(position.get("status") or "")
    entry_time = _optional_aware(position.get("entry_time"))
    entry_price = _positive_number(position.get("entry_price"))
    if direction not in {"LONG", "SHORT"} or entry_time is None or entry_price is None:
        return {
            "version": 1,
            "status": "NOT_APPLICABLE",
            "as_of": now.isoformat(),
        }
    ordered = sorted(
        (
            dict(item)
            for item in bars
            if isinstance(item, Mapping)
            and (at := _optional_aware(item.get("time"))) is not None
            and entry_time <= at <= now
        ),
        key=lambda item: str(item.get("time")),
    )
    latest_behavior = derive_position_behavior_audit(
        position,
        ordered,
        as_of=now.isoformat(),
    )
    result: dict[str, Any] = {
        "version": 1,
        "status": "NOT_TRIGGERED",
        "as_of": now.isoformat(),
        "decision_authority": "PROGRAM",
        "required_position_action": None,
        "direction": direction,
        "setup_key": position.get("active_setup_key"),
        "entry_time": entry_time.isoformat(),
        "entry_price": entry_price,
        "behavior_status": latest_behavior.get("status"),
        "behavior_audit": latest_behavior,
        "reason_code": None,
        "signal_time": None,
        "fill_time": None,
        "fill_price": None,
        "event_id": None,
    }
    failure: dict[str, Any] | None = None
    failure_at: datetime | None = None
    for bar in ordered:
        at = _aware(bar.get("time"), "bar.time")
        audit = derive_position_behavior_audit(
            position,
            ordered,
            as_of=at.isoformat(),
        )
        if audit.get("status") not in {"EXPIRED_NO_PROGRESS", "FAILED_HOLD"}:
            continue
        failure = audit
        failure_at = _optional_aware(audit.get("failed_at")) or at
        break
    if failure is None or failure_at is None:
        return result

    fill_bar = next(
        (
            item
            for item in ordered
            if _aware(item.get("time"), "bar.time") > failure_at
        ),
        None,
    )
    reason_code = (
        "EXPECTED_BEHAVIOR_FAILED_HOLD"
        if failure.get("status") == "FAILED_HOLD"
        else "EXPECTED_BEHAVIOR_EXPIRED_NO_PROGRESS"
    )
    fill_time = str(fill_bar.get("time")) if isinstance(fill_bar, Mapping) else None
    fill_price = _positive_number(fill_bar.get("open")) if isinstance(fill_bar, Mapping) else None
    status = "TRIGGERED" if fill_time is not None and fill_price is not None else "PENDING_FILL"
    event_id = (
        _stable_id(
            "EXIT_FILLED",
            position.get("active_setup_key"),
            position.get("entry_time"),
            reason_code,
            failure_at.isoformat(),
            fill_time,
            fill_price,
        )
        if status == "TRIGGERED"
        else None
    )
    result.update(
        {
            "status": status,
            "required_position_action": "EXIT" if status == "TRIGGERED" else None,
            "behavior_status": failure.get("status"),
            "behavior_audit": failure,
            "reason_code": reason_code,
            "signal_time": failure_at.isoformat(),
            "fill_time": fill_time,
            "fill_price": fill_price,
            "event_id": event_id,
        }
    )
    return result


def build_event_lifecycle(
    previous_state: Mapping[str, Any] | None,
    new_events: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    valid_bars: int = DEFAULT_EVENT_VALID_BARS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now = _aware(as_of, "as_of")
    if valid_bars < 1:
        raise ReplayExecutionGateError("事件有效根數必須大於0。")
    prior_items = previous_state.get("events") if isinstance(previous_state, Mapping) else None
    source_items = prior_items if isinstance(prior_items, list) else []
    by_id: dict[str, dict[str, Any]] = {
        str(item.get("event_id")): dict(item)
        for item in source_items
        if isinstance(item, Mapping) and item.get("event_id")
    }
    raw_by_id: dict[str, dict[str, Any]] = {}
    for raw in new_events:
        if not isinstance(raw, Mapping) or not raw.get("event_id"):
            continue
        event_id = str(raw["event_id"])
        raw_by_id[event_id] = dict(raw)
        occurred = _aware(raw.get("event_time"), "event.event_time")
        expires = occurred + timedelta(minutes=valid_bars)
        item = by_id.get(event_id) or {
            "event_id": event_id,
            "event_type": raw.get("event_type"),
            "event_time": occurred.isoformat(),
            "recorded_at": raw.get("recorded_at") or as_of,
            "valid_bars": valid_bars,
            "expires_at": expires.isoformat(),
            "analyzed": False,
            "analyzed_at": None,
            "consumed_at": None,
            "status": "ACTIVE",
        }
        # Keep the complete causal event while it is waiting for the next AI
        # cadence bar.  A material event can first appear on a hidden one-minute
        # program tick; ``new_events`` will then be empty on the following AI
        # tick, so an id-only lifecycle record is not enough to hand it over.
        item["payload"] = dict(raw)
        if not item.get("analyzed") and now > _aware(item.get("expires_at"), "event.expires_at"):
            item["status"] = "EXPIRED"
        by_id[event_id] = item
    for item in by_id.values():
        if item.get("status") == "ACTIVE" and now > _aware(item.get("expires_at"), "event.expires_at"):
            item["status"] = "EXPIRED"
    active: list[dict[str, Any]] = []
    for event_id, item in by_id.items():
        if item.get("status") != "ACTIVE" or item.get("analyzed"):
            continue
        payload = raw_by_id.get(event_id) or item.get("payload")
        if isinstance(payload, Mapping):
            active.append(dict(payload))
    state = {
        "version": 1,
        "as_of": as_of,
        "events": sorted(by_id.values(), key=lambda item: (str(item.get("event_time")), str(item.get("event_id"))))[-200:],
    }
    active.sort(key=lambda item: (str(item.get("event_time")), str(item.get("event_id"))))
    return state, active


def mark_events_analyzed(
    state: Mapping[str, Any],
    event_ids: Sequence[str],
    *,
    analyzed_at: str,
) -> dict[str, Any]:
    wanted = {str(item) for item in event_ids}
    result = dict(state)
    events: list[dict[str, Any]] = []
    for raw in state.get("events", []) if isinstance(state.get("events"), list) else []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if str(item.get("event_id")) in wanted and item.get("status") == "ACTIVE":
            item.update(
                {
                    "analyzed": True,
                    "analyzed_at": analyzed_at,
                    "consumed_at": analyzed_at,
                    "status": "ANALYZED",
                }
            )
        events.append(item)
    result.update({"as_of": analyzed_at, "events": events})
    return result


def _first_triggered_bar(
    bars: Sequence[Mapping[str, Any]],
    *,
    after: datetime,
    through: datetime,
    operator: str,
    level: float,
) -> dict[str, Any] | None:
    previous_close: float | None = None
    for raw in bars:
        at = _aware(raw.get("time"), "bar.time")
        close = _positive_number(raw.get("close"))
        low = _positive_number(raw.get("low"))
        high = _positive_number(raw.get("high"))
        open_price = _positive_number(raw.get("open"))
        if close is None or low is None or high is None or open_price is None:
            previous_close = close
            continue
        if at <= after:
            previous_close = close
            continue
        if at > through:
            break
        # Once a setup is ARMED, any later closed bar beyond its fixed line is
        # a valid confirmation.  Requiring the immediately preceding close to
        # be on the other side loses setups that become knowable only after an
        # n=2 correction endpoint is confirmed while price is already beyond
        # the line; the next bar remaining beyond it is still causal evidence,
        # not a retroactive fill.  Reclaim operators remain crossing-specific.
        crossed = (
            operator == "CLOSE_ABOVE" and close > level
        ) or (
            operator == "CLOSE_BELOW" and close < level
        ) or (
            operator == "RECLAIM_ABOVE" and low <= level < close and (open_price <= level or previous_close is None or previous_close <= level)
        ) or (
            operator == "RECLAIM_BELOW" and high >= level > close and (open_price >= level or previous_close is None or previous_close >= level)
        )
        if crossed:
            return dict(raw)
        previous_close = close
    return None


def _latest_still_active_trigger(
    bars: Sequence[Mapping[str, Any]],
    *,
    after: datetime,
    through: datetime,
    operator: str,
    level: float,
) -> dict[str, Any] | None:
    """Return a trigger only when its direction still holds at the cutoff.

    Sparse two-bar analysis can first observe a valid close trigger together
    with a later closed bar that has already reclaimed the line against the
    setup.  That old trigger was never actionable by the analyzer and must not
    be queued for a future fill.  Continue scanning after an invalidated
    signal so a genuinely new trigger later in the same sparse window can
    still qualify.
    """

    cursor = after
    while True:
        signal = _first_triggered_bar(
            bars,
            after=cursor,
            through=through,
            operator=operator,
            level=level,
        )
        if signal is None:
            return None
        signal_time = _aware(signal.get("time"), "signal.time")
        later_closes = [
            _positive_number(item.get("close"))
            for item in bars
            if isinstance(item, Mapping)
            and (at := _optional_aware(item.get("time"))) is not None
            and signal_time < at <= through
        ]
        above = operator in {"CLOSE_ABOVE", "RECLAIM_ABOVE"}
        still_active = all(
            close is not None and (close > level if above else close < level)
            for close in later_closes
        )
        if still_active:
            return signal
        cursor = signal_time


def _empty_entry_gate(now: datetime) -> dict[str, Any]:
    return {
        "version": 1,
        "status": "NONE",
        "event_id": None,
        "setup_key": None,
        "setup_name": None,
        "direction": None,
        "entry_role": None,
        "trigger_operator": None,
        "trigger_level": None,
        "signal_time": None,
        "signal_close": None,
        "eligible_from": None,
        "valid_bars": None,
        "expires_at": None,
        "evaluated_at": now.isoformat(),
    }


def _stable_id(*parts: Any) -> str:
    material = "\n".join(str(item) for item in parts).encode("utf-8")
    return "EG-" + hashlib.sha256(material).hexdigest()[:20]


def _valid_bars(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
        return 3
    return value


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        return None
    return float(value)


def _optional_aware(value: Any) -> datetime | None:
    try:
        return _aware(value, "time")
    except ReplayExecutionGateError:
        return None


def _aware(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ReplayExecutionGateError(f"{field}必須是含時區時間。")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReplayExecutionGateError(f"{field}不是有效時間。") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ReplayExecutionGateError(f"{field}必須包含時區。")
    return result
